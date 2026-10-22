"""Legislators' names from malegislature.gov, to check the spelling of legislators in a draft (flag-only).

Approved by the project owner. It reads public pages only: the House and Senate member lists, and the
member page of the committee holding the hearing (named on the hearing page). It never changes a name: a
legislator's name that isn't on the list gets a ⚠ with the closest spelling, and the committee's members are
listed on the review page to help name a questioner. Public witnesses aren't legislators, so their names
aren't checked. If the pages can't be loaded, the draft is simply not checked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import difflib
import html
import http.client
import json
import logging
from pathlib import Path
import re
import unicodedata
import urllib.request
import uuid

from .pipeline import InputError
from .rich_review import lead_flag
from .tls import context as tls_context

SITE = "https://malegislature.gov"
MAX_PAGE_BYTES = 4 * 1024 * 1024
KEEP = timedelta(days=7)  # Membership rarely changes; a week-old list is fine for spelling.
TITLED = re.compile(r"\b(?:representative|senator|rep|sen)\b\.?", re.I)
# Words that name a role, not a person: "Senate Chair", "Vice Chair", "Madam Chair", "Committee Member".
ROLE = {"committee", "member", "members", "chair", "chairs", "chairman", "chairwoman", "chairperson", "co", "vice",
        "madam", "mister", "mr", "mrs", "ms", "dr", "house", "senate", "state", "legislator", "clerk", "staff",
        "aide", "the", "a", "an", "of", "and", "for", "to"}
UNSEEN = {"unknown", "unclear", "unidentified"}  # Not a name at all: the speaker wasn't identified.
ELSEWHERE = re.compile(r"\b(?:u\.?\s?s\.?|united states|congress\w*|former|ex)\b", re.I)  # Not in this Legislature.
CLOSE = 0.75  # Surname similarity for "did you mean": Smyth/Smith 0.8, Delano/Delaney 0.77.
LEGISLATOR = "The Legislature's member list has {name}; check the spelling."
NOT_LISTED = "No legislator with this surname is on the Legislature's member list; check the name."


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "HearingCompiler/1"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=tls_context()) as response:
            page = response.read(MAX_PAGE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise InputError(f"Could not load {url}: {getattr(error, 'reason', error)}") from None
    if len(page) > MAX_PAGE_BYTES:
        raise InputError(f"{url} is unexpectedly large.")
    return page.decode("utf-8", errors="replace")


def parse_members(page: str, branch: str) -> list[dict]:
    """The House or Senate member list: each row's "Follow in MyLegislature: <name>" and profile code."""
    members = []
    for row in re.split(r"<tr[\s>]", page):  # One legislator per table row.
        name = re.search(r'aria-label="Follow in MyLegislature: ([^"]+)"', row)
        code = re.search(r'href="/Legislators/Profile/([A-Za-z0-9_]+)"', row)
        if name and code:
            members.append({"name": " ".join(html.unescape(name.group(1)).split()), "code": code.group(1),
                            "branch": branch})
    return members


def parse_committee_list(page: str) -> dict:
    """Committee names (as on the list, like "Housing") to codes, lower-cased."""
    return {" ".join(html.unescape(name).split()).lower(): code for code, name in
            re.findall(r'href="/Committees/Detail/([A-Za-z0-9_]+)"[^>]*>([^<]+)<', page)}


def parse_committee_members(page: str) -> list[dict]:
    """A committee's members page: each name with its role (Chair, Vice Chair), by branch."""
    members = []
    for branch, part in re.findall(r"<h2>\s*(Senate|House) Members\s*</h2>(.*?)(?=<h2>|$)", page, re.S | re.I):
        for code, name, role in re.findall(r'href="/Legislators/Profile/([A-Za-z0-9_]+)">([^<]+)</a>\s*'
                                           r'(?:<br\s*/?>\s*<span>([^<]*)</span>)?', part):
            members.append({"name": " ".join(html.unescape(name).split()), "code": code, "branch": branch.title(),
                            "role": " ".join(role.split()) or None})
    return members


class Cache:
    """What was read from each page, kept for a week in the workspace, so each hearing doesn't fetch it again."""

    def __init__(self, path: Path | None):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
        except (OSError, ValueError):
            self.data = {}

    def read(self, url: str, parse, get=None):
        """parse(page) for url, from the last week's reading if there is one. Only the result is kept."""
        kept = self.data.get(url)
        if isinstance(kept, dict) and "data" in kept and get is None:
            try:
                if datetime.now(timezone.utc) - datetime.fromisoformat(kept["fetched_utc"]) < KEEP:
                    return kept["data"]
            except (KeyError, TypeError, ValueError):
                pass
        data = parse((get or _get)(url))
        if data:  # An empty reading (a changed page) is asked again next time, not kept for a week.
            self.data[url] = {"data": data, "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        return data

    def save(self):
        if self.path is None:
            return
        temporary = self.path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self.data), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            logging.exception("Could not save the legislator list; it will be fetched again")
        finally:
            if temporary.exists():
                temporary.unlink()


def fetch(committee: str | None, cache_path: Path | None = None, get=None) -> dict:
    """{"legislators": [...], "committee": {"name", "url", "members"} or None}. Raises InputError if the member
    lists can't be loaded; a committee that can't be found is just left out."""
    cache = Cache(cache_path)
    legislators = cache.read(f"{SITE}/Legislators/Members/House", lambda page: parse_members(page, "House"), get) + \
        cache.read(f"{SITE}/Legislators/Members/Senate", lambda page: parse_members(page, "Senate"), get)
    if len(legislators) < 100:  # 160 representatives and 40 senators; fewer means the page changed.
        raise InputError("The Legislature's member lists didn't look as expected.")
    found = None
    match = re.match(r"(Joint|House|Senate) Committee on (.+)", committee or "")
    if match:
        try:
            codes = cache.read(f"{SITE}/Committees/{match.group(1)}", parse_committee_list, get)
            code = codes.get(match.group(2).strip().lower())
            if code:
                url = f"{SITE}/Committees/Detail/{code}/Members"
                members = cache.read(url, parse_committee_members, get)
                found = {"name": committee, "url": url, "members": members} if members else None
        except InputError as error:
            logging.warning("The committee's member page couldn't be loaded: %s", error)
    if len(legislators) >= 100 and get is None:
        cache.save()
    return {"legislators": legislators, "committee": found,
            "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _unbracketed(name) -> str:
    return re.sub(r"\([^)]*\)", " ", name if isinstance(name, str) else "")


def _plain(name) -> list[str]:
    text = unicodedata.normalize("NFKD", _unbracketed(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = TITLED.sub(" ", text)
    words = re.sub(r"[^a-z' -]+", " ", text.lower().replace("’", "'")).split()
    return [w for w in words if w not in ("jr", "sr", "ii", "iii", "iv")]


def _surnames(words) -> set[str]:
    """A surname's spellings: "holt-lane" also as "holt lane", and "lowell barrett" as hyphenated."""
    if not words:
        return set()
    last = words[-1]
    found = {last, last.replace("-", "")} | set(last.split("-")[-1:])
    if len(words) >= 2:
        found |= {f"{words[-2]}-{last}", f"{words[-2]}{last}"}
    return {w for w in found if w}


def suggestion(name, legislators) -> tuple[str, str | None]:
    """("ok" | "close" | "missing" | "skip", the closest legislator's name for "close")."""
    words = _plain(name)
    if not words or UNSEEN & set(words) or ELSEWHERE.search(_unbracketed(name)):
        return "skip", None
    words = [w for w in words if not all(part in ROLE for part in w.split("-") if part)]  # "co-chair" too.
    if not words or len(words[-1].replace("-", "")) < 3:
        return "skip", None  # A role, not a person, or a surname too short to compare.
    surnames = {}
    for person in legislators:
        for spelling in _surnames(_plain(person["name"])):
            surnames.setdefault(spelling, person["name"])
    if _surnames(words) & set(surnames):
        return "ok", None
    surname = words[-1]
    close = max(((difflib.SequenceMatcher(None, surname, s).ratio(), full) for s, full in surnames.items()),
                default=(0, None))
    return ("close", close[1]) if close[0] >= CLOSE else ("missing", None)


def check(draft: dict, roster: dict) -> int:
    """Flags legislators' names (titled testifiers and respondents, and every named questioner) that aren't on the
    member list. Returns how many were flagged. Values and reviews never change."""
    legislators = roster.get("legislators") or []
    if not legislators:
        return 0
    flagged = 0

    def look(item, always):
        nonlocal flagged
        name = item.get("value") if isinstance(item, dict) else None
        if not name or not (always or TITLED.search(_unbracketed(name))):  # "aide to Representative Hale" isn't titled.
            return
        outcome, closest = suggestion(name, legislators)
        message = LEGISLATOR.format(name=closest) if outcome == "close" else NOT_LISTED if outcome == "missing" else None
        if message:
            item["flag"] = lead_flag(message, item.get("flag"))
            flagged += 1
    questions = list(draft.get("unassigned_questions") or [])
    for unit in draft.get("units") or []:
        for person in unit.get("testifiers") or []:
            look(person.get("name"), False)
        questions += unit.get("questions") or []
    for question in questions:
        look((question.get("asker") or {}).get("name"), True)  # Questioners are committee members.
        for response in question.get("responses") or []:
            look((response.get("respondent") or {}).get("name"), False)
    return flagged


# Shadow mode. A rule that would clear a name's ⚠ only marks the item; nothing she sees
# changes. Her later decisions on marked items show whether the rule is certain enough to turn on: a marked item
# she edits is a wrong clear. The rule settles spelling only, never who spoke: on one real hearing, 6 of the 11 ⚠ an
# exact-match rule would have cleared were "who asked is inferred", which a member list can't settle.
SPELLING = re.compile(r"spoken as|transcript (?:says|has|spells)|spell|misheard|heard (?:as|once)|transcribed as|"
                      r"sounds like|agenda confirms|sponsor list|verified against", re.I)
NOT_SPELLING = re.compile(r"infer|identif|address|not named|unnamed|unclear|unknown|who (?:asked|answered|spoke)|"
                          r"may (?:be|have)|possibl|perhaps|not stated|not said|second check|audio check|member list|"
                          r"not found|confirm by listening", re.I)
SHADOW_RULE = "member-list spelling"


def _full(name) -> str:
    """A name for exact comparison: no title, role, accents, initials or punctuation."""
    return " ".join(w for w in _plain(name) if w not in ROLE and len(w.strip("'-")) > 1)


def shadow_confirm(draft: dict, legislators, sponsors=()) -> int:
    """Marks (item["would_clear"]) the name ⚠ this rule would clear: every sentence of the flag is a spelling
    doubt, and the name exactly matches a full name on the member list or the agenda's sponsors. Returns how many.
    Flags, values and reviews are untouched."""
    official = {}
    for name in [p["name"] for p in legislators or []] + [s for s in sponsors if isinstance(s, str)]:
        if _full(name):
            official.setdefault(_full(name), name)
    marked = 0

    def look(item):
        nonlocal marked
        flag = item.get("flag") if isinstance(item, dict) else None
        if not flag or item.get("review") == "APPROVED" or not item.get("value"):
            return
        # Sentences end at ";" or at a word's full stop, not an initial's ("Dana R. Whitcomb").
        sentences = [part for part in re.split(r"(?<=;)\s+|(?<=[a-z0-9]{2}[.!?])\s+", flag) if part.strip()]
        if not sentences or not all(SPELLING.search(part) and not NOT_SPELLING.search(part) for part in sentences):
            return
        matched = official.get(_full(item["value"]))
        if matched:
            item["would_clear"] = {"rule": SHADOW_RULE, "matched": matched}
            marked += 1
    questions = list(draft.get("unassigned_questions") or [])
    for unit in draft.get("units") or []:
        for person in unit.get("testifiers") or []:
            look(person.get("name"))
        questions += unit.get("questions") or []
    for question in questions:
        look((question.get("asker") or {}).get("name"))
        for response in question.get("responses") or []:
            look((response.get("respondent") or {}).get("name"))
    return marked

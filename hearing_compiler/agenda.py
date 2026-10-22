"""Official agenda (bill numbers, titles, sponsors) from a public malegislature.gov hearing page.

The agenda lets drafting normalize spoken bill numbers and titles. It never
establishes who testified or their positions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import html
import http.client
import re
import urllib.request

from .pipeline import InputError
from .tls import context as tls_context

HEARING_URL = re.compile(r"https://(?:www\.)?malegislature\.gov/Events/Hearings/Detail/(\d{1,7})/?(?:[?#].*)?")
MAX_PAGE_BYTES = 4 * 1024 * 1024


def check_url(url) -> str | None:
    """A normalized hearing-page link, None when empty; rejects anything else."""
    if url is None or (isinstance(url, str) and not url.strip()):
        return None
    match = HEARING_URL.fullmatch(url.strip()) if isinstance(url, str) else None
    if not match:
        raise InputError("Use the hearing's page on malegislature.gov, like "
                         "https://malegislature.gov/Events/Hearings/Detail/1234, or leave the link empty.")
    return f"https://malegislature.gov/Events/Hearings/Detail/{match.group(1)}"


def parse(page: str) -> list[dict]:
    bills, seen = [], set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())
                 for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) >= 2 and re.fullmatch(r"[HS]\.\d{1,5}", cells[0]) and cells[0] not in seen:
            seen.add(cells[0])
            bills.append({"bill": cells[0], "title": cells[1][:500], "sponsor": (cells[2] if len(cells) > 2 else "")[:200]})
    return bills


def committee_name(page: str) -> str | None:
    """The committee on a hearing page's heading, like "Joint Committee on Housing"."""
    match = re.search(r'id="hearingTitle"[^>]*>(.*?)</h1>', page, re.S | re.I)
    text = " ".join(html.unescape(re.sub(r"<span[^>]*>.*?</span>|<[^>]+>", " ", match.group(1), flags=re.S)).split()) \
        if match else ""
    return text if re.match(r"(?:Joint|House|Senate) Committee on ", text) else None


def fetch(url: str) -> dict:
    url = check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "HearingCompiler/1"})
    try:
        with urllib.request.urlopen(request, timeout=20, context=tls_context()) as response:
            page = response.read(MAX_PAGE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:  # Includes a cut-off or garbled page.
        raise InputError(f"Could not load the hearing page: {getattr(error, 'reason', error)}") from None
    if len(page) > MAX_PAGE_BYTES:
        raise InputError("The hearing page is unexpectedly large; its agenda was not used.")
    text = page.decode("utf-8", errors="replace")
    bills = parse(text)
    if not bills:
        raise InputError("No bills were found on that hearing page.")
    return {"url": url, "bills": bills, "committee": committee_name(text),
            "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}

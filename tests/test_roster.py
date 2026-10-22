"""The flag-only legislator name check, from synthetic copies of malegislature.gov's page markup."""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

from hearing_compiler import agenda, extraction, roster
from hearing_compiler.pipeline import InputError
from hearing_compiler.store import Store
from test_gemini_extraction import KEY, Client, Keys, drafted, hearing, morgan, RIVERS


def member_row(name, code):
    return (f'<tr><td><a aria-label="Follow in MyLegislature: {name}" class="star"></a></td>'
            f'<td><a href="/Legislators/Profile/{code}"><img alt=""></a></td><td>8th District</td></tr>')


HOUSE = "<table>" + "".join(member_row(f"Member{n} Person{n}", f"M{n}") for n in range(150)) + \
    member_row("Morgan Ellery", "C_H1") + member_row("Jordan Hale", "JXD1") + "</table>"
SENATE = "<table>" + member_row("Sam Ort&#xED;z", "AXG0") + member_row("Casey O&#x27;Neill", "PMO") + "</table>"
JOINT = '<ul><li><a href="/Committees/Detail/J19">Housing</a></li><li><a href="/Committees/Detail/J28">Labor and Workforce Development</a></li></ul>'
MEMBERS = ('<h2>Senate Members</h2><ul><li><a href="/Legislators/Profile/PMO">Casey M. O&#x27;Neill</a> <br />'
           ' <span>Chair</span></li></ul><h2>House Members</h2><ul><li><a href="/Legislators/Profile/JXD1">Jordan Hale'
           '</a> <br /> <span>Vice Chair</span></li><li><a href="/Legislators/Profile/C_H1">Morgan Ellery</a>'
           '</li></ul>')
PAGES = {f"{roster.SITE}/Legislators/Members/House": HOUSE, f"{roster.SITE}/Legislators/Members/Senate": SENATE,
         f"{roster.SITE}/Committees/Joint": JOINT, f"{roster.SITE}/Committees/Detail/J19/Members": MEMBERS}


class RosterTests(unittest.TestCase):
    def found(self):
        return roster.fetch("Joint Committee on Housing", get=PAGES.__getitem__)

    def test_the_hearing_page_names_its_committee(self):
        page = ('<h1 id="hearingTitle"><span class="subTitle">Hearing Details </span>Joint Committee on Housing'
                '</h1>')
        self.assertEqual(agenda.committee_name(page), "Joint Committee on Housing")
        self.assertIsNone(agenda.committee_name('<h1 id="hearingTitle">Hearing Details</h1>'))

    def test_member_lists_and_the_committee_page_are_read_with_their_names_as_written(self):
        found = self.found()
        names = {p["name"] for p in found["legislators"]}
        self.assertIn("Sam Ortíz", names)
        self.assertIn("Casey O'Neill", names)
        self.assertIn("Morgan Ellery", names)  # Codes may contain an underscore.
        self.assertEqual([(m["name"], m["role"], m["branch"]) for m in found["committee"]["members"]],
                         [("Casey M. O'Neill", "Chair", "Senate"), ("Jordan Hale", "Vice Chair", "House"),
                          ("Morgan Ellery", None, "House")])
        with self.assertRaisesRegex(InputError, "didn't look as expected"):
            changed = {**PAGES, f"{roster.SITE}/Legislators/Members/House": "<table></table>"}
            roster.fetch("Joint Committee on Housing", get=changed.__getitem__)

    def test_only_legislators_names_are_checked_and_only_flagged(self):
        legislators = self.found()["legislators"]
        cases = {"Representative Morgan Elleray": ("close", "Morgan Ellery"),
                 "Senator Sam Ortiz": ("ok", None), "Sen. Casey M. O’Neill": ("ok", None),
                 "Representative Zzyzx": ("missing", None), "Committee Member": ("skip", None),
                 "Unidentified": ("skip", None)}
        for name, expected in cases.items():
            self.assertEqual(roster.suggestion(name, legislators), expected, name)
        entry = morgan("H.1111", bill_check=None, testifiers=[
            {"name": "Representative Morgan Elleray", "organization": None,
             "quote": "I'm Alex Morgan with Example Legal Services", "cues": [2]},
            {"name": "Dana Elleray", "organization": None, "quote": "I'm Alex Morgan with Example Legal Services",
             "cues": [2]}])  # A public witness with a near-miss surname isn't a legislator: not checked.
        draft = drafted(hearing(*RIVERS), [entry])
        values = [p["name"]["value"] for p in draft["units"][0]["testifiers"]]
        self.assertEqual(roster.check(draft, {"legislators": legislators}), 1)
        person = draft["units"][0]["testifiers"][0]["name"]
        self.assertTrue(person["flag"].startswith("The Legislature's member list has Morgan Ellery"))
        self.assertNotIn("member list", draft["units"][0]["testifiers"][1]["name"].get("flag") or "")
        self.assertEqual([p["name"]["value"] for p in draft["units"][0]["testifiers"]], values)

    def test_the_draft_keeps_the_committee_and_a_failed_fetch_just_skips_the_check(self):
        with tempfile.TemporaryDirectory() as folder:
            keys = Keys()
            keys.set(KEY)
            service = extraction.ExtractionService(Store(Path(folder)), threading.Event(), keys, Client())
            asked = []
            service.fetch_roster = lambda committee, path: asked.append((committee, path.name)) or self.found()
            draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)])
            service.check_names(draft)
            self.assertEqual(asked, [])  # No committee on the (fake) hearing page: nothing fetched.
            draft["extraction_provenance"]["committee"] = "Joint Committee on Housing"
            service.check_names(draft)
            self.assertEqual(asked, [("Joint Committee on Housing", "legislators.json")])
            self.assertEqual(len(draft["roster"]["committee"]["members"]), 3)

            def offline(committee, path):
                raise InputError("Could not load the member list: offline")
            service.fetch_roster = offline
            fresh = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)])
            fresh["extraction_provenance"]["committee"] = "Joint Committee on Housing"
            service.check_names(fresh)
            self.assertNotIn("roster", fresh)

    def test_what_was_read_is_kept_for_a_week_but_not_the_pages_or_an_empty_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legislators.json"
            cache, calls = roster.Cache(path), []
            original = roster._get
            roster._get = lambda url: calls.append(url) or "<p>page</p>"
            try:
                self.assertEqual(cache.read("https://example.org/a", lambda page: ["read"]), ["read"])
                cache.read("https://example.org/b", lambda page: [])
                cache.save()
                again = roster.Cache(path)
                self.assertEqual(again.read("https://example.org/a", lambda page: ["new"]), ["read"])
                again.read("https://example.org/b", lambda page: [])
            finally:
                roster._get = original
            self.assertEqual(calls, ["https://example.org/a", "https://example.org/b", "https://example.org/b"])
            self.assertNotIn("<p>", path.read_text(encoding="utf-8"))

    def test_roles_unidentified_speakers_and_other_legislatures_are_not_checked_and_hyphens_match(self):
        legislators = self.found()["legislators"] + [{"name": "Jamie Lowell-Barrett", "branch": "House"},
                                                     {"name": "Avery Holt Lane", "branch": "House"}]
        for name in ("Senate Chair", "Vice Chair", "Co-Chair", "Madam Chair", "Unknown legislator",
                     "Unidentified Senate member", "U.S. Representative Jordan Pierce", "Former Senator Dan Whitcomb",
                     "Committee Member"):
            self.assertEqual(roster.suggestion(name, legislators), ("skip", None), name)
        for name in ("Representative Jamie Lowell Barrett", "Representative Avery Holt-Lane", "Chair Hale"):
            self.assertEqual(roster.suggestion(name, legislators), ("ok", None), name)
        draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None, testifiers=[
            {"name": "Jane Roe (aide to Representative Hale)", "organization": None,
             "quote": "I'm Alex Morgan with Example Legal Services", "cues": [2]}])])
        self.assertEqual(roster.check(draft, {"legislators": legislators}), 0)  # An aide isn't titled.


if __name__ == "__main__":
    unittest.main()

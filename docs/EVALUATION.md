# Evaluation

How the app was tested on real public hearings, what the numbers show, and what they don't prove.

Every number here comes from **real public hearings**: Massachusetts legislative committee hearings, whose
recordings and agendas are public. Hearings are labelled A–C instead of by name. Synthetic tests (unit
tests and the browser check) cover behaviour, not accuracy, and aren't counted here.

| Hearing | Length | Notes |
|---|---|---|
| A | 2 h 03 min | Used for the audio check |
| B | 2 h 30 min, 52 agenda bills | Fresh hearing; every unflagged field checked |
| C | 4 h 2 min (34,663 words) | Long hearing; drafted in parts; three runs. The user reviewed its first 2 hours |

## What "good" means

These targets were set with the project owner before the precision work started:

1. **Recall first.** Errors that reach the notes should carry a ⚠. **Zero** wrong legislator names, bills
   or positions without one.
2. **No missed testimony without a warning.**
3. **Precision as high as recall allows.** A ⚠ costs the reviewer a listen, but a missing ⚠ costs
   accuracy, so precision is never bought with recall.
4. **Don't target the number of ⚠ or review minutes.** Track them, but a check rewarded for hitting a count
   learns to invent flags.
5. Hands-off runs: at least 9 of 10. End-to-end time: 45 minutes or less per 2-hour hearing. Cost:
   $200 or less for a year's backlog.

## 1. Can the reader find the evidence? (quote grounding)

The model must quote the transcript **verbatim** for every item. Code, not the model, finds each quote and
keeps its timestamp, so ▶ plays the right moment. A quote that can't be found is flagged.

| Run | Quotes located |
|---|---|
| Hearing B, one call | 232 of 233 (99.6%) |
| Hearing C, run 1 (drafted in parts) | 635 of 642 (98.9%) |
| Hearing C, run 2 | 592 of 617 (95.9%) |
| Hearing C, run 3 | 610 of 618 (98.7%) |

**A defect this found:** the model sometimes joined two passages with "…". Each piece was verbatim, but the
joined quote wasn't. On one draft that was 30 of 33 unfound quotes. The fix accepts stitched quotes whose
pieces appear in order within a few minutes. That draft went from 262 to 289 of 311 located, 27 fewer
"quote not found" ⚠, with no timestamp moved.

## 2. Are the unflagged parts right? (the trust question)

On hearing B, **every unflagged field** (284) was checked against the transcript by two independent AI
reviewers:

| Verdict | Fields |
|---|---|
| Correct | 235 |
| Minor wording only | 39 |
| Transcript itself garbled | 3 |
| Can't verify | 1 |
| **Wrong** | **6 (about 2%)** |

- **Names, organizations, bills, titles and all 38 positions:** no material errors.
- **The 6 errors were all in arguments and question text, and 5 of them were overstatement:** one case
  turned into a pattern, "attempted to harm" turned into "harm", a dropped hedge, "sent to jail" turned
  into "lifetime incarceration".

That finding drove the next check.

## 3. The flag-only second check

A second AI pass reads each testimony against its transcript lines and reports items that are
overstated, not said, misattributed or changed, each with a verbatim quote. It never edits anything.
Code discards any finding whose quote isn't in the transcript.

It was tested before release on two hearings with known errors:

| | Result |
|---|---|
| Known errors caught | **11 of 13** (5 of 6 on hearing B; 6 of 7 on another hearing) |
| Quotes verified in the transcript | 36 of 36 flags, 59 of 59 omissions |
| False alarms | 2 on the second hearing; both led to rule changes before release |
| Cost and time | about 25 s per hearing, 4 calls at a time; about $0.10–0.15 per 2-hour hearing |

The two false alarms:
- **A bill number:** the transcript said 1064 where the agenda confirmed 1065. The check now never trades
  an agenda bill number for a misheard one.
- **A name "not stated":** the transcript window was too short to include the chair's introduction. The
  window is now wider.

## 4. The audio check

The transcript is machine-made, and no text check can catch a misheard number. So for unflagged names and
statements with figures, the app cuts the seconds they come from out of the recording, in plain Python,
and asks the model what is said there. A "different" answer adds a ⚠ quoting what it heard.

| Hearing A | Result |
|---|---|
| Items heard | 58 (39 names, 19 statements with figures), in 35 calls and 33 s |
| Flags | **1, and it was a true error.** The speaker gave three figures (40% don't leave, 71% report threats, 48% delay leaving); the draft merged them into "up to 40% … delay leaving". The model heard 48%. |

The transcript was right in that case; the drafting was wrong, in an item nobody had flagged.

## 5. Long hearings and model variation

Hearings over about 3.5 hours are drafted in parts. Cuts land near even sizes, on the chair's hand-offs
("Next we have…", "Move on now to…").

| Hearing C | Run 1 | Run 2 (after the fix) |
|---|---|---|
| Calls | 4 (one ran out of room and was split again) | 3 |
| Model time | 807 s | 370 s |
| Reply tokens | 136k | 65k |
| Cuts | 1 clean, 1 through a testimony | both clean |

**Run-to-run variation is real.** The same transcript gave 79, 68 and 73 testimonies across three runs.
- **Skipped testimonies:** one run listed the bills for 5 testimonies but drafted nothing for them.
- **The fix:** the app now redrafts just those lines, with one more try if that also comes back empty. If
  it's still empty, a clear "a testimony may be missing at [time]" warning stays.
- **Evidence:** sending one skipped testimony's lines alone, three times, returned it all three times.

## 6. Precision: how many ⚠ were real?

From the user's own review log of the first 2 hours of hearing C (on an earlier build). A ⚠ counts as
"real" when she changed the item:

| Kind of ⚠ | Count | Changed |
|---|---|---|
| Hearing-wide warnings | 21 | 0 (most were removed by a later fix) |
| Name spellings the model had already corrected | 19 | 0 |
| Other notes on arguments | 17 | 0 |
| Other doubts about a name | 9 | 3 |
| Questioner inferred | 8 | 2 |
| Bill unclear | 2 | 2 |
| **All** | **78** | **7 (9%)** |

**What we do with this:** a check may remove a caution ⚠ only when it's **certain**.
1. It settles only the doubt it verifies: a member list proves spelling, never who spoke.
2. It uses official lists, or two sources that agree.
3. It runs in **shadow mode** first.
4. It goes live only after zero wrong clears on 50 or more items.

**What shadow mode found:**
- **A looser rule would have been wrong.** An exact-name rule would have cleared 11 ⚠ that she kept
  unchanged, which looks perfect. But 6 of them were "who asked is inferred", a doubt a list can't settle.
- **The strict rule clears 3, and she kept all 3.**

## 7. The legislator name check

Names of legislators are checked against the Legislature's public member lists, and the committee's
members are shown on the review screen. On three real drafts: 196 legislators read, **0 false flags.**

It honestly couldn't have caught the user's own name fixes: all of those were public witnesses, who aren't
on any list. Its real value showed in naming questioners, which she had looked up by hand.

## 8. From real use

- **Review time:** about 20 minutes for a 2-hour hearing, from the user's own report.
- **How she reviews:** she listens at every ⚠, approves everything unflagged at the end, and doesn't edit
  the notes afterwards. So the copied note is effectively final, which is why recall comes first.
- **Her one complaint, timestamps landing in the wrong place, traced to two causes:**
  - points often start before the quoted words, so −10 s / +10 s buttons were added;
  - stitched quotes (section 1).

## What this doesn't prove

- **It's a handful of hearings and one reviewer.** It's evidence for this workflow, not a benchmark.
- **"Wrong" in section 2 was judged against the transcript** by AI reviewers. Errors in the transcript
  itself need the audio, which is what the audio check is for.
- **"She kept it" doesn't prove an item was right.** She also approved real errors unflagged. That's why
  recall is measured separately, by spot-checks.
- **Model behaviour drifts between runs and versions,** so these numbers are re-measured, not assumed.

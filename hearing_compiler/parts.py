"""Where to cut a hearing's transcript when one Gemini call can't draft all of it.

Only used when it's needed: over the word limit, or after Gemini runs out of room for its reply. A hearing
that fits is drafted in one call, exactly as before. Cuts land near even word counts, at the longest pause
or a chair's hand-off ("Next we have ...") nearby, so a testimony rarely runs through one.
"""
from __future__ import annotations

import math
import re

from . import gemini

# About 1.4 h of speech. On a real 4-hour hearing a 17,300-word part with many short testimonies needed more than
# Gemini's 65,536 reply tokens (about 3.8 per word), so parts are kept well under that.
PART_WORDS = 12000
SEARCH = 0.08  # A cut may move this share of the words either way to land on a pause or a hand-off.
MAX_PARTS = 12  # About a day of speech; anything longer is more likely a wrong file than a hearing.
MIN_CUES = 40  # A range shorter than this isn't split again.
# A chair moving on: "Next we have", "Up now is", "We'll move now to", "Move on now to". Not "next year".
HANDOFF = re.compile(r"\b(?:next (?:we have|we'll|we will|up|is|on|will be|panel|speaker|witness|person|group|bill|"
                     r"testif\w*)|up next|up now|call(?:ing)? (?:up|on|forward)|invite|please come (?:up|forward)|"
                     r"we(?:'ll| will) (?:now )?(?:hear|move|go|turn) (?:to|from|now)|mov(?:e|ing) (?:on )?(?:now )?to)\b",
                     re.I)
PAUSE = 3.0  # A longer silence at a cut may be a change of speaker.


def needed(cues) -> int:
    """How many parts a transcript needs: 1 when it fits in one call."""
    words = sum(len(c["text"].split()) for c in cues)
    return 1 if words <= gemini.MAX_WORDS else max(2, math.ceil(words / PART_WORDS))


def handoff(cue) -> bool:
    return bool(HANDOFF.search(cue["text"]))


def _timed(cue) -> bool:
    return all(isinstance(cue.get(k), (int, float)) and not isinstance(cue.get(k), bool) for k in ("start", "end"))


def split(cues, count) -> list[list[dict]]:
    """cues cut into count consecutive parts (fewer if there are too few lines)."""
    count = max(1, min(count, len(cues) // 2 or 1))
    before = [0]
    for cue in cues:
        before.append(before[-1] + len(cue["text"].split()))
    total, cuts = before[-1], []
    for i in range(1, count):
        target, start = total * i / count, (cuts[-1] + 1) if cuts else 1
        allowed = range(start, len(cues) - (count - i) + 1)
        near = [j for j in allowed if abs(before[j] - target) <= SEARCH * total / count]
        if not near:
            near = [min(allowed, key=lambda j: abs(before[j] - target))]

        def pause(j):
            return (max(0.0, cues[j]["start"] - cues[j - 1]["end"])
                    if 0 < j < len(cues) and _timed(cues[j]) and _timed(cues[j - 1]) else 0.0)

        def score(j):
            # A hand-off beats any pause: on a real hearing the longest pause nearby was a 30 s gap inside a testimony. A
            # hand-off and the pause after it go together, so the next part starts with the chair's introduction.
            if handoff(cues[j]):
                return 1, pause(j) + pause(j + 1), -abs(before[j] - target)
            return 0, pause(j), -abs(before[j] - target)
        cuts.append(max(near, key=score))
    edges = [0] + cuts + [len(cues)]
    return [cues[a:b] for a, b in zip(edges, edges[1:])]


def moved_on(cues, first, procedural=(), since=0) -> bool:
    """Whether the chair moved on at a cut (first: the cue number that starts the later part): a hand-off in the
    lines around it, or chair procedure there. since: where the testimony before the cut began, so its own
    introduction doesn't count."""
    if any(handoff(cues[n - 1]) for n in range(max(1, first - 3, since + 1), min(len(cues), first + 1) + 1)):
        return True
    return any(isinstance(p.get("start_cue"), int) and isinstance(p.get("end_cue"), int)
               and p["start_cue"] <= first + 1 and p["end_cue"] >= first - 3 for p in procedural)


def continues(cues, first, procedural=(), since=0) -> bool:
    """Whether speech likely goes on across a cut: the chair didn't move on and there's no long pause. Only then
    may a later part's unnamed testimony be joined to the one before it."""
    if moved_on(cues, first, procedural, since):
        return False
    before, after = cues[first - 2], cues[first - 1]
    return not (_timed(before) and _timed(after) and after["start"] - before["end"] > PAUSE)

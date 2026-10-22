# Decisions and lessons

The calls that shaped the app, why they were made, and what they cost.

## Decisions

### The AI drafts and flags; it never decides
- **Decision:** model output is a draft. No check may change a value or approve an item. People approve
  every field, or approve everything unflagged at once.
- **Why:** a confident wrong name or position in the notes is worse than no note. The reviewer must be
  able to trust anything without a ⚠.
- **Cost:** some listening the model could in principle have saved.

### Code owns citations
- **Decision:** the model must quote the transcript verbatim for every item. Code finds the quote and keeps
  its timestamp; the model's own line numbers are ignored. A quote that can't be found is flagged.
- **Why:** models misreport where things are said, and ▶ has to land on the right moment.
- **Cost:** strict matching flags paraphrased quotes. One stitched-quote pattern had to be allowed
  explicitly.

### Recall before precision
- **Decision:** errors reaching the notes must carry a ⚠, and names, bills and positions have zero
  tolerance. Fewer false ⚠ comes second.
- **Why:** the reviewer listens at every ⚠ but approves the rest without listening. A false ⚠ costs a
  minute; a missing ⚠ costs accuracy.
- **Cost:** a low starting precision (9%), now being raised carefully (below).

### A check may clear a ⚠ only when it's certain
- **Decision:** a clearing rule must:
  - settle only the doubt it verifies;
  - use official lists or two sources that agree;
  - run in shadow mode first;
  - go live only after zero wrong clears on 50 or more items.
- **Why:** an exact-name rule looked perfect against the reviewer's decisions. But 6 of its 11 clears
  were "who asked is inferred", a doubt a member list can't settle.
- **Cost:** precision rises slowly.

### Long hearings are cut where the chair moves on
- **Decision:** cut transcripts into parts at hand-off lines ("Next we have…") near even sizes, never at a
  bare pause.
- **Why:** a 30-second gap inside one testimony split it in two on a real hearing.
- **Cost:** parts are uneven, and a cut warning stays for review.

### Every AI reply is saved
- **Decision:** replies are saved by a hash of the full request, before parsing.
- **Why:** retries and rebuilds must never pay twice, or repeat work that already finished. A "ran out of
  room" reply is reused only where the remedy differs (split the lines).
- **Cost:** disk space, plus care to keep transient inputs (like a file path) out of the hash.

### Flag-only checks must say what they couldn't check
- **Decision:** any check that fails, hits its cap or can't read a format adds a visible "not checked"
  item, and Rebuild can retry it.
- **Why:** otherwise a draft with no audio flags looks like a draft that passed the audio check.

### Small, local, replaceable parts
- **Decision:** a Python standard-library server and plain JavaScript, with no framework. Transcription,
  drafting and checks are separate modules behind small interfaces.
- **Why:** one user, one Mac, and a fast loop. The transcription and AI vendor could change without a
  rewrite.
- **Cost:** some hand-built UI that a framework would have given for free.

## Lessons

- **Get real data first.** One real hearing exposed problems weeks of synthetic tests hadn't. Every major
  improvement here came from a real run.
- **Learn how the reviewer actually works.** She listens at every ⚠ and approves the rest. That one
  observation set the whole metric strategy.
- **A hosted model varies between runs.** Detect the failure pattern in code, retry just that piece, and
  keep a warning if the retry fails too.
- **Test a data source against real corrections before building on it.** The legislator roster would have
  caught none of the reviewer's own name fixes, which were all public witnesses, so it was scoped to
  naming questioners instead.
- **Review every feature that spends money or deletes.** Independent reviews found 4–8 real defects per
  feature, including uncancellable paid calls and duplicated testimony.
- **Verify platform behaviour on the platform.** CI runners with security features off happily started a
  build that a normal Mac would refuse.

# Architecture

A local Mac app: a small Python standard-library server with a plain-JavaScript review screen in the
browser. Everything runs on the user's Mac except the AI calls.

```mermaid
flowchart TD
    subgraph Mac["On the Mac"]
        Q[Batch queue<br/>audio.py] --> T[Transcription<br/>MacWhisper CLI]
        T --> D[Drafting<br/>extraction.py · gemini.py]
        D --> K[Checks<br/>second_check · audio_check · roster]
        K --> S[(Workspace<br/>store.py, JSON on disk)]
        S --> R[Review screen<br/>web/]
        R --> O[Copy · Word · Markdown]
        U[Updater<br/>updater.py] -.-> Mac
    end
    D <-->|transcript text| G[(Gemini API)]
    K <-->|text · short audio clips| G
    D <-->|agenda| L[(Public hearing page)]
    K <-->|member lists| L
    U <-->|signed builds| GH[(Releases repository)]
```

## The pipeline

1. **Intake** ([`audio.py`](../hearing_compiler/audio.py), [`store.py`](../hearing_compiler/store.py)).
   - **What comes in:** MP3, WAV or M4A files, or a whole folder, each kept in the workspace. A
     transcript (TXT, SRT, VTT, MacWhisper JSON) can be imported instead.
   - **The queue:** it runs one hearing at a time and keeps the Mac awake while it works (`caffeinate`).
     A failed hearing doesn't stop the others.
2. **Transcription** ([`providers.py`](../hearing_compiler/providers.py)).
   - **How:** MacWhisper's command-line tool on the Mac. The transcript is saved before anything else,
     so a later retry never transcribes again.
3. **Drafting** ([`extraction.py`](../hearing_compiler/extraction.py), [`gemini.py`](../hearing_compiler/gemini.py)).
   - **The request:** one Gemini request with the timestamped transcript and the official agenda, read
     from the public hearing page. The reply is structured JSON: testimonies, bills, positions,
     arguments, and questions with their answers.
   - **Quotes:** every item carries a verbatim quote. Code locates it in the transcript and uses its line
     numbers, never the model's.
   - **Fixing the reply:** code rejoins entries the model split, attaches bill-only entries to their
     testimony, merges companion bills, and redrafts testimony the model listed but skipped.
   - **Saving:** each reply is saved before parsing, by a hash of the model and prompt. A retry or rebuild
     reuses it and never pays twice.
4. **Long hearings** ([`parts.py`](../hearing_compiler/parts.py)).
   - **Cutting:** over about 30,000 words, the transcript is cut into parts of about 12,000 words, at the
     chair's hand-offs. A reply that runs out of room is split again.
   - **Joining:** parts keep the hearing's own line numbers, so their entries join back into one draft,
     and a testimony a cut ran through is rejoined.
   - **Visibility and rebuilds:** each cut is listed for review. The cut points are saved, so a rebuild
     finds the same replies.
5. **Checks.** All are flag-only: they add ⚠, and never change a value or approve anything.
   - [`second_check.py`](../hearing_compiler/second_check.py): one call per testimony comparing each item
     with its transcript lines. Findings without a verified quote are dropped, and possibly-missed points
     are listed apart.
   - [`audio_check.py`](../hearing_compiler/audio_check.py) with [`clips.py`](../hearing_compiler/clips.py):
     the seconds behind unflagged names and figures are cut from the recording and sent to the model.
     MP3 is cut at frame boundaries in plain Python, WAV with `wave`, and M4A through macOS's
     `afconvert`.
   - [`roster.py`](../hearing_compiler/roster.py): legislators' names against the Legislature's public
     member lists, cached for a week, plus a shadow-mode rule that records what it *would* clear.
   - Every check reports what it couldn't check, so a quiet draft is never mistaken for a checked one.
6. **Review** ([`web/`](../web/), [`rich_review.py`](../hearing_compiler/rich_review.py)).
   - **Flags:** ⚠ items, *Next ⚠*, and *Approve everything not flagged*.
   - **Audio:** ▶ on every timestamp, with −10 s / +10 s.
   - **Edits:** corrections with a full review log, and structural repairs (add or remove a testimony,
     an argument or a Q&A).
7. **Output** ([`word_export.py`](../hearing_compiler/word_export.py)). Copy-ready notes, Word, Markdown
   or JSON. Anything unresolved stays marked ⚠ in the copied text.

## Storage and recovery

- **One workspace folder:** a `state.json`, plus the recordings and saved AI replies.
- **Saving:** writes are atomic (a temporary file, then a replace), and a failed save rolls back in memory.
- **Restarts:** a hearing that was processing shows Retry after a restart, and completed stages are never
  repeated.
- **Backups:** backup and restore cover the whole workspace and are verified when restored
  ([`workspace_backup.py`](../hearing_compiler/workspace_backup.py)).
- **Rebuild:** redoes an unreviewed draft with the latest code from its saved replies, but refuses once
  any review work exists.

## Release and update path

1. **CI.** Every app change is built with PyInstaller on GitHub's macOS runners. The full tests and
   package checks run there, along with an HTTPS check and a rehearsal of the self-update.
2. **Signing.** The app is signed with a pinned identity. A check reads every binary's signature flags,
   because CI runners have System Integrity Protection off and would start a build that a normal Mac
   refuses. An early build taught us that the hard way.
3. **Pre-release, then promotion.** CI publishes a pre-release, and only a person promotes it to the
   installed app.
4. **Self-update** ([`updater.py`](../hearing_compiler/updater.py)).
   - **When:** on launch, if nothing is processing.
   - **Checks:** it downloads the latest build and verifies its signer.
   - **Install:** it keeps the previous build and switches over.
   - **Rollback:** if the new build doesn't start, it rolls back to the previous one.

## Privacy and cost

- **Public hearings only.** Transcription is local.
- **What leaves the Mac:** transcript text and short audio clips go to Gemini.
- **Keys:** the user's API key lives in the macOS Keychain, never in files, exports or backups.
- **Cost:** saved replies keep repeat work free. Redrafts and audio clips are capped per hearing.

## Testing

- **Unit tests** ([`tests/`](../tests/)): synthetic fixtures, with a fake model at the paid boundary, so
  no test needs a key or spends money.
- **The browser check** ([`scripts/friday_check.py`](../scripts/friday_check.py)):
  - **Where:** WebKit and Chromium, against the real server.
  - **What it covers:** a synthetic hearing through intake, a failed transcription, an attached
    transcript, drafting, the checks, review, Next ⚠, playback, rebuild, copy and Stop.
- **Real-hearing runs:** in a notebook with the user's key, against public recordings. See the
  [evaluation](EVALUATION.md).

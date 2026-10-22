"use strict";

const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const state = {token: "", hearings: [], job: null, view: "hearings", selected: new Set(), edit: null, structure: null, editSource: null, structureSource: null, reviewFocus: null, retry: null, busy: false, macwhisper: null, saveError: false, connection: null, audioFiles: [], textFiles: [], attachAudio: null, attachTranscript: null, mediaJob: null, stopped: false, flagCursor: null};
state.transcriptFilter = "all";
let toastTimer;

function time(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "Time unavailable";
  const n = Math.floor(seconds);
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60].map((v) => String(v).padStart(2, "0")).join(":");
}
function evidenceLabel(segment, range = false) {
  if (segment.timing_unavailable) return `Text line ${escapeHTML(segment.source_line)}`;
  return range ? `${time(segment.start)}–${time(segment.end)}` : time(segment.start);
}
// A player-style time: 2:05, 12:35, or 1:02:03 past the first hour.
function clock(seconds) {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  const pad = (v) => String(v).padStart(2, "0");
  return n >= 3600 ? `${Math.floor(n / 3600)}:${pad(Math.floor(n / 60) % 60)}:${pad(n % 60)}` : `${Math.floor(n / 60)}:${pad(n % 60)}`;
}
function timed(segment) { return Boolean(segment) && !segment.timing_unavailable && Number.isFinite(segment.start); }
// The button that plays the recording from a segment and shows it in the transcript. Untimed text keeps its line.
function timeButton(segment) {
  const id = escapeHTML(segment.id);
  if (!timed(segment)) {
    const label = evidenceLabel(segment);
    return `<button type="button" class="time-button" data-evidence="${id}" aria-label="Show in the transcript: ${label}">${label}</button>`;
  }
  const at = clock(segment.start);
  return state.job?.audio_url
    ? `<button type="button" class="time-button" data-evidence="${id}" aria-label="Play the recording from ${at}">▶ ${at}</button>`
    : `<button type="button" class="time-button" data-evidence="${id}" aria-label="Show the transcript at ${at}">${at}</button>`;
}
// The earliest timed segment among evidence ids, for "Listen from".
function firstTimed(ids = []) {
  const wanted = new Set(ids);
  return state.job?.draft?.segments.filter((segment) => wanted.has(segment.id) && timed(segment))
    .reduce((first, segment) => (!first || segment.start < first.start ? segment : first), null) || null;
}
// Where a draft path sits, for the player's "what's playing" label.
function placeLabel(path = "") {
  const number = (pattern) => Number(path.match(pattern)?.[1]) + 1;
  if (/^\/(units|testimonies)\/\d+/.test(path)) return `Testimony ${number(/^\/\w+\/(\d+)/)}`;
  if (/^\/questions\/\d+/.test(path)) return `Q&A ${number(/^\/questions\/(\d+)/)}`;
  if (/^\/unassigned_questions\/\d+/.test(path)) return `Unlinked question ${number(/^\/unassigned_questions\/(\d+)/)}`;
  if (path.startsWith("/issues/")) return "Other things to check";
  if (path.startsWith("/testimony_groups/")) return "Shared testimony";
  return "";
}
function playingParts(button) {
  const segment = button.closest("[data-segment]");
  if (segment) {
    const item = state.job?.draft?.segments.find((s) => s.id === segment.dataset.segment);
    return ["Transcript", item && !item.speaker_unavailable ? item.speaker : ""];
  }
  const holder = button.closest("[data-field]");
  const path = holder?.dataset.field || button.closest("[data-focus-target]")?.dataset.focusTarget || "";
  return [placeLabel(path), holder?.querySelector("[data-edit]")?.dataset.label || ""];
}
// The flag Gemini drafts put on a questioner's name that wasn't said near the question.
const INFERRED_ASKER = /^Who asked is inferred from context/;
function questionerListen(path, item) {
  if (!state.job?.audio_url) return "";
  let question = null;
  try { question = getField(path.replace(/\/asker(\/name)?$/, "/question")); } catch { question = null; }
  const segment = firstTimed(question?.evidence) || firstTimed(item.evidence);
  // The cited line is often the answer, so start a little earlier to hear who asked.
  return segment ? `<button type="button" class="secondary small listen-button" data-listen="${escapeHTML(segment.id)}" data-lead="${ASKER_LEAD}" aria-label="Listen to this question from just before ${clock(segment.start)}">▶ Listen from just before</button>` : "";
}
const ASKER_LEAD = 15;
function date(value) {
  const parsed = new Date(value + "T12:00:00");
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("en-US", {year: "numeric", month: "long", day: "numeric"});
}
// Messages sit above the sticky player rather than over its controls.
function placeToast() {
  const player = $("#audio-player");
  const top = player.hidden ? innerHeight : player.getBoundingClientRect().top;
  $("#toast").style.bottom = top < innerHeight ? `${Math.round(innerHeight - top + 12)}px` : "";
}
// The sticky player moves when the page end comes into view, so a showing message follows it.
addEventListener("scroll", () => { if (!$("#toast").hidden) placeToast(); }, {passive: true});
function toast(message) {
  $("#toast").textContent = message;
  placeToast();
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4200);
}
function fail(error) {
  $("#global-error").textContent = error.message || String(error);
  $("#global-error").hidden = false;
}
async function api(path, body) {
  const response = await fetch(path, body ? {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Review-Token": state.token},
    body: JSON.stringify(body),
  } : {cache: "no-store"});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || "The local workspace could not complete this action.");
    error.status = response.status;
    throw error;
  }
  return data;
}
function statusPill(status) {
  const palette = {APPROVED: "green", UNRESOLVED: "amber", NEEDS_REVIEW: "amber", PENDING: "gray", Complete: "teal", Failed: "red", Analyzing: "teal", Transcribing: "teal", Waiting: "gray", "Awaiting transcript": "gray"};
  const label = {APPROVED: "Approved", UNRESOLVED: "Unresolved details", NEEDS_REVIEW: "Needs review", PENDING: "Pending"}[status] || status;
  return `<span class="pill ${palette[status] || "gray"}">${escapeHTML(label)}</span>`;
}
function setView(view) {
  if (state.busy) return toast("Your review is saving. Wait before changing screens.");
  if (reviewEditorOpen()) return toast("Save or cancel your open correction before changing screens.");
  state.view = ["hearings", "review", "output"].includes(view) ? view : "hearings";
  for (const name of ["hearings", "review", "output"]) {
    $(`#${name}-view`).hidden = name !== state.view;
    const tab = $(`[data-view="${name}"]`);
    if (name === state.view) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  }
  history.replaceState(null, "", `#${state.view}`);
  if (state.view !== "review") $("#source-audio").pause();
  render();
}
function getField(path) {
  return path.split("/").filter(Boolean).reduce((node, part) => node[part], state.job.draft);
}
function reviewEditorOpen() {
  return Boolean($("#edit-dialog")?.open || $("#structure-dialog")?.open);
}
function buttonFocusKey(element) {
  if (!element || !element.closest("#review-view")) return null;
  if (element.id === "transcript-filter") return {name: "id", value: "transcript-filter"};
  if (element.matches("[data-focus-summary]")) return {name: "data-focus-summary", value: "review"};
  for (const name of ["data-edit", "data-approve", "data-repair", "data-evidence", "data-listen", "data-action", "data-focus-field"]) {
    if (element.hasAttribute(name)) return {name, value: element.getAttribute(name)};
  }
  return null;
}
function restoreReviewFocus(key = state.reviewFocus) {
  if (reviewEditorOpen()) return;
  const matching = key && [...document.querySelectorAll(`#review-view [${key.name}]`)].find((node) => node.getAttribute(key.name) === key.value);
  (matching || $("#draft-heading"))?.focus({preventScroll: true});
}
function captureReviewPosition() {
  return {job: $("#review-view").dataset.job || "", evidence: $(".evidence-content")?.scrollTop || 0,
    attentionOpen: Boolean($(".review-focus details")?.open),
    windowX: window.scrollX, windowY: window.scrollY, focus: buttonFocusKey(document.activeElement)};
}
function restoreReviewPosition(position) {
  $("#review-view").dataset.job = state.job?.id || "";
  if (!position.job || position.job !== state.job?.id) return;
  const attention = $(".review-focus details");
  if (attention) attention.open = position.attentionOpen;
  const evidence = $(".evidence-content");
  if (evidence) {
    const behavior = evidence.style.scrollBehavior;
    evidence.style.scrollBehavior = "auto";
    evidence.scrollTop = position.evidence;
    evidence.style.scrollBehavior = behavior;
  }
  if (position.focus && !reviewEditorOpen()) restoreReviewFocus(position.focus);
  window.scrollTo({left: position.windowX, top: position.windowY, behavior: "instant"});
}
function repairButton(op, label, values = {}, className = "secondary small", accessibleLabel = label) {
  return `<button type="button" class="${className}" data-repair="${escapeHTML(JSON.stringify({op, ...values}))}" aria-label="${escapeHTML(accessibleLabel)}">${escapeHTML(label)}</button>`;
}
function testimonyLabel(testimony) {
  return testimony ? `${testimony.name.value || "Identity unresolved"} (${testimony.speaker})` : "Unknown testimony";
}
function evidenceChoices(scope, ids = []) {
  return state.job.draft.segments.map((segment) => `<label><input type="checkbox" data-evidence-choice="${escapeHTML(scope)}" value="${escapeHTML(segment.id)}" ${ids.includes(segment.id) ? "checked" : ""}><span class="segment-choice-text"><strong>${evidenceLabel(segment, true)} · ${escapeHTML(segment.speaker)} · ${escapeHTML(segment.id)}</strong><span>${escapeHTML(segment.text)}</span></span></label>`).join("");
}
function selectedEvidence(scope) {
  return [...document.querySelectorAll("[data-evidence-choice]")].filter((input) => input.dataset.evidenceChoice === scope && input.checked).map((input) => input.value);
}
function speakerOptions(selected = "", optional = false) {
  const speakers = [...new Set(state.job.draft.segments.map((segment) => segment.speaker))];
  return `${optional ? '<option value="">Unknown respondent</option>' : '<option value="" disabled>Choose transcript speaker</option>'}${speakers.map((speaker) => `<option ${speaker === selected ? "selected" : ""} value="${escapeHTML(speaker)}">${escapeHTML(speaker)}</option>`).join("")}`;
}
function questionConfirmation(testimony, statement, i, j) {
  if (!statement.questions_none) return "";
  const item = statement.questions_none;
  const confirmed = item.review === "APPROVED" && Boolean(item.value);
  return `<div class="question-review-note ${confirmed ? "confirmed" : ""}" data-focus-target="/testimonies/${i}/statements/${j}/questions_none">${confirmed ? "✓ Reviewer confirmed: None noted during this testimony." : "No matching Q&A detected. Check for missed exchanges before confirming none."} ${item.evidence.length ? evidenceLinks(item.evidence) : ""}<br>${repairButton("confirm_no_questions", confirmed ? "Change confirmation" : "Confirm no questions", {testimony: i, statement: j}, "text-button")}</div>`;
}
function structureInput(id, label, value = "", required = false, textarea = false) {
  // macOS autocorrect could swap a typed surname for a dictionary word; the spelling underline stays.
  const attrs = `id="${id}" ${required ? "required" : ""} maxlength="50000" autocorrect="off" autocapitalize="off"`;
  return `<label for="${id}">${label}</label>${textarea ? `<textarea ${attrs} rows="3">${escapeHTML(value || "")}</textarea>` : `<input ${attrs} value="${escapeHTML(value || "")}">`}`;
}
function structureEvidence(scope, title, ids = []) {
  return `<details class="evidence-picker" open><summary>${title}</summary><p class="muted">Select the transcript segments supporting this repair. The original transcript stays unchanged.</p><div class="segment-choices">${evidenceChoices(scope, ids)}</div></details>`;
}
function openStructure(operation) {
  if (operation.op.startsWith("rich_")) return openRichStructure(operation);
  if (state.busy || reviewEditorOpen()) return toast("Finish your current correction first.");
  state.structure = operation;
  state.structureSource = {id: state.job.id, revision: state.job.revision, request_id: crypto.randomUUID()};
  const {op, testimony, statement, question} = operation;
  const draft = state.job.draft;
  const titles = {add_testimony: "Add missed testimony", remove_testimony: "Remove testimony from the draft", add_statement: "Add bill statement", remove_statement: "Remove bill statement", add_argument: "Add missed argument", remove_argument: "Remove argument", add_question: "Add missed legislator Q&A", remove_question: "Remove Q&A", set_question_speakers: "Repair Q&A speaker links", group_testimonies: "Group shared testimony", ungroup_testimonies: "Ungroup testimony", confirm_no_questions: "Review whether questions were asked"};
  $("#structure-title").textContent = titles[op] || "Repair hearing draft";
  $("#structure-error").hidden = true;
  $("#save-structure").textContent = op.startsWith("remove_") ? "Remove from draft" : "Save repair";
  let description = "New or changed content remains a draft until reviewed. Speaker labels do not establish a person's identity.";
  let inputs = "";
  if (op === "add_testimony") {
    inputs = `<label for="repair-speaker">Transcript speaker</label><select id="repair-speaker" required>${speakerOptions()}</select>${structureInput("repair-name", "Witness name, if established")}${structureInput("repair-organization", "Organization, if applicable")}${structureEvidence("repair-evidence", "Evidence for the testimony")}`;
    description += " A blank bill statement will be created for you to complete.";
  } else if (op === "add_statement") {
    inputs = `${structureInput("repair-bill", "Bill, if established")}<label for="repair-position">Position</label><select id="repair-position"><option>Unclear</option><option>Support</option><option>Oppose</option></select>${structureEvidence("repair-evidence", "Bill and position evidence")}`;
  } else if (op === "add_argument") {
    inputs = `${structureInput("repair-value", "Main argument", "", true, true)}${structureEvidence("repair-evidence", "Argument evidence")}`;
  } else if (op === "add_question") {
    inputs = `<label for="repair-asker-speaker">Question speaker</label><select id="repair-asker-speaker" required>${speakerOptions()}</select>${structureInput("repair-asker", "Question asker's name, if established")}<label for="repair-respondent-speaker">Response speaker</label><select id="repair-respondent-speaker">${speakerOptions("", true)}</select>${structureInput("repair-respondent", "Respondent's name, if established")}${structureInput("repair-bill", "Bill, if established")}${structureInput("repair-question", "Question", "", true, true)}${structureEvidence("repair-evidence", "Question evidence")}${structureInput("repair-response", "Response, if present", "", false, true)}${structureEvidence("repair-response-evidence", "Response evidence")}`;
  } else if (op === "set_question_speakers") {
    const item = draft.questions[question];
    inputs = `<label for="repair-asker-speaker">Question speaker</label><select id="repair-asker-speaker" required>${speakerOptions(item.asker_speaker)}</select><label for="repair-respondent-speaker">Response speaker</label><select id="repair-respondent-speaker">${speakerOptions(item.respondent_speaker || "", true)}</select>`;
    description = "Correct the links to transcript speakers. Names, question, response and evidence will need review after the links change.";
  } else if (op === "group_testimonies") {
    inputs = `<div class="group-choices">${draft.testimonies.map((item, i) => `<label><input type="checkbox" data-group-member value="${i}">${escapeHTML(testimonyLabel(item))}</label>`).join("")}</div>`;
    description = "Select at least two testifiers who delivered shared testimony. Individual speaker records, evidence and statements remain separate. Approve the shared-testimony confirmation afterward.";
  } else if (op === "confirm_no_questions") {
    const item = draft.testimonies[testimony].statements[statement].questions_none;
    const confirmed = item.review === "APPROVED" && Boolean(item.value);
    inputs = `<label class="check-label"><input id="repair-none-confirmed" type="checkbox" ${confirmed ? "checked" : ""}> I checked this testimony and its surrounding exchange; no legislator questions were noted.</label>${structureEvidence("repair-evidence", "Reviewed testimony and exchange", item.evidence)}`;
    description = "A missing detected question is not proof that none was asked. Select the segments you checked. Uncheck the box to return this confirmation to unresolved.";
  } else {
    const detail = op === "remove_testimony" ? testimonyLabel(draft.testimonies[testimony]) : op === "remove_argument" ? draft.testimonies[testimony].statements[statement].arguments[operation.argument].value : op === "remove_statement" ? draft.testimonies[testimony].statements[statement].bill.value || "Bill unresolved" : op === "remove_question" ? draft.questions[question].question.value : "Shared testimony grouping";
    inputs = `<p>${escapeHTML(detail)}</p><label class="check-label"><input type="checkbox" required> ${op === "ungroup_testimonies" ? "Separate these entries while preserving individual testimony." : "Remove this item and its contained draft fields. The transcript and saved review history remain."}</label>`;
    description = op === "ungroup_testimonies" ? "This removes the shared-testimony grouping, preserving all testifiers and statements." : "Use this when the extracted item is incorrect or duplicated. This removes it from the current draft and exports.";
  }
  $("#structure-description").textContent = description;
  $("#structure-fields").innerHTML = inputs;
  repairListen(operation);
  $("#structure-dialog").showModal();
  $("#structure-fields input, #structure-fields select, #structure-fields textarea")?.focus();
}
function structureOperation() {
  if (state.structure.op.startsWith("rich_")) return richStructureOperation();
  const operation = {...state.structure};
  const text = (id) => $(id).value.trim() || null;
  if (operation.op === "add_testimony") Object.assign(operation, {speaker: $("#repair-speaker").value, name: text("#repair-name"), organization: text("#repair-organization"), evidence: selectedEvidence("repair-evidence")});
  if (operation.op === "add_statement") Object.assign(operation, {bill: text("#repair-bill"), position: $("#repair-position").value, evidence: selectedEvidence("repair-evidence")});
  if (operation.op === "add_argument") Object.assign(operation, {value: text("#repair-value"), evidence: selectedEvidence("repair-evidence")});
  if (operation.op === "add_question") Object.assign(operation, {asker_speaker: $("#repair-asker-speaker").value, respondent_speaker: $("#repair-respondent-speaker").value || null, asker: text("#repair-asker"), respondent: text("#repair-respondent"), bill: text("#repair-bill"), question: text("#repair-question"), response: text("#repair-response"), evidence: selectedEvidence("repair-evidence"), response_evidence: selectedEvidence("repair-response-evidence")});
  if (operation.op === "set_question_speakers") Object.assign(operation, {asker_speaker: $("#repair-asker-speaker").value, respondent_speaker: $("#repair-respondent-speaker").value || null});
  if (operation.op === "group_testimonies") {
    operation.testimonies = [...document.querySelectorAll("[data-group-member]:checked")].map((input) => Number(input.value));
    if (operation.testimonies.length < 2) throw new Error("Select at least two testifiers for shared testimony.");
  }
  if (operation.op === "confirm_no_questions") Object.assign(operation, {confirmed: $("#repair-none-confirmed").checked, evidence: selectedEvidence("repair-evidence")});
  return operation;
}
function evidenceLinks(ids) {
  const lookup = new Map(state.job.draft.segments.map((s) => [s.id, s]));
  return [...new Set(ids)].map((id) => (lookup.has(id) ? timeButton(lookup.get(id)) : "")).join("");
}
function isGeminiDraft() { return state.job?.draft?.provider === "gemini-draft-v1"; }
function field(path, label, options = {}) {
  const item = getField(path);
  const unknown = !item.value && !item.not_applicable;
  const gemini = isGeminiDraft();
  const flagged = Boolean(item.flag) && item.review !== "APPROVED";
  // Gemini drafts mark doubt explicitly, so unflagged items stay calm; older drafts keep the cautious style.
  const uncertain = flagged || unknown || item.review === "UNRESOLVED" || (!gemini && item.review === "NEEDS_REVIEW");
  // Hearing-wide and second-check flags already say what to do, so they get no "Check:" prefix.
  const needs = flagged ? `⚠ ${path.startsWith("/issues/") || /^(Second|Audio) check:/.test(item.flag) ? "" : "Check: "}${item.flag}` : unknown ? "Not found in the transcript. Add it if it was said."
    : item.review === "APPROVED" ? (item.corrected ? "Corrected and approved" : "Approved") : gemini ? "AI draft" : "Draft — human review required";
  const notApplicable = unknown && path.endsWith("/organization")
    ? `<button type="button" class="text-button" data-na="${escapeHTML(path)}">Not applicable</button>` : "";
  return `<div class="field ${uncertain ? "uncertain" : ""} ${flagged ? "flagged" : ""} ${unknown ? "blank" : ""} ${options.name ? "name" : ""} ${options.argument ? "argument" : ""}" data-field="${escapeHTML(path)}">
    ${label ? `<div class="section-label">${escapeHTML(label)}</div>` : ""}
    <div class="field-top"><div class="field-value">${escapeHTML(item.not_applicable ? "Not applicable" : item.value || options.fallback || "Unknown")}</div>
    <div class="field-actions">
      ${flagged && INFERRED_ASKER.test(item.flag) ? questionerListen(path, item) : ""}
      <button type="button" class="text-button" data-edit="${escapeHTML(path)}" data-label="${escapeHTML(label || "Main argument")}" aria-label="Edit ${escapeHTML(label || "main argument")}">Edit</button>
      ${options.remove ? repairButton("remove_argument", "Remove", options.remove, "text-button remove-button", "Remove main argument") : ""}
      ${item.review === "APPROVED" ? '<span class="approved-mark">✓ Reviewed</span>' : unknown ? notApplicable : `<button type="button" class="text-button" data-approve="${escapeHTML(path)}" aria-label="Approve ${escapeHTML(label || "main argument")}">Approve</button>`}
    </div></div>
    <div class="field-note ${flagged ? "flag-note" : ""}">${escapeHTML(needs)}${item.evidence.length ? ` · <span class="evidence-links">${evidenceLinks(item.evidence)}</span>` : " · Manual confirmation needed"}</div>
    ${item.source_quotes?.length ? `<details><summary>Original model quotations</summary>${item.source_quotes.map(q => `<p>${escapeHTML(q.segment_id)}: “${escapeHTML(q.quote)}”</p>`).join("")}</details>` : ""}
  </div>`;
}
function transcriptFailed(job) {
  // job.audio, not job.kind: the full hearing from /api/hearings/<id> has no kind.
  return Boolean(job.audio) && job.processing === "Failed" && job.failed_stage === "transcription";
}
function transcriptWanted(job) {
  return job.processing === "Awaiting transcript" || transcriptFailed(job);
}
// How long a hearing has been in its current stage, from the server's stage_started_at (ISO, UTC).
function stageStarted(value) {
  if (typeof value !== "string" || !value.trim()) return NaN;
  let text = value.trim().replace(/(\.\d{3})\d+/, "$1");  // Older Safari wants at most milliseconds.
  if (!/(Z|[+-]\d\d:?\d\d)$/i.test(text)) text += "Z";
  return Date.parse(text);
}
function recordingLength(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 55) return `${minutes}-minute`;
  const hours = Math.round(minutes / 60);
  return Math.abs(minutes - hours * 60) <= 5 ? `${hours}-hour` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}
function stageHint(job) {
  if (job.processing === "Waiting") return "waiting for the hearing ahead";
  const duration = job.duration_seconds;
  // Over about 3.5 hours the draft is made in parts, one after another.
  if (job.processing === "Analyzing") return duration > 12600 ? "usually 6–12 min for a hearing this long" : "usually 3–5 min";
  if (typeof duration !== "number" || !Number.isFinite(duration) || duration <= 0) return "usually 10–30 min for a 2-hour hearing";
  const low = Math.max(1, Math.round(duration / 3600 * 5)), high = Math.max(low + 1, Math.round(duration / 3600 * 15));
  return `usually ${low}–${high} min for this ${recordingLength(duration)} recording`;
}
function progressHTML(job) {
  if (!job || !["Waiting", "Transcribing", "Analyzing"].includes(job.processing)) return "";
  const started = stageStarted(job.stage_started_at);
  if (!Number.isFinite(started)) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - started) / 60000));
  const elapsed = minutes < 1 ? "under a minute so far" : minutes < 60 ? `${minutes} min so far` : `${Math.floor(minutes / 60)} h ${minutes % 60} min so far`;
  return `${escapeHTML(job.processing)} · ${elapsed} · <span class="stage-hint">${escapeHTML(stageHint(job))}</span>`
    // Waiting behind other hearings in a batch can rightly take hours.
    + (minutes >= 60 && job.processing !== "Waiting" ? '<span class="stage-late">This has taken over an hour. If it doesn\'t finish soon, write a comment on the checklist.</span>' : "");
}
// Polling refreshes the elapsed time in place, so focus and scroll stay put.
function updateProgress() {
  for (const node of document.querySelectorAll("[data-progress]")) {
    const id = node.dataset.progress;
    const html = progressHTML(state.hearings.find((job) => job.id === id) || (state.job?.id === id ? state.job : null));
    if (node.innerHTML !== html) node.innerHTML = html;
  }
}
function renderQueue() {
  $("#hearings-view").innerHTML = `
    <div class="queue-heading"><div><h1 id="queue-title">Batch queue</h1><p>Completed processing still needs human review. Progress and recordings are saved locally.</p></div>
    <div class="queue-actions"><button class="primary" type="button" data-action="audio-files">Add MP3 files</button><button class="secondary" type="button" data-action="audio-folder">Choose folder</button></div></div>
    <div class="connection-note">${state.connection?.ready ? `MacWhisper connected · ${escapeHTML(state.connection.model_id)} · ${escapeHTML(state.connection.output_format || "json")}` : state.connection?.platform === "macOS" ? "Import Free transcript exports or check the installed MacWhisper CLI." : "MacWhisper runs on macOS. Import TXT, SRT, VTT or JSON here; original recordings can be attached for playback."} <button class="text-button" type="button" data-action="connection">Connection settings</button></div>
    ${state.hearings.length ? `<div class="card table-wrap"><table><thead><tr><th>Hearing</th><th>Processing</th><th>Review</th><th>Action</th></tr></thead><tbody>
    ${state.hearings.map((job) => `<tr><td><div class="hearing-name">${escapeHTML(job.hearing.title || "Invalid transcript")}</div><div class="queue-details">${escapeHTML(job.hearing.committee)} · ${escapeHTML(date(job.hearing.date))}<br>${escapeHTML(job.hearing.source_file)}</div></td>
    <td>${statusPill(job.processing)}<div class="stage-progress" data-progress="${escapeHTML(job.id)}">${progressHTML(job)}</div>${job.error ? `<div class="failure-detail">${escapeHTML(job.failed_stage || "Input")}: ${escapeHTML(job.error)}</div>` : ""}</td><td>${statusPill(job.review.status)}${job.review.flags ? `<div class="queue-flags">⚠ ${job.review.flags} to check</div>` : ""}</td>
    <td><div class="queue-actions-cell">${job.processing === "Complete" ? `<button class="secondary small" type="button" data-open="${escapeHTML(job.id)}">Open draft</button>` : job.kind === "audio" ? `<button class="secondary small" type="button" data-open="${escapeHTML(job.id)}">Open audio</button>` : ""}
    ${job.processing === "Failed" ? `<button class="secondary small" type="button" data-retry="${escapeHTML(job.id)}">Retry</button>` : ""}
    ${job.processing === "Complete" && job.draft_method !== "gemini" && state.extractionProvider === "gemini" && !job.review.approved
      ? `<button class="secondary small" type="button" data-redraft="${escapeHTML(job.id)}">Redraft with Gemini</button>` : ""}
    ${job.can_rebuild === true ? `<button class="secondary small" type="button" data-rebuild="${escapeHTML(job.id)}">Rebuild with the latest fixes</button>` : ""}
    ${transcriptWanted(job) ? `<button class="secondary small" type="button" data-attach-transcript="${escapeHTML(job.id)}">Attach transcript</button>` : ""}</div></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="card empty-state"><h2>No hearings yet</h2><p>Add public hearing recordings or import a MacWhisper transcript to get started.</p><button class="primary" type="button" data-action="audio-files">Add MP3 files</button></div>`}
    <div class="transcript-tools"><span>Already have a transcript?</span><div class="queue-actions"><button class="secondary small" type="button" data-action="text">Import text (.txt)</button><button class="secondary small" type="button" data-action="text">Import subtitles (.srt/.vtt)</button><button class="secondary small" type="button" data-action="macwhisper">Import MacWhisper JSON</button><button class="secondary small" type="button" data-action="import">Import transcript JSON</button></div></div>`;
}
function renderReview() {
  if (state.job?.draft?.draft_format === "hearing-review-units/v1") return renderRichReview();
  const position = captureReviewPosition();
  renderReviewContent();
  restoreReviewPosition(position);
}
function renderReviewContent() {
  const job = state.job;
  if (!job?.draft) {
    $("#review-view").innerHTML = job ? `<div class="card hearing-header"><div><h1 id="review-title">${escapeHTML(job.hearing.title)}</h1><p class="metadata">${escapeHTML(job.hearing.committee)} · ${escapeHTML(date(job.hearing.date))}</p></div>${statusPill(job.processing)}</div><div class="card empty-state"><h2>${job.processing === "Awaiting transcript" ? "Recording saved · transcript needed" : transcriptFailed(job) ? "Transcription failed" : job.processing === "Failed" ? "Processing failed" : "Hearing processing"}</h2><div class="stage-progress" data-progress="${escapeHTML(job.id)}">${progressHTML(job)}</div><p>${escapeHTML(job.error || "The original recording is available below. Its draft will appear when transcription and extraction finish.")}</p>${transcriptWanted(job) ? `<button class="primary" type="button" data-attach-transcript="${escapeHTML(job.id)}">Attach MacWhisper transcript</button>` : ""}</div>` : `<div class="card empty-state"><h2 id="review-title">Choose a hearing to review</h2><p>Open a hearing from the batch queue.</p><button class="primary" type="button" data-action="queue">Open batch queue</button></div>`;
    return;
  }
  const draft = job.draft, hearing = draft.hearing;
  $("#review-view").innerHTML = `
    <div class="card hearing-header"><div><h1 id="review-title">${escapeHTML(hearing.title)}</h1><p class="metadata">${escapeHTML(hearing.committee)} · ${escapeHTML(date(hearing.date))}</p><p class="source">▤ ${escapeHTML(hearing.source_file)} · ${draft.segments.length} transcript segments</p></div><div class="header-actions">${statusPill(job.review.status)}<button class="secondary small" type="button" data-action="queue">Change hearing</button></div></div>
    <div class="review-layout"><div class="card draft-pane"><div class="pane-heading"><h2 tabindex="-1" id="draft-heading">Hearing draft</h2><p class="muted">${job.review.approved} of ${job.review.fields} fields reviewed</p></div><div class="repair-toolbar"><p class="muted">Repair missed or incorrect material using the transcript.</p><div class="queue-actions">${repairButton("add_testimony", "Add testimony")}${repairButton("group_testimonies", "Group shared testimony")}</div></div><div class="draft-content">
    ${(draft.testimony_groups || []).map((group, i) => `<section class="witness-group" aria-label="Shared testimony"><p><strong>Grouped testifiers:</strong> ${group.members.map((id) => escapeHTML(testimonyLabel(draft.testimonies.find((t) => t.id === id)))).join(" and ")}</p>${field(`/testimony_groups/${i}/confirmation`, "Shared testimony confirmation")}${repairButton("ungroup_testimonies", "Ungroup", {group_id: group.id}, "text-button")}</section>`).join("")}
    ${draft.testimonies.map((testimony, i) => `<div class="witness">
      <div class="section-actions"><h3>Testimony ${i + 1}</h3>${repairButton("remove_testimony", "Remove testimony", {testimony: i}, "text-button remove-button")}</div>
      ${field(`/testimonies/${i}/name`, "Witness", {name: true, fallback: `${testimony.speaker} — identity unresolved`})}
      ${field(`/testimonies/${i}/organization`, "Organization")}
      <p class="muted">Transcript label: ${escapeHTML(testimony.speaker)}${(draft.testimony_groups || []).some((g) => g.members.includes(testimony.id)) ? " · Member of shared testimony" : ""}</p>
      ${testimony.statements.map((statement, j) => `<section class="bill-section" aria-label="Bill testimony">
        <div class="section-actions"><h3>Bill statement ${j + 1}</h3>${repairButton("remove_statement", "Remove statement", {testimony: i, statement: j}, "text-button remove-button")}</div>
        ${field(`/testimonies/${i}/statements/${j}/bill`, "Bill")}
        ${field(`/testimonies/${i}/statements/${j}/position`, "Position")}
        <div class="section-actions"><h3>Main arguments</h3>${repairButton("add_argument", "Add argument", {testimony: i, statement: j}, "text-button")}</div>
        ${statement.arguments.length ? statement.arguments.map((_, k) => field(`/testimonies/${i}/statements/${j}/arguments/${k}`, "", {argument: true, remove: {testimony: i, statement: j, argument: k}})).join("") : '<p class="muted">No argument detected. Check the transcript for omissions.</p>'}
        ${questionConfirmation(testimony, statement, i, j)}
      </section>`).join("")}
      ${repairButton("add_statement", "Add bill statement", {testimony: i})}</div>`).join("")}
      ${!draft.testimonies.length ? '<p class="muted">No testimony remains in this draft. Add missed testimony after checking the transcript.</p>' : ""}
      <section class="qa-section"><div class="section-actions"><h2>Legislator Q&amp;A</h2>${repairButton("add_question", "Add Q&A")}</div>
      ${draft.questions.length ? draft.questions.map((q, i) => `<div class="qa-item">
        <div class="section-actions"><p class="association-note">Transcript speakers: ${escapeHTML(q.asker_speaker)} → ${escapeHTML(q.respondent_speaker || "Unresolved respondent")}</p><div class="queue-actions">${repairButton("set_question_speakers", "Repair speaker links", {question: i}, "text-button")}${repairButton("remove_question", "Remove Q&A", {question: i}, "text-button remove-button")}</div></div>
        ${field(`/questions/${i}/asker`, "Question asker", {fallback: `${q.asker_speaker} — identity unresolved`})}
        ${field(`/questions/${i}/bill`, "Question's bill")}
        ${field(`/questions/${i}/question`, "Question")}
        ${field(`/questions/${i}/respondent`, "Respondent")}
        ${field(`/questions/${i}/response`, "Response", {fallback: "No response detected"})}
      </div>`).join("") : '<p class="muted">No questions detected. Check the transcript for missed exchanges.</p>'}</section>
      ${draft.issues.length ? `<section class="qa-section"><h3>Other things to check</h3>${draft.issues.map((_, i) => field(`/issues/${i}`, "To check")).join("")}</section>` : ""}
      </div></div>
      <aside class="card evidence-pane" aria-label="Transcript evidence"><div class="pane-heading"><h2>Transcript evidence</h2><button class="text-button" type="button" data-action="clear-evidence">Clear highlight</button></div>
      ${renderReviewFocus(draft)}
      <div class="evidence-content">${draft.segments.map((segment) => `<article class="segment ${state.selected.has(segment.id) ? "selected" : ""}" id="segment-${escapeHTML(segment.id)}" data-segment="${escapeHTML(segment.id)}">
        <div class="segment-header">${timeButton(segment)}<span>· ${escapeHTML(segment.speaker)}</span><span class="muted">${escapeHTML(segment.id)}</span></div>
        <p>${escapeHTML(segment.text)}</p></article>`).join("")}<p class="transcript-filter-empty" hidden>No unreferenced passages remain. This does not establish completeness or approval.</p></div>
        <div class="audio-note">${draft.source_format === "plain-text-utf8" ? `Text lines highlight source evidence. Timestamps and speaker turns are unavailable; use manual audio playback. ${job.audio_url ? "" : `<button class="text-button" type="button" data-attach-audio="${escapeHTML(job.id)}">Attach MP3</button>`}` : job.audio_url ? "Click ▶ on any time to listen to the source recording from that point." : `Attach the original recording to listen at these timestamps. <button class="text-button" type="button" data-attach-audio="${escapeHTML(job.id)}">Attach MP3</button>`}</div>
      </aside></div>
    <div class="review-bottom"><p>Saved in this local workspace. Corrections and evidence survive restart; export a separate copy for safekeeping.</p><button class="primary" type="button" data-action="output">Preview hearing notes <span aria-hidden="true">→</span></button></div>`;
  applyTranscriptFilter();
}
function renderMarkdown(markdown) {
  let list = false;
  const output = [];
  for (const line of markdown.split("\n")) {
    const closeList = () => { if (list) { output.push("</ul>"); list = false; } };
    if (line.startsWith("- ")) {
      if (!list) { output.push("<ul>"); list = true; }
      output.push(`<li>${escapeHTML(line.slice(2))}</li>`);
      continue;
    }
    closeList();
    if (/^### /.test(line)) output.push(`<h3>${escapeHTML(line.slice(4))}</h3>`);
    else if (/^## /.test(line)) output.push(`<h2>${escapeHTML(line.slice(3))}</h2>`);
    else if (/^# /.test(line)) output.push(`<h1>${escapeHTML(line.slice(2))}</h1>`);
    else if (line === "---") output.push("<hr>");
    else if (line) output.push(`<p ${/^(Evidence:|Source reference:)/.test(line) ? 'class="provenance"' : ""}>${escapeHTML(line.replace(/^\*\*|\*\*$/g, ""))}</p>`);
  }
  if (list) output.push("</ul>");
  return output.join("");
}
function renderOutput() {
  const job = state.job;
  if (!job?.draft) {
    $("#output-view").innerHTML = '<div class="card empty-state"><h2 id="output-title">Open a hearing first</h2><p>Choose a completed draft from the hearing workspace.</p><button class="primary" type="button" data-action="queue">Open hearing workspace</button></div>';
    return;
  }
  $("#output-view").innerHTML = `<div class="output-heading"><div><h1 id="output-title">Notion-ready note</h1><p>${statusPill(job.review.status)} <span class="muted"> · ${escapeHTML(job.draft.hearing.title)}</span></p></div><div class="queue-actions"><button class="secondary" type="button" data-action="docx">Export Word (.docx)</button><button class="secondary" type="button" data-action="snapshot">Export saved data (.json)</button><button class="secondary" type="button" data-action="download">Export Markdown</button><button class="primary" type="button" data-action="copy">Copy Hearing Notes</button></div></div>
    ${job.review.status !== "APPROVED" ? `<div class="output-warning">This note still contains draft or unresolved fields. Their labels are included when you copy or export. <button class="text-button" type="button" data-action="review">Continue review</button></div>` : ""}
    <div class="output-layout"><article class="card document-preview">${renderMarkdown(job.notion || job.markdown)}</article><aside class="card paste-guide"><h2>Saved locally</h2><p>This hearing and your corrections are stored in the app workspace. Word and Markdown export readable notes; saved-data JSON also includes transcript evidence and review history. Audio stays in the local workspace and is not included in these exports.</p><h2>Before you paste</h2><ol><li>Open the correct hearing page.</li><li>Compare with existing notes.</li><li>Add missing material without replacing existing work.</li></ol><p>Tags: <strong>Hearing Notes</strong> and <strong>${escapeHTML(job.draft.hearing.committee)}</strong>.</p><p>Speaker names in the draft come from transcript text or your corrections. Speaker separation alone does not establish identity.</p><button class="secondary" type="button" data-action="review">Back to review</button></aside></div>`;
}
function render() {
  renderQueue();
  renderReview();
  renderOutput();
  $("#save-status").textContent = state.busy ? "Saving…" : state.saveError ? "Save failed — reload before continuing" : "Progress saved locally";
  syncPlayer();
}
async function refresh() {
  state.hearings = await api("/api/hearings");
}
async function openHearing(id, view = "review") {
  state.job = await api(`/api/hearings/${encodeURIComponent(id)}`);
  state.saveError = false;
  state.selected.clear();
  state.flagCursor = null;
  state.transcriptFilter = "all";
  setView(view);
}
async function saveChanges(changes = [], operations = [], source = null) {
  if (state.busy) throw new Error("A review save is in progress. Wait for it to finish.");
  state.busy = true;
  $("#save-status").textContent = "Saving…";
  try {
    const bound = source || {id: state.job.id, revision: state.job.revision, request_id: crypto.randomUUID()};
    state.job = await api("/api/review", {...bound, changes, operations});
    state.saveError = false;
    try { await refresh(); }
    catch (error) { fail(new Error(`Review was saved. Queue refresh failed: ${error.message}`)); }
    try { render(); }
    catch (error) { fail(new Error(`Review was saved. Display refresh failed: ${error.message}. Reopen the saved hearing.`)); }
    toast("Review saved locally.");
  } catch (error) {
    state.saveError = ![400, 422].includes(error.status);
    throw error;
  } finally {
    state.busy = false;
    $("#save-status").textContent = state.saveError ? "Save failed — reload before continuing" : "Progress saved locally";
  }
}
function highlight(id, parts = [], lead = 0) {
  if (state.transcriptFilter !== "all") {
    state.transcriptFilter = "all";
    if ($("#transcript-filter")) $("#transcript-filter").value = "all";
    applyTranscriptFilter();
  }
  state.selected = new Set([id]);
  for (const segment of document.querySelectorAll("[data-segment]")) {
    segment.classList.toggle("selected", segment.dataset.segment === id);
    if (segment.dataset.segment === id) revealSegment(segment);
  }
  seekSegment(id, parts, lead);
}
// Scrolls only the transcript box, centring the passage in the room above the player, so the page (and her
// place in the draft) stays put even when the transcript sits below the draft on a narrow window.
function revealSegment(segment) {
  const box = segment.closest(".evidence-content");
  if (!box) return;
  const player = $("#audio-player");
  const area = box.getBoundingClientRect(), line = segment.getBoundingClientRect();
  const top = Math.max(area.top, 0), bottom = Math.min(area.bottom, player.hidden ? innerHeight : player.getBoundingClientRect().top);
  const room = Math.max(bottom - top, line.height);
  box.scrollTo({top: box.scrollTop + line.top - top - (room - line.height) / 2,
                behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
}
function edit(path, label) {
  if (state.busy) return;
  state.edit = path;
  state.editSource = {id: state.job.id, revision: state.job.revision, request_id: crypto.randomUUID()};
  const item = getField(path);
  $("#edit-title").textContent = `Review ${label.toLowerCase()}`;
  $("#original-value").textContent = `Original draft: ${item.original_value ?? "Unknown"}`;
  $("#edit-value").value = item.value ?? "";
  const isPosition = path.endsWith("/position");
  $("#edit-value").hidden = isPosition;
  $("#position-value").hidden = !isPosition;
  $("#position-value").value = isPosition ? item.value : "Unclear";
  $("#edit-review").value = item.review === "READY" ? "NEEDS_REVIEW" : item.review;
  $("#organization-applicability").hidden = !path.endsWith("/organization");
  $("#organization-na").checked = Boolean(item.not_applicable);
  $("#edit-value").disabled = Boolean(item.not_applicable);
  $("#edit-review").disabled = Boolean(item.not_applicable);
  $("#edit-evidence").innerHTML = evidenceChoices("edit-evidence", item.evidence);
  $("#edit-error").hidden = true;
  const asker = INFERRED_ASKER.test(item.flag || "") && item.review !== "APPROVED";
  let heard = item.evidence;
  if (asker) {
    try { heard = getField(path.replace(/\/asker(\/name)?$/, "/question")).evidence; } catch { heard = item.evidence; }
  }
  setListenBar($("#edit-listen"), firstTimed(heard) || firstTimed(item.evidence), [placeLabel(path), label], asker ? ASKER_LEAD : 0);
  $("#edit-dialog").showModal();
  (isPosition ? $("#position-value") : $("#edit-value")).focus();
}
async function readFiles(files) {
  const documents = [];
  const errors = [];
  for (const file of files) {
    if (file.size > 8 * 1024 * 1024) { errors.push(`${file.name} is larger than 8 MB.`); continue; }
    try { documents.push(JSON.parse(await file.text())); }
    catch { errors.push(`${file.name} is not valid JSON; it was skipped.`); }
  }
  return {documents, errors};
}
async function importDocuments(documents) {
  const result = await api("/api/import", {documents});
  await refresh();
  setView("hearings");
  const failed = result.filter((r) => r.processing === "Failed");
  if (failed.length) fail(new Error(`${failed.length} input(s) need attention. ${failed.map((r) => r.error).join(" ")}`));
  else toast(`Imported ${result.length} hearing(s). Saved drafts were reused where available.`);
}
function showUpdate(update) {
  if (!update.available) return;
  $("#update-note").innerHTML = `<span><strong>Update available:</strong> ${escapeHTML(update.latest)}. ${update.can_install ? "Saved hearings and corrections are kept." : escapeHTML(update.reason || "")}</span>`
    + (update.can_install ? '<button class="primary small" type="button" data-action="install-update">Update and restart</button>' : "");
  $("#update-note").hidden = false;
}
function reviewFields(node, path = "", out = []) {
  if (node && typeof node === "object" && !Array.isArray(node) && "value" in node && "evidence" in node && "review" in node) {
    out.push({path, item: node});
  } else if (Array.isArray(node)) {
    node.forEach((value, index) => reviewFields(value, `${path}/${index}`, out));
  } else if (node && typeof node === "object") {
    for (const [key, value] of Object.entries(node)) {
      if (!["segments", "hearing", "review_log", "extraction_provenance", "coverage_ledger"].includes(key)) reviewFields(value, `${path}/${key}`, out);
    }
  }
  return out;
}
function unflaggedApprovals(node, prefix) {
  // Flagged items, missing values, whole-hearing issues and no-question decisions need a person.
  return reviewFields(node, prefix).filter(({path, item}) => item.review !== "APPROVED" && !item.flag
    && (item.value || item.not_applicable) && !path.startsWith("/issues") && !path.endsWith("/questions_none"))
    .map(({path}) => ({path, review: "APPROVED"}));
}
// Blanks the copied note marks ⚠: a bill, position, name, organization or reply. Blanks it doesn't
// print (a bill title, or a Q&A's bill or role) aren't visited.
const MARKED_BLANK = /^\/units\/\d+\/(bill|position)$|\/(name|organization|response_resolution)$/;
function blankHint(node) {
  const path = node.dataset.field || "";
  return "No ⚠ flags left. " + (node.matches("button") ? "This testimony has no Q&A or note, so the copied note marks it ⚠. Click No questions if none were asked."
    : path.endsWith("/organization") ? "This organization is blank, so the copied note marks the name ⚠. Click Not applicable if they spoke for themselves; otherwise Edit."
    : path.endsWith("/response_resolution") ? "No reply is recorded, so the copied note marks this Q&A ⚠. Click No response (comment only) if none was needed; otherwise add the reply."
    : "This item is blank, so the copied note marks it ⚠. Click Edit to fill it in.");
}
function nextFlag() {
  let flags = [...document.querySelectorAll("#review-view .field.flagged")];
  // With every flag handled, go on to what the copied note still marks ⚠: the blanks above, and a
  // testimony with no Q&A and no note, through its No questions button.
  const blanks = !flags.length;
  if (blanks) {
    flags = [...document.querySelectorAll("#review-view .field.blank, #review-view [data-no-questions].blank")]
      .filter((node) => !node.dataset.field || MARKED_BLANK.test(node.dataset.field));
  }
  if (!flags.length) return toast("Nothing is flagged. Everything left is unflagged AI draft.");
  const key = (node) => node.dataset.field ?? `no-questions/${node.dataset.noQuestions}`;
  // Move on from the item this button last showed while it is on screen, whether it was skipped,
  // corrected or approved. Otherwise start from the first one below the toolbar. Focus can't be
  // used: Safari doesn't focus a clicked button.
  const cursor = state.flagCursor?.job === state.job.id
    && [...document.querySelectorAll("#review-view [data-field], #review-view [data-no-questions]")].find((node) => key(node) === state.flagCursor.path);
  const box = cursor && cursor.getBoundingClientRect();
  const next = (box && box.bottom > 0 && box.top < innerHeight
    ? flags.find((node) => cursor.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING)
    : flags.find((node) => node.getBoundingClientRect().top > 120)) || flags[0];
  state.flagCursor = {job: state.job.id, path: key(next)};
  next.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center"});
  (next.matches("button") ? next : next.querySelector("button"))?.focus({preventScroll: true});
  if (blanks) toast(blankHint(next));
}
async function performAction(action) {
  if (state.busy) return toast("Your review is saving. Wait before continuing.");
  if (reviewEditorOpen()) return toast("Save or cancel your open correction before continuing.");
  if (action === "next-flag") return nextFlag();
  if (action === "approve-unflagged") {
    const changes = unflaggedApprovals(state.job.draft, "");
    if (!changes.length) return toast("Everything not flagged is already approved.");
    if (!confirm(`Approve ${changes.length} unflagged item(s)? Flagged (⚠) items stay for you to check.`)) return;
    return saveChanges(changes);
  }
  if (action === "install-update") {
    $("#update-note").textContent = "Downloading and checking the update… Keep this tab open; the app restarts by itself.";
    try {
      const result = await api("/api/update/install", {});
      state.stopped = true;
      $("#source-audio").pause();
      document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
      $("#update-note").textContent = `Installing ${result.installing}. The app will reopen in a new tab in a few seconds; you can close this one.`;
    } catch (error) {
      $("#update-note").textContent = error.message;
    }
    return;
  }
  if (action === "stop") {
    // Stopping abandons a transcription or draft in progress; waiting hearings resume on reopening.
    await refresh().catch(() => {});
    if (state.hearings.some((job) => job.processing === "Transcribing" || job.processing === "Analyzing")
        && !confirm("A hearing is being transcribed or drafted right now. Stopping interrupts it, and you'll need to click Retry next time. Stop anyway?")) return;
    await api("/api/shutdown", {});
    state.stopped = true;
    $("#source-audio").pause();
    const stopping = "Stopping the app. Wait about 15 seconds before opening it again. Your reviews and corrections are saved.";
    $("#save-status").textContent = stopping;
    document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    return toast(stopping);
  }
  if (action === "queue") return setView("hearings");
  if (action === "review" || action === "output") return setView(action);
  if (action === "import") return $("#import-files").click();
  if (action === "text") return $("#text-files").click();
  if (action === "audio-files") return $("#audio-files").click();
  if (action === "audio-folder") return $("#audio-folder").click();
  if (action === "connection") return openConnection();
  if (action === "extraction") return openExtraction();
  if (action === "backup") return openWorkspaceBackup();
  if (action === "macwhisper") return $("#macwhisper-file").click();
  if (action === "clear-evidence") { state.selected.clear(); return renderReview(); }
  if (action === "docx" || action === "snapshot") return downloadSavedExport(action === "docx" ? "docx" : "json");
  if (action === "download") {
    return downloadSavedExport("md");
  }
  if (action === "copy") {
    // Safari only allows a clipboard write while the click is being handled, so the write starts
    // here, before any await, and the saved note fills it in when it arrives.
    const note = savedExportResponse("md").then((response) => response.text());
    const blob = note.then((text) => new Blob([text], {type: "text/plain"}));
    blob.catch(() => {});  // An export failure is reported below, not as a stray rejection.
    try {
      await (typeof ClipboardItem === "function" && navigator.clipboard.write
        ? navigator.clipboard.write([new ClipboardItem({"text/plain": blob})])
        : note.then((text) => navigator.clipboard.writeText(text)));
    } catch {
      await note;  // An export problem, such as a newer save in another tab, keeps its own message.
      throw new Error("Copy didn't work in this browser. Use Export Markdown and copy from that file.");
    }
    // The copied note itself says what is still open: a line with ⚠, or nothing.
    const unmarked = Boolean(state.job.notion) && !(await note).includes("⚠");
    return toast(state.job.review.flags ? `Copied. ${state.job.review.flags} item(s) marked ⚠ still need checking.`
      : state.job.review.status === "APPROVED" ? "Reviewed hearing notes copied."
      : unmarked ? "Hearing notes copied. Nothing in them is marked ⚠." : "Draft copied. Items marked ⚠ still need checking.");
  }
}
document.addEventListener("click", async (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  $("#global-error").hidden = true;
  try {
    const focusKey = buttonFocusKey(target);
    if (focusKey) state.reviewFocus = focusKey;
    if (target.dataset.view) return setView(target.dataset.view);
    if (target.dataset.action) return await performAction(target.dataset.action);
    if (target.dataset.open) return await openHearing(target.dataset.open);
    if (target.dataset.audio) return audioControl(target);
    if (target.dataset.evidence || target.dataset.listen) return highlight(target.dataset.evidence || target.dataset.listen, playingParts(target), Number(target.dataset.lead) || 0);
    if (target.dataset.focusField) return focusDraftField(target.dataset.focusField);
    if (target.dataset.edit) return edit(target.dataset.edit, target.dataset.label);
    if (target.dataset.repair) return openStructure(JSON.parse(target.dataset.repair));
    if (target.dataset.approve) return await saveChanges([{path: target.dataset.approve, review: "APPROVED"}]);
    if (target.dataset.na) return await saveChanges([{path: target.dataset.na, not_applicable: true, review: "APPROVED"}]);
    if (target.dataset.noResponse) {
      return await saveChanges([{path: `${target.dataset.noResponse}/response_resolution`, value: "No response; comment only.", review: "APPROVED"}]);
    }
    if (target.dataset.approveUnit) {
      const index = Number(target.dataset.approveUnit);
      const changes = unflaggedApprovals(state.job.draft.units[index], `/units/${index}`);
      return changes.length ? await saveChanges(changes) : toast("Everything not flagged in this testimony is already approved.");
    }
    if (target.dataset.noQuestions) {
      const index = Number(target.dataset.noQuestions);
      return await saveChanges([], [{op: "rich_confirm_no_questions", path: `/units/${index}`, confirmed: true,
        evidence: state.job.draft.units[index].segment_ids.slice(-3)}]);
    }
    if (target.dataset.jump) return document.getElementById(target.dataset.jump)?.scrollIntoView({block: "start"});
    if (target.dataset.redraft) {
      await api("/api/redraft", {id: target.dataset.redraft});
      await refresh();
      renderQueue();
      return toast("Redrafting with Gemini in the background. The new draft replaces the old one when it's ready.");
    }
    if (target.dataset.rebuild) {
      if (!confirm("Rebuild this draft with the app's latest fixes? It reuses the saved AI reply. The new second check may cost a few cents the first time it runs. Nothing you've reviewed is affected.")) return;
      target.disabled = true;
      toast("Rebuilding the draft. This usually takes under a minute.");
      try { await api("/api/rebuild", {id: target.dataset.rebuild}); } finally { target.disabled = false; }
      await refresh();
      renderQueue();
      const rebuilt = state.hearings.find((job) => job.id === target.dataset.rebuild);
      return toast(rebuilt?.processing === "Complete" ? "Draft rebuilt with the app's latest fixes." : "Rebuilding with the app's latest fixes. The new draft appears when it's ready.");
    }
    if (target.dataset.attachAudio) {
      state.attachAudio = target.dataset.attachAudio;
      return $("#attach-audio-file").click();
    }
    if (target.dataset.attachTranscript) {
      state.attachTranscript = target.dataset.attachTranscript;
      return $("#attach-transcript-file").click();
    }
    if (target.dataset.retry) {
      state.retry = target.dataset.retry;
      const job = state.hearings.find((j) => j.id === state.retry);
      if (job.kind === "audio") {
        await api("/api/retry", {id: job.id});
        await refresh();
        render();
        return toast("This hearing was queued for retry. Completed work is preserved.");
      }
      $("#retry-error").textContent = job.error;
      $("#retry-dialog").showModal();
    }
  } catch (error) { fail(error); }
});
document.addEventListener("change", event => {
  if (event.target.id !== "transcript-filter") return;
  state.transcriptFilter = event.target.value === "unreferenced" ? "unreferenced" : "all";
  applyTranscriptFilter();
});
$("#edit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#save-edit").disabled = true;
  try {
    const notApplicable = state.edit.endsWith("/organization") && $("#organization-na").checked;
    const value = notApplicable ? null : state.edit.endsWith("/position") ? $("#position-value").value : $("#edit-value").value.trim() || null;
    const change = {path: state.edit, value, review: notApplicable ? "APPROVED" : $("#edit-review").value};
    if (state.edit.endsWith("/organization")) change.not_applicable = notApplicable;
    const evidence = selectedEvidence("edit-evidence");
    const operations = JSON.stringify(evidence) === JSON.stringify(getField(state.edit).evidence) ? [] : [{op: "set_evidence", path: state.edit, evidence}];
    await saveChanges([change], operations, state.editSource);
    $("#edit-dialog").close();
    restoreReviewFocus();
  } catch (error) {
    $("#edit-error").textContent = error.message;
    $("#edit-error").hidden = false;
  } finally { $("#save-edit").disabled = false; }
});
for (const selector of ["#close-dialog", "#cancel-edit"]) $(selector).addEventListener("click", () => { if (!state.busy) { $("#edit-dialog").close(); restoreReviewFocus(); } });
$("#organization-na").addEventListener("change", (event) => {
  $("#edit-value").disabled = event.target.checked;
  $("#edit-review").disabled = event.target.checked;
  $("#edit-review").value = event.target.checked ? "APPROVED" : "NEEDS_REVIEW";
});
$("#structure-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#save-structure").disabled = true;
  try {
    // A repair that changes nothing (a Q&A "moved" to where it already is) is not saved.
    const operation = structureOperation();
    if (operation) await saveChanges([], [operation], state.structureSource);
    $("#structure-dialog").close();
    restoreReviewFocus();
  } catch (error) {
    $("#structure-error").textContent = error.message;
    $("#structure-error").hidden = false;
  } finally { $("#save-structure").disabled = false; }
});
for (const selector of ["#close-structure", "#cancel-structure"]) $(selector).addEventListener("click", () => { if (!state.busy) { $("#structure-dialog").close(); restoreReviewFocus(); } });
for (const selector of ["#edit-dialog", "#structure-dialog"]) $(selector).addEventListener("cancel", (event) => {
  if (state.busy) event.preventDefault();
});
async function savedExportResponse(format) {
  if (!state.job?.draft) throw new Error("Open a hearing draft before exporting.");
  if (state.saveError) throw new Error("The last save failed. Keep your correction open or reload the saved hearing before exporting.");
  const response = await fetch(`/api/export/${encodeURIComponent(state.job.id)}?format=${format}&revision=${encodeURIComponent(state.job.revision)}`, {cache: "no-store"});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || "Could not export the saved hearing.");
  }
  return response;
}
async function downloadSavedExport(format) {
  const response = await savedExportResponse(format);
  const disposition = response.headers.get("Content-Disposition") || "";
  const name = disposition.match(/filename="([^"\r\n]+)"/i)?.[1] || `${state.job.id}-hearing-notes.${format}`;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name.replace(/[\\/]/g, "-");
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(format === "docx" ? "Word document exported with current review labels." : format === "md" ? "Saved Markdown notes exported." : "Saved hearing data exported. Recordings remain in the local workspace.");
}
$("#close-retry").addEventListener("click", () => $("#retry-dialog").close());
$("#choose-replacement").addEventListener("click", () => $("#retry-file").click());
$("#retry-original").addEventListener("click", async () => {
  try {
    const result = await api("/api/retry", {id: state.retry});
    await refresh();
    $("#retry-dialog").close();
    render();
    if (result.error) fail(new Error(result.error));
  } catch (error) { fail(error); }
});
$("#retry-file").addEventListener("change", async (event) => {
  try {
    const {documents, errors} = await readFiles(event.target.files);
    if (errors.length || !documents.length) throw new Error(errors.join(" ") || "Choose a corrected transcript.");
    const result = await api("/api/retry", {id: state.retry, document: documents[0]});
    await refresh();
    $("#retry-dialog").close();
    render();
    if (result.error) fail(new Error(result.error)); else toast("Corrected input processed. The rest of the workspace was preserved.");
  } catch (error) { fail(error); }
  finally { event.target.value = ""; }
});
$("#import-files").addEventListener("change", async (event) => {
  try {
    const {documents, errors} = await readFiles(event.target.files);
    if (documents.length) await importDocuments(documents);
    if (errors.length) fail(new Error(errors.join(" ")));
  }
  catch (error) { fail(error); }
  finally { event.target.value = ""; }
});
window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
$("#macwhisper-file").addEventListener("change", async (event) => {
  try {
    const {documents, errors} = await readFiles(event.target.files);
    if (errors.length || !documents.length) throw new Error(errors.join(" ") || "Choose a MacWhisper JSON export.");
    state.macwhisper = documents[0];
    $("#mw-title").value = event.target.files[0].name.replace(/\.json$/i, "").replace(/[_-]/g, " ");
    $("#mw-source").value = event.target.files[0].name;
    $("#mw-date").value = "";
    $("#mw-committee").value = "";
    $("#mw-public").checked = false;
    $("#mw-error").hidden = true;
    $("#macwhisper-dialog").showModal();
    $("#mw-title").focus();
  } catch (error) { fail(error); }
  finally { event.target.value = ""; }
});
for (const selector of ["#close-macwhisper", "#cancel-macwhisper"]) $(selector).addEventListener("click", () => $("#macwhisper-dialog").close());
async function textFileContent(file) {
  if (!file || !/\.(txt|srt|vtt)$/i.test(file.name) || !file.size || file.size > 4 * 1024 * 1024) {
    throw new Error("Choose a nonempty .txt, .srt or .vtt file no larger than 4 MB.");
  }
  try {
    return new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(await file.arrayBuffer());
  } catch (error) {
    throw new Error(`${file.name}: save/export the transcript as UTF-8 text.`);
  }
}
$("#text-files").addEventListener("change", (event) => {
  try {
    const files = [...event.target.files];
    if (!files.length || files.length > 30 || files.some((f) => !/\.(txt|srt|vtt)$/i.test(f.name))) {
      throw new Error("Choose between 1 and 30 TXT/SRT/VTT transcripts.");
    }
    state.textFiles = files;
    $("#text-committee").value = state.job?.hearing.committee || "";
    $("#text-public").checked = false;
    $("#text-error").hidden = true;
    $("#text-progress").textContent = "";
    $("#text-file-details").innerHTML = files.map((file, i) => {
      const suggested = file.name.match(/(20\d{2})[-_.](\d{2})[-_.](\d{2})/);
      return `<div class="audio-detail"><div class="filename">${escapeHTML(file.name)}</div>
        <label for="text-title-${i}">Hearing title</label><input id="text-title-${i}" required maxlength="200" value="${escapeHTML(file.name.replace(/\.(txt|srt|vtt)$/i, "").replace(/[_-]/g, " "))}">
        <label for="text-date-${i}">Confirm hearing date</label><input id="text-date-${i}" type="date" required value="${suggested ? `${suggested[1]}-${suggested[2]}-${suggested[3]}` : ""}">
      <label for="text-link-${i}">Hearing page link <span class="muted">(optional, helps the AI match bills)</span></label><input id="text-link-${i}" type="url" inputmode="url" placeholder="https://malegislature.gov/Events/Hearings/Detail/…"></div>`;
    }).join("");
    $("#text-dialog").showModal();
    $("#text-committee").focus();
  } catch (error) { fail(error); }
  finally { event.target.value = ""; }
});
for (const id of ["#close-text", "#cancel-text"]) $(id).addEventListener("click", () => $("#text-dialog").close());
$("#text-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  for (const id of ["#save-text", "#cancel-text", "#close-text"]) $(id).disabled = true;
  const errors = [];
  let last = null;
  try {
    for (let i = 0; i < state.textFiles.length; i++) {
      const file = state.textFiles[i];
      $("#text-progress").textContent = `Importing ${i + 1} of ${state.textFiles.length}: ${file.name}`;
      try {
        const result = await api("/api/text", {
          text: await textFileContent(file), filename: file.name, source_scope: "public_hearing",
          hearing: {title: $(`#text-title-${i}`).value.trim(), date: $(`#text-date-${i}`).value,
            committee: $("#text-committee").value.trim(), source_file: file.name,
            hearing_url: $(`#text-link-${i}`).value.trim()},
        });
        if (result.processing === "Failed") errors.push(`${file.name}: ${result.error}`);
        else last = result.id;
      } catch (error) { errors.push(`${file.name}: ${error.message}`); }
    }
    await refresh();
    $("#text-dialog").close();
    if (last) await openHearing(last); else setView("hearings");
    if (errors.length) fail(new Error(errors.join(" ")));
    else toast("Transcripts imported. Check source evidence and speaker attribution; subtitle times need alignment review.");
  } finally {
    for (const id of ["#save-text", "#cancel-text", "#close-text"]) $(id).disabled = false;
  }
});
$("#macwhisper-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#save-macwhisper").disabled = true;
  try {
    const result = await api("/api/macwhisper", {
      export: state.macwhisper,
      hearing: {title: $("#mw-title").value.trim(), date: $("#mw-date").value,
        committee: $("#mw-committee").value.trim(), source_file: $("#mw-source").value.trim()},
      synthetic: false,
    });
    await refresh();
    $("#macwhisper-dialog").close();
    if (result.processing === "Failed") { setView("hearings"); throw new Error(result.error); }
    await openHearing(result.id);
    toast("MacWhisper transcript imported. Its draft still needs human review.");
  } catch (error) {
    $("#mw-error").textContent = error.message;
    $("#mw-error").hidden = false;
    if (!$("#macwhisper-dialog").open) fail(error);
  } finally { $("#save-macwhisper").disabled = false; }
});

function syncPlayer() {
  const audio = $("#source-audio");
  const hasAudio = Boolean(state.job?.audio_url);
  $("#audio-player").hidden = state.view !== "review" || !hasAudio;
  const identity = hasAudio ? state.job.id : null;
  if (identity !== state.mediaJob) {
    audio.pause();
    audio.removeAttribute("src");
    state.mediaJob = identity;
    $("#audio-error").hidden = true;
    $("#audio-time").textContent = "00:00:00";
    $("#audio-duration").textContent = "00:00:00";
    $("#audio-seek").value = 0;
    $("#audio-seek").max = 1;
    $("#player-now").textContent = "";
    $("#player-now").hidden = true;
    if (identity) audio.src = state.job.audio_url;
    audio.load();
  }
  if (hasAudio) $("#player-source").textContent = `Source recording · ${state.job.audio.filename}`;
  if (!$("#toast").hidden) placeToast();
}
function seekSegment(id, parts = [], lead = 0) {
  const segment = state.job?.draft?.segments.find((item) => item.id === id);
  if (timed(segment)) playFrom(Math.max(0, segment.start - lead), parts);
}
// Plays the recording from a time. parts name what is playing, e.g. ["Testimony 7", "Main argument"].
function playFrom(start, parts = []) {
  if (!state.job?.audio_url || !Number.isFinite(start)) return;
  const audio = $("#source-audio");
  const expectedJob = state.job.id;
  const seek = () => {
    if (state.job?.id !== expectedJob) return;
    if (!Number.isFinite(audio.duration) || start >= audio.duration) {
      const message = "This timestamp is outside the attached recording. Check that it is the original audio used for this transcript.";
      $("#audio-error").textContent = message;
      $("#audio-error").hidden = false;
      const shown = document.querySelector("dialog[open] .error-text");  // The bottom player is behind an open dialog.
      if (shown) { shown.textContent = message; shown.hidden = false; }
      return;
    }
    $("#audio-error").hidden = true;
    audio.currentTime = start;
    $("#player-now").textContent = [...parts, `from ${clock(start)}`].filter(Boolean).join(" · ");
    $("#player-now").hidden = false;
    audio.play().catch(() => toast("Audio is positioned at the evidence timestamp. Press Play to listen."));
  };
  if (audio.readyState >= 1) seek();
  else audio.addEventListener("loadedmetadata", seek, {once: true});
}
function nudge(seconds) {
  const audio = $("#source-audio");
  if (!state.job?.audio_url || !Number.isFinite(audio.duration)) return;
  audio.currentTime = Math.min(Math.max(audio.currentTime + seconds, 0), audio.duration);
}
// The compact controls inside the modal Edit and repair dialogs, where the bottom player can't be reached.
function setListenBar(bar, segment, parts = [], lead = 0) {
  bar.hidden = !state.job?.audio_url;  // Play, pause and ±10 s work even when this item has no time of its own.
  if (bar.hidden) return;
  const listen = bar.querySelector('[data-audio="listen"]');
  listen.hidden = !segment;
  if (segment) {
    const start = Math.max(0, segment.start - lead);
    listen.textContent = lead ? `▶ Listen from just before ${clock(segment.start)}` : `▶ Listen from ${clock(segment.start)}`;
    listen.dataset.start = start;
    listen.dataset.place = JSON.stringify(parts);
  }
  syncListenBars();
}
function repairListen(operation) {
  // The item being repaired: a Q&A, statement or testimony, never the whole draft.
  let path = operation.path ? operation.path.replace(/\/(arguments|questions|testifiers|responses)$/, "")
    : Number.isInteger(operation.question) ? `/questions/${operation.question}`
    : Number.isInteger(operation.argument) ? `/testimonies/${operation.testimony}/statements/${operation.statement}/arguments/${operation.argument}`
    : Number.isInteger(operation.statement) ? `/testimonies/${operation.testimony}/statements/${operation.statement}`
    : Number.isInteger(operation.testimony) ? `/testimonies/${operation.testimony}` : "";
  if (operation.group_id) {
    const index = (state.job.draft.testimony_groups || []).findIndex((group) => group.id === operation.group_id);
    path = index < 0 ? "" : `/testimony_groups/${index}`;
  }
  let node = null;
  try { node = /\/\d+/.test(path) ? getField(path) : null; } catch { node = null; }
  const ids = [];
  (function collect(value) {
    if (Array.isArray(value)) return value.forEach(collect);
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      if ((key === "evidence" || key === "segment_ids") && Array.isArray(child)) ids.push(...child);
      else if (key !== "review_log") collect(child);
    }
  })(node);
  setListenBar($("#structure-listen"), firstTimed(ids), [placeLabel(path)]);
}
function syncListenBars() {
  const audio = $("#source-audio");
  for (const bar of document.querySelectorAll("[data-listen-bar]")) {
    if (bar.hidden) continue;
    const toggle = bar.querySelector('[data-audio="toggle"]');
    toggle.textContent = audio.paused ? "Play" : "Pause";
    toggle.setAttribute("aria-label", audio.paused ? "Play the recording" : "Pause the recording");
    bar.querySelector("[data-audio-time]").textContent = clock(audio.currentTime);
  }
}
function audioControl(button) {
  const audio = $("#source-audio");
  const action = button.dataset.audio;
  if (action === "listen") return playFrom(Number(button.dataset.start), JSON.parse(button.dataset.place || "[]"));
  if (action === "back" || action === "forward") return nudge(action === "back" ? -10 : 10);
  if (action === "toggle" && state.job?.audio_url) {
    if (!audio.paused) return audio.pause();
    audio.play().catch((error) => {
      const message = button.closest("dialog")?.querySelector(".error-text");
      if (message) { message.textContent = `Could not start audio playback: ${error.message}`; message.hidden = false; }
    });
  }
}
const sourceAudio = $("#source-audio");
sourceAudio.addEventListener("loadedmetadata", () => {
  if (Number.isFinite(sourceAudio.duration)) {
    $("#audio-seek").max = sourceAudio.duration;
    $("#audio-duration").textContent = time(sourceAudio.duration);
  }
});
sourceAudio.addEventListener("timeupdate", () => {
  $("#audio-time").textContent = time(sourceAudio.currentTime);
  $("#audio-seek").value = sourceAudio.currentTime;
  syncListenBars();
});
sourceAudio.addEventListener("play", () => {
  $("#audio-play").textContent = "Ⅱ";
  $("#audio-play").setAttribute("aria-label", "Pause source audio");
  syncListenBars();
});
sourceAudio.addEventListener("pause", () => {
  $("#audio-play").textContent = "▶";
  $("#audio-play").setAttribute("aria-label", "Play source audio");
  syncListenBars();
});
sourceAudio.addEventListener("error", () => {
  $("#audio-error").textContent = "The recording could not be played. Check its format and that the saved workspace still contains the audio file.";
  $("#audio-error").hidden = false;
});
$("#audio-play").addEventListener("click", async () => {
  try {
    if (sourceAudio.paused) await sourceAudio.play(); else sourceAudio.pause();
  } catch (error) { fail(new Error(`Could not start audio playback: ${error.message}`)); }
});
$("#audio-seek").addEventListener("input", (event) => {
  if (Number.isFinite(sourceAudio.duration)) sourceAudio.currentTime = Number(event.target.value);
});
$("#audio-speed").addEventListener("change", (event) => { sourceAudio.playbackRate = Number(event.target.value); });
$("#audio-volume").addEventListener("input", (event) => { sourceAudio.volume = Number(event.target.value); });

function chooseRecordings(files) {
  const selected = [...files].filter((file) => /\.(mp3|wav|m4a)$/i.test(file.name));
  if (!selected.length) throw new Error("Choose MP3, WAV or M4A hearing recordings.");
  if (selected.length > 30) throw new Error("Add up to 30 recordings at a time.");
  state.audioFiles = selected;
  $("#audio-committee").value = state.job?.hearing.committee || "";
  $("#audio-public").checked = false;
  $("#audio-import-error").hidden = true;
  $("#upload-progress").textContent = "";
  $("#audio-file-details").innerHTML = selected.map((file, index) => {
    const detectedDate = file.name.match(/(20\d{2})[-_.](\d{2})[-_.](\d{2})/);
    const dateSuggestion = detectedDate ? `${detectedDate[1]}-${detectedDate[2]}-${detectedDate[3]}` : "";
    return `<div class="audio-detail"><div class="filename">${escapeHTML(file.webkitRelativePath || file.name)} · ${(file.size / 1024 / 1024).toFixed(1)} MB</div>
      <label for="audio-title-${index}">Hearing title</label><input id="audio-title-${index}" required maxlength="200" value="${escapeHTML(file.name.replace(/\.[^.]+$/, "").replace(/_/g, " "))}">
      <label for="audio-date-${index}">Hearing date</label><input id="audio-date-${index}" type="date" required value="${escapeHTML(dateSuggestion)}">
      <label for="audio-link-${index}">Hearing page link <span class="muted">(optional, helps the AI match bills)</span></label><input id="audio-link-${index}" type="url" inputmode="url" placeholder="https://malegislature.gov/Events/Hearings/Detail/…"></div>`;
  }).join("");
  $("#audio-import-dialog").showModal();
  $("#audio-committee").focus();
}
for (const selector of ["#audio-files", "#audio-folder"]) $(selector).addEventListener("change", (event) => {
  try { chooseRecordings(event.target.files); } catch (error) { fail(error); }
  finally { event.target.value = ""; }
});
for (const selector of ["#close-audio-import", "#cancel-audio-import"]) $(selector).addEventListener("click", () => $("#audio-import-dialog").close());
async function uploadRecording(file, metadata) {
  if (!file.size || file.size > 1024 * 1024 * 1024) throw new Error(`${file.name}: choose a nonempty recording smaller than 1 GB.`);
  const response = await fetch(`/api/audio/upload?metadata=${encodeURIComponent(JSON.stringify(metadata))}`, {
    method: "POST", headers: {"X-Review-Token": state.token, "Content-Type": "application/octet-stream"}, body: file,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(`${file.name}: ${result.error}`);
  return result;
}
$("#audio-import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#save-audio-import").disabled = true;
  $("#cancel-audio-import").disabled = true;
  $("#close-audio-import").disabled = true;
  const errors = [];
  let reused = 0;
  try {
    for (let index = 0; index < state.audioFiles.length; index++) {
      const file = state.audioFiles[index];
      $("#upload-progress").textContent = `Saving recording ${index + 1} of ${state.audioFiles.length}: ${file.name}`;
      try {
        const result = await uploadRecording(file, {
          title: $(`#audio-title-${index}`).value.trim(), date: $(`#audio-date-${index}`).value,
          committee: $("#audio-committee").value.trim(), source_file: file.name, source_scope: "public_hearing",
          hearing_url: $(`#audio-link-${index}`).value.trim(),
        });
        if (result.reused) reused++;
      } catch (error) { errors.push(error.message); }
    }
    await refresh();
    $("#audio-import-dialog").close();
    setView("hearings");
    if (errors.length) fail(new Error(errors.join(" ")));
    else toast(`${state.audioFiles.length} recording(s) saved. ${reused ? `${reused} existing hearing(s) reused with edits preserved.` : "Processing continues in the queue."}`);
  } finally {
    $("#save-audio-import").disabled = false;
    $("#cancel-audio-import").disabled = false;
    $("#close-audio-import").disabled = false;
  }
});
$("#attach-audio-file").addEventListener("change", async (event) => {
  try {
    const file = event.target.files[0];
    if (!file) return;
    const job = await api(`/api/hearings/${encodeURIComponent(state.attachAudio)}`);
    await uploadRecording(file, {...job.hearing, source_file: file.name, source_scope: "public_hearing", attach_to: job.id});
    await refresh();
    await openHearing(job.id);
    toast("Source recording attached. Existing review work was preserved.");
  } catch (error) { fail(error); }
  finally { event.target.value = ""; }
});
$("#attach-transcript-file").addEventListener("change", async (event) => {
  try {
    const file = event.target.files[0];
    if (!file) return;
    if (/\.(txt|srt|vtt)$/i.test(file.name)) {
      await api("/api/text", {attach_to: state.attachTranscript, text: await textFileContent(file), filename: file.name});
    } else {
      const {documents, errors} = await readFiles(event.target.files);
      if (errors.length || !documents.length) throw new Error(errors.join(" ") || "Choose MacWhisper JSON, TXT, SRT or VTT.");
      await api("/api/audio/transcript", {id: state.attachTranscript, export: documents[0]});
    }
    await refresh();
    setView("hearings");
    toast("Transcript attached. Draft extraction is queued; the original audio stays linked.");
  } catch (error) { fail(error); }
  finally { event.target.value = ""; }
});
async function openConnection() {
  state.connection = await api("/api/transcription");
  $("#connection-status").textContent = state.connection.platform === "macOS"
    ? state.connection.installed ? `mw detected. ${state.connection.model_id ? `Selected model: ${state.connection.model_id}.` : "Choose an installed local model."}`
      : "mw was not found. Install the command-line tool from MacWhisper's Advanced settings."
    : `This computer runs ${state.connection.platform}. MacWhisper runs on macOS; transcript import and source-audio playback remain available here.`;
  $("#check-connection").disabled = state.connection.platform !== "macOS";
  $("#connection-form").hidden = true;
  $("#connection-error").hidden = true;
  $("#connection-dialog").showModal();
}
$("#close-connection").addEventListener("click", () => $("#connection-dialog").close());
$("#check-connection").addEventListener("click", async () => {
  $("#check-connection").disabled = true;
  try {
    const result = await api("/api/transcription/check", {});
    if (!result.models.length) throw new Error("No supported local model was listed. Download a local transcription model in MacWhisper first.");
    $("#transcription-model").innerHTML = result.models.map((id) => `<option value="${escapeHTML(id)}">${escapeHTML(id)}</option>`).join("");
    if (result.models.includes(state.connection.model_id)) $("#transcription-model").value = state.connection.model_id;
    $("#transcription-format").value = state.connection.output_format || "srt";
    $("#connection-version").textContent = result.version;
    $("#connection-form").hidden = false;
  } catch (error) {
    $("#connection-error").textContent = error.message;
    $("#connection-error").hidden = false;
  } finally { $("#check-connection").disabled = false; }
});
$("#connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#save-connection").disabled = true;
  try {
    state.connection = await api("/api/transcription/configure", {model_id: $("#transcription-model").value, output_format: $("#transcription-format").value});
    $("#connection-dialog").close();
    renderQueue();
    toast("MacWhisper model saved. Waiting recordings are queued for transcription.");
  } catch (error) {
    $("#connection-error").textContent = error.message;
    $("#connection-error").hidden = false;
  } finally { $("#save-connection").disabled = false; }
});
let polling = false;
setInterval(async () => {
  if (!state.token || state.stopped || polling) return;
  polling = true;
  try {
    const before = JSON.stringify(state.hearings);
    await refresh();
    if (before !== JSON.stringify(state.hearings)) renderQueue();
    updateProgress();
    // A dialog may have deferred an earlier revision. Check it on every poll,
    // even when the queue summary has already caught up with that save.
    if (state.job) {
      const summary = state.hearings.find((job) => job.id === state.job.id);
      if (summary && (summary.processing !== state.job.processing || summary.revision !== state.job.revision || summary.error !== state.job.error) && !state.busy && !reviewEditorOpen()) {
        const requested = {id: state.job.id, revision: state.job.revision};
        const updated = await api(`/api/hearings/${encodeURIComponent(requested.id)}`);
        if (!state.busy && !reviewEditorOpen() && !state.saveError &&
            state.job?.id === requested.id && state.job.revision === requested.revision) {
          state.job = updated;
          renderReview();
          renderOutput();
          syncPlayer();
        }
      }
    }
  } catch (error) {
    $("#save-status").textContent = "Connection unavailable. Keep the app running, then reload.";
  } finally { polling = false; }
}, 1000);

(async function start() {
  try {
    state.token = (await api("/api/session")).token;
    api("/api/version").then((version) => { $("#app-version").textContent = version.label; }).catch(() => {});
    api("/api/update").then((update) => { showUpdate(update); if (update.auto) performAction("install-update"); }).catch(() => {});
    await refresh();
    const workspace = await api("/api/workspace");
    if (workspace.set_aside) fail(new Error(`${workspace.set_aside} saved hearing(s) could not be opened and were set aside unchanged. The rest of the workspace is available; the original saved file is kept in ${workspace.directory}/backups.`));
    state.connection = await api("/api/transcription");
    const drafting = await api("/api/extraction");
    state.extractionProvider = drafting.provider;
    $("#extraction-note").textContent = extractionNote(drafting);
    setView(location.hash.slice(1) || "hearings");
  } catch (error) {
    $("#save-status").textContent = "Workspace unavailable";
    fail(new Error(`Could not connect to the local app. Keep its Python server running and reload. ${error.message}`));
  }
})();

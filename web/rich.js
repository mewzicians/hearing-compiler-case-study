"use strict";

function hasSpeakers(draft) { return draft.segments.some(segment => !segment.speaker_unavailable); }
function openFlagCount(node) { return reviewFields(node).filter(({item}) => item.flag && item.review !== "APPROVED").length; }
function richLabels(labels) {
  return labels.length ? labels.map(escapeHTML).join(", ") : "Voice mapping unavailable";
}
function richRepair(op, label, path) { return repairButton(op, label, {path}); }
function richQuestion(q, path) {
  return `<section class="witness-group"><div class="section-actions"><h3>Question exchange</h3>${richRepair("rich_remove", "Remove Q&A", path)}</div>
    ${field(path + "/asker/name", "Question asker")}${field(path + "/asker_role", "Question role")}
    ${hasSpeakers(state.job.draft) ? `<p class="muted">${richLabels(q.asker.speaker_labels)} ${richRepair("rich_set_mapping", "Repair asker mapping", path + "/asker")}</p>` : ""}
    ${field(path + "/bill", "Bill association")}${field(path + "/question", "Question")}
    ${q.responses.map((response, index) => `<div class="question-response">${field(`${path}/responses/${index}/respondent/name`, "Respondent")}
      ${hasSpeakers(state.job.draft) ? `<p class="muted">${richLabels(response.respondent.speaker_labels)} ${richRepair("rich_set_mapping", "Repair respondent mapping", `${path}/responses/${index}/respondent`)}</p>` : ""}
      ${field(`${path}/responses/${index}/text`, "Response")}${richRepair("rich_remove", "Remove response", `${path}/responses/${index}`)}</div>`).join("")}
    ${q.responses.length ? "" : `<p class="muted">No retained response · ${escapeHTML(String(q.response_status).replaceAll("_", " "))}. Check for a missed or truncated answer.</p>`}
    ${!q.responses.length && q.response_resolution ? field(path + "/response_resolution", "Missing response source review") : ""}
    ${!q.responses.length && q.response_resolution && !q.response_resolution.value
      ? `<button type="button" class="secondary small" data-no-response="${escapeHTML(path)}">No response (comment only)</button>` : ""}
    ${richRepair("rich_add_response", "Add response", path + "/responses")}
    ${richRepair("rich_move_question", "Repair testimony association", path)}</section>`;
}
// Points a second AI check thinks the notes left out. Listed beside the testimony, never added to the notes.
function possiblyMissed(draft, unit, i) {
  const gaps = (draft.second_check?.missed || []).filter(gap => gap.unit === unit.id);
  if (!gaps.length) return "";
  return `<details class="possibly-missed" open data-focus-target="/units/${i}/possibly_missed"><summary>Possibly missed (${gaps.length})</summary>
    <p class="muted">A second AI check thinks this testimony also made these points, and none of them are in the notes. Listen, then use Add argument for any that matter.</p>
    <ul>${gaps.map(gap => `<li><p>${escapeHTML(gap.point)}</p><p class="muted">Transcript: “${escapeHTML(gap.quote)}” ${evidenceLinks(gap.evidence || [])}</p></li>`).join("")}</ul></details>`;
}
// The hearing committee's members from malegislature.gov, to help name a questioner.
function committeeMembers(draft) {
  const committee = draft.roster?.committee;
  if (!committee?.members?.length) return "";
  return `<details class="testimony-index committee-members"><summary>Committee members (${committee.members.length})</summary>
    <p class="muted">From the ${escapeHTML(committee.name)} page on malegislature.gov, to help name a questioner.</p>
    <ul>${committee.members.map(m => `<li>${escapeHTML(m.name)}${m.role ? ` · ${escapeHTML(m.role)}` : ""} · ${escapeHTML(m.branch)}</li>`).join("")}</ul></details>`;
}
function renderRichReview() {
  const position = captureReviewPosition(), job = state.job, draft = job.draft;
  $("#review-view").innerHTML = `<div class="card hearing-header"><div><h1 id="review-title">${escapeHTML(draft.hearing.title)}</h1>
    <p class="metadata">${escapeHTML(draft.hearing.committee)} · ${escapeHTML(date(draft.hearing.date))}</p><p class="source">${escapeHTML(draft.hearing.source_file)}</p></div>${statusPill(job.review.status)}</div>
    <div class="review-layout"><div class="card draft-pane"><div class="pane-heading"><h2 id="draft-heading" tabindex="-1">Hearing draft</h2><p class="muted">${job.review.approved} of ${job.review.fields} fields reviewed</p></div>
    ${isGeminiDraft() ? `<p class="muted">Gemini draft. Items marked ⚠ need checking against the audio. Unmarked items haven't been checked yet: check names, and listen to any point with a number, percentage, dollar amount or date.</p>
      <div class="review-tools"><strong>${openFlagCount(draft)} to check</strong><button type="button" class="secondary small" data-action="next-flag">Next ⚠</button>
      <button type="button" class="secondary small" data-action="approve-unflagged">Approve everything not flagged</button></div>`
      : '<p class="muted">Older AI draft. Check every claim and attribution before approving.</p>'}
    <details class="testimony-index" open><summary>Testimonies (${draft.units.length})</summary><ol>${draft.units.map((unit, i) => {
      const first = draft.segments.find(s => s.id === unit.segment_ids[0]);
      const flags = openFlagCount(unit);
      return `<li><button type="button" class="text-button" data-jump="unit-${i}">${escapeHTML(unit.bill.value || "Unknown bill")} · ${escapeHTML(unit.testifiers.map(p => p.name.value || "Unknown").join(", ") || "No testifier")}${first ? ` · ${evidenceLabel(first)}` : ""}${flags ? ` · ⚠ ${flags}` : ""}</button></li>`;
    }).join("")}</ol></details>
    ${committeeMembers(draft)}
    ${richRepair("rich_add_unit", "Add testimony", "/units")}
    ${draft.units.map((unit, i) => `<section class="witness-group" id="unit-${i}"><div class="section-actions"><h3>Testimony ${i + 1}</h3>
      <button type="button" class="secondary small" data-approve-unit="${i}">Approve this testimony</button>
      ${!unit.questions.length && unit.questions_none?.review !== "APPROVED" ? `<button type="button" class="secondary small ${unit.questions_none?.value ? "" : "blank"}" data-no-questions="${i}">No questions</button>` : ""}
      ${richRepair("rich_remove", "Remove testimony", `/units/${i}`)}</div>
      ${field(`/units/${i}/bill`, "Bill")}${field(`/units/${i}/bill_title`, "Bill title")}
      ${unit.testifiers.map((person, j) => `<div>${field(`/units/${i}/testifiers/${j}/name`, "Testifier")}${field(`/units/${i}/testifiers/${j}/organization`, "Organization")}
        ${hasSpeakers(draft) ? `<p class="muted">${richLabels(person.speaker_labels)}</p>${richRepair("rich_set_mapping", "Repair voice mapping", `/units/${i}/testifiers/${j}`)}` : ""}
        ${richRepair("rich_remove", "Remove testifier", `/units/${i}/testifiers/${j}`)}</div>`).join("")}
      ${richRepair("rich_add_testifier", "Add testifier", `/units/${i}/testifiers`)}
      ${unit.joint_confirmation ? field(`/units/${i}/joint_confirmation`, "Joint presentation") : ""}
      ${unit.testifiers.length > 1 ? richRepair("rich_confirm_joint", "Review joint presentation", `/units/${i}`) : ""}
      ${field(`/units/${i}/position`, "Position")}<h3>Main arguments</h3>
      ${unit.arguments.map((argument, j) => `<div>${field(`/units/${i}/arguments/${j}/text`, "Main argument")}
        ${hasSpeakers(draft) ? `<p class="muted">Contributors: ${richLabels(argument.contributors)}</p>${richRepair("rich_set_contributors", "Repair contributors", `/units/${i}/arguments/${j}`)}` : ""}
        ${richRepair("rich_remove", "Remove argument", `/units/${i}/arguments/${j}`)}</div>`).join("") || '<p class="muted">No retained argument. Check for omissions.</p>'}
      ${richRepair("rich_add_argument", "Add argument", `/units/${i}/arguments`)}<h3>Question exchanges</h3>
      ${unit.questions.map((q, j) => richQuestion(q, `/units/${i}/questions/${j}`)).join("")}
      ${richRepair("rich_add_question", "Add Q&A", `/units/${i}/questions`)}
      ${unit.questions.length ? "" : `<div data-focus-target="/units/${i}/questions_none"><p class="muted">${unit.questions_none?.review === "APPROVED" ? "✓ No questions confirmed." : escapeHTML(unit.questions_none?.value || "No questions were found in this testimony.")}</p></div>`}
      ${possiblyMissed(draft, unit, i)}</section>`).join("") || '<p class="muted">No retained testimony. Add missed material after source review.</p>'}
    ${draft.unassigned_questions.length ? `<h3>Questions not linked to a testimony</h3>${draft.unassigned_questions.map((q, i) => richQuestion(q, `/unassigned_questions/${i}`)).join("")}` : ""}
    ${draft.issues.length ? `<h3>Other things to check</h3>${draft.issues.map((_, i) => field(`/issues/${i}`, "To check")).join("")}` : ""}
    </div><aside class="card evidence-pane"><div class="pane-heading"><h2>Transcript evidence</h2><button type="button" class="text-button" data-action="clear-evidence">Clear highlight</button></div>
    ${renderReviewFocus(draft)}
    <div class="evidence-content">${draft.segments.map(segment => `<article class="segment ${state.selected.has(segment.id) ? "selected" : ""}" data-segment="${escapeHTML(segment.id)}"><div class="segment-header">
      ${timeButton(segment)}<span>${segment.speaker_unavailable ? "" : escapeHTML(segment.speaker)}</span></div><p>${escapeHTML(segment.text)}</p></article>`).join("")}<p class="transcript-filter-empty" hidden>No unreferenced passages remain. This does not establish completeness or approval.</p></div>
    <div class="audio-note">${draft.source_format === "plain-text-utf8" ? "Text is untimed. Use manual audio playback." : job.audio_url ? "Click ▶ on any time to listen from that point." : "Attach the original recording to listen at these times."}
      ${job.audio_url ? "" : `<button type="button" class="text-button" data-attach-audio="${escapeHTML(job.id)}">Attach MP3</button>`}</div></aside></div>`;
  applyTranscriptFilter();
  restoreReviewPosition(position);
}
function openRichStructure(operation) {
  if (state.busy || reviewEditorOpen()) return toast("Finish the current correction first.");
  state.structure = operation;
  state.structureSource = {id: state.job.id, revision: state.job.revision, request_id: crypto.randomUUID()};
  const labels = [...new Set(state.job.draft.segments.filter(s => !s.speaker_unavailable).map(s => s.speaker))];
  const mapping = `<label for="rich-labels">Original voice labels (comma separated; blank means unavailable)</label><input id="rich-labels" value=""><p class="muted">Available: ${labels.map(escapeHTML).join(", ") || "None"}</p>`;
  let inputs;
  if (operation.op === "rich_remove") inputs = '<label class="check-label"><input type="checkbox" required> Remove this item; retain original source and history.</label>';
  else if (["rich_set_mapping", "rich_set_contributors"].includes(operation.op)) {
    const target = getField(operation.path);
    inputs = mapping.replace('value=""', `value="${escapeHTML((target.speaker_labels || target.contributors).join(", "))}"`);
  } else if (operation.op === "rich_move_question") {
    // Start on where the Q&A is now, so saving without a choice changes nothing.
    const current = operation.path.replace(/\/\d+$/, ""), draft = state.job.draft;
    const segments = new Map(draft.segments.map(s => [s.id, s]));
    const option = (value, label) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}${value === current ? " (current)" : ""}</option>`;
    inputs = `<label for="rich-destination">Testimony association</label><select id="rich-destination">${draft.units.map((u, i) => {
      const first = segments.get(u.segment_ids[0]);
      return option(`/units/${i}/questions`, `Testimony ${i + 1} · ${escapeHTML(u.testifiers.map(p => p.name.value || "Unknown").join(", ") || "No testifier")}${first ? ` · ${evidenceLabel(first)}` : ""}`);
    }).join("")}${option("/unassigned_questions", "Unassigned")}</select>`;
  } else if (["rich_confirm_no_questions", "rich_confirm_joint"].includes(operation.op)) {
    inputs = `<label class="check-label"><input id="rich-confirmed" type="checkbox"> ${operation.op === "rich_confirm_joint" ? "I checked the source; these people presented joint testimony." : "I checked this complete testimony and exchange; no questions were noted."}</label>` + structureEvidence("rich-evidence", "Reviewed source");
  } else {
    inputs = mapping + structureInput("rich-name", "Person name, if supported") + structureInput("rich-organization", "Organization, if applicable")
      + structureInput("rich-bill", "Bill, if supported") + '<label for="rich-position">Position</label><select id="rich-position"><option>Unclear</option><option>Support</option><option>Oppose</option></select>'
      + '<label for="rich-role">Question role</label><select id="rich-role"><option value="">Unknown</option><option value="legislator">Legislator</option><option value="other">Other</option></select>'
      + structureInput("rich-value", "Argument, question or response text", "", false, true)
      + structureEvidence("rich-evidence", "Supporting transcript evidence");
  }
  $("#structure-title").textContent = "Repair hearing draft";
  $("#structure-description").textContent = "Retain source support and uncertainty. Repairs return affected content to review.";
  $("#structure-fields").innerHTML = inputs;
  $("#structure-error").hidden = true;
  repairListen(operation);
  $("#structure-dialog").showModal();
}
function richStructureOperation() {
  const op = {...state.structure};
  if (op.op === "rich_remove") return op;
  if (op.op === "rich_move_question") {
    // Moving to the same place would reorder the Q&A and reset its review, so it is skipped.
    const destination = $("#rich-destination").value;
    return destination === op.path.replace(/\/\d+$/, "") ? null : {...op, destination};
  }
  const labels = $("#rich-labels")?.value.split(",").map(s => s.trim()).filter(Boolean) || [];
  if (["rich_set_mapping", "rich_set_contributors"].includes(op.op)) return {...op, labels};
  const evidence = selectedEvidence("rich-evidence");
  if (["rich_confirm_no_questions", "rich_confirm_joint"].includes(op.op)) return {...op, confirmed: $("#rich-confirmed").checked, evidence};
  return {...op, labels, evidence, name: $("#rich-name").value.trim() || null,
    organization: $("#rich-organization").value.trim() || null, bill: $("#rich-bill").value.trim() || null,
    position: $("#rich-position").value, role: $("#rich-role").value || null, value: $("#rich-value").value.trim() || null};
}
function extractionNote(settings) {
  return settings.provider === "gemini"
    ? "Human review required. New hearings are drafted by Gemini; check the items marked ⚠ against the audio before copying notes."
    : "Human review required. New hearings use basic rules (no AI), which find very little. Turn on Gemini under AI extraction.";
}
function showExtraction(settings) {
  $("#extraction-provider").value = settings.provider;
  $("#gemini-settings").hidden = settings.provider !== "gemini";
  $("#gemini-key").value = "";
  $("#gemini-key-status").textContent = settings.key_saved ? `A key is saved (${settings.key_storage}). Leave the field empty to keep it.` : "No key saved yet.";
  $("#forget-gemini-key").hidden = !settings.key_saved;
}
async function openExtraction() {
  const settings = await api("/api/extraction");
  showExtraction(settings);
  if (settings.retired) $("#extraction-error").textContent = "Local AI drafting was removed from this app. New drafts use the rule baseline until you choose Gemini.";
  $("#extraction-error").hidden = !settings.retired;
  $("#extraction-dialog").showModal();
}
for (const id of ["#close-extraction", "#cancel-extraction"]) $(id).addEventListener("click", () => $("#extraction-dialog").close());
$("#extraction-provider").addEventListener("change", () => { $("#gemini-settings").hidden = $("#extraction-provider").value !== "gemini"; });
$("#forget-gemini-key").addEventListener("click", async () => {
  try {
    showExtraction(await api("/api/extraction/forget-key", {}));
    $("#extraction-note").textContent = extractionNote({provider: "rules"});
    toast("The saved Gemini key was removed from this Mac.");
  } catch (error) { $("#extraction-error").textContent = error.message; $("#extraction-error").hidden = false; }
});
$("#extraction-form").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    const settings = await api("/api/extraction/configure", {provider: $("#extraction-provider").value,
      model: $("#extraction-model").value, api_key: $("#gemini-key").value});
    $("#gemini-key").value = "";
    $("#extraction-note").textContent = extractionNote(settings);
    state.extractionProvider = settings.provider;
    if (state.view === "hearings") renderQueue();
    $("#extraction-dialog").close();
    toast(settings.provider === "gemini" ? "Gemini drafting is on. New hearings will be drafted in the background." : "Rule baseline drafting saved. Existing completed reviews are preserved.");
  } catch (error) { $("#extraction-error").textContent = error.message; $("#extraction-error").hidden = false; }
  finally { if (submit) submit.disabled = false; }
});

async function openWorkspaceBackup() {
  const info = await api("/api/workspace");
  $("#workspace-location").textContent = `Saved workspace: ${info.directory}`;
  $("#restore-workspace").disabled = info.hearings !== 0;
  $("#backup-error").hidden = true;
  $("#backup-progress").textContent = "";
  $("#backup-dialog").showModal();
}
$("#close-backup").addEventListener("click", () => $("#backup-dialog").close());
$("#download-backup").addEventListener("click", async () => {
  $("#download-backup").disabled = true;
  $("#backup-progress").textContent = "Creating and verifying the full backup…";
  try {
    const prepared = await api("/api/workspace/backup-prepare", {});
    const link = document.createElement("a");
    link.href = prepared.download_url;
    link.download = "hearing-workspace-backup.zip";
    link.click();
    $("#backup-progress").textContent = "Verified backup prepared. Check that your browser's download finishes, then keep it in a separate local location.";
  } catch (error) { $("#backup-error").textContent = error.message; $("#backup-error").hidden = false; }
  finally { $("#download-backup").disabled = false; }
});
$("#restore-workspace").addEventListener("click", () => $("#restore-workspace-file").click());
$("#restore-workspace-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  if (!file) return;
  $("#restore-workspace").disabled = true;
  $("#backup-progress").textContent = "Verifying and restoring into this empty workspace…";
  try {
    const response = await fetch("/api/workspace/restore", {method: "POST",
      headers: {"X-Review-Token": state.token, "Content-Type": "application/zip"}, body: file});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Restore failed.");
    await refresh();
    $("#backup-dialog").close();
    setView("hearings");
    toast(`${result.restored} hearing(s) restored with original recordings and review history.`);
  } catch (error) { $("#backup-error").textContent = error.message; $("#backup-error").hidden = false; }
  finally { event.target.value = ""; }
});

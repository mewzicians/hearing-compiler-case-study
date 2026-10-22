"use strict";

function reviewFocusInventory(draft) {
  const fields = [];
  function visit(node, path = "") {
    if (!node || typeof node !== "object") return;
    if (!Array.isArray(node) && ["value", "evidence", "review"].every(key => Object.hasOwn(node, key))) {
      fields.push({path, item: node});
      return;
    }
    if (Array.isArray(node)) return node.forEach((item, index) => visit(item, `${path}/${index}`));
    for (const [key, value] of Object.entries(node)) {
      if (!["segments", "hearing", "review_log", "coverage_ledger", "provenance"].includes(key)) visit(value, `${path}/${key}`);
    }
  }
  visit(draft);
  const known = new Set((draft.segments || []).map(segment => segment.id));
  const used = new Set();
  const candidates = [];
  for (const entry of fields) {
    const {path, item} = entry;
    const issue = /^\/issues\//.test(path);
    const references = (item.evidence || []).filter(id => known.has(id));
    if (!issue && !item.not_applicable && item.value !== null && item.value !== "") references.forEach(id => used.add(id));
    if (item.review === "APPROVED") continue;
    const missing = (item.value === null || item.value === "") && !item.not_applicable;
    const reasons = [];
    if (issue) reasons.push("Review note");
    else {
      if (missing || item.review === "UNRESOLVED") reasons.push("Unresolved value");
      if (!missing && !item.not_applicable && references.length === 0) reasons.push("No current transcript reference");
      if (item.basis === "contextual") reasons.push("Contextual attribution");
      if (item.missing_reason) reasons.push(`Source limit: ${item.missing_reason.replaceAll("_", " ")}`);
    }
    if (reasons.length) candidates.push({path, reasons, evidence: references, value: item.value});
  }
  return {
    candidates,
    unreferenced: (draft.segments || []).filter(segment => !used.has(segment.id)).map(segment => segment.id),
    used: [...used],
  };
}

function reviewFocusLabel(path) {
  const last = path.split("/").pop();
  if (/^\/issues\//.test(path)) return "Review note";
  if (/\/arguments\/\d+(?:\/text)?$/.test(path)) return "Main argument";
  if (/\/responses\/\d+\/text$/.test(path) || last === "response") return "Response";
  const names = {name: "Identity", organization: "Organization", bill: "Bill", bill_title: "Bill title",
    position: "Position", question: "Question", asker: "Question asker", respondent: "Respondent",
    asker_role: "Question role", questions_none: "Question absence", response_resolution: "Missing response",
    confirmation: "Shared testimony"};
  return names[last] || "Draft field";
}

function renderReviewFocus(draft) {
  const inventory = reviewFocusInventory(draft);
  return `<section class="review-focus" aria-label="Review focus">
    <details><summary data-focus-summary="review">Review focus · ${inventory.candidates.length} specific item(s)</summary>
      <p class="muted">These are source or draft issues, not confidence scores. Check other claims too; referenced text does not prove correctness.</p>
      ${inventory.candidates.length ? inventory.candidates.map(item => `<div class="focus-item">
        <button type="button" class="text-button" data-focus-field="${escapeHTML(item.path)}">${escapeHTML(reviewFocusLabel(item.path))}</button>
        <span>${escapeHTML(item.reasons.join(" · "))}</span>
        ${item.value ? `<p>${escapeHTML(item.value)}</p>` : ""}
      </div>`).join("") : "<p>No specific item is detected here. This does not establish accuracy or completeness.</p>"}
    </details>
    <label for="transcript-filter">Transcript view</label>
    <select id="transcript-filter">
      <option value="all" ${state.transcriptFilter !== "unreferenced" ? "selected" : ""}>All original passages</option>
      <option value="unreferenced" ${state.transcriptFilter === "unreferenced" ? "selected" : ""}>Not referenced by draft fields (${inventory.unreferenced.length})</option>
    </select>
    <p class="muted">Unreferenced passages can contain procedure, repetition or missed material. Review them before adding anything; this is not an omission finding.</p>
  </section>`;
}

function applyTranscriptFilter() {
  if (!state.job?.draft) return;
  const unreferenced = new Set(reviewFocusInventory(state.job.draft).unreferenced);
  let visible = 0;
  document.querySelectorAll("#review-view [data-segment]").forEach(node => {
    node.hidden = state.transcriptFilter === "unreferenced" && !unreferenced.has(node.dataset.segment);
    if (!node.hidden) visible++;
  });
  const empty = document.querySelector("#review-view .transcript-filter-empty");
  if (empty) empty.hidden = visible !== 0;
}

function focusDraftField(path) {
  const node = [...document.querySelectorAll("#review-view [data-field], #review-view [data-focus-target]")]
    .find(field => field.dataset.field === path || field.dataset.focusTarget === path);
  if (!node) return;
  node.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center"});
  node.querySelector("button[data-edit], button[data-repair]")?.focus({preventScroll: true});
}

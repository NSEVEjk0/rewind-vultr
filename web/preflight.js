"use strict";
const importState = { initialized: null, selected: new Set(), columns: new Set(), page: 0, planPage: 0, resultPage: 0, unknownPage: 0, dirty: false, editing: false, signature: null };
const importCount = (count, noun) => count + " " + noun + (count === 1 ? "" : "s");

function resetPreflight() {
  Object.assign(importState, { initialized: null, selected: new Set(), columns: new Set(), page: 0, planPage: 0, resultPage: 0, unknownPage: 0, dirty: false, editing: false, signature: null });
}
function welcomeWorkflow(mode) {
  state.mode = mode;
  const importing = mode === "preflight";
  show("recovery-start", !importing); show("import-start", importing);
  $("workspace-kind").textContent = importing ? "Import check" : "Data recovery";
  $("welcome-title").replaceChildren(document.createTextNode(importing ? "Check the next import." : "Undo a bad import."), node("br"), node("span", importing ? "Keep mistakes out." : "Keep today’s work."));
  $("welcome-intro").textContent = importing ? "You meant to change prices, not erase a field or reset a payment. Compare the next CSV with today’s export, hold the risky changes out, and approve a tested update before importing it." : "Wrong prices. Overwritten fields. A bulk update gone wrong. Tell Rewind what happened in your CSV or database. It investigates the mistake, protects later changes it can observe, and prepares a repair for you to review.";
  for (const [id, selected] of [["recovery-mode", !importing], ["import-mode", importing]]) { $(id).classList.toggle("selected", selected); $(id).setAttribute("aria-pressed", String(selected)); }
  $("nav-import").classList.toggle("active", importing); $("nav-recovery").classList.toggle("active", !importing);
}
function chooseWorkflow(mode) {
  if (state.view && progressStates.has(state.view.status)) return;
  if (state.workspace) clearWorkspace();
  welcomeWorkflow(mode); notice("");
}
function importSelectionSignature(view) { return view.id + ":" + view.current.file_sha256 + ":" + view.incoming.file_sha256 + ":" + JSON.stringify(view.import_review.policy); }
function saveImportSelection() {
  if (importState.signature) sessionStorage.setItem(storageKey + ":import-selection", JSON.stringify({ signature: importState.signature, cells: [...importState.selected] }));
}
function initializeImportSelection(view, force = false) {
  if (!view.import_review) return;
  const signature = importSelectionSignature(view);
  if (!force && signature === importState.signature) return;
  importState.signature = signature;
  const eligible = view.import_review.changes.filter(item => item.eligible).map(cellId);
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(storageKey + ":import-selection") || "null"); } catch { /* A stale browser preference does not authorize cells. */ }
  const cells = view.import_plan && !force ? view.import_plan.targets.map(cellId) : !force && saved?.signature === signature && Array.isArray(saved.cells) ? saved.cells : eligible;
  importState.selected = new Set(cells.filter(id => eligible.includes(id)));
  importState.page = 0; importState.unknownPage = 0; saveImportSelection();
}
function importPolicyChanged() {
  importState.dirty = true;
  show("import-review-panel", false);
  $("import-policy-note").textContent = "Your rules changed. Check this import again to update the review.";
  show("import-policy-note", !!state.view?.import_review);
}
function buildImportNumeric(preferred) {
  const select = $("import-numeric"), previous = preferred ?? select.value;
  select.replaceChildren(); const off = node("option", "No numeric check"); off.value = ""; select.append(off);
  for (const column of state.view.incoming.columns.filter(item => importState.columns.has(item.name))) {
    const option = node("option", fieldName(column.name)); option.value = column.name; select.append(option);
  }
  select.value = importState.columns.has(previous) ? previous : "";
  $("import-percent").disabled = !select.value;
}
function buildImportPolicy(view) {
  const policy = view.import_review?.policy;
  importState.columns = new Set(policy?.columns || (view.example ? ["price"] : []));
  $("import-columns").replaceChildren();
  for (const column of view.incoming.columns.filter(item => !item.primary_key)) {
    const label = node("label"), input = document.createElement("input"); input.type = "checkbox"; input.value = column.name; input.checked = importState.columns.has(column.name);
    input.addEventListener("change", () => { if (input.checked) importState.columns.add(column.name); else importState.columns.delete(column.name); buildImportNumeric(); importPolicyChanged(); });
    label.append(input, node("span", fieldName(column.name))); $("import-columns").append(label);
  }
  buildImportNumeric(policy?.numeric_column || (view.example ? "price" : ""));
  $("import-percent").value = policy?.max_percent || 25;
}
function renderPreflight(view) {
  state.view = view; state.mode = "preflight";
  show("welcome", false); show("workspace", false); show("import-workspace", true);
  $("workspace-kind").textContent = "Import check";
  $("nav-import").classList.add("active"); $("nav-recovery").classList.remove("active");
  const busy = progressStates.has(view.status), ready = view.status === "ready";
  $("nav-import").disabled = busy; $("nav-recovery").disabled = busy; $("import-start-over").disabled = busy;
  $("import-message").textContent = view.message; show("privacy-actions", !busy);
  for (const name of ["current", "incoming"]) {
    $("import-" + name + "-count").textContent = view[name] ? view[name].row_count + " records" : "Reading…";
    show("import-download-" + name, !!view[name]);
  }
  if (ready && importState.initialized !== view.id) {
    importState.initialized = view.id; buildImportPolicy(view); initializeImportSelection(view);
  }
  const planning = ready && !!view.import_plan && !importState.editing;
  show("import-policy-panel", ready && !planning);
  show("import-review-panel", ready && !planning && !!view.import_review && !importState.dirty);
  show("import-plan-panel", planning); show("import-progress", busy); show("import-result-panel", view.status === "verified");
  if (busy) {
    $("import-title").textContent = view.status === "inspecting" ? "Reading your CSV files" : "Testing your approved import";
    $("import-progress-title").textContent = { inspecting: "Checking the files in separate environments", queued: "Your import preview is queued", running: "Running the update on a copy", checking: "Checking every value independently", finishing: "Confirming every environment was removed" }[view.status] || "Checking your import";
    $("import-progress-message").textContent = view.message;
  } else if (ready) {
    $("import-title").textContent = planning ? "Review the update you actually want" : view.import_review && !importState.dirty ? "The good updates. The mistakes held out." : "What should this import change?";
    if (view.import_review && !importState.dirty) { initializeImportSelection(view); renderImportReview(); }
    if (planning) renderImportPlan();
  } else if (view.status === "verified") { $("import-title").textContent = "Your import is checked and ready"; renderImportResult(); }
  else { $("import-title").textContent = "The import check did not complete"; notice(view.message); }
}
function renderImportReview() {
  const review = state.view.import_review, changes = review.changes;
  importState.page = Math.min(importState.page, Math.max(0, Math.ceil(changes.length / PAGE_SIZE) - 1));
  const start = importState.page * PAGE_SIZE;
  $("import-review-title").textContent = importCount(review.eligible_count, "update") + " within your rules. " + importCount(review.held_count, "change") + " held out.";
  $("import-review-description").textContent = "All " + review.current_rows + " current records stay. Fields outside " + review.policy.columns.map(fieldName).join(", ") + " are protected.";
  for (const [id, count] of [["import-eligible", review.eligible_count], ["import-held", review.held_count], ["import-unknown", review.unknown_ids.length]]) $(id).textContent = count;
  $("import-changes-body").replaceChildren();
  for (const item of changes.slice(start, start + PAGE_SIZE)) {
    const tr = node("tr"), checkCell = node("td"), input = document.createElement("input");
    tr.classList.toggle("import-held-row", !item.eligible); input.type = "checkbox"; input.checked = importState.selected.has(cellId(item)); input.disabled = !item.eligible;
    input.dataset.cell = cellId(item); input.setAttribute("aria-label", "Include " + item.column + " in record " + item.key.value);
    input.addEventListener("change", () => { if (input.checked) importState.selected.add(cellId(item)); else importState.selected.delete(cellId(item)); saveImportSelection(); renderImportReview(); [...$("import-changes-body").querySelectorAll("input")].find(element => element.dataset.cell === cellId(item))?.focus({ preventScroll: true }); });
    checkCell.append(input);
    const check = node("td", undefined, "import-check-cell"); check.append(node("span", item.eligible ? "Within your rules" : "Held out", "pill " + (item.eligible ? "green" : "amber")));
    for (const reason of item.reasons) check.append(node("small", review.reason_labels[reason]));
    if (item.percent_change !== null && item.reasons.includes("large-numeric-change")) check.append(node("small", item.percent_change + "% change; your limit is " + review.policy.max_percent + "%."));
    tr.append(checkCell, node("td", item.key.value, "record-tag"), node("td", fieldName(item.column)), node("td", cellValue(item.before, item.column), "cell-before"), node("td", cellValue(item.after, item.column), "cell-after"), check); $("import-changes-body").append(tr);
  }
  $("import-selection-count").textContent = importCount(importState.selected.size, "update") + " included";
  $("import-select-all").checked = review.eligible_count > 0 && importState.selected.size === review.eligible_count;
  $("import-select-all").indeterminate = importState.selected.size > 0 && importState.selected.size < review.eligible_count;
  $("import-select-all").disabled = !review.eligible_count;
  $("import-page-info").textContent = changes.length ? "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, changes.length) + " of " + changes.length + " changes" : "No existing values change in this import.";
  $("import-previous").disabled = !importState.page; $("import-next").disabled = start + PAGE_SIZE >= changes.length;
  const limit = state.config?.max_target_cells || 1000;
  $("import-plan-button").disabled = !importState.selected.size || importState.selected.size > limit || importState.dirty;
  $("import-selection-note").textContent = importState.selected.size > limit ? "Include at most " + limit + " cells per import check." : review.eligible_count ? "Held changes and unknown IDs stay out. Deselect any update you do not want." : "No update fits these rules. Revise your CSV or change the intended fields and check again.";
  renderImportUnknowns(review.unknown_ids);
}
function renderImportUnknowns(items) {
  const container = $("import-unknowns"); container.replaceChildren(); container.hidden = !items.length;
  if (!items.length) return;
  importState.unknownPage = Math.min(importState.unknownPage, Math.floor((items.length - 1) / PAGE_SIZE));
  const start = importState.unknownPage * PAGE_SIZE;
  container.append(node("h3", importCount(items.length, "unknown ID") + " will stay out"), node("p", "These records are absent from today's export. This import updates existing records only."));
  const ids = node("div", undefined, "unknown-ids"); for (const item of items.slice(start, start + PAGE_SIZE)) ids.append(node("span", item.key.value, "pill neutral")); container.append(ids);
  if (items.length > PAGE_SIZE) {
    const pagination = node("div", undefined, "pagination"), controls = node("div");
    pagination.append(node("span", "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, items.length) + " of " + items.length));
    for (const [direction, symbol, disabled] of [[-1, "←", !importState.unknownPage], [1, "→", start + PAGE_SIZE >= items.length]]) { const button = node("button", symbol, "secondary compact"); button.disabled = disabled; button.setAttribute("aria-label", direction < 0 ? "Previous unknown IDs" : "Next unknown IDs"); button.addEventListener("click", () => { importState.unknownPage += direction; renderImportUnknowns(items); }); controls.append(button); }
    pagination.append(controls); container.append(pagination);
  }
}
function renderImportCells(kind, items, pageName) {
  importState[pageName] = Math.min(importState[pageName], Math.max(0, Math.ceil(items.length / PAGE_SIZE) - 1));
  const start = importState[pageName] * PAGE_SIZE, body = $("import-" + kind + "-body"); body.replaceChildren();
  for (const item of items.slice(start, start + PAGE_SIZE)) { const tr = node("tr"); tr.append(node("td", item.key.value, "record-tag"), node("td", fieldName(item.column)), node("td", cellValue(item.before, item.column), "cell-before"), node("td", cellValue(item.after, item.column), "cell-after")); body.append(tr); }
  $("import-" + kind + "-page-info").textContent = "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, items.length) + " of " + items.length + " approved updates";
  $("import-" + kind + "-previous").disabled = !importState[pageName]; $("import-" + kind + "-next").disabled = start + PAGE_SIZE >= items.length;
}
function renderImportPlan() {
  const plan = state.view.import_plan;
  $("import-plan-title").textContent = "Update " + plan.target_count + " cells. Keep all " + plan.current_rows + " records.";
  $("import-plan-description").textContent = "Include " + plan.columns.map(fieldName).join(", ") + " updates in " + importCount(plan.selected_rows, "record") + ". Leave out " + importCount(plan.withheld_cells, "other change") + " and " + importCount(plan.unknown_ids, "unknown ID") + ".";
  $("import-approve").textContent = "Approve and test " + plan.target_count + " updates";
  renderImportCells("plan", plan.targets, "planPage");
}
function renderImportResult() {
  const payload = state.view.evidence.payload, result = payload.verification;
  $("import-result-title").textContent = importCount(result.updated_cells, "approved update") + " passed.";
  $("import-result-detail").textContent = "All " + importCount(result.retained_rows, "record") + " kept. " + importCount(result.withheld_cells, "other change") + " and " + importCount(result.unknown_ids, "unknown ID") + " left out. Every value outside approval stayed the same.";
  renderImportCells("result", result.diff, "resultPage");
  $("import-evidence-checks").replaceChildren();
  for (const check of [...result.checks, { label: "Every isolated environment was removed", ok: payload.all_sandboxes_removed }, { label: "Original files stayed unchanged", ok: payload.original_inputs_unchanged }]) { const line = node("div"); line.append(node("span", check.ok ? "✓" : "×"), node("span", check.label)); $("import-evidence-checks").append(line); }
  $("import-runtime-details").replaceChildren();
  for (const text of [payload.sandbox_runs.length + " separate gVisor runs on Vultr. No network access. All removed.", "Import checking uses fixed rules and trusted workers; it makes no model calls.", "Approved import SHA-256: " + payload.approved_import_sha256, "Resulting export SHA-256: " + payload.artifact_sha256, "Checked contract: update existing records by ID; preserve omitted fields."]) $("import-runtime-details").append(node("div", text));
}
function saveImportChanges() {
  const rows = [["record_id", "field", "before", "after"]];
  for (const item of state.view.evidence.payload.verification.diff) rows.push([item.key.value, item.column, item.before.value, item.after.value]);
  const csv = rows.map(row => row.map(value => '"' + String(value).replaceAll('"', '""') + '"').join(",")).join("\r\n") + "\r\n";
  saveBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), "rewind-import-changes.csv");
}
function initializePreflight() {
  for (const id of ["import-mode", "nav-import"]) $(id).addEventListener("click", () => chooseWorkflow("preflight"));
  for (const id of ["recovery-mode", "nav-recovery"]) $(id).addEventListener("click", () => chooseWorkflow("recovery"));
  $("import-start-over").addEventListener("click", () => chooseWorkflow("preflight"));
  $("import-example-button").addEventListener("click", () => withAccess(() => perform(async () => saveWorkspace(await api("/api/import-example", {})), $("import-example-button"))));
  $("import-upload-toggle").addEventListener("click", () => { show("import-upload-panel", $("import-upload-panel").hidden); if (!$("import-upload-panel").hidden) $("import-upload-panel").scrollIntoView({ behavior: "smooth", block: "center" }); });
  $("import-inspect-button").addEventListener("click", () => withAccess(() => perform(async () => {
    const primary_key = $("import-key-input").value; if (!primary_key) throw new Error("Enter the column that uniquely identifies every record.");
    const current = await encodeFile($("import-current-file").files[0], "csv"), incoming = await encodeFile($("import-incoming-file").files[0], "csv");
    saveWorkspace(await api("/api/imports", { current, incoming, primary_key }));
  }, $("import-inspect-button"))));
  $("import-numeric").addEventListener("change", () => { $("import-percent").disabled = !$("import-numeric").value; importPolicyChanged(); });
  $("import-percent").addEventListener("input", importPolicyChanged);
  $("import-check-button").addEventListener("click", () => perform(async () => {
    if (!importState.columns.size) throw new Error("Choose at least one field this import should update.");
    const max_percent = Number($("import-percent").value); if (!Number.isInteger(max_percent) || max_percent < 1 || max_percent > 1000) throw new Error("Use a numeric change limit from 1 to 1,000 percent.");
    const view = await api(workspacePath("/import-review"), { policy: { columns: [...importState.columns], numeric_column: $("import-numeric").value, max_percent }, expected_version: state.view.version });
    if (state.workspace?.id !== view.id) return;
    importState.dirty = false; importState.editing = false; initializeImportSelection(view, true); show("import-policy-note", false); renderPreflight(view);
  }, $("import-check-button")));
  $("import-select-all").addEventListener("change", () => { importState.selected.clear(); if ($("import-select-all").checked) for (const item of state.view.import_review.changes.filter(item => item.eligible)) importState.selected.add(cellId(item)); saveImportSelection(); renderImportReview(); });
  for (const [kind, pageName, render] of [["", "page", renderImportReview], ["plan-", "planPage", renderImportPlan], ["result-", "resultPage", renderImportResult]]) for (const [direction, suffix] of [[-1, "previous"], [1, "next"]]) $("import-" + kind + suffix).addEventListener("click", () => { importState[pageName] += direction; render(); });
  $("import-plan-button").addEventListener("click", () => perform(async () => {
    if (importState.dirty) throw new Error("Check this import again after changing your rules.");
    const targets = state.view.import_review.changes.filter(item => item.eligible && importState.selected.has(cellId(item))).map(item => ({ token: item.token, column: item.column }));
    const view = await api(workspacePath("/import-plan"), { targets, expected_version: state.view.version });
    if (state.workspace?.id !== view.id) return;
    importState.editing = false; importState.planPage = 0; renderPreflight(view);
  }, $("import-plan-button")));
  $("import-edit").addEventListener("click", () => { importState.editing = true; renderPreflight(state.view); });
  $("import-approve").addEventListener("click", () => withAccess(() => perform(async () => { await api(workspacePath("/preflight"), { plan_sha256: state.view.import_plan.sha256 }); await poll(); }, $("import-approve"))));
  for (const kind of ["current", "incoming"]) $("import-download-" + kind).addEventListener("click", () => perform(() => download(kind, "rewind-" + kind + ".csv")));
  $("import-download-approved").addEventListener("click", () => perform(() => download("approved-import", "rewind-approved-import.csv")));
  $("import-download-preview").addEventListener("click", () => perform(() => download("database", "rewind-import-preview.csv")));
  $("import-download-plan").addEventListener("click", () => saveJson(state.view.import_plan, "rewind-approved-import-plan.json"));
  $("import-download-checks").addEventListener("click", () => saveJson(state.view.evidence, "rewind-import-checks.json"));
  $("import-download-changes").addEventListener("click", saveImportChanges);
}

"use strict";
const $ = id => document.getElementById(id);
const state = { config: null, access: "", workspace: null, view: null, comparison: null, selected: new Set(), columns: new Set(), page: 0, planPage: 0, resultPage: 0, protectedPages: {}, editing: false, manual: false, pending: null, timer: null, failures: 0, initialized: null, proposal: null };
const PAGE_SIZE = 10;
const progressStates = new Set(["inspecting", "queued", "diagnosing", "investigating", "diagnostic-checking", "generating", "running", "checking", "finishing"]);
const storageKey = "rewind-private-workspace";
const fieldName = name => state.view?.example && ["price", "price_cents"].includes(name) ? "Price (USD)" : name.replaceAll("_", " ");
const rowLabel = () => state.view?.example ? "invoices" : "records";
const cellId = item => item.token + ":" + item.column;
const flatChoices = () => (state.comparison?.choices || []).flatMap(row => row.differences.map(item => ({ ...item, token: row.token, key: row.key })));

function notice(message) { $("notice").textContent = message; $("notice").hidden = !message; }
function show(id, visible) { $(id).hidden = !visible; }
function node(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
function cellValue(cell, column) {
  if (!cell || cell.type === "null") return "—";
  if (state.view?.example && column === "price_cents" && cell.type === "integer") return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(cell.value) / 100);
  if (state.view?.example && column === "price") return "$" + cell.value;
  return cell.value === "" ? "(empty)" : cell.value;
}
async function api(path, body) {
  const headers = {};
  if (state.workspace) headers.Authorization = "Bearer " + state.workspace.token;
  if (state.access) headers["X-Rewind-Access"] = state.access;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const value = await response.json();
  if (!response.ok) { if (response.status === 403) state.access = ""; throw new Error(value.error || "The request could not be completed."); }
  return value;
}
function workspacePath(suffix = "") { return "/api/workspaces/" + state.workspace.id + suffix; }
async function perform(callback, button) {
  notice(""); if (button) button.disabled = true;
  try { await callback(); } catch (error) { notice(error.message || "The operation could not be completed."); }
  finally { if (button) button.disabled = false; }
}
function withAccess(callback, requireInference = false) {
  if (!(requireInference ? state.config?.available : state.config?.inspection_available)) { notice(state.config?.message || "The recovery worker is not connected yet."); return; }
  if (!state.config.access_required || state.access) { void perform(callback); return; }
  state.pending = callback; $("access-dialog").showModal(); $("access-input").focus();
}
function saveWorkspace(value) {
  clearTimeout(state.timer);
  comparisonRequest++;
  state.workspace = { id: value.id, token: value.token };
  sessionStorage.setItem(storageKey, JSON.stringify(state.workspace));
  state.initialized = null; state.proposal = null; state.editing = false; state.manual = false;
  state.selected.clear(); state.columns.clear(); state.comparison = null; state.page = 0; state.planPage = 0; state.resultPage = 0; state.protectedPages = {};
  state.view = null; renderRows();
  for (const id of ["selection-panel", "plan-panel", "result-panel", "findings-panel", "diagnosis-panel", "privacy-actions"]) show(id, false);
  show("welcome", false); show("workspace", true); void poll();
}
async function poll() {
  clearTimeout(state.timer); if (!state.workspace) return;
  const workspaceId = state.workspace.id;
  try {
    const view = await api(workspacePath());
    if (state.workspace?.id !== workspaceId) return;
    state.failures = 0; await render(view);
    if (progressStates.has(view.status)) state.timer = setTimeout(poll, 1100);
  } catch {
    if (state.workspace?.id !== workspaceId) return;
    state.failures++;
    if (state.failures < 5) state.timer = setTimeout(poll, 2000);
    else notice("The connection was interrupted. Reload to check this saved workspace; no extra model request will be started.");
  }
}
function proposalComparison(proposal) {
  const rows = new Map();
  for (const item of proposal.targets) {
    if (!rows.has(item.token)) rows.set(item.token, { token: item.token, key: item.key, differences: [] });
    rows.get(item.token).differences.push(item);
  }
  return { choices: [...rows.values()], overlaps: proposal.overlaps, already_restored: proposal.already_restored, checkpoint_available: true };
}
async function render(view) {
  state.view = view; show("welcome", false); show("workspace", true);
  $("workspace-label").textContent = view.example ? "BAD-IMPORT EXAMPLE · YOUR RECOVERY WORKSPACE" : "YOUR RECOVERY";
  $("workspace-message").textContent = view.message;
  $("start-over").disabled = progressStates.has(view.status);
  show("checkpoint-card", !!view.has_checkpoint); $("source-cards").classList.toggle("three", !!view.has_checkpoint);
  for (const name of ["backup", "checkpoint", "current"]) {
    if (view[name]) { $(name + "-count").textContent = view[name].row_count + " " + rowLabel(); show("download-" + name, true); }
    else { $(name + "-count").textContent = "Reading…"; show("download-" + name, false); }
  }
  const busy = progressStates.has(view.status), ready = view.status === "ready";
  show("privacy-actions", !busy);
  const proposed = view.diagnosis?.status === "proposed" ? view.diagnosis.proposal : null;
  if (ready && state.initialized !== view.id) {
    state.initialized = view.id; state.manual = !view.checkpoint; buildColumns(view);
    $("instruction").value = view.instruction || (view.example ? "The import multiplied our prices by 100. Undo that bad import, but keep all records, payments and prices changed afterward." : "");
    if (view.plan) {
      const proposedCells = proposed && new Set(proposed.targets.map(cellId));
      state.manual = !proposedCells || !view.plan.targets.every(item => proposedCells.has(cellId(item)));
      state.columns = new Set(state.manual ? view.plan.scope.columns : proposed.columns); buildColumns(view);
      if (state.manual) await loadComparison(false);
      else { state.proposal = proposed.sha256; state.comparison = proposalComparison(proposed); }
      state.selected = new Set(view.plan.targets.map(cellId)); renderRows();
    } else if (view.example && !view.checkpoint) {
      state.columns.add("price_cents"); buildColumns(view); await loadComparison(true);
    }
  }
  if (ready && proposed && state.proposal !== proposed.sha256 && !state.manual) {
    state.proposal = proposed.sha256; state.columns = new Set(proposed.columns);
    state.comparison = proposalComparison(proposed); state.page = 0; state.protectedPages["selection-protected"] = 0;
    state.selected = new Set(proposed.targets.map(cellId)); renderRows();
  }
  const selection = ready && (!view.plan || state.editing) && (state.manual || !!proposed || !!view.diagnosis);
  const investigate = ready && !view.plan && !!view.checkpoint && !view.diagnosis && !state.manual;
  show("diagnosis-panel", investigate); show("findings-panel", ready && !!proposed && !state.manual);
  show("progress-panel", busy); show("selection-panel", selection);
  show("plan-panel", ready && !!view.plan && !state.editing); show("result-panel", view.status === "verified");
  if (busy) {
    const diagnosing = view.active_operation === "diagnose";
    $("workspace-title").textContent = view.status === "inspecting" ? "Reading your file versions" : diagnosing ? "Investigating the bad import" : "Preparing your approved recovery";
    $("progress-title").textContent = { inspecting: "Checking the uploaded versions", queued: diagnosing ? "Your investigation is queued" : "Your recovery is queued", diagnosing: "Vultr AI is writing the investigation", investigating: "Comparing all three versions", "diagnostic-checking": "Checking the proposed repair scope", generating: "Vultr AI is writing the recovery", running: "Trying the recovery on copies", checking: "Checking every cell independently", finishing: "Confirming environment removal" }[view.status];
    $("progress-message").textContent = view.message;
    for (const [id, statuses] of [["stage-write", ["generating", "diagnosing", "queued"]], ["stage-run", ["running", "investigating"]], ["stage-check", ["checking", "diagnostic-checking", "finishing"]]]) $(id).classList.toggle("active", statuses.includes(view.status));
    const first = [...view.events].reverse().find(event => event.status === "queued")?.at || view.created_at;
    $("elapsed").textContent = "Working for " + Math.max(0, Math.round((Date.now() - first) / 1000)) + " seconds.";
  } else if (ready) {
    $("workspace-title").textContent = view.plan && !state.editing ? "Review what will change" : investigate ? "What went wrong?" : proposed && !state.manual ? "The mistake found. The later work protected." : "Choose what to restore";
    $("diagnose-button").disabled = !state.config?.available;
    $("diagnosis-availability").textContent = state.config?.available ? "You review the proposed repair before anything is restored." : "Inspection is ready. Connect Vultr Inference to investigate, or choose the fields yourself.";
    $("selection-title").textContent = proposed && !state.manual ? "Review the proposed replacements" : "Which fields were damaged?";
    $("selection-description").textContent = proposed && !state.manual ? "Each checked box is one exact cell. Later overlapping edits are protected below." : "Choose fields, then select individual cells below.";
    show("columns", state.manual); show("other-fields", !!proposed && !state.manual);
    $("keep-rows").textContent = "All " + view.current.row_count + " " + rowLabel();
    $("scope-note").textContent = view.checkpoint ? "Only incident changes still at the after-incident value can be restored. Observed later edits stay unchanged." : "No incident checkpoint: later edits to selected cells cannot be identified. Select only the cells damaged by the mistake.";
    if (view.diagnosis?.status === "failed" && !state.comparison) {
      state.manual = true; buildColumns(view); show("columns", true);
      notice(view.diagnosis.message + " One model call remains for recovery.");
    }
    if (proposed && !state.manual) {
      $("findings-title").textContent = proposed.targets.length + " proposed repairs. " + proposed.overlaps.length + " later price edits protected.";
      if (!proposed.columns.every(name => ["price", "price_cents"].includes(name))) $("findings-title").textContent = proposed.targets.length + " proposed repairs. " + proposed.overlaps.length + " overlapping edits protected.";
      $("findings-summary").textContent = proposed.summary;
      renderRows();
    }
    if (view.plan && !state.editing) renderPlan(view.plan);
  } else if (view.status === "verified") {
    $("workspace-title").textContent = "Recovery checked and ready"; renderResult(view.evidence.payload);
  } else { $("workspace-title").textContent = "This operation did not complete"; notice(view.message); }
}
function buildColumns(view) {
  $("columns").replaceChildren(); $("table-name").textContent = view.current.table;
  for (const column of view.current.columns.filter(item => !item.primary_key)) {
    const label = node("label"), input = document.createElement("input");
    input.type = "checkbox"; input.value = column.name; input.checked = state.columns.has(column.name);
    input.addEventListener("change", () => { if (input.checked) state.columns.add(column.name); else state.columns.delete(column.name); void perform(() => loadComparison(false)); });
    label.append(input, node("span", fieldName(column.name))); $("columns").append(label);
  }
}
let comparisonRequest = 0;
async function loadComparison(selectAll) {
  const requestId = ++comparisonRequest; state.selected.clear(); state.page = 0; state.comparison = null; state.protectedPages["selection-protected"] = 0;
  show("selection-limit", false);
  $("review-button").disabled = true;
  if (!state.columns.size) { show("compare-empty", true); show("comparison", false); show("selection-protected", false); $("review-button").disabled = true; return; }
  const comparison = await api(workspacePath("/compare"), { columns: [...state.columns] });
  if (requestId !== comparisonRequest) return;
  state.comparison = comparison;
  if (selectAll) for (const item of flatChoices()) state.selected.add(cellId(item));
  renderRows();
}
function renderRows() {
  const choices = flatChoices(), start = state.page * PAGE_SIZE, page = choices.slice(start, start + PAGE_SIZE);
  show("compare-empty", !choices.length); show("comparison", !!choices.length);
  $("compare-empty").textContent = state.columns.size ? "No restorable cells in these fields. Later edits stay protected." : "Choose at least one field to see its changes.";
  $("changes-body").replaceChildren();
  for (const item of page) {
    const tr = node("tr"), checkboxCell = node("td"), checkbox = document.createElement("input");
    checkbox.type = "checkbox"; checkbox.checked = state.selected.has(cellId(item)); checkbox.setAttribute("aria-label", "Restore " + item.column + " in record " + item.key.value);
    checkbox.dataset.cell = cellId(item);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(cellId(item)); else state.selected.delete(cellId(item)); renderRows();
      [...$("changes-body").querySelectorAll("input")].find(input => input.dataset.cell === cellId(item))?.focus({ preventScroll: true });
    });
    checkboxCell.append(checkbox);
    tr.append(checkboxCell, node("td", item.key.value, "record-tag"), node("td", fieldName(item.column)), node("td", cellValue(item.current, item.column), "cell-before"), node("td", cellValue(item.backup, item.column), "cell-after")); $("changes-body").append(tr);
  }
  $("selection-count").textContent = state.selected.size + " of " + choices.length + " cells selected";
  $("select-all").checked = !!choices.length && state.selected.size === choices.length;
  $("select-all").indeterminate = state.selected.size > 0 && state.selected.size < choices.length;
  const limit = state.config?.max_target_cells || 1000;
  $("review-button").disabled = !state.selected.size || state.selected.size > limit;
  $("selection-limit").textContent = "A repair can include up to " + limit.toLocaleString() + " cells. Deselect some cells to review a smaller repair.";
  show("selection-limit", state.selected.size > limit);
  $("page-info").textContent = choices.length ? "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, choices.length) + " of " + choices.length : "";
  $("previous-page").disabled = state.page === 0; $("next-page").disabled = start + PAGE_SIZE >= choices.length;
  renderProtected("selection-protected", state.comparison?.overlaps || []);
}
function renderProtected(id, items) {
  const container = $(id); container.replaceChildren(); container.hidden = !items.length;
  if (!items.length) return;
  container.append(node("h3", items.length + " overlapping later edits will stay"), node("p", "These cells changed again after the mistake. Rewind keeps today’s value."));
  const wrap = node("div", undefined, "table-wrap"), table = node("table", undefined, "protected-table"), head = node("thead"), hr = node("tr"), body = node("tbody");
  for (const label of ["Record", "Field", "Good version", "After the mistake", "Today’s value · kept"]) hr.append(node("th", label));
  head.append(hr); table.append(head, body);
  const page = Math.min(state.protectedPages[id] || 0, Math.floor((items.length - 1) / PAGE_SIZE)), start = page * PAGE_SIZE;
  state.protectedPages[id] = page;
  for (const item of items.slice(start, start + PAGE_SIZE)) {
    const tr = node("tr");
    tr.append(node("td", item.key.value, "record-tag"), node("td", fieldName(item.column)), node("td", cellValue(item.backup, item.column), "cell-value"), node("td", cellValue(item.checkpoint, item.column), "cell-before"), node("td", cellValue(item.current, item.column), "cell-after")); body.append(tr);
  }
  wrap.append(table); container.append(wrap);
  if (items.length > PAGE_SIZE) {
    const pagination = node("div", undefined, "pagination"), info = node("span", "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, items.length) + " of " + items.length + " protected edits"), controls = node("div");
    info.setAttribute("aria-live", "polite");
    for (const [direction, symbol, disabled] of [[-1, "←", page === 0], [1, "→", start + PAGE_SIZE >= items.length]]) {
      const button = node("button", symbol, "secondary compact"); button.type = "button"; button.disabled = disabled;
      button.setAttribute("aria-label", (direction < 0 ? "Previous" : "Next") + " protected edits");
      button.addEventListener("click", () => {
        state.protectedPages[id] = page + direction; renderProtected(id, items);
        const buttons = [...container.querySelectorAll("button")];
        (buttons.find(item => item.getAttribute("aria-label") === button.getAttribute("aria-label") && !item.disabled) || buttons.find(item => !item.disabled))?.focus({ preventScroll: true });
      });
      controls.append(button);
    }
    pagination.append(info, controls); container.append(pagination);
  }
}
function renderPlan(plan) {
  $("plan-title").textContent = "Restore " + plan.target_count + " cells. Keep " + plan.current_rows + " " + rowLabel() + ".";
  $("plan-description").textContent = "Restore only the approved " + plan.scope.columns.map(fieldName).join(", ") + " cells in " + plan.selected_rows + " records.";
  $("control-rows").textContent = plan.backup_rows + " " + rowLabel();
  $("control-detail").textContent = "Would lose " + plan.reference.lost_current_rows + " newer records.\nWould reset " + plan.reference.reverted_protected_updates + " other updates.";
  $("planned-rows").textContent = plan.current_rows + " " + rowLabel();
  $("planned-detail").textContent = "Keep " + plan.new_rows + " newer records.\nKeep " + plan.protected_update_count + " other updates" + (plan.history.protected_overlap_count ? ", including " + plan.history.protected_overlap_count + " later price edits." : ".") + "\nRestore only your " + plan.target_count + " approved cells.";
  if (!plan.scope.columns.every(name => ["price", "price_cents"].includes(name))) $("planned-detail").textContent = $("planned-detail").textContent.replace("later price edits", "overlapping later edits");
  $("recover-button").textContent = "Approve and restore " + plan.target_count + " cells";
  $("recover-button").disabled = !state.config?.available;
  $("approval-detail").textContent = "Review " + plan.sha256.slice(0, 12) + " · Approval binds every input fingerprint and these exact cells. A changed selection requires new review.";
  $("plan-history").textContent = plan.history.note;
  renderPlanRows(); renderProtected("plan-protected", plan.history.overlaps);
}
function renderPlanRows() {
  const targets = state.view.plan.targets, start = state.planPage * PAGE_SIZE;
  $("plan-body").replaceChildren();
  for (const item of targets.slice(start, start + PAGE_SIZE)) {
    const tr = node("tr"); tr.append(node("td", item.key.value, "record-tag"), node("td", fieldName(item.column)), node("td", cellValue(item.current, item.column), "cell-before"), node("td", cellValue(item.backup, item.column), "cell-after")); $("plan-body").append(tr);
  }
  $("plan-page-info").textContent = "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, targets.length) + " of " + targets.length + " approved cells";
  $("plan-previous").disabled = state.planPage === 0; $("plan-next").disabled = start + PAGE_SIZE >= targets.length;
}
function renderResult(payload) {
  const result = payload.verification;
  const prices = state.view.example && result.diff.every(item => ["price", "price_cents"].includes(item.column));
  $("result-title").textContent = result.restored_cells + (prices ? " invoice prices restored" : " cells restored");
  $("result-detail").textContent = "All " + result.retained_rows + " " + rowLabel() + " retained. " + result.retained_new_rows + " newer records and " + result.protected_update_count + " other updates kept.";
  for (const [id, value] of [["stat-restored", result.restored_cells], ["stat-rows", result.retained_rows], ["stat-new", result.retained_new_rows], ["stat-updates", result.protected_update_count]]) $(id).textContent = value;
  $("download-database").textContent = "Download recovered " + (payload.format === "csv" ? "CSV" : "database") + " ↓";
  $("diff-total").textContent = result.diff.length + " checked changes"; renderResultRows();
  renderProtected("result-protected", payload.plan.history?.overlaps || []);
  $("evidence-checks").replaceChildren();
  const checks = [...result.checks, { label: "Original versions stay unchanged", ok: payload.original_inputs_unchanged }, { label: "Every temporary environment was removed", ok: payload.all_sandboxes_removed }];
  for (const check of checks) { const line = node("div"); line.append(node("span", check.ok ? "✓" : "×"), node("span", check.label)); $("evidence-checks").append(line); }
  $("runtime-details").replaceChildren();
  const provider = payload.attempts.at(-1)?.provider;
  const lines = ["Provider: Vultr Serverless Inference · " + (provider?.model || ""), "Model calls: " + payload.model_calls + " of at most 2. " + (payload.diagnosis?.status === "proposed" ? "One investigation and one approved recovery." : "Manual repair workflow."),
    "Isolated runs: " + payload.sandbox_runs.length + ". gVisor · no network · nonroot · 192 MiB · 0.5 CPU · 48 PIDs · 8-second deadline.",
    "Generated investigation, generated repair and trusted result inspection use separate containers.",
    "Recovered file SHA-256: " + payload.artifact_sha256, "Vultr repair response: " + (provider?.response_id || "not supplied"),
    "These checks are observed by the controller. Snapshots do not authenticate a complete edit history."];
  if (payload.diagnosis?.attempt?.provider?.response_id) lines.push("Vultr investigation response: " + payload.diagnosis.attempt.provider.response_id);
  for (const text of lines) $("runtime-details").append(node("div", text));
}
function renderResultRows() {
  const changes = state.view.evidence.payload.verification.diff;
  state.resultPage = Math.min(state.resultPage, Math.max(0, Math.ceil(changes.length / PAGE_SIZE) - 1));
  const start = state.resultPage * PAGE_SIZE;
  $("result-body").replaceChildren();
  for (const item of changes.slice(start, start + PAGE_SIZE)) {
    const tr = node("tr"); tr.append(node("td", item.key.value, "record-tag"), node("td", fieldName(item.column)), node("td", cellValue(item.before, item.column), "cell-before"), node("td", cellValue(item.after, item.column), "cell-after"), node("td", "✓ Matches good version", "cell-after")); $("result-body").append(tr);
  }
  $("result-page-info").textContent = changes.length ? "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, changes.length) + " of " + changes.length + " checked changes" : "No cells changed.";
  $("result-previous").disabled = state.resultPage === 0; $("result-next").disabled = start + PAGE_SIZE >= changes.length;
}
async function download(kind, filename) {
  const response = await fetch(workspacePath("/" + kind), { headers: { Authorization: "Bearer " + state.workspace.token } });
  if (!response.ok) throw new Error("The checked file could not be downloaded. Reload to check the saved result.");
  saveBlob(await response.blob(), filename);
}
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob), a = document.createElement("a"); a.href = url; a.download = filename; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function saveJson(value, filename) { saveBlob(new Blob([JSON.stringify(value, null, 2) + "\n"], { type: "application/json" }), filename); }
function saveChanges() {
  const rows = [["record_id", "field", "before", "after", "before_type", "after_type"]];
  for (const item of state.view.evidence.payload.verification.diff) rows.push([
    item.key.value, item.column, item.before.value ?? "", item.after.value ?? "", item.before.type, item.after.type
  ]);
  const csv = rows.map(row => row.map(value => '"' + String(value).replaceAll('"', '""') + '"').join(",")).join("\r\n") + "\r\n";
  saveBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), "rewind-changes.csv");
}
async function encodeFile(file) {
  const minimum = $("format-select").value === "csv" ? 1 : 512;
  if (!file || file.size < minimum || file.size > 256 * 1024) throw new Error("Choose a good version and today’s file, up to 256 KiB each.");
  const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ""; for (const value of bytes) binary += String.fromCharCode(value); return btoa(binary);
}
async function chooseManually() {
  state.manual = true; buildColumns(state.view);
  if (!state.columns.size && state.view.example) state.columns.add(state.view.format === "csv" ? "price" : "price_cents");
  buildColumns(state.view); await loadComparison(state.view.example); await render(state.view);
}

$("example-button").addEventListener("click", () => withAccess(() => perform(async () => saveWorkspace(await api("/api/example", { format: "csv", scenario: "overlap" })), $("example-button"))));
$("upload-toggle").addEventListener("click", () => { show("upload-panel", $("upload-panel").hidden); if (!$("upload-panel").hidden) $("upload-panel").scrollIntoView({ behavior: "smooth", block: "center" }); });
$("format-select").addEventListener("change", () => {
  const csv = $("format-select").value === "csv"; show("key-label", csv);
  for (const name of ["backup", "checkpoint", "current"]) { $(name + "-file").accept = csv ? ".csv" : ".sqlite,.sqlite3,.db"; $(name + "-file").value = ""; }
  $("file-help").textContent = csv ? "UTF-8 CSV · matching headers · 1,000 records · 2–16 fields · 256 KiB per file." : "Use consistent SQLite copies taken with the Online Backup API. One ordinary table, stable primary key, matching schema; a WAL file alone is not a complete snapshot.";
});
$("inspect-button").addEventListener("click", () => withAccess(() => perform(async () => {
  const format = $("format-select").value, primary_key = format === "csv" ? $("key-input").value : "";
  if (format === "csv" && !primary_key) throw new Error("Enter the CSV column that uniquely identifies each record.");
  const backup = await encodeFile($("backup-file").files[0]), current = await encodeFile($("current-file").files[0]);
  const request = { backup, current, format, primary_key };
  if ($("checkpoint-file").files[0]) request.checkpoint = await encodeFile($("checkpoint-file").files[0]);
  saveWorkspace(await api("/api/workspaces", request));
}, $("inspect-button"))));
$("diagnose-button").addEventListener("click", () => withAccess(() => perform(async () => {
  const instruction = $("instruction").value.trim(); if (!instruction) throw new Error("Describe the mistake first.");
  await api(workspacePath("/diagnose"), { instruction, expected_version: state.view.version }); await poll();
}, $("diagnose-button")), true));
$("manual-button").addEventListener("click", () => perform(chooseManually));
$("other-fields").addEventListener("click", () => perform(chooseManually));
$("select-all").addEventListener("change", () => { state.selected.clear(); if ($("select-all").checked) for (const item of flatChoices()) state.selected.add(cellId(item)); renderRows(); });
$("previous-page").addEventListener("click", () => { state.page--; renderRows(); });
$("next-page").addEventListener("click", () => { state.page++; renderRows(); });
$("plan-previous").addEventListener("click", () => { state.planPage--; renderPlanRows(); });
$("plan-next").addEventListener("click", () => { state.planPage++; renderPlanRows(); });
$("result-previous").addEventListener("click", () => { state.resultPage--; renderResultRows(); });
$("result-next").addEventListener("click", () => { state.resultPage++; renderResultRows(); });
$("review-button").addEventListener("click", () => perform(async () => {
  const targets = flatChoices().filter(item => state.selected.has(cellId(item))).map(item => ({ token: item.token, column: item.column }));
  const view = await api(workspacePath("/plan"), { targets, expected_version: state.view.version });
  if (state.workspace?.id !== view.id) return;
  state.editing = false; state.planPage = 0; state.protectedPages["plan-protected"] = 0; await render(view);
}, $("review-button")));
$("edit-selection").addEventListener("click", () => { state.editing = true; void render(state.view); });
$("recover-button").addEventListener("click", () => withAccess(() => perform(async () => { await api(workspacePath("/recover"), { plan_sha256: state.view.plan.sha256 }); await poll(); }, $("recover-button")), true));
function clearWorkspace() { clearTimeout(state.timer); comparisonRequest++; sessionStorage.removeItem(storageKey); state.workspace = null; state.view = null; notice(""); show("welcome", true); show("workspace", false); show("privacy-actions", false); window.scrollTo({ top: 0, behavior: "smooth" }); }
$("start-over").addEventListener("click", clearWorkspace);
$("delete-workspace").addEventListener("click", () => $("delete-dialog").showModal());
$("delete-cancel").addEventListener("click", () => $("delete-dialog").close());
$("delete-confirm").addEventListener("click", () => perform(async () => { await api(workspacePath("/delete"), {}); $("delete-dialog").close(); clearWorkspace(); }, $("delete-confirm")));
for (const kind of ["database", "backup", "checkpoint", "current"]) $("download-" + kind).addEventListener("click", () => perform(() => download(kind, "rewind-" + (kind === "database" ? "recovered" : kind) + "." + (state.view.format || "sqlite"))));
$("download-plan").addEventListener("click", () => saveJson(state.view.plan, "rewind-approved-plan.json"));
$("download-diff").addEventListener("click", saveChanges);
$("download-evidence").addEventListener("click", () => saveJson(state.view.evidence, "rewind-checks.json"));
$("how-button").addEventListener("click", () => $("how-dialog").showModal());
$("how-close").addEventListener("click", () => $("how-dialog").close());
$("access-cancel").addEventListener("click", () => { state.pending = null; $("access-dialog").close(); });
$("access-form").addEventListener("submit", event => { event.preventDefault(); state.access = $("access-input").value.trim(); $("access-input").value = ""; $("access-dialog").close(); const callback = state.pending; state.pending = null; if (callback) void perform(callback); });
async function initialize() {
  try {
    state.config = await api("/api/config");
    $("connection").className = "connection " + (state.config.inspection_available ? "ready" : "unavailable");
    $("connection").replaceChildren(node("i"), node("span", state.config.available ? "Vultr recovery connected" : state.config.inspection_available ? "Ready to review copies" : "Recovery unavailable"));
    show("video-link", !!state.config.demo_available);
    const saved = sessionStorage.getItem(storageKey);
    if (saved) { const value = JSON.parse(saved); if (/^[a-f0-9]{32}$/.test(value.id) && typeof value.token === "string") { state.workspace = value; void poll(); } }
  } catch { $("connection").replaceChildren(node("i"), node("span", "Worker not connected")); }
}
void initialize();

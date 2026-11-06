"use strict";
const $ = id => document.getElementById(id);
const state = { config: null, access: "", workspace: null, view: null, comparison: null, selected: new Set(), columns: new Set(), page: 0, editing: false, pending: null, timer: null, failures: 0, initialized: null };
const PAGE_SIZE = 10;
const progressStates = new Set(["inspecting", "queued", "generating", "running", "checking", "finishing"]);
const storageKey = "rewind-private-workspace";
const fieldName = name => state.view?.example && name === "price_cents" ? "Price (USD)" : name.replaceAll("_", " ");
const rowLabel = () => state.view?.example ? "invoices" : "records";

function notice(message) { $("notice").textContent = message; $("notice").hidden = !message; }
function show(id, visible) { $(id).hidden = !visible; }
function node(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
function cellValue(cell, column) {
  if (!cell || cell.type === "null") return "—";
  if (state.view?.example && column === "price_cents" && cell.type === "integer") return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(cell.value) / 100);
  return cell.value;
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
  notice("");
  if (button) button.disabled = true;
  try { await callback(); } catch (error) { notice(error.message || "The operation could not be completed."); }
  finally { if (button) button.disabled = false; }
}
function withAccess(callback, requireInference = false) {
  if (!(requireInference ? state.config?.available : state.config?.inspection_available)) { notice(state.config?.message || "Live recovery is not connected yet."); return; }
  if (state.access) { void perform(callback); return; }
  state.pending = callback;
  $("access-dialog").showModal();
  $("access-input").focus();
}
function saveWorkspace(value) {
  state.workspace = { id: value.id, token: value.token };
  sessionStorage.setItem(storageKey, JSON.stringify(state.workspace));
  state.initialized = null; state.editing = false; state.selected.clear(); state.columns.clear(); state.comparison = null;
  show("welcome", false); show("workspace", true); void poll();
}
async function poll() {
  clearTimeout(state.timer);
  if (!state.workspace) return;
  try {
    const view = await api(workspacePath()); state.failures = 0; await render(view);
    if (progressStates.has(view.status)) state.timer = setTimeout(poll, 1100);
  } catch (error) {
    state.failures++;
    if (state.failures < 5) state.timer = setTimeout(poll, 2000);
    else notice("The connection was interrupted. Reload to check the saved workspace; no extra model request will be started.");
  }
}
async function render(view) {
  state.view = view;
  show("welcome", false); show("workspace", true);
  $("workspace-label").textContent = view.example ? "FICTIONAL INVOICE EXAMPLE · ACTUAL RECOVERY WORKFLOW" : "YOUR RECOVERY";
  $("workspace-message").textContent = view.message;
  $("start-over").disabled = progressStates.has(view.status);
  if (view.backup && view.current) {
    $("backup-count").textContent = view.backup.row_count + " " + rowLabel();
    $("current-count").textContent = view.current.row_count + " " + rowLabel();
    $("backup-detail").textContent = "Good backup · inspected copy";
    $("current-detail").textContent = "Current data · inspected copy";
    show("download-backup", true); show("download-current", true);
  }
  show("progress-panel", progressStates.has(view.status));
  show("selection-panel", view.status === "ready" && (!view.plan || state.editing));
  show("plan-panel", view.status === "ready" && !!view.plan && !state.editing);
  show("result-panel", view.status === "verified");
  if (progressStates.has(view.status)) {
    $("workspace-title").textContent = view.status === "inspecting" ? "Reading your snapshots" : "Restoring your selected cells";
    $("progress-title").textContent = { inspecting: "Checking both database copies", queued: "Your recovery is queued", generating: "Vultr AI is writing the recovery", running: "Trying the recovery on copies", checking: "Checking every cell independently", finishing: "Confirming sandbox removal" }[view.status];
    $("progress-message").textContent = view.message;
    for (const [id, statuses] of [["stage-write", ["generating", "queued"]], ["stage-run", ["running"]], ["stage-check", ["checking", "finishing"]]]) $(id).classList.toggle("active", statuses.includes(view.status));
    const first = view.events.find(event => event.status === "queued")?.at || view.created_at;
    $("elapsed").textContent = "Working for " + Math.max(0, Math.round((Date.now() - first) / 1000)) + " seconds. This is an actual background operation.";
  } else if (view.status === "ready") {
    $("workspace-title").textContent = view.plan && !state.editing ? "Review what will change" : "Choose what to restore";
    if (state.initialized !== view.id) {
      state.initialized = view.id;
      buildColumns(view);
      if (view.example && view.current.columns.some(column => column.name === "price_cents")) { state.columns.add("price_cents"); $("columns").querySelector('input[value="price_cents"]').checked = true; await loadComparison(true); }
    }
    $("keep-rows").textContent = "All " + view.current.row_count + " " + rowLabel();
    if (view.plan && !state.editing) renderPlan(view.plan);
  } else if (view.status === "verified") {
    $("workspace-title").textContent = "Recovery checked and ready";
    renderResult(view.evidence.payload);
  } else {
    $("workspace-title").textContent = "This recovery did not pass";
    notice(view.message);
  }
}
function buildColumns(view) {
  $("columns").replaceChildren();
  $("table-name").textContent = view.current.table;
  for (const column of view.current.columns.filter(item => !item.primary_key)) {
    const label = node("label"), input = document.createElement("input");
    input.type = "checkbox"; input.value = column.name; input.checked = state.columns.has(column.name);
    input.addEventListener("change", () => { if (input.checked) state.columns.add(column.name); else state.columns.delete(column.name); void perform(() => loadComparison(false)); });
    label.append(input, node("span", fieldName(column.name))); $("columns").append(label);
  }
}
let comparisonRequest = 0;
async function loadComparison(selectAll) {
  const requestId = ++comparisonRequest;
  state.selected.clear(); state.page = 0; state.comparison = null;
  if (!state.columns.size) { show("compare-empty", true); show("comparison", false); $("review-button").disabled = true; return; }
  const comparison = await api(workspacePath("/compare"), { columns: [...state.columns] });
  if (requestId !== comparisonRequest) return;
  state.comparison = comparison;
  if (selectAll) for (const row of comparison.choices) state.selected.add(row.token);
  show("compare-empty", comparison.choices.length === 0); show("comparison", comparison.choices.length > 0);
  $("compare-empty").textContent = comparison.choices.length ? "" : "These fields already match the backup in every shared record.";
  renderRows();
}
function renderRows() {
  const choices = state.comparison?.choices || [];
  const start = state.page * PAGE_SIZE, page = choices.slice(start, start + PAGE_SIZE);
  $("changes-body").replaceChildren();
  for (const item of page) for (const difference of item.differences) {
    const tr = node("tr"), checkboxCell = node("td"), checkbox = document.createElement("input");
    checkbox.type = "checkbox"; checkbox.checked = state.selected.has(item.token); checkbox.setAttribute("aria-label", "Restore record " + item.key.value);
    checkbox.addEventListener("change", () => { if (checkbox.checked) state.selected.add(item.token); else state.selected.delete(item.token); renderRows(); });
    checkboxCell.append(checkbox);
    tr.append(checkboxCell, node("td", item.key.value, "record-tag"), node("td", fieldName(difference.column)), node("td", cellValue(difference.current, difference.column), "cell-before"), node("td", cellValue(difference.backup, difference.column), "cell-after"));
    $("changes-body").append(tr);
  }
  $("selection-count").textContent = state.selected.size + " of " + choices.length + " changed records selected";
  $("select-all").checked = choices.length > 0 && state.selected.size === choices.length;
  $("select-all").indeterminate = state.selected.size > 0 && state.selected.size < choices.length;
  $("review-button").disabled = !state.selected.size;
  $("page-info").textContent = choices.length ? "Showing " + (start + 1) + "–" + Math.min(start + PAGE_SIZE, choices.length) + " of " + choices.length : "";
  $("previous-page").disabled = state.page === 0; $("next-page").disabled = start + PAGE_SIZE >= choices.length;
}
function renderPlan(plan) {
  const fields = plan.scope.columns.map(fieldName).join(", ");
  $("plan-title").textContent = "Restore " + plan.target_count + " cells. Keep " + plan.current_rows + " " + rowLabel() + ".";
  $("plan-description").textContent = "Restore " + fields + " in " + plan.selected_rows + " selected records from the good backup.";
  $("control-rows").textContent = plan.backup_rows + " " + rowLabel();
  $("control-detail").textContent = "Would lose " + plan.reference.lost_current_rows + " newer records.\nWould reset " + plan.reference.reverted_protected_updates + " other updates.";
  $("planned-rows").textContent = plan.current_rows + " " + rowLabel();
  $("planned-detail").textContent = "Keep " + plan.new_rows + " newer records.\nKeep " + plan.protected_update_count + " other updates.\nRestore only your " + plan.target_count + " selected cells.";
  $("recover-button").textContent = "Restore these " + plan.target_count + " cells";
  $("recover-button").disabled = !state.config?.available;
  if (!state.config?.available) notice(state.config?.message || "Connect Vultr before starting the recovery.");
}
function renderResult(payload) {
  const result = payload.verification;
  $("result-title").textContent = result.restored_cells + (state.view.example ? " invoice prices restored" : " cells restored");
  $("result-detail").textContent = "All " + result.retained_rows + " " + rowLabel() + " retained. " + result.retained_new_rows + " newer records and " + result.protected_update_count + " other updates kept.";
  for (const [id, value] of [["stat-restored", result.restored_cells], ["stat-rows", result.retained_rows], ["stat-new", result.retained_new_rows], ["stat-updates", result.protected_update_count]]) $(id).textContent = value;
  $("diff-total").textContent = result.diff.length + " checked changes";
  $("result-body").replaceChildren();
  for (const item of result.diff.slice(0, 12)) {
    const tr = node("tr"); tr.append(node("td", item.key.value, "record-tag"), node("td", fieldName(item.column)), node("td", cellValue(item.before, item.column), "cell-before"), node("td", cellValue(item.after, item.column), "cell-after"), node("td", "✓ Matches backup", "cell-after")); $("result-body").append(tr);
  }
  $("result-page-note").textContent = "Showing the first " + Math.min(12, result.diff.length) + " of " + result.diff.length + " changes. Download the complete list above.";
  $("evidence-checks").replaceChildren();
  const checks = [...result.checks, { label: "Original snapshots stay unchanged", ok: payload.original_inputs_unchanged }, { label: "Every temporary sandbox was removed", ok: payload.all_sandboxes_removed }];
  for (const check of checks) { const line = node("div"); line.append(node("span", check.ok ? "✓" : "×"), node("span", check.label)); $("evidence-checks").append(line); }
  $("runtime-details").replaceChildren();
  const provider = payload.attempts.at(-1)?.provider;
  const runtimeLines = ["Actual provider: Vultr Serverless Inference · " + (provider?.model || ""), "Model attempts: " + payload.attempts.length + ". No full-backup fallback.", "Fresh isolated runs: " + payload.sandbox_runs.length + ". gVisor · no network · nonroot · 192 MiB · 0.5 CPU · 48 PIDs · 8-second execution deadline.", "The recovery ran in one container; its database was checked in a different, fresh container.", "Recovered file SHA-256: " + payload.artifact_sha256, "Vultr response: " + (provider?.response_id || "not supplied"), "These are controller-observed checks, not a hardware or host attestation."];
  for (const text of runtimeLines) $("runtime-details").append(node("div", text));
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
async function encodeFile(file) {
  if (!file || file.size < 512 || file.size > 256 * 1024) throw new Error("Choose a good backup and a current SQLite snapshot, up to 256 KB each.");
  const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ""; for (const value of bytes) binary += String.fromCharCode(value); return btoa(binary);
}

$("example-button").addEventListener("click", () => withAccess(() => perform(async () => { const value = await api("/api/example", {}); saveWorkspace(value); }, $("example-button"))));
$("upload-toggle").addEventListener("click", () => { show("upload-panel", $("upload-panel").hidden); if (!$("upload-panel").hidden) $("upload-panel").scrollIntoView({ behavior: "smooth", block: "center" }); });
$("inspect-button").addEventListener("click", () => withAccess(() => perform(async () => { const backup = await encodeFile($("backup-file").files[0]); const current = await encodeFile($("current-file").files[0]); saveWorkspace(await api("/api/workspaces", { backup, current })); }, $("inspect-button"))));
$("select-all").addEventListener("change", () => { state.selected.clear(); if ($("select-all").checked) for (const row of state.comparison.choices) state.selected.add(row.token); renderRows(); });
$("previous-page").addEventListener("click", () => { state.page--; renderRows(); });
$("next-page").addEventListener("click", () => { state.page++; renderRows(); });
$("review-button").addEventListener("click", () => perform(async () => { const view = await api(workspacePath("/plan"), { columns: [...state.columns], keys: [...state.selected], expected_version: state.view.version }); state.editing = false; await render(view); }, $("review-button")));
$("edit-selection").addEventListener("click", () => { state.editing = true; void render(state.view); });
$("recover-button").addEventListener("click", () => withAccess(() => perform(async () => { await api(workspacePath("/recover"), { plan_sha256: state.view.plan.sha256 }); await poll(); }, $("recover-button")), true));
$("start-over").addEventListener("click", () => { clearTimeout(state.timer); sessionStorage.removeItem(storageKey); state.workspace = null; state.view = null; notice(""); show("welcome", true); show("workspace", false); window.scrollTo({ top: 0, behavior: "smooth" }); });
$("download-database").addEventListener("click", () => perform(() => download("database", "rewind-recovered.sqlite")));
$("download-backup").addEventListener("click", () => perform(() => download("backup", "rewind-backup.sqlite")));
$("download-current").addEventListener("click", () => perform(() => download("current", "rewind-current.sqlite")));
$("download-diff").addEventListener("click", () => saveJson(state.view.evidence.payload.verification.diff, "rewind-changes.json"));
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

import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";
import { chromium } from "@playwright/test";

process.umask(0o077);
const origin = process.env.REWIND_TEST_URL || "http://127.0.0.1:18182";
const output = process.env.REWIND_REPORT_DIR ? pathToFileURL(process.env.REWIND_REPORT_DIR.replace(/\/?$/, "/")) : new URL("../reports/import-browser/", import.meta.url);
const record = process.argv.includes("--record");
await mkdir(output, { recursive: true, mode: 0o700 });
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1512, height: 982 }, acceptDownloads: true,
  ...(record ? { recordVideo: { dir: new URL("videos/", output).pathname, size: { width: 1512, height: 982 } } } : {}) });
const page = await context.newPage(), startedAt = Date.now(), stages = [], errors = [];
page.on("pageerror", error => errors.push(error.name));
const mark = name => stages.push({ name, seconds: (Date.now() - startedAt) / 1000 });
const linger = async () => { if (record) await page.waitForTimeout(2500); };
const hash = raw => createHash("sha256").update(raw).digest("hex");
const canonical = value => Array.isArray(value) ? "[" + value.map(canonical).join(",") + "]" : value && typeof value === "object" ? "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}" : JSON.stringify(value);
async function view() { return page.evaluate(async () => { const workspace = JSON.parse(sessionStorage.getItem("rewind-private-workspace")); return (await fetch("/api/workspaces/" + workspace.id, { headers: { Authorization: "Bearer " + workspace.token } })).json(); }); }
async function screenshot(name, mobile = false) {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "Import screen overflows horizontally.");
  await page.screenshot({ path: new URL(name + ".png", output).pathname, fullPage: true });
  if (mobile) await page.setViewportSize({ width: 1512, height: 982 });
}
async function downloaded(id, name) { const event = page.waitForEvent("download"); await page.locator("#" + id).click(); const item = await event; await item.saveAs(new URL(name, output).pathname); return readFile(new URL(name, output)); }
let completed = false, result, demoWorkspace, accessRequired = false, privateCode;
async function authorizePrivateRequest() {
  if (!(await page.locator("#access-dialog").isVisible())) return;
  assert.equal(accessRequired, true, "The public import workflow requested an access code.");
  privateCode ||= (await readFile(process.env.REWIND_ACCESS_FILE || new URL("../.local/demo-access.txt", import.meta.url), "utf8")).trim();
  await page.locator("#access-input").fill(privateCode);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
}
try {
  await page.goto(origin); await page.locator("#connection.ready").waitFor({ timeout: 30000 });
  const config = await (await page.request.get(origin + "/api/config")).json();
  accessRequired = config.access_required;
  assert.equal(config.preflight_available, true);
  await page.locator("#import-mode").click();
  assert.equal(await page.locator("#import-start").isVisible(), true);
  assert.equal(await page.locator("#recovery-start").isVisible(), false);
  await screenshot("import-home"); await screenshot("import-home-mobile", true); mark("opened"); await linger();
  await page.locator("#import-example-button").click();
  if (config.access_required) {
    await authorizePrivateRequest();
  } else assert.equal(await page.locator("#access-dialog").isVisible(), false);
  await page.locator("#import-policy-panel").waitFor({ state: "visible", timeout: 60000 });
  assert.equal(await page.locator('#import-columns input[value="price"]').isChecked(), true);
  assert.equal(await page.locator("#import-numeric").inputValue(), "price");
  await screenshot("intended-update"); mark("inspected"); await linger();
  await page.locator("#import-check-button").click();
  await page.locator("#import-review-panel").waitFor({ state: "visible" });
  let value = await view();
  assert.equal(value.model_calls, 0);
  assert.deepEqual([value.import_review.eligible_count, value.import_review.held_count, value.import_review.unknown_ids.length], [22, 26, 1]);
  assert.equal(await page.locator("#import-selection-count").innerText(), "22 updates included");
  assert.ok((await page.locator("#import-changes-body").innerText()).includes("(empty)"), "A blank price was not clearly labelled.");
  assert.match(await page.locator("#import-unknowns").innerText(), /9999/);
  const collected = [];
  do {
    collected.push(...await page.locator("#import-changes-body tr").evaluateAll(rows => rows.map(row => ({ id: row.children[1].textContent, field: row.children[2].textContent, held: row.querySelector("input").disabled }))));
    if (await page.locator("#import-next").isDisabled()) break;
    await page.locator("#import-next").click();
  } while (true);
  assert.equal(collected.length, 48); assert.equal(collected.filter(item => item.held).length, 26);
  while (await page.locator("#import-previous").isEnabled()) await page.locator("#import-previous").click();
  const cell = page.locator('#import-changes-body input:not([disabled])').first();
  const excludedLabel = await cell.getAttribute("aria-label"); await cell.uncheck();
  assert.equal(await page.locator("#import-selection-count").innerText(), "21 updates included");
  await page.reload(); await page.locator("#import-review-panel").waitFor({ state: "visible" });
  assert.equal(await page.locator("#import-selection-count").innerText(), "21 updates included");
  assert.equal(await page.getByRole("checkbox", { name: excludedLabel, exact: true }).isChecked(), false);
  await page.getByRole("checkbox", { name: excludedLabel, exact: true }).check();
  await screenshot("held-changes"); await screenshot("held-changes-mobile", true); mark("reviewed"); await linger();
  await page.locator("#import-plan-button").click(); await page.locator("#import-plan-panel").waitFor({ state: "visible" });
  value = await view(); assert.equal(value.import_plan.target_count, 22); assert.equal(value.import_plan.workspace_version, value.version);
  await page.locator("#import-plan-next").click(); assert.match(await page.locator("#import-plan-page-info").innerText(), /11–20/); await page.locator("#import-plan-previous").click();
  await page.reload(); await page.locator("#import-plan-panel").waitFor({ state: "visible" });
  assert.match(await page.locator("#import-plan-title").innerText(), /22 cells.*24 records/);
  await screenshot("exact-import-plan"); await screenshot("exact-import-plan-mobile", true); mark("planned"); await linger();
  const denied = await page.evaluate(async () => { const workspace = JSON.parse(sessionStorage.getItem("rewind-private-workspace")); return (await fetch("/api/workspaces/" + workspace.id + "/approved-import", { headers: { Authorization: "Bearer " + workspace.token } })).status; });
  assert.equal(denied, 409, "Download was allowed before execution and independent checks.");
  await page.locator("#import-approve").click();
  await authorizePrivateRequest();
  await page.locator("#import-result-panel").waitFor({ state: "visible", timeout: 60000 }); mark("verified");
  value = await view(); result = value.evidence;
  assert.equal(hash(canonical(result.payload)), result.sha256);
  assert.equal(result.payload.workflow, "preflight"); assert.equal(result.payload.model_calls, 0);
  assert.equal(result.payload.verification.ok, true); assert.ok(result.payload.verification.checks.every(item => item.ok));
  assert.equal(result.payload.all_sandboxes_removed, true);
  assert.ok(result.payload.sandbox_runs.every(run => run.policy_verified && run.runtime === "runsc" && run.teardown.confirmed));
  assert.equal(result.payload.verification.updated_cells, 22); assert.equal(result.payload.verification.retained_rows, 24);
  assert.ok(result.payload.verification.diff.every(item => item.column === "price"));
  await screenshot("checked-import"); await screenshot("checked-import-mobile", true); await linger();
  assert.equal(hash(await downloaded("import-download-approved", "approved-import.csv")), result.payload.approved_import_sha256);
  assert.equal(hash(await downloaded("import-download-preview", "resulting-export.csv")), result.payload.artifact_sha256);
  const downloadedEvidence = JSON.parse((await downloaded("import-download-checks", "import-evidence.json")).toString("utf8"));
  assert.equal(downloadedEvidence.sha256, result.sha256);
  await downloaded("import-download-changes", "import-changes.csv"); mark("downloaded");
  await page.locator("#import-result-next").click(); assert.match(await page.locator("#import-result-page-info").innerText(), /11–20/);
  await page.reload(); await page.locator("#import-result-panel").waitFor({ state: "visible" });
  demoWorkspace = await page.evaluate(() => JSON.parse(sessionStorage.getItem("rewind-private-workspace")));
  if (record || process.env.REWIND_KEEP_WORKSPACE === "1") await writeFile(new URL("workspace.private.json", output), JSON.stringify(demoWorkspace), { mode: 0o600 });
  const currentResponse = await page.request.get(origin + "/api/workspaces/" + demoWorkspace.id + "/current", { headers: { Authorization: "Bearer " + demoWorkspace.token } });
  assert.equal(currentResponse.status(), 200); const currentFile = await currentResponse.body();
  await page.locator("#import-start-over").click(); await page.locator("#import-upload-toggle").click();
  await page.locator("#import-key-input").fill("product_id");
  await page.locator("#import-current-file").setInputFiles({ name: "today.csv", mimeType: "text/csv", buffer: currentFile });
  await page.locator("#import-incoming-file").setInputFiles({ name: "price-update.csv", mimeType: "text/csv", buffer: Buffer.from("product_id,price\r\n0003,25.50\r\n") });
  await page.locator("#import-inspect-button").click(); await authorizePrivateRequest();
  await page.locator("#import-policy-panel").waitFor({ state: "visible", timeout: 60000 });
  await page.locator('#import-columns input[value="price"]').check(); await page.locator("#import-numeric").selectOption("price");
  await page.locator("#import-check-button").click(); await page.locator("#import-review-panel").waitFor({ state: "visible" });
  assert.equal(await page.locator("#import-selection-count").innerText(), "1 update included");
  await page.locator("#import-percent").fill("30"); assert.equal(await page.locator("#import-review-panel").isVisible(), false);
  assert.match(await page.locator("#import-policy-note").innerText(), /rules changed/);
  await page.locator("#import-check-button").click(); await page.locator("#import-review-panel").waitFor({ state: "visible" });
  await page.locator("#import-plan-button").click(); await page.locator("#import-plan-panel").waitFor({ state: "visible" });
  await page.locator("#import-approve").click(); await authorizePrivateRequest();
  await page.locator("#import-result-panel").waitFor({ state: "visible", timeout: 60000 });
  const uploaded = await view(); assert.equal(uploaded.model_calls, 0); assert.equal(uploaded.evidence.payload.verification.updated_cells, 1); assert.equal(uploaded.evidence.payload.verification.retained_rows, 24);
  assert.equal(uploaded.evidence.payload.verification.diff[0].key.value, "0003");
  await screenshot("uploaded-partial-import"); await screenshot("uploaded-partial-import-mobile", true);
  await page.locator("#delete-workspace").click(); await page.locator("#delete-confirm").click();
  await page.locator("#import-start").waitFor({ state: "visible" });
  assert.equal(await page.evaluate(() => sessionStorage.getItem("rewind-private-workspace")), null);
  assert.equal((await page.request.get(origin + "/api/workspaces/" + uploaded.id)).status(), 404);
  assert.deepEqual(errors, []); completed = true;
  console.log("Actual import browser checks passed: 22 approved updates, 26 held changes, 1 unknown ID excluded, 24 records retained, exact downloads, persisted selection, partial-column upload and deletion. Zero model calls.");
} catch (error) {
  await page.screenshot({ path: new URL("incomplete.png", output).pathname, fullPage: true }).catch(() => {});
  await writeFile(new URL("browser-error.private.txt", output), error.stack || String(error), { mode: 0o600 });
  console.error("Import browser verification failed (" + error.name + "). Private diagnostics are in the report directory."); process.exitCode = 1;
} finally {
  await writeFile(new URL("verification.json", output), JSON.stringify({ at: new Date().toISOString(), origin, completed, record, modelCalls: 0, evidenceSha256: result?.sha256, stages }, null, 2));
  await context.close(); const videoPath = record ? await page.video().path() : null;
  await writeFile(new URL("recording.json", output), JSON.stringify({ completed, stages, videoPath, durationSeconds: (Date.now() - startedAt) / 1000 }, null, 2));
  await browser.close();
}

import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";
import { chromium } from "@playwright/test";

process.umask(0o077);
const origin = process.env.REWIND_TEST_URL || "http://127.0.0.1:18182";
const live = process.argv.includes("--live"), planOnly = process.argv.includes("--plan-only"), record = process.argv.includes("--record");
const output = process.env.REWIND_REPORT_DIR ? pathToFileURL(process.env.REWIND_REPORT_DIR.replace(/\/?$/, "/")) : new URL("../reports/", import.meta.url);
await mkdir(output, { recursive: true, mode: 0o700 });
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1512, height: 982 }, locale: "en-US", timezoneId: "UTC", acceptDownloads: true,
  ...(record ? { recordVideo: { dir: new URL("videos/", output).pathname, size: { width: 1512, height: 982 } } } : {}) });
const page = await context.newPage();
const startedAt = Date.now(), stages = [];
const mark = name => stages.push({ name, seconds: (Date.now() - startedAt) / 1000 });
const linger = async () => { if (record) await page.waitForTimeout(2500); };
const canonical = value => Array.isArray(value) ? "[" + value.map(canonical).join(",") + "]" : value && typeof value === "object" ? "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}" : JSON.stringify(value);
let completed = false, result, accessRequired;
try {
  await page.goto(origin); mark("opened");
  assert.match(await page.title(), /Rewind/);
  assert.ok(await page.getByRole("heading", { name: "Undo a bad import. Keep today’s work." }).isVisible());
  await page.screenshot({ path: new URL("desktop.png", output).pathname, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "Mobile page overflows horizontally.");
  await page.screenshot({ path: new URL("mobile.png", output).pathname, fullPage: true });
  await page.setViewportSize({ width: 1512, height: 982 });
  await page.getByRole("button", { name: "How it works" }).click();
  assert.ok(await page.getByRole("heading", { name: "Restore only what went wrong." }).isVisible());
  await page.getByRole("button", { name: "Got it" }).click(); mark("explained");
  if (live || planOnly) {
    const config = await (await page.request.get(origin + "/api/config")).json(); accessRequired = config.access_required;
    assert.equal(config.inspection_available, true, "The real isolated inspection worker is unavailable.");
    if (live) assert.equal(config.available, true, "The real Vultr inference connection is unavailable.");
    await page.getByRole("button", { name: "Try the bad-import example" }).click();
    if (accessRequired) {
      const code = (await readFile(process.env.REWIND_ACCESS_FILE || new URL("../.local/demo-access.txt", import.meta.url), "utf8")).trim();
      await page.getByLabel("Access code", { exact: true }).fill(code);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
    } else assert.equal(await page.locator("#access-dialog").isVisible(), false, "Public app requested an access code.");
    await page.getByRole("button", { name: "Investigate this import" }).waitFor({ state: "visible", timeout: 60000 });
    mark("inspected"); await page.screenshot({ path: new URL("three-versions.png", output).pathname, fullPage: true }); await linger();
    if (live) await writeFile(new URL("workspace.private.json", output), JSON.stringify(await page.evaluate(() => JSON.parse(sessionStorage.getItem("rewind-private-workspace"))), null, 2), { mode: 0o600 });
    if (live) {
      mark("diagnosis-started");
      await page.getByRole("button", { name: "Investigate this import" }).click();
      await page.getByRole("heading", { name: "117 proposed repairs. 3 later price edits protected.", exact: true }).waitFor({ timeout: 110000 });
      mark("diagnosed");
    } else await page.getByRole("button", { name: "Choose the fields myself" }).click();
    await page.getByRole("button", { name: "Review proposed repair" }).waitFor({ state: "visible", timeout: 10000 });
    assert.equal(await page.locator("#selection-count").innerText(), "117 of 117 cells selected");
    assert.match(await page.locator("#selection-protected").innerText(), /3 overlapping later edits/);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "Mobile recovery view overflows.");
    await page.screenshot({ path: new URL("mobile-review.png", output).pathname, fullPage: true });
    await page.setViewportSize({ width: 1512, height: 982 });
    await page.screenshot({ path: new URL("selected-cells.png", output).pathname, fullPage: true }); mark("selected"); await linger();
    await page.getByRole("button", { name: "Review proposed repair" }).click();
    await page.getByRole("heading", { name: "Restore 117 cells. Keep 127 invoices.", exact: true }).waitFor();
    assert.match(await page.locator("#planned-detail").innerText(), /Keep 7 newer records/);
    assert.match(await page.locator("#planned-detail").innerText(), /Keep 23 other updates/);
    assert.match(await page.locator("#plan-protected").innerText(), /3 overlapping later edits/);
    assert.match(await page.locator("#control-detail").innerText(), /Would lose 7/);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "Mobile approval view overflows.");
    await page.screenshot({ path: new URL("mobile-plan.png", output).pathname, fullPage: true });
    await page.setViewportSize({ width: 1512, height: 982 });
    await page.getByRole("button", { name: "Next approved cells", exact: true }).click();
    assert.match(await page.locator("#plan-page-info").innerText(), /Showing 11–20/);
    await page.getByRole("button", { name: "Previous approved cells", exact: true }).click();
    await page.screenshot({ path: new URL("recovery-plan.png", output).pathname, fullPage: true }); mark("planned"); await linger();
    if (planOnly) {
      const value = await page.evaluate(async () => {
        const workspace = JSON.parse(sessionStorage.getItem("rewind-private-workspace"));
        return (await fetch("/api/workspaces/" + workspace.id, { headers: { Authorization: "Bearer " + workspace.token } })).json();
      });
      assert.equal(value.model_calls, 0, "Plan-only browser check started a model call.");
      if (!config.available) assert.equal(await page.getByRole("button", { name: "Approve and restore 117 cells" }).isDisabled(), true);
      const capability = await page.evaluate(() => JSON.parse(sessionStorage.getItem("rewind-private-workspace")));
      const files = {};
      for (const name of ["backup", "checkpoint", "current"]) {
        const response = await page.request.get(origin + "/api/workspaces/" + capability.id + "/" + name, { headers: { Authorization: "Bearer " + capability.token } });
        assert.equal(response.status(), 200); files[name] = await response.body();
      }
      // Exercise real file uploads without a checkpoint, then delete this owned test workspace.
      await page.getByRole("button", { name: "New recovery", exact: true }).click();
      await page.getByRole("button", { name: "Use my own files", exact: true }).click();
      await page.getByLabel("Unique record ID column").fill("invoice_id");
      for (const name of ["backup", "current"]) await page.locator("#" + name + "-file").setInputFiles({ name: name + ".csv", mimeType: "text/csv", buffer: files[name] });
      await page.getByRole("button", { name: "Inspect my files" }).click();
      await page.locator("#selection-panel").waitFor({ state: "visible", timeout: 60000 });
      assert.match(await page.locator("#scope-note").innerText(), /No incident checkpoint/);
      await page.locator('#columns input[value="price"]').check();
      await page.locator("#comparison").waitFor({ state: "visible" });
      await page.locator("#select-all").check();
      assert.equal(await page.locator("#selection-count").innerText(), "120 of 120 cells selected");
      const firstCell = page.locator('#changes-body input[type="checkbox"]').first();
      const excludedCell = await firstCell.getAttribute("aria-label");
      await firstCell.uncheck();
      assert.equal(await page.locator("#selection-count").innerText(), "119 of 120 cells selected");
      await page.getByRole("button", { name: "Review proposed repair" }).click();
      await page.getByRole("heading", { name: "Restore 119 cells. Keep 127 records.", exact: true }).waitFor();
      await page.reload();
      await page.getByRole("heading", { name: "Restore 119 cells. Keep 127 records.", exact: true }).waitFor();
      await page.getByRole("button", { name: "Edit selection", exact: true }).click();
      assert.equal(await page.locator("#selection-count").innerText(), "119 of 120 cells selected");
      assert.equal(await page.getByRole("checkbox", { name: excludedCell, exact: true }).isChecked(), false, "Reload restored a deliberately excluded cell to the approval selection.");
      await page.getByRole("button", { name: "Delete this workspace", exact: true }).click();
      await page.getByRole("button", { name: "Delete files and workspace", exact: true }).click();
      await page.getByRole("heading", { name: "Undo a bad import. Keep today’s work." }).waitFor();
      assert.equal(await page.evaluate(() => sessionStorage.getItem("rewind-private-workspace")), null);
      // The legacy SQLite example remains usable through the unchanged API.
      await page.evaluate(async () => saveWorkspace(await api("/api/example", {})));
      await page.locator("#selection-panel").waitFor({ state: "visible", timeout: 60000 });
      assert.equal(await page.locator("#selection-count").innerText(), "120 of 120 cells selected");
      await page.getByRole("button", { name: "Review proposed repair" }).click();
      await page.getByRole("heading", { name: "Restore 120 cells. Keep 127 invoices.", exact: true }).waitFor();
      assert.match(await page.locator("#planned-detail").innerText(), /Keep 20 other updates/);
    }
    if (live) {
      // One diagnosis and one recovery sequence. No route mocks, job retries or provider fallback.
      mark("recovery-started");
      await page.getByRole("button", { name: "Approve and restore 117 cells" }).click();
      await page.getByRole("heading", { name: "117 invoice prices restored", exact: true }).waitFor({ timeout: 120000 });
      mark("verified");
      for (const [id, expected] of [["stat-restored", "117"], ["stat-rows", "127"], ["stat-new", "7"], ["stat-updates", "23"]]) assert.equal(await page.locator("#" + id).innerText(), expected);
      await page.getByRole("button", { name: "Next checked changes", exact: true }).click();
      assert.match(await page.locator("#result-page-info").innerText(), /Showing 11–20 of 117/);
      await page.getByRole("button", { name: "Previous checked changes", exact: true }).click();
      await page.screenshot({ path: new URL("verified-result.png", output).pathname, fullPage: true }); await linger();
      await page.locator("#result-protected").scrollIntoViewIfNeeded();
      await page.screenshot({ path: new URL("protected-result.png", output).pathname, fullPage: true }); mark("protected"); await linger();
      await page.getByText("How we checked this recovery", { exact: false }).click();
      await page.locator("#runtime-details").scrollIntoViewIfNeeded(); mark("checks");
      await page.screenshot({ path: new URL("containment-and-cleanup.png", output).pathname, fullPage: true }); await linger();
      const evidenceEvent = page.waitForEvent("download"); await page.getByRole("button", { name: "Download checks", exact: true }).click();
      await (await evidenceEvent).saveAs(new URL("live-evidence.json", output).pathname);
      result = JSON.parse(await readFile(new URL("live-evidence.json", output), "utf8"));
      assert.equal(createHash("sha256").update(canonical(result.payload)).digest("hex"), result.sha256);
      assert.equal(result.payload.status, "verified"); assert.equal(result.payload.model_calls, 2);
      assert.equal(result.payload.all_sandboxes_removed, true);
      assert.equal(result.payload.diagnosis.status, "proposed");
      assert.equal(result.payload.diagnosis.attempt.provider.name, "vultr-serverless-inference");
      assert.ok(result.payload.diagnosis.attempt.provider.response_id);
      assert.equal(result.payload.attempts.length, 1);
      assert.ok(result.payload.attempts.every(attempt => attempt.provider.name === "vultr-serverless-inference" && attempt.provider.response_id));
      assert.ok(result.payload.sandbox_runs.every(run => run.policy_verified && run.runtime === "runsc" && run.teardown.confirmed));
      const databaseEvent = page.waitForEvent("download"); await page.getByRole("button", { name: "Download recovered CSV" }).click();
      await (await databaseEvent).saveAs(new URL("recovered.csv", output).pathname); mark("exported");
      assert.equal(createHash("sha256").update(await readFile(new URL("recovered.csv", output))).digest("hex"), result.payload.artifact_sha256);
      await writeFile(new URL("workspace.private.json", output), JSON.stringify(await page.evaluate(() => JSON.parse(sessionStorage.getItem("rewind-private-workspace"))), null, 2), { mode: 0o600 });
      await page.reload(); await page.getByRole("heading", { name: "117 invoice prices restored", exact: true }).waitFor(); mark("persisted");
    }
  }
  completed = true;
  console.log(live ? "Actual Vultr diagnosis and recovery passed: 117 prices restored, 127 invoices, 7 newer records, 20 payments and 3 later price edits retained; two model calls, all sandboxes removed and downloaded CSV fingerprint verified." : planOnly ? "Actual isolated CSV inspection and checkpoint review passed: 117 exact cells, 3 protected overlaps, 127 retained records. No model call started." : "Browser layout and explanation checks passed without model calls.");
} catch (error) {
  await page.screenshot({ path: new URL("incomplete.png", output).pathname, fullPage: true }).catch(() => {});
  await writeFile(new URL("browser-error.private.txt", output), error.stack || String(error), { mode: 0o600 });
  console.error("Browser verification failed (" + error.name + "). Private diagnostics are in reports/. No retry was started."); process.exitCode = 1;
} finally {
  await writeFile(new URL("browser-verification.json", output), JSON.stringify({ at: new Date().toISOString(), origin, live, planOnly, record, completed, accessRequired, evidenceSha256: result?.sha256 }, null, 2));
  await context.close();
  const videoPath = record ? await page.video().path() : null;
  await writeFile(new URL("recording-metadata.json", output), JSON.stringify({ completed, origin, live, stages, videoPath, durationSeconds: (Date.now() - startedAt) / 1000 }, null, 2));
  await browser.close();
}

import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";
import { chromium } from "@playwright/test";

process.umask(0o077);
const origin = process.env.REWIND_TEST_URL || "http://127.0.0.1:18182";
const live = process.argv.includes("--live");
const planOnly = process.argv.includes("--plan-only");
const record = process.argv.includes("--record");
const output = process.env.REWIND_REPORT_DIR ? pathToFileURL(process.env.REWIND_REPORT_DIR.replace(/\/?$/, "/")) : new URL("../reports/", import.meta.url);
await mkdir(output, { recursive: true, mode: 0o700 });
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1512, height: 982 }, locale: "en-US", timezoneId: "UTC",
  acceptDownloads: true, ...(record ? { recordVideo: { dir: new URL("videos/", output).pathname, size: { width: 1512, height: 982 } } } : {}) });
const page = await context.newPage();
let completed = false, result, accessRequired;
try {
  await page.goto(origin);
  assert.match(await page.title(), /Rewind/);
  assert.ok(await page.getByRole("heading", { name: "Undo a bad import. Keep today’s work." }).isVisible());
  await page.screenshot({ path: new URL("desktop.png", output).pathname, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "Mobile page overflows horizontally.");
  await page.screenshot({ path: new URL("mobile.png", output).pathname, fullPage: true });
  await page.setViewportSize({ width: 1512, height: 982 });
  await page.getByRole("button", { name: "How it works" }).click();
  assert.ok(await page.getByRole("heading", { name: "Restore only what went wrong." }).isVisible());
  await page.getByRole("button", { name: "Got it" }).click();
  if (live || planOnly) {
    const configResponse = await page.request.get(origin + "/api/config");
    const config = await configResponse.json();
    accessRequired = config.access_required;
    assert.equal(config.inspection_available, true, "The real isolated inspection worker is unavailable.");
    if (live) assert.equal(config.available, true, "The real isolated Vultr recovery is unavailable.");
    await page.getByRole("button", { name: "Try the invoice example" }).click();
    if (accessRequired) {
      const code = (await readFile(process.env.REWIND_ACCESS_FILE || new URL("../.local/demo-access.txt", import.meta.url), "utf8")).trim();
      await page.getByLabel("Demo access code", { exact: true }).fill(code);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
    } else {
      assert.equal(await page.locator("#access-dialog").isVisible(), false, "The public demo asked for a shared access code.");
    }
    await page.getByRole("button", { name: "Review my selection" }).waitFor({ state: "visible", timeout: 55000 });
    assert.equal(await page.locator("#selection-count").innerText(), "120 of 120 changed records selected");
    await page.screenshot({ path: new URL("selected-cells.png", output).pathname, fullPage: true });
    await page.waitForTimeout(1600);
    await page.getByRole("button", { name: "Review my selection" }).click();
    await page.getByRole("heading", { name: "Restore 120 cells. Keep 127 invoices." }).waitFor();
    assert.match(await page.locator("#planned-detail").innerText(), /Keep 7 newer records/);
    assert.match(await page.locator("#planned-detail").innerText(), /Keep 20 other updates/);
    assert.match(await page.locator("#control-detail").innerText(), /Would lose 7/);
    await page.screenshot({ path: new URL("recovery-plan.png", output).pathname, fullPage: true });
    await page.waitForTimeout(1800);
    if (planOnly && !config.available) {
      assert.equal(await page.getByRole("button", { name: "Restore these 120 cells" }).isDisabled(), true);
      const refused = await page.evaluate(async () => {
        const workspace = JSON.parse(sessionStorage.getItem("rewind-private-workspace"));
        const read = await fetch("/api/workspaces/" + workspace.id, { headers: { Authorization: "Bearer " + workspace.token } });
        const value = await read.json();
        const response = await fetch("/api/workspaces/" + workspace.id + "/recover", { method: "POST", headers: { Authorization: "Bearer " + workspace.token, "Content-Type": "application/json" }, body: JSON.stringify({ plan_sha256: value.plan.sha256 }) });
        return response.status;
      });
      assert.equal(refused, 503, "An unconfigured model was allowed to start recovery.");
    }
    if (live) {
    // One real recovery request. The bounded server may correct one rejected
    // candidate, but this recorder never retries a model turn or mocks a route.
    await page.getByRole("button", { name: "Restore these 120 cells" }).click();
    await page.getByRole("heading", { name: "120 invoice prices restored", exact: true }).waitFor({ timeout: 185000 });
    assert.equal(await page.locator("#stat-restored").innerText(), "120");
    assert.equal(await page.locator("#stat-rows").innerText(), "127");
    assert.equal(await page.locator("#stat-new").innerText(), "7");
    assert.equal(await page.locator("#stat-updates").innerText(), "20");
    await page.screenshot({ path: new URL("verified-result.png", output).pathname, fullPage: true });
    await page.waitForTimeout(1800);
    await page.getByText("How we checked this recovery", { exact: false }).click();
    await page.locator("#runtime-details").scrollIntoViewIfNeeded();
    await page.screenshot({ path: new URL("containment-and-cleanup.png", output).pathname, fullPage: true });
    await page.waitForTimeout(1800);
    const evidenceDownload = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download checks", exact: true }).click();
    const evidence = await evidenceDownload;
    const evidencePath = new URL("live-evidence.json", output).pathname;
    await evidence.saveAs(evidencePath);
    result = JSON.parse(await readFile(evidencePath, "utf8"));
    const canonical = value => {
      if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
      if (value && typeof value === "object") return "{" + Object.keys(value).sort().map(key => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
      return JSON.stringify(value);
    };
    assert.equal(createHash("sha256").update(canonical(result.payload)).digest("hex"), result.sha256);
    assert.equal(result.payload.status, "verified");
    assert.equal(result.payload.all_sandboxes_removed, true);
    assert.ok(result.payload.attempts.every(attempt => attempt.provider.name === "vultr-serverless-inference" && attempt.provider.response_id));
    assert.ok(result.payload.sandbox_runs.every(run => run.policy_verified && run.runtime === "runsc" && run.teardown.confirmed));
    const databaseDownload = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download recovered database" }).click();
    const database = await databaseDownload;
    const artifactPath = new URL("recovered.sqlite", output).pathname;
    await database.saveAs(artifactPath);
    assert.equal(createHash("sha256").update(await readFile(artifactPath)).digest("hex"), result.payload.artifact_sha256);
    await page.reload();
    await page.getByRole("heading", { name: "120 invoice prices restored", exact: true }).waitFor();
    await page.waitForTimeout(1700);
    }
  }
  completed = true;
  console.log(live ? "Actual Vultr recovery passed: 120 prices restored, 127 invoices retained, 7 newer invoices and 20 payment updates preserved. All sandboxes removed; downloaded artifact matches its checked fingerprint." : planOnly ? "Actual sandboxed database inspection and selection plan passed: 120 selected price cells, 127 current invoices, 7 newer invoices and 20 payment updates. No model call was started." : "Actual browser layout and explanation checks passed. No recovery or model call was started.");
} catch (error) {
  await page.screenshot({ path: new URL("incomplete.png", output).pathname, fullPage: true }).catch(() => {});
  console.error("Browser verification failed (" + error.name + "). Private diagnostics are in reports/. No retry was started.");
  process.exitCode = 1;
} finally {
  await writeFile(new URL("browser-verification.json", output), JSON.stringify({ at: new Date().toISOString(), origin, live, planOnly, record, completed, accessRequired, evidenceSha256: result?.sha256 }, null, 2));
  await context.close();
  await browser.close();
}

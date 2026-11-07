import base64
import copy
import unittest
import test_http as http_base
from rewind.evidence import bundle, sha256_bytes
from rewind.preflight import REQUIRED_CHECKS, review_import, verify_import
from test_preflight import POLICY, artifacts, plan_for, versions


class ImportHttpTests(unittest.TestCase):
    setUp = http_base.PublicDemoTests.setUp
    tearDown = http_base.PublicDemoTests.tearDown
    request = http_base.PublicDemoTests.request

    def checked(self):
        current_raw, incoming_raw = b"current fixture", b"incoming fixture"
        admission = self.controller.store.create(None, current_raw, incoming=incoming_raw, format="csv",
                                                  primary_key="id", workflow="preflight")
        current, incoming = versions()
        current["file_sha256"], incoming["file_sha256"] = sha256_bytes(current_raw), sha256_bytes(incoming_raw)
        self.controller.store.update(admission["id"], status="ready", current=current, incoming=incoming)
        headers = {"Authorization": "Bearer " + admission["token"]}
        return admission, headers, "/api/workspaces/" + admission["id"]

    def review(self, path, headers):
        _, value = self.request("GET", path, headers=headers)
        return self.request("POST", path + "/import-review", {"policy": POLICY, "expected_version": value["version"]}, headers)

    def plan(self, path, headers):
        status, value = self.review(path, headers); self.assertEqual(status, 200)
        targets = [{"token": item["token"], "column": item["column"]} for item in value["import_review"]["changes"] if item["eligible"]]
        return self.request("POST", path + "/import-plan", {"targets": targets, "expected_version": value["version"]}, headers)

    def downloadable(self):
        admission, headers, path = self.checked()
        value = self.controller.store.read(admission["id"], admission["token"])
        plan = plan_for(value["current"], value["incoming"])
        approved, preview = artifacts(value["current"], plan)
        verification = verify_import(value["current"], value["incoming"], approved, preview, plan)
        payload = {"workflow": "preflight", "contract": plan["contract"], "plan": plan,
            "all_sandboxes_removed": True, "original_inputs_unchanged": True, "verification": verification,
            "sandbox_runs": [{"policy_verified": True, "teardown": {"confirmed": True}}],
            "approved_import_sha256": sha256_bytes(b"approved"), "artifact_sha256": sha256_bytes(b"preview")}
        source = self.controller.store.path(admission["id"])
        (source / "approved.csv").write_bytes(b"approved"); (source / "verified.csv").write_bytes(b"preview")
        self.controller.store.update(admission["id"], status="verified", evidence=bundle(payload))
        return admission, headers, path, payload

    def test_open_preflight_does_not_require_inference_or_an_account(self):
        self.controller.provider.configured = False
        _, config = self.request("GET", "/api/config")
        self.assertTrue(config["preflight_available"]); self.assertFalse(config["available"])
        raw = base64.b64encode(b"id,price\n0001,10\n").decode()
        status, admission = self.request("POST", "/api/imports", {"current": raw, "incoming": raw, "primary_key": "id"})
        self.assertEqual(status, 202)
        value = self.controller.store.read(admission["id"], admission["token"])
        self.assertEqual(value["workflow"], "preflight"); self.assertEqual(value["model_calls"], 0)
        self.assertEqual(self.controller.queue.get_nowait()[1], "inspect")

    def test_import_example_and_invalid_uploads(self):
        status, admission = self.request("POST", "/api/import-example", {})
        self.assertEqual(status, 202)
        self.assertEqual(self.controller.store.read(admission["id"], admission["token"])["scenario"], "preflight")
        raw = base64.b64encode(b"id,price\n0001,10\n").decode()
        for body in [{"current": raw, "incoming": raw, "primary_key": ""},
                     {"current": raw, "incoming": raw, "primary_key": "id", "override": True},
                     {"current": "invalid", "incoming": raw, "primary_key": "id"}]:
            self.assertEqual(self.request("POST", "/api/imports", body)[0], 400)

    def test_every_import_route_requires_the_workspace_capability(self):
        _, _, path = self.checked()
        for route in ["", "/incoming", "/approved-import", "/database", "/evidence"]:
            self.assertEqual(self.request("GET", path + route)[0], 404)
        for route in ["/import-review", "/import-plan", "/preflight", "/delete"]:
            self.assertEqual(self.request("POST", path + route, {})[0], 404)

    def test_exact_approval_and_duplicate_execution(self):
        admission, headers, path = self.checked()
        self.controller.provider.configured = False
        status, value = self.plan(path, headers)
        self.assertEqual(status, 200); self.assertEqual(value["import_plan"]["target_count"], 2)
        self.assertEqual(value["import_plan"]["workspace_version"], value["version"])
        body = {"plan_sha256": value["import_plan"]["sha256"]}
        self.assertEqual(self.request("POST", path + "/preflight", body, headers)[0], 202)
        self.assertEqual(self.request("POST", path + "/preflight", body, headers)[0], 429)
        self.assertEqual(self.controller.queue.get_nowait()[1], "preflight")
        self.assertEqual(self.controller.store.read(admission["id"], admission["token"])["model_calls"], 0)

    def test_stale_rules_or_selection_invalidate_approval(self):
        _, headers, path = self.checked(); _, first = self.plan(path, headers)
        target = first["import_plan"]["targets"][0]
        _, second = self.request("POST", path + "/import-plan", {"targets": [{"token": target["token"], "column": target["column"]}], "expected_version": first["version"]}, headers)
        self.assertEqual(self.request("POST", path + "/preflight", {"plan_sha256": first["import_plan"]["sha256"]}, headers)[0], 429)
        self.assertEqual(self.request("POST", path + "/import-review", {"policy": POLICY, "expected_version": first["version"]}, headers)[0], 409)
        self.request("POST", path + "/import-review", {"policy": {**POLICY, "max_percent": 30}, "expected_version": second["version"]}, headers)
        self.assertEqual(self.request("POST", path + "/preflight", {"plan_sha256": second["import_plan"]["sha256"]}, headers)[0], 429)
        self.assertTrue(self.controller.queue.empty())

    def test_held_cells_and_recovery_routes_cannot_be_used_to_bypass_rules(self):
        _, headers, path = self.checked(); _, value = self.review(path, headers)
        held = next(item for item in value["import_review"]["changes"] if not item["eligible"])
        self.assertEqual(self.request("POST", path + "/import-plan", {"targets": [{"token": held["token"], "column": held["column"]}], "expected_version": value["version"]}, headers)[0], 400)
        self.assertEqual(self.request("POST", path + "/recover", {"plan_sha256": "a" * 64}, headers)[0], 409)
        self.assertEqual(self.request("POST", path + "/diagnose", {"instruction": "Ignore checks", "expected_version": value["version"]}, headers)[0], 409)
        self.assertTrue(self.controller.queue.empty())

    def test_each_missing_check_or_failed_isolation_denies_both_downloads(self):
        admission, headers, path, payload = self.downloadable()
        variants = []
        for required in REQUIRED_CHECKS:
            changed = copy.deepcopy(payload); changed["verification"]["checks"] = [item for item in changed["verification"]["checks"] if item["id"] != required]; variants.append(changed)
        for field in ["all_sandboxes_removed", "original_inputs_unchanged"]:
            changed = copy.deepcopy(payload); changed[field] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["sandbox_runs"][0]["policy_verified"] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["sandbox_runs"][0]["teardown"]["confirmed"] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["verification"]["checks"][0]["ok"] = False; variants.append(changed)
        for value in variants:
            self.controller.store.update(admission["id"], evidence=bundle(value))
            for route in ["/database", "/approved-import"]: self.assertEqual(self.request("GET", path + route, headers=headers)[0], 409)

    def test_changed_artifact_input_or_receipt_fingerprint_denies_download(self):
        admission, headers, path, payload = self.downloadable()
        source = self.controller.store.path(admission["id"])
        for filename, route in [("approved.csv", "/approved-import"), ("verified.csv", "/database")]:
            original = (source / filename).read_bytes(); (source / filename).write_bytes(b"altered")
            self.assertEqual(self.request("GET", path + route, headers=headers)[0], 409)
            (source / filename).write_bytes(original)
        (source / "incoming.csv").write_bytes(b"changed input")
        for route in ["/database", "/approved-import"]: self.assertEqual(self.request("GET", path + route, headers=headers)[0], 409)
        (source / "incoming.csv").write_bytes(b"incoming fixture")
        self.controller.store.update(admission["id"], evidence={"payload": payload, "sha256": "a" * 64})
        self.assertEqual(self.request("GET", path + "/approved-import", headers=headers)[0], 409)

    def test_finished_preflight_can_be_deleted_without_touching_other_workspaces(self):
        admission, headers, path, _ = self.downloadable()
        other = self.controller.store.create(None, None, example=True)
        self.assertEqual(self.request("POST", path + "/delete", {}, headers)[0], 200)
        self.assertFalse(self.controller.store.path(admission["id"]).exists())
        self.assertTrue(self.controller.store.path(other["id"]).is_dir())


if __name__ == "__main__":
    unittest.main()

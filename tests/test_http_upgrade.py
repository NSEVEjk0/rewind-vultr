import base64
import copy
import json
import unittest
import test_http as http_base
from test_upgrade import versions
from rewind.contract import compare, make_plan
from rewind.evidence import sha256_bytes


class UpgradeHttpTests(unittest.TestCase):
    setUp = http_base.PublicDemoTests.setUp
    tearDown = http_base.PublicDemoTests.tearDown
    request = http_base.PublicDemoTests.request

    def checked(self):
        admission = self.controller.store.create(None, None, example=True, scenario="overlap")
        good, incident, today = versions()
        self.controller.store.update(admission["id"], status="ready", backup=good, checkpoint=incident, current=today)
        headers = {"Authorization": "Bearer " + admission["token"]}
        path = "/api/workspaces/" + admission["id"]
        return admission, headers, path

    def plan_request(self, path, headers):
        _, value = self.request("GET", path, headers=headers)
        comparison = compare(value["backup"], value["current"], ["price_cents"], value["checkpoint"])
        targets = [{"token": row["token"], "column": item["column"]} for row in comparison["choices"] for item in row["differences"]]
        return self.request("POST", path + "/plan", {"targets": targets, "expected_version": value["version"]}, headers)

    def test_csv_upload_requires_key_and_supports_optional_checkpoint(self):
        encoded = base64.b64encode(b"id,price\n001,2.00\n").decode()
        status, _ = self.request("POST", "/api/workspaces", {"format": "csv", "backup": encoded, "current": encoded})
        self.assertEqual(status, 400)
        status, admission = self.request("POST", "/api/workspaces", {"format": "csv", "primary_key": "id",
             "backup": encoded, "checkpoint": encoded, "current": encoded})
        self.assertEqual(status, 202)
        value = self.controller.store.read(admission["id"], admission["token"])
        self.assertEqual(value["format"], "csv"); self.assertTrue(value["has_checkpoint"])
        self.assertEqual(value["model_calls"], 0)
        self.assertEqual((self.controller.store.path(admission["id"]) / "checkpoint.csv").read_bytes(), b"id,price\n001,2.00\n")

    def test_request_bound_handles_three_maximum_size_files(self):
        encoded = base64.b64encode(b"x"*(256*1024)).decode()
        status, admission = self.request("POST", "/api/workspaces", {"format": "csv", "primary_key": "id",
            "backup": encoded, "checkpoint": encoded, "current": encoded})
        self.assertEqual(status, 202)
        self.assertEqual(self.controller.store.read(admission["id"], admission["token"])["status"], "inspecting")

    def test_legacy_and_new_examples_remain_available_without_signin(self):
        for body, format, checkpoint in [({}, "sqlite", False), ({"format": "csv", "scenario": "overlap"}, "csv", True)]:
            status, admission = self.request("POST", "/api/example", body)
            self.assertEqual(status, 202)
            value = self.controller.store.read(admission["id"], admission["token"])
            self.assertEqual(value["format"], format); self.assertEqual(value["has_checkpoint"], checkpoint)

    def test_diagnosis_requires_owner_three_checked_versions_and_current_version(self):
        _, headers, path = self.checked()
        _, value = self.request("GET", path, headers=headers)
        body = {"instruction": "Undo the bad price import.", "expected_version": value["version"]}
        self.assertEqual(self.request("POST", path+"/diagnose", body)[0], 404)
        self.assertEqual(self.request("POST", path+"/diagnose", {**body, "expected_version": value["version"]-1}, headers)[0], 429)
        self.assertEqual(self.request("POST", path+"/diagnose", body, headers)[0], 202)
        self.assertEqual(self.request("POST", path+"/diagnose", body, headers)[0], 429)
        self.assertEqual(self.controller.queue.get_nowait()[1], "diagnose")
        value = self.controller.store.read(path.split("/")[-1], headers["Authorization"][7:])
        self.assertTrue(value["diagnosis_started"]); self.assertIsNone(value["plan"])
        self.assertEqual(value["model_calls"], 0, "Queued requests are counted only when the worker attempts the provider.")

    def test_missing_checkpoint_and_empty_or_oversized_task_are_rejected(self):
        admission, headers, path = self.checked()
        self.controller.store.update(admission["id"], checkpoint=None)
        value = self.controller.store.read(admission["id"], admission["token"])
        self.assertEqual(self.request("POST", path+"/diagnose", {"instruction": "Prices broke.", "expected_version": value["version"]}, headers)[0], 429)
        for instruction in ["", "x"*1001]:
            self.assertEqual(self.request("POST", path+"/diagnose", {"instruction": instruction, "expected_version": value["version"]}, headers)[0], 400)
        self.assertTrue(self.controller.queue.empty())

    def test_exact_plan_rejects_overlap_and_binds_workspace_version(self):
        admission, headers, path = self.checked()
        status, value = self.plan_request(path, headers)
        self.assertEqual(status, 200); self.assertEqual(value["plan"]["target_count"], 117)
        self.assertEqual(value["plan"]["workspace_version"], value["version"])
        comparison = compare(value["backup"], value["current"], ["price_cents"], value["checkpoint"])
        overlap = comparison["overlaps"][0]
        status, _ = self.request("POST", path+"/plan", {"targets": [{"token": overlap["token"], "column": overlap["column"]}],
            "expected_version": value["version"]}, headers)
        self.assertEqual(status, 400)
        self.controller.store.update(admission["id"], message="The reviewed workspace changed.")
        self.assertEqual(self.request("POST", path+"/recover", {"plan_sha256": value["plan"]["sha256"]}, headers)[0], 429)
        self.assertTrue(self.controller.queue.empty())

    def test_changed_selection_rejects_old_plan_approval(self):
        _, headers, path = self.checked()
        _, value = self.plan_request(path, headers)
        old = value["plan"]["sha256"]
        target = value["plan"]["targets"][0]
        _, current = self.request("POST", path+"/plan", {"targets": [{"token": target["token"], "column": target["column"]}], "expected_version": value["version"]}, headers)
        self.assertNotEqual(old, current["plan"]["sha256"])
        self.assertEqual(self.request("POST", path+"/recover", {"plan_sha256": old}, headers)[0], 429)
        self.assertEqual(self.request("POST", path+"/recover", {"plan_sha256": current["plan"]["sha256"]}, headers)[0], 202)

    def test_failed_checks_cleanup_and_hash_changes_each_deny_download(self):
        admission, headers, path = self.checked()
        raw = b"id,price\n001,1.20\n"
        (self.controller.store.path(admission["id"])/"verified.sqlite").write_bytes(raw)
        ids = ["sqlite-integrity", "same-schema", "same-records", "same-sequences", "restored-cells", "protected-cells"]
        payload = {"all_sandboxes_removed": True, "verification": {"ok": True, "checks": [{"id": name, "ok": True} for name in ids]},
            "sandbox_runs": [{"policy_verified": True, "teardown": {"confirmed": True}}], "artifact_sha256": sha256_bytes(raw)}
        variants = []
        changed = copy.deepcopy(payload); changed["verification"]["ok"] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["verification"]["checks"][0]["ok"] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["verification"]["checks"] = []; variants.append(changed)
        changed = copy.deepcopy(payload); changed["all_sandboxes_removed"] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["sandbox_runs"][0]["teardown"]["confirmed"] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["sandbox_runs"][0]["policy_verified"] = False; variants.append(changed)
        changed = copy.deepcopy(payload); changed["artifact_sha256"] = "f"*64; variants.append(changed)
        for item in variants:
            self.controller.store.update(admission["id"], status="verified", evidence={"payload": item})
            self.assertEqual(self.request("GET", path+"/database", headers=headers)[0], 409)

    def test_checkpoint_download_stays_private(self):
        _, _, path = self.checked()
        self.assertEqual(self.request("GET", path+"/checkpoint")[0], 404)

    def test_workspace_deletion_requires_owner_and_finished_operation(self):
        admission, headers, path = self.checked()
        self.assertEqual(self.request("POST", path+"/delete", {})[0], 404)
        self.controller.store.update(admission["id"], status="queued")
        self.assertEqual(self.request("POST", path+"/delete", {}, headers)[0], 429)
        self.controller.store.update(admission["id"], status="ready")
        self.assertEqual(self.request("POST", path+"/delete", {}, headers)[0], 200)
        self.assertFalse(self.controller.store.path(admission["id"]).exists())
        for suffix in ["", "/database", "/backup", "/checkpoint", "/evidence"]:
            self.assertEqual(self.request("GET", path+suffix, headers=headers)[0], 404)


if __name__ == "__main__":
    unittest.main()

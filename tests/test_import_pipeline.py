import base64
import copy
import unittest
from rewind.evidence import sha256_bytes
from rewind.import_pipeline import run_preflight
from rewind.pipeline import InspectionError
from test_preflight import artifacts, plan_for, versions


class ImportPipelineTests(unittest.TestCase):
    def setUp(self):
        self.current, self.incoming = versions()
        self.current_raw, self.incoming_raw = b"current fixture", b"incoming fixture"
        self.current["file_sha256"] = sha256_bytes(self.current_raw)
        self.incoming["file_sha256"] = sha256_bytes(self.incoming_raw)
        self.plan = plan_for(self.current, self.incoming)
        self.approved, self.preview = artifacts(self.current, self.plan)
        self.approved_raw, self.preview_raw = b"approved artifact fixture", b"preview artifact fixture"
        self.approved["file_sha256"] = sha256_bytes(self.approved_raw)
        self.preview["file_sha256"] = sha256_bytes(self.preview_raw)
        self.prior = [{"policy_verified": True, "teardown": {"confirmed": True}}]
        self.calls = []
        owner = self
        class Sandbox:
            def run(self, operation, files, *, scope):
                owner.calls.append((operation, files, scope))
                if operation == "inspect":
                    payload = owner.approved if files["current.csv"] == owner.approved_raw else owner.preview
                else:
                    payload = {"artifact": base64.b64encode(owner.approved_raw if operation == "prepare-import" else owner.preview_raw).decode()}
                return {"operation": operation, "ok": True, "payload": payload,
                        "policy_verified": True, "teardown": {"confirmed": True}}
        self.sandbox = Sandbox()

    def run_pipeline(self, **changes):
        args = dict(current=self.current, incoming=self.incoming, current_raw=self.current_raw,
                    incoming_raw=self.incoming_raw, plan=self.plan, prior_runs=self.prior, sandbox=self.sandbox)
        return run_preflight(**{**args, **changes})

    def test_separate_file_production_inspection_execution_and_acceptance(self):
        evidence, approved, preview = self.run_pipeline()
        self.assertEqual((approved, preview), (self.approved_raw, self.preview_raw))
        self.assertEqual([item[0] for item in self.calls], ["prepare-import", "inspect", "import-preview", "inspect"])
        self.assertEqual(self.calls[2][1]["incoming.csv"], self.approved_raw)
        self.assertNotIn("targets", self.calls[0][2], "The file-producing worker must not receive acceptance values.")
        self.assertEqual(evidence["payload"]["model_calls"], 0)
        self.assertTrue(evidence["payload"]["verification"]["ok"])

    def test_changed_inputs_or_plan_stop_before_execution(self):
        for change in [{"current_raw": b"changed"}, {"incoming_raw": b"changed"}]:
            with self.assertRaises(InspectionError): self.run_pipeline(**change)
        plan = copy.deepcopy(self.plan); plan["sha256"] = "f" * 64
        with self.assertRaises(InspectionError): self.run_pipeline(plan=plan)
        self.assertEqual(self.calls, [])

    def test_missing_prior_isolation_or_teardown_stops_before_execution(self):
        for prior in [[], [{"policy_verified": False, "teardown": {"confirmed": True}}],
                      [{"policy_verified": True, "teardown": {"confirmed": False}}]]:
            with self.assertRaises(InspectionError): self.run_pipeline(prior_runs=prior)
        self.assertEqual(self.calls, [])

    def test_failed_removal_stops_before_any_later_run(self):
        original = self.sandbox.run
        def fail(*args, **kwargs):
            result = original(*args, **kwargs); result["teardown"]["confirmed"] = False
            return result
        self.sandbox.run = fail
        with self.assertRaises(InspectionError): self.run_pipeline()
        self.assertEqual([item[0] for item in self.calls], ["prepare-import"])

    def test_consistently_wrong_import_and_preview_still_fail_independent_rules(self):
        self.approved["rows"][0]["values"]["price"]["value"] = "9999"
        self.preview["rows"][0]["values"]["price"]["value"] = "9999"
        evidence, approved, preview = self.run_pipeline()
        self.assertEqual(evidence["payload"]["status"], "failed")
        self.assertIsNone(approved); self.assertIsNone(preview)
        self.assertIsNone(evidence["payload"]["approved_import_sha256"])
        self.assertIsNone(evidence["payload"]["artifact_sha256"])


if __name__ == "__main__":
    unittest.main()

"""Trusted JSON fixtures test authority and accounting; no uploaded parsing."""
import base64
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from rewind.contract import ScopeError, compare, diagnostic_metadata, incident_changes, key_token, make_plan, rebind_plan, row_map, validate_diagnosis, verify_candidate
from rewind.evidence import sha256_bytes
from rewind.pipeline import InspectionError, decode_file, run_diagnosis, run_recovery
from rewind.provider import VultrProvider
from rewind.store import AdmissionDenied, Store
from test_contract import integer, snapshots, text


def versions():
    good, today = snapshots()
    for version in (good, today):
        for column in version["columns"]:
            column["declared_type"] = "INTEGER" if column["name"] != "payment_status" else "TEXT"
    incident = copy.deepcopy(today)
    incident["rows"] = incident["rows"][:120]
    incident["row_count"] = 120
    incident["file_sha256"] = "d" * 64
    for row in incident["rows"]:
        row["values"]["payment_status"] = text("unpaid")
    for number, value in [(3, 3100), (42, 7900), (88, 13750)]:
        today["rows"][number - 1]["values"]["price_cents"] = integer(value)
    return good, incident, today


def precise_plan(good, incident, today):
    changes = compare(good, today, ["price_cents"], incident)
    return make_plan(good, today, targets=[{"token": row["token"], "column": item["column"]}
                     for row in changes["choices"] for item in row["differences"]], checkpoint=incident)


def repaired(good, today, plan):
    result = copy.deepcopy(today)
    old, rows = row_map(good), row_map(result)
    for item in plan["targets"]:
        rows[item["token"]]["values"][item["column"]] = old[item["token"]]["values"][item["column"]]
    return result


class OverlapContractTests(unittest.TestCase):
    def test_117_prices_restored_with_23_updates_and_3_overlaps_protected(self):
        good, incident, today = versions()
        plan = precise_plan(good, incident, today)
        result = verify_candidate(good, today, repaired(good, today, plan), plan)
        self.assertTrue(result["ok"])
        self.assertEqual((result["restored_cells"], result["retained_rows"], result["retained_new_rows"],
                          result["protected_update_count"], result["protected_overlap_count"]), (117, 127, 7, 23, 3))
        self.assertEqual(plan["history"]["later_update_count"], 23)

    def test_blind_price_restore_and_whole_backup_are_rejected(self):
        good, incident, today = versions()
        plan = precise_plan(good, incident, today)
        blind = repaired(good, today, plan)
        for number in (3, 42, 88):
            blind["rows"][number-1]["values"]["price_cents"] = good["rows"][number-1]["values"]["price_cents"]
        self.assertFalse(verify_candidate(good, today, blind, plan)["ok"])
        self.assertFalse(verify_candidate(good, today, good, plan)["ok"])
        with self.assertRaises(ScopeError):
            make_plan(good, today, targets=[{"token": key_token(integer(3)), "column": "price_cents"}], checkpoint=incident)

    def test_exact_cells_do_not_authorize_rows_times_columns(self):
        good, today = snapshots()
        targets = [{"token": key_token(integer(1)), "column": "price_cents"},
                   {"token": key_token(integer(2)), "column": "payment_status"}]
        plan = make_plan(good, today, targets=targets)
        result = repaired(good, today, plan)
        self.assertTrue(verify_candidate(good, today, result, plan)["ok"])
        result["rows"][0]["values"]["payment_status"] = text("unpaid")
        result["rows"][1]["values"]["price_cents"] = good["rows"][1]["values"]["price_cents"]
        verification = verify_candidate(good, today, result, plan)
        self.assertFalse(verification["ok"])
        self.assertEqual(len(verification["failures"]["protected_cells"]), 2)

    def test_already_restored_cells_and_missing_checkpoint_records_are_excluded(self):
        good, incident, today = versions()
        today["rows"][0]["values"]["price_cents"] = good["rows"][0]["values"]["price_cents"]
        incident["rows"].pop(1); incident["row_count"] -= 1
        comparison = compare(good, today, ["price_cents"], incident)
        self.assertEqual(len(comparison["already_restored"]), 1)
        self.assertEqual(len(comparison["choices"]), 115)

    def test_current_deletions_are_preserved(self):
        good, incident, today = versions()
        today["rows"].pop(0); today["row_count"] -= 1
        plan = precise_plan(good, incident, today)
        verification = verify_candidate(good, today, repaired(good, today, plan), plan)
        self.assertTrue(verification["ok"])
        self.assertEqual((verification["restored_cells"], verification["retained_rows"]), (116, 126))

    def test_no_checkpoint_requires_manual_scope_and_cannot_identify_overlaps(self):
        good, _, today = versions()
        comparison = compare(good, today, ["price_cents"])
        self.assertFalse(comparison["checkpoint_available"])
        self.assertIn("cannot identify later edits", comparison["history_note"])
        self.assertEqual(len(comparison["choices"]), 120)
        plan = make_plan(good, today, ["price_cents"], [item["token"] for item in comparison["choices"]])
        self.assertFalse(plan["history"]["checkpoint_available"])

    def test_snapshots_cannot_detect_edits_that_returned_to_incident_value(self):
        good, incident, today = versions()
        today["rows"][2]["values"]["price_cents"] = copy.deepcopy(incident["rows"][2]["values"]["price_cents"])
        comparison = compare(good, today, ["price_cents"], incident)
        self.assertEqual(len(comparison["choices"]), 118)
        self.assertIn("returned to the same value", comparison["history_note"])

    def test_approval_binds_checkpoint_version_and_exact_targets(self):
        good, incident, today = versions()
        plan = precise_plan(good, incident, today)
        self.assertEqual(rebind_plan(good, today, plan, incident)["sha256"], plan["sha256"])
        changed = copy.deepcopy(incident); changed["file_sha256"] = "e" * 64
        self.assertNotEqual(rebind_plan(good, today, plan, changed)["sha256"], plan["sha256"])
        versioned = make_plan(good, today, targets=[{"token": plan["targets"][0]["token"], "column": "price_cents"}],
                              checkpoint=incident, workspace_version=10)
        self.assertNotEqual(versioned["sha256"], make_plan(good, today, targets=[{"token": plan["targets"][0]["token"], "column": "price_cents"}],
                              checkpoint=incident, workspace_version=11)["sha256"])

    def test_diagnosis_is_validated_not_trusted_as_repair_authority(self):
        good, incident, today = versions()
        all_prices = [{"key": item["key"], "column": item["column"]} for item in incident_changes(good, incident, today)]
        proposal = validate_diagnosis(good, incident, today, {"cells": all_prices, "summary": "Investigated price changes."})
        self.assertEqual((len(proposal["targets"]), len(proposal["overlaps"])), (117, 3))
        bad = [{"key": integer(1), "column": "payment_status"}]
        for cells in [bad, all_prices + all_prices[:1], [{"key": integer(121), "column": "price_cents"}]]:
            with self.assertRaises(ScopeError):
                validate_diagnosis(good, incident, today, {"cells": cells, "summary": "Untrusted proposal."})

    def test_scope_size_is_bounded_before_execution(self):
        good, today = snapshots()
        for snapshot in (good, today):
            for row in snapshot["rows"]:
                row["key"] = text(row["key"]["value"].zfill(512))
                row["values"]["invoice_id"] = row["key"]
        comparison = compare(good, today, ["price_cents"])
        with self.assertRaises(ScopeError):
            make_plan(good, today, ["price_cents"], [item["token"] for item in comparison["choices"]])

    def test_diagnostic_cannot_hide_overlaps_or_expand_its_repair_mask(self):
        good, incident, today = versions()
        observed = incident_changes(good, incident, today)
        only_one = next(item for item in observed if item["classification"] == "restore")
        proposal = validate_diagnosis(good, incident, today, {"cells": [{"key": only_one["key"], "column": "price_cents"}],
                                                          "summary": "A deliberately partial proposal."})
        self.assertEqual(len(proposal["targets"]), 1)
        self.assertEqual(len(proposal["overlaps"]), 3)


class CsvJsonContractTests(unittest.TestCase):
    def test_literal_csv_values_unique_keys_and_order_are_preserved(self):
        schema = [["csv", "id", "price", "note"]]
        columns = [{"name": name} for name in ["id", "price", "note"]]
        good = {"format": "csv", "table": "records", "primary_key": "id", "schema": schema, "columns": columns,
                "integrity": True, "sequence": [], "file_sha256": "a"*64, "row_count": 2, "rows": []}
        for key, price, note in [("0007", "001.20", "São Paulo, café\nsecond line"), ("0008", "2.00", "=SUM(1,2)")]:
            good["rows"].append({"key": text(key), "values": {"id": text(key), "price": text(price), "note": text(note)}})
        today = copy.deepcopy(good)
        today["file_sha256"] = "b"*64; today["rows"][0]["values"]["price"] = text("120.00")
        plan = make_plan(good, today, targets=[{"token": key_token(text("0007")), "column": "price"}])
        candidate = repaired(good, today, plan)
        self.assertTrue(verify_candidate(good, today, candidate, plan)["ok"])
        self.assertEqual(candidate["rows"][0]["values"]["price"], text("001.20"))
        candidate["rows"].reverse()
        self.assertFalse(verify_candidate(good, today, candidate, plan)["ok"])
        with self.assertRaises(ScopeError):
            make_plan(good, today, targets=[{"token": key_token(text("7")), "column": "price"}])

    def test_csv_request_can_be_small_but_remains_bounded(self):
        self.assertEqual(decode_file(base64.b64encode(b"id,value\n001,2\n").decode(), "csv"), b"id,value\n001,2\n")
        for raw in [b"", b"x" * (256 * 1024 + 1)]:
            with self.assertRaises(InspectionError):
                decode_file(base64.b64encode(raw).decode(), "csv")


def run_record(payload, *, teardown=True):
    return {"ok": True, "payload": payload, "policy_verified": True, "teardown": {"confirmed": teardown}, "runtime": "runsc"}


class FakeProvider:
    """Explicit unit fixture; never replaces the live Vultr provider."""
    def __init__(self):
        self.calls = 0
    def candidate(self):
        self.calls += 1
        return {"code": "def recover(a,b,c,d): pass", "explanation": "Unit fixture only.", "sha256": "f"*64,
                "provider": {"name": "unit-fixture"}, "data_sent": "Unit fixture"}
    def propose(self, *args, **kwargs):
        return self.candidate()
    def diagnose(self, *args, **kwargs):
        return self.candidate()


class PipelineBoundaryTests(unittest.TestCase):
    def bound_versions(self):
        good, incident, today = versions()
        good_raw, incident_raw, today_raw = [b"SQLite format 3\x00" + bytes([number])*600 for number in (1, 2, 3)]
        for snapshot, raw in zip((good, incident, today), (good_raw, incident_raw, today_raw)):
            snapshot["file_sha256"] = sha256_bytes(raw)
        return good, incident, today, good_raw, incident_raw, today_raw

    def test_diagnosis_plus_failed_repair_never_gets_a_third_call(self):
        good, incident, today, good_raw, incident_raw, today_raw = self.bound_versions()
        plan = precise_plan(good, incident, today)
        provider = FakeProvider()
        class FailedSandbox:
            def run(self, *args, **kwargs):
                return {**run_record({}), "ok": False, "error": "Unit fixture failed."}
        evidence, artifact = run_recovery(backup=good, current=today, checkpoint=incident, backup_raw=good_raw,
            current_raw=today_raw, checkpoint_raw=incident_raw, plan=plan, prior_runs=[run_record({})],
            sandbox=FailedSandbox(), provider=provider, model_calls_prior=1, max_attempts=1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(evidence["payload"]["model_calls"], 2)
        self.assertIsNone(artifact)
        with self.assertRaises(InspectionError):
            run_recovery(backup=good, current=today, checkpoint=incident, backup_raw=good_raw, current_raw=today_raw,
                checkpoint_raw=incident_raw, plan=plan, prior_runs=[run_record({})], sandbox=FailedSandbox(),
                provider=provider, model_calls_prior=1, max_attempts=2)

    def test_unconfirmed_input_cleanup_stops_before_any_model_call(self):
        good, incident, today, good_raw, incident_raw, today_raw = self.bound_versions()
        provider = FakeProvider()
        with self.assertRaises(InspectionError):
            run_diagnosis(backup=good, checkpoint=incident, current=today, backup_raw=good_raw,
                checkpoint_raw=incident_raw, current_raw=today_raw, instruction="Prices broke.",
                prior_runs=[run_record({}, teardown=False)], sandbox=None, provider=provider)
        self.assertEqual(provider.calls, 0)

    def test_changed_checkpoint_stops_recovery_before_model_call(self):
        good, incident, today, good_raw, incident_raw, today_raw = self.bound_versions()
        provider = FakeProvider()
        with self.assertRaises(InspectionError):
            run_recovery(backup=good, current=today, checkpoint=incident, backup_raw=good_raw, current_raw=today_raw,
                checkpoint_raw=incident_raw + b"x", plan=precise_plan(good, incident, today),
                prior_runs=[run_record({})], sandbox=None, provider=provider)
        self.assertEqual(provider.calls, 0)

    def test_mutated_execution_mask_cannot_reuse_approved_hash(self):
        good, incident, today, good_raw, incident_raw, today_raw = self.bound_versions()
        provider = FakeProvider()
        plan = precise_plan(good, incident, today)
        plan["scope"]["cells"].append({"key": integer(3), "column": "price_cents"})
        with self.assertRaises(InspectionError):
            run_recovery(backup=good, current=today, checkpoint=incident, backup_raw=good_raw, current_raw=today_raw,
                checkpoint_raw=incident_raw, plan=plan, prior_runs=[run_record({})], sandbox=None, provider=provider)
        self.assertEqual(provider.calls, 0)

    def test_unconfirmed_candidate_removal_denies_artifact_and_retry(self):
        good, incident, today, good_raw, incident_raw, today_raw = self.bound_versions()
        provider = FakeProvider()
        class LeakedSandbox:
            def run(self, *args, **kwargs):
                return run_record({"artifact": base64.b64encode(today_raw).decode()}, teardown=False)
        evidence, artifact = run_recovery(backup=good, current=today, checkpoint=incident, backup_raw=good_raw,
            current_raw=today_raw, checkpoint_raw=incident_raw, plan=precise_plan(good, incident, today),
            prior_runs=[run_record({})], sandbox=LeakedSandbox(), provider=provider)
        self.assertEqual(provider.calls, 1); self.assertIsNone(artifact)
        self.assertFalse(evidence["payload"]["all_sandboxes_removed"])

    def test_inference_receives_metadata_and_counts_without_rows_or_ids(self):
        good, incident, today = versions()
        today["rows"][0]["values"]["payment_status"] = text("PRIVATE-CUSTOMER-CANARY")
        provider = VultrProvider("unit-test-key")
        requests = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({"id": "unit-response", "choices": [{"message": {"content": json.dumps({
                    "code": "def diagnose(a,b,c,d): pass\ndef recover(a,b,c,d): pass",
                    "explanation": "Unit parser fixture."})}}]}).encode()
        def respond(request, **kwargs):
            requests.append(json.loads(request.data)); return Response()
        with patch("rewind.provider.urllib.request.urlopen", side_effect=respond):
            provider.diagnose(diagnostic_metadata(good, incident, today), "Investigate bad prices.")
            provider.propose(today, precise_plan(good, incident, today))
        raw = json.dumps(requests)
        self.assertNotIn("PRIVATE-CUSTOMER-CANARY", raw)
        self.assertNotIn('"rows"', raw)
        self.assertNotIn('"keys"', raw)
        self.assertIn("incident_changed_cells", raw)


class StoreBudgetTests(unittest.TestCase):
    def test_restart_fails_interrupted_diagnosis_without_retry(self):
        with TemporaryDirectory() as temporary:
            store = Store(Path(temporary)); workspace = store.create(None, None, example=True)
            store.update(workspace["id"], status="investigating", model_calls=1)
            restarted = Store(Path(temporary))
            value = restarted.read(workspace["id"], workspace["token"])
            self.assertEqual(value["status"], "failed"); self.assertEqual(value["model_calls"], 1)

    def test_diagnosis_can_only_be_reserved_once_and_budget_stops_at_two(self):
        with TemporaryDirectory() as temporary:
            store = Store(Path(temporary)); workspace = store.create(None, None, example=True)
            store.update(workspace["id"], status="ready", checkpoint={"file_sha256": "a"*64})
            value = store.read(workspace["id"], workspace["token"])
            store.reserve_diagnosis(workspace["id"], workspace["token"], "Prices broke.", value["version"])
            store.consume_model_call(workspace["id"]); store.update(workspace["id"], status="ready")
            with self.assertRaises(AdmissionDenied):
                store.reserve_diagnosis(workspace["id"], workspace["token"], "Prices broke.", store.read(workspace["id"], workspace["token"])["version"])
            store.consume_model_call(workspace["id"])
            with self.assertRaises(AdmissionDenied):
                store.consume_model_call(workspace["id"])


if __name__ == "__main__":
    unittest.main()

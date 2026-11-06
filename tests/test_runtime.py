import copy
import json
import unittest
from rewind.runtime import RUNTIME, merged_config, preserved


class RuntimePreservationTests(unittest.TestCase):
    def setUp(self):
        self.before = {"daemon": {"id": "same-daemon", "version": "same-version", "root": "/var/lib/docker",
            "default_runtime": "runc", "runtimes": {"runc": {"path": "runc"}}},
            "containers": {"same-container": {"id": "same-container", "image": "same-image", "running": True,
            "restart_count": 0, "started_at": "same-start", "mounts": [
                {"Source": "/a", "Destination": "/data", "RW": True},
                {"Source": "/b", "Destination": "/config", "RW": False}]}}}

    def test_mount_order_does_not_change_preservation(self):
        after = copy.deepcopy(self.before)
        after["containers"]["same-container"]["mounts"].reverse()
        after["daemon"]["runtimes"]["runsc"] = {"path": str(RUNTIME)}
        preserved(self.before, after)

    def test_real_mount_state_restart_and_default_changes_are_rejected(self):
        for key, value in [("image", "different-image"), ("running", False), ("restart_count", 1), ("started_at", "new-start")]:
            with self.subTest(key=key):
                after = copy.deepcopy(self.before)
                after["containers"]["same-container"][key] = value
                with self.assertRaises(RuntimeError):
                    preserved(self.before, after)
        after = copy.deepcopy(self.before)
        after["containers"]["same-container"]["mounts"][0]["RW"] = False
        with self.assertRaises(RuntimeError):
            preserved(self.before, after)
        after = copy.deepcopy(self.before)
        after["daemon"]["default_runtime"] = "runsc"
        with self.assertRaises(RuntimeError):
            preserved(self.before, after)

    def test_only_matching_live_runtime_is_persisted(self):
        config = {"log-driver": "json-file", "default-runtime": "runc"}
        merged = merged_config(config, {"runsc": {"path": str(RUNTIME)}})
        self.assertEqual(merged["log-driver"], config["log-driver"])
        self.assertEqual(merged["default-runtime"], "runc")
        self.assertEqual(merged["runtimes"]["runsc"], {"path": str(RUNTIME)})
        self.assertNotIn("runtimes", config)
        with self.assertRaises(RuntimeError):
            merged_config(config, {"runsc": {"path": "/different/runsc"}})

    def test_supported_capability_order_is_ignored_but_feature_changes_are_rejected(self):
        feature = "org.opencontainers.runtime-spec.features"
        self.before["daemon"]["runtimes"]["runc"]["status"] = {feature: json.dumps({"ociVersionMax": "1.3.0", "linux": {"capabilities": ["CAP_KILL", "CAP_CHOWN"]}})}
        after = copy.deepcopy(self.before)
        after["daemon"]["runtimes"]["runc"]["status"][feature] = json.dumps({"linux": {"capabilities": ["CAP_CHOWN", "CAP_KILL"]}, "ociVersionMax": "1.3.0"})
        preserved(self.before, after)
        after["daemon"]["runtimes"]["runc"]["status"][feature] = json.dumps({"linux": {"capabilities": ["CAP_CHOWN"]}, "ociVersionMax": "1.3.0"})
        with self.assertRaises(RuntimeError):
            preserved(self.before, after)


if __name__ == "__main__":
    unittest.main()

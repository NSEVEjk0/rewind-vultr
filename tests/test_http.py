import http.client
import json
import queue
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rewind.server import Controller, Handler
from rewind.store import Store


class VideoRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory()
        web = Path(cls.temporary.name)
        cls.video = bytes(range(256)) * 4
        (web / "recording.mp4").write_bytes(cls.video)
        (web / "empty.mp4").write_bytes(b"")
        (web / "notes.json").write_bytes(b'{"example":true}')
        cls.web_patch = patch("rewind.server.WEB", web)
        cls.web_patch.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.controller = SimpleNamespace(
            sandbox_ready=True, provider=SimpleNamespace(configured=True),
            store=SimpleNamespace(read=lambda *_: None),
        )
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        cls.server.server_close()
        cls.web_patch.stop()
        cls.temporary.cleanup()

    def request(self, range_header=None, path="/recording.mp4"):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            headers = {} if range_header is None else {"Range": range_header}
            client.request("GET", path, headers=headers)
            response = client.getresponse()
            body = response.read()
            headers = dict(response.getheaders())
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            return response.status, headers, body
        finally:
            client.close()

    def test_full_video_advertises_byte_ranges(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertNotIn("Content-Range", headers)
        self.assertEqual(body, self.video)

    def test_inclusive_open_ended_and_clamped_segments(self):
        for value, first, last in [("bytes=10-19", 10, 19),
                                   ("bytes=1000-", 1000, 1023),
                                   ("bytes=1020-9999", 1020, 1023),
                                   ("BYTES=0-0", 0, 0)]:
            with self.subTest(value=value):
                status, headers, body = self.request(value)
                self.assertEqual(status, 206)
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertEqual(headers["Content-Range"], f"bytes {first}-{last}/1024")
                self.assertEqual(body, self.video[first:last + 1])

    def test_suffix_ranges_include_the_final_bytes(self):
        for value, first in [("bytes=-12", 1012), ("bytes=-9999", 0)]:
            with self.subTest(value=value):
                status, headers, body = self.request(value)
                self.assertEqual(status, 206)
                self.assertEqual(headers["Content-Range"], f"bytes {first}-1023/1024")
                self.assertEqual(body, self.video[first:])

    def test_malformed_and_unsatisfiable_ranges_have_empty_416_responses(self):
        values = ["bytes=1024-", "bytes=2000-3000", "bytes=40-20", "bytes=-0",
                  "bytes=", "bytes=-", "bytes=abc-def", "bytes=1-2-3", "bytes=+1-",
                  "bytes=" + "9" * 21 + "-", "bytes=" + "9" * 200 + "-"]
        for value in values:
            with self.subTest(value=value):
                status, headers, body = self.request(value)
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], "bytes */1024")
                self.assertEqual(body, b"")
        status, headers, body = self.request("bytes=0-", "/empty.mp4")
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */0")
        self.assertEqual(body, b"")

    def test_unknown_units_and_multiple_ranges_use_the_full_response(self):
        for value in ["items=0-1", "bytes=0-1,4-5"]:
            with self.subTest(value=value):
                status, headers, body = self.request(value)
                self.assertEqual(status, 200)
                self.assertNotIn("Content-Range", headers)
                self.assertEqual(body, self.video)

    def test_ranges_do_not_change_json_or_private_workspace_responses(self):
        for path, expected_status in [("/notes.json", 200), ("/api/health", 200),
                                      ("/api/workspaces/" + "a" * 32 + "/database", 404)]:
            with self.subTest(path=path):
                original_status, original_headers, original_body = self.request(path=path)
                status, headers, body = self.request("bytes=0-1", path)
                self.assertEqual(original_status, expected_status)
                self.assertEqual(status, expected_status)
                self.assertEqual(body, original_body)
                self.assertNotIn("Content-Range", headers)
                self.assertNotIn("Accept-Ranges", headers)


class PublicDemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        # Use the real HTTP handler and admission store, with no execution worker.
        # These requests exercise access and quotas without making model calls.
        self.controller = Controller.__new__(Controller)
        self.controller.origin = "https://rewind.example"
        self.controller.access_code = "test-only-shared-code"
        self.controller.public_demo = True
        self.controller.sandbox_ready = True
        self.controller.readiness = "The recovery worker is unavailable."
        self.controller.provider = SimpleNamespace(configured=True, model="test-model")
        self.controller.store = Store(Path(self.temporary.name))
        self.controller.queue = queue.Queue(maxsize=4)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.controller = self.controller
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if method == "POST":
            headers.setdefault("Origin", self.controller.origin)
            headers["Content-Type"] = "application/json"
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            client.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def ready_workspace(self):
        status, admission = self.request("POST", "/api/example", {})
        self.assertEqual(status, 202)
        self.controller.queue.get_nowait()
        self.controller.queue.task_done()
        self.controller.store.update(admission["id"], status="ready", plan={"sha256": "f" * 64})
        return admission

    def test_public_config_and_example_need_no_shared_code(self):
        self.controller.access_code = ""
        status, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(config["access_required"])
        self.assertTrue(config["available"])
        self.assertTrue(config["inspection_available"])
        self.assertIsNone(config["daily_limit"])
        status, admission = self.request("POST", "/api/example", {})
        self.assertEqual(status, 202)
        self.assertEqual(self.controller.queue.get_nowait(), (admission["id"], "inspect"))

    def test_private_mode_still_requires_the_shared_code(self):
        self.controller.public_demo = False
        status, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(config["access_required"])
        for headers in [{}, {"X-Rewind-Access": "wrong"}]:
            status, _ = self.request("POST", "/api/example", {}, headers)
            self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/example", {}, {"X-Rewind-Access": self.controller.access_code})
        self.assertEqual(status, 202)

    def test_public_workspaces_and_downloads_still_require_their_capability(self):
        status, admission = self.request("POST", "/api/example", {})
        self.assertEqual(status, 202)
        path = "/api/workspaces/" + admission["id"]
        for suffix in ["", "/evidence", "/database", "/backup", "/current"]:
            for headers in [{}, {"Authorization": "Bearer another-browser-token"}]:
                status, _ = self.request("GET", path + suffix, headers=headers)
                self.assertEqual(status, 404)
        status, value = self.request("GET", path, headers={"Authorization": "Bearer " + admission["token"]})
        self.assertEqual(status, 200)
        self.assertEqual(value["id"], admission["id"])
        self.assertNotIn("token_sha256", value)

    def test_public_requests_still_reject_foreign_origins(self):
        status, _ = self.request("POST", "/api/example", {}, {"Origin": "https://another.example"})
        self.assertEqual(status, 403)
        self.assertTrue(self.controller.queue.empty())
        self.assertEqual(json.loads(self.controller.store.admission_path.read_text())["uploads"], 0)

    def test_public_inspections_can_repeat_without_daily_or_workspace_quotas(self):
        for _ in range(40):
            self.ready_workspace()
        status, _ = self.request("POST", "/api/example", {})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(self.controller.store.admission_path.read_text())["uploads"], 41)

    def test_public_recovery_requires_owner_and_approval_without_a_daily_quota(self):
        admission = self.ready_workspace()
        path = "/api/workspaces/" + admission["id"] + "/recover"
        headers = {"Authorization": "Bearer " + admission["token"]}
        status, _ = self.request("POST", path, {"plan_sha256": "f" * 64})
        self.assertEqual(status, 404)
        status, _ = self.request("POST", path, {"plan_sha256": "changed"}, headers)
        self.assertEqual(status, 429)
        status, _ = self.request("POST", path, {"plan_sha256": "f" * 64}, headers)
        self.assertEqual(status, 202)
        status, _ = self.request("POST", path, {"plan_sha256": "f" * 64}, headers)
        self.assertEqual(status, 429)
        self.assertEqual(self.controller.queue.get_nowait(), (admission["id"], "recover"))
        self.controller.queue.task_done()
        self.controller.store.update(admission["id"], status="failed")
        for _ in range(8):
            another = self.ready_workspace()
            status, _ = self.request("POST", "/api/workspaces/" + another["id"] + "/recover",
                                     {"plan_sha256": "f" * 64}, {"Authorization": "Bearer " + another["token"]})
            self.assertEqual(status, 202)
            self.assertEqual(self.controller.queue.get_nowait(), (another["id"], "recover"))
            self.controller.queue.task_done()
            self.controller.store.update(another["id"], status="failed")
        self.assertEqual(json.loads(self.controller.store.admission_path.read_text())["recoveries"], 9)

    def test_public_mode_still_requires_ready_isolation_and_configured_inference(self):
        self.controller.sandbox_ready = False
        status, _ = self.request("POST", "/api/example", {})
        self.assertEqual(status, 503)
        self.assertTrue(self.controller.queue.empty())
        self.controller.sandbox_ready = True
        self.controller.provider.configured = False
        admission = self.ready_workspace()
        status, _ = self.request("POST", "/api/workspaces/" + admission["id"] + "/recover",
                                 {"plan_sha256": "f" * 64}, {"Authorization": "Bearer " + admission["token"]})
        self.assertEqual(status, 503)
        self.assertTrue(self.controller.queue.empty())
        self.assertEqual(json.loads(self.controller.store.admission_path.read_text())["recoveries"], 0)


if __name__ == "__main__":
    unittest.main()

import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rewind.server import Handler


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


if __name__ == "__main__":
    unittest.main()

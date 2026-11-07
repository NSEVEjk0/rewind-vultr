import unittest
from rewind.deploy import environment, hostname, public_origin, proxy_plan


class DeploymentPlanTests(unittest.TestCase):
    def test_gateway_upgrade_keeps_other_routes_and_redirects_legacy_links(self):
        host = "rewind.backend.example"
        original = b"# Existing service\nother.example { reverse_proxy 127.0.0.1:8080 }\n"
        direct = proxy_plan(original, host)
        staged = proxy_plan(direct, host, "https://rewind-demo.vercel.app", redirect_legacy=False)
        final = proxy_plan(staged, host, "https://rewind-demo.vercel.app")
        self.assertTrue(final.startswith(original))
        self.assertIn(b"handle_path /__web/*", final)
        self.assertIn(b"redir https://rewind-demo.vercel.app{uri} 307", final)
        self.assertEqual(proxy_plan(final, host, "https://rewind-demo.vercel.app"), final)

    def test_public_origin_rejects_credentials_paths_and_injected_routes(self):
        self.assertEqual(public_origin("https://rewind-demo.vercel.app/"), "https://rewind-demo.vercel.app")
        for value in ["http://rewind.example", "https://user:pass@rewind.example", "https://rewind.example/path",
                      "https://rewind.example?redirect=other", "https://rewind.example#fragment", "https://evil\n.example"]:
            with self.subTest(value=value), self.assertRaises((RuntimeError, ValueError)):
                public_origin(value)

    def test_proxy_addition_preserves_every_existing_byte_and_is_idempotent(self):
        original = b"# Existing app\n139.84.249.47.sslip.io {\n reverse_proxy 127.0.0.1:8080\n}\n"
        result = proxy_plan(original,"rewind.139.84.249.47.sslip.io")
        self.assertTrue(result.startswith(original))
        self.assertIn(b"reverse_proxy 127.0.0.1:18182", result)
        self.assertEqual(proxy_plan(result,"rewind.139.84.249.47.sslip.io"),result)
        with self.assertRaises(RuntimeError):
            proxy_plan(result,"other-rewind.139.84.249.47.sslip.io")

    def test_conflicting_hostnames_and_route_injection_are_rejected(self):
        with self.assertRaises(RuntimeError):
            proxy_plan(b"rewind.example.org { reverse_proxy other:9999 }", "rewind.example.org")
        for value in ("evil\n{", "https://rewind.example.org", "rewind.example.org:443", "../rewind", "-bad.example.org"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                hostname(value)

    def test_private_env_is_parsed_as_data_and_never_executed(self):
        parsed=environment('# private settings\nexport VULTR_INFERENCE_API_KEY="literal-$(touch /tmp/never-run)"\nVULTR_INFERENCE_MODEL=glm-5.3-flash\nEMPTY=\n')
        self.assertEqual(parsed['VULTR_INFERENCE_API_KEY'],'literal-$(touch /tmp/never-run)')
        self.assertEqual(parsed['EMPTY'],'')
        with self.assertRaises(RuntimeError):
            environment('BAD_SETTING=two unquoted words')


if __name__=="__main__":
    unittest.main()

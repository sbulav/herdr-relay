import asyncio
import pathlib
import re
import unittest
from unittest.mock import patch

from relay import herdr_relay


REPO = pathlib.Path(__file__).resolve().parent.parent


class _Served(Exception):
    """Raised by the fake `serve` so main() stops right after the call under test."""


class KeepaliveTests(unittest.TestCase):
    def test_serve_uses_relay_keepalive(self):
        # The library defaults (20 s interval, 20 s timeout) drop a phone that
        # pauses briefly; #68 pins a 90 s timeout on the real call site.
        seen = {}

        async def fake_serve(*args, **kwargs):
            seen["handler"] = args[0]
            seen.update(kwargs)
            raise _Served

        server = herdr_relay.server
        with (
            patch.object(server, "require_auth_token", lambda: None),
            patch.object(server.projects, "public_snapshot", lambda: None),
            patch.object(server.catalogs, "refresh_all", lambda: None),
            patch.object(server.lifecycle, "recover_start_operations", lambda: None),
            patch.object(server, "serve", fake_serve),
        ):
            with self.assertRaises(_Served):
                asyncio.run(server.main())

        self.assertEqual(20, seen["ping_interval"])
        self.assertEqual(90, seen["ping_timeout"])
        self.assertIs(server.handle_client, seen["handler"])
        self.assertIs(server.process_request, seen["process_request"])


class HerdrFloorTests(unittest.TestCase):
    def test_manifests_require_herdr_0_9(self):
        # Polling reads one `herdr api snapshot` per host (#84); 0.8 has no such command.
        for manifest in ("herdr-plugin.toml", "relay/herdr-plugin.toml"):
            with self.subTest(manifest=manifest):
                lines = (REPO / manifest).read_text().splitlines()
                self.assertIn('min_herdr_version = "0.9.0"', lines)

    def test_readme_names_the_floor(self):
        self.assertIn("- herdr 0.9+", (REPO / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()


class WebPageScriptOriginTests(unittest.TestCase):
    """The browser client's page holds the relay token, so it runs no script from another origin."""

    PAGE = REPO / "web" / "index.html"

    @staticmethod
    def _directives(policy):
        return {
            part.split()[0]: part.split()[1:]
            for part in (piece.strip() for piece in policy.split(";")) if part
        }

    def _meta_policy(self):
        match = re.search(
            r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">',
            self.PAGE.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(match, "index.html carries no CSP <meta>")
        return match.group(1)

    def test_page_names_no_script_on_another_origin(self):
        page = self.PAGE.read_text(encoding="utf-8")
        self.assertNotRegex(page, r"<script[^>]*\bsrc\s*=\s*[\"']?(?:https?:)?//")
        self.assertNotRegex(page, r"\bimport\b[^;\n]*[\"'](?:https?:)?//")
        self.assertNotRegex(page, r"\bimport\s*\(\s*[\"'](?:https?:)?//")

    def test_meta_policy_matches_the_header_except_frame_ancestors(self):
        header = self._directives(herdr_relay.server.WEB_APP_CSP)
        self.assertEqual(header.pop("frame-ancestors"), ["'none'"])
        self.assertEqual(self._directives(self._meta_policy()), header)

    def test_pages_headers_carry_the_relay_policy(self):
        # The Cloudflare Pages copy of web/ gets the policy as a header too: a
        # <meta> cannot cover a script injected ahead of it.
        lines = (REPO / "web" / "_headers").read_text(encoding="utf-8").splitlines()
        rules = [line for line in lines if line and not line.startswith("#")]
        self.assertEqual(rules[0], "/*")
        name, _, value = rules[1].strip().partition(": ")
        self.assertEqual(name, "Content-Security-Policy")
        self.assertEqual(value, herdr_relay.server.WEB_APP_CSP)

    def test_policy_allows_scripts_from_this_origin_and_inline_only(self):
        self.assertEqual(
            self._directives(herdr_relay.server.WEB_APP_CSP)["script-src"],
            ["'self'", "'unsafe-inline'"],
        )

    def test_relay_serves_the_page_with_the_policy(self):
        class Headers:
            def raw_items(self):
                return [("Authorization", "Bearer token")]

        class Request:
            headers = Headers()
            path = "/"

        with patch.object(herdr_relay.config, "AUTH_TOKEN", "token"):
            response = asyncio.run(herdr_relay.process_request(None, Request()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Content-Security-Policy"], herdr_relay.server.WEB_APP_CSP,
        )

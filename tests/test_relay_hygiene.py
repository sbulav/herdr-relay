import asyncio
import pathlib
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
    def test_manifests_require_herdr_0_8(self):
        # Launches use herdr 0.8's tab-then-attach contract (#49); 0.7 cannot run them.
        for manifest in ("herdr-plugin.toml", "relay/herdr-plugin.toml"):
            with self.subTest(manifest=manifest):
                lines = (REPO / manifest).read_text().splitlines()
                self.assertIn('min_herdr_version = "0.8.0"', lines)

    def test_readme_names_the_floor(self):
        self.assertIn("- herdr 0.8+", (REPO / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay import herdr_relay


class HostConfigurationTests(unittest.TestCase):
    def valid_document(self):
        return {
            "schema_version": 1,
            "hosts": [
                {
                    "id": "workstation",
                    "display_name": "Workstation",
                    "ssh": {"target": "deploy@workstation"},
                    "project_roots": ["/srv/projects"],
                    "herdr": {"wrapper": ["nix", "run", "--"], "binary": "/opt/herdr"},
                    "harnesses": [{"id": "claude", "display_name": "Claude Code"}],
                    "power": {"wake": {"mac": "00:11:22:33:44:55"}, "shutdown": True},
                    "readiness_timeout_seconds": 20,
                }
            ],
        }

    def load(self, document):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return herdr_relay.hosts.load_hosts(str(path))

    def test_versioned_config_loads_private_routing_and_capabilities(self):
        host = self.load(self.valid_document())[0]

        self.assertEqual(host["id"], "workstation")
        self.assertEqual(herdr_relay.hosts.ssh_target(host), "deploy@workstation")
        self.assertEqual(herdr_relay.hosts.herdr_command(host), ["nix", "run", "--", "/opt/herdr"])
        self.assertEqual(herdr_relay.hosts.power_capabilities(host), {"wake": True, "shutdown": True})

    def test_invalid_timeout_and_project_root_are_rejected(self):
        invalid = self.valid_document()
        invalid["hosts"][0]["readiness_timeout_seconds"] = 0
        with self.assertRaises(ValueError):
            self.load(invalid)

        invalid = self.valid_document()
        invalid["hosts"][0]["project_roots"] = ["relative/path"]
        with self.assertRaises(ValueError):
            self.load(invalid)

    def test_shutdown_requires_an_ssh_target(self):
        invalid = self.valid_document()
        invalid["hosts"][0]["ssh"] = {}
        with self.assertRaises(ValueError):
            self.load(invalid)

        invalid = self.valid_document()
        invalid["hosts"][0].pop("ssh")
        with self.assertRaises(ValueError):
            self.load(invalid)

    def test_public_host_has_state_but_no_private_configuration(self):
        host = self.load(self.valid_document())[0]
        public = herdr_relay.hosts.public_host(
            host,
            {"ssh_reachable": True, "herdr_ready": True, "active_agent_count": 3},
        )

        self.assertEqual(public["status"], "ready")
        self.assertEqual(public["active_agent_count"], 3)
        self.assertEqual(public["capabilities"], {"wake": True, "shutdown": True})
        encoded = json.dumps(public)
        self.assertNotIn("deploy@workstation", encoded)
        self.assertNotIn("00:11:22:33:44:55", encoded)
        self.assertNotIn("/srv/projects", encoded)
        self.assertNotIn("/opt/herdr", encoded)

    def test_public_readiness_distinguishes_ssh_and_herdr_failures(self):
        host = self.load(self.valid_document())[0]

        offline = herdr_relay.hosts.public_host(host, {"ssh_reachable": False, "herdr_ready": False})
        unavailable = herdr_relay.hosts.public_host(host, {"ssh_reachable": True, "herdr_ready": False})
        ready = herdr_relay.hosts.public_host(
            host,
            {"ssh_reachable": True, "herdr_ready": True, "active_agent_count": 0},
        )

        self.assertEqual(offline["status"], "offline")
        self.assertEqual(unavailable["status"], "herdr_unavailable")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["active_agent_count"], 0)

    def test_poll_probes_ssh_before_herdr(self):
        host = self.load(self.valid_document())[0]
        with (
            patch.object(herdr_relay.herdr, "run_ssh_checked", return_value=False) as ssh,
            patch.object(herdr_relay.herdr, "run_herdr_checked") as herdr,
        ):
            agents, probe = herdr_relay.herdr.get_agents_from_host(host=host)

        self.assertEqual(agents, [])
        self.assertEqual(probe["ssh_reachable"], False)
        ssh.assert_called_once()
        herdr.assert_not_called()


if __name__ == "__main__":
    unittest.main()

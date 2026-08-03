from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
from typing import Optional
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import adaptive_proxy


CORE = adaptive_proxy.LIBCYBER_CORE_EXECUTABLES[0]
HELPER = adaptive_proxy.LIBCYBER_HELPER_EXECUTABLE


class LibCyberDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = Path(self.temporary_directory.name)
        self.config_path = adaptive_proxy.libcyber_config_path(self.home)
        self.config_path.parent.mkdir(parents=True)
        self.environment = patch.dict(
            os.environ,
            {
                "YDR_ADAPTIVE_TEST_CLASH_PORTS": "",
                "YDR_ADAPTIVE_TEST_SYSTEM_PROXY_PORTS": "",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_config(self, text: str) -> None:
        self.config_path.write_text(text, encoding="utf-8")

    def process_fixtures(
        self,
        port: int,
        *,
        listener_pid: int = 4242,
        core_executable: str = CORE,
        core_config: Optional[Path] = None,
        listener_address: Optional[str] = None,
    ):
        helper_pid = 4241
        active_config = self.config_path if core_config is None else core_config

        def fake_run_text(command, timeout=5.0):
            if command[0] == "/fake/lsof":
                address = listener_address or f"127.0.0.1:{port}"
                return 0, f"p{listener_pid}\nn{address}\n"
            if command[0] == "/fake/ps":
                pid = int(command[2])
                if pid == listener_pid:
                    return (
                        0,
                        f" {helper_pid} {core_executable} -d /safe/libs -f {active_config}\n",
                    )
                if pid == helper_pid:
                    return 0, f" 1 {HELPER}\n"
            return 1, ""

        executable_by_pid = {
            listener_pid: core_executable,
            helper_pid: HELPER,
        }
        return fake_run_text, lambda pid: executable_by_pid.get(pid, "")

    def discover_with_fixtures(self, port: int, **kwargs):
        fake_run_text, fake_process_executable = self.process_fixtures(
            port, **kwargs
        )
        with patch.object(adaptive_proxy, "run_text", side_effect=fake_run_text), patch.object(
            adaptive_proxy,
            "process_executable",
            side_effect=fake_process_executable,
        ):
            return adaptive_proxy.discover_candidates(
                self.home, "/fake/scutil", "/fake/lsof", "/fake/ps"
            )

    def test_trusted_libcyber_8890_is_selected_after_two_healthy_cycles(self) -> None:
        self.write_config("port: 8890\nsocks-port: 8891\nmode: rule\n")
        candidates = self.discover_with_fixtures(8890)
        self.assertEqual(candidates, ["DIRECT", "LOOPBACK:8890"])

        health = {"DIRECT": False, "LOOPBACK:8890": True}
        first = adaptive_proxy.evaluate(candidates, health, "DIRECT", {})
        second = adaptive_proxy.evaluate(
            candidates,
            health,
            "DIRECT",
            adaptive_proxy.evaluation_state_updates(first),
        )
        self.assertEqual(first["decision"], "pending")
        self.assertEqual(second["decision"], "switch_ready")
        self.assertEqual(second["candidate_route"], "LOOPBACK:8890")

    def test_dynamic_libcyber_http_port_is_discovered(self) -> None:
        self.write_config("port: 17777\nsocks-port: 17778\n")
        self.assertEqual(
            self.discover_with_fixtures(17777),
            ["DIRECT", "LOOPBACK:17777"],
        )

    def test_stale_listener_bound_to_another_config_is_rejected(self) -> None:
        self.write_config("port: 8890\nsocks-port: 8891\n")
        self.assertEqual(
            self.discover_with_fixtures(
                8890, core_config=Path("/tmp/stale-libcyber-config.yaml")
            ),
            ["DIRECT"],
        )

    def test_spoofed_listener_owner_is_rejected(self) -> None:
        self.write_config("port: 8890\nsocks-port: 8891\n")
        self.assertEqual(
            self.discover_with_fixtures(
                8890, core_executable="/tmp/core-darwin-amd64"
            ),
            ["DIRECT"],
        )

    def test_non_loopback_listener_is_rejected(self) -> None:
        self.write_config("port: 8890\nsocks-port: 8891\n")
        self.assertEqual(
            self.discover_with_fixtures(8890, listener_address="*:8890"),
            ["DIRECT"],
        )

    def test_malformed_or_ambiguous_config_rejects_all_libcyber_ports(self) -> None:
        invalid_configs = (
            "port: |\n  8890\nsocks-port: 8891\n",
            "port: 8890\nport: 8892\nsocks-port: 8891\n",
            "port: 65536\nsocks-port: 8891\n",
            "port: $(id)\nsocks-port: 8891\n",
            "port: 8890\nsocks-port: [8891, 8892]\n",
            "port 8890\nsocks-port: 8891\n",
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                self.write_config(config)
                self.assertEqual(
                    adaptive_proxy.configured_libcyber_ports(self.home), {}
                )

    def test_socks_only_config_is_not_mislabeled_as_http(self) -> None:
        self.write_config("socks-port: 8891\n")
        self.assertEqual(
            adaptive_proxy.configured_libcyber_ports(self.home),
            {"socks-port": 8891},
        )
        self.assertEqual(
            adaptive_proxy.discover_candidates(
                self.home, "/fake/scutil", "/fake/lsof", "/fake/ps"
            ),
            ["DIRECT"],
        )


class ExistingRouteRegressionTests(unittest.TestCase):
    def test_clash_discovery_remains_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            os.environ,
            {
                "YDR_ADAPTIVE_TEST_CLASH_PORTS": "7897",
                "YDR_ADAPTIVE_TEST_SYSTEM_PROXY_PORTS": "",
                "YDR_ADAPTIVE_TEST_LISTENERS": "7897=mihomo",
            },
        ):
            candidates = adaptive_proxy.discover_candidates(
                Path(temporary_directory), "/fake/scutil", "/fake/lsof", "/fake/ps"
            )
        self.assertEqual(candidates, ["DIRECT", "LOOPBACK:7897"])

    def test_direct_tun_path_remains_free_of_explicit_proxy_variables(self) -> None:
        dirty_environment = {
            "PATH": "/usr/bin",
            "http_proxy": "http://127.0.0.1:9999",
            "HTTPS_PROXY": "http://127.0.0.1:9999",
            "ALL_PROXY": "socks5://127.0.0.1:9998",
        }
        clean = adaptive_proxy.command_env("DIRECT", dirty_environment)
        self.assertEqual(clean, {"PATH": "/usr/bin"})
        result = adaptive_proxy.evaluate(
            ["DIRECT"], {"DIRECT": True}, "DIRECT", {}
        )
        self.assertEqual(result["decision"], "stable")
        self.assertEqual(result["current_ok"], "1")

    def test_each_control_plane_endpoint_requires_two_successes(self) -> None:
        with patch.object(
            adaptive_proxy.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ) as run_mock:
            healthy = adaptive_proxy.probe_route(
                "DIRECT",
                "/fake/curl",
                successes=2,
                connect_timeout=1,
                max_time=2,
            )
        self.assertTrue(healthy)
        self.assertEqual(run_mock.call_count, 4)
        commands = [call.args[0] for call in run_mock.call_args_list]
        for url in adaptive_proxy.PROBE_URLS:
            self.assertEqual(sum(command[-1] == url for command in commands), 2)


if __name__ == "__main__":
    unittest.main()

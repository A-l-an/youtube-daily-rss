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


def netstat_listener_line(
    local_address: str,
    pid: int = 4242,
    display_name: str = "untrusted-display-name",
) -> str:
    return (
        f"tcp4 0 0 {local_address} *.* LISTEN 0 0 131072 131072 "
        f"{display_name}:{pid} 00180 00000006 000000000bace862 "
        "00000000 00000800 1 0 000000\n"
    )


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
        lsof_denied: bool = False,
        netstat_output: Optional[str] = None,
    ):
        helper_pid = 4241
        active_config = self.config_path if core_config is None else core_config

        def fake_run_text(command, timeout=5.0):
            if command[0] == "/fake/lsof":
                if lsof_denied:
                    return 1, ""
                address = listener_address or f"127.0.0.1:{port}"
                return 0, f"p{listener_pid}\nn{address}\n"
            if command[0] == "/fake/netstat":
                output = netstat_output
                if output is None:
                    output = netstat_listener_line(
                        f"127.0.0.1.{port}", listener_pid
                    )
                return 0, output
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
                self.home,
                "/fake/scutil",
                "/fake/lsof",
                "/fake/ps",
                "/fake/netstat",
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

    def test_root_owned_lsof_denied_uses_netstat_pid_not_display_name(self) -> None:
        self.write_config("port: 8890\nsocks-port: 8891\n")
        self.assertEqual(
            self.discover_with_fixtures(
                8890,
                lsof_denied=True,
                netstat_output=netstat_listener_line(
                    "127.0.0.1.8890", display_name="spoofable-name-is-ignored"
                ),
            ),
            ["DIRECT", "LOOPBACK:8890"],
        )

    def test_netstat_same_pid_duplicates_are_unique_but_two_pids_are_rejected(self) -> None:
        one_pid = netstat_listener_line("127.0.0.1.8890", 4242) * 2
        self.assertEqual(
            adaptive_proxy.parse_netstat_loopback_listener_pid(8890, one_pid),
            4242,
        )
        two_pids = one_pid + netstat_listener_line("127.0.0.1.8890", 4343)
        self.assertIsNone(
            adaptive_proxy.parse_netstat_loopback_listener_pid(8890, two_pids)
        )

    def test_netstat_non_loopback_target_port_is_rejected(self) -> None:
        self.assertIsNone(
            adaptive_proxy.parse_netstat_loopback_listener_pid(
                8890,
                netstat_listener_line("127.0.0.1.8890")
                + netstat_listener_line("*.8890"),
            )
        )

    def test_netstat_malformed_target_record_is_rejected(self) -> None:
        malicious_records = (
            (
                "tcp4 0 0 127.0.0.1.8890 *.* LISTEN 0 0 131072 131072 "
                "forged:4242 00180 not-hex 000000000bace862 00000000 "
                "00000800 1 0 000000\n"
            ),
            "tcp4 0 127.0.0.1.8890 *.* LISTEN\n",
        )
        for record in malicious_records:
            with self.subTest(record=record):
                self.assertIsNone(
                    adaptive_proxy.parse_netstat_loopback_listener_pid(
                        8890, record
                    )
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

    def test_core_lineage_drift_after_listener_reread_is_rejected(self) -> None:
        core_args = f"{CORE} -d /safe/libs -f {self.config_path}"
        core_initial = (4241, core_args)
        helper_initial = (1, HELPER)
        drift_cases = {
            "core_args": (4241, core_args + " --changed"),
            "core_ppid": (4341, core_args),
        }
        for name, core_final in drift_cases.items():
            with self.subTest(name=name), patch.object(
                adaptive_proxy,
                "loopback_listener_pid",
                side_effect=[4242, 4242],
            ), patch.object(
                adaptive_proxy,
                "process_executable",
                side_effect=[CORE, HELPER, CORE],
            ), patch.object(
                adaptive_proxy,
                "process_parent_and_args",
                side_effect=[
                    core_initial,
                    helper_initial,
                    core_final,
                ],
            ):
                self.assertFalse(
                    adaptive_proxy.listener_is_trusted_libcyber(
                        8890, self.config_path, "/fake/lsof", "/fake/ps"
                    )
                )

    def test_helper_lineage_drift_after_listener_reread_is_rejected(self) -> None:
        core_args = f"{CORE} -d /safe/libs -f {self.config_path}"
        core_initial = (4241, core_args)
        helper_initial = (1, HELPER)
        drift_cases = {
            "helper_args": (1, HELPER + " --changed"),
            "grandparent_pid": (99, HELPER),
        }
        for name, helper_final in drift_cases.items():
            with self.subTest(name=name), patch.object(
                adaptive_proxy,
                "loopback_listener_pid",
                side_effect=[4242, 4242],
            ), patch.object(
                adaptive_proxy,
                "process_executable",
                side_effect=[CORE, HELPER, CORE, HELPER],
            ), patch.object(
                adaptive_proxy,
                "process_parent_and_args",
                side_effect=[
                    core_initial,
                    helper_initial,
                    core_initial,
                    helper_final,
                ],
            ):
                self.assertFalse(
                    adaptive_proxy.listener_is_trusted_libcyber(
                        8890, self.config_path, "/fake/lsof", "/fake/ps"
                    )
                )

    def test_listener_pid_drift_between_lineage_snapshots_is_rejected(self) -> None:
        core_args = f"{CORE} -d /safe/libs -f {self.config_path}"
        with patch.object(
            adaptive_proxy,
            "loopback_listener_pid",
            side_effect=[4242, 4343],
        ), patch.object(
            adaptive_proxy,
            "process_executable",
            side_effect=[CORE, HELPER],
        ), patch.object(
            adaptive_proxy,
            "process_parent_and_args",
            side_effect=[(4241, core_args), (1, HELPER)],
        ):
            self.assertFalse(
                adaptive_proxy.listener_is_trusted_libcyber(
                    8890, self.config_path, "/fake/lsof", "/fake/ps"
                )
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

    def test_config_symlink_is_rejected_without_following_it(self) -> None:
        target = self.home / "attacker.yaml"
        target.write_text("port: 8890\nsocks-port: 8891\n", encoding="utf-8")
        self.config_path.symlink_to(target)
        self.assertEqual(adaptive_proxy.configured_libcyber_ports(self.home), {})

    def test_config_path_swap_during_fd_read_is_rejected(self) -> None:
        self.write_config("port: 8890\nsocks-port: 8891\n")
        replacement = self.home / "replacement.yaml"
        replacement.write_text("port: 17777\nsocks-port: 17778\n", encoding="utf-8")
        real_read = os.read
        swapped = False

        def swapping_read(fd, count):
            nonlocal swapped
            data = real_read(fd, count)
            if not swapped:
                os.replace(replacement, self.config_path)
                swapped = True
            return data

        with patch.object(adaptive_proxy.os, "read", side_effect=swapping_read):
            self.assertEqual(adaptive_proxy.configured_libcyber_ports(self.home), {})

    def test_group_or_world_writable_config_is_rejected(self) -> None:
        self.write_config("port: 8890\nsocks-port: 8891\n")
        self.config_path.chmod(0o666)
        self.assertEqual(adaptive_proxy.configured_libcyber_ports(self.home), {})

    def test_socks_only_config_is_not_mislabeled_as_http(self) -> None:
        self.write_config("socks-port: 8891\n")
        self.assertEqual(
            adaptive_proxy.configured_libcyber_ports(self.home),
            {"socks-port": 8891},
        )
        self.assertEqual(
            adaptive_proxy.discover_candidates(
                self.home,
                "/fake/scutil",
                "/fake/lsof",
                "/fake/ps",
                "/fake/netstat",
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
                Path(temporary_directory),
                "/fake/scutil",
                "/fake/lsof",
                "/fake/ps",
                "/fake/netstat",
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

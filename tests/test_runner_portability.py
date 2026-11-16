from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class RunnerPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.intel = self.tmp / "usr/local"
        self.arm = self.tmp / "opt/homebrew"
        self.system_python = self.tmp / "system-python3"
        self.github_path = self.tmp / "github-path"
        self.github_path.touch()
        workflow = yaml.load((ROOT / ".github/workflows/daily.yml").read_text(), Loader=yaml.BaseLoader)
        self.steps = {step["name"]: step for step in workflow["jobs"]["build"]["steps"]}

    def executable(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)

    def run_step(self, name: str) -> subprocess.CompletedProcess[str]:
        script = self.steps[name]["run"]
        if name == "Install Python dependencies":
            # Exercise interpreter selection without installing or fetching dependencies.
            script = script.split("rm -rf .venv-ci", 1)[0]
            script += '\nprintf "SELECTED=%s\\n" "$PYTHON_BIN"\n'
        script = script.replace("/usr/local", str(self.intel)).replace("/opt/homebrew", str(self.arm))
        script = script.replace("/usr/bin/python3", str(self.system_python))
        return subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail"],
            input=script, text=True, capture_output=True, cwd=self.tmp,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp),
                 "GITHUB_PATH": str(self.github_path), "GITHUB_WORKSPACE": str(self.tmp)},
            check=False,
        )

    def provision_tools(self, prefix: Path, missing: str = "") -> None:
        self.executable(prefix / "bin/brew", 'test "$1" = "--prefix" || exit 90\n'
                        + "printf '%s\\n' " + shlex.quote(str(prefix)))
        for tool in ("ffmpeg", "gtar", "deno"):
            if tool != missing:
                self.executable(prefix / "bin" / tool, f"echo '{tool} fixture version'")

    def provision_python(self, path: Path, version: tuple[int, int] = (3, 13)) -> None:
        version_check = f"import sys; sys.version_info = {version!r}; exec(sys.argv[1])"
        self.executable(path,
                        'case "$1" in\n'
                        f'  -c) exec {shlex.quote(sys.executable)} -c {shlex.quote(version_check)} "$2" ;;\n'
                        '  --version) echo "Python fixture" ;;\n'
                        '  *) exit 91 ;;\n'
                        'esac')

    def test_preflight_finds_tools_and_exports_paths_on_both_architectures(self) -> None:
        for prefix in (self.intel, self.arm):
            with self.subTest(prefix=prefix.name):
                self.provision_tools(prefix)
                result = self.run_step("Preflight local runner")
                for tool in ("brew", "ffmpeg", "gtar", "deno"):
                    (prefix / "bin" / tool).unlink()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for tool in ("ffmpeg", "gtar", "deno"):
                    self.assertIn(f"{tool} fixture version", result.stdout)
                paths = self.github_path.read_text().splitlines()
                for path in (self.intel / "bin", self.arm / "bin", self.tmp / ".deno/bin"):
                    self.assertIn(str(path), paths)

    def test_preflight_fails_when_a_required_tool_is_missing(self) -> None:
        for missing in ("ffmpeg", "gtar", "deno"):
            with self.subTest(missing=missing):
                self.provision_tools(self.intel, missing)
                result = self.run_step("Preflight local runner")
                for path in (self.intel / "bin").iterdir():
                    path.unlink()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stdout + result.stderr)

    def test_selects_installed_homebrew_python_on_both_architectures(self) -> None:
        for prefix in (self.intel, self.arm):
            with self.subTest(prefix=prefix.name):
                candidate = prefix / "bin/python3.13"
                self.provision_python(candidate)
                result = self.run_step("Install Python dependencies")
                candidate.unlink()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"SELECTED={candidate}\n", result.stdout)
                self.assertIn("Python fixture", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_rejects_old_interpreter_and_uses_supported_one_in_other_prefix(self) -> None:
        self.provision_python(self.intel / "bin/python3.13", version=(3, 10))
        candidate = self.arm / "bin/python3.11"
        self.provision_python(candidate, version=(3, 11))
        result = self.run_step("Install Python dependencies")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"SELECTED={candidate}\n", result.stdout)

    def test_fails_without_supported_homebrew_python_instead_of_using_system_python(self) -> None:
        self.provision_python(self.intel / "bin/python3.13", version=(3, 10))
        self.provision_python(self.system_python, version=(3, 9))
        result = self.run_step("Install Python dependencies")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("3.11", result.stdout + result.stderr)
        self.assertNotIn("SELECTED=", result.stdout)


if __name__ == "__main__":
    unittest.main()

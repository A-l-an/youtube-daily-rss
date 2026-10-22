from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import asr


class XiemacASRTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = self.enterContext(ExitStack())
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.cli = self.tmp / "xiemac-transcribe"
        self.reply = self.tmp / "reply.json"
        self.calls = self.tmp / "calls.jsonl"
        self.audio = self.tmp / "audio with spaces;echo nope.mp3"
        self.audio.write_bytes(b"synthetic audio")
        self.chunks = [self.audio, self.tmp / "chunk-002.mp3"]
        self.chunks[1].write_bytes(b"synthetic second chunk")
        self.config = {
            "enabled": True, "provider": "xiemac", "model": "large-v3-turbo",
            "language_candidates": ["zh"], "chunk_seconds": 60,
        }
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"reply = json.loads(pathlib.Path({str(self.reply)!r}).read_text())\n"
            f"with pathlib.Path({str(self.calls)!r}).open('a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "name = pathlib.Path(sys.argv[1]).name\n"
            "item = reply.get(name, reply['default'])\n"
            "sys.stderr.write('synthetic-private-stderr')\n"
            "sys.stdout.write(item['stdout'])\n"
            "sys.exit(item.get('returncode', 0))\n"
        )
        self.cli.chmod(0o700)
        self.set_reply(self.success("第一段"), second=self.success("第二段"))
        stack.enter_context(patch.dict(os.environ, {"PATH": os.environ.get("PATH", os.defpath)}, clear=True))
        stack.enter_context(patch.object(asr, "XIEMAC_TRANSCRIBE", self.cli, create=True))
        stack.enter_context(patch.object(asr.shutil, "which", return_value="/usr/bin/ffmpeg"))
        stack.enter_context(patch.object(asr, "_download_audio", return_value=self.audio))
        self.chunk_audio = stack.enter_context(patch.object(asr, "_chunk_audio", return_value=self.chunks))
        stack.enter_context(patch.object(asr.time, "sleep"))
        stack.enter_context(patch.object(asr, "resolve_ai_credentials", side_effect=AssertionError("credentials touched")))
        stack.enter_context(patch.object(asr, "make_openai_compatible_client", side_effect=AssertionError("SDK touched")))

    @staticmethod
    def success(text: str) -> dict:
        return {"provider": "remote-whisper.cpp", "model": "large-v3-turbo", "text": text,
                "segments": [{"start": 0, "end": 1, "text": text}], "duration": 1}

    def set_reply(self, payload, *, raw: bool = False, returncode: int = 0, second=None) -> None:
        first = {"stdout": payload if raw else json.dumps(payload), "returncode": returncode}
        self.reply.write_text(json.dumps({
            "default": first,
            self.chunks[1].name: first if second is None else {"stdout": json.dumps(second)},
        }))

    def run_asr(self):
        return asr.transcribe_video("https://example.test/video", self.config)

    def test_xiemac_runs_fixed_cli_safe_argv_and_merges_every_chunk_without_keys(self) -> None:
        result = self.run_asr()
        self.assertEqual(result.status, "success", result.error_message)
        self.assertEqual(result.model, "large-v3-turbo")
        self.assertEqual(result.text, "第一段\n\n第二段")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(calls, [
            [str(chunk), "--language", "zh", "--model", "large-v3-turbo", "--timeout", "180"]
            for chunk in self.chunks
        ])
        self.assertEqual(self.chunk_audio.call_args.args[2], 30)

    def test_default_config_uses_xiemac_while_summary_stays_deepseek(self) -> None:
        config = yaml.safe_load((ROOT / "config.yml").read_text())
        self.config = config["asr"]
        result = self.run_asr()
        self.assertEqual(result.status, "success", result.error_message)
        self.assertEqual(config["summary"]["provider"], "deepseek")
        self.assertEqual(config["asr"]["language_candidates"], ["zh"])
        self.assertEqual(config["asr"]["chunk_seconds"], 30)
        self.assertEqual(self.chunk_audio.call_args.args[2], 30)

    def test_empty_malformed_or_wrong_provenance_never_becomes_text(self) -> None:
        payloads = [
            "", "not json synthetic-private-stdout", "null", "[]", "{}",
            json.dumps(self.success("   ")),
            json.dumps({**self.success("text"), "text": {"error": "bad"}}),
            json.dumps({**self.success("text"), "provider": "wrong-provider"}),
            json.dumps({**self.success("text"), "model": "wrong-model"}),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.set_reply(payload, raw=True)
                result = self.run_asr()
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.text, "")
                self.assertNotIn("synthetic-private", result.error_message)

    def test_remote_busy_stops_without_partial_text_or_language_retry(self) -> None:
        self.config["language_candidates"] = ["zh", "en"]
        self.set_reply(self.success("第一段"), second={
            "provider": "remote-whisper.cpp", "error": {"code": "remote_busy", "message": "synthetic-private"},
        })
        result = self.run_asr()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.text, "")
        self.assertIn("remote_busy", result.error_message)
        self.assertNotIn("synthetic-private", result.error_message)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_cli_failure_codes_are_bounded_and_stderr_is_not_persisted(self) -> None:
        for payload, returncode, expected in (
            ({"provider": "remote-whisper.cpp", "error": {"code": "timeout"}}, 1, "timeout"),
            ({"provider": "remote-whisper.cpp", "error": {"code": "synthetic-private" * 100}}, 1, "client_failed"),
            (self.success("not a successful exit"), 1, "client_failed"),
        ):
            with self.subTest(expected=expected):
                self.set_reply(payload, returncode=returncode)
                result = self.run_asr()
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.text, "")
                self.assertIn(expected, result.error_message)
                self.assertNotIn("synthetic-private", result.error_message)
                self.assertLess(len(result.error_message), 160)

    def test_missing_cli_is_failed(self) -> None:
        self.cli.unlink()
        result = self.run_asr()
        self.assertEqual(result.status, "failed")
        self.assertIn("cli_unavailable", result.error_message)

    def test_stale_asr_model_environment_is_rejected_before_cli(self) -> None:
        os.environ["ASR_MODEL"] = "whisper-1"
        result = self.run_asr()
        self.assertEqual(result.status, "failed")
        self.assertIn("unsupported_model", result.error_message)
        self.assertFalse(self.calls.exists())

    def test_outer_timeout_sends_sigterm_and_allows_cli_cleanup(self) -> None:
        cleaned = self.tmp / "cleanup-completed"
        ready = self.tmp / "handler-ready"
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, signal, time\n"
            "def stop(*args):\n"
            f"    pathlib.Path({str(cleaned)!r}).write_text('cleaned')\n"
            "    raise SystemExit(1)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
            "while True: time.sleep(0.05)\n"
        )
        real_popen = asr.subprocess.Popen

        def start_ready_cli(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            deadline = time.monotonic() + 5
            while not ready.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
                    self.fail("Fake CLI did not install its signal handler")
                threading.Event().wait(0.01)
            return process

        with (
            patch.object(asr.subprocess, "Popen", side_effect=start_ready_cli),
            patch.object(asr, "XIEMAC_PROCESS_TIMEOUT", 0.3, create=True),
        ):
            result = self.run_asr()
        self.assertEqual(result.status, "failed")
        self.assertIn("timeout", result.error_message)
        self.assertEqual(cleaned.read_text(), "cleaned")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ai_client
import summarize


class DeepSeekSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = self.enterContext(ExitStack())
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deepseek-test-key"}, clear=True))
        stack.enter_context(patch.object(Path, "cwd", return_value=tmp))
        stack.enter_context(patch.object(ai_client, "SHARED_PROVIDERS_ENV", tmp / "providers.env", create=True))
        stack.enter_context(patch.object(ai_client, "LOCAL_HKUST_GZ_PROJECT", tmp / "school"))
        self.requests = []
        self.status_code = 200
        self.finish_reason = "stop"
        self.error_message = "Synthetic provider failure"
        self.content = json.dumps({"title": "Test digest", "one_sentence_summary": "NVDA discussion"})
        self.record = {
            "title": "Test finance video",
            "channel_name": "Test channel",
            "url": "https://example.test/video",
            "text": "The creator discussed NVDA.",
            "content_source": "captions",
        }

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.status_code != 200:
                return httpx.Response(self.status_code, json={"error": {"message": self.error_message}})
            return httpx.Response(200, json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{"index": 0, "finish_reason": self.finish_reason, "message": {
                    "role": "assistant", "content": self.content
                }}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        def make_test_client(credentials):
            client = OpenAI(
                api_key=credentials.api_key,
                base_url=credentials.base_url,
                http_client=httpx.Client(transport=httpx.MockTransport(handle)),
            )
            self.addCleanup(client.close)
            return client

        stack.enter_context(patch.object(summarize, "make_openai_compatible_client", make_test_client))

    def summarize(self, kind: str, config: dict):
        if kind == "daily":
            return summarize.summarize_daily_digest([self.record], config)
        return summarize.summarize_transcript(self.record, self.record["text"], "captions", config)

    def test_both_summary_paths_use_deepseek_default_model_endpoint_and_json_output(self) -> None:
        os.environ["OPENAI_SUMMARY_MODEL"] = "gpt-4o-mini"
        for kind in ("daily", "transcript"):
            with self.subTest(kind=kind):
                result = self.summarize(kind, {"provider": "deepseek"})
                self.assertEqual(result.status, "success", result.error_message)
                request = self.requests[-1]
                self.assertEqual(str(request.url), "https://api.deepseek.com/v1/chat/completions")
                self.assertEqual(request.headers["Authorization"], "Bearer deepseek-test-key")
                body = json.loads(request.content)
                self.assertEqual(body["model"], "deepseek-flash")
                self.assertEqual(body["response_format"], {"type": "json_object"})
                self.assertEqual(body["max_tokens"], 16384)
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertIn("NVDA", result.summary_html)

    def test_deepseek_empty_content_or_empty_json_is_a_failed_summary(self) -> None:
        for kind in ("daily", "transcript"):
            for content in (None, "", "   ", "{}", "[]", "null"):
                with self.subTest(kind=kind, content=content):
                    self.content = content
                    result = self.summarize(kind, {"provider": "deepseek"})
                    self.assertEqual(result.status, "failed")
                    self.assertIsNone(result.summary_data)

    def test_deepseek_length_finish_is_failed_even_when_partial_output_is_valid_json(self) -> None:
        self.finish_reason = "length"
        for kind in ("daily", "transcript"):
            with self.subTest(kind=kind):
                result = self.summarize(kind, {"provider": "deepseek"})
                self.assertEqual(result.status, "failed")
                self.assertIsNone(result.summary_data)
                self.assertIn("finish_reason=length", result.error_message)

    def test_legacy_providers_do_not_receive_deepseek_request_parameters(self) -> None:
        os.environ.update(HKUST_GZ_API_KEY="school-test-key", OPENAI_API_KEY="openai-test-key")
        for provider in ("hkust_gz", "openai"):
            with self.subTest(provider=provider):
                result = self.summarize("daily", {"provider": provider, "model": "legacy-test-model"})
                self.assertEqual(result.status, "success", result.error_message)
                body = json.loads(self.requests[-1].content)
                self.assertNotIn("thinking", body)
                self.assertNotIn("response_format", body)
                self.assertNotIn("max_tokens", body)

    def test_summary_model_environment_overrides_config(self) -> None:
        os.environ["SUMMARY_MODEL"] = "deepseek-test-model"
        result = self.summarize("daily", {"provider": "deepseek", "model": "deepseek-flash"})
        self.assertEqual(result.status, "success", result.error_message)
        self.assertEqual(json.loads(self.requests[0].content)["model"], "deepseek-test-model")

    def test_authentication_error_cannot_persist_echoed_api_key(self) -> None:
        os.environ.update(HKUST_GZ_API_KEY="school-test-key", OPENAI_API_KEY="openai-test-key")
        self.status_code = 401
        for provider, key in (
            ("deepseek", "deepseek-test-key"),
            ("hkust_gz", "school-test-key"),
            ("openai", "openai-test-key"),
        ):
            for kind in ("daily", "transcript"):
                with self.subTest(provider=provider, kind=kind):
                    self.error_message = f"Authentication Fails, Your api key: {key} is invalid ({key})"
                    result = self.summarize(kind, {"provider": provider})
                    self.assertEqual(result.status, "failed")
                    self.assertNotIn(key, json.dumps(result.to_dict()))
                    self.assertIn("401", result.error_message)
                    self.assertIn("[REDACTED]", result.error_message)
                    self.assertNotIn("Authentication Fails", result.summary_html)
                    self.assertIn("模型摘要暂不可用，已保留原视频链接。", result.summary_html)

    def test_client_initialization_error_cannot_persist_echoed_api_key(self) -> None:
        key = "deepseek-test-key"
        for kind in ("daily", "transcript"):
            with self.subTest(kind=kind), patch.object(
                summarize,
                "make_openai_compatible_client",
                side_effect=RuntimeError(f"Synthetic client initialization failure for {key}"),
            ):
                result = self.summarize(kind, {"provider": "deepseek"})
                self.assertEqual(result.status, "failed")
                self.assertNotIn(key, json.dumps(result.to_dict()))
                self.assertIn("Synthetic client initialization failure", result.error_message)
                self.assertIn("[REDACTED]", result.error_message)

    def test_configured_failure_never_attempts_school_or_openai_models(self) -> None:
        config = yaml.safe_load((ROOT / "config.yml").read_text())
        self.status_code = 400
        result = self.summarize("daily", config["summary"])
        self.assertEqual(result.status, "failed")
        self.assertEqual([json.loads(request.content)["model"] for request in self.requests], ["deepseek-flash"])

    def test_workflow_routes_summary_and_asr_credentials_independently(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/daily.yml").read_text())
        step = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("name") == "Generate feed")
        values = {
            "${{ secrets.DEEPSEEK_API_KEY }}": "deepseek-workflow-test-key",
            "${{ vars.DEEPSEEK_BASE_URL }}": "https://api.deepseek.com/v1",
            "${{ secrets.HKUST_GZ_API_KEY }}": "school-workflow-test-key",
            "${{ vars.HKUST_GZ_BASE_URL }}": "https://gpt-api.hkust-gz.edu.cn/v1",
        }
        config = yaml.safe_load((ROOT / "config.yml").read_text())
        with patch.dict(os.environ, {name: values.get(value, "") for name, value in step["env"].items()}, clear=True):
            summary = ai_client.resolve_ai_credentials(config["summary"])
            asr = ai_client.resolve_ai_credentials({"provider": "hkust_gz", "model": "whisper-1"})
        self.assertEqual(summary.provider, "deepseek")
        self.assertEqual(summary.api_key, "deepseek-workflow-test-key")
        self.assertEqual(summary.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(asr.provider, "hkust_gz")
        self.assertEqual(asr.api_key, "school-workflow-test-key")
        self.assertEqual(asr.base_url, "https://gpt-api.hkust-gz.edu.cn/v1")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ai_client


class ModelJSONTests(unittest.TestCase):
    def test_preserves_whitespace_escapes_unicode_and_json_types(self) -> None:
        content = '{"标题":"观点\n续行\r结束\t列", "nested":["\\\"quoted\\\"", "\\n", "\\u0000", true, null, 2]}'
        self.assertEqual(ai_client.parse_model_json(content), {
            "标题": "观点\n续行\r结束\t列",
            "nested": ['"quoted"', "\n", "\x00", True, None, 2],
        })
        for content, expected in (("{}", {}), ("[]", []), ("null", None), ('"text"', "text")):
            with self.subTest(content=content):
                self.assertEqual(ai_client.parse_model_json(content), expected)

    def test_rejects_every_other_literal_c0_control_even_after_allowed_newline(self) -> None:
        for code in range(32):
            if chr(code) in "\t\r\n":
                continue
            for prefix in ("", "\n"):
                with self.subTest(code=code, prefix=repr(prefix)):
                    with self.assertRaises(json.JSONDecodeError):
                        ai_client.parse_model_json('{"value":"' + prefix + chr(code) + '"}')

    def test_rejects_malformed_truncated_or_extra_json(self) -> None:
        for content in (
            '{"value":"line\nunfinished',
            '{"value":"line\nsecond"',
            '{"value":"line\nsecond",}',
            '{"value":"line\nsecond"} {"extra":1}',
            '{"value":"line\nsecond\\x"}',
            '{"value":unquoted}',
            '```json\n{"value":"line\nsecond"}\n```',
        ):
            with self.subTest(content=repr(content)):
                with self.assertRaises(json.JSONDecodeError):
                    ai_client.parse_model_json(content)


class AICredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        stack = self.enterContext(ExitStack())
        self.tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.project = self.tmp / "project"
        self.school = self.tmp / "school"
        self.shared = self.tmp / "providers.env"
        self.project.mkdir()
        self.school.mkdir()
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(Path, "cwd", return_value=self.project))
        stack.enter_context(patch.object(ai_client, "LOCAL_HKUST_GZ_PROJECT", self.school))
        stack.enter_context(patch.object(ai_client, "SHARED_PROVIDERS_ENV", self.shared, create=True))

    def test_deepseek_uses_its_own_key_and_official_default_endpoint(self) -> None:
        os.environ.update(
            DEEPSEEK_API_KEY="deepseek-test-key",
            HKUST_GZ_API_KEY="school-test-key",
            HKUST_GZ_BASE_URL="https://school.example/v1",
            OPENAI_API_KEY="openai-test-key",
        )
        credentials = ai_client.resolve_ai_credentials({"provider": "deepseek"})
        self.assertEqual(credentials.provider, "deepseek")
        self.assertEqual(credentials.api_key, "deepseek-test-key")
        self.assertEqual(credentials.base_url, "https://api.deepseek.com/v1")

    def test_api_key_is_excluded_from_credential_repr(self) -> None:
        credentials = ai_client.AICredentials("deepseek", "private-test-value")
        self.assertNotIn("private-test-value", repr(credentials))

    def test_deepseek_environment_overrides_project_and_shared_values(self) -> None:
        (self.project / ".env").write_text(
            "DEEPSEEK_API_KEY=project-test-key\nDEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        )
        self.shared.write_text("DEEPSEEK_API_KEY=shared-test-key\n")
        os.environ.update(
            DEEPSEEK_API_KEY="environment-test-key",
            DEEPSEEK_BASE_URL="https://api.deepseek.com/v1/",
        )
        credentials = ai_client.resolve_ai_credentials({"provider": "deepseek"})
        self.assertEqual(credentials.api_key, "environment-test-key")
        self.assertEqual(credentials.base_url, "https://api.deepseek.com/v1")

    def test_deepseek_project_dotenv_precedes_shared_file_and_ignores_school_dotenv(self) -> None:
        (self.project / ".env").write_text(
            "export DEEPSEEK_API_KEY='project-test-key'\nDEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        )
        (self.school / ".env").write_text("DEEPSEEK_API_KEY=school-project-test-key\n")
        self.shared.write_text("DEEPSEEK_API_KEY=shared-test-key\n")
        credentials = ai_client.resolve_ai_credentials({"provider": "deepseek"})
        self.assertEqual(credentials.api_key, "project-test-key")
        self.assertEqual(credentials.base_url, "https://api.deepseek.com")

    def test_deepseek_shared_file_is_a_readonly_fallback(self) -> None:
        content = (
            "# Synthetic fixture only\nDEEPSEEK_API_KEY=shared-test-key\n"
            "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1/\n"
            "HKUST_GZ_API_KEY=unrelated-test-key\n"
        )
        self.shared.write_text(content)
        self.shared.chmod(0o400)
        (self.school / ".env").write_text("DEEPSEEK_API_KEY=school-project-test-key\n")
        credentials = ai_client.resolve_ai_credentials({"provider": "deepseek"})
        self.assertEqual(credentials.api_key, "shared-test-key")
        self.assertEqual(credentials.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(self.shared.read_text(), content)
        self.assertEqual(self.shared.stat().st_mode & 0o777, 0o400)

    def test_missing_deepseek_key_does_not_use_school_or_openai_key(self) -> None:
        os.environ.update(HKUST_GZ_API_KEY="school-test-key", OPENAI_API_KEY="openai-test-key")
        self.shared.write_text("HKUST_GZ_API_KEY=shared-school-test-key\n")
        with self.assertRaisesRegex(ValueError, "Missing DEEPSEEK_API_KEY"):
            ai_client.resolve_ai_credentials({"provider": "deepseek", "api_key_env": "HKUST_GZ_API_KEY"})

    def test_deepseek_rejects_nonofficial_endpoint_without_echoing_input(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "deepseek-test-key"
        for endpoint in (
            "https://gpt-api.hkust-gz.edu.cn/v1",
            "https://api.deepseek.com.attacker.example/v1",
            "http://api.deepseek.com/v1",
            "https://private-test-value@api.deepseek.com/v1",
        ):
            with self.subTest(endpoint=endpoint):
                os.environ["DEEPSEEK_BASE_URL"] = endpoint
                with self.assertRaises(ValueError) as raised:
                    ai_client.resolve_ai_credentials({"provider": "deepseek"})
                self.assertNotIn("private-test-value", str(raised.exception))

    def test_deepseek_config_endpoint_is_used_when_environment_is_absent(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "deepseek-test-key"
        credentials = ai_client.resolve_ai_credentials(
            {"provider": "deepseek", "base_url": "https://api.deepseek.com"}
        )
        self.assertEqual(credentials.base_url, "https://api.deepseek.com")

    def test_school_asr_keeps_its_own_key_and_does_not_read_shared_file(self) -> None:
        os.environ.update(HKUST_GZ_API_KEY="school-test-key", DEEPSEEK_API_KEY="deepseek-test-key")
        self.shared.write_text("HKUST_GZ_BASE_URL=https://wrong.example\n")
        original_read_text = Path.read_text

        def read_without_shared(path, *args, **kwargs):
            if path == self.shared:
                self.fail("School credentials must not read the shared DeepSeek file")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_without_shared):
            credentials = ai_client.resolve_ai_credentials({"provider": "hkust_gz", "model": "whisper-1"})
        self.assertEqual(credentials.api_key, "school-test-key")
        self.assertEqual(credentials.base_url, "https://gpt-api.hkust-gz.edu.cn/v1")

    def test_openai_does_not_read_shared_provider_file(self) -> None:
        self.shared.write_text("OPENAI_API_KEY=shared-openai-test-key\n")
        with self.assertRaisesRegex(ValueError, "Missing OPENAI_API_KEY"):
            ai_client.resolve_ai_credentials({"provider": "openai"})


if __name__ == "__main__":
    unittest.main()

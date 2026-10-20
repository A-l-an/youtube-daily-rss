from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit


DEFAULT_HKUST_GZ_BASE_URL = "https://gpt-api.hkust-gz.edu.cn/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
LOCAL_HKUST_GZ_PROJECT = Path("/Users/alan/Documents/Life/hkustgz-speech-tools")
SHARED_PROVIDERS_ENV = Path("/Users/alan/.config/janus-ai/providers.env")


@dataclass
class AICredentials:
    provider: str
    api_key: str = field(repr=False)
    base_url: Optional[str] = None


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _dotenv_candidates(extra_paths: Optional[Iterable[Path]] = None) -> list[Path]:
    candidates = [Path.cwd() / ".env", LOCAL_HKUST_GZ_PROJECT / ".env"]
    if extra_paths:
        candidates.extend(extra_paths)
    return candidates


def _read_dotenv_value(name: str, paths: Iterable[Path]) -> Optional[str]:
    for env_file in paths:
        if not env_file.exists():
            continue
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return _strip_env_quotes(value.strip())
    return None


def read_dotenv_value(name: str, extra_paths: Optional[Iterable[Path]] = None) -> Optional[str]:
    return _read_dotenv_value(name, _dotenv_candidates(extra_paths))


def env_or_dotenv(name: str, extra_paths: Optional[Iterable[Path]] = None) -> Optional[str]:
    return os.getenv(name) or read_dotenv_value(name, extra_paths)


def resolve_ai_credentials(config: Dict[str, Any], default_provider: str = "hkust_gz") -> AICredentials:
    provider = config.get("provider", default_provider)
    if provider == "deepseek":
        local_paths = [Path.cwd() / ".env"]
        api_key = (
            os.getenv("DEEPSEEK_API_KEY")
            or _read_dotenv_value("DEEPSEEK_API_KEY", local_paths)
            or _read_dotenv_value("DEEPSEEK_API_KEY", [SHARED_PROVIDERS_ENV])
        )
        if not api_key:
            raise ValueError("Missing DEEPSEEK_API_KEY")
        base_url = (
            os.getenv("DEEPSEEK_BASE_URL")
            or _read_dotenv_value("DEEPSEEK_BASE_URL", local_paths)
            or config.get("base_url")
            or _read_dotenv_value("DEEPSEEK_BASE_URL", [SHARED_PROVIDERS_ENV])
            or DEFAULT_DEEPSEEK_BASE_URL
        ).rstrip("/")
        endpoint = urlsplit(base_url)
        if (
            endpoint.scheme != "https"
            or endpoint.netloc != "api.deepseek.com"
            or endpoint.path not in {"", "/v1"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("DEEPSEEK_BASE_URL must use the official DeepSeek HTTPS API endpoint")
        return AICredentials(provider=provider, api_key=api_key, base_url=base_url)

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY") or read_dotenv_value("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Missing OPENAI_API_KEY")
        return AICredentials(provider=provider, api_key=api_key)

    if provider in {"hkust_gz", "hkust-gz", "hkustgz"}:
        api_key = (
            env_or_dotenv("HKUST_GZ_API_KEY")
            or env_or_dotenv("HKUSTGZ_API_KEY")
            or env_or_dotenv(config.get("api_key_env", "HKUST_GZ_API_KEY"))
        )
        if not api_key:
            raise ValueError("Missing HKUST_GZ_API_KEY")
        base_url = (
            env_or_dotenv("HKUST_GZ_BASE_URL")
            or config.get("base_url")
            or DEFAULT_HKUST_GZ_BASE_URL
        )
        return AICredentials(provider="hkust_gz", api_key=api_key, base_url=base_url.rstrip("/"))

    raise ValueError(f"Unsupported AI provider: {provider}")


def make_openai_compatible_client(credentials: AICredentials) -> Any:
    from openai import OpenAI

    kwargs: Dict[str, Any] = {"api_key": credentials.api_key}
    if credentials.base_url:
        kwargs["base_url"] = credentials.base_url
    return OpenAI(**kwargs)

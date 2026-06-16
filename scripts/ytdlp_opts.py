"""Shared helper that hardens yt-dlp calls against YouTube's bot gate.

Everything is driven by environment variables so that nothing sensitive (cookies,
machine paths) has to live in the repository. When none of the variables are set
this module is a no-op and yt-dlp behaves exactly as before (legacy behaviour),
which keeps the unit tests and any non-configured environment working.

Recognised environment variables
--------------------------------
YTDLP_COOKIES           Path to a Netscape cookies.txt exported from a logged-in
                        YouTube session. Used via yt-dlp's ``cookiefile`` option,
                        which needs no Keychain access at run time.
YT_IMPERSONATE          curl_cffi browser-TLS target, e.g. ``chrome-131``. Set to
                        ``none``/``off`` to disable. Silently ignored if curl_cffi
                        or the target is unavailable.
YTDLP_AUDIO_CLIENTS     Comma-separated player clients for audio (ASR) downloads,
                        e.g. ``android,mweb,web``. Overrides the caller default.
YTDLP_SUBTITLE_CLIENTS  Comma-separated player clients for subtitle downloads.
YTDLP_POT_BASE_URL      Base URL of a bgutil PO-token provider (HTTP mode), e.g.
                        ``http://127.0.0.1:4416``. Enables SABR-gated formats.

``main.py`` calls :func:`bootstrap_env_from_config` so the same values can also be
declared under ``youtube_access:`` in ``config.yml`` (env always wins).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def _expand(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return os.path.expanduser(os.path.expandvars(value))


def resolve_cookiefile() -> Optional[str]:
    """Return an existing cookies file path from ``YTDLP_COOKIES`` or ``None``."""
    raw = os.getenv("YTDLP_COOKIES")
    path = _expand(raw)
    if path and Path(path).is_file():
        return path
    if raw:
        logging.warning(
            "YTDLP_COOKIES=%r but no readable file there; continuing without cookies",
            raw,
        )
    return None


def _impersonate_target() -> Optional[Any]:
    raw = os.getenv("YT_IMPERSONATE")
    if not raw or raw.strip().lower() in {"", "none", "off", "false", "0", "disabled"}:
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        return ImpersonateTarget.from_str(raw.strip())
    except Exception as exc:  # curl_cffi missing / invalid target -> degrade gracefully
        logging.warning("impersonate %r unavailable (%s); continuing without it", raw, exc)
        return None


def _clients(env_name: str) -> Optional[List[str]]:
    raw = os.getenv(env_name)
    if not raw:
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or None


def merge_into(options: Dict[str, Any], *, audio: bool = False) -> Dict[str, Any]:
    """Merge cookie/impersonate/client/POT options into a yt-dlp options dict.

    Mutates and returns ``options``. ``extractor_args`` is merged (not replaced) so
    the caller's existing ``player_client`` default survives unless an override env
    var is set. No-op when no relevant env var is present.
    """
    cookiefile = resolve_cookiefile()
    if cookiefile:
        options["cookiefile"] = cookiefile

    target = _impersonate_target()
    if target is not None:
        options["impersonate"] = target

    # Modern yt-dlp must solve YouTube's JS challenge via the EJS solver (run by Deno);
    # without it you get "only images available" / SABR 403s on audio. Default on.
    rc_raw = os.getenv("YTDLP_REMOTE_COMPONENTS", "ejs:github")
    if rc_raw and rc_raw.strip().lower() not in {"none", "off", "false", "0", "disabled"}:
        options["remote_components"] = [item.strip() for item in rc_raw.split(",") if item.strip()]

    extractor: Dict[str, Dict[str, Any]] = {}

    clients = _clients("YTDLP_AUDIO_CLIENTS" if audio else "YTDLP_SUBTITLE_CLIENTS")
    if clients:
        extractor.setdefault("youtube", {})["player_client"] = clients

    pot_base = os.getenv("YTDLP_POT_BASE_URL")
    if pot_base:
        extractor["youtubepot-bgutilhttp"] = {"base_url": [pot_base]}

    if extractor:
        merged = dict(options.get("extractor_args") or {})
        for key, value in extractor.items():
            if key in merged and isinstance(merged[key], dict):
                combined = dict(merged[key])
                combined.update(value)
                merged[key] = combined
            else:
                merged[key] = value
        options["extractor_args"] = merged

    return options


def bootstrap_env_from_config(config: Optional[Dict[str, Any]]) -> None:
    """Populate the YTDLP_* env vars from ``config['youtube_access']`` if unset."""
    access = (config or {}).get("youtube_access") or {}

    scalar = {
        "YTDLP_COOKIES": access.get("cookies_file"),
        "YT_IMPERSONATE": access.get("impersonate"),
        "YTDLP_POT_BASE_URL": access.get("pot_base_url"),
    }
    for env_name, value in scalar.items():
        if value and not os.getenv(env_name):
            os.environ[env_name] = str(value)

    listish = {
        "YTDLP_AUDIO_CLIENTS": access.get("audio_player_clients"),
        "YTDLP_SUBTITLE_CLIENTS": access.get("subtitle_player_clients"),
    }
    for env_name, value in listish.items():
        if value and not os.getenv(env_name):
            if isinstance(value, (list, tuple)):
                os.environ[env_name] = ",".join(str(item) for item in value)
            else:
                os.environ[env_name] = str(value)

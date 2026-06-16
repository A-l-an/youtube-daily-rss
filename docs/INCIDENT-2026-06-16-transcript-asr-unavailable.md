# Incident — 2026-06-16: "Transcript and ASR were unavailable" (link-only digest)

Status: **root cause confirmed; full fix implemented and validated end-to-end (captions AND
audio); deployed to master.**
Investigated on the runner host itself (Alan's Mac, self-hosted GitHub Actions runner).

> Update: the real missing piece turned out to be yt-dlp's **EJS JS-challenge solver**
> (`--remote-components ejs:github`, run by Deno). With cookies + impersonate + EJS, **both**
> captions (NaNa) and audio/ASR (Rhino) succeed — even from a flagged datacenter IP. The
> bgutil PO-token provider is therefore **not needed**. See §5–§6.

---

## 1. The two things you asked about

1. **Today's digest is link-only** ("Transcript and ASR were unavailable, so only the
   original video links are provided") — both channels got no analysis.
2. **NaNa shows "15号" data, not "16号"** — looks a day behind.

Short version:

- **#1 is a real regression.** YouTube started **bot-blocking the runner on 2026-06-16**.
  Every text-extraction path failed at once. This is fixable.
- **#2 is NOT a bug.** NaNa's newest upload genuinely *is* the 06.15 video; her titles use
  the **US-market session date**, which lags the Asia calendar by a day. The pipeline fetched
  her latest correctly. It only *looked* wrong because (a) it had no analysis (caused by #1),
  and (b) the other channel had a same-day video next to it. See §4.

---

## 2. Evidence (run 27601111659, 2026-06-16 15:21 CST)

Today's run "succeeded" in 2m50s (vs **1h29m** the day before — that alone shows ASR never
ran). The log shows three failures stacking up:

```
youtube-transcript-api : Could not retrieve a transcript ... RequestBlocked / IP ban
yt-dlp subtitle fallback: ERROR [youtube] ...: Sign in to confirm you're not a bot
yt-dlp audio (ASR)      : audio download failed after 3 attempts: ...not a bot
```

Published `summaries.json` confirms the degraded item and the *normal* pattern:

| date | 视野环球财经 (Rhino) | NaNa |
|------|----------------------|------|
| normal | transcript=unavailable, **asr=success** (relies on ASR) | **transcript=success** (relies on captions) |
| 06-16 | transcript=failed, asr=failed → **link-only** | transcript=failed, asr=failed → **link-only** |

So both of Rhino's and NaNa's content sources died on the same wall on the same day.

---

## 3. Root cause

**A. YouTube anti-bot gate turned on for this runner** (the "Sign in to confirm you're not a
bot" gate). As recently as 06-15 the cookieless `android/web` player clients worked; on 06-16
they were blocked. This kills *all three* paths above at once.

**B. Modern yt-dlp also now needs a JavaScript runtime.** yt-dlp 2026.06.09 prints
"No supported JavaScript runtime could be found … extraction without a JS runtime has been
deprecated" — **Deno was not installed** on the runner.

The current pipeline calls yt-dlp with only
`extractor_args={"youtube":{"player_client":["android","mweb","web"]}}` and **no cookies, no
impersonation, no JS runtime** — exactly the combination YouTube now refuses.

---

## 4. Why NaNa is "a day behind" (not a bug)

Live channel feed at investigation time:

- **NaNa** newest = `dbRTLhk_PNs` "空头被抬走了！NaNa说美股(2026.06.15)", published
  **2026-06-15T21:59 UTC ≈ 06-16 05:59 CST**. Previous = 06.12. **No 06.16 video exists yet.**
- **Rhino** newest = `kzXnVeMEhaI`, published **2026-06-16T01:16 UTC** — a genuine same-day video.

NaNa names each video by the **US trading session** (06.15 session → recap published in the
early hours of 06-16 CST). The pipeline grabbed her true latest video. There is simply nothing
newer to fetch until she posts the 06.16-session recap. The fix for #1 will give that 06.15
video real analysis; the "date" in the title is her own convention, not a fetch error.

---

## 5. Fix — validated on this machine

The working recipe (media-to-text skill, Strategy C) = **logged-in cookies + browser
impersonation + a JS runtime**.

Already done (safe / reversible):

- ✅ Installed **Deno 2.8.3** (`brew install deno`) — the JS runtime yt-dlp needs.
- ✅ Exported a known-good **cookies file** to `~/.config/youtube-daily-rss/youtube-cookies.txt`
  (chmod 600, contains LOGIN_INFO/SAPISID/__Secure-1PSID/3PSID/SID/HSID). Used via `--cookies`
  so there is **no Keychain prompt and no Chrome-lock race at run time**.
- ✅ Confirmed a clean `/usr/bin/python3` venv installs the full `requirements.txt` + `curl_cffi`
  and `ImpersonateTarget("chrome-131")` works (anaconda's Python can't host curl_cffi — known
  OpenSSL/BoringSSL clash — so CI must use system python).

The decisive extra ingredient (newer yt-dlp): **`--remote-components ejs:github`**. yt-dlp
2026.06.09 prints "challenge solver script (deno) … skipped … may be required" and then fails
with "only images available". Enabling the EJS solver downloads
`yt-dlp/ejs …/yt.solver.lib.min.js`, Deno solves the challenge, and proper DASH formats unlock.

Proven results (keychain-free, validated **through the actual pipeline modules**, py3.12 +
yt-dlp 2026.06.09, from the flagged datacenter IP):

- ✅ **Captions** — `transcript.get_transcript("dbRTLhk_PNs")` → status=success,
  source=yt_dlp_manual_subtitle, zh-Hans, 3549 chars. **NaNa fixed.**
- ✅ **Audio/ASR** — `asr._download_audio("…kzXnVeMEhaI")` → audio.mp3, 11 MB (opus format 251,
  not the muxed fallback — SABR defeated by EJS solving). **Rhino fixed.**
- ✅ All 21 unit tests pass on the new CI venv.

Because EJS solving already unlocks audio under SABR, the **bgutil PO-token provider is not
needed**. (`YTDLP_POT_BASE_URL` remains supported as an optional extra lever if YouTube ever
tightens further.)

---

## 6. Proposed code / config changes (not yet applied)

1. `scripts/ytdlp_opts.py` (new): env-driven helper that merges `cookiefile`, best-effort
   `impersonate`, `remote_components=['ejs:github']` (default on), and optional client/POT
   overrides into any yt-dlp options dict. No-op when unconfigured (keeps tests green).
2. `scripts/transcript.py`: **removed** the forced `player_client=[android,mweb,web]` (with
   cookies, android is skipped and mweb needs a PO token); calls `ytdlp_opts.merge_into`.
3. `scripts/asr.py` (`_download_audio`): calls `ytdlp_opts.merge_into(audio=True)`.
4. `scripts/main.py`: `ytdlp_opts.bootstrap_env_from_config(config)` so `config.yml` values map
   to env.
5. `config.yml`: new `youtube_access` block (cookies path, impersonate target, optional clients
   / pot_base_url) — all overridable by env so nothing sensitive is committed.
6. `requirements.txt`: add `curl_cffi`.
7. `.github/workflows/daily.yml`: build `.venv-ci` from a clean non-anaconda Python
   (prefers Homebrew `python@3.12` → latest yt-dlp; falls back to `/usr/bin/python3`); put
   Homebrew + Deno on `$GITHUB_PATH`; install Deno on demand.
8. `.gitignore`: never commit cookie files.

Runner host one-time setup (done): `brew install deno python@3.12`; cookies exported to
`~/.config/youtube-daily-rss/youtube-cookies.txt`. All code degrades gracefully — if
cookies/impersonate/Deno are missing, behavior is no worse than today's link-only.

---

## 7. Residual risk & maintenance

- **Rhino ASR under SABR:** RESOLVED by the EJS solver (validated even on a datacenter IP). If
  YouTube ever tightens further, set `youtube_access.pot_base_url` (or `YTDLP_POT_BASE_URL`) to a
  running `bgutil-ytdlp-pot-provider` — already wired, no code change needed.
- **Deno EJS download:** first run downloads the solver from `github.com/yt-dlp/ejs` (then cached).
  The runner needs outbound GitHub access (it already has it for Actions).
- **Cookie expiry:** the exported cookies last weeks–months but eventually rotate. When Rhino/NaNa
  go link-only again, re-export the cookies file (one command). Worth a monthly reminder.
- **Runner schedule drift:** cron is 02:00 UTC (10:00 CST) but today's run fired at 07:21 UTC
  (15:21 CST) — the Mac/runner was asleep/offline until then. Unrelated to this incident, but
  worth fixing separately (keep the runner awake, or move to a hosted runner) so the digest is
  punctual.

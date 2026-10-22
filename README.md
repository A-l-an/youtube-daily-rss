# Daily YouTube Finance Digest RSS

This project builds a daily RSS 2.0 feed from the latest public videos of:

- 视野环球财经 / `@RhinoFinance`
- NaNa说美股 / `@NaNaShuoMeiGu`

It checks YouTube Atom feeds, tries public captions first, optionally falls back to ASR, merges both channels into one daily summary with an OpenAI-compatible model, and writes `public/feed.xml` plus hosted digest pages under `public/items/`.

## Quick Start

```bash
cd youtube-daily-rss
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set:

```bash
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
ASR_MODEL=large-v3-turbo
```

Summaries use the official DeepSeek API with `deepseek-flash`. DeepSeek credentials
are resolved from environment variables, then the project's `.env`, then the
read-only local fallback `/Users/alan/.config/janus-ai/providers.env`. Only the
`deepseek` provider reads that shared file, and it reads only `DEEPSEEK_API_KEY`
and `DEEPSEEK_BASE_URL`; it never substitutes an HKUST-GZ or OpenAI key. The base
URL must be `https://api.deepseek.com/v1` or `https://api.deepseek.com`.
ASR uses Alan's existing `/Users/alan/.local/bin/xiemac-transcribe` CLI and the
`large-v3-turbo` model on the owned xiemac server over SSH/Tailscale. It requires
no school or OpenAI API key. Models stay on xiemac; there is no local-model or
paid-cloud fallback.

The DeepSeek request uses JSON output, disables thinking, and caps output at 16384
tokens. Empty content, an empty JSON object, or `finish_reason=length` is treated
as a failed summary.
See the official [JSON output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)
and [thinking mode](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/) guides.

Run a no-write check:

```bash
python scripts/main.py --dry-run
```

Run the full pipeline:

```bash
python scripts/main.py
```

Reprocess the latest video from each channel:

```bash
python scripts/main.py --force
```

Rebuild one already-ended local date without changing the default latest-video behavior:

```bash
python scripts/main.py --report-date 2026-07-20
```

Date backfills use the latest trustworthy Atom entry from each channel before that
date's local end-of-day cutoff. They fail without writing files if the date is today
or later, any Atom timestamp is untrusted, or either channel has no eligible entry.
Use `--force` with `--report-date` only to replace that same date's deterministic item.

## Schedule

The GitHub Actions workflow runs daily at:

- `10:00` China Standard Time / Taipei Time
- `02:00 UTC`

The cron expression is:

```yaml
0 2 * * *
```

The workflow also supports manual `workflow_dispatch`. Manual runs default to reusing the latest existing daily item; set `report_date` to an ended local `YYYY-MM-DD` for a date backfill, and set `force=true` only when you intentionally want to replace the selected latest or dated item.

The scheduled workflow is designed for a macOS self-hosted runner, not a GitHub-hosted cloud runner. This avoids common YouTube blocking against cloud provider IP ranges.

## Configuration

Main settings live in `config.yml`.

The YouTube fetcher uses official channel Atom feeds:

```text
https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
```

No YouTube API key is required for this.

## Pipeline Behavior

For each run, the pipeline fetches the latest public video from each channel and creates one merged daily RSS item for that pair of videos unless the same pair has already been published. Use `--force` to rebuild the latest pair.

Processing order:

1. Fetch the latest video from each channel Atom feed.
2. Skip only when the current pair of latest videos already has a merged `daily_digest` item.
3. For each video, try captions with `youtube-transcript-api`.
4. If captions fail, try public subtitles and auto captions through `yt-dlp`, preferring manual subtitles before auto captions.
5. If captions still fail and this is not a dry run, try optional ASR through the existing xiemac Whisper CLI over SSH/Tailscale.
6. If text exists from either video, generate one Simplified Chinese merged digest through the configured OpenAI-compatible chat endpoint.
7. The merge prompt groups the same stock, index, sector, or macro event into one section and shows each creator's view, common points, and differences.
8. If text or LLM summary is unavailable, still publish a link-only daily RSS item containing both original video links.
9. Write `state.json`, `summaries.json`, and `public/feed.xml`. A repeated non-force run is a true no-op when its latest pair or dated item already exists.

RSS items include:

- title: one merged daily digest title
- link: hosted digest page, for example `https://a-l-an.github.io/youtube-daily-rss/items/daily-VIDEO_ID-VIDEO_ID.html`
- guid: merged digest ID built from the latest video IDs
- pubDate: digest processing time
- description and `content:encoded`: HTML digest
- categories: `YouTube Digest`, `Finance`, `Stock Market`, and source status

The digest is structured for scanning in an RSS reader. It includes one overall summary plus merged bullet sections for individual stocks, indices, sectors, and macro themes mentioned by either channel. If both creators discuss the same stock or sector, their views are shown in the same section rather than as two separate items. Original YouTube URLs are kept inside the digest body, but they are not the RSS item's primary link, so readers should open the text digest page first.

Date-backfill items use GUID `daily-date-YYYY-MM-DD`, store `report_date` in the
item and digest state, force the same date prefix onto the title, and use the target
date's last local instant as `pubDate`; `processed_at` remains the actual run time.

## Caption and ASR Limitations

Caption extraction depends on public YouTube transcript availability. Some videos have no captions, blocked captions, incomplete auto captions, or language tracks that do not match the configured preferences. The pipeline tries both `youtube-transcript-api` and `yt-dlp` subtitle extraction before falling back to audio ASR.

ASR is optional and only runs when:

- `asr.enabled` is `true`
- the existing `~/.local/bin/xiemac-transcribe` CLI is executable for the runner user
- Tailscale and the existing `ssh xiemac` configuration can reach the owned server, with SSH host verification intact
- `ffmpeg` is installed
- `yt-dlp` can technically access the video audio
- using ASR is appropriate for your deployment and use case

If direct YouTube audio extraction fails or is not appropriate, ASR is skipped or marked failed and the RSS item falls back to the original video link.

The pipeline splits audio into at most 15-second chunks, including the final
short chunk, then calls the CLI sequentially with `--language zh --model
large-v3-turbo --timeout 180`. The outer process limit is 210 seconds per chunk
and permits SIGTERM cleanup. Every chunk must return non-empty text with the
expected `remote-whisper.cpp` provider and model before the combined transcript
is accepted. Malformed responses, timeouts, or `remote_busy` fail explicitly;
partial text is not published as a complete transcript. Before each CLI call,
the project reads health and established inference connections over strict SSH
to the same host used by the CLI (`XIEMAC_ASR_HOST`, default `xiemac`). Observed
busy work or an uncertain check stops the call without starting transcription.
This snapshot avoids observed busy work; it cannot provide an atomic scheduling
guarantee if another task starts between the check and the request. The project
does not change the shared service or its priority policy.

The 15-second limit was calibrated on a full captionless video: a 30-second
request repeatedly returned invalid word timestamps near minute 29, while
shorter requests passed the existing response validation. No timestamps are
discarded or repaired to turn an invalid response into a success.

The CLI and its SSH/Tailscale setup must already be installed; this project does
not install a service or download a model. Legacy `hkust_gz` and `openai` ASR
providers remain available only when explicitly selected in `config.yml` with
their own credentials and supported model. There is no automatic provider switch.

## Evidence and source-retention practices

The daily feed already prefers captions before ASR and asks the summary model to
separate creator opinions from facts mentioned in the source. A source's stated
facts are not independently fact-checked market data. Do not add unsupported
market interpretations or describe a partial input as a full-video analysis.

The following gaps remain in the production pipeline; these are implementation
priorities, not capabilities supplied by this documentation:

- **Time anchors:** caption `start`/`duration` and ASR segments are currently
  flattened to text. Preserve those segments before adding timestamped evidence
  and links back to the exact part of each source video.
- **Input coverage:** merged input is currently limited to a 60,000-character
  prefix. A long first source can consume the budget before the second source's
  text is included. Record each source's available and included text range, and
  report missing or truncated coverage explicitly before claiming both sources
  were analyzed.
- **Reproducible originals:** production stores summaries and source metadata,
  but not immutable transcripts, content hashes, or resumable transcription
  receipts. Keep originals, their source IDs, timestamps, extraction method and
  hashes separate from derived summaries. Temporary ASR validation artifacts do
  not establish production retention or resume support.

For a user-supplied cross-platform link, locate and check the original author and
video identity before extraction; an official title/author check such as YouTube
oEmbed can help. Fixed configured channel IDs and RSS discovery do not need an
extra cross-platform search on every daily run.

When a long-form source is intentionally retained in Alan's knowledge vault,
reuse the existing media-capture validator and no-overwrite publisher. Keep the
original transcript, neutral source record and explicitly requested AI guide
separate. Vault publication is not part of the current RSS pipeline, and should
not silently duplicate every daily item or create Alan's personal reflections.

## GitHub Pages Deployment

1. Push this project to a GitHub repository.
2. Register a macOS self-hosted runner for the repository.
   - Required labels: `self-hosted`, `macOS`, `youtube-rss`
   - Keep the Mac awake, online, and able to access YouTube at the scheduled time.
   - Install `ffmpeg` and GNU tar (`brew install gnu-tar`) and ensure `python`, `ffmpeg`, and `gtar` are available on the runner service PATH.
   - Keep the existing xiemac CLI executable at `~/.local/bin/xiemac-transcribe` and ensure the runner's SSH/Tailscale route is available.
3. On the Janus `alan` runner, keep the DeepSeek key in the canonical local
   `/Users/alan/.config/janus-ai/providers.env` with permissions `600`. The
   credential resolver uses this file read-only when the workflow's DeepSeek
   secret is empty; no copy to GitHub Secrets is required on this runner.
   For other runners, add the repository secret `DEEPSEEK_API_KEY` for summaries.
4. Optionally add repository variables:
   - `DEEPSEEK_BASE_URL`: `https://api.deepseek.com/v1` (the default if unset)
   - `SUMMARY_MODEL`: `deepseek-flash`
   - `ASR_MODEL`: `large-v3-turbo`
   - When migrating ASR, **change the old `ASR_MODEL=whisper-1` repository
     variable (and any local `.env` override) to `large-v3-turbo`, or remove it**.
     The environment overrides `config.yml`; xiemac rejects the old school model
     ID with `unsupported_model` rather than calling a different provider.
   - When migrating an existing deployment, **change the old `SUMMARY_MODEL`
     repository variable from `DeepSeek-V4-Pro` to `deepseek-flash`, or remove it**.
     This variable overrides `config.yml`; the old school model ID is not valid
     for this configuration. No school/OpenAI fallback models are configured.
5. Go to **Settings -> Pages** and set the source to **GitHub Actions**.
6. Run **Daily YouTube RSS Digest** manually once from the Actions tab.
7. Subscribe to:

```text
https://a-l-an.github.io/youtube-daily-rss/feed.xml
```

Update `rss.link` in `config.yml` to the final URL once known.

## Cloudflare Pages Deployment

Cloudflare Pages can serve the generated `public/` directory.

Recommended setup:

- Build command: `python -m pip install -r requirements.txt && python scripts/main.py`
- Output directory: `public`
- Environment variable: `DEEPSEEK_API_KEY`. Default xiemac ASR requires the self-hosted runner's CLI and SSH/Tailscale setup; disable ASR for a hosted-cloud build that lacks them.

Cloudflare Pages scheduled builds are not as direct as GitHub Actions cron. A practical setup is to keep GitHub Actions generating and committing `public/feed.xml`, then let Cloudflare Pages deploy from the repository.

## Files

- `scripts/fetch_youtube.py`: YouTube Atom feed fetcher
- `scripts/transcript.py`: public transcript/caption extraction
- `scripts/ai_client.py`: isolated DeepSeek, HKUST-GZ, and OpenAI credential/client helpers
- `scripts/asr.py`: optional ASR fallback
- `scripts/summarize.py`: digest generation and HTML rendering
- `scripts/rss.py`: RSS 2.0 writer
- `scripts/main.py`: CLI orchestration
- `state.json`: processed video state
- `summaries.json`: stored RSS digest items
- `public/feed.xml`: generated feed

## Notes

The generated summary is for information organization only and is not investment advice. The prompt asks the model to avoid unsupported claims, separate creator opinions from factual market information, preserve ticker symbols and company names, and call out uncertainty.

## Private portfolio guidance (opt-in)

`python scripts/portfolio_guidance.py` creates a separate local report from both
Futu and HSBC holdings recorded in Portfolio Local, plus the latest available day
and an inclusive seven-calendar-day window of this repository's channel evidence.
It reads `summaries.json` without changing the public feed, state, or Pages files.
The analysis always requests official **`deepseek-v4-pro`**, high thinking, and JSON
output, with a 32768-token output budget for the full private portfolio report.
A `finish_reason=length` response still fails without producing a partial report.
`SUMMARY_MODEL` does not affect it; there is no Flash fallback or automatic
retry after a model failure. Model reasoning is never saved.

The default configuration is
`~/.config/youtube-daily-rss/portfolio-guidance.json`. A missing configuration or
`"enabled": false` safely skips the private feature. Keep the configuration and
password file outside Git with permissions `600`. The following is a template;
account-name mapping keys must be the exact names in the local app, and every
mapped, non-archived account must be present. Other accounts remain out of scope.

```json
{
  "enabled": false,
  "model": "deepseek-v4-pro",
  "holdings_confirmed_at": "2026-10-23",
  "source": {
    "base_url": "https://portfolio.example.test:8447",
    "password_file": "~/.config/youtube-daily-rss/portfolio-password",
    "account_aliases": {
      "YOUR_FUTU_HKD_ACCOUNT_NAME": {"alias": "Futu-HKD", "broker": "futu"},
      "YOUR_FUTU_USD_ACCOUNT_NAME": {"alias": "Futu-USD", "broker": "futu"},
      "YOUR_HSBC_USD_ACCOUNT_NAME": {"alias": "HSBC-USD", "broker": "hsbc"}
    }
  },
  "output_dir": "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Life Knowledge Vault/02 Areas/Finance/持仓指导",
  "cache_dir": "~/.cache/youtube-daily-rss/portfolio-guidance"
}
```

Use the actual date on which the person confirmed the holdings; the script never
advances that date. An optional `source.connect_address` may be an IP address to
connect to while retaining the `base_url` hostname for SNI and full TLS certificate
verification. HTTPS is required. Session cookies and profile scope stay in memory;
the only authenticated operations are login, profile-state read, accounts,
holdings-list, snapshots, and stored latest-quote reads. No portfolio mutation,
quote refresh, historical ASR, or trading operation is called.

```bash
# Validate local paths, read holdings and cached/available evidence, without a model call.
python scripts/portfolio_guidance.py --check-only
# Generate for the Shanghai calendar date; the latest opinion date is separately labelled.
python scripts/portfolio_guidance.py --date 2026-10-23
# Use a different private configuration.
python scripts/portfolio_guidance.py --config ~/.config/youtube-daily-rss/portfolio-guidance.json
# Explicitly pay for a fresh generation and save a new revision.
python scripts/portfolio_guidance.py --force
```

Only aliases, tickers, currencies, direction, percentages, cost/valuation status,
and source dates reach the model/report. Raw account names/IDs, quantities,
portfolio amounts, notes, passwords, and cookies do not. Account weight is the
ledger's recorded weight. Combined security weights exclude cash and require
complete valuations in one base currency. Missing quotes or invalid valuations
remain unknown, including the affected weight denominator. Cost-to-price deviation
is `(cost / security market value - 1) × 100%`, not investment performance; zero or
unknown costs are not divided. Snapshot dates are sparse bookkeeping dates, not
broker verification dates; quote dates and the human-confirmed date are separate.

Only successful, source-attributed summaries count as opinions. Empty or punctuated
“not mentioned” placeholders are excluded. Direct summary coverage is limited to
assets with a real source view; mixed absence/industry comments remain indirect.
Recovered captions use their actual body, never titles or quality notes, to
establish direct coverage. The model also receives a locally computed map of
allowed direct evidence IDs for each position; unsupported direct claims still
fail validation. For failed daily
entries whose captions previously succeeded, the script can recover captions and
cache them privately. It never requests historical ASR. Success caches are
`cache_dir/captions/<video_id>.json`, schema version 1, with `video_id`,
`status: "success"`, `source: "captions"`, full `text`, its UTF-8 `sha256`, and
`fetched_on` (`YYYY-MM-DD`). The cache verifies video identity and text hash.
Unavailable captions are attempted at most once per report date. A repeated video
counts once on its earliest known source date, not once per daily digest. Reports
list missing days and unavailable sources; incomplete coverage is explicitly
`insufficient`. Captions beyond 60,000 characters per source are marked truncated.
`--check-only` uses existing caption caches but does not fetch missing captions.
The private CLI initializes the existing `youtube_access` settings before recovery;
explicit environment settings retain priority. Known ASR/transcription uncertainty
is retained in the model evidence and the report.

All generated directories are `700` and files `600`; repository-contained paths
and symlinks are rejected. Reports are named `持仓指导_日报_YY-MM-DD.md`; collisions
produce `_v2`, `_v3`, etc. without overwriting user files. Archived version gaps
are not refilled: matching regular files with permissions `600` are searched for
identical content first; otherwise a new file follows the highest existing version
for that stem, date and suffix. Other dates, suffixes and archive contents are
excluded. Identical input reuses a
validated private result without another model call; fetched-at timestamps do not
change the input fingerprint. A process lock prevents simultaneous paid runs.
Model prose is escaped as text, and source links are generated locally from the
validated video IDs. Guidance remains conditional because investment horizon and
risk budget are unknown. For the latest available day, matching direct views
from successful structured summaries are displayed separately under each channel,
using the original summary view rather than a model-written merger. Without such
views, the existing text is labelled as model-organized and the structured-summary
gap is stated. Pro still supplies the week comparison, impact and conditional
analysis. This display-only change reuses validated paid analyses; the original
analysis/cache is retained and changed reports receive a revision. The exact
internal phrase `week_status=insufficient` is displayed as “周证据不足”.
Every model prose field is checked for explicit share
counts, trading amounts, target allocations, and profit guarantees (including
Chinese numerals), while price conditions and descriptions of current weights
remain allowed. Reports display states in Chinese and the ledger-read time in
Beijing time; cached timestamps remain UTC.

The workflow's final private step runs only when repository variable
`PORTFOLIO_GUIDANCE_ENABLED` equals `true` **and** the runner's local configuration is
enabled. It is placed after the public commit and Pages deployment. Private files
are never staged, uploaded as an artifact, or copied into `public/`. Adding this
code does not enable the variable or create a scheduled task; enablement and
pushing the feature require separate authorization.

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
HKUST_GZ_API_KEY=your_hkust_gz_api_key
HKUST_GZ_BASE_URL=https://gpt-api.hkust-gz.edu.cn/v1
```

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

## Schedule

The GitHub Actions workflow runs daily at:

- `10:00` China Standard Time / Taipei Time
- `02:00 UTC`

The cron expression is:

```yaml
0 2 * * *
```

The workflow also supports manual `workflow_dispatch`. Manual runs default to reusing the latest existing daily item; set `force=true` only when you intentionally want to reprocess the latest videos.

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
5. If captions still fail and this is not a dry run, try optional ASR through the HKUST-GZ OpenAI-compatible speech endpoint.
6. If text exists from either video, generate one Simplified Chinese merged digest through the configured OpenAI-compatible chat endpoint.
7. The merge prompt groups the same stock, index, sector, or macro event into one section and shows each creator's view, common points, and differences.
8. If text or LLM summary is unavailable, still publish a link-only daily RSS item containing both original video links.
9. Write `state.json`, `summaries.json`, and `public/feed.xml`.

RSS items include:

- title: one merged daily digest title
- link: hosted digest page, for example `https://a-l-an.github.io/youtube-daily-rss/items/daily-VIDEO_ID-VIDEO_ID.html`
- guid: merged digest ID built from the latest video IDs
- pubDate: digest processing time
- description and `content:encoded`: HTML digest
- categories: `YouTube Digest`, `Finance`, `Stock Market`, and source status

The digest is structured for scanning in an RSS reader. It includes one overall summary plus merged bullet sections for individual stocks, indices, sectors, and macro themes mentioned by either channel. If both creators discuss the same stock or sector, their views are shown in the same section rather than as two separate items. Original YouTube URLs are kept inside the digest body, but they are not the RSS item's primary link, so readers should open the text digest page first.

## Caption and ASR Limitations

Caption extraction depends on public YouTube transcript availability. Some videos have no captions, blocked captions, incomplete auto captions, or language tracks that do not match the configured preferences. The pipeline tries both `youtube-transcript-api` and `yt-dlp` subtitle extraction before falling back to audio ASR.

ASR is optional and only runs when:

- `asr.enabled` is `true`
- `HKUST_GZ_API_KEY` is set
- `ffmpeg` is installed
- `yt-dlp` can technically access the video audio
- using ASR is appropriate for your deployment and use case

If direct YouTube audio extraction fails or is not appropriate, ASR is skipped or marked failed and the RSS item falls back to the original video link.

## GitHub Pages Deployment

1. Push this project to a GitHub repository.
2. Register a macOS self-hosted runner for the repository.
   - Required labels: `self-hosted`, `macOS`, `youtube-rss`
   - Keep the Mac awake, online, and able to access YouTube at the scheduled time.
   - Install `ffmpeg` and GNU tar (`brew install gnu-tar`) and ensure `python`, `ffmpeg`, and `gtar` are available on the runner service PATH.
3. In repository settings, add the secret `HKUST_GZ_API_KEY`.
4. Optionally add repository variables:
   - `HKUST_GZ_BASE_URL`: `https://gpt-api.hkust-gz.edu.cn/v1`
   - `SUMMARY_MODEL`: `DeepSeek-V4-Pro`
   - `ASR_MODEL`: `whisper-1`
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
- Environment variable: `HKUST_GZ_API_KEY`

Cloudflare Pages scheduled builds are not as direct as GitHub Actions cron. A practical setup is to keep GitHub Actions generating and committing `public/feed.xml`, then let Cloudflare Pages deploy from the repository.

## Files

- `scripts/fetch_youtube.py`: YouTube Atom feed fetcher
- `scripts/transcript.py`: public transcript/caption extraction
- `scripts/ai_client.py`: HKUST-GZ/OpenAI-compatible credential and client helpers
- `scripts/asr.py`: optional ASR fallback
- `scripts/summarize.py`: digest generation and HTML rendering
- `scripts/rss.py`: RSS 2.0 writer
- `scripts/main.py`: CLI orchestration
- `state.json`: processed video state
- `summaries.json`: stored RSS digest items
- `public/feed.xml`: generated feed

## Notes

The generated summary is for information organization only and is not investment advice. The prompt asks the model to avoid unsupported claims, separate creator opinions from factual market information, preserve ticker symbols and company names, and call out uncertainty.

# Daily YouTube Finance Digest RSS

This project builds a daily RSS 2.0 feed from the latest public videos of:

- 视野环球财经 / `@RhinoFinance`
- NaNa说美股 / `@NaNaShuoMeiGu`

It checks YouTube Atom feeds, tries public captions first, optionally falls back to ASR, summarizes available text with an OpenAI-compatible model, and writes `public/feed.xml`.

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

The workflow also supports manual `workflow_dispatch`.

## Configuration

Main settings live in `config.yml`.

The YouTube fetcher uses official channel Atom feeds:

```text
https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
```

No YouTube API key is required for this.

## Pipeline Behavior

For each channel, the pipeline processes only the latest public video unless `--force` is used.

Processing order:

1. Fetch latest video from the channel Atom feed.
2. Skip if the video ID already exists in `state.json`.
3. Try captions with `youtube-transcript-api`.
4. If captions fail and this is not a dry run, try optional ASR through the HKUST-GZ OpenAI-compatible speech endpoint.
5. If text exists, generate a Simplified Chinese digest through the configured OpenAI-compatible chat endpoint.
6. If text or LLM summary is unavailable, still publish a link-only RSS item.
7. Write `state.json`, `summaries.json`, and `public/feed.xml`.

RSS items include:

- title: `[频道名] 视频标题`
- link: original YouTube URL
- guid: YouTube video ID
- pubDate: original video published time when available
- description: HTML digest
- categories: `YouTube Digest`, `Finance`, `Stock Market`, and source status

The digest is structured for scanning in an RSS reader. It includes an overall summary plus grouped bullet sections for individual stocks, indices, sectors, and macro themes mentioned in the video.

## Caption and ASR Limitations

Caption extraction depends on public YouTube transcript availability. Some videos have no captions, blocked captions, incomplete auto captions, or language tracks that do not match the configured preferences.

ASR is optional and only runs when:

- `asr.enabled` is `true`
- `HKUST_GZ_API_KEY` is set
- `ffmpeg` is installed
- `yt-dlp` can technically access the video audio
- using ASR is appropriate for your deployment and use case

If direct YouTube audio extraction fails or is not appropriate, ASR is skipped or marked failed and the RSS item falls back to the original video link.

## GitHub Pages Deployment

1. Push this project to a GitHub repository.
2. In repository settings, add the secret `HKUST_GZ_API_KEY`.
3. Optionally add repository variables:
   - `HKUST_GZ_BASE_URL`: `https://gpt-api.hkust-gz.edu.cn/v1`
   - `SUMMARY_MODEL`: `DeepSeek-V4-Pro`
   - `ASR_MODEL`: `whisper-1`
4. Go to **Settings -> Pages** and set the source to **GitHub Actions**.
5. Run **Daily YouTube RSS Digest** manually once from the Actions tab.
6. Subscribe to:

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

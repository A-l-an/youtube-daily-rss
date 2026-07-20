from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import sys
import tempfile
from typing import Optional
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fetch_youtube
import main as main_module
import rss as rss_module
import summarize as summarize_module
from fetch_youtube import Video
from summarize import SummaryResult


def atom_entry(video_id: str, title: str, published: Optional[str]) -> str:
    published_xml = f"<published>{published}</published>" if published is not None else ""
    return f"""
  <entry>
    <id>yt:video:{video_id}</id>
    <yt:videoId>{video_id}</yt:videoId>
    <title>{title}</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>
    {published_xml}
  </entry>"""


def atom_feed(*entries: str) -> bytes:
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<feed xmlns:yt=\"http://www.youtube.com/xml/schemas/2015\" "
        "xmlns=\"http://www.w3.org/2005/Atom\"><title>Example</title>"
        + "".join(entries)
        + "</feed>"
    ).encode()


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200
        self.headers = {"content-type": "application/atom+xml"}


def video(video_id: str, published: str = "2026-07-20T00:00:00+00:00") -> Video:
    return Video(
        channel_name=f"Channel {video_id}",
        channel_id=f"UC{video_id}",
        handle=f"@{video_id}",
        video_id=video_id,
        title=f"Video {video_id}",
        url=f"https://www.youtube.com/watch?v={video_id}",
        published=published,
    )


def record(source: Video) -> dict:
    return {
        **source.to_dict(),
        "source_status": "captions",
        "content_source": "YouTube captions",
        "transcript_status": "success",
        "transcript_language": "zh",
        "transcript_source": "youtube_transcript_api",
        "transcript_error": None,
        "asr_status": "skipped",
        "asr_model": None,
        "asr_error": None,
        "text": "content",
    }


class HistoricalAtomSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.channel = {"name": "Example", "channel_id": "UCexample", "handle": "@example"}

    def test_selects_latest_entry_before_local_next_midnight(self) -> None:
        feed = atom_feed(
            atom_entry("after", "After cutoff", "2026-07-20T16:00:00+00:00"),
            atom_entry("old", "Older", "2026-07-19T00:00:00+00:00"),
            atom_entry("chosen", "Chosen", "2026-07-20T15:59:59+00:00"),
        )
        with patch.object(fetch_youtube.requests, "get", return_value=FakeResponse(feed)):
            selected = fetch_youtube.fetch_video_for_report_date(
                self.channel, date(2026, 7, 20), "Asia/Shanghai"
            )
        self.assertEqual(selected.video_id, "chosen")
        self.assertEqual(selected.published, "2026-07-20T15:59:59+00:00")

    def test_any_untrusted_published_timestamp_fails_closed(self) -> None:
        feed = atom_feed(
            atom_entry("unknown", "Unknown date", None),
            atom_entry("old", "Older", "2026-07-19T00:00:00+00:00"),
        )
        with patch.object(fetch_youtube.requests, "get", return_value=FakeResponse(feed)):
            with self.assertRaisesRegex(RuntimeError, "Untrusted Atom entry"):
                fetch_youtube.fetch_video_for_report_date(
                    self.channel, date(2026, 7, 20), "Asia/Shanghai"
                )

    def test_timestamp_without_timezone_fails_closed(self) -> None:
        feed = atom_feed(atom_entry("naive", "Naive", "2026-07-20T12:00:00"))
        with patch.object(fetch_youtube.requests, "get", return_value=FakeResponse(feed)):
            with self.assertRaisesRegex(RuntimeError, "has no timezone"):
                fetch_youtube.fetch_video_for_report_date(
                    self.channel, date(2026, 7, 20), "Asia/Shanghai"
                )

    def test_invalid_timestamp_fails_closed(self) -> None:
        feed = atom_feed(atom_entry("invalid", "Invalid", "not-a-timestamp"))
        with patch.object(fetch_youtube.requests, "get", return_value=FakeResponse(feed)):
            with self.assertRaisesRegex(RuntimeError, "invalid published timestamp"):
                fetch_youtube.fetch_video_for_report_date(
                    self.channel, date(2026, 7, 20), "Asia/Shanghai"
                )

    def test_no_entry_before_cutoff_fails(self) -> None:
        feed = atom_feed(atom_entry("after", "After cutoff", "2026-07-21T00:00:00+00:00"))
        with patch.object(fetch_youtube.requests, "get", return_value=FakeResponse(feed)):
            with self.assertRaisesRegex(RuntimeError, "No Atom entry"):
                fetch_youtube.fetch_video_for_report_date(
                    self.channel, date(2026, 7, 20), "Asia/Shanghai"
                )


class ReportDateBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "timezone": "Asia/Shanghai",
            "channels": [{"name": "A", "channel_id": "UCA"}, {"name": "B", "channel_id": "UCB"}],
            "summary": {},
            "rss": {
                "title": "Test feed",
                "description": "Test description",
                "link": "https://example.test/feed.xml",
                "output_path": "public/feed.xml",
            },
        }
        summary_data = {"title": "2026-07-21 — 模型标题"}
        self.summary = SummaryResult(
            status="success",
            summary_html=summarize_module._render_daily_structured_digest(
                [record(video("a")), record(video("b"))],
                "YouTube captions",
                summary_data,
            ),
            model="model",
            summary_data=summary_data,
        )

    def test_only_strictly_past_local_dates_are_allowed(self) -> None:
        now = datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(
            main_module.parse_ended_report_date("2026-07-20", self.config, now),
            date(2026, 7, 20),
        )
        for invalid in ("2026-07-21", "2026-07-22", "2026-7-20", "2026-02-30"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                main_module.parse_ended_report_date(invalid, self.config, now)

    def test_dated_item_has_stable_id_date_title_and_last_local_instant(self) -> None:
        with patch.object(main_module, "utc_now_iso", return_value="2026-07-21T03:04:05+00:00"):
            item = main_module.build_daily_item(
                main_module.report_date_digest_id(date(2026, 7, 20)),
                [record(video("a")), record(video("b"))],
                self.summary,
                self.config,
                report_date=date(2026, 7, 20),
                published=main_module.report_date_published_iso(date(2026, 7, 20), self.config),
            )
        self.assertEqual(item["digest_id"], "daily-date-2026-07-20")
        self.assertEqual(item["report_date"], "2026-07-20")
        self.assertEqual(item["title"], "2026-07-20 模型标题")
        self.assertEqual(item["summary_data"]["title"], "2026-07-20 模型标题")
        self.assertTrue(item["summary_html"].startswith("<h2>2026-07-20 模型标题</h2>"))
        self.assertNotIn("2026-07-21", item["summary_html"])
        self.assertEqual(item["published"], "2026-07-20T15:59:59.999999+00:00")
        self.assertEqual(item["processed_at"], "2026-07-21T03:04:05+00:00")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feed_path = rss_module.write_rss(self.config, [item], root)
            tree = ET.parse(feed_path)
            description = tree.findtext("./channel/item/description") or ""
            encoded = tree.findtext(
                f"./channel/item/{{{rss_module.CONTENT_NS}}}encoded"
            ) or ""
            item_page = (
                root / "public" / "items" / "daily-date-2026-07-20.html"
            ).read_text(encoding="utf-8")

        for rendered in (description, encoded, item_page):
            self.assertIn("2026-07-20 模型标题", rendered)
            self.assertNotIn("2026-07-21", rendered)

    def test_dated_link_only_and_failed_renderers_receive_controlled_title(self) -> None:
        rendered = summarize_module.render_daily_link_only_digest(
            [record(video("a")), record(video("b"))],
            reason="No usable transcript.",
        )
        for status in ("link_only", "failed"):
            with self.subTest(status=status), patch.object(
                main_module, "local_date_label", return_value="2026-07-21"
            ):
                item = main_module.build_daily_item(
                    "daily-date-2026-07-20",
                    [record(video("a")), record(video("b"))],
                    SummaryResult(status=status, summary_html=rendered, error_message="failure"),
                    self.config,
                    report_date=date(2026, 7, 20),
                    published=main_module.report_date_published_iso(date(2026, 7, 20), self.config),
                )
            self.assertTrue(item["summary_html"].startswith("<h2>2026-07-20 美股视频融合摘要</h2>"))
            self.assertNotIn("每日美股视频融合摘要", item["summary_html"])
            self.assertIsNone(item["summary_data"])

    def test_retitle_refuses_html_outside_daily_renderer_contract(self) -> None:
        result = SummaryResult(status="failed", summary_html="<p>No controlled heading</p>")
        with self.assertRaisesRegex(ValueError, "controlled <h2>"):
            summarize_module.retitle_rendered_daily_summary(result, "2026-07-20 Title")

    def test_default_item_behavior_and_pair_id_are_unchanged(self) -> None:
        sources = [video("a"), video("b")]
        summary = SummaryResult(
            status="success",
            summary_html="<h2>Original title</h2><p>x</p>",
            summary_data={"title": "Original title"},
        )
        with patch.object(main_module, "utc_now_iso", return_value="2026-07-21T03:04:05+00:00"):
            item = main_module.build_daily_item(
                main_module.daily_digest_id(sources), [record(source) for source in sources], summary, self.config
            )
        self.assertEqual(item["digest_id"], "daily-a-b")
        self.assertEqual(item["title"], "Original title")
        self.assertEqual(item["published"], item["processed_at"])
        self.assertNotIn("report_date", item)

    def test_dated_state_does_not_replace_existing_video_mapping(self) -> None:
        state = {
            "processed_videos": {"a": {"daily_digest_id": "daily-old", "sentinel": True}},
            "daily_digests": {},
        }
        main_module.mark_state_processed(
            state,
            "daily-date-2026-07-20",
            [record(video("a")), record(video("b"))],
            self.summary,
            "2026-07-21T03:04:05+00:00",
            report_date=date(2026, 7, 20),
            preserve_existing_video_links=True,
        )
        self.assertEqual(state["processed_videos"]["a"], {"daily_digest_id": "daily-old", "sentinel": True})
        self.assertEqual(state["processed_videos"]["b"]["daily_digest_id"], "daily-date-2026-07-20")
        self.assertEqual(state["processed_videos"]["b"]["report_date"], "2026-07-20")
        self.assertEqual(state["daily_digests"]["daily-date-2026-07-20"]["report_date"], "2026-07-20")

    def test_upsert_replaces_same_date_without_duplicate(self) -> None:
        summaries = {"items": []}
        main_module.upsert_daily_summary(
            summaries, {"digest_id": "daily-date-2026-07-20", "title": "first"}
        )
        main_module.upsert_daily_summary(
            summaries, {"digest_id": "daily-date-2026-07-20", "title": "second"}
        )
        self.assertEqual(summaries["items"], [{"digest_id": "daily-date-2026-07-20", "title": "second"}])

    def test_existing_dated_item_non_force_is_zero_write_and_zero_fetch(self) -> None:
        state = {"last_run_at": "old", "processed_videos": {}, "daily_digests": {}}
        summaries = {
            "items": [{"item_type": "daily_digest", "digest_id": "daily-date-2026-07-20", "video_id": "daily-date-2026-07-20"}]
        }
        args = argparse.Namespace(dry_run=False, force=False, report_date="2026-07-20")
        with (
            patch.object(main_module, "load_config", return_value=self.config),
            patch.object(main_module, "parse_ended_report_date", return_value=date(2026, 7, 20)),
            patch.object(main_module, "load_json", side_effect=[state, summaries]),
            patch.object(main_module, "fetch_video_for_report_date") as fetch_mock,
            patch.object(main_module, "save_json") as save_mock,
            patch.object(main_module, "write_rss") as rss_mock,
        ):
            self.assertEqual(main_module.run(args), 0)
        fetch_mock.assert_not_called()
        save_mock.assert_not_called()
        rss_mock.assert_not_called()

    def test_existing_default_pair_is_zero_write(self) -> None:
        sources = [video("a"), video("b")]
        state = {
            "last_run_at": "old",
            "processed_videos": {"a": {}, "b": {}},
            "daily_digests": {"daily-a-b": {}},
        }
        summaries = {
            "items": [{"item_type": "daily_digest", "digest_id": "daily-a-b", "video_id": "daily-a-b"}]
        }
        args = argparse.Namespace(dry_run=False, force=False, report_date=None)
        with (
            patch.object(main_module, "load_config", return_value=self.config),
            patch.object(main_module, "load_json", side_effect=[state, summaries]),
            patch.object(main_module, "fetch_latest_video", side_effect=sources),
            patch.object(main_module, "collect_video_content") as collect_mock,
            patch.object(main_module, "save_json") as save_mock,
            patch.object(main_module, "write_rss") as rss_mock,
        ):
            self.assertEqual(main_module.run(args), 0)
        collect_mock.assert_not_called()
        save_mock.assert_not_called()
        rss_mock.assert_not_called()

    def test_dated_mode_requires_every_channel_and_writes_nothing_on_failure(self) -> None:
        state = {"last_run_at": "old", "processed_videos": {}, "daily_digests": {}}
        summaries = {"items": []}
        args = argparse.Namespace(dry_run=False, force=False, report_date="2026-07-20")
        with (
            patch.object(main_module, "load_config", return_value=self.config),
            patch.object(main_module, "parse_ended_report_date", return_value=date(2026, 7, 20)),
            patch.object(main_module, "load_json", side_effect=[state, summaries]),
            patch.object(
                main_module,
                "fetch_video_for_report_date",
                side_effect=[video("a"), RuntimeError("no trusted entry")],
            ),
            patch.object(main_module, "save_json") as save_mock,
            patch.object(main_module, "write_rss") as rss_mock,
        ):
            self.assertEqual(main_module.run(args), 1)
        save_mock.assert_not_called()
        rss_mock.assert_not_called()


class WorkflowDispatchTests(unittest.TestCase):
    def test_report_date_is_passed_through_env_and_quoted_argv(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
        self.assertIn("report_date:", workflow)
        self.assertIn("WORKFLOW_REPORT_DATE: ${{ inputs.report_date }}", workflow)
        self.assertIn('args+=(--report-date "$WORKFLOW_REPORT_DATE")', workflow)
        self.assertIn('.venv-ci/bin/python scripts/main.py "${args[@]}"', workflow)
        self.assertNotIn("scripts/main.py --report-date ${{ inputs.report_date }}", workflow)


if __name__ == "__main__":
    unittest.main()

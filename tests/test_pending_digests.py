from __future__ import annotations

import argparse
from datetime import date
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import main
from fetch_youtube import Video
from summarize import SummaryResult


def video(identifier):
    return Video('Channel ' + identifier, 'UC' + identifier, '@' + identifier, identifier,
                 'Original ' + identifier, 'https://www.youtube.com/watch?v=' + identifier,
                 '2026-11-09T00:00:00+00:00')


def source_record(source, available=True):
    return {**source.to_dict(), 'source_status': 'captions' if available else 'link-only',
            'content_source': 'YouTube captions' if available else 'metadata only',
            'transcript_status': 'success' if available else 'failed',
            'asr_status': 'skipped' if available else 'failed',
            'asr_error': None if available else 'xiemac ASR: remote_unavailable',
            'text': 'Original transcript ' + source.video_id if available else ''}


class PendingDigestTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = {'channels': [{'name': 'A'}, {'name': 'B'}],
                       'asr': {'cache_dir': str(self.root / 'cache')}, 'summary': {},
                       'rss': {'output_path': 'public/feed.xml', 'title': 'Test'}}
        self.latest = [video('a'), video('b')]
        self.available = {'a'}
        self.collected = []
        self.summary_calls = []
        self.summary_status = 'success'
        self.enterContext(patch.object(main, 'ROOT', self.root))
        self.enterContext(patch.object(main, 'STATE_PATH', self.root / 'state.json'))
        self.enterContext(patch.object(main, 'SUMMARIES_PATH', self.root / 'summaries.json'))
        self.enterContext(patch.object(main, 'load_config', return_value=self.config))
        self.enterContext(patch.object(main, 'load_dotenv'))
        self.enterContext(patch.object(main, 'fetch_latest_video', side_effect=lambda channel: self.latest[0 if channel['name'] == 'A' else 1]))
        self.enterContext(patch.object(main, 'collect_video_content', side_effect=self.collect))
        self.enterContext(patch.object(main, 'summarize_daily_digest', side_effect=self.summarize))
        self.seed_legacy()

    def collect(self, source, config, dry_run):
        self.collected.append(source)
        return source_record(source, source.video_id in self.available)

    def summarize(self, records, config):
        self.summary_calls.append(deepcopy(records))
        return SummaryResult(self.summary_status, '<h2>Useful title</h2><p>Useful analysis</p>',
                             model='test', error_message='summary failure' if self.summary_status == 'failed' else None,
                             summary_data={'title': 'Useful title'})

    def seed_legacy(self):
        records = [source_record(v, v.video_id in self.available) for v in self.latest]
        item = {'item_type': 'daily_digest', 'digest_id': 'daily-a-b', 'video_id': 'daily-a-b',
                'source_video_ids': ['a', 'b'], 'source_videos': [{k: v for k, v in r.items() if k != 'text'} for r in records],
                'published': '2026-11-10T02:00:00+00:00', 'processed_at': '2026-11-10T02:00:00+00:00',
                'summary_status': 'success', 'summary_model': 'test',
                'summary_data': {'title': 'Useful title'}, 'title': 'Useful title',
                'summary_html': '<h2>Useful title</h2><p>Existing useful analysis</p>'}
        main.save_json(main.SUMMARIES_PATH, {'items': [item]})
        main.save_json(main.STATE_PATH, {'processed_videos': {'a': {}, 'b': {}},
                                       'daily_digests': {'daily-a-b': {'summary_status': 'success'}}})

    def run_daily(self, **kwargs):
        return main.run(argparse.Namespace(dry_run=False, force=False, report_date=None, **kwargs))

    def items(self):
        return {i['digest_id']: i for i in json.loads(main.SUMMARIES_PATH.read_text())['items']}

    def test_existing_partial_retries_without_force_preserving_guid_and_publication(self):
        self.available.add('b')
        self.assertEqual(self.run_daily(), 0)
        item = self.items()['daily-a-b']
        self.assertEqual(len(self.summary_calls), 1)
        self.assertEqual(item['published'], '2026-11-10T02:00:00+00:00')
        self.assertEqual(item['completion_status'], 'complete')
        self.assertEqual(item['coverage_status'], 'complete')
        self.assertEqual(item['video_id'], 'daily-a-b')

    def test_repeated_missing_source_keeps_summary_without_duplicate_model_call(self):
        self.run_daily()
        self.run_daily()
        self.assertEqual(self.summary_calls, [])
        self.assertEqual([v.video_id for v in self.collected], ['a', 'b', 'b'])
        item = self.items()['daily-a-b']
        self.assertEqual(item['completion_status'], 'pending')
        self.assertEqual(item['summary_status'], 'success')
        self.assertIn('Existing useful analysis', item['summary_html'])
        self.assertIn('转录服务暂不可用', item['summary_html'])
        self.assertIn('正文待补齐', item['summary_html'])
        state = json.loads(main.STATE_PATH.read_text())
        self.assertEqual(state['daily_digests']['daily-a-b']['completion_status'], 'pending')
        page = self.root / 'public/items/daily-a-b.html'
        self.assertIn('转录服务暂不可用', page.read_text())

    def test_complete_digest_skips_all_content_and_model_work(self):
        self.available.add('b')
        self.seed_legacy()
        original = main.SUMMARIES_PATH.read_bytes()
        self.run_daily()
        self.assertEqual(self.collected, [])
        self.assertEqual(self.summary_calls, [])
        self.assertEqual(main.SUMMARIES_PATH.read_bytes(), original)

    def test_pending_older_digest_uses_original_videos_after_latest_advances(self):
        self.run_daily()
        self.latest = [video('c'), video('d')]
        self.available.update({'b', 'c', 'd'})
        self.collected.clear()
        self.run_daily()
        items = self.items()
        self.assertEqual(set(items), {'daily-a-b', 'daily-c-d'})
        self.assertEqual(items['daily-a-b']['completion_status'], 'complete')
        self.assertEqual(items['daily-a-b']['published'], '2026-11-10T02:00:00+00:00')
        self.assertEqual(items['daily-a-b']['source_video_ids'], ['a', 'b'])
        self.assertEqual([v.video_id for v in self.collected], ['c', 'd', 'b'])
        self.assertEqual(self.collected[-1], video('b'))
        self.assertEqual(json.loads(main.STATE_PATH.read_text())['daily_digests']['daily-a-b']['completion_status'], 'complete')

    def test_latest_legacy_incomplete_is_recovered_but_archive_is_not_backfilled(self):
        summaries = json.loads(main.SUMMARIES_PATH.read_text())
        old = deepcopy(summaries['items'][0])
        old.update(digest_id='daily-old', video_id='daily-old', published='2026-10-01T00:00:00+00:00',
                   source_video_ids=['old'], source_videos=[{k: v for k, v in source_record(video('old'), False).items() if k != 'text'}])
        summaries['items'].append(old)
        main.save_json(main.SUMMARIES_PATH, summaries)
        self.latest = [video('c'), video('d')]
        self.available.update({'b', 'c', 'd'})
        self.run_daily()
        self.assertEqual(self.items()['daily-a-b']['completion_status'], 'complete')
        self.assertEqual(self.items()['daily-old'], old)
        self.assertNotIn('old', [v.video_id for v in self.collected])

    def test_failed_summary_retries_even_with_complete_source_coverage(self):
        self.available.add('b')
        self.summary_status = 'failed'
        self.run_daily()
        self.assertEqual(self.items()['daily-a-b']['completion_status'], 'pending')
        self.assertIn('Existing useful analysis', self.items()['daily-a-b']['summary_html'])
        self.assertIn('本次摘要更新失败，已保留已有分析，稍后重试', self.items()['daily-a-b']['summary_html'])
        self.assertNotIn('摘要暂未生成', self.items()['daily-a-b']['summary_html'])
        self.summary_status = 'success'
        self.run_daily()
        self.assertEqual(len(self.summary_calls), 2)
        self.assertEqual(self.items()['daily-a-b']['completion_status'], 'complete')

    def test_forced_failed_retry_does_not_degrade_complete_summary(self):
        self.available.add('b')
        self.seed_legacy()
        previous = self.items()['daily-a-b']
        self.available.clear()
        main.run(argparse.Namespace(dry_run=False, force=True, report_date=None))
        item = self.items()['daily-a-b']
        self.assertEqual(item['source_videos'], previous['source_videos'])
        self.assertEqual(item['summary_html'], previous['summary_html'])
        self.assertEqual(item['published'], previous['published'])
        self.assertEqual(self.summary_calls, [])

    def test_dry_run_does_not_write_outputs_or_cache(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        main.run(argparse.Namespace(dry_run=True, force=False, report_date=None))
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(self.summary_calls, [])

    def test_failed_forced_model_retry_retains_complete_digest(self):
        self.available.add('b')
        self.seed_legacy()
        previous = self.items()['daily-a-b']
        self.summary_status = 'failed'
        main.run(argparse.Namespace(dry_run=False, force=True, report_date=None))
        self.assertEqual(self.items()['daily-a-b'], previous)

    def test_complete_digest_skips_even_without_processed_video_index(self):
        self.available.add('b')
        self.seed_legacy()
        main.save_json(main.STATE_PATH, {})
        self.run_daily()
        self.assertEqual(self.collected, [])
        self.assertEqual(self.summary_calls, [])

    def test_dated_pending_retry_reconstructs_original_records_without_discovery(self):
        summaries = json.loads(main.SUMMARIES_PATH.read_text())
        item = summaries['items'][0]
        item.update(digest_id='daily-date-2026-11-09', video_id='daily-date-2026-11-09', report_date='2026-11-09')
        main.save_json(main.SUMMARIES_PATH, summaries)
        self.available.add('b')
        with patch.object(main, 'fetch_video_for_report_date', side_effect=RuntimeError('expired Atom entry')):
            status = main.run(argparse.Namespace(dry_run=False, force=False, report_date='2026-11-09'))
        self.assertEqual(status, 0)
        item = self.items()['daily-date-2026-11-09']
        self.assertEqual(item['completion_status'], 'complete')
        self.assertEqual(item['published'], '2026-11-10T02:00:00+00:00')

    def test_partial_discovery_does_not_create_a_complete_single_source_digest(self):
        self.latest = [video('c'), video('d')]
        self.available.update({'c', 'd'})
        with patch.object(main, 'fetch_latest_video', side_effect=[video('c'), RuntimeError('feed unavailable')]):
            self.run_daily()
        self.assertNotIn('daily-c', self.items())
        self.assertEqual(self.summary_calls, [])

    def test_equal_attempt_times_choose_only_one_recent_registered_pending(self):
        self.run_daily()
        summaries = json.loads(main.SUMMARIES_PATH.read_text())
        older = deepcopy(summaries['items'][0])
        older.update(digest_id='daily-old', video_id='daily-old', published='2026-10-01T00:00:00+00:00',
                     source_video_ids=['old'], source_videos=[{k: v for k, v in source_record(video('old'), False).items() if k != 'text'}])
        summaries['items'].append(older)
        main.save_json(main.SUMMARIES_PATH, summaries)
        self.latest = [video('c'), video('d')]
        self.available.update({'c', 'd'})
        self.collected.clear()
        self.run_daily()
        self.assertEqual([v.video_id for v in self.collected], ['c', 'd', 'b'])

    def test_failed_partial_summary_retries_unchanged_input_and_recovers_before_source(self):
        summaries = json.loads(main.SUMMARIES_PATH.read_text())
        summaries['items'][0]['summary_status'] = 'failed'
        main.save_json(main.SUMMARIES_PATH, summaries)
        self.summary_status = 'failed'
        self.run_daily()
        self.run_daily()
        self.assertEqual(len(self.summary_calls), 2)
        self.assertIn('摘要暂未生成，稍后重试', self.items()['daily-a-b']['summary_html'])
        self.summary_status = 'success'
        self.run_daily()
        self.run_daily()
        self.assertEqual(len(self.summary_calls), 3)
        item = self.items()['daily-a-b']
        self.assertEqual(item['summary_status'], 'success')
        self.assertEqual(item['summary_attempt_status'], 'success')
        self.assertEqual(item['coverage_status'], 'pending')
        self.assertEqual(item['completion_status'], 'pending')
        self.assertEqual(item['video_id'], 'daily-a-b')
        self.assertEqual(item['published'], '2026-11-10T02:00:00+00:00')
        self.assertIn('Useful analysis', item['summary_html'])
        self.available.add('b')
        self.run_daily()
        self.assertEqual(len(self.summary_calls), 4)
        self.assertEqual(self.items()['daily-a-b']['completion_status'], 'complete')

    def test_failed_changed_partial_retries_while_preserving_previous_analysis(self):
        self.run_daily()
        source_path = next((self.root / 'cache/sources').glob('*.json'))
        source_path.unlink()
        self.summary_status = 'failed'
        original_collect = self.collect
        def changed(source, config, dry_run):
            record = original_collect(source, config, dry_run)
            if record['text']:
                record['text'] += ' corrected captions'
            return record
        with patch.object(main, 'collect_video_content', side_effect=changed):
            self.run_daily()
            self.run_daily()
        self.assertEqual(len(self.summary_calls), 2)
        item = self.items()['daily-a-b']
        self.assertIn('Existing useful analysis', item['summary_html'])
        self.assertEqual(item['summary_attempt_status'], 'failed')
        self.assertEqual(item['completion_status'], 'pending')
        self.summary_status = 'success'
        self.run_daily()
        self.run_daily()
        self.assertEqual(len(self.summary_calls), 3)
        item = self.items()['daily-a-b']
        self.assertEqual(item['summary_attempt_status'], 'success')
        self.assertIn('Useful analysis', item['summary_html'])
        self.assertNotIn('Existing useful analysis', item['summary_html'])
        self.assertEqual(item['completion_status'], 'pending')

    def test_force_retries_unchanged_successful_partial_summary(self):
        self.run_daily()
        main.run(argparse.Namespace(dry_run=False, force=True, report_date=None))
        self.assertEqual(len(self.summary_calls), 1)
        item = self.items()['daily-a-b']
        self.assertIn('Useful analysis', item['summary_html'])
        self.assertNotIn('Existing useful analysis', item['summary_html'])
        self.assertEqual(item['completion_status'], 'pending')
        self.assertEqual(item['published'], '2026-11-10T02:00:00+00:00')

    def test_failed_summary_with_missing_historical_text_still_retries(self):
        summaries = json.loads(main.SUMMARIES_PATH.read_text())
        summaries['items'][0]['summary_status'] = 'failed'
        main.save_json(main.SUMMARIES_PATH, summaries)
        self.available = {'b'}
        self.run_daily()
        self.assertEqual(len(self.summary_calls), 1)
        item = self.items()['daily-a-b']
        self.assertEqual(item['summary_status'], 'success')
        self.assertEqual(item['coverage_status'], 'pending')
        self.assertEqual(item['completion_status'], 'pending')

    def test_missing_previous_text_cannot_promote_stale_partial_summary_to_complete(self):
        self.available = {'b'}
        self.run_daily()
        self.run_daily()
        item = self.items()['daily-a-b']
        self.assertEqual(item['completion_status'], 'pending')
        self.assertEqual(self.summary_calls, [])
        self.assertIn('Existing useful analysis', item['summary_html'])
        self.assertIn('Channel a：转录服务暂不可用', item['summary_html'])

    def test_permanently_failing_newer_pending_does_not_starve_older_recoverable_digest(self):
        self.run_daily()
        summaries = json.loads(main.SUMMARIES_PATH.read_text())
        newer = summaries['items'][0]
        newer['processed_at'] = '2026-11-08T02:00:00+00:00'
        older = deepcopy(newer)
        older.update(digest_id='daily-e-f', video_id='daily-e-f',
                     published='2026-11-09T02:00:00+00:00', processed_at='2026-11-09T02:00:00+00:00',
                     source_video_ids=['e', 'f'], source_videos=[
                         {k: v for k, v in source_record(video(identifier), False).items() if k != 'text'}
                         for identifier in ('e', 'f')])
        summaries['items'].append(older)
        main.save_json(main.SUMMARIES_PATH, summaries)
        state = json.loads(main.STATE_PATH.read_text())
        state['daily_digests']['daily-a-b']['processed_at'] = newer['processed_at']
        state['daily_digests']['daily-a-b']['last_attempted_at'] = newer['processed_at']
        main.save_json(main.STATE_PATH, state)
        self.latest = [video('c'), video('d')]
        self.available.update({'c', 'd', 'e', 'f'})
        self.collected.clear()
        self.run_daily()
        self.assertEqual([source.video_id for source in self.collected], ['c', 'd', 'b'])
        self.collected.clear()
        self.run_daily()
        self.assertEqual([source.video_id for source in self.collected], ['e', 'f'])
        self.assertEqual(self.items()['daily-e-f']['completion_status'], 'complete')
        self.assertEqual(self.items()['daily-a-b']['completion_status'], 'pending')

    def test_public_coverage_counts_current_text_while_retaining_historical_success(self):
        self.available = {'b'}
        self.run_daily()
        item = self.items()['daily-a-b']
        self.assertEqual(item['source_videos'][0]['transcript_status'], 'success')
        self.assertEqual(item['coverage_status'], 'pending')
        self.assertIn('正文待补齐（1/2）', item['summary_html'])
        self.assertIn('转录服务暂不可用', item['summary_html'])
        self.assertNotIn('complete', item['summary_html'])
        self.assertNotIn('remote_unavailable', item['summary_html'])
        self.assertEqual(json.loads(main.STATE_PATH.read_text())['daily_digests']['daily-a-b']['coverage_status'], 'pending')

    def test_public_complete_text_with_failed_summary_has_chinese_status(self):
        self.available.add('b')
        self.summary_status = 'failed'
        self.run_daily()
        item = self.items()['daily-a-b']
        self.assertEqual(item['coverage_status'], 'complete')
        self.assertIn('正文已齐（2/2）', item['summary_html'])
        self.assertIn('摘要待更新', item['summary_html'])
        self.assertNotIn('pending', item['summary_html'])

    def test_public_common_failure_reasons_are_chinese_but_metadata_keeps_diagnostics(self):
        for error, explanation in (
            ('xiemac ASR: remote_unavailable', '转录服务暂不可用'),
            ('xiemac ASR: remote_busy', '转录服务繁忙'),
            ('xiemac ASR: timeout', '转录请求超时'),
            ('YouTube members-only video: access denied', '视频访问受限'),
        ):
            with self.subTest(error=error):
                def collect(source, config, dry_run):
                    record = self.collect(source, config, dry_run)
                    if source.video_id == 'b':
                        record['asr_error'] = error
                    return record
                with patch.object(main, 'collect_video_content', side_effect=collect):
                    self.run_daily()
                item = self.items()['daily-a-b']
                self.assertIn(explanation, item['summary_html'])
                self.assertNotIn(error, item['summary_html'])
                self.assertEqual(item['source_videos'][1]['asr_error'], error)


if __name__ == '__main__':
    unittest.main()

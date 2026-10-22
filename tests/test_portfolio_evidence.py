from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import portfolio_evidence as evidence
from transcript import TranscriptResult

TODAY = date(2026, 10, 23)


def video(identifier='abcdefghijk', channel='Channel A', status='captions'):
    return {'video_id': identifier, 'channel_name': channel, 'title': 'NVDA commentary',
            'source_status': status, 'transcript_status': 'success' if status == 'captions' else 'unavailable',
            'asr_status': 'success' if status == 'ASR' else 'failed', 'published': None}


def daily(day, sources=None, success=False):
    return {'published': f'{day}T02:00:00Z', 'summary_status': 'success' if success else 'failed',
            'source_videos': sources or [video()], 'summary_data': {
                'one_sentence_summary': 'DO NOT USE FAILED OR MERGED SUMMARY',
                'source_quality_note': 'ASR证券名有识别歧义，请核对原视频。',
                'asset_sections': [{'name': 'NVDA', 'type': 'stock', 'source_views': [
                    {'channel_name': 'Channel A', 'view': 'NVDA conditional earnings view', 'evidence': 'growth'},
                    {'channel_name': 'Channel B', 'view': 'unrelated company', 'evidence': 'risk'}]}]}}


class PortfolioEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.cache = Path(self.tmp).resolve() / 'cache'
        self.get = self.enterContext(patch.object(evidence, 'get_transcript', return_value=TranscriptResult(
            status='success', source='manual_caption', text='NVDA earnings from real captions.')))

    def build(self, items, **kwargs):
        return evidence.build_evidence({'items': items}, TODAY, self.cache, {}, **kwargs)

    def test_failed_summary_recovered_only_from_previously_successful_captions(self):
        result = self.build([daily('2026-10-21', [video(), video('ABCDEFGHIJK', 'Channel B', 'link-only')])])
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(result['sources'][0]['kind'], 'recovered_captions')
        self.assertNotIn('DO NOT USE', json.dumps(result))
        self.assertEqual(result['week_status'], 'insufficient')
        self.assertEqual(result['latest_date'], '2026-10-21')
        self.assertEqual(len(result['source_gaps']), 1)
        self.get.assert_called_once_with('abcdefghijk', {})

    def test_duplicate_video_earliest_date_no_week_inflation_and_7day_boundary(self):
        result = self.build([daily('2026-10-16'), daily('2026-10-17'), daily('2026-10-23'),
                             daily('2026-10-17', [video('12345678901')]), daily('2026-10-24', [video('12345678902')])])
        self.assertEqual([s['video_id'] for s in result['sources']], ['12345678901'])
        self.assertEqual(result['covered_dates'], ['2026-10-17'])
        self.assertEqual(result['latest_date'], '2026-10-17')
        self.assertEqual(len(result['missing_dates']), 6)
        self.get.assert_called_once()

    def test_success_summary_has_per_source_views_and_no_caption_fetch(self):
        result = self.build([daily('2026-10-22', [video(), video('ABCDEFGHIJK', 'Channel B', 'ASR')], True)])
        self.get.assert_not_called()
        self.assertEqual(len(result['sources']), 2)
        a = next(x for x in result['sources'] if x['channel'] == 'Channel A')
        b = next(x for x in result['sources'] if x['channel'] == 'Channel B')
        self.assertNotIn('unrelated company', a['content'])
        self.assertNotIn('conditional earnings', b['content'])
        self.assertEqual(result['latest_date'], '2026-10-22')
        self.assertEqual(b['source_status'], 'ASR')
        self.assertEqual(a['source_status'], 'captions')
        self.assertIn('ASR证券名有识别歧义', b['source_quality_note'])
        self.assertIn('ASR', b['content_source'])

    def test_cache_identity_hash_permissions_and_repeat_fetch(self):
        original = self.build([daily('2026-10-21')])
        repeat = self.build([daily('2026-10-21')])
        self.assertEqual(original, repeat)
        self.get.assert_called_once()
        target = self.cache / 'captions' / 'abcdefghijk.json'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        data = json.loads(target.read_text())
        data['text'] = 'corrupted'
        target.write_text(json.dumps(data))
        bad = self.build([daily('2026-10-21')])
        self.assertEqual(bad['sources'], [])
        self.assertEqual(bad['source_gaps'][0]['reason'], 'caption_cache_invalid')
        self.get.assert_called_once()

    def test_failed_cache_prevents_same_day_retry_and_hides_upstream_error(self):
        self.get.side_effect = RuntimeError('PRIVATE API KEY')
        first = self.build([daily('2026-10-21')])
        again = self.build([daily('2026-10-21')])
        self.assertEqual(first, again)
        self.get.assert_called_once()
        self.assertNotIn('PRIVATE', json.dumps(first))
        self.assertEqual(first['latest_date'], None)

    def test_existing_success_cache_contract_and_recover_false(self):
        evidence.private_directory(self.cache / 'captions')
        text = 'NVDA recovered by parent'
        evidence.write_private_json(self.cache / 'captions' / 'abcdefghijk.json', {
            'schema_version': 1, 'video_id': 'abcdefghijk', 'status': 'success', 'source': 'captions',
            'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'fetched_on': '2026-10-23'})
        result = self.build([daily('2026-10-21')], recover_captions=False)
        self.assertEqual(result['sources'][0]['content'], text)
        self.get.assert_not_called()

    def test_source_published_date_in_shanghai_takes_priority(self):
        source = video(); source['published'] = '2026-10-21T23:00:00Z'
        result = self.build([daily('2026-10-23', [source])])
        self.assertEqual(result['covered_dates'], ['2026-10-22'])
        self.assertEqual(result['sources'][0]['date_basis'], 'video_published')

    def test_reject_repo_or_symlink_cache_before_fetch(self):
        for cache in [evidence.REPO_ROOT / 'public' / 'private-cache', Path(self.tmp) / 'linked']:
            if cache.name == 'linked': cache.symlink_to(self.cache, target_is_directory=True)
            with self.assertRaises(evidence.PortfolioEvidenceError):
                evidence.build_evidence({'items': [daily('2026-10-21')]}, TODAY, cache, {})
        self.get.assert_not_called()

    def test_punctuated_no_opinion_placeholders_are_not_evidence(self):
        for text in ('未提及。', '未讨论！', '无。', ' 未表达观点； ', '未直接提及NVDA。'):
            item = daily('2026-10-22', success=True)
            item['summary_data']['asset_sections'][0]['source_views'][0]['view'] = text
            with self.subTest(text=text):
                result = self.build([item])
                self.assertEqual(result['sources'], [])
                self.assertEqual(result['covered_dates'], [])
        self.get.assert_not_called()

    def test_mixed_absence_retains_industry_view_without_direct_asset_scope(self):
        item = daily('2026-10-22', success=True)
        item['summary_data']['asset_sections'][0]['source_views'][0]['view'] = '未提及 NVDA，但讨论了行业资本开支。'
        result = self.build([item])
        self.assertEqual(len(result['sources']), 1)
        self.assertIn('行业资本开支', result['sources'][0]['content'])
        self.assertEqual(result['sources'][0]['direct_assets'], [])

    def test_direct_asset_scope_comes_from_non_placeholder_source_view(self):
        item = daily('2026-10-22', success=True)
        item['summary_data']['asset_sections'][0]['name'] = 'NVDA / 英伟达'
        item['summary_data']['asset_sections'][0]['source_views'][0]['view'] = '认为需求增长仍强，但盈利预期可能过高。'
        source = self.build([item])['sources'][0]
        self.assertEqual(source['direct_assets'], ['NVDA / 英伟达'])

    def test_targeted_absence_grammar_is_shared_by_summary_and_captions(self):
        for text in ('暂无对NVDA的具体观点。', '没有关于NVDA的评价。',
                     '未给出关于NVDA的明确观点。', '对NVDA未作出评价。',
                     '未对NVDA发表看法。', 'NVDA暂无具体讨论。'):
            item = daily('2026-10-22', success=True)
            item['summary_data']['asset_sections'][0]['source_views'][0]['view'] = text
            with self.subTest(text=text):
                self.assertEqual(self.build([item])['sources'], [])
                captions = {'kind': 'recovered_captions', 'content': text}
                self.assertFalse(evidence.supports_direct(captions, 'NVDA'))
                item['summary_data']['asset_sections'][0]['source_views'][0]['view'] = text.rstrip('。') + '，但讨论了行业资本开支。'
                source = self.build([item])['sources'][0]
                self.assertIn('行业资本开支', source['content'])
                self.assertEqual(source['direct_assets'], [])

    def test_negative_price_revision_is_not_absence_of_opinion(self):
        for text in ('没有上调NVDA目标价，仍维持持有观点。', '对NVDA没有观点分歧，均看好需求。'):
            item = daily('2026-10-22', success=True)
            item['summary_data']['asset_sections'][0]['source_views'][0]['view'] = text
            with self.subTest(text=text):
                source = self.build([item])['sources'][0]
                self.assertEqual(source['direct_assets'], ['NVDA'])

    def test_caption_only_absence_is_not_evidence_but_mixed_industry_text_remains(self):
        self.get.return_value = TranscriptResult(status='success', source='manual_caption', text='暂无对NVDA的具体观点。')
        result = self.build([daily('2026-10-22')])
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['covered_dates'], [])
        self.get.return_value = TranscriptResult(status='success', source='manual_caption',
                                                text='暂无对NVDA的具体观点，但讨论了行业资本开支。')
        result = self.build([daily('2026-10-22', [video('12345678901')])])
        self.assertIn('行业资本开支', result['sources'][0]['content'])
        self.assertFalse(evidence.supports_direct(result['sources'][0], 'NVDA'))

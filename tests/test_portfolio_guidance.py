from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import portfolio_guidance as guidance
import portfolio_evidence as evidence

TODAY = date(2026, 10, 23)


def portfolio():
    return {'schema_version': 1, 'as_of_date': TODAY.isoformat(), 'fetched_at': '2026-10-23T00:00:00Z',
            'brokers': ['futu', 'hsbc'], 'accounts': [
                {'alias': 'Futu-USD', 'broker': 'futu', 'currency': 'USD', 'positions_as_of': '2026-09-07',
                 'positions_source': 'CALCULATED', 'broker_verified': False},
                {'alias': 'HSBC-USD', 'broker': 'hsbc', 'currency': 'USD', 'positions_as_of': '2026-08-25',
                 'positions_source': 'CALCULATED', 'broker_verified': False}],
            'positions': [{'key': 'Futu-USD:NVDA:USD', 'account_alias': 'Futu-USD', 'broker': 'futu',
                'symbol': 'NVDA', 'currency': 'USD', 'direction': 'long', 'recorded_account_weight_pct': 25,
                'portfolio_security_weight_pct': None, 'cost_to_price_deviation_pct': None,
                'cost_status': 'unknown', 'positions_as_of': '2026-09-07', 'positions_source': 'CALCULATED',
                'quote_as_of': '2026-10-22', 'quote_is_stale': False, 'valuation_available': True}]}


def source_evidence():
    return {'schema_version': 1, 'window_start': '2026-10-17', 'window_end': '2026-10-23',
            'latest_date': '2026-10-22', 'covered_dates': ['2026-10-22'],
            'missing_dates': ['2026-10-17', '2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-23'],
            'week_status': 'insufficient', 'source_gaps': [], 'sources': [{
                'evidence_id': 'video:abcdefghijk', 'video_id': 'abcdefghijk', 'date': '2026-10-22',
                'date_basis': 'first_digest_date', 'channel': 'Synthetic channel', 'kind': 'summary',
                'content': 'NVDA data center spending risk.', 'content_truncated': False,
                'source_status': 'ASR', 'content_source': 'ASR transcription', 'direct_assets': ['NVDA'],
                'source_quality_note': '证券名称可能识别错误，核对原视频。',
                'url': 'https://www.youtube.com/watch?v=abcdefghijk'}]}


def analysis():
    return {'portfolio_overview': {'text': '信息有限，先核对风险预算。', 'evidence_ids': ['video:abcdefghijk']},
            'week_overview': {'status': 'insufficient', 'text': '仅覆盖最新可用日，无法推断完整周趋势。',
                              'evidence_ids': ['video:abcdefghijk']},
            'positions': [{'key': 'Futu-USD:NVDA:USD', 'coverage': 'direct', 'status': 'observe',
                'latest_day_view': '数据中心投资存在风险。', 'week_view': '缺少跨日对照。',
                'impact': '可能影响估值。', 'conditional_action': '若增长预期下修，再评估减仓。',
                'conditions': ['先明确投资期限'], 'risks': ['估值风险'], 'invalidation_conditions': ['增长继续上修'],
                'evidence_ids': ['video:abcdefghijk'], 'latest_evidence_ids': ['video:abcdefghijk'],
                'week_evidence_ids': ['video:abcdefghijk']}]}


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.config = self.tmp / 'config.json'
        self.config_data = {'enabled': True, 'source': {}, 'output_dir': str(self.tmp / 'output'),
                            'cache_dir': str(self.tmp / 'cache'), 'holdings_confirmed_at': '2026-10-23',
                            'model': 'deepseek-v4-pro'}
        self.save_config()
        self.fetch = self.enterContext(patch.object(guidance, 'fetch_portfolio', return_value=portfolio()))
        self.build = self.enterContext(patch.object(guidance, 'build_evidence', return_value=source_evidence()))
        self.enterContext(patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only', 'SUMMARY_MODEL': 'deepseek-flash'}, clear=True))
        self.requests = []
        self.answer = analysis()
        self.finish = 'stop'
        self.status = 200
        self.response_model = 'deepseek-v4-pro'
        def handle(request):
            self.requests.append(request)
            if self.status != 200:
                return httpx.Response(self.status, json={'error': {'message': 'SECRET PRIVATE HOLDINGS'}})
            return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion', 'created': 1,
                'model': self.response_model, 'choices': [{'index': 0, 'finish_reason': self.finish,
                'message': {'role': 'assistant', 'content': json.dumps(self.answer) if self.answer is not None else '',
                            'reasoning_content': 'DO NOT SAVE REASONING'}}]})
        def client(credentials):
            c = OpenAI(api_key=credentials.api_key, base_url=credentials.base_url,
                       http_client=httpx.Client(transport=httpx.MockTransport(handle)))
            self.addCleanup(c.close)
            return c
        self.enterContext(patch.object(guidance, 'make_openai_compatible_client', client))

    def save_config(self):
        self.config.write_text(json.dumps(self.config_data)); self.config.chmod(0o600)

    def run_guidance(self, **kwargs):
        return guidance.run(self.config, TODAY, **kwargs)

    def test_fixed_pro_request_and_private_report_truthful_dates(self):
        result = self.run_guidance()
        self.assertEqual(result['status'], 'created')
        body = json.loads(self.requests[0].content)
        self.assertEqual(body['model'], 'deepseek-v4-pro')
        self.assertEqual(body['thinking'], {'type': 'enabled'})
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertEqual(body['max_tokens'], 32768)
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(str(self.requests[0].url), 'https://api.deepseek.com/v1/chat/completions')
        target = Path(result['path'])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        report = target.read_text()
        for expected in ['最新可用观点日期：2026-10-22', '持仓人工确认日期：2026-10-23', '证据不足', '2026-09-07',
                         'https://www.youtube.com/watch?v=abcdefghijk', '未知', '语音转写', '证券名称可能识别错误']:
            self.assertIn(expected, report)
        self.assertNotIn('DO NOT SAVE REASONING', report)
        self.assertFalse(target.is_relative_to(evidence.REPO_ROOT))

    def test_missing_disabled_and_check_only_do_not_call_model(self):
        self.assertEqual(guidance.run(self.tmp / 'missing.json', TODAY)['status'], 'skipped')
        self.config_data['enabled'] = False; self.save_config()
        self.assertEqual(self.run_guidance()['status'], 'skipped')
        self.fetch.assert_not_called()
        self.config_data['enabled'] = True; self.save_config()
        self.assertEqual(self.run_guidance(check_only=True)['status'], 'checked')
        self.assertEqual(self.requests, [])
        self.assertEqual(list((self.tmp / 'output').glob('*.md')), [])

    def test_model_override_invalid_json_length_unknown_refs_and_missing_position_fail(self):
        self.config_data['model'] = 'deepseek-flash'; self.save_config()
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_must_be_pro$'): self.run_guidance()
        self.config_data['model'] = 'deepseek-v4-pro'; self.save_config()
        cases = []
        invalid = analysis(); invalid['positions'] = []; cases.append(invalid)
        invalid = analysis(); invalid['positions'][0]['evidence_ids'] = ['video:FAKE']; cases.append(invalid)
        invalid = analysis(); invalid['week_overview']['status'] = 'complete'; cases.append(invalid)
        cases.extend([None, {}, []])
        for value in cases:
            self.answer = value
            with self.subTest(value=value), self.assertRaises(guidance.PortfolioGuidanceError): self.run_guidance()
        self.answer = analysis(); self.finish = 'length'
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_response_incomplete$'): self.run_guidance()
        self.assertEqual(list((self.tmp / 'output').glob('*.md')), [])

    def test_direct_relevance_and_latest_date_refs_are_enforced(self):
        ev = source_evidence(); ev['sources'][0]['content'] = 'Only unrelated macro policy.'; ev['sources'][0]['direct_assets'] = []
        self.build.return_value = ev
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_direct_evidence_invalid$'): self.run_guidance()
        self.answer['positions'][0]['coverage'] = 'indirect'
        self.assertEqual(self.run_guidance()['status'], 'created')

    def test_replay_ignores_fetch_time_and_changed_inputs_create_revision_without_overwrite(self):
        first = self.run_guidance(); before = Path(first['path']).read_bytes()
        self.fetch.return_value['fetched_at'] = '2026-10-23T02:00:00Z'
        repeat = self.run_guidance()
        self.assertEqual(repeat['status'], 'reused'); self.assertEqual(repeat['path'], first['path'])
        self.assertEqual(len(self.requests), 1)
        self.fetch.return_value['positions'][0]['recorded_account_weight_pct'] = 30
        second = self.run_guidance()
        self.assertTrue(second['path'].endswith('_v2_26-10-23.md'))
        self.assertEqual(Path(first['path']).read_bytes(), before)
        self.assertEqual(len(self.requests), 2)

    def test_existing_user_report_collision_and_model_text_escaped(self):
        evidence.private_directory(self.tmp / 'output')
        target = self.tmp / 'output' / '持仓指导_日报_26-10-23.md'
        evidence.write_private_text(target, 'user original')
        self.answer['positions'][0]['impact'] = '![](https://evil.test/exfil?private) <img src=x> [open](https://evil.test)'
        result = self.run_guidance()
        self.assertTrue(result['path'].endswith('_v2_26-10-23.md'))
        self.assertEqual(target.read_text(), 'user original')
        rendered = Path(result['path']).read_text()
        self.assertNotIn('<img', rendered); self.assertNotIn('https://evil.test', rendered)
        self.assertNotIn('![](', rendered)

    def test_private_paths_permissions_and_symlink_fail_before_network(self):
        for output in [evidence.REPO_ROOT / 'public', self.tmp / 'symlink']:
            if output.name == 'symlink': output.symlink_to(self.tmp / 'elsewhere', target_is_directory=True)
            self.config_data['output_dir'] = str(output); self.save_config()
            with self.assertRaises(guidance.PortfolioGuidanceError): self.run_guidance()
        self.fetch.assert_not_called()
        self.config_data['output_dir'] = str(self.tmp / 'output'); self.save_config(); self.config.chmod(0o644)
        with self.assertRaises(guidance.PortfolioGuidanceError): self.run_guidance()
        self.assertEqual(self.requests, [])

    def test_errors_safe_no_retries_and_public_tree_unchanged(self):
        public_paths = [evidence.REPO_ROOT / 'summaries.json', evidence.REPO_ROOT / 'state.json', *sorted((evidence.REPO_ROOT / 'public').rglob('*'))]
        def hashes():
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in public_paths if p.is_file()}
        before = hashes(); self.status = 500
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_unavailable$') as raised: self.run_guidance()
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(hashes(), before)
        self.assertNotIn('SECRET', repr(raised.exception))

    def test_response_model_mismatch_rejected_and_forced_run_is_revision(self):
        self.response_model = 'deepseek-flash'
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_mismatch$'):
            self.run_guidance()
        self.response_model = 'deepseek-v4-pro'
        first = self.run_guidance()
        forced = self.run_guidance(force=True)
        self.assertTrue(forced['path'].endswith('_v2_26-10-23.md'))
        self.assertNotEqual(first['path'], forced['path'])
        repeat = self.run_guidance()
        self.assertEqual(repeat['status'], 'reused')
        self.assertEqual(len(self.requests), 3)

    def test_unexpected_raw_fields_do_not_reach_model_or_report(self):
        self.fetch.return_value.update(accountId='PRIVATE-ACCOUNT', notes='PRIVATE-NOTE')
        self.fetch.return_value['positions'][0].update(quantity=99117, marketValue=672883, notes='PRIVATE-NOTE')
        result = self.run_guidance()
        outbound = self.requests[0].content.decode()
        report = Path(result['path']).read_text()
        for forbidden in ('PRIVATE-ACCOUNT', 'PRIVATE-NOTE', '99117', '672883', 'quantity', 'marketValue'):
            self.assertNotIn(forbidden, outbound)
            self.assertNotIn(forbidden, report)

    def test_exact_trade_instruction_is_rejected(self):
        self.answer['positions'][0]['conditional_action'] = '若下跌，卖出100股。'
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_action_invalid$'):
            self.run_guidance()

    def test_summary_shell_or_quality_note_cannot_establish_direct_support(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        source = payload['evidence']['sources'][0]
        source['direct_assets'] = []
        for text in ('[{"asset":"NVDA","view":"未提及。"}]',
                     '[{"asset":"NVDA","view":"未提及 NVDA，但讨论了行业资本开支。"}]',
                     'Only interest-rate policy.'):
            source['content'] = text
            source['source_quality_note'] = 'NVDA may be an ASR recognition error.'
            with self.subTest(content=text), self.assertRaisesRegex(
                    guidance.PortfolioGuidanceError, '^model_direct_evidence_invalid$'):
                guidance.validate_analysis(analysis(), payload)
        indirect = analysis(); indirect['positions'][0]['coverage'] = 'indirect'
        self.assertEqual(guidance.validate_analysis(indirect, payload), indirect)

    def test_caption_direct_support_uses_body_and_excludes_negated_mentions(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        source = payload['evidence']['sources'][0]
        source.update(kind='recovered_captions', direct_assets=[], source_quality_note='NVDA discussion')
        for content in ('仅讨论行业资本开支。', '未提及 NVDA，但讨论了行业资本开支。'):
            source['content'] = content
            with self.subTest(content=content), self.assertRaisesRegex(
                    guidance.PortfolioGuidanceError, '^model_direct_evidence_invalid$'):
                guidance.validate_analysis(analysis(), payload)
        source['content'] = 'NVDA 的增长预期需要观察。'
        self.assertEqual(guidance.validate_analysis(analysis(), payload), analysis())

    def test_meaningful_summary_asset_scope_allows_direct_view(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        source = payload['evidence']['sources'][0]
        source.update(content='认为增长预期过高，应观察财报。', direct_assets=['NVDA / 英伟达'])
        self.assertEqual(guidance.validate_analysis(analysis(), payload), analysis())

    def test_trade_instruction_check_covers_all_free_text_fields_and_chinese_numbers(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        cases = [
            ('portfolio_overview', 'text', '建议买入100股。'),
            ('week_overview', 'text', '若下跌，卖出一百股。'),
            ('positions', 'latest_day_view', '建议买入价值一千美元的股票。'),
            ('positions', 'week_view', '若风险上升，减仓百分之十。'),
            ('positions', 'impact', '卖出100股。'),
            ('positions', 'conditional_action', '若下跌，卖出一百股。'),
            ('positions', 'conditions', ['目标仓位调整为10%。']),
            ('positions', 'risks', ['可以保证盈利。']),
            ('positions', 'invalidation_conditions', ['若上修，投入两千美元买入。']),
            ('positions', 'conditional_action', '将账户权重降至两成。'),
            ('positions', 'conditional_action', '若上涨，买入1,000股。'),
        ]
        for section, field, text in cases:
            result = analysis()
            target = result['positions'][0] if section == 'positions' else result[section]
            target[field] = text
            with self.subTest(field=field, text=text), self.assertRaisesRegex(
                    guidance.PortfolioGuidanceError, '^model_action_invalid$'):
                guidance.validate_analysis(result, payload)

    def test_price_conditions_current_weights_and_no_guarantee_disclaimer_remain_valid(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        for text in ('若股价跌破100美元且基本面转弱，再评估减仓。',
                     '当前账本账户权重25%，先核对风险预算。',
                     '若价格突破一百美元，再观察需求变化。',
                     '不能保证收益，需先明确风险预算。',
                     '无法保证盈利，应观察风险。'):
            result = analysis(); result['positions'][0]['conditional_action'] = text
            with self.subTest(text=text):
                self.assertEqual(guidance.validate_analysis(result, payload), result)

    def test_private_caption_recovery_bootstraps_access_config_and_keeps_env_priority(self):
        import ytdlp_opts
        from transcript import TranscriptResult
        cookie = self.tmp / 'synthetic-cookies.txt'; cookie.write_text('synthetic cookies')
        override_cookie = self.tmp / 'override-cookies.txt'; override_cookie.write_text('synthetic override')
        public_config = {'youtube_access': {'cookies_file': str(cookie), 'impersonate': 'chrome-131'}, 'transcript': {}}
        item = {'published': '2026-10-22T02:00:00Z', 'summary_status': 'failed', 'source_videos': [
            {'video_id': 'abcdefghijk', 'channel_name': 'Synthetic channel',
             'source_status': 'captions', 'transcript_status': 'success'}]}
        self.build.side_effect = lambda summaries, as_of, cache, config, recover_captions: evidence.build_evidence(
            {'items': [item]}, as_of, cache, config, recover_captions)
        captured = []
        def get_transcript(video_id, config):
            captured.append(ytdlp_opts.merge_into({}, audio=False))
            return TranscriptResult(status='success', source='manual_caption', text='NVDA demand outlook.')
        with patch.object(guidance.yaml, 'safe_load', return_value=public_config), patch.object(evidence, 'get_transcript', get_transcript):
            for index, overrides in enumerate(({}, {'YTDLP_COOKIES': str(override_cookie), 'YT_IMPERSONATE': 'none'})):
                self.config_data['cache_dir'] = str(self.tmp / f'access-cache-{index}'); self.save_config()
                with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only', **overrides}, clear=True):
                    self.run_guidance()
                self.assertEqual(captured[-1].get('cookiefile'), str(cookie if index == 0 else override_cookie))
                self.assertEqual('impersonate' in captured[-1], index == 0)

    def test_caption_bootstrap_failure_has_safe_code(self):
        import ytdlp_opts
        with patch.object(ytdlp_opts, 'bootstrap_env_from_config', side_effect=RuntimeError('SECRET COOKIE PATH')):
            with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^youtube_access_config_invalid$') as raised:
                self.run_guidance()
        self.assertIsNone(raised.exception.__cause__)
        self.build.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_report_translates_internal_states_to_chinese(self):
        data = analysis(); data['positions'][0]['status'] = 'conditional_reduce'
        holdings = portfolio(); holdings['positions'][0]['cost_status'] = 'known'
        ev = source_evidence(); ev['source_gaps'] = [{'date': '2026-10-21', 'channel': 'Other channel',
            'url': 'https://www.youtube.com/watch?v=ABCDEFGHIJK', 'reason': 'source_unavailable'}]
        report = guidance.render_report(holdings, ev, data, TODAY.isoformat())
        for internal in ('direct', 'conditional_reduce', 'known', 'CALCULATED', 'insufficient', 'first_digest_date', 'summary', 'source_unavailable'):
            self.assertNotIn(internal, report)
        for label in ('直接观点', '满足条件再评估减仓', '已知成本', '账本计算快照', '证据不足', '首次日报收录日', '成功摘要', '来源不可用'):
            self.assertIn(label, report)

    def test_report_displays_beijing_time_without_changing_cached_utc(self):
        timestamp = '2026-10-22T18:30:00Z'
        self.fetch.return_value['fetched_at'] = timestamp
        result = self.run_guidance()
        self.assertIn('本地账本读取时间：2026-10-23 02:30:00（北京时间）', Path(result['path']).read_text())
        cached = list((self.tmp / 'cache' / 'reports').glob('*.json'))
        self.assertEqual(len(cached), 1)
        self.assertEqual(json.loads(cached[0].read_text())['fetched_at'], timestamp)
        self.fetch.return_value['fetched_at'] = '2026-10-23T00:00:00Z'
        self.assertEqual(self.run_guidance()['status'], 'reused')
        self.assertEqual(len(self.requests), 1)

    def test_adjacent_trade_verbs_and_position_target_forms_are_rejected(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        for text in ('若风险上升，卖100股。', '若反弹，买一百股。', '将仓位调至10%。',
                     '将账户权重调到百分之十。', '若上涨，买100美元的股票。'):
            result = analysis(); result['positions'][0]['conditional_action'] = text
            with self.subTest(text=text), self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_action_invalid$'):
                guidance.validate_analysis(result, payload)

    def test_hold_price_triggers_are_not_transaction_amounts_or_target_weights(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        for text in ('继续持有至股价跌破100美元时重新评估。',
                     '持有至股价下跌10%时再评估基本面。',
                     '买入条件为股价突破100美元且盈利改善。',
                     '卖出条件为股价下跌10%且基本面转弱。'):
            result = analysis(); result['positions'][0]['conditional_action'] = text
            with self.subTest(text=text):
                self.assertEqual(guidance.validate_analysis(result, payload), result)

    def test_payload_exposes_only_locally_allowed_direct_refs_per_position(self):
        holdings = portfolio()
        for symbol in ('IBM', 'UNKNOWN'):
            position = deepcopy(holdings['positions'][0])
            position.update(key=f'Futu-USD:{symbol}:USD', symbol=symbol)
            holdings['positions'].append(position)
        ev = source_evidence()
        caption = deepcopy(ev['sources'][0])
        caption.update(evidence_id='video:ABCDEFGHIJK', kind='recovered_captions', content='IBM盈利预期可能过高。',
                       direct_assets=[], source_quality_note='NVDA possibly misrecognized')
        ev['sources'].append(caption)
        absent = deepcopy(caption)
        absent.update(evidence_id='video:12345678901', content='暂无对NVDA的具体观点。')
        ev['sources'].append(absent)
        payload = guidance._payload(holdings, ev, TODAY.isoformat())
        self.assertEqual(payload['allowed_direct_evidence_ids'], {
            'Futu-USD:NVDA:USD': ['video:abcdefghijk'],
            'Futu-USD:IBM:USD': ['video:ABCDEFGHIJK'],
            'Futu-USD:UNKNOWN:USD': []})
        self.assertIn('allowed_direct_evidence_ids', guidance.PROMPT)
        ev['sources'][0]['direct_assets'] = []
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_direct_evidence_invalid$'):
            guidance.validate_analysis(analysis(), guidance._payload(portfolio(), ev, TODAY.isoformat()))

    def test_explicit_hold_value_targets_are_rejected_without_blocking_price_triggers(self):
        payload = guidance._payload(portfolio(), source_evidence(), TODAY.isoformat())
        for text in ('建议持有价值100美元的股票。', '若逻辑成立，保留一千美元的持仓。'):
            result = analysis(); result['positions'][0]['conditional_action'] = text
            with self.subTest(text=text), self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^model_action_invalid$'):
                guidance.validate_analysis(result, payload)
        result = analysis()
        result['positions'][0]['conditional_action'] = '继续持有至股价跌破100美元时重新评估。'
        self.assertEqual(guidance.validate_analysis(result, payload), result)

    def test_latest_day_renders_original_direct_views_separately_by_channel(self):
        ev = source_evidence()
        original_a = '财报不突出，但基本面暂难形成操作依据；仅考虑波段观察。'
        original_b = '财报缺少亮点，市场关注机器人与潜在合并预期。'
        ev['sources'][0].update(channel='甲频道', content=json.dumps([
            {'asset': 'NVDA', 'type': 'stock', 'view': original_a, 'evidence': '原摘要A'}]))
        second = deepcopy(ev['sources'][0])
        second.update(channel='乙频道', evidence_id='video:12345678901', video_id='12345678901',
                      url='https://www.youtube.com/watch?v=12345678901',
                      content=json.dumps([{'asset': 'NVDA', 'type': 'stock', 'view': original_b, 'evidence': '原摘要B'}]))
        ev['sources'].append(second)
        data = analysis(); data['positions'][0]['latest_day_view'] = '两来源均认为错误混合论断。'
        before = deepcopy(data)
        report = guidance.render_report(portfolio(), ev, data, TODAY.isoformat())
        latest = report.split('最新可用日观点', 1)[1].split('七日窗口中的变化与分歧', 1)[0]
        self.assertIn(f'[甲频道 · 2026-10-22](https://www.youtube.com/watch?v=abcdefghijk)：{original_a}', latest)
        self.assertIn(f'[乙频道 · 2026-10-22](https://www.youtube.com/watch?v=12345678901)：{original_b}', latest)
        self.assertNotIn('错误混合论断', latest)
        self.assertIn('持仓影响：可能影响估值。', report)
        self.assertIn('条件式思路：若增长预期下修，再评估减仓。', report)
        self.assertEqual(data, before)

    def test_latest_day_fallback_labels_model_and_does_not_promote_invalid_views(self):
        cases = [
            {'view': '未提及。'}, {'view': '暂无对NVDA的具体观点。'},
            {'view': '未提及NVDA，但讨论行业资本开支。'}, {'asset': 'IBM', 'view': 'IBM需求强。'},
            {'direct_assets': [], 'view': 'Only macro views.', 'source_quality_note': 'NVDA'},
            {'date': '2026-10-21', 'view': '较早日期的真实观点。'},
            {'kind': 'recovered_captions', 'view': '字幕正文观点。'},
            {'content': 'not structured JSON'},
        ]
        for changes in cases:
            ev = source_evidence(); source = ev['sources'][0]
            record = {'asset': changes.get('asset', 'NVDA'), 'type': 'stock',
                      'view': changes.get('view', '默认观点'), 'evidence': ''}
            source.update({k: v for k, v in changes.items() if k not in ('asset', 'view')})
            source['content'] = changes.get('content', json.dumps([record]))
            data = analysis(); data['positions'][0].update(coverage='indirect', latest_day_view='只有间接信息，需补充证据。')
            with self.subTest(changes=changes):
                report = guidance.render_report(portfolio(), ev, data, TODAY.isoformat())
                latest = report.split('最新可用日观点', 1)[1].split('七日窗口中的变化与分歧', 1)[0]
                self.assertIn('模型整理', latest)
                self.assertIn('无可展示的结构化直接摘要', latest)
                self.assertIn('只有间接信息，需补充证据。', latest)
                self.assertNotIn(record['view'], latest)

    def test_original_source_views_remain_plain_text_and_only_exact_internal_status_translates(self):
        ev = source_evidence()
        ev['sources'][0]['content'] = json.dumps([{'asset': 'NVDA', 'type': 'stock',
            'view': '需求待观察。<img src=x> ![x](https://evil.test/exfil)', 'evidence': ''}])
        data = analysis(); data['positions'][0]['impact'] = 'week_status=insufficient，需谨慎。'
        report = guidance.render_report(portfolio(), ev, data, TODAY.isoformat())
        self.assertIn('需求待观察。', report)
        self.assertIn(r'&lt;img src\=x&gt;', report)
        self.assertNotIn('<img', report); self.assertNotIn('https://evil.test', report)
        self.assertIn('周证据不足，需谨慎。', report)
        self.assertEqual(guidance._plain('week_status=complete'), r'week\_status\=complete')
        self.assertEqual(data['positions'][0]['impact'], 'week_status=insufficient，需谨慎。')

    def test_existing_paid_analysis_is_reused_for_deterministic_render_revision(self):
        self.build.return_value['sources'][0]['content'] = json.dumps([
            {'asset': 'NVDA', 'type': 'stock', 'view': '原作者的独立观点。', 'evidence': ''}])
        self.answer['positions'][0]['latest_day_view'] = '模型混合观点。'
        with patch.object(guidance, 'render_report', return_value='旧版模型混合展示\n'):
            first = self.run_guidance()
        cache_files = list((self.tmp / 'cache' / 'reports').glob('*.json'))
        cached_before = {str(p): p.read_bytes() for p in cache_files}
        revised = self.run_guidance()
        self.assertTrue(revised['path'].endswith('_v2_26-10-23.md'))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual({str(p): p.read_bytes() for p in cache_files}, cached_before)
        self.assertEqual(Path(first['path']).read_text(), '旧版模型混合展示\n')
        self.assertIn('原作者的独立观点。', Path(revised['path']).read_text())
        self.assertNotIn('模型混合观点。', Path(revised['path']).read_text())
        self.assertEqual(self.run_guidance()['status'], 'reused')
        self.assertEqual(len(self.requests), 1)

    def test_revision_reuses_current_after_first_version_archived_and_appends_v4(self):
        directory = evidence.private_directory(self.tmp / 'revisions')
        args = (directory, '持仓指导_日报', '.md')
        first, _ = guidance._revision(*args, 'first', date_suffix='_26-10-23')
        second, _ = guidance._revision(*args, 'second', date_suffix='_26-10-23')
        current, _ = guidance._revision(*args, 'current', date_suffix='_26-10-23')
        archive = evidence.private_directory(directory / '99 旧版归档')
        first.rename(archive / first.name)
        evidence.write_private_text(archive / '持仓指导_日报_v9999_26-10-23.md', 'archived-only')
        reused, status = guidance._revision(*args, 'current', reuse=True, date_suffix='_26-10-23')
        self.assertEqual((reused, status), (current, 'reused'))
        self.assertFalse(first.exists())
        added, status = guidance._revision(*args, 'new', reuse=True, date_suffix='_26-10-23')
        self.assertEqual((added.name, status), ('持仓指导_日报_v4_26-10-23.md', 'created'))
        self.assertEqual(second.read_text(), 'second'); self.assertEqual(current.read_text(), 'current')
        self.assertEqual((archive / first.name).read_text(), 'first')

    def test_revision_counter_and_reuse_are_isolated_by_stem_date_and_suffix(self):
        directory = evidence.private_directory(self.tmp / 'isolated-revisions')
        for name in ('report_v9999_26-10-24.md', 'report_v8888_26-10-23.json', 'other_v7777_26-10-23.md'):
            evidence.write_private_text(directory / name, 'new')
        evidence.write_private_text(directory / 'report_v2_26-10-23.md', 'previous')
        path, status = guidance._revision(directory, 'report', '.md', 'new', reuse=True, date_suffix='_26-10-23')
        self.assertEqual((path.name, status), ('report_v3_26-10-23.md', 'created'))

    def test_revision_reuse_requires_regular_mode600_file_and_preserves_other_entries(self):
        directory = evidence.private_directory(self.tmp / 'safe-revisions')
        old = directory / 'report.md'; old.write_text('same'); old.chmod(0o644)
        target = self.tmp / 'target'; evidence.write_private_text(target, 'same')
        (directory / 'report_v2.md').symlink_to(target)
        (directory / 'report_v3.md').mkdir(mode=0o700)
        path, status = guidance._revision(directory, 'report', '.md', 'same', reuse=True)
        self.assertEqual((path.name, status), ('report_v4.md', 'created'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(old.stat().st_mode & 0o777, 0o644)
        self.assertEqual(old.read_text(), 'same'); self.assertEqual(target.read_text(), 'same')
        self.assertEqual(guidance._revision(directory, 'report', '.md', 'same', reuse=True), (path, 'reused'))

    def test_revision_creation_race_never_overwrites_competing_file(self):
        directory = evidence.private_directory(self.tmp / 'racing-revisions')
        evidence.write_private_text(directory / 'report_v3.md', 'previous')
        original_write = guidance.write_private_text
        def competing_write(path, content):
            if path.name == 'report_v4.md':
                original_write(path, 'competitor')
            return original_write(path, content)
        with patch.object(guidance, 'write_private_text', competing_write):
            path, status = guidance._revision(directory, 'report', '.md', 'new')
        self.assertEqual((path.name, status), ('report_v5.md', 'created'))
        self.assertEqual((directory / 'report_v4.md').read_text(), 'competitor')

    def test_revision_limit_applies_after_highest_existing_version(self):
        directory = evidence.private_directory(self.tmp / 'limited-revisions')
        latest = directory / 'report_v10000.md'; evidence.write_private_text(latest, 'current')
        self.assertEqual(guidance._revision(directory, 'report', '.md', 'current', reuse=True), (latest, 'reused'))
        with self.assertRaisesRegex(guidance.PortfolioGuidanceError, '^revision_limit$'):
            guidance._revision(directory, 'report', '.md', 'new', reuse=True)

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import portfolio_source as source

TODAY = date(2026, 10, 23)


def fixtures():
    accounts = [{'id': 'secret-f', 'name': 'private-f', 'currency': 'HKD'},
                {'id': 'secret-h', 'name': 'private-h', 'currency': 'USD'},
                {'id': 'other', 'name': 'out-of-scope', 'currency': 'USD'}]
    def holding(symbol, value, cost):
        return {'holdingType': 'security', 'quantity': '991.2', 'notes': 'SECRET-NOTE',
                'instrument': {'id': 'private-' + symbol, 'symbol': symbol, 'currency': 'USD'},
                'localCurrency': 'USD', 'baseCurrency': 'USD', 'fxRate': '1',
                'marketValue': {'local': str(value), 'base': str(value)},
                'costBasis': None if cost is None else {'local': str(cost), 'base': str(cost)},
                'weight': '0.4', 'asOfDate': '2026-10-23'}
    rows = {'secret-f': [holding('AAA', 100, 80), holding('BBB', 100, 0)],
            'secret-h': [holding('CCC', 200, None)]}
    quotes = {h['instrument']['id']: {'quote': {'close': '10', 'timestamp': '2026-10-22T20:00:00Z'},
               'quoteDate': '2026-10-22', 'isStale': False} for hs in rows.values() for h in hs}
    return accounts, rows, quotes


class PortfolioSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.password = Path(self.tmp) / 'password'
        self.password.write_text('private-password')
        self.password.chmod(0o600)
        self.config = {'base_url': 'https://portfolio.test:8447', 'password_file': str(self.password),
                       'account_aliases': {'private-f': {'alias': 'Futu-HKD', 'broker': 'futu'},
                                           'private-h': {'alias': 'HSBC-USD', 'broker': 'hsbc'}}}
        self.accounts, self.rows, self.quotes = fixtures()
        self.calls = []
        self.snapshot_source = 'CALCULATED'
        def request(client, method, endpoint, payload=None):
            self.calls.append((method, endpoint, payload))
            if endpoint == '/auth/login': return {}
            if endpoint == '/profiles/get_profile_state': return {'session': {'scopeId': 'private-scope'}}
            if endpoint == '/accounts': return self.accounts
            if endpoint.startswith('/holdings/list?'):
                from urllib.parse import parse_qs, urlsplit
                return self.rows[parse_qs(urlsplit(endpoint).query)['accountId'][0]]
            if endpoint.startswith('/snapshots?'):
                return [{'snapshotDate': '2026-09-07', 'isDateValid': True, 'source': self.snapshot_source}]
            if endpoint == '/market-data/quotes/latest': return self.quotes
            self.fail('Unexpected endpoint: ' + endpoint)
        self.enterContext(patch.object(source.PortfolioClient, 'request', request))

    def test_both_brokers_whitelist_and_cost_semantics(self):
        result = source.fetch_portfolio(self.config, TODAY)
        self.assertEqual(result['brokers'], ['futu', 'hsbc'])
        a, b, c = result['positions']
        self.assertEqual(a['cost_to_price_deviation_pct'], -20)
        self.assertEqual(a['portfolio_security_weight_pct'], 25)
        self.assertEqual(a['recorded_account_weight_pct'], 40)
        self.assertEqual(b['cost_status'], 'zero')
        self.assertIsNone(b['cost_to_price_deviation_pct'])
        self.assertEqual(c['cost_status'], 'unknown')
        self.assertEqual(a['positions_as_of'], '2026-09-07')
        self.assertEqual(a['quote_as_of'], '2026-10-22')
        self.assertFalse(result['accounts'][0]['broker_verified'])
        serialized = json.dumps(result)
        for forbidden in ['secret-', 'private-', '991.2', 'SECRET-NOTE', 'quantity', 'marketValue', 'accountId']:
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(len([x for x in self.calls if x[1].startswith('/holdings/list?')]), 2)
        self.assertEqual({x[1] for x in self.calls if x[0] == 'POST'},
                         {'/auth/login', '/profiles/get_profile_state', '/market-data/quotes/latest'})

    def test_missing_quote_preserves_unknown_not_zero_and_invalidates_account_denominator(self):
        self.quotes['private-AAA']['quote'] = None
        self.rows['secret-f'][0]['marketValue'] = {'local': 0, 'base': 0}
        self.rows['secret-f'][0]['weight'] = 0
        result = source.fetch_portfolio(self.config, TODAY)['positions']
        self.assertFalse(result[0]['valuation_available'])
        self.assertIsNone(result[0]['recorded_account_weight_pct'])
        self.assertIsNone(result[1]['recorded_account_weight_pct'])
        self.assertTrue(all(p['portfolio_security_weight_pct'] is None for p in result))
        self.assertIsNone(result[0]['cost_to_price_deviation_pct'])

    def test_stale_quote_reported_and_bad_numbers_not_emitted(self):
        self.quotes['private-AAA']['isStale'] = True
        self.rows['secret-h'][0]['marketValue']['base'] = 'NaN'
        self.rows['secret-f'][1]['costBasis']['local'] = 'Infinity'
        result = source.fetch_portfolio(self.config, TODAY)['positions']
        self.assertTrue(result[0]['quote_is_stale'])
        self.assertIsNone(result[1]['cost_to_price_deviation_pct'])
        self.assertTrue(all(p['portfolio_security_weight_pct'] is None for p in result))
        json.dumps(result, allow_nan=False)

    def test_cross_base_currency_disables_combined_weights(self):
        self.rows['secret-h'][0]['baseCurrency'] = 'HKD'
        self.assertTrue(all(p['portfolio_security_weight_pct'] is None
                            for p in source.fetch_portfolio(self.config, TODAY)['positions']))

    def test_missing_mapped_account_and_broker_fail_closed(self):
        self.accounts.pop(1)
        with self.assertRaisesRegex(source.PortfolioSourceError, '^account_scope_incomplete$'):
            source.fetch_portfolio(self.config, TODAY)
        self.config['account_aliases'].pop('private-h')
        with self.assertRaises(source.PortfolioSourceError):
            source.fetch_portfolio(self.config, TODAY)

    def test_config_rejects_non_https_non_ip_connect_and_unsafe_password(self):
        for key, value in [('base_url', 'http://portfolio.test'), ('connect_address', 'other.test')]:
            with self.subTest(key=key), self.assertRaises(source.PortfolioSourceError):
                source.fetch_portfolio({**self.config, key: value}, TODAY)
        self.password.chmod(0o644)
        with self.assertRaisesRegex(source.PortfolioSourceError, '^password_permissions$'):
            source.fetch_portfolio(self.config, TODAY)

    def test_errors_are_safe_without_original_message(self):
        with patch.object(source.PortfolioClient, 'request', side_effect=RuntimeError('password=SECRET')):
            with self.assertRaisesRegex(source.PortfolioSourceError, '^source_unavailable$') as raised:
                source.fetch_portfolio(self.config, TODAY)
        self.assertIsNone(raised.exception.__cause__)

    def test_connect_ip_keeps_tls_hostname_verification(self):
        context = ssl.create_default_context()
        sock = MagicMock()
        with patch.object(source.socket, 'create_connection', return_value=sock) as connect:
            with patch.object(context, 'wrap_socket', return_value=MagicMock()) as wrap:
                conn = source.PinnedHTTPSConnection('portfolio.test', 8447, '127.0.0.1', context=context)
                conn.connect()
        self.assertEqual(connect.call_args.args[0], ('127.0.0.1', 8447))
        self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'portfolio.test')
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_real_snapshot_source_enum_is_preserved(self):
        for value in ('MANUAL_ENTRY', 'BROKER_IMPORTED', 'CSV_IMPORT'):
            self.snapshot_source = value
            with self.subTest(source=value):
                self.assertEqual(source.fetch_portfolio(self.config, TODAY)['accounts'][0]['positions_source'], value)

    def test_tiny_invalid_values_cannot_overflow_ratios(self):
        self.rows['secret-f'][0]['marketValue']['local'] = '1e-10000'
        result = source.fetch_portfolio(self.config, TODAY)
        self.assertFalse(result['positions'][0]['valuation_available'])
        json.dumps(result, allow_nan=False)


class PortfolioTransportTests(unittest.TestCase):
    def test_readonly_transport_origin_cookie_scope_and_bounded_read(self):
        client = source.PortfolioClient({'base_url': 'https://portfolio.test:8447'})
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200; response.read.return_value = b'{}'
        response.getheader.return_value = 'wf_session=TESTCOOKIE; Secure; HttpOnly'
        with patch.object(source, 'PinnedHTTPSConnection', return_value=conn):
            client.request('POST', '/auth/login', {'password': 'TESTPASSWORD'})
            client.scope = 'TESTSCOPE'
            client.request('GET', '/accounts')
        headers = conn.request.call_args.kwargs['headers']
        self.assertEqual(headers['Origin'], 'https://portfolio.test:8447')
        self.assertEqual(headers['Cookie'], 'wf_session=TESTCOOKIE')
        self.assertEqual(headers['x-wf-profile-scope'], 'TESTSCOPE')
        self.assertEqual(conn.request.call_args.args[:2], ('GET', '/api/v1/accounts'))
        self.assertEqual(response.read.call_args.args[0], 4 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(source.PortfolioSourceError, '^endpoint_not_allowed$'):
            client.request('POST', '/holdings/recalculate', {})

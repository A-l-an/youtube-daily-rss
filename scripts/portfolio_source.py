"""Read-only Portfolio Local adapter. Raw account data never leaves this module."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import http.client
from http.cookies import SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
from urllib.parse import urlencode, urlsplit


class PortfolioSourceError(Exception):
    """Only fixed, non-sensitive error codes may cross this boundary."""


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, connect_address=None, **kwargs):
        super().__init__(host, port=port, timeout=30, **kwargs)
        self.connect_address = connect_address

    def connect(self):
        sock = socket.create_connection((self.connect_address or self.host, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _password(path):
    path = Path(path).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r', encoding='utf-8') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise PortfolioSourceError('password_permissions')
            value = handle.read(65537).strip()
            if not value or len(value) > 65536:
                raise PortfolioSourceError('password_invalid')
            return value
    except PortfolioSourceError:
        raise
    except Exception:
        raise PortfolioSourceError('password_unavailable') from None


class PortfolioClient:
    def __init__(self, config):
        endpoint = urlsplit(config['base_url'])
        if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
                or endpoint.path not in ('', '/') or endpoint.query or endpoint.fragment):
            raise PortfolioSourceError('source_config_invalid')
        address = config.get('connect_address')
        if address is not None:
            ipaddress.ip_address(address)
        self.host, self.port, self.address = endpoint.hostname, endpoint.port or 443, address
        self.origin = f'https://{endpoint.netloc}'
        self.cookie = None
        self.scope = None

    def request(self, method, endpoint, payload=None):
        allowed = (method, endpoint.split('?')[0]) in {
            ('POST', '/auth/login'), ('POST', '/profiles/get_profile_state'),
            ('GET', '/accounts'), ('GET', '/holdings/list'), ('GET', '/snapshots'),
            ('POST', '/market-data/quotes/latest'),
        }
        if not allowed:
            raise PortfolioSourceError('endpoint_not_allowed')
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'Origin': self.origin}
        if self.cookie:
            headers['Cookie'] = self.cookie
        if self.scope:
            headers['x-wf-profile-scope'] = self.scope
        conn = PinnedHTTPSConnection(self.host, self.port, self.address, context=ssl.create_default_context())
        try:
            conn.request(method, '/api/v1' + endpoint,
                         body=json.dumps(payload) if payload is not None else None, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise PortfolioSourceError('source_http_error')
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise PortfolioSourceError('source_response_too_large')
            if endpoint == '/auth/login':
                cookies = SimpleCookie()
                cookies.load(response.getheader('Set-Cookie', ''))
                if 'wf_session' not in cookies:
                    raise PortfolioSourceError('source_auth_failed')
                self.cookie = 'wf_session=' + cookies['wf_session'].value
            return json.loads(raw)
        finally:
            conn.close()


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and (result == 0 or -20 <= result.adjusted() < 30) else None
    except (InvalidOperation, ValueError):
        return None


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _identifier(value, pattern, code='source_data_invalid'):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise PortfolioSourceError(code)
    return value


def _fetch(config, as_of):
    aliases = config.get('account_aliases')
    if not isinstance(aliases, dict) or not aliases:
        raise PortfolioSourceError('source_config_invalid')
    seen_aliases = set()
    for spec in aliases.values():
        alias = _identifier(spec['alias'], r'[A-Za-z][A-Za-z0-9_-]{0,47}', 'source_config_invalid')
        if spec['broker'] not in ('futu', 'hsbc') or alias in seen_aliases:
            raise PortfolioSourceError('source_config_invalid')
        seen_aliases.add(alias)
    if {x['broker'] for x in aliases.values()} != {'futu', 'hsbc'}:
        raise PortfolioSourceError('account_scope_incomplete')
    client = PortfolioClient(config)
    client.request('POST', '/auth/login', {'password': _password(config['password_file'])})
    profile = client.request('POST', '/profiles/get_profile_state', {})
    client.scope = profile['session']['scopeId']
    if not isinstance(client.scope, str) or not client.scope:
        raise PortfolioSourceError('source_profile_missing')
    accounts = client.request('GET', '/accounts')
    selected = [a for a in accounts if a.get('name') in aliases and not a.get('isArchived', False)]
    if len(selected) != len(aliases) or {a['name'] for a in selected} != set(aliases):
        raise PortfolioSourceError('account_scope_incomplete')
    projected_accounts, raw_rows = [], []
    for account in selected:
        alias = aliases[account['name']]
        query = urlencode({'accountId': account['id']})
        holdings = client.request('GET', '/holdings/list?' + query + '&includeClosed=false')
        snapshots = client.request('GET', '/snapshots?' + query)
        valid = [s for s in snapshots if s.get('isDateValid') is True
                 and _day(s.get('snapshotDate')) and _day(s['snapshotDate']) <= as_of]
        latest = max(valid, key=lambda s: s['snapshotDate']) if valid else {}
        position_date = _day(latest.get('snapshotDate'))
        snapshot_source = latest.get('source')
        # Never pass through free text from a snapshot or treat its date as broker verification.
        snapshot_source = snapshot_source if snapshot_source in ('CALCULATED', 'MANUAL_ENTRY', 'BROKER_IMPORTED', 'CSV_IMPORT') else 'UNKNOWN'
        projected = {'alias': alias['alias'], 'broker': alias['broker'],
                     'currency': _identifier(account['currency'], r'[A-Za-z]{3}'),
                     'positions_as_of': position_date.isoformat() if position_date else None,
                     'positions_source': snapshot_source, 'broker_verified': False}
        projected_accounts.append(projected)
        for h in holdings:
            if h.get('holdingType') == 'security' and not h.get('isClosed', False):
                raw_rows.append((projected, h))
    quotes = client.request('POST', '/market-data/quotes/latest',
                            {'assetIds': sorted({h['instrument']['id'] for _, h in raw_rows})})
    positions, bases, amounts, account_valid = [], set(), [], {}
    keys = set()
    for account, h in raw_rows:
        instrument = h['instrument']
        symbol = _identifier(instrument['symbol'], r'[A-Za-z0-9.^=_:/-]{1,40}')
        currency = _identifier(instrument['currency'], r'[A-Za-z]{3}')
        key = f"{account['alias']}:{symbol}:{currency}"
        if key in keys:
            raise PortfolioSourceError('duplicate_position')
        keys.add(key)
        quantity = _number(h.get('quantity'))
        direction = 'long' if quantity is not None and quantity > 0 else 'short' if quantity is not None and quantity < 0 else 'unknown'
        snapshot = quotes.get(instrument['id']) or {}
        quote = snapshot.get('quote') or {}
        quote_day = _day(snapshot.get('quoteDate')) or _day(quote.get('timestamp'))
        price = _number(quote.get('close'))
        local_value = _number((h.get('marketValue') or {}).get('local'))
        base_value = _number((h.get('marketValue') or {}).get('base'))
        value_available = bool(price is not None and price > 0 and quote_day and quote_day <= as_of
                               and local_value is not None and local_value != 0)
        base_ccy = h.get('baseCurrency')
        fx = _number(h.get('fxRate'))
        combined_valid = (value_available and direction == 'long' and local_value > 0
                          and base_value is not None and base_value > 0
                          and isinstance(base_ccy, str) and bool(re.fullmatch(r'[A-Z]{3}', base_ccy))
                          and fx is not None and fx > 0)
        bases.add(base_ccy)
        amounts.append(base_value if combined_valid else None)
        account_valid[account['alias']] = account_valid.get(account['alias'], True) and value_available
        cost = _number((h.get('costBasis') or {}).get('local'))
        cost_status = 'unknown' if cost is None else 'zero' if cost == 0 else 'known' if cost > 0 else 'unsupported'
        deviation = None
        if direction == 'long' and cost_status == 'known' and value_available and local_value > 0:
            deviation = round(float((cost / local_value - 1) * 100), 4)
        weight = _number(h.get('weight'))
        positions.append({'key': key, 'account_alias': account['alias'], 'broker': account['broker'],
                          'symbol': symbol, 'currency': currency, 'direction': direction,
                          'recorded_account_weight_pct': round(float(weight * 100), 4) if weight is not None and value_available else None,
                          'portfolio_security_weight_pct': None, 'cost_to_price_deviation_pct': deviation,
                          'cost_status': cost_status, 'positions_as_of': account['positions_as_of'],
                          'positions_source': account['positions_source'],
                          'quote_as_of': quote_day.isoformat() if quote_day else None,
                          'quote_is_stale': snapshot.get('isStale') is not False or not value_available,
                          'valuation_available': value_available})
    for p in positions:
        if not account_valid[p['account_alias']]:
            p['recorded_account_weight_pct'] = None
    if amounts and all(v is not None for v in amounts) and len(bases) == 1:
        total = sum(amounts)
        for position, amount in zip(positions, amounts):
            position['portfolio_security_weight_pct'] = round(float(amount / total * 100), 4)
    return {'schema_version': 1, 'fetched_at': datetime.now(timezone.utc).isoformat(),
            'as_of_date': as_of.isoformat(), 'brokers': ['futu', 'hsbc'],
            'accounts': sorted(projected_accounts, key=lambda a: a['alias']),
            'positions': sorted(positions, key=lambda p: p['key'])}


def fetch_portfolio(source_config: dict, as_of: date) -> dict:
    try:
        return _fetch(source_config, as_of)
    except PortfolioSourceError:
        raise
    except Exception:
        raise PortfolioSourceError('source_unavailable') from None

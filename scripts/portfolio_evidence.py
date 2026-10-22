"""Seven-day, source-bounded evidence; historical recovery uses captions only."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from transcript import get_transcript

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_CHARS = 60000


class PortfolioEvidenceError(Exception):
    pass


def private_path(path: Path) -> Path:
    path = Path(os.path.abspath(Path(path).expanduser()))
    if path.is_relative_to(REPO_ROOT):
        raise PortfolioEvidenceError('private_path_in_repository')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise PortfolioEvidenceError('private_path_symlink')
        if (part / '.git').exists():
            raise PortfolioEvidenceError('private_path_in_repository')
    return path


def private_directory(path: Path) -> Path:
    path = private_path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise PortfolioEvidenceError('private_directory_permissions')
    return path


def read_private_json(path: Path):
    path = private_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise PortfolioEvidenceError('private_file_permissions')
        raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise PortfolioEvidenceError('private_file_too_large')
        return json.loads(raw)


def write_private_text(path: Path, content: str):
    path = private_path(path)
    private_directory(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_private_json(path: Path, data):
    write_private_text(path, json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n')


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(ZoneInfo('Asia/Shanghai')).date()
    except ValueError:
        return None


def _recover(video_id, as_of, cache, config, recover):
    path = cache / f'{video_id}.json'
    failure = cache / f'{video_id}.{as_of.isoformat()}.failed.json'
    if path.exists() or path.is_symlink():
        try:
            data = read_private_json(path)
            text = data['text']
            if (data['schema_version'] != 1 or data['video_id'] != video_id or data['source'] != 'captions'
                    or data['status'] != 'success' or not isinstance(text, str) or not text.strip()
                    or hashlib.sha256(text.encode()).hexdigest() != data['sha256']):
                raise ValueError()
            return text, None
        except Exception:
            return None, 'caption_cache_invalid'
    if failure.exists() or failure.is_symlink():
        try:
            data = read_private_json(failure)
            if data != {'schema_version': 1, 'video_id': video_id, 'status': 'unavailable',
                        'attempted_on': as_of.isoformat()}:
                raise ValueError()
            return None, 'captions_unavailable'
        except Exception:
            return None, 'caption_cache_invalid'
    if not recover:
        return None, 'captions_not_recovered'
    try:
        # Existing extractors may print upstream errors; keep that material out of CLI logs.
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = get_transcript(video_id, config)
        finally:
            logging.disable(previous)
        if result.status == 'success' and result.text.strip() and result.source in {
                'auto_caption', 'manual_caption', 'yt_dlp_auto_caption', 'yt_dlp_manual_subtitle'}:
            payload = {'schema_version': 1, 'video_id': video_id, 'status': 'success', 'source': 'captions',
                       'text': result.text, 'sha256': hashlib.sha256(result.text.encode()).hexdigest(),
                       'fetched_on': as_of.isoformat(), 'transcript_source': result.source}
            if len(json.dumps(payload).encode()) > MAX_FILE_BYTES:
                return None, 'caption_too_large'
            write_private_json(path, payload)
            return result.text, None
    except Exception:
        pass
    if not failure.exists():
        write_private_json(failure, {'schema_version': 1, 'video_id': video_id, 'status': 'unavailable',
                                     'attempted_on': as_of.isoformat()})
    return None, 'captions_unavailable'


OPINION = r'(?:观点|评价|评论|讨论|看法|分析|表态|意见|信息)(?!分歧|冲突|矛盾|改变|变化|分化)'
QUALIFIER = r'(?:(?:任何|具体|明确|相关|直接|单独|独立|进一步)\s*)*'
TOPIC = r'(?:(?:对|关于|针对|就)\s*[^，,。！？!?；;\n]{1,40}?\s*(?:的)?)?'
EXPRESS = r'(?:给出|提供|发表|表达|形成|作出|做出|进行)'
NO_MENTION = re.compile(
    rf'(?:尚未|暂未|未|没有|并未)(?:直接|单独|具体|明确)?(?:提及|讨论|涉及|谈及|谈到|分析|评价|评论|表达观点)|'
    rf'(?:暂无|暂时没有|尚无|并无|没有|无)\s*(?:{EXPRESS}\s*)?{TOPIC}{QUALIFIER}{OPINION}|'
    rf'(?:尚未|暂未|未|并未)\s*(?:{EXPRESS}\s*{TOPIC}|{TOPIC}{EXPRESS}\s*){QUALIFIER}{OPINION}|'
    r'not\s+mentioned|no\s+mention', re.IGNORECASE)


def _clauses(text):
    return [part.strip() for part in re.split(r'[。！？!?；;，,\n]|但|然而|不过', text) if part.strip()]


def _no_opinion(text):
    normalized = re.sub(r'[\W_]+', '', text).casefold()
    placeholders = {'无', 'notmentioned', 'nomention', 'nocomment', 'none', 'na'}
    return (not normalized or normalized in placeholders
            or all(NO_MENTION.search(clause) for clause in _clauses(text)))


def supports_direct(source, symbol):
    pattern = r'(?<![A-Za-z0-9])' + re.escape(symbol) + r'(?![A-Za-z0-9])'
    if source['kind'] == 'summary':
        # This scope is built only after filtering actual source views, never from JSON wrappers or quality notes.
        return any(re.search(pattern, asset, re.IGNORECASE) for asset in source.get('direct_assets', []))
    if source['kind'] == 'recovered_captions':
        return any(re.search(pattern, clause, re.IGNORECASE) and not NO_MENTION.search(clause)
                   for clause in _clauses(source['content']))
    return False


def _direct_summary_views(source, symbol):
    if source.get('kind') != 'summary' or not supports_direct(source, symbol):
        return []
    try:
        sections = json.loads(source['content'])
    except (KeyError, TypeError, ValueError):
        return []
    if not isinstance(sections, list):
        return []
    views = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        asset, view = section.get('asset'), section.get('view')
        if (not isinstance(asset, str) or asset not in source.get('direct_assets', [])
                or not isinstance(view, str) or _no_opinion(view) or NO_MENTION.search(view)):
            continue
        if supports_direct({'kind': 'summary', 'direct_assets': [asset]}, symbol):
            views.append(view)
    return views


def _source_summary(item, channel):
    sections, direct_assets = [], set()
    for section in (item.get('summary_data') or {}).get('asset_sections', []):
        for view in section.get('source_views', []):
            text = view.get('view')
            if view.get('channel_name') == channel and isinstance(text, str) and not _no_opinion(text):
                asset = section.get('name', '')
                sections.append({'asset': asset, 'type': section.get('type', ''),
                                 'view': text, 'evidence': view.get('evidence', '')})
                # A mixed absence/industry view remains useful indirectly, without claiming the section's ticker.
                if isinstance(asset, str) and asset and not NO_MENTION.search(text):
                    direct_assets.add(asset)
    content = json.dumps(sections, ensure_ascii=False) if sections else None
    return content, sorted(direct_assets) if content and len(content) <= MAX_SOURCE_CHARS else []


def build_evidence(summaries: dict, as_of: date, cache_dir: Path, transcript_config: dict,
                   recover_captions: bool = True) -> dict:
    cache = private_directory(Path(cache_dir) / 'captions')
    start = as_of - timedelta(days=6)
    candidates = {}
    for item in summaries.get('items', []):
        item_date = _date(item.get('report_date')) or _date(item.get('published'))
        if item_date is None or item_date > as_of:
            continue
        for source in item.get('source_videos', []):
            video_id = source.get('video_id', '')
            if not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                continue
            published_date = _date(source.get('published'))
            source_date = published_date or item_date
            if source_date > as_of:
                continue
            candidate = candidates.setdefault(video_id, {'date': source_date, 'date_basis':
                'video_published' if published_date else 'first_digest_date', 'entries': []})
            if source_date < candidate['date']:
                candidate.update(date=source_date, date_basis='video_published' if published_date else 'first_digest_date')
            candidate['entries'].append((item, source))
    sources, gaps, channels = [], [], set()
    for video_id, candidate in sorted(candidates.items(), key=lambda x: (x[1]['date'], x[0])):
        if candidate['date'] < start:
            continue
        entries = candidate['entries']
        source = entries[0][1]
        channel = source.get('channel_name') or 'unknown'
        channels.add(channel)
        text, kind, reason = None, None, 'source_unavailable'
        direct_assets = []
        origin = {'source_status': 'captions', 'content_source': 'YouTube captions (recovered)',
                  'source_quality_note': ''}
        for item, entry in entries:
            success_source = ((entry.get('source_status') == 'captions' and entry.get('transcript_status') == 'success')
                              or (entry.get('source_status') == 'ASR' and entry.get('asr_status') == 'success'))
            if item.get('summary_status') == 'success' and success_source:
                text, direct_assets = _source_summary(item, channel)
                if text:
                    kind = 'summary'
                    origin = {'source_status': entry['source_status'],
                              'content_source': entry.get('content_source') or entry['source_status'],
                              'source_quality_note': (item.get('summary_data') or {}).get('source_quality_note') or ''}
                    break
        if text is None:
            eligible = any(item.get('summary_status') != 'success' and entry.get('transcript_status') == 'success'
                           and entry.get('source_status') == 'captions' for item, entry in entries)
            if eligible:
                text, reason = _recover(video_id, as_of, cache, transcript_config, recover_captions)
                kind = 'recovered_captions'
                if text and _no_opinion(text):
                    text, reason = None, 'source_unavailable'
        common = {'video_id': video_id, 'date': candidate['date'].isoformat(),
                  'date_basis': candidate['date_basis'], 'channel': channel,
                  'url': 'https://www.youtube.com/watch?v=' + video_id}
        if text:
            sources.append({**common, **origin, 'evidence_id': 'video:' + video_id, 'kind': kind,
                            'direct_assets': direct_assets, 'content': text[:MAX_SOURCE_CHARS], 'content_truncated': len(text) > MAX_SOURCE_CHARS})
        else:
            gaps.append({**common, 'reason': reason})
    covered = sorted({s['date'] for s in sources})
    all_dates = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    missing = [day for day in all_dates if day not in covered]
    # Full calendar coverage is deliberately conservative, including weekends and channel gaps.
    complete = (not missing and not gaps and len(channels) >= 2
                and all({s['channel'] for s in sources if s['date'] == day} == channels for day in all_dates))
    return {'schema_version': 1, 'window_start': start.isoformat(), 'window_end': as_of.isoformat(),
            'latest_date': max(covered) if covered else None, 'covered_dates': covered,
            'missing_dates': missing, 'week_status': 'complete' if complete else 'insufficient',
            'source_gaps': gaps, 'sources': sources}

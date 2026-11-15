#!/usr/bin/env python3
"""CAKRA MVP v1.0 — monitored public-source observations and assisted fact verification.

Python 3.9+, standard library only. Local-only by default. No auto-posting.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import factcheck
import intelligence
import monitoring
import investigation
import unified_fact
import discovery
import evaluation
import release
import research_analysis
import regional
import collection_jobs
import ai_providers
import screening
import topic_radar
import youtube_radar
import comment_analysis
import simulation

BASE = Path(__file__).resolve().parent
DEFAULT_DB = BASE / 'cakra.db'
YOUTUBE_ENDPOINT = 'https://www.googleapis.com/youtube/v3/commentThreads'
ALLOWED_VERDICTS = {'belum_terverifikasi', 'didukung_bukti', 'bertentangan_bukti', 'konteks_kurang'}


ENV_KEYS = {'YOUTUBE_API_KEY', 'FACTCHECK_API_KEY', 'META_MODEL_API_KEY', 'MODEL_API_KEY', 'META_MODEL', 'META_BASE_URL', 'CAKRA_AI_DAILY_REQUEST_LIMIT', 'CAKRA_AI_SPENDING_CAP'}

def load_env_file(path):
    """Load optional local .env credentials without dependencies. Existing process env wins."""
    path = Path(path)
    if not path.is_file():
        return 0
    count = 0
    for raw_line in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if key not in ENV_KEYS:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not value or value.startswith('ISI_KEY_'):
            continue
        if key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def open_db(path=DEFAULT_DB):
    conn = sqlite3.connect(str(path), timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS comments (
        comment_id TEXT PRIMARY KEY,
        video_id TEXT NOT NULL,
        author_id TEXT,
        author_name TEXT NOT NULL,
        body TEXT NOT NULL,
        published_at TEXT NOT NULL,
        source_url TEXT NOT NULL,
        dataset TEXT NOT NULL CHECK(dataset IN ('real','demo')),
        collected_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id);
      CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(author_id);
      CREATE TABLE IF NOT EXISTS investigations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comment_id TEXT NOT NULL REFERENCES comments(comment_id),
        claim TEXT NOT NULL,
        verdict TEXT NOT NULL,
        evidence_url TEXT NOT NULL,
        evidence_title TEXT NOT NULL,
        source_date TEXT,
        note TEXT NOT NULL,
        draft TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved')),
        created_at TEXT NOT NULL,
        approved_at TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_investigations_comment ON investigations(comment_id);
    ''')
    factcheck.init_tables(conn)
    intelligence.init_tables(conn)
    monitoring.init_tables(conn)
    investigation.init_tables(conn)
    unified_fact.init_tables(conn)
    discovery.init_tables(conn)
    evaluation.init_tables(conn)
    regional.init_tables(conn)
    collection_jobs.init_tables(conn)
    ai_providers.init_tables(conn)
    screening.init_tables(conn)
    topic_radar.init_tables(conn)
    youtube_radar.init_tables(conn)
    comment_analysis.init_tables(conn)
    return conn


def parse_video_id(raw):
    raw = str(raw or '').strip()
    if re.fullmatch(r'[a-zA-Z0-9_-]{11}', raw):
        return raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password or parsed.port:
        raise ValueError('Masukkan URL YouTube https:// atau video ID yang valid.')
    host = (parsed.hostname or '').lower()
    parts = parsed.path.strip('/').split('/')
    if host in ('youtu.be', 'www.youtu.be'):
        video_id = parts[0] if len(parts)==1 else ''
    elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        video_id = parts[1] if len(parts)==2 and parts[0] in ('shorts', 'live', 'embed') else urllib.parse.parse_qs(parsed.query).get('v', [''])[0] if parsed.path.rstrip('/')=='/watch' else ''
    else:
        raise ValueError('Hanya URL YouTube/youtu.be yang diterima.')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{11}', video_id):
        raise ValueError('Video ID tidak valid.')
    return video_id


def fetch_youtube_page(api_key, video_id, page_token=None):
    params = {'key': api_key, 'part': 'snippet', 'videoId': video_id,
              'maxResults': '100', 'order': 'time', 'textFormat': 'plainText'}
    if page_token:
        params['pageToken'] = page_token
    url = YOUTUBE_ENDPOINT + '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={'User-Agent': 'CAKRA-MVP/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise RuntimeError('Respons YouTube melebihi batas ukuran.')
            return json.loads(body)
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode('utf-8', 'replace'))
            errors = body.get('error', {}).get('errors') or []
            if any(isinstance(item,dict) and item.get('reason')=='commentsDisabled' for item in errors):
                raise RuntimeError('Komentar dinonaktifkan untuk video ini (commentsDisabled).') from None
            allowed={'quotaExceeded','rateLimitExceeded','dailyLimitExceeded','forbidden','videoNotFound'}
            reason=next((item.get('reason') for item in errors if isinstance(item,dict) and item.get('reason') in allowed),'')
        except (ValueError, AttributeError):
            reason = ''
        raise RuntimeError(f'YouTube API HTTP {error.code}'+(f' ({reason})' if reason else '.')) from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError('Koneksi ke YouTube gagal. Cek koneksi jaringan.') from error


def store_youtube_thread(conn, video_id, thread):
    try:
        comment = thread['snippet']['topLevelComment']
        snippet = comment['snippet']
        cid = comment['id']
        body = snippet.get('textDisplay', '')
    except (KeyError, TypeError):
        return False
    if not cid or not body.strip():
        return False
    author = snippet.get('authorChannelId') or {}
    author_id = author.get('value') if isinstance(author, dict) else None
    conn.execute('''
      INSERT INTO comments (comment_id,video_id,author_id,author_name,body,published_at,source_url,dataset,collected_at)
      VALUES (?,?,?,?,?,?,?,?,?)
      ON CONFLICT(comment_id) DO UPDATE SET
        author_id=excluded.author_id,author_name=excluded.author_name,
        body=excluded.body,published_at=excluded.published_at,collected_at=excluded.collected_at
    ''', (cid, video_id, author_id, snippet.get('authorDisplayName') or 'Pengguna', body,
          snippet.get('publishedAt') or '',
          'https://www.youtube.com/watch?v=' + video_id + '&lc=' + urllib.parse.quote(cid, safe=''),
          'real', utc_now()))
    return True


def collect_youtube(conn, video, limit=100, fetcher=fetch_youtube_page):
    api_key = os.getenv('YOUTUBE_API_KEY', '').strip()
    if not api_key:
        raise ValueError('YOUTUBE_API_KEY belum disetel. Lihat README.md.')
    video_id = parse_video_id(video)
    limit = int(limit)
    if not 1 <= limit <= 500:
        raise ValueError('Limit harus antara 1 dan 500 komentar utama.')
    count = 0
    token = None
    visited = set()
    while count < limit:
        page = fetcher(api_key, video_id, token)
        if not isinstance(page, dict) or not isinstance(page.get('items', []), list):
            raise RuntimeError('Respons YouTube API tidak sesuai format.')
        for item in page.get('items', []):
            if count >= limit:
                break
            if store_youtube_thread(conn, video_id, item):
                count += 1
        conn.commit()
        token = page.get('nextPageToken')
        if not token or token in visited or not page.get('items'):
            break
        visited.add(token)
    return {'video_id': video_id, 'processed': count, 'dataset': 'real',
            'note': 'Hanya komentar utama (top-level), bukan semua balasan.'}


def seed_demo(conn):
    """Clearly fictional data for offline UI and unit tests. Never presented as live."""
    now = datetime(2026, 11, 8, 7, 0, tzinfo=timezone.utc)
    rows = []
    pattern_a = [
        'Kabar jembatan X ditutup permanen, bagikan sekarang!',
        'Kabar jembatan X ditutup permanen bagikan sekarang',
        'Kabar jembatan X ditutup permanen! Bagikan sekarang!',
        'Kabar jembatan X ditutup permanen, bagikan sekarang!!',
        'Kabar jembatan X ditutup permanen, bagikan sekarang!',
    ]
    pattern_b = [
        'Lihat berita terbaru di https://contoh.invalid/berita-viral ini penting sekali',
        'Lihat berita terbaru di https://contoh.invalid/berita-viral, ini penting sekali!',
        'Lihat berita terbaru di https://contoh.invalid/berita-viral ini penting sekali',
    ]
    normal = [
        'Apakah ada pengumuman terbaru dari dinas setempat?',
        'Saya belum menemukan konfirmasi dari sumber resmi.',
        'Terima kasih, saya akan cek kembali informasinya.',
        'Video ini direkam kapan ya?',
        'Jangan sebarkan informasi tanpa memeriksa tanggal dan sumbernya.',
    ]
    messages = pattern_a + pattern_b + normal
    for idx, body in enumerate(messages):
        video = 'DEMO000001' if idx % 2 == 0 else 'DEMO000002'
        stamp = now + timedelta(seconds=idx * 12 if idx < 8 else idx * 1000)
        rows.append((f'demo-{idx+1:03}', video, f'DEMO-AUTHOR-{idx+1:03}',
                     f'Akun Contoh {idx+1:02}', body, stamp.isoformat(),
                     f'https://example.invalid/cakra/demo/{idx+1}', 'demo', utc_now()))
    conn.executemany('''INSERT OR IGNORE INTO comments
        (comment_id,video_id,author_id,author_name,body,published_at,source_url,dataset,collected_at)
        VALUES (?,?,?,?,?,?,?,?,?)''', rows)
    conn.commit()
    return {'inserted_or_existing': len(rows), 'dataset': 'demo',
            'note': 'DATA FIKTIF. Tidak mewakili akun atau kejadian nyata.'}


def clear_real_data(conn):
    """Delete every operational row (real dataset plus any demo seeds); schema kept.

    Only called with the operational database connection, never the demo DB.
    """
    before = conn.total_changes
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            name = row[0].replace('"', '""')
            conn.execute('DELETE FROM "' + name + '"')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
    return {'cleared': True, 'dataset': 'real', 'deleted_rows': conn.total_changes - before}


URL_REGEX = re.compile(r'https?://\S+', re.IGNORECASE)

def normalize_content(s):
    s = unicodedata.normalize('NFKC', s or '').lower()
    s = URL_REGEX.sub(' ', s)
    s = re.sub(r'[^\w\s]', ' ', s, flags=re.UNICODE)
    return ' '.join(s.split())


def similarity(a, b):
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def find_clusters(comments, threshold=0.88, min_authors=3):
    """Heuristic near-duplicate content groups, not bot/identity classification.

    Analysis is per dataset (demo/real). Cap 500 observations for O(n^2) comparison.
    """
    records = list(comments)[:500]
    parent = list(range(len(records)))
    normalized = [normalize_content(r['body']) for r in records]

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(records)):
        if len(normalized[i]) < 22:
            continue
        for j in range(i + 1, len(records)):
            if records[i]['dataset'] != records[j]['dataset'] or len(normalized[j]) < 22:
                continue
            if abs(len(normalized[i]) - len(normalized[j])) > max(len(normalized[i]), len(normalized[j])) * 0.25:
                continue
            if similarity(normalized[i], normalized[j]) >= threshold:
                parent[root(j)] = root(i)

    grouped = defaultdict(list)
    for i, record in enumerate(records):
        grouped[root(i)].append(record)
    clusters = []
    for group in grouped.values():
        authors = {r['author_id'] for r in group if r['author_id']}
        if len(authors) < min_authors:
            continue
        times = []
        for r in group:
            try:
                times.append(datetime.fromisoformat(str(r['published_at']).replace('Z', '+00:00')).timestamp())
            except ValueError:
                pass
        span = round((max(times) - min(times)) / 60, 1) if len(times) >= 2 else None
        labels = sorted(group, key=lambda r: r['published_at'])
        clusters.append({
            'id': ('demo-' if group[0]['dataset']=='demo' else 'real-') + labels[0]['comment_id'][:20],
            'dataset': group[0]['dataset'],
            'representative': labels[0]['body'][:180],
            'comment_count': len(group), 'observed_unique_authors': len(authors),
            'videos': len({r['video_id'] for r in group}),
            'time_span_minutes': span,
            'comments': [r['comment_id'] for r in labels],
            'interpretation': 'Kemiripan konten teramati. Bukan bukti bot, click farm, atau niat penulis.'
        })
    return sorted(clusters, key=lambda c: (c['comment_count'], c['observed_unique_authors']), reverse=True)


def parse_timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None
        return dt.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def content_urls(body):
    """Extract comparable public links from comment text; no URL is fetched."""
    found = set()
    for match in URL_REGEX.findall(body or ''):
        raw = match.rstrip('.,!?:;)]}\"\'')
        try:
            parsed = urllib.parse.urlsplit(raw)
            if parsed.scheme in ('http', 'https') and parsed.hostname:
                host = parsed.hostname.lower()
                path = parsed.path.rstrip('/') or '/'
                found.add(host + path + ('?' + parsed.query if parsed.query else ''))
        except ValueError:
            pass
    return found


def build_activity_network(comments, threshold=0.88, window_minutes=30,
                           max_nodes=45, max_edges=90):
    """Evidence-backed observed account relationships, NOT confirmed bot activity.

    An edge requires near-duplicate public text, different author IDs, same dataset,
    and a publication gap within the selected window. Anonymous authors are omitted.
    Every returned edge contains original source links for human review.
    """
    if not 0.82 <= threshold <= 0.98:
        raise ValueError('Kemiripan harus antara 0.82 dan 0.98.')
    if not 5 <= window_minutes <= 180:
        raise ValueError('Jendela waktu harus antara 5 dan 180 menit.')
    records = [dict(r) for r in list(comments)[:500] if r['author_id']]
    docs = []
    authors = {}
    for row in records:
        text = normalize_content(row['body'])
        # Ignore boilerplate and very short utterances such as "setuju" / "oke".
        if len(text) < 22 or len(text.split()) < 4:
            continue
        ts = parse_timestamp(row['published_at'])
        if ts is None:
            continue
        key = str(row['dataset']) + ':' + str(row['author_id'])
        authors[key] = {
            'id': key, 'author_id': row['author_id'],
            'name': row['author_name'], 'dataset': row['dataset']
        }
        docs.append((row, text, set(text.split()), ts, key, content_urls(row['body'])))

    matches = {}
    # Prevent quadratic memory growth if hundreds of accounts post identical text.
    candidate_limit = 12000
    candidate_cap_hit = False
    for i, (a, a_text, a_tokens, a_ts, a_id, a_urls) in enumerate(docs):
        for b, b_text, b_tokens, b_ts, b_id, b_urls in docs[i+1:]:
            if a_id == b_id or a['dataset'] != b['dataset']:
                continue
            gap = abs(a_ts - b_ts) / 60
            if gap > window_minutes:
                continue
            if abs(len(a_text)-len(b_text)) > max(len(a_text), len(b_text)) * 0.25:
                continue
            if len(a_tokens & b_tokens) < 3:
                continue
            score = similarity(a_text, b_text)
            if score < threshold:
                continue
            left_id, right_id = sorted((a_id, b_id))
            left, right = (a,b) if a_id == left_id else (b,a)
            shared = a_urls & b_urls
            key = (left_id, right_id)
            if key not in matches and len(matches) >= candidate_limit:
                candidate_cap_hit = True
                continue
            edge = matches.setdefault(key, {
                'source': left_id, 'target': right_id,
                'dataset': a['dataset'], 'matched_pairs': 0,
                'max_similarity': 0.0, 'min_gap_minutes': None,
                'comment_ids': set(), 'videos': set(),
                'shared_links': set(), 'examples': []
            })
            edge['matched_pairs'] += 1
            edge['max_similarity'] = max(edge['max_similarity'], score)
            edge['min_gap_minutes'] = (gap if edge['min_gap_minutes'] is None else
                                       min(edge['min_gap_minutes'], gap))
            edge['comment_ids'].update((a['comment_id'], b['comment_id']))
            edge['videos'].update((a['video_id'], b['video_id']))
            edge['shared_links'].update(shared)
            if len(edge['examples']) < 3:
                edge['examples'].append({
                    'similarity': round(score, 3), 'gap_minutes': round(gap, 1),
                    'left': {'comment_id': left['comment_id'], 'text': left['body'][:500],
                             'url': left['source_url'], 'published_at': left['published_at']},
                    'right': {'comment_id': right['comment_id'], 'text': right['body'][:500],
                              'url': right['source_url'], 'published_at': right['published_at']}
                })
    ranked = sorted(matches.values(),
                    key=lambda e: (-len(e['comment_ids']), -e['matched_pairs'],
                                   -e['max_similarity'], e['source'], e['target']))
    selected = []
    chosen = set()
    for e in ranked:
        new_nodes = {e['source'], e['target']} - chosen
        if len(chosen) + len(new_nodes) > max_nodes:
            continue
        selected.append(e)
        chosen.update(new_nodes)
        if len(selected) >= max_edges:
            break
    nodes = []
    for author_id in sorted(chosen):
        account_comments = [row for row in records if row['dataset'] + ':' + str(row['author_id']) == author_id]
        nodes.append({**authors[author_id], 'observed_comments': len(account_comments),
                      'observed_videos': len({r['video_id'] for r in account_comments})})
    edges = []
    for e in selected:
        edges.append({
            'id': e['source'] + '|' + e['target'], 'source': e['source'],
            'target': e['target'], 'dataset': e['dataset'],
            'matched_pairs': e['matched_pairs'],
            'distinct_comments': len(e['comment_ids']),
            'max_similarity': round(e['max_similarity'], 3),
            'min_gap_minutes': round(e['min_gap_minutes'], 1),
            'observed_videos': len(e['videos']),
            'shared_links': sorted(e['shared_links'])[:5],
            'examples': e['examples'],
            'interpretation': 'Kesamaan konten dan waktu publikasi; bukan bukti bot atau koordinasi yang disengaja.'
        })
    return {
        'nodes': nodes, 'edges': edges,
        'settings': {'similarity_threshold': threshold, 'time_window_minutes': window_minutes},
        'analyzed_comments': len(records),
        'eligible_comments': len(docs),
        'total_candidate_edges': len(matches),
        'truncated': len(selected) < len(matches) or candidate_cap_hit,
        'candidate_cap_hit': candidate_cap_hit,
        'method': 'Pasangan komentar hampir identik dari ID akun berbeda dalam dataset sama dan rentang waktu dipilih.',
        'warning': 'Graf hanya menunjukkan kemiripan aktivitas yang teramati, bukan identitas, otomatisasi, click farm, atau kebenaran klaim.'
    }


def valid_http_url(s):
    try:
        u = urllib.parse.urlsplit(s)
        return (u.scheme in ('http', 'https') and bool(u.hostname) and
                not u.username and not u.password and len(s) <= 1500)
    except ValueError:
        return False


def create_investigation(conn, payload):
    comment_id = str(payload.get('comment_id') or '').strip()
    row = conn.execute('SELECT comment_id FROM comments WHERE comment_id=?', (comment_id,)).fetchone()
    if not row:
        raise ValueError('Komentar sumber tidak ditemukan.')
    claim = str(payload.get('claim') or '').strip()[:1000]
    verdict = str(payload.get('verdict') or 'belum_terverifikasi')
    url = str(payload.get('evidence_url') or '').strip()
    title = str(payload.get('evidence_title') or '').strip()[:240]
    date = str(payload.get('source_date') or '').strip()[:50]
    note = str(payload.get('note') or '').strip()[:1000]
    if not claim:
        raise ValueError('Klaim harus diisi.')
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError('Status verifikasi tidak valid.')
    if not valid_http_url(url) or not title:
        raise ValueError('Masukkan URL http(s) dan judul sumber yang akan diperiksa.')
    if verdict == 'bertentangan_bukti':
        intro = 'Klarifikasi: klaim berikut perlu ditinjau karena terdapat sumber yang menyajikan informasi berbeda:'
    elif verdict == 'didukung_bukti':
        intro = 'Informasi tambahan: ada sumber yang mendukung klaim berikut:'
    else:
        intro = 'Klaim berikut belum dapat dipastikan hanya dari bukti yang tersedia:'
    draft = f'{intro} “{claim}”\nRujukan untuk diperiksa: {title} — {url}'
    if date:
        draft += f' (tanggal sumber: {date})'
    if note:
        draft += '\nCatatan konteks: ' + note
    draft += '\nMohon periksa konteks, tanggal, dan sumber sebelum menyimpulkan.'
    cursor = conn.execute('''INSERT INTO investigations
       (comment_id,claim,verdict,evidence_url,evidence_title,source_date,note,draft,created_at)
       VALUES (?,?,?,?,?,?,?,?,?)''',
       (comment_id, claim, verdict, url, title, date, note, draft, utc_now()))
    conn.commit()
    return {'id': cursor.lastrowid, 'status':'draft', 'draft':draft}


def approve_investigation(conn, identifier):
    row = conn.execute('SELECT id,status FROM investigations WHERE id=?', (identifier,)).fetchone()
    if not row:
        raise ValueError('Draft tidak ditemukan.')
    if row['status'] != 'draft':
        raise ValueError('Draft sudah disetujui sebelumnya.')
    conn.execute('UPDATE investigations SET status=?, approved_at=? WHERE id=?',
                 ('approved', utc_now(), identifier))
    conn.commit()
    return {'id': identifier, 'status': 'approved', 'published': False,
            'note': 'Hanya disetujui untuk disalin secara manual. Tidak ada posting otomatis.'}


def overview(conn, dataset='all', similarity_threshold=0.88, window_minutes=30):
    if dataset not in ('demo', 'real', 'all'):
        raise ValueError('Filter dataset tidak valid.')
    where = '' if dataset=='all' else ' WHERE dataset=?'
    params = () if dataset=='all' else (dataset,)
    rows = [dict(r) for r in conn.execute('SELECT * FROM comments'+where+' ORDER BY published_at DESC LIMIT 500', params)]
    stats = {
        'comments': len(rows), 'authors_with_id': len({r['author_id'] for r in rows if r['author_id']}),
        'videos': len({r['video_id'] for r in rows}),
        'real': sum(r['dataset']=='real' for r in rows),
        'demo': sum(r['dataset']=='demo' for r in rows)
    }
    clusters = find_clusters(rows)
    network = build_activity_network(rows, similarity_threshold, window_minutes)
    drafts = [dict(r) for r in conn.execute('''SELECT i.*, c.author_name, c.dataset FROM investigations i
                           JOIN comments c ON c.comment_id=i.comment_id
                           ORDER BY i.id DESC LIMIT 100''')]
    if dataset != 'all':
        drafts = [r for r in drafts if r['dataset']==dataset]
    return {'stats':stats, 'clusters':clusters, 'network':network, 'comments':rows[:200], 'drafts':drafts,
            'limit_notice':'Analisis maksimal 500 komentar terbaru per filter; tabel menampilkan 200.'}


SCAN_LOCK = threading.Lock()
COLLECTION_LOCK = threading.Lock()
RADAR_LOCK = threading.Lock()
YOUTUBE_RADAR_LOCK = threading.Lock()
YOUTUBE_AI_LOCK = threading.Lock()
YOUTUBE_REPLY_LOCK = threading.Lock()
COMMENT_ANALYSIS_LOCK = threading.Lock()
SIMULATION_LOCK = threading.Lock()

def launch_comment_analysis(db_path,ident):
    def worker():
        try:comment_analysis.run(db_path,ident,os.getenv('YOUTUBE_API_KEY','').strip(),youtube_radar.request_api,fetch_youtube_page,store_youtube_thread)
        finally:COMMENT_ANALYSIS_LOCK.release()
    threading.Thread(target=worker,daemon=True,name='comment-analysis').start()


def launch_radar_scan(db_path, feed_id):
    # Caller has acquired RADAR_LOCK; one radar scan can run at a time.
    def worker():
        try:
            with open_db(db_path) as conn:
                topic_radar.scan_feed(conn, feed_id, fetcher=topic_radar.fetch_public)
        except (ValueError, RuntimeError, sqlite3.Error):
            pass  # Failures inside scan_feed are retained as scan history.
        finally:
            RADAR_LOCK.release()
    threading.Thread(target=worker, name='cakra-radar', daemon=True).start()

def fetch_bluesky_page(_key, keyword, cursor):
    params = {'q':intelligence.checked_keyword(keyword), 'limit':30, 'sort':'latest'}
    if cursor: params['cursor'] = cursor
    data = intelligence.request_json(intelligence.BLUESKY_SEARCH + '?' + urllib.parse.urlencode(params))
    posts = data.get('posts')
    if not isinstance(posts, list): raise ValueError('Respons Bluesky tidak valid.')
    return {'items':posts, 'nextPageToken':data.get('cursor')}

def store_bluesky_post(conn, keyword, raw):
    parsed = intelligence.parse_bluesky({'posts':[raw]})
    if not parsed: return False
    ident = intelligence.save_observation(conn, parsed[0])
    conn.execute("INSERT OR IGNORE INTO intel_topics(keyword,platform,created_at) VALUES(?,'bluesky',?)",(keyword,utc_now()))
    topic = conn.execute("SELECT id FROM intel_topics WHERE keyword=? AND platform='bluesky'",(keyword,)).fetchone()[0]
    conn.execute('INSERT OR IGNORE INTO intel_topic_observations(topic_id,observation_id) VALUES(?,?)',(topic,ident))
    return True

def launch_collection(db_path, job_id, key, source):
    # Caller holds COLLECTION_LOCK; each process runs at most one bounded worker.
    def worker():
        try:
            collection_jobs.run(db_path, job_id, key, fetch_youtube_page if source=='youtube' else fetch_bluesky_page, store_youtube_thread if source=='youtube' else store_bluesky_post)
        finally:
            COLLECTION_LOCK.release()
    threading.Thread(target=worker, name='cakra-collection', daemon=True).start()



class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    # Enabled only by isolated test subclasses using a non-operational database.
    allow_test_fixtures = False

    def simulation_path(self):
        path=Path(self.db_path).resolve().with_name('cakra_demo.db')
        if path.is_symlink():raise ValueError('Database simulasi tidak boleh berupa symlink.')
        path=path.resolve()
        real_paths=(Path(self.db_path).resolve(), DEFAULT_DB.resolve())
        if path in real_paths or (path.exists() and any(p.exists() and path.samefile(p) for p in real_paths)):
            raise ValueError('Database simulasi harus terpisah dari database nyata.')
        return path

    def simulation_get(self, parsed):
        q=urllib.parse.parse_qs(parsed.query)
        with open_db(self.simulation_path()) as conn:
            simulation.init(conn)
            comment_analysis.recover(conn)
            ident=q.get('id',[''])[0]
            if parsed.path=='/api/simulation/jobs': result={'jobs':comment_analysis.listing(conn)}
            elif parsed.path=='/api/simulation/job': result=comment_analysis.get(conn,ident)
            elif parsed.path=='/api/simulation/findings': result=simulation.result(conn,ident,q.get('page',['1'])[0],q.get('category',['all'])[0],q.get('ai_page',['1'])[0])
            elif parsed.path=='/api/simulation/finding': result=comment_analysis.finding_detail(conn,ident,q.get('page',['1'])[0])
            elif parsed.path=='/api/simulation/cases': result=investigation.list_cases(conn)
            elif parsed.path=='/api/simulation/case': result=investigation.case_detail(conn,ident)
            elif parsed.path=='/api/simulation/facts': result=unified_fact.list_cases(conn,q.get('investigation_id',[''])[0])
            elif parsed.path=='/api/simulation/status': result=ai_providers.status(conn)
            else: result=None
        if result is None:return self.response(404,{'error':'Tidak ditemukan.'})
        result['simulation_label']=simulation.LABEL
        return self.response(200,result)

    def simulation_post(self, request):
        if self.path=='/api/simulation/preview':
            rows=simulation.parse(request.get('text'),request.get('format'))
            return self.response(200,{'count':len(rows),'preview':rows[:5],'simulation_label':simulation.LABEL})
        if self.path in ('/api/simulation/start','/api/simulation/resume','/api/simulation/clear'):
            if not SIMULATION_LOCK.acquire(blocking=False):raise ValueError('Analisis simulasi sedang berjalan.')
            launched=False
            try:
                if self.path=='/api/simulation/clear' and request.get('confirmed') is not True:raise ValueError('Konfirmasi penghapusan diperlukan.')
                rows=simulation.parse(request.get('text'),request.get('format')) if self.path.endswith('/start') else None
                path=self.simulation_path()
                with open_db(path) as conn:
                    simulation.init(conn)
                    if self.path.endswith('/clear'):
                        simulation.clear(conn)
                        ident=None
                    elif self.path.endswith('/resume'):
                        ident=comment_analysis.resume(conn,request.get('id'),request.get('provider'),request.get('model'))['id']
                    else:
                        ident=simulation.create(conn,rows,request.get('provider','muse'),request.get('model',''),request.get('ai_enabled',False))
                if ident is None:
                    return self.response(200,{'cleared':True,'simulation_label':simulation.LABEL})
                def worker():
                    try: simulation.run(path,ident)
                    finally: SIMULATION_LOCK.release()
                threading.Thread(target=worker,name='simulation-analysis',daemon=True).start()
                launched=True
                return self.response(200,{'id':ident,'simulation_label':simulation.LABEL})
            finally:
                if not launched:SIMULATION_LOCK.release()
        with open_db(self.simulation_path()) as conn:
            simulation.init(conn)
            if self.path=='/api/simulation/investigate': result=comment_analysis.open_case(conn,request.get('finding_id'),dataset='demo')
            elif self.path=='/api/simulation/note': result=investigation.add_note(conn,request.get('case_id'),request.get('note'))
            elif self.path=='/api/simulation/review': result=investigation.review(conn,request.get('case_id'),request.get('verdict'),request.get('note'))
            elif self.path=='/api/simulation/status': result=investigation.change_status(conn,request.get('case_id'),request.get('status'))
            elif self.path=='/api/simulation/evidence': result=unified_fact.add_evidence(conn,request)
            elif self.path=='/api/simulation/fact-review': result=unified_fact.review(conn,request.get('case_id'),request.get('verdict'),request.get('note'),request.get('sources_reviewed',False))
            else: result=None
        if result is None:return self.response(404,{'error':'Tidak ditemukan.'})
        result['simulation_label']=simulation.LABEL
        return self.response(200,result)

    def _test_fixtures(self):
        return self.allow_test_fixtures and Path(self.db_path).resolve() != DEFAULT_DB.resolve()

    def log_message(self, format, *args):
        sys.stderr.write('%s %s\n' % (self.address_string(), format % args))

    def response(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'")
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith('/api/simulation/'):
            try:return self.simulation_get(parsed)
            except (ValueError,TypeError,sqlite3.Error) as exc:return self.response(400,{'error':str(exc)})
        if not self._test_fixtures():
            archived_assets=('/regional.js','/regional.css','/collection-ui.js','/ai-ui.js','/screening-ui.js','/unified-ui.js','/radar-ui.js','/youtube-radar-ui.js')
            if parsed.path in ('/api/radar','/api/radar/issue') or parsed.path in archived_assets or parsed.path.startswith(('/api/youtube-radar','/api/regional/')):
                return self.response(404, {'error':'Fitur arsip tidak aktif.'})
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if any(k == 'dataset' and v == 'demo' for k,v in query):
                return self.response(400, {'error':'Hanya data nyata tersedia.'})
            query = [(k, 'real' if k == 'dataset' and v == 'all' else v) for k,v in query]
            parsed = parsed._replace(query=urllib.parse.urlencode(query))
            if parsed.path == '/api/release':
                return self.response(404, {'error':'Tidak ditemukan.'})
        if parsed.path == '/':
            data = (BASE/'dashboard.html').read_bytes()
            marker = b'<!--CAKRA:PRELOADER-->'
            fragment = BASE/'preloader.html'
            if marker in data and fragment.is_file():
                data = data.replace(marker, fragment.read_bytes())
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https://i.ytimg.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == '/peta-indonesia.webp':
            image = BASE / 'assets' / 'peta-indonesia.webp'
            if not image.is_file():
                self.send_error(404)
                return
            data = image.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/webp')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == '/background.jpg':
            # Optional user-supplied dashboard background; no general file serving.
            background = BASE / 'assets' / 'background.jpg'
            if not background.is_file():
                self.send_error(404)
                return
            data = background.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path in ('/comment-workspace.js', '/comment-workspace.css'):
            path = BASE / parsed.path.lstrip('/')
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript; charset=utf-8' if path.suffix == '.js' else 'text/css; charset=utf-8')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == '/api/regional/map':
            if not regional.MAP_PATH.is_file():
                return self.response(503, {'error':'Dataset batas provinsi belum terpasang. Jalankan python regional.py saat koneksi internet tersedia.'})
            try:
                data = regional.normalize_map(json.loads(regional.MAP_PATH.read_text(encoding='utf-8')))
                return self.response(200, data)
            except (ValueError, OSError, TypeError) as exc:
                return self.response(503, {'error':'Dataset peta tidak valid: '+str(exc)})
        elif parsed.path == '/api/radar':
            with open_db(self.db_path) as conn:
                return self.response(200, topic_radar.dashboard(conn))
        elif parsed.path == '/api/youtube-radar':
            try:
                with open_db(self.db_path) as conn:
                    return self.response(200, youtube_radar.dashboard(conn,urllib.parse.parse_qs(parsed.query).get('page',['1'])[0]))
            except (ValueError,sqlite3.Error) as exc:
                return self.response(400, {'error':str(exc)})
        elif parsed.path in ('/api/analysis/jobs','/api/analysis/job','/api/analysis/findings','/api/analysis/finding'):
            try:
                q=urllib.parse.parse_qs(parsed.query)
                with open_db(self.db_path) as conn:
                    comment_analysis.recover(conn)
                    if parsed.path.endswith('/jobs'):result={'jobs':comment_analysis.listing(conn,q.get('video_id',[None])[0])}
                    elif parsed.path.endswith('/job'):result=comment_analysis.get(conn,q.get('id',[''])[0])
                    elif parsed.path.endswith('/finding'):result=comment_analysis.finding_detail(conn,q.get('id',[''])[0],q.get('page',['1'])[0])
                    else:result=comment_analysis.findings(conn,q.get('id',[''])[0],q.get('page',['1'])[0],q.get('category',['all'])[0],q.get('ai_page',['1'])[0])
                    return self.response(200,result)
            except (ValueError,TypeError,sqlite3.Error) as exc:return self.response(400,{'error':str(exc)})
        elif parsed.path == '/api/youtube-radar/video':
            try:
                with open_db(self.db_path) as conn:
                    return self.response(200, youtube_radar.get_video(conn,urllib.parse.parse_qs(parsed.query).get('id',[''])[0]))
            except (ValueError,sqlite3.Error) as exc:
                return self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/youtube-radar/regional':
            try:
                with open_db(self.db_path) as conn:
                    return self.response(200, youtube_radar.regional_videos(conn,urllib.parse.parse_qs(parsed.query).get('province',[''])[0]))
            except (ValueError,sqlite3.Error) as exc:
                return self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/youtube-radar/replies':
            with open_db(self.db_path) as conn:
                rows=[dict(r) for r in conn.execute('SELECT id,job_id,video_id,status,pages_count,retrieved_count,error,updated_at FROM yt_radar_reply_jobs ORDER BY id DESC LIMIT 100')]
                return self.response(200,{'jobs':rows})
        elif parsed.path == '/api/youtube-radar/query-plans':
            with open_db(self.db_path) as conn:
                proposals=[dict(r) for r in conn.execute("SELECT id,province_code,query,provider,model,status,created_at FROM yt_query_proposals WHERE status='proposed' ORDER BY id DESC LIMIT 100")]
                jobs=[dict(r) for r in conn.execute('SELECT id,province_code,provider,model,status,error,updated_at FROM yt_query_plan_jobs ORDER BY id DESC LIMIT 30')]
                return self.response(200,{'proposals':proposals,'jobs':jobs})
        elif parsed.path == '/api/radar/issue':
            q = urllib.parse.parse_qs(parsed.query)
            try:
                with open_db(self.db_path) as conn:
                    return self.response(200, topic_radar.issue_detail(conn, q.get('id', [''])[0]))
            except (ValueError, sqlite3.Error) as exc:
                return self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/screening/runs':
            with open_db(self.db_path) as conn:
                return self.response(200, {'runs':[dict(r) for r in conn.execute('SELECT * FROM screening_runs ORDER BY id DESC LIMIT 50')]})
        elif parsed.path in ('/api/screening/results','/api/screening/detail'):
            q=urllib.parse.parse_qs(parsed.query)
            try:
                with open_db(self.db_path) as conn:
                    if parsed.path.endswith('/detail'):
                        return self.response(200, screening.detail(conn,q.get('id',[''])[0]))
                    return self.response(200, screening.results(conn,q.get('run_id',[''])[0],q.get('decision',['relevant'])[0],q.get('page',['1'])[0],q.get('page_size',['25'])[0]))
            except (ValueError, TypeError, sqlite3.Error) as exc:
                return self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/ai/diagnostics':
            with open_db(self.db_path) as conn:
                return self.response(200, ai_providers.diagnostics(conn))
        elif parsed.path == '/api/ai/status':
            with open_db(self.db_path) as conn:
                return self.response(200, ai_providers.status(conn))
        elif parsed.path == '/api/collection/jobs':
            with open_db(self.db_path) as conn:
                return self.response(200, {'jobs':collection_jobs.listing(conn)})
        elif parsed.path == '/api/regional/events':
            q = urllib.parse.parse_qs(parsed.query)
            try:
                with open_db(self.db_path) as conn:
                    result = regional.list_events(conn, *(q.get(k, [v])[0] for k,v in [('dataset','real'),('province',''),('category',''),('days','7'),('verification',''),('source','')]))
                    result['provinces'] = regional.PROVINCES
                    return self.response(200, result)
            except (ValueError, sqlite3.Error) as exc:
                return self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/release':
            try:
                with open_db(self.db_path) as conn:
                    self.response(200, release.readiness(conn, BASE))
            except sqlite3.Error:
                self.response(503, {'error':'Pemeriksaan kesiapan database gagal.'})
        elif parsed.path == '/api/health':
            self.response(200, {'ok': True, 'version': release.VERSION, 'service': 'CAKRA local-only'})
        elif parsed.path == '/api/factcheck/cases':
            query = urllib.parse.parse_qs(parsed.query)
            try:
                with open_db(self.db_path) as conn:
                    self.response(200, factcheck.list_cases(conn, query.get('dataset', ['real'])[0]))
            except (ValueError, sqlite3.Error) as exc:
                self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/unified/cases':
            try:
                q = urllib.parse.parse_qs(parsed.query)
                with open_db(self.db_path) as conn:
                    self.response(200, unified_fact.list_cases(conn,q.get('investigation_id',[None])[0]))
            except (ValueError,sqlite3.Error) as exc:
                self.response(400,{'error':str(exc)})
        elif parsed.path == '/api/investigations':
            with open_db(self.db_path) as conn:
                query = urllib.parse.parse_qs(parsed.query)
                if 'id' in query:
                    try:
                        result = investigation.case_detail(conn,query['id'][0])
                        if not self._test_fixtures() and result['dataset']=='demo':return self.response(404,{'error':'Tidak ditemukan.'})
                    except ValueError as exc:
                        return self.response(400,{'error':str(exc)})
                else:
                    result = investigation.list_cases(conn)
                    if not self._test_fixtures():result['cases']=[case for case in result['cases'] if case['dataset']=='real']
                self.response(200,result)
        elif parsed.path == '/api/investigations/report':
            query = urllib.parse.parse_qs(parsed.query)
            try:
                ident = int(query.get('id',['-1'])[0])
                fmt = query.get('format',['json'])[0]
                if fmt not in ('json','md'):
                    raise ValueError('Format laporan tidak dikenal.')
                with open_db(self.db_path) as conn:
                    if fmt == 'json':
                        payload = json.dumps(investigation.report_json(conn,ident),ensure_ascii=False,indent=2).encode('utf-8')
                        mimetype='application/json; charset=utf-8'
                        suffix='json'
                    else:
                        payload = investigation.report_markdown(conn,ident).encode('utf-8')
                        mimetype='text/markdown; charset=utf-8'
                        suffix='md'
                self.send_response(200)
                self.send_header('Content-Type',mimetype)
                self.send_header('Content-Disposition',f'attachment; filename="cakra-case-{ident}.{suffix}"')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'none'")
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError,sqlite3.Error) as exc:
                self.response(400,{'error':str(exc)})
        elif parsed.path == '/api/evaluation':
            try:
                q = urllib.parse.parse_qs(parsed.query)
                with open_db(self.db_path) as conn:
                    self.response(200, evaluation.dashboard(conn,q.get('dataset',['real'])[0]))
            except (ValueError,sqlite3.Error) as exc:
                self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/evaluation/sample':
            try:
                q = urllib.parse.parse_qs(parsed.query)
                with open_db(self.db_path) as conn:
                    self.response(200, evaluation.sample(conn,q.get('dataset',['real'])[0],
                                      q.get('limit',[80])[0]))
            except (ValueError,sqlite3.Error) as exc:
                self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/evaluation/audit':
            try:
                q = urllib.parse.parse_qs(parsed.query)
                with open_db(self.db_path) as conn:
                    self.response(200, evaluation.audit_log(conn,q.get('dataset',['real'])[0],
                                      q.get('limit',[40])[0]))
            except (ValueError,sqlite3.Error) as exc:
                self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/evaluation/report':
            q = urllib.parse.parse_qs(parsed.query)
            try:
                dataset = q.get('dataset',['real'])[0]
                fmt = q.get('format',['md'])[0]
                if fmt not in ('md','json'):
                    raise ValueError('Format laporan harus md atau json.')
                with open_db(self.db_path) as conn:
                    if fmt=='json':
                        payload=json.dumps(evaluation.report_json(conn,dataset),ensure_ascii=False,indent=2).encode('utf-8')
                        mime='application/json; charset=utf-8'
                    else:
                        payload=evaluation.report_markdown(conn,dataset).encode('utf-8')
                        mime='text/markdown; charset=utf-8'
                self.send_response(200)
                self.send_header('Content-Type',mime)
                self.send_header('Content-Disposition',f'attachment; filename="cakra-evaluation-{dataset}.{fmt}"')
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'none'")
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError,sqlite3.Error) as exc:
                self.response(400,{'error':str(exc)})
        elif parsed.path == '/api/discovery':
            try:
                q = urllib.parse.parse_qs(parsed.query)
                with open_db(self.db_path) as conn:
                    self.response(200, discovery.dashboard(conn,q.get('dataset',['real'])[0],
                                  q.get('status',['all'])[0],q.get('limit',[120])[0]))
            except (ValueError,sqlite3.Error) as exc:
                self.response(400,{'error':str(exc)})
        elif parsed.path == '/api/monitor':
            with open_db(self.db_path) as conn:
                self.response(200, monitoring.dashboard(conn))
        elif parsed.path == '/api/intel':
            try:
                with open_db(self.db_path) as conn:
                    self.response(200, intelligence.dashboard_data(conn))
            except (ValueError, sqlite3.Error) as exc:
                self.response(400, {'error':str(exc)})
        elif parsed.path == '/api/overview':
            query = urllib.parse.parse_qs(parsed.query)
            dataset = query.get('dataset', ['real'])[0]
            try:
                threshold = float(query.get('threshold', ['0.88'])[0])
                window = int(query.get('window', ['30'])[0])
                with open_db(self.db_path) as conn:
                    self.response(200, overview(conn, dataset, threshold, window))
            except (ValueError, OverflowError) as exc:
                self.response(400, {'error':str(exc)})
        else:
            self.response(404, {'error':'Tidak ditemukan.'})

    def do_POST(self):
        # Same-origin-only local UI; no cross-origin API calls.
        origin = self.headers.get('Origin', '')
        host = self.headers.get('Host', '')
        if (self.headers.get('X-CAKRA-Request') != '1' or
            (origin and urllib.parse.urlsplit(origin).netloc != host)):
            return self.response(403, {'error':'Permintaan tidak diizinkan.'})
        if self.path == '/api/regional/bmkg':
            return self.response(404, {'error':'Tidak ditemukan.'})
        if not self._test_fixtures() and (self.path.startswith('/api/radar/') or self.path.startswith('/api/youtube-radar/')):
            return self.response(404, {'error':'Radar berita tidak aktif.'})
        if not self._test_fixtures() and (self.path.startswith('/api/regional/') or self.path in ('/api/intel/scan','/api/monitor/scan','/api/monitor/config')):
            return self.response(404, {'error':'Fitur arsip tidak aktif.'})
        if not self._test_fixtures() and self.path in ('/api/regional/demo','/api/release/demo','/api/demo','/api/monitor/demo','/api/intel/demo'):
            return self.response(404, {'error':'Tidak ditemukan.'})
        try:
            length = int(self.headers.get('Content-Length', '-1'))
            if not 0 <= length <= (1250000 if self.path.startswith('/api/simulation/') else 32768):
                raise ValueError('Body terlalu besar atau panjang tidak valid.')
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError('JSON harus berupa object.')
            if self.path.startswith('/api/simulation/'):
                return self.simulation_post(request)
            if not self._test_fixtures() and request.get('dataset') == 'demo':
                raise ValueError('Hanya data nyata tersedia.')
            with open_db(self.db_path) as conn:
                if self.path == '/api/analysis/start':
                    video_id=parse_video_id(request.get('url'))
                    if not COMMENT_ANALYSIS_LOCK.acquire(blocking=False):raise ValueError('Analisis lain sedang berjalan.')
                    try:
                        result=comment_analysis.create(conn,video_id,request.get('provider','muse'),request.get('model',''),request.get('include_replies',True),request.get('max_requests',200))
                        launch_comment_analysis(self.db_path,result['id'])
                    except Exception:
                        COMMENT_ANALYSIS_LOCK.release();raise
                elif self.path == '/api/analysis/resume':
                    if not COMMENT_ANALYSIS_LOCK.acquire(blocking=False):raise ValueError('Analisis lain sedang berjalan.')
                    try:
                        result=comment_analysis.resume(conn,request.get('id'),request.get('provider'),request.get('model'))
                        launch_comment_analysis(self.db_path,result['id'])
                    except Exception:
                        COMMENT_ANALYSIS_LOCK.release();raise
                elif self.path == '/api/analysis/cancel':
                    result=comment_analysis.cancel(conn,request.get('id'))
                elif self.path == '/api/analysis/investigate':
                    result=comment_analysis.open_case(conn,request.get('finding_id'))
                elif self.path == '/api/data/clear':
                    if request.get('confirmed') is not True:raise ValueError('Konfirmasi penghapusan diperlukan.')
                    if not COMMENT_ANALYSIS_LOCK.acquire(blocking=False):raise ValueError('Analisis sedang berjalan; hapus data setelah selesai.')
                    try:
                        if not COLLECTION_LOCK.acquire(blocking=False):raise ValueError('Pengumpulan data sedang berjalan; hapus data setelah selesai.')
                        try:result=clear_real_data(conn)
                        finally:COLLECTION_LOCK.release()
                    finally:COMMENT_ANALYSIS_LOCK.release()
                elif self.path == '/api/radar/feed':
                    result = topic_radar.add_feed(conn,request.get('name'),request.get('url'),request.get('authorized'),request.get('interval_minutes',120))
                elif self.path == '/api/radar/gdelt':
                    result = topic_radar.add_gdelt(conn,request.get('query'),request.get('authorized'),request.get('interval_minutes',120))
                elif self.path == '/api/radar/feed/status':
                    result = topic_radar.set_feed(conn,request.get('id'),request.get('enabled'))
                elif self.path == '/api/radar/settings':
                    result = topic_radar.set_auto(conn,request.get('auto_enabled'))
                elif self.path == '/api/radar/scan':
                    ident = topic_radar.can_scan(conn,request.get('id'))
                    if not RADAR_LOCK.acquire(blocking=False): raise ValueError('Pemindaian radar lain sedang berjalan.')
                    try:
                        launch_radar_scan(self.db_path,ident)
                    except Exception:
                        RADAR_LOCK.release()
                        raise
                    result = {'started':True,'feed_id':ident}
                elif self.path == '/api/radar/investigate':
                    result = topic_radar.investigate(conn,request.get('id'))
                elif self.path == '/api/youtube-radar/settings':
                    result = youtube_radar.configure(conn,request.get('enabled'),request.get('mode'),request.get('search_limit'),request.get('metadata_limit'),request.get('replies_limit'))
                elif self.path == '/api/youtube-radar/review':
                    result = youtube_radar.review(conn,request.get('video_id'),request.get('decision'),request.get('note'))
                elif self.path == '/api/youtube-radar/location':
                    result = youtube_radar.link_location(conn,request.get('video_id'),request.get('province_code'),request.get('quote'),request.get('confirmed'))
                elif self.path == '/api/youtube-radar/analyze':
                    result = youtube_radar.enqueue_analysis(conn,request.get('video_id'),request.get('provider'),request.get('model'),request.get('fallback',False),request.get('mode','metadata'))
                elif self.path == '/api/youtube-radar/query-plan':
                    result = youtube_radar.enqueue_query_plan(conn,request.get('province_code'),request.get('provider'),request.get('model'),request.get('fallback',False))
                elif self.path == '/api/youtube-radar/query-approve':
                    result = youtube_radar.apply_query_proposal(conn,request.get('id'))
                elif self.path == '/api/youtube-radar/investigate':
                    result = youtube_radar.investigate(conn,request.get('video_id'))
                elif self.path == '/api/youtube-radar/collect':
                    video=youtube_radar.get_video(conn,request.get('video_id'))
                    if video['relevance_status']!='RELEVANT': raise ValueError('Tinjau dan tandai video relevan sebelum mengumpulkan komentar.')
                    if video['source_status']!='available': raise ValueError('Video tidak tersedia atau metadata tidak lengkap; periksa sumber sebelum mengumpulkan komentar.')
                    key=os.getenv('YOUTUBE_API_KEY','').strip()
                    if not key: raise ValueError('YOUTUBE_API_KEY belum disetel.')
                    include_replies=request.get('include_replies',False)
                    if type(include_replies) is not bool: raise ValueError('Pilihan balasan tidak valid.')
                    if not COLLECTION_LOCK.acquire(blocking=False): raise ValueError('Pekerjaan lain sedang berjalan.')
                    try:
                        conn.execute('BEGIN IMMEDIATE')
                        video=youtube_radar.get_video(conn,request.get('video_id'))
                        if video['relevance_status']!='RELEVANT' or video['source_status']!='available': raise ValueError('Status video berubah; tinjau kembali sebelum mengumpulkan komentar.')
                        result=collection_jobs.new(conn,video['video_id'],request.get('pages_per_run',20),'youtube',commit=False)
                        conn.execute('INSERT INTO yt_radar_collection_jobs(video_id,job_id,created_at,include_replies) VALUES(?,?,?,?)',(video['video_id'],result['id'],youtube_radar.now(),int(include_replies)))
                        conn.commit();launch_collection(self.db_path,result['id'],key,'youtube')
                    except Exception:
                        conn.rollback();COLLECTION_LOCK.release();raise
                elif self.path == '/api/youtube-radar/replies/resume':
                    result=youtube_radar.resume_reply(conn,request.get('id'))
                elif self.path == '/api/youtube-radar/scan':
                    if not YOUTUBE_RADAR_LOCK.acquire(blocking=False): raise ValueError('Siklus radar sedang berjalan.')
                    def run_youtube_radar():
                        try:
                            with open_db(self.db_path) as worker_conn:
                                youtube_radar.run_cycle(worker_conn,os.getenv('YOUTUBE_API_KEY','').strip(),force=True)
                        finally: YOUTUBE_RADAR_LOCK.release()
                    try: threading.Thread(target=run_youtube_radar,daemon=True,name='youtube-radar-manual').start()
                    except Exception:
                        YOUTUBE_RADAR_LOCK.release();raise
                    result={'started':True}
                elif self.path == '/api/ai/diagnostic':
                    result = ai_providers.diagnostic(conn,request.get('provider',''),request.get('model',''),request.get('kind',''),request.get('confirmed',False))
                elif self.path == '/api/ai/models':
                    result = ai_providers.models(conn,request.get('provider',''))
                elif self.path == '/api/screening/start':
                    result = screening.start(conn,request.get('query',''),request.get('source','all'),request.get('job_id'))
                elif self.path == '/api/screening/batch':
                    result = screening.run_batch(conn,request.get('id'),request.get('limit',150))
                elif self.path == '/api/screening/review':
                    result = screening.review(conn,request.get('id'))
                elif self.path == '/api/screening/ai':
                    result = screening.ai_review(conn,request.get('id'),request,ai_providers)
                elif self.path == '/api/screening/ai-batch':
                    result = screening.ai_batch(conn,request.get('run_id'),request.get('ids'),request,ai_providers)
                elif self.path == '/api/ai/analyze':
                    result = ai_providers.analyze(conn, request)
                elif self.path == '/api/collection/start':
                    source = request.get('source','youtube')
                    if not self._test_fixtures() and source!='youtube':raise ValueError('Hanya sumber YouTube tersedia.')
                    if source not in ('youtube','bluesky'): raise ValueError('Konektor belum tersedia.')
                    key = os.getenv('YOUTUBE_API_KEY', '').strip() if source=='youtube' else ''
                    if source=='youtube' and not key: raise ValueError('YOUTUBE_API_KEY belum disetel.')
                    video_id = parse_video_id(request.get('video')) if source=='youtube' else intelligence.checked_keyword(request.get('query'))
                    issue_id = request.get('issue_id')
                    if issue_id is not None: topic_radar.issue_detail(conn, issue_id)
                    if not COLLECTION_LOCK.acquire(blocking=False): raise ValueError('Pekerjaan lain sedang berjalan.')
                    try:
                        result = collection_jobs.new(conn, video_id, request.get('pages_per_run', 20),source,commit=False)
                        if issue_id is not None: topic_radar.link_job(conn,issue_id,result['id'],commit=False)
                        conn.commit()
                        launch_collection(self.db_path, result['id'], key, source)
                    except Exception:
                        conn.rollback()
                        COLLECTION_LOCK.release()
                        raise
                elif self.path == '/api/collection/control':
                    action = request.get('action')
                    if action == 'resume':
                        job = collection_jobs.get(conn,request.get('id'))
                        source = job['source']
                        key = os.getenv('YOUTUBE_API_KEY', '').strip() if source=='youtube' else ''
                        if source=='youtube' and not key: raise ValueError('YOUTUBE_API_KEY belum disetel.')
                        if not COLLECTION_LOCK.acquire(blocking=False): raise ValueError('Pekerjaan lain masih berjalan atau sedang berhenti.')
                        try:
                            result = collection_jobs.control(conn,request.get('id'),action)
                            launch_collection(self.db_path,result['id'],key,source)
                        except Exception:
                            COLLECTION_LOCK.release()
                            raise
                    else:
                        result = collection_jobs.control(conn,request.get('id'),action)
                elif self.path == '/api/regional/link-screening':
                    result = regional.link_screening(conn,request.get('result_id'),request.get('province_code'),request.get('location_text'),request.get('confirmed',False))
                elif self.path == '/api/regional/investigate':
                    result = regional.open_case(conn,request.get('event_id'))
                elif self.path == '/api/research/analyze':
                    result = research_analysis.analyze(conn, request)
                elif self.path == '/api/release/demo':
                    result = release.prepare_demo(conn, seed_demo)
                elif self.path == '/api/evaluation/label':
                    result = evaluation.label_source(conn,request.get('kind'),request.get('source_ref'),
                        request.get('label'),request.get('rationale'),request.get('reviewer','Local analyst'),
                        request.get('expected_sha256'))
                elif self.path == '/api/discovery/scan':
                    result = discovery.scan(conn)
                elif self.path == '/api/discovery/status':
                    result = discovery.set_status(conn,request.get('id'),request.get('status'))
                elif self.path == '/api/discovery/investigate':
                    result = discovery.investigate(conn,request.get('id'))
                elif self.path == '/api/demo':
                    result = seed_demo(conn)
                    discovery.scan(conn)
                elif self.path == '/api/collect':
                    result = collect_youtube(conn, request.get('video'), request.get('limit', 100))
                    discovery.scan(conn)
                elif self.path == '/api/factcheck/extract':
                    source = str(request.get('text') or '').strip()
                    comment_id = str(request.get('comment_id') or '').strip()
                    if comment_id:
                        source_row = conn.execute('SELECT body FROM comments WHERE comment_id=?', (comment_id,)).fetchone()
                        if not source_row:
                            raise ValueError('Komentar tidak ditemukan.')
                        source = source_row['body']
                    if bool(request.get('use_ai')):
                        result = factcheck.muse_extract(source, conn)
                    else:
                        result = factcheck.candidate_claims(source)
                elif self.path == '/api/factcheck/search':
                    result = factcheck.search_factchecks(request.get('query'), language=str(request.get('language') or 'id'))
                elif self.path == '/api/factcheck/cases':
                    result = factcheck.create_case(conn, request.get('comment_id'), request.get('claim'))
                elif self.path == '/api/factcheck/evidence':
                    result = factcheck.add_evidence(conn, request)
                elif self.path == '/api/factcheck/review':
                    cid=investigation._id(request.get('case_id'))
                    oldrow=conn.execute('''SELECT fc.verdict,fc.reviewer_note,fc.reviewed_at,c.dataset
                        FROM fact_cases fc JOIN comments c ON c.comment_id=fc.comment_id WHERE fc.id=?''',(cid,)).fetchone()
                    result = factcheck.review_case(conn,cid,request.get('verdict'),request.get('note'))
                    if oldrow:
                        evaluation.audit_case_review(conn,oldrow['dataset'],'legacy_fact_review',cid,
                            dict(oldrow),{'verdict':request.get('verdict'),'note':request.get('note')},request.get('note'))
                elif self.path == '/api/factcheck/draft':
                    result = factcheck.generate_response(conn, request.get('case_id'))
                elif self.path == '/api/factcheck/approve':
                    result = factcheck.approve_response(conn, request.get('id'))
                elif self.path == '/api/unified/extract':
                    result = unified_fact.extract(conn,request.get('investigation_id'),
                                request.get('item_id'),request.get('use_ai',False))
                elif self.path == '/api/unified/create':
                    result = unified_fact.create(conn,request.get('investigation_id'),
                                request.get('item_id'),request.get('claim'))
                elif self.path == '/api/unified/evidence':
                    result = unified_fact.add_evidence(conn,request)
                elif self.path == '/api/unified/review':
                    cid=investigation._id(request.get('case_id'))
                    oldrow=conn.execute('SELECT verdict,review_note,reviewed_at,dataset FROM unified_fact_cases WHERE id=?',(cid,)).fetchone()
                    result = unified_fact.review(conn,cid,request.get('verdict'),
                                request.get('note'),request.get('sources_reviewed',False))
                    if oldrow:
                        evaluation.audit_case_review(conn,oldrow['dataset'],'unified_fact_review',cid,
                            dict(oldrow),{'verdict':request.get('verdict'),'note':request.get('note')},request.get('note'))
                elif self.path == '/api/unified/draft':
                    result = unified_fact.draft(conn,request.get('case_id'))
                elif self.path == '/api/unified/approve':
                    result = unified_fact.approve(conn,request.get('id'),request.get('confirmed',False))
                elif self.path == '/api/investigations/create':
                    result = investigation.create_case(conn, request.get('title'),request.get('summary',''),
                        source_kind=request.get('kind') or None,source_ref=request.get('ref'),
                        url=request.get('url',''),body=request.get('body',''),source_title=request.get('source_title',''),
                        dataset=request.get('dataset'))
                elif self.path == '/api/investigations/item':
                    result = investigation.add_item(conn,request.get('case_id'),kind=request.get('kind'),
                        ref=request.get('ref'),url=request.get('url',''),body=request.get('body',''),
                        title=request.get('title',''))
                elif self.path == '/api/investigations/note':
                    result = investigation.add_note(conn,request.get('case_id'),request.get('note'))
                elif self.path == '/api/investigations/review':
                    cid=investigation._id(request.get('case_id'))
                    oldrow=conn.execute('SELECT verdict,review_note,dataset FROM intel_investigation_cases WHERE id=?',(cid,)).fetchone()
                    result = investigation.review(conn,cid,request.get('verdict'),request.get('note'))
                    if oldrow:
                        evaluation.audit_case_review(conn,oldrow['dataset'],'investigation_review',cid,
                            dict(oldrow),{'verdict':request.get('verdict'),'note':request.get('note')},request.get('note'))
                elif self.path == '/api/investigations/status':
                    result = investigation.change_status(conn,request.get('case_id'),request.get('status'))
                elif self.path == '/api/investigations/link-fact':
                    result = investigation.link_fact_case(conn,request.get('case_id'),request.get('fact_case_id'))
                elif self.path == '/api/intel/topics/add':
                    result = intelligence.add_topic(conn, request.get('keyword'), request.get('platform'))
                elif self.path == '/api/intel/topics/delete':
                    result = intelligence.delete_topic(conn, int(request.get('id', -1)))
                elif self.path == '/api/intel/scan':
                    if not SCAN_LOCK.acquire(blocking=False):
                        raise RuntimeError('Pemindaian lain sedang berjalan. Coba lagi setelah selesai.')
                    try:
                        result = monitoring.scan_and_analyze(conn, int(request.get('topic_id', -1)),
                                                             max_items=int(request.get('limit',20)))
                    finally:
                        SCAN_LOCK.release()
                    discovery.scan(conn)
                elif self.path == '/api/monitor/config':
                    result = monitoring.update_config(conn, request.get('enabled'), request.get('interval_minutes'))
                elif self.path == '/api/monitor/scan':
                    if not SCAN_LOCK.acquire(blocking=False):
                        raise RuntimeError('Pemindaian lain sedang berjalan. Coba lagi setelah selesai.')
                    try:
                        result = monitoring.scan_and_analyze(conn,int(request.get('topic_id',-1)),max_items=20)
                    finally:
                        SCAN_LOCK.release()
                    discovery.scan(conn)
                elif self.path == '/api/monitor/alert/status':
                    result = monitoring.set_alert_status(conn,request.get('id'),request.get('status'))
                elif self.path == '/api/monitor/demo':
                    result = monitoring.demo_alert(conn)
                elif self.path == '/api/response/publication':
                    result = monitoring.log_publication(conn,request.get('response_id'),
                                                        request.get('platform'),request.get('url'),
                                                        request.get('note',''))
                elif self.path == '/api/response/withdraw':
                    result = monitoring.withdraw_publication(conn,request.get('id'))
                elif self.path == '/api/intel/observers/add':
                    result = intelligence.add_observer(conn, request.get('platform'), request.get('handle'),
                                                       request.get('profile_url'), request.get('purpose', ''))
                elif self.path == '/api/intel/observers/delete':
                    result = intelligence.delete_observer(conn, int(request.get('id', -1)))
                elif self.path == '/api/intel/lead':
                    result = intelligence.manual_lead(conn, request.get('platform'), request.get('url'),
                                                      request.get('body'), request.get('author_name', ''),
                                                      request.get('topic_id'))
                    discovery.scan(conn)
                elif self.path == '/api/intel/demo':
                    result = intelligence.seed_intel_demo(conn)
                    discovery.scan(conn)
                elif self.path == '/api/drafts':
                    result = create_investigation(conn, request)
                elif self.path == '/api/drafts/approve':
                    result = approve_investigation(conn, int(request.get('id', -1)))
                else:
                    return self.response(404, {'error':'Tidak ditemukan.'})
            self.response(200, result)
        except (ValueError, TypeError, sqlite3.Error, RuntimeError, ai_providers.ProviderError, json.JSONDecodeError) as exc:
            self.response(400, {'error':str(exc)})


def background_scans(db_path, stop_event):
    """Never posts content; only runs opt-in scheduled public API searches.

    Every cycle reopens DB and respects persisted enabled setting and daily cap.
    """
    while not stop_event.wait(25):
        if not SCAN_LOCK.acquire(blocking=False):
            continue
        try:
            with open_db(db_path) as conn:
                outcome = monitoring.run_due(conn)
                if outcome.get('scanned',0)>0:
                    discovery.scan(conn)
        except (ValueError, RuntimeError, sqlite3.Error) as exc:
            print('Background monitor: '+str(exc), file=sys.stderr)
        finally:
            SCAN_LOCK.release()


def radar_background_scans(db_path, stop_event):
    """Only operator-enabled feeds are scanned; no new source is enabled implicitly."""
    while not stop_event.wait(25):
        if not RADAR_LOCK.acquire(blocking=False):
            continue
        try:
            with open_db(db_path) as conn:
                for feed_id in topic_radar.due_feeds(conn):
                    enabled = conn.execute('SELECT auto_enabled FROM radar_settings WHERE id=1').fetchone()
                    if stop_event.is_set() or not enabled or not enabled[0]:
                        break
                    try:
                        topic_radar.scan_feed(conn,feed_id,fetcher=topic_radar.fetch_public)
                    except (ValueError, RuntimeError, sqlite3.Error):
                        continue
        except (ValueError, RuntimeError, sqlite3.Error):
            pass
        finally:
            RADAR_LOCK.release()

def youtube_background_scans(db_path,stop_event):
    while not stop_event.is_set():
        if YOUTUBE_RADAR_LOCK.acquire(blocking=False):
            try:
                with open_db(db_path) as conn:
                    youtube_radar.run_cycle(conn,os.getenv('YOUTUBE_API_KEY','').strip())
            except (ValueError,RuntimeError,sqlite3.Error) as exc:
                print('Radar YouTube: '+str(exc),file=sys.stderr)
            finally: YOUTUBE_RADAR_LOCK.release()
        if stop_event.wait(20): break

def youtube_background_analysis(db_path,stop_event):
    while not stop_event.is_set():
        if YOUTUBE_AI_LOCK.acquire(blocking=False):
            try:
                with open_db(db_path) as conn:
                    row=conn.execute("SELECT id FROM yt_radar_ai_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                    if row: youtube_radar.run_analysis(conn,row['id'])
                    else:
                        row=conn.execute("SELECT id FROM yt_query_plan_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                        if row: youtube_radar.run_query_plan(conn,row['id'])
            except (ValueError,RuntimeError,sqlite3.Error): pass
            finally: YOUTUBE_AI_LOCK.release()
        if stop_event.wait(10): break

def youtube_background_replies(db_path,stop_event):
    while not stop_event.is_set():
        if YOUTUBE_REPLY_LOCK.acquire(blocking=False):
            try:
                with open_db(db_path) as conn:
                    row=conn.execute("SELECT id FROM yt_radar_reply_jobs WHERE status='queued' ORDER BY updated_at,id LIMIT 1").fetchone()
                    if row: youtube_radar.run_reply_page(conn,row['id'],os.getenv('YOUTUBE_API_KEY','').strip(),store_youtube_thread)
            except (ValueError,RuntimeError,sqlite3.Error): pass
            finally: YOUTUBE_REPLY_LOCK.release()
        if stop_event.wait(10): break


def main(argv=None):
    load_env_file(BASE / '.env')
    parser = argparse.ArgumentParser(description='CAKRA MVP — local intelligence workbench')
    parser.add_argument('command', choices=['serve','collect','doctor'], nargs='?', default='serve')
    parser.add_argument('video', nargs='?', help='YouTube URL/ID (collect only)')
    parser.add_argument('--db', default=str(DEFAULT_DB), help='SQLite database path')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    with open_db(db_path) as conn:
        if args.command == 'serve':
            collection_jobs.recover(conn)
            comment_analysis.recover(conn)
        if args.command == 'doctor':
            print(json.dumps(release.readiness(conn, BASE), ensure_ascii=False, indent=2))
            return
        if args.command == 'collect':
            if not args.video:
                parser.error('Gunakan: python server.py collect URL_VIDEO')
            try:
                result = collect_youtube(conn, args.video, args.limit)
                result['discovery'] = discovery.scan(conn)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            except (ValueError, RuntimeError) as exc:
                parser.exit(1, 'ERROR: ' + str(exc) + '\n')
            return
    if not 1024 <= args.port <= 65535:
        parser.error('Port harus antara 1024 dan 65535.')
    Handler.db_path = db_path
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'CAKRA v{release.VERSION} berjalan: http://127.0.0.1:{args.port}')
    print('Lokal saja. CTRL+C untuk berhenti. Tidak memposting komentar otomatis.')
    stop_event=threading.Event()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nCAKRA dihentikan.')
    finally:
        stop_event.set()
        server.server_close()


if __name__ == '__main__':
    main()

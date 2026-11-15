"""Opt-in, bounded public RSS/Atom discovery with conservative issue grouping."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sqlite3
import unicodedata
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import factcheck
import investigation
import screening

MAX_FEED_BYTES = 2_000_000
MAX_FEED_ITEMS = 100
USER_AGENT = 'CAKRA-Radar/1.0'
GDELT_ENDPOINT = 'https://api.gdeltproject.org/api/v2/doc/doc'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS radar_feeds (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
      source_kind TEXT NOT NULL DEFAULT 'rss',
      enabled INTEGER NOT NULL DEFAULT 1, authorized INTEGER NOT NULL DEFAULT 0,
      interval_minutes INTEGER NOT NULL DEFAULT 120, etag TEXT NOT NULL DEFAULT '',
      last_modified TEXT NOT NULL DEFAULT '', last_checked TEXT NOT NULL DEFAULT '',
      last_success TEXT NOT NULL DEFAULT '', last_status TEXT NOT NULL DEFAULT 'not_checked',
      last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS radar_settings (
      id INTEGER PRIMARY KEY CHECK(id=1), auto_enabled INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL
    );
    INSERT OR IGNORE INTO radar_settings(id,auto_enabled,updated_at) VALUES(1,0,'');
    CREATE TABLE IF NOT EXISTS radar_scans (
      id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL REFERENCES radar_feeds(id),
      started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL,
      received INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
      changed INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS radar_articles (
      id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL REFERENCES radar_feeds(id),
      native_id TEXT NOT NULL, source_url TEXT NOT NULL, canonical_url TEXT NOT NULL,
      title TEXT NOT NULL, summary TEXT NOT NULL, raw_title TEXT NOT NULL DEFAULT '', raw_summary TEXT NOT NULL DEFAULT '',
      published_at TEXT NOT NULL DEFAULT '',
      source_observed_at TEXT NOT NULL DEFAULT '',
      observed_at TEXT NOT NULL, updated_at TEXT NOT NULL, content_sha256 TEXT NOT NULL,
      normalized_title TEXT NOT NULL, category TEXT NOT NULL, source_status TEXT NOT NULL,
      UNIQUE(feed_id,native_id)
    );
    CREATE INDEX IF NOT EXISTS idx_radar_articles_canonical ON radar_articles(canonical_url);
    CREATE INDEX IF NOT EXISTS idx_radar_articles_title ON radar_articles(normalized_title);
    CREATE INDEX IF NOT EXISTS idx_radar_articles_observed ON radar_articles(observed_at);
    CREATE TABLE IF NOT EXISTS radar_article_versions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER NOT NULL REFERENCES radar_articles(id),
      source_url TEXT NOT NULL, raw_title TEXT NOT NULL, raw_summary TEXT NOT NULL,
      content_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_radar_article_versions_article ON radar_article_versions(article_id,id);
    CREATE TABLE IF NOT EXISTS radar_issues (
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, category TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'needs_review', first_observed TEXT NOT NULL,
      last_observed TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS radar_issue_articles (
      issue_id INTEGER NOT NULL REFERENCES radar_issues(id),
      article_id INTEGER NOT NULL UNIQUE REFERENCES radar_articles(id),
      grouping_method TEXT NOT NULL, rationale TEXT NOT NULL,
      PRIMARY KEY(issue_id,article_id)
    );
    CREATE TABLE IF NOT EXISTS radar_issue_cases (
      issue_id INTEGER PRIMARY KEY REFERENCES radar_issues(id),
      case_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id)
    );
    CREATE TABLE IF NOT EXISTS radar_issue_jobs (
      issue_id INTEGER NOT NULL REFERENCES radar_issues(id),
      job_id INTEGER NOT NULL REFERENCES collection_jobs(id), created_at TEXT NOT NULL,
      PRIMARY KEY(issue_id,job_id)
    );
    CREATE INDEX IF NOT EXISTS idx_radar_issue_articles_issue ON radar_issue_articles(issue_id);
    ''')
    # Additive upgrades for databases that received an earlier radar schema.
    feed_columns = {row[1] for row in conn.execute('PRAGMA table_info(radar_feeds)')}
    article_columns = {row[1] for row in conn.execute('PRAGMA table_info(radar_articles)')}
    if 'source_kind' not in feed_columns:
        conn.execute("ALTER TABLE radar_feeds ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'rss'")
    if 'source_observed_at' not in article_columns:
        conn.execute("ALTER TABLE radar_articles ADD COLUMN source_observed_at TEXT NOT NULL DEFAULT ''")
    if 'raw_title' not in article_columns:
        conn.execute("ALTER TABLE radar_articles ADD COLUMN raw_title TEXT NOT NULL DEFAULT ''")
    if 'raw_summary' not in article_columns:
        conn.execute("ALTER TABLE radar_articles ADD COLUMN raw_summary TEXT NOT NULL DEFAULT ''")
    conn.commit()


def safe_url(value):
    value = str(value or '').strip()
    if not value or len(value) > 1500 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('URL harus berupa HTTPS publik yang valid.')
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.lower() != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if '.' not in host or host.endswith(('.local', '.localhost', '.internal', '.onion', '.invalid', '.test')):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if not re.fullmatch(r'[a-z0-9.-]+', host) or '..' in host:
            raise ValueError
        return urllib.parse.urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('URL harus berupa HTTPS publik tanpa kredensial dan port khusus.') from exc


def resolve_public(host):
    """Reject mixed/private DNS and pin the accepted address for the HTTPS socket."""
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError('DNS sumber tidak tersedia.') from exc
    addresses = []
    for item in records:
        try:
            address = ipaddress.ip_address(item[4][0].split('%')[0])
        except ValueError as exc:
            raise RuntimeError('Alamat DNS tidak valid.') from exc
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise RuntimeError('Alamat sumber bukan jaringan publik.')
        addresses.append(str(address))
    if not addresses:
        raise RuntimeError('Sumber tidak memiliki alamat publik.')
    return addresses[0]


def fetch_public(url, headers=None):
    """Fetch only HTTPS, pin validated DNS and refuse all redirects."""
    url = safe_url(url)
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    address = resolve_public(host)
    conn = http.client.HTTPSConnection(host, 443, timeout=10, context=ssl.create_default_context())
    try:
        raw_socket = socket.create_connection((address, 443), timeout=10)
        try:
            conn.sock = conn._context.wrap_socket(raw_socket, server_hostname=host)
        except Exception:
            raw_socket.close()
            raise
        path = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        request_headers = {'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml, text/plain;q=0.8'}
        request_headers.update(headers or {})
        conn.request('GET', path, headers=request_headers)
        response = conn.getresponse()
        data = response.read(MAX_FEED_BYTES + 1)
        if len(data) > MAX_FEED_BYTES:
            raise RuntimeError('Respons sumber melebihi batas 2 MB.')
        return response.status, {k.lower(): v for k, v in response.getheaders()}, data
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        raise RuntimeError('Koneksi sumber gagal atau melewati batas waktu.') from exc
    finally:
        conn.close()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value, limit):
    parser = _Text()
    parser.feed(str(value or '')[:12000])
    return ' '.join(' '.join(parser.parts).split())[:limit]


def normalized(value):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', value).casefold()))


def parse_date(value):
    value = str(value or '').strip()
    if not value:
        return ''
    try:
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            result = parsedate_to_datetime(value)
        if not result or result.tzinfo is None:
            return ''
        return result.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError):
        return ''


def _child(element, name):
    for child in element:
        if child.tag.split('}')[-1].lower() == name:
            return child
    return None


def _content(element, name):
    child = _child(element, name)
    return ''.join(child.itertext()) if child is not None else ''


def parse_feed(data):
    if not isinstance(data, bytes) or len(data) > MAX_FEED_BYTES or b'\x00' in data:
        raise ValueError('Ukuran feed tidak valid.')
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('Deklarasi XML tidak diizinkan.')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError('Format RSS/Atom tidak valid.') from exc
    root_name = root.tag.split('}')[-1].lower()
    if root_name not in ('rss', 'rdf', 'feed'):
        raise ValueError('Dokumen bukan RSS atau Atom.')
    candidates = [x for x in root.iter() if x.tag.split('}')[-1].lower() in ('item', 'entry')][:MAX_FEED_ITEMS]
    records = []
    for entry in candidates:
        raw_title = _content(entry, 'title')[:12000]
        raw_summary = (_content(entry, 'description') or _content(entry, 'summary'))[:12000]
        title = plain(raw_title, 300)
        summary = plain(raw_summary, 1500)
        link = _content(entry, 'link').strip()
        for child in entry:
            if child.tag.split('}')[-1].lower() == 'link' and child.attrib.get('href') and child.attrib.get('rel', 'alternate') == 'alternate':
                link = child.attrib['href']
                break
        try:
            url = safe_url(link)
        except ValueError:
            continue
        if not title:
            continue
        native = plain(_content(entry, 'guid') or _content(entry, 'id'), 1000) or url
        published = parse_date(_content(entry, 'pubdate') or _content(entry, 'published') or _content(entry, 'updated') or _content(entry, 'date'))
        records.append({'native_id': hashlib.sha256(native.encode()).hexdigest(), 'source_url': url,
                        'canonical_url': url, 'title': title, 'summary': summary, 'raw_title': raw_title,
                        'raw_summary': raw_summary, 'published_at': published,
                        'source_observed_at': ''})
    return records


def parse_gdelt(data):
    if not isinstance(data, bytes) or len(data) > MAX_FEED_BYTES:
        raise ValueError('Ukuran respons GDELT tidak valid.')
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Respons GDELT bukan JSON yang valid.') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('articles'), list) or len(payload['articles']) > MAX_FEED_ITEMS:
        raise ValueError('Format daftar artikel GDELT tidak valid.')
    records = []
    for item in payload['articles']:
        if not isinstance(item, dict):
            continue
        try:
            url = safe_url(item.get('url'))
        except ValueError:
            continue
        raw_title = str(item.get('title') or '')[:12000]
        title = plain(raw_title, 300)
        if not title:
            continue
        records.append({'native_id': hashlib.sha256(url.encode()).hexdigest(), 'source_url': url,
                        'canonical_url': url, 'title': title, 'summary': '', 'raw_title': raw_title,
                        'raw_summary': '', 'published_at': '',
                        # GDELT's seen date is not presented as the article's publication time.
                        'source_observed_at': parse_date(item.get('seendate'))})
    return records


def add_feed(conn, name, url, authorized, interval_minutes=120, source_kind='rss'):
    name = plain(name, 120)
    if len(name) < 3:
        raise ValueError('Nama sumber harus 3–120 karakter.')
    if authorized is not True:
        raise ValueError('Konfirmasi izin dan ketentuan akses sumber diperlukan.')
    try:
        if isinstance(interval_minutes, bool):
            raise ValueError
        interval = int(interval_minutes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Interval tidak valid.') from exc
    if not 60 <= interval <= 1440:
        raise ValueError('Interval harus 60–1440 menit.')
    if source_kind not in ('rss', 'gdelt'):
        raise ValueError('Jenis sumber tidak tersedia.')
    url = safe_url(url)
    if source_kind == 'gdelt' and not url.startswith(GDELT_ENDPOINT + '?'):
        raise ValueError('Endpoint GDELT tidak valid.')
    try:
        ident = conn.execute('INSERT INTO radar_feeds(name,url,authorized,interval_minutes,created_at,source_kind) VALUES(?,?,1,?,?,?)',
                             (name, url, interval, now(), source_kind)).lastrowid
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError('Sumber feed sudah terdaftar.') from exc
    return {'id': ident, 'name': name, 'url': url, 'status': 'not_checked'}


def add_gdelt(conn, query, authorized, interval_minutes=120):
    query = ' '.join(str(query or '').split())
    if not 3 <= len(query) <= 100 or not re.search(r'\w', query):
        raise ValueError('Topik GDELT harus 3–100 karakter.')
    params = urllib.parse.urlencode({'query': query, 'mode': 'artlist', 'format': 'json', 'maxrecords': 50, 'timespan': '7d'})
    return add_feed(conn, 'GDELT: ' + query[:100], GDELT_ENDPOINT + '?' + params, authorized, interval_minutes, 'gdelt')


def set_feed(conn, ident, enabled):
    if type(enabled) is not bool:
        raise ValueError('Status sumber harus boolean.')
    try:
        ident = investigation._id(ident)
    except ValueError:
        raise ValueError('ID sumber tidak valid.') from None
    if not conn.execute('UPDATE radar_feeds SET enabled=? WHERE id=?', (int(enabled), ident)).rowcount:
        raise ValueError('Sumber tidak ditemukan.')
    conn.commit()
    return {'id': ident, 'enabled': enabled}


def set_auto(conn, enabled):
    if type(enabled) is not bool:
        raise ValueError('Status jadwal harus boolean.')
    conn.execute('UPDATE radar_settings SET auto_enabled=?,updated_at=? WHERE id=1', (int(enabled), now()))
    conn.commit()
    return {'auto_enabled': enabled}


def can_scan(conn, ident):
    ident = investigation._id(ident)
    row = conn.execute('SELECT enabled,authorized,last_checked FROM radar_feeds WHERE id=?', (ident,)).fetchone()
    if not row or not row['enabled'] or not row['authorized']:
        raise ValueError('Sumber tidak tersedia atau belum diizinkan.')
    if row['last_checked']:
        try:
            last = datetime.fromisoformat(row['last_checked'])
            if last.tzinfo is None or datetime.now(timezone.utc) - last < timedelta(seconds=60):
                raise ValueError('Tunggu 60 detik sebelum memindai sumber ini kembali.')
        except TypeError:
            raise ValueError('Waktu scan sumber tidak valid.') from None
    return ident


def _header(value):
    value = str(value or '')
    return value if len(value) <= 500 and '\r' not in value and '\n' not in value else ''


def _same_event_candidate(conn, record):
    same_url = conn.execute('''SELECT l.issue_id FROM radar_articles a JOIN radar_issue_articles l ON l.article_id=a.id
        WHERE a.canonical_url=? LIMIT 1''', (record['canonical_url'],)).fetchone()
    if same_url:
        return same_url[0], 'same_url', 'URL kanonis sama; sumber feed tetap dicatat terpisah.'
    title_key = normalized(record['title'])
    if not record['published_at'] or len(title_key) < 25 or len(title_key.split()) < 4:
        return None
    try:
        published = datetime.fromisoformat(record['published_at'])
    except ValueError:
        return None
    rows = conn.execute('''SELECT a.published_at,l.issue_id FROM radar_articles a
        JOIN radar_issue_articles l ON l.article_id=a.id
        WHERE a.normalized_title=? AND a.published_at<>'' ORDER BY a.id DESC LIMIT 30''',
        (title_key,)).fetchall()
    for row in rows:
        try:
            other = datetime.fromisoformat(row[0])
            if abs((published-other).total_seconds()) <= 48 * 3600:
                return row[1], 'same_title_time_candidate', 'Judul identik dan waktu publikasi berdekatan; hubungan kejadian belum diverifikasi.'
        except (ValueError, TypeError):
            continue
    return None


def _store_article(conn, feed_id, record, stamp):
    raw_title = record.get('raw_title', record['title'])
    raw_summary = record.get('raw_summary', record['summary'])
    digest = hashlib.sha256((raw_title + '\n' + raw_summary + '\n' + record['source_url']).encode()).hexdigest()
    existing = conn.execute('SELECT id,content_sha256 FROM radar_articles WHERE feed_id=? AND native_id=?',
                            (feed_id, record['native_id'])).fetchone()
    category, _, _, _ = screening.classify(record['title'] + ' ' + record['summary'], '', record['source_url'])
    if existing:
        if existing['content_sha256'] != digest:
            conn.execute("UPDATE radar_articles SET title=?,summary=?,raw_title=?,raw_summary=?,source_url=?,canonical_url=?,published_at=?,source_observed_at=?,updated_at=?,content_sha256=?,normalized_title=?,category=?,source_status='changed' WHERE id=?",
                         (record['title'], record['summary'], raw_title, raw_summary, record['source_url'], record['canonical_url'], record['published_at'], record.get('source_observed_at', ''), stamp, digest, normalized(record['title']), category, existing['id']))
            conn.execute('INSERT INTO radar_article_versions(article_id,source_url,raw_title,raw_summary,content_sha256,observed_at) VALUES(?,?,?,?,?,?)',
                         (existing['id'], record['source_url'], raw_title, raw_summary, digest, stamp))
            return 'changed'
        return 'existing'
    candidate = _same_event_candidate(conn, record)
    article_id = conn.execute('''INSERT INTO radar_articles(feed_id,native_id,source_url,canonical_url,title,summary,raw_title,raw_summary,published_at,source_observed_at,
      observed_at,updated_at,content_sha256,normalized_title,category,source_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (feed_id, record['native_id'], record['source_url'], record['canonical_url'], record['title'], record['summary'], raw_title, raw_summary,
       record['published_at'], record.get('source_observed_at', ''), stamp, stamp, digest, normalized(record['title']), category, 'available')).lastrowid
    conn.execute('INSERT INTO radar_article_versions(article_id,source_url,raw_title,raw_summary,content_sha256,observed_at) VALUES(?,?,?,?,?,?)',
                 (article_id, record['source_url'], raw_title, raw_summary, digest, stamp))
    if candidate:
        issue_id, method, reason = candidate
        conn.execute('UPDATE radar_issues SET last_observed=? WHERE id=?', (stamp, issue_id))
    else:
        issue_id = conn.execute('INSERT INTO radar_issues(title,category,first_observed,last_observed) VALUES(?,?,?,?)',
                                (record['title'], category, stamp, stamp)).lastrowid
        method, reason = 'new_candidate', 'Satu publikasi; perlu pemeriksaan sumber dan konteks.'
    conn.execute('INSERT INTO radar_issue_articles(issue_id,article_id,grouping_method,rationale) VALUES(?,?,?,?)',
                 (issue_id, article_id, method, reason))
    return 'added'


def _robots_allowed(feed_url, fetcher):
    parsed = urllib.parse.urlsplit(feed_url)
    status, _, body = fetcher('https://' + parsed.netloc + '/robots.txt', {})
    if status in (404, 410):
        return True
    if status != 200:
        raise RuntimeError('Aturan robots.txt tidak dapat diperiksa; sumber tidak dipindai.')
    robot = urllib.robotparser.RobotFileParser()
    robot.parse(body[:256000].decode('utf-8', 'replace').splitlines())
    return robot.can_fetch(USER_AGENT, feed_url)


def scan_feed(conn, ident, fetcher=fetch_public, *, respect_cooldown=True):
    ident = investigation._id(ident)
    feed = conn.execute('SELECT * FROM radar_feeds WHERE id=?', (ident,)).fetchone()
    if not feed or not feed['enabled'] or not feed['authorized']:
        raise ValueError('Sumber tidak tersedia atau belum diizinkan.')
    if respect_cooldown and feed['last_checked']:
        try:
            last = datetime.fromisoformat(feed['last_checked'])
            if datetime.now(timezone.utc) - last < timedelta(seconds=60):
                raise ValueError('Tunggu 60 detik sebelum memindai sumber ini kembali.')
        except TypeError:
            raise ValueError('Waktu scan sumber tidak valid.') from None
    started = now()
    scan_id = conn.execute("INSERT INTO radar_scans(feed_id,started_at,finished_at,status,detail) VALUES(?,?,'','running','Pemindaian dimulai.')", (ident, started)).lastrowid
    conn.execute("UPDATE radar_feeds SET last_checked=?,last_status='running',last_error='' WHERE id=?", (started, ident))
    conn.commit()
    try:
        if not _robots_allowed(feed['url'], fetcher):
            raise RuntimeError('robots.txt tidak mengizinkan pengambilan feed.')
        headers = {}
        if feed['etag']:
            headers['If-None-Match'] = _header(feed['etag'])
        if feed['last_modified']:
            headers['If-Modified-Since'] = _header(feed['last_modified'])
        if feed['source_kind'] == 'gdelt':
            headers['Accept'] = 'application/json'
        status, response_headers, body = fetcher(feed['url'], headers)
        if status == 304:
            records, state = [], 'not_modified'
        elif status == 200:
            records, state = (parse_gdelt(body) if feed['source_kind'] == 'gdelt' else parse_feed(body)), 'ok'
        else:
            raise RuntimeError('Sumber merespons HTTP %d; redirect tidak diikuti.' % status)
        stamp = now()
        counts = {'added': 0, 'changed': 0}
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT enabled,authorized FROM radar_feeds WHERE id=?', (ident,)).fetchone()
        if not active or not active['enabled'] or not active['authorized']:
            raise ValueError('Sumber dinonaktifkan sebelum hasil disimpan.')
        for record in records:
            outcome = _store_article(conn, ident, record, stamp)
            if outcome in counts:
                counts[outcome] += 1
        conn.execute('''UPDATE radar_feeds SET last_checked=?,last_success=?,last_status=?,last_error='',
            etag=?,last_modified=? WHERE id=?''',
            (stamp, stamp, state, _header(response_headers.get('etag', feed['etag'])),
             _header(response_headers.get('last-modified', feed['last_modified'])), ident))
        conn.execute('''UPDATE radar_scans SET finished_at=?,status=?,received=?,added=?,changed=?,detail=? WHERE id=?''',
                     (stamp, state, len(records), counts['added'], counts['changed'],
                      'Feed diproses; relasi antarartikel memerlukan review.', scan_id))
        conn.commit()
        return {'feed_id': ident, 'status': state, 'received': len(records), **counts}
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        conn.rollback()
        stamp = now()
        # Do not persist untrusted exception text, credentials, or response bodies.
        message = str(exc)[:300] if isinstance(exc, (RuntimeError, ValueError)) else 'Pemindaian gagal.'
        conn.execute("UPDATE radar_feeds SET last_checked=?,last_status='error',last_error=? WHERE id=?", (stamp, message, ident))
        conn.execute("UPDATE radar_scans SET finished_at=?,status='error',detail=? WHERE id=?", (stamp, message, scan_id))
        conn.commit()
        return {'feed_id': ident, 'status': 'error', 'error': message}


def recover(conn):
    stamp = now()
    conn.execute("UPDATE radar_feeds SET last_status='error',last_error='Server berhenti saat scan; jalankan ulang secara manual.' WHERE id IN (SELECT feed_id FROM radar_scans WHERE status='running')")
    conn.execute("UPDATE radar_scans SET status='interrupted',finished_at=?,detail='Server berhenti sebelum hasil selesai disimpan.' WHERE status='running'", (stamp,))
    conn.commit()


def due_feeds(conn):
    setting = conn.execute('SELECT auto_enabled FROM radar_settings WHERE id=1').fetchone()
    if not setting or not setting[0]:
        return []
    current = datetime.now(timezone.utc)
    due = []
    for row in conn.execute('SELECT id,last_checked,interval_minutes FROM radar_feeds WHERE enabled=1 AND authorized=1 ORDER BY id LIMIT 100'):
        try:
            last = datetime.fromisoformat(row['last_checked']) if row['last_checked'] else None
        except ValueError:
            last = None
        if last is None or current-last >= timedelta(minutes=row['interval_minutes']):
            due.append(row['id'])
        if len(due) >= 3:
            break
    return due


def dashboard(conn):
    feeds = [dict(x) for x in conn.execute('''SELECT id,name,url,source_kind,enabled,authorized,interval_minutes,last_checked,last_success,last_status,last_error
        FROM radar_feeds ORDER BY id LIMIT 100''')]
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
    previous = (datetime.now(timezone.utc)-timedelta(hours=48)).isoformat()
    rows = conn.execute('''SELECT i.*,COUNT(DISTINCT a.canonical_url) AS article_count,
      COUNT(DISTINCT a.feed_id) AS source_count,
      SUM(CASE WHEN a.observed_at>=? THEN 1 ELSE 0 END) AS recent_observations,
      SUM(CASE WHEN a.observed_at>=? AND a.observed_at<? THEN 1 ELSE 0 END) AS previous_observations
      FROM radar_issues i JOIN radar_issue_articles l ON l.issue_id=i.id
      JOIN radar_articles a ON a.id=l.article_id
      GROUP BY i.id ORDER BY i.last_observed DESC,i.id DESC LIMIT 100''', (cutoff, previous, cutoff)).fetchall()
    issues = [dict(x) for x in rows]
    settings = conn.execute('SELECT auto_enabled,updated_at FROM radar_settings WHERE id=1').fetchone()
    return {'feeds': feeds, 'issues': issues, 'auto_enabled': bool(settings['auto_enabled']),
            'limits': 'Jumlah pengamatan dalam feed terdaftar, bukan ukuran opini publik atau kejadian yang terverifikasi.'}


def issue_detail(conn, ident):
    ident = investigation._id(ident)
    row = conn.execute('SELECT * FROM radar_issues WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('Kandidat isu tidak ditemukan.')
    articles = [dict(x) for x in conn.execute('''SELECT a.id,a.title,a.summary,a.source_url,a.published_at,a.source_observed_at,a.observed_at,
      a.source_status,f.name AS feed_name,l.grouping_method,l.rationale FROM radar_issue_articles l
      JOIN radar_articles a ON a.id=l.article_id JOIN radar_feeds f ON f.id=a.feed_id
      WHERE l.issue_id=? ORDER BY a.id LIMIT 100''', (ident,))]
    scans = [dict(x) for x in conn.execute('''SELECT s.feed_id,s.finished_at,s.status,s.received,s.added,s.changed
      FROM radar_scans s WHERE s.feed_id IN (SELECT a.feed_id FROM radar_articles a JOIN radar_issue_articles l
      ON l.article_id=a.id WHERE l.issue_id=?) ORDER BY s.id DESC LIMIT 20''', (ident,))]
    jobs = [dict(x) for x in conn.execute('''SELECT j.id,j.source,j.status,j.retrieved_count,j.unique_count,j.updated_at
      FROM radar_issue_jobs l JOIN collection_jobs j ON j.id=l.job_id WHERE l.issue_id=? ORDER BY j.id DESC LIMIT 20''', (ident,))]
    return {**dict(row), 'articles': articles, 'recent_scans': scans, 'jobs': jobs,
            'limitations': 'Relasi artikel adalah kandidat; jumlah liputan terbatas pada sumber yang dikonfigurasi.'}


def link_job(conn, issue_id, job_id, *, commit=True):
    issue_id, job_id = investigation._id(issue_id), investigation._id(job_id)
    if not conn.execute('SELECT 1 FROM radar_issues WHERE id=?', (issue_id,)).fetchone():
        raise ValueError('Kandidat isu tidak ditemukan.')
    if not conn.execute('SELECT 1 FROM collection_jobs WHERE id=?', (job_id,)).fetchone():
        raise ValueError('Pekerjaan pengumpulan tidak ditemukan.')
    conn.execute('INSERT OR IGNORE INTO radar_issue_jobs(issue_id,job_id,created_at) VALUES(?,?,?)',
                 (issue_id,job_id,now()))
    if commit: conn.commit()
    return {'issue_id': issue_id, 'job_id': job_id}


def investigate(conn, ident):
    ident = investigation._id(ident)
    conn.execute('BEGIN IMMEDIATE')
    try:
        existing = conn.execute('SELECT case_id FROM radar_issue_cases WHERE issue_id=?', (ident,)).fetchone()
        if existing:
            conn.commit()
            return {'id': existing[0], 'existing': True}
        issue = issue_detail(conn, ident)
        if not issue['articles']:
            raise ValueError('Isu belum memiliki sumber.')
        title = ('Radar: ' + issue['title'])[:160]
        stamp = now()
        cid = conn.execute("INSERT INTO intel_investigation_cases(title,summary,dataset,created_at,updated_at) VALUES(?,?,'real',?,?)",
                           (title, 'Kandidat isu dari feed publik; perlu pemeriksaan manusia.', stamp, stamp)).lastrowid
        investigation._event(conn, cid, 'case_created', 'Kasus radar dibuat; sumber dan hubungan perlu diperiksa.')
        attached = set()
        for article in issue['articles'][:20]:
            note = ('Judul sumber: ' + article['title'] + '\nCuplikan feed: ' + (article['summary'] or 'Tidak tersedia') +
                    '\nPenerbit feed: ' + article['feed_name'] + '\nWaktu publikasi: ' + (article['published_at'] or 'Tidak diketahui'))[:4000]
            source_title = article['feed_name'] if len(article['feed_name']) >= 4 else article['feed_name'] + ' / sumber'
            source = investigation._source(conn, 'reference', None, url=article['source_url'], body=note,
                                           title=source_title[:200])
            if source['source_ref'] not in attached:
                investigation._add_item(conn, cid, source)
                attached.add(source['source_ref'])
        conn.execute('INSERT INTO radar_issue_cases(issue_id,case_id) VALUES(?,?)', (ident, cid))
        conn.commit()
        return {'id': cid, 'existing': False}
    except Exception:
        conn.rollback()
        raise

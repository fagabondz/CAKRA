"""CAKRA v0.4 — authorized public-source topic research, no account automation.

Public Bluesky AppView (no auth) and YouTube Data API v3 (user API key).
Observer registry holds public profile identifiers, not credentials.
All manual submissions are explicitly marked as unverified leads.
"""
from __future__ import annotations

import difflib
import ipaddress
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from collections import defaultdict

BLUESKY_SEARCH = 'https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts'
YOUTUBE_SEARCH = 'https://www.googleapis.com/youtube/v3/search'
PLATFORMS = ('bluesky', 'youtube', 'instagram', 'tiktok', 'x', 'facebook', 'threads')
AUTO_PLATFORMS = ('bluesky', 'youtube')
PLATFORM_HOSTS = {
    'bluesky': {'bsky.app'},
    'youtube': {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'},
    'instagram': {'instagram.com', 'www.instagram.com'},
    'tiktok': {'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com'},
    'x': {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'},
    'facebook': {'facebook.com', 'www.facebook.com', 'm.facebook.com', 'fb.watch'},
    'threads': {'threads.net', 'www.threads.net', 'threads.com', 'www.threads.com'},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS intel_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL,
        platform TEXT NOT NULL CHECK(platform IN ('bluesky','youtube')),
        created_at TEXT NOT NULL,
        UNIQUE(keyword,platform)
    );
    CREATE TABLE IF NOT EXISTS intel_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        source_id TEXT NOT NULL,
        author_id TEXT NOT NULL DEFAULT '',
        author_name TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL,
        published_at TEXT NOT NULL,
        source_url TEXT NOT NULL,
        provenance TEXT NOT NULL CHECK(provenance IN ('api','manual','demo')),
        collected_at TEXT NOT NULL,
        UNIQUE(platform,source_id)
    );
    CREATE INDEX IF NOT EXISTS idx_intel_obs_platform ON intel_observations(platform);
    CREATE TABLE IF NOT EXISTS intel_topic_observations (
        topic_id INTEGER NOT NULL REFERENCES intel_topics(id) ON DELETE CASCADE,
        observation_id INTEGER NOT NULL REFERENCES intel_observations(id),
        PRIMARY KEY(topic_id,observation_id)
    );
    CREATE TABLE IF NOT EXISTS intel_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL REFERENCES intel_topics(id) ON DELETE CASCADE,
        checked_at TEXT NOT NULL,
        fetched_count INTEGER NOT NULL,
        new_count INTEGER NOT NULL,
        source TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_intel_runs_topic ON intel_runs(topic_id);
    CREATE TABLE IF NOT EXISTS intel_observers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        handle TEXT NOT NULL,
        profile_url TEXT NOT NULL,
        purpose TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(platform,profile_url)
    );
    ''')
    conn.execute('PRAGMA foreign_keys=ON')


def checked_keyword(value):
    word = str(value or '').strip()
    if not 3 <= len(word) <= 100 or not re.search(r'\w', word, re.UNICODE):
        raise ValueError('Topik harus 3–100 karakter dan mengandung huruf/angka.')
    return word


def add_topic(conn, keyword, platform):
    keyword = checked_keyword(keyword)
    if platform not in AUTO_PLATFORMS:
        raise ValueError('Pengamatan API saat ini hanya mendukung Bluesky dan YouTube.')
    try:
        cursor = conn.execute('INSERT INTO intel_topics(keyword,platform,created_at) VALUES(?,?,?)',
                              (keyword, platform, utc_now()))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Topik tersebut sudah terdaftar di platform yang sama.') from exc
    conn.commit()
    return {'id': cursor.lastrowid, 'keyword':keyword, 'platform':platform}


def delete_topic(conn, topic_id):
    row = conn.execute('SELECT id FROM intel_topics WHERE id=?', (topic_id,)).fetchone()
    if not row:
        raise ValueError('Topik tidak ditemukan.')
    conn.execute('DELETE FROM intel_topics WHERE id=?', (topic_id,))
    conn.commit()
    return {'removed': int(topic_id), 'note':'Catatan sumber tetap tersimpan; asosiasi topik dihapus.'}


def validated_public_profile(platform, url):
    if platform not in PLATFORMS:
        raise ValueError('Platform pengamat tidak dikenal.')
    url = str(url or '').strip()
    if not 15 <= len(url) <= 1000:
        raise ValueError('URL profil harus 15–1000 karakter.')
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443)
                or (parsed.hostname or '').lower() not in PLATFORM_HOSTS[platform]):
            raise ValueError('URL profil harus HTTPS dan memakai domain resmi platform yang dipilih.')
        if not parsed.path or parsed.path == '/':
            raise ValueError('URL profil harus merujuk ke akun tertentu.')
    except ValueError as exc:
        raise ValueError(str(exc) if 'URL profil' in str(exc) else 'URL profil tidak valid.') from exc
    return url


def add_observer(conn, platform, handle, profile_url, purpose=''):
    handle = str(handle or '').strip()
    purpose = str(purpose or '').strip()
    if not 2 <= len(handle) <= 100 or len(purpose) > 400:
        raise ValueError('Nama akun harus 2–100 karakter dan catatan maksimum 400 karakter.')
    profile_url = validated_public_profile(platform, profile_url)
    try:
        cursor = conn.execute('''INSERT INTO intel_observers(platform,handle,profile_url,purpose,created_at)
                                VALUES(?,?,?,?,?)''', (platform,handle,profile_url,purpose,utc_now()))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Akun pengamat tersebut sudah tercatat.') from exc
    conn.commit()
    return {'id':cursor.lastrowid, 'platform':platform,'handle':handle,
            'note':'Registrasi akun publik saja; tidak melakukan login, follow, scraping, atau posting.'}


def delete_observer(conn, observer_id):
    cursor = conn.execute('DELETE FROM intel_observers WHERE id=?',(observer_id,))
    if not cursor.rowcount:
        raise ValueError('Akun pengamat tidak ditemukan.')
    conn.commit()
    return {'removed':int(observer_id)}


def request_json(url):
    # All URLs passed to this method are built internally from fixed official hosts.
    req = urllib.request.Request(url, headers={'User-Agent':'CAKRA-Research/0.4 (public-read-only)',
                                                'Accept':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(2_000_001)
            if len(raw)>2_000_000:
                raise RuntimeError('Respons API melewati batas ukuran 2 MB.')
            value = json.loads(raw.decode('utf-8'))
            if not isinstance(value, dict):
                raise RuntimeError('Format respons API tidak valid.')
            return value
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Layanan API merespons HTTP {exc.code}; cek pembatasan dan kuota.') from None
    except (urllib.error.URLError,TimeoutError) as exc:
        raise RuntimeError('Koneksi ke layanan API gagal.') from None
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError('Format JSON dari API tidak valid.') from None


def parse_bluesky(raw):
    results=[]
    for post in (raw.get('posts') or [])[:30]:
        if not isinstance(post,dict):
            continue
        author=post.get('author') or {}
        record=post.get('record') or {}
        if not isinstance(author,dict) or not isinstance(record,dict):
            continue
        uri=str(post.get('uri') or '')
        m=re.fullmatch(r'at://(did:(?:plc|web):[A-Za-z0-9.:%_-]+)/app\.bsky\.feed\.post/([A-Za-z0-9_-]+)',uri)
        body=str(record.get('text') or '').strip()[:4000]
        if not m or not body:
            continue
        did,rkey=m.groups()
        if not str(author.get('did','')).startswith('did:'):
            continue
        results.append({
            'platform':'bluesky','source_id':uri,'author_id':str(author['did'])[:200],
            'author_name':str(author.get('handle') or author.get('displayName') or author['did'])[:160],
            'body':body, 'published_at':str(record.get('createdAt') or post.get('indexedAt') or utc_now())[:80],
            'source_url':'https://bsky.app/profile/'+urllib.parse.quote(did,safe=':')+'/post/'+rkey,
            'provenance':'api'
        })
    return results


def parse_youtube(raw):
    results=[]
    for item in (raw.get('items') or [])[:30]:
        if not isinstance(item,dict):
            continue
        ident=item.get('id') or {}
        snip=item.get('snippet') or {}
        if not isinstance(ident,dict) or not isinstance(snip,dict):
            continue
        video=str(ident.get('videoId') or '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video):
            continue
        title=str(snip.get('title') or '').strip()
        if not title:
            continue
        description=str(snip.get('description') or '').strip()[:2000]
        results.append({
            'platform':'youtube','source_id':video,
            'author_id':str(snip.get('channelId') or '')[:200],
            'author_name':str(snip.get('channelTitle') or 'Channel YouTube')[:160],
            'body':title[:400] + ('\n'+description if description else ''),
            'published_at':str(snip.get('publishedAt') or utc_now())[:80],
            'source_url':'https://www.youtube.com/watch?v='+video,
            'provenance':'api'
        })
    return results


def get_public_results(keyword, platform, limit=20, fetcher=request_json):
    keyword=checked_keyword(keyword)
    limit=int(limit)
    if not 1 <= limit <= 30:
        raise ValueError('Batas pencarian harus 1–30 hasil.')
    if platform=='bluesky':
        url=BLUESKY_SEARCH+'?'+urllib.parse.urlencode({'q':keyword,'limit':limit,'sort':'latest'})
        return parse_bluesky(fetcher(url))[:limit]
    if platform=='youtube':
        key=os.getenv('YOUTUBE_API_KEY','').strip()
        if not key:
            raise ValueError('YOUTUBE_API_KEY diperlukan untuk mencari video YouTube.')
        url=YOUTUBE_SEARCH+'?'+urllib.parse.urlencode({
            'key':key,'part':'snippet','type':'video','q':keyword,
            'maxResults':limit,'order':'date','safeSearch':'moderate'})
        return parse_youtube(fetcher(url))[:limit]
    raise ValueError('Konektor platform tersebut belum tersedia.')


def save_observation(conn, item):
    args=(item['platform'],item['source_id'],item['author_id'],item['author_name'],
          item['body'],item['published_at'],item['source_url'],item['provenance'],utc_now())
    conn.execute('''INSERT INTO intel_observations
        (platform,source_id,author_id,author_name,body,published_at,source_url,provenance,collected_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,source_id) DO UPDATE SET
        author_id=excluded.author_id,author_name=excluded.author_name,
        body=excluded.body,published_at=excluded.published_at,collected_at=excluded.collected_at''',args)
    row=conn.execute('SELECT id FROM intel_observations WHERE platform=? AND source_id=?',
                     (item['platform'],item['source_id'])).fetchone()
    return row['id']


def scan_topic(conn, topic_id, limit=20, fetcher=request_json):
    topic=conn.execute('SELECT * FROM intel_topics WHERE id=?',(topic_id,)).fetchone()
    if not topic:
        raise ValueError('Topik tidak ditemukan.')
    items=get_public_results(topic['keyword'],topic['platform'],limit=limit,fetcher=fetcher)
    new=0
    for item in items:
        obs_id=save_observation(conn,item)
        cursor=conn.execute('''INSERT OR IGNORE INTO intel_topic_observations(topic_id,observation_id)
                               VALUES(?,?)''',(topic_id,obs_id))
        new+=cursor.rowcount
    conn.execute('''INSERT INTO intel_runs(topic_id,checked_at,fetched_count,new_count,source)
                    VALUES(?,?,?,?,?)''',(topic_id,utc_now(),len(items),new,'official_public_api'))
    conn.commit()
    return {'topic_id':topic_id,'platform':topic['platform'], 'keyword':topic['keyword'],
            'fetched':len(items),'new_for_topic':new, 'source':'official_public_api',
            'notice':'Hasil pencarian tidak mengukur seluruh aktivitas platform; tidak ada label bot/hoaks.'}


def valid_observation_link(platform,url):
    if platform not in PLATFORMS:
        raise ValueError('Platform bukti tidak dikenal.')
    url=str(url or '').strip()
    if not 15 <= len(url) <= 1200:
        raise ValueError('URL observasi tidak valid.')
    try:
        p=urllib.parse.urlsplit(url)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443):
            raise ValueError('Hanya tautan HTTPS publik yang diizinkan.')
        if (p.hostname or '').lower() not in PLATFORM_HOSTS[platform]:
            raise ValueError('URL harus berasal dari domain platform yang dipilih.')
        if not p.path or p.path=='/':
            raise ValueError('URL harus merujuk pada konten tertentu.')
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return url


def manual_lead(conn,platform,url,body,author_name='',topic_id=None):
    url=valid_observation_link(platform,url)
    body=str(body or '').strip()
    if not 8 <= len(body) <= 4000:
        raise ValueError('Cuplikan observasi manual harus 8–4000 karakter.')
    author_name=str(author_name or '').strip()[:160]
    # distinguish manual leads from API observations even if same canonical URL
    # because manual text may not be a faithful copy of the post.
    item={'platform':platform,'source_id':'manual:'+url, 'author_id':'',
          'author_name':author_name or 'Belum diverifikasi','body':body,
          'published_at':utc_now(), 'source_url':url,'provenance':'manual'}
    obs_id=save_observation(conn,item)
    if topic_id not in (None,''):
        row=conn.execute('SELECT id FROM intel_topics WHERE id=?',(topic_id,)).fetchone()
        if not row:
            raise ValueError('Topik tidak ditemukan.')
        conn.execute('INSERT OR IGNORE INTO intel_topic_observations(topic_id,observation_id) VALUES(?,?)',
                     (topic_id,obs_id))
    conn.commit()
    return {'id':obs_id,'provenance':'manual', 'notice':'Lead manual belum diverifikasi; bukan data yang diambil API.'}


def narrative_groups(items, min_similarity=.86):
    """Simple similarity over observations; no inference of same operators or truth."""
    subset=[dict(x) for x in items[:180]]
    def normalized(text):
        s=re.sub(r'https?://\S+','',str(text or '').lower())
        return ' '.join(re.sub(r'[^\w\s]',' ',s).split())[:500]
    texts=[normalized(x['body']) for x in subset]
    groups=[]
    parent=list(range(len(subset)))
    def find(x):
        while parent[x]!=x:
            parent[x]=parent[parent[x]]
            x=parent[x]
        return x
    for i in range(len(subset)):
        if len(texts[i])<35:continue
        for j in range(i+1,len(subset)):
            if len(texts[j])<35:continue
            if subset[i]['provenance']!=subset[j]['provenance']:continue
            if abs(len(texts[i])-len(texts[j]))>max(len(texts[i]),len(texts[j]))*.20:continue
            if difflib.SequenceMatcher(None,texts[i],texts[j],autojunk=False).ratio() >= min_similarity:
                parent[find(j)]=find(i)
    grouped=defaultdict(list)
    for i,row in enumerate(subset):grouped[find(i)].append(row)
    for members in grouped.values():
        if len(members)<2:continue
        platforms=sorted({m['platform'] for m in members})
        groups.append({'representative':members[0]['body'][:250], 'count':len(members),
                       'platforms':platforms,'source_urls':[m['source_url'] for m in members[:8]],
                       'provenance':members[0]['provenance'],
                       'warning':'Kesamaan teks saja, bukan bukti koordinasi, bot, atau kebenaran konten.'})
    return sorted(groups,key=lambda g:-g['count'])[:15]


def dashboard_data(conn):
    topics=[dict(r) for r in conn.execute('''SELECT t.*,
          (SELECT COUNT(*) FROM intel_topic_observations x WHERE x.topic_id=t.id) AS observations,
          (SELECT MAX(checked_at) FROM intel_runs r WHERE r.topic_id=t.id) AS last_scan,
          (SELECT new_count FROM intel_runs r WHERE r.topic_id=t.id ORDER BY id DESC LIMIT 1) AS last_new
          FROM intel_topics t ORDER BY t.id DESC LIMIT 100''')]
    observers=[dict(r) for r in conn.execute('SELECT * FROM intel_observers ORDER BY id DESC LIMIT 100')]
    rows=[dict(r) for r in conn.execute('''SELECT * FROM intel_observations
           ORDER BY collected_at DESC,id DESC LIMIT 180''')]
    runs=[dict(r) for r in conn.execute('''SELECT r.id, r.topic_id,t.keyword,t.platform,r.checked_at,r.fetched_count,r.new_count
           FROM intel_runs r JOIN intel_topics t ON t.id=r.topic_id ORDER BY r.id DESC LIMIT 30''')]
    total=conn.execute('SELECT COUNT(*) AS c FROM intel_observations').fetchone()['c']
    return {'topics':topics,'observers':observers,'observations':rows,'runs':runs,
            'narratives':narrative_groups(rows),
            'stats':{'observations_total':total,'topics':len(topics),'observers':len(observers),
                     'api':sum(x['provenance']=='api' for x in rows),
                     'manual':sum(x['provenance']=='manual' for x in rows),
                     'demo':sum(x['provenance']=='demo' for x in rows)},
            'limits':'Menampilkan 180 observasi terbaru; asosiasi topik tidak berarti seluruh platform dipantau.',
            'security':'Tidak ada login akun, pengiriman komentar, atau scraping browser.'}


def seed_intel_demo(conn):
    topic = conn.execute("SELECT id FROM intel_topics WHERE platform='bluesky' AND keyword='CAKRA DEMO'").fetchone()
    if topic:
        topic_id=topic['id']
    else:
        topic_id=add_topic(conn,'CAKRA DEMO','bluesky')['id']
    examples=[
        ('demo:bsky1','CAKRA Demo 1','Informasi jembatan contoh ditutup permanen menurut kabar ini, segera bagikan.', 'https://example.invalid/cakra/intel/1'),
        ('demo:bsky2','CAKRA Demo 2','Informasi jembatan contoh ditutup permanen menurut kabar ini segera bagikan!', 'https://example.invalid/cakra/intel/2'),
        ('demo:bsky3','CAKRA Demo 3','Kapan pengumuman resmi tentang kondisi jembatan contoh dikeluarkan?', 'https://example.invalid/cakra/intel/3'),
    ]
    count=0
    for source_id,author,body,url in examples:
        item={'platform':'bluesky','source_id':source_id,'author_id':source_id,
              'author_name':author,'body':body, 'published_at':'2026-11-11T08:00:00+00:00',
              'source_url':url,'provenance':'demo'}
        obs_id=save_observation(conn,item)
        count+=conn.execute('INSERT OR IGNORE INTO intel_topic_observations(topic_id,observation_id) VALUES(?,?)',
                            (topic_id,obs_id)).rowcount
    conn.commit()
    return {'topic_id':topic_id,'inserted_links':count,'provenance':'demo',
            'notice':'SIMULASI; link example.invalid bukan sumber nyata.'}

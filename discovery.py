"""CAKRA v0.8: evidence-first, deterministic claim triage.

Ranks review urgency only; never measures truth, credibility or an individual's identity.
No network calls, no unattended publishing, no automatic fact-check verdict.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict

import investigation
from intelligence import utc_now

MAX_PER_SOURCE = 750
MAX_SOURCES = 800  # each table; bounded to keep scans responsive on modest PCs


def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS discovery_candidates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL CHECK(kind IN ('comment','observation')),
      source_ref TEXT NOT NULL,
      platform TEXT NOT NULL,
      dataset TEXT NOT NULL CHECK(dataset IN ('real','demo')),
      provenance TEXT NOT NULL CHECK(provenance IN ('api','manual','demo')),
      claim_text TEXT NOT NULL,
      claim_hash TEXT NOT NULL,
      author_id TEXT NOT NULL DEFAULT '',
      author_name TEXT NOT NULL DEFAULT '',
      source_url TEXT NOT NULL,
      published_at TEXT NOT NULL,
      score INTEGER NOT NULL DEFAULT 0,
      reasons_json TEXT NOT NULL DEFAULT '[]',
      status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','investigating','dismissed')),
      investigation_id INTEGER,
      fact_case_id INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(kind,source_ref,claim_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_discovery_sort ON discovery_candidates(dataset,status,score);
    ''')


def extract_candidate(text):
    """Conservative sentence selection. An analyst MUST check whether it is a factual claim."""
    if not isinstance(text, str):
        return None
    text = re.sub(r'\s+', ' ', text).strip()
    if not 30 <= len(text) <= 10000:
        return None
    for chunk in re.split(r'(?<=[.!?])\s+', text):
        s = chunk.strip(' \"\u201c\u201d')[:MAX_PER_SOURCE]
        if len(s) < 30 or len(s.split()) < 6:
            continue
        low = s.lower()
        if s.endswith('?') or s.startswith(('http://','https://')):
            continue
        # Do not queue obvious questions, greetings and fragments as assertions.
        if re.match(r'^(apa(kah)?|kapan|bagaimana|mengapa|kenapa|siapa|dimana|di mana|tolong|please|hi|halo|hai)\b',low):
            continue
        # Should contain some characters other than URLs or handles.
        if len(re.sub(r'https?://\S+|@\w+', '', s).strip()) < 25:
            continue
        return s
    return None


def claim_key(text):
    norm = re.sub(r'[^\w\s]', ' ', text.lower())
    norm = ' '.join(norm.split())[:500]
    return hashlib.sha256(norm.encode('utf-8')).hexdigest()


def _sources(conn):
    for r in conn.execute('''SELECT comment_id AS ref, 'comment' AS kind,
        'youtube' AS platform, dataset, CASE WHEN dataset='demo' THEN 'demo' ELSE 'api' END AS provenance,
        author_id, author_name, body, source_url, published_at
        FROM comments ORDER BY collected_at DESC LIMIT ?''',(MAX_SOURCES,)):
        yield dict(r)
    for r in conn.execute('''SELECT CAST(id AS TEXT) AS ref,'observation' AS kind,
        platform, CASE WHEN provenance='demo' THEN 'demo' ELSE 'real' END AS dataset,
        provenance,author_id,author_name,body,source_url,published_at
        FROM intel_observations ORDER BY collected_at DESC,id DESC LIMIT ?''',(MAX_SOURCES,)):
        yield dict(r)


def scan(conn):
    """Queue candidate assertions from already-acquired sources; never fetch from platforms.

    Maintains analyst status and investigation IDs across rescans.
    Scores use observed duplication, distinct public IDs and platform presence only.
    """
    init_tables(conn)
    candidates = []
    for r in _sources(conn):
        claim = extract_candidate(r['body'])
        if not claim:
            continue
        r['claim'] = claim
        r['hash'] = claim_key(claim)
        candidates.append(r)
    groups = defaultdict(list)
    for r in candidates:
        groups[(r['dataset'], r['hash'])].append(r)
    stamp = utc_now()
    inserted = 0
    for r in candidates:
        peers = groups[(r['dataset'],r['hash'])]
        # Public IDs only; don't infer unique humans from names or missing IDs.
        authors = {(p['platform'],p['author_id']) for p in peers if p['author_id']}
        platforms = {p['platform'] for p in peers}
        count = len(peers)
        repeat_points = min(30, max(0,count-1)*10)
        author_points = min(20, max(0,len(authors)-1)*10)
        platform_points = min(15, max(0,len(platforms)-1)*15)
        factual_marker = bool(re.search(r'\b\d{2,4}\b|\b(tanggal|tahun|persen|jumlah|resmi|menurut|terjadi|ditutup|dibuka|meningkat|menurun|diumumkan|diputuskan|confirmed|reported|announced)\b',r['claim'], re.I))
        checkable_points = 15 if factual_marker else 5
        direct_points = 10 if r['provenance']=='api' else 0
        # Convert the 0–90 raw review-urgency heuristic to a 0–100 display scale.
        # This normalization does NOT make the score a probability or accuracy estimate.
        raw_points = repeat_points+author_points+platform_points+checkable_points+direct_points
        score = round(raw_points / 90 * 100)
        reasons = [f'Kemunculan teks setara dalam sampel: {count} (bukan ukuran popularitas global)',
                   f'ID penulis publik berbeda yang teramati: {len(authors)}',
                   f'Platform di sampel: {len(platforms)}',
                   'Penanda faktual terdeteksi' if factual_marker else 'Klaim perlu dipastikan secara manual',
                   'Sumber API publik' if r['provenance']=='api' else 'Lead manual/simulasi: belum diverifikasi']
        import json
        cur = conn.execute('''INSERT OR IGNORE INTO discovery_candidates
          (kind,source_ref,platform,dataset,provenance,claim_text,claim_hash,author_id,author_name,
           source_url,published_at,score,reasons_json,status,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)''',
          (r['kind'],str(r['ref']),r['platform'],r['dataset'],r['provenance'],r['claim'],r['hash'],
           r['author_id'] or '',r['author_name'] or '',r['source_url'],r['published_at'],
           score,json.dumps(reasons,ensure_ascii=False),stamp,stamp))
        inserted += max(0,cur.rowcount)
        conn.execute('''UPDATE discovery_candidates SET score=?,reasons_json=?,updated_at=?
          WHERE kind=? AND source_ref=? AND claim_hash=?''',
          (score,json.dumps(reasons,ensure_ascii=False),stamp,r['kind'],str(r['ref']),r['hash']))
    conn.commit()
    return {'processed':len(candidates),'new_candidates':inserted,'limited_to_latest_per_table':MAX_SOURCES,
            'note':'Triage awal berdasarkan teks teramati. Skor BUKAN probabilitas hoaks, bot, atau kebenaran.'}


def dashboard(conn, dataset='all', status='all', limit=120):
    if dataset not in ('all','real','demo'):
        raise ValueError('Dataset tidak dikenal.')
    if status not in ('all','queued','investigating','dismissed'):
        raise ValueError('Status antrean tidak dikenal.')
    try:
        limit = int(limit)
    except (TypeError,ValueError):
        raise ValueError('Limit tidak valid.')
    if not 1 <= limit <= 200:
        raise ValueError('Limit harus 1–200.')
    filters, params = [], []
    if dataset != 'all':
        filters.append('dataset=?'); params.append(dataset)
    if status != 'all':
        filters.append('status=?'); params.append(status)
    where = (' WHERE ' + ' AND '.join(filters)) if filters else ''
    import json
    rows = [dict(r) for r in conn.execute('SELECT * FROM discovery_candidates'+where+
                  ' ORDER BY score DESC,id DESC LIMIT ?',(*params,limit))]
    for r in rows:
        r['reasons']=json.loads(r.pop('reasons_json'))
    total_where = ' WHERE dataset=?' if dataset != 'all' else ''
    total_params = [dataset] if dataset != 'all' else []
    totals = {r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM discovery_candidates'+total_where+' GROUP BY status',total_params)}
    return {'items':rows,'totals':totals,'warning':'Prioritas review dari sampel; bukan hasil fact-check atau tuduhan terhadap pemilik akun.',
            'limits':f'Maksimum {limit} kandidat ditampilkan; scan terbaru membaca hingga {MAX_SOURCES} sumber per tabel.'}


def set_status(conn, identifier, status):
    cid = investigation._id(identifier)
    if status not in ('queued','dismissed'):
        raise ValueError('Status hanya boleh queued atau dismissed; investigasi dibuat dengan tombol Investigasi.')
    row = conn.execute('SELECT * FROM discovery_candidates WHERE id=?',(cid,)).fetchone()
    if not row:
        raise ValueError('Kandidat tidak ditemukan.')
    if row['status']=='investigating':
        raise ValueError('Kandidat sedang ditinjau; kelola melalui Investigations.')
    conn.execute('UPDATE discovery_candidates SET status=?,updated_at=? WHERE id=?',(status,utc_now(),cid))
    conn.commit()
    return {'id':cid,'status':status}


def investigate(conn, identifier):
    """Start a human-reviewed case linked to the original source; no verdict is implied."""
    from unified_fact import create as create_fact
    cid=investigation._id(identifier)
    row=conn.execute('SELECT * FROM discovery_candidates WHERE id=?',(cid,)).fetchone()
    if not row:
        raise ValueError('Kandidat tidak ditemukan.')
    if row['status']=='dismissed':
        raise ValueError('Kandidat diabaikan. Kembalikan ke antrean terlebih dulu.')
    if row['investigation_id']:
        return {'candidate_id':cid,'investigation_id':row['investigation_id'],
                'fact_case_id':row['fact_case_id'],'existing':True}
    # Source may be removed since discovery; _source will validate its presence.
    title='Periksa klaim: '+row['claim_text'][:135]
    inv=investigation.create_case(conn,title,summary='Kandidat otomatis. Belum ada keputusan kebenaran.',
        source_kind=row['kind'],source_ref=row['source_ref'])
    item=conn.execute('''SELECT id FROM intel_investigation_items
            WHERE case_id=? AND kind=? AND source_ref=?''',
            (inv['id'],row['kind'],row['source_ref'])).fetchone()
    if item is None:
        raise ValueError('Gagal menghubungkan bukti asal.')
    fact=create_fact(conn,inv['id'],item['id'],row['claim_text'])
    conn.execute('''UPDATE discovery_candidates SET investigation_id=?,fact_case_id=?,status='investigating',updated_at=? WHERE id=?''',
                 (inv['id'],fact['id'],utc_now(),cid))
    conn.commit()
    return {'candidate_id':cid,'investigation_id':inv['id'],'fact_case_id':fact['id'],'existing':False,
            'notice':'Kasus dibuat, klaim belum diverifikasi. Periksa sumber sebelum menilai.'}

"""CAKRA v0.6: source-aware cross-platform case workbench.

No scraping, identity attribution, autonomous truth verdicts, or platform posting.
Evidence is a snapshot of a previously stored observation or analyst-entered URL.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

import factcheck

KINDS = {'comment', 'observation', 'alert', 'reference'}
STATUSES = {'open', 'in_review', 'closed'}
VERDICTS = {'unreviewed', 'supported', 'contradicted', 'mixed', 'insufficient'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS intel_investigation_cases (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_review','closed')),
      verdict TEXT NOT NULL DEFAULT 'unreviewed' CHECK(verdict IN ('unreviewed','supported','contradicted','mixed','insufficient')),
      review_note TEXT NOT NULL DEFAULT '',
      dataset TEXT NOT NULL CHECK(dataset IN ('real','demo')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS intel_investigation_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      case_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK(kind IN ('comment','observation','alert','reference')),
      source_ref TEXT NOT NULL,
      platform TEXT NOT NULL,
      provenance TEXT NOT NULL CHECK(provenance IN ('api','manual','demo')),
      author_name TEXT NOT NULL DEFAULT '',
      body TEXT NOT NULL,
      source_url TEXT NOT NULL DEFAULT '',
      published_at TEXT NOT NULL DEFAULT '',
      snapshot_sha256 TEXT NOT NULL,
      evidence_urls_json TEXT NOT NULL DEFAULT '[]',
      added_at TEXT NOT NULL,
      UNIQUE(case_id,kind,source_ref)
    );
    CREATE INDEX IF NOT EXISTS idx_investigation_items_case ON intel_investigation_items(case_id);
    CREATE TABLE IF NOT EXISTS intel_investigation_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      case_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      note TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_investigation_events_case ON intel_investigation_events(case_id);
    CREATE TABLE IF NOT EXISTS intel_investigation_fact_links (
      case_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id) ON DELETE CASCADE,
      fact_case_id INTEGER NOT NULL REFERENCES fact_cases(id),
      created_at TEXT NOT NULL,
      PRIMARY KEY(case_id,fact_case_id)
    );
    ''')


def _case(conn, case_id):
    row = conn.execute('SELECT * FROM intel_investigation_cases WHERE id=?', (_id(case_id),)).fetchone()
    if row is None:
        raise ValueError('Kasus investigasi tidak ditemukan.')
    return dict(row)


def _id(value):
    if isinstance(value, bool):
        raise ValueError('ID tidak valid.')
    try:
        num = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('ID tidak valid.') from exc
    if num < 1:
        raise ValueError('ID tidak valid.')
    return num


def _bounded(value, field, minimum, maximum):
    text = str(value or '').strip()
    if not minimum <= len(text) <= maximum:
        raise ValueError(f'{field} harus {minimum}–{maximum} karakter.')
    return text


def _source(conn, kind, ref, *, url='', body='', title=''):
    kind = str(kind or '')
    if kind not in KINDS:
        raise ValueError('Tipe sumber tidak dikenal.')
    if kind == 'comment':
        row = conn.execute('SELECT * FROM comments WHERE comment_id=?', (str(ref),)).fetchone()
        if not row:
            raise ValueError('Komentar sumber tidak tersedia.')
        return dict(kind=kind, source_ref=row['comment_id'], platform='manual' if row['dataset']=='demo' and row['video_id'].startswith('sim-') else 'youtube',
                    provenance='demo' if row['dataset'] == 'demo' else 'api',
                    author_name=row['author_name'], body=row['body'], source_url=row['source_url'],
                    published_at=row['published_at'], evidence_urls=[])
    if kind == 'observation':
        row = conn.execute('SELECT * FROM intel_observations WHERE id=?', (_id(ref),)).fetchone()
        if not row:
            raise ValueError('Observasi sumber tidak tersedia.')
        return dict(kind=kind, source_ref=str(row['id']), platform=row['platform'],
                    provenance=row['provenance'], author_name=row['author_name'],
                    body=row['body'], source_url=row['source_url'],
                    published_at=row['published_at'], evidence_urls=[])
    if kind == 'alert':
        row = conn.execute('SELECT * FROM monitor_alerts WHERE id=?', (_id(ref),)).fetchone()
        if not row:
            raise ValueError('Peringatan sumber tidak tersedia.')
        links = json.loads(row['evidence_json'])
        return dict(kind=kind, source_ref=str(row['id']), platform='monitor',
                    provenance=row['provenance'], author_name='CAKRA Early Warning',
                    body=row['title'] + '\n' + row['explanation'], source_url='',
                    published_at=row['created_at'], evidence_urls=links[:20])
    if not factcheck.public_evidence_url(str(url or '')):
        raise ValueError('Tautan rujukan harus berupa URL HTTPS publik yang valid.')
    body = _bounded(body, 'Catatan rujukan', 15, 4000)
    title = _bounded(title, 'Judul rujukan', 4, 200)
    # Unique reference ID is a digest of normalized URL + content; no external GET.
    digest = hashlib.sha256((url + '\n' + body).encode('utf-8')).hexdigest()
    return dict(kind='reference', source_ref=digest, platform='reference', provenance='manual',
                author_name=title, body=body, source_url=str(url).strip(),
                published_at='', evidence_urls=[])


def _hash(source):
    relevant = {k: source[k] for k in ('kind','source_ref','platform','provenance','author_name','body','source_url','published_at','evidence_urls')}
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _event(conn, case_id, event_type, note):
    conn.execute('INSERT INTO intel_investigation_events(case_id,event_type,note,created_at) VALUES(?,?,?,?)',
                 (case_id,event_type,note,now()))
    conn.execute('UPDATE intel_investigation_cases SET updated_at=? WHERE id=?', (now(),case_id))


def _add_item(conn, case_id, source):
    case = _case(conn, case_id)
    source_dataset = 'demo' if source['provenance'] == 'demo' else 'real'
    if source_dataset != case['dataset']:
        raise ValueError('Data SIMULASI tidak boleh dicampur dengan data nyata/manual dalam satu kasus.')
    try:
        cur = conn.execute('''INSERT INTO intel_investigation_items
            (case_id,kind,source_ref,platform,provenance,author_name,body,source_url,published_at,
             snapshot_sha256,evidence_urls_json,added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (case_id,source['kind'],source['source_ref'],source['platform'],source['provenance'],
             source['author_name'],source['body'],source['source_url'],source['published_at'],
             _hash(source),json.dumps(source['evidence_urls'],ensure_ascii=False),now()))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Bukti ini sudah dimasukkan dalam kasus.') from exc
    if case['verdict'] != 'unreviewed':
        conn.execute('UPDATE intel_investigation_cases SET verdict=?,review_note=? WHERE id=?',
                     ('unreviewed','',case_id))
        _event(conn,case_id,'review_invalidated','Ada bukti baru; review sebelumnya perlu diulang.')
    _event(conn,case_id,'source_attached',f"{source['kind']} / {source['platform']} / {source['source_ref'][:100]}")
    return cur.lastrowid


def create_case(conn, title, summary='', *, source_kind=None, source_ref=None, url='', body='', source_title='', dataset=None):
    title = _bounded(title,'Judul kasus',8,160)
    summary = _bounded(summary,'Ringkasan kasus',0,1500)
    source = _source(conn,source_kind,source_ref,url=url,body=body,title=source_title) if source_kind else None
    if source:
        group = 'demo' if source['provenance']=='demo' else 'real'
    elif dataset in ('real','demo'):
        group = dataset
    else:
        group = 'real'
    stamp = now()
    cur = conn.execute('INSERT INTO intel_investigation_cases(title,summary,dataset,created_at,updated_at) VALUES(?,?,?,?,?)',
                       (title,summary,group,stamp,stamp))
    cid = cur.lastrowid
    _event(conn,cid,'case_created','Kasus investigasi dibuat. Belum ada temuan yang terkonfirmasi.')
    if source:
        _add_item(conn,cid,source)
    conn.commit()
    return {'id':cid,'dataset':group,'status':'open'}


def add_item(conn, case_id, *, kind, ref=None, url='', body='', title=''):
    case = _case(conn,case_id)
    if case['status']=='closed':
        raise ValueError('Kasus ditutup. Buka kembali sebelum menambah bukti.')
    source = _source(conn,kind,ref,url=url,body=body,title=title)
    item_id = _add_item(conn,case['id'],source)
    conn.commit()
    return {'id':item_id,'case_id':case['id'], 'kind':source['kind'], 'provenance':source['provenance']}


def add_note(conn, case_id, note):
    case = _case(conn,case_id)
    note = _bounded(note,'Catatan analis',10,1600)
    if case['status']=='closed':
        raise ValueError('Kasus ditutup. Buka kembali sebelum menambah catatan.')
    _event(conn,case['id'],'analyst_note',note)
    conn.commit()
    return {'case_id':case['id'],'saved':True}


def review(conn, case_id, verdict, note):
    case = _case(conn,case_id)
    if case['status']=='closed':
        raise ValueError('Buka kembali kasus sebelum melakukan review.')
    if verdict not in VERDICTS or verdict=='unreviewed':
        raise ValueError('Status verifikasi tidak valid.')
    note = _bounded(note,'Alasan review',20,1600)
    count = conn.execute('SELECT COUNT(*) FROM intel_investigation_items WHERE case_id=?',(case['id'],)).fetchone()[0]
    if count < 1:
        raise ValueError('Perlu minimal satu bukti sebelum melakukan review.')
    conn.execute('UPDATE intel_investigation_cases SET verdict=?,review_note=? WHERE id=?',(verdict,note,case['id']))
    _event(conn,case['id'],'analyst_review',f'{verdict}: {note}')
    conn.commit()
    return {'id':case['id'],'verdict':verdict,'reviewed':True, 'human_review':True}


def change_status(conn, case_id, status):
    case = _case(conn,case_id)
    if status not in STATUSES:
        raise ValueError('Status kasus tidak valid.')
    if status=='closed' and (case['verdict']=='unreviewed' or not case['review_note']):
        raise ValueError('Kasus harus melalui review sebelum ditutup.')
    conn.execute('UPDATE intel_investigation_cases SET status=? WHERE id=?',(status,case['id']))
    _event(conn,case['id'],'status_change',f"{case['status']} → {status}")
    conn.commit()
    return {'id':case['id'],'status':status}


def link_fact_case(conn, case_id, fact_case_id):
    case = _case(conn,case_id)
    if case['status']=='closed':
        raise ValueError('Kasus ditutup.')
    fact = conn.execute('SELECT * FROM fact_cases WHERE id=?',(_id(fact_case_id),)).fetchone()
    if not fact:
        raise ValueError('Kasus Fact Verification tidak ditemukan.')
    linked = conn.execute('''SELECT 1 FROM intel_investigation_items
          WHERE case_id=? AND kind='comment' AND source_ref=?''',(case['id'],fact['comment_id'])).fetchone()
    if not linked:
        raise ValueError('Lampirkan komentar yang sama sebagai bukti sebelum menautkan kasus cek fakta.')
    try:
        conn.execute('INSERT INTO intel_investigation_fact_links(case_id,fact_case_id,created_at) VALUES(?,?,?)',
                     (case['id'],fact['id'],now()))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Kasus cek fakta ini sudah ditautkan.') from exc
    _event(conn,case['id'],'fact_case_linked',f'Fact Verification #{fact["id"]} (bukan verifikasi otomatis).')
    conn.commit()
    return {'id':case['id'],'fact_case_id':fact['id']}


def _norm(text):
    text = re.sub(r'https?://\S+','',str(text or '').lower())
    return ' '.join(re.sub(r'[^\w\s]',' ',text).split())[:500]


def _correlations(items):
    # Evidence comparison only, never bot scoring or identity inference.
    matches=[]
    for i,a in enumerate(items[:80]):
        if a['kind']=='alert':
            continue
        left = _norm(a['body'])
        if len(left)<35:
            continue
        for b in items[i+1:80]:
            if b['kind']=='alert' or a['platform']==b['platform'] or a['provenance']!=b['provenance']:
                continue
            right=_norm(b['body'])
            if len(right)<35:
                continue
            score=round(difflib.SequenceMatcher(None,left,right,autojunk=False).ratio(),3)
            if score>=.85:
                matches.append({'left_id':a['id'],'right_id':b['id'],
                                'platforms':[a['platform'],b['platform']], 'similarity':score,
                                'note':'Kemiripan teks lintas platform; tidak membuktikan pengendali akun sama atau koordinasi.'})
    return sorted(matches,key=lambda x:-x['similarity'])[:30]


def case_detail(conn, case_id):
    case = _case(conn,case_id)
    items = [dict(row) for row in conn.execute('SELECT * FROM intel_investigation_items WHERE case_id=? ORDER BY id',(case['id'],))]
    for item in items:
        item['evidence_urls']=json.loads(item.pop('evidence_urls_json'))
        item['snapshot_integrity']='match' if _hash(item)==item['snapshot_sha256'] else 'changed'
    events=[dict(row) for row in conn.execute('SELECT * FROM intel_investigation_events WHERE case_id=? ORDER BY id DESC',(case['id'],))]
    facts=[dict(row) for row in conn.execute('''SELECT fc.id,fc.claim,fc.verdict,fc.reviewer_note,fc.reviewed_at
          FROM intel_investigation_fact_links l JOIN fact_cases fc ON fc.id=l.fact_case_id
          WHERE l.case_id=? ORDER BY fc.id''',(case['id'],))]
    return {**case,'items':items,'events':events,'fact_cases':facts,
            'cross_platform_matches':_correlations(items),
            'warning':'Hubungan aktivitas bukan bukti bot, click farm, identitas operator, atau kepalsuan klaim.'}


def list_cases(conn):
    cases=[dict(x) for x in conn.execute('''SELECT c.*,
      (SELECT COUNT(*) FROM intel_investigation_items i WHERE i.case_id=c.id) item_count,
      (SELECT COUNT(DISTINCT i.platform) FROM intel_investigation_items i WHERE i.case_id=c.id AND i.platform NOT IN ('monitor','reference')) platform_count
      FROM intel_investigation_cases c ORDER BY c.updated_at DESC,c.id DESC LIMIT 150''')]
    return {'cases':cases,'note':'REAL berarti bukan dataset simulasi; lead manual tetap belum diverifikasi.'}


def report_json(conn, case_id):
    import unified_fact
    detail=case_detail(conn,case_id)
    detail['unified_claim_reviews']=unified_fact.list_cases(conn,detail['id'])['cases']
    return {'project':'CAKRA v0.7','exported_at':now(),'investigation':detail,
            'limitations':['Sumber manual tidak diambil atau diverifikasi otomatis.',
                           'SHA-256 merupakan checksum snapshot lokal, bukan bukti forensik yang anti-manipulasi.',
                           'Kemiripan aktivitas tidak membuktikan bot atau operasi click farm.',
                           'Kesimpulan review berasal dari analis, bukan putusan AI.',
                           'Tidak ada publikasi komentar otomatis.']}


def report_markdown(conn, case_id):
    data=report_json(conn,case_id)
    c=data['investigation']
    clean=lambda s: str(s or '').replace('\r',' ').replace('\n',' ').replace('|','\\|')
    lines=[f'# CAKRA — Laporan Investigasi #{c["id"]}',
           '',f'**Judul:** {clean(c["title"])}', f'**Dataset:** {c["dataset"]}',
           f'**Status:** {c["status"]}',f'**Review analis:** {c["verdict"]}',
           f'**Dibuat:** {c["created_at"]}',f'**Ekspor:** {data["exported_at"]}',
           '', '## Ringkasan',clean(c['summary']) or '(Belum ada ringkasan)',
           '', '## Catatan verifikasi analis',clean(c['review_note']) or '(Belum ada review)',
           '', '## Bukti yang direkam']
    for item in c['items']:
        lines += [f'### Bukti #{item["id"]} — {clean(item["platform"])} / {clean(item["provenance"])}',
                  f'- Tipe: {item["kind"]}', f'- Sumber: {clean(item["source_url"])}',
                  f'- Waktu sumber: {clean(item["published_at"])}',
                  f'- SHA-256 snapshot: `{item["snapshot_sha256"]}`',
                  '', clean(item['body']), '']
        for u in item['evidence_urls']:
            lines.append('- Tautan pendukung sinyal: '+clean(u))
    lines += ['## Kemiripan teks lintas platform']
    for m in c['cross_platform_matches']:
        lines.append(f'- Bukti #{m["left_id"]} dan #{m["right_id"]}: kemiripan {m["similarity"]:.1%}. Hanya petunjuk.')
    lines += ['', '## Kasus pemeriksaan fakta terkait']
    for f in c['fact_cases']:
        lines.append(f'- Fact Verification #{f["id"]}: {f["verdict"]}. {clean(f["claim"])}')
    lines += ['', '## Pemeriksaan klaim lintas platform']
    for f in c.get('unified_claim_reviews',[]):
        lines += [f'### Klaim #{f["id"]} — {clean(f["platform"])}',
                  f'- Bukti sumber: #{f["item_id"]} ({clean(f["provenance"])})',
                  f'- Penilaian analis: {clean(f["verdict"])}',
                  f'- Klaim: {clean(f["claim"])}',
                  f'- Catatan: {clean(f["review_note"])}']
        for ev in f['evidence']:
            lines.append(f'- Sumber pemeriksaan: {clean(ev["title"])} — {clean(ev["url"])}')
        for r in f['responses']:
            lines.append(f'- Draft #{r["id"]}: {clean(r["status"])} (bukan publikasi otomatis)')
    lines += ['', '## Kronologi audit']
    for e in reversed(c['events']):
        lines.append(f'- {e["created_at"]} — {e["event_type"]}: {clean(e["note"])}')
    lines += ['', '## Keterbatasan'] + ['- '+x for x in data['limitations']]
    return '\n'.join(lines)+'\n'

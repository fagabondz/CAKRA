"""CAKRA v0.7 — source-bound, human-reviewed cross-platform claim workflow.

Never scrapes a web page, infers an operator identity, auto-verdicts or auto-publishes.
Source contents are frozen in a v0.6 investigation item with provenance labels.
"""
from __future__ import annotations

import sqlite3
import json

import factcheck
import investigation

VERDICTS = {'supported', 'contradicted', 'mixed', 'insufficient'}


def _id(value):
    return investigation._id(value)


def _text(value, name, minimum, maximum):
    return investigation._bounded(value, name, minimum, maximum)


def init_tables(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS unified_fact_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id),
        item_id INTEGER NOT NULL REFERENCES intel_investigation_items(id),
        snapshot_sha256 TEXT NOT NULL,
        claim TEXT NOT NULL,
        dataset TEXT NOT NULL CHECK(dataset IN ('real','demo')),
        verdict TEXT NOT NULL DEFAULT 'unreviewed'
          CHECK(verdict IN ('unreviewed','supported','contradicted','mixed','insufficient')),
        review_note TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_unified_inv ON unified_fact_cases(investigation_id);
      CREATE TABLE IF NOT EXISTS unified_fact_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER NOT NULL REFERENCES unified_fact_cases(id),
        source_type TEXT NOT NULL CHECK(source_type IN ('manual','factcheck_search')),
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        publisher TEXT NOT NULL DEFAULT '',
        textual_rating TEXT NOT NULL DEFAULT '',
        review_date TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_unified_evidence_case ON unified_fact_evidence(case_id);
      CREATE TABLE IF NOT EXISTS unified_fact_responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER NOT NULL REFERENCES unified_fact_cases(id),
        body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved')),
        created_at TEXT NOT NULL,
        approved_at TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_unified_responses_case ON unified_fact_responses(case_id);
    ''')


def _case(conn, identifier):
    case = conn.execute('SELECT * FROM unified_fact_cases WHERE id=?', (_id(identifier),)).fetchone()
    if not case:
        raise ValueError('Kasus pemeriksaan klaim tidak ditemukan.')
    return case


def _source(conn, investigation_id, item_id):
    inv_id, item_id = _id(investigation_id), _id(item_id)
    inv = conn.execute('SELECT * FROM intel_investigation_cases WHERE id=?', (inv_id,)).fetchone()
    if not inv:
        raise ValueError('Investigasi tidak ditemukan.')
    item = conn.execute('SELECT * FROM intel_investigation_items WHERE id=? AND case_id=?',
                        (item_id, inv_id)).fetchone()
    if not item:
        raise ValueError('Bukti bukan bagian dari investigasi yang dipilih.')
    if item['kind'] == 'alert':
        raise ValueError('Peringatan agregat bukan teks sumber klaim. Pilih komentar atau posting asli.')
    if ('demo' if item['provenance']=='demo' else 'real') != inv['dataset']:
        raise ValueError('Provenance bukti dan dataset kasus tidak cocok.')
    if not item['body'] or len(item['body'].strip()) < 15:
        raise ValueError('Teks sumber terlalu singkat untuk ekstraksi klaim.')
    return inv, item


def extract(conn, investigation_id, item_id, use_ai=False):
    _, item = _source(conn, investigation_id, item_id)
    if use_ai is not True and use_ai is not False:
        raise ValueError('Pengaturan AI harus boolean.')
    text = item['body'][:2500]
    if use_ai:
        result = factcheck.muse_extract(text, conn)
    else:
        result = factcheck.candidate_claims(text)
    result['source'] = {'item_id': item['id'], 'platform': item['platform'],
                        'provenance': item['provenance'], 'kind': item['kind']}
    return result


def create(conn, investigation_id, item_id, claim):
    inv, item = _source(conn, investigation_id, item_id)
    if inv['status'] == 'closed':
        raise ValueError('Buka kembali investigasi sebelum membuat pemeriksaan klaim.')
    claim = _text(claim, 'Klaim', 15, 1000)
    timestamp = factcheck.now_utc()
    cur = conn.execute('''INSERT INTO unified_fact_cases
      (investigation_id,item_id,snapshot_sha256,claim,dataset,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?)''', (inv['id'], item['id'], item['snapshot_sha256'], claim,
                                 inv['dataset'], timestamp, timestamp))
    investigation._event(conn, inv['id'], 'claim_review_opened',
                         f'Klaim #{cur.lastrowid} dari bukti #{item["id"]}; belum diverifikasi.')
    conn.commit()
    return {'id': cur.lastrowid, 'investigation_id': inv['id'], 'dataset': inv['dataset'],
            'verdict': 'unreviewed', 'verified': False}


def _unchanged_source(conn, fc):
    _, source = _source(conn, fc['investigation_id'], fc['item_id'])
    check=dict(source)
    check['evidence_urls']=json.loads(check['evidence_urls_json'])
    if (source['snapshot_sha256'] != fc['snapshot_sha256'] or
            investigation._hash(check) != source['snapshot_sha256']):
        raise ValueError('Snapshot sumber berubah; buat kasus klaim baru untuk sumber terkini.')
    return source


def add_evidence(conn, payload):
    fc = _case(conn, payload.get('case_id'))
    _unchanged_source(conn, fc)
    if conn.execute("SELECT 1 FROM unified_fact_responses WHERE case_id=? AND status='approved'",
                    (fc['id'],)).fetchone():
        raise ValueError('Sudah disetujui. Buat kasus klaim baru jika ada bukti tambahan.')
    source_type = str(payload.get('source_type') or 'manual')
    if source_type not in ('manual', 'factcheck_search'):
        raise ValueError('Tipe bukti tidak dikenali.')
    url = str(payload.get('url') or '').strip()
    title = _text(payload.get('title'), 'Judul bukti', 4, 240)
    if not factcheck.public_evidence_url(url):
        raise ValueError('URL bukti harus HTTPS publik yang valid.')
    publisher = _text(payload.get('publisher', ''), 'Penerbit', 0, 160)
    rating = _text(payload.get('rating', ''), 'Rating sumber', 0, 160)
    date = _text(payload.get('review_date', ''), 'Tanggal sumber', 0, 60)
    note = _text(payload.get('note', ''), 'Catatan bukti', 0, 1000)
    cur = conn.execute('''INSERT INTO unified_fact_evidence
     (case_id,source_type,title,url,publisher,textual_rating,review_date,note,created_at)
     VALUES (?,?,?,?,?,?,?,?,?)''', (fc['id'],source_type,title,url,publisher,rating,date,note,factcheck.now_utc()))
    conn.execute("UPDATE unified_fact_cases SET verdict='unreviewed',review_note='',reviewed_at=NULL,updated_at=? WHERE id=?",
                 (factcheck.now_utc(), fc['id']))
    conn.execute("DELETE FROM unified_fact_responses WHERE case_id=? AND status='draft'", (fc['id'],))
    investigation._event(conn, fc['investigation_id'], 'claim_evidence_added',
                         f'Klaim #{fc["id"]}, sumber #{cur.lastrowid}; perlu review ulang.')
    conn.commit()
    return {'id': cur.lastrowid, 'case_id': fc['id'], 'review_reset': True,
            'warning': 'Sumber dicatat sebagai kandidat bukti dan belum diperiksa otomatis.'}


def review(conn, case_id, verdict, note, sources_reviewed=False):
    fc = _case(conn, case_id)
    _unchanged_source(conn, fc)
    if sources_reviewed is not True:
        raise ValueError('Analis harus mengonfirmasi telah membuka dan meninjau sumber bukti.')
    if str(verdict) not in VERDICTS:
        raise ValueError('Status review tidak valid.')
    note = _text(note, 'Alasan review', 20, 1600)
    if not conn.execute('SELECT 1 FROM unified_fact_evidence WHERE case_id=?', (fc['id'],)).fetchone():
        raise ValueError('Minimal satu sumber bukti harus dilampirkan.')
    if conn.execute("SELECT 1 FROM unified_fact_responses WHERE case_id=? AND status='approved'",
                    (fc['id'],)).fetchone():
        raise ValueError('Sudah ada respons disetujui; buat kasus klaim baru untuk revisi.')
    timestamp = factcheck.now_utc()
    conn.execute('''UPDATE unified_fact_cases SET verdict=?,review_note=?,reviewed_at=?,updated_at=? WHERE id=?''',
                 (verdict, note, timestamp, timestamp, fc['id']))
    conn.execute("DELETE FROM unified_fact_responses WHERE case_id=? AND status='draft'",(fc['id'],))
    investigation._event(conn, fc['investigation_id'], 'claim_human_review',
                         f'Klaim #{fc["id"]}: {verdict} — dilakukan analis, bukan AI.')
    conn.commit()
    return {'id':fc['id'], 'verdict':verdict, 'human_review':True, 'published':False}


def draft(conn, case_id):
    fc = _case(conn, case_id)
    item = _unchanged_source(conn, fc)
    if fc['verdict'] == 'unreviewed' or not fc['reviewed_at']:
        raise ValueError('Simpan review analis sebelum membuat draft.')
    rows = conn.execute('SELECT * FROM unified_fact_evidence WHERE case_id=? ORDER BY id',(fc['id'],)).fetchall()
    if not rows:
        raise ValueError('Kasus belum memiliki bukti.')
    opener = {
        'supported': 'Sumber yang ditinjau memberikan dukungan terhadap klaim ini.',
        'contradicted': 'Sumber yang ditinjau memberikan informasi yang bertentangan dengan klaim ini.',
        'mixed': 'Klaim ini memerlukan konteks tambahan; sumber memberi gambaran yang tidak sepenuhnya sama.',
        'insufficient': 'Dari sumber yang diperiksa, klaim ini belum dapat dipastikan.'
    }[fc['verdict']]
    sources = []
    for evidence in rows[:3]:
        line = f"- {evidence['title']}"
        if evidence['publisher']:
            line += f" ({evidence['publisher']})"
        if evidence['review_date']:
            line += f" [{evidence['review_date'][:10]}]"
        sources.append(line + ': ' + evidence['url'])
    response = (f'{opener}\nKlaim yang diperiksa: “{fc["claim"]}”\n'
                f'Sumber untuk diperiksa:\n' + '\n'.join(sources) +
                f'\nPenjelasan analis: {fc["review_note"]}\n'
                'Catatan: verifikasi kembali konteks, waktu, dan tautan sebelum membagikan.')
    if item['provenance']=='manual':
        response += '\nCatatan internal: posting sumber dicatat manual, belum diambil ulang oleh API.'
    if fc['dataset']=='demo':
        response = '[SIMULASI — JANGAN DIPUBLIKASIKAN SEBAGAI KASUS NYATA]\n' + response
    timestamp = factcheck.now_utc()
    cur=conn.execute('INSERT INTO unified_fact_responses(case_id,body,created_at) VALUES (?,?,?)',
                     (fc['id'], response, timestamp))
    investigation._event(conn,fc['investigation_id'],'claim_draft_created',f'Klaim #{fc["id"]}, draft #{cur.lastrowid}.')
    conn.commit()
    return {'id':cur.lastrowid,'body':response,'status':'draft','published':False,'dataset':fc['dataset']}


def approve(conn, response_id, confirmed=False):
    if confirmed is not True:
        raise ValueError('Analis harus mengonfirmasi persetujuan draft.')
    row = conn.execute('''SELECT r.*, c.investigation_id,c.dataset,c.item_id,c.snapshot_sha256,
      c.reviewed_at,c.verdict FROM unified_fact_responses r
      JOIN unified_fact_cases c ON c.id=r.case_id WHERE r.id=?''', (_id(response_id),)).fetchone()
    if not row or row['status']!='draft':
        raise ValueError('Draft belum tersedia atau sudah disetujui.')
    _unchanged_source(conn,row)
    if row['verdict']=='unreviewed' or not row['reviewed_at']:
        raise ValueError('Review tidak berlaku lagi; buat draft baru.')
    timestamp=factcheck.now_utc()
    conn.execute('UPDATE unified_fact_responses SET status=?,approved_at=? WHERE id=?',
                 ('approved',timestamp,row['id']))
    investigation._event(conn,row['investigation_id'],'claim_draft_approved',
                         f'Draft #{row["id"]} disetujui untuk salin manual; tidak dipublikasikan otomatis.')
    conn.commit()
    return {'id':row['id'],'status':'approved','published':False,'dataset':row['dataset']}


def list_cases(conn, investigation_id=None):
    if investigation_id is not None:
        _id(investigation_id)
    clauses = ' WHERE f.investigation_id=?' if investigation_id is not None else ''
    args = (int(investigation_id),) if investigation_id is not None else ()
    rows = conn.execute('''SELECT f.*, i.platform, i.provenance, i.kind, i.source_url,
          i.body AS source_body, i.author_name, i.snapshot_sha256 AS item_sha256
        FROM unified_fact_cases f JOIN intel_investigation_items i ON i.id=f.item_id'''
        + clauses + ' ORDER BY f.id DESC LIMIT 150',args).fetchall()
    cases=[]
    for row in rows:
        data=dict(row)
        data['evidence']=[dict(e) for e in conn.execute('SELECT * FROM unified_fact_evidence WHERE case_id=? ORDER BY id', (row['id'],))]
        data['responses']=[dict(r) for r in conn.execute('SELECT * FROM unified_fact_responses WHERE case_id=? ORDER BY id DESC LIMIT 20',(row['id'],))]
        data['snapshot_integrity']='match' if row['snapshot_sha256']==row['item_sha256'] else 'changed'
        cases.append(data)
    return {'cases': cases,'warning':'Sumber manual dan hasil API bukan verifikasi otomatis; keputusan hanya dari analis.'}


def approved_responses(conn):
    return [dict(r) for r in conn.execute('''SELECT r.id,r.case_id,r.body,r.approved_at,
      c.claim,c.verdict,c.dataset, i.platform,i.source_url,c.investigation_id
      FROM unified_fact_responses r JOIN unified_fact_cases c ON c.id=r.case_id
      JOIN intel_investigation_items i ON i.id=c.item_id
      WHERE r.status='approved' ORDER BY r.id DESC LIMIT 80''')]

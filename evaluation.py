"""CAKRA v0.9: human-labeled evaluation of CLAIM CANDIDATE DISCOVERY.

Metrics evaluate whether a public text should enter a fact-checking queue, *not*
truthfulness, bot status, sentiment, author identity, or real-world prevalence.
No external requests, autonomous conclusions or publishing.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import discovery

LABELS = {'checkable', 'not_checkable', 'uncertain'}
KINDS = {'comment', 'observation'}
DATASETS = {'real', 'demo'}
MAX_RECENT_PER_SOURCE = 500


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS eval_source_labels (
      kind TEXT NOT NULL CHECK(kind IN ('comment','observation')),
      source_ref TEXT NOT NULL,
      dataset TEXT NOT NULL CHECK(dataset IN ('real','demo')),
      content_sha256 TEXT NOT NULL,
      label TEXT NOT NULL CHECK(label IN ('checkable','not_checkable','uncertain')),
      rationale TEXT NOT NULL,
      reviewer TEXT NOT NULL DEFAULT 'Local analyst',
      labeled_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(kind,source_ref)
    );
    CREATE INDEX IF NOT EXISTS idx_eval_labels_dataset ON eval_source_labels(dataset,label);
    CREATE TABLE IF NOT EXISTS eval_audit_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      dataset TEXT NOT NULL CHECK(dataset IN ('real','demo')),
      category TEXT NOT NULL,
      subject_kind TEXT NOT NULL,
      subject_ref TEXT NOT NULL,
      previous_json TEXT NOT NULL,
      current_json TEXT NOT NULL,
      reason TEXT NOT NULL,
      occurred_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_eval_audit_dataset ON eval_audit_events(dataset,id);
    ''')


def _dataset(dataset):
    if dataset not in DATASETS:
        raise ValueError('Pilih dataset real atau demo secara terpisah.')
    return dataset


def _source(conn, kind, ref):
    kind = str(kind or '')
    ref = str(ref or '').strip()
    if kind not in KINDS or not ref or len(ref) > 250:
        raise ValueError('Referensi sumber tidak valid.')
    if kind == 'comment':
        row = conn.execute('''SELECT 'comment' kind,comment_id source_ref,
            'youtube' platform,dataset,
            CASE WHEN dataset='demo' THEN 'demo' ELSE 'api' END provenance,
            author_name,body,source_url,published_at,collected_at
            FROM comments WHERE comment_id=?''', (ref,)).fetchone()
    else:
        if not ref.isdigit():
            raise ValueError('ID observasi tidak valid.')
        row = conn.execute('''SELECT 'observation' kind,CAST(id AS TEXT) source_ref,
            platform,CASE WHEN provenance='demo' THEN 'demo' ELSE 'real' END dataset,
            provenance,author_name,body,source_url,published_at,collected_at
            FROM intel_observations WHERE id=?''', (int(ref),)).fetchone()
    if row is None:
        raise ValueError('Sumber tidak ditemukan dalam database lokal.')
    return dict(row)


def _fingerprint(s):
    value = json.dumps([s['kind'],s['source_ref'],s['dataset'],s['body']], ensure_ascii=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _predicted(conn, s):
    """True only if CURRENT extracted text has a matching STORED v0.8 candidate.

    Analyst must run Claim Discovery scan before evaluating for meaningful scores.
    Dismissed/investigating candidates still count as a positive prediction.
    """
    claim = discovery.extract_candidate(s['body'])
    if not claim:
        return False
    return conn.execute('''SELECT 1 FROM discovery_candidates
        WHERE kind=? AND source_ref=? AND claim_hash=? LIMIT 1''',
        (s['kind'],s['source_ref'],discovery.claim_key(claim))).fetchone() is not None


def _decorate(conn, source):
    s = dict(source)
    s['predicted'] = _predicted(conn, s)
    s['content_sha256'] = _fingerprint(s)
    label = conn.execute('''SELECT label,rationale,reviewer,content_sha256,updated_at FROM eval_source_labels
        WHERE kind=? AND source_ref=?''', (s['kind'],s['source_ref'])).fetchone()
    s['human_label'] = label['label'] if label and label['content_sha256'] == s['content_sha256'] else None
    s['label_stale'] = bool(label and label['content_sha256'] != s['content_sha256'])
    s['rationale'] = label['rationale'] if s['human_label'] else ''
    s['reviewer'] = label['reviewer'] if s['human_label'] else ''
    s['label_updated_at'] = label['updated_at'] if s['human_label'] else None
    return s


def sample(conn, dataset='real', limit=80):
    """Return both detected and missed items; deterministic selection independent of score.

    Two candidate frames: newest N from each table, then stable hash-order selection.
    This is a practical sample, not representative of all public posts.
    """
    dataset = _dataset(dataset)
    try:
        limit = int(limit)
    except (ValueError, TypeError) as exc:
        raise ValueError('Limit tidak valid.') from exc
    if not 1 <= limit <= 200:
        raise ValueError('Limit sampel harus antara 1–200.')
    comments = [dict(r) for r in conn.execute('''SELECT comment_id AS source_ref,'comment' AS kind,
        'youtube' AS platform,dataset,CASE WHEN dataset='demo' THEN 'demo' ELSE 'api' END AS provenance,
        author_name,body,source_url,published_at,collected_at FROM comments WHERE dataset=?
        ORDER BY collected_at DESC,comment_id DESC LIMIT ?''',(dataset,MAX_RECENT_PER_SOURCE))]
    observations = [dict(r) for r in conn.execute('''SELECT CAST(id AS TEXT) AS source_ref,
        'observation' AS kind,platform,CASE WHEN provenance='demo' THEN 'demo' ELSE 'real' END AS dataset,
        provenance,author_name,body,source_url,published_at,collected_at FROM intel_observations
        WHERE CASE WHEN provenance='demo' THEN 'demo' ELSE 'real' END = ?
        ORDER BY collected_at DESC,id DESC LIMIT ?''',(dataset,MAX_RECENT_PER_SOURCE))]
    pool = comments+observations
    # Sort by stable hash rather than priority or label: includes classifier negatives.
    pool.sort(key=lambda s:hashlib.sha256((s['kind']+':'+s['source_ref']).encode('utf-8')).digest())
    rows = [_decorate(conn,s) for s in pool[:limit]]
    return {'dataset':dataset,'sample_size':len(rows),'pool_size':len(pool),
            'sources':rows,'warning':'Sampel deterministik dari sumber terbaru, bukan sampel acak populasi media sosial. Termasuk data yang tidak terdeteksi.'}


def _audit(conn, dataset, category, kind, ref, previous, current, reason):
    conn.execute('''INSERT INTO eval_audit_events
        (dataset,category,subject_kind,subject_ref,previous_json,current_json,reason,occurred_at)
        VALUES (?,?,?,?,?,?,?,?)''', (dataset,category,kind,str(ref),
        json.dumps(previous,ensure_ascii=False,sort_keys=True),
        json.dumps(current,ensure_ascii=False,sort_keys=True),reason,now()))


def label_source(conn, kind, ref, label, rationale, reviewer='Local analyst', expected_sha256=None):
    if label not in LABELS:
        raise ValueError('Label harus checkable, not_checkable, atau uncertain.')
    rationale = str(rationale or '').strip()
    reviewer = str(reviewer or '').strip() or 'Local analyst'
    if not 10 <= len(rationale) <= 1500:
        raise ValueError('Catatan dasar penilaian harus 10–1500 karakter.')
    if not 2 <= len(reviewer) <= 80:
        raise ValueError('Nama/alias reviewer harus 2–80 karakter.')
    s = _source(conn,kind,ref)
    digest = _fingerprint(s)
    if expected_sha256 and expected_sha256 != digest:
        raise ValueError('Sumber sudah berubah. Muat ulang sebelum menyimpan label.')
    before = conn.execute('''SELECT label,rationale,reviewer,content_sha256 FROM eval_source_labels
        WHERE kind=? AND source_ref=?''',(s['kind'],s['source_ref'])).fetchone()
    old = dict(before) if before else None
    new = {'label':label,'rationale':rationale,'reviewer':reviewer,'content_sha256':digest}
    stamp=now()
    conn.execute('''INSERT INTO eval_source_labels
        (kind,source_ref,dataset,content_sha256,label,rationale,reviewer,labeled_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(kind,source_ref) DO UPDATE SET
        dataset=excluded.dataset,content_sha256=excluded.content_sha256,label=excluded.label,
        rationale=excluded.rationale,reviewer=excluded.reviewer,updated_at=excluded.updated_at''',
        (s['kind'],s['source_ref'],s['dataset'],digest,label,rationale,reviewer,stamp,stamp))
    _audit(conn,s['dataset'],'source_label',s['kind'],s['source_ref'],old,new,rationale)
    conn.commit()
    return {'kind':s['kind'],'source_ref':s['source_ref'],'dataset':s['dataset'],'label':label,'saved':True}


def audit_case_review(conn, dataset, category, case_id, before, after, note):
    """Append local log of an analyst review decision; not tamper-proof."""
    dataset = _dataset(dataset)
    if category not in ('investigation_review','unified_fact_review','legacy_fact_review'):
        raise ValueError('Kategori audit tidak dikenal.')
    _audit(conn,dataset,category,'case',str(case_id),before,after,str(note or '')[:1500])
    conn.commit()


def _metrics(conn, dataset):
    _dataset(dataset)
    counts = {'TP':0,'FP':0,'FN':0,'TN':0}
    uncertain, stale, missing = 0, 0, 0
    outcomes=[]
    rows = conn.execute('SELECT * FROM eval_source_labels WHERE dataset=? ORDER BY updated_at DESC',(dataset,)).fetchall()
    for row in rows:
        try:
            s=_source(conn,row['kind'],row['source_ref'])
        except ValueError:
            missing+=1;continue
        if s['dataset'] != dataset or _fingerprint(s) != row['content_sha256']:
            stale+=1;continue
        if row['label']=='uncertain':
            uncertain+=1;continue
        prediction = _predicted(conn,s)
        truth = row['label']=='checkable'
        outcome = 'TP' if prediction and truth else 'FP' if prediction else 'FN' if truth else 'TN'
        counts[outcome]+=1
        outcomes.append({'kind':s['kind'],'source_ref':s['source_ref'],'platform':s['platform'],
                         'source_url':s['source_url'],'outcome':outcome,'label':row['label'],
                         'predicted':prediction,'rationale':row['rationale'],'reviewer':row['reviewer']})
    tp,fp,fn,tn=(counts[k] for k in ('TP','FP','FN','TN'))
    precision=tp/(tp+fp) if tp+fp else None
    recall=tp/(tp+fn) if tp+fn else None
    f1=(2*precision*recall/(precision+recall) if precision+recall else 0.0) if precision is not None and recall is not None else None
    total=tp+fp+fn+tn
    accuracy=(tp+tn)/total if total else None
    return {'dataset':dataset,'counts':counts,'metrics':{'precision':precision,'recall':recall,'f1':f1,'accuracy':accuracy},
            'labeled_count':len(rows),'evaluated_count':total,'uncertain_count':uncertain,
            'stale_count':stale,'missing_count':missing,'error_examples':[r for r in outcomes if r['outcome'] in ('FP','FN')][:30],
            'methodology':'Unit evaluasi: teks sumber (satu komentar/posting). Positif prediksi = kandidat dari pemindaian v0.8 tersimpan dan cocok dengan teks saat ini. Ground truth = label analis: memiliki klaim faktual yang layak diperiksa, bukan hoaks.',
            'limitations':['Tidak mengukur kebenaran klaim, ketepatan identitas, deteksi bot, click farm, atau kualitas kutipan ekstraksi.',
                'Hanya sumber berlabel manusia yang masuk denominator; pemilihan label dapat bias.',
                'Dataset demo dan real dihitung terpisah; data nyata belum mewakili seluruh populasi.',
                'Label tidak pasti dan label kedaluwarsa dikecualikan; pemeriksaan silang label antar-analis belum tersedia.',
                'Jalankan Claim Discovery Scan sebelum evaluasi agar prediksi tersimpan mutakhir.']}


def dashboard(conn,dataset='real'):
    return _metrics(conn,dataset)


def audit_log(conn,dataset='real',limit=40):
    dataset=_dataset(dataset)
    try:
        limit=int(limit)
    except (TypeError,ValueError) as exc:
        raise ValueError('Limit tidak valid.') from exc
    if not 1 <= limit <= 100:
        raise ValueError('Limit audit 1–100.')
    result=[dict(r) for r in conn.execute('''SELECT * FROM eval_audit_events
        WHERE dataset=? ORDER BY id DESC LIMIT ?''',(dataset,limit))]
    for r in result:
        r['previous']=json.loads(r.pop('previous_json'))
        r['current']=json.loads(r.pop('current_json'))
    return {'dataset':dataset,'events':result,'note':'Audit SQLite lokal; dapat dimodifikasi oleh siapa pun yang memiliki akses tulis ke file database.'}


def report_json(conn,dataset='real'):
    metrics=_metrics(conn,dataset)
    return {'application':'CAKRA','version':'0.9','report_type':'Claim Discovery Evaluation',
            'generated_at':now(),**metrics,'caution':'Tidak ada dasar untuk mengklaim akurasi deteksi hoaks/bot dari hasil evaluasi ini.'}


def report_markdown(conn,dataset='real'):
    r=report_json(conn,dataset)
    m=r['metrics'];c=r['counts']
    fmt=lambda n: 'Belum terdefinisi (denominator nol)' if n is None else f'{n*100:.2f}%'
    rows=[f'# CAKRA v0.9 — Evaluasi Claim Discovery ({dataset.upper()})',
          '',f'Tanggal ekspor: {r["generated_at"]}',
          '', '## Sampel dan ground truth',
          f'- Sumber berlabel: {r["labeled_count"]}',f'- Sampel layak evaluasi: {r["evaluated_count"]}',
          f'- Label tidak pasti: {r["uncertain_count"]}',f'- Label stale: {r["stale_count"]}',
          f'- Sumber hilang: {r["missing_count"]}',
          '', '## Confusion matrix',
          '| | Label checkable | Label not_checkable |', '|---|---:|---:|',
          f'| Terdeteksi | TP: {c["TP"]} | FP: {c["FP"]} |',
          f'| Tidak terdeteksi | FN: {c["FN"]} | TN: {c["TN"]} |',
          '', '## Metrics',
          f'- Precision: **{fmt(m["precision"])}**',f'- Recall: **{fmt(m["recall"])}**',
          f'- F1: **{fmt(m["f1"])}**',f'- Accuracy: **{fmt(m["accuracy"])}**',
          '', '## Metode',r['methodology'],
          '', '## Batasan']+[f'- {s}' for s in r['limitations']]
    rows+=['','## Contoh kesalahan yang perlu dianalisis']
    for x in r['error_examples']:
        rows.append(f'- {x["outcome"]} · {x["platform"]} · {x["kind"]} `{x["source_ref"]}` · {x["source_url"]}')
    if not r['error_examples']:
        rows.append('- Tidak ada contoh FP/FN pada sampel yang diberi label. Ini bukan jaminan sistem bebas kesalahan.')
    return '\n'.join(rows)+'\n'

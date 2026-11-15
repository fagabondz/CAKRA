"""Auditable, bounded screening of stored public-source content."""
from __future__ import annotations
import difflib, hashlib, json, re, unicodedata
from datetime import datetime, timezone
import factcheck

RULES={
 'cyber':r'\b(siber|cyber|ransomware|phishing|malware|peretasan|kerentanan|kebocoran data)\b',
 'infrastructure':r'\b(gangguan layanan|layanan digital|jaringan telekomunikasi|infrastruktur digital|pusat data)\b',
 'integrity':r'\b(klarifikasi|pemeriksaan fakta|disinformasi|informasi palsu|klaim faktual)\b',
 'safety':r'\b(peringatan keamanan|peringatan keselamatan|imbauan keamanan)\b'
}
def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def normal(text):return ' '.join(unicodedata.normalize('NFKC',text).casefold().split())
def stance_markers(text):
    return bool(re.search(r'\b(tidak|bukan|hoaks|salah|bantah|dibantah|keliru|palsu|not|false|deny)\b',text))
def candidate_tokens(text):
    stop={'yang','dan','untuk','dari','pada','dengan','dalam','atau','the','and','with','this'}
    words={word for word in re.findall(r'\w{4,}',text) if word not in stop}
    return sorted(words,key=lambda word:hashlib.sha256(word.encode()).digest())[:8]

# Explainable topic candidates, not factual equivalence or a trained semantic model.
CONCEPTS={
 'digital_service':r'\b(layanan digital|pelayanan digital|sistem layanan|sistem online|layanan online|sistem daring)\b',
 'outage':r'\b(terganggu|gangguan|tidak dapat diakses|tidak bisa diakses|tak dapat diakses|tidak tersedia|mati|lumpuh)\b',
 'cyber':r'\b(siber|cyber|malware|ransomware|peretasan|diretas)\b',
 'data_breach':r'\b(kebocoran data|data bocor|data pribadi bocor)\b',
}
def concepts(text):
    text=normal(text)
    found={k for k,p in CONCEPTS.items() if re.search(p,text)}
    # Corrections and denials require human interpretation, never merge them here.
    if re.search(r'\b(hoaks|bantah|dibantah|tidak benar|bukan benar|klarifikasi)\b',text): return ()
    if {'digital_service','outage'} <= found: return ('digital_service+outage',)
    if {'cyber','data_breach'} <= found: return ('cyber+data_breach',)
    return ()

def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS screening_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, source TEXT NOT NULL,
      max_comment_rowid INTEGER NOT NULL, max_observation_id INTEGER NOT NULL,
      comment_cursor INTEGER NOT NULL DEFAULT 0, observation_cursor INTEGER NOT NULL DEFAULT 0,
      processed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS screening_results(
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES screening_runs(id),
      kind TEXT NOT NULL, source_ref TEXT NOT NULL, source_url TEXT NOT NULL, published_at TEXT NOT NULL,
      content_sha256 TEXT NOT NULL, normalized TEXT NOT NULL, prefix TEXT NOT NULL, category TEXT NOT NULL,
      decision TEXT NOT NULL, priority INTEGER NOT NULL, explanation TEXT NOT NULL,
      duplicate_of INTEGER REFERENCES screening_results(id), ai_status TEXT NOT NULL DEFAULT 'not_processed',
      ai_json TEXT NOT NULL DEFAULT '', review_status TEXT NOT NULL DEFAULT 'unreviewed',
      UNIQUE(run_id,kind,source_ref)
    );
    CREATE INDEX IF NOT EXISTS idx_screen_feed ON screening_results(run_id,decision,priority DESC,id);
    CREATE INDEX IF NOT EXISTS idx_screen_prefix ON screening_results(run_id,prefix);
    CREATE TABLE IF NOT EXISTS screening_candidate_tokens(
      run_id INTEGER NOT NULL REFERENCES screening_runs(id), token TEXT NOT NULL,
      result_id INTEGER NOT NULL REFERENCES screening_results(id), PRIMARY KEY(run_id,token,result_id));
    CREATE TABLE IF NOT EXISTS screening_concepts(
      run_id INTEGER NOT NULL REFERENCES screening_runs(id), concept TEXT NOT NULL,
      result_id INTEGER NOT NULL REFERENCES screening_results(id), PRIMARY KEY(run_id,concept,result_id));
    CREATE TABLE IF NOT EXISTS screening_related(
      result_id INTEGER NOT NULL REFERENCES screening_results(id), related_id INTEGER NOT NULL REFERENCES screening_results(id),
      rationale TEXT NOT NULL, PRIMARY KEY(result_id,related_id));
    CREATE INDEX IF NOT EXISTS idx_screening_related_reverse ON screening_related(related_id,result_id);
    CREATE TABLE IF NOT EXISTS screening_audit(id INTEGER PRIMARY KEY, result_id INTEGER NOT NULL REFERENCES screening_results(id), action TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')
    columns={row[1] for row in conn.execute('PRAGMA table_info(screening_runs)')}
    for name,definition in (('job_id','INTEGER REFERENCES collection_jobs(id)'),('max_job_item_id','INTEGER NOT NULL DEFAULT 0'),('job_item_cursor','INTEGER NOT NULL DEFAULT 0')):
        if name not in columns: conn.execute('ALTER TABLE screening_runs ADD COLUMN '+name+' '+definition)

def start(conn,query,source='all',job_id=None):
    if not isinstance(query,str) or len(query.strip())>120:raise ValueError('Topik maksimal 120 karakter.')
    if source not in ('all','comments','observations'):raise ValueError('Sumber penyaringan tidak dikenal.')
    max_item=0
    if job_id is not None:
        if isinstance(job_id,bool): raise ValueError('ID pekerjaan tidak valid.')
        try: job_id=int(job_id)
        except (TypeError,ValueError,OverflowError) as exc: raise ValueError('ID pekerjaan tidak valid.') from exc
        job=conn.execute('SELECT source,retrieved_count FROM collection_jobs WHERE id=?',(job_id,)).fetchone()
        if not job: raise ValueError('Pekerjaan pengumpulan tidak ditemukan.')
        required='comments' if job['source']=='youtube' else 'observations'
        if source not in ('all',required): raise ValueError('Sumber penyaringan tidak cocok dengan pekerjaan.')
        source=required
        max_item=conn.execute('SELECT COALESCE(MAX(id),0) FROM collection_job_items WHERE job_id=?',(job_id,)).fetchone()[0]
        if not max_item and job['retrieved_count']:
            raise ValueError('Pekerjaan lama tidak memiliki relasi record; jalankan pengumpulan baru untuk cakupan khusus.')
    cm=conn.execute("SELECT COALESCE(MAX(rowid),0) FROM comments WHERE dataset='real'").fetchone()[0] if source in ('all','comments') else 0
    ob=conn.execute("SELECT COALESCE(MAX(id),0) FROM intel_observations WHERE provenance!='demo'").fetchone()[0] if source in ('all','observations') else 0
    stamp=now();ident=conn.execute("INSERT INTO screening_runs(query,source,max_comment_rowid,max_observation_id,status,created_at,updated_at,job_id,max_job_item_id) VALUES(?,?,?,?,'ready',?,?,?,?)",(query.strip(),source,cm,ob,stamp,stamp,job_id,max_item)).lastrowid;conn.commit();return get_run(conn,ident)

def get_run(conn,ident):
    try:
        if isinstance(ident,bool): raise ValueError
        ident=int(ident)
    except (TypeError,ValueError,OverflowError) as exc:raise ValueError('ID penyaringan tidak valid.') from exc
    row=conn.execute('SELECT * FROM screening_runs WHERE id=?',(ident,)).fetchone()
    if not row:raise ValueError('Penyaringan tidak ditemukan.')
    return dict(row)

def classify(text,query,url):
    text=normal(text);terms=[word for word in re.findall(r'\w+',normal(query)) if len(word)>2][:12]
    matched=[term for term in terms if term in text]
    category=next((name for name,pattern in RULES.items() if re.search(pattern,text)),'uncertain')
    source_ok=factcheck.public_evidence_url(url)
    if category!='uncertain' and (not terms or matched) and source_ok:decision='relevant'
    elif matched or (category!='uncertain' and not source_ok):decision='uncertain'
    else:decision='filtered'
    score=(40 if category!='uncertain' else 0)+(min(30,len(matched)*10))+(15 if source_ok else 0)
    explanation='Kategori '+category+'; kata topik cocok '+str(len(matched))+'/'+str(len(terms))+'; sumber HTTPS '+('tersedia' if source_ok else 'belum tersedia')+'. Skor prioritas hanya urutan pemeriksaan.'
    return category,decision,score,explanation

def run_batch(conn,ident,limit=150, *, dataset='real'):
    if dataset not in ('real','demo'):raise ValueError('Dataset tidak valid.')
    if isinstance(limit,bool):raise ValueError('Batas batch tidak valid.')
    try:limit=int(limit)
    except (TypeError,ValueError,OverflowError) as exc:raise ValueError('Batas batch tidak valid.') from exc
    if not 1<=limit<=200:raise ValueError('Batch harus 1–200 item.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        run=get_run(conn,ident)
        if run['status']=='completed':conn.commit();return run
        remaining=limit;processed=0;comment_cursor=run['comment_cursor'];observation_cursor=run['observation_cursor']
        job_cursor=run['job_item_cursor']
        if run['job_id'] is not None:
            rows=conn.execute('''SELECT i.id AS cursor,i.kind,i.source_ref,
              CASE WHEN i.kind='comment' THEN c.body ELSE o.body END AS body,
              CASE WHEN i.kind='comment' THEN c.source_url ELSE o.source_url END AS source_url,
              CASE WHEN i.kind='comment' THEN c.published_at ELSE o.published_at END AS published_at
              FROM collection_job_items i LEFT JOIN comments c ON i.kind='comment' AND c.comment_id=i.source_ref AND c.dataset=?
              LEFT JOIN intel_observations o ON i.kind='observation' AND o.id=CAST(i.source_ref AS INTEGER) AND o.provenance!='demo'
              WHERE i.job_id=? AND i.id>? AND i.id<=? AND (c.comment_id IS NOT NULL OR o.id IS NOT NULL)
              ORDER BY i.id LIMIT ?''',(dataset,run['job_id'],job_cursor,run['max_job_item_id'],remaining)).fetchall()
            items=[(r['kind'],dict(r)) for r in rows]
        else:
            comments=conn.execute("SELECT rowid AS cursor,comment_id AS source_ref,body,source_url,published_at FROM comments WHERE dataset='real' AND rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",(comment_cursor,run['max_comment_rowid'],remaining)).fetchall()
            items=[('comment',dict(r)) for r in comments];remaining-=len(comments)
            if remaining:
                obs=conn.execute("SELECT id AS cursor,CAST(id AS TEXT) AS source_ref,body,source_url,published_at FROM intel_observations WHERE provenance!='demo' AND id>? AND id<=? ORDER BY id LIMIT ?",(observation_cursor,run['max_observation_id'],remaining)).fetchall()
                items += [('observation',dict(r)) for r in obs]
        for kind,row in items:
            text=str(row['body'] or '');normalized=normal(text)[:2500];prefix=normalized[:24];duplicate=None
            if prefix:
                previous=conn.execute('SELECT id,normalized FROM screening_results WHERE run_id=? AND prefix=? ORDER BY id DESC LIMIT 30',(run['id'],prefix)).fetchall()
                candidates={old['id']:old for old in previous}
                for token in candidate_tokens(normalized):
                    matches=conn.execute('''SELECT r.id,r.normalized FROM screening_candidate_tokens t
                        JOIN screening_results r ON r.id=t.result_id WHERE t.run_id=? AND t.token=?
                        ORDER BY t.result_id DESC LIMIT 15''',(run['id'],token)).fetchall()
                    for old in matches: candidates[old['id']]=old
                previous=list(candidates.values())[:120]
                for old in previous:
                    if normalized==old['normalized'] or (stance_markers(normalized)==stance_markers(old['normalized']) and len(normalized)>30 and difflib.SequenceMatcher(None,normalized,old['normalized'],autojunk=True).ratio()>=.94):duplicate=old['id'];break
            category,decision,priority,explanation=classify(text,run['query'],row['source_url'])
            result_id=conn.execute('''INSERT INTO screening_results(run_id,kind,source_ref,source_url,published_at,content_sha256,normalized,prefix,category,decision,priority,explanation,duplicate_of)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(run['id'],kind,str(row['source_ref']),row['source_url'] or '',row['published_at'] or '',hashlib.sha256(text.encode()).hexdigest(),normalized,prefix,category,decision,priority,explanation,duplicate)).lastrowid
            conn.executemany('INSERT INTO screening_candidate_tokens(run_id,token,result_id) VALUES(?,?,?)',[(run['id'],token,result_id) for token in candidate_tokens(normalized)])
            for concept in concepts(normalized):
                candidates=conn.execute('SELECT result_id FROM screening_concepts WHERE run_id=? AND concept=? ORDER BY result_id DESC LIMIT 3',(run['id'],concept)).fetchall()
                for candidate in candidates:
                    conn.execute('INSERT OR IGNORE INTO screening_related(result_id,related_id,rationale) VALUES(?,?,?)',
                        (result_id,candidate['result_id'],'Konsep bersama: '+concept+'; hubungan topik kandidat, waktu dan lokasi perlu diperiksa.'))
                conn.execute('INSERT INTO screening_concepts(run_id,concept,result_id) VALUES(?,?,?)',(run['id'],concept,result_id))
            if run['job_id'] is not None: job_cursor=row['cursor']
            elif kind=='comment':comment_cursor=row['cursor']
            else:observation_cursor=row['cursor']
            processed+=1
        more_comments=conn.execute("SELECT 1 FROM comments WHERE dataset='real' AND rowid>? AND rowid<=? LIMIT 1",(comment_cursor,run['max_comment_rowid'])).fetchone()
        more_observations=conn.execute("SELECT 1 FROM intel_observations WHERE provenance!='demo' AND id>? AND id<=? LIMIT 1",(observation_cursor,run['max_observation_id'])).fetchone()
        if run['job_id'] is not None:
            more=conn.execute('''SELECT 1 FROM collection_job_items i LEFT JOIN comments c ON i.kind='comment' AND c.comment_id=i.source_ref AND c.dataset=?
              LEFT JOIN intel_observations o ON i.kind='observation' AND o.id=CAST(i.source_ref AS INTEGER) AND o.provenance!='demo'
              WHERE i.job_id=? AND i.id>? AND i.id<=? AND (c.comment_id IS NOT NULL OR o.id IS NOT NULL) LIMIT 1''',(dataset,run['job_id'],job_cursor,run['max_job_item_id'])).fetchone()
            state='ready' if more else 'completed'
        else: state='ready' if more_comments or more_observations else 'completed'
        conn.execute('UPDATE screening_runs SET comment_cursor=?,observation_cursor=?,job_item_cursor=?,processed=processed+?,status=?,updated_at=? WHERE id=?',(comment_cursor,observation_cursor,job_cursor,processed,state,now(),run['id']));conn.commit();return get_run(conn,run['id'])
    except Exception:conn.rollback();raise

def results(conn,ident,decision='relevant',page=1,page_size=25):
    run=get_run(conn,ident)
    if decision not in ('relevant','uncertain','filtered','all'):raise ValueError('Filter hasil tidak dikenal.')
    try:page=int(page);page_size=int(page_size)
    except (TypeError,ValueError,OverflowError) as exc:raise ValueError('Halaman tidak valid.') from exc
    if not 1<=page<=100000 or not 1<=page_size<=50:raise ValueError('Halaman tidak valid.')
    where='run_id=?';params=[run['id']]
    if decision!='all':where+=' AND decision=?';params.append(decision)
    total=conn.execute('SELECT count(*) FROM screening_results WHERE '+where,params).fetchone()[0]
    rows=[dict(r) for r in conn.execute('''SELECT id,kind,source_ref,source_url,published_at,content_sha256,category,decision,priority,explanation,duplicate_of,ai_status,ai_json,review_status FROM screening_results WHERE '''+where+' ORDER BY priority DESC,id LIMIT ? OFFSET ?',params+[page_size,(page-1)*page_size])]
    for row in rows:
        detail=source(conn,row);row['excerpt']=(detail['text'] or '')[:300];row['source_changed']=detail['source_changed'];row.pop('ai_json',None)
    counts={r[0]:r[1] for r in conn.execute('SELECT decision,count(*) FROM screening_results WHERE run_id=? GROUP BY decision',(run['id'],))}
    duplicate_count=conn.execute('SELECT count(*) FROM screening_results WHERE run_id=? AND duplicate_of IS NOT NULL',(run['id'],)).fetchone()[0]
    reviewed=conn.execute("SELECT count(*) FROM screening_results WHERE run_id=? AND review_status='reviewed'",(run['id'],)).fetchone()[0]
    return {'run':run,'results':rows,'total':total,'page':page,'page_size':page_size,'counts':counts,'duplicates':duplicate_count,'reviewed':reviewed}

def source(conn,row):
    if row['kind']=='comment':
        found=conn.execute("SELECT body FROM comments WHERE dataset='real' AND comment_id=?",(row['source_ref'],)).fetchone()
    else:
        found=conn.execute("SELECT body FROM intel_observations WHERE provenance!='demo' AND id=?",(row['source_ref'],)).fetchone()
    text=found['body'] if found else ''
    return {'text':text,'source_changed':not found or hashlib.sha256(text.encode()).hexdigest()!=row['content_sha256']}

def detail(conn,ident):
    row=conn.execute('SELECT * FROM screening_results WHERE id=?',(int(ident),)).fetchone()
    if not row:raise ValueError('Hasil penyaringan tidak ditemukan.')
    data=dict(row);data.pop('normalized',None);data.pop('prefix',None);data.update(source(conn,data))
    data['ai_result']=json.loads(data.pop('ai_json')) if data['ai_json'] else None
    data['related']=[dict(r) for r in conn.execute('''SELECT r.id,r.kind,r.source_ref,r.source_url,r.published_at,l.rationale
      FROM screening_related l JOIN screening_results r ON r.id=CASE WHEN l.result_id=? THEN l.related_id ELSE l.result_id END
      WHERE l.result_id=? OR l.related_id=? ORDER BY r.id DESC LIMIT 15''',(data['id'],data['id'],data['id']))]
    return data

def review(conn,ident):
    item=detail(conn,ident)
    if item['source_changed']:raise ValueError('Sumber berubah; jalankan penyaringan ulang sebelum review.')
    conn.execute("UPDATE screening_results SET review_status='reviewed' WHERE id=?",(item['id'],));conn.execute("INSERT INTO screening_audit(result_id,action,created_at) VALUES(?,'reviewed',?)",(item['id'],now()));conn.commit();return {'id':item['id'],'review_status':'reviewed'}


def ai_review(conn,ident,payload,provider_module):
    item=detail(conn,ident)
    if item['source_changed']:raise ValueError('Sumber berubah; jalankan penyaringan ulang.')
    request={'provider':payload.get('provider'),'model':payload.get('model'),'task':'relevance',
             'text':item['text'][:2500],'source_url':item['source_url'] if factcheck.public_evidence_url(item['source_url']) else '',
             'fallback':payload.get('fallback',False),'use_cache':payload.get('use_cache',False)}
    try:
        output=provider_module.analyze(conn,request)
    except (ValueError,provider_module.ProviderError):
        conn.execute("UPDATE screening_results SET ai_status='failed',decision='uncertain' WHERE id=?",(item['id'],))
        conn.execute("INSERT INTO screening_audit(result_id,action,created_at) VALUES(?,'ai_failed',?)",(item['id'],now()));conn.commit();raise
    if detail(conn,ident)['source_changed']:raise ValueError('Sumber berubah ketika analisis berjalan; hasil tidak disimpan.')
    conn.execute("UPDATE screening_results SET ai_status='needs_review',ai_json=? WHERE id=?",(json.dumps(output,ensure_ascii=False),item['id']))
    conn.execute("INSERT INTO screening_audit(result_id,action,created_at) VALUES(?,'ai_analyzed',?)",(item['id'],now()));conn.commit()
    return output

def ai_batch(conn,run_id,ids,payload,provider_module):
    run=get_run(conn,run_id)
    if not isinstance(ids,list) or not 1<=len(ids)<=5 or any(type(x) is not int or x<1 for x in ids) or len(set(ids))!=len(ids):
        raise ValueError('Pilih 1–5 hasil berbeda untuk analisis AI.')
    for ident in ids:
        row=conn.execute('SELECT run_id,decision FROM screening_results WHERE id=?',(ident,)).fetchone()
        if not row or row['run_id']!=run['id'] or row['decision']!='relevant':
            raise ValueError('Semua hasil harus relevan dan berasal dari pekerjaan yang sama.')
    done=[]
    for ident in ids:
        try:
            result=ai_review(conn,ident,{**payload,'use_cache':True},provider_module)
            done.append({'id':ident,'provider':result['provider'],'model':result['model'],'cache_hit':result.get('cache_hit',False)})
        except (ValueError,provider_module.ProviderError) as exc:
            return {'completed':done,'failed_id':ident,'error':str(exc),'remaining':len(ids)-len(done)-1}
    return {'completed':done,'failed_id':None,'remaining':0}

"""User-provided simulations, stored only in a separate SQLite database."""
import csv
import io
import json
import sqlite3
import uuid
from datetime import datetime

import ai_providers
import collection_jobs
import comment_analysis
import screening

LABEL = 'SIMULASI — BUKAN DATA NYATA'
MAX_TEXT = 200000
MAX_ROWS = 1000


def parse(text, fmt):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('Input simulasi maksimal 200.000 karakter.')
    if fmt == 'lines':
        rows = [{'comment': line} for line in text.splitlines() if line.strip()]
    elif fmt == 'csv':
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')), strict=True)
            if not reader.fieldnames:
                raise ValueError('CSV memerlukan header comment.')
            names = [x.strip().lower() for x in reader.fieldnames]
            if len(set(names)) != len(names) or 'comment' not in names or any(x not in ('username','comment','published_at','parent_id') for x in names):
                raise ValueError('Header CSV harus memuat comment dan hanya kolom yang didukung.')
            reader.fieldnames = names
            rows = list(reader)
        except csv.Error as exc:
            raise ValueError('CSV tidak valid: '+str(exc)) from None
    else:
        raise ValueError('Format input tidak dikenal.')
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError('Masukkan 1–1.000 komentar yang valid.')
    clean=[]
    for i,row in enumerate(rows,1):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Baris {i}: jumlah kolom CSV tidak cocok.')
        text_value=row.get('comment','').strip()
        author=row.get('username','').strip()
        parent=row.get('parent_id','').strip()
        stamp=row.get('published_at','').strip()
        if not text_value or len(text_value)>10000 or len(author)>200 or len(parent)>200:
            raise ValueError(f'Baris {i}: komentar wajib diisi dan panjang kolom harus sesuai batas.')
        if stamp:
            try:
                parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError()
            except ValueError:
                raise ValueError(f'Baris {i}: published_at harus ISO 8601 dengan zona waktu.') from None
        clean.append({'comment':text_value,'username':author,'published_at':stamp,'parent_id':parent})
    return clean


def init(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS simulation_settings(analysis_id INTEGER PRIMARY KEY, ai_enabled INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS simulation_sentiment(analysis_id INTEGER NOT NULL, source_ref TEXT NOT NULL, sentiment TEXT NOT NULL, PRIMARY KEY(analysis_id,source_ref))')
    conn.commit()


def create(conn, rows, provider='muse', model='', ai_enabled=False):
    if type(ai_enabled) is not bool:
        raise ValueError('Pengaturan AI tidak valid.')
    init(conn)
    batch_id='sim-'+uuid.uuid4().hex
    job=comment_analysis.create(conn,batch_id,provider,model,False)
    collection=collection_jobs.new(conn,batch_id,20,'youtube')
    stamp=comment_analysis.now()
    try:
        conn.execute('BEGIN IMMEDIATE')
        for row in rows:
            cid='sim-'+uuid.uuid4().hex
            conn.execute("INSERT INTO comments(comment_id,video_id,author_id,author_name,body,published_at,source_url,dataset,collected_at) VALUES(?,?,NULL,?,?,?,'','demo',?)",
                         (cid,batch_id,row['username'],row['comment'],row['published_at'],stamp))
            conn.execute("INSERT INTO collection_job_items(job_id,kind,source_ref,observed_at) VALUES(?,'comment',?,?)",(collection['id'],cid,stamp))
            if row['parent_id']:
                conn.execute('INSERT INTO comment_analysis_reply_links(analysis_id,comment_id,parent_id) VALUES(?,?,?)',(job['id'],cid,row['parent_id']))
        conn.execute("UPDATE collection_jobs SET status='completed',retrieved_count=?,unique_count=?,updated_at=? WHERE id=?",(len(rows),len(rows),stamp,collection['id']))
        conn.execute('UPDATE comment_analysis_jobs SET collection_job_id=? WHERE id=?',(collection['id'],job['id']))
        conn.execute('INSERT INTO simulation_settings VALUES(?,?)',(job['id'],int(ai_enabled)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return job['id']


def run(db_path, ident, analyzer=ai_providers.analyze_source):
    conn=sqlite3.connect(str(db_path),timeout=10)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        job=comment_analysis.get(conn,ident)
        enabled=bool(conn.execute('SELECT ai_enabled FROM simulation_settings WHERE analysis_id=?',(ident,)).fetchone()[0])
        comment_analysis._set(conn,ident,'PROCESSING','Mengelompokkan dan menganalisis pola simulasi')
        run=screening.get_run(conn,job['screening_run_id']) if job['screening_run_id'] else screening.start(conn,'','comments',job['collection_job_id'])
        if not job['screening_run_id']:
            conn.execute('UPDATE comment_analysis_jobs SET screening_run_id=? WHERE id=?',(run['id'],ident));conn.commit()
        while screening.run_batch(conn,run['id'],150,dataset='demo')['status']!='completed':pass
        job=comment_analysis.get(conn,ident)
        comment_analysis._behavior(conn,job,run['id'],min_repeat_length=1)
        summary=comment_analysis.get(conn,ident)['behavioral']
        summary['authors_supplied']=conn.execute('''SELECT count(*) FROM collection_job_items i JOIN comments c ON c.comment_id=i.source_ref
            WHERE i.job_id=? AND length(trim(c.author_name))>0''',(job['collection_job_id'],)).fetchone()[0]
        conn.execute('UPDATE comment_analysis_jobs SET behavioral_json=? WHERE id=?',(json.dumps(summary,ensure_ascii=False),ident));conn.commit()
        if enabled:
            comment_analysis._set(conn,ident,'ANALYZING','Menganalisis simulasi dengan AI')
            def with_sentiment(c,source,provider,model,**kwargs):
                result=analyzer(c,source,provider,model,**kwargs)
                for verdict in result['verdicts']:
                    row=c.execute('SELECT source_ref FROM screening_results WHERE id=? AND run_id=?',(verdict['ref'],run['id'])).fetchone()
                    if row:c.execute('INSERT OR REPLACE INTO simulation_sentiment VALUES(?,?,?)',(ident,row[0],verdict.get('sentiment','unclear')))
                c.commit()
                return result
            ok,reason=comment_analysis._ai(conn,job,run['id'],with_sentiment)
            comment_analysis._set(conn,ident,'COMPLETED' if ok else 'PARTIAL','Analisis simulasi selesai' if ok else 'Analisis simulasi parsial',reason)
        else:
            comment_analysis._set(conn,ident,'COMPLETED','Analisis pola selesai; AI tidak diaktifkan')
    except Exception:
        conn.rollback()
        comment_analysis._set(conn,ident,'FAILED','Simulasi gagal','Pemrosesan simulasi gagal; periksa input dan konfigurasi.')
    finally:
        conn.close()


def result(conn, ident, page=1, category='all', ai_page=1):
    data=comment_analysis.findings(conn,ident,page,category)
    try: ai_page=int(ai_page)
    except (ValueError,TypeError,OverflowError): raise ValueError('Halaman AI tidak valid.') from None
    if not 1<=ai_page<=100000:raise ValueError('Halaman AI tidak valid.')
    data['simulation_label']=LABEL
    data['ai_enabled']=bool(conn.execute('SELECT ai_enabled FROM simulation_settings WHERE analysis_id=?',(int(ident),)).fetchone()[0])
    data['ai_page']=ai_page
    data['ai_total']=conn.execute('SELECT count(*) FROM comment_analysis_ai_results WHERE analysis_id=?',(int(ident),)).fetchone()[0]
    data['ai_results']=[dict(row) for row in conn.execute('''SELECT a.source_ref,a.kind,a.quote,a.claim,a.explanation,a.provider,a.model,a.created_at,a.cache_hit,substr(c.body,1,1500) AS body,c.author_name,
        COALESCE(s.sentiment,'unclear') AS sentiment FROM comment_analysis_ai_results a
        JOIN comments c ON c.comment_id=a.source_ref
        LEFT JOIN simulation_sentiment s ON s.analysis_id=a.analysis_id AND s.source_ref=a.source_ref
        WHERE a.analysis_id=? ORDER BY a.id LIMIT 25 OFFSET ?''',(int(ident),(ai_page-1)*25))]
    return data


def clear(conn):
    # Only called with the explicitly selected simulation connection, never the real DB.
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            name=row[0].replace('"','""')
            conn.execute('DELETE FROM "'+name+'"')
        conn.commit()
    except Exception:
        conn.rollback();raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')

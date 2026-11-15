"""Resumable, bounded page-by-page YouTube collection jobs."""
from __future__ import annotations
import hashlib, json, sqlite3, time
from datetime import datetime, timezone

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS collection_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_id TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('queued','running','paused','completed','rate_limited','failed','cancelled')),
      cursor TEXT, pages_per_run INTEGER NOT NULL, retrieved_count INTEGER NOT NULL DEFAULT 0,
      unique_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
      error_count INTEGER NOT NULL DEFAULT 0, pages_count INTEGER NOT NULL DEFAULT 0,
      end_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS collection_job_cursors(
      job_id INTEGER NOT NULL REFERENCES collection_jobs(id), cursor TEXT NOT NULL,
      PRIMARY KEY(job_id,cursor)
    );
    CREATE INDEX IF NOT EXISTS idx_collection_jobs_updated ON collection_jobs(updated_at DESC);
    CREATE TABLE IF NOT EXISTS collection_job_items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES collection_jobs(id),
      kind TEXT NOT NULL CHECK(kind IN ('comment','observation')), source_ref TEXT NOT NULL,
      observed_at TEXT NOT NULL, UNIQUE(job_id,kind,source_ref));
    CREATE INDEX IF NOT EXISTS idx_collection_job_items_job ON collection_job_items(job_id,id);
    CREATE TABLE IF NOT EXISTS collection_job_rejections(
      job_id INTEGER NOT NULL REFERENCES collection_jobs(id), page_hash TEXT NOT NULL,
      item_index INTEGER NOT NULL, item_hash TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(job_id,page_hash,item_index,item_hash));
    ''')

def new(conn,video_id,pages_per_run=20,source='youtube', *, commit=True):
    if source not in ('youtube','bluesky'): raise ValueError('Sumber tidak tersedia.')
    if isinstance(pages_per_run,bool): raise ValueError('Batas halaman tidak valid.')
    try: pages_per_run=int(pages_per_run)
    except (TypeError, ValueError, OverflowError) as exc: raise ValueError('Batas halaman tidak valid.') from exc
    if not 1 <= pages_per_run <= 1000: raise ValueError('Batas halaman harus 1-1000 per sesi.')
    stamp=now();job=conn.execute("INSERT INTO collection_jobs(source,source_id,status,pages_per_run,created_at,updated_at) VALUES(?,?,'queued',?,?,?)",(source,video_id,pages_per_run,stamp,stamp)).lastrowid
    if commit: conn.commit()
    return get(conn,job)

def get(conn,ident):
    try:
        if isinstance(ident,bool): raise ValueError
        ident=int(ident)
    except (TypeError, ValueError, OverflowError) as exc: raise ValueError('ID pekerjaan tidak valid.') from exc
    row=conn.execute('SELECT * FROM collection_jobs WHERE id=?',(ident,)).fetchone()
    if not row: raise ValueError('Pekerjaan tidak ditemukan.')
    return dict(row)

def listing(conn):
    return [dict(row) for row in conn.execute('SELECT * FROM collection_jobs ORDER BY id DESC LIMIT 100')]

def control(conn,ident,action):
    job=get(conn,ident)
    allowed={'pause':(('queued','running'),'paused'),'cancel':(('queued','running','paused','rate_limited','failed'),'cancelled'),'resume':(('paused','rate_limited','failed'),'queued')}
    if action not in allowed or job['status'] not in allowed[action][0]: raise ValueError('Tindakan tidak sesuai status pekerjaan.')
    newstatus=allowed[action][1]
    cur=conn.execute('UPDATE collection_jobs SET status=?,end_reason=?,updated_at=? WHERE id=? AND status=?',(newstatus,{'pause':'Dijeda operator','cancel':'Dihentikan operator','resume':''}[action],now(),job['id'],job['status']))
    if not cur.rowcount: raise ValueError('Status pekerjaan berubah; perbarui tampilan.')
    conn.commit();return get(conn,ident)

def recover(conn):
    conn.execute("UPDATE collection_jobs SET status='paused',end_reason='Server berhenti; lanjutkan secara manual',updated_at=? WHERE status IN ('running','queued')",(now(),));conn.commit()

def run(db_path,ident,api_key,fetch_page,store_item, *, sleep=time.sleep, strict=False):
    conn=sqlite3.connect(str(db_path),timeout=10);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA busy_timeout=10000')
    try:
        conn.execute('BEGIN IMMEDIATE')
        job=get(conn,ident)
        if job['status']!='queued': conn.rollback();return
        conn.execute("UPDATE collection_jobs SET status='running',updated_at=? WHERE id=?",(now(),ident));conn.commit()
        for _ in range(job['pages_per_run']):
            job=get(conn,ident)
            if job['status']!='running': return
            page=None
            for attempt in range(3):
                try:
                    page=fetch_page(api_key,job['source_id'],job['cursor']);break
                except Exception as error:
                    reason=str(error).lower()
                    if 'commentsdisabled' in reason:
                        conn.execute("UPDATE collection_jobs SET status='failed',error_count=error_count+1,end_reason='Komentar dinonaktifkan pada video ini.',updated_at=? WHERE id=? AND status='running'",(now(),ident));conn.commit();return
                    if 'batas permintaan sesi' in reason:
                        conn.execute("UPDATE collection_jobs SET status='paused',end_reason='Batas permintaan sesi tercapai; lanjutkan secara manual.',updated_at=? WHERE id=? AND status='running'",(now(),ident));conn.commit();return
                    rate=('429' in reason or 'quota' in reason or 'rate limit' in reason)
                    if rate or attempt==2:
                        status='rate_limited' if rate else 'failed'
                        message='Batas layanan tercapai; lanjutkan setelah kuota tersedia.' if rate else 'Pengambilan gagal; periksa jaringan dan izin API.'
                        conn.execute("UPDATE collection_jobs SET status=?,error_count=error_count+1,end_reason=?,updated_at=? WHERE id=? AND status='running'",(status,message,now(),ident));conn.commit();return
                    sleep(0.25*(2**attempt))
                    if get(conn,ident)['status']!='running':return
            if not isinstance(page,dict) or not isinstance(page.get('items'),list) or len(page['items']) > 100:
                conn.execute("UPDATE collection_jobs SET status='failed',error_count=error_count+1,end_reason=?,updated_at=? WHERE id=? AND status='running'",('Respons sumber tidak valid.',now(),ident));conn.commit();return
            token=page.get('nextPageToken')
            if token is not None and (not isinstance(token,str) or len(token)>1000 or not token):
                conn.execute("UPDATE collection_jobs SET status='failed',error_count=error_count+1,end_reason=?,updated_at=? WHERE id=? AND status='running'",('Cursor sumber tidak valid.',now(),ident));conn.commit();return
            conn.execute('BEGIN IMMEDIATE')
            current=get(conn,ident)
            if current['status']!='running': conn.rollback();return
            linked=conn.execute('''SELECT v.relevance_status,v.source_status FROM yt_radar_collection_jobs l
                JOIN yt_radar_videos v ON v.video_id=l.video_id WHERE l.job_id=?''',(ident,)).fetchone()
            if linked and (linked['relevance_status']!='RELEVANT' or linked['source_status']!='available'):
                conn.execute("UPDATE collection_jobs SET status='paused',end_reason='Status video berubah; tinjau kembali sebelum melanjutkan.',updated_at=? WHERE id=?",(now(),ident));conn.commit();return
            if token and (token==current['cursor'] or conn.execute('SELECT 1 FROM collection_job_cursors WHERE job_id=? AND cursor=?',(ident,token)).fetchone()):
                conn.execute("UPDATE collection_jobs SET status='failed',error_count=error_count+1,end_reason=?,updated_at=? WHERE id=?",('Cursor berulang; pemeriksaan diperlukan.',now(),ident));conn.commit();return
            retrieved=unique=duplicates=invalid=0
            reply_option=conn.execute('SELECT include_replies FROM yt_radar_collection_jobs WHERE job_id=?',(ident,)).fetchone() if current['source']=='youtube' else None
            def reject(item,index,reason):
                nonlocal invalid
                invalid+=1
                if strict:
                    page_hash=hashlib.sha256(('first:' if current['cursor'] is None else 'page:'+current['cursor']).encode()).hexdigest()
                    item_hash=hashlib.sha256(json.dumps(item,ensure_ascii=True,sort_keys=True,default=str).encode()).hexdigest()
                    conn.execute('INSERT OR IGNORE INTO collection_job_rejections VALUES(?,?,?,?,?,?)',(ident,page_hash,index,item_hash,reason,now()))
            for index,item in enumerate(page['items']):
                try:
                    if current['source']=='youtube':
                        cid=item['snippet']['topLevelComment']['id']
                        sn=item['snippet']['topLevelComment']['snippet']
                        if not isinstance(cid,str) or not cid or len(cid)>200 or not isinstance(sn,dict):raise ValueError('missing_id_or_snippet')
                        body=sn.get('textDisplay')
                        if not isinstance(body,str) or not body.strip():raise ValueError('missing_or_empty_text')
                        if sn.get('publishedAt') is not None and not isinstance(sn.get('publishedAt'),str):raise ValueError('invalid_timestamp')
                        if sn.get('authorDisplayName') is not None and not isinstance(sn.get('authorDisplayName'),str):raise ValueError('invalid_author')
                        author=sn.get('authorChannelId')
                        if author is not None and (not isinstance(author,dict) or (author.get('value') is not None and not isinstance(author.get('value'),str))):raise ValueError('invalid_author')
                        exists=bool(conn.execute('SELECT 1 FROM comments WHERE comment_id=?',(str(cid),)).fetchone())
                    else:
                        cid=item['uri']
                        exists=bool(conn.execute("SELECT 1 FROM intel_observations WHERE platform='bluesky' AND source_id=?",(str(cid),)).fetchone())
                except (KeyError,TypeError,ValueError) as exc:
                    reject(item,index,str(exc) if isinstance(exc,ValueError) and str(exc) in ('missing_id_or_snippet','missing_or_empty_text','invalid_timestamp','invalid_author') else 'missing_id_or_snippet');continue
                if store_item(conn,current['source_id'],item):
                    if current['source']=='youtube':
                        kind, source_ref = 'comment', str(cid)
                    else:
                        found=conn.execute("SELECT id FROM intel_observations WHERE platform='bluesky' AND source_id=?",(str(cid),)).fetchone()
                        if not found: raise ValueError('Observasi tersimpan tidak ditemukan.')
                        kind, source_ref = 'observation', str(found['id'])
                    conn.execute('INSERT OR IGNORE INTO collection_job_items(job_id,kind,source_ref,observed_at) VALUES(?,?,?,?)',
                                 (ident,kind,source_ref,now()))
                    if reply_option and reply_option['include_replies']:
                        reply_count=item.get('snippet',{}).get('totalReplyCount',0)
                        if type(reply_count) is int and reply_count>0:
                            conn.execute('INSERT OR IGNORE INTO yt_radar_reply_jobs(job_id,video_id,parent_id,updated_at) VALUES(?,?,?,?)',
                                         (ident,current['source_id'],str(cid),now()))
                    retrieved+=1
                    if exists: duplicates+=1
                    else: unique+=1
                else:
                    reject(item,index,'item_not_stored')
            if token:conn.execute('INSERT INTO collection_job_cursors(job_id,cursor) VALUES(?,?)',(ident,token))
            status='running' if token else 'completed'
            reason='' if status=='running' else 'Sumber tidak memiliki halaman lanjutan.'
            conn.execute('''UPDATE collection_jobs SET cursor=?,status=?,end_reason=?,pages_count=pages_count+1,
                retrieved_count=retrieved_count+?,unique_count=unique_count+?,duplicate_count=duplicate_count+?,error_count=error_count+?,updated_at=? WHERE id=?''',
                (token,status,reason,retrieved,unique,duplicates,invalid,now(),ident));conn.commit()
            if status=='completed': return
        conn.execute("UPDATE collection_jobs SET status='paused',end_reason='Batas halaman sesi tercapai; lanjutkan bila diperlukan.',updated_at=? WHERE id=? AND status='running'",(now(),ident));conn.commit()
    except Exception as exc:
        conn.rollback()
        message=str(exc) if strict and isinstance(exc,ValueError) and str(exc)=='Item komentar tidak valid; halaman belum disimpan.' else 'Kesalahan penyimpanan; data halaman sebelumnya tetap ada.'
        conn.execute("UPDATE collection_jobs SET status='failed',error_count=error_count+1,end_reason=?,updated_at=? WHERE id=? AND status='running'",(message,now(),ident));conn.commit()
    finally: conn.close()

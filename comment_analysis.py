"""Persisted user-requested YouTube comment analysis; no autonomous discovery."""
from __future__ import annotations
import hashlib, json, re, sqlite3, time, urllib.parse
from datetime import datetime, timezone, timedelta
import collection_jobs, screening, research_analysis, ai_providers, investigation, youtube_radar

def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS comment_analysis_videos(
      video_id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,channel_title TEXT NOT NULL,
      published_at TEXT NOT NULL,thumbnail_url TEXT NOT NULL,comment_count TEXT NOT NULL,fetched_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS comment_analysis_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,video_id TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,
      provider TEXT NOT NULL,model TEXT NOT NULL,fallback INTEGER NOT NULL,include_replies INTEGER NOT NULL,
      max_requests INTEGER NOT NULL,collection_job_id INTEGER,screening_run_id INTEGER,
      behavioral_json TEXT NOT NULL DEFAULT '',ai_failed INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',started_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,completed_at TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS comment_analysis_api_calls(
      id INTEGER PRIMARY KEY AUTOINCREMENT,analysis_id INTEGER NOT NULL REFERENCES comment_analysis_jobs(id),
      kind TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS comment_analysis_replies(
      id INTEGER PRIMARY KEY AUTOINCREMENT,analysis_id INTEGER NOT NULL REFERENCES comment_analysis_jobs(id),
      parent_id TEXT NOT NULL,cursor TEXT,status TEXT NOT NULL DEFAULT 'queued',pages INTEGER NOT NULL DEFAULT 0,
      received INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',UNIQUE(analysis_id,parent_id));
    CREATE TABLE IF NOT EXISTS comment_analysis_reply_cursors(
      reply_id INTEGER NOT NULL REFERENCES comment_analysis_replies(id),cursor TEXT NOT NULL,PRIMARY KEY(reply_id,cursor));
    CREATE TABLE IF NOT EXISTS comment_analysis_reply_links(
      analysis_id INTEGER NOT NULL REFERENCES comment_analysis_jobs(id),comment_id TEXT NOT NULL REFERENCES comments(comment_id),
      parent_id TEXT NOT NULL,PRIMARY KEY(analysis_id,comment_id));
    CREATE TABLE IF NOT EXISTS comment_analysis_ai_results(
      id INTEGER PRIMARY KEY AUTOINCREMENT,analysis_id INTEGER NOT NULL REFERENCES comment_analysis_jobs(id),
      source_ref TEXT NOT NULL,content_sha256 TEXT NOT NULL,kind TEXT NOT NULL,quote TEXT NOT NULL,claim TEXT NOT NULL,
      explanation TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,created_at TEXT NOT NULL,
      UNIQUE(analysis_id,source_ref,content_sha256));
    CREATE TABLE IF NOT EXISTS comment_analysis_findings(
      id INTEGER PRIMARY KEY AUTOINCREMENT,analysis_id INTEGER NOT NULL REFERENCES comment_analysis_jobs(id),
      source_ref TEXT NOT NULL,category TEXT NOT NULL,kind TEXT NOT NULL,explanation TEXT NOT NULL,
      quote TEXT NOT NULL,claim TEXT NOT NULL,source_sha256 TEXT NOT NULL DEFAULT '',evidence_count INTEGER NOT NULL DEFAULT 1,
      group_key TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(analysis_id,source_ref,category,group_key,source_sha256));
    CREATE TABLE IF NOT EXISTS comment_analysis_cases(
      finding_id INTEGER PRIMARY KEY REFERENCES comment_analysis_findings(id),case_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id));
    CREATE TABLE IF NOT EXISTS comment_analysis_finding_evidence(
      finding_id INTEGER NOT NULL REFERENCES comment_analysis_findings(id),source_ref TEXT NOT NULL REFERENCES comments(comment_id),
      PRIMARY KEY(finding_id,source_ref));
    CREATE TABLE IF NOT EXISTS comment_analysis_urls(
      run_id INTEGER NOT NULL REFERENCES screening_runs(id),source_ref TEXT NOT NULL REFERENCES comments(comment_id),
      url_hash TEXT NOT NULL,url_text TEXT NOT NULL,PRIMARY KEY(run_id,source_ref,url_hash));
    CREATE INDEX IF NOT EXISTS idx_comment_analysis_urls ON comment_analysis_urls(run_id,url_hash);
    CREATE INDEX IF NOT EXISTS idx_comment_analysis_findings ON comment_analysis_findings(analysis_id,category,id);
    ''')
    cols={r[1] for r in conn.execute('PRAGMA table_info(comment_analysis_jobs)')}
    if 'ai_failed' not in cols:conn.execute('ALTER TABLE comment_analysis_jobs ADD COLUMN ai_failed INTEGER NOT NULL DEFAULT 0')
    if 'video_snapshot' not in cols:conn.execute("ALTER TABLE comment_analysis_jobs ADD COLUMN video_snapshot TEXT NOT NULL DEFAULT ''")
    if 'ai_coverage_json' not in cols:conn.execute("ALTER TABLE comment_analysis_jobs ADD COLUMN ai_coverage_json TEXT NOT NULL DEFAULT ''")
    ai_cols={r[1] for r in conn.execute('PRAGMA table_info(comment_analysis_ai_results)')}
    if 'cache_hit' not in ai_cols:conn.execute('ALTER TABLE comment_analysis_ai_results ADD COLUMN cache_hit INTEGER')
    conn.commit()

def create(conn,video_id,provider,model='',include_replies=True,max_requests=200):
    if provider=='meta':provider='muse'
    if provider=='gemini':raise ValueError('Penyedia Gemini sudah dihapus; gunakan Muse.')
    if provider!='muse':raise ValueError('Pilih penyedia AI.')
    if model is not None and (not isinstance(model,str) or len(model)>100 or (model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}',model))):raise ValueError('Model tidak valid.')
    if type(include_replies) is not bool:raise ValueError('Pengaturan tidak valid.')
    if type(max_requests) is not int or not 1<=max_requests<=1000:raise ValueError('Batas permintaan harus 1–1000 per sesi.')
    stamp=now();ident=conn.execute('''INSERT INTO comment_analysis_jobs(video_id,status,stage,provider,model,fallback,include_replies,max_requests,started_at,updated_at)
      VALUES(?,'QUEUED','Menunggu pekerja',?,?,0,?,?,?,?)''',(video_id,provider,model or '',int(include_replies),max_requests,stamp,stamp)).lastrowid
    conn.commit();return get(conn,ident)

def get(conn,ident):
    ident=investigation._id(ident)
    row=conn.execute('SELECT * FROM comment_analysis_jobs WHERE id=?',(ident,)).fetchone()
    if not row:raise ValueError('Analisis tidak ditemukan.')
    data=dict(row)
    snapshot=data.pop('video_snapshot')
    data['video']=json.loads(snapshot) if snapshot else (dict(v) if (v:=conn.execute('SELECT * FROM comment_analysis_videos WHERE video_id=?',(data['video_id'],)).fetchone()) else None)
    cid=data['collection_job_id'];sid=data['screening_run_id']
    data['collection']=collection_jobs.get(conn,cid) if cid else None
    data['collected']=conn.execute('SELECT count(*) FROM collection_job_items WHERE job_id=? AND kind=\'comment\'',(cid,)).fetchone()[0] if cid else 0
    data['screened']=conn.execute('SELECT count(*) FROM screening_results WHERE run_id=?',(sid,)).fetchone()[0] if sid else 0
    data['ai_analyzed']=conn.execute('''SELECT count(*) FROM screening_results s JOIN comment_analysis_ai_results a
      ON a.analysis_id=? AND a.source_ref=s.source_ref AND a.content_sha256=s.content_sha256 WHERE s.run_id=?''',(ident,sid)).fetchone()[0] if sid else 0
    # Items waiting for screening are also waiting for AI.  Counting only the
    # screened snapshot makes an in-progress collection look more complete.
    data['ai_pending']=max(0,data['collected']-data['ai_analyzed'])
    data['replies_received']=conn.execute('SELECT count(*) FROM comment_analysis_reply_links WHERE analysis_id=?',(ident,)).fetchone()[0]
    data['quarantined']=conn.execute('SELECT count(*) FROM collection_job_rejections WHERE job_id=?',(cid,)).fetchone()[0] if cid else 0
    data['reply_pending']=conn.execute("SELECT count(*) FROM comment_analysis_replies WHERE analysis_id=? AND status!='completed'",(ident,)).fetchone()[0]
    data['api_calls']=conn.execute('SELECT count(*) FROM comment_analysis_api_calls WHERE analysis_id=?',(ident,)).fetchone()[0]
    data['behavioral']=json.loads(data.pop('behavioral_json') or '{}')
    data['ai_coverage']=json.loads(data.pop('ai_coverage_json') or '{}')
    return data

def listing(conn,video_id=None):
    if video_id is not None:
        if not isinstance(video_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):raise ValueError('Video ID tidak valid.')
        rows=conn.execute('SELECT id FROM comment_analysis_jobs WHERE video_id=? ORDER BY id DESC LIMIT 1',(video_id,))
    else:rows=conn.execute('SELECT id FROM comment_analysis_jobs ORDER BY id DESC LIMIT 30')
    return [get(conn,r['id']) for r in rows]

def _set(conn,ident,status,stage,error=''):
    conn.execute('UPDATE comment_analysis_jobs SET status=?,stage=?,error=?,updated_at=?,completed_at=CASE WHEN ? IN (\'COMPLETED\',\'PARTIAL\',\'FAILED\',\'CANCELLED\') THEN ? ELSE completed_at END WHERE id=? AND status!=\'CANCELLED\'',(status,stage,error[:500],now(),status,now(),ident));conn.commit()

def recover(conn):
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(timespec='seconds')
    conn.execute("UPDATE comment_analysis_jobs SET status='PARTIAL',stage='Terhenti; lanjutkan secara manual',error='Server berhenti; permintaan AI yang terputus mungkin telah dikenakan biaya.',updated_at=? WHERE status IN ('QUEUED','COLLECTING','PROCESSING','ANALYZING') AND updated_at<?",(now(),cutoff))
    conn.commit()

def cancel(conn,ident):
    job=get(conn,ident)
    if job['status'] in ('COMPLETED','CANCELLED'):raise ValueError('Analisis sudah berakhir.')
    conn.execute("UPDATE comment_analysis_jobs SET status='CANCELLED',stage='Dihentikan operator',updated_at=?,completed_at=? WHERE id=?",(now(),now(),job['id']));conn.commit()
    if job['collection'] and job['collection']['status'] in ('queued','running','paused','rate_limited','failed'):
        try:collection_jobs.control(conn,job['collection_job_id'],'cancel')
        except ValueError:pass
    return {'id':job['id'],'status':'CANCELLED'}

def resume(conn,ident,provider=None,model=None):
    job=get(conn,ident)
    if job['status'] not in ('PARTIAL','FAILED'):raise ValueError('Analisis tidak dapat dilanjutkan.')
    provider=provider or job['provider'];model=job['model'] if model is None else model
    if provider=='meta':provider='muse'
    if provider=='gemini' or (model or '').startswith('gemini-'):raise ValueError('Analisis ini memakai Gemini yang sudah dihapus; mulai analisis baru dengan Muse.')
    if provider!='muse' or not isinstance(model,str) or len(model)>100 or (model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}',model)):raise ValueError('Pengaturan AI tidak valid.')
    conn.execute("UPDATE comment_analysis_jobs SET status='QUEUED',stage='Menunggu pekerja',provider=?,model=?,fallback=0,error='',updated_at=? WHERE id=?",(provider,model,now(),job['id']));conn.commit();return get(conn,ident)

def _reserve(conn,ident,kind,start_count,limit):
    total=conn.execute('SELECT count(*) FROM comment_analysis_api_calls WHERE analysis_id=?',(ident,)).fetchone()[0]
    if total-start_count>=limit:raise RuntimeError('quota: batas permintaan sesi tercapai; lanjutkan secara manual.')
    conn.execute('INSERT INTO comment_analysis_api_calls(analysis_id,kind,created_at) VALUES(?,?,?)',(ident,kind,now()))
    conn.execute('UPDATE comment_analysis_jobs SET updated_at=? WHERE id=?',(now(),ident));conn.commit()

def _metadata(conn,ident,video_id,key,fetch_api,start_count,limit):
    _reserve(conn,ident,'videos.list',start_count,limit)
    data=fetch_api('videos',{'part':'snippet,statistics','id':video_id,'maxResults':1},key)
    items=data.get('items') if isinstance(data,dict) else None
    if not isinstance(items,list) or len(items)!=1 or items[0].get('id')!=video_id:raise ValueError('Video tidak tersedia atau tidak dapat diakses YouTube API.')
    snippet=items[0].get('snippet') or {};stats=items[0].get('statistics') or {}
    if not isinstance(snippet,dict) or not isinstance(snippet.get('title'),str) or not snippet['title'].strip():raise ValueError('Metadata video tidak lengkap.')
    thumb='';thumbs=snippet.get('thumbnails') or {}
    if isinstance(thumbs,dict):
        import re
        for name in ('high','medium','default'):
            entry=thumbs.get(name);value=entry.get('url') if isinstance(entry,dict) else None
            if isinstance(value,str) and re.fullmatch(r'https://i\.ytimg\.com/(?:vi|vi_webp)/'+re.escape(video_id)+r'/[A-Za-z0-9_.-]+',value):thumb=value;break
    conn.execute('''INSERT INTO comment_analysis_videos VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,
      description=excluded.description,channel_title=excluded.channel_title,published_at=excluded.published_at,thumbnail_url=excluded.thumbnail_url,
      comment_count=excluded.comment_count,fetched_at=excluded.fetched_at''',(video_id,snippet['title'][:500],str(snippet.get('description') or '')[:10000],str(snippet.get('channelTitle') or '')[:200],str(snippet.get('publishedAt') or '')[:80],thumb,str(stats.get('commentCount') or '')[:30],now()));conn.commit()

def _reply_pages(conn,job,key,fetch_api,store_item,start_count):
    while True:
        if get(conn,job['id'])['status']=='CANCELLED':return False,'Dibatalkan.'
        row=conn.execute("SELECT * FROM comment_analysis_replies WHERE analysis_id=? AND status IN ('queued','failed') ORDER BY id LIMIT 1",(job['id'],)).fetchone()
        if not row:return True,''
        try:
            _reserve(conn,job['id'],'comments.list',start_count,job['max_requests'])
            params={'part':'snippet','parentId':row['parent_id'],'maxResults':100,'textFormat':'plainText'}
            if row['cursor']:params['pageToken']=row['cursor']
            data=fetch_api('comments',params,key);items=data.get('items');token=data.get('nextPageToken')
            if not isinstance(items,list) or len(items)>100 or (token is not None and (not isinstance(token,str) or not token or len(token)>1000)):raise ValueError('Respons balasan tidak valid.')
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT status FROM comment_analysis_jobs WHERE id=?',(job['id'],)).fetchone()[0]=='CANCELLED':conn.rollback();return False,'Dibatalkan.'
            if token and (token==row['cursor'] or conn.execute('SELECT 1 FROM comment_analysis_reply_cursors WHERE reply_id=? AND cursor=?',(row['id'],token)).fetchone()):raise ValueError('Cursor balasan berulang.')
            count=0
            for index,item in enumerate(items):
                sn=item.get('snippet') if isinstance(item,dict) else None
                cid=item.get('id') if isinstance(item,dict) else None
                body=sn.get('textDisplay') if isinstance(sn,dict) else None
                reason=''
                if not isinstance(cid,str) or not cid or len(cid)>200 or not isinstance(sn,dict):reason='reply_missing_id_or_snippet'
                elif sn.get('parentId')!=row['parent_id']:reason='reply_parent_mismatch'
                elif not isinstance(body,str) or not body.strip():reason='reply_missing_or_empty_text'
                elif sn.get('publishedAt') is not None and not isinstance(sn.get('publishedAt'),str):reason='reply_invalid_timestamp'
                elif sn.get('authorDisplayName') is not None and not isinstance(sn.get('authorDisplayName'),str):reason='reply_invalid_author'
                elif sn.get('authorChannelId') is not None and (not isinstance(sn.get('authorChannelId'),dict) or (sn['authorChannelId'].get('value') is not None and not isinstance(sn['authorChannelId'].get('value'),str))):reason='reply_invalid_author'
                if reason:
                    page_hash=hashlib.sha256(('reply:'+row['parent_id']+(':first' if row['cursor'] is None else ':page:'+row['cursor'])).encode()).hexdigest()
                    item_hash=hashlib.sha256(json.dumps(item,ensure_ascii=True,sort_keys=True,default=str).encode()).hexdigest()
                    conn.execute('INSERT OR IGNORE INTO collection_job_rejections VALUES(?,?,?,?,?,?)',(job['collection_job_id'],page_hash,index,item_hash,reason,now()))
                    continue
                if store_item(conn,job['video_id'],{'snippet':{'topLevelComment':item}}):
                    conn.execute("INSERT OR IGNORE INTO collection_job_items(job_id,kind,source_ref,observed_at) VALUES(?,'comment',?,?)",(job['collection_job_id'],cid,now()))
                    conn.execute('INSERT OR IGNORE INTO comment_analysis_reply_links(analysis_id,comment_id,parent_id) VALUES(?,?,?)',(job['id'],cid,row['parent_id']));count+=1
                else:raise ValueError('Balasan valid tidak dapat disimpan.')
            if token:conn.execute('INSERT INTO comment_analysis_reply_cursors(reply_id,cursor) VALUES(?,?)',(row['id'],token))
            conn.execute("UPDATE comment_analysis_replies SET cursor=?,status=?,pages=pages+1,received=received+?,error='' WHERE id=?",(token,'queued' if token else 'completed',count,row['id']));conn.commit()
        except Exception as exc:
            conn.rollback();message='Batas permintaan sesi tercapai.' if 'quota' in str(exc).lower() else 'Pengambilan balasan gagal; checkpoint dipertahankan.'
            conn.execute("UPDATE comment_analysis_replies SET status='failed',error=? WHERE id=?",(message,row['id']));conn.commit();return False,message

def _behavior(conn,job,run_id, *, min_repeat_length=30):
    rows=[dict(r) for r in conn.execute('''SELECT CASE WHEN c.dataset='demo' THEN 'manual' ELSE 'youtube' END AS platform,c.comment_id AS source_ref,c.body,c.published_at
      FROM screening_results s JOIN comments c ON c.comment_id=s.source_ref WHERE s.run_id=? AND s.kind='comment' ''',(run_id,))]
    summary=research_analysis.behavioral(rows)
    for row in rows:
        for value in set(re.findall(r'https?://[^\s<>"\']{1,2000}',row['body'],flags=re.I)[:20]):
            value=value.rstrip('.,;:!?)]}')
            try:
                parsed=urllib.parse.urlsplit(value)
                if parsed.scheme.lower() not in ('http','https') or not parsed.hostname:continue
                normalized=urllib.parse.urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),parsed.path,parsed.query,''))
            except ValueError:continue
            digest=hashlib.sha256(normalized.encode()).hexdigest()
            conn.execute('INSERT OR IGNORE INTO comment_analysis_urls(run_id,source_ref,url_hash,url_text) VALUES(?,?,?,?)',(run_id,row['source_ref'],digest,normalized))
    conn.execute('UPDATE comment_analysis_jobs SET behavioral_json=? WHERE id=?',(json.dumps(summary,ensure_ascii=False),job['id']))
    groups=conn.execute('''SELECT min(id) AS root,normalized,count(*) AS n FROM screening_results
      WHERE run_id=? AND length(normalized)>=? GROUP BY normalized HAVING count(*)>1 ORDER BY n DESC LIMIT 100''',(run_id,min_repeat_length)).fetchall()
    for group in groups:
        row=conn.execute('SELECT source_ref,content_sha256 FROM screening_results WHERE id=?',(group['root'],)).fetchone()
        conn.execute('''INSERT OR IGNORE INTO comment_analysis_findings(analysis_id,source_ref,category,kind,explanation,quote,claim,source_sha256,evidence_count,group_key,created_at)
          VALUES(?,?,'behavior','repeated_text',?,'','',?,?,?,?)''',(job['id'],row['source_ref'],'Teks ternormalisasi hingga 2.500 karakter berulang pada sampel; bukan bukti otomatisasi atau koordinasi.',row['content_sha256'],group['n'],hashlib.sha256(group['normalized'].encode()).hexdigest(),now()))
        key=hashlib.sha256(group['normalized'].encode()).hexdigest()
        fid=conn.execute("SELECT id FROM comment_analysis_findings WHERE analysis_id=? AND source_ref=? AND category='behavior' AND group_key=? AND source_sha256=?",(job['id'],row['source_ref'],key,row['content_sha256'])).fetchone()[0]
        conn.execute('''INSERT OR IGNORE INTO comment_analysis_finding_evidence(finding_id,source_ref)
          SELECT ?,source_ref FROM screening_results WHERE run_id=? AND normalized=?''',(fid,run_id,group['normalized']))
    similar=conn.execute('''SELECT root.id,root.source_ref,root.content_sha256,count(*) AS n
      FROM screening_results d JOIN screening_results root ON root.id=d.duplicate_of
      WHERE d.run_id=? AND d.normalized!=root.normalized GROUP BY root.id ORDER BY n DESC LIMIT 100''',(run_id,)).fetchall()
    for group in similar:
        group_key='near:'+str(group['id'])
        conn.execute('''INSERT OR IGNORE INTO comment_analysis_findings(analysis_id,source_ref,category,kind,explanation,quote,claim,source_sha256,evidence_count,group_key,created_at)
          VALUES(?,?,'behavior','similar_text',?,'','',?,?,?,?)''',(job['id'],group['source_ref'],'Teks mirip memerlukan review konteks; kemiripan tidak membuktikan kesamaan maksud atau koordinasi.',group['content_sha256'],group['n']+1,group_key,now()))
        fid=conn.execute("SELECT id FROM comment_analysis_findings WHERE analysis_id=? AND source_ref=? AND category='behavior' AND group_key=? AND source_sha256=?",(job['id'],group['source_ref'],group_key,group['content_sha256'])).fetchone()[0]
        conn.execute('INSERT OR IGNORE INTO comment_analysis_finding_evidence(finding_id,source_ref) VALUES(?,?)',(fid,group['source_ref']))
        conn.execute('INSERT OR IGNORE INTO comment_analysis_finding_evidence(finding_id,source_ref) SELECT ?,source_ref FROM screening_results WHERE run_id=? AND duplicate_of=? AND normalized!=?',(fid,run_id,group['id'],conn.execute('SELECT normalized FROM screening_results WHERE id=?',(group['id'],)).fetchone()[0]))
    urls=conn.execute('SELECT url_hash,min(url_text) AS url_text,min(source_ref) AS source_ref,count(*) AS n FROM comment_analysis_urls WHERE run_id=? GROUP BY url_hash HAVING count(*)>1 ORDER BY n DESC LIMIT 100',(run_id,)).fetchall()
    for group in urls:
        src=conn.execute('SELECT content_sha256 FROM screening_results WHERE run_id=? AND source_ref=?',(run_id,group['source_ref'])).fetchone()
        group_key='url:'+group['url_hash']
        conn.execute('''INSERT OR IGNORE INTO comment_analysis_findings(analysis_id,source_ref,category,kind,explanation,quote,claim,source_sha256,evidence_count,group_key,created_at)
          VALUES(?,?,'behavior','repeated_url',?,?,'',?,?,?,?)''',(job['id'],group['source_ref'],'URL yang sama berulang pada sampel; tidak membuktikan koordinasi atau isi URL.',group['url_text'][:1000],src['content_sha256'],group['n'],group_key,now()))
        fid=conn.execute("SELECT id FROM comment_analysis_findings WHERE analysis_id=? AND source_ref=? AND category='behavior' AND group_key=? AND source_sha256=?",(job['id'],group['source_ref'],group_key,src['content_sha256'])).fetchone()[0]
        conn.execute('INSERT OR IGNORE INTO comment_analysis_finding_evidence(finding_id,source_ref) SELECT ?,source_ref FROM comment_analysis_urls WHERE run_id=? AND url_hash=?',(fid,run_id,group['url_hash']))
    stamps=[]
    for row in rows:
        try:
            stamp=datetime.fromisoformat(row['published_at'].replace('Z','+00:00'))
            if stamp.tzinfo is not None:stamps.append((stamp.timestamp(),row['source_ref']))
        except (ValueError,TypeError,AttributeError):pass
    stamps.sort();left=0;best=(0,0,0)
    for right,(stamp,_) in enumerate(stamps):
        while stamp-stamps[left][0]>600:left+=1
        if right-left+1>best[0]:best=(right-left+1,left,right)
    summary['max_comments_10min']=best[0];summary['burst_threshold']=5
    if best[0]>=5:
        refs=[x[1] for x in stamps[best[1]:best[2]+1]];ref=refs[0]
        source=conn.execute('SELECT body FROM comments WHERE comment_id=?',(ref,)).fetchone();digest=hashlib.sha256(source['body'].encode()).hexdigest()
        start=datetime.fromtimestamp(stamps[best[1]][0],timezone.utc).isoformat(timespec='seconds')
        end=datetime.fromtimestamp(stamps[best[2]][0],timezone.utc).isoformat(timespec='seconds')
        reason=f'{best[0]} komentar teramati dalam jendela 10 menit ({start} sampai {end}); bukan bukti koordinasi atau bot.'
        conn.execute('''INSERT OR IGNORE INTO comment_analysis_findings(analysis_id,source_ref,category,kind,explanation,quote,claim,source_sha256,evidence_count,group_key,created_at)
          VALUES(?,?,'behavior','temporal_burst',?,'','',?,?,?,?)''',(job['id'],ref,reason,digest,best[0],'burst:'+start,now()))
        fid=conn.execute("SELECT id FROM comment_analysis_findings WHERE analysis_id=? AND source_ref=? AND category='behavior' AND group_key=? AND source_sha256=?",(job['id'],ref,'burst:'+start,digest)).fetchone()[0]
        conn.executemany('INSERT OR IGNORE INTO comment_analysis_finding_evidence(finding_id,source_ref) VALUES(?,?)',[(fid,ref) for ref in refs])
    conn.execute('UPDATE comment_analysis_jobs SET behavioral_json=? WHERE id=?',(json.dumps(summary,ensure_ascii=False),job['id']))
    conn.commit()

def _ai(conn,job,run_id,analyzer):
    if get(conn,job['id'])['status']=='CANCELLED':return False,'Dibatalkan.'
    rows=conn.execute('''SELECT s.id,s.source_ref,s.content_sha256,c.body,c.author_name,c.published_at FROM screening_results s JOIN comments c ON c.comment_id=s.source_ref
      LEFT JOIN comment_analysis_ai_results a ON a.analysis_id=? AND a.source_ref=s.source_ref AND a.content_sha256=s.content_sha256
      WHERE s.run_id=? AND s.kind='comment' AND a.id IS NULL ORDER BY s.id''',(job['id'],run_id)).fetchall()
    if not rows:return True,''
    comments=[]
    for r in rows:
        text=(r['body'] or '')[:1500]
        if not text.strip() or hashlib.sha256((r['body'] or '').encode()).hexdigest()!=r['content_sha256']:
            return False,'Sumber kosong atau berubah; jalankan ulang penyaringan.'
        comments.append({'ref':r['id'],'author':(r['author_name'] or '')[:200],'published_at':(r['published_at'] or '')[:100],'text':text})
    context=((job['video']['title']+'\n'+job['video']['description'])[:1500] if job['video'] else '')
    label='video '+job['video_id'] if job['video_id'] and not str(job['video_id']).startswith('sim-') else 'sumber '+str(job['video_id'])
    try:result=analyzer(conn,{'label':label,'context':context,'comments':comments},job['provider'],job['model'] or None)
    except (ValueError,ai_providers.ProviderError) as exc:
        conn.execute('UPDATE comment_analysis_jobs SET ai_failed=? WHERE id=?',(len(comments),job['id']));conn.commit()
        return False,str(exc)[:500]
    mapping={r['id']:r for r in rows}
    by_ref={v['ref']:v for v in result['verdicts']}
    conn.execute('BEGIN IMMEDIATE')
    try:
        if conn.execute('SELECT status FROM comment_analysis_jobs WHERE id=?',(job['id'],)).fetchone()[0]=='CANCELLED':conn.rollback();return False,'Dibatalkan.'
        for row in rows:
            current=conn.execute('SELECT body FROM comments WHERE comment_id=?',(row['source_ref'],)).fetchone()
            if not current or hashlib.sha256(current['body'].encode()).hexdigest()!=row['content_sha256']:raise ValueError('Sumber berubah saat analisis AI.')
            verdict=by_ref.get(row['id'])
            if verdict is None:
                kind,quote,claim,explanation='safe','','','Tidak ada indikasi; digolongkan aman.'
            else:
                kind,quote,claim,explanation=verdict['category'],verdict['quote'],verdict.get('claim',''),verdict['explanation']
            conn.execute('''INSERT OR IGNORE INTO comment_analysis_ai_results(analysis_id,source_ref,content_sha256,kind,quote,claim,explanation,provider,model,created_at,cache_hit)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(job['id'],row['source_ref'],row['content_sha256'],kind,quote,claim,explanation,result['provider'],result['model'],now(),1 if result.get('cache_hit') is True else None))
            if verdict is not None:
                conn.execute('''INSERT OR IGNORE INTO comment_analysis_findings(analysis_id,source_ref,category,kind,explanation,quote,claim,source_sha256,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?)''',(job['id'],row['source_ref'],kind,kind,explanation,quote,claim,row['content_sha256'],now()))
                fid=conn.execute('SELECT id FROM comment_analysis_findings WHERE analysis_id=? AND source_ref=? AND category=? AND group_key=\'\' AND source_sha256=?',(job['id'],row['source_ref'],kind,row['content_sha256'])).fetchone()[0]
                conn.execute('INSERT OR IGNORE INTO comment_analysis_finding_evidence(finding_id,source_ref) VALUES(?,?)',(fid,row['source_ref']))
        total=conn.execute('SELECT count(*) FROM screening_results WHERE run_id=? AND kind=\'comment\'',(run_id,)).fetchone()[0]
        coverage=json.dumps({'analyzed':len(rows),'total':total,'calls':result.get('calls',1),'chunked':bool(result.get('chunked')),'model':result['model']},ensure_ascii=False)
        conn.execute('UPDATE comment_analysis_jobs SET ai_failed=0,ai_coverage_json=?,updated_at=? WHERE id=?',(coverage,now(),job['id']));conn.commit()
    except Exception as exc:conn.rollback();return False,str(exc)[:500]
    return True,''

def run(db_path,ident,key,fetch_api,fetch_page,store_item,analyzer=ai_providers.analyze_source):
    conn=sqlite3.connect(str(db_path),timeout=10);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA busy_timeout=10000');conn.execute('PRAGMA foreign_keys=ON')
    try:
        conn.execute('BEGIN IMMEDIATE');row=conn.execute('SELECT status FROM comment_analysis_jobs WHERE id=?',(ident,)).fetchone()
        if not row or row['status']!='QUEUED':conn.rollback();return
        other=conn.execute("SELECT 1 FROM comment_analysis_jobs WHERE id!=? AND status IN ('COLLECTING','PROCESSING','ANALYZING') LIMIT 1",(ident,)).fetchone()
        if other:
            conn.execute("UPDATE comment_analysis_jobs SET status='PARTIAL',stage='Menunggu pekerja lain',error='Analisis lain sedang berjalan; lanjutkan setelah selesai.',updated_at=? WHERE id=?",(now(),ident));conn.commit();return
        conn.execute("UPDATE comment_analysis_jobs SET status='COLLECTING',stage='Mengambil metadata',updated_at=? WHERE id=?",(now(),ident));conn.commit()
        job=get(conn,ident);start_count=job['api_calls'];partial=''
        if not key or key.startswith('ISI_KEY_'):raise ValueError('YOUTUBE_API_KEY belum dikonfigurasi.')
        cached=job['video']
        try:fresh=bool(cached) and datetime.fromisoformat(cached['fetched_at'])>datetime.now(timezone.utc)-timedelta(hours=6)
        except (ValueError,TypeError):fresh=False
        # A new analysis of an existing video is an explicit refresh; a resume
        # may use the recent metadata already fetched by that same job.
        if not fresh or (cached and not job['collection_job_id']):
            _metadata(conn,ident,job['video_id'],key,fetch_api,start_count,job['max_requests'])
        # An analysis keeps the exact video context used by its AI results.
        # Later metadata refreshes may update the shared video cache, but cannot
        # silently change the context of an existing, resumable analysis.
        if not conn.execute('SELECT video_snapshot FROM comment_analysis_jobs WHERE id=?',(ident,)).fetchone()[0]:
            video=conn.execute('SELECT * FROM comment_analysis_videos WHERE video_id=?',(job['video_id'],)).fetchone()
            conn.execute('UPDATE comment_analysis_jobs SET video_snapshot=? WHERE id=?',(json.dumps(dict(video),ensure_ascii=False),ident));conn.commit()
        if not job['collection_job_id']:
            collection=collection_jobs.new(conn,job['video_id'],20,'youtube')
            conn.execute('UPDATE comment_analysis_jobs SET collection_job_id=? WHERE id=?',(collection['id'],ident));conn.commit()
        job=get(conn,ident);cid=job['collection_job_id']
        def fetch(k,v,cursor):
            _reserve(conn,ident,'commentThreads.list',start_count,job['max_requests']);return fetch_page(k,v,cursor)
        def save(c,v,item):
            ok=store_item(c,v,item)
            if ok and job['include_replies']:
                sn=item.get('snippet') or {};count=sn.get('totalReplyCount',0);parent=(sn.get('topLevelComment') or {}).get('id')
                if type(count) is int and count>0 and isinstance(parent,str) and parent:
                    c.execute('INSERT OR IGNORE INTO comment_analysis_replies(analysis_id,parent_id) VALUES(?,?)',(ident,parent))
            return ok
        current=collection_jobs.get(conn,cid)
        if current['status'] in ('paused','rate_limited','failed'):collection_jobs.control(conn,cid,'resume')
        _set(conn,ident,'COLLECTING','Mengumpulkan komentar')
        while collection_jobs.get(conn,cid)['status']=='queued':
            collection_jobs.run(db_path,cid,key,fetch,save,strict=True)
            current=collection_jobs.get(conn,cid)
            if current['status']=='paused' and current['end_reason'].startswith('Batas halaman'):
                collection_jobs.control(conn,cid,'resume')
        current=collection_jobs.get(conn,cid)
        if get(conn,ident)['status']=='CANCELLED':return
        if current['status']!='completed':partial=current['end_reason'] or 'Pengumpulan komentar belum selesai.'
        if job['include_replies'] and not partial:
            _set(conn,ident,'COLLECTING','Mengumpulkan balasan')
            ok,reason=_reply_pages(conn,get(conn,ident),key,fetch_api,store_item,start_count)
            if not ok:partial=reason
        quarantined=conn.execute('SELECT count(*) FROM collection_job_rejections WHERE job_id=?',(cid,)).fetchone()[0]
        if quarantined:
            partial=(partial+' '+str(quarantined)+' item tidak valid dikarantina; cakupan tidak lengkap. Perbarui analisis untuk mencoba kembali.').strip()
        if get(conn,ident)['status']=='CANCELLED':return
        _set(conn,ident,'PROCESSING','Mengelompokkan dan memeriksa pola')
        job=get(conn,ident);max_item=conn.execute('SELECT COALESCE(MAX(id),0) FROM collection_job_items WHERE job_id=?',(cid,)).fetchone()[0]
        old=screening.get_run(conn,job['screening_run_id']) if job['screening_run_id'] else None
        if not old or old['max_job_item_id']!=max_item:
            old=screening.start(conn,'','comments',cid);conn.execute('UPDATE comment_analysis_jobs SET screening_run_id=? WHERE id=?',(old['id'],ident));conn.commit()
        while screening.run_batch(conn,old['id'],150)['status']!='completed':
            if get(conn,ident)['status']=='CANCELLED':return
            conn.execute('UPDATE comment_analysis_jobs SET updated_at=? WHERE id=?',(now(),ident));conn.commit()
        job=get(conn,ident);_behavior(conn,job,old['id'])
        _set(conn,ident,'ANALYZING','Menganalisis komentar dengan AI')
        ok,reason=_ai(conn,job,old['id'],analyzer)
        if not ok:partial=(partial+' '+reason).strip()
        if get(conn,ident)['status']=='CANCELLED':return
        _set(conn,ident,'PARTIAL' if partial else 'COMPLETED','Hasil parsial' if partial else 'Analisis selesai',partial)
    except Exception as exc:
        conn.rollback();message=str(exc) if isinstance(exc,(ValueError,RuntimeError,ai_providers.ProviderError,youtube_radar.APIError)) else 'Pekerjaan gagal; periksa koneksi dan konfigurasi.'
        _set(conn,ident,'FAILED','Gagal',message)
    finally:conn.close()

def findings(conn,ident,page=1,category='all',ai_page=1):
    job=get(conn,ident)
    if category not in ('all','behavior','bot','provocative','hoax'):raise ValueError('Kategori tidak valid.')
    try:page=int(page)
    except (ValueError,TypeError,OverflowError) as exc:raise ValueError('Halaman tidak valid.') from exc
    if not 1<=page<=100000:raise ValueError('Halaman tidak valid.')
    try:ai_page=int(ai_page)
    except (ValueError,TypeError,OverflowError) as exc:raise ValueError('Halaman AI tidak valid.') from exc
    if not 1<=ai_page<=100000:raise ValueError('Halaman AI tidak valid.')
    filters="f.analysis_id=?";args=[job['id']]
    if category!='all':filters+=' AND f.category=?';args.append(category)
    total=conn.execute('SELECT count(*) FROM comment_analysis_findings f WHERE '+filters,args).fetchone()[0]
    rows=[dict(r) for r in conn.execute('''SELECT f.*,c.body,c.source_url,c.published_at,c.author_name FROM comment_analysis_findings f
      JOIN comments c ON c.comment_id=f.source_ref WHERE '''+filters+' ORDER BY f.id LIMIT 25 OFFSET ?',args+[(page-1)*25])]
    for row in rows:
        row['source_changed']=hashlib.sha256(row['body'].encode()).hexdigest()!=row['source_sha256']
        row['ai_input_truncated']=row['category']!='behavior' and len(row['body'])>1500
        row['body']=row['body'][:1200]
    counts={r['category']:r['n'] for r in conn.execute('SELECT category,count(*) AS n FROM comment_analysis_findings WHERE analysis_id=? GROUP BY category',(job['id'],))}
    ai_total=conn.execute('SELECT count(*) FROM comment_analysis_ai_results WHERE analysis_id=?',(job['id'],)).fetchone()[0]
    ai_rows=[dict(r) for r in conn.execute('''SELECT a.source_ref,a.kind,a.quote,a.claim,a.explanation,a.provider,a.model,a.created_at,a.cache_hit,a.content_sha256,c.body,c.author_name
      FROM comment_analysis_ai_results a JOIN comments c ON c.comment_id=a.source_ref WHERE a.analysis_id=? ORDER BY a.id LIMIT 25 OFFSET ?''',(job['id'],(ai_page-1)*25))]
    for row in ai_rows:
        row['source_changed']=hashlib.sha256(row['body'].encode()).hexdigest()!=row.pop('content_sha256')
        row['ai_input_truncated']=len(row['body'])>1500
        row['body']=row['body'][:1500]
    return {'analysis':job,'findings':rows,'counts':counts,'total':total,'page':page,'page_size':25,'ai_total':ai_total,'ai_results':ai_rows}

def finding_detail(conn,ident,page=1):
    ident=investigation._id(ident)
    try:page=int(page)
    except (ValueError,TypeError,OverflowError) as exc:raise ValueError('Halaman tidak valid.') from exc
    if not 1<=page<=100000:raise ValueError('Halaman tidak valid.')
    finding=conn.execute('SELECT * FROM comment_analysis_findings WHERE id=?',(ident,)).fetchone()
    if not finding:raise ValueError('Temuan tidak ditemukan.')
    job=get(conn,finding['analysis_id'])
    total=conn.execute('SELECT count(*) FROM comment_analysis_finding_evidence WHERE finding_id=?',(ident,)).fetchone()[0]
    evidence=[dict(r) for r in conn.execute('''SELECT c.comment_id,c.body,c.source_url,c.published_at,c.author_name,r.parent_id,s.content_sha256
      FROM comment_analysis_finding_evidence e JOIN comments c ON c.comment_id=e.source_ref
      LEFT JOIN comment_analysis_reply_links r ON r.analysis_id=? AND r.comment_id=c.comment_id
      LEFT JOIN screening_results s ON s.run_id=? AND s.kind='comment' AND s.source_ref=c.comment_id
      WHERE e.finding_id=? ORDER BY c.comment_id LIMIT 25 OFFSET ?''',(finding['analysis_id'],job['screening_run_id'],ident,(page-1)*25))]
    for row in evidence:
        row['source_changed']=not row['content_sha256'] or hashlib.sha256(row['body'].encode()).hexdigest()!=row['content_sha256']
        row.pop('content_sha256')
        row['truncated']=len(row['body'])>10000
        row['body']=row['body'][:10000]
    return {'finding':dict(finding),'evidence':evidence,'total':total,'page':page,'page_size':25,
            'warning':'Sumber dan hasil AI perlu review manusia; pengulangan bukan bukti bot atau koordinasi.'}

def open_case(conn,finding_id, *, dataset='real'):
    if dataset not in ('real','demo'):raise ValueError('Dataset tidak valid.')
    ident=investigation._id(finding_id);conn.execute('BEGIN IMMEDIATE')
    try:
        row=conn.execute('SELECT * FROM comment_analysis_findings WHERE id=?',(ident,)).fetchone()
        if not row:raise ValueError('Temuan tidak ditemukan.')
        source=conn.execute('SELECT body FROM comments WHERE comment_id=?',(row['source_ref'],)).fetchone()
        if not source or hashlib.sha256(source['body'].encode()).hexdigest()!=row['source_sha256']:raise ValueError('Sumber berubah; jalankan analisis ulang sebelum investigasi.')
        found=conn.execute('SELECT case_id FROM comment_analysis_cases WHERE finding_id=?',(ident,)).fetchone()
        if found:conn.commit();return {'id':found[0],'existing':True}
        job=get(conn,row['analysis_id']);stamp=now()
        cid=conn.execute("INSERT INTO intel_investigation_cases(title,summary,dataset,created_at,updated_at) VALUES(?,?,?,?,?)",(('SIMULASI — ' if dataset=='demo' else '')+'Temuan komentar #'+str(ident),('Analisis #'+str(job['id'])+(' / data simulasi. ' if dataset=='demo' else ' / video '+job['video_id']+'. ')+row['explanation'])[:1500],dataset,stamp,stamp)).lastrowid
        investigation._event(conn,cid,'case_created','Kasus dibuat dari temuan; perlu review manusia.')
        refs=[row['source_ref']]+[r['source_ref'] for r in conn.execute('SELECT source_ref FROM comment_analysis_finding_evidence WHERE finding_id=? AND source_ref!=? ORDER BY source_ref LIMIT 19',(ident,row['source_ref']))]
        verified_refs=[]
        for ref in refs:
            source_row=conn.execute('''SELECT s.content_sha256,c.body FROM screening_results s JOIN comments c ON c.comment_id=s.source_ref
                WHERE s.run_id=? AND s.source_ref=?''',(job['screening_run_id'],ref)).fetchone()
            if source_row and hashlib.sha256(source_row['body'].encode()).hexdigest()==source_row['content_sha256']:verified_refs.append(ref)
        refs=verified_refs
        if not refs:raise ValueError('Bukti sumber berubah; jalankan analisis ulang.')
        item_ids={}
        for ref in refs:item_ids[ref]=investigation._add_item(conn,cid,investigation._source(conn,'comment',ref))
        claim=(row['claim'] or row['quote']).strip() if row['category']=='hoax' else ''
        if len(claim)>=15 and row['source_ref'] in item_ids:
            item=conn.execute('SELECT snapshot_sha256 FROM intel_investigation_items WHERE id=?',(item_ids[row['source_ref']],)).fetchone()
            fid=conn.execute('''INSERT INTO unified_fact_cases(investigation_id,item_id,snapshot_sha256,claim,dataset,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?)''',(cid,item_ids[row['source_ref']],item['snapshot_sha256'],claim[:1000],dataset,stamp,stamp)).lastrowid
            investigation._event(conn,cid,'claim_review_opened','Kandidat klaim #'+str(fid)+' belum diverifikasi.')
        conn.execute('INSERT INTO comment_analysis_cases(finding_id,case_id) VALUES(?,?)',(ident,cid));conn.commit()
        return {'id':cid,'existing':False,'evidence_count':len(refs)}
    except Exception:conn.rollback();raise

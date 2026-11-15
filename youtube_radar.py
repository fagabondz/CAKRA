"""YouTube-only discovery: bounded public API calls, persisted coverage and checkpoints."""
from __future__ import annotations
import hashlib, json, re, sqlite3, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
import regional
import investigation
import ai_providers

BASE='https://www.googleapis.com/youtube/v3/'
INTERVAL=1200
VIDEO_ID=re.compile(r'^[A-Za-z0-9_-]{11}$')
ISSUES=('gangguan layanan publik','insiden keamanan siber','bencana keselamatan masyarakat','klaim faktual layanan publik')

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS yt_radar_settings(
      id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
      interval_seconds INTEGER NOT NULL DEFAULT 1200, search_limit INTEGER NOT NULL DEFAULT 72,
      metadata_limit INTEGER NOT NULL DEFAULT 100, replies_limit INTEGER NOT NULL DEFAULT 100,
      coverage_mode TEXT NOT NULL DEFAULT 'standard', next_run INTEGER NOT NULL DEFAULT 0,
      running INTEGER NOT NULL DEFAULT 0, last_started TEXT NOT NULL DEFAULT '',
      last_success TEXT NOT NULL DEFAULT '', api_status TEXT NOT NULL DEFAULT 'not_checked',
      last_error TEXT NOT NULL DEFAULT '');
    INSERT OR IGNORE INTO yt_radar_settings(id) VALUES(1);
    CREATE TABLE IF NOT EXISTS yt_radar_provinces(
      code TEXT PRIMARY KEY, name TEXT NOT NULL, last_attempt TEXT NOT NULL DEFAULT '',
      last_success TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
      results_found INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'not_scanned');
    CREATE TABLE IF NOT EXISTS yt_radar_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, province_code TEXT NOT NULL UNIQUE REFERENCES yt_radar_provinces(code),
      query TEXT NOT NULL, window_start TEXT NOT NULL DEFAULT '', cursor TEXT, next_cursor TEXT, pending_ids TEXT NOT NULL DEFAULT '[]',
      status TEXT NOT NULL DEFAULT 'queued', retry_after INTEGER NOT NULL DEFAULT 0,
      last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS yt_radar_cursors(
      job_id INTEGER NOT NULL REFERENCES yt_radar_jobs(id), cursor TEXT NOT NULL,
      PRIMARY KEY(job_id,cursor));
    CREATE TABLE IF NOT EXISTS yt_radar_videos(
      video_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, channel_id TEXT NOT NULL,
      channel_title TEXT NOT NULL, published_at TEXT NOT NULL, category_id TEXT NOT NULL,
      duration TEXT NOT NULL, thumbnails_json TEXT NOT NULL, statistics_json TEXT NOT NULL,
      first_observed TEXT NOT NULL, last_observed TEXT NOT NULL, source_status TEXT NOT NULL DEFAULT 'available',
      relevance_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW', analysis_json TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS yt_radar_discoveries(
      id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES yt_radar_videos(video_id),
      province_code TEXT NOT NULL REFERENCES yt_radar_provinces(code), query TEXT NOT NULL,
      discovered_at TEXT NOT NULL, UNIQUE(video_id,province_code,query));
    CREATE INDEX IF NOT EXISTS idx_yt_discovery_province ON yt_radar_discoveries(province_code,id DESC);
    CREATE TABLE IF NOT EXISTS yt_radar_quota(
      id INTEGER PRIMARY KEY AUTOINCREMENT, bucket TEXT NOT NULL, reserved_at INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_yt_quota_bucket ON yt_radar_quota(bucket,reserved_at);
    CREATE TABLE IF NOT EXISTS yt_radar_cycles(
      id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL, province_code TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS yt_radar_reviews(
      id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES yt_radar_videos(video_id),
      decision TEXT NOT NULL, note TEXT NOT NULL, metadata_sha256 TEXT NOT NULL, reviewed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS yt_radar_cases(
      video_id TEXT PRIMARY KEY REFERENCES yt_radar_videos(video_id),
      case_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id));
    CREATE TABLE IF NOT EXISTS yt_radar_collection_jobs(
      video_id TEXT NOT NULL REFERENCES yt_radar_videos(video_id),
      job_id INTEGER NOT NULL REFERENCES collection_jobs(id), created_at TEXT NOT NULL, include_replies INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(video_id,job_id));
    CREATE TABLE IF NOT EXISTS yt_radar_reply_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES collection_jobs(id),
      video_id TEXT NOT NULL REFERENCES yt_radar_videos(video_id), parent_id TEXT NOT NULL,
      cursor TEXT, status TEXT NOT NULL DEFAULT 'queued', pages_count INTEGER NOT NULL DEFAULT 0,
      session_pages INTEGER NOT NULL DEFAULT 0, retrieved_count INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, UNIQUE(job_id,parent_id));
    CREATE TABLE IF NOT EXISTS yt_radar_reply_cursors(
      reply_job_id INTEGER NOT NULL REFERENCES yt_radar_reply_jobs(id), cursor TEXT NOT NULL,
      PRIMARY KEY(reply_job_id,cursor));
    CREATE TABLE IF NOT EXISTS yt_radar_reply_links(
      reply_job_id INTEGER NOT NULL REFERENCES yt_radar_reply_jobs(id), comment_id TEXT NOT NULL REFERENCES comments(comment_id),
      parent_id TEXT NOT NULL, PRIMARY KEY(reply_job_id,comment_id));
    CREATE TABLE IF NOT EXISTS yt_radar_ai_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES yt_radar_videos(video_id),
      provider TEXT NOT NULL, model TEXT NOT NULL, fallback INTEGER NOT NULL DEFAULT 0,
      mode TEXT NOT NULL DEFAULT 'metadata', status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_yt_active_ai_job ON yt_radar_ai_jobs(video_id) WHERE status IN ('queued','running');
    CREATE TABLE IF NOT EXISTS yt_radar_ai_results(
      job_id INTEGER PRIMARY KEY REFERENCES yt_radar_ai_jobs(id),video_id TEXT NOT NULL REFERENCES yt_radar_videos(video_id),
      mode TEXT NOT NULL, metadata_sha256 TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS yt_radar_video_locations(
      video_id TEXT PRIMARY KEY REFERENCES yt_radar_videos(video_id), province_code TEXT NOT NULL REFERENCES yt_radar_provinces(code),
      quote TEXT NOT NULL, confidence TEXT NOT NULL, metadata_sha256 TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS yt_query_plan_jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, province_code TEXT NOT NULL REFERENCES yt_radar_provinces(code),
      provider TEXT NOT NULL, model TEXT NOT NULL, fallback INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
      error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS yt_query_proposals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, plan_job_id INTEGER NOT NULL REFERENCES yt_query_plan_jobs(id),
      province_code TEXT NOT NULL REFERENCES yt_radar_provinces(code), query TEXT NOT NULL,
      provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'proposed', created_at TEXT NOT NULL);
    ''')
    settings_columns={r[1] for r in conn.execute('PRAGMA table_info(yt_radar_settings)')}
    if 'replies_limit' not in settings_columns: conn.execute('ALTER TABLE yt_radar_settings ADD COLUMN replies_limit INTEGER NOT NULL DEFAULT 100')
    if 'coverage_mode' not in settings_columns: conn.execute("ALTER TABLE yt_radar_settings ADD COLUMN coverage_mode TEXT NOT NULL DEFAULT 'standard'")
    job_columns={r[1] for r in conn.execute('PRAGMA table_info(yt_radar_collection_jobs)')}
    if 'include_replies' not in job_columns: conn.execute('ALTER TABLE yt_radar_collection_jobs ADD COLUMN include_replies INTEGER NOT NULL DEFAULT 0')
    ai_columns={r[1] for r in conn.execute('PRAGMA table_info(yt_radar_ai_jobs)')}
    if 'mode' not in ai_columns:conn.execute("ALTER TABLE yt_radar_ai_jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'metadata'")
    search_columns={r[1] for r in conn.execute('PRAGMA table_info(yt_radar_jobs)')}
    if 'retry_after' not in search_columns:conn.execute('ALTER TABLE yt_radar_jobs ADD COLUMN retry_after INTEGER NOT NULL DEFAULT 0')
    proposal_columns={r[1] for r in conn.execute('PRAGMA table_info(yt_query_proposals)')}
    if 'provider' not in proposal_columns:conn.execute("ALTER TABLE yt_query_proposals ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
    if 'model' not in proposal_columns:conn.execute("ALTER TABLE yt_query_proposals ADD COLUMN model TEXT NOT NULL DEFAULT ''")
    conn.executemany('INSERT OR IGNORE INTO yt_radar_provinces(code,name) VALUES(?,?)',regional.PROVINCES.items())
    conn.commit()

class APIError(Exception):
    def __init__(self,message,status='error'):
        super().__init__(message);self.status=status

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl): return None

def request_api(kind,params,key):
    if kind not in ('search','videos','comments'): raise ValueError('Endpoint tidak didukung.')
    url=BASE+kind+'?'+urllib.parse.urlencode({**params,'key':key})
    req=urllib.request.Request(url,headers={'User-Agent':'CAKRA-YouTube-Radar/1.0'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req,timeout=20) as response:
            raw=response.read(2_000_001)
        if len(raw)>2_000_000: raise APIError('Respons YouTube melebihi 2 MB.')
        data=json.loads(raw)
        if not isinstance(data,dict): raise ValueError
        return data
    except urllib.error.HTTPError as exc:
        raise APIError('YouTube menolak permintaan (HTTP %s).'%exc.code,
                       'limited_or_forbidden' if exc.code in (403,429) else 'error') from None
    except (urllib.error.URLError,TimeoutError,OSError):
        raise APIError('YouTube tidak dapat dihubungi; periksa jaringan.') from None
    except (ValueError,UnicodeError): raise APIError('Respons YouTube tidak valid.') from None

def _reserve(conn,bucket,clock):
    conn.execute('BEGIN IMMEDIATE')
    try:
        settings=conn.execute('SELECT search_limit,metadata_limit,replies_limit FROM yt_radar_settings WHERE id=1').fetchone()
        if bucket not in ('search','metadata','replies'): raise ValueError('Bucket kuota tidak dikenal.')
        limit=settings[{'search':'search_limit','metadata':'metadata_limit','replies':'replies_limit'}[bucket]]
        used=conn.execute('SELECT count(*) FROM yt_radar_quota WHERE bucket=? AND reserved_at>?',(bucket,clock-86400)).fetchone()[0]
        if used>=limit: raise APIError('Batas lokal '+bucket+' 24 jam tercapai.','local_quota')
        conn.execute('INSERT INTO yt_radar_quota(bucket,reserved_at) VALUES(?,?)',(bucket,clock));conn.commit()
    except Exception: conn.rollback();raise

def recover(conn):
    # Do not steal a recent in-flight job from another local server process.
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(timespec='seconds')
    conn.execute("UPDATE yt_radar_cycles SET status='interrupted',finished_at=?,detail='Server terhenti; checkpoint dipertahankan.' WHERE status='running' AND started_at<?",(now(),cutoff))
    conn.execute("UPDATE yt_radar_settings SET running=0,api_status='interrupted' WHERE id=1 AND running=1 AND last_started<?",(cutoff,))
    conn.execute("UPDATE yt_radar_ai_jobs SET status='interrupted',error='Server berhenti; permintaan mungkin sudah dikenakan biaya, periksa sebelum mengulang.',updated_at=? WHERE status='running' AND updated_at<?",(now(),cutoff))
    conn.execute("UPDATE yt_radar_reply_jobs SET status='paused',error='Server berhenti; lanjutkan secara manual.',updated_at=? WHERE status='running' AND updated_at<?",(now(),cutoff))
    conn.execute("UPDATE yt_query_plan_jobs SET status='interrupted',error='Server berhenti; periksa penggunaan sebelum mengulang.',updated_at=? WHERE status='running' AND updated_at<?",(now(),cutoff))
    conn.commit()

def configure(conn,enabled,mode=None,search_limit=None,metadata_limit=None,replies_limit=None):
    if type(enabled) is not bool: raise ValueError('Status radar harus boolean.')
    if mode is not None:
        if mode not in ('standard','high','custom'):raise ValueError('Mode cakupan tidak valid.')
        if mode=='standard':search_limit,metadata_limit,replies_limit=72,100,100
        elif mode=='high':search_limit,metadata_limit,replies_limit=100,200,200
        else:
            values=(search_limit,metadata_limit,replies_limit)
            if any(type(x) is not int for x in values):raise ValueError('Batas kuota harus berupa bilangan bulat.')
            if not 1<=search_limit<=1000 or not 1<=metadata_limit<=10000 or not 1<=replies_limit<=10000:raise ValueError('Batas kuota di luar rentang.')
        conn.execute('UPDATE yt_radar_settings SET coverage_mode=?,search_limit=?,metadata_limit=?,replies_limit=? WHERE id=1',(mode,search_limit,metadata_limit,replies_limit))
    conn.execute('UPDATE yt_radar_settings SET next_run=CASE WHEN ?=1 AND enabled=0 THEN 0 ELSE next_run END,enabled=? WHERE id=1',(int(enabled),int(enabled)))
    conn.commit();return {'enabled':enabled}

def _ids(data):
    items=data.get('items')
    if not isinstance(items,list) or len(items)>50: raise APIError('Format hasil pencarian YouTube tidak valid.')
    ids=[]
    for item in items:
        vid=(item.get('id') or {}).get('videoId') if isinstance(item,dict) and isinstance(item.get('id'),dict) else None
        if isinstance(vid,str) and VIDEO_ID.fullmatch(vid) and vid not in ids: ids.append(vid)
    token=data.get('nextPageToken')
    if token is not None and (not isinstance(token,str) or not token or len(token)>1000): raise APIError('Cursor pencarian tidak valid.')
    return ids,token

def _metadata(conn,job,data,ids,stamp):
    items=data.get('items')
    if not isinstance(items,list) or len(items)>50: raise APIError('Format metadata YouTube tidak valid.')
    accepted=set()
    for item in items:
        if not isinstance(item,dict): continue
        vid=item.get('id');snippet=item.get('snippet')
        if vid not in ids or vid in accepted or not isinstance(snippet,dict): continue
        title=snippet.get('title');published=snippet.get('publishedAt')
        if not isinstance(title,str) or not title.strip() or not isinstance(published,str): continue
        accepted.add(vid)
        description=snippet.get('description') if isinstance(snippet.get('description'),str) else ''
        thumb=snippet.get('thumbnails') if isinstance(snippet.get('thumbnails'),dict) else {}
        stats=item.get('statistics') if isinstance(item.get('statistics'),dict) else {}
        details=item.get('contentDetails') if isinstance(item.get('contentDetails'),dict) else {}
        conn.execute('''INSERT INTO yt_radar_videos(video_id,title,description,channel_id,channel_title,published_at,category_id,duration,thumbnails_json,statistics_json,first_observed,last_observed)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
          title=excluded.title,description=excluded.description,channel_id=excluded.channel_id,channel_title=excluded.channel_title,
          published_at=excluded.published_at,category_id=excluded.category_id,duration=excluded.duration,
          thumbnails_json=excluded.thumbnails_json,statistics_json=excluded.statistics_json,last_observed=excluded.last_observed,source_status='available',
          relevance_status=CASE WHEN yt_radar_videos.title!=excluded.title OR yt_radar_videos.description!=excluded.description THEN 'NEEDS_REVIEW' ELSE yt_radar_videos.relevance_status END,
          analysis_json=CASE WHEN yt_radar_videos.title!=excluded.title OR yt_radar_videos.description!=excluded.description OR yt_radar_videos.thumbnails_json!=excluded.thumbnails_json THEN '' ELSE yt_radar_videos.analysis_json END''',
          (vid,title[:500],description[:10000],str(snippet.get('channelId') or '')[:200],str(snippet.get('channelTitle') or '')[:200],published[:80],str(snippet.get('categoryId') or '')[:20],str(details.get('duration') or '')[:80],json.dumps(thumb,ensure_ascii=False)[:12000],json.dumps(stats)[:4000],stamp,stamp))
        conn.execute('INSERT OR IGNORE INTO yt_radar_discoveries(video_id,province_code,query,discovered_at) VALUES(?,?,?,?)',(vid,job['province_code'],job['query'],stamp))
    for missing in set(ids)-accepted:
        conn.execute("UPDATE yt_radar_videos SET source_status='unavailable_or_incomplete' WHERE video_id=?",(missing,))
    return len(accepted)

def run_cycle(conn,key,fetcher=request_api,clock=None,force=False):
    clock=int(time.time()) if clock is None else int(clock)
    recover(conn)
    conn.execute('BEGIN IMMEDIATE')
    try:
        cfg=conn.execute('SELECT * FROM yt_radar_settings WHERE id=1').fetchone()
        if cfg['running']: conn.rollback();return {'started':False,'reason':'running'}
        if not force and (not cfg['enabled'] or cfg['next_run']>clock): conn.rollback();return {'started':False,'reason':'not_due'}
        stamp=now();conn.execute('UPDATE yt_radar_settings SET running=1,last_started=?,next_run=? WHERE id=1',(stamp,clock+INTERVAL))
        cycle=conn.execute("INSERT INTO yt_radar_cycles(started_at,status) VALUES(?,'running')",(stamp,)).lastrowid;conn.commit()
    except Exception:conn.rollback();raise
    province='';status='error';detail=''
    try:
        if not key or key.startswith('ISI_KEY_'): raise APIError('YOUTUBE_API_KEY belum dikonfigurasi.','not_configured')
        job=conn.execute("SELECT * FROM yt_radar_jobs WHERE status='metadata' AND retry_after<=? ORDER BY updated_at,id LIMIT 1",(clock,)).fetchone()
        if job is None:
            p=conn.execute("SELECT * FROM yt_radar_provinces p WHERE NOT EXISTS(SELECT 1 FROM yt_radar_jobs j WHERE j.province_code=p.code AND j.status='metadata') ORDER BY last_attempt,code LIMIT 1").fetchone()
            if p is None:raise APIError('Semua checkpoint metadata menunggu waktu percobaan ulang.','waiting_retry')
            province=p['code'];job=conn.execute('SELECT * FROM yt_radar_jobs WHERE province_code=?',(province,)).fetchone()
            if job is None:
                query=p['name']+' '+ISSUES[p['attempts']%len(ISSUES)]
                conn.execute('INSERT INTO yt_radar_jobs(province_code,query,updated_at) VALUES(?,?,?)',(province,query,stamp))
                job=conn.execute('SELECT * FROM yt_radar_jobs WHERE province_code=?',(province,)).fetchone()
            if not job['window_start']:
                window=(datetime.fromtimestamp(clock,timezone.utc)-timedelta(days=30)).isoformat(timespec='seconds')
                conn.execute('UPDATE yt_radar_jobs SET window_start=? WHERE id=?',(window,job['id']))
                job=conn.execute('SELECT * FROM yt_radar_jobs WHERE id=?',(job['id'],)).fetchone()
            conn.execute("UPDATE yt_radar_provinces SET last_attempt=?,attempts=attempts+1,status='searching' WHERE code=?",(stamp,province));conn.commit()
            _reserve(conn,'search',clock)
            params={'part':'snippet','type':'video','q':job['query'],'maxResults':50,'relevanceLanguage':'id','regionCode':'ID','order':'date',
                    'publishedAfter':job['window_start']}
            if job['cursor']: params['pageToken']=job['cursor']
            ids,token=_ids(fetcher('search',params,key))
            if token and (token==job['cursor'] or conn.execute('SELECT 1 FROM yt_radar_cursors WHERE job_id=? AND cursor=?',(job['id'],token)).fetchone()):
                raise APIError('Cursor YouTube berulang; perlu pemeriksaan.')
            conn.execute("UPDATE yt_radar_jobs SET pending_ids=?,next_cursor=?,status='metadata',updated_at=? WHERE id=?",(json.dumps(ids),token,stamp,job['id']));conn.commit()
            job=conn.execute('SELECT * FROM yt_radar_jobs WHERE id=?',(job['id'],)).fetchone()
        else: province=job['province_code']
        ids=json.loads(job['pending_ids'])
        if ids:
            if not force and not conn.execute('SELECT enabled FROM yt_radar_settings WHERE id=1').fetchone()[0]:raise APIError('Radar dijeda; metadata menunggu siklus berikutnya.','paused')
            _reserve(conn,'metadata',clock)
            data=fetcher('videos',{'part':'snippet,contentDetails,statistics','id':','.join(ids),'maxResults':50},key)
        else: data={'items':[]}
        conn.execute('BEGIN IMMEDIATE')
        if not force and not conn.execute('SELECT enabled FROM yt_radar_settings WHERE id=1').fetchone()[0]:raise APIError('Radar dijeda; metadata menunggu siklus berikutnya.','paused')
        count=_metadata(conn,job,data,ids,stamp)
        token=job['next_cursor']
        if token:conn.execute('INSERT INTO yt_radar_cursors(job_id,cursor) VALUES(?,?)',(job['id'],token))
        if token:
            conn.execute("UPDATE yt_radar_jobs SET cursor=?,next_cursor=NULL,pending_ids='[]',status='queued',retry_after=0,last_error='',updated_at=? WHERE id=?",(token,stamp,job['id']))
        else:
            conn.execute('DELETE FROM yt_radar_cursors WHERE job_id=?',(job['id'],))
            p=conn.execute('SELECT attempts,name FROM yt_radar_provinces WHERE code=?',(province,)).fetchone()
            query=p['name']+' '+ISSUES[p['attempts']%len(ISSUES)]
            conn.execute("UPDATE yt_radar_jobs SET query=?,window_start='',cursor=NULL,next_cursor=NULL,pending_ids='[]',status='queued',retry_after=0,last_error='',updated_at=? WHERE id=?",(query,stamp,job['id']))
        conn.execute("UPDATE yt_radar_provinces SET last_success=?,results_found=results_found+?,status='ok' WHERE code=?",(stamp,count,province))
        conn.commit();status='ok';detail=str(count)+' metadata video diterima; '+str(len(ids)-count)+' tidak tersedia/lengkap. Relevansi belum diverifikasi.'
    except (APIError,ValueError,TypeError,KeyError,sqlite3.Error) as exc:
        conn.rollback();status=exc.status if isinstance(exc,APIError) else 'error'
        detail=str(exc) if isinstance(exc,APIError) else 'Siklus gagal; checkpoint dipertahankan.'
        if province:
            conn.execute('UPDATE yt_radar_provinces SET status=? WHERE code=?',(status,province))
            conn.execute("UPDATE yt_radar_jobs SET last_error=?,retry_after=CASE WHEN status='metadata' AND ? NOT IN ('paused','local_quota') THEN ? ELSE retry_after END WHERE province_code=?",(detail,status,clock+2400,province))
        if status=='limited_or_forbidden':conn.execute('UPDATE yt_radar_settings SET next_run=? WHERE id=1',(clock+7200,))
    finally:
        finished=now()
        conn.execute('UPDATE yt_radar_cycles SET finished_at=?,status=?,province_code=?,detail=? WHERE id=?',(finished,status,province,detail,cycle))
        conn.execute('UPDATE yt_radar_settings SET running=0,api_status=?,last_error=?,last_success=CASE WHEN ?=\'ok\' THEN ? ELSE last_success END WHERE id=1',(status,'' if status=='ok' else detail,status,finished))
        conn.commit()
    return {'started':True,'status':status,'province_code':province,'detail':detail}

def dashboard(conn,page=1):
    if isinstance(page,bool): raise ValueError('Halaman tidak valid.')
    try:page=int(page)
    except (ValueError,TypeError,OverflowError) as exc:raise ValueError('Halaman tidak valid.') from exc
    if not 1<=page<=100000:raise ValueError('Halaman tidak valid.')
    cfg=dict(conn.execute('SELECT * FROM yt_radar_settings WHERE id=1').fetchone())
    cutoff=int(time.time())-86400
    quota={r['bucket']:r['n'] for r in conn.execute('SELECT bucket,count(*) AS n FROM yt_radar_quota WHERE reserved_at>? GROUP BY bucket',(cutoff,))}
    provinces=[dict(r) for r in conn.execute('SELECT * FROM yt_radar_provinces ORDER BY code')]
    videos=[dict(r) for r in conn.execute('''SELECT v.video_id,v.title,v.channel_title,v.published_at,v.last_observed,v.relevance_status,v.source_status,v.thumbnails_json,
      (SELECT count(*) FROM yt_radar_discoveries d WHERE d.video_id=v.video_id) AS search_contexts
      FROM yt_radar_videos v ORDER BY v.last_observed DESC,v.video_id LIMIT 30 OFFSET ?''',((page-1)*30,))]
    for video in videos:
        video['thumbnail_url']=thumbnail_url(video);video.pop('thumbnails_json')
    total=conn.execute('SELECT count(*) FROM yt_radar_videos').fetchone()[0]
    counts={r['relevance_status']:r['n'] for r in conn.execute('SELECT relevance_status,count(*) AS n FROM yt_radar_videos GROUP BY relevance_status')}
    return {'settings':cfg,'quota':quota,'provinces':provinces,'videos':videos,'page':page,'page_size':30,'total':total,'counts':counts,
            'limitations':'Konteks pencarian bukan bukti lokasi kejadian. Relevansi dan lokasi perlu ditinjau; kuota hanya mencatat permintaan radar ini.'}

def get_video(conn,video_id):
    if not isinstance(video_id,str) or not VIDEO_ID.fullmatch(video_id): raise ValueError('ID video tidak valid.')
    row=conn.execute('SELECT * FROM yt_radar_videos WHERE video_id=?',(video_id,)).fetchone()
    if not row: raise ValueError('Video belum tersedia dari YouTube.')
    data=dict(row)
    job=conn.execute('SELECT id,status,error,provider,model,updated_at FROM yt_radar_ai_jobs WHERE video_id=? ORDER BY id DESC LIMIT 1',(video_id,)).fetchone()
    data['latest_ai_job']=dict(job) if job else None
    return data

def enqueue_query_plan(conn,province_code,provider,model,fallback=False):
    if province_code not in regional.PROVINCES:raise ValueError('Provinsi tidak valid.')
    if type(fallback) is not bool:raise ValueError('Pengaturan fallback tidak valid.')
    _,_,model=ai_providers.config(provider,model)
    if conn.execute("SELECT count(*) FROM yt_query_plan_jobs WHERE status IN ('queued','running')").fetchone()[0]>=10:raise ValueError('Antrean perencanaan AI penuh.')
    if conn.execute("SELECT 1 FROM yt_query_plan_jobs WHERE province_code=? AND status IN ('queued','running')",(province_code,)).fetchone():raise ValueError('Provinsi sudah memiliki perencanaan AI aktif.')
    stamp=now();ident=conn.execute('INSERT INTO yt_query_plan_jobs(province_code,provider,model,fallback,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                                   (province_code,provider,model,int(fallback),stamp,stamp)).lastrowid
    conn.commit();return {'id':ident,'status':'queued'}

def safe_query_proposals(province_code,values):
    name=regional.PROVINCES[province_code].casefold()
    allowed=set(re.findall(r'\w+',name))|{'merauke' if province_code=='93' else name}
    allowed|={'gangguan','layanan','publik','digital','siber','keamanan','insiden','bencana','keselamatan','masyarakat',
              'infrastruktur','listrik','transportasi','telekomunikasi','jaringan','peringatan','informasi','data','peretasan'}
    issues={'gangguan','siber','keamanan','insiden','bencana','keselamatan','infrastruktur','peretasan','peringatan'}
    result=[]
    for value in values[:4] if isinstance(values,list) else []:
        if not isinstance(value,str) or not 5<=len(value)<=120:continue
        text=' '.join(value.casefold().split());words=re.findall(r'\w+',text)
        if not re.fullmatch(r'[\w\s]+',text) or name not in text or not 2<=len(words)<=14 or not set(words)&issues:continue
        if any(word not in allowed for word in words):continue
        if text not in result:result.append(text)
    return result

def run_query_plan(conn,ident,analyzer=ai_providers.analyze):
    conn.execute('BEGIN IMMEDIATE')
    try:
        job=conn.execute('SELECT * FROM yt_query_plan_jobs WHERE id=?',(ident,)).fetchone()
        if not job or job['status']!='queued':conn.rollback();return {'status':'not_queued'}
        conn.execute("UPDATE yt_query_plan_jobs SET status='running',updated_at=? WHERE id=?",(now(),ident));conn.commit()
    except Exception:conn.rollback();raise
    try:
        name=regional.PROVINCES[job['province_code']]
        output=analyzer(conn,{'provider':job['provider'],'model':job['model'],'fallback':bool(job['fallback']),
          'task':'youtube_query_plan','text':'Provinsi: '+name+'. Fokus: gangguan layanan, keamanan siber, bencana dan keselamatan masyarakat.',
          'use_cache':True})
        proposals=safe_query_proposals(job['province_code'],output.get('claims'))
        conn.executemany('INSERT INTO yt_query_proposals(plan_job_id,province_code,query,provider,model,created_at) VALUES(?,?,?,?,?,?)',
                         [(ident,job['province_code'],query,str(output.get('provider') or '')[:40],str(output.get('model') or '')[:100],now()) for query in proposals])
        conn.execute("UPDATE yt_query_plan_jobs SET status='completed',updated_at=? WHERE id=?",(now(),ident));conn.commit()
        return {'status':'completed','accepted':len(proposals)}
    except Exception as exc:
        conn.rollback();message=str(exc) if isinstance(exc,(ValueError,ai_providers.ProviderError)) else 'Perencanaan AI gagal.'
        conn.execute("UPDATE yt_query_plan_jobs SET status='failed',error=?,updated_at=? WHERE id=?",(message[:500],now(),ident));conn.commit()
        return {'status':'failed','error':message[:500]}

def apply_query_proposal(conn,ident):
    if type(ident) is not int or ident<1:raise ValueError('ID usulan tidak valid.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        proposal=conn.execute("SELECT * FROM yt_query_proposals WHERE id=? AND status='proposed'",(ident,)).fetchone()
        if not proposal:raise ValueError('Usulan tidak ditemukan atau sudah dipakai.')
        if conn.execute('SELECT running FROM yt_radar_settings WHERE id=1').fetchone()[0]:raise ValueError('Tunggu siklus radar selesai sebelum mengganti kueri.')
        if not safe_query_proposals(proposal['province_code'],[proposal['query']]):raise ValueError('Usulan tidak lolos validasi.')
        job=conn.execute('SELECT * FROM yt_radar_jobs WHERE province_code=?',(proposal['province_code'],)).fetchone()
        if job and (job['cursor'] or job['status']=='metadata'):raise ValueError('Selesaikan checkpoint pencarian provinsi sebelum mengganti kueri.')
        if job:conn.execute('UPDATE yt_radar_jobs SET query=?,window_start=\'\',updated_at=? WHERE id=?',(proposal['query'],now(),job['id']))
        else:conn.execute('INSERT INTO yt_radar_jobs(province_code,query,updated_at) VALUES(?,?,?)',(proposal['province_code'],proposal['query'],now()))
        conn.execute("UPDATE yt_query_proposals SET status='approved' WHERE id=?",(ident,));conn.commit()
        return {'id':ident,'status':'approved'}
    except Exception:conn.rollback();raise

def thumbnail_url(video):
    try:thumb=json.loads(video['thumbnails_json'])
    except (ValueError,TypeError):return ''
    if not isinstance(thumb,dict):return ''
    for label in ('high','medium','default','standard','maxres'):
        entry=thumb.get(label)
        url=entry.get('url') if isinstance(entry,dict) else None
        if isinstance(url,str) and re.fullmatch(r'https://i\.ytimg\.com/(?:vi|vi_webp)/'+re.escape(video['video_id'])+r'/[A-Za-z0-9_.-]+',url):return url
    return ''

def enqueue_analysis(conn,video_id,provider,model,fallback=False,mode='metadata'):
    video=get_video(conn,video_id)
    if type(fallback) is not bool: raise ValueError('Pengaturan fallback tidak valid.')
    if mode not in ('metadata','thumbnail'):raise ValueError('Mode analisis tidak dikenal.')
    if mode!='metadata' and video['source_status']!='available':raise ValueError('Sumber video tidak tersedia atau metadata tidak lengkap.')
    if mode=='thumbnail' and provider not in ('muse','meta'):raise ValueError('Analisis thumbnail membutuhkan Muse.')
    if mode!='metadata' and fallback:raise ValueError('Fallback lintas provider belum tersedia untuk media.')
    if mode=='thumbnail' and not thumbnail_url(video):raise ValueError('Thumbnail YouTube yang valid tidak tersedia pada metadata.')
    _,_,model=ai_providers.config(provider,model)
    stamp=now()
    try:
        ident=conn.execute('INSERT INTO yt_radar_ai_jobs(video_id,provider,model,fallback,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                           (video_id,provider,model,int(fallback),mode,stamp,stamp)).lastrowid
        conn.commit();return {'id':ident,'status':'queued'}
    except sqlite3.IntegrityError as exc:
        conn.rollback();raise ValueError('Video sudah memiliki pekerjaan AI aktif.') from exc

def run_analysis(conn,ident,analyzer=ai_providers.analyze):
    conn.execute('BEGIN IMMEDIATE')
    try:
        job=conn.execute('SELECT * FROM yt_radar_ai_jobs WHERE id=?',(ident,)).fetchone()
        if not job or job['status']!='queued':conn.rollback();return {'status':'not_queued'}
        video=get_video(conn,job['video_id'])
        fingerprint=hashlib.sha256((video['title']+'\n'+video['description']+'\n'+video['thumbnails_json']).encode()).hexdigest()
        conn.execute("UPDATE yt_radar_ai_jobs SET status='running',updated_at=? WHERE id=?",(now(),ident));conn.commit()
    except Exception: conn.rollback();raise
    try:
        source=('Judul video: '+video['title']+'\nDeskripsi: '+video['description'])[:2500]
        task={'metadata':'content','thumbnail':'youtube_thumbnail'}[job['mode']]
        payload={'provider':job['provider'],'model':job['model'],'fallback':bool(job['fallback']),
            'task':task,'text':source,'source_url':'https://www.youtube.com/watch?v='+video['video_id'],'use_cache':True}
        if job['mode']=='thumbnail':payload['thumbnail_url']=thumbnail_url(video)
        output=analyzer(conn,payload)
        conn.execute('BEGIN IMMEDIATE')
        fresh=get_video(conn,video['video_id'])
        current=hashlib.sha256((fresh['title']+'\n'+fresh['description']+'\n'+fresh['thumbnails_json']).encode()).hexdigest()
        if current!=fingerprint:
            conn.execute("UPDATE yt_radar_ai_jobs SET status='stale',error='Metadata berubah selama analisis; hasil tidak dipakai.',updated_at=? WHERE id=?",(now(),ident));conn.commit();return {'status':'stale'}
        fields=['title','description']+([] if job['mode']=='metadata' else [job['mode']+'_url'])
        result={'result':output,'metadata_sha256':fingerprint,'fields_inspected':fields,
                'limitations':['URL media dikirim ke provider; pemrosesan bergantung kemampuan dan akses model.','Lokasi dan klaim memerlukan review manusia.']}
        encoded=json.dumps(result,ensure_ascii=False)
        conn.execute('INSERT INTO yt_radar_ai_results(job_id,video_id,mode,metadata_sha256,result_json,created_at) VALUES(?,?,?,?,?,?)',
                     (ident,video['video_id'],job['mode'],fingerprint,encoded,now()))
        conn.execute('UPDATE yt_radar_videos SET analysis_json=? WHERE video_id=?',(encoded,video['video_id']))
        conn.execute("UPDATE yt_radar_ai_jobs SET status='completed',error='',updated_at=? WHERE id=?",(now(),ident));conn.commit()
        return {'status':'completed'}
    except Exception as exc:
        conn.rollback()
        message=str(exc) if isinstance(exc,(ValueError,ai_providers.ProviderError)) else 'Penyimpanan analisis gagal.'
        conn.execute("UPDATE yt_radar_ai_jobs SET status='failed',error=?,updated_at=? WHERE id=?",(message[:500],now(),ident));conn.commit()
        return {'status':'failed','error':message[:500]}

def review(conn,video_id,decision,note):
    if decision not in ('RELEVANT','NOT_RELEVANT','NEEDS_REVIEW'): raise ValueError('Keputusan review tidak dikenal.')
    if not isinstance(note,str) or not 15<=len(note.strip())<=1000: raise ValueError('Alasan review harus 15–1000 karakter.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        video=get_video(conn,video_id)
        fingerprint=hashlib.sha256((video['title']+'\n'+video['description']).encode()).hexdigest()
        conn.execute('UPDATE yt_radar_videos SET relevance_status=? WHERE video_id=?',(decision,video_id))
        conn.execute('INSERT INTO yt_radar_reviews(video_id,decision,note,metadata_sha256,reviewed_at) VALUES(?,?,?,?,?)',
                     (video_id,decision,note.strip(),fingerprint,now()));conn.commit()
    except Exception:conn.rollback();raise
    return {'video_id':video_id,'relevance_status':decision,'verified':False}

def link_location(conn,video_id,province_code,quote,confirmed):
    if province_code not in regional.PROVINCES: raise ValueError('Provinsi tidak valid.')
    if confirmed is not True: raise ValueError('Konfirmasi bahwa lokasi merujuk pada kejadian diperlukan.')
    if not isinstance(quote,str) or not 3<=len(quote.strip())<=160: raise ValueError('Kutipan lokasi harus 3–160 karakter.')
    quote=quote.strip()
    match=regional.resolve_location(quote)
    if match['province_code']!=province_code: raise ValueError('Kutipan tidak cocok atau ambigu dengan referensi provinsi.')
    conn.execute('BEGIN IMMEDIATE')
    try:
        video=get_video(conn,video_id)
        if video['relevance_status']!='RELEVANT': raise ValueError('Tinjau relevansi video sebelum menautkan lokasi.')
        if quote.casefold() not in (video['title']+'\n'+video['description']).casefold(): raise ValueError('Kutipan tidak ditemukan pada metadata video.')
        fingerprint=hashlib.sha256((video['title']+'\n'+video['description']).encode()).hexdigest()
        conn.execute('''INSERT INTO yt_radar_video_locations(video_id,province_code,quote,confidence,metadata_sha256,updated_at)
          VALUES(?,?,?,'SUPPORTED_LOCATION',?,?) ON CONFLICT(video_id) DO UPDATE SET province_code=excluded.province_code,
          quote=excluded.quote,confidence=excluded.confidence,metadata_sha256=excluded.metadata_sha256,updated_at=excluded.updated_at''',
          (video_id,province_code,quote,fingerprint,now()));conn.commit()
    except Exception:conn.rollback();raise
    return {'video_id':video_id,'province_code':province_code,'confidence':'SUPPORTED_LOCATION','verified':False}

def regional_videos(conn,province_code=''):
    if province_code and province_code not in regional.PROVINCES: raise ValueError('Provinsi tidak valid.')
    rows=conn.execute('''SELECT v.video_id,v.title,v.description,v.channel_title,v.published_at,v.last_observed,v.thumbnails_json,v.analysis_json,
      l.province_code,l.quote,l.confidence,l.metadata_sha256 FROM yt_radar_videos v JOIN yt_radar_video_locations l ON l.video_id=v.video_id
      WHERE v.relevance_status='RELEVANT' AND v.source_status='available' AND (?='' OR l.province_code=?) ORDER BY v.last_observed DESC LIMIT 500''',(province_code,province_code)).fetchall()
    out=[]
    for row in rows:
        item=dict(row);fingerprint=hashlib.sha256((item['title']+'\n'+item['description']).encode()).hexdigest()
        if fingerprint!=item.pop('metadata_sha256'): continue
        current_ai_hash=hashlib.sha256((item['title']+'\n'+item['description']+'\n'+item['thumbnails_json']).encode()).hexdigest()
        try:
            analysis=json.loads(item.pop('analysis_json') or '{}')
            result=analysis.get('result',{}) if analysis.get('metadata_sha256')==current_ai_hash else {}
            item['ai_summary']=str(result.get('explanation') or '')[:2000]
            item['ai_category']=str(result.get('category') or '')[:40]
        except (ValueError,TypeError,AttributeError):item['ai_summary']='';item['ai_category']=''
        item['thumbnail_url']=thumbnail_url(item);item.pop('thumbnails_json');item.pop('description');out.append(item)
    return {'videos':out,'limited_to':500,'warning':'Lokasi didukung kutipan metadata dan konfirmasi analis; bukan lokasi terverifikasi.'}

def run_reply_page(conn,ident,key,store_item,fetcher=request_api,clock=None):
    clock=int(time.time()) if clock is None else int(clock)
    conn.execute('BEGIN IMMEDIATE')
    try:
        job=conn.execute('SELECT * FROM yt_radar_reply_jobs WHERE id=?',(ident,)).fetchone()
        if not job or job['status']!='queued':conn.rollback();return {'status':'not_queued'}
        conn.execute("UPDATE yt_radar_reply_jobs SET status='running',updated_at=? WHERE id=?",(now(),ident));conn.commit()
    except Exception:conn.rollback();raise
    try:
        if not key or key.startswith('ISI_KEY_'):raise APIError('YOUTUBE_API_KEY belum dikonfigurasi.','not_configured')
        current_video=get_video(conn,job['video_id'])
        if current_video['relevance_status']!='RELEVANT' or current_video['source_status']!='available':raise APIError('Relevansi atau ketersediaan video berubah; tinjau sebelum melanjutkan.')
        _reserve(conn,'replies',clock)
        params={'part':'snippet','parentId':job['parent_id'],'maxResults':100,'textFormat':'plainText'}
        if job['cursor']:params['pageToken']=job['cursor']
        data=fetcher('comments',params,key);items=data.get('items');token=data.get('nextPageToken')
        if not isinstance(items,list) or len(items)>100:raise APIError('Respons balasan YouTube tidak valid.')
        if token is not None and (not isinstance(token,str) or not token or len(token)>1000):raise APIError('Cursor balasan tidak valid.')
        conn.execute('BEGIN IMMEDIATE')
        current_video=get_video(conn,job['video_id'])
        if current_video['relevance_status']!='RELEVANT' or current_video['source_status']!='available':raise APIError('Relevansi atau ketersediaan video berubah; halaman balasan tidak disimpan.')
        if token and (token==job['cursor'] or conn.execute('SELECT 1 FROM yt_radar_reply_cursors WHERE reply_job_id=? AND cursor=?',(ident,token)).fetchone()):raise APIError('Cursor balasan berulang.')
        count=0
        for item in items:
            if not isinstance(item,dict) or not isinstance(item.get('snippet'),dict):continue
            snippet=item['snippet'];cid=item.get('id')
            if not isinstance(cid,str) or not 1<=len(cid)<=200 or snippet.get('parentId')!=job['parent_id'] or not snippet.get('publishedAt'):continue
            wrapper={'snippet':{'topLevelComment':item}}
            if store_item(conn,job['video_id'],wrapper):
                conn.execute("INSERT OR IGNORE INTO collection_job_items(job_id,kind,source_ref,observed_at) VALUES(?,'comment',?,?)",(job['job_id'],cid,now()))
                conn.execute('INSERT OR IGNORE INTO yt_radar_reply_links(reply_job_id,comment_id,parent_id) VALUES(?,?,?)',(ident,cid,job['parent_id']))
                count+=1
        if token:conn.execute('INSERT INTO yt_radar_reply_cursors(reply_job_id,cursor) VALUES(?,?)',(ident,token))
        status='completed' if not token else 'paused' if job['session_pages']+1>=20 else 'queued'
        error='Batas 20 halaman sesi tercapai; lanjutkan secara manual.' if status=='paused' else ''
        conn.execute('UPDATE yt_radar_reply_jobs SET cursor=?,status=?,pages_count=pages_count+1,session_pages=session_pages+1,retrieved_count=retrieved_count+?,error=?,updated_at=? WHERE id=?',
                     (token,status,count,error,now(),ident));conn.commit();return {'status':status,'received':count}
    except Exception as exc:
        conn.rollback()
        status='rate_limited' if isinstance(exc,APIError) and exc.status=='limited_or_forbidden' else 'paused' if isinstance(exc,APIError) and exc.status in ('local_quota','not_configured') else 'failed'
        message=str(exc) if isinstance(exc,APIError) else 'Pengambilan balasan gagal; checkpoint dipertahankan.'
        conn.execute('UPDATE yt_radar_reply_jobs SET status=?,error=?,updated_at=? WHERE id=?',(status,message[:500],now(),ident));conn.commit()
        return {'status':status,'error':message[:500]}

def resume_reply(conn,ident):
    if isinstance(ident,bool):raise ValueError('ID pekerjaan balasan tidak valid.')
    try:ident=int(ident)
    except (TypeError,ValueError,OverflowError) as exc:raise ValueError('ID pekerjaan balasan tidak valid.') from exc
    cur=conn.execute("UPDATE yt_radar_reply_jobs SET status='queued',session_pages=0,error='',updated_at=? WHERE id=? AND status IN ('paused','failed','rate_limited')",(now(),ident))
    if not cur.rowcount:raise ValueError('Pekerjaan balasan tidak dapat dilanjutkan.')
    conn.commit();return {'id':ident,'status':'queued'}

def investigate(conn,video_id):
    conn.execute('BEGIN IMMEDIATE')
    try:
        video=get_video(conn,video_id)
        existing=conn.execute('SELECT case_id FROM yt_radar_cases WHERE video_id=?',(video_id,)).fetchone()
        if existing: conn.commit();return {'id':existing[0],'existing':True}
        stamp=now();title=('YouTube: '+video['title'])[:160]
        cid=conn.execute("INSERT INTO intel_investigation_cases(title,summary,dataset,created_at,updated_at) VALUES(?,?,'real',?,?)",
                         (title,'Metadata video publik; konten video dan klaim perlu ditinjau manusia.',stamp,stamp)).lastrowid
        investigation._event(conn,cid,'case_created','Kasus video dibuat; isi dan konteks belum diverifikasi.')
        body=('Judul: '+video['title']+'\nDeskripsi: '+video['description'][:2500]+'\nChannel ID: '+video['channel_id']+
              '\nPublikasi: '+video['published_at']+'\nMetadata diambil: '+video['last_observed'])
        if video['analysis_json']:
            try:
                analysis=json.loads(video['analysis_json']).get('result',{})
                body+='\nAnalisis kandidat: '+str(analysis.get('explanation') or '')[:700]+'\nProvider/model: '+str(analysis.get('provider') or '')+'/'+str(analysis.get('model') or '')+'\nWaktu analisis: '+str(analysis.get('timestamp') or '')
            except (ValueError,TypeError,AttributeError):pass
        body=body[:4000]
        source=investigation._source(conn,'reference',None,url='https://www.youtube.com/watch?v='+video_id,
                                    body=body,title=(video['channel_title'] or 'YouTube video')[:200] if len(video['channel_title'])>=4 else 'YouTube video')
        source.update(provenance='api',platform='youtube',published_at=video['published_at'])
        investigation._add_item(conn,cid,source)
        conn.execute('INSERT INTO yt_radar_cases(video_id,case_id) VALUES(?,?)',(video_id,cid));conn.commit()
        return {'id':cid,'existing':False}
    except Exception:conn.rollback();raise

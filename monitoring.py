"""CAKRA v0.5: opt-in scheduled public-API scans, cautious alerts, manual response log.

No login, automated social publishing, or inference of account ownership.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from statistics import median

import intelligence

AUTO_SCAN_DAILY_CAP = 12  # Global background scans, not including deliberate manual scans.
MAX_TOPICS_PER_CYCLE = 3


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _parsed(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def init_tables(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS monitor_config (
        id INTEGER PRIMARY KEY CHECK(id=1),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
        interval_minutes INTEGER NOT NULL DEFAULT 120 CHECK(interval_minutes BETWEEN 60 AND 1440),
        updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS monitor_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER REFERENCES intel_topics(id) ON DELETE SET NULL,
        alert_type TEXT NOT NULL CHECK(alert_type IN ('new_matches_spike','similar_public_narrative','demo')),
        title TEXT NOT NULL,
        explanation TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'review' CHECK(level IN ('review','notice')),
        provenance TEXT NOT NULL CHECK(provenance IN ('api','demo')),
        evidence_json TEXT NOT NULL DEFAULT '[]',
        signal_json TEXT NOT NULL DEFAULT '{}',
        fingerprint TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','dismissed')),
        created_at TEXT NOT NULL,
        reviewed_at TEXT
      );
      CREATE INDEX IF NOT EXISTS idx_monitor_alerts_status ON monitor_alerts(status,created_at);
      CREATE TABLE IF NOT EXISTS monitor_scan_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER,
        mode TEXT NOT NULL CHECK(mode IN ('manual','scheduled')),
        status TEXT NOT NULL CHECK(status IN ('success','error')),
        started_at TEXT NOT NULL,
        summary TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_monitor_scan_audit_mode ON monitor_scan_audit(mode,started_at);
      CREATE TABLE IF NOT EXISTS response_publication_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        response_id INTEGER NOT NULL UNIQUE REFERENCES fact_responses(id),
        platform TEXT NOT NULL,
        publication_url TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        reported_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'reported' CHECK(status IN ('reported','withdrawn'))
      );
      INSERT OR IGNORE INTO monitor_config(id,enabled,interval_minutes,updated_at)
      VALUES(1,0,120,'initial');
    ''')
    conn.execute('PRAGMA foreign_keys=ON')


def config(conn):
    return dict(conn.execute('SELECT enabled,interval_minutes,updated_at FROM monitor_config WHERE id=1').fetchone())


def update_config(conn, enabled, interval_minutes):
    if not isinstance(enabled, bool):
        raise ValueError('Status monitoring harus true atau false.')
    if isinstance(interval_minutes, bool):
        raise ValueError('Interval tidak valid.')
    try:
        interval = int(interval_minutes)
    except (ValueError,TypeError,OverflowError) as exc:
        raise ValueError('Interval tidak valid.') from exc
    if interval not in (60,120,240,360,720,1440):
        raise ValueError('Pilih interval 60, 120, 240, 360, 720, atau 1440 menit.')
    conn.execute('UPDATE monitor_config SET enabled=?,interval_minutes=?,updated_at=? WHERE id=1',
                 (int(enabled),interval,now()))
    conn.commit()
    return config(conn)


def _save_alert(conn, *, topic_id, kind, title, explanation, evidence, signals, fingerprint,
                provenance='api', level='review'):
    cursor = conn.execute('''INSERT OR IGNORE INTO monitor_alerts
      (topic_id,alert_type,title,explanation,level,provenance,evidence_json,signal_json,fingerprint,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?)''',
      (topic_id,kind,title,explanation,level,provenance,json.dumps(evidence,ensure_ascii=False),
       json.dumps(signals,ensure_ascii=False),fingerprint,now()))
    return cursor.rowcount


def _new_match_spike(conn, topic_id, prior_runs, scan_result):
    # This is about API result *sampling*, not platform-wide popularity.
    history = [int(r['new_count']) for r in prior_runs[:6]]
    current = int(scan_result['new_for_topic'])
    if len(history) < 3 or current < 5:
        return 0
    typical = median(history)
    if current < max(5, typical * 3):
        return 0
    topic = conn.execute('SELECT keyword,platform FROM intel_topics WHERE id=?',(topic_id,)).fetchone()
    recent = [dict(row) for row in conn.execute('''SELECT o.source_url FROM intel_observations o
      JOIN intel_topic_observations x ON x.observation_id=o.id
      WHERE x.topic_id=? AND o.provenance='api'
      ORDER BY o.collected_at DESC,o.id DESC LIMIT 6''',(topic_id,))]
    # Daily dedup: repeated sampling cannot flood alert queue.
    day = now()[:10]
    return _save_alert(conn,topic_id=topic_id,kind='new_matches_spike',
        title=f'Peningkatan hasil baru terambil: {topic["keyword"]}',
        explanation='Jumlah hasil baru pada sampel pencarian API meningkat dibanding riwayat. '
                    'Bukan pengukuran total aktivitas, kebenaran konten, atau indikasi bot.',
        evidence=[x['source_url'] for x in recent],
        signals={'new_matches':current,'baseline_median_new_matches':typical,
                 'previous_scan_count':len(history),'source':'sampled_official_api'},
        fingerprint=f'spike:{topic_id}:{day}',level='notice')


def _narrative_signal(conn, topic_id):
    # Only API observations with actual public author IDs, never manual/demo leads.
    rows = [dict(x) for x in conn.execute('''SELECT o.* FROM intel_observations o
      JOIN intel_topic_observations x ON x.observation_id=o.id
      WHERE x.topic_id=? AND o.provenance='api' AND o.author_id<>''
      ORDER BY o.id DESC LIMIT 50''',(topic_id,))]
    if len(rows) < 3:
        return 0
    def norm(s):
        text = re.sub(r'https?://\S+', '', (s or '').lower())
        return ' '.join(re.sub(r'[^\w\s]',' ',text).split())[:350]
    # Cheap exact normalized content test: stricter but interpretable.
    groups = {}
    for entry in rows:
        normalized = norm(entry['body'])
        if len(normalized) >= 55:
            groups.setdefault(normalized, []).append(entry)
    for normalized, members in groups.items():
        authors = {p['author_id'] for p in members}
        times = [_parsed(p['published_at']) for p in members]
        if len(authors)<3 or any(x is None for x in times):
            continue
        if (max(times)-min(times)).total_seconds()>3600:
            continue
        # To avoid recency deception, only flag when publication within recent 2 days.
        right_now=datetime.now(timezone.utc)
        if max(times)<right_now-timedelta(days=2) or max(times)>right_now+timedelta(minutes=5):
            continue
        topic=conn.execute('SELECT keyword FROM intel_topics WHERE id=?',(topic_id,)).fetchone()
        import hashlib
        fingerprint='similar:'+str(topic_id)+':'+hashlib.sha256(normalized.encode()).hexdigest()[:20]
        return _save_alert(conn,topic_id=topic_id,kind='similar_public_narrative',
          title=f'Pola teks identik antar akun: {topic["keyword"]}',
          explanation='Minimal tiga ID akun publik berbeda memublikasikan teks ternormalisasi identik dalam satu jam. '
                      'Kemiripan ini belum membuktikan bot, click farm, atau koordinasi tidak sah.',
          evidence=[m['source_url'] for m in members[:8]],
          signals={'observed_accounts':len(authors),'observed_items':len(members),
                   'time_span_minutes':round((max(times)-min(times)).total_seconds()/60,1),
                   'test':'normalized_exact_text'}, fingerprint=fingerprint)
    return 0


def scan_and_analyze(conn, topic_id, *, mode='manual', fetcher=None, max_items=20):
    if mode not in ('manual','scheduled'):
        raise ValueError('Mode pemindaian tidak valid.')
    topic=conn.execute('SELECT id FROM intel_topics WHERE id=?',(int(topic_id),)).fetchone()
    if not topic:
        raise ValueError('Topik tidak ditemukan.')
    history=list(conn.execute('SELECT new_count FROM intel_runs WHERE topic_id=? ORDER BY id DESC LIMIT 6',
                              (topic_id,)))
    started=now()
    try:
        if fetcher is None:
            result=intelligence.scan_topic(conn,topic_id,limit=max_items)
        else:
            result=intelligence.scan_topic(conn,topic_id,limit=max_items,fetcher=fetcher)
        created=_new_match_spike(conn,topic_id,history,result)+_narrative_signal(conn,topic_id)
        conn.execute('INSERT INTO monitor_scan_audit(topic_id,mode,status,started_at,summary) VALUES(?,?,?,?,?)',
                     (topic_id,mode,'success',started,
                      f'{result["fetched"]} hasil; {result["new_for_topic"]} tautan baru; {created} peringatan baru'))
        conn.commit()
        return {**result,'new_alerts':created,'mode':mode}
    except (ValueError,RuntimeError,sqlite3.Error) as exc:
        conn.execute('INSERT INTO monitor_scan_audit(topic_id,mode,status,started_at,summary) VALUES(?,?,?,?,?)',
                     (topic_id,mode,'error',started,str(exc)[:300]))
        conn.commit()
        raise


def run_due(conn, *, fetcher=None, current_time=None):
    settings=config(conn)
    if not settings['enabled']:
        return {'status':'disabled','scanned':0}
    current_time=current_time or datetime.now(timezone.utc)
    today=current_time.astimezone(timezone.utc).date().isoformat()
    used=conn.execute('''SELECT COUNT(*) AS c FROM monitor_scan_audit
      WHERE mode='scheduled' AND started_at>=?''',(today+'T00:00:00',)).fetchone()['c']
    if used >= AUTO_SCAN_DAILY_CAP:
        return {'status':'daily_cap','scanned':0}
    topics=conn.execute('''SELECT t.id,t.platform,
      (SELECT MAX(checked_at) FROM intel_runs r WHERE r.topic_id=t.id) AS last_scan,
      (SELECT MAX(started_at) FROM monitor_scan_audit a WHERE a.topic_id=t.id) AS last_attempt
      FROM intel_topics t WHERE t.keyword!='CAKRA DEMO' ORDER BY t.id''').fetchall()
    done=0
    for topic in topics:
        if done>=min(MAX_TOPICS_PER_CYCLE,AUTO_SCAN_DAILY_CAP-used):
            break
        latest=max((dt for dt in (_parsed(topic['last_scan']),_parsed(topic['last_attempt'])) if dt),default=None)
        if latest and (current_time-latest).total_seconds()<settings['interval_minutes']*60:
            continue
        try:
            scan_and_analyze(conn,topic['id'],mode='scheduled',fetcher=fetcher,max_items=20)
        except (ValueError, RuntimeError,sqlite3.Error):
            pass  # auditable failure; next attempt respects interval
        done+=1
    return {'status':'checked','scanned':done,'daily_remaining':max(0,AUTO_SCAN_DAILY_CAP-used-done)}


def set_alert_status(conn, alert_id, status):
    if status not in ('open','acknowledged','dismissed'):
        raise ValueError('Status peringatan tidak valid.')
    row=conn.execute('SELECT id FROM monitor_alerts WHERE id=?',(int(alert_id),)).fetchone()
    if not row:
        raise ValueError('Peringatan tidak ditemukan.')
    conn.execute('UPDATE monitor_alerts SET status=?,reviewed_at=? WHERE id=?',
                 (status,now() if status!='open' else None,alert_id))
    conn.commit()
    return {'id':int(alert_id),'status':status}


def demo_alert(conn):
    token = 'CAKRA-V05-DEMO-STATIC'
    created=_save_alert(conn,topic_id=None,kind='demo',
        title='SIMULASI: aktivitas konten serupa',
        explanation='Peringatan fiktif untuk menguji UI. Tidak ada akun atau peristiwa nyata yang dituduh.',
        evidence=[],signals={'dataset':'demo','example_count':7},fingerprint=token,
        provenance='demo',level='notice')
    conn.commit()
    return {'created':bool(created),'provenance':'demo'}


def log_publication(conn,response_id,platform,url,note=''):
    # This is a manual record only; we do NOT use platform APIs to post or verify the URL.
    response=conn.execute('''SELECT r.*,cm.dataset,c.verdict,c.reviewed_at
      FROM fact_responses r
      JOIN fact_cases c ON c.id=r.case_id
      JOIN comments cm ON cm.comment_id=c.comment_id
      WHERE r.id=?''',(int(response_id),)).fetchone()
    if not response or response['status']!='approved' or not response['reviewed_at']:
        raise ValueError('Respons belum disetujui analis.')
    if response['dataset']!='real':
        raise ValueError('Respons dari komentar SIMULASI tidak dapat dicatat sebagai publikasi nyata.')
    if response['verdict'] not in ('supported','contradicted','mixed','insufficient'):
        raise ValueError('Kasus belum direview.')
    # Keep reference to a platform URL, not a claim that the publication was checked by us.
    url = intelligence.valid_observation_link(platform,url)
    note=str(note or '').strip()
    if len(note)>500:
        raise ValueError('Catatan maksimum 500 karakter.')
    try:
        cur=conn.execute('''INSERT INTO response_publication_log
          (response_id,platform,publication_url,note,reported_at) VALUES(?,?,?,?,?)''',
          (response_id,platform,url,note,now()))
    except sqlite3.IntegrityError as exc:
        raise ValueError('Respons ini sudah memiliki catatan publikasi. Jangan catat dua kali.') from exc
    conn.commit()
    return {'id':cur.lastrowid,'response_id':int(response_id),'status':'reported',
            'note':'Laporan manual oleh operator; CAKRA tidak memverifikasi bahwa posting benar-benar diterbitkan.'}


def withdraw_publication(conn,log_id):
    cur=conn.execute('UPDATE response_publication_log SET status=? WHERE id=?',('withdrawn',int(log_id)))
    if not cur.rowcount:
        raise ValueError('Catatan publikasi tidak ditemukan.')
    conn.commit()
    return {'id':int(log_id),'status':'withdrawn'}


def dashboard(conn):
    import unified_fact
    a=[dict(row) for row in conn.execute('''SELECT m.*,t.keyword,t.platform AS topic_platform
      FROM monitor_alerts m LEFT JOIN intel_topics t ON t.id=m.topic_id
      ORDER BY CASE m.status WHEN 'open' THEN 0 ELSE 1 END,m.id DESC LIMIT 100''')]
    for item in a:
        item['evidence']=json.loads(item.pop('evidence_json'))
        item['signals']=json.loads(item.pop('signal_json'))
    audits=[dict(row) for row in conn.execute('''SELECT a.*,t.keyword,t.platform
      FROM monitor_scan_audit a LEFT JOIN intel_topics t ON t.id=a.topic_id
      ORDER BY a.id DESC LIMIT 30''')]
    approved=[dict(row) for row in conn.execute('''SELECT r.id,r.case_id,r.body,r.approved_at,
      c.claim,c.verdict,cm.source_url,cm.dataset
      FROM fact_responses r JOIN fact_cases c ON c.id=r.case_id
      JOIN comments cm ON cm.comment_id=c.comment_id
      WHERE r.status='approved' ORDER BY r.id DESC LIMIT 60''')]
    logs=[dict(row) for row in conn.execute('''SELECT l.*,r.body FROM response_publication_log l
      JOIN fact_responses r ON r.id=l.response_id ORDER BY l.id DESC LIMIT 60''')]
    today=datetime.now(timezone.utc).date().isoformat()
    uses=conn.execute("SELECT COUNT(*) AS c FROM monitor_scan_audit WHERE mode='scheduled' AND started_at>=?",
                      (today+'T00:00:00',)).fetchone()['c']
    return {'config':config(conn),'alerts':a,'audits':audits,'approved_responses':approved,
            'publication_logs':logs,'unified_approved':unified_fact.approved_responses(conn),
            'stats':{'open':sum(x['status']=='open' for x in a),'total_alerts':len(a),
                     'scheduled_scans_today':uses,'scheduled_daily_cap':AUTO_SCAN_DAILY_CAP},
            'notice':'Peringatan adalah sinyal berbasis sampel API, bukan kepastian bot/hoaks. Publikasi manual tidak diperiksa ulang oleh CAKRA.'}

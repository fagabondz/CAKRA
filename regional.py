"""Regional information model and bounded public-source adapter for CAKRA."""
from __future__ import annotations
import hashlib, json, math, re, sqlite3, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
import factcheck
import investigation

BASE = Path(__file__).resolve().parent
MAP_PATH = BASE / 'data' / 'indonesia-provinces.geojson'
MAP_URL = 'https://raw.githubusercontent.com/denyherianto/indonesia-geojson-topojson-maps-with-38-provinces/main/GeoJSON/indonesia-38-provinces.geojson'
PROVINCES = dict([('11','Aceh'),('12','Sumatera Utara'),('13','Sumatera Barat'),('14','Riau'),('15','Jambi'),('16','Sumatera Selatan'),('17','Bengkulu'),('18','Lampung'),('19','Kepulauan Bangka Belitung'),('21','Kepulauan Riau'),('31','DKI Jakarta'),('32','Jawa Barat'),('33','Jawa Tengah'),('34','DI Yogyakarta'),('35','Jawa Timur'),('36','Banten'),('51','Bali'),('52','Nusa Tenggara Barat'),('53','Nusa Tenggara Timur'),('61','Kalimantan Barat'),('62','Kalimantan Tengah'),('63','Kalimantan Selatan'),('64','Kalimantan Timur'),('65','Kalimantan Utara'),('71','Sulawesi Utara'),('72','Sulawesi Tengah'),('73','Sulawesi Selatan'),('74','Sulawesi Tenggara'),('75','Gorontalo'),('76','Sulawesi Barat'),('81','Maluku'),('82','Maluku Utara'),('91','Papua'),('92','Papua Barat'),('93','Papua Selatan'),('94','Papua Tengah'),('95','Papua Pegunungan'),('96','Papua Barat Daya')])
CATEGORIES = {'cyber':'Keamanan siber','infrastructure':'Infrastruktur kritis','integrity':'Integritas informasi','safety':'Keselamatan publik'}
# Conservative matching of explicit regency names only. Expand using a reviewed reference.
REGENCIES = {'merauke':'93'}

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def norm(s): return re.sub(r'[^a-z0-9]+',' ',str(s or '').casefold()).strip()

def resolve_location(text):
    text = norm(text)
    hits = set()
    occupied=[]
    for code,name in sorted(PROVINCES.items(), key=lambda item: len(item[1]), reverse=True):
        term = norm(name)
        if code == '91': continue # Papua can mean the island, not just the province.
        for match in re.finditer(r'(?<!\w)'+re.escape(term)+r'(?!\w)',text):
            if any(match.start() < end and match.end() > start for start,end in occupied): continue
            hits.add(code); occupied.append(match.span())
    for place,code in REGENCIES.items():
        if re.search(r'(?<!\w)'+re.escape(place)+r'(?!\w)',text): hits.add(code)
    if len(hits)==1:
        code=hits.pop(); return {'province_code':code,'confidence':'text_inference','basis':'Nama wilayah eksplisit dalam teks sumber; perlu review'}
    return {'province_code':None,'confidence':'ambiguous' if len(hits)>1 or re.search(r'\bpapua\b',text) else 'unknown','basis':'Lokasi belum terverifikasi'}

def normalize_map(raw):
    if raw.get('type') != 'FeatureCollection' or not isinstance(raw.get('features'),list): raise ValueError('Bukan GeoJSON FeatureCollection.')
    byname = {norm(name):code for code,name in PROVINCES.items()}
    byname.update({'daerah khusus ibukota jakarta':'31','dki jakarta':'31','daerah istimewa yogyakarta':'34','di yogyakarta':'34','kep bangka belitung':'19','kep riau':'21'})
    features=[]; seen=set()
    for item in raw['features']:
        props=item.get('properties') or {}; name=norm(props.get('PROVINSI') or props.get('name'))
        code=byname.get(name)
        if not code or code in seen: raise ValueError('Nama wilayah tidak dikenal atau duplikat: '+name)
        geom=item.get('geometry') or {}
        if geom.get('type') not in ('Polygon','MultiPolygon') or not geom.get('coordinates'): raise ValueError('Geometri tidak valid: '+name)
        polygons = [geom['coordinates']] if geom['type']=='Polygon' else geom['coordinates']
        for polygon in polygons:
            if not isinstance(polygon,list) or not polygon: raise ValueError('Polygon kosong: '+name)
            for ring in polygon:
                if not isinstance(ring,list) or len(ring)<4 or ring[0][:2]!=ring[-1][:2]: raise ValueError('Ring tidak tertutup: '+name)
                for point in ring:
                    if not isinstance(point,list) or len(point)<2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in point[:2]) or not (-180<=point[0]<=180 and -90<=point[1]<=90): raise ValueError('Koordinat tidak valid: '+name)
        seen.add(code); features.append({'type':'Feature','id':code,'properties':{'code':code,'name':PROVINCES[code]},'geometry':geom})
    if seen != set(PROVINCES): raise ValueError('GeoJSON harus memuat tepat 38 provinsi.')
    return {'type':'FeatureCollection','features':features}

def download_map():
    req=urllib.request.Request(MAP_URL,headers={'User-Agent':'CAKRA/1.0'})
    with urllib.request.urlopen(req,timeout=25) as r:
        content=r.read(4_000_001)
    if len(content)>4_000_000: raise ValueError('Dataset peta terlalu besar.')
    result=normalize_map(json.loads(content))
    MAP_PATH.parent.mkdir(exist_ok=True)
    temp=MAP_PATH.with_suffix('.tmp'); temp.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf-8');temp.replace(MAP_PATH)
    return len(result['features'])

def init_tables(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS regional_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, dataset TEXT NOT NULL CHECK(dataset IN ('real','demo')),
      title TEXT NOT NULL, summary TEXT NOT NULL, category TEXT NOT NULL, province_code TEXT,
      location_text TEXT NOT NULL, location_confidence TEXT NOT NULL, location_basis TEXT NOT NULL,
      source_name TEXT NOT NULL, source_url TEXT NOT NULL, source_type TEXT NOT NULL, verification TEXT NOT NULL,
      event_at TEXT NOT NULL DEFAULT '', published_at TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL,
      UNIQUE(dataset,source_key)
    );
    CREATE INDEX IF NOT EXISTS idx_regional_scope ON regional_events(dataset,province_code,category,event_at);
    CREATE TABLE IF NOT EXISTS regional_case_links(event_id INTEGER PRIMARY KEY REFERENCES regional_events(id), case_id INTEGER NOT NULL REFERENCES intel_investigation_cases(id));
    CREATE TABLE IF NOT EXISTS regional_screen_links(event_id INTEGER PRIMARY KEY REFERENCES regional_events(id), result_id INTEGER NOT NULL UNIQUE REFERENCES screening_results(id));
    CREATE TABLE IF NOT EXISTS regional_collection_log(id INTEGER PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, collected_at TEXT NOT NULL);
    ''')

def upsert(conn, event):
    required=('source_key','dataset','title','summary','category','location_text','location_confidence','location_basis','source_name','source_url','source_type','verification','observed_at')
    if any(not event.get(k) for k in required): raise ValueError('Metadata kejadian tidak lengkap.')
    if event['category'] not in CATEGORIES or event['dataset'] not in ('real','demo'): raise ValueError('Kategori atau dataset tidak valid.')
    if event.get('province_code') and event['province_code'] not in PROVINCES: raise ValueError('Provinsi tidak dikenal.')
    if not factcheck.public_evidence_url(event['source_url']): raise ValueError('URL sumber harus HTTPS publik.')
    fields=('source_key','dataset','title','summary','category','province_code','location_text','location_confidence','location_basis','source_name','source_url','source_type','verification','event_at','published_at','observed_at')
    vals=[event.get(k,'') if k!='province_code' else event.get(k) for k in fields]
    conn.execute('INSERT INTO regional_events ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+') ON CONFLICT(dataset,source_key) DO UPDATE SET title=excluded.title,summary=excluded.summary,category=excluded.category,province_code=excluded.province_code,location_text=excluded.location_text,location_confidence=excluded.location_confidence,location_basis=excluded.location_basis,event_at=excluded.event_at,published_at=excluded.published_at,observed_at=excluded.observed_at',vals)

def list_events(conn, dataset='real', province='', category='', days='7', verification='', source=''):
    if dataset != 'real': raise ValueError('Hanya data nyata tersedia.')
    if province and province not in PROVINCES: raise ValueError('Provinsi tidak valid.')
    if category and category not in CATEGORIES: raise ValueError('Kategori tidak valid.')
    if days not in ('7','30','90','all'): raise ValueError('Periode tidak valid.')
    sql="SELECT * FROM regional_events WHERE dataset=? AND lower(source_name) != 'bmkg' AND category IN ('cyber','infrastructure','integrity','safety')";args=[dataset]
    for field,value in [('province_code',province),('category',category),('verification',verification),('source_name',source)]:
        if value: sql+=' AND '+field+'=?';args.append(value)
    if days!='all': sql+=' AND COALESCE(NULLIF(event_at,\'\'),NULLIF(published_at,\'\'),observed_at)>=?';args.append((datetime.now(timezone.utc)-timedelta(days=int(days))).isoformat())
    rows=[dict(r) for r in conn.execute(sql+' ORDER BY COALESCE(NULLIF(event_at,\'\'),observed_at) DESC LIMIT 500',args)]
    import screening
    visible=[]
    for row in rows:
        link=conn.execute('SELECT result_id FROM regional_screen_links WHERE event_id=?',(row['id'],)).fetchone()
        if link and screening.detail(conn,link['result_id'])['source_changed']: continue
        visible.append(row)
    sources=[r[0] for r in conn.execute("SELECT DISTINCT source_name FROM regional_events WHERE dataset='real' AND lower(source_name)!='bmkg' AND category IN ('cyber','infrastructure','integrity','safety') ORDER BY source_name")]
    return {'events':visible,'count':len(visible),'unlocated':sum(not x['province_code'] for x in visible),'source_status':{},'available_sources':sources}

def link_screening(conn, result_id, province_code, location_text, confirmed):
    import screening
    if confirmed is not True: raise ValueError('Konfirmasikan bahwa lokasi merujuk pada kejadian, bukan lokasi akun.')
    if province_code not in PROVINCES: raise ValueError('Provinsi tidak dikenal.')
    if not isinstance(location_text,str) or not 3<=len(location_text.strip())<=120: raise ValueError('Kutipan lokasi harus 3?120 karakter.')
    item=screening.detail(conn,result_id)
    if item['source_changed']: raise ValueError('Sumber berubah; jalankan penyaringan ulang.')
    if item['review_status']!='reviewed' or item['decision']!='relevant': raise ValueError('Tinjau hasil relevan sebelum menautkan lokasi.')
    if norm(location_text) not in norm(item['text']): raise ValueError('Kutipan lokasi harus ada dalam teks sumber.')
    if resolve_location(location_text)['province_code']!=province_code: raise ValueError('Kutipan lokasi belum cukup untuk menentukan provinsi ini.')
    if item['category'] not in CATEGORIES: raise ValueError('Kategori tidak tersedia pada peta regional.')
    if not factcheck.public_evidence_url(item['source_url']): raise ValueError('Tautan sumber HTTPS publik diperlukan.')
    key='screening:'+item['kind']+':'+item['source_ref']+':'+item['content_sha256']
    existing=conn.execute("SELECT id,province_code FROM regional_events WHERE dataset='real' AND source_key=?",(key,)).fetchone()
    if existing and existing['province_code']!=province_code: raise ValueError('Kejadian sudah ditautkan ke provinsi lain; perlu review manual.')
    source_name='YouTube' if item['kind']=='comment' else 'Observasi publik'
    event=dict(source_key=key,dataset='real',title=('Informasi untuk ditinjau: '+item['text'][:120]).replace('\n',' '),summary=item['text'][:1500],category=item['category'],province_code=province_code,location_text=location_text.strip(),location_confidence='analyst_review_required',location_basis='Analis memilih kutipan lokasi kejadian dari teks sumber; bukan verifikasi resmi.',source_name=source_name,source_url=item['source_url'],source_type='public_source',verification='unverified',event_at='',published_at=item['published_at'],observed_at=now())
    upsert(conn,event)
    event_id=conn.execute("SELECT id FROM regional_events WHERE dataset='real' AND source_key=?",(key,)).fetchone()[0]
    conn.execute('INSERT OR IGNORE INTO regional_screen_links(event_id,result_id) VALUES(?,?)',(event_id,item['id']))
    conn.execute("INSERT INTO screening_audit(result_id,action,created_at) VALUES(?,'regional_linked',?)",(item['id'],now()))
    conn.commit();return {'event_id':event_id,'province_code':province_code,'verified':False}

def open_case(conn,event_id):
    # Lock the association through creation so repeated requests reuse one case.
    conn.execute('BEGIN IMMEDIATE')
    try:
        event=conn.execute('SELECT * FROM regional_events WHERE id=?',(investigation._id(event_id),)).fetchone()
        if not event or event['dataset'] != 'real' or event['source_name'].casefold() == 'bmkg' or event['category'] not in CATEGORIES: raise ValueError('Kejadian tidak tersedia untuk operasi.')
        existing=conn.execute('SELECT case_id FROM regional_case_links WHERE event_id=?',(event['id'],)).fetchone()
        if existing:
            conn.commit(); return {'id':existing[0],'existing':True}
        summary=('Regional '+PROVINCES.get(event['province_code'],'lokasi belum terverifikasi')+' | event #'+str(event['id'])+' | '+event['summary'])[:1500]
        stamp=now()
        cid=conn.execute("INSERT INTO intel_investigation_cases(title,summary,dataset,created_at,updated_at) VALUES(?,?,?,?,?)",(('Regional: '+event['title'])[:160],summary,event['dataset'],stamp,stamp)).lastrowid
        investigation._event(conn,cid,'case_created','Kasus regional dibuat. Belum ada temuan yang terkonfirmasi.')
        link=conn.execute('SELECT r.kind,r.source_ref,r.content_sha256 FROM regional_screen_links l JOIN screening_results r ON r.id=l.result_id WHERE l.event_id=?',(event['id'],)).fetchone()
        if link:
            source=investigation._source(conn,link['kind'],link['source_ref'])
            if hashlib.sha256(source['body'].encode()).hexdigest()!=link['content_sha256']: raise ValueError('Sumber berubah; perbarui penyaringan.')
        else:
            source=dict(kind='reference',source_ref='regional:'+str(event['id']),platform='regional',provenance='manual',author_name=event['source_name'],body=event['summary'],source_url=event['source_url'],published_at=event['published_at'],evidence_urls=[])
        investigation._add_item(conn,cid,source)
        conn.execute('INSERT INTO regional_case_links(event_id,case_id) VALUES(?,?)',(event['id'],cid));conn.commit()
        return {'id':cid,'existing':False}
    except Exception:
        conn.rollback(); raise

if __name__ == '__main__':
    try: print('Peta terpasang:',download_map(),'provinsi')
    except Exception as exc: print('Peta belum terpasang:',exc); raise SystemExit(1)

"""CAKRA v1.0 demo/release helpers. Offline-only; never auto-publishes.

The demo case is synthetic. This module deliberately does not make a truth verdict.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import discovery
import intelligence
import investigation
import unified_fact

VERSION = '1.0.0'
DEMO_TITLE = 'DEMO v1.0 — Kabar Jembatan X (FIKTIF)'


def _count(conn, table, where='', values=()):
    # Names and conditions are authored constants, never user supplied.
    return conn.execute('SELECT COUNT(*) FROM ' + table + (' WHERE ' + where if where else ''), values).fetchone()[0]


def readiness(conn: sqlite3.Connection, base: Path, env=None):
    """Operational readiness, not a measure of algorithm accuracy."""
    import os
    env = os.environ if env is None else env
    base = Path(base)
    check = conn.execute('PRAGMA integrity_check').fetchone()[0]
    real_comments = _count(conn, 'comments', "dataset='real'")
    demo_comments = _count(conn, 'comments', "dataset='demo'")
    real_obs = _count(conn, 'intel_observations', "provenance!='demo'")
    demo_obs = _count(conn, 'intel_observations', "provenance='demo'")
    investigated = _count(conn, 'intel_investigation_cases')
    claim_cases = _count(conn, 'unified_fact_cases')
    demo_case = conn.execute('SELECT id FROM intel_investigation_cases WHERE title=? AND dataset=? ORDER BY id LIMIT 1', (DEMO_TITLE, 'demo')).fetchone()
    items = {
        'database_integrity': check == 'ok',
        'dashboard_exists': (base / 'dashboard.html').is_file(),
        'tests_exist': (base / 'tests' / 'test_cakra.py').is_file(),
        'submission_guide_exists': (base / 'SUBMISSION_KIT.md').is_file(),
        'demo_data_available': bool(demo_comments + demo_obs),
        'demo_investigation_available': bool(demo_case),
        'real_data_available': bool(real_comments + real_obs),
        'youtube_api_configured': bool(env.get('YOUTUBE_API_KEY', '').strip()) and not env.get('YOUTUBE_API_KEY','').startswith('ISI_KEY_'),
        'factcheck_api_configured': bool(env.get('FACTCHECK_API_KEY', '').strip()) and not env.get('FACTCHECK_API_KEY','').startswith('ISI_KEY_'),
        'muse_api_configured': bool((env.get('META_MODEL_API_KEY','') or env.get('MODEL_API_KEY','')).strip()) and not (env.get('META_MODEL_API_KEY','') or env.get('MODEL_API_KEY','')).strip().startswith('ISI_KEY_'),
    }
    status = 'ready_for_offline_demo' if all(items[k] for k in ('database_integrity','dashboard_exists','demo_data_available','demo_investigation_available')) else 'needs_demo_setup'
    return {
        'version': VERSION,
        'status': status,
        'checks': items,
        'counts': {
            'real_comments': real_comments, 'demo_comments': demo_comments,
            'real_observations': real_obs, 'demo_observations': demo_obs,
            'investigations': investigated, 'unified_claim_cases': claim_cases,
            'claim_discovery_candidates': _count(conn, 'discovery_candidates'),
            'human_evaluation_labels': _count(conn, 'eval_source_labels'),
        },
        'demo_investigation_id': demo_case['id'] if demo_case else None,
        'notes': [
            'Status ready_for_offline_demo hanya berarti alur demonstrasi tersedia, bukan akurasi deteksi terbukti.',
            'Key API tidak dikirim ke browser; hanya status konfigurasi boolean.',
            'Data simulasi dan data nyata tidak digabung dalam satu kasus.',
            'Tidak ada publikasi media sosial otomatis.'
        ]
    }


def prepare_demo(conn, seed_comments):
    """Idempotent fixture using only synthetic sources in the existing database.

    Does not create evidence claiming to verify the hypothetical event, does not
    approve drafts, and never contacts an external service.
    """
    comments = seed_comments(conn)
    observations = intelligence.seed_intel_demo(conn)
    scanned = discovery.scan(conn)
    row = conn.execute('SELECT id FROM intel_investigation_cases WHERE title=? AND dataset=? ORDER BY id LIMIT 1',
                       (DEMO_TITLE, 'demo')).fetchone()
    created = row is None
    if row is None:
        case = investigation.create_case(
            conn, DEMO_TITLE,
            'Kasus fiktif untuk demo; pola teks serupa BUKAN bukti bot, disinformasi, atau kejadian sebenarnya.',
            source_kind='comment', source_ref='demo-001')
        case_id = case['id']
    else:
        case_id = row['id']
    obs_row = conn.execute("SELECT id FROM intel_observations WHERE source_id='demo:bsky1' AND provenance='demo'").fetchone()
    if obs_row and not conn.execute("SELECT 1 FROM intel_investigation_items WHERE case_id=? AND kind='observation' AND source_ref=?",
                                (case_id, str(obs_row['id']))).fetchone():
        investigation.add_item(conn, case_id, kind='observation', ref=obs_row['id'])
    source = conn.execute("SELECT id FROM intel_investigation_items WHERE case_id=? AND kind='comment' AND source_ref='demo-001'",
                          (case_id,)).fetchone()
    claim = conn.execute('SELECT id FROM unified_fact_cases WHERE investigation_id=? ORDER BY id LIMIT 1', (case_id,)).fetchone()
    if source and not claim:
        unified_fact.create(conn, case_id, source['id'], 'Jembatan X ditutup permanen menurut kabar yang beredar.')
    return {
        'dataset': 'demo', 'idempotent': True, 'new_case': created,
        'investigation_id': case_id, 'comments': comments['inserted_or_existing'],
        'bluesky_demo_links': observations['inserted_links'],
        'discovery_processed': scanned.get('processed'),
        'warning': 'SIMULASI / FIKTIF. Tidak ada verifikasi kebenaran, penyimpulan bot, atau publikasi otomatis.'
    }

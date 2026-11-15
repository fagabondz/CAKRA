#!/usr/bin/env python3
"""Read-only CAKRA operator checks. Does not print credentials or mutate the database."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def show(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


def env_presence(path: Path) -> dict[str, bool]:
    loaded: dict[str, bool] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            name, value = s.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            loaded[name] = bool(value) and not value.upper().startswith(("ISI_", "YOUR_", "REPLACE_"))
    return {key: bool(os.environ.get(key)) or loaded.get(key, False) for key in (
        "YOUTUBE_API_KEY", "FACTCHECK_API_KEY", "META_MODEL_API_KEY", "MODEL_API_KEY", "META_MODEL"
    )}


def verify_sqlite(path: Path) -> None:
    if not path.is_file():
        show("WARN", "SQLite", "cakra.db tidak ditemukan; tidak membuat database")
        return
    try:
        # Read-only SQLite URI prevents accidental schema changes.
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = len(db.execute("PRAGMA foreign_key_check").fetchall())
        if integrity == "ok" and foreign_keys == 0:
            show("OK", "SQLite integrity", "ok; foreign_key_check=0 (read-only)")
        else:
            show("FAIL", "SQLite integrity", f"integrity={integrity}; foreign_key_issues={foreign_keys}")
    except Exception as exc:
        show("FAIL", "SQLite integrity", f"{type(exc).__name__}: {exc}")


def verify_geometry(path: Path) -> None:
    if not path.is_file():
        show("WARN", "Geometri peta", "GeoJSON belum tersedia")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        features = data.get("features", []) if isinstance(data, dict) else []
        shapes = sum(1 for f in features if isinstance(f, dict) and f.get("geometry", {}).get("type") in ("Polygon", "MultiPolygon"))
        status = "OK" if len(features) == shapes == 38 else "WARN"
        show(status, "Geometri peta", f"features={len(features)}; polygon/multipolygon={shapes}; validasi administratif tetap perlu modul regional.py")
    except Exception as exc:
        show("FAIL", "Geometri peta", f"{type(exc).__name__}: {exc}")


def get_external(url: str, label: str, expected: str) -> None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "CAKRA-Operator-Diagnostic/1.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            content = response.read(1_000_000)
            status = response.status
            final_url = response.geturl()
        if status != 200:
            show("WARN", label, f"HTTP {status}")
            return
        if expected == "xml":
            root = ET.fromstring(content)
            parsed = root.tag.split("}")[-1].lower() in ("rss", "feed", "rdf")
        elif expected == "json":
            parsed = isinstance(json.loads(content), dict)
        else:
            parsed = True
        show("OK" if parsed else "WARN", label, f"HTTP 200; format={'sesuai' if parsed else 'tak terduga'}" + ("; redirect terdeteksi" if final_url != url else ""))
    except urllib.error.HTTPError as exc:
        show("WARN", label, f"HTTP {exc.code}; periksa kebijakan, throttling, dan adapter")
    except Exception as exc:
        show("WARN", label, f"{type(exc).__name__}: {str(exc)[:180]}")


def check_local_api(port: int) -> None:
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            show("OK", "CAKRA local API", f"HTTP {response.status}; {url}")
    except Exception as exc:
        show("WARN", "CAKRA local API", f"tidak merespons pada {url} ({type(exc).__name__}); jalankan server terlebih dahulu")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pemeriksaan CAKRA non-destruktif; tidak membocorkan nilai API key.")
    parser.add_argument("--root", default=".", help="Folder proyek CAKRA (default: direktori saat ini)")
    parser.add_argument("--port", type=int, default=8765, help="Port server lokal")
    parser.add_argument("--online", action="store_true", help="Uji satu permintaan GET ke RSS ANTARA, GDELT, dan Meta status publik; tanpa API key")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    print("CAKRA OPERATOR DIAGNOSTIC — READ ONLY")
    print(f"Folder: {root}")
    print("Tidak membaca/menampilkan nilai API key; tidak mengubah file atau database.\n")
    if not root.is_dir():
        show("FAIL", "Direktori proyek", "folder tidak ditemukan")
        sys.exit(2)
    for name in ("server.py", "dashboard.html", "collection_jobs.py", "screening.py", "ai_providers.py", "regional.py"):
        show("OK" if (root / name).is_file() else "WARN", f"File {name}", "ada" if (root / name).is_file() else "tidak ditemukan di lokasi ini")
    config = env_presence(root / ".env")
    print("\nKonfigurasi (hanya status tersedia/tidak):")
    for name in ("YOUTUBE_API_KEY", "FACTCHECK_API_KEY", "META_MODEL_API_KEY", "MODEL_API_KEY", "META_MODEL"):
        available = config[name] or (name == "META_MODEL_API_KEY" and config["MODEL_API_KEY"])
        show("OK" if available else "WARN", name, "tersedia" if available else "belum dikonfigurasi")
    print("\nDatabase dan peta:")
    verify_sqlite(root / "cakra.db")
    verify_geometry(root / "data" / "indonesia-provinces.geojson")
    print("\nServer lokal:")
    check_local_api(args.port)
    if args.online:
        print("\nJaringan publik (tanpa autentikasi / API berbayar):")
        get_external("https://www.antaranews.com/rss/tekno.xml", "RSS ANTARA Tekno", "xml")
        params = urllib.parse.urlencode({"query": '"cybersecurity"', "mode": "artlist", "maxrecords": "5", "timespan": "1week", "format": "json"})
        get_external("https://api.gdeltproject.org/api/v2/doc/doc?" + params, "GDELT DOC API", "json")
        get_external("https://api.meta.ai/v1/status", "Meta Model API public status", "json")
    print("\nSelesai. Hasil tes konektivitas ini tidak menggantikan pengujian adapter CAKRA yang memeriksa robots, izin, dan batas kuota.")


if __name__ == "__main__":
    main()

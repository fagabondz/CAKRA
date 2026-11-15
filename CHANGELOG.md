## Siap GitHub: restruktur + bersih + README - 15 November 2026

- Struktur: gambar pindah ke `assets/` (rute URL tidak berubah); docs arsip/brief dihapus (Revisi, directive, stage report, FINAL_*, audit lama); `tests/` (25 berkas) + `cleanup_simulation.py` yatim dihapus permanen atas perintah eksplisit (suite terakhir 201 lolos sebelum penghapusan).
- Keamanan rilis: `.gitignore` mencakup db/env/cache/tmp; `.gitattributes` normalisasi teks; pindai rahasia bersih; README ditulis ulang.

## Preloader + audit bersih + adopsi AI 5:56 - 15 November 2026

- Preloader peta Indonesia terintegrasi dashboard: `preloader.html` menjadi fragmen komponen yang disuntik server ke placeholder; progres mengikuti boot nyata (maks 8 dtk failsafe); hormati reduced-motion; `/peta-indonesia.webp` dibuka untuk produksi.
- Hapus 10 berkas tak terpakai: 8 UI arsip (ai/collection/radar/regional/screening/unified/youtube-radar), `cakra_collector.py` (tanpa referensi, skema tak kompatibel), screenshot Demo Center lama; rute static dirapikan.
- Adopsi `ai_providers.py` 5:56 PM: default muse-spark-1.1, timeout 90, retry 3x + backoff, detail error HTTP, validasi longgar, `_extras`, prompt parafrasa; tes+docs diselaraskan.
- Catatan: integrasi preloader menyimpang dari arahan demo lama atas perintah eksplisit; `.env` tetap pin muse-spark-1.3 (menang atas default); 3 direktori tmp sisa milik SYSTEM aman dihapus manual.

## Migrasi ke Responses API resmi - 15 November 2026

- Seluruh pemanggilan AI kini mengikuti quickstart resmi (`POST /v1/responses`, `instructions`+`input`, `text.format` json_schema strict, baca `output_text`, usage `input/output_tokens`); tinggalkan bentuk Chat Completions.
- Tanpa `temperature`/`max_output_tokens` (ikut default yang disetel; respons terpotong terdeteksi via status `incomplete` lalu korpus dibelah); thumbnail memakai blok `input_text`+`input_image`.
- Skema dan parsing lama dihapus; seluruh fixture transport ditulis ulang ke envelope Responses.

## Diagnostik jaringan AI - 15 November 2026

- `request_json`: sekali retry untuk galat jaringan/timeout dan HTTP 429/5xx; pesan error kini menyebut host + lapisan gagal (DNS/TCP-timeout/TLS/proxy) + jumlah percobaan; 404 tak lagi ditandai retryable; hint per kode (401 kunci, 403 izin/kuota, 404 model, 407 proxy).
- Label kartu UI memuat 200 karakter alasan agar diagnosis terbaca.
- Catatan operator: `Tidak dapat menghubungi api.meta.ai` = masalah jalur jaringan/proxy/firewall, BUKAN kunci; 401 = kunci salah. Uji via browser ke https://api.meta.ai/v1/models (401/JSON = terjangkau).

## Analisis AI per-sumber + prompt SOC - 15 November 2026

- Alur AI komentar ditulis ulang: seluruh username+komentar tiap sumber (nyata/demo) disatukan dalam satu panggilan `analyze_source` dengan system prompt analis SOC; AI mengembalikan verdict terindikasi (bot/provokatif/hoaks), sisanya dicatat aman.
- Ketahanan: respons terpotong/HTTP 413 otomatis belah-dua dan gabung (batas daun 10); cache per sub-korpus membuat resume hanya membayar yang kurang; cakupan AI (`ai_coverage_json`) tampil di UI.
- Label kartu kategori kini sadar-status (berjalan/gagal/nonaktif/menunggu); temuan dikelompokkan per kategori dengan nama penulis; hapus `comment_batch` dan taksonomi lama.
- Akar label "Analisis AI belum tersedia": demo berjalan tanpa AI (penggunaan kosong), job nyata PARTIAL karena api.meta.ai tak terjangkau; bukan salah hitung.

## Migrasi penganalisis ke Muse - 15 November 2026

- Penganalisis komentar kini Muse (Meta Model API, default muse-spark-1.3); seluruh fungsi Gemini dihapus (provider, endpoint video, `legacy_gemini_request`, ekstraksi, kunci env). Alias lawas `meta` tetap dipetakan ke Muse; fallback lintas provider dihapus.
- Taksonomi AI baru sesuai tujuan CAKRA: indikasi bot, narasi provokatif, indikasi hoaks (+ netral/kurang konteks); UI menampilkan 4 kartu kategori, filter, dan label Indonesia.
- Pengaturan menjadi panel overlay (tutup via Esc/klik-luar/tombol); navigasi mobile memakai hamburger; topbar lengket; hormati `prefers-reduced-motion`.
- `.env` operator: hapus manual dua baris `GEMINI_*` (tidak lagi dibaca); `META_MODEL=muse-spark-1.3` sudah sesuai.

## Hapus data + perapihan UI - 15 November 2026

- Pengaturan mendapat Zona berbahaya: **Hapus data nyata** (`POST /api/data/clear`, database `cakra.db`) dan **Hapus data demo** (memakai ulang `POST /api/simulation/clear`, database `cakra_demo.db`); keduanya meminta konfirmasi dan endpoint nyata menolak saat analisis/pengumpulan berjalan.
- Tombol hapus memakai gaya `.danger`; diagnostik provider ditampilkan per baris; hover tombol disederhanakan; gerak nonaktif saat `prefers-reduced-motion`.
- Tes baru `tests/test_data_clear.py`: unit penghapusan + isolasi database, dan HTTP untuk konfirmasi, kunci sibuk, serta keberadaan tombol.

## Frontend mengacu Stitch - 12 November 2026

- Mengadaptasi tiga desain lokal `stitch_cakra`: navy/cyan, panel bersudut tegas, sidebar tiga menu, masthead dan jam UTC.
- Menata halaman analisis menjadi input terpadu dan dua panel mesin; hasil perilaku memakai kartu statistik agregat.
- Grafik kronologi menghitung 12 interval dari komentar yang dimuat; tidak menggunakan angka contoh desain.
- Form kasus baru dapat dibuka/tutup; pemilihan sumber otomatis membuka formulirnya. Kontrol lama dan ID integrasi tetap dipertahankan.
- Layout mobile memakai navigasi bawah. Font memakai fallback lokal tanpa CDN.
- Backup frontend sebelum perubahan: `backup/dashboard_before_stitch.html`.

## Pembaruan riset defensif - 2026-11-12

- Menambahkan `research_analysis.py`: cadence/frekuensi agregat, pengulangan teks, kecukupan data, analisis konten Gemini dengan validasi respons dan penanganan error.
- Menambahkan endpoint analisis terpadu tanpa penulisan data, pembatasan sampel, provenance dan checksum teks.
- Tiga menu utama dan ruang dua mesin; konten tersimpan dapat diteruskan ke kasus pemeriksaan.
- Memperbaiki penutupan koneksi SQLite pada context manager agar database sementara tidak terkunci di Windows.
- Menambahkan tujuh tes mesin/HTTP dengan contoh sintetis dan respons AI tiruan. Tidak ada migrasi skema atau perubahan kredensial.

# CAKRA Release History

## v1.0.0 — Hackathon Demo Release (12 Nov 2026)

- New **Demo Center** page with offline demonstration instructions and release checks.
- New `/api/health` and `/api/release` read-only endpoints.
- New `/api/release/demo` action and `py -3 server.py demo` command to prepare a **synthetic**, idempotent YouTube + Bluesky investigation with an *unreviewed* claim.
- New `py -3 server.py doctor` command; key values are never exposed in the API response.
- New `start_demo_windows.bat`, `SUBMISSION_KIT.md`, `DEMO_CHECKLIST.md`, and `RELEASE_TEST_REPORT.md`.
- Standardized visible release labels to v1.0; updated YouTube request user-agent.
- Preserves v0.1–v0.9 modules, schemas, and compatibility. **No automatic publishing, automatic truth judgment, or account attribution.**

## v0.9 — Intelligence Evaluation

Human-labeled Claim Discovery evaluation, source fingerprinting, audit, and metrics.

## v0.8 — Autonomous Claim Discovery

Source-linked claims, prioritization queue, and investigation creation.

## v0.7 — Unified Fact Verification

Cross-platform evidence review and human-approved clarification drafts.

## v0.6 — Cross-Platform Investigations

Investigations combining observations and evidence with snapshot hashing and reporting.

## v0.5 — Early Warning / Response Workspace

Optional scheduled monitoring, alerts, and manual-publication tracking.

## v0.4 — Virtual Intelligence

Bluesky public search, YouTube topic search, watchlists, and manual leads.

## v0.3 — Fact Verification

Google Fact Check candidates, optional Gemini extraction, human decisions.

## v0.2 — Network Explorer

Similarity/time-based graph with traceable comment pairs.

## v0.1 — Foundation

Local YouTube comment collection, SQLite database, dashboard, and offline demo data.

# CAKRA — Analisis Komentar YouTube

CAKRA menganalisis komentar publik dari satu video YouTube atau input simulasi operator dan membantu investigasi berbasis bukti: **URL/CSV → komentar → analisis AI dan pola → temuan → investigasi**. Penganalisis AI adalah Muse (Meta Model API) yang mengkategorikan komentar: indikasi akun bot, narasi provokatif, indikasi hoaks, atau aman.

## Mulai cepat

1. Butuh Python 3.11+.
2. Salin `.env.example` menjadi `.env` lalu isi kunci API (`META_MODEL_API_KEY`, kunci YouTube, kunci Fact Check). Jangan bagikan `.env`.
3. Jalankan `python server.py serve` di folder proyek (atau klik dua kali `start_windows.bat`).
4. Buka alamat localhost yang dicetak server di browser.

Server hanya melayani `127.0.0.1` dan tidak memulai pemindaian otomatis.

## Menggunakan

- Tempel URL pada **Analisis YouTube** lalu tekan **Analisis sekarang**. Pengaturan memakai model Muse yang dapat diatur (default `muse-spark-1.1`), balasan, dan batas permintaan, serta tombol hapus data nyata dan data demo dengan konfirmasi.
- Tab **Data Simulasi**: ketik satu komentar per baris atau impor CSV UTF-8 (`comment`, opsional `username`, `published_at`, `parent_id`), pratinjau, lalu analisis. Maksimal 1.000 komentar dan 200.000 karakter per input.
- Periksa progres dan temuan yang dikelompokkan per kategori, buka bukti, lalu **Investigasi lebih lanjut** untuk mencatat review manusia. Hasil parsial dapat dilanjutkan.
- Teks komentar dikirim ke provider AI dan penggunaan dapat dikenai biaya. Pola bukan bukti bahwa akun adalah bot; klasifikasi AI bukan putusan fakta.

## Data

- `cakra.db`: data nyata; `cakra_demo.db`: data demo. Keduanya lokal dan tidak ikut ke Git.
- Zona berbahaya di Pengaturan menghapus seluruh data nyata atau demo secara permanen setelah konfirmasi.

## Struktur proyek

- `server.py`, modul `*.py`: backend Python stdlib-only.
- `dashboard.html`, `comment-workspace.js`, `comment-workspace.css`, `preloader.html`: aplikasi web.
- `assets/`: gambar (latar + peta preloader).
- `data/`: data peta.
- `docs/`: kontrak data, runbook, gerbang rilis, progres.

## Keamanan

- Kunci API hanya dibaca di backend dari `.env` (tidak pernah ke browser); `.env` dan `*.db` masuk `.gitignore`.
- API hanya menerima request bertanda lokal; tanpa header tersebut request ditolak.

## Dokumentasi

- [Alur terperinci](ALUR_DAN_CARA_KERJA_CAKRA.md), [progres](docs/IMPLEMENTATION_PROGRESS.md), [langkah manual](docs/MANUAL_ACTIONS.md), [runbook demo](docs/DEMO_RUNBOOK.md), [status gerbang rilis](docs/RELEASE_GATE_STATUS.md), [kontrak data](docs/DATA_CONTRACTS.md), [riwayat perubahan](CHANGELOG.md).

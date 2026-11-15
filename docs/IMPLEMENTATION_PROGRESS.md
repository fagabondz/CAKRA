# Progres refactor final CAKRA

Arahan aktif: [Revisi.md](../Revisi.md), alur satu URL YouTube sampai investigasi. Status di bawah membedakan implementasi, pengujian lokal dan integrasi nyata. Arsip laporan sebelumnya sudah dihapus dari workspace pada audit 15 Nov 2026.

## Audit / fase 0 — TESTED LOCALLY

Audit dependency [COMMENT_REFACTOR_AUDIT.md](COMMENT_REFACTOR_AUDIT.md); baseline sebelum refactor 170 tes lulus. Backup SQLite `backup/cakra-before-comment-refactor-20261114T085227154686Z.db` memiliki integrity check OK dan 0 pelanggaran foreign key; berisi 824 komentar dan 4 investigasi. Migrasi diuji pada salinan backup: 824 komentar, 4 investigasi dan 1 kasus fact verification tetap utuh; integrity check OK dan 0 pelanggaran foreign key. Database riwayat dan `.env` dipertahankan. Folder proyek saat audit bukan repository Git sehingga diff Git tidak tersedia.

## Fase 1: cleanup — IMPLEMENTED / TESTED LOCALLY

`server.py`, `dashboard.html` dan tes keamanan: autostart radar/monitor lama dan navigasi peta, radar, berita serta Reports dinonaktifkan. Endpoint radar/regional operasional diblokir; modul dan tabel historis dipertahankan untuk kompatibilitas. Tes startup memastikan tidak ada worker lama dimulai. Risiko: kode arsip masih ada untuk kompatibilitas, bukan kemampuan operasional yang baru.

## Fase 2: URL dan collection — IMPLEMENTED / TESTED LOCALLY

`server.py`, `collection_jobs.py`, `comment_analysis.py`: validasi URL, metadata, komentar, balasan per induk, pagination, checkpoint, deduplikasi, resume, batas sesi dan pesan komentar dinonaktifkan. Snapshot metadata menjaga konteks analisis. Tes collection dan alur terisolasi lulus. Risiko: kuota lokal bukan saldo resmi, cakupan tergantung YouTube dan izin, koneksi nyata belum terbukti.

## Fase 3: mesin analisis — IMPLEMENTED / TESTED LOCALLY dengan keterbatasan

`screening.py`, `research_analysis.py`, `ai_providers.py`, `comment_analysis.py`: normalisasi, kandidat duplikat, pola waktu dan URL agregat, batch Gemini/Meta, validasi ID/kutipan, cache, fallback yang diizinkan dan penggunaan tercatat. Tes screening/provider dan kasus alur terpadu lulus. Risiko: pengelompokan heuristik bukan semantik yang sempurna, belum ada precision/recall terkalibrasi, maksimal 100 kelompok per jenis, AI menerima 1.500 karakter awal komentar, benchmark dataset besar belum dilakukan. Temuan bukan vonis.

## Fase 4: orkestrasi — IMPLEMENTED / TESTED LOCALLY

Satu job persisten menjalankan koleksi, preprocessing, pola dan AI di worker terkontrol; status parsial, cancel, resume, checkpoint dan pemulihan disediakan. Tes mencakup kegagalan AI, pemulihan dan konkurensi. Risiko: restart memerlukan kelanjutan manual setelah tenggang 5 menit; request AI terputus dapat dikenai biaya.

## Fase 5: temuan dan investigasi — IMPLEMENTED / TESTED LOCALLY

Temuan dan bukti berpaginasi dapat membuka kasus idempoten dengan snapshot; klaim menjadi kandidat fact verification dan review manusia tersedia. Tes HTTP terisolasi mencakup alur URL hingga kasus dan keterkaitan bukti. Risiko: maksimal 20 bukti disalin ke snapshot kasus; seluruh referensi tetap tersedia di temuan. Google Fact Check nyata belum diverifikasi.

## Fase 6: UI — IMPLEMENTED / pemeriksaan visual BLOCKED

`dashboard.html`, `comment-workspace.css`, `comment-workspace.js`: dua area, input URL, progres backend, filter, bukti, investigasi dan review. `node --check comment-workspace.js` lulus. Gate frontend: **GATE: ERROR**; `consistency.mjs` tidak dapat menelusuri viewport 1440/390 dan `check.mjs` timeout. Oleh sebab itu kenyamanan, responsivitas dan interaksi visual belum terverifikasi di browser. Lihat [tindakan manual](MANUAL_ACTIONS.md).

## Fase 7: QA dan integrasi — TESTED LOCALLY / BLOCKED

Regresi terakhir: **187 tes lulus** dalam 203,058 detik. Pada pengujian sebelumnya satu tes HTTP screening melewati timeout 5 detik ketika lingkungan lambat; 10 tes screening kemudian lulus sendiri, dan tes tersebut juga lulus pada suite terakhir. Fixture terisolasi tidak membuktikan provider nyata. Percobaan metadata YouTube nyata gagal: jaringan tidak dapat menghubungi YouTube. Gemini, Meta dan Google Fact Check belum memperoleh respons nyata yang diverifikasi. Instruksi operator ada di [MANUAL_ACTIONS.md](MANUAL_ACTIONS.md). Jangan menandai pekerjaan sebagai VERIFIED WITH REAL API sebelum verifikasi berhasil.


## Pembaruan verifikasi sesi ini

Baseline ulang menjalankan 182 tes dengan satu timeout HTTP saat beberapa pemeriksaan berjalan bersamaan. Regresi berikutnya lulus **186 tes**. Setelah itu lookup analisis berdasarkan video diperluas ke seluruh riwayat; **14 tes terarah lulus**, termasuk tes HTTP lookup dan alur investigasi. Sintaks Python dan JavaScript lulus. Pemeriksaan read-only pada database operasional menghasilkan `integrity_check=ok` dan nol pelanggaran foreign key.

Halaman komentar atau balasan yang berisi item rusak kini tidak dimajukan diam-diam; job menjadi parsial dan checkpoint dipertahankan. Penghitung AI tertunda mencakup item yang belum disaring. UI menawarkan membuka analisis sebelumnya atau memperbaruinya. Analisis baru untuk video yang sudah ada memperbarui metadata. Hasil AI belum digunakan ulang antarjob baru. Pemeriksaan browser dan API nyata tetap belum terverifikasi dalam sesi ini.

## Manual Simulation Input

Implementasi: `simulation.py`, `server.py`, `screening.py`, `comment_analysis.py`, `investigation.py`, `ai_providers.py`, `dashboard.html`, `comment-workspace.js`, `comment-workspace.css`; pengujian `tests/test_simulation.py`. Teks multiline dan CSV dipratinjau sebelum analisis. Simulasi hanya disimpan dalam `cakra_demo.db`, menggunakan dataset demo, tanpa URL atau timestamp buatan dan tanpa panggilan YouTube. Penyaringan, pola dan batch AI memakai mesin yang sama; AI memerlukan pilihan eksplisit. Kasus simulasi terpisah dan data dapat dihapus setelah konfirmasi. Input dibatasi 1.000 komentar dan 200.000 karakter.

Regresi menyeluruh: **192 tes lulus**. Setelah penambahan tes metadata CSV dan pengamanan hardlink, **8 tes simulasi** dan **14 tes analisis komentar nyata** lulus; sintaks Python dan JavaScript lulus. Pengujian menggunakan database sementara dan provider tiruan, bukan API nyata. Browser interaktif dan koneksi Gemini/Meta nyata belum diverifikasi pada sesi ini. Gate visual sebelumnya `GATE: ERROR` karena browser timeout. Model dapat salah dan keterbatasan metadata ditampilkan; klasifikasi dan sentimen bukan putusan fakta.


## Arahan aktif: final demo dan submission — 15 November 2026

`CAKRA_FINAL_DEMO_DIRECTIVE.md` menggantikan prioritas `Revisi.md`. Audit dan backup privat: `FINAL_RELEASE_AUDIT.md`; manifest: `FINAL_FILE_MANIFEST.md`. Baseline aktual 195 tes lulus, kedua DB dan backup integrity `ok`/foreign key 0. Database historis tidak diubah untuk pengujian.

Perubahan P0: status model dan uji generation dengan konfirmasi eksplisit, mode JSON schema Meta, hasil AI tersimpan ditampilkan pada kedua mode, provenance cache jika tersedia, resume simulasi menggunakan batch/screening yang sudah tersimpan, dan karantina item koleksi invalid tanpa membuang item valid. Kuantitas karantina mempertahankan status PARTIAL. UI dipadatkan dan menampilkan langkah/status backend. Tes fixture tidak memverifikasi layanan nyata.

Tindakan berikutnya yang tepat: jalankan tes akhir, buat dan verifikasi paket source tanpa secret/database, perbarui `FINAL_TEST_REPORT.md` dan `RELEASE_GATE_STATUS.md`. Operator perlu menjalankan FINAL-001 (provider nyata dengan persetujuan biaya), FINAL-002 (URL nyata), dan FINAL-003 (browser). Sampai G1 dan jalur demo dipastikan, status global bukan READY FOR DEMO.

### Penyelesaian lokal tahap final

Regresi penuh setelah sebagian besar perubahan: 199 tes lulus dalam 163,451 detik. Dua tes berikutnya untuk sanitasi error dan balasan tanpa waktu lulus dalam suite 16 tes terarah; sintaks lulus. Salinan runtime tanpa `.env`/database menjalankan server, mengembalikan halaman, riwayat kosong dan preview simulasi. Integrity kedua database tetap `ok`, foreign key 0, dan enam tabel utama sama dengan backup.

Langkah operator berikutnya: ikuti `FINAL-001` untuk menguji provider dan model dengan persetujuan biaya, kemudian `FINAL-002`/`FINAL-003` untuk URL YouTube dan browser. Belum ada bukti generation API nyata; status global tidak boleh ditandai siap demo. Keterbatasan dan status setiap gate ada di `RELEASE_GATE_STATUS.md`.

Regresi menyeluruh terakhir: **201 tes lulus**; setelah run dimulai satu pengujian status ketersediaan model ditambahkan dan suite provider terakhir lulus **13 tes**. Uji job simulasi 110 komentar pada salinan backup dengan provider fixture menyelesaikan 110 hasil; DB operasional tetap utuh. Paket source berlabel UNVERIFIED dipersiapkan tanpa data atau kunci; ikuti `RELEASE_GATE_STATUS.md` sebelum klaim demo.

Ruang investigasi simulasi kini juga dapat menyimpan referensi dan review klaim di DB simulasi. Tes HTTP terarah 11/11 lulus, termasuk pemeriksaan tidak ada kasus klaim simulasi pada DB nyata. Pengujian browser dan API nyata tetap pending.

Regresi terbaru: **202 tes lulus** dalam 201,821 detik; setelah suite dimulai satu tes tambahan pemetaan ancaman fixture ditambahkan dan 12 tes simulasi terarah lulus. Tidak ada perubahan kode Python aplikasi setelah suite dimulai; perubahan terakhir pada tampilan diperiksa dengan `node --check`. Gate API nyata dan browser tetap pending.

ZIP source yang diekstrak tanpa kredensial/database lulus uji HTTP CSV fixture → hasil → investigasi → catatan/review → tutup/buka serta isolasi kasus. Hasil ini lokal; G1/G5/G7 masih menunggu operator sebagaimana `RELEASE_GATE_STATUS.md`.

## Migrasi penganalisis ke Muse — 15 November 2026

Penganalisis komentar kini Muse (Meta Model API, default `muse-spark-1.1` sejak adopsi 5:56 PM); seluruh fungsi Gemini dihapus dan fallback lintas provider dihapus (kolom DB `fallback` dipertahankan berisi 0). Taksonomi AI: indikasi bot, narasi provokatif, indikasi hoaks, netral, konteks kurang; hanya tiga pertama menjadi temuan (`bot`/`provocative`/`hoax`). UI: 4 kartu kategori + filter + label Indonesia, pengaturan overlay, hamburger mobile, topbar lengket. Temuan lama berkategori `claim`/`harmful` tetap tampil pada filter Semua. Tes: suite provider/komentar/simulasi/factcheck/riset ditulis ulang ke Muse; verifikasi API nyata dan browser tetap menunggu operator (FINAL-001/003).

Temuan audit berikutnya: kartu kategori menampilkan "Analisis AI belum tersedia" karena AI memang tak pernah tersimpan (demo: AI nonaktif; nyata: PARTIAL `Layanan AI tidak dapat dihubungi`, ai_failed=10) — bukan salah hitung. Alur diganti ke analisis per-sumber di atas; `comment_batch` dihapus.

Adopsi 5:56 PM: `ai_providers.py` diubah di luar sesi (default 1.1, timeout 90, retry 3x + backoff, detail error HTTP, validasi longgar, `_extras`, prompt parafrasa); tes+docs diselaraskan ke semantik itu atas persetujuan operator.

Siap GitHub: gambar ke `assets/`; hapus 8 docs arsip/brief; hapus `tests/` + `cleanup_simulation.py` atas perintah eksplisit (suite terakhir 201 lolos); `.gitignore`/`.gitattributes` disiapkan; pindai rahasia bersih; README ditulis ulang.

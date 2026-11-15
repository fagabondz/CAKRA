# Kontrak data internal CAKRA

Kontrak ini memetakan konsep ke penyimpanan yang telah ada dan penyimpanan tambahan untuk radar. Semua waktu baru dinyatakan sebagai ISO 8601 UTC jika diketahui. Informasi dari sumber adalah kandidat, bukan verdict kebenaran.

| Konsep | Representasi | Identitas dan provenance | Siklus hidup |
|---|---|---|---|
| Source record media sosial | `comments` / `intel_observations` | ID native, platform, URL, teks asli, waktu dan provenance | Hash pada `screening_results` dan snapshot investigasi mendeteksi perubahan lokal; ketiadaan dari satu scan bukan bukti sumber dihapus. |
| Video YouTube radar | `yt_radar_videos` / `yt_radar_discoveries` | ID valid dari YouTube, metadata `videos.list`, waktu observasi, kueri dan provinsi pencarian | Deduplikasi ID; kueri bukan bukti lokasi. Metadata yang berubah membatalkan relevansi sebelumnya; video tidak dikarang dari rencana AI. |
| Jadwal dan kuota YouTube | `yt_radar_settings`, `yt_radar_cycles`, `yt_radar_jobs`, `yt_radar_quota` | Interval, cursor, jendela kueri, status, attempt dan reservasi bucket | Kuota lokal bergulir hanya mencakup panggilan radar dan bukan saldo resmi penyedia. Checkpoint metadata dipertahankan bila pengayaan gagal. |
| Lokasi video | `yt_radar_video_locations` | Kutipan metadata, provinsi, status dan hash metadata | Hanya setelah review relevansi dan konfirmasi operator; tidak dianggap lokasi terverifikasi. Metadata berubah atau video tidak tersedia mengeluarkan lokasi dari feed peta. |
| Analisis video | `yt_radar_ai_jobs`, `yt_radar_ai_results` | Provider, model, mode, status, output dan hash snapshot metadata | Pekerjaan AI persisten; hasil perlu review, input media hanya URL resmi, output stale tidak dipakai. Riwayat hasil berhasil dipertahankan. |
| Balasan YouTube | `yt_radar_reply_jobs`, `yt_radar_reply_cursors`, `yt_radar_reply_links` | Job pengumpulan asal, ID induk, ID balasan dan cursor | Dipilih operator; halaman dan relasi commit atomik, batas 20 halaman per sesi, kegagalan dapat dilanjutkan tanpa menghapus sumber. |
| Record dalam pekerjaan pengumpulan | `collection_job_items` | `job_id`, jenis, referensi sumber, waktu observasi | Ditulis atomik bersama halaman dan checkpoint; satu sumber dapat dikaitkan ke banyak pekerjaan. Relasi pekerjaan lama tidak ditebak atau di-backfill dari waktu/kata kunci. |
| Run penyaringan khusus pekerjaan | `screening_runs.job_id`, `max_job_item_id`, `job_item_cursor` | Batas snapshot relasi saat run dimulai dan cursor batch | Record yang terhubung setelah run dimulai memerlukan run baru; record tetap mempertahankan provenance aslinya. |
| Topic | `intel_topics`; topik radar dikelompokkan berdasarkan hasil kategori aturan | Kata kunci dan platform; ruang lingkup eksplisit | Scan manual/terjadwal sesuai pengaturan operator. |
| Issue candidate | `radar_issues` | ID internal, judul sumber, kategori dan waktu pengamatan | Kandidat perlu review; bukan fakta atau ancaman terverifikasi. |
| Issue cluster | `radar_issue_articles` | Relasi issue–artikel, metode dan alasan | Relasi konservatif; artikel dan setiap sumber tidak dihapus ketika dikelompokkan. |
| Claim | `discovery_candidates`, `fact_cases`, `unified_fact_cases` | Terikat referensi sumber atau snapshot bukti | `unreviewed` sampai review manusia dengan bukti. |
| Regional event | `regional_events`, `regional_screen_links` | Kode provinsi, kutipan lokasi, sumber dan hasil penyaringan | Hasil berubah tidak ditampilkan sampai ditinjau ulang; lokasi tetap belum terverifikasi. |

## Source record artikel radar

`radar_articles` menyimpan ID internal, feed asal, ID native, URL sumber, URL kanonis konservatif, judul dan cuplikan feed, waktu publikasi jika tersedia, waktu pengamatan, hash konten, kategori aturan, dan status sumber. Teks feed disimpan terpisah dari judul yang dinormalisasi untuk perbandingan. URL kanonis tidak dipakai sebagai bukti kebenaran atau izin pengambilan halaman artikel.

Status yang dapat disimpulkan dari scan feed adalah `available`, `changed`, atau `unknown`. Artikel yang tidak muncul lagi dalam feed **tidak** otomatis dinyatakan dihapus: feed memiliki cakupan terbatas. Pemeriksaan langsung sumber untuk status `deleted`/`inaccessible` belum tersedia dan harus dilaporkan sebagai keterbatasan.

## Transaksi dan migrasi

Penambahan tabel memakai `CREATE TABLE/INDEX IF NOT EXISTS`; data lama dan `.env` tidak diubah. Pengambilan jaringan dilakukan di luar transaksi SQLite; penulisan artikel, relasi issue dan hasil scan dilakukan dalam transaksi. Backup sebelum migrasi dan pemeriksaan integritas wajib dilakukan. Pengaktifan WAL tidak diasumsikan aman tanpa pengujian pola concurrency yang sesuai.

## Batas interpretasi

Provenance dan SHA-256 membuktikan hubungan dan perubahan pada data yang tersimpan, bukan kebenaran isi. Hasil berbasis judul dan waktu adalah pengelompokan kandidat yang perlu ditinjau, bukan kepastian bahwa artikel menjelaskan kejadian identik. Tidak ada profil ancaman individu atau klasifikasi politik berdasarkan pandangan.

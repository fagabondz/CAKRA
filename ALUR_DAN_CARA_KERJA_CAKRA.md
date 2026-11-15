# Alur dan Cara Kerja CAKRA

Dokumen arsitektur dan alur kerja CAKRA. Petunjuk radar dan peta lama sudah dihapus dari workspace pada audit 15 Nov 2026.

## Tujuan

CAKRA membantu analis memeriksa komentar publik dari video yang dipilih: **URL → metadata → komentar dan balasan → penyaringan dan pola → AI → temuan → investigasi → review manusia**. Sistem tidak mencari video otomatis saat server dimulai. Pola berulang tidak membuktikan otomatisasi atau identitas penulis; klaim AI bukan putusan fakta.

## Mulai dan pengaturan

Jalankan `python server.py serve`, buka localhost, tempel URL watch, youtu.be, Shorts atau live, lalu pilih **Analisis sekarang**. Server memvalidasi dan mengekstrak ID; endpoint YouTube sudah ditentukan dan URL sembarang tidak diunduh. Playlist tanpa video, kanal dan host lain ditolak.

Pengaturan memakai penganalisis Muse (Meta Model API, default muse-spark-1.1); model, balasan, dan batas permintaan YouTube per sesi (1–1000, awal 200) dapat diatur. Kunci disimpan server. Zona berbahaya menyediakan Hapus data nyata dan Hapus data demo dengan konfirmasi; keduanya permanen dan penolakan terjadi saat analisis berjalan. AI menerima teks komentar serta judul/deskripsi video; penggunaan layanan dapat dikenai biaya.

## Metadata dan koleksi

Job menyimpan ID, status dan checkpoint. Metadata `videos.list` diambil dari cache hingga enam jam atau diminta lagi. Snapshot metadata menjaga konteks yang digunakan hasil AI pada analisis tersebut. Jumlah komentar pada metadata berbeda dari jumlah berhasil dikumpulkan.

`collection_jobs.py` memanggil `commentThreads.list` berpaginasi dan menyimpan halaman bertahap tanpa duplikasi. Jika balasan dipilih, `comments.list` dipanggil per induk melalui antrean dan cursor tersendiri. ID, teks asli, URL sumber, waktu yang tersedia, dan hubungan induk–balasan dipertahankan. Waktu kosong tidak direkayasa.

Batas sesi menghitung request CAKRA, bukan saldo resmi YouTube. Bila jaringan, izin atau kuota menghentikan proses, status parsial dan alasannya ditampilkan. Job dapat dilanjutkan dari checkpoint; jumlah terkumpul tidak mengklaim seluruh komentar platform.

## Pemrosesan dan AI

`screening.py` menormalisasi teks, membuat hash dan mencari duplikasi atau kandidat kemiripan dalam batch tanpa mengganti teks sumber. Penanda bantahan mengurangi pengelompokan pandangan berlawanan, tetapi tidak menjamin pemahaman semantik.

`comment_analysis.py` dan `research_analysis.py` merangkum pengulangan teks yang cukup panjang, kandidat teks mirip, URL berulang, serta lonjakan dalam jendela 10 menit. URL di komentar diperlakukan sebagai teks, tidak otomatis dibuka. Batas 100 kelompok per kategori dan ambang heuristik dapat melewatkan pola. Belum tersedia precision/recall terkalibrasi atau benchmark dataset besar.

`ai_providers.py` memakai integrasi Muse (Meta Model API, endpoint `/responses` sesuai quickstart resmi) dengan pembatasan, logging, dan cache. Satu panggilan AI per sumber (seluruh username+komentar); bila respons terpotong, korpus dibelah otomatis dan hasil digabung. Ref hasil harus ada dalam input; kutipan atau klaim harus ada dalam teks input. AI menerima maksimal 1.500 karakter awal setiap komentar; UI menandai pemotongan. Kategori AI mencakup indikasi bot, narasi provokatif, indikasi hoaks, dan aman; hanya tiga kategori pertama menjadi temuan, sisanya dicatat aman. Prompt sistem analis SOC meminta verdict hanya untuk yang terindikasi; label kartu sadar-status (berjalan/gagal/nonaktif/menunggu) dan cakupan AI ditampilkan. Hasil adalah kandidat review. Sumber berubah ditandai; temuan yang tidak sesuai tidak boleh langsung dibuka sebagai investigasi baru.

Jika AI gagal, hasil deterministik tetap tersedia dan status parsial. Penghitung membedakan terkumpul, disaring, dianalisis AI, menunggu dan gagal pada batch terakhir. Kegagalan tidak menghasilkan label palsu.

## Progres dan pemulihan

Status: `QUEUED`, `COLLECTING`, `PROCESSING`, `ANALYZING`, `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLED`. Progres berasal dari backend tanpa persentase simulasi. Worker terkontrol menjalankan maksimal satu analisis pada suatu waktu tanpa memblokir HTTP.

Operator dapat melanjutkan job parsial/gagal atau menghentikan job aktif. Setelah server berhenti, job aktif yang tidak diperbarui selama lima menit ditandai parsial; lanjutkan manual. Permintaan AI yang terputus mungkin sudah dikenai biaya.

## Temuan dan investigasi

Hasil dibagi menjadi pola aktivitas, klaim dan indikasi konten berbahaya. Pilih temuan untuk melihat alasan, jumlah pendukung, teks, waktu dan tautan sumber. Daftar bukti berpaginasi. **Investigasi lebih lanjut** membuat kasus dengan snapshot bukti secara idempoten; maksimal 20 bukti disalin ke kasus, sedangkan semua referensi temuan tetap tersedia.

Klaim membuat kandidat fact verification yang belum ditinjau. Dalam Investigasi, analis dapat menambahkan catatan dan referensi, mencari kandidat Google Fact Check jika tersedia, membuka sumber, menyimpan review serta menutup atau membuka kembali kasus. Pencarian bukan verdict; periksa sumber, tanggal dan konteks. Gunakan hasil tidak cukup jika bukti belum memadai.

## Penyimpanan dan modul

SQLite yang ada digunakan kembali; migrasi bersifat tambahan dan idempoten. Riwayat dan investigasi lama dipertahankan. Backup pengembangan disimpan privat di luar paket distribusi; buat backup database operator sebelum migrasi dan jangan menimpa database operasional saat memeriksanya.

Validasi URL, query terparameterisasi, escaping dan pembatasan ukuran/worker mengurangi risiko; AI tetap dapat salah. Modul utama: `server.py` untuk API dan worker, `comment_analysis.py` untuk orkestrasi dan temuan, `collection_jobs.py`/`screening.py`/`research_analysis.py` untuk koleksi dan pola, `ai_providers.py` untuk AI, `investigation.py`/`unified_fact.py`/`factcheck.py` untuk bukti dan review, serta `dashboard.html` dan `comment-workspace.css/js` untuk antarmuka.

Lihat [progres](docs/IMPLEMENTATION_PROGRESS.md) dan [langkah manual](docs/MANUAL_ACTIONS.md).

## Input simulasi terisolasi

Di tab **Data Simulasi**, masukkan satu komentar per baris atau impor CSV UTF-8. Header `comment` wajib; `username`, `published_at`, dan `parent_id` opsional. Waktu harus menyertakan zona waktu ISO 8601. Input divalidasi dan ditampilkan dalam pratinjau sebelum analisis; batas 1.000 komentar dan 200.000 karakter. Tidak ada komentar, akun, tautan, atau waktu bawaan.

Input disimpan hanya di `cakra_demo.db` dengan dataset simulasi, lalu menggunakan penyaringan, pencocokan, analisis pola, dan batch AI yang sama. Server tidak memanggil YouTube untuk simulasi. Analisis AI hanya berjalan jika dicentang secara eksplisit; model Muse mengikuti pengaturan. Permintaan dapat dikenai biaya dan teks dikirim kepada penyedia. Jika AI gagal, status parsial dan hasil pola tetap tersedia. Anggaran dan catatan penggunaan AI lokal simulasi tersimpan di database simulasi yang terpisah.

Pilih temuan dan **Investigasi lebih lanjut** untuk membuat kasus pada ruang simulasi terpisah; pilih ruang data nyata atau simulasi di area Investigasi. Catatan dan review simulasi tetap berada di database simulasi. Teks tanpa waktu/identitas tidak memperoleh metadata buatan, dan indikator temporal ditandai data tidak cukup. Sentimen dan klasifikasi bukan fakta terverifikasi. Tekan **Hapus data simulasi** dan setujui konfirmasi untuk menghapus seluruh input, hasil, penggunaan AI lokal dan kasus simulasi; database nyata tidak disentuh.

Pada investigasi simulasi, referensi dan review klaim juga tersimpan di `cakra_demo.db`. Pencarian referensi hanya dilakukan setelah operator menekannya; klaim dan referensi simulasi tetap berlabel SIMULASI, dan hasil pencarian tidak otomatis menjadi verdict.

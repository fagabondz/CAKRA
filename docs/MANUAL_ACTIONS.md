# Tindakan operator

Dokumen ini mencatat verifikasi yang belum dilakukan, bukan instruksi untuk mengaktifkan radar atau peta. Jangan membagikan kunci API melalui chat.

## Uji layanan YouTube dan AI

1. Pastikan YouTube Data API aktif dan `YOUTUBE_API_KEY` tersedia di `.env` lokal. Pastikan kunci Meta Model API dan model Muse juga tersedia; lihat `.env.example`.
2. Periksa izin, kuota, dan biaya penyedia. Setelah menyetujui penggunaan API, jalankan server dan analisis satu video publik yang dipilih operator dengan batas sesi rendah.
3. Periksa judul dan tautan video, jumlah komentar dan balasan, status job, checkpoint bila terhenti, serta kutipan AI terhadap sumber asli. Jika terjadi batas layanan, uji **Lanjutkan analisis** setelah kuota tersedia.
4. Periksa penggunaan pada konsol penyedia. Hasil fixture lokal tidak membuktikan bahwa akun, model, kuota atau jaringan dapat digunakan.

Status: **belum diverifikasi dengan layanan nyata pada sesi ini**. Jangan menempelkan kredensial atau komentar sensitif ke laporan pengujian.

## Pemeriksaan fakta

Jika Google Fact Check API tersedia untuk akun operator, cari klaim yang tidak rahasia dari kasus investigasi. Buka referensi sumber, periksa klaim dan konteksnya, lalu simpan catatan dan review manusia. Hasil pencarian adalah kandidat referensi, bukan putusan otomatis. Status verifikasi nyata: belum dilakukan pada sesi ini.

## Uji browser

Jalankan server lokal dan periksa alur input URL, pilihan analisis lama atau pembaruan, status dan kelanjutan job, pagination temuan, pembukaan investigasi, catatan dan review. Uji keyboard serta lebar layar laptop dan ponsel. Periksa bahwa tidak ada request eksternal sampai pengguna memulai analisis atau pencarian referensi. Status verifikasi interaktif: belum dilakukan pada sesi ini.

## Arsip

Instruksi manual historis, termasuk pengujian radar dan peta, sudah dihapus dari workspace pada audit 15 Nov 2026 dan tidak berlaku sebagai alur operasional baru.

## Input simulasi manual

Gunakan tab Data Simulasi untuk menguji input teks atau CSV milik sendiri. AI tidak aktif secara default; sebelum mencentangnya, periksa kunci, model dan biaya penyedia. Periksa bahwa hasil dan kasus berlabel SIMULASI — BUKAN DATA NYATA, metadata yang tidak tersedia tetap kosong, dan penghapusan meminta konfirmasi. Database simulasi `cakra_demo.db` terpisah dari data nyata. Untuk verifikasi browser, uji tab, pratinjau, CSV, navigasi keyboard, ruang investigasi, dan penghapusan pada ukuran desktop serta mobile. Tes fixture tidak membuktikan akses provider nyata.

## FINAL-001 — Uji provider dan model nyata (G1, G3, G7)

- **Status:** PENDING MANUAL. Provider tunggal Muse sejak 15 Nov 2026 (Gemini dihapus; default muse-spark-1.1). Kunci/kuota akun operator belum diverifikasi; tidak ada uji generation berbiaya dalam sesi ini.
- **Dokumentasi:** [Meta API](https://dev.meta.ai/docs/api-reference).
- **Langkah:** operator memeriksa key, model, billing dan izin pada penyedia tanpa membagikan rahasia. Setelah menyetujui potensi biaya, buka Pengaturan, tekan **Periksa daftar model**, pilih model Muse dengan dukungan generation, lalu **Uji respons AI** dan konfirmasi. Daftar model saja bukan bukti generasi berhasil.
- **Output/verifikasi:** status generation berhasil dengan waktu/model; jalankan satu CSV simulasi dengan AI diaktifkan, pastikan hasil tersimpan dan muncul, restart server dan buka riwayat. Jika gagal, catat hanya HTTP/status tersanitasi dan periksa model, API version, akses dan jaringan. Jangan membagikan key. Lakukan review manusia.

## FINAL-002 — Koleksi YouTube live (G2, G7)

- **Status:** PENDING MANUAL. Tes lokal menggunakan fixture; URL publik dan akses/kuota akun operator belum diverifikasi di sesi ini.
- **Dokumentasi:** [commentThreads.list](https://developers.google.com/youtube/v3/docs/commentThreads/list), [comments.list](https://developers.google.com/youtube/v3/docs/comments/list).
- **Langkah:** setelah memastikan izin dan kuota, pilih satu video publik dengan komentar, jalankan analisis, periksa metadata, pagination, balasan, jumlah dan status. Jika item dikarantina, status harus parsial dan analisis baru dapat mencoba sumber kembali.
- **Output/verifikasi:** tautan komentar dan jumlah aktual dapat ditelusuri; jangan menyamakan jumlah metadata platform dengan jumlah terkumpul. Catat kesalahan dan tidak menampilkan rahasia.

## FINAL-003 — Browser dan demo (G5, G7)

- **Status:** PENDING MANUAL. Tidak tersedia alat browser interaktif yang terverifikasi pada sesi ini; gate visual terdahulu berakhir ERROR/timeout.
- **Langkah:** gunakan `DEMO_RUNBOOK.md` pada 1440×900, 1366×768 dan 390×844. Periksa tab, keyboard, CSV, progress parsial/AI gagal, temuan, bukti, simpan review, tutup/buka kembali, dan pemisahan nyata/simulasi.
- **Output/verifikasi:** dokumentasikan tampilan dan interaksi yang benar tanpa mengambil screenshot kunci, komentar sensitif, atau data nyata yang tidak diizinkan. Jangan tandai siap demo sebelum jalur yang dipilih berhasil end-to-end.

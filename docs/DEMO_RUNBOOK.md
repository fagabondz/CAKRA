# Runbook demo CAKRA (3–4 menit)

**Status demo:** jangan klaim siap sebelum hasil AI nyata dan interaksi browser diverifikasi sesuai `RELEASE_GATE_STATUS.md`. Siapkan URL publik atau CSV simulasi milik operator. Jangan menampilkan `.env`, data pribadi atau database riil kepada pihak yang tidak berwenang.

1. Jalankan `python server.py serve` dari folder aplikasi di Windows; buka `http://127.0.0.1:8765`. Hentikan dengan `Ctrl+C`. `start_windows.bat` memakai `python`; pastikan Python 3 tersedia pada PATH.
2. Pada Pengaturan pilih provider/model yang benar-benar berhasil diuji untuk akun. **Periksa daftar model** hanya menunjukkan model terdaftar. **Uji respons AI** mengirim satu teks netral setelah konfirmasi biaya. Periksa status generation; jangan membagikan kunci. Bila tidak tersedia, lanjutkan hanya sebagai demo pola non-AI dengan status parsial/AI tidak tersedia.
3. Pilih URL YouTube, atau tab **Data Simulasi** dan impor CSV operator (header `comment`; `username`, `published_at`, `parent_id` opsional). Pastikan label SIMULASI terlihat dan periksa pratinjau. Aktifkan AI secara eksplisit bila disetujui. Mulai analisis.
4. Tunjukkan status aktual, komentar terkumpul, disaring, dianalisis AI, menunggu dan gagal. Angka komentar pada metadata YouTube bukan jumlah terkumpul. Tampilkan tiga kategori tanpa mengisi temuan palsu. Indikator pola bukan bukti bot; klaim bukan putusan hoaks.
5. Pilih temuan, buka bukti dan **Investigasi lebih lanjut**. Jelaskan sumber, keterbatasan dan status review. Tambahkan catatan analis dan simpan review manusia; tutup lalu buka kembali bila ingin memperlihatkan persistensi. Kasus simulasi tetap di ruang simulasi, bukan kejadian nyata.
6. Jika koleksi mengarantina item invalid, hasil tetap parsial dan jumlahnya ditampilkan pada alasan; perbarui analisis untuk mencoba kembali sumber. Bila AI gagal, periksa provider/model dan gunakan **Lanjutkan analisis** setelah memperbaiki penyebab. Tidak ada persentase palsu.

Kegagalan layanan, pengujian browser, dan langkah verifikasi riil dijelaskan di `MANUAL_ACTIONS.md`. Jangan memakai CSV atau database operasional sebagai lampiran tanpa otorisasi.


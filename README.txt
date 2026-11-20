RetroDroid Compat — Render Docker Sunucu Paketi (v4.3)
======================================================

Bu paketteki dosyalar:
1. Dockerfile        -> Java 17 + apktool + aapt + zipalign + apksigner + keystore
2. requirements.txt  -> FastAPI, Uvicorn, python-multipart
3. server.py         -> Gercek APK analiz, targetSdk (varsayilan API 28), android:exported,
                        resources.arsc STORE (sikistirmasiz), zipalign ve v1/v2/v3 apksigner verify
4. index.html        -> Render sunucusuna dogrudan bagli RetroDroid Compat arayuzu

RENDER'A YUKLEME ADIMLARI:
1) Bu ZIP icindeki 4 dosyayi (Dockerfile, requirements.txt, server.py, index.html)
   GitHub deponun ana dizinine (eski dosyalarin uzerine) yukle.
2) Render panelinde servisine girip "Manual Deploy" -> "Clear build cache & deploy" de.
3) Deploy bitince telefonunun Chrome tarayicisindan dogrudan Render adresini ac:
   https://<senin-servisin>.onrender.com
4) Ustteki yesil "Render Aktif" rozetini gorunce .apk dosyani sec,
   Hedef API olarak "API 28 (Onerilen)" secili kalsin ve "Uyumlu Hale Getir"e bas.
5) Islem bitince "Dosyayi Cihaza Indir" -> "Render Imzali & Dogrulanmis APK Indir" ile kur.

"PAKET GECERSIZ GORUNDUGUNDEN UYGULAMA YUKLENEMEDI" HATASINI ONLEME:
- Oyunun eski/orijinal surumu telefonda yukluyse kurmadan once mutlaka kaldir.
- API 28 (Android 9) secenegi Android 14/15/16 engelini asar ve eski oyunlari bozmaz.
- Yine de hata verirse "Yalnizca Hizala & Imzala (Guvenli Mod)" secenegini dene.
- Analiz kartinda "Yalnizca 32-bit (ARMv7a)" uyarisi cikiyorsa ve telefonun yalnizca
  64-bit destekliyorsa (Snapdragon 8 Gen 3/Elite, Pixel 7+), Android bu oyunu donanimsal
  olarak kurmaya izin vermez.

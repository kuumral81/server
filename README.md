# RetroDroid Compat — Render APK Analiz & İmzalama Sunucusu

Bu depo (`https://github.com/kuumral81/J`) **RetroDroid Compat** web arayüzünü (`index.html`) ve gerçek `aapt` + `apktool` + `zipalign` + `apksigner` çalıştıran FastAPI sunucusunu (`server.py`) tek Docker konteyneri içinde barındırır.

## Render Üzerinde Yayınlama

1. [dashboard.render.com](https://dashboard.render.com) → **New +** → **Web Service**
2. GitHub deposu olarak **`kuumral81/J`** seçin.
3. Runtime otomatik **Docker** olarak algılanır (**Region:** Frankfurt, **Plan:** Free).
4. **Deploy Web Service** butonuna basın.
5. Deploy bitince verilen `https://....onrender.com` adresini Chrome'da açın.

## Notlar
- Render Free planında 512 MB RAM sınırı nedeniyle varsayılan sunucu yükleme limiti **150 MB** (`MAX_UPLOAD_MB=150`) olarak ayarlanmıştır; ücretli plana geçerseniz Render Environment Variables üzerinden `5120` (5 GB) yapabilirsiniz.
- Sunucu 15 dakika işlem olmazsa uyku moduna geçer; sayfa açıldığında otomatik olarak `/health` isteğiyle uyandırılır.

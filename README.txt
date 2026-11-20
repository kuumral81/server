RetroDroid Compat — Genel Amaçlı Render Sunucu Paketi (v4.5)
============================================================

Bu paket belirli tek bir oyun için değil, yüklediğin TÜM Android uygulama ve
oyun paketleri için genel olarak çalışır.

Desteklenen Biçimler (5 GB'a kadar, 8 MB parçalı yükleme):
- .apk   (Tekil klasik ve modern APK paketleri)
- .xapk  (APK + OBB veya çoklu Split APK içeren XAPK paketleri)
- .apks / .apkm / .zip (Parçalı Split APK veya APK+OBB arşivleri)

Tüm Yüklenen Dosyalar İçin Otomatik İşlem Akışı:
1. 8 MB parçalarla sunucuya güvenli yükleme (büyük dosyalarda kopma ve RAM taşması olmaz).
2. Gerçek aapt analizi: Paket kimliği (packageId), minSdk, targetSdk, ABI (32/64-bit) ve OBB tespiti.
3. Evrensel TargetSDK yükseltme (varsayılan API 28):
   - Manifestte targetSdkVersion varsa 4 baytlık hızlı Binary AXML yaması (< 10 ms, < 1 MB RAM).
   - Çok eski APK'larda targetSdkVersion hiç yoksa veya API 31+ (android:exported) seçildiyse,
     dev assets/lib dosyalarını RAM'e almadan yalnızca hafif manifest kopyası (slim apktool) derlenir.
4. Akışlı (1 MB streaming) ZIP yeniden yazma: eski META-INF temizlenir, resources.arsc ve .so
   dosyaları Android 11+ kuralına uygun şekilde STORE (sıkıştırmasız) kaydedilir.
5. zipalign -f -p 4 + apksigner v1/v2/v3 imzalama + apksigner verify doğrulaması.
6. Split APK içeren paketlerde tüm parçalar AYNI anahtarla imzalanıp kurulabilir .apks paketi üretilir.

RENDER'A YÜKLEME:
1) Dockerfile, requirements.txt, server.py ve index.html dosyalarını GitHub depona yükle.
2) Render panelinde "Manual Deploy -> Clear build cache & deploy" yap.
3) Telefonundan Chrome ile https://<senin-servisin>.onrender.com adresini aç.

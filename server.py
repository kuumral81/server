import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

app = FastAPI(title="RetroDroid Compat Render API", version="4.3")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WORK_DIR = Path("/tmp/retrodroid_jobs")
WORK_DIR.mkdir(parents=True, exist_ok=True)
KEYSTORE = Path("/app/retrodroid.jks")
KEY_PASS = os.environ.get("KEYSTORE_PASS", "123456")
# Render Free planda 512 MB RAM oldugu icin varsayilan 120 MB; ortam degiskeniyle artirilabilir
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "120"))
MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024
JOB_TTL_SECONDS = 3600  # 1 saatten eski gecici isleri temizle

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)


def cleanup_old_jobs():
    now = time.time()
    for item in WORK_DIR.iterdir():
        try:
            if item.is_dir() and (now - item.stat().st_mtime) > JOB_TTL_SECONDS:
                shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass


def run_cmd(cmd: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def save_upload_stream(upload: UploadFile, dest: Path) -> int:
    total = 0
    with dest.open("wb") as out:
        while chunk := await upload.read(2 * 1024 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Dosya sunucu sınırını ({MAX_UPLOAD_MB} MB) aşıyor.",
                )
            out.write(chunk)
    if total < 100:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Dosya boş veya geçersiz.")
    return total


def inspect_apk(apk_path: Path) -> dict:
    # PK/ZIP basligi kontrolu
    with apk_path.open("rb") as f:
        head = f.read(4)
    if len(head) < 2 or head[:2] != b"PK":
        raise HTTPException(status_code=400, detail="Geçerli bir APK/ZIP arşivi değil (PK başlığı yok).")

    proc = run_cmd(["aapt", "dump", "badging", str(apk_path)], timeout=60)
    out = proc.stdout or ""

    pkg = re.search(r"package: name='([^']+)'", out)
    ver_name = re.search(r"versionName='([^']*)'", out)
    ver_code = re.search(r"versionCode='([^']*)'", out)
    app_label = re.search(r"application-label(?:-[a-zA-Z_-]+)?:'([^']+)'", out)
    min_sdk = re.search(r"sdkVersion:'(\d+)'", out)
    target_sdk = re.search(r"targetSdkVersion:'(\d+)'", out)

    abis = set()
    has_arsc = False
    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            for name in z.namelist():
                if name == "resources.arsc":
                    has_arsc = True
                elif name.startswith("lib/") and name.endswith(".so"):
                    parts = name.split("/")
                    if len(parts) >= 3:
                        abis.add(parts[1])
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="APK arşivi bozuk veya eksik inmiş.")

    abi_list = sorted(abis)
    has_32 = any(a in abi_list for a in ("armeabi", "armeabi-v7a", "x86"))
    has_64 = any(a in abi_list for a in ("arm64-v8a", "x86_64"))
    is_32bit_only = bool(has_32 and not has_64)

    warnings = []
    t_sdk = int(target_sdk.group(1)) if target_sdk else 0
    if is_32bit_only:
        warnings.append(
            "Bu APK yalnızca 32-bit (ARMv7a) kütüphane içeriyor. "
            "Yalnızca 64-bit destekleyen modern telefonlarda (Snapdragon 8 Gen 3/Elite, Pixel 7+) "
            "Android kurulumu yine engelleyebilir."
        )
    if t_sdk and t_sdk < 23:
        warnings.append(
            f"Orijinal targetSdkVersion ({t_sdk}) Android 14/15/16 kurulum sınırının (23) altında. "
            "API 28'e yükseltilmesi önerilir."
        )

    return {
        "packageId": pkg.group(1) if pkg else "bilinmiyor",
        "appLabel": app_label.group(1) if app_label else apk_path.stem,
        "versionName": ver_name.group(1) if ver_name else "",
        "versionCode": ver_code.group(1) if ver_code else "",
        "minSdk": int(min_sdk.group(1)) if min_sdk else 0,
        "targetSdk": t_sdk,
        "abis": abi_list if abi_list else ["Saf Java/DEX (Mimari Bağımsız)"],
        "is32BitOnly": is_32bit_only,
        "hasResourcesArsc": has_arsc,
        "warnings": warnings,
    }


def patch_manifest_for_modern_android(manifest_path: Path, target_sdk: int) -> list[str]:
    notes = []
    if not manifest_path.exists():
        return notes
    try:
        tree = ET.parse(manifest_path)
        root = tree.getroot()
    except Exception as exc:
        notes.append(f"Manifest XML ayrıştırılamadı (atlandı): {exc}")
        return notes

    exported_attr = f"{{{ANDROID_NS}}}exported"
    extract_attr = f"{{{ANDROID_NS}}}extractNativeLibs"
    app_el = root.find("application")

    if app_el is not None:
        # Android 6+ eski .so hizalama sorunlarini azaltmak icin extractNativeLibs=true
        if extract_attr not in app_el.attrib:
            app_el.set(extract_attr, "true")
            notes.append("Manifest: android:extractNativeLibs='true' eklendi.")

        # Android 12+ (targetSdk >= 31) icin intent-filter iceren her bilesene android:exported zorunlu
        if target_sdk >= 31:
            added_exported = 0
            for tag in ("activity", "activity-alias", "service", "receiver"):
                for comp in app_el.findall(tag):
                    if comp.find("intent-filter") is not None and exported_attr not in comp.attrib:
                        comp.set(exported_attr, "true")
                        added_exported += 1
            if added_exported:
                notes.append(f"Android 12+ kuralı: {added_exported} bileşene android:exported='true' eklendi.")

    # uses-sdk icinde sabit targetSdkVersion varsa guncelle
    uses_sdk = root.find("uses-sdk")
    if uses_sdk is not None:
        t_attr = f"{{{ANDROID_NS}}}targetSdkVersion"
        if t_attr in uses_sdk.attrib:
            uses_sdk.set(t_attr, str(target_sdk))
            notes.append(f"uses-sdk targetSdkVersion -> {target_sdk} güncellendi.")

    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
    return notes


def sanitize_apk_zip(apk_in: Path, apk_out: Path) -> None:
    """
    Eski META-INF imzalarini temizler, Android 11+ icin resources.arsc ve .so
    dosyalarini SIKISTIRILMAMIS (ZIP_STORED) olarak yeniden yazar.
    """
    with zipfile.ZipFile(apk_in, "r") as zin, zipfile.ZipFile(apk_out, "w") as zout:
        for item in zin.infolist():
            name = item.filename
            # Eski imza dosyalarini kaldir
            if name.startswith("META-INF/") and (
                name.endswith((".SF", ".RSA", ".DSA", ".EC"))
                or name == "META-INF/MANIFEST.MF"
                or name.startswith("META-INF/SIG-")
            ):
                continue
            data = zin.read(name)
            info = zipfile.ZipInfo(name)
            info.date_time = item.date_time
            info.external_attr = item.external_attr
            if name == "resources.arsc" or name.endswith(".so"):
                info.compress_type = zipfile.ZIP_STORED
            else:
                info.compress_type = item.compress_type
            zout.writestr(info, data)


@app.get("/health")
def health():
    cleanup_old_jobs()
    return {
        "status": "ok",
        "engine": "RetroDroid Render Server v4.3",
        "maxUploadMB": MAX_UPLOAD_MB,
        "keystoreReady": KEYSTORE.exists(),
    }


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    idx = Path("/app/index.html")
    if idx.exists():
        return idx.read_text(encoding="utf-8")
    return "<h3>RetroDroid Render API aktif. <a href='/docs'>API Dokümantasyonu (/docs)</a></h3>"


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    cleanup_old_jobs()
    fname = Path(file.filename or "uygulama.apk").name
    if not fname.lower().endswith(".apk"):
        raise HTTPException(
            status_code=400,
            detail="Sunucu gerçek APK analizi şu an .apk dosyalarını destekler (.xapk dosyaları için önce içindeki .apk'yı çıkarın).",
        )

    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    apk_path = job_dir / fname

    try:
        size_bytes = await save_upload_stream(file, apk_path)
        meta = inspect_apk(apk_path)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"APK analiz hatası: {exc}")

    return {
        "jobId": job_id,
        "fileName": fname,
        "sizeBytes": size_bytes,
        **meta,
    }


@app.post("/api/patch-sign")
async def api_patch_sign(
    jobId: str = Form(...),
    targetSdk: int = Form(28),
    mode: str = Form("patch_and_sign"),  # "patch_and_sign" | "resign_only"
    resign: bool = Form(True),
):
    if not re.fullmatch(r"[a-f0-9]{12}", jobId):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği (jobId).")

    job_dir = WORK_DIR / jobId
    if not job_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="APK işi bulunamadı (sunucu uyku sonrası sıfırlanmış olabilir). Dosyayı yeniden yükleyin.",
        )

    apks = [p for p in job_dir.glob("*.apk") if not p.stem.endswith("_uyumlu")]
    if not apks:
        raise HTTPException(status_code=404, detail="Kaynak APK dosyası bulunamadı.")

    src_apk = apks[0]
    target_sdk = max(23, min(35, int(targetSdk)))
    logs: list[str] = []

    dec_dir = job_dir / "decoded"
    rebuilt = job_dir / "rebuilt.apk"
    sanitized = job_dir / "sanitized.apk"
    aligned = job_dir / "aligned.apk"
    signed = job_dir / f"{src_apk.stem}_uyumlu.apk"

    try:
        apk_for_zip_fix = src_apk

        if mode == "patch_and_sign":
            logs.append(f"[APKTOOL] Kaynak açılıyor (-s, DEX korunuyor): {src_apk.name}")
            d_res = run_cmd(["apktool", "d", "-s", "-f", str(src_apk), "-o", str(dec_dir)], timeout=180)
            if d_res.returncode != 0:
                logs.append("[UYARI] Apktool kaynak açma başarısız oldu; güvenli 'Yalnızca Hizala & İmzala' moduna geçiliyor.")
                logs.append((d_res.stderr or d_res.stdout or "")[:300].strip())
                apk_for_zip_fix = src_apk
            else:
                yml = dec_dir / "apktool.yml"
                if yml.exists():
                    txt = yml.read_text(encoding="utf-8", errors="ignore")
                    if re.search(r"targetSdkVersion:", txt):
                        txt = re.sub(
                            r"targetSdkVersion:\s*['\"]?\d+['\"]?",
                            f"targetSdkVersion: '{target_sdk}'",
                            txt,
                        )
                    elif "sdkInfo:" in txt:
                        txt = txt.replace("sdkInfo:", f"sdkInfo:\n  targetSdkVersion: '{target_sdk}'", 1)
                    else:
                        txt += f"\nsdkInfo:\n  targetSdkVersion: '{target_sdk}'\n"
                    yml.write_text(txt, encoding="utf-8")
                    logs.append(f"[MANIFEST] apktool.yml targetSdkVersion -> {target_sdk} ayarlandı.")

                manifest_notes = patch_manifest_for_modern_android(dec_dir / "AndroidManifest.xml", target_sdk)
                for note in manifest_notes:
                    logs.append(f"[MANIFEST] {note}")

                # Once aapt2 ile derlemeyi dene; eski kaynaklarda hata verirse standart apktool b dene
                b_res = run_cmd(["apktool", "b", "--use-aapt2", str(dec_dir), "-o", str(rebuilt)], timeout=180)
                if b_res.returncode != 0 or not rebuilt.exists():
                    logs.append("[APKTOOL] aapt2 derlemesi başarısız, klasik aapt1 ile yeniden deneniyor...")
                    b_res = run_cmd(["apktool", "b", str(dec_dir), "-o", str(rebuilt)], timeout=180)

                if b_res.returncode == 0 and rebuilt.exists():
                    logs.append("[APKTOOL] Yeniden paketleme başarılı.")
                    apk_for_zip_fix = rebuilt
                else:
                    logs.append("[UYARI] Apktool yeniden derleyemedi; kaynakları bozmamak için orijinal APK hizalanıp imzalanacak.")
                    logs.append((b_res.stderr or b_res.stdout or "")[:300].strip())
                    apk_for_zip_fix = src_apk
        else:
            logs.append("[MOD] Güvenli mod: Orijinal kaynaklar korunarak hizalama + yeniden imzalama yapılıyor.")

        # resources.arsc ve .so dosyalarini sikistirilmamis (STORE) yap, eski META-INF'i temizle
        sanitize_apk_zip(apk_for_zip_fix, sanitized)
        logs.append("[ZIP] Eski META-INF imzaları temizlendi; resources.arsc ve .so dosyaları STORE (sıkıştırmasız) yapıldı.")

        # 4-byte + sayfa hizalama (MUTLAKA apksigner oncesinde yapilmali)
        z_res = run_cmd(["zipalign", "-f", "-p", "4", str(sanitized), str(aligned)], timeout=120)
        if z_res.returncode != 0 or not aligned.exists():
            raise HTTPException(status_code=500, detail=f"zipalign hatası: {z_res.stderr or z_res.stdout}")
        logs.append("[ZIPALIGN] 4-byte ve sayfa hizalama (-p 4) tamamlandı.")

        # v1 + v2 + v3 imzalama ve kesin dogrulama
        if resign:
            s_res = run_cmd([
                "apksigner", "sign",
                "--ks", str(KEYSTORE),
                "--ks-pass", f"pass:{KEY_PASS}",
                "--key-pass", f"pass:{KEY_PASS}",
                "--v1-signing-enabled", "true",
                "--v2-signing-enabled", "true",
                "--v3-signing-enabled", "true",
                "--out", str(signed),
                str(aligned),
            ], timeout=120)
            if s_res.returncode != 0 or not signed.exists():
                raise HTTPException(status_code=500, detail=f"apksigner imzalama hatası: {s_res.stderr or s_res.stdout}")
            logs.append("[SIGN] APK Signature Scheme v1 + v2 + v3 imzası uygulandı.")

            v_res = run_cmd(["apksigner", "verify", "--verbose", str(signed)], timeout=60)
            if v_res.returncode != 0:
                signed.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail=f"İmza doğrulaması başarısız: {v_res.stderr or v_res.stdout}")
            logs.append("[VERIFY] apksigner verify BAŞARILI — Geçerli ve kurulabilir APK üretildi!")
        else:
            shutil.copy(aligned, signed)
            logs.append("[UYARI] Yeniden imzalama kapalı olduğu için imzasız APK üretildi (Android kurmaz).")

        final_meta = inspect_apk(signed)
        return {
            "jobId": jobId,
            "fileName": signed.name,
            "sizeBytes": signed.stat().st_size,
            "downloadUrl": f"/api/download/{jobId}/{signed.name}",
            "verified": bool(resign),
            "targetSdk": final_meta.get("targetSdk", target_sdk),
            "is32BitOnly": final_meta.get("is32BitOnly", False),
            "warnings": final_meta.get("warnings", []),
            "logs": logs,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="APK işleme zaman aşımına uğradı (dosya Render Free için fazla büyük olabilir).")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Beklenmeyen sunucu hatası: {exc}")
    finally:
        shutil.rmtree(dec_dir, ignore_errors=True)
        for tmp in (rebuilt, sanitized, aligned):
            tmp.unlink(missing_ok=True)


@app.get("/api/download/{job_id}/{filename}")
def api_download(job_id: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği.")
    target = WORK_DIR / job_id / Path(filename).name
    if not target.exists() or target.stat().st_size < 100:
        raise HTTPException(status_code=404, detail="İmzalı APK bulunamadı veya süresi doldu.")
    return FileResponse(
        path=target,
        filename=target.name,
        media_type="application/vnd.android.package-archive",
    )

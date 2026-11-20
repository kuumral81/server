import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

app = FastAPI(title="RetroDroid Compat — Render APK API", version="4.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WORK_DIR = Path("/tmp/retrodroid_jobs")
WORK_DIR.mkdir(parents=True, exist_ok=True)
KEYSTORE = Path("/app/retrodroid.jks")
KEY_PASS = os.getenv("KEYSTORE_PASS", "123456")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "150"))
MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024
JOB_TTL_SECONDS = 30 * 60  # 30 dakikadan eski geçici işleri temizle

API_TO_ANDROID = {
    9: "Android 2.3",
    10: "Android 2.3.3",
    11: "Android 3.0",
    14: "Android 4.0",
    15: "Android 4.0.3",
    16: "Android 4.1",
    17: "Android 4.2",
    18: "Android 4.3",
    19: "Android 4.4",
    21: "Android 5.0",
    22: "Android 5.1",
    23: "Android 6.0",
    24: "Android 7.0",
    25: "Android 7.1",
    26: "Android 8.0",
    27: "Android 8.1",
    28: "Android 9.0",
    29: "Android 10",
    30: "Android 11",
    31: "Android 12",
    33: "Android 13",
    34: "Android 14",
    35: "Android 15",
    36: "Android 16",
}


def cleanup_old_jobs() -> None:
    now = time.time()
    for item in WORK_DIR.iterdir():
        if not item.is_dir():
            continue
        try:
            if now - item.stat().st_mtime > JOB_TTL_SECONDS:
                shutil.rmtree(item, ignore_errors=True)
        except OSError:
            pass


def sanitize_filename(name: str) -> str:
    base = Path(name or "paket.apk").name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "paket.apk"


async def save_upload_stream(upload: UploadFile, dest: Path) -> int:
    total = 0
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(2 * 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Dosya sunucu sınırını ({MAX_UPLOAD_MB} MB) aşıyor.",
                )
            out.write(chunk)
    return total


def inspect_apk(apk_path: Path) -> dict:
    proc = subprocess.run(
        ["aapt", "dump", "badging", str(apk_path)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    out = proc.stdout or ""
    pkg_m = re.search(r"package: name='([^']+)'", out)
    ver_code_m = re.search(r"versionCode='([^']+)'", out)
    ver_name_m = re.search(r"versionName='([^']+)'", out)
    label_m = re.search(r"application-label:'([^']+)'", out)
    min_sdk_m = re.search(r"sdkVersion:'(\d+)'", out)
    target_sdk_m = re.search(r"targetSdkVersion:'(\d+)'", out)

    abis = set()
    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            for name in z.namelist():
                if name.startswith("lib/") and name.endswith(".so"):
                    parts = name.split("/")
                    if len(parts) >= 3:
                        abis.add(parts[1])
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Geçersiz APK/ZIP arşivi.")

    min_sdk = int(min_sdk_m.group(1)) if min_sdk_m else 0
    target_sdk = int(target_sdk_m.group(1)) if target_sdk_m else min_sdk
    android_label = API_TO_ANDROID.get(target_sdk or min_sdk, f"API {target_sdk or min_sdk}")

    return {
        "packageId": pkg_m.group(1) if pkg_m else "bilinmiyor",
        "appName": label_m.group(1) if label_m else apk_path.stem,
        "versionCode": ver_code_m.group(1) if ver_code_m else "1",
        "versionName": ver_name_m.group(1) if ver_name_m else "1.0",
        "minSdk": min_sdk,
        "targetSdk": target_sdk,
        "legacyAndroid": android_label,
        "abis": sorted(abis) or ["Saf Java/DEX (Mimari Bağımsız)"],
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "engine": "RetroDroid Render Server v4.2",
        "maxUploadMB": MAX_UPLOAD_MB,
        "tools": {
            "aapt": bool(shutil.which("aapt")),
            "apktool": bool(shutil.which("apktool")),
            "zipalign": bool(shutil.which("zipalign")),
            "apksigner": bool(shutil.which("apksigner")),
            "keystore": KEYSTORE.exists(),
        },
    }


@app.get("/", response_class=HTMLResponse)
def serve_index():
    idx = Path("/app/index.html")
    if idx.exists():
        return idx.read_text(encoding="utf-8")
    return "<h2>RetroDroid Compat API Aktif</h2><p>Test için <a href='/docs'>/docs</a> veya <a href='/health'>/health</a> açın.</p>"


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    cleanup_old_jobs()
    safe_name = sanitize_filename(file.filename or "paket.apk")
    lower = safe_name.lower()
    if not (lower.endswith(".apk") or lower.endswith(".xapk")):
        raise HTTPException(status_code=400, detail="Yalnızca .apk veya .xapk dosyaları analiz edilebilir.")

    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    uploaded_path = job_dir / safe_name
    size_bytes = await save_upload_stream(file, uploaded_path)

    if lower.endswith(".apk"):
        meta = inspect_apk(uploaded_path)
        return {
            "jobId": job_id,
            "fileName": safe_name,
            "ext": "APK",
            "sizeBytes": size_bytes,
            "hasObb": False,
            **meta,
        }

    # .xapk içeriğinden manifest.json ve dahili .apk bilgisini çıkar
    has_obb = False
    inner_apk_path = None
    xapk_manifest = {}
    try:
        with zipfile.ZipFile(uploaded_path, "r") as z:
            names = z.namelist()
            has_obb = any(n.lower().endswith(".obb") for n in names)
            if "manifest.json" in names:
                try:
                    xapk_manifest = json.loads(z.read("manifest.json").decode("utf-8", errors="ignore"))
                except Exception:
                    xapk_manifest = {}
            apk_entries = [n for n in names if n.lower().endswith(".apk") and "/" not in n]
            if not apk_entries:
                apk_entries = [n for n in names if n.lower().endswith(".apk")]
            if apk_entries:
                chosen = apk_entries[0]
                inner_apk_path = job_dir / sanitize_filename(Path(chosen).name)
                with z.open(chosen) as src, inner_apk_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Geçersiz XAPK/ZIP arşivi.")

    if inner_apk_path and inner_apk_path.exists():
        meta = inspect_apk(inner_apk_path)
    else:
        meta = {
            "packageId": xapk_manifest.get("package_name", "bilinmiyor"),
            "appName": xapk_manifest.get("name", uploaded_path.stem),
            "versionCode": str(xapk_manifest.get("version_code", "1")),
            "versionName": str(xapk_manifest.get("version_name", "1.0")),
            "minSdk": int(xapk_manifest.get("min_sdk_version", 14) or 14),
            "targetSdk": int(xapk_manifest.get("target_sdk_version", 19) or 19),
            "legacyAndroid": "Android 4.4",
            "abis": ["XAPK Arşivi"],
        }

    return {
        "jobId": job_id,
        "fileName": safe_name,
        "ext": "XAPK",
        "sizeBytes": size_bytes,
        "hasObb": has_obb,
        **meta,
    }


@app.post("/api/patch-sign")
async def api_patch_sign(
    jobId: str = Form(...),
    targetSdk: int = Form(34),
    applyTargetSdk: bool = Form(True),
    resign: bool = Form(True),
):
    if not re.fullmatch(r"[a-f0-9]{12}", jobId):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği.")

    job_dir = WORK_DIR / jobId
    if not job_dir.exists():
        raise HTTPException(status_code=404, detail="İş klasörü bulunamadı (sunucu uyku sonrası sıfırlanmış olabilir).")

    apks = [p for p in job_dir.glob("*.apk") if not p.name.endswith("_uyumlu.apk")]
    if not apks:
        raise HTTPException(status_code=404, detail="İşlenecek APK dosyası bulunamadı.")

    src_apk = apks[0]
    dec_dir = job_dir / "decoded"
    rebuilt = job_dir / "rebuilt.apk"
    aligned = job_dir / "aligned.apk"
    signed = job_dir / f"{src_apk.stem}_uyumlu.apk"
    logs = []

    try:
        logs.append(f"[SERVER] Kaynak APK bulundu: {src_apk.name}")
        work_apk = src_apk

        if applyTargetSdk:
            logs.append(f"[APKTOOL] Manifest/kaynaklar açılıyor (targetSdk -> API {targetSdk})...")
            subprocess.run(
                ["apktool", "d", "-s", "-f", str(src_apk), "-o", str(dec_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            yml = dec_dir / "apktool.yml"
            if yml.exists():
                txt = yml.read_text(encoding="utf-8", errors="ignore")
                if re.search(r"targetSdkVersion:\s*['\"]?\d+['\"]?", txt):
                    txt = re.sub(
                        r"targetSdkVersion:\s*['\"]?\d+['\"]?",
                        f"targetSdkVersion: '{targetSdk}'",
                        txt,
                    )
                else:
                    txt += f"\nsdkInfo:\n  targetSdkVersion: '{targetSdk}'\n"
                yml.write_text(txt, encoding="utf-8")
                logs.append(f"[MANIFEST] apktool.yml içinde targetSdkVersion API {targetSdk} olarak güncellendi.")

            logs.append("[APKTOOL] APK yeniden derleniyor...")
            subprocess.run(
                ["apktool", "b", str(dec_dir), "-o", str(rebuilt)],
                check=True,
                capture_output=True,
                text=True,
                timeout=240,
            )
            work_apk = rebuilt

        logs.append("[ZIPALIGN] 4-byte sayfa hizalaması uygulanıyor...")
        subprocess.run(
            ["zipalign", "-f", "-p", "4", str(work_apk), str(aligned)],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )

        if resign:
            logs.append("[APKSIGNER] APK Signature Scheme v2/v3 ile imzalanıyor...")
            subprocess.run(
                [
                    "apksigner",
                    "sign",
                    "--ks",
                    str(KEYSTORE),
                    "--ks-pass",
                    f"pass:{KEY_PASS}",
                    "--key-pass",
                    f"pass:{KEY_PASS}",
                    "--v2-signing-enabled",
                    "true",
                    "--v3-signing-enabled",
                    "true",
                    "--out",
                    str(signed),
                    str(aligned),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
            logs.append(f"[SIGN] Başarılı! İmzalı APK üretildi: {signed.name}")
        else:
            shutil.copy(aligned, signed)
            logs.append(f"[DONE] İmzasız hizalanmış APK hazır: {signed.name}")

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="APK işlemi zaman aşımına uğradı (Render Free CPU/RAM sınırı).")
    except subprocess.CalledProcessError as e:
        err_msg = (e.stderr or e.stdout or str(e)).strip()[-350:]
        raise HTTPException(status_code=500, detail=f"APK işleme hatası: {err_msg}")
    finally:
        shutil.rmtree(dec_dir, ignore_errors=True)
        rebuilt.unlink(missing_ok=True)
        aligned.unlink(missing_ok=True)

    return {
        "jobId": jobId,
        "fileName": signed.name,
        "sizeBytes": signed.stat().st_size,
        "downloadUrl": f"/api/download/{jobId}/{signed.name}",
        "logs": logs,
    }


@app.get("/api/download/{job_id}/{filename}")
def api_download(job_id: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği.")
    safe_file = Path(filename).name
    target = WORK_DIR / job_id / safe_file
    if not target.exists():
        raise HTTPException(status_code=404, detail="Dosya bulunamadı veya süresi doldu.")
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/vnd.android.package-archive",
    )

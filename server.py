import json
import os
import re
import shutil
import struct
import subprocess
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

app = FastAPI(title="RetroDroid Compat Render API", version="4.5")
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
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "5120"))  # 5 GB = 5120 MB
MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024
CHUNK_SIZE_MB = 8
JOB_TTL_SECONDS = 3600
SUPPORTED_EXTS = (".apk", ".xapk", ".apks", ".apkm", ".zip")

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)

# Android Binary XML (AXML) sabitleri
RES_XML_TYPE = 0x0003
RES_XML_RESOURCE_MAP_TYPE = 0x0180
RES_XML_START_ELEMENT_TYPE = 0x0102
ATTR_MIN_SDK = 0x0101020C
ATTR_TARGET_SDK = 0x01010270


def cleanup_old_jobs(keep_job_id: str | None = None):
    """Suresi dolan isleri siler; disk alani azaldiysa en eski isleri temizler."""
    now = time.time()
    dirs = []
    for item in WORK_DIR.iterdir():
        if not item.is_dir():
            continue
        if keep_job_id and item.name == keep_job_id:
            continue
        try:
            mtime = item.stat().st_mtime
            if (now - mtime) > JOB_TTL_SECONDS:
                shutil.rmtree(item, ignore_errors=True)
            else:
                dirs.append((mtime, item))
        except Exception:
            pass

    try:
        dirs.sort(key=lambda x: x[0])
        while dirs and shutil.disk_usage(WORK_DIR).free < 1500 * 1024 * 1024:
            _, old_dir = dirs.pop(0)
            shutil.rmtree(old_dir, ignore_errors=True)
    except Exception:
        pass


def run_cmd(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def patch_binary_axml_target_sdk(axml_bytes: bytes, new_target_sdk: int) -> tuple[bytes, dict]:
    """
    Herhangi bir APK'nin binary AndroidManifest.xml (AXML) dosyasindaki
    android:targetSdkVersion (0x01010270) degerini 4 bayt yerinde (in-place) gunceller.
    APK boyutu 1 MB da olsa 5 GB da olsa < 10 ms ve < 1 MB RAM ile calisir.
    """
    buf = bytearray(axml_bytes)
    n = len(buf)
    if n < 8:
        return axml_bytes, {"patched": False, "reason": "AXML boyutu gecersiz"}

    ftype, fhdr, _ = struct.unpack_from("<HHI", buf, 0)
    if ftype != RES_XML_TYPE:
        return axml_bytes, {"patched": False, "reason": f"Binary AXML degil (0x{ftype:04x})"}

    pos = fhdr
    res_map: list[int] = []
    found_target = False
    old_target = None
    old_min = None

    while pos + 8 <= n:
        ctype, chdr, csize = struct.unpack_from("<HHI", buf, pos)
        if csize < 8 or pos + csize > n:
            break

        if ctype == RES_XML_RESOURCE_MAP_TYPE:
            count = (csize - chdr) // 4
            res_map = list(struct.unpack_from(f"<{count}I", buf, pos + chdr))

        elif ctype == RES_XML_START_ELEMENT_TYPE:
            ext_off = pos + chdr
            if ext_off + 20 <= pos + csize:
                _, _, attr_start, attr_size, attr_count = struct.unpack_from("<iiHHH", buf, ext_off)
                attrs_base = ext_off + attr_start
                for i in range(attr_count):
                    a_off = attrs_base + i * attr_size
                    if a_off + 20 > pos + csize:
                        break
                    _, a_name, _, _, _, _, val_data = struct.unpack_from("<IIIHBBI", buf, a_off)
                    res_id = res_map[a_name] if 0 <= a_name < len(res_map) else 0
                    if res_id == ATTR_MIN_SDK:
                        old_min = val_data
                    elif res_id == ATTR_TARGET_SDK:
                        old_target = val_data
                        struct.pack_into("<IHBBI", buf, a_off + 8, 0xFFFFFFFF, 8, 0, 0x10, int(new_target_sdk))
                        found_target = True

        pos += csize

    return bytes(buf), {
        "patched": found_target,
        "oldTargetSdk": old_target,
        "newTargetSdk": new_target_sdk if found_target else old_target,
        "minSdk": old_min,
    }


def patch_manifest_xml_apktool(manifest_path: Path, target_sdk: int) -> list[str]:
    notes = []
    if not manifest_path.exists():
        return notes
    try:
        tree = ET.parse(manifest_path)
        root = tree.getroot()
    except Exception as exc:
        notes.append(f"Manifest XML ayrıştırılamadı: {exc}")
        return notes

    exported_attr = f"{{{ANDROID_NS}}}exported"
    extract_attr = f"{{{ANDROID_NS}}}extractNativeLibs"
    app_el = root.find("application")

    if app_el is not None:
        if extract_attr not in app_el.attrib:
            app_el.set(extract_attr, "true")
            notes.append("Manifest: android:extractNativeLibs='true' eklendi.")
        if target_sdk >= 31:
            added = 0
            for tag in ("activity", "activity-alias", "service", "receiver"):
                for comp in app_el.findall(tag):
                    if comp.find("intent-filter") is not None and exported_attr not in comp.attrib:
                        comp.set(exported_attr, "true")
                        added += 1
            if added:
                notes.append(f"Android 12+ kuralı: {added} bileşene android:exported='true' eklendi.")

    uses_sdk = root.find("uses-sdk")
    if uses_sdk is not None:
        t_attr = f"{{{ANDROID_NS}}}targetSdkVersion"
        if t_attr in uses_sdk.attrib:
            uses_sdk.set(t_attr, str(target_sdk))

    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
    return notes


def build_slim_manifest_apk(src_apk: Path, slim_apk: Path) -> int:
    """
    Buyuk APK'larda (ornegin 500 MB - 5 GB) yalnizca manifest + kaynak tablolarini
    iceren hafif bir gecici APK olusturur (assets/, lib/, classes*.dex atlanir).
    Boylece apktool dusuk RAM ile calisip yeni AndroidManifest.xml uretebilir.
    """
    total = 0
    with zipfile.ZipFile(src_apk, "r") as zin, zipfile.ZipFile(slim_apk, "w") as zout:
        for item in zin.infolist():
            name = item.filename
            if (
                name.startswith(("assets/", "lib/", "META-INF/"))
                or (name.startswith("classes") and name.endswith(".dex"))
                or item.is_dir()
            ):
                continue
            info = zipfile.ZipInfo(name)
            info.date_time = item.date_time
            info.compress_type = item.compress_type
            with zin.open(item, "r") as rfp, zout.open(info, "w") as wfp:
                shutil.copyfileobj(rfp, wfp, 512 * 1024)
            total += item.file_size
    return total


def stream_rewrite_apk(
    apk_in: Path,
    apk_out: Path,
    patched_manifest: bytes | None = None,
    patched_arsc: bytes | None = None,
) -> None:
    """
    RAM tuketmeden (1 MB akisla) herhangi bir boyuttaki APK'yi yeniden yazar:
    - Eski META-INF imzalarini temizler
    - Android 11+ icin resources.arsc ve .so dosyalarini SIKISTIRILMAMIS (ZIP_STORED) yapar
    - Istenirse yamalanmis binary AndroidManifest.xml (ve gerekirse resources.arsc) enjekte eder
    """
    with zipfile.ZipFile(apk_in, "r") as zin, zipfile.ZipFile(apk_out, "w", allowZip64=True) as zout:
        for item in zin.infolist():
            name = item.filename
            if name.startswith("META-INF/") and (
                name.endswith((".SF", ".RSA", ".DSA", ".EC"))
                or name == "META-INF/MANIFEST.MF"
                or name.startswith("META-INF/SIG-")
            ):
                continue

            info = zipfile.ZipInfo(name)
            info.date_time = item.date_time
            info.external_attr = item.external_attr

            if item.is_dir():
                info.compress_type = zipfile.ZIP_STORED
                zout.writestr(info, b"")
                continue

            if name == "resources.arsc" or name.endswith(".so"):
                info.compress_type = zipfile.ZIP_STORED
            else:
                info.compress_type = item.compress_type

            if name == "AndroidManifest.xml" and patched_manifest is not None:
                zout.writestr(info, patched_manifest)
            elif name == "resources.arsc" and patched_arsc is not None:
                zout.writestr(info, patched_arsc)
            else:
                with zin.open(item, "r") as rfp, zout.open(
                    info, "w", force_zip64=(item.file_size >= 0xFFFFFFFF)
                ) as wfp:
                    shutil.copyfileobj(rfp, wfp, 1024 * 1024)


def align_and_sign_apk(src_apk: Path, out_apk: Path, resign: bool = True, timeout: int = 300) -> list[str]:
    """Tek bir APK'yi zipalign (-p 4) + apksigner (v1/v2/v3) + apksigner verify isleminden gecirir."""
    logs: list[str] = []
    aligned = out_apk.with_suffix(".aligned.apk")
    try:
        z_res = run_cmd(["zipalign", "-f", "-p", "4", str(src_apk), str(aligned)], timeout=timeout)
        if z_res.returncode != 0 or not aligned.exists():
            raise HTTPException(status_code=500, detail=f"zipalign hatası: {(z_res.stderr or z_res.stdout)[:400]}")
        logs.append(f"[ZIPALIGN] {out_apk.name}: 4-byte ve sayfa hizalama (-p 4) tamamlandı.")

        if resign:
            s_res = run_cmd([
                "apksigner", "sign",
                "--ks", str(KEYSTORE),
                "--ks-pass", f"pass:{KEY_PASS}",
                "--key-pass", f"pass:{KEY_PASS}",
                "--v1-signing-enabled", "true",
                "--v2-signing-enabled", "true",
                "--v3-signing-enabled", "true",
                "--out", str(out_apk),
                str(aligned),
            ], timeout=timeout)
            if s_res.returncode != 0 or not out_apk.exists():
                raise HTTPException(status_code=500, detail=f"apksigner hatası: {(s_res.stderr or s_res.stdout)[:400]}")
            logs.append(f"[SIGN] {out_apk.name}: v1 + v2 + v3 imzası uygulandı.")

            v_res = run_cmd(["apksigner", "verify", "--verbose", str(out_apk)], timeout=180)
            if v_res.returncode != 0:
                out_apk.unlink(missing_ok=True)
                raise HTTPException(status_code=500, detail=f"İmza doğrulaması başarısız: {(v_res.stderr or v_res.stdout)[:400]}")
            logs.append(f"[VERIFY] {out_apk.name}: apksigner verify BAŞARILI!")
        else:
            shutil.move(str(aligned), str(out_apk))
            logs.append(f"[UYARI] {out_apk.name}: İmzasız bırakıldı (Android kurmaz).")
    finally:
        aligned.unlink(missing_ok=True)
    return logs


def unpack_container_if_needed(uploaded_path: Path, job_dir: Path) -> tuple[Path, dict]:
    """
    Herhangi bir .apk, .xapk, .apks, .apkm veya .zip paketini inceler:
    - Dogrudan AndroidManifest.xml iceriyorsa tekil APK olarak dondurur.
    - Icinde bir veya daha fazla .apk (ve istege bagli .obb) barindiran konteyner ise
      base/ana APK'yi, split APK listesini ve OBB kayitlarini cikarir.
    """
    extra = {
        "isContainer": False,
        "containerType": "APK",
        "innerApkName": None,
        "splitApks": [],
        "obbFiles": [],
    }
    try:
        with zipfile.ZipFile(uploaded_path, "r") as z:
            names = z.namelist()
            if "AndroidManifest.xml" in names:
                return uploaded_path, extra

            inner_apks = [n for n in names if n.lower().endswith(".apk") and not n.startswith("__MACOSX")]
            obb_entries = [n for n in names if n.lower().endswith(".obb") and not n.startswith("__MACOSX")]
            if not inner_apks:
                return uploaded_path, extra

            # Base APK secimi: base.apk varsa o, yoksa config/split olmayan ilk APK
            def score_base(entry_name: str) -> tuple[int, int]:
                low = Path(entry_name).name.lower()
                if low == "base.apk":
                    return (0, 0)
                if "config." in low or "split_" in low:
                    return (2, -z.getinfo(entry_name).file_size)
                return (1, -z.getinfo(entry_name).file_size)

            inner_apks.sort(key=score_base)
            chosen = inner_apks[0]
            splits = inner_apks[1:]

            extra["isContainer"] = True
            extra["containerType"] = uploaded_path.suffix.lstrip(".").upper() or "XAPK"
            extra["innerApkName"] = Path(chosen).name
            extra["splitApks"] = [
                {"zipEntry": s, "name": Path(s).name, "sizeBytes": z.getinfo(s).file_size}
                for s in splits
            ]
            extra["obbFiles"] = [
                {"zipEntry": o, "name": Path(o).name, "sizeBytes": z.getinfo(o).file_size}
                for o in obb_entries
            ]

            extracted_apk = job_dir / Path(chosen).name
            with z.open(chosen, "r") as rfp, extracted_apk.open("wb") as wfp:
                shutil.copyfileobj(rfp, wfp, 1024 * 1024)

            (job_dir / "container_meta.json").write_text(
                json.dumps({"archive": uploaded_path.name, **extra}),
                encoding="utf-8",
            )
            return extracted_apk, extra
    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=400,
            detail="Dosya geçerli bir APK / XAPK / APKS / ZIP arşivi değil veya eksik yüklenmiş.",
        )


def inspect_apk(apk_path: Path, container_info: dict | None = None) -> dict:
    with apk_path.open("rb") as f:
        head = f.read(4)
    if len(head) < 2 or head[:2] != b"PK":
        raise HTTPException(status_code=400, detail="Geçerli bir APK/ZIP arşivi değil (PK başlığı yok).")

    proc = run_cmd(["aapt", "dump", "badging", str(apk_path)], timeout=90)
    out = proc.stdout or ""

    pkg = re.search(r"package: name='([^']+)'", out)
    ver_name = re.search(r"versionName='([^']*)'", out)
    ver_code = re.search(r"versionCode='([^']*)'", out)
    app_label = re.search(r"application-label(?:-[a-zA-Z_-]+)?:'([^']+)'", out)
    min_sdk = re.search(r"sdkVersion:'(\d+)'", out)
    target_sdk = re.search(r"targetSdkVersion:'(\d+)'", out)

    abis = set()
    has_arsc = False
    has_manifest = False
    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            for name in z.namelist():
                if name == "AndroidManifest.xml":
                    has_manifest = True
                elif name == "resources.arsc":
                    has_arsc = True
                elif name.startswith("lib/") and name.endswith(".so"):
                    parts = name.split("/")
                    if len(parts) >= 3:
                        abis.add(parts[1])
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="APK arşivi bozuk veya yarım inmiş.")

    # Split APK'larda mimariler ayri split dosya adlarinda olabilir (config.arm64_v8a.apk vb.)
    split_apks = (container_info or {}).get("splitApks") or []
    for sp in split_apks:
        sp_low = sp["name"].lower()
        if "arm64" in sp_low:
            abis.add("arm64-v8a")
        elif "armeabi_v7a" in sp_low or "armeabi-v7a" in sp_low:
            abis.add("armeabi-v7a")
        elif "x86_64" in sp_low:
            abis.add("x86_64")
        elif "x86" in sp_low:
            abis.add("x86")

    if not has_manifest:
        raise HTTPException(
            status_code=400,
            detail="Bu dosyanın içinde AndroidManifest.xml bulunamadı. Geçerli bir Android paketi değil.",
        )

    abi_list = sorted(abis)
    has_32 = any(a in abi_list for a in ("armeabi", "armeabi-v7a", "x86"))
    has_64 = any(a in abi_list for a in ("arm64-v8a", "x86_64"))
    is_32bit_only = bool(has_32 and not has_64)

    warnings = []
    t_sdk = int(target_sdk.group(1)) if target_sdk else 0
    if container_info and container_info.get("isContainer"):
        obb_cnt = len(container_info.get("obbFiles") or [])
        sp_cnt = len(split_apks)
        warnings.append(
            f"{container_info.get('containerType', 'XAPK')} paketi tespit edildi: "
            f"Ana APK ({container_info['innerApkName']}), {sp_cnt} ek parça (Split APK), {obb_cnt} OBB dosyası."
        )
    if is_32bit_only:
        warnings.append(
            "DİKKAT — Yalnızca 32-bit (ARMv7a) paket: Telefonunuz yalnızca 64-bit destekliyorsa "
            "(Snapdragon 8 Gen 3/Elite, Pixel 7+) Android bu paketi ana sisteme kurmayı donanımsal olarak reddeder "
            "(çözüm: Virtual Master / VMOS Pro 32-bit sanal makine)."
        )
    if t_sdk and t_sdk < 23:
        warnings.append(
            f"Orijinal targetSdkVersion ({t_sdk}) Android 14/15/16 kurulum sınırının (23) altında. "
            "Hızlı Binary AXML yamasıyla API 28'e yükseltilecek."
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
        "isContainer": bool(container_info and container_info.get("isContainer")),
        "splitCount": len(split_apks),
        "obbFiles": (container_info or {}).get("obbFiles", []),
        "warnings": warnings,
    }


@app.get("/health")
def health():
    cleanup_old_jobs()
    free_mb = 0
    try:
        free_mb = int(shutil.disk_usage(WORK_DIR).free / (1024 * 1024))
    except Exception:
        pass
    return {
        "status": "ok",
        "engine": "RetroDroid Render Server v4.5",
        "maxUploadMB": MAX_UPLOAD_MB,
        "chunkSizeMB": CHUNK_SIZE_MB,
        "freeDiskMB": free_mb,
        "keystoreReady": KEYSTORE.exists(),
        "supportedFormats": list(SUPPORTED_EXTS),
        "fastBinaryAxml": True,
    }


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    idx = Path("/app/index.html")
    if idx.exists():
        return idx.read_text(encoding="utf-8")
    return "<h3>RetroDroid Render API v4.5 aktif. <a href='/docs'>/docs</a></h3>"


# ------------------------------------------------------------------------------
# GENEL PARÇALI (CHUNKED) YÜKLEME — TÜM .APK / .XAPK / .APKS / .APKM / .ZIP (5 GB)
# ------------------------------------------------------------------------------
@app.post("/api/upload/init")
async def api_upload_init(
    fileName: str = Form(...),
    totalSize: int = Form(...),
):
    cleanup_old_jobs()
    safe_name = Path(fileName or "paket.apk").name
    if not safe_name.lower().endswith(SUPPORTED_EXTS):
        raise HTTPException(
            status_code=400,
            detail="Desteklenen biçimler: .apk, .xapk, .apks, .apkm, .zip",
        )
    if totalSize <= 0 or totalSize > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Dosya boyutu geçersiz veya {MAX_UPLOAD_MB} MB sınırını aşıyor.",
        )

    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "jobId": job_id,
        "fileName": safe_name,
        "totalSize": int(totalSize),
        "receivedBytes": 0,
    }
    (job_dir / "upload_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return {"jobId": job_id, "fileName": safe_name, "chunkSizeMB": CHUNK_SIZE_MB}


@app.post("/api/upload/chunk")
async def api_upload_chunk(
    jobId: str = Form(...),
    chunkIndex: int = Form(...),
    chunk: UploadFile = File(...),
):
    if not re.fullmatch(r"[a-f0-9]{12}", jobId):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği.")
    job_dir = WORK_DIR / jobId
    meta_file = job_dir / "upload_meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="Yükleme oturumu bulunamadı (sunucu yeniden başlamış olabilir).")

    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    dest = job_dir / meta["fileName"]

    written = 0
    with dest.open("ab") as out:
        while part := await chunk.read(1024 * 1024):
            written += len(part)
            if meta["receivedBytes"] + written > MAX_BYTES:
                raise HTTPException(status_code=413, detail="5 GB sınırı aşıldı.")
            out.write(part)

    meta["receivedBytes"] += written
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    return {"jobId": jobId, "chunkIndex": chunkIndex, "receivedBytes": meta["receivedBytes"]}


@app.post("/api/upload/finish")
async def api_upload_finish(jobId: str = Form(...)):
    if not re.fullmatch(r"[a-f0-9]{12}", jobId):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği.")
    job_dir = WORK_DIR / jobId
    meta_file = job_dir / "upload_meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="Yükleme oturumu bulunamadı.")

    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    uploaded_path = job_dir / meta["fileName"]
    if not uploaded_path.exists() or uploaded_path.stat().st_size < 100:
        raise HTTPException(status_code=400, detail="Yüklenen dosya bulunamadı veya eksik.")

    try:
        apk_path, container_info = unpack_container_if_needed(uploaded_path, job_dir)
        info = inspect_apk(apk_path, container_info)
        meta["targetApkName"] = apk_path.name
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Analiz hatası: {exc}")

    return {
        "jobId": jobId,
        "fileName": apk_path.name,
        "sizeBytes": apk_path.stat().st_size,
        **info,
    }


@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...)):
    cleanup_old_jobs()
    fname = Path(file.filename or "uygulama.apk").name
    if not fname.lower().endswith(SUPPORTED_EXTS):
        raise HTTPException(status_code=400, detail="Desteklenen biçimler: .apk, .xapk, .apks, .apkm, .zip")

    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    uploaded_path = job_dir / fname

    total = 0
    try:
        with uploaded_path.open("wb") as out:
            while part := await file.read(2 * 1024 * 1024):
                total += len(part)
                if total > MAX_BYTES:
                    raise HTTPException(status_code=413, detail=f"Dosya {MAX_UPLOAD_MB} MB sınırını aşıyor.")
                out.write(part)
        apk_path, container_info = unpack_container_if_needed(uploaded_path, job_dir)
        info = inspect_apk(apk_path, container_info)
        (job_dir / "upload_meta.json").write_text(
            json.dumps({"jobId": job_id, "fileName": fname, "targetApkName": apk_path.name}),
            encoding="utf-8",
        )
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"APK analiz hatası: {exc}")

    return {"jobId": job_id, "fileName": apk_path.name, "sizeBytes": apk_path.stat().st_size, **info}


# ------------------------------------------------------------------------------
# GENEL APK / SPLIT APK YAMALAMA + ZIPALIGN + v1/v2/v3 İMZALAMA + DOĞRULAMA
# ------------------------------------------------------------------------------
@app.post("/api/patch-sign")
async def api_patch_sign(
    jobId: str = Form(...),
    targetSdk: int = Form(28),
    mode: str = Form("patch_and_sign"),  # "patch_and_sign" | "resign_only"
    resign: bool = Form(True),
):
    if not re.fullmatch(r"[a-f0-9]{12}", jobId):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği (jobId).")

    cleanup_old_jobs(keep_job_id=jobId)
    job_dir = WORK_DIR / jobId
    if not job_dir.exists():
        raise HTTPException(
            status_code=404,
            detail="İş kaydı bulunamadı (sunucu uyku sonrası sıfırlanmış olabilir). Dosyayı yeniden yükleyin.",
        )

    src_apk = None
    meta_file = job_dir / "upload_meta.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        cand = job_dir / (meta.get("targetApkName") or meta.get("fileName") or "")
        if cand.exists() and cand.suffix.lower() == ".apk":
            src_apk = cand
    if src_apk is None:
        apks = [p for p in job_dir.glob("*.apk") if not p.stem.endswith("_uyumlu")]
        if apks:
            src_apk = apks[0]
    if src_apk is None or not src_apk.exists():
        raise HTTPException(status_code=404, detail="Kaynak APK dosyası bulunamadı.")

    src_size_mb = src_apk.stat().st_size / (1024 * 1024)
    target_sdk = max(23, min(35, int(targetSdk)))
    logs: list[str] = []

    slim_apk = job_dir / "slim_manifest.apk"
    dec_dir = job_dir / "decoded"
    rebuilt_slim = job_dir / "rebuilt_slim.apk"
    sanitized = job_dir / "sanitized.apk"
    signed = job_dir / f"{src_apk.stem}_uyumlu.apk"

    try:
        patched_manifest_bytes = None
        patched_arsc_bytes = None

        if mode == "patch_and_sign":
            with zipfile.ZipFile(src_apk, "r") as zin:
                raw_manifest = zin.read("AndroidManifest.xml")

            # Yol 1: TargetSDK < 31 ise dogrudan hizli Binary AXML yerinde yamalamayi dene (0 kaynak riski)
            need_exported_patch = target_sdk >= 31
            axml_patched = False
            if not need_exported_patch:
                new_manifest, axml_info = patch_binary_axml_target_sdk(raw_manifest, target_sdk)
                if axml_info.get("patched"):
                    patched_manifest_bytes = new_manifest
                    axml_patched = True
                    logs.append(
                        f"[AXML] Hızlı Binary Manifest yaması başarılı! "
                        f"targetSdkVersion: {axml_info.get('oldTargetSdk')} → {target_sdk} "
                        f"(minSdk: {axml_info.get('minSdk')}) — Tüm varlıklar bozulmadan korundu."
                    )

            # Yol 2: Eski APK'da targetSdk hic yazmiyorsa VEYA targetSdk >= 31 (android:exported) gerekiyorsa
            # dev dosyayi degil yalnizca hafif manifest/kaynak kopyasini (slim_apk) apktool ile derle!
            if not axml_patched:
                logs.append("[SLIM-APKTOOL] Yalnızca manifest ve kaynak tablosu ayrıştırılıyor (dev assets/lib RAM'e alınmıyor)...")
                slim_bytes = build_slim_manifest_apk(src_apk, slim_apk)
                if slim_bytes <= 150 * 1024 * 1024:
                    d_res = run_cmd(["apktool", "d", "-s", "-f", str(slim_apk), "-o", str(dec_dir)], timeout=180)
                    if d_res.returncode == 0:
                        yml = dec_dir / "apktool.yml"
                        if yml.exists():
                            txt = yml.read_text(encoding="utf-8", errors="ignore")
                            if re.search(r"targetSdkVersion:", txt):
                                txt = re.sub(r"targetSdkVersion:\s*['\"]?\d+['\"]?", f"targetSdkVersion: '{target_sdk}'", txt)
                            elif "sdkInfo:" in txt:
                                txt = txt.replace("sdkInfo:", f"sdkInfo:\n  targetSdkVersion: '{target_sdk}'", 1)
                            else:
                                txt += f"\nsdkInfo:\n  targetSdkVersion: '{target_sdk}'\n"
                            yml.write_text(txt, encoding="utf-8")

                        for n_txt in patch_manifest_xml_apktool(dec_dir / "AndroidManifest.xml", target_sdk):
                            logs.append(f"[MANIFEST] {n_txt}")

                        b_res = run_cmd(["apktool", "b", str(dec_dir), "-o", str(rebuilt_slim)], timeout=180)
                        if b_res.returncode == 0 and rebuilt_slim.exists():
                            with zipfile.ZipFile(rebuilt_slim, "r") as zrb:
                                if "AndroidManifest.xml" in zrb.namelist():
                                    patched_manifest_bytes = zrb.read("AndroidManifest.xml")
                                if "resources.arsc" in zrb.namelist():
                                    patched_arsc_bytes = zrb.read("resources.arsc")
                            logs.append(f"[SLIM-APKTOOL] Yeni AndroidManifest.xml (API {target_sdk}) derlendi ve ana APK'ya enjekte edilecek.")
                        else:
                            logs.append("[UYARI] Hafif apktool derlemesi başarısız; güvenli hizalama + imzalama moduna geçiliyor.")
                    else:
                        logs.append("[UYARI] Apktool manifesti açamadı; güvenli hizalama + imzalama moduna geçiliyor.")
                else:
                    logs.append("[BİLGİ] Kaynak tablosu çok büyük; güvenli akışlı hizalama + imzalama uygulanıyor.")
                shutil.rmtree(dec_dir, ignore_errors=True)
                slim_apk.unlink(missing_ok=True)
                rebuilt_slim.unlink(missing_ok=True)
        else:
            logs.append("[MOD] Güvenli mod: Orijinal kaynaklar korunarak hizalama + yeniden imzalama yapılıyor.")

        # 2) Akisli (1 MB streaming) ZIP yeniden yazma + eski META-INF temizligi + resources.arsc/.so STORE
        stream_rewrite_apk(
            src_apk,
            sanitized,
            patched_manifest=patched_manifest_bytes,
            patched_arsc=patched_arsc_bytes,
        )
        logs.append("[ZIP] Akışlı paketleme tamamlandı: eski META-INF temizlendi, resources.arsc ve .so STORE yapıldı.")

        if src_size_mb > 180:
            src_apk.unlink(missing_ok=True)

        # 3) Ana APK icin zipalign + apksigner + verify
        logs.extend(align_and_sign_apk(sanitized, signed, resign=resign, timeout=300))
        sanitized.unlink(missing_ok=True)

        final_meta = inspect_apk(signed)

        # 4) Konteyner (XAPK/APKS/APKM/ZIP) icinde ek Split APK'lar varsa hepsini AYNI anahtarla imzalayip paketle
        container_meta_path = job_dir / "container_meta.json"
        obb_downloads = []
        split_bundle_download = None

        if container_meta_path.exists():
            cm = json.loads(container_meta_path.read_text(encoding="utf-8"))
            obbs = cm.get("obbFiles", [])
            splits = cm.get("splitApks", [])
            archive_path = job_dir / cm["archive"]

            obb_downloads = [
                {
                    "name": o["name"],
                    "sizeBytes": o["sizeBytes"],
                    "downloadUrl": f"/api/download-obb/{jobId}/{idx}",
                }
                for idx, o in enumerate(obbs)
            ]

            if splits and archive_path.exists():
                logs.append(f"[SPLIT] {len(splits)} ek Split APK tespit edildi; tüm parçalar aynı anahtarla yeniden imzalanıyor...")
                bundle_path = job_dir / f"{signed.stem}_SplitPaket.apks"
                with zipfile.ZipFile(archive_path, "r") as zsrc, zipfile.ZipFile(bundle_path, "w", allowZip64=True) as zbundle:
                    # Imzali ana APK'yi ekle
                    zbundle.write(signed, arcname="base.apk", compress_type=zipfile.ZIP_STORED)
                    for sp in splits:
                        sp_raw = job_dir / f"raw_{sp['name']}"
                        sp_clean = job_dir / f"clean_{sp['name']}"
                        sp_signed = job_dir / f"signed_{sp['name']}"
                        try:
                            with zsrc.open(sp["zipEntry"], "r") as rfp, sp_raw.open("wb") as wfp:
                                shutil.copyfileobj(rfp, wfp, 1024 * 1024)
                            stream_rewrite_apk(sp_raw, sp_clean)
                            sp_raw.unlink(missing_ok=True)
                            logs.extend(align_and_sign_apk(sp_clean, sp_signed, resign=resign, timeout=180))
                            sp_clean.unlink(missing_ok=True)
                            zbundle.write(sp_signed, arcname=sp["name"], compress_type=zipfile.ZIP_STORED)
                        finally:
                            for tmp_sp in (sp_raw, sp_clean, sp_signed):
                                tmp_sp.unlink(missing_ok=True)
                split_bundle_download = {
                    "name": bundle_path.name,
                    "sizeBytes": bundle_path.stat().st_size,
                    "downloadUrl": f"/api/download/{jobId}/{bundle_path.name}",
                }
                logs.append(f"[SPLIT] Tüm parçalar aynı anahtarla imzalanıp {bundle_path.name} paketine birleştirildi (SAI / ZArchiver ile kurulabilir).")

        return {
            "jobId": jobId,
            "fileName": signed.name,
            "sizeBytes": signed.stat().st_size,
            "downloadUrl": f"/api/download/{jobId}/{signed.name}",
            "splitBundle": split_bundle_download,
            "verified": bool(resign),
            "targetSdk": final_meta.get("targetSdk", target_sdk),
            "is32BitOnly": final_meta.get("is32BitOnly", False),
            "warnings": final_meta.get("warnings", []),
            "obbDownloads": obb_downloads,
            "logs": logs,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="APK işleme zaman aşımına uğradı.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sunucu işleme hatası: {exc}")
    finally:
        shutil.rmtree(dec_dir, ignore_errors=True)
        for tmp in (slim_apk, rebuilt_slim, sanitized):
            tmp.unlink(missing_ok=True)


@app.get("/api/download/{job_id}/{filename}")
def api_download(job_id: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği.")
    target = WORK_DIR / job_id / Path(filename).name
    if not target.exists() or target.stat().st_size < 100:
        raise HTTPException(status_code=404, detail="İmzalı dosya bulunamadı veya süresi doldu.")
    media = (
        "application/vnd.android.package-archive"
        if target.suffix.lower() == ".apk"
        else "application/octet-stream"
    )
    return FileResponse(path=target, filename=target.name, media_type=media)


@app.get("/api/download-obb/{job_id}/{obb_index}")
def api_download_obb(job_id: str, obb_index: int):
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise HTTPException(status_code=400, detail="Geçersiz iş kimliği.")
    job_dir = WORK_DIR / job_id
    cm_path = job_dir / "container_meta.json"
    if not cm_path.exists():
        raise HTTPException(status_code=404, detail="OBB kaydı bulunamadı.")
    cm = json.loads(cm_path.read_text(encoding="utf-8"))
    obbs = cm.get("obbFiles", [])
    if obb_index < 0 or obb_index >= len(obbs):
        raise HTTPException(status_code=404, detail="OBB indeksi geçersiz.")
    obb_meta = obbs[obb_index]
    extracted_obb = job_dir / obb_meta["name"]
    if not extracted_obb.exists():
        archive_path = job_dir / cm["archive"]
        if not archive_path.exists():
            raise HTTPException(status_code=404, detail="Kaynak arşiv bulunamadı.")
        with zipfile.ZipFile(archive_path, "r") as z:
            with z.open(obb_meta["zipEntry"], "r") as rfp, extracted_obb.open("wb") as wfp:
                shutil.copyfileobj(rfp, wfp, 1024 * 1024)
    return FileResponse(path=extracted_obb, filename=extracted_obb.name, media_type="application/octet-stream")

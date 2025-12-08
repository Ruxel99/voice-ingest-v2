
import os, sys, time, json, shutil, pathlib, threading
from queue import Queue
from pathlib import Path
from dotenv import load_dotenv
import psutil
import time, json, threading, signal, faulthandler
try:
    import msvcrt  # type: ignore
except ImportError:
    msvcrt = None
import tempfile

from threading import Thread
from utils.hashing import sha256_file
from utils.audio_utils import is_supported_audio
from pipeline.transcribe import transcribe_audio
from pipeline.parser import run_parser
from pipeline.publish import publish_inventory, publish_production,publish_sales
import os
import platform           # <-- FALTABA
from pathlib import Path  # <-- por si no estaba
import codecs
# --- arriba del archivo, con el resto de imports ---
import logging
import os, sys, time, subprocess, ctypes, json
from pathlib import Path
# === CATALOGO EMBEBIDO (solo _MEIPASS) ===
import sys, csv
from pathlib import Path

# === ENV desde bundle (_MEIPASS) o junto al exe ===
from dotenv import load_dotenv
from pathlib import Path
import sys, os

#============#
import platform
import subprocess
from pathlib import Path

def _install_mac_autorun():
    # Ruta al ejecutable actual (PyInstaller la expone así)
    exe_path = Path(sys.argv[0]).resolve()

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)

    plist_path = plist_dir / "com.cosito.voice.plist"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cosito.voice</string>

    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
        <string>--loop</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>WorkingDirectory</key>
    <string>{exe_path.parent}</string>

    <key>StandardOutPath</key>
    <string>{exe_path.parent}/cosito_mac.log</string>

    <key>StandardErrorPath</key>
    <string>{exe_path.parent}/cosito_mac_error.log</string>
</dict>
</plist>
"""

    plist_path.write_text(plist_content)

    # Cargar el LaunchAgent
    subprocess.run(["launchctl", "load", str(plist_path)], check=False)


#=============#





q = Queue(maxsize=16)
def load_env():
    """
    Carga .env desde el bundle (PyInstaller _MEIPASS) o, si no existe,
    desde el directorio del ejecutable. Finalmente carga el entorno del SO.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(sys.argv[0]).parent))
    env_path = base / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    # También permite variables del sistema / usuario sin machacar las ya cargadas
    load_dotenv(override=False)

load_env()

# === Fecha/hora desde nombre de archivo: RYYYYMMDDHHMMSS.WAV ===
import re, datetime as dt

def read_json_safe(path: Path) -> dict:
    for enc in ("utf-8-sig","utf-8"):
        try:
            return json.loads(path.read_text(encoding=enc))
        except UnicodeDecodeError:
            pass
    return json.loads(path.read_text(encoding="latin-1", errors="ignore"))

def captured_at_from_name(fname: str) -> str | None:
    """
    Devuelve ISO8601 Z (UTC) si fname = 'RYYYYMMDDHHMMSS.wav'
    """
    base = os.path.basename(fname)
    m = re.match(r"R(\d{14})\.", base, flags=re.IGNORECASE)
    if not m:
        return None
    s = m.group(1)
    try:
        t = dt.datetime.strptime(s, "%Y%m%d%H%M%S")
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None

def load_embedded_catalog() -> dict:
    """Lee catalog/catalog.tsv desde el bundle (PyInstaller _MEIPASS) o junto al exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(sys.argv[0]).parent))
    p = base / "catalog" / "catalog.tsv"
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    flavor_by_name = {}
    for row in csv.reader(text, delimiter="\t"):
        if not row or all(not c.strip() for c in row):
            continue
        a = row[0].strip().upper()
        b = (row[1].strip().upper() if len(row) > 1 else "")
        if not a or not b:
            continue
        # detecta qué celda parece SKU (contiene guión y es corta)
        if "-" in a and len(a) <= 8:
            sku, name = a, b
        elif "-" in b and len(b) <= 8:
            sku, name = b, a
        else:
            continue
        flavor_by_name[name] = sku
    return flavor_by_name

# Lee del entorno; si es 0 o vacío, NO hay watchdog
WATCHDOG_MAX_IDLE_SECS = int(os.getenv("WATCHDOG_MAX_IDLE_SECS", "0") or "0")
ARGS_RUN = "--loop"          # lo que usas para correr en modo continuo
from pathlib import Path
APP_NAME = Path(sys.argv[0]).stem  # p.ej. Cosito_CHICO



def default_app_root() -> Path:
    system = platform.system()
    if system == "Windows":
        # Antes: PROGRAMDATA = C:\ProgramData  (requiere admin en muchos PCs)
        # Ahora: usar carpeta del usuario: C:\Users\USER\AppData\Local\Cosito\...
        local_base = os.getenv("LOCALAPPDATA")
        if local_base:
            base = Path(local_base)  # normalmente C:\Users\USER\AppData\Local
        else:
            # Fallback por si LOCALAPPDATA no existe por alguna razón
            base = Path.home() / "AppData" / "Local"
        return base / "Cosito" / "voice-ingest" / APP_NAME

    elif system == "Darwin":  # macOS
        base = Path.home() / "Library" / "Application Support"
        return base / "Cosito" / "voice-ingest" / APP_NAME

    else:  # Linux
        base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return base / "Cosito" / "voice-ingest" / APP_NAME

APP_ROOT = default_app_root()
APP_ROOT.mkdir(parents=True, exist_ok=True)

LOG_DIR  = APP_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE    = APP_ROOT / "state.json"
LOCKFILE = APP_ROOT / f"{APP_NAME}.lock"

BASE_DIR = APP_ROOT   # si tu código usa BASE_DIR como raíz padre
INBOX_DIR = BASE_DIR / "storage" / "inbox"
PROCESSED_DIR = BASE_DIR / "storage" / "processed"
FAILED_DIR = BASE_DIR / "storage" / "failed"

LOCK_HANDLE = None
LOCK_PATH = Path(tempfile.gettempdir()) / "cosito_ingest.lock"
EXTRA_SCAN_DIRS = [
    p.strip()
    for p in os.getenv("EXTRA_SCAN_DIRS", "").split(os.pathsep)
    if p.strip()
]

EVISTR_LABEL = os.getenv("EVISTR_LABEL")          # p.ej. 'L357'
EVISTR_SUBDIR = os.getenv("EVISTR_SUBDIR", "RECORD")





def _ensure_dirs():
    # Solo lo necesario para logging y state al inicio
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

def _single_instance_lock():
    """
    Lock de instancia única basado en archivo + PID.
    - Si el archivo no existe: lo crea con el PID actual.
    - Si existe:
        * Si el PID grabado NO existe -> se considera lock viejo y se sobreescribe.
        * Si el PID existe -> se asume que ya hay otra instancia y se sale.
    """
    try:
        existing_pid = None
        if LOCKFILE.exists():
            try:
                text = LOCKFILE.read_text(encoding="utf-8").strip()
                if text:
                    existing_pid = int(text)
            except Exception:
                existing_pid = None

        if existing_pid is not None:
            # ¿Ese PID sigue vivo?
            alive = False
            try:
                p = psutil.Process(existing_pid)
                alive = p.is_running() and (p.status() != psutil.STATUS_ZOMBIE)
            except Exception:
                alive = False

            if alive:
                # Hay otra instancia viva: salimos
                raise SystemExit("Otra instancia ya está corriendo.")
            else:
                # Lock huérfano: lo sobreescribimos con nuestro PID
                LOCKFILE.write_text(str(os.getpid()), encoding="utf-8")
        else:
            # No había PID válido: escribimos nuestro PID
            LOCKFILE.write_text(str(os.getpid()), encoding="utf-8")

    except SystemExit:
        # Re-lanzamos SystemExit tal cual
        raise
    except Exception as e:
        # Si algo raro pasa, lo logeamos pero NO impedimos correr
        try:
            log.warning(f"No pude gestionar el lock de instancia única: {e}")
        except Exception:
            print(f"[WARN] No pude gestionar el lock: {e}", file=sys.stderr)

def _is_admin():
    # En macOS / Linux simplemente devolvemos False: no usamos este camino
    if platform.system() != "Windows":
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def _exe_and_workdir():
    # ruta al exe real cuando empaquetas con PyInstaller onefile
    exe_path = Path(sys.argv[0]).resolve()
    workdir = exe_path.parent
    return exe_path, workdir

def _task_exists(name=APP_NAME):
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", name],
                             capture_output=True, text=True)
        return out.returncode == 0
    except Exception:
        return False

def _install_task_onstart():
    exe, work = _exe_and_workdir()
    # crea/actualiza tarea al iniciar el sistema (requiere admin)
    cmd = [
        "schtasks", "/Create",
        "/SC", "ONSTART",
        "/RL", "HIGHEST",
        "/TN", APP_NAME,
        "/TR", f'"{exe}" {ARGS_RUN}',
        "/F"
    ]
    # define el directorio de inicio vía /ARG no existe; usamos Start in desde accesos directos, para tarea no aplica
    res = subprocess.run(" ".join(cmd), shell=True)
    if res.returncode != 0:
        raise RuntimeError("No pude crear la tarea programada (se necesitan privilegios de admin).")

def _install_startup_shortcut():
    # crea acceso directo en la carpeta Startup del usuario (sin admin)
    exe, work = _exe_and_workdir()
    startup = Path(os.getenv("APPDATA")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    lnk = startup / f"{APP_NAME}.lnk"
    ps = f'''
$sh = New-Object -ComObject WScript.Shell;
$lnk = $sh.CreateShortcut("{str(lnk)}");
$lnk.TargetPath = "{str(exe)}";
$lnk.WorkingDirectory = "{str(work)}";
$lnk.Arguments = "{ARGS_RUN}";
$lnk.WindowStyle = 7;  # minimizado
$lnk.IconLocation = "{str(exe)},0";
$lnk.Save();
'''
    subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command", ps], check=True)

def _mark_installed(method:str):
    STATE.write_text(json.dumps({"installed": True, "method": method, "ts": time.time()}, indent=2), encoding="utf-8")

def _already_installed():
    return STATE.exists()

def ensure_autorun():
    _ensure_dirs()
    _single_instance_lock()

    system = platform.system()

    if system == "Windows":
        if _already_installed():
            return
        try:
            if _is_admin():
                _install_task_onstart()
                _mark_installed("task_onstart")
            else:
                _install_startup_shortcut()
                _mark_installed("startup_shortcut")
        except:
            _install_startup_shortcut()
            _mark_installed("startup_shortcut_fallback")

    elif system == "Darwin":  # macOS
        # instalamos autorun estilo macOS
        _install_mac_autorun()

# === LOGGING a archivo + consola (rotativo) ===
import logging, logging.handlers
def setup_logging():
    _ensure_dirs()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL","INFO"))
    ch = logging.StreamHandler(sys.stdout); ch.setFormatter(fmt); root.addHandler(ch)
    fh = logging.handlers.RotatingFileHandler(str(LOG_DIR/"ingest.log"), maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt); root.addHandler(fh)
    log = logging.getLogger()

log = logging.getLogger()
# === LLAMADA DE ARRANQUE ===
setup_logging()
ensure_autorun()
logging.getLogger().info("Auto-run OK; método de instalación: %s",
                         json.loads(STATE.read_text(encoding="utf-8")).get("method"))





AUDIO_EXTS = set([e.strip().lower() for e in os.getenv("AUDIO_EXTS", ".wav,.mp3,.m4a").split(",")])

def is_supported_audio(path: Path, allowed_exts: set) -> bool:
    return path.suffix.lower() in allowed_exts

INBOX        = INBOX_DIR
PROCESSED    = PROCESSED_DIR
FAILED       = FAILED_DIR
MANIFESTS    = BASE_DIR / "storage" / "manifests"
UPLOAD_CACHE = BASE_DIR / "storage" / "uploads"
CONCURRENCY  = int(os.getenv("CONCURRENCY","2"))
INBOX.mkdir(parents=True, exist_ok=True)
DELETE_FROM_DEVICE_AFTER_COPY = os.getenv("DELETE_FROM_DEVICE_AFTER_COPY","false").lower()=="true"
DELETE_LOCAL_AFTER_POST       = os.getenv("DELETE_LOCAL_AFTER_POST","false").lower()=="true"
EXTRA_SCAN_DIRS = [p.strip() for p in os.getenv("EXTRA_SCAN_DIRS","").split(",") if p.strip()]

for d in [INBOX, PROCESSED, FAILED, MANIFESTS, UPLOAD_CACHE]:
    d.mkdir(parents=True, exist_ok=True)

CATALOG_TSV = os.getenv("CATALOG_TSV", "storage/catalog/flavors.tsv")



HEARTBEAT = APP_ROOT / "heartbeat.json"

def touch_heartbeat(why="ok"):
    try:
        HEARTBEAT.write_text(json.dumps({"ts": time.time(), "why": why}), encoding="utf-8")
    except Exception:
        pass

def watchdog_thread(max_idle_sec=300):
    while True:
        try:
            if HEARTBEAT.exists():
                data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
                age = time.time() - float(data.get("ts", 0))
                if age > max_idle_sec:
                    log.error(f"Watchdog: sin latido {int(age)}s (> {max_idle_sec}), saliendo…")
                    os._exit(1)  # dejar que el usuario/servicio lo relance
        except Exception:
            pass
        time.sleep(30)

# activar faulthandler y watchdog al inicio del main
faulthandler.enable()
signal.signal(getattr(signal, "SIGBREAK", signal.SIGINT), lambda *a: faulthandler.dump_traceback())


def load_flavor_catalog(tsv_path: str) -> dict:
    """
    Lee storage/catalog/flavors.tsv con formato:
    NOMBRE<TAB>SKU
    (Cada alias en una fila apuntando al mismo SKU)
    Devuelve: {"ALFAJOR":"GI-AL", "PIE DE LIMON":"GI-PL", ...}
    """
    m = {}
    p = pathlib.Path(tsv_path)
    if not p.exists():
        return m
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("\t")]
        if len(parts) >= 2:
            name, sku = parts[0].upper(), parts[1]
            if name:
                m[name] = sku
    return m




def list_removable_mounts():
    # Si pides estricto: respeta SOLO EXTRA_SCAN_DIRS
    if os.getenv("STRICT_SCAN_ONLY", "false").lower() == "true" and EXTRA_SCAN_DIRS:
        return [p for p in EXTRA_SCAN_DIRS if os.path.exists(p)]

    mounts = []

    # -------------------------
    # WINDOWS
    # -------------------------
    if os.name == 'nt':
        import string
        import ctypes

        DRIVE_REMOVABLE = 2
        GetDriveTypeW = ctypes.windll.kernel32.GetDriveTypeW
        GetVolumeInformationW = ctypes.windll.kernel32.GetVolumeInformationW

        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                t = GetDriveTypeW(root)
                if t != DRIVE_REMOVABLE or not os.path.exists(root):
                    continue

                # Si definiste etiqueta, filtra por ella
                if EVISTR_LABEL:
                    vol_name_buf = ctypes.create_unicode_buffer(1024)
                    fs_name_buf = ctypes.create_unicode_buffer(1024)
                    serial = ctypes.c_uint32()
                    max_comp_len = ctypes.c_uint32()
                    fs_flags = ctypes.c_uint32()

                    ok = GetVolumeInformationW(
                        ctypes.c_wchar_p(root),
                        vol_name_buf,
                        len(vol_name_buf),
                        ctypes.byref(serial),
                        ctypes.byref(max_comp_len),
                        ctypes.byref(fs_flags),
                        fs_name_buf,
                        len(fs_name_buf),
                    )

                    if ok:
                        label = (vol_name_buf.value or "").strip()
                        # Compara ignorando mayúsculas/minúsculas
                        if EVISTR_LABEL.lower() not in label.lower():
                            continue

                # Si hay subcarpeta (RECORD), apunta directo ahí
                full_path = os.path.join(root, EVISTR_SUBDIR)
                if os.path.exists(full_path):
                    mounts.append(full_path)
                else:
                    # Si no existe la subcarpeta, al menos añade la raíz
                    mounts.append(root)

            except Exception:
                pass

        # Agrega paths extra fijos si quieres
        mounts.extend([p for p in EXTRA_SCAN_DIRS if os.path.exists(p)])

        # Deduplicar
        seen, uniq = set(), []
        for m in mounts:
            if m and m not in seen:
                uniq.append(m)
                seen.add(m)
        return uniq

    # -------------------------
    # LINUX / macOS
    # -------------------------
    for p in psutil.disk_partitions(all=False):
        try:
            fstype = (p.fstype or "").lower()
        except Exception:
            fstype = ""
        if (
            "media" in p.mountpoint.lower()
            or "volumes" in p.mountpoint.lower()
            or fstype in ("vfat", "exfat", "msdos", "ntfs", "hfs", "apfs")
        ):
            # Si tienes EVISTR_SUBDIR, prefierelo
            full_path = os.path.join(p.mountpoint, EVISTR_SUBDIR)
            if os.path.exists(full_path):
                mounts.append(full_path)
            else:
                mounts.append(p.mountpoint)

    mounts.extend([p for p in EXTRA_SCAN_DIRS if os.path.exists(p)])

    seen, uniq = set(), []
    for m in mounts:
        if m and m not in seen and os.path.exists(m):
            uniq.append(m)
            seen.add(m)

    return uniq

def wait_for_file_ready(path: Path, min_stable_secs: float = 1.5, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    last = -1; stable_since = None
    while time.time() < end:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(0.2); continue
        if size == last:
            if stable_since is None: stable_since = time.time()
            if time.time() - stable_since >= min_stable_secs: return True
        else:
            last = size; stable_since = None
        time.sleep(0.25)
    return False

def discover_new_audios(src_mount: str):
    new_files = []
    for root, _, files in os.walk(src_mount):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTS:
                src = Path(root) / f
                try:
                    h = sha256_file(src)
                    man = MANIFESTS / f"{h}.json"
                    if man.exists():
                        continue

                    dst = INBOX / f"{h}{ext}"   # usar INBOX absoluto
                    if not dst.exists():
                        shutil.copy2(src, dst)

                    meta = {
                        "status": "copied",
                        "hash": h,
                        "source": str(src.resolve()),
                        "local_path": str(dst.resolve()),   # ABSOLUTA
                        "orig_name": f,                      # nombre original
                        "filename": dst.name,
                        "ext": ext.lower(),
                        "created_at": dt.datetime.utcnow().isoformat() + "Z",
                    }
                    man.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    new_files.append(dst)

                    if DELETE_FROM_DEVICE_AFTER_COPY:
                        try:
                            if src.stat().st_size == dst.stat().st_size:
                                os.remove(src)
                            else:
                                print(f"[WARN] Tamaño difiere, no borro origen: {src}", file=sys.stderr)
                        except Exception as e:
                            print(f"[WARN] No pude borrar en dispositivo: {src} -> {e}", file=sys.stderr)

                except Exception as e:
                    print(f"[WARN] Error copiando {src}: {e}", file=sys.stderr)
    return new_files

def process_one(manifest_path: pathlib.Path):
    try:
        meta = read_json_safe(manifest_path)
        hash_id = meta.get("hash") or manifest_path.stem
        audio = Path(meta.get("local_path") or (INBOX / f"{hash_id}.wav"))
        # Normaliza extensión si guardas .mp3/.m4a a veces
        if not wait_for_file_ready(audio):
            log.warning("Archivo no llegó a estar estable a tiempo: %s", audio)
            raise RuntimeError("Audio inválido o no encontrado")
        if not audio.exists():
            # intenta otras extensiones conocidas por si el escáner guardó distinto
            for ext in (".wav", ".mp3", ".m4a", ".WAV", ".MP3", ".M4A"):
                cand = audio.with_suffix(ext)
                if cand.exists():
                    audio = cand
                    break

        # Espera a que termine de copiarse
        if not wait_for_file_ready(audio):
            logging.getLogger().warning("Archivo no llegó a estar estable a tiempo: %s", audio)
            raise RuntimeError("Audio inválido o no encontrado")

        # Valida extensión en minúscula
        if not audio.exists() or audio.suffix.lower() not in AUDIO_EXTS:
            logging.getLogger().warning(
                "Audio no válido: path=%s exists=%s ext=%s allowed=%s",
                audio, audio.exists(), audio.suffix, AUDIO_EXTS
            )
            raise RuntimeError("Audio inválido o no encontrado")

        hash_id = meta.get("hash","unknown")
        result_dir = BASE_DIR / "storage" / "results"
        result_dir.mkdir(parents=True, exist_ok=True)


        # 1) Transcripción
        transcript, stt_meta = transcribe_audio(audio)

        # LOG: muestra primeras ~160 chars
        preview = (transcript[:160] + "…") if len(transcript) > 160 else transcript
        log.info(f"[{hash_id}] transcript: {preview!r}")

        # DUMP: transcript a archivo (útil para auditar)
        (result_dir / f"{hash_id}.transcript.txt").write_text(transcript, encoding="utf-8")

                # 2) Parser
        flavor_by_name = load_flavor_catalog(CATALOG_TSV)
        default_loc = os.getenv("LOCATION_CODE", "MALLPLAZA-BOG")
        parsed_obj, parse_meta = run_parser(
            transcript,
            attempt_hint=None,
            flavor_by_name=None,
            default_location=default_loc
        )

        # === nombre de archivo para hora YYYYMMDDHHMMSS ===
        audio_name = Path(meta.get("orig_name") or audio.name).name
        cap = captured_at_from_name(audio_name)
        # LOG: resumen del parsed
        intent = (parsed_obj.get("intent") or "").upper()
        if intent == "INVENTORY":
            items = parsed_obj.get("items", [])
            log.info(f"[{hash_id}] parsed intent=INVENTORY items={len(items)} -> {items}")
            
        elif intent in ("PRODUCTION", "BATCH"):
            batches = parsed_obj.get("batches", [])
            log.info(f"[{hash_id}] parsed intent=PRODUCTION batches={len(batches)} -> {batches}")
            
        elif intent == "SALES":
            items = parsed_obj.get("items", [])
            log.info(f"[{hash_id}] parsed intent=SALES items={len(items)} -> {items}")
            
        else:
            log.warning(f"[{hash_id}] parsed intent desconocido: {intent}")

        # DUMP: parsed completo
        (result_dir / f"{hash_id}.parsed.json").write_text(
            json.dumps(parsed_obj, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # 3) Publicación (con resumen para manifiesto)
        posted_inventory = []
        posted_batches = []
        posted_sales = None

        # Nota: dejamos captured_at=None; los publishers calculan fecha/hora desde filename
        if intent == "INVENTORY":
            items = parsed_obj.get("items", [])
            for it in items:
                it["filename"] = audio_name
            posted_inventory = publish_inventory(transcript, items, captured_at=None)
            for i, r in enumerate(posted_inventory, 1):
                log.info(f"[{hash_id}] POST inventory #{i}: payload={r.get('payload')} resp={r.get('response')}")

        elif intent in ("PRODUCTION", "BATCH"):
            entries = parsed_obj.get("batches", [])
            for b in entries:
                b["filename"] = audio_name
            posted_batches = publish_production(entries, transcript=transcript, captured_at=None)
            for i, r in enumerate(posted_batches, 1):
                log.info(f"[{hash_id}] POST production #{i}: payload={r.get('payload')} resp={r.get('response')}")

        elif intent == "SALES":
            items = parsed_obj.get("items", [])
            for it in items:
                it["filename"] = audio_name
            posted_sales = publish_sales(
                transcript=transcript,
                items=items,
                captured_at=None,
                audio_name=audio_name,
                notes=""
            )
            # es un único POST; posted_sales es lista con un elemento
            for i, r in enumerate(posted_sales, 1):
                log.info(f"[{hash_id}] POST sales #{i}: payload={r.get('payload')} resp={r.get('response')}")

        else:
            raise RuntimeError(f"Intent desconocido: {parsed_obj.get('intent')}")

        # DUMP: qué se posteó (para auditoría)
        (result_dir / f"{hash_id}.posted.json").write_text(
            json.dumps(
                {"inventory": posted_inventory, "batches": posted_batches, "sales": posted_sales},
                ensure_ascii=False, indent=2
            ),
            encoding="utf-8"
        )

        # 4) Estado final en manifiesto
        meta.update({
            "status": "processed",
            "stt_meta": stt_meta,
            "parse_meta": parse_meta,
            "parsed_obj": parsed_obj,
            "posted_inventory": posted_inventory,
            "posted_batches": posted_batches,
            "posted_sales": posted_sales,
            "processed_at": time.time()
        })
        manifest_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        # 5) Borrado/movido local
        if DELETE_LOCAL_AFTER_POST:
            try:
                os.remove(audio)
            except Exception as e:
                log.warning(f"[{hash_id}] No pude borrar local {audio}: {e}")
        else:
            shutil.move(str(audio), PROCESSED / audio.name)

        log.info(f"[{hash_id}] ✅ done intent={intent}")
        return True

    except Exception as e:
        # Error path: marca manifiesto y mueve audio a failed
        try:
            meta = read_json_safe(manifest_path)
        except Exception:
            meta = {}

        meta.update({"status": "failed", "error": str(e), "failed_at": time.time()})
        manifest_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        lp = meta.get("local_path")
        if lp and pathlib.Path(lp).exists():
            try:
                shutil.move(lp, FAILED / pathlib.Path(lp).name)
            except Exception as me:
                log.error(f"[{meta.get('hash','?')}] no pude mover a failed: {me}")
        log.error(f"[{meta.get('hash','?')}] ❌ {manifest_path.name}: {e}")
        return False

def worker():
    while True:
        man = q.get()
        try:
            process_one(man)
        except Exception as e:
            log.exception("worker error: %s", e)
        finally:
            q.task_done()  # <- SIEMPRE

# Lanzar workers como daemon
for _ in range(2):  # o el número que uses
    Thread(target=worker, daemon=True).start()

def run_once():
    mounts = list_removable_mounts()
    if not mounts:
        print("No se detectan grabadoras montadas ni EXTRA_SCAN_DIRS.")
    total_new = []
    for m in mounts:
        total_new.extend(discover_new_audios(m))
    print(f"Nuevos copiados: {len(total_new)}")

    pendings = []
    for p in MANIFESTS.glob("*.json"):
        try:
            st = json.loads(p.read_text()).get("status")
        except Exception:
            st = None
        if st == "copied":
            pendings.append(p)

    if not pendings:
        print("Nada pendiente.")
        return

    # Encola en la cola GLOBAL y espera a que terminen los workers
    for man in pendings:
        q.put(man)

    q.join()

if __name__ == "__main__":
    if WATCHDOG_MAX_IDLE_SECS > 0:
        log.info(f"Iniciando watchdog con timeout={WATCHDOG_MAX_IDLE_SECS}s")
        Thread(target=watchdog_thread, args=(WATCHDOG_MAX_IDLE_SECS,), daemon=True).start()
    else:
        log.info("Watchdog deshabilitado (WATCHDOG_MAX_IDLE_SECS=0)")
    touch_heartbeat("start")
    LOOP = "--loop" in sys.argv
    interval = 15
    if LOOP:
        print("Ingestor en modo continuo. Ctrl+C para salir.")
        while True:
            run_once()
            time.sleep(interval)
    else:
        run_once()


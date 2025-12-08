
import os, json, uuid, pathlib, requests
from dotenv import load_dotenv
from pathlib import Path
# --- asegurar que el .env esté cargado ---
import os, sys
from pathlib import Path
import os, json, uuid, pathlib, requests, platform, socket
from dotenv import load_dotenv
from pathlib import Path


try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    exe_dir = Path(sys.argv[0]).resolve().parent
    env_path = exe_dir / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    else:
        # si el exe está empaquetado (PyInstaller)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            emb = Path(meipass) / ".env"
            if emb.exists():
                load_dotenv(emb, override=False)

    # sincroniza API_BASE y API_BASE_URL
    if not os.getenv("API_BASE") and os.getenv("API_BASE_URL"):
        os.environ["API_BASE"] = os.getenv("API_BASE_URL")
# --- fin carga .env ---

API_BASE = os.getenv("API_BASE","https://voice.api.cosito.ai/api/v1").rstrip("/")
DEVICES_REGISTER = f"{API_BASE}/devices/register"
DEVICE_AUTH      = f"{API_BASE}/devices/auth"

BAKERY_DIR      = pathlib.Path(os.getenv("BAKERY_DIR","~/.bakery")).expanduser()
DEVICE_CREDS_FN = BAKERY_DIR / "device_creds.json"
DEVICE_TOKEN_FN = BAKERY_DIR / "device_token.json"

def _ensure_dirs():
    BAKERY_DIR.mkdir(parents=True, exist_ok=True)

def build_headers(require_auth=True):
    h = {"Content-Type": "application/json", "X-Idempotency-Key": str(uuid.uuid4())}
    env_tok = (os.getenv("API_TOKEN") or "").strip()
    if require_auth:
        if env_tok:
            h["Authorization"] = env_tok if env_tok.startswith("Bearer ") else f"Bearer {env_tok}"
        elif DEVICE_TOKEN_FN.exists():
            try:
                tok = json.loads(DEVICE_TOKEN_FN.read_text(encoding="utf-8")).get("access_token")
                if tok:
                    h["Authorization"] = f"Bearer {tok}"
            except Exception:
                pass
    return h

def create_device():
    owner_email = os.getenv("OWNER_EMAIL")
    if not owner_email:
        raise RuntimeError("Falta OWNER_EMAIL")
    name = (os.getenv("DEVICE_NAME") or platform.node() or socket.gethostname() or "pc").strip()
    payload = {
        "device_name": name,
        "device_type": "pc_usb_ingest",
        "owner_email": owner_email,
        "location": os.getenv("LOCATION_CODE","Field"),
        "description": f"USB ingest device: {name}",
        "scopes": ["project:read","project:write"]
    }
    r = requests.post(DEVICES_REGISTER, json=payload, timeout=20)
    if r.status_code != 201:
        raise RuntimeError(f"register {r.status_code}: {r.text}")
    data = r.json()
    _ensure_dirs()
    DEVICE_CREDS_FN.write_text(json.dumps({
        "device_id": data.get("device_id"),
        "client_id": data.get("client_id"),
        "client_secret": data.get("client_secret")
    }, indent=2), encoding="utf-8")
    return data

def device_join(client_id=None, client_secret=None, scope="project:read project:write"):
    if not client_id or not client_secret:
        if DEVICE_CREDS_FN.exists():
            c = json.loads(DEVICE_CREDS_FN.read_text(encoding="utf-8"))
            client_id = client_id or c.get("client_id")
            client_secret = client_secret or c.get("client_secret")
    if not client_id or not client_secret:
        raise RuntimeError("Faltan client_id/client_secret; ejecuta create_device() primero")
    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope
    }
    r = requests.post(DEVICE_AUTH, data=form, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"auth {r.status_code}: {r.text}")
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError("Sin access_token en respuesta de /devices/auth")
    _ensure_dirs()
    DEVICE_TOKEN_FN.write_text(json.dumps({"access_token": tok}, indent=2), encoding="utf-8")
    return tok

def ensure_device_and_token():
    if (os.getenv("API_TOKEN") or "").strip():
        return
    try:
        if DEVICE_TOKEN_FN.exists() and json.loads(DEVICE_TOKEN_FN.read_text()).get("access_token"):
            return
    except Exception:
        pass
    need_register = True
    if DEVICE_CREDS_FN.exists():
        try:
            c = json.loads(DEVICE_CREDS_FN.read_text())
            if c.get("client_id") and c.get("client_secret"):
                need_register = False
        except Exception:
            pass
    if need_register:
        create_device()
    device_join()

def _post_json(url, payload, require_auth=True, timeout=45, _retry=False):
    headers = build_headers(require_auth=require_auth)
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code == 401 and require_auth and not _retry:
        try:
            device_join()
        except Exception:
            r.raise_for_status()
        headers = build_headers(require_auth=require_auth)
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} {url}: {r.text}")
    try:
        return r.json()
    except Exception:
        return {"status":"ok","http_status":r.status_code}

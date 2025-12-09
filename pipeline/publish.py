import os, json, datetime as dt
from utils.auth import ensure_device_and_token, _post_json, DEVICE_CREDS_FN
import requests
from requests.adapters import HTTPAdapter, Retry

import requests
from requests.adapters import HTTPAdapter, Retry

# --- asegurar que el .env esté cargado ---
import os, sys
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
PRODUCTION_ENDPOINT = f"{API_BASE}/bakery/production/batches"
INVENTORY_ENDPOINT  = f"{API_BASE}/bakery/inventory/movements"
SALES_ENDPOINT      = f"{API_BASE}/bakery/sales" 
LOCATION_CODE = os.getenv("LOCATION_CODE","Calle-97")
# --- Sesión HTTP global con reintentos y backoff ---
_http = requests.Session()
_retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.8,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
)
_http.mount("https://", HTTPAdapter(max_retries=_retry))
_http.mount("http://",  HTTPAdapter(max_retries=_retry))


import os, re, datetime as dt
from pathlib import Path

# Puedes ajustar esto si quieres forzar la zona horaria local del sitio:
LOCATION_TZ = os.getenv("LOCATION_TZ", "America/Bogota")  # o deja "" para no usar TZ
try:
    from zoneinfo import ZoneInfo  # Py>=3.9
except Exception:
    ZoneInfo = None

_TS14 = re.compile(r"(\d{14})")  # busca 14 dígitos seguidos

def _parse_filename_timestamp(name: str) -> dt.datetime | None:
    """
    Busca 'YYYYMMDDHHMMSS' dentro del nombre. Ej: R20251007070752.WAV
    Devuelve datetime *naive* (sin tz) con esa fecha/hora.
    """
    if not name:
        return None
    m = _TS14.search(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except Exception:
        return None

def _from_filename_or_now(filename: str | None, fallback_iso_utc: str) -> tuple[str, str, str]:
    """
    A partir del filename genera:
      captured_at_iso_utc, date_local, time_local
    - Si LOCATION_TZ existe y zoneinfo está disponible, interpreta el timestamp del
      archivo en esa TZ local y lo convierte a UTC para captured_at.
    - Si no hay TZ disponible, usa el timestamp tal cual para date/time y also
      lo forma en ISO 'naive' + 'Z' (asumiendo que era ya UTC).
    """
    # valor por defecto a partir del fallback
    ts_iso = (fallback_iso_utc or dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    ymd = ts_iso[:10]         # YYYY-MM-DD
    hms = ts_iso[11:19]       # HH:MM:SS

    if not filename:
        return ts_iso, ymd, hms

    base = Path(filename).name
    naive = _parse_filename_timestamp(base)
    if not naive:
        return ts_iso, ymd, hms

    # Si tenemos zona horaria, interpretamos ese datetime como local y convertimos a UTC
    if LOCATION_TZ and ZoneInfo:
        try:
            local_dt = naive.replace(tzinfo=ZoneInfo(LOCATION_TZ))
            utc_dt = local_dt.astimezone(dt.timezone.utc)
            ts_iso = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            # date/time en hora local (la que se ve en planta)
            ymd = local_dt.strftime("%Y-%m-%d")
            hms = local_dt.strftime("%H:%M:%S")
            return ts_iso, ymd, hms
        except Exception:
            pass

    # Sin TZ: usamos naive como "local" para date/time y construimos captured_at en 'Z'
    ymd = naive.strftime("%Y-%m-%d")
    hms = naive.strftime("%H:%M:%S")
    ts_iso = naive.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ts_iso, ymd, hms


def _device_id_str():
    c = json.loads(DEVICE_CREDS_FN.read_text(encoding="utf-8"))
    return str(c["device_id"])


def _infer_movement_type(transcript: str) -> str:
    """
    Decide movement_type para la API de inventory movements,
    usando solo el lenguaje natural del transcript.

    - Si suena a recepción/envío desde fábrica → RECEIVED_FROM_FACTORY
    - Si suena a stock disponible / quedan → CURRENT_STOCK
    """
    t = (transcript or "").lower()

    # Palabras que indican recepción/envío desde fábrica
    received_words = [
        "recibimos", "recibí", "recibi", "recibio", "recibió",
        "llegaron", "llego", "llegó",
        "se enviaron", "enviamos", "envié", "envie",
        "mandamos", "mandé", "mande",
        "salieron", "salio", "salió",
        "desde fábrica", "de fabrica", "de fábrica",
        "recibido de fábrica", "recibido de fabrica"
    ]
    if any(w in t for w in received_words):
        return "RECEIVED_FROM_FACTORY"

    # Palabras que indican stock actual
    stock_words = [
        "quedan", "queda",
        "hay", "hay en stock", "stock",
        "en inventario", "inventario",
        "disponibles", "disponible"
    ]
    if any(w in t for w in stock_words):
        return "CURRENT_STOCK"

    # Por defecto, si no está claro, lo tratamos como stock actual
    return "CURRENT_STOCK"


def publish_inventory(transcript: str, items: list, captured_at: str = None):
    """
    Publica movimientos de inventario contra la API de inventory movements.

    - Usa SIEMPRE el mismo endpoint INVENTORY_ENDPOINT.
    - Decide movement_type en función del transcript:
        * 'se enviaron / mandamos / llegaron / recibimos' → RECEIVED_FROM_FACTORY
        * 'quedan / hay / stock / inventario'            → CURRENT_STOCK
    - Hace UN POST por item (como antes).
    """
    ensure_device_and_token()
    device_id = _device_id_str()

    movement_type = _infer_movement_type(transcript or "")
    results = []

    for it in items or []:
        audio_name = (
            it.get("filename")
            or it.get("audio_filename")
            or it.get("source_name")
            or it.get("audio_name")
            or it.get("audio")
        )

        cap_iso, _, _ = _from_filename_or_now(audio_name, captured_at)

        product_name = (
            it.get("product_name")
            or it.get("flavor_name")
            or it.get("flavor")
            or it.get("name")
            or ""
        ).strip()

        sku = (it.get("sku") or "").strip()

        qty_raw = it.get("qty", it.get("quantity"))
        try:
            qty_val = int(qty_raw) if qty_raw is not None else 0
        except Exception:
            qty_val = 0

        if not product_name or not sku or qty_val <= 0:
            results.append({
                "payload": {
                    "sku": sku,
                    "product_name": product_name,
                    "quantity": qty_val
                },
                "response": {
                    "error": "Campos obligatorios inválidos (product_name/sku/quantity)"
                }
            })
            continue

        # ↕ cómo mapeamos la cantidad:
        # para RECEIVED_FROM_FACTORY: delta positivo = lo que entró
        # para CURRENT_STOCK: el backend probablemente interpretará este valor
        quantity_delta = qty_val

        # Armamos el cuerpo tal como lo espera el backend
        payload = {
            "device_id": device_id,
            "location_code": str(LOCATION_CODE),
            "captured_at": cap_iso,
            "inventory_movement": {
                "sku": sku,
                "product_name": product_name,
                "location": str(LOCATION_CODE),     # reutilizamos la misma location
                "quantity_delta": quantity_delta,
                "reason": movement_type,            # p.ej. "RECEIVED_FROM_FACTORY" o "CURRENT_STOCK"
            },
            "notes": transcript or ""
        }

        resp = _post_json(
            INVENTORY_ENDPOINT,
            payload,
            require_auth=True,
            timeout=45
        )

        results.append({"payload": payload, "response": resp})

    return results

import datetime as dt

def _iso_now():
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _ymd(captured_at_iso: str) -> tuple[str, str, str]:
    """
    Recibe 'YYYY-MM-DDTHH:MM:SSZ' y devuelve (YYYYMMDD, YYYY-MM-DD, HH:MM:SS)
    """
    ts = captured_at_iso or _iso_now()
    # Maneja Zulu
    ts2 = ts.replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(ts2)
    return (t.strftime("%Y%m%d"), t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"))

def publish_production(entries: list, transcript: str = None, captured_at: str = None):
    ensure_device_and_token()  # asegura que exista token o se registre
    

    device_id = _device_id_str()

    results = []
    for e in entries:
        # --- NUEVO: intenta leer el nombre de archivo desde la entrada ---
        # Usa la clave que tengas disponible; dejo varias alternativas:
        audio_name = (
            e.get("filename")
            or e.get("audio_filename")
            or e.get("source_name")
            or e.get("audio_name")
            or e.get("audio")              # si guardas la ruta completa aquí
        )

        # construye captured_at/date/time desde el filename (o cae al now)
        cap_iso, date_local, time_local = _from_filename_or_now(audio_name, captured_at)

        # normaliza campos
        flavor = (e.get("flavor") or e.get("flavor_name") or e.get("product_name") or "").strip()
        sku = (e.get("sku") or "").strip()
        blender = str(e.get("blender") or e.get("blender_number") or "").strip()
        qty_raw = e.get("qty", e.get("quantity"))
        try:
            qty_val = int(qty_raw) if qty_raw is not None else 0
        except Exception:
            qty_val = 0

        if not flavor or not sku or qty_val <= 0:
            results.append({"payload": {"sku": sku, "flavor": flavor}, "response": {"error": "Campos obligatorios inválidos"}})
            continue

        bnum = blender if blender else "0"
        batch_code = f"{date_local.replace('-','')}B{bnum}{sku}"

        production_batch = {
            "batch_code": batch_code,
            "date": date_local,           # <- tomado del filename
            "time": time_local,           # <- tomado del filename
            "blender_number": blender,
            "sku": sku,
            "flavor": flavor,
            "quantity": {"value": qty_val, "unit": "UNITS"},
            "reason": "VOICE_COUNT",
        }

        payload = {
            "device_id": device_id,
            "location_code": str(LOCATION_CODE),
            "captured_at": cap_iso,       # <- ISO UTC desde filename
            "audio_ref": {
                "transcript": (transcript or e.get("transcript") or ""),
                "audio_url": None
            },
            "production_batch": production_batch
        }
        resp = _post_json(PRODUCTION_ENDPOINT, payload, require_auth=True, timeout=45)
        results.append({"payload": payload, "response": resp})

    return results


def publish_sales(transcript: str, items: list, captured_at: str = None, notes: str = "", audio_name: str = ""):
    # Siempre asegura autenticación (si ya tienes ensure_device_and_token y _post_json, úsalos)
    ensure_device_and_token()

    device_id   = _device_id_str()
    captured_at = captured_at or _iso_now()

    # 1) Normalizar
    norm_items = []
    for it in items or []:
        qty = int(it.get("quantity") or it.get("qty") or 0)
        sku = (it.get("sku") or "").strip()
        name = (it.get("name") or it.get("flavor_name") or "").strip()
        if qty > 0 and sku:                       # <-- filtro: sólo líneas válidas
            norm_items.append({
                "sku": sku,
                "product_name": name or None,
                "quantity": qty,
                "unit": it.get("unit") or "UNITS",
            })

    # 2) Si no hay ítems válidos, no sigas
    if not norm_items:
        return {"success": False, "message": "Sin items válidos para venta", "items": []}

    # 3) Construir payload **antes** del try, para que siempre exista
    payload = {
        "device_id": device_id,
        "location_code": str(LOCATION_CODE),
        "captured_at": captured_at,
        "audio_ref": {"transcript": transcript or "", "audio_url": None},
        "items": norm_items,
        "notes": notes or ""
    }

    # 4) POST con auth
    try:
        resp = _post_json(SALES_ENDPOINT, payload, require_auth=True, timeout=45)
        return [{"payload": payload, "response": resp}]
    except Exception as e:
        raise

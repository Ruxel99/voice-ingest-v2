# pipeline/parser.py
# --- al inicio de pipeline/parser.py (importa csv y Path) ---
import os, json, re
import datetime as dt
import openai
import csv
import sys
from pathlib import Path

# imports arriba (asegúrate de tenerlos):
import sys, csv, logging, pkgutil
from pathlib import Path
# Catálogo por defecto (último fallback). NOMBRE → SKU (en mayúsculas)
DEFAULT_CATALOG = {
    "ALFAJOR": "GI-AL",
    "AVELLANA": "GI-AV",
    "BISCOFF": "GI-BI",
    "CARAMEL DREAM": "GI-CD",
    "CHOCO-BROWNIE": "GI-CB",
    "CHOCO BROWNIE": "GI-CB",
    "LA ORIGINAL": "GI-OR",
    "MES": "GI-MES",
    "MILOVE": "GI-MI",
    "MI LOVE": "GI-MI",
    "OH-T MEAL": "GI-OH",
    "OH T MEAL": "GI-OH",
    "PIE DE LIMON": "GI-PL",
    "RED VELVET": "GI-RV",
    "S´MORES": "GI-SM",
    "SMORES": "GI-SM",
    "SECRETO DE CARAMELO": "GI-SC",
    "COOKIES AND CREAM": "BI-CC",
    "KINDER": "BI-KI",
    "SNICKERS": "BI-SN",
    "CAMPFIRE": "BI-CA",
    "LECHE KLIM": "BI-LK",
    "MINIS": "MIN",
    "MASA 40 (C)": "C-40",
    "MASA 75 (C)": "C-75",
    "PANES (C)": "C-C",
    "NUTS4YOU": "GI-NU",
    "MACADAMIA": "GI-NU",
    "Pistacho Bliss": "GI-PB",
    "PISTACHO BLISS": "GI-PB",
    "PISTACHO": "GI-PB",
}

def _load_embedded_catalog() -> dict:
    """
    Lee catalog/catalog.tsv desde:
    1) _MEIPASS (PyInstaller onefile, solo-lectura)
    2) pkgutil.get_data (fallback dentro del bundle)
    3) junto al ejecutable (modo dev)
    """
    flavor_by_name = {}

    def _parse_tsv_lines(lines):
        out = {}
        for row in csv.reader(lines, delimiter="\t"):
            if not row or all(not c.strip() for c in row):
                continue
            a = row[0].strip().upper()
            b = (row[1].strip().upper() if len(row) > 1 else "")
            if not a or not b:
                continue
            if "-" in a and len(a) <= 8:
                sku, name = a, b
            elif "-" in b and len(b) <= 8:
                sku, name = b, a
            else:
                continue
            out[name] = sku
        return out

    # 1) _MEIPASS
    try:
        base = Path(getattr(sys, "_MEIPASS", Path(sys.argv[0]).parent))
        p = base / "catalog" / "catalog.tsv"
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            flavor_by_name = _parse_tsv_lines(text)
            logging.getLogger().info("Catálogo embebido cargado (_MEIPASS): %d items", len(flavor_by_name))
            return flavor_by_name
    except Exception as e:
        logging.getLogger().warning("No se pudo leer catálogo desde _MEIPASS (%s). Intentando pkgutil...", e)

    # 2) pkgutil (dentro del bundle)
    try:
        data = pkgutil.get_data(__package__ or "__main__", "catalog/catalog.tsv")
        if data:
            text = data.decode("utf-8", errors="ignore").splitlines()
            flavor_by_name = _parse_tsv_lines(text)
            logging.getLogger().info("Catálogo embebido cargado (pkgutil): %d items", len(flavor_by_name))
            return flavor_by_name
    except Exception as e:
        logging.getLogger().warning("pkgutil.get_data falló (%s). Intentando ruta local...", e)

    # 3) Al lado del exe (modo dev)
    try:
        dev_p = Path(sys.argv[0]).resolve().parent / "catalog" / "catalog.tsv"
        if dev_p.exists():
            text = dev_p.read_text(encoding="utf-8", errors="ignore").splitlines()
            flavor_by_name = _parse_tsv_lines(text)
            logging.getLogger().info("Catálogo local cargado (dev): %d items", len(flavor_by_name))
            return flavor_by_name
    except Exception as e:
        logging.getLogger().warning("Fallo leyendo catálogo local (dev) (%s).", e)

    logging.getLogger().warning("Catálogo no disponible. Usando vacío.")
    logging.getLogger().warning("Catálogo no disponible. Usando DEFAULT_CATALOG (%d items).", len(DEFAULT_CATALOG))
    return DEFAULT_CATALOG.copy()


# === Utilidad: normalizar catálogo de sabores si lo tienes a mano ===
# Espera un dict "flavor_by_name" = {"ALFAJOR":"GI-AL", ...}
def _catalog_block(flavor_by_name: dict) -> str:
    if not flavor_by_name:
        return "- (no catalog provided)"
    lines = [f'- "{name}": "{sku or ""}"' for name, sku in flavor_by_name.items()]
    return "\n".join(lines)

def _build_parser_prompt(transcript: str, flavor_by_name: dict, today_iso: str, default_location: str):
    catalog_block = _catalog_block(flavor_by_name)
    return f"""
You are a deterministic voice command parser. You must return ONLY one valid JSON object.

The transcript may contain multiple repeated patterns and multiple product families (for example SMORES and MINIS).
You MUST extract ALL of them. Do not merge, average, or drop anything. If there are N numeric quantities in the
transcript that refer to products, you MUST return at least N items/batches in the JSON.

### Catalog (flavor_name → sku)
{catalog_block}

### Intents

We only have three intents:

- "PRODUCTION"  → cuando se está HACIENDO / PRODUCIENDO ("hice", "se hicieron", "en la batidora X", etc.).
- "INVENTORY"   → cuando se habla de stock actual o movimientos de inventario:
    - stock actual: ("quedan", "queda", "hay", "hay en inventario", "stock", "en inventario").
    - movimientos de inventario / recibidos de fábrica: ("recibimos", "recibí", "recibio", "recibió",
      "se enviaron", "enviamos", "mandamos", "llegaron", "llegó", "salieron", "desde fábrica", "de fábrica").
  En TODOS esos casos, el intent sigue siendo "INVENTORY".
- "SALES"       → cuando se habla de ventas ("vendí", "vendimos", "se vendieron", "ventas").

### GENERAL SALES PATTERN (ONLY IF INTENT = 'SALES')

Sales is LESS frequent.  
You MUST choose intent = "SALES" ONLY when the transcript clearly talks about selling
(using words like "vendí", "vendimos", "venta", "ventas", "se vendieron").

If there is NO clear selling language, you MUST NOT choose "SALES".
In those cases, if there are patterns like "<number> de <flavor>" without selling verbs,
you MUST default to "INVENTORY" (stock / inventory), NOT "SALES".

For SALES, every time you see a pattern like:

- "<number> de <flavor>"
- "<number> <flavor>"
- "<number> en <flavor>"

you MUST create **one separate item** in the "items" array.

This applies even when they are chained with commas or "y":

- "1 de alfajor, 2 de pay de limón, 3 de avellana"
- "5 de minis, 4 en for you, 2 de campfire"
- "4 de smurfs, 1 de smurfs, 2 de alfajor y 2 de pay de limón"

Each numeric pattern (1, 2, 3, 5, 4, 2, 4, 1, 2, 2, ...) MUST produce its own JSON item.
Do NOT fuse "5 de minis, 4 en for you" into a single item. They are **two distinct items**:
one for MINIS and one for NUTS FOR YOU (or the closest matching catalog flavor).

### ASR / Speech Recognition Noise (IMPORTANT FOR INVENTORY)

Sometimes the transcript will contain small errors from speech recognition.
You MUST interpret them as the most likely Spanish word according to context:

- "kean", "ke an", "quean", "que an", "ke en" at the beginning of the phrase,
  followed by a number and a flavor, MUST be interpreted as "quedan":
  - Example: "Kean 45 de Chocó Brownie" → interpret as "quedan 45 de Choco Brownie" (INVENTORY).

- Phrases like "que es 2 de avellana", "que es dos de avellana", when they contain:
  "que es" + number + "de" + flavor_name
  MUST be treated as "quedan 2 de avellana" → INVENTORY.

When you detect these ASR variants, you MUST choose intent = "INVENTORY",
NOT "SALES".

### Pattern Rules

#### PRODUCTION (making)
Examples to extract (multiple allowed):
- "{{quantity}} galletas de {{flavor}} en la {{mezcladora|batidora|blender}} {{number}}"
  - Variants: "galletas"/"galleta", "de"/"del"/"sabor", "mezcladora"/"batidora"/"blender".
  - Each batch must be a separate element in "batches".

#### INVENTORY (stock)
Examples:
- "quedan {{quantity}} de {{flavor}}"
- "hay {{quantity}} galletas de {{flavor}}"
- "inventario: {{flavor}} {{quantity}}"
- "se enviaron {{quantity}} de {{flavor}} a {{lugar}}"
- "recibimos {{quantity}} de {{flavor}}"
- "llegaron {{quantity}} de {{flavor}}"

Each flavor becomes one item in "items".

#### SALES (ventas)
Examples:
- "vendí {{quantity}} de {{flavor}}"
- "vendimos {{quantity}} galletas de {{flavor}}"
- "se vendieron {{quantity}} {{flavor}}"

Also, VERY IMPORTANT, there can be lists and multiple product families:

- "Vendimos de {{flavor1}} 1, {{flavor1}} 3, {{flavor1}} 6, ..."
- "y de {{flavor2}} vendimos 1 {{flavor2}}, 5 {{flavor2}}, 6 {{flavor2}}, ..."

You MUST:

- Extract ALL quantities for EACH flavor that appears.
- NOT stop after SMORES if later the transcript continues with MINIS or any other flavor.
- Go through the ENTIRE transcript from start to end and process every numeric pattern that matches a product.

### COMPOSITE SALES PRODUCT: "CRUKY" / "CROOKIES" / "CROAZAN" (VERY IMPORTANT)

Sometimes the transcript will mention a composite product such as:

- "cruky", "crukis", "crookies", "croazan", or similar minor spelling variants.

For the **SALES** intent, these words refer to a combo product that is actually made of several catalog products.
When this happens:

1. You MUST **NOT** create an item with flavor_name "CRUKY", "CROOKIES", "CROAZAN", etc.
2. Instead, for **each 1 unit** of this composite product, you MUST create the following component sales:

   - 1 unit of flavor_name "MASA 40"
   - 1 unit of flavor_name "MASA 75"
   - 1 unit of flavor_name "PANES (C)"

   (Use the exact catalog flavor names and their corresponding sku from the catalog_block.)

3. The quantity for each component is the same as the quantity mentioned for the composite product.

Example:

Transcript:
"vendimos 3 crukis"

Desired items in "items":

- 1 item: {{"flavor_name": "MASA 40", "sku": "<sku for MASA 40>", "quantity": 3, "unit": "UNITS"}}
- 1 item: {{"flavor_name": "MASA 75", "sku": "<sku for MASA 75>", "quantity": 3, "unit": "UNITS"}}
- 1 item: {{"flavor_name": "PANES (C)", "sku": "<sku for PANES (C)>", "quantity": 3, "unit": "UNITS"}}

You MUST NEVER output an item whose flavor_name is "CRUKY", "CROOKIES", "CROAZAN" or similar. Always expand it into the 3 component flavors above, only for SALES.

### COMPLEX SALES EXAMPLE (THIS IS CRUCIAL)

Transcript:
\"\"\"  
Bueno y vendimos un monton, vendimos de smores 1, smores 3, smores 6, smores 9, smores 8, smores 7, smores 5, smores 6, smores 2, smores 9, smores 5, smores 4, smores 1, smores 22  
y de minis vendimos 1 mini, 2 minis, 5 minis, 6 minis, 9 minis, 14 minis, 23 minis, 4 minis, 5 minis y 1 mini  
\"\"\"  

Desired JSON (simplified):

{{
  "intent": "SALES",
  "location": "{default_location}",
  "items": [
    {{"flavor_name": "SMORES", "sku": "GI-SM", "quantity": 1, "unit": "UNITS"}},
    {{"flavor_name": "SMORES", "sku": "GI-SM", "quantity": 3, "unit": "UNITS"}},
    {{... all remaining SMORES quantities ...}},
    {{"flavor_name": "MINIS", "sku": "MIN", "quantity": 1, "unit": "UNITS"}},
    {{"flavor_name": "MINIS", "sku": "MIN", "quantity": 2, "unit": "UNITS"}},
    {{... all remaining MINIS quantities ...}},
    {{"flavor_name": "MINIS", "sku": "MIN", "quantity": 1, "unit": "UNITS"}}
  ]
}}

IMPORTANT:
- Do NOT ignore the second product family ("MINIS").
- Every time you see "minis" you must normalize the flavor to the catalog name, for example "MINIS" → "MINI", and use its sku from the catalog (for example sku "MIN" if present).
- Sales ALWAYS go to the "items" array with fields: flavor_name, sku, quantity, unit.
- If a flavor appears multiple times (e.g. many ALFAJOR mentions), each mention with its own quantity becomes a separate item, even if the flavor is repeated.

### Intent Decision (MUST)

- If the transcript is about making products (hice, se hicieron, batidora, mezcladora) → "PRODUCTION".

- If the transcript is about stock remaining or inventory movements 
  (quedan, queda, hay, hay en inventario, stock, inventario, recibimos, se enviaron, llegaron, mandamos desde fábrica),
  OR if the transcript contains ONLY patterns like "<number> de <flavor>" but NO clear selling verbs,
  you MUST choose intent = "INVENTORY".

- You MUST choose "SALES" ONLY when the transcript clearly talks about selling
  using words like: "vendí", "vendimos", "se vendieron", "venta", "ventas".
  Without these selling words, YOU MUST NOT choose "SALES".

- If both production and sales appear mixed, choose the INTENT that best matches
  the majority of numeric patterns AND the verbs used.

### Output Format (STRICT)

#### PRODUCTION Output:
{{
  "intent": "PRODUCTION",
  "date": "{today_iso}",
  "batches": [
    {{
      "blender": "1",
      "flavor_name": "ALFAJOR",
      "sku": "GI-AL",
      "quantity": 26
    }}
  ]
}}

#### INVENTORY Output:
{{
  "intent": "INVENTORY",
  "location": "{default_location}",
  "items": [
    {{
      "flavor_name": "ALFAJOR",
      "sku": "GI-AL",
      "quantity": 12
    }}
  ]
}}

#### SALES Output:
{{
  "intent": "SALES",
  "location": "{default_location}",
  "items": [
    {{
      "flavor_name": "ALFAJOR",
      "sku": "GI-AL",
      "quantity": 2,
      "unit": "UNITS"
    }}
  ]
}}

### Normalization

- Normalize flavor_name to one of the catalog keys (best-effort), e.g. "limón" → "PIE DE LIMON", "snikers" → "SNICKERS", "smurfs" → "SMORES", "clean" → "LECHE KLIM", "minis" → "MINI", "for you" → "NUTS FOR YOU".
- For the composite product words ("cruky", "crukis", "crookies", "croazan", etc.), normalize them to the composite rule described above and DO NOT use them directly as flavor_name.
- If no close match exists in the catalog, leave sku = "" but still create the item.
- `quantity` must be integer ≥ 1.
- For PRODUCTION:
  - `blender` is the number after "mezcladora"/"batidora"/"blender". If missing, set "".
- For SALES:
  - Always set `unit` = "UNITS".

Before writing the final JSON, internally identify every numeric pattern and its associated flavor text to ensure none are lost. Then output ONLY the final JSON.

### Transcript to parse:
\"\"\"{transcript}\"\"\"  

Return ONLY the JSON. No explanation.
""".strip()


def _chat_json(model: str, system_prompt: str) -> dict:
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    # 1) Intento con response_format (SDKs nuevos)
    try:
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0,
            timeout=45
        )
        content = resp.choices[0].message.content
        return json.loads(content)
    except Exception as e1:
        # 2) Fallback sin response_format
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0
        )
        content = (resp.choices[0].message.content or "").strip()
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end != -1 and end >= start:
            content = content[start:end+1]
        return json.loads(content)

def _fallback_rule_based(transcript: str, default_location: str):
    # Mini-fallback: separa por comas/saltos de línea y usa (sku|nombre)+cantidad si hay dígitos
    chunks = re.split(r"[,\n;·•\-–—]", transcript or "")
    items = []
    for c in chunks:
        c2 = c.strip()
        if not c2: continue
        # busca último entero
        nums = re.findall(r"\d+", c2)
        if not nums: continue
        qty = int(nums[-1])
        # nombre aproximado = palabras no numéricas
        name = re.sub(r"\d+", "", c2).strip().upper()
        if qty > 0 and name:
            items.append({"flavor_name": name, "sku": "", "quantity": qty})
    if items:
        return {"intent": "INVENTORY", "location": default_location, "items": items}
    # si nada, devolver producción vacía
    today_iso = dt.date.today().isoformat()
    return {"intent": "PRODUCTION", "date": today_iso, "batches": []}

def run_parser(transcript: str, attempt_hint=None, flavor_by_name: dict=None, default_location: str=None):
    """
    Devuelve:
      INVENTORY: {"intent":"INVENTORY","location": "...","items":[{"flavor_name","sku","quantity"}, ...]}
      PRODUCTION: {"intent":"PRODUCTION","date":"YYYY-MM-DD","batches":[{"blender","flavor_name","sku","quantity"}, ...]}
    """
    default_location = default_location or os.getenv("LOCATION_CODE", "MALLPLAZA-BOG")
    model = os.getenv("GPT_MODEL", "gpt-4o-mini")
    today_iso = dt.date.today().isoformat()

    # --- NUEVO: catálogo embebido por defecto ---
    if not flavor_by_name:
        flavor_by_name = _load_embedded_catalog()

    if not transcript or not transcript.strip():
        return {"intent":"INVENTORY","location":default_location,"items":[]}, {"parser":"gpt-adapter","attempt":attempt_hint,"note":"empty transcript"}

    if not os.getenv("OPENAI_API_KEY"):
        data = _fallback_rule_based(transcript, default_location)
        return data, {"parser":"rule-fallback","attempt":attempt_hint}

    prompt = _build_parser_prompt(transcript, flavor_by_name or {}, today_iso, default_location)

    try:
        data = _chat_json(model, prompt)

        if data.get("intent") == "INVENTORY":
            for it in data.get("items", []):
                it["quantity"]    = int(it.get("quantity") or 0)
                it["flavor_name"] = (it.get("flavor_name") or "").upper()
                it["sku"]         = it.get("sku") or ""

        elif data.get("intent") == "PRODUCTION":
            for b in data.get("batches", []):
                b["quantity"]    = int(b.get("quantity") or 0)
                b["flavor_name"] = (b.get("flavor_name") or "").upper()
                b["sku"]         = b.get("sku") or ""
                b["blender"]     = str(b.get("blender") or "")
            if "date" not in data or not data["date"]:
                data["date"] = today_iso

        elif data.get("intent") == "SALES":             
            for it in data.get("items", []):
                it["quantity"]    = int(it.get("quantity") or 0)
                it["flavor_name"] = (it.get("flavor_name") or "").upper()
                it["sku"]         = it.get("sku") or ""
                it["unit"]        = it.get("unit") or "UNITS"
            # opcional: si quieres forzar location:
            if "location" not in data or not data["location"]:
                data["location"] = default_location

        else:
            data = {"intent":"INVENTORY","location":default_location,"items":[]}


        return data, {"parser":"gpt-adapter","attempt":attempt_hint}
    except Exception as e:
        fb = _fallback_rule_based(transcript, default_location)
        return fb, {"parser":"rule-fallback","attempt":attempt_hint,"error":str(e)}

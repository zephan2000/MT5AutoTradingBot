# /mnt/data/llm_normalize.py
import os, json, re, requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ==== OpenRouter config ====
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OR_TOKEN = os.getenv("OR_TOKEN")  # <- put your OpenRouter key in .env as OR_TOKEN=...
MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct-v0.3")  # default to the free tier

# (Optional but recommended for OpenRouter etiquette/analytics)
OR_REFERER = os.getenv("OR_REFERER", "http://localhost")
OR_TITLE   = os.getenv("OR_TITLE",   "Signal Normalizer")

SYSTEM = """You are "SignalNormalizer", a deterministic converter that outputs ONLY one JSON object following this schema:
{"symbol": "string|null","side": "LONG|SHORT|null","entry": [number,number?]|null,"targets": [number]|null,"stop": number|null,"timeframe":"string|null","confidence": number,"issues": [string]|null,"raw_text": "string","source":{"platform":"telegram","group_id":"string|null","message_id":"string|null","received_ts":"string|null"},"idempotency_key":"string"}
Rules:
- Do not invent. Use null for unknown.
- UPPERCASE symbol. Remove leading '#'.
- Return JSON only, no markdown or prose.
"""

def _headers():
    if not OR_TOKEN:
        return None
    h = {
        "Authorization": f"Bearer {OR_TOKEN}",
        "Content-Type": "application/json",
        "HTTP-Referer": OR_REFERER,
        "X-Title": OR_TITLE,
    }
    return h

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def normalize_message(raw_text: str, group_id=None, message_id=None):
    """
    Normalize a messy trade signal string into strict JSON via OpenRouter (gpt-oss-20b:free).
    Falls back to a lightweight dict if no token is configured or on failure.
    """
    # Fallback (no key or any failure)
    fallback = {
        "symbol": None,
        "side": None,
        "entry": None,
        "targets": [],
        "stop": None,
        "timeframe": None,
        "confidence": 0,
        "issues": ["no_api_key"] if not OR_TOKEN else [],
        "raw_text": raw_text,
        "source": {
            "platform": "telegram",
            "group_id": str(group_id) if group_id is not None else None,
            "message_id": str(message_id) if message_id is not None else None,
            "received_ts": _now_iso(),
        },
        "idempotency_key": f"{group_id or 'na'}:{message_id or 'na'}:{abs(hash(raw_text))%10**8}",
    }

    if not OR_TOKEN:
        return fallback

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"{raw_text}\n\nReturn only the JSON object."}
    ]

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{raw_text}\n\nReturn only valid JSON matching the schema above."}
        ],
        "temperature": 0.0,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},  # enforce clean JSON
    }

    try:
        resp = requests.post(OPENROUTER_URL, headers=_headers(), data=json.dumps(payload), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print("=== RAW RESPONSE ===")
        print(json.dumps(data, indent=2))

         # response_format=json_object guarantees JSON in choices[0].message.content
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(content)   # <-- direct load, no regex needed

        # Post-fix: add missing fields
        src = parsed.get("source") or {}
        src.setdefault("platform", "telegram")
        src.setdefault("group_id", str(group_id) if group_id else None)
        src.setdefault("message_id", str(message_id) if message_id else None)
        src.setdefault("received_ts", _now_iso())
        parsed["source"] = src

        parsed.setdefault("raw_text", raw_text)
        parsed.setdefault(
            "idempotency_key",
            f"{group_id or 'na'}:{message_id or 'na'}:{abs(hash(raw_text))%10**8}",
        )

        if isinstance(parsed.get("symbol"), str):
            sym = parsed["symbol"].lstrip("#").upper()
            parsed["symbol"] = sym if sym else None

        return parsed

    except Exception as e:
        print("[ERR] normalize_message failed:", e)
        fallback["issues"].append(f"exception:{type(e).__name__}")
        return fallback

# if __name__ == "__main__":
#     # Quick smoke test
#     sample = """📩 #WLDUSDT 30m | Mid-Term
#     📉 Long Entry Zone: 1.1045-1.0413
#     Target 1: 1.1343
#     Target 2: 1.1641
#     Target 3: 1.1940
#     Target 4: 1.2834
#     ❌Stop-Loss: 1.0132
#     """

#     print(json.dumps(normalize_message(sample, group_id="12345", message_id="67890"), indent=2))

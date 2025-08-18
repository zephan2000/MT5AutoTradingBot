import re
from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel, field_validator
import spacy
from spacy.matcher import Matcher

# Lightweight, rule-based spaCy (no external model required)
nlp = spacy.blank("en")
matcher = Matcher(nlp.vocab)

# Basic number token rule
_NUM = {"TEXT": {"REGEX": r"^[0-9]+(?:[.,][0-9]+)?$"}}
_DASH = {"TEXT": {"REGEX": r"[-–—]"}}

# Side
matcher.add("SIDE", [[{"LOWER": {"IN": ["long", "short", "buy", "sell"]}}]])
# Entry range / single
matcher.add("ENTRY_RANGE", [[{"LOWER": {"IN": ["entry", "entryzone", "entry_zone", "entry-area", "entryrange"]}},
                              {"IS_PUNCT": True, "OP": "?"},
                              _NUM, _DASH, _NUM]])
matcher.add("ENTRY_SINGLE", [[{"LOWER": "entry"}, {"IS_PUNCT": True, "OP": "?"}, _NUM]])
# Targets / Take / TP
matcher.add("TARGET", [[{"LOWER": {"IN": ["target", "tp", "take"]}}, {"IS_DIGIT": True, "OP": "?"}, {"IS_PUNCT": True, "OP": "?"}, _NUM]])
# Stop / SL
matcher.add("STOP", [[{"LOWER": {"IN": ["stop", "sl", "stoploss", "stop-loss"]}}, {"IS_PUNCT": True, "OP": "?"}, _NUM]])

class TradeSignal(BaseModel):
    symbol: str
    side: str  # LONG/SHORT
    entry: Tuple[float, Optional[float]]
    targets: List[float] = []
    stop: Optional[float] = None
    timeframe: Optional[str] = None
    source: Optional[Dict[str, Any]] = None

    @field_validator("side", mode="before")
    @classmethod
    def _norm_side(cls, v):
        v = (v or "").upper()
        return "LONG" if v in {"LONG", "BUY"} else ("SHORT" if v in {"SHORT", "SELL"} else v)

def _f(x: str) -> float:
    return float(x.replace(",", "").strip())

def parse_trade_signal(raw_text: str, hints: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Final deterministic structuring. Accepts optional LLM hints:
      hints = {symbol, side, entry:[low,high?], targets:[], stop, timeframe}
    Returns dict compatible with your current DB usage:
      {"action": "buy|sell", "symbol": "WLDUSDT", "entry_min": 1.01, "entry_max": 1.04, "sl": 1.0, "tp": [..]}
    """
    hints = hints or {}
    text_norm = raw_text.replace("–", "-").replace("—", "-")
    doc = nlp(text_norm)
    upper = text_norm.upper()

    # Symbol heuristic (prefer hints)
    symbol = (hints.get("symbol") or "").upper() or _extract_symbol(upper)

    # Timeframe (prefer hints)
    timeframe = (hints.get("timeframe") or None)
    # Side
    side = hints.get("side") or _first_match(doc, "SIDE")
    # Entry
    entry = hints.get("entry")
    entry_tuple: Tuple[Optional[float], Optional[float]] = (None, None)

    matches = matcher(doc)
    targets: List[float] = list(hints.get("targets") or [])
    stop = hints.get("stop")

    if not entry:
        name = None
        for mid, s, e in matches:
            name = nlp.vocab.strings[mid]
            span = doc[s:e]
            if name == "ENTRY_RANGE" and entry_tuple == (None, None):
                nums = [t.text for t in span if re.match(r"^[0-9]+(?:[.,][0-9]+)?$", t.text)]
                if len(nums) >= 2:
                    a, b = _f(nums[0]), _f(nums[1])
                    entry_tuple = (min(a, b), max(a, b))
            elif name == "ENTRY_SINGLE" and entry_tuple == (None, None):
                nums = [t.text for t in span if re.match(r"^[0-9]+(?:[.,][0-9]+)?$", t.text)]
                if nums:
                    p = _f(nums[0])
                    entry_tuple = (p, p)
    else:
        # Hints entry array → tuple
        if len(entry) == 1:
            entry_tuple = (float(entry[0]), float(entry[0]))
        elif len(entry) >= 2:
            a, b = float(entry[0]), float(entry[1])
            entry_tuple = (min(a, b), max(a, b))

    # Targets/Stop from text if not in hints
    if not targets:
        for mid, s, e in matches:
            if nlp.vocab.strings[mid] == "TARGET":
                nums = [t.text for t in doc[s:e] if re.match(r"^[0-9]+(?:[.,][0-9]+)?$", t.text)]
                if nums:
                    targets.append(_f(nums[-1]))
    if stop is None:
        for mid, s, e in matches:
            if nlp.vocab.strings[mid] == "STOP":
                nums = [t.text for t in doc[s:e] if re.match(r"^[0-9]+(?:[.,][0-9]+)?$", t.text)]
                if nums:
                    stop = _f(nums[-1])

    # Validation (required)
    if not (symbol and side and entry_tuple[0] is not None):
        return None
    # Sanity checks
    if any(x is not None and x <= 0 for x in [entry_tuple[0], entry_tuple[1] or entry_tuple[0], stop or 1]):
        return None

    action = "buy" if side.upper() in {"LONG", "BUY"} else "sell"
    return {
        "action": action,
        "symbol": symbol,
        "entry_min": entry_tuple[0],
        "entry_max": entry_tuple[1] if entry_tuple[1] is not None else entry_tuple[0],
        "sl": stop,
        "tp": targets,
        "timeframe": timeframe
    }

def _extract_symbol(upper_text: str) -> Optional[str]:
    m = re.search(r"#?([A-Z]{3,12}(?:USDT|USD|JPY|BTC|ETH)?)", upper_text)
    return m.group(1).upper() if m else None

def _first_match(doc, name: str) -> Optional[str]:
    for mid, s, e in matcher(doc):
        if nlp.vocab.strings[mid] == name:
            return doc[s:e].text
    return None


# Old Parsing Tech
# def parse_trade_signal(raw_text: str) -> dict:
#     text = raw_text.upper()
#     lines = [re.sub(r"[^\w.\s:-]", "", l).strip() for l in text.splitlines() if l.strip()]
#     result = {"action": None, "symbol": None, "entry_min": None, "entry_max": None, "sl": None, "tp": []}
#     for line in lines:
#         m = re.match(r"(BUY|SELL)\s+([A-Z0-9/_-]{3,12})(?:\s+([\d.]+))?", line)
#         if m:
#             action, symbol, entry = m.groups()
#             result["action"], result["symbol"] = action.lower(), symbol.upper()
#             if entry: result["entry_min"] = result["entry_max"] = float(entry)
#             continue
#         if line.startswith("ENTRY"):
#             nums = re.findall(r"[\d.]+", line)
#             if len(nums)==1: result["entry_min"]=result["entry_max"]=float(nums[0])
#             elif len(nums)>=2:
#                 e1,e2 = map(float, nums[:2]); result["entry_min"],result["entry_max"]=sorted((e1,e2))
#             continue
#         if "SL" in line:
#             nums = re.findall(r"[\d.]+", line)
#             if nums: result["sl"] = float(nums[0]); continue
#         if "TP" in line:
#             nums = re.findall(r"[\d.]+", line)
#             if nums: result["tp"].append(float(nums[0])); continue
#     return result
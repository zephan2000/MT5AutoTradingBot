import re
import spacy
nlp = spacy.load("en_core_web_sm")

def parse_trade_signal(raw_text: str) -> dict:
    text = raw_text.upper()
    lines = [re.sub(r"[^\w.\s:-]", "", l).strip() for l in text.splitlines() if l.strip()]
    result = {"action": None, "symbol": None, "entry_min": None, "entry_max": None, "sl": None, "tp": []}
    for line in lines:
        m = re.match(r"(BUY|SELL)\s+([A-Z0-9/_-]{3,12})(?:\s+([\d.]+))?", line)
        if m:
            action, symbol, entry = m.groups()
            result["action"], result["symbol"] = action.lower(), symbol.upper()
            if entry: result["entry_min"] = result["entry_max"] = float(entry)
            continue
        if line.startswith("ENTRY"):
            nums = re.findall(r"[\d.]+", line)
            if len(nums)==1: result["entry_min"]=result["entry_max"]=float(nums[0])
            elif len(nums)>=2:
                e1,e2 = map(float, nums[:2]); result["entry_min"],result["entry_max"]=sorted((e1,e2))
            continue
        if "SL" in line:
            nums = re.findall(r"[\d.]+", line)
            if nums: result["sl"] = float(nums[0]); continue
        if "TP" in line:
            nums = re.findall(r"[\d.]+", line)
            if nums: result["tp"].append(float(nums[0])); continue
    return result

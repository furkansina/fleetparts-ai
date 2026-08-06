import json
import threading
from datetime import date

USAGE_FILE = "token_usage.json"
DAILY_TOKEN_BUDGET = 200000  # Groq ücretsiz kotanın günlük token sınırı (tahmini referans)

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "date" in data:
                return data
    except Exception:
        pass
    return {"date": str(date.today()), "total_tokens": 0, "call_count": 0}


def _save(data: dict):
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_usage(total_tokens: int):
    """Her Groq çağrısından sonra çağrılır; gün değiştiyse sayaç otomatik sıfırlanır."""
    if not total_tokens:
        return
    with _lock:
        data = _load()
        today = str(date.today())
        if data.get("date") != today:
            data = {"date": today, "total_tokens": 0, "call_count": 0}
        data["total_tokens"] += total_tokens
        data["call_count"] += 1
        _save(data)


def get_today_usage() -> dict:
    with _lock:
        data = _load()
        today = str(date.today())
        if data.get("date") != today:
            data = {"date": today, "total_tokens": 0, "call_count": 0}
        data["budget"] = DAILY_TOKEN_BUDGET
        data["remaining_estimate"] = max(0, DAILY_TOKEN_BUDGET - data["total_tokens"])
        data["percent_used"] = round(100 * data["total_tokens"] / DAILY_TOKEN_BUDGET, 1)
        return data

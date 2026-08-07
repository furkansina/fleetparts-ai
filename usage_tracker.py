import os
import json
import time
import base64
import threading
from datetime import date

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

USAGE_FILE = "token_usage.json"
DAILY_TOKEN_BUDGET = 200000  # Groq ücretsiz kotanın günlük token sınırı (tahmini referans)

_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0}
CACHE_TTL = 15  # saniye - sık çağrıldığı için (her Groq isteğinden sonra) çok kısa tutuldu


def _load_from_disk() -> dict:
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "date" in data:
                return data
    except Exception:
        pass
    return {"date": str(date.today()), "total_tokens": 0, "call_count": 0}


def _load_live() -> dict:
    """Render'ın diski her yeniden deploy'da sıfırlandığı için (kod güncellemesi = sayaç kaybı)
    kullanım verisi GitHub'dan canlı okunur - catalog.json ile aynı desen. Kısa önbellek,
    her Groq çağrısında GitHub'ı gereksiz yormasın diye."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache["data"]

    if GITHUB_REPO:
        try:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{USAGE_FILE}"
            res = requests.get(url, timeout=8)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict) and "date" in data:
                    _cache["data"] = data
                    _cache["fetched_at"] = now
                    return data
        except Exception:
            pass

    return _load_from_disk()


def _save(data: dict):
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _sync_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{USAGE_FILE}"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        sha = None
        res = requests.get(api_url, headers=headers, timeout=10)
        if res.status_code == 200:
            sha = res.json().get("sha")
        with open(USAGE_FILE, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        body = {"message": "Token kullanım sayacı güncelleme", "content": content_b64}
        if sha:
            body["sha"] = sha
        requests.put(api_url, headers=headers, json=body, timeout=10)
    except Exception:
        pass


def record_usage(total_tokens: int):
    """Her Groq çağrısından sonra çağrılır; gün değiştiyse sayaç otomatik sıfırlanır.
    En güncel hali GitHub'dan okuyup üzerine ekler ve tekrar GitHub'a yazar - böylece Render
    bir kod güncellemesiyle yeniden başlasa bile bugünkü toplam kullanım kaybolmaz."""
    if not total_tokens:
        return
    with _lock:
        data = _load_live()
        today = str(date.today())
        if data.get("date") != today:
            data = {"date": today, "total_tokens": 0, "call_count": 0}
        data["total_tokens"] += total_tokens
        data["call_count"] += 1
        _save(data)
        _cache["data"] = data
        _cache["fetched_at"] = time.time()
        _sync_to_github()


def get_today_usage() -> dict:
    with _lock:
        data = _load_live()
        today = str(date.today())
        if data.get("date") != today:
            data = {"date": today, "total_tokens": 0, "call_count": 0}
        data["budget"] = DAILY_TOKEN_BUDGET
        data["remaining_estimate"] = max(0, DAILY_TOKEN_BUDGET - data["total_tokens"])
        data["percent_used"] = round(100 * data["total_tokens"] / DAILY_TOKEN_BUDGET, 1)
        return data

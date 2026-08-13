import os
import json
import time
import uuid
import base64
import threading
import requests
from datetime import datetime, timezone

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

MISSED_SEARCH_FILE = "missed_searches.json"
MAX_ENTRIES = 3000  # dosyanın sınırsız büyümesini önlemek için en eski kayıtlar budanır

_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0}
_CACHE_TTL = 30  # saniye

# lead_store.py/outreach.py ile aynı desen (2026-08-13'te eklendi) - bu dosyanın GitHub'a
# yedeklenmesi bozulursa /usage üzerinden görünür olsun diye.
_github_sync_status = {"last_error": None, "last_success_at": None, "consecutive_failures": 0}


def _load_from_disk() -> list:
    try:
        with open(MISSED_SEARCH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def load_missed_searches() -> list:
    """Katalogda karşılığı bulunamayan aramaların kaydını döndürür - catalog.json ile aynı
    desen: önce GitHub'dan kısa önbellekle canlı okumayı dener, ulaşılamazsa diskteki son
    bilinen hale döner."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL:
        return _cache["data"]
    if GITHUB_REPO:
        try:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{MISSED_SEARCH_FILE}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    _cache["data"] = data
                    _cache["fetched_at"] = now
                    return data
        except Exception:
            pass
    return _load_from_disk()


def _save_to_disk(items: list):
    with open(MISSED_SEARCH_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _sync_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        _github_sync_status["last_error"] = "GITHUB_TOKEN veya GITHUB_REPO tanımlı değil - yedekleme hiç aktif değil."
        _github_sync_status["consecutive_failures"] += 1
        return
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{MISSED_SEARCH_FILE}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    with open(MISSED_SEARCH_FILE, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")
    for attempt in range(3):
        try:
            sha = None
            res = requests.get(api_url, headers=headers, timeout=15)
            if res.status_code == 200:
                sha = res.json().get("sha")
            body = {"message": "Kaçırılan arama kaydı güncelleme", "content": content_b64}
            if sha:
                body["sha"] = sha
            put_res = requests.put(api_url, headers=headers, json=body, timeout=15)
            if put_res.status_code in (200, 201):
                _github_sync_status["last_success_at"] = time.time()
                _github_sync_status["last_error"] = None
                _github_sync_status["consecutive_failures"] = 0
                return
            if put_res.status_code == 409 and attempt < 2:
                continue  # başka bir süreç araya girdi - sha'yı yeniden al ve tekrar dene
            _github_sync_status["last_error"] = f"HTTP {put_res.status_code}: {put_res.text[:200]}"
            _github_sync_status["consecutive_failures"] += 1
            return
        except Exception as e:
            print(f"  [missed_search_log] GitHub senkronizasyon hatasi (deneme {attempt+1}/3): {e}")


def log_missed_search(search_type: str, query_text: str, category: str = ""):
    """Katalogda karşılığı bulunamayan bir aramayı kaydeder - müşteri kimliği/telefonu HİÇ
    tutulmaz, sadece ne arandığı (metin/kategori) ve ne zaman. Amaç: 'gerçekten talep var ama
    stoğumuzda olmayan ürünler' sorusuna veriyle cevap vermek - önceden bu bilgi (NOT_IN_CATALOG
    sonuçları) hiçbir yere kaydedilmiyor, anında kayboluyordu."""
    query_text = (query_text or "").strip()
    if not query_text:
        return
    entry = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "search_type": search_type,  # "photo" | "text"
        "query_text": query_text[:300],
        "category": (category or "")[:150],
    }
    with _lock:
        items = load_missed_searches() + [entry]
        if len(items) > MAX_ENTRIES:
            items = items[-MAX_ENTRIES:]
        _save_to_disk(items)
        _cache["data"] = items
        _cache["fetched_at"] = time.time()
    _sync_to_github()

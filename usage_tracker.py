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
DAILY_TOKEN_BUDGET = 200000  # Groq ücretsiz kotanın (hesap başına) günlük token sınırı

_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0}
CACHE_TTL = 15  # saniye - sık çağrıldığı için (her Groq isteğinden sonra) çok kısa tutuldu

POOLS = ("customer", "bulk", "customer2")

# Groq'un HER yanıtında (200 de olsa hata da olsa) döndürdüğü x-ratelimit-* header'larının en son
# görülen hali, havuz başına bellekte tutulur. Bizim kendi saydığımız günlük bütçe TAHMİNİ
# (DAILY_TOKEN_BUDGET, aşağıda) Groq'un gerçek sınırıyla uyuşmayabiliyor - gerçek bir kullanımda
# tespit edildi: biz "%17 kullanıldı" diyorduk, Groq aynı anda "kota bitti" diyordu. Süreç
# yeniden başladığında bu önbellek sıfırlanır ama bir sonraki gerçek Groq çağrısıyla hemen
# kendini tazeler - kalıcı depolamaya (disk/GitHub) yazmaya değecek kritik bir veri değil.
_rate_limit_cache = {}


def record_rate_limit_headers(pool: str, headers) -> None:
    snapshot = {k: v for k, v in headers.items() if k.lower().startswith("x-ratelimit") or k.lower() == "retry-after"}
    if snapshot:
        _rate_limit_cache[pool] = snapshot


def get_rate_limit_snapshot(pool: str) -> dict:
    return dict(_rate_limit_cache.get(pool, {}))


def get_real_remaining_tokens(pool: str):
    """Varsa Groq'un en son bildirdiği GERÇEK kalan token sayısını döndürür (bizim tahminimiz
    değil). Tam header adı garanti olmadığı için 'remaining' ve 'token' geçen herhangi bir
    x-ratelimit header'ı esnek şekilde aranır; bulunamazsa None döner (çağıran o zaman kendi
    tahminine döner)."""
    for key, value in _rate_limit_cache.get(pool, {}).items():
        key_l = key.lower()
        if "remaining" in key_l and "token" in key_l:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _empty_day() -> dict:
    return {
        "date": str(date.today()),
        "customer": {"total_tokens": 0, "call_count": 0},
        "bulk": {"total_tokens": 0, "call_count": 0},
        "customer2": {"total_tokens": 0, "call_count": 0},
    }


def _is_valid_shape(data) -> bool:
    # "customer2" eski kayıtlarda (3. hesap eklenmeden önce) yok olabilir - eksikse eksik
    # sayılmaz, get_today_usage/record_usage aşağıda .setdefault ile tamamlar.
    return isinstance(data, dict) and "date" in data and "customer" in data and "bulk" in data


def _load_from_disk() -> dict:
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if _is_valid_shape(data):
                return data
    except Exception:
        pass
    return _empty_day()


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
                if _is_valid_shape(data):
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


def record_usage(total_tokens: int, pool: str = "customer"):
    """Her Groq çağrısından sonra çağrılır; gün değiştiyse sayaç otomatik sıfırlanır.
    `pool`: "customer" (müşteri arama) veya "bulk" (katalog tarama gibi toplu işler) - iki ayrı
    Groq hesabının kullanımı karışmasın diye ayrı ayrı takip edilir. En güncel hali GitHub'dan
    okuyup üzerine ekler ve tekrar GitHub'a yazar - böylece Render bir kod güncellemesiyle
    yeniden başlasa bile bugünkü toplam kullanım kaybolmaz."""
    if not total_tokens:
        return
    if pool not in POOLS:
        pool = "customer"
    with _lock:
        data = _load_live()
        today = str(date.today())
        if data.get("date") != today:
            data = _empty_day()
        data.setdefault(pool, {"total_tokens": 0, "call_count": 0})
        data[pool]["total_tokens"] += total_tokens
        data[pool]["call_count"] += 1
        _save(data)
        _cache["data"] = data
        _cache["fetched_at"] = time.time()
        _sync_to_github()


def get_today_usage() -> dict:
    """İki havuzun (customer/bulk) bugünkü kullanımını ayrı ayrı döndürür. Geriye dönük uyumluluk
    için üst seviyede de (henüz güncellenmemiş bir istemci varsa) müşteri havuzunun değerleri
    düz alanlar olarak tekrarlanır."""
    with _lock:
        data = _load_live()
        today = str(date.today())
        if data.get("date") != today:
            data = _empty_day()

        result = {"date": data["date"], "budget": DAILY_TOKEN_BUDGET}
        for pool in POOLS:
            p = data.setdefault(pool, {"total_tokens": 0, "call_count": 0})
            result[pool] = {
                "total_tokens": p["total_tokens"],
                "call_count": p["call_count"],
                "budget": DAILY_TOKEN_BUDGET,
                "remaining_estimate": max(0, DAILY_TOKEN_BUDGET - p["total_tokens"]),
                "percent_used": round(100 * p["total_tokens"] / DAILY_TOKEN_BUDGET, 1),
            }

        result["total_tokens"] = result["customer"]["total_tokens"]
        result["call_count"] = result["customer"]["call_count"]
        result["remaining_estimate"] = result["customer"]["remaining_estimate"]
        result["percent_used"] = result["customer"]["percent_used"]
        return result

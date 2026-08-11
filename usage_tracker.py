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
# Groq'un console.groq.com/settings/limits panelinden DOĞRULANDI (2026-08-10, qwen/qwen3.6-27b):
# 30 istek/dk, 1000 istek/gün, 8000 token/dk, 200.000 token/gün. Token sınırı zaten doğru
# tahmin edilmişti; asıl GÖZDEN KAÇAN sınır GÜNLÜK İSTEK SAYISIYDI (aşağıda DAILY_REQUEST_BUDGET) -
# "kota bitti" hatalarının bir kısmının aslında token değil istek sınırından kaynaklanmış
# olabileceği ihtimali bunun eklenmesiyle artık takip edilebiliyor.
DAILY_TOKEN_BUDGET = 200000    # Groq ücretsiz kotanın (hesap başına) günlük TOKEN sınırı (TPD) - qwen/qwen3.6-27b havuzları
DAILY_REQUEST_BUDGET = 1000    # Groq ücretsiz kotanın (hesap başına) günlük İSTEK sayısı sınırı (RPD) - qwen/qwen3.6-27b havuzları

# BUG (2026-08-11 tespit edildi): lead netleştirme (classify-ambiguous / classify-for-product)
# llama-3.3-70b-versatile modelini kullanıyor - bu, Groq'ta MÜŞTERİ arama akışının kullandığı
# qwen modelinden TAMAMEN AYRI bir kota/limit setine sahip (ayrı model = ayrı sunucu taraflı
# limit). Ama önceden bu çağrılar kullanım takibinde "customer" havuzuna yazılıyordu (aynı ana
# hesap/API anahtarı kullanıldığı için) - bu da GERÇEKTE ayrı olan llama kotasının, MÜŞTERİ
# arama havuzunun bizim kendi TAHMİNİ bütçesini (200K token / 1000 istek) tüketiyormuş GİBİ
# görünmesine yol açıyordu. Sonucu: baba büyük bir lead netleştirme partisi çalıştırdığında,
# call_groq_api'deki ön-kontrol (bkz. app.py "requests_remaining_estimate < 3 ise atla") GERÇEK
# müşteri aramalarını -gerçekte hiç dokunulmamış bir kotayla- "neredeyse bitti" sanıp anlıksız
# olarak atlayabiliyordu; ayrıca herkese açık /usage sayacı müşteri havuzunu yanlış yüksek
# gösteriyordu. Çözüm: llama/lead-netleştirme çağrıları artık kendi ayrı "leads_ai" havuzunda
# izleniyor (bkz. app.py _resolve_key_chain) - gerçek Groq hesap/kimlik bilgisi hâlâ ana hesap
# (GROQ_API_KEY), sadece YEREL kullanım sayacı artık müşteri havuzuyla karışmıyor.
LEADS_AI_DAILY_TOKEN_BUDGET = 100000   # Groq'ta llama-3.3-70b-versatile için doğrulanan ayrı günlük TOKEN sınırı
LEADS_AI_DAILY_REQUEST_BUDGET = 1000   # llama havuzu için de ayrı izlenen istek sayısı (qwen ile aynı varsayım, ayrı hesap değil ayrı model)

# Havuz başına gerçek günlük bütçe - POOLS'taki her ada karşılık gelir. Yeni bir havuz eklenirse
# (bkz. app.py register_pool) burada bir karşılığı yoksa varsayılan qwen bütçesine (DAILY_TOKEN_BUDGET/
# DAILY_REQUEST_BUDGET) düşer - bkz. _budget_for_pool.
POOL_TOKEN_BUDGET = {"leads_ai": LEADS_AI_DAILY_TOKEN_BUDGET}
POOL_REQUEST_BUDGET = {"leads_ai": LEADS_AI_DAILY_REQUEST_BUDGET}


def _budget_for_pool(pool: str) -> tuple:
    """(token_budget, request_budget) - havuza özel bir bütçe tanımlıysa onu, yoksa qwen
    varsayılanını döndürür."""
    return POOL_TOKEN_BUDGET.get(pool, DAILY_TOKEN_BUDGET), POOL_REQUEST_BUDGET.get(pool, DAILY_REQUEST_BUDGET)


_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0}
CACHE_TTL = 15  # saniye - sık çağrıldığı için (her Groq isteğinden sonra) çok kısa tutuldu

POOLS = ["customer", "bulk", "customer2", "leads_ai"]


def register_pool(label: str) -> None:
    """Sabit 3 hesaba (customer/bulk/customer2) ek olarak Render'a eklenen HERHANGİ bir ek Groq
    hesabını (ör. GROQ_API_KEY_4, _5, ...) kullanım takibine dahil eder - bu çağrılmadan bir pool
    ile record_usage() çağrılırsa kullanım sessizce 'customer' havuzuna yanlış yazılırdı."""
    if label not in POOLS:
        POOLS.append(label)

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
        "leads_ai": {"total_tokens": 0, "call_count": 0},
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
        pass  # kullanım sayacı yedeklemesi kritik değil (en kötü ihtimalle sayaç sıfırlanır,
        # katalog verisi gibi kalıcı kaybolmaz) - bu yüzden burada ayrı bir durum takibi eklenmedi,
        # asıl kritik olan katalog yedeklemesi app.py'deki _github_sync_status ile izleniyor.


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
            token_budget, request_budget = _budget_for_pool(pool)
            result[pool] = {
                "total_tokens": p["total_tokens"],
                "call_count": p["call_count"],
                "budget": token_budget,
                "remaining_estimate": max(0, token_budget - p["total_tokens"]),
                "percent_used": round(100 * p["total_tokens"] / token_budget, 1),
                "request_budget": request_budget,
                "requests_remaining_estimate": max(0, request_budget - p["call_count"]),
            }

        result["total_tokens"] = result["customer"]["total_tokens"]
        result["call_count"] = result["customer"]["call_count"]
        result["remaining_estimate"] = result["customer"]["remaining_estimate"]
        result["percent_used"] = result["customer"]["percent_used"]
        return result

import os
import json
import time
import base64
import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

LEAD_REVIEWS_FILE = "lead_reviews.json"
LEAD_AI_SCORES_FILE = "lead_ai_scores.json"
LEAD_PRODUCT_SCORES_FILE = "lead_product_scores.json"

_leads_cache = {"data": None, "fetched_at": 0}
LEADS_CACHE_TTL = 60  # saniye


def load_leads() -> list:
    """leads.json'ı GitHub'dan canlı çeker. Bu dosyayı Render değil, haftalık GitHub Actions
    taraması yazıyor (git push ile) - Render'ın burada yerel/ephemeral bir kopyası yok, her zaman
    GitHub'daki en güncel hali okunur. Kısa süreli önbellek tekrarlayan sayfa yüklemelerinde
    GitHub'ı gereksiz yormaz."""
    now = time.time()
    if _leads_cache["data"] is not None and (now - _leads_cache["fetched_at"]) < LEADS_CACHE_TTL:
        return _leads_cache["data"]

    if not GITHUB_REPO:
        return _leads_cache["data"] or []

    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/leads.json"
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            data = res.json()
            leads = data if isinstance(data, list) else []
            _leads_cache["data"] = leads
            _leads_cache["fetched_at"] = now
            return leads
    except Exception:
        pass
    return _leads_cache["data"] or []


_lead_reviews_cache = {"data": None, "fetched_at": 0}
_lead_ai_scores_cache = {"data": None, "fetched_at": 0}
_lead_product_scores_cache = {"data": None, "fetched_at": 0}
_DICT_CACHE_TTL = 30  # saniye

def _load_json_dict_live(filename: str, cache: dict) -> dict:
    """catalog.json/leads.json ile aynı desen: önce GitHub'dan (kısa önbellekle) canlı okumayı dener,
    böylece bu dosya Render'a yeni bir deploy tetiklemeden GitHub üzerinden değişse bile (örn. elle
    bir düzeltme) birkaç saniye içinde canlıya yansır. Ulaşılamazsa Render'ın kendi diskindeki en son
    bilinen hale döner."""
    now = time.time()
    if cache["data"] is not None and (now - cache["fetched_at"]) < _DICT_CACHE_TTL:
        return cache["data"]
    if GITHUB_REPO:
        try:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{filename}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict):
                    cache["data"] = data
                    cache["fetched_at"] = now
                    return data
        except Exception:
            pass
    return _load_json_dict_from_disk(filename)


def _load_json_dict_from_disk(filename: str) -> dict:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_lead_reviews() -> dict:
    return _load_json_dict_live(LEAD_REVIEWS_FILE, _lead_reviews_cache)


def save_lead_reviews(reviews: dict):
    with open(LEAD_REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=4)


def _sync_json_file_to_github(filename: str, message: str):
    """Verilen dosyayı GitHub'a yedekler; Render diski her deploy'da sıfırlandığı için
    kalıcılık böyle sağlanır (catalog.json ile aynı desen)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        sha = None
        res = requests.get(api_url, headers=headers, timeout=15)
        if res.status_code == 200:
            sha = res.json().get("sha")
        with open(filename, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        body = {"message": message, "content": content_b64}
        if sha:
            body["sha"] = sha
        requests.put(api_url, headers=headers, json=body, timeout=15)
    except Exception:
        pass


def sync_lead_reviews_to_github():
    _sync_json_file_to_github(LEAD_REVIEWS_FILE, "Lead inceleme durumu güncelleme")
    # Az önce diske yazdığımız kesin doğru hali önbelleğe hemen yansıt - GitHub'a gidip gelmeyi
    # veya önbellek süresinin dolmasını beklemeden hemen sonraki okuma güncel veriyi görsün
    _lead_reviews_cache["data"] = _load_json_dict_from_disk(LEAD_REVIEWS_FILE)
    _lead_reviews_cache["fetched_at"] = time.time()


def load_lead_ai_scores() -> dict:
    return _load_json_dict_live(LEAD_AI_SCORES_FILE, _lead_ai_scores_cache)


def save_lead_ai_scores(scores: dict):
    with open(LEAD_AI_SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=4)


def sync_lead_ai_scores_to_github():
    _sync_json_file_to_github(LEAD_AI_SCORES_FILE, "AI lead sınıflandırma güncelleme")
    _lead_ai_scores_cache["data"] = _load_json_dict_from_disk(LEAD_AI_SCORES_FILE)
    _lead_ai_scores_cache["fetched_at"] = time.time()


def load_lead_product_scores() -> dict:
    """Her ürün sorgusu için ayrı bir anahtar altında saklanan, lead'lerin O ÜRÜNE özel
    alım ihtimali skorlarını döndürür - genel hedef kitle skorundan (relevance_score) farklı
    olarak 'bu spesifik ürünü kim alır' sorusuna cevap verir."""
    return _load_json_dict_live(LEAD_PRODUCT_SCORES_FILE, _lead_product_scores_cache)


def save_lead_product_scores(scores: dict):
    with open(LEAD_PRODUCT_SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=4)


def sync_lead_product_scores_to_github():
    _sync_json_file_to_github(LEAD_PRODUCT_SCORES_FILE, "Ürün bazlı lead sınıflandırma güncelleme")
    _lead_product_scores_cache["data"] = _load_json_dict_from_disk(LEAD_PRODUCT_SCORES_FILE)
    _lead_product_scores_cache["fetched_at"] = time.time()

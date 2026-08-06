import os
import json
import time
import base64
import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

LEAD_REVIEWS_FILE = "lead_reviews.json"

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


def load_lead_reviews() -> dict:
    try:
        with open(LEAD_REVIEWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_lead_reviews(reviews: dict):
    with open(LEAD_REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=4)


def sync_lead_reviews_to_github():
    """lead_reviews.json'ı GitHub'a yedekler; Render diski her deploy'da sıfırlandığı için
    kalıcılık böyle sağlanır (catalog.json ile aynı desen)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{LEAD_REVIEWS_FILE}"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        sha = None
        res = requests.get(api_url, headers=headers, timeout=15)
        if res.status_code == 200:
            sha = res.json().get("sha")
        with open(LEAD_REVIEWS_FILE, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        body = {"message": "Lead inceleme durumu güncelleme", "content": content_b64}
        if sha:
            body["sha"] = sha
        requests.put(api_url, headers=headers, json=body, timeout=15)
    except Exception:
        pass

import os
import json
import time
import base64
import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

CONTACTS_FILE = "existing_contacts.json"
BROADCAST_LOG_FILE = "broadcast_log.json"

CACHE_TTL = 30  # saniye
_contacts_cache = {"data": None, "fetched_at": 0}
_broadcast_log_cache = {"data": None, "fetched_at": 0}

def _load_json_list_live(filename: str, cache: dict) -> list:
    """catalog.json/leads.json ile aynı desen: önce GitHub'dan (kısa önbellekle) canlı okumayı dener,
    böylece bu dosyalardan biri Render'a yeni bir deploy tetiklemeden GitHub üzerinden değişse bile
    (örn. elle bir düzeltme) birkaç saniye içinde canlıya yansır. Ulaşılamazsa Render'ın kendi
    diskindeki en son bilinen hale döner."""
    now = time.time()
    if cache["data"] is not None and (now - cache["fetched_at"]) < CACHE_TTL:
        return cache["data"]
    if GITHUB_REPO:
        try:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{filename}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    cache["data"] = data
                    cache["fetched_at"] = now
                    return data
        except Exception:
            pass
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

# Her seferinde farklı bir açıdan mesaj üretmesi için - statik fiyat listesi olmasın diye
BROADCAST_ANGLES = [
    "Bu hafta öne çıkan bir ürün/kategori hakkında kısa, ilgi çekici bir WhatsApp duyuru mesajı yaz.",
    "Yeni gelen stok hakkında müşterileri bilgilendiren, merak uyandıran kısa bir mesaj yaz.",
    "Mevsimsel/dönemsel bir hatırlatma (bakım zamanı, kış hazırlığı vb.) temalı kısa bir mesaj yaz.",
    "Sadık müşterilere teşekkür + kurumsal güven mesajı ver, satış baskısı yapmadan.",
]


def load_contacts() -> list:
    return _load_json_list_live(CONTACTS_FILE, _contacts_cache)


def save_contacts(contacts: list):
    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        json.dump(contacts, f, ensure_ascii=False, indent=4)


def load_broadcast_log() -> list:
    return _load_json_list_live(BROADCAST_LOG_FILE, _broadcast_log_cache)


def save_broadcast_log(log: list):
    with open(BROADCAST_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=4)


def _sync_to_github(filename: str, message: str):
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


def _load_json_list_from_disk(filename: str) -> list:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def sync_broadcast_log_to_github():
    _sync_to_github(BROADCAST_LOG_FILE, "Broadcast log güncelleme")
    # Az önce diske yazdığımız kesin doğru hali önbelleğe hemen yansıt - GitHub'a gidip gelmeyi
    # veya önbellek süresinin dolmasını beklemeden hemen sonraki okuma güncel veriyi görsün
    _broadcast_log_cache["data"] = _load_json_list_from_disk(BROADCAST_LOG_FILE)
    _broadcast_log_cache["fetched_at"] = time.time()


def sync_contacts_to_github():
    _sync_to_github(CONTACTS_FILE, "Müşteri listesi güncelleme")
    _contacts_cache["data"] = _load_json_list_from_disk(CONTACTS_FILE)
    _contacts_cache["fetched_at"] = time.time()

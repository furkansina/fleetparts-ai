import os
import json
import base64
import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

CONTACTS_FILE = "existing_contacts.json"
BROADCAST_LOG_FILE = "broadcast_log.json"

# Her seferinde farklı bir açıdan mesaj üretmesi için - statik fiyat listesi olmasın diye
BROADCAST_ANGLES = [
    "Bu hafta öne çıkan bir ürün/kategori hakkında kısa, ilgi çekici bir WhatsApp duyuru mesajı yaz.",
    "Yeni gelen stok hakkında müşterileri bilgilendiren, merak uyandıran kısa bir mesaj yaz.",
    "Mevsimsel/dönemsel bir hatırlatma (bakım zamanı, kış hazırlığı vb.) temalı kısa bir mesaj yaz.",
    "Sadık müşterilere teşekkür + kurumsal güven mesajı ver, satış baskısı yapmadan.",
]


def load_contacts() -> list:
    try:
        with open(CONTACTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_contacts(contacts: list):
    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        json.dump(contacts, f, ensure_ascii=False, indent=4)


def load_broadcast_log() -> list:
    try:
        with open(BROADCAST_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


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


def sync_broadcast_log_to_github():
    _sync_to_github(BROADCAST_LOG_FILE, "Broadcast log güncelleme")


def sync_contacts_to_github():
    _sync_to_github(CONTACTS_FILE, "Müşteri listesi güncelleme")

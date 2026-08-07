import re
import time

import requests

from provinces import PROVINCES, PROVINCE_SLUGS_TBC

TBC_BASE = "https://www.turkbusinesscenter.com"
TBC_HEADERS = {"User-Agent": "fleetparts-lead-discovery/1.0 (+isletme rehberi arama, iletisim: kod sahibi)"}

# Hedef kitlemize (yedek parça toptancıları + filo sahibi lojistik firmaları) yakın kategoriler.
# ÖNCEDEN sadece en dar 2 kategori taranıyordu ("çok geniş olur, gürültü katar" diye) - ama bu
# gereksiz temkinlilik ham veriyi ciddi şekilde daraltıyordu. Zaten score_lead() + AI netleştirme
# aşaması alakasızları (bağımsız tamirci, sürücü kursu vb.) ayıklıyor - ayıklama işini puanlama
# sistemine bırakıp buradaki ağı genişletmek daha doğru: az veriden seçmek yerine çok veriden seçelim.
TBC_CATEGORIES = {
    "otomotiv-yedek-parca-firmalari": "Oto Yedek Parça (Firma Rehberi)",
    "tasimacilik-nakliye-firmalari": "Taşımacılık/Nakliye (Firma Rehberi)",
    "otomotiv-yan-sanayi-firmalari": "Otomotiv Yan Sanayi (Firma Rehberi)",
    "otomotiv-firmalari": "Otomotiv / Oto Yan Sanayi (Firma Rehberi)",
}

_LIST_ITEM_RE = re.compile(r'<a class="c" href="(/firma/[^"]+)"><b>([^<]+)</b></a>')
_PAGER_RE = re.compile(r'<div class="pager">(.*?)</div>', re.DOTALL)
_PAGE_NUM_RE = re.compile(r'>(\d+)<')
_FIELD_RE = re.compile(r'<td[^>]*>\s*(Adres|Şehir|Telefon|Web Sitesi):\s*</td>\s*<td[^>]*>(.*?)</td>', re.DOTALL)
_TAG_STRIP_RE = re.compile(r'<[^>]+>')


def province_to_slug(province: str) -> str:
    try:
        idx = PROVINCES.index(province)
        return PROVINCE_SLUGS_TBC[idx]
    except ValueError:
        return ""


def _fetch(url: str) -> str:
    try:
        res = requests.get(url, headers=TBC_HEADERS, timeout=20)
        if res.status_code == 200:
            return res.text
    except Exception as e:
        print(f"  [directory] {url}: hata - {e}")
    return ""


def _extract_detail_fields(detail_html: str) -> dict:
    """Firma detay sayfasından iletişim bilgilerini çıkarır. KASITLI OLARAK 'Yetkili Kişi'
    alanı hiç okunmaz/saklanmaz - bu projede kişi adı hiçbir kaynaktan (KVKK) leads.json'a
    yazılmaz, sadece kurumsal bilgiler (adres, telefon, web) alınır."""
    fields = {}
    for label, raw_value in _FIELD_RE.findall(detail_html):
        value = _TAG_STRIP_RE.sub("", raw_value).strip()
        fields[label] = value
    return fields


def _list_pages(first_page_html: str, list_url: str) -> list:
    """İlk sayfanın HTML'inden toplam sayfa sayısını çıkarıp diğer sayfaların URL'lerini üretir."""
    pager_match = _PAGER_RE.search(first_page_html)
    if not pager_match:
        return []
    page_numbers = [int(n) for n in _PAGE_NUM_RE.findall(pager_match.group(1))]
    max_page = max(page_numbers) if page_numbers else 1
    return [f"{list_url.rstrip('/')}/{p}" for p in range(2, max_page + 1)]


def search_province(province: str, delay: float = 0.4) -> list:
    """turkbusinesscenter.com'daki (ücretsiz, girişsiz, Türkiye'ye özel B2B firma rehberi)
    ilgili kategorilerden bir ildeki firmaları çeker. OSM'e tamamlayıcı bir kaynaktır - OSM'in
    yakalayamadığı, sadece bu rehbere kayıtlı firmaları da bulur. Sunucuya nazik davranmak için
    (robots.txt kontrol edildi, sadece bilinen toplu-indirme botları engelli) istekler arasında
    küçük bir bekleme var."""
    slug = province_to_slug(province)
    if not slug:
        return []

    results = []
    for category, label in TBC_CATEGORIES.items():
        list_url = f"{TBC_BASE}/il/{slug}-{category}/"
        first_html = _fetch(list_url)
        if not first_html:
            continue

        firm_links = dict(_LIST_ITEM_RE.findall(first_html))  # {url: isim}
        for page_url in _list_pages(first_html, list_url):
            time.sleep(delay)
            page_html = _fetch(page_url)
            if page_html:
                firm_links.update(dict(_LIST_ITEM_RE.findall(page_html)))

        for detail_path, name in firm_links.items():
            time.sleep(delay)
            detail_html = _fetch(f"{TBC_BASE}{detail_path}")
            if not detail_html:
                continue
            fields = _extract_detail_fields(detail_html)
            results.append({
                "site_id": f"tbc_{detail_path.strip('/').split('/')[-1]}",
                "name": (name or "").strip(),
                "shop_type": "directory",  # score_lead için: OSM etiketi değil, isim/kategori bazlı değerlendirilir
                "category_label": label,
                "phone": fields.get("Telefon", ""),
                "website": fields.get("Web Sitesi", ""),
                "address": fields.get("Adres", ""),
                "lat": None,
                "lon": None,
            })

    return results

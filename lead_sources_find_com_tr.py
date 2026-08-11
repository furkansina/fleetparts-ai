import html
import re
import time

import requests

from provinces import PROVINCES

HEADERS = {"User-Agent": "fleetparts-lead-discovery/1.0 (+find.com.tr firma rehberi arama)"}

# find.com.tr - resmi ticaret sicili kaynaklı (firma detay sayfalarında Vergi No/Sicil No/NACE
# Kodu/Mersis No görülüyor) geniş kapsamlı bir firma rehberi. Gerçek bir testte doğrulandı
# (2026-08-11): OSM/turkbusinesscenter.com'un neredeyse hiç veri bulamadığı Bingöl gibi küçük
# illerde bile "oto yedek parça" araması TEK BAŞINA 24 gerçek, doğru kategorili firma buluyor -
# mevcut kaynakların HİÇBİRİ küçük illerde bu derinliğe ulaşamıyordu.
#
# ÖNEMLİ SINIRLAMA: bu kaynak TELEFON NUMARASI vermiyor (ne arama sonuçlarında ne çoğu firma
# detay sayfasında - denenen örneklerde hiç yoktu). Yani bu lead'ler WhatsApp'tan doğrudan
# ulaşılamaz durumda gelir - leads.html zaten telefonsuz lead'ler için "Google'da Ara" bağlantısı
# gösteriyor, o akış burada da aynen çalışır. Skorlama (score_lead) da telefon olmadığı için
# data_completeness puanını otomatik düşük tutar - bu lead'ler panelde phone'lu olanların
# altında sıralanır, bu doğru/beklenen bir davranış (aksiyon almadan önce elle telefon aranması
# gerektiği için daha düşük öncelik).
BASE = "https://www.find.com.tr"
QUERY = "oto yedek parça"  # hem isim hem kategori/alt-kategori metnini eşleştiriyor (gerçek testte doğrulandı)

_MAX_PAGES_SAFETY = 10  # büyük illerde (İstanbul ~20 sayfa) çalışma süresini sınırlar; asıl hedef
# olan küçük Anadolu illerinde zaten 1-2 sayfa var, bu tavana hiç çarpmıyorlar
_PAGE_NUM_RE = re.compile(r"\?Page=(\d+)\"")

_TR_FOLD = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
})


def province_to_slug(province: str) -> str:
    """find.com.tr'nin URL yapısı basit ASCII katlamalı, boşluksuz il adı kullanıyor
    (örn. 'Bingöl' -> 'bingol', 'İstanbul' -> 'istanbul') - gerçek örneklerle doğrulandı."""
    return province.translate(_TR_FOLD).lower().replace(" ", "")


def _fetch(url: str) -> str:
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            return ""
        res.encoding = "utf-8"
        return res.text
    except Exception as e:
        print(f"  [find.com.tr] {url}: hata - {e}")
        return ""


def _max_page(html: str) -> int:
    nums = [int(n) for n in _PAGE_NUM_RE.findall(html)]
    return max(nums) if nums else 1


_ITEM_SPLIT = '<div class="search-item page-shadow">'
_NAME_RE = re.compile(r'<h2>\s*<a href="(/Company/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>')
_CAT_RE = re.compile(r'<li>Kategoriler\s*:</li>\s*<li><a href="[^"]*">([^<]*)</a></li>', re.DOTALL)
_SUBCAT_RE = re.compile(r'<li>Alt Kategoriler\s*:</li>\s*<li><a href="[^"]*">([^<]*)</a></li>', re.DOTALL)


def _parse_results(page_html: str, province: str) -> list:
    results = []
    blocks = page_html.split(_ITEM_SPLIT)[1:]
    for block in blocks:
        m = _NAME_RE.search(block)
        if not m:
            continue
        detail_path, name = m.groups()
        name = html.unescape(name).strip()
        if not name:
            continue
        cat_m = _CAT_RE.search(block)
        subcat_m = _SUBCAT_RE.search(block)
        category = html.unescape(cat_m.group(1)).strip() if cat_m else ""
        subcategory = html.unescape(subcat_m.group(1)).strip() if subcat_m else ""
        category_label = " - ".join(p for p in (category, subcategory) if p) or "Firma Rehberi"
        slug = detail_path.strip("/").split("/")[-1]
        results.append({
            "site_id": f"findcomtr_{slug}",
            "name": name,
            "shop_type": "directory",
            "category_label": category_label,
            "phone": "",
            "website": "",
            "address": "",
            "lat": None,
            "lon": None,
        })
    return results


def search_province(province: str, delay: float = 0.4) -> list:
    slug = province_to_slug(province)
    from urllib.parse import quote
    base_url = f"{BASE}/Search/{quote(QUERY)}/TumKategoriler/{slug}"
    first_html = _fetch(base_url)
    if not first_html:
        return []

    results = _parse_results(first_html, province)
    max_page = min(_max_page(first_html), _MAX_PAGES_SAFETY)

    for page in range(2, max_page + 1):
        time.sleep(delay)
        page_html = _fetch(f"{base_url}?Page={page}")
        if not page_html:
            continue
        results.extend(_parse_results(page_html, province))

    return results


def search_all(delay: float = 0.4) -> list:
    """81 ilin tamamını tarar - önceki kaynakların (OSM, turkbusinesscenter.com) küçük illerde
    neredeyse boş döndüğü durumlarda bile bu kaynak gerçek sonuç veriyor."""
    all_results = []
    for province in PROVINCES:
        try:
            items = search_province(province, delay=delay)
        except Exception as e:
            print(f"  [find.com.tr] {province} taraması başarısız - {e}")
            items = []
        for item in items:
            item["province"] = province
        print(f"  [find.com.tr] {province}: {len(items)} firma")
        all_results.extend(items)
        time.sleep(delay)
    return all_results

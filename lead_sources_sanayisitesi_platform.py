import re
import time

import requests

HEADERS = {"User-Agent": "fleetparts-lead-discovery/1.0 (+sanayisitesi.com.tr otomotiv firmalari rehberi)"}

# sanayisitesi.com.tr TEK bir platform ama onlarca ili İL-ADI.sanayisitesi.com.tr alt alan
# adlarıyla ayrı ayrı barındırıyor - HEPSİ AYNI ŞABLONU kullanıyor (aynı "/otomotiv-firmalari"
# yolu, aynı "box-business" HTML yapısı). Bu, önceki lead_sources_sanayi_sitesi.py'deki gibi
# şehir başına özel bir parser yazmak yerine TEK bir parser'ı parametreleyip onlarca ile birden
# uygulayabileceğimiz anlamına geliyor - gerçek bir kullanımda doğrulandı (2026-08-11):
# eskisehir/bursa/konya/adana/gaziantep/denizli/manisa/kocaeli/antalya/kahramanmaras/diyarbakir/
# balikesir/elazig/erzurum alt alan adlarının HEPSİ aynı "/otomotiv-firmalari" yolunda gerçek,
# ayrıştırılabilir sonuç döndürdü (mersin/samsun/malatya/sanliurfa/trabzon/sakarya/van gibi
# bazı alt alan adları da var ama o ilde kayıtlı otomotiv firması olmadığı için (0 sonuç, hata
# değil) kasıtlı olarak listeye alınmadı - gerçek veri yoksa taramanın bir anlamı yok).
#
# robots.txt kontrol edildi (yalnızca /images/, /core/, /serpito971/ engelli - /otomotiv-firmalari
# serbest).
CITIES = {
    "eskisehir": "Eskişehir",
    "bursa": "Bursa",
    "konya": "Konya",
    "adana": "Adana",
    "gaziantep": "Gaziantep",
    "denizli": "Denizli",
    "manisa": "Manisa",
    "kocaeli": "Kocaeli",
    "antalya": "Antalya",
    "kahramanmaras": "Kahramanmaraş",
    "diyarbakir": "Diyarbakır",
    "balikesir": "Balıkesir",
    "elazig": "Elazığ",
    "erzurum": "Erzurum",
}

_MAX_PAGES_SAFETY = 40  # gercek bir kullanimda en yogun il (Eskisehir) 10 sayfaydi - bolluk payi

_PAGER_NUM_RE = re.compile(r'\?page=(\d+)"[^>]*>\s*\d+\s*</a>')


def _fetch(url: str) -> str:
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            return ""
        # Bazı eski/dinamik siteler HTTP başlığında charset belirtmiyor - requests bu durumda
        # yanlış bir kodlamaya (ISO-8859-1) düşüp Türkçe karakterleri bozuyor, gerçek içerik UTF-8
        # (bkz. lead_sources_sanayi_sitesi.py'deki aynı not - aynı sorun burada da tespit edildi).
        res.encoding = "utf-8"
        return res.text
    except Exception as e:
        print(f"  [sanayisitesi-platform] {url}: hata - {e}")
        return ""


def _max_page(html: str) -> int:
    nums = [int(n) for n in _PAGER_NUM_RE.findall(html)]
    return max(nums) if nums else 1


def _parse_listing_page(html: str, city_id: str, province_label: str) -> list:
    """Her ilan '<div class="box-business">' ile başlıyor - bloklara ayırıp her blok içinde
    isim/kategori/adres/telefonu ayrı ayrı arıyoruz. Tek bir dev regex yerine bu yaklaşım, iç
    içe HTML'in kırılganlığına karşı daha dayanıklı (aynı desen izmir3 parser'ında da kullanıldı)."""
    results = []
    blocks = html.split('<div class="box-business">')[1:]
    for block in blocks:
        name_m = re.search(r'<h3>\s*([^<]+?)\s*</h3>', block)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        if not name:
            continue
        slug_m = re.search(r'href="(/firma/[^"]+)"', block)
        category_m = re.search(r'<h4>\s*([^<]*?)\s*</h4>', block)
        address_m = re.search(r'<div class="address"[^>]*>(.*?)</div>', block, re.DOTALL)
        phone_m = re.search(r'<div class="phone">\s*<a href="tel:([^"]*)"', block)

        category = category_m.group(1).strip() if category_m else ""
        address = re.sub(r'\s+', ' ', address_m.group(1)).strip() if address_m else ""
        phone = phone_m.group(1).strip() if phone_m else ""
        slug = slug_m.group(1).strip("/").split("/")[-1] if slug_m else re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        category_label = f"{province_label} Sanayi Sitesi - {category}" if category else f"{province_label} Sanayi Sitesi"
        results.append({
            "site_id": f"sanayisitesi_{city_id}_{slug}",
            "name": name,
            "shop_type": "directory",
            "category_label": category_label,
            "phone": phone,
            "website": "",
            "address": address,
            "lat": None,
            "lon": None,
        })
    return results


def search_city(city_id: str, province_label: str, delay: float = 0.4) -> list:
    """Bir ildeki (bu platformda kayıtlı) tüm otomotiv sektörü firmalarını çeker - sayfalama
    otomatik takip edilir."""
    base = f"http://{city_id}.sanayisitesi.com.tr/otomotiv-firmalari"
    first_html = _fetch(base)
    if not first_html:
        return []

    results = _parse_listing_page(first_html, city_id, province_label)
    max_page = min(_max_page(first_html), _MAX_PAGES_SAFETY)

    for page in range(2, max_page + 1):
        time.sleep(delay)
        page_html = _fetch(f"{base}?page={page}")
        if not page_html:
            continue
        results.extend(_parse_listing_page(page_html, city_id, province_label))

    return results


def search_all(delay: float = 0.4) -> list:
    """CITIES'teki tüm illeri sırayla tarar. Her sonucun 'province' alanı buradan (CITIES'in
    Türkçe il adı karşılığından) atanır - run_discovery.py'deki diğer sanayi sitesi
    kaynaklarıyla (bkz. lead_sources_sanayi_sitesi.search_all) aynı çağrı deseni."""
    all_results = []
    for city_id, province_label in CITIES.items():
        try:
            items = search_city(city_id, province_label, delay=delay)
        except Exception as e:
            print(f"  [sanayisitesi-platform] {city_id} taraması başarısız - {e}")
            items = []
        for item in items:
            item["province"] = province_label
        print(f"  [sanayisitesi-platform] {province_label}: {len(items)} firma")
        all_results.extend(items)
        time.sleep(delay)
    return all_results

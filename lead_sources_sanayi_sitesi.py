import re
import time

import requests

HEADERS = {"User-Agent": "fleetparts-lead-discovery/1.0 (+sanayi sitesi esnaf rehberi arama)"}

# OSM (harita etiketleri) ve turkbusinesscenter.com (kurumsal Ltd./A.Ş. odaklı B2B rehberi) ikisi
# de Türkiye'deki gerçek yedek parça/tamir esnafının BÜYÜK kısmını (küçük, şahıs/esnaf statülü,
# şehrin "Sanayi Sitesi" bölgesinde kümelenen dükkanlar) yakalamıyor - gerçek bir testte tespit
# edildi (Adana'da sadece 28 kaliteli sonuç, halbuki şehrin kendi Oto Sanayi Sitesi'nde tek
# başına yüzlerce esnaf var). Bazı büyük sanayi siteleri/şehir rehberleri kendi web sitelerinde
# firma listesi tutuyor - burada TEK TEK doğrulanmış, gerçek, robots.txt'e uygun kaynaklar var.
# Her sitenin HTML yapısı farklı olduğu için ortak bir regex yerine site başına parser fonksiyonu
# kullanılıyor - kırılgan "hepsine uysun" regex'i yerine daha güvenilir.
SOURCES = [
    {
        "id": "ankara_oto_sanayi",
        "province": "Ankara",
        "base_url": "https://ankaraotosanayi.com/",
        "parser": "ankara",
        # Sadece hedef kitleyle doğrudan ilgili kategoriler alındı - "Banka/Sigorta",
        # "Oto Alım Satım Kiralama" gibi alakasız kategoriler kasıtlı dışarıda bırakıldı.
        "categories": {
            "Yedek-Parca-Aksesuar-k14": "Yedek Parça/Aksesuar",
            "cikma-Yedek-hurdaci--k17": "Çıkma Yedek (Hurdacı)",
            "Elektrik-Aku-Elektronik-k4": "Elektrik/Akü/Elektronik",
            "Kaporta-Boya-sase-k6": "Kaporta/Boya/Şase",
            "onDuzen-Lastik-Amortisor-k9": "Ön Düzen-Lastik/Amortisör",
            "Rektefiye-Turbo-Pompa-k18": "Rektefiye/Turbo/Pompa",
            "Kayis-Rulman-Yag-k19": "Kayış/Rulman/Yağ",
            "Oto-Kurtarma-Vinc-k8": "Oto Kurtarma/Vinç",
            "Otogaz-LPG-Montaj-k10": "Otogaz LPG Montaj",
            "Egzoz-Torna-Kaynak-k3": "Egzoz/Torna/Kaynak",
            "Radyator-Klima-k7": "Radyatör/Klima",
            "Civata-Boya-Hirdavat-k16": "Cıvata/Boya/Hırdavat",
        },
    },
    {
        "id": "sanko_sanayi_istanbul",
        "province": "İstanbul",
        "base_url": "https://www.sankootosanayi.com/",
        "parser": "sanko",
        "categories": {
            "sektorler/oto-yedek-parcacilar": "Oto Yedek Parçacılar",
            "sektorler/oto-elektrikcileri": "Oto Elektrikçileri",
            "sektorler/oto-lastikciler": "Oto Lastikçiler",
            "sektorler/oto-kaporta-boya": "Oto Kaporta/Boya",
            "sektorler/oto-motor-tamirciler": "Oto Motor Tamirciler",
            "sektorler/oto-levazimati-ve-nalburlar": "Oto Levazımatı/Nalburlar",
            "sektorler/tornacilar": "Tornacılar",
        },
    },
    {
        # İzmir'in en büyük sanayi sitesi (11 araç + 2 yaya girişi, ~1400 işletme) - kendi
        # firma rehberini PHP tabanlı bir sitede tutuyor, robots.txt yok (kısıtlama yok).
        "id": "izmir_3_sanayi_sitesi",
        "province": "İzmir",
        "base_url": "https://www.3sanayisitesi.com.tr/firma-listesi.php?kategori=",
        "parser": "izmir3",
        "categories": {
            "63": "Yedek Parça",
            "95": "Kaporta",
            "92": "LPG-Otogaz",
            "87": "Oto Boya",
            "81": "Oto Elektrik/Klima",
            "75": "Pompa",
            "74": "Radyatör",
            "96": "Kapakçı",
            "111": "Balata",
            "109": "Çıkma Parça",
            "106": "Döşeme",
            "105": "Egzoz",
        },
    },
]

_ANKARA_BLOCK_RE = re.compile(
    r'href="([a-zA-Z0-9\-]+-f\d+)"\s+class="menu">\s*<strong><span[^>]*>([^<]+)</span></strong></a>'
    r'(.{0,1200}?)</table>',
    re.DOTALL,
)
_ANKARA_PHONE_RE = re.compile(r"Telefon:\s*([0-9][0-9 \-]{8,20})")
_ANKARA_ADDRESS_RE = re.compile(r"Adres:\s*([^<]{5,150})</td>")
# Site her firma kaydında "Bu firmanın bilgileri X tarihinde güncellendi" diye açık bir tarih
# veriyor - bazıları yıllar önce güncellenmiş, hâlâ faaliyette olduğu garanti değil. Bu tarihi
# çıkarıp lead'e ekliyoruz ki hem arayüzde görülebilsin hem puanlamada eskilik cezası uygulanabilsin.
_ANKARA_UPDATED_RE = re.compile(r"<strong>(\d{2}\.\d{2}\.\d{4})</strong>\s*\r?\n?\s*tarihinde güncellendi")

_SANKO_BLOCK_RE = re.compile(
    r'<p class="text-orange fw-600 mb-1 fs-16">\s*<span><strong>([^<]+)</strong></span></p>'
    r'(.{0,900}?)</ul>',
    re.DOTALL,
)
_SANKO_PHONE_RE = re.compile(r'href="tel:([^"]+)"')
_SANKO_ADDRESS_RE = re.compile(r"<li>([^<]{10,200})</li>\s*$")


def _fetch(url: str) -> str:
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            return ""
        # Bazı eski siteler HTTP başlığında charset belirtmiyor - requests bu durumda
        # ISO-8859-1'e düşüyor ve Türkçe karakterler bozuluyor, gerçek içerik UTF-8.
        res.encoding = "utf-8"
        return res.text
    except Exception as e:
        print(f"  [sanayi-sitesi] {url}: hata - {e}")
        return ""


def _parse_ankara(html: str, category_label: str) -> list:
    results = []
    for m in _ANKARA_BLOCK_RE.finditer(html):
        detail_path, name, tail = m.groups()
        name = name.strip()
        if not name:
            continue
        phone_m = _ANKARA_PHONE_RE.search(tail)
        phone = phone_m.group(1).split("-")[0].strip() if phone_m else ""
        addr_m = _ANKARA_ADDRESS_RE.search(tail)
        address = addr_m.group(1).strip() if addr_m else ""
        updated_m = _ANKARA_UPDATED_RE.search(tail)
        source_updated_at = updated_m.group(1) if updated_m else ""
        firm_no = detail_path.rsplit("-f", 1)[-1]
        results.append({
            "site_id": f"ankaraotosanayi_{firm_no}",
            "name": name,
            "shop_type": "directory",
            "category_label": f"Ankara Oto Sanayi Rehberi - {category_label}",
            "phone": phone,
            "website": "",
            "address": address,
            "lat": None,
            "lon": None,
            "source_updated_at": source_updated_at,
        })
    return results


def _parse_sanko(html: str, category_label: str) -> list:
    results = []
    for m in _SANKO_BLOCK_RE.finditer(html):
        name, tail = m.groups()
        name = name.strip()
        if not name:
            continue
        phone_m = _SANKO_PHONE_RE.search(tail)
        phone = phone_m.group(1).strip() if phone_m else ""
        addr_m = _SANKO_ADDRESS_RE.search(tail.strip())
        address = addr_m.group(1).strip() if addr_m else ""
        # site_id icin isim+kategori normalize edilip kullaniliyor, sitede sabit bir
        # firma ID'si (URL) yok
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        results.append({
            "site_id": f"sanko_{slug}_{category_label[:10]}",
            "name": name,
            "shop_type": "directory",
            "category_label": f"Sanko Sanayi Sitesi - {category_label}",
            "phone": phone,
            "website": "",
            "address": address,
            "lat": None,
            "lon": None,
        })
    return results


_IZMIR3_BLOCK_RE = re.compile(r'<article class="entry panel">(.{0,8000}?)</article>', re.DOTALL)
_IZMIR3_NAME_RE = re.compile(r'firma-detay\.php\?firma=([^"]+)">\s*([^<]+?)\s*</a>\s*</h3>')
_IZMIR3_PHONE_RE = re.compile(r'fa-phone"></i>\s*([0-9][0-9 ]{8,40})</li>')


def _pick_best_phone(raw: str) -> str:
    """Bu site bazen aynı satırda birden fazla numara (sabit hat + cep) boşlukla ayrılmış
    şekilde veriyor - WhatsApp menzili için cep (05xx) varsa onu tercih et, yoksa ilkini al."""
    numbers = raw.split()
    for n in numbers:
        if n.startswith("05") and len(n) == 11:
            return n
    return numbers[0] if numbers else ""


def _parse_izmir3(html: str, category_label: str) -> list:
    results = []
    for block_m in _IZMIR3_BLOCK_RE.finditer(html):
        block = block_m.group(1)
        name_m = _IZMIR3_NAME_RE.search(block)
        if not name_m:
            continue
        slug, name = name_m.groups()
        name = name.strip()
        if not name:
            continue
        phone_m = _IZMIR3_PHONE_RE.search(block)
        phone = _pick_best_phone(phone_m.group(1).strip()) if phone_m else ""
        results.append({
            "site_id": f"izmir3sanayi_{slug}",
            "name": name,
            "shop_type": "directory",
            "category_label": f"İzmir 3. Sanayi Sitesi - {category_label}",
            "phone": phone,
            "website": "",
            "address": "",
            "lat": None,
            "lon": None,
        })
    return results


_PARSERS = {"ankara": _parse_ankara, "sanko": _parse_sanko, "izmir3": _parse_izmir3}

# Kayseri Yeni Sanayi rehberi diğerlerinden farklı: liste sayfası sadece isim+kategori veriyor,
# telefon/adres için her firmanın kendi detay sayfası ayrıca çekilmesi gerekiyor. Detay
# sayfasında "Firma Yetkilisi: <kişi adı>" alanı da VAR ama KASITLI OLARAK hiç okunmuyor - bu
# projede kişi adı hiçbir kaynaktan (KVKK) leads.json'a yazılmaz. Sadece "firmainfo" kutusundaki
# (Adres/Telefon) kurumsal bilgiler alınıyor, sayfanın geri kalanı hiç parse edilmiyor.
KAYSERI_BASE = "https://kayseriyenisanayi.com"
KAYSERI_CATEGORIES = {
    "oto-yedek-parca": "Oto Yedek Parça", "servis-yedek-parca": "Servis/Yedek Parça",
    "mekanik": "Mekanik", "motor": "Motor", "sanziman": "Şanzıman",
    "kaporta": "Kaporta", "kaporta-boya": "Kaporta/Boya", "egzoz": "Egzoz",
    "rot-balans-lastik": "Rot Balans/Lastik", "lastik": "Lastik",
    "oto-elektrik": "Oto Elektrik", "lpg-otogaz": "LPG/Otogaz",
    "oto-aksesuar": "Oto Aksesuar", "tamir-ve-bakim-servisi": "Tamir ve Bakım Servisi",
    "bakim-servisi": "Bakım Servisi",
}
_KAYSERI_LIST_ITEM_RE = re.compile(r'<h2 class="post-title">\s*<a href="([^"]+)"[^>]*>([^<]+)</a>')
_KAYSERI_NEXT_PAGE_RE = re.compile(r'<a href="([^"]+)"\s*>Sonraki sayfa')
_KAYSERI_FIRMAINFO_RE = re.compile(r'<div class="firmainfo">(.*?)</div>\s*<div class="yazi_paylas">', re.DOTALL)
_KAYSERI_ADDRESS_RE = re.compile(r"Adres</span>([^<]{2,150})</li>")
_KAYSERI_PHONE_RE = re.compile(r'class="telefon"><span>Telefon</span><a href="tel:([^"]+)"')


def _scan_kayseri(delay: float) -> list:
    results = {}  # detail_url -> item, kategoriler arasinda ayni firma tekrar cikabilir
    for cat_slug, cat_label in KAYSERI_CATEGORIES.items():
        page_url = f"{KAYSERI_BASE}/firmalar/{cat_slug}/"
        pages_visited = 0
        while page_url and pages_visited < 15:  # makul bir tavan, sonsuz döngüye karşı
            html = _fetch(page_url)
            pages_visited += 1
            if not html:
                break
            for detail_url, name in _KAYSERI_LIST_ITEM_RE.findall(html):
                if detail_url not in results:
                    results[detail_url] = {"name": name.strip(), "category_label": cat_label}
            next_m = _KAYSERI_NEXT_PAGE_RE.search(html)
            page_url = next_m.group(1) if next_m else None
            if page_url:
                time.sleep(delay)

    items = []
    for detail_url, info in results.items():
        time.sleep(delay)
        detail_html = _fetch(detail_url)
        if not detail_html:
            continue
        box_m = _KAYSERI_FIRMAINFO_RE.search(detail_html)
        box = box_m.group(1) if box_m else ""
        addr_m = _KAYSERI_ADDRESS_RE.search(box)
        phone_m = _KAYSERI_PHONE_RE.search(box)
        slug = detail_url.rstrip("/").rsplit("/", 1)[-1]
        items.append({
            "site_id": f"kayseriyenisanayi_{slug}",
            "name": info["name"],
            "shop_type": "directory",
            "category_label": f"Kayseri Yeni Sanayi Rehberi - {info['category_label']}",
            "phone": phone_m.group(1).strip() if phone_m else "",
            "website": "",
            "address": addr_m.group(1).strip() if addr_m else "",
            "lat": None,
            "lon": None,
            "province": "Kayseri",
        })
    return items


def search_all(delay: float = 0.5) -> list:
    """Bilinen tüm sanayi sitesi/bölgesel oto rehberi kaynaklarını tarar. search_province'ten
    farklı olarak il parametresi almaz - her kaynak zaten sabit bir ile bağlı (Ankara/İstanbul),
    bu yüzden tek seferde tüm kaynaklar taranır. Sonuçlardaki 'province' alanı çağıran tarafından
    değil, doğrudan buradan (kaynağın kendi iline göre) atanır."""
    all_results = []
    for source in SOURCES:
        parser = _PARSERS[source["parser"]]
        for cat_path, cat_label in source["categories"].items():
            url = source["base_url"] + cat_path
            html = _fetch(url)
            if not html:
                continue
            items = parser(html, cat_label)
            for item in items:
                item["province"] = source["province"]
            all_results.extend(items)
            time.sleep(delay)

    try:
        all_results.extend(_scan_kayseri(delay))
    except Exception as e:
        print(f"  [sanayi-sitesi] kayseri tarama hatasi - {e}")

    return all_results

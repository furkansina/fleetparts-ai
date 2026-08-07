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
    return all_results

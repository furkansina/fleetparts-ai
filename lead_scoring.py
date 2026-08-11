import re

from provinces import NAME_KEYWORDS_HIGH_VALUE
from lead_dedupe import is_mobile_phone, turkish_lower

# OSM'in ham etiket değerleri (car_parts, yes, convenience vb.) okunabilir değil -
# arayüzde gösterilecek temiz sektör etiketleri buradan gelir
SECTOR_LABELS = {
    "car_parts": "Oto Yedek Parça",
    "logistics": "Lojistik/Nakliye Ofisi",
    "car_repair": "Oto Tamir Servisi",
    "tyres": "Lastikçi",
    "trailer": "Dorse/Treyler Satış-Servis",
    "agrarian": "Tarım Makinesi Ekipman/Parça",
}

# Anahtar kelimeler kelime sınırıyla (\b) aranır, düz alt-dize (substring) araması DEĞİL.
# Aksi halde örneğin "filo" kelimesi "Profilo" (gerçek bir beyaz eşya markası) gibi tamamen
# alakasız isimlerin içinde de eşleşip yanlış pozitif üretiyordu - gerçek bir örnekte tespit edildi.
_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in NAME_KEYWORDS_HIGH_VALUE) + r")\b"
)

# "tasimacilik-nakliye-firmalari" gibi geniş bir rehber kategorisi ev eşyası taşıyan (evden eve
# nakliyat - küçük araçlarla çalışan bireysel taşınma firmaları, toptan parça alıcısı değiller)
# ve seyahat acenteleriyle (araç filosu olmayan) dolu çıktı - gerçek bir testte tespit edildi.
# Bu kelimeler geçen firmalar hedef kitle DIŞI sayılır, sektör eşleşmesi olsa bile.
#
# GENİŞLETME (2026-08-11, gerçek canlı veride tespit edildi): aynı geniş "tasimacilik-nakliye"/
# "otomotiv-yan-sanayi" rehber kategorileri altında "Car Lease Rent A Car" (araç kiralama, skor
# 65!) ve "Menderes Kiralık Asansör Mobilya Asansörü" (mobilya/eşya taşıma asansörü kiralama,
# yedek parçayla hiç ilgisi yok, skor 65!) gibi TAMAMEN alakasız firmalar üst sıralara çıktı.
# Kök neden: bu liste önceden sadece "evden eve nakliyat/turizm" örneklerine göre kurulmuştu,
# aynı kategori altındaki diğer alakasız iş kollarını (araç/ekipman kiralama, sigorta, muayene,
# sürücü kursu, ikinci el galeri) kapsamıyordu. Bu liste bir kere genişletildiği için düzeltme
# TÜM şehirler/kaynaklar için aynı anda geçerli olur - şehir başına ayrı ayrı uğraşmaya gerek yok.
_EXCLUDE_KEYWORDS = [
    "evden eve", "turizm", "seyahat", "travel", "tur operatör",
    "rent a car", "rent-a-car", "araç kiralama", "arac kiralama", "oto kiralama",
    "asansör", "asansor",
    "sürücü kursu", "surucu kursu", "ehliyet kursu",
    "araç muayene", "arac muayene", "tüvtürk", "tuvturk",
    "sigorta", "kasko",
    "ekspertiz",
    "oto galeri", "araba galerisi",
    "detaylı temizlik", "detayli temizlik", "oto kuaför", "oto kuafor",
    "emlak", "gayrimenkul",
]
_EXCLUDE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _EXCLUDE_KEYWORDS) + r")\b"
)

# Bir işletme adı hem "tamir/servis" hem "yıkama" sinyali taşıyorsa (örn. "Atak Lastik Tamiri ve
# Oto Yıkama") bu küçük ölçekli, perakende/işçilik odaklı bir esnaf dükkanıdır - toptan parça
# alıcısı değildir. Bare "yıkama" tek başına evrensel dışlama listesine KONULMADI çünkü büyük bir
# yedek parça/lojistik firmasının adında yan hizmet olarak geçebilir (yanlış pozitif riski); bu
# yüzden sadece tamir/servis sinyaliyle BİRLİKTE göründüğünde anlamlı sayılır.
_WASH_KEYWORDS_RE = re.compile(r"\b(yıkama|yikama|oto\s*kuaför|oto\s*kuafor)\b")

# İşletme sahibinin (baba) oğluna WhatsApp'ta AÇIKÇA belirttiği hedef kitle tanımı: "sanayideki
# yedek parçacılar VE tamirciler" reddedildi - hedef "Anadolu'daki toptan ve perakendeciler" ile
# kendi bakımını kendi yapabilen (elektrik/kaporta işini halledebilen), parça STOKLAYAN lojistik/
# filo firmaları. Yani PARÇA SATAN işletmeler hedef, müşterinin aracını tamir eden/işçilik satan
# SERVİS işletmeleri hedef DEĞİL. Rehber kaynaklarının (turkbusinesscenter.com, sanayi sitesi
# rehberleri) kategori etiketi bunu ayırt etmek için kullanılır - "parça" kelimesi geçen bir
# kategori aynı zamanda servis kelimesi içerse bile (örn. "Servis Yedek Parça") parça sattığı
# için hedef kabul edilir, ama SADECE servis/tamir kelimesi geçip parça kelimesi hiç geçmiyorsa
# (örn. "Motor Tamircileri", "Kaporta Boya", "Bakım Servisi") hedef dışı sayılır.
_SERVICE_ONLY_CATEGORY_KEYWORDS = [
    "tamir", "bakım servisi", "bakim servisi", "boya", "kaynak", "torna", "rektefiye",
    "kurtarma", "vinç", "vinc", "montaj", "mekanik",
]
_PARTS_CATEGORY_KEYWORDS = [
    "yedek parça", "yedek parca", "parça", "parca", "aksesuar", "hurdacı", "hurdaci",
    "çıkma", "cikma", "levazımat", "levazimat", "nalbur", "lastik",
]
_SERVICE_CATEGORY_PATTERN = re.compile("|".join(re.escape(k) for k in _SERVICE_ONLY_CATEGORY_KEYWORDS))
_PARTS_CATEGORY_PATTERN = re.compile("|".join(re.escape(k) for k in _PARTS_CATEGORY_KEYWORDS))


_MIN_SOURCE_YEAR = 2024


def _is_stale_source(raw: dict) -> bool:
    """Bazı kaynaklar (örn. Ankara Oto Sanayi Rehberi) her kayıtta açık bir 'X tarihinde
    güncellendi' bilgisi veriyor - bazıları yıllar önce güncellenmiş, işletmenin hâlâ faaliyette
    olduğu garanti değil. Kullanıcı açıkça 2024 öncesi tarihli kayıtların yok sayılmasını istedi.
    Bu bilgi olmayan kaynaklar (OSM, turkbusinesscenter.com, çoğu sanayi sitesi) için bu kontrol
    hiç uygulanmaz - yoksa neredeyse tüm veri (tarih bilgisi hiç sunmadıkları için) cezalanırdı."""
    updated_at = raw.get("source_updated_at", "")
    if not updated_at:
        return False
    try:
        year = int(updated_at.strip()[-4:])
        return year < _MIN_SOURCE_YEAR
    except (ValueError, IndexError):
        return False


def score_lead(raw: dict) -> dict:
    """Kural bazlı skor: OSM'den gelen ham veriyi (isim, kategori, iletişim bilgisi)
    hedef kitle profiline (toptancı/lojistik, bağımsız tamirci DEĞİL) göre puanlar."""
    name_lower = turkish_lower(raw.get("name", ""))
    shop_type = raw.get("shop_type", "")

    keyword_hit = bool(_KEYWORD_PATTERN.search(name_lower))
    is_excluded = bool(_EXCLUDE_PATTERN.search(name_lower))
    is_stale = _is_stale_source(raw)

    if is_stale:
        return {
            "relevance_score": 5,
            "score_breakdown": {"sector_match": 0, "geography": 0, "growth_signal": 0, "data_completeness": 0},
            "score_reasoning": f"Kaynak kaydı {raw.get('source_updated_at', '')} tarihinden kalma (2024 öncesi) - işletmenin hâlâ faaliyette olduğu belirsiz, düşük öncelik.",
            "entity_type_note": "Belirsiz",
            "sector_label": "Eski Kayıt (2024 Öncesi)",
            "phone_is_mobile": is_mobile_phone(raw.get("phone", "")) if raw.get("phone") else False,
        }

    if is_excluded:
        return {
            "relevance_score": 5,
            "score_breakdown": {"sector_match": 0, "geography": 0, "growth_signal": 0, "data_completeness": 0},
            "score_reasoning": "İsminde 'evden eve nakliyat', 'turizm' gibi hedef kitle dışı bir ifade geçiyor (ev eşyası taşımacılığı/seyahat acentesi, toptan parça alıcısı değil).",
            "entity_type_note": "Belirsiz",
            "sector_label": "Hedef Dışı",
            "phone_is_mobile": is_mobile_phone(raw.get("phone", "")) if raw.get("phone") else False,
        }

    category_label_lower = turkish_lower(raw.get("category_label", ""))
    has_parts_signal = bool(_PARTS_CATEGORY_PATTERN.search(category_label_lower)) or keyword_hit

    sector_match = 0
    if shop_type == "car_parts":
        sector_match += 30
    if shop_type == "logistics":
        sector_match += 30
    if shop_type == "tyres":
        # Lastikçi (özellikle ağır vasıta lastikçisi) hem kendi başına potansiyel müşteri hem de
        # genelde diğer parça ihtiyaçlarını da bilen/yönlendiren bir aktör - orta seviye puan.
        sector_match += 20
    if shop_type in ("trailer", "agrarian"):
        # Dorse/treyler ve tarım makinesi bayileri hedef kitlenin tam merkezinde - kendileri de
        # sürekli mekanik/hidrolik parça ihtiyacı olan, ağır vasıta ekosisteminin bir parçası.
        sector_match += 30
    if shop_type == "directory":
        # DEĞİŞTİ (2026-08-11): turkbusinesscenter.com/sanayisitesi.com.tr gibi rehberlerden gelen
        # kategori etiketi ÖNCEDEN körü körüne +30 alıyordu - bu, "Car Lease Rent A Car" ve
        # "Menderes Kiralık Asansör" gibi TAMAMEN alakasız firmaların (kendilerini geniş bir
        # "Taşımacılık/Nakliye" ya da "Otomotiv Yan Sanayi" şemsiyesine kaydetmiş olsalar bile)
        # yüksek skor almasına yol açtı - rehber sitelerin kendi kategorizasyonu firma-beyanlı ve
        # güvenilmez çıktı. Artık taban güven payı düşürüldü (15); TAM güven (30) sadece isimde
        # veya kategori etiketinde GERÇEK bir parça/hedef kitle sinyali (has_parts_signal) varsa
        # veriliyor - yani rehber kaydı TEK BAŞINA artık yeterli değil, ek bir doğrulama istiyor.
        sector_match += 30 if has_parts_signal else 15
    if keyword_hit:
        sector_match += 25

    is_service_only_category = (
        shop_type == "directory"
        and bool(_SERVICE_CATEGORY_PATTERN.search(category_label_lower))
        and not bool(_PARTS_CATEGORY_PATTERN.search(category_label_lower))
    )

    # İsimde hem tamir/servis hem yıkama sinyali birlikte geçiyorsa (bkz. _WASH_KEYWORDS_RE notu)
    # küçük ölçekli bir esnaf dükkanıdır - "Lastikçi" (tyres) etiketiyle gelse bile toptan alıcı
    # değildir. Gerçek bir örnekte tespit edildi: "Atak Lastik Tamiri ve Oto Yıkama" (tyres, +20
    # taban puanla skor 50'ye çıkmıştı).
    is_wash_and_repair_shop = (
        bool(_WASH_KEYWORDS_RE.search(name_lower))
        and bool(_SERVICE_CATEGORY_PATTERN.search(name_lower))
    )

    # Bağımsız tamirci/servis tespiti: OSM'de "car_repair" kategorisinde OLMASI ya da rehber
    # kaynağında saf servis/tamir kategorisinde (parça satışı belirtilmeden) OLMASI, ya da
    # tamir+yıkama birlikte geçen küçük esnaf dükkanı olması, VE toptan/lojistik/filo gibi hiçbir
    # hedef kitle sinyali yoksa hedef dışı say
    is_independent_repair = (
        (shop_type == "car_repair" or is_service_only_category or is_wash_and_repair_shop)
        and not keyword_hit
    )

    phone = raw.get("phone", "")
    phone_is_mobile = is_mobile_phone(phone) if phone else False

    data_completeness = 0
    if phone:
        # Cep telefonu (05XX) tam puan alır - WhatsApp'tan ulaşılabilir. Sabit hat da bir
        # miktar puan alır (telefonla aranabilir) ama çok daha az, çünkü asıl kanalımız WhatsApp.
        data_completeness += 10 if phone_is_mobile else 3
    if raw.get("address"):
        data_completeness += 5
    if raw.get("website"):
        data_completeness += 5

    geography = 20  # il filtresiyle zaten kapsam içinde, sabit puan

    if is_independent_repair:
        relevance_score = min(15, sector_match + data_completeness)
        if is_service_only_category:
            reasoning = f"'{raw.get('category_label', '')}' hizmet/tamir odaklı bir kategori görünüyor (parça satışı belirtilmemiş) - hedef kitle dışı, düşük öncelik."
        else:
            reasoning = "Bağımsız oto tamir servisi görünüyor - hedef kitle dışı (sanayideki tamirciler), düşük öncelik."
        entity_type_note = "Belirsiz"
    else:
        relevance_score = min(100, sector_match + geography + data_completeness)
        if shop_type == "car_parts":
            reasoning = "OSM'de 'oto yedek parça' kategorisinde kayıtlı."
        elif shop_type == "trailer":
            reasoning = "OSM'de 'dorse/treyler satış-servis' kategorisinde kayıtlı - hedef kitlenin tam merkezinde."
        elif shop_type == "agrarian":
            reasoning = "OSM'de 'tarım makinesi ekipman/parça' kategorisinde kayıtlı."
        elif shop_type == "directory":
            reasoning = f"Firma rehberinde '{raw.get('category_label', '')}' kategorisinde kayıtlı."
        elif keyword_hit:
            reasoning = "İsminde nakliye/lojistik/dorse/filo gibi hedef kitle anahtar kelimesi geçiyor."
        else:
            reasoning = "Sektör kategorisi eşleşmesi bulundu, isim bazlı ek doğrulama önerilir."
        entity_type_note = "Belirsiz"

    if phone and not phone_is_mobile:
        reasoning += " (Not: numara sabit hat görünüyor, WhatsApp'tan değil sadece telefonla ulaşılabilir.)"

    if shop_type == "directory":
        sector_label = raw.get("category_label", "Firma Rehberi")
    elif shop_type in SECTOR_LABELS:
        sector_label = SECTOR_LABELS[shop_type]
    elif keyword_hit:
        sector_label = "İsim Eşleşmesi (Nakliye/Lojistik/Filo)"
    else:
        sector_label = "Belirsiz"

    return {
        "relevance_score": relevance_score,
        "score_breakdown": {
            "sector_match": sector_match,
            "geography": geography,
            "growth_signal": 0,
            "data_completeness": data_completeness,
        },
        "score_reasoning": reasoning,
        "entity_type_note": entity_type_note,
        "sector_label": sector_label,
        "phone_is_mobile": phone_is_mobile,
    }

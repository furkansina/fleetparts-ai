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
_EXCLUDE_KEYWORDS = ["evden eve", "turizm", "seyahat", "travel", "tur operatör"]
_EXCLUDE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _EXCLUDE_KEYWORDS) + r")\b"
)


def score_lead(raw: dict) -> dict:
    """Kural bazlı skor: OSM'den gelen ham veriyi (isim, kategori, iletişim bilgisi)
    hedef kitle profiline (toptancı/lojistik, bağımsız tamirci DEĞİL) göre puanlar."""
    name_lower = turkish_lower(raw.get("name", ""))
    shop_type = raw.get("shop_type", "")

    keyword_hit = bool(_KEYWORD_PATTERN.search(name_lower))
    is_excluded = bool(_EXCLUDE_PATTERN.search(name_lower))

    if is_excluded:
        return {
            "relevance_score": 5,
            "score_breakdown": {"sector_match": 0, "geography": 0, "growth_signal": 0, "data_completeness": 0},
            "score_reasoning": "İsminde 'evden eve nakliyat', 'turizm' gibi hedef kitle dışı bir ifade geçiyor (ev eşyası taşımacılığı/seyahat acentesi, toptan parça alıcısı değil).",
            "entity_type_note": "Belirsiz",
            "sector_label": "Hedef Dışı",
            "phone_is_mobile": is_mobile_phone(raw.get("phone", "")) if raw.get("phone") else False,
        }

    sector_match = 0
    if shop_type == "car_parts":
        sector_match += 30
    if shop_type == "logistics":
        sector_match += 30
    if shop_type == "tyres":
        # Lastikçi (özellikle ağır vasıta lastikçisi) hem kendi başına potansiyel müşteri hem de
        # genelde diğer parça ihtiyaçlarını da bilen/yönlendiren bir aktör - orta seviye puan.
        sector_match += 20
    if shop_type == "directory":
        # turkbusinesscenter.com gibi bir B2B firma rehberinden geliyor - sitenin kendisi zaten
        # firmayı "Otomotiv Yedek Parça" veya "Taşımacılık/Nakliye" kategorisine kaydetmiş,
        # bu OSM'in shop=car_parts etiketi kadar güçlü bir sinyal.
        sector_match += 30
    if keyword_hit:
        sector_match += 25

    # Bağımsız tamirci/servis tespiti: sadece "car_repair" kategorisinde ve
    # toptan/lojistik/filo gibi hiçbir hedef kitle sinyali yoksa hedef dışı say
    is_independent_repair = shop_type == "car_repair" and not keyword_hit

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
        reasoning = "Bağımsız oto tamir servisi görünüyor - hedef kitle dışı (sanayideki tamirciler), düşük öncelik."
        entity_type_note = "Belirsiz"
    else:
        relevance_score = min(100, sector_match + geography + data_completeness)
        if shop_type == "car_parts":
            reasoning = "OSM'de 'oto yedek parça' kategorisinde kayıtlı."
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

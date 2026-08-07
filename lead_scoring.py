import re

from provinces import NAME_KEYWORDS_HIGH_VALUE
from lead_dedupe import is_mobile_phone, turkish_lower

# OSM'in ham etiket değerleri (car_parts, yes, convenience vb.) okunabilir değil -
# arayüzde gösterilecek temiz sektör etiketleri buradan gelir
SECTOR_LABELS = {
    "car_parts": "Oto Yedek Parça",
    "logistics": "Lojistik/Nakliye Ofisi",
    "car_repair": "Oto Tamir Servisi",
}

# Anahtar kelimeler kelime sınırıyla (\b) aranır, düz alt-dize (substring) araması DEĞİL.
# Aksi halde örneğin "filo" kelimesi "Profilo" (gerçek bir beyaz eşya markası) gibi tamamen
# alakasız isimlerin içinde de eşleşip yanlış pozitif üretiyordu - gerçek bir örnekte tespit edildi.
_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in NAME_KEYWORDS_HIGH_VALUE) + r")\b"
)


def score_lead(raw: dict) -> dict:
    """Kural bazlı skor: OSM'den gelen ham veriyi (isim, kategori, iletişim bilgisi)
    hedef kitle profiline (toptancı/lojistik, bağımsız tamirci DEĞİL) göre puanlar."""
    name_lower = turkish_lower(raw.get("name", ""))
    shop_type = raw.get("shop_type", "")

    keyword_hit = bool(_KEYWORD_PATTERN.search(name_lower))

    sector_match = 0
    if shop_type == "car_parts":
        sector_match += 30
    if shop_type == "logistics":
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
        elif keyword_hit:
            reasoning = "İsminde nakliye/lojistik/dorse/filo gibi hedef kitle anahtar kelimesi geçiyor."
        else:
            reasoning = "Sektör kategorisi eşleşmesi bulundu, isim bazlı ek doğrulama önerilir."
        entity_type_note = "Belirsiz"

    if phone and not phone_is_mobile:
        reasoning += " (Not: numara sabit hat görünüyor, WhatsApp'tan değil sadece telefonla ulaşılabilir.)"

    if shop_type in SECTOR_LABELS:
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

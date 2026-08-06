from provinces import NAME_KEYWORDS_HIGH_VALUE


def score_lead(raw: dict) -> dict:
    """Kural bazlı skor: OSM'den gelen ham veriyi (isim, kategori, iletişim bilgisi)
    hedef kitle profiline (toptancı/lojistik, bağımsız tamirci DEĞİL) göre puanlar."""
    name_lower = raw.get("name", "").lower()
    shop_type = raw.get("shop_type", "")

    keyword_hit = any(k in name_lower for k in NAME_KEYWORDS_HIGH_VALUE)

    sector_match = 0
    if shop_type == "car_parts":
        sector_match += 30
    if shop_type == "office" or shop_type == "logistics":
        sector_match += 30
    if keyword_hit:
        sector_match += 25

    # Bağımsız tamirci/servis tespiti: sadece "car_repair" kategorisinde ve
    # toptan/lojistik/filo gibi hiçbir hedef kitle sinyali yoksa hedef dışı say
    is_independent_repair = shop_type == "car_repair" and not keyword_hit

    data_completeness = 0
    if raw.get("phone"):
        data_completeness += 10
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
            reasoning = "İsminde nakliye/lojistik/toptan/dorse gibi hedef kitle anahtar kelimesi geçiyor."
        else:
            reasoning = "Sektör kategorisi eşleşmesi bulundu, isim bazlı ek doğrulama önerilir."
        entity_type_note = "Belirsiz"

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
    }

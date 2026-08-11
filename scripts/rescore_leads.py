"""lead_scoring.py'deki kurallar her değiştiğinde (bkz. 2026-08-11 sertleştirmesi) leads.json'daki
TÜM mevcut kayıtları yeni kurallarla yeniden puanlar - haftalık taramanın tekrar çalışmasını
beklemeden, mevcut hatalı kayıtları hemen düzeltir.

İki yol izlenir:
  1. Kayıtta raw_shop_type/raw_category_label VARSA (2026-08-11'den sonra taranmış): score_lead()
     TAM olarak yeniden çalıştırılır - orijinal taramadaki kadar doğru sonuç verir.
  2. Kayıtta bu ham alanlar YOKSA (daha eski kayıt, henüz yeniden taranmamış): sadece isim bazlı
     dışlama deseni (score_lead içindeki _EXCLUDE_PATTERN ve _WASH_KEYWORDS_RE ile aynı mantık)
     uygulanır - ham kategori/tip bilgisi kaybolduğu için tam yeniden puanlama yapılamaz, ama en
     azından "Car Lease Rent A Car" gibi isimden belli olan net hatalar burada da yakalanır.

Kullanım: python scripts/rescore_leads.py [--dry-run]
"""
import sys
import os
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lead_scoring import score_lead, SECTOR_LABELS, _EXCLUDE_PATTERN, _WASH_KEYWORDS_RE, _SERVICE_CATEGORY_PATTERN
from lead_dedupe import turkish_lower, sanitize_phone, is_mobile_phone

LEADS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leads.json")

# turkbusinesscenter.com/sanayi rehberleri gibi kaynaklarda shop_type hep "directory" - bu
# durumda score_lead() sector_label'i DOĞRUDAN category_label olarak kaydediyor. Yani bu
# kaynaklardan gelen eski (raw_category_label'ı kaybolmuş) kayıtlarda bile stored sector_guess,
# o kaydın ORİJİNAL category_label'ının ta kendisi - has_parts_signal'i yeniden hesaplamak için
# kullanılabilir. 2026-08-11 canlı denetiminde tespit edildi: "Otomotiv / Oto Yan Sanayi" gibi
# GENİŞ bir kategori altında bisikletçi, oto yıkama, araç kiralama, halı yıkama, hasarlı araç
# alım-satım gibi tamamen alakasız firmalar da vardı - bunlar has_parts_signal hardening'inden
# (bkz. lead_scoring.py) ÖNCE taranmış, flat +30 ile skorlanmış ve raw veri kaybolduğu için
# sadece isim bazlı düzeltmeyle yakalanamıyorlardı. Bu artık o boşluğu kapatıyor.
_DIRECTORY_SOURCES = {"turkbusinesscenter", "sanayi_sitesi", "sanayisitesi_platform", "find_com_tr"}
_NON_CATEGORY_SECTOR_GUESSES = set(SECTOR_LABELS.values()) | {
    "Hedef Dışı", "Hedef Dışı (isme göre geriye dönük düzeltme)",
    "Eski Kayıt (2024 Öncesi)", "Belirsiz", "İsim Eşleşmesi (Nakliye/Lojistik/Filo)", "",
}


def rescore_legacy_by_name(lead: dict) -> dict | None:
    """Ham girdisi olmayan eski bir kayıt için sadece isme bakarak yeniden değerlendirir.
    Sadece net bir dışlama sinyali varsa (aksi halde dokunmadan None döner - eski skor
    korunur, çünkü ham kategori/tip olmadan POZİTİF yönde bir iyileştirme yapılamaz, sadece
    net kötü olanlar indirilebilir)."""
    name_lower = turkish_lower(lead.get("company_name", ""))
    is_excluded = bool(_EXCLUDE_PATTERN.search(name_lower))
    is_wash_repair = bool(_WASH_KEYWORDS_RE.search(name_lower)) and bool(_SERVICE_CATEGORY_PATTERN.search(name_lower))
    if not (is_excluded or is_wash_repair):
        return None
    if (lead.get("relevance_score") or 0) <= 15:
        return None  # zaten dusuk, dokunmaya gerek yok
    reason = (
        "İsminde hedef kitle dışı bir ifade geçiyor (araç/ekipman kiralama, sigorta, muayene vb.)"
        if is_excluded else
        "İsminde tamir+yıkama birlikte geçiyor - küçük ölçekli esnaf dükkanı görünüyor, toptan alıcı değil."
    )
    return {
        "relevance_score": 5 if is_excluded else 15,
        "sector_guess": "Hedef Dışı (isme göre geriye dönük düzeltme)",
        "score_reasoning": f"[Geriye dönük düzeltme 2026-08-11] {reason}",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(LEADS_FILE, "r", encoding="utf-8") as f:
        leads = json.load(f)

    full_rescored = 0
    category_recovered = 0
    legacy_demoted = 0
    unchanged = 0
    phone_cleaned = 0

    for lead in leads:
        # Telefon temizliği HER kayıtta, skorlama dalından bağımsız uygulanır - çoklu numara
        # ("+90...;+90...") ve sahte/placeholder ("(000) - 0000000") numaralar skor ne olursa
        # olsun panelde bozuk/yanlış bir WhatsApp linkine yol açıyordu (2026-08-11 tespit edildi).
        old_phone = lead.get("phone", "")
        new_phone = sanitize_phone(old_phone)
        if new_phone != old_phone:
            phone_cleaned += 1
            lead["phone"] = new_phone
            lead["phone_is_mobile"] = is_mobile_phone(new_phone) if new_phone else False

        if lead.get("raw_shop_type") is not None:
            raw = {
                "name": lead.get("company_name", ""),
                "shop_type": lead.get("raw_shop_type", ""),
                "category_label": lead.get("raw_category_label", ""),
                "phone": lead.get("phone", ""),
                "address": lead.get("address", ""),
                "website": "",
            }
            scoring = score_lead(raw)
            if scoring["relevance_score"] != lead.get("relevance_score"):
                full_rescored += 1
            lead["relevance_score"] = scoring["relevance_score"]
            lead["score_breakdown"] = scoring["score_breakdown"]
            lead["score_reasoning"] = scoring["score_reasoning"]
            lead["sector_guess"] = scoring["sector_label"]
            lead["phone_is_mobile"] = scoring["phone_is_mobile"]
        elif (
            lead.get("source") in _DIRECTORY_SOURCES
            and (lead.get("sector_guess") or "") not in _NON_CATEGORY_SECTOR_GUESSES
        ):
            # sector_guess, bu kaynaklarda orijinal category_label'ın ta kendisi (bkz. üstteki not)
            raw = {
                "name": lead.get("company_name", ""),
                "shop_type": "directory",
                "category_label": lead.get("sector_guess", ""),
                "phone": lead.get("phone", ""),
                "address": lead.get("address", ""),
                "website": "",
            }
            scoring = score_lead(raw)
            if scoring["relevance_score"] != lead.get("relevance_score"):
                category_recovered += 1
            lead["relevance_score"] = scoring["relevance_score"]
            lead["score_breakdown"] = scoring["score_breakdown"]
            lead["score_reasoning"] = scoring["score_reasoning"]
            # sector_guess KASITLI OLARAK değiştirilmiyor - zaten orijinal category_label,
            # score_lead() ne olsa aynısını sector_label olarak geri döndürür.
            lead["phone_is_mobile"] = scoring["phone_is_mobile"]
        else:
            patch = rescore_legacy_by_name(lead)
            if patch:
                lead.update(patch)
                legacy_demoted += 1
            else:
                unchanged += 1

    print(f"Telefonu temizlenen (çoklu/sahte/kesik numara düzeltildi): {phone_cleaned}")
    print(f"Tam yeniden puanlanan (ham veri mevcut): {full_rescored}")
    print(f"Kategori metninden kurtarılıp yeniden puanlanan (sector_guess -> category_label): {category_recovered}")
    print(f"İsme göre geriye dönük indirilen (eski kayıt, ham veri yok): {legacy_demoted}")
    print(f"Değişmeyen: {unchanged}")
    print(f"Toplam kayıt: {len(leads)}")

    if args.dry_run:
        print("\n--dry-run: leads.json'a yazılmadı.")
        return

    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=4)
    print(f"\n{LEADS_FILE} güncellendi.")


if __name__ == "__main__":
    main()

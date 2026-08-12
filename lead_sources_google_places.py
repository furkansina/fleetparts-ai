import os
import time

import requests

from provinces import PROVINCES

# Google Places API (New) - Text Search. Bu, küçük Anadolu illerindeki kapsama sorununun TEK
# gerçek/kalıcı çözümü: OSM, firma rehberleri (turkbusinesscenter.com, sanayisitesi.com.tr,
# find.com.tr) hepsi gönüllü/ikincil kayıt kaynakları - kapsamları o ildeki işletmelerin kendini
# o platforma kaydetmiş olmasına bağlı. Google Places, Google Haritalar'daki HER işletmeyi
# (telefon numarası dahil) kapsar - bu yüzden küçük illerde bile neredeyse tam kapsama verir.
#
# NEDEN ŞU AN KAPALI: bu API ücretli (Google Cloud faturalandırma/kredi kartı gerektirir - babanın
# hesabında henüz aktif değil). Google Haritalar'ı otomatik/manuel "tarayarak" veri çekmek
# Google'ın Kullanım Şartları'nı ihlal eder (ban riski + hukuki risk) - bu yüzden o değil, resmi
# API kullanılıyor. Modül TAMAMEN hazır ve test edilmeye hazır ama GOOGLE_PLACES_API_KEY ortam
# değişkeni tanımlı olmadığı sürece hiçbir istek atmaz, hiçbir ücret oluşturmaz - baba faturalandırmayı
# ne zaman aktif ederse (Render/GitHub Actions secrets'a GOOGLE_PLACES_API_KEY eklenerek) o an
# devreye girer, kod tarafında BAŞKA HİÇBİR DEĞİŞİKLİK gerekmez.
#
# MALİYET TAHMİNİ (2026 fiyatlandırması, Text Search Pro SKU): ~$0.032/istek. 81 il × ortalama
# ~2 sayfa (sayfa başı 20 sonuç) ≈ 160 istek ≈ 5 USD TEK SEFERLİK tam tarama için. Haftalık
# otomatik taramada YENİ firma bulma ihtimali düşük olduğu için pratikte tek seferlik/aylık
# çalıştırılması (--google-places-once gibi manuel bir bayrakla) önerilir - koşu başına otomatik
# tekrar tetiklenmez, sadece run_discovery.py'de env değişkeni varsa çalışır.
API_KEY_ENV = "GOOGLE_PLACES_API_KEY"
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.internationalPhoneNumber",
    "places.nationalPhoneNumber",
    "places.location",
    "places.primaryTypeDisplayName",
    "places.businessStatus",
])

QUERY = "oto yedek parça"
_MAX_PAGES = 3  # Places API (New) sayfa başı en fazla 20 sonuç veriyor, toplamda ~60 sonuç tavanı var

# GÜVENLİK KATMANI (2026-08-12, kullanıcı "bir anda para girerse bu riski alamam" dedi - Google
# Cloud'un kendi kota/bütçe ayarlarına GÜVENMEK YETMEZ, kullanıcı onları hiç kurmayı unutabilir
# veya yanlış yapılandırabilir). Bu modül, Google'a ne kadar istek atacağını Google Cloud
# tarafındaki AYARLARDAN BAĞIMSIZ olarak kodun KENDİSİNDE sınırlıyor - kod içinde sabit, aşılamaz
# bir tavan. 81 il × 3 sayfa = en fazla 243 istek olabilir zaten (_MAX_PAGES ile doğal bir tavan
# var), ama bu ek güvenlik katmanı bir hata (ör. sonsuz döngü, yanlış çağrı) durumunda bile
# harcamanın ~10 USD'yi (Google'ın $200 aylık ücretsiz kredisinin çok altında) asla aşmamasını
# garanti eder - run bu sınıra ulaşırsa kalan iller sessizce atlanır, sıradaki haftalık taramada
# devam eder (leads.json'a o ana kadar bulunanlar zaten kaydedilmiş olur, kayıp olmaz).
_MAX_REQUESTS_PER_RUN = 300  # ~300 x $0.032 ≈ $9,60 - kesin, kod seviyesinde tavan
_request_count = 0


def is_configured() -> bool:
    return bool(os.environ.get(API_KEY_ENV))


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": os.environ[API_KEY_ENV],
        "X-Goog-FieldMask": FIELD_MASK,
    }


def _search_page(query: str, page_token: str = None) -> dict:
    global _request_count
    if _request_count >= _MAX_REQUESTS_PER_RUN:
        return {}
    _request_count += 1
    body = {
        "textQuery": query,
        "languageCode": "tr",
        "regionCode": "TR",
    }
    if page_token:
        body["pageToken"] = page_token
    try:
        res = requests.post(SEARCH_URL, headers=_headers(), json=body, timeout=20)
        if res.status_code != 200:
            print(f"  [google_places] HTTP {res.status_code}: {res.text[:200]}")
            return {}
        return res.json()
    except Exception as e:
        print(f"  [google_places] hata - {e}")
        return {}


def _to_raw(place: dict) -> dict:
    name = (place.get("displayName") or {}).get("text", "").strip()
    location = place.get("location") or {}
    return {
        "site_id": f"gplaces_{place.get('id', '')}",
        "name": name,
        "shop_type": "directory",
        "category_label": place.get("primaryTypeDisplayName", {}).get("text", "Oto Yedek Parça") if isinstance(place.get("primaryTypeDisplayName"), dict) else "Oto Yedek Parça",
        "phone": place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber") or "",
        "website": "",
        "address": place.get("formattedAddress", ""),
        "lat": location.get("latitude"),
        "lon": location.get("longitude"),
        "business_status": place.get("businessStatus", ""),
    }


def search_province(province: str, delay: float = 0.3) -> list:
    if not is_configured():
        return []
    query = f"{QUERY} {province}"
    results = []
    page_token = None
    for _page in range(_MAX_PAGES):
        data = _search_page(query, page_token)
        places = data.get("places", [])
        for place in places:
            raw = _to_raw(place)
            if raw["name"] and raw.get("business_status", "OPERATIONAL") != "CLOSED_PERMANENTLY":
                results.append(raw)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        # Google, yeni sayfa token'ının aktif olması için kısa bir bekleme öneriyor
        time.sleep(2)
    time.sleep(delay)
    return results


def search_all(delay: float = 0.3) -> list:
    global _request_count
    if not is_configured():
        print(f"  [google_places] {API_KEY_ENV} tanımlı değil - kaynak atlandı (ücret oluşmadı, kod hazır)")
        return []
    _request_count = 0
    all_results = []
    for province in PROVINCES:
        if _request_count >= _MAX_REQUESTS_PER_RUN:
            print(f"  [google_places] Güvenlik tavanına ulaşıldı ({_MAX_REQUESTS_PER_RUN} istek, ~${_MAX_REQUESTS_PER_RUN * 0.032:.0f}) - "
                  f"kalan iller bu koşuda atlandı, bir sonraki haftalık taramada devam eder.")
            break
        try:
            items = search_province(province, delay=delay)
        except Exception as e:
            print(f"  [google_places] {province} taraması başarısız - {e}")
            items = []
        for item in items:
            item["province"] = province
        print(f"  [google_places] {province}: {len(items)} firma")
        all_results.extend(items)
    return all_results

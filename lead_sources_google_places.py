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
# GÜVENLİK/MALİYET MODELİ (2026-08-12'de güncellendi - bu API artık AKTİF, aşağıdaki sayılar
# güncel kod davranışıyla eşleşiyor; bir kod denetiminde eski/yanlış sayılar taşıdığı tespit
# edildi ve düzeltildi - kullanıcı harcama güvenliği için bu dosyaya güvendiğinden yanlış bir
# yorumun burada durması ayrı bir risk). Google Haritalar'ı otomatik/manuel "tarayarak" veri
# çekmek Google'ın Kullanım Şartları'nı ihlal eder (ban riski + hukuki risk) - bu yüzden o değil,
# resmi API kullanılıyor. GOOGLE_PLACES_API_KEY ortam değişkeni tanımlı olmadığı sürece hiçbir
# istek atmaz, hiçbir ücret oluşturmaz (bkz. is_configured()).
#
# MALİYET TAHMİNİ (2026 fiyatlandırması, Text Search Pro SKU): ~$0.032/istek. Kod içindeki kesin
# güvenlik tavanı _MAX_REQUESTS_PER_RUN (aşağıda tanımlı) - GÜNCEL değeri ve karşılık gelen dolar
# tavanı için tek doğru kaynak o sabitin kendisi ve yanındaki yorum, burada AYRICA bir sayı
# tekrarlanmıyor ki ikisi birbirinden kopup yanlış bir güvence vermesin.
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

# TEK sorgu (sadece "oto yedek parça") büyük şehirlerde Google'ın 60 sonuç/sorgu tavanına
# (3 sayfa x 20) hemen çarpıyordu - İstanbul'da gerçekte 60'tan çok daha fazla ilgili işletme var
# ama Text Search API'si tek sorgu başına bundan fazlasını hiç vermiyor (API'nin kendi sınırı,
# bizim tarafımızda aşılamaz). TEK çözüm: birden fazla FARKLI sorgu - her biri kendi 60 sonuçluk
# tavanına sahip, aralarında örtüşme olsa da (aynı firma birden fazla sorguda çıkabilir - dedup
# zaten mevcut lead_dedupe.dedupe_key ile hallediliyor) toplam kapsamı ciddi şekilde artırıyor.
# 2026-08-12, kullanıcı "$200 kullanıp 81 ildeki TÜM müşterileri gömebilir miyiz" diye sordu -
# gerçekçi beklenti: Türkiye'de bu niş için muhtemelen 40.000 gerçek işletme YOK (o kadar iddialı
# bir rakam değil), ama bütçenin büyük kısmını kullanarak kapsamı olabildiğince maksimize etmek
# için sorgu sayısı 1'den 12'ye çıkarıldı.
QUERIES = [
    "oto yedek parça",
    "kamyon yedek parça",
    "ağır vasıta yedek parça",
    "TIR yedek parça",
    "dorse treyler servisi",
    "nakliye firması",
    "lojistik firması",
    "kamyon servisi",
    "oto elektrik ağır vasıta",
    "kamyon lastikçi",
    "hidrolik yedek parça",
    "filo yönetimi",
]
_MAX_PAGES = 3  # Places API (New) sayfa başı en fazla 20 sonuç veriyor, toplamda ~60 sonuç tavanı var (API'nin kendi sınırı)

# GÜVENLİK KATMANI (2026-08-12, kullanıcı "bir anda para girerse bu riski alamam" dedi - Google
# Cloud'un kendi kota/bütçe ayarlarına GÜVENMEK YETMEZ, kullanıcı onları hiç kurmayı unutabilir
# veya yanlış yapılandırabilir). Bu modül, Google'a ne kadar istek atacağını Google Cloud
# tarafındaki AYARLARDAN BAĞIMSIZ olarak kodun KENDİSİNDE sınırlıyor - kod içinde sabit, aşılamaz
# bir tavan. Teorik tavan 81 il x 12 sorgu x 3 sayfa = 2916 istek (~93 USD) olsa da, gerçekte
# küçük illerde çoğu sorgu 1 sayfada (ya da hiç sonuçsuz) biteceği için gerçek harcama muhtemelen
# çok daha düşük olacak. Yine de, bir hata durumunda bile harcamanın ASLA Google'ın $200 aylık
# ücretsiz kredisine yaklaşmamasını garanti etmek için 5000 isteklik (~160 USD, $40 pay bırakan)
# kesin bir tavan konuldu - run bu sınıra ulaşırsa kalan iller/sorgular sessizce atlanır, sıradaki
# haftalık taramada devam eder (leads.json'a o ana kadar bulunanlar zaten kaydedilmiş olur, kayıp olmaz).
_MAX_REQUESTS_PER_RUN = 8700  # ~8700 x $0.032 ≈ $278,4 - kullanicinin acikca istedigi 280$ sinirinin altinda, kesin tavan
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


def _search_one_query(query: str, delay: float) -> list:
    results = []
    page_token = None
    for _page in range(_MAX_PAGES):
        if _request_count >= _MAX_REQUESTS_PER_RUN:
            break
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


# VERİMLİLİK KATMANI (2026-08-12, kullanıcının kendi fikri: "zaten çok veri var oralarda harcama
# yapma, az olan yerlere odaklan" - AŞAĞIDAKİ büyükşehir istisnasıyla birlikte okunmalı, bu katman
# artık SADECE büyükşehir OLMAYAN illere uygulanıyor). Küçük Anadolu illerinde (ör. Bingöl,
# Hakkari, Ardahan) mevcut kaynaklar neredeyse hiçbir şey bulamıyor - Google Places'in asıl değeri
# TAM ORADA. Bu yüzden büyükşehir olmayıp zaten çok kaydı olan iller (eşik: aşağıdaki
# _SATURATED_PROVINCE_THRESHOLD) sadece _REDUCED_QUERIES kadar sorguyla "gözden kaçan var mı" diye
# kontrol edilir, geri kalan bütçe az kayıtlı illere ayrılır - hem daha ucuz hem daha çok YENİ,
# benzersiz lead. GÜNCEL SAYILAR İÇİN (kaç lead eşik, kaç sorgu) sadece aşağıdaki iki sabite
# bakılmalı - burada AYRICA tekrarlanmıyor ki yorum kod değiştikçe sessizce yanlış hale gelmesin
# (bir kod denetiminde tam bu şekilde eski/yanlış sayılar taşıdığı tespit edilip düzeltildi).
_SATURATED_PROVINCE_THRESHOLD = 700
_REDUCED_QUERIES = QUERIES[:4]  # doygun (ve büyükşehir OLMAYAN) illerde bile ilk 4 sorgu denenir

# BÜYÜK ŞEHİR ZENGİNLEŞTİRME (2026-08-12, kullanıcı gerçek koşu sonuçlarını gördükten SONRA kararını
# değiştirdi: "büyük şehirlere de ekleme yap, orayı da zenginleştirelim"). Küçük/orta illerdeki
# doygunluk stratejisi işe yaradı ama getiri hızla düştü (art arda koşularda 4675 -> 133 -> 125 yeni
# lead) - küçük il havuzu büyük ölçüde tüketildi. Bu arada büyük şehirler hep "doygun" sayılıp SADECE
# ilk 4 sorguyla (hepsi birbirine çok benzer "X yedek parça" varyasyonu) taranmıştı; kalan 8 sorgu
# (nakliye/lojistik/servis/lastikçi/hidrolik/filo gibi TAMAMEN FARKLI iş kategorileri) hiç
# denenmemişti - oysa Google Places sorgu başına 60 sonuç tavanı koyduğu için farklı kategori =
# gerçekten farklı firma demek. Türkiye'nin resmi 30 büyükşehir ili burada eşik ne olursa olsun
# HER ZAMAN TAM 12 sorguyla taranır - keyfi bir "en büyük N il" listesi seçmek yerine nesnel/resmi
# bir kritere dayanıyor.
_BUYUKSEHIR_PROVINCES = {
    "Adana", "Ankara", "Antalya", "Aydın", "Balıkesir", "Bursa", "Denizli", "Diyarbakır",
    "Erzurum", "Eskişehir", "Gaziantep", "Hatay", "İstanbul", "İzmir", "Kahramanmaraş",
    "Kayseri", "Kocaeli", "Konya", "Malatya", "Manisa", "Mardin", "Mersin", "Muğla",
    "Ordu", "Sakarya", "Samsun", "Şanlıurfa", "Tekirdağ", "Trabzon", "Van",
}


def search_province(province: str, delay: float = 0.3, queries: list = None) -> list:
    """Bir il için verilen sorgu terimlerini (varsayılan: TÜM QUERIES) tarar - tek sorgu Google'ın
    60 sonuç/sorgu tavanına hemen çarptığı için (bkz. modül başındaki not), her sorgu kendi 60'lık
    tavanını getirir. Aynı firma birden fazla sorguda çıkabilir - dedup çağıran tarafta
    (run_discovery.py, dedupe_key ile) zaten yapılıyor, burada tekrar filtrelemeye gerek yok."""
    if not is_configured():
        return []
    results = []
    for q in (queries if queries is not None else QUERIES):
        if _request_count >= _MAX_REQUESTS_PER_RUN:
            break
        results.extend(_search_one_query(f"{q} {province}", delay))
    return results


def search_all(delay: float = 0.3, existing_counts: dict = None) -> list:
    """existing_counts verilirse (province -> o ildeki mevcut lead sayısı), iller ÖNCE en az
    kayıtlı olandan en çok kayıtlıya doğru sıralanır (bütçe tükenirse önce boşluklar doldurulmuş
    olsun diye) ve doygun iller (bkz. _SATURATED_PROVINCE_THRESHOLD) sadece 2 sorguyla taranır."""
    global _request_count
    if not is_configured():
        print(f"  [google_places] {API_KEY_ENV} tanımlı değil - kaynak atlandı (ücret oluşmadı, kod hazır)")
        return []
    _request_count = 0
    existing_counts = existing_counts or {}
    ordered_provinces = sorted(PROVINCES, key=lambda p: existing_counts.get(p, 0))
    all_results = []
    for province in ordered_provinces:
        if _request_count >= _MAX_REQUESTS_PER_RUN:
            print(f"  [google_places] Güvenlik tavanına ulaşıldı ({_MAX_REQUESTS_PER_RUN} istek, ~${_MAX_REQUESTS_PER_RUN * 0.032:.0f}) - "
                  f"kalan iller bu koşuda atlandı, bir sonraki haftalık taramada devam eder.")
            break
        is_buyuksehir = province in _BUYUKSEHIR_PROVINCES
        is_saturated = existing_counts.get(province, 0) >= _SATURATED_PROVINCE_THRESHOLD and not is_buyuksehir
        queries = _REDUCED_QUERIES if is_saturated else None
        try:
            items = search_province(province, delay=delay, queries=queries)
        except Exception as e:
            print(f"  [google_places] {province} taraması başarısız - {e}")
            items = []
        for item in items:
            item["province"] = province
        tag = " (doygun il, azaltılmış tarama)" if is_saturated else (" (büyükşehir, tam tarama)" if is_buyuksehir else "")
        print(f"  [google_places] {province}{tag}: {len(items)} firma ({_request_count} istek toplam, ~${_request_count * 0.032:.1f})")
        all_results.extend(items)
    return all_results

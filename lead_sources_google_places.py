import os
import time
import json
import base64
from datetime import datetime, timezone

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

# GÜVENLİK KATMANI - TEK KOŞU (2026-08-12, kullanıcı "bir anda para girerse bu riski alamam" dedi).
# Bu, tek bir çalıştırmanın (bir hata/sonsuz döngü durumunda bile) aşamayacağı bir tavan - ama
# BAŞLI BAŞINA kullanıcının 280$ tavanını GARANTİ ETMEZ, çünkü bu sayaç her yeni süreç başında
# (her haftalık GitHub Actions koşusunda) sıfırlanıyor. GERÇEK, KOŞULAR ARASI KÜMÜLATİF tavan
# aşağıdaki "BÜTÇE KATMANI - KÜMÜLATİF" bölümünde.
_MAX_REQUESTS_PER_RUN = 8700  # ~8700 x $0.032 ≈ $278,4 - TEK koşu için tavan, kümülatif tavan değil
_request_count = 0

# BÜTÇE KATMANI - KÜMÜLATİF (2026-08-17'de eklendi, GERÇEK BİR PARA RİSKİ tespit edildiği için):
# BUG - yukarıdaki _MAX_REQUESTS_PER_RUN modül seviyesinde bir Python değişkeni, her YENİ süreç
# (her haftalık GitHub Actions koşusu ayrı bir süreçtir) onu sıfırdan başlatır. Yani kod SADECE
# "bu TEK koşu 278$'ı aşamaz" diyordu, "TÜM ZAMANLARDA TOPLAM 280$'ı aşamaz" DEMİYORDU - kullanıcının
# asıl istediği kesinlikle ikincisiydi. Sonuç: haftalık otomatik tarama her Pazartesi ~$25-31
# harcıyor (30 büyükşehir ili HER HAFTA, aynı 12 sorguyla, doygunluk kontrolünden muaf olduğu için
# tam olarak yeniden taranıyor - yeni işletme oranı haftada haftaya neredeyse hiç değişmediği için
# bu harcamanın neredeyse tamamı önceki haftayla AYNI firmaları tekrar bulup çöpe atıyor, dedupe
# zaten var). Bu tespit edilene kadar GERÇEKTEN ~$131,6 harcanmıştı (6 farklı koşuda) - kullanıcının
# 280$'lık tavanının %47'si, kod bunun FARKINDA bile değildi. Bu tempoda (haftada ~$31) tavan
# yaklaşık 4-5 hafta içinde (Eylül sonu civarı) aşılırdı. Artık GitHub'a yedeklenen kalıcı bir
# durum dosyası (aşağıda) TÜM koşulardaki gerçek harcamayı topluyor ve kalan bütçe HARD CAP'in
# altına düşerse (güvenlik payı bırakarak) bu modül YENİ İSTEK ATMAYI TAMAMEN REDDEDİYOR - süreç
# yeniden başlasa bile. Ayrıca aynı ili her hafta yeniden taramanın israfını önlemek için, bir il
# en fazla _PROVINCE_COOLDOWN_DAYS günde bir yeniden taranıyor (büyükşehir dahil).
_CUMULATIVE_HARD_CAP_USD = 260.0  # kullanıcının 280$ tavanının altında, ~20$ güvenlik payı
_PROVINCE_COOLDOWN_DAYS = 21  # bir il en fazla 3 haftada bir yeniden taranır (aynı firmaları tekrar tekrar aramamak için)
_BUDGET_STATE_FILE = "google_places_budget.json"
_COST_PER_REQUEST = 0.032


def _load_budget_state() -> dict:
    """catalog.json/leads.json ile aynı desen: GitHub'dan canlı okur, ulaşılamazsa diskteki son
    bilinen hale döner. Hiç yoksa (ilk çalıştırma) sıfırdan başlar."""
    github_repo = os.environ.get("GITHUB_REPO", "")
    github_branch = os.environ.get("GITHUB_BRANCH", "main")
    if github_repo:
        try:
            url = f"https://raw.githubusercontent.com/{github_repo}/{github_branch}/{_BUDGET_STATE_FILE}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    try:
        with open(_BUDGET_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"total_requests": 0, "total_spend_estimate": 0.0, "province_last_scanned": {}, "history": []}


def _save_and_sync_budget_state(state: dict):
    with open(_BUDGET_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        print("  [google_places] UYARI: GITHUB_TOKEN/GITHUB_REPO yok - bütçe durumu yedeklenemedi, bir sonraki koşu bu harcamayı bilmeyebilir.")
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    api_url = f"https://api.github.com/repos/{repo}/contents/{_BUDGET_STATE_FILE}"
    with open(_BUDGET_STATE_FILE, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")
    for attempt in range(3):
        try:
            sha = None
            res = requests.get(api_url, headers=headers, timeout=15)
            if res.status_code == 200:
                sha = res.json().get("sha")
            body = {"message": "Google Places kümülatif bütçe durumu güncelleme", "content": content_b64}
            if sha:
                body["sha"] = sha
            put_res = requests.put(api_url, headers=headers, json=body, timeout=15)
            if put_res.status_code in (200, 201):
                return
            if put_res.status_code == 409 and attempt < 2:
                continue
            print(f"  [google_places] UYARI: bütçe durumu GitHub'a yazılamadı (HTTP {put_res.status_code}) - bir sonraki koşu bu harcamayı bilmeyebilir.")
            return
        except Exception as e:
            print(f"  [google_places] bütçe senkronizasyon hatası (deneme {attempt+1}/3): {e}")


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
        # BUG (2026-08-13'te bir kod denetiminde tespit edildi): Google bir işletme için tip
        # bilgisi DÖNDÜRMEDİĞİNDE eskiden burası "Oto Yedek Parça" diye UYDURUYORDU - bu da
        # lead_scoring.py'deki has_parts_signal kontrolünü (category_label içinde "parça" arıyor)
        # yanlışlıkla tetikleyip "kanıtsız rehber kaydı için düşük taban" sertleştirmesini (2026-08-11'de
        # eklenmişti) tam da bu kaynak için geçersiz kılıyordu - tipi bilinmeyen bir işletme, gerçek
        # bir sinyal yokken sanki doğrulanmış "parça" kategorisindeymiş gibi yüksek skor alıyordu.
        # Artık tip yoksa boş string bırakılıyor - skorlama gerçek sinyal eksikliğini doğru görüyor.
        "category_label": place.get("primaryTypeDisplayName", {}).get("text", "") if isinstance(place.get("primaryTypeDisplayName"), dict) else "",
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
    olsun diye) ve doygun iller (bkz. _SATURATED_PROVINCE_THRESHOLD) sadece 2 sorguyla taranır.

    2026-08-17'den itibaren KÜMÜLATİF bütçe (bkz. modül başındaki not) ve il başına
    _PROVINCE_COOLDOWN_DAYS soğuma süresi de burada uygulanıyor - kalıcı durum GitHub'dan okunur,
    her ilin son taranma tarihi ve TÜM zamanlardaki gerçek harcama buradan gelir."""
    global _request_count
    if not is_configured():
        print(f"  [google_places] {API_KEY_ENV} tanımlı değil - kaynak atlandı (ücret oluşmadı, kod hazır)")
        return []

    budget_state = _load_budget_state()
    spent_so_far = float(budget_state.get("total_spend_estimate", 0.0))
    remaining_budget = _CUMULATIVE_HARD_CAP_USD - spent_so_far
    if remaining_budget <= 0:
        print(f"  [google_places] KÜMÜLATİF GÜVENLİK TAVANINA ULAŞILDI: bugüne kadar toplam ~${spent_so_far:.1f} harcandı "
              f"(tavan: ${_CUMULATIVE_HARD_CAP_USD:.0f}) - bu kaynak TAMAMEN devre dışı, kullanıcı onayı olmadan tekrar açılmamalı.")
        return []

    # Bu koşuda harcanabilecek ÜST sınır: tek-koşu tavanı VE kalan kümülatif bütçeden hangisi
    # daha düşükse ona uyulur - ikisi birlikte, ilki tek bir koşunun kontrolsüz büyümesini,
    # ikincisi TÜM zamanlardaki toplam harcamanın kullanıcının tavanını aşmasını engeller.
    effective_run_cap = min(_MAX_REQUESTS_PER_RUN, int(remaining_budget / _COST_PER_REQUEST))
    print(f"  [google_places] Kümülatif harcama: ~${spent_so_far:.1f} / ${_CUMULATIVE_HARD_CAP_USD:.0f} "
          f"(kalan ~${remaining_budget:.1f}) - bu koşuda en fazla {effective_run_cap} istek atılabilir.")

    _request_count = 0
    existing_counts = existing_counts or {}
    province_last_scanned = budget_state.setdefault("province_last_scanned", {})
    now = datetime.now(timezone.utc)

    def _is_in_cooldown(province: str) -> bool:
        last = province_last_scanned.get(province)
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            return False
        return (now - last_dt).days < _PROVINCE_COOLDOWN_DAYS

    ordered_provinces = sorted(PROVINCES, key=lambda p: existing_counts.get(p, 0))
    all_results = []
    skipped_cooldown = 0
    for province in ordered_provinces:
        if _request_count >= effective_run_cap:
            print(f"  [google_places] Bu koşunun tavanına ulaşıldı ({_request_count} istek, ~${_request_count * _COST_PER_REQUEST:.1f}) - "
                  f"kalan iller bu koşuda atlandı, bir sonraki haftalık taramada devam eder.")
            break
        if _is_in_cooldown(province):
            skipped_cooldown += 1
            continue
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
        province_last_scanned[province] = now.isoformat()
        tag = " (doygun il, azaltılmış tarama)" if is_saturated else (" (büyükşehir, tam tarama)" if is_buyuksehir else "")
        print(f"  [google_places] {province}{tag}: {len(items)} firma ({_request_count} istek toplam, ~${_request_count * _COST_PER_REQUEST:.1f})")
        all_results.extend(items)

    if skipped_cooldown:
        print(f"  [google_places] {skipped_cooldown} il son {_PROVINCE_COOLDOWN_DAYS} gün içinde zaten tarandığı için bu koşuda atlandı (gereksiz tekrar taramayı önler).")

    # Bu koşunun gerçek harcamasını kalıcı duruma ekle ve GitHub'a yedekle - bir sonraki koşu
    # (süreç yeniden başlasa bile) bunu bilsin diye.
    run_spend = _request_count * _COST_PER_REQUEST
    budget_state["total_requests"] = budget_state.get("total_requests", 0) + _request_count
    budget_state["total_spend_estimate"] = spent_so_far + run_spend
    budget_state.setdefault("history", []).append({
        "date": now.isoformat(), "requests": _request_count, "spend_estimate": round(run_spend, 2),
    })
    _save_and_sync_budget_state(budget_state)
    print(f"  [google_places] Bu koşuda harcanan: ~${run_spend:.1f} | YENİ KÜMÜLATİF TOPLAM: ~${budget_state['total_spend_estimate']:.1f} / ${_CUMULATIVE_HARD_CAP_USD:.0f}")

    return all_results

import requests

# Birden fazla ücretsiz Overpass aynası - biri yavaş/meşgulse diğeri denenir.
# NOT: "overpass.osm.ch" kasıtlı olarak burada YOK - gerçek bir testte tespit edildi ki bu ayna
# Türkiye verisi için "başarılı" (HTTP 200) ama HER ZAMAN BOŞ sonuç dönüyor (İstanbul'da bile
# 0 sonuç, <1sn'de) - muhtemelen Türkiye'nin idari alan sınırlarını indekslememiş sınırlı bir
# ayna. Bu, sessizce "o ilde hiç firma yok" gibi yanlış bir sonuca yol açıp veri kaybına sebep
# oluyordu - listede tutmak aramanın kalitesini bozardı.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_HEADERS = {"Accept": "*/*", "User-Agent": "fleetparts-lead-discovery/1.0"}
# NOT: "toptan" (wholesale) kasıtlı olarak burada YOK - tek başına çok genel, market/tekstil/gıda
# gibi alakasız toptancıları da OSM sonuçlarına dahil edip gürültü üretiyordu (örn. "Bizim Toptan Market").
# "otobüs" ve "iş makine" işletmenin kendi hedef araç kapsamında (ağır vasıta, tır, kamyon,
# iş makinesi ve otobüs) olmasına rağmen aramaya hiç dahil edilmemişti, eklendi.
# NOT: "tır" DENENDİ ve KALDIRILDI - Overpass'ın regex'i kelime sınırı koruması olmadan çalıştığı
# için "Çamlıktır", "Tırkaz" gibi Türkçe'de son derece yaygın "-tır" eki/heceyle biten/başlayan
# tamamen alakasız isimleri de yakalayıp gerçek bir taramada gürültü ürettiği tespit edildi.
NAME_REGEX = "nakliye|lojistik|dorse|yedek parça|kamyon|taşımacılık|transport|treyler|otobüs|iş makine"


def build_tag_query(province: str) -> str:
    """Etiket bazlı arama (shop=car_parts vb.) - indeksli olduğu için ucuz/hızlı, büyük
    şehirlerde bile saniyeler içinde döner."""
    # NOT: "shop"=tyres (lastikçi) ve "shop"=car (galeri) ÖNCEDEN kasıtlı dışarıda bırakılmıştı
    # ("gürültü katar" diye) - ama bu, ayıklama işini puanlama sistemi yerine keşif aşamasında
    # yapmaya çalışmaktı ve ham veriyi gereksiz daraltıyordu. Artık dahil ediliyorlar, alakasız
    # olanlar zaten score_lead() ve AI netleştirme aşamasında düşük puan alıp elenecek.
    # NOT: "out ... 300" sonuç tavanı KASITLI OLARAK 2000'e çıkarıldı - İstanbul/Ankara gibi
    # büyük illerde gerçek eşleşme sayısı 300'ü kolayca aşabiliyordu ve eski tavan bunu
    # SESSİZCE kırpıyordu (hata vermeden, sadece son 300'den sonrasını atıyordu) - fark
    # edilmesi zor bir veri kaybı kaynağıydı, artık büyük şehirlerde de tüm sonuçlar dönüyor.
    # "shop"=trailer (dorse/treyler satış+parça+tamir) ve "shop"=agrarian (tarım makinesi
    # parçaları/ekipmanı) eklendi - ikisi de hedef kitledeki "iş makinesi"/"dorse/treyler"
    # segmentine dosdoğru karşılık geliyor, OSM wiki'sinde tanımı net, gürültü riski düşük
    # (galeri/oto yıkama gibi genel/alakasız kategoriler kasıtlı olarak hâlâ dışarıda).
    return (
        f'[out:json][timeout:60];'
        f'area["name"="{province}"]["admin_level"="4"]->.searchArea;'
        f'('
        f'node["shop"="car_parts"](area.searchArea);'
        f'way["shop"="car_parts"](area.searchArea);'
        f'node["shop"="car_repair"](area.searchArea);'
        f'way["shop"="car_repair"](area.searchArea);'
        f'node["shop"="tyres"](area.searchArea);'
        f'way["shop"="tyres"](area.searchArea);'
        f'node["shop"="trailer"](area.searchArea);'
        f'way["shop"="trailer"](area.searchArea);'
        f'node["shop"="agrarian"](area.searchArea);'
        f'way["shop"="agrarian"](area.searchArea);'
        f'node["office"="logistics"](area.searchArea);'
        f'way["office"="logistics"](area.searchArea);'
        f');'
        f'out center body 2000;'
    )


def build_name_query(province: str) -> str:
    """İsim bazlı serbest metin (regex) araması - indekssiz olduğu için pahalı, büyük/kalabalık
    şehirlerde (örn. Ankara) zaman aşımına uğrayabiliyor. Bu yüzden etiket sorgusundan AYRI
    tutulur: bu sorgu başarısız olsa bile etiket sorgusunun sonuçları kaybolmasın diye."""
    return (
        f'[out:json][timeout:45];'
        f'area["name"="{province}"]["admin_level"="4"]->.searchArea;'
        f'node["name"~"{NAME_REGEX}",i](area.searchArea);'
        f'out center body 2000;'
    )


def _parse_elements(data: dict) -> list:
    results = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = (tags.get("name") or "").strip()
        if not name:
            continue

        center = el.get("center") or {}
        lat = el.get("lat", center.get("lat"))
        lon = el.get("lon", center.get("lon"))

        address_parts = [
            tags.get(k) for k in ["addr:street", "addr:housenumber", "addr:district", "addr:city"]
            if tags.get(k)
        ]

        results.append({
            "osm_id": f"{el.get('type')}_{el.get('id')}",
            "name": name,
            "shop_type": tags.get("shop") or tags.get("office") or "",
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "website": tags.get("website") or tags.get("contact:website") or "",
            "address": " ".join(address_parts),
            "lat": lat,
            "lon": lon,
        })
    return results


def _run_query(query: str, mirror_offset: int, province: str, label: str) -> list:
    """Tek bir Overpass sorgusunu birden fazla ayna deneyerek çalıştırır. Hepsi başarısız
    olursa boş liste döner - bu sorgunun başarısız olması ÇAĞIRANIN diğer sorgusunu etkilemez
    (bkz. search_province: etiket ve isim sorguları birbirinden bağımsız çalışır)."""
    n = len(OVERPASS_MIRRORS)
    ordered_mirrors = [OVERPASS_MIRRORS[(mirror_offset + i) % n] for i in range(n)]

    for mirror in ordered_mirrors:
        try:
            # istemci taraf zaman aşımı, sorgunun kendi [timeout:60] değerinden daha kısa OLAMAZ -
            # aksi halde sunucu henüz bitirmeden istemci pes edip büyük şehir sonuçlarını kaybederdi.
            res = requests.post(mirror, data={"data": query}, headers=OVERPASS_HEADERS, timeout=75)
            if res.status_code != 200:
                print(f"  [{province}/{label}] {mirror}: HTTP {res.status_code}")
                continue

            data = res.json()
            if data.get("remark"):
                print(f"  [{province}/{label}] {mirror}: {data['remark']}")
                continue

            return _parse_elements(data)
        except Exception as e:
            print(f"  [{province}/{label}] {mirror}: hata - {e}")
            continue

    return []


def search_province(province: str, mirror_offset: int = 0) -> list:
    """Bir ildeki potansiyel firmaları OpenStreetMap/Overpass API üzerinden arar. Ücretsiz,
    API anahtarı/kart gerektirmez.

    Etiket (shop=car_parts vb.) ve isim (serbest metin regex) araması AYRI İKİ SORGU olarak
    çalıştırılır - gerçek bir testte tespit edildi ki isim regex araması büyük/kalabalık
    şehirlerde (örn. Ankara) çok pahalı olup zaman aşımına uğruyor; eskiden bu TEK bir birleşik
    sorgu olduğu için isim kısmı zaman aşımına uğrayınca etiket kısmının bulduğu gerçek
    sonuçlar (Ankara'da 141 firma!) da beraber kayboluyordu. Ayrı sorgularla biri başarısız
    olsa bile diğeri kurtulur.

    mirror_offset: hangi aynadan başlanacağını belirler - paralel taramada her iş parçacığı
    farklı bir aynadan başlarsa hepsi aynı sunucuya aynı anda yüklenip rate limit'e takılmaz."""
    tag_results = _run_query(build_tag_query(province), mirror_offset, province, "etiket")
    name_results = _run_query(build_name_query(province), mirror_offset, province, "isim")

    # Bir node hem etiket hem isim eşleşmesiyle iki sorguda da çıkabilir - osm_id'ye göre birleştir
    merged = {}
    for r in tag_results + name_results:
        merged[r["osm_id"]] = r
    return list(merged.values())

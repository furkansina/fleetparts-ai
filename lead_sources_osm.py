import requests

# Birden fazla ücretsiz Overpass aynası - biri yavaş/meşgulse diğeri denenir
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]
OVERPASS_HEADERS = {"Accept": "*/*", "User-Agent": "fleetparts-lead-discovery/1.0"}
# NOT: "toptan" (wholesale) kasıtlı olarak burada YOK - tek başına çok genel, market/tekstil/gıda
# gibi alakasız toptancıları da OSM sonuçlarına dahil edip gürültü üretiyordu (örn. "Bizim Toptan Market").
NAME_REGEX = "nakliye|lojistik|dorse|yedek parça|kamyon|taşımacılık|transport|treyler"


def build_query(province: str) -> str:
    # Etiket bazlı aramalar (shop=car_parts vb.) ucuz/indeksli; geniş isim regex taraması
    # pahalı olduğu için sadece node'larda yapılır (way'lerde değil) - büyük illerde zaman
    # aşımını önlemek için bu şekilde sınırlandırıldı.
    return (
        f'[out:json][timeout:30];'
        f'area["name"="{province}"]["admin_level"="4"]->.searchArea;'
        f'('
        f'node["shop"="car_parts"](area.searchArea);'
        f'way["shop"="car_parts"](area.searchArea);'
        f'node["shop"="car_repair"](area.searchArea);'
        f'way["shop"="car_repair"](area.searchArea);'
        f'node["office"="logistics"](area.searchArea);'
        f'way["office"="logistics"](area.searchArea);'
        f'node["name"~"{NAME_REGEX}",i](area.searchArea);'
        f');'
        f'out center body 300;'
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


def search_province(province: str) -> list:
    """Bir ildeki potansiyel firmaları OpenStreetMap/Overpass API üzerinden arar.
    Ücretsiz, API anahtarı/kart gerektirmez. Birden fazla ayna sırayla denenir;
    hepsi başarısız olursa (rate limit, zaman aşımı vb.) boş liste döner - tek bir
    ilin başarısız olması tüm koşuyu durdurmasın diye."""
    query = build_query(province)

    for mirror in OVERPASS_MIRRORS:
        try:
            # İstemci zaman aşımı, sorgunun kendi [timeout:30] değerinden biraz fazla tutuluyor;
            # yavaş/tıkanık bir aynada uzun süre beklemek yerine hızlıca bir sonraki aynaya geçilir.
            res = requests.post(mirror, data={"data": query}, headers=OVERPASS_HEADERS, timeout=40)
            if res.status_code != 200:
                print(f"  [{province}] {mirror}: HTTP {res.status_code}")
                continue

            data = res.json()
            if data.get("remark"):
                print(f"  [{province}] {mirror}: {data['remark']}")
                continue

            return _parse_elements(data)
        except Exception as e:
            print(f"  [{province}] {mirror}: hata - {e}")
            continue

    return []

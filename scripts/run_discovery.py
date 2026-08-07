import sys
import os
import json
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provinces import PROVINCES
from lead_sources_osm import search_province, OVERPASS_MIRRORS
from lead_sources_directory import search_province as search_province_directory
from lead_sources_sanayi_sitesi import search_all as search_sanayi_sitesi
from lead_scoring import score_lead
from lead_dedupe import dedupe_key

LEADS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leads.json")


def _build_lead(lead_id, source, raw, province, scoring, batch_id):
    return {
        "lead_id": lead_id,
        "source": source,
        "company_name": raw["name"],
        "entity_type_note": scoring["entity_type_note"],
        "sector_guess": scoring["sector_label"],
        "province": province,
        "district": "",
        "address": raw.get("address", ""),
        "phone": raw.get("phone", ""),
        "phone_is_mobile": scoring["phone_is_mobile"],
        "lat": raw.get("lat"),
        "lon": raw.get("lon"),
        "growth_signal": None,
        "relevance_score": scoring["relevance_score"],
        "score_breakdown": scoring["score_breakdown"],
        "score_reasoning": scoring["score_reasoning"],
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "scan_batch_id": batch_id,
    }


def load_existing_leads():
    try:
        with open(LEADS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provinces", type=int, default=None, help="Sadece ilk N ili tara (test için)")
    parser.add_argument("--dry-run", action="store_true", help="leads.json'a yazma, sadece özet göster")
    parser.add_argument("--workers", type=int, default=2, help="Aynı anda taranacak il sayısı (gerçekte güvenilir 2 Overpass aynası olduğu için varsayılan 2)")
    args = parser.parse_args()

    provinces_to_scan = PROVINCES[: args.provinces] if args.provinces else PROVINCES

    existing = load_existing_leads()
    existing_keys = {dedupe_key(l.get("company_name", ""), l.get("phone", "")) for l in existing}

    batch_id = datetime.now(timezone.utc).strftime("%Y-W%V")
    new_leads = []
    seen_this_run = set()
    lock = threading.Lock()

    def scan_one(idx, province):
        # Her iş parçacığı farklı bir Overpass aynasından başlar - aksi halde 3 il aynı anda
        # aynı sunucuya yüklenip rate limit'e (429/timeout) daha kolay takılırdı.
        mirror_offset = idx % len(OVERPASS_MIRRORS)
        osm_results = search_province(province, mirror_offset=mirror_offset)
        # turkbusinesscenter.com (Türkiye'ye özel B2B firma rehberi) OSM'e tamamlayıcı ikinci
        # kaynak - OSM'in yakalayamadığı firmaları da bulur. Biri başarısız olsa diğeri etkilenmez.
        try:
            directory_results = search_province_directory(province)
        except Exception as e:
            print(f"  [{province}/directory] hata - {e}")
            directory_results = []
        return province, osm_results, directory_results

    # İller aynı anda taranır (varsayılan 3'ü birlikte) - tek bir ilin yavaş/başarısız olması
    # diğerlerini bloklamaz. Eskiden tamamen sıralı + il başına 3sn bekleme vardı (81 ilde
    # kötü senaryoda 45dk'ya kadar sürüyordu); paralel tarama bunu bulunan firma kalitesinden
    # ödün vermeden (aynı sorgu, aynı skorlama) belirgin şekilde hızlandırır.
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for idx, province in enumerate(provinces_to_scan):
            futures[executor.submit(scan_one, idx, province)] = province
            time.sleep(0.5)  # istekleri hafifçe kademeler, ilk anda toplu patlama olmasın

        completed = 0
        for future in as_completed(futures):
            completed += 1
            province = futures[future]
            try:
                _, osm_results, directory_results = future.result()
            except Exception as e:
                print(f"[{completed}/{len(provinces_to_scan)}] {province}: beklenmeyen hata - {e}")
                continue
            print(f"[{completed}/{len(provinces_to_scan)}] Tarandı: {province} "
                  f"(OSM: {len(osm_results)}, Rehber: {len(directory_results)} ham sonuç)")

            # Her iki kaynaktan gelen sonuçlar aynı şekilde işlenir; hangi kaynaktan geldiği
            # lead_id öneki (osm_/tbc_) ve 'source' alanıyla ayırt edilir.
            tagged_results = [("osm", raw) for raw in osm_results] + [("directory", raw) for raw in directory_results]

            with lock:
                for origin, raw in tagged_results:
                    key = dedupe_key(raw["name"], raw.get("phone", ""))
                    if key in existing_keys or key in seen_this_run:
                        continue
                    seen_this_run.add(key)

                    scoring = score_lead(raw)
                    if origin == "osm":
                        lead_id = f"osm_{raw['osm_id']}"
                        source = "openstreetmap"
                    else:
                        lead_id = raw["site_id"]  # zaten "tbc_" önekli
                        source = "turkbusinesscenter"
                    new_leads.append(_build_lead(lead_id, source, raw, province, scoring, batch_id))

                # Her il tamamlandığında diske yazılır - koşu yarıda kesilse/zaman aşımına uğrasa
                # bile o ana kadarki ilerleme kaybolmaz (GitHub Actions'taki commit adımı ne bulursa onu kaydeder)
                if not args.dry_run:
                    combined_so_far = existing + new_leads
                    with open(LEADS_FILE, "w", encoding="utf-8") as f:
                        json.dump(combined_so_far, f, ensure_ascii=False, indent=4)

    # Sanayi sitesi / bölgesel oto rehberi kaynakları il bazında değil - her biri zaten sabit
    # bir ile bağlı (Ankara/İstanbul/İzmir), bu yüzden il döngüsünden AYRI, tek seferlik bir
    # geçiş olarak çalıştırılıyor (bkz. lead_sources_sanayi_sitesi.py).
    print("\nSanayi sitesi rehberleri taranıyor (Ankara/İstanbul/İzmir)...")
    try:
        sanayi_results = search_sanayi_sitesi()
    except Exception as e:
        print(f"  sanayi sitesi taraması basarisiz - {e}")
        sanayi_results = []
    print(f"  Sanayi sitesi ham sonuç: {len(sanayi_results)}")
    for raw in sanayi_results:
        key = dedupe_key(raw["name"], raw.get("phone", ""))
        if key in existing_keys or key in seen_this_run:
            continue
        seen_this_run.add(key)
        scoring = score_lead(raw)
        new_leads.append(_build_lead(raw["site_id"], "sanayi_sitesi", raw, raw["province"], scoring, batch_id))
    if not args.dry_run:
        combined_so_far = existing + new_leads
        with open(LEADS_FILE, "w", encoding="utf-8") as f:
            json.dump(combined_so_far, f, ensure_ascii=False, indent=4)

    print(f"\nToplam yeni lead: {len(new_leads)}")
    for l in sorted(new_leads, key=lambda x: -x["relevance_score"])[:15]:
        print(f"  [{l['relevance_score']:>3}] {l['company_name']} | {l['province']} | {l['sector_guess']}")

    if args.dry_run:
        print("\nDRY RUN - leads.json'a yazılmadı")
        return

    print(f"\nleads.json güncellendi. Toplam kayıt: {len(existing) + len(new_leads)}")


if __name__ == "__main__":
    main()

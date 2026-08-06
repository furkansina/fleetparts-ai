import sys
import os
import json
import time
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provinces import PROVINCES
from lead_sources_osm import search_province
from lead_scoring import score_lead
from lead_dedupe import dedupe_key

LEADS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leads.json")


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
    args = parser.parse_args()

    provinces_to_scan = PROVINCES[: args.provinces] if args.provinces else PROVINCES

    existing = load_existing_leads()
    existing_keys = {dedupe_key(l.get("company_name", ""), l.get("phone", "")) for l in existing}

    batch_id = datetime.now(timezone.utc).strftime("%Y-W%V")
    new_leads = []
    seen_this_run = set()

    for i, province in enumerate(provinces_to_scan):
        print(f"Taranıyor: {province}...")
        if i > 0:
            time.sleep(3)  # Overpass'ı art arda hızlı istekle kısıtlamaya uğratmamak için
        raw_results = search_province(province)
        for raw in raw_results:
            key = dedupe_key(raw["name"], raw.get("phone", ""))
            if key in existing_keys or key in seen_this_run:
                continue
            seen_this_run.add(key)

            scoring = score_lead(raw)
            lead = {
                "lead_id": f"osm_{raw['osm_id']}",
                "source": "openstreetmap",
                "company_name": raw["name"],
                "entity_type_note": scoring["entity_type_note"],
                "sector_guess": scoring["sector_label"],
                "province": province,
                "district": "",
                "address": raw.get("address", ""),
                "phone": raw.get("phone", ""),
                "lat": raw.get("lat"),
                "lon": raw.get("lon"),
                "growth_signal": None,
                "relevance_score": scoring["relevance_score"],
                "score_breakdown": scoring["score_breakdown"],
                "score_reasoning": scoring["score_reasoning"],
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "scan_batch_id": batch_id,
            }
            new_leads.append(lead)

        # Her ilden sonra diske yazılır - koşu yarıda kesilse/zaman aşımına uğrasa bile
        # o ana kadarki ilerleme kaybolmaz (GitHub Actions'taki commit adımı ne bulursa onu kaydeder)
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

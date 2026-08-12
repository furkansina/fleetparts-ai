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
from lead_sources_sanayisitesi_platform import search_all as search_sanayisitesi_platform
from lead_sources_find_com_tr import search_all as search_find_com_tr
from lead_sources_izto import search_all as search_izto
import lead_sources_google_places
from lead_scoring import score_lead
from lead_dedupe import dedupe_key, sanitize_phone

LEADS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leads.json")
# BU TARAMANIN kendi bulduklarının AYRI, canlı-güncellenen kaydı (2026-08-11'de eklendi - bkz.
# main() sonundaki uzun not). leads.json'a artık BU SCRIPT içinde hiç yazılmıyor; workflow'un
# commit adımı, çalışmanın HERHANGİ bir noktasında (tamamlansa da timeout'ta kesilse de) bu
# dosyanın o ana kadarki en güncel halini origin/main'in TAZE leads.json'una scripts/
# merge_new_leads.py ile ekliyor - git seviyesinde uzlaşma/rebase hiç gerekmiyor.
NEW_LEADS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "new_leads_this_run.json")


def _checkpoint_new_leads(new_leads):
    with open(NEW_LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(new_leads, f, ensure_ascii=False, indent=4)


def _build_lead(lead_id, source, raw, province, scoring, batch_id):
    return {
        "lead_id": lead_id,
        "source": source,
        "company_name": raw["name"],
        "entity_type_note": scoring["entity_type_note"],
        "sector_guess": scoring["sector_label"],
        # HAM skorlama girdileri de saklanıyor (2026-08-11 eklendi) - önceden sadece TÜRETİLMİŞ
        # sector_label saklanıyordu, bu da lead_scoring.py'de bir kural iyileştirildiğinde (bkz.
        # gerçek örnek: "Car Lease Rent A Car" gibi yanlış-pozitiflerin düzeltilmesi) mevcut
        # kayıtların GERİYE DÖNÜK yeniden puanlanmasını İMKANSIZ kılıyordu (girdi kaybolmuştu,
        # sadece isim bazlı bir yama uygulanabiliyordu). Artık her kural değişikliği, tarama
        # tekrar çalıştırılmadan da scripts/rescore_leads.py ile TÜM geçmişe uygulanabilir.
        "raw_shop_type": raw.get("shop_type", ""),
        "raw_category_label": raw.get("category_label", ""),
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


def _add_results(results, source, id_field, province_field, existing_keys, seen_this_run, new_leads, batch_id):
    """Il döngüsüne bağlı olmayan (tek seferlik) kaynaklar için ortak ekleme/dedupe mantığı -
    sanayi_sitesi, sanayisitesi_platform, find_com_tr, google_places hepsi aynı şekli izliyor."""
    added = 0
    for raw in results:
        raw["phone"] = sanitize_phone(raw.get("phone", ""))
        key = dedupe_key(raw["name"], raw.get("phone", ""))
        if key in existing_keys or key in seen_this_run:
            continue
        seen_this_run.add(key)
        scoring = score_lead(raw)
        province = raw[province_field] if province_field else ""
        new_leads.append(_build_lead(raw[id_field], source, raw, province, scoring, batch_id))
        added += 1
    return added


def run_osm_and_directory_phase(provinces_to_scan, workers, existing_keys, seen_this_run, new_leads, batch_id, dry_run):
    """OSM (Overpass) + turkbusinesscenter.com rehber taraması - il bazlı, paralel.

    BİLİNÇLİ OLARAK EN SONDA ÇALIŞTIRILIYOR (2026-08-11/12'de tespit edildi): Overpass'ın
    ücretsiz aynaları zaman zaman çok yavaşlıyor/504 dönüyor - gerçek bir taramada 81 ilin
    sadece 65'i 148 dakikada taranabildi ve workflow'un 150dk zaman aşımına çarptı - ÜSTELİK bu
    81 il DAHA ÖNCEKİ başarılı taramalarda zaten kapsandığı için hepsi tekilleştirmede elendi,
    SIFIR yeni lead üretti. Bu arada find.com.tr/sanayi siteleri gibi gerçekten YENİ veri
    üretebilecek (ve çok daha hızlı/güvenilir) kaynaklar hiç çalışma fırsatı bulamadı. Artık
    OSM+rehber taraması en sona alındı - Overpass ne kadar yavaş/kötü olursa olsun, diğer TÜM
    kaynaklar önce kendi sonuçlarını bulup diske işler; OSM zaman aşımına uğrarsa sadece kendi
    (zaten büyük ölçüde tekrar niteliğindeki) sonuçları kaybolur, değerli kaynaklar etkilenmez."""
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

    # İller aynı anda taranır (varsayılan 2'si birlikte) - tek bir ilin yavaş/başarısız olması
    # diğerlerini bloklamaz. Eskiden tamamen sıralı + il başına 3sn bekleme vardı (81 ilde
    # kötü senaryoda 45dk'ya kadar sürüyordu); paralel tarama bunu bulunan firma kalitesinden
    # ödün vermeden (aynı sorgu, aynı skorlama) belirgin şekilde hızlandırır.
    with ThreadPoolExecutor(max_workers=workers) as executor:
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
                    raw["phone"] = sanitize_phone(raw.get("phone", ""))
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
                if not dry_run:
                    _checkpoint_new_leads(new_leads)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provinces", type=int, default=None, help="Sadece ilk N ili tara (test için)")
    parser.add_argument("--only", type=str, default=None, help="Virgülle ayrılmış belirli il adları (örn. 'Gaziantep,Muş') - ağ kesintisi gibi nedenlerle boş kalan illeri hızlıca yeniden taramak için")
    parser.add_argument("--skip-sanayi-sitesi", action="store_true", help="Sanayi sitesi kaynaklarını atla (zaten tarandıysa tekrar taramaya gerek yok)")
    parser.add_argument("--skip-osm", action="store_true", help="OSM/Overpass + turkbusinesscenter.com il taramasını atla (Overpass yavaş/kesintili olduğunda diğer kaynaklara zaman bırakmak için)")
    parser.add_argument("--dry-run", action="store_true", help="leads.json'a yazma, sadece özet göster")
    parser.add_argument("--workers", type=int, default=2, help="Aynı anda taranacak il sayısı (gerçekte güvenilir 2 Overpass aynası olduğu için varsayılan 2)")
    args = parser.parse_args()

    if args.only:
        wanted = {p.strip() for p in args.only.split(",")}
        provinces_to_scan = [p for p in PROVINCES if p in wanted]
    else:
        provinces_to_scan = PROVINCES[: args.provinces] if args.provinces else PROVINCES

    existing = load_existing_leads()
    existing_keys = {dedupe_key(l.get("company_name", ""), l.get("phone", "")) for l in existing}

    batch_id = datetime.now(timezone.utc).strftime("%Y-W%V")
    new_leads = []
    seen_this_run = set()

    # Sanayi sitesi / bölgesel oto rehberi kaynakları il bazında değil - her biri zaten sabit
    # bir ile bağlı (Ankara/İstanbul/İzmir), bu yüzden il döngüsünden AYRI, tek seferlik bir
    # geçiş olarak çalıştırılıyor (bkz. lead_sources_sanayi_sitesi.py). --only ile hedefli bir
    # yeniden tarama yapılıyorsa (örn. ağ kesintisinden boş kalan bir ili tamamlamak) bu adım
    # gereksiz - zaten taranmış olur, dedup zaten atlar ama zaman kaybetmemek için atlanıyor.
    if args.only or args.skip_sanayi_sitesi:
        sanayi_results = []
    else:
        print("Sanayi sitesi rehberleri taranıyor (Ankara/İstanbul/İzmir)...")
        try:
            sanayi_results = search_sanayi_sitesi()
        except Exception as e:
            print(f"  sanayi sitesi taraması basarisiz - {e}")
            sanayi_results = []
        print(f"  Sanayi sitesi ham sonuç: {len(sanayi_results)}")
    added = _add_results(sanayi_results, "sanayi_sitesi", "site_id", "province", existing_keys, seen_this_run, new_leads, batch_id)
    print(f"  -> {added} yeni lead eklendi (toplam yeni: {len(new_leads)})")
    if not args.dry_run:
        _checkpoint_new_leads(new_leads)

    # sanayisitesi.com.tr platformu (Eskişehir/Bursa/Konya/Adana/Gaziantep/Denizli/Manisa/Kocaeli/
    # Antalya/Kahramanmaraş/Diyarbakır/Balıkesir/Elazığ/Erzurum - bkz. lead_sources_sanayisitesi_
    # platform.py CITIES) - 2026-08-11'de eklendi, yukarıdaki bölgesel rehberlerle AYNI mantıkla
    # (il döngüsünden bağımsız, tek seferlik) çalışır ama TAMAMEN AYRI bir kaynak/dosya olduğu
    # için kendi try/except bloğu var - biri başarısız olursa diğerini etkilemez.
    if args.only or args.skip_sanayi_sitesi:
        platform_results = []
    else:
        print("\nsanayisitesi.com.tr ağı taranıyor (14 il)...")
        try:
            platform_results = search_sanayisitesi_platform()
        except Exception as e:
            print(f"  sanayisitesi.com.tr taraması başarısız - {e}")
            platform_results = []
        print(f"  sanayisitesi.com.tr ham sonuç: {len(platform_results)}")
    added = _add_results(platform_results, "sanayisitesi_platform", "site_id", "province", existing_keys, seen_this_run, new_leads, batch_id)
    print(f"  -> {added} yeni lead eklendi (toplam yeni: {len(new_leads)})")
    if not args.dry_run:
        _checkpoint_new_leads(new_leads)

    # find.com.tr (resmi ticaret sicili kaynaklı firma rehberi) - 2026-08-11'de eklendi. Diğer
    # kaynakların (OSM, turkbusinesscenter.com, sanayi siteleri) neredeyse hiç veri bulamadığı
    # küçük Anadolu illerinde (Bingöl, Hakkari, Ardahan, Tunceli vb.) bile onlarca gerçek, doğru
    # kategorili firma buluyor - 81 ilin TAMAMINI kapsıyor (bölgesel/tek-şehir kaynakların aksine
    # il döngüsüne değil, kendi search_all() fonksiyonuna bağlı, tek seferlik çalışır). SINIRLAMA:
    # telefon numarası vermiyor - bu lead'ler phone="" ile eklenir, leads.html'deki "Google'da Ara"
    # akışına düşer, skorlama da bunu data_completeness üzerinden otomatik olarak düşük önceliğe
    # koyar (bkz. lead_scoring.py). --only ile hedefli il taramasında atlanır (zaten tüm illeri
    # tarıyor, tek bir ile hedeflenemez).
    if args.only or args.skip_sanayi_sitesi:
        find_results = []
    else:
        print("\nfind.com.tr firma rehberi taranıyor (81 il)...")
        try:
            find_results = search_find_com_tr()
        except Exception as e:
            print(f"  find.com.tr taraması başarısız - {e}")
            find_results = []
        print(f"  find.com.tr ham sonuç: {len(find_results)}")
    added = _add_results(find_results, "find_com_tr", "site_id", "province", existing_keys, seen_this_run, new_leads, batch_id)
    print(f"  -> {added} yeni lead eklendi (toplam yeni: {len(new_leads)})")
    if not args.dry_run:
        _checkpoint_new_leads(new_leads)

    # İzmir Ticaret Odası (İZTO) - RESMİ oda sicil kaydı, "Üye Firma Sorgulama" aracı - 2026-08-12'de
    # eklendi. Diğer TÜM kaynaklardan (OSM, turkbusinesscenter.com, sanayi siteleri, find.com.tr)
    # FARKLI bir kaynak TÜRÜ: harita/dizin taraması değil, bizzat Ticaret Odası'nın kendi üye
    # sicilinden, TOBB standart "Meslek Grubu" sınıflandırmasıyla filtrelenen resmi bir kayıt (bkz.
    # lead_sources_izto.py - TESK/Esnaf Odaları ve İSO/BTSO/ATSO gibi diğer odaların hepsi CAPTCHA'lı
    # veya sadece tam isim eşleşmesi arıyor, İZTO'nunki CAPTCHA'sız ve kategori bazlı arama yapıyor).
    # Gerçek bir testte doğrulandı: 5 ilgili meslek grubu (otomotiv parça toptan/perakende, lastik-akü,
    # yük taşıma, lojistik-gümrük) TOPLAM 3666 ham kayıt döndürdü - TEK bir il (İzmir) için bile diğer
    # kaynakların çoğundan daha derin bir kapsama. SINIRLAMA: find.com.tr gibi telefon numarası
    # vermiyor. İl bağımsız değil (sadece İzmir) ama sanayi_sitesi/find_com_tr gibi tek seferlik
    # çalışıyor, il döngüsüne girmiyor.
    if args.only or args.skip_sanayi_sitesi:
        izto_results = []
    else:
        print("\nİzmir Ticaret Odası (İZTO) üye sicili taranıyor (5 meslek grubu)...")
        try:
            izto_results = search_izto()
        except Exception as e:
            print(f"  izto taraması başarısız - {e}")
            izto_results = []
        print(f"  İZTO ham sonuç: {len(izto_results)}")
    added = _add_results(izto_results, "izto", "site_id", "province", existing_keys, seen_this_run, new_leads, batch_id)
    print(f"  -> {added} yeni lead eklendi (toplam yeni: {len(new_leads)})")
    if not args.dry_run:
        _checkpoint_new_leads(new_leads)

    # Google Places API - tüm illerde telefon dahil neredeyse tam kapsama veren TEK gerçek çözüm,
    # ama ücretli (bkz. lead_sources_google_places.py). GOOGLE_PLACES_API_KEY ortam değişkeni
    # (Render/GitHub Actions secrets) tanımlanana kadar TAMAMEN pasif - hiçbir istek atılmaz,
    # hiçbir ücret oluşmaz. Baba faturalandırmayı aktif edip anahtarı eklediği an, kod tarafında
    # başka hiçbir değişiklik gerekmeden otomatik devreye girer.
    if lead_sources_google_places.is_configured() and not args.only:
        print("\nGoogle Places taranıyor (81 il, ücretli API)...")
        try:
            gplaces_results = lead_sources_google_places.search_all()
        except Exception as e:
            print(f"  Google Places taraması başarısız - {e}")
            gplaces_results = []
        print(f"  Google Places ham sonuç: {len(gplaces_results)}")
        added = _add_results(gplaces_results, "google_places", "site_id", "province", existing_keys, seen_this_run, new_leads, batch_id)
        print(f"  -> {added} yeni lead eklendi (toplam yeni: {len(new_leads)})")
        if not args.dry_run:
            _checkpoint_new_leads(new_leads)

    # OSM (Overpass) + turkbusinesscenter.com il taraması - bkz. run_osm_and_directory_phase
    # docstring'i: BİLİNÇLİ OLARAK EN SONA ALINDI, Overpass'ın yavaş/kesintili olduğu günlerde
    # yukarıdaki (daha hızlı/güvenilir ve hâlâ yeni veri üretebilen) kaynakların zaman aşımından
    # ETKİLENMEMESİ için. --skip-osm ile elle de atlanabilir (ör. Overpass'ın kötü gittiği
    # bilindiğinde bu turu tamamen boşa harcamamak için).
    if args.skip_osm:
        print("\n--skip-osm: OSM/turkbusinesscenter il taraması atlandı.")
    else:
        print(f"\nOSM + turkbusinesscenter.com taranıyor ({len(provinces_to_scan)} il)...")
        run_osm_and_directory_phase(provinces_to_scan, args.workers, existing_keys, seen_this_run, new_leads, batch_id, args.dry_run)

    print(f"\nToplam yeni lead: {len(new_leads)}")
    for l in sorted(new_leads, key=lambda x: -x["relevance_score"])[:15]:
        print(f"  [{l['relevance_score']:>3}] {l['company_name']} | {l['province']} | {l['sector_guess']}")

    if args.dry_run:
        print("\nDRY RUN - hiçbir dosyaya yazılmadı")
        return

    _checkpoint_new_leads(new_leads)
    print(f"\n{NEW_LEADS_FILE} güncel ({len(new_leads)} yeni lead) - leads.json'a birleştirme workflow'un commit adımında (scripts/merge_new_leads.py) yapılacak.")


if __name__ == "__main__":
    main()

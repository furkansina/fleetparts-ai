"""discovery-scan.yml'in commit adımında çalışır. new_leads_this_run.json'daki (bu taramanın
kendi bulduğu, run_discovery.py tarafından yazılan) lead'leri, o ANDA origin/main'de duran GERÇEK
GÜNCEL leads.json'un üzerine (Python/dedupe seviyesinde) ekler.

NEDEN BU SCRIPT VAR: run_discovery.py 2.5 saate kadar sürebiliyor - bu kadar uzun bir sürede
leads.json GitHub'da BAŞKA commit'lerle (elle düzeltme, skor sertleştirmesi vb.) defalarca
değişebiliyor. Taramanın kendi (iş BAŞLARKEN diskte ne varsa onu baz alan) leads.json'unu git
rebase ile uzlaştırmaya çalışmak, bu büyüklükte bir JSON blob'unda güvenilir çalışmıyor - gerçek
bir taramada CONFLICT verip TÜM taramanın (2.5 saatlik find.com.tr dahil) hiç GitHub'a
yansımadan kaybolmasına yol açtı (2026-08-11). Bu script git'e hiç ihtiyaç duymadan, sadece
Python'da JSON birleştirme yaparak bu sorunu kökten çözer - workflow ÖNCE `git fetch` + `git
checkout origin/main -- leads.json` ile leads.json'u en güncel haline getirir, SONRA bu script
sadece new_leads_this_run.json'daki (ve origin/main'de henüz olmayan) kayıtları ekler.

Kullanım: python scripts/merge_new_leads.py
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lead_dedupe import dedupe_key

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEADS_FILE = os.path.join(BASE_DIR, "leads.json")
NEW_LEADS_FILE = os.path.join(BASE_DIR, "new_leads_this_run.json")


def main():
    if not os.path.exists(NEW_LEADS_FILE):
        print("new_leads_this_run.json yok - bu taramada henüz hiçbir kayıt bulunamamış (ya da erken aşamada kesilmiş). Birleştirilecek bir şey yok.")
        return

    with open(NEW_LEADS_FILE, "r", encoding="utf-8") as f:
        new_leads = json.load(f)

    try:
        with open(LEADS_FILE, "r", encoding="utf-8") as f:
            current = json.load(f)
            if not isinstance(current, list):
                current = []
    except Exception:
        current = []

    existing_keys = {dedupe_key(l.get("company_name", ""), l.get("phone", "")) for l in current}

    added = 0
    seen_this_merge = set()
    for lead in new_leads:
        key = dedupe_key(lead.get("company_name", ""), lead.get("phone", ""))
        if key in existing_keys or key in seen_this_merge:
            continue
        seen_this_merge.add(key)
        current.append(lead)
        added += 1

    with open(LEADS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=4)

    print(f"Bu taramada bulunan {len(new_leads)} kayıttan {added} tanesi origin/main'in güncel "
          f"leads.json'una eklendi ({len(new_leads) - added} tanesi zaten varmış - başka bir "
          f"kaynaktan/önceki bir taramadan gelmiş olabilir). Toplam kayıt: {len(current)}.")


if __name__ == "__main__":
    main()

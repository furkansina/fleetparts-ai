import os
import re
import shutil
import json
import base64
import time
import secrets
import threading
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import fitz  # PyMuPDF - PDF sayfalarını görsele çevirmek için
from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
import csv
import io
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import lead_store
import outreach
import usage_tracker
from lead_dedupe import is_mobile_phone, turkish_lower

app = FastAPI(title="FleetParts AI - Universal Heavy Duty Master Engine")

# Faz 2 (lead keşfi/inceleme) yönetim sayfaları için basit koruma -
# katalog/parça arama (mevcut ürün) herkese açık kalır, sadece yeni admin sayfaları korunur
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
security = HTTPBasic()

def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    valid_user = bool(ADMIN_USER) and secrets.compare_digest(credentials.username, ADMIN_USER)
    valid_pass = bool(ADMIN_PASS) and secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (valid_user and valid_pass):
        raise HTTPException(status_code=401, detail="Yetkisiz erişim", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# API Anahtarı / Token (Render ortamından GROQ_API_KEY olarak çeker) - console.groq.com/keys, kredi kartı gerektirmez
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")            # Ana hesap - MÜŞTERİ havuzunun birincil anahtarı
GROQ_API_KEY_BULK = os.environ.get("GROQ_API_KEY_BULK", "")  # İkinci (varsa) hesap - SADECE toplu iş (katalog tarama) için ayrılmış
GROQ_API_KEY_THIRD = os.environ.get("GROQ_API_KEY_THIRD", "")  # Üçüncü (varsa) hesap - müşteri havuzunun 2. yedeği, kota hiç bitmesin diye ekstra güvence
GROQ_VISION_MODEL = "qwen/qwen3.6-27b"     # Görsel gerektiren işler (parça fotoğrafı, katalog sayfası) - hesap başına günlük 200K token kotası
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"  # Sadece iç/toplu kullanım (lead ön-değerlendirme) - AYRI, 100K token kotası

# Render'ın diski her deploy'da sıfırlandığı için katalog GitHub'a da yedeklenir (kalıcılık için)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

# Faz 5: soğuk lead'lerin kendi WhatsApp'ından ilk temas kurabileceği herkese açık katalog sayfası
BUSINESS_WHATSAPP_NUMBER = os.environ.get("BUSINESS_WHATSAPP_NUMBER", "")

UPLOAD_DIR = "temp_images"
CATALOG_DIR = "sample_catalogs"
CATALOG_FILE = "catalog.json"
MAX_UPLOAD_MB = 20  # Groq görsel API'sinin sabit dosya boyutu sınırı

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CATALOG_DIR, exist_ok=True)

DEFAULT_CATALOG = [
    {
        "id": "FRN-001",
        "oem": "1505234",
        "name": "Disk Fren Balatası Heavy",
        "brand": "Orijinal Kalite",
        "specs": "Döküm arka plaka, 247x110mm, çift kulaklı bağlantı noktası, kalınlık 25mm, aşınma sensör yuvalı.",
        "stock": 45
    },
    {
        "id": "VLF-102",
        "oem": "4324102227",
        "name": "Hava Kurutucu Dağıtıcı Valf (4 Yollu)",
        "brand": "Wabco Tipi",
        "specs": "Alüminyum döküm gövde, 4 adet M22 hava basınç portu, alt kısımda 7 pin elektronik soket, silindirik üst hazne.",
        "stock": 15
    },
    {
        "id": "FLT-303",
        "oem": "21707134",
        "name": "Ana Yakıt ve Su Ayırıcı Filtre",
        "brand": "FleetGuard",
        "specs": "Silindirik kağıt filtre elemanı, üst conta çapı 90mm, tahliye musluklu metal dış gövde.",
        "stock": 60
    }
]

def seed_default_catalog():
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CATALOG, f, ensure_ascii=False, indent=4)

# Başlangıç Evrensel Katalog Veritabanı (dosya yoksa veya bozuk/boşsa yeniden oluştur)
if not os.path.exists(CATALOG_FILE) or os.path.getsize(CATALOG_FILE) == 0:
    seed_default_catalog()

def _load_catalog_from_disk() -> list:
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        seed_default_catalog()
        return DEFAULT_CATALOG

_catalog_cache = {"data": None, "fetched_at": 0}
CATALOG_CACHE_TTL = 30  # saniye

def load_catalog() -> list:
    """Kataloğu önce GitHub'dan (kısa önbellekle) canlı çekmeye çalışır - böylece GitHub'da yapılan
    HERHANGİ bir değişiklik (uygulama üzerinden yükleme, elle bir düzeltme, ileride bir script) Render'da
    yeni bir deploy tetiklenmesini beklemeden birkaç saniye içinde canlıya yansır. Bunu eklememizin sebebi:
    catalog.json Render'ın 'ignored paths' listesinde olduğu için tek başına ona yapılan bir GitHub
    değişikliği yeni bir deploy tetiklemiyor - eskiden bu yüzden GitHub'daki düzeltmeler sessizce
    canlıya hiç yansımıyordu. GitHub'a ulaşılamazsa (yapılandırılmamış/ağ hatası) Render'ın kendi
    diskindeki en son bilinen hale döner."""
    now = time.time()
    if _catalog_cache["data"] is not None and (now - _catalog_cache["fetched_at"]) < CATALOG_CACHE_TTL:
        return _catalog_cache["data"]

    if GITHUB_REPO:
        try:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{CATALOG_FILE}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    _catalog_cache["data"] = data
                    _catalog_cache["fetched_at"] = now
                    return data
        except Exception:
            pass

    return _load_catalog_from_disk()

def save_catalog(catalog: list):
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=4)

def sync_catalog_to_github():
    """catalog.json'u GitHub'a yedekler; Render her yeniden deploy olduğunda diski sıfırladığı için kalıcılık böyle sağlanır."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/catalog.json"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        sha = None
        res = requests.get(api_url, headers=headers, timeout=15)
        if res.status_code == 200:
            sha = res.json().get("sha")
        with open(CATALOG_FILE, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        body = {"message": "Katalog otomatik güncelleme", "content": content_b64}
        if sha:
            body["sha"] = sha
        requests.put(api_url, headers=headers, json=body, timeout=15)
    except Exception:
        pass

def _resolve_key_chain(pool: str, use_secondary_model: bool) -> list:
    """`pool`'a göre hangi Groq hesabının/hesaplarının hangi sırayla deneneceğini belirler.
    Her eleman (etiket, api_anahtarı) çifti - etiket hata mesajlarında/kullanım takibinde
    hangi hesabın kullanıldığını belirtmek için kullanılır."""
    if use_secondary_model:
        # llama modeli zaten ayrı bir model/kota (havuz ayrımından bağımsız), her zaman ana hesap üzerinden
        return [("customer", GROQ_API_KEY)]
    if pool == "bulk":
        # NOT: bulk havuzu ÖNCEDEN kasıtlı olarak SADECE ikinci hesabı kullanıyordu (ana müşteri
        # hesabına asla dokunmasın diye). Ama gerçek kullanımda Groq'un GERÇEK günlük limitinin
        # bizim varsaydığımızdan (200K) çok daha düşük olduğu ortaya çıktı - katalog taraması tek
        # hesapla güne sığmıyor. Müşteri hesabı bugün neredeyse hiç kullanılmadığı için (boşta
        # duran kapasite), katalog taraması artık kendi hesabı bitince müşteri hesabına da
        # düşebiliyor - müşteri arama önceliğini korumak için hâlâ EN SON denenen hesap bu.
        chain = []
        if GROQ_API_KEY_BULK:
            chain.append(("bulk", GROQ_API_KEY_BULK))
        chain.append(("customer", GROQ_API_KEY))
        if GROQ_API_KEY_THIRD:
            chain.append(("customer2", GROQ_API_KEY_THIRD))
        return chain
    chain = [("customer", GROQ_API_KEY)]
    if GROQ_API_KEY_BULK:
        chain.append(("bulk", GROQ_API_KEY_BULK))
    if GROQ_API_KEY_THIRD:
        # 3. hesap - müşteri havuzunun 2. yedeği. Ana VE bulk hesabın ikisi de günlük kotasını
        # doldurursa devreye girer, kota tükenmesine karşı ekstra güvence.
        chain.append(("customer2", GROQ_API_KEY_THIRD))
    return chain

def call_groq_api(prompt: str, image_path: str = None, use_secondary_model: bool = False, pool: str = "customer") -> str:
    """Groq (OpenAI uyumlu) chat completions uç noktasına istek atan evrensel bağlantı yöneticisi.
    Varsayılan olarak HER ŞEY kanıtlanmış qwen modelinde kalır (müşteriye giden satış mesajı, eşleştirme
    gibi kritik çıktılarda kalite/güvenilirlik kotadan daha önemli). Sadece açıkça `use_secondary_model=True`
    verilen, müşteriye hiç gösterilmeyen iç/toplu işler (örn. lead ön-değerlendirme metni) ayrı kotalı
    llama modeline gider - qwen bazen Türkçe metne yabancı kelime karıştırdığı için llama'yı müşteri
    tarafına hiç kullanmıyoruz.

    `pool` parametresi HANGİ GROQ HESABININ kullanılacağını belirler (qwen için iki ayrı hesap
    olabilir - bkz. GROQ_API_KEY / GROQ_API_KEY_BULK):
    - "customer" (varsayılan): müşteri arama akışı (vision_agent/match_agent/find_by_text). ANA
      hesap (GROQ_API_KEY) kullanılır. O hesabın GÜNLÜK kotası biterse ve ikinci hesap
      tanımlıysa, MÜŞTERİ HİÇBİR ŞEY FARK ETMEDEN sessizce ikinci hesaba geçilir - müşteri
      arama asla toplu iş yüzünden kotasız kalmasın diye önceliklidir.
    - "bulk": katalog tarama gibi toplu/iç işler. SADECE ikinci hesap (GROQ_API_KEY_BULK)
      kullanılır - ana (müşteri) hesabına ASLA dokunmaz, ikisi karışmasın diye. İkinci hesap
      henüz tanımlanmadıysa (geçiş dönemi, tek hesapla çalışılıyor) ana hesaba düşer.
    """
    key_chain = _resolve_key_chain(pool, use_secondary_model)

    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as img_f:
            b64_img = base64.b64encode(img_f.read()).decode("utf-8")
        ext = image_path.split('.')[-1].lower()
        mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "pdf": "application/pdf"}
        mime_type = mime_map.get(ext, "image/jpeg")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
        ]
        model = GROQ_VISION_MODEL
        # max_tokens belirtilmezse Groq'un varsayılanı çok düşük kalıyor - yoğun bir katalog
        # sayfası (onlarca ürün) taranırken JSON yanıtı yarıda kesiliyordu (gerçek örnekte tespit
        # edildi). AMA çok yüksek de tutulamaz: Groq'un dakikalık kotası (TPM) sadece 8000 token
        # ve görsel girdinin kendisi zaten ~2500-3000 token tutuyor - 8000 verince tek başına bile
        # TPM'i dolduruyordu (413/429). 4500 hem çoğu katalog sayfası için yeterli hem de girdiyle
        # toplamda dakikalık bütçenin içinde kalıyor.
        payload = {"model": model, "reasoning_effort": "none", "max_tokens": 4500, "messages": [{"role": "user", "content": content}]}
    elif use_secondary_model:
        model = GROQ_TEXT_MODEL
        payload = {"model": model, "max_tokens": 8000, "messages": [{"role": "user", "content": prompt}]}
    else:
        model = GROQ_VISION_MODEL
        # Metin tabanlı çağrılar (satış mesajı, eşleştirme kararı) çok daha kısa çıktı üretir -
        # düşük tutmak dakikalık kotadan (TPM) daha az pay harcar, diğer isteklere yer bırakır.
        payload = {"model": model, "reasoning_effort": "none", "max_tokens": 2500, "messages": [{"role": "user", "content": prompt}]}
    last_error = ""
    daily_exhausted_count = 0
    for key_label, api_key in key_chain:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        for attempt in range(3):
            try:
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=60)
                # Groq'un GERÇEK kota durumunu (kendi tahminimiz değil) her yanıtta yakala - bkz.
                # usage_tracker.get_real_remaining_tokens ve modül üstü not.
                usage_tracker.record_rate_limit_headers(key_label, res.headers)
                if res.status_code == 200:
                    data = res.json()
                    usage_tracker.record_usage(data.get("usage", {}).get("total_tokens", 0), pool=key_label)
                    return data["choices"][0]["message"]["content"]
                if res.status_code == 429:
                    last_error = f"HTTP 429 ({key_label} hesap): {res.text}"
                    # Günlük token kotası bu HESAP için tamamen bittiyse tekrar denemek anlamsız
                    # (gece sıfırlanana kadar asla başarılı olmaz) - zincirdeki bir sonraki hesaba
                    # (varsa) geç. Dakikalık (TPM) limitse aynı hesapla tekrar dene.
                    if "per day" in res.text.lower() or "tpd" in res.text.lower():
                        daily_exhausted_count += 1
                        break
                    # Groq'un hata mesajı dakikalık kota (TPM) dolduğunda tam gereken bekleme süresini
                    # veriyor (örn. "Please try again in 47.78s") - sabit 10sn yeterli olmadığı gerçek
                    # bir yükleme testinde tespit edildi. Sabit süre yerine bu gerçek süreyi kullanıyoruz,
                    # aşırı uzamasın diye (Render'ın istek zaman aşımını aşmamak için) üst sınır koyuyoruz.
                    wait_match = re.search(r"try again in ([\d.]+)s", res.text)
                    wait_seconds = min(float(wait_match.group(1)), 45) + 1 if wait_match else 10
                    time.sleep(wait_seconds)
                    continue
                if res.status_code == 413 and payload.get("max_tokens"):
                    # İstek (girdi + istenen max_tokens) dakikalık token sınırını (TPM) aşıyor - özellikle
                    # yoğun/büyük bir katalog sayfası görseli + geniş max_tokens kombinasyonunda oluşur.
                    # Groq'un hata mesajı gerçek sınırı ve istenen miktarı verdiği için max_tokens'ı buna
                    # göre otomatik küçültüp aynı isteği tekrar deneriz - statik bir sayı tahmin etmek yerine.
                    match = re.search(r"Limit (\d+), Requested (\d+)", res.text)
                    if match:
                        limit, requested = int(match.group(1)), int(match.group(2))
                        overage = requested - limit
                        new_max_tokens = max(500, payload["max_tokens"] - overage - 200)
                        if new_max_tokens < payload["max_tokens"]:
                            payload["max_tokens"] = new_max_tokens
                            continue
                    last_error = f"HTTP 413 ({key_label} hesap): {res.text}"
                    break
                last_error = f"HTTP {res.status_code} ({key_label} hesap): {res.text}"
                break
            except requests.RequestException as e:
                last_error = f"Groq Bağlantı Hatası ({key_label} hesap): {str(e)}"
                break
        # bu hesapla olmadıysa (günlük kota bitti ya da başka bir hata) zincirdeki bir sonraki hesaba geçilir

    if daily_exhausted_count >= len(key_chain):
        # Zincirdeki HER hesabın günlük kotası aynı anda doldu
        if len(key_chain) > 1:
            raise Exception(f"🚨 Zincirdeki tüm yapay zeka hesaplarının ({len(key_chain)} hesap) günlük kotası aynı anda doldu (çok nadir bir durum). Kota gece yenilenir.")
        if pool == "bulk" and key_chain[0][0] == "bulk":
            raise Exception("Toplu işlem (katalog tarama) hesabının günlük kotası doldu. Müşteri arama ayrı bir hesap kullandığı için ETKİLENMEZ. Kota gece yenilenir.")
        raise Exception("Günlük yapay zeka kullanım kotası doldu. Kota gece (Groq sıfırlama saatinde) yenilenir, biraz sonra tekrar deneyin.")

    raise Exception(f"Groq isteği başarısız oldu: {last_error}")

def extract_json_object(raw_text: str) -> dict:
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()
    start = clean_text.find("{")
    end = clean_text.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError(f"Yanıtta JSON bulunamadı: {raw_text[:200]!r}")
    return json.loads(clean_text[start:end])

def call_groq_json(prompt: str, image_path: str = None, use_secondary_model: bool = False, pool: str = "customer") -> dict:
    """JSON bekleyen çağrılar için: modelin bozuk/boş yanıt verdiği durumlarda bir kez daha dener."""
    last_error = None
    for attempt in range(2):
        raw_text = call_groq_api(prompt, image_path, use_secondary_model=use_secondary_model, pool=pool)
        try:
            return extract_json_object(raw_text)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
    raise Exception(f"Model geçerli JSON döndürmedi: {last_error}")

# ---------------------------------------------------------
# EVRENSEL AJAN 1: UNIVERSAL INDUSTRIAL SCANNER & OCR
# ---------------------------------------------------------
def vision_agent(image_path: str) -> dict:
    prompt = """
    Sen ağır vasıta, tır, kamyon, iş makinesi ve otobüslere ait KÜRESEL ÇAPTAKİ TÜM YEDEK PARÇALARI (Fren, Havalı Sistem, Filtreler, Süspansiyon, Sensörler, Dişliler, Valfler, Pompalar vb.) kusursuz tanıyan evrensel bir yapay zeka mühendisisin.

    ÖNCE ŞUNU KONTROL ET: Görselde gerçekten ağır vasıta/kamyon/otobüs/iş makinesi yedek parçası var mı? Görsel bulanık, alakasız (insan, hayvan, belge, ekran görüntüsü, iç mekan vb.) veya boşsa, ya da hiçbir teknik parça özelliği ayırt edilemiyorsa 'is_part_detected' alanını false yap ve diğer alanları boş/null bırak - ASLA görselde olmayan bir parça uydurma.

    Görselde gerçek bir parça varsa, parça ne kadar kirli, paslı, yağlı veya kötü açıyla çekilmiş olursa olsun odaklan ve şu teknik verileri çıkar:

    1. OCR Optik Karakter Taraması: Parça üzerindeki döküm yazılarını, OEM numaralarını, silik etiketleri ve seri numaralarını harf harf oku. KARIŞABİLECEK KARAKTERLERE ÖZELLİKLE DİKKAT ET: 0 (sıfır) ile O (harf), 1 (bir) ile I/l (harf), 8 ile B, 5 ile S sık karışır - döküm derinliği, yazı tipi ve çevredeki diğer karakterlerin deseninden hangisi olduğuna dikkatlice karar ver. Bir kod net okunamıyor/yarısı silikse, o kodu ocr_extracted_codes'a EKLEME (yanlış kod eklemek hiç kod eklememekten daha kötüdür).
    2. Topolojik Mühendislik Haritası: Parçanın rekorlarını, dişli hatve yapılarını, cıvata/montaj delik sayısını, elektrik pin/soketlerini detaylı say.
    3. Geometrik Sınıflandırma: Parçanın ana kategorisini (Örn: Fren Sistemleri, Hava Valfleri, Filtrasyon, Hidrolik vb.) ve tam adını belirle.

    Çıktıyı SADECE ve kesinlikle şu JSON formatında ver:
    {
      "is_part_detected": true,
      "universal_category": "Fren / Hava Sistemi / Filtre / Süspansiyon / Diğer",
      "exact_name_classification": "Parçanın Sektörel Net Adı",
      "ocr_extracted_codes": ["Kod1", "Kod2", "Net okunamayan/şüpheli kod yoksa boş liste"],
      "topology_map": {
        "ports_or_threads": "Rekor, boru veya dişli bağlantı detayları ve sayıları",
        "electrical_pins_or_sockets": "Elektronik soket, pin veya sensör uçları",
        "mounting_holes_and_flanges": "Civata delikleri, kulaklar veya flanş yapısı"
      },
      "geometry_and_material": "Malzeme cinsi (Alüminyum döküm, sac, plastik, balata materyali vb.) ve fiziksel form"
    }
    Görselde parça tespit edilemediyse SADECE şunu döndür: {"is_part_detected": false}
    """
    try:
        return call_groq_json(prompt, image_path)
    except Exception as e:
        raise Exception(f"Universal Tarama Hatası: {str(e)}")

# ---------------------------------------------------------
# EVRENSEL AJAN 2: UNIVERSAL PRECISION MATCHER
# ---------------------------------------------------------
def _select_search_candidates(catalog: list, keywords: list, exact_codes: set = None, max_candidates: int = 30) -> list:
    """Eşleştirme prompt'una TÜM kataloğu gömmek yerine makul bir aday alt kümesi seçer.
    Katalog küçükken (onlarca ürün) sorun yaratmıyordu, ama büyüdükçe (yüzlerce/binlerce ürün,
    art arda birden fazla katalog PDF'i yüklendikçe kaçınılmaz) her TEK arama isteği Groq'un
    dakikalık token sınırını (TPM) aşıp sürekli 413/429 ile başarısız olmaya başlardı - gerçek
    bir kullanımda yoğun TEK bir katalog sayfası bile bu sınırları zorluyordu, tüm katalog çok
    daha büyük olacaktı. `exact_codes` içindeki OEM/id'lerle eşleşenler HER ZAMAN dahil edilir
    (LLM'in kod-çakışması/OCR-hatası kontrolünü kaybetmemek için); geri kalan yer anahtar
    kelimelerle (kategori/isim) en çok örtüşen ürünlerle doldurulur."""
    exact_codes = {turkish_lower(c) for c in (exact_codes or set()) if c}
    exact_items, exact_ids, rest = [], set(), []
    for item in catalog:
        oem = turkish_lower(str(item.get("oem", "")).strip())
        pid = turkish_lower(str(item.get("id", "")).strip())
        if exact_codes and (oem in exact_codes or pid in exact_codes):
            exact_items.append(item)
            exact_ids.add(id(item))
        else:
            rest.append(item)

    words = [turkish_lower(w) for w in keywords if w and len(w) > 2]
    scored = []
    for item in rest:
        hay = turkish_lower(" ".join(str(item.get(f, "")) for f in ("name", "specs", "brand", "oem", "id")))
        hits = sum(1 for w in words if w in hay)
        if hits:
            scored.append((hits, item))
    scored.sort(key=lambda t: -t[0])

    remaining_slots = max(0, max_candidates - len(exact_items))
    candidates = exact_items + [item for _, item in scored[:remaining_slots]]
    if not candidates:
        # Ne kod eşleşmesi ne anahtar kelime örtüşmesi oldu - körü körüne göndermemek için
        # yine de bir örneklem gönder, LLM'in "hiçbir aday yoktu" deyip NOT_IN_CATALOG demesi
        # boş prompt göndermekten daha güvenli/tutarlı.
        candidates = catalog[:max_candidates]
    return candidates


def match_agent(vision_data: dict) -> dict:
    catalog = load_catalog()
    if not catalog:
        return {"id": "NOT_IN_CATALOG", "name": "Katalog Boş", "match_reason": "Veritabanında kayıtlı ürün bulunamadı."}

    keywords = [vision_data.get("universal_category", ""), vision_data.get("exact_name_classification", "")]
    candidates = _select_search_candidates(catalog, keywords, exact_codes=set(vision_data.get("ocr_extracted_codes", []) or []))

    prompt = f"""
    Sen sıfır hata toleransına sahip kurumsal bir parça eşleştirme motorusun.
    Müşterinin sahadan gönderdiği parçanın tarama verisi:
    {json.dumps(vision_data, ensure_ascii=False)}

    Sistemimizdeki Parça Katalog Veritabanından İlgili Adaylar ({len(candidates)}/{len(catalog)} kayıt - kod eşleşmesi ve kategori benzerliğine göre önceden daraltıldı):
    {json.dumps(candidates, ensure_ascii=False)}

    EŞLEŞTİRME PRENSİPLERİ:
    1. OEM / KOD EŞLEŞMESİ: Tarama verisindeki 'ocr_extracted_codes' içindeki herhangi bir kod katalogdaki 'oem' veya 'id' ile uyuşuyorsa YÜKSEK bir güven skoru (90-100) VER, FAKAT önce şu kritik kontrolü yap: eşleşen katalog ürününün kategorisi/'specs' bilgisi ile taranan parçanın 'universal_category', 'topology_map' ve 'geometry_and_material' bilgisi AÇIKÇA ÇELİŞİYORSA (örn. kod bir "hava valfi"ne ait ama taranan parça net biçimde bir "fren balatası" görünümündeyse), bu muhtemelen bir OCR OKUMA HATASI sonucu tesadüfi bir KOD ÇAKIŞMASIDIR - bu durumda güven skorunu 40'ın altına düşür ve decision_logic'te bu çelişkiyi açıkça belirt ("Kod eşleşti ama fiziksel özellikler uyuşmuyor, muhtemelen OCR hatası" gibi).
    2. TOPOLOJİK UYUM: Kod okunamadıysa; parça kategorisi, rekor/delik sayıları ve fiziksel özellikleri katalogdaki ürünlerin 'specs' bilgileriyle kıyaslanır. Uyum oranı hesaplanır.
    3. BELİRSİZLİK: Katalogda birden fazla ürün taranan parçaya benzer derecede uygunsa (aralarında net bir ayrım yapılamıyorsa), bunu kesin bir eşleşme gibi sunma - güven skorunu 60'ın altında tut ve decision_logic'te hangi ürünler arasında belirsizlik olduğunu belirt.
    4. Eşleşme skoru %70'in altındaysa kesinlikle yanlış parça riskine girilmez ve 'NOT_IN_CATALOG' döndürülür.

    Çıktı SADECE şu JSON yapısında olmalıdır:
    {{
      "matched_id": "katalog_id_yada_NOT_IN_CATALOG",
      "match_accuracy_score": 92,
      "decision_logic": "Neden eşleştiğine dair net teknik mühendislik kanıtı"
    }}
    """
    try:
        result = call_groq_json(prompt)
        matched_id = result.get("matched_id")
        score = int(result.get("match_accuracy_score", 0))
        decision = result.get("decision_logic", "")

        if matched_id and matched_id != "NOT_IN_CATALOG" and score >= 70:
            matches = [item for item in catalog if str(item.get("id")) == str(matched_id)]
            if len(matches) > 1:
                # Katalogda aynı id'ye sahip birden fazla kayıt var - veri bütünlüğü sorunu,
                # yanlış/eski kaydı sunmaktansa açıkça "eşleşme sağlanamadı" demek daha güvenli
                return {
                    "id": "NOT_IN_CATALOG",
                    "name": "Katalog Veri Çakışması",
                    "match_reason": f"'{matched_id}' koduna sahip birden fazla katalog kaydı bulundu - lütfen kataloğu kontrol edin, otomatik eşleştirme güvenli değil."
                }
            if matches:
                item_copy = matches[0].copy()
                item_copy["match_score"] = score
                item_copy["match_evidence"] = decision
                return item_copy

        return {
            "id": "NOT_IN_CATALOG",
            "name": "Katalog Dışı / Eşleşme Sağlanamadı",
            "match_reason": f"Benzerlik skoru (%{score}) yeterli eşik değerinin altında kaldı. Kanıt: {decision}"
        }
    except Exception as e:
        raise Exception(f"Eşleştirme Motoru Hatası: {str(e)}")

# ---------------------------------------------------------
# EVRENSEL AJAN 2b: METİN TABANLI ARAMA (OEM KODU / PARÇA ADI)
# ---------------------------------------------------------
def find_by_text(query: str) -> dict:
    """Fotoğraf olmadan, girilen OEM kodu veya parça adına göre kataloğu arar."""
    catalog = load_catalog()
    if not catalog:
        return {"id": "NOT_IN_CATALOG", "name": "Katalog Boş", "match_reason": "Veritabanında kayıtlı ürün bulunamadı."}

    q = turkish_lower(query.strip())

    # 1. Önce birebir OEM/ID eşleşmesi dene (hızlı ve %100 güvenilir, yapay zekaya gerek yok)
    # turkish_lower kullanılıyor: normal .lower() Türkçe büyük 'İ'yi 'i'ye değil görünmez bir
    # noktalama işaretine çeviriyor - örn. müşteri "İVECO 12345" yazınca katalogdaki "iveco
    # 12345" ile eşleşmeyebiliyordu.
    exact_matches = [
        item for item in catalog
        if q == turkish_lower(str(item.get("oem", "")).strip()) or q == turkish_lower(str(item.get("id", "")).strip())
    ]
    if len(exact_matches) > 1:
        # Aynı koda sahip birden fazla katalog kaydı - veri bütünlüğü sorunu, rastgele birini
        # seçmek yerine açıkça uyar (bu normalde merge_catalog_items ile önlenir, ekstra güvenlik)
        return {
            "id": "NOT_IN_CATALOG",
            "name": "Katalog Veri Çakışması",
            "match_reason": f"'{query}' koduna sahip birden fazla katalog kaydı bulundu - lütfen kataloğu kontrol edin."
        }
    if exact_matches:
        item_copy = exact_matches[0].copy()
        item_copy["match_score"] = 100
        item_copy["match_evidence"] = "OEM/katalog kodu ile birebir eşleşme"
        return item_copy

    # 2. Birebir kod eşleşmesi yoksa, yapay zekaya isim/açıklama bazlı eşleştirt (örn: "DAF sol çamurluk")
    candidates = _select_search_candidates(catalog, query.split())
    prompt = f"""
    Sen sıfır hata toleransına sahip kurumsal bir parça eşleştirme motorusun.
    Sahadaki kullanıcı, elinde fotoğraf olmadan şu metni yazdı: "{query}"
    Bu metin bir OEM kodu, marka+parça adı (örn: "DAF sol çamurluk") veya serbest bir açıklama olabilir.

    Sistemimizdeki Parça Katalog Veritabanından İlgili Adaylar ({len(candidates)}/{len(catalog)} kayıt - anahtar kelime benzerliğine göre önceden daraltıldı):
    {json.dumps(candidates, ensure_ascii=False)}

    EŞLEŞTİRME PRENSİPLERİ:
    1. Yazılan metin katalogdaki 'oem' veya 'id' alanıyla TAM olarak uyuyorsa güven skoru yüksek olmalıdır (bu zaten kod içinde ayrıca kontrol edildi, buraya gelmiş olması tam eşleşme OLMADIĞI anlamına gelir). SADECE KISMİ/parçalı bir kod benzerliği varsa (örn. birkaç hane ortak) bunu asla yüksek güven sayma - kısmi kod benzerliği yanlış parça riski taşır, güven skorunu en fazla 50-60 civarında tut ve NOT_IN_CATALOG'a düşmesine izin ver.
    2. Kod uyuşmuyorsa, metindeki marka/parça adı katalogdaki 'name', 'brand' ve 'specs' alanlarıyla anlam olarak kıyaslanır - bu tür isim/açıklama bazlı eşleşmeler net ve tek bir aday varsa yüksek güven alabilir.
    3. BELİRSİZLİK: Katalogda birden fazla ürün yazılan metne benzer derecede uygunsa, kesin bir eşleşme gibi sunma - güven skorunu 60'ın altında tut ve decision_logic'te belirsizliği belirt.
    4. Eşleşme skoru %70'in altındaysa kesinlikle yanlış parça riskine girilmez ve 'NOT_IN_CATALOG' döndürülür.

    Çıktı SADECE şu JSON yapısında olmalıdır:
    {{
      "matched_id": "katalog_id_yada_NOT_IN_CATALOG",
      "match_accuracy_score": 92,
      "decision_logic": "Neden eşleştiğine dair net teknik mühendislik kanıtı"
    }}
    """
    try:
        result = call_groq_json(prompt)
        matched_id = result.get("matched_id")
        score = int(result.get("match_accuracy_score", 0))
        decision = result.get("decision_logic", "")

        if matched_id and matched_id != "NOT_IN_CATALOG" and score >= 70:
            matches = [item for item in catalog if str(item.get("id")) == str(matched_id)]
            if len(matches) > 1:
                return {
                    "id": "NOT_IN_CATALOG",
                    "name": "Katalog Veri Çakışması",
                    "match_reason": f"'{matched_id}' koduna sahip birden fazla katalog kaydı bulundu - lütfen kataloğu kontrol edin, otomatik eşleştirme güvenli değil."
                }
            if matches:
                item_copy = matches[0].copy()
                item_copy["match_score"] = score
                item_copy["match_evidence"] = decision
                return item_copy

        return {
            "id": "NOT_IN_CATALOG",
            "name": "Katalog Dışı / Eşleşme Sağlanamadı",
            "match_reason": f"Benzerlik skoru (%{score}) yeterli eşik değerinin altında kaldı. Kanıt: {decision}"
        }
    except Exception as e:
        raise Exception(f"Metin Arama Motoru Hatası: {str(e)}")

# ---------------------------------------------------------
# FASTAPI ENDPOINT MİMARİSİ
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
        return html.replace("__BUSINESS_WHATSAPP_NUMBER__", BUSINESS_WHATSAPP_NUMBER)
    except Exception:
        return "<h2>FleetParts AI - Universal Heavy Duty Master Engine Aktif</h2>"

@app.get("/katalog", response_class=HTMLResponse)
def read_katalog():
    """Herkese açık, giriş gerektirmeyen katalog vitrini - soğuk lead'lerin kendi
    WhatsApp'larından ilk temas kurabileceği (opt-in) sayfa."""
    try:
        with open("katalog.html", "r", encoding="utf-8") as f:
            html = f.read()
        return html.replace("__BUSINESS_WHATSAPP_NUMBER__", BUSINESS_WHATSAPP_NUMBER)
    except Exception:
        return "<h2>Katalog sayfası bulunamadı</h2>"

@app.get("/get-catalog")
def get_catalog_endpoint():
    return {"catalog": load_catalog(), "files": os.listdir(CATALOG_DIR)}

@app.post("/delete-catalog-item")
def delete_catalog_item(item_id: str = Form(...), _: str = Depends(require_admin)):
    """Yapay zeka taramasının ürettiği hatalı/alakasız bir kaydı (ör. bir kapak sayfasındaki
    logonun yanlışlıkla parça sanılması) katalogdan çıkarmak için - eskiden bunun için hiçbir
    yol yoktu, kötü bir kayıt kalıcı olarak katalogda takılı kalıyordu."""
    with CATALOG_WRITE_LOCK:
        catalog = _load_catalog_from_disk()
        new_catalog = [item for item in catalog if str(item.get("id")) != str(item_id)]
        if len(new_catalog) == len(catalog):
            return {"status": "error", "message": f"'{item_id}' id'li bir kayıt bulunamadı."}
        save_catalog(new_catalog)
    sync_catalog_to_github()
    _catalog_cache["data"] = new_catalog
    _catalog_cache["fetched_at"] = time.time()
    return {"status": "success", "message": "Kayıt silindi.", "remaining": len(new_catalog)}

@app.get("/katalog-yonetim", response_class=HTMLResponse)
def read_katalog_yonetim(_: str = Depends(require_admin)):
    """Katalog yükleme/görüntüleme - eskiden herkese açık ana sayfadaydı (index.html), gerçek bir
    güvenlik incelemesinde tespit edildi ki bu, siteyi bulan HERKESİN katalog yükleyip yapay zeka
    kotasını tüketebilmesi/kataloğa yanlış veri karıştırabilmesi anlamına geliyordu - bu yüzden
    diğer yönetici sayfalarıyla (leads, broadcast) aynı korumaya alınıp buraya taşındı."""
    try:
        with open("katalog-yonetim.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "<h2>Katalog yönetim sayfası bulunamadı</h2>"

CATALOG_SCAN_PROMPT = """
Bu, ağır vasıta yedek parça kataloğuna ait bir sayfa, fotoğraf veya web sayfası ekran görüntüsü.
Görselde TEK bir parça olabileceği gibi, bir tabloda/gridde ONLARCA farklı parça da olabilir
(farklı ölçü, renk veya varyant olarak listelenmiş olsa bile HER SATIR/HER VARYANT ayrı bir parçadır).
Görseldeki HER BİR parçayı tek tek tara ve çıkar. Görselde hiç parça yoksa (kapak sayfası, boş sayfa vb.) boş liste döndür.

ÖNEMLİ - YOĞUN TABLO SAYFALARI: Sayfada çok sayıda satır/ürün varsa (örn. 15-30+ satırlık bir
fiyat listesi tablosu), 'specs' alanını KISA tut (tek kısa cümle veya birkaç kelime, uzun teknik
açıklama YAZMA) - yanıt uzunluğu sınırlı, çok sayıda ürünü TAM ve EKSİKSİZ çıkarmak, az sayıda
ürünü uzun uzun anlatmaktan çok daha önemli. Hiçbir satırı ASLA atlama.

Bir üründe birden fazla kod görünüyorsa (kendi kod sistemi + üretici/OEM referans numarası gibi),
'oem' alanına en belirgin/asıl ürün kodunu yaz.

MARKA KURALI (ÇOK ÖNEMLİ): 'brand' alanına SADECE görselde/katalog sayfasında AÇIKÇA YAZILI OLARAK
görünen bir üretici/marka adı varsa yaz. Ürünün tipinden, şeklinden veya genel izlenimden marka
TAHMİN ETME veya UYDURMA. Görselde/katalogda hiçbir marka adı yazmıyorsa ya da emin değilsen,
'brand' alanını kesinlikle boş string ("") bırak - yanlış marka bilgisi vermek boş bırakmaktan
çok daha kötüdür.

SADECE şu JSON yapısında bir DİZİ (array) döndür, başka hiçbir şey yazma:
[
    {
        "id": "PRC-" + rasgele 4 haneli sayı,
        "oem": "Parçanın kod/OEM numarası (Yoksa 'OEM-BELİRSİZ')",
        "name": "Parçanın adı (varsa ölçü/renk/varyant bilgisiyle birlikte)",
        "brand": "SADECE görselde açıkça yazılı olan üretici/marka adı - yoksa/emin değilsen kesinlikle boş string",
        "specs": "Ölçüler, bağlantı tipi, malzeme ve diğer teknik detaylar",
        "stock": 25
    }
]
"""

def _salvage_partial_json_objects(text: str) -> list:
    """Model yanıtı (çok yoğun/kalabalık bir sayfa yüzünden) max_tokens sınırına takılıp
    yarıda kesilirse, dizinin kapanış ']' işareti hiç gelmez ve normal json.loads tamamen
    başarısız olur - halbuki dizinin İLK N ÖĞESİ genelde tam ve geçerlidir, sadece SONUNCU
    (yarım kalan) öğe bozuktur. Köşeli parantez derinliğini elle sayarak dizideki her TAM
    {...} nesnesini tek tek çıkarır, sadece yarım kalan son nesneyi atar - böylece bir sayfada
    30 üründen 29'u tam çıkmışsa o 29'u kaybetmeyiz, sadece 1 tanesini."""
    items = []
    depth = 0
    obj_start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    obj = json.loads(text[obj_start:i + 1])
                    if isinstance(obj, dict):
                        items.append(obj)
                except (ValueError, json.JSONDecodeError):
                    pass
                obj_start = None
    return items

def extract_json_array(raw_text: str) -> list:
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()
    start = clean_text.find("[")
    if start == -1:
        raise ValueError(f"Yanıtta JSON dizisi bulunamadı: {raw_text[:200]!r}")
    end = clean_text.rfind("]") + 1
    if end > start:
        try:
            data = json.loads(clean_text[start:end])
            return data if isinstance(data, list) else []
        except (ValueError, json.JSONDecodeError):
            pass  # tam dizi bozuk (muhtemelen yarıda kesilmiş) - aşağıda parça parça kurtarmayı dene
    salvaged = _salvage_partial_json_objects(clean_text[start + 1:])
    if not salvaged:
        raise ValueError(f"Yanıtta geçerli JSON dizisi bulunamadı: {raw_text[:200]!r}")
    return salvaged

def call_groq_json_array(prompt: str, image_path: str = None, use_secondary_model: bool = False, pool: str = "customer") -> list:
    last_error = None
    for attempt in range(2):
        raw_text = call_groq_api(prompt, image_path, use_secondary_model=use_secondary_model, pool=pool)
        try:
            return extract_json_array(raw_text)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
    raise Exception(f"Model geçerli JSON dizisi döndürmedi: {last_error}")

def render_pdf_pages_to_images(pdf_path: str, filename: str = None, already_scanned: set = None):
    """PDF'in her sayfasını PNG görsele çevirir, sayfa numarası/etiketi/yolunu tek tek (generator
    olarak) üretir. ÖNEMLİ: eskiden TÜM sayfalar tek seferde (bir liste doldurulup) render edilip
    öyle döndürülüyordu - 40 sayfalık yoğun bir PDF'de bu, herhangi bir Groq çağrısı başlamadan
    ÖNCE tek bir sürekli CPU/bellek yüklü blok oluşturuyordu ve gerçek bir kullanımda Render'ın
    süreci ortasında yeniden başlatmasına (muhtemelen kaynak sınırı) yol açtığı tespit edildi.
    Artık her sayfa SIRAYLA render edilip hemen kullanılıyor, Groq çağrılarıyla iç içe - tek
    seferde bellekte/CPU'da en fazla bir sayfa var.

    `already_scanned` (bu dosyanın 'filename (sayfa N)' etiketli, daha önce başarıyla taranmış
    sayfalarının kümesi) verilirse o sayfalar hiç render edilmeden (fitz/PNG maliyeti olmadan)
    atlanır - aynı dosyayı ikinci kez yüklerken zaten bitmiş sayfaları boşuna render edip zaman/
    CPU harcamamak için."""
    already_scanned = already_scanned or set()
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    zoom_matrix = fitz.Matrix(2.0, 2.0)  # ~144 DPI, OCR için yeterli netlik
    doc = fitz.open(pdf_path)
    try:
        for page_index in range(len(doc)):
            page_num = page_index + 1
            page_label = f"{filename} (sayfa {page_num})"
            if page_label in already_scanned:
                continue
            pix = doc[page_index].get_pixmap(matrix=zoom_matrix)
            page_path = os.path.join(CATALOG_DIR, f"{base_name}_sayfa{page_num}.png")
            pix.save(page_path)
            del pix
            yield page_num, page_label, page_path
    finally:
        doc.close()

CATALOG_WRITE_LOCK = threading.Lock()  # istekler arası paylaşılan kilit - iki ayrı yükleme isteği aynı anda gelirse birbirinin kaydını ezmesin diye

def merge_catalog_items(catalog: list, new_items: list) -> tuple:
    """Yeni taranan parçaları kataloğa ekler. Aynı OEM koduna sahip bir parça (aynı ürün iki farklı
    katalog dosyasında/sayfasında geçmişse) tekrar eklenmez, mevcut kayıt güncellenir - katalogda
    aynı ürünün birden fazla kopyası birikmesin diye. OEM kodu boş/'OEM-BELİRSİZ' olan parçalar
    güvenilir şekilde eşleştirilemeyeceği için (yanlışlıkla farklı iki ürünü birleştirmemek adına)
    her zaman yeni kayıt olarak eklenir."""
    oem_index = {
        str(item.get("oem", "")).strip().lower(): idx
        for idx, item in enumerate(catalog)
        if item.get("oem") and str(item.get("oem")).strip().upper() != "OEM-BELİRSİZ"
    }
    added = 0
    updated = 0
    for new_item in new_items:
        oem_key = str(new_item.get("oem", "")).strip().lower()
        is_known_oem = bool(oem_key) and oem_key != "oem-belirsiz"
        if is_known_oem and oem_key in oem_index:
            catalog[oem_index[oem_key]] = new_item
            updated += 1
        else:
            catalog.append(new_item)
            if is_known_oem:
                oem_index[oem_key] = len(catalog) - 1
            added += 1
    return catalog, added, updated

def _scan_catalog_source(filename: str, file_path: str, on_page_done=None) -> list:
    """Bir katalog dosyasını tarar. PDF ise her sayfayı, görselse görselin kendisini tarar;
    her ikisinde de sayfada/görselde kaç parça varsa hepsi çıkarılır (tek parça da olabilir, onlarca da).
    Her çıkarılan parçaya hangi dosyadan (ve PDF'se hangi sayfadan) geldiği 'source_file' alanıyla
    işlenir - ileride aynı ürün birden fazla katalogda geçtiğinde hangisinden alındığı görülebilsin diye.
    İşlem başarılı da olsa başarısız da olsa orijinal yüklenen dosya sonunda diskten silinir
    (veri zaten catalog.json'a işlendi, kaynak dosyayı tutmanın bir faydası yok - Render'ın
    sınırlı diskini zamanla doldurmasın diye).

    `on_page_done` verilirse her sayfa/görsel bittiğinde (o sayfanın ürünleriyle) hemen çağrılır -
    çağıran bunu kataloğa ANINDA kaydetmek için kullanır. Çok sayfalı bir PDF ortasında sunucu
    çökerse/yeniden başlarsa (gerçek bir kullanımda oldu - Render kaynak sınırı ya da platform
    kaynaklı olabilir), TÜM dosyanın sonunu beklemek yerine o ana kadar taranan sayfalar zaten
    kalıcı olarak kaydedilmiş olur - ne veri ne harcanan token boşa gitmez.

    ÖNEMLİ: aynı dosya (aynı orijinal ad) ikinci kez yüklenirse - örn. kota bitip yarıda kalan
    bir taramayı tamamlamak için - daha önce başarıyla taranmış sayfalar TEKRAR Groq'a
    gönderilmez, kataloğun kendisinden ('source_file' alanına bakarak) hangi sayfaların zaten
    işlendiği anlaşılır ve atlanır. Gerçek bir kullanımda tespit edildi: bu kontrol olmadan her
    yeniden deneme TÜM dosyayı baştan tarıyor, kotayı gereksiz yere 2-3 kat hızlı tüketiyordu."""
    try:
        if filename.lower().endswith(".pdf"):
            already_scanned = {
                item.get("source_file") for item in _load_catalog_from_disk()
                if isinstance(item.get("source_file"), str) and item["source_file"].startswith(f"{filename} (sayfa ")
            }
            items = []
            for _page_num, page_label, page_path in render_pdf_pages_to_images(file_path, filename=filename, already_scanned=already_scanned):
                try:
                    page_items = call_groq_json_array(CATALOG_SCAN_PROMPT, page_path, pool="bulk")
                    for item in page_items:
                        item["source_file"] = page_label
                    items.extend(page_items)
                    if on_page_done:
                        on_page_done(page_items)
                finally:
                    if os.path.exists(page_path):
                        os.remove(page_path)
            return items
        items = call_groq_json_array(CATALOG_SCAN_PROMPT, file_path, pool="bulk")
        for item in items:
            item["source_file"] = filename
        if on_page_done:
            on_page_done(items)
        return items
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# Katalog taraması (özellikle çok sayfalı bir PDF) birkaç dakika sürebilir - bunu TEK bir HTTP
# isteği içinde bekletmek gerçek bir kullanımda sürekli başarısız oluyordu (Render'ın önündeki
# proxy/worker, uzun süre yanıt gelmeyen istekleri belirli bir süre sonra kesiyor - istemci bunu
# "sunucu uyandırılıyor" sanıp TÜM dosyayı BAŞTAN yeniden gönderiyordu, bu da hem asla
# bitmeyen bir döngü hem de gereksiz tekrarlanan token harcaması demekti, gerçek bir denemede
# tespit edildi). Çözüm: yükleme isteği dosyaları kaydedip HEMEN bir "iş no" ile döner, gerçek
# tarama arka planda bir iş parçacığında devam eder, istemci ayrı bir uç noktadan (durum sorgusu)
# birkaç saniyede bir "ne durumda" diye sorar - hiçbir tek istek uzun süre açık kalmaz.
_catalog_jobs = {}
_catalog_jobs_lock = threading.Lock()


def _run_catalog_upload_job(job_id: str, saved_paths: list):
    job = _catalog_jobs[job_id]
    added_count = 0
    updated_count = 0
    failed = list(job["failed"])
    pages_since_sync = 0
    last_sync_time = time.time()

    def save_page_items(page_items):
        # Her sayfa bitince HEMEN yerel diske kaydedilir - dosyanın tamamının bitmesini beklemez.
        # GitHub'a gönderme (asıl kalıcılık - Render'ın diski her yeniden deploy'da sıfırlanıyor)
        # ise her sayfada DEĞİL, en fazla birkaç sayfada bir yapılır: GitHub'a PUT edilen içerik
        # TÜM kataloğun o anki hali (delta değil), katalog büyüdükçe (yüzlerce sayfa/ürün) bu
        # her sayfada tekrar tekrar büyüyen bir yükü ağa göndermek demekti - gerçek bir kullanımda
        # yoğun bir dosyada bunun toplam süreyi ciddi uzattığı ve sunucunun sayfa sayfa
        # ilerlerken periyodik olarak yeniden başlamasıyla çakıştığı gözlemlendi. Yine de veri
        # KAYBI riski yok: yerel kayıt her sayfada oluyor, sadece GitHub'a itme seyrekleştirildi.
        nonlocal added_count, updated_count, pages_since_sync, last_sync_time
        if not page_items:
            return
        with CATALOG_WRITE_LOCK:
            catalog = _load_catalog_from_disk()
            catalog, added, updated = merge_catalog_items(catalog, page_items)
            added_count += added
            updated_count += updated
            save_catalog(catalog)
        _catalog_cache["data"] = catalog
        _catalog_cache["fetched_at"] = time.time()
        pages_since_sync += 1
        if pages_since_sync >= 3 or (time.time() - last_sync_time) >= 20:
            sync_catalog_to_github()
            pages_since_sync = 0
            last_sync_time = time.time()
        with _catalog_jobs_lock:
            job["pages_done"] += 1

    for filename, file_path in saved_paths:
        with _catalog_jobs_lock:
            job["current_file"] = filename
        try:
            _scan_catalog_source(filename, file_path, on_page_done=save_page_items)
        except Exception as e:
            failed.append(f"{filename}: {str(e)}")
        with _catalog_jobs_lock:
            job["done_files"] += 1

    if pages_since_sync:
        # Son birkaç sayfa henüz GitHub'a itilmediyse (seyreltme yüzünden) işin sonunda kesin itilir.
        sync_catalog_to_github()

    message = f"{added_count} adet yeni parça eklendi."
    if updated_count:
        message += f" {updated_count} adet zaten katalogda vardı, bilgileri güncellendi."
    if failed:
        message += f" {len(failed)} dosya işlenemedi: " + "; ".join(failed)

    with _catalog_jobs_lock:
        job["status"] = "success" if (added_count > 0 or updated_count > 0 or not failed) else "error"
        job["message"] = message
        job["current_file"] = None


@app.post("/upload-catalog-files")
def upload_catalog_files(files: list[UploadFile] = File(...), _: str = Depends(require_admin)):
    # NOT: bu endpoint eskiden korumasızdı - ana sayfa (index.html) herkese açık olduğu için
    # siteyi bulan HERKES katalog yükleyip hem yapay zeka kotasını (bulk havuz) tüketebilir hem
    # de kataloga yanlış/saçma veri karıştırabilirdi (merge_catalog_items ne gelirse birleştirir).
    # Gerçek bir güvenlik taramasında tespit edildi - diğer tüm yönetici işlemleriyle (leads,
    # broadcast vb.) aynı require_admin korumasına alındı.
    # Günlük yapay zeka kotası zaten neredeyse bittiyse (katalog taraması en pahalı işlemdir,
    # her sayfa bir görsel analizi gerektirir) hiç denemeden önceden net bir uyarı ver - aksi
    # halde her dosya tek tek başarısız olur, kullanıcı neden olduğunu anlamadan zaman kaybeder.
    # Katalog taraması artık bulk->customer->customer2 zincirini deniyor (bkz. _resolve_key_chain) -
    # zincirdeki HERHANGİ bir hesapta yer varsa engelleme, gerçek tarama sırasında zaten sıradaki
    # hesaba geçilecek. Gerçek bir kullanımda tespit edildi: kendi saydığımız token tahmini
    # Groq'un gerçek günlük limitinden farklı çıkabiliyor, bu yüzden bu kontrol sadece kaba bir
    # ön uyarı - asıl güvence tarama sırasındaki otomatik hesap geçişi.
    usage = usage_tracker.get_today_usage()
    chain_pools = {label for label, _ in _resolve_key_chain("bulk", False)}

    def _remaining(pool):
        real = usage_tracker.get_real_remaining_tokens(pool)
        return real if real is not None else usage.get(pool, {}).get("remaining_estimate", usage_tracker.DAILY_TOKEN_BUDGET)

    any_room = any(_remaining(p) >= 2000 for p in chain_pools)
    if not any_room:
        return {
            "status": "error",
            "message": "Bugünkü yapay zeka kullanım kotası (denenen tüm hesaplarda) doldu. "
                       "Kota gece (Groq sıfırlama saatinde) yenilenir, o zaman tekrar deneyin."
        }

    saved_paths = []
    oversized = []

    for file in files:
        # Dosya adına rastgele önek eklenir: hem aynı anda gelen iki isteğin aynı dosya adını
        # kullanıp birbirinin üzerine yazmasını (veri karışması) hem de dosya adı üzerinden
        # dizin gezinme (path traversal) girişimlerini engeller.
        safe_name = f"{uuid.uuid4().hex}_{os.path.basename(file.filename)}"
        file_path = os.path.join(CATALOG_DIR, safe_name)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            oversized.append(f"{file.filename} ({size_mb:.1f}MB, {MAX_UPLOAD_MB}MB sınırını aşıyor)")
            os.remove(file_path)
            continue
        saved_paths.append((file.filename, file_path))

    job_id = uuid.uuid4().hex
    with _catalog_jobs_lock:
        _catalog_jobs[job_id] = {
            "status": "processing",
            "total_files": len(saved_paths),
            "done_files": 0,
            "pages_done": 0,
            "current_file": saved_paths[0][0] if saved_paths else None,
            "failed": list(oversized),
            "message": "",
        }
    threading.Thread(target=_run_catalog_upload_job, args=(job_id, saved_paths), daemon=True).start()
    return {"status": "processing", "job_id": job_id, "total_files": len(saved_paths)}


@app.get("/upload-catalog-status/{job_id}")
def get_upload_catalog_status(job_id: str, _: str = Depends(require_admin)):
    with _catalog_jobs_lock:
        job = _catalog_jobs.get(job_id)
        if not job:
            # Sunucu tarama ortasında yeniden başlamış olabilir (Render kaynak sınırı vb.) - ama
            # her sayfa taranır taranmaz kalıcı kaydedildiği için (bkz. _run_catalog_upload_job)
            # o ana kadarki ilerleme KAYBOLMAZ, sadece iş takibi sıfırlanır. Kullanıcıyı
            # korkutmak yerine ne yapması gerektiğini net söylüyoruz.
            return {
                "status": "error",
                "resumable": True,
                "message": "Sunucu tarama sırasında yeniden başlamış olabilir. Merak etme, o ana kadar taranan sayfalar zaten kalıcı olarak kaydedildi (\"Şu Anki Kataloğu Görüntüle\" ile kontrol edebilirsin). Aynı dosyayı tekrar yüklersen zaten eklenmiş ürünler tekrar eklenmez, sadece kalanlar taranır."
            }
        return dict(job)

# ---------------------------------------------------------
# FAZ 2: LEAD KEŞFİ / İNCELEME (korumalı admin sayfaları)
# ---------------------------------------------------------
@app.get("/leads", response_class=HTMLResponse)
def read_leads(_: str = Depends(require_admin)):
    try:
        with open("leads.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "<h2>Lead sayfası bulunamadı</h2>"

@app.get("/usage")
def get_usage():
    """Bugünkü tahmini Groq token kullanımını döndürür (gerçek API yanıtlarından toplanır).
    Hassas veri içermediği için (sadece toplam token sayısı) herkese açık - hem public
    index.html hem admin sayfaları burayı kullanıyor.

    Her havuz için ayrıca `real_remaining_tokens` alanı var - bizim kendi saydığımız
    `remaining_estimate` bir TAHMİN, bu alan ise (varsa) Groq'un bu süreçte en son gördüğü
    GERÇEK rate-limit header değeri. İkisi arasında büyük fark varsa DAILY_TOKEN_BUDGET
    varsayımının güncellenmesi gerektiğinin işaretidir."""
    result = usage_tracker.get_today_usage()
    for pool in usage_tracker.POOLS:
        if pool in result:
            result[pool]["real_remaining_tokens"] = usage_tracker.get_real_remaining_tokens(pool)
    return result

@app.get("/leads-data")
def get_leads_data(_: str = Depends(require_admin)):
    leads = lead_store.load_leads()
    reviews = lead_store.load_lead_reviews()
    ai_scores = lead_store.load_lead_ai_scores()
    merged = []
    for lead in leads:
        lid = lead.get("lead_id")
        review = reviews.get(lid, {})
        item = dict(lead)
        item["status"] = review.get("status", "yeni")
        item["note"] = review.get("note", "")
        ai = ai_scores.get(lid)
        if ai:
            item["relevance_score"] = ai["relevance_score"]
            item["score_reasoning"] = ai["score_reasoning"]
            item["ai_reviewed"] = True
        merged.append(item)
    return {"leads": merged}

@app.post("/leads/{lead_id}/status")
def update_lead_status(
    lead_id: str,
    status: str = Form(...),
    note: str = Form(""),
    _: str = Depends(require_admin)
):
    reviews = lead_store.load_lead_reviews()
    reviews[lead_id] = {
        "status": status,
        "note": note,
        "reviewed_at": datetime.now(timezone.utc).isoformat()
    }
    lead_store.save_lead_reviews(reviews)
    lead_store.sync_lead_reviews_to_github()
    return {"status": "success"}

@app.post("/leads/bulk-status")
def bulk_update_lead_status(
    lead_ids: str = Form(...),  # virgülle ayrılmış id listesi
    status: str = Form(...),
    _: str = Depends(require_admin)
):
    """Birden fazla lead'i tek seferde aynı duruma işaretler (933 kayıtta tek tek tıklamamak için)."""
    ids = [i.strip() for i in lead_ids.split(",") if i.strip()]
    reviews = lead_store.load_lead_reviews()
    now = datetime.now(timezone.utc).isoformat()
    for lid in ids:
        existing_note = reviews.get(lid, {}).get("note", "")
        reviews[lid] = {"status": status, "note": existing_note, "reviewed_at": now}
    lead_store.save_lead_reviews(reviews)
    lead_store.sync_lead_reviews_to_github()
    return {"status": "success", "message": f"{len(ids)} lead güncellendi."}

@app.post("/leads/add-to-contacts")
def add_leads_to_contacts(
    lead_ids: str = Form(...),  # virgülle ayrılmış id listesi
    _: str = Depends(require_admin)
):
    """Lead Keşfi (Faz 2) ile Müşteri İletişimi (Faz 1) arasındaki köprü: seçilen lead'leri
    mevcut kişi listesine ekler. Otomatik mesaj GÖNDERMEZ - sadece listeye ekler, gönderim
    /broadcast sayfasından her zamanki gibi elle/AI taslağıyla yapılır. Sabit hat numaralı
    lead'ler eklenmez (WhatsApp'tan ulaşılamaz), CSV/liste dışında bırakılmaları için sayılır."""
    ids = [i.strip() for i in lead_ids.split(",") if i.strip()]
    leads_by_id = {l.get("lead_id"): l for l in lead_store.load_leads()}
    contacts = outreach.load_contacts()
    existing_phones = {c.get("phone", "").strip() for c in contacts if c.get("phone")}

    added = 0
    skipped_no_mobile = 0
    skipped_duplicate = 0
    for lid in ids:
        lead = leads_by_id.get(lid)
        if not lead:
            continue
        phone = (lead.get("phone") or "").strip()
        if not phone or not is_mobile_phone(phone):
            skipped_no_mobile += 1
            continue
        if phone in existing_phones:
            skipped_duplicate += 1
            continue
        contacts.append({
            "name": lead.get("company_name", ""),
            "phone": phone,
            "company": lead.get("company_name", ""),
            "opted_in_at": None,
            "source": "lead_discovery",
        })
        existing_phones.add(phone)
        added += 1

    outreach.save_contacts(contacts)
    outreach.sync_contacts_to_github()

    message = f"{added} kişi Müşteri İletişimi listesine eklendi."
    if skipped_no_mobile:
        message += f" {skipped_no_mobile} lead cep telefonu (WhatsApp) numarası olmadığı için eklenmedi."
    if skipped_duplicate:
        message += f" {skipped_duplicate} lead zaten listede vardı."
    return {"status": "success", "message": message, "added": added}

@app.get("/leads-export")
def export_leads_csv(_: str = Depends(require_admin)):
    """Kataloğu Excel'de açılabilir CSV olarak indirir - sahada kağıt/excel üzerinden çalışmak için."""
    leads = lead_store.load_leads()
    reviews = lead_store.load_lead_reviews()
    ai_scores = lead_store.load_lead_ai_scores()  # AI ile netleştirilmiş lead'lerin GÜNCEL skoru buradan gelir

    output = io.StringIO()
    output.write("﻿")  # Excel'in Türkçe karakterleri doğru göstermesi için UTF-8 BOM
    writer = csv.writer(output)
    writer.writerow(["Firma Adı", "Sektör", "İl", "İlçe", "Adres", "Telefon", "Skor", "Durum", "Not", "Gerekçe"])
    for lead in leads:
        lid = lead.get("lead_id")
        review = reviews.get(lid, {})
        ai = ai_scores.get(lid)
        # AI ile netleştirilmiş bir lead'se /leads sayfasında gösterilen güncel skor kullanılır,
        # yoksa keşif anındaki kural bazlı skor - bu ikisi tutarsız olursa saha ekibi yanlış önceliklendirir
        score = ai["relevance_score"] if ai else lead.get("relevance_score", "")
        reasoning = ai["score_reasoning"] if ai else lead.get("score_reasoning", "")
        writer.writerow([
            lead.get("company_name", ""),
            lead.get("sector_guess", ""),
            lead.get("province", ""),
            lead.get("district", ""),
            lead.get("address", ""),
            lead.get("phone", ""),
            score,
            review.get("status", "yeni"),
            review.get("note", ""),
            reasoning,
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fleetparts_leadler.csv"}
    )

CLASSIFY_AMBIGUOUS_BATCH_SIZE = 20
CLASSIFY_SCORE_MIN = 20
CLASSIFY_SCORE_MAX = 60

@app.post("/leads/classify-ambiguous")
def classify_ambiguous_leads(_: str = Depends(require_admin)):
    """Kural bazlı skorlamanın net karar veremediği (ne çok yüksek ne çok düşük skorlu)
    lead'leri Groq ile toplu değerlendirir. Zaten değerlendirilmiş olanları tekrar sormaz."""
    leads = lead_store.load_leads()
    ai_scores = lead_store.load_lead_ai_scores()

    candidates = [
        l for l in leads
        if l.get("lead_id") not in ai_scores
        and CLASSIFY_SCORE_MIN <= (l.get("relevance_score") or 0) <= CLASSIFY_SCORE_MAX
    ]

    if not candidates:
        return {"status": "success", "message": "Netleştirilecek belirsiz lead kalmadı.", "classified": 0, "remaining": 0}

    # TEK bir grup (20) işlenir - Render'ın istek zaman aşımını aşmamak için.
    # Arayüz (leads.html) bu endpoint'i "remaining" 0 olana kadar otomatik olarak tekrar çağırır.
    batch = candidates[:CLASSIFY_AMBIGUOUS_BATCH_SIZE]
    now = datetime.now(timezone.utc).isoformat()
    listing = "\n".join(
        f'{i+1}. lead_id="{l["lead_id"]}" | isim="{l.get("company_name","")}" | '
        f'kategori="{l.get("sector_guess","")}" | il="{l.get("province","")}"'
        for i, l in enumerate(batch)
    )
    prompt = f"""
    Sen ağır vasıta (kamyon, TIR, otobüs, iş makinesi) yedek parça toptan satıcısının hedef müşteri analistisin.
    Hedef kitle: oto yedek parça toptancıları/satıcıları VEYA kendi filosu olan nakliye/lojistik firmaları.
    Hedef DIŞI: bağımsız tamirciler/servisler, alakasız sektörler (market, giyim, gıda vb.).

    Aşağıdaki firmaları değerlendir:
    {listing}

    Her biri için 0-100 arası bir uygunluk skoru ve kısa bir gerekçe ver.

    SADECE şu JSON dizisini döndür:
    [
      {{"lead_id": "...", "relevance_score": 55, "score_reasoning": "..."}}
    ]
    """
    try:
        results = call_groq_json_array(prompt, use_secondary_model=True)  # iç kullanım, müşteriye gösterilmez
        for r in results:
            lid = r.get("lead_id")
            if not lid:
                continue
            ai_scores[lid] = {
                "relevance_score": int(r.get("relevance_score", 0)),
                "score_reasoning": r.get("score_reasoning", ""),
                "classified_at": now,
            }
        lead_store.save_lead_ai_scores(ai_scores)
        lead_store.sync_lead_ai_scores_to_github()
        remaining = len(candidates) - len(batch)
        return {
            "status": "success",
            "message": f"{len(results)} lead netleştirildi.",
            "classified": len(results),
            "remaining": remaining,
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "remaining": len(candidates)}

CLASSIFY_PRODUCT_BATCH_SIZE = 20

def _slugify_product_key(text: str) -> str:
    slug = re.sub(r"[^a-z0-9ğüşıöç]+", "_", text.strip().lower())
    return slug.strip("_")[:60] or "urun"

@app.post("/leads/classify-for-product")
def classify_leads_for_product(
    product_description: str = Form(...),
    province: str = Form(""),  # boşsa tüm iller, doluysa sadece o ile bakılır (token tasarrufu)
    _: str = Depends(require_admin)
):
    """'Bu ürünü kim alır?' sorusuna cevap verir - genel hedef kitle skorundan (relevance_score)
    farklı olarak, VERİLEN SPESİFİK ürüne göre her lead'in alım ihtimalini AI ile değerlendirir.
    Aynı ürün sorgusu tekrar gönderilirse zaten değerlendirilmiş lead'ler tekrar sorulmaz.
    NOT: Bu sadece ürün KATEGORİSİ/tipi bazında eşleştirme yapar (örn. 'kim hortum adaptörü
    alır') - OSM verisinde firmaların hangi araç MARKASINI/MODELİNİ kullandığı hiç kayıtlı
    olmadığı için marka/model bazlı hedefleme (örn. 'kim IVECO S-WAY için parça alır') mevcut
    veriyle yapılamaz."""
    product_description = product_description.strip()
    if not product_description:
        return {"status": "error", "message": "Ürün açıklaması boş olamaz."}

    product_key = _slugify_product_key(product_description)
    leads = lead_store.load_leads()
    reviews = lead_store.load_lead_reviews()
    product_scores = lead_store.load_lead_product_scores()
    existing = product_scores.get(product_key, {}).get("scores", {})

    candidates = [
        l for l in leads
        if l.get("lead_id") not in existing
        and (l.get("relevance_score") or 0) > 15  # bağımsız tamirciler en fazla 15 alabiliyor (score_lead'deki tavan) - o tavanı da içeri almamak için sıkı eşitsizlik
        and reviews.get(l.get("lead_id"), {}).get("status") != "reddedildi"
        and (not province or l.get("province") == province)
    ]

    if not candidates:
        return {
            "status": "success",
            "message": "Bu ürün için değerlendirilecek yeni lead kalmadı.",
            "classified": 0,
            "remaining": 0,
            "product_key": product_key,
        }

    batch = candidates[:CLASSIFY_PRODUCT_BATCH_SIZE]
    now = datetime.now(timezone.utc).isoformat()
    listing = "\n".join(
        f'{i+1}. lead_id="{l["lead_id"]}" | isim="{l.get("company_name","")}" | '
        f'kategori="{l.get("sector_guess","")}" | il="{l.get("province","")}"'
        for i, l in enumerate(batch)
    )
    prompt = f"""
    Sen ağır vasıta (kamyon, TIR, otobüs, iş makinesi) yedek parça sektöründe satış hedefleme uzmanısın.
    Satmak istediğimiz SPESİFİK ürün: "{product_description}"

    Bu ürünü kimler satın alır düşün:
    - Bu ürünü TOPTAN/PERAKENDE satacak oto yedek parça toptancıları/satıcıları (yeniden satış için) - genelde YÜKSEK ihtimal
    - Kendi filosundaki araçlarda KULLANMAK için satın alacak nakliye/lojistik/filo sahibi firmalar (kendi bakımları için) - ORTA-YÜKSEK ihtimal
    DEĞİL (düşük skor ver):
    - Bu ürünle sektörel olarak hiç ilgisi olmayan firmalar
    - Bağımsız oto tamir servisleri (genelde toptan/stok alımı yapmazlar)

    Aşağıdaki firmaları bu SPESİFİK ürüne göre değerlendir (genel oto yedek parça hedef kitlesi
    olmaları tek başına yeterli değil - bu ürünle ilgilenip ilgilenmeyecekleri önemli):
    {listing}

    Her biri için 0-100 arası "bu ürünü satın alma ihtimali" skoru ve kısa bir gerekçe ver.

    SADECE şu JSON dizisini döndür:
    [
      {{"lead_id": "...", "product_fit_score": 65, "product_fit_reasoning": "..."}}
    ]
    """
    try:
        results = call_groq_json_array(prompt, use_secondary_model=True)  # iç kullanım, ayrı (llama) kota
        if product_key not in product_scores:
            product_scores[product_key] = {"product_description": product_description, "classified_at": now, "scores": {}}
        for r in results:
            lid = r.get("lead_id")
            if not lid:
                continue
            product_scores[product_key]["scores"][lid] = {
                "product_fit_score": int(r.get("product_fit_score", 0)),
                "product_fit_reasoning": r.get("product_fit_reasoning", ""),
                "classified_at": now,
            }
        product_scores[product_key]["classified_at"] = now
        lead_store.save_lead_product_scores(product_scores)
        lead_store.sync_lead_product_scores_to_github()
        remaining = len(candidates) - len(batch)
        return {
            "status": "success",
            "message": f"{len(results)} lead '{product_description}' ürününe göre değerlendirildi.",
            "classified": len(results),
            "remaining": remaining,
            "product_key": product_key,
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "remaining": len(candidates), "product_key": product_key}

@app.get("/leads-product-scores/{product_key}")
def get_leads_product_scores(product_key: str, _: str = Depends(require_admin)):
    """Belirli bir ürün sorgusu için o ana kadar hesaplanmış tüm lead skorlarını döndürür
    (leads.html bunu 'remaining' 0 olana kadar tekrar tekrar çağırdıktan sonra sonucu göstermek için kullanır)."""
    product_scores = lead_store.load_lead_product_scores()
    entry = product_scores.get(product_key)
    if not entry:
        return {"status": "error", "message": "Bu ürün için henüz bir değerlendirme yok."}
    return {"status": "success", "product_description": entry.get("product_description", ""), "scores": entry.get("scores", {})}

@app.get("/leads-product-queries")
def list_leads_product_queries(_: str = Depends(require_admin)):
    """Daha önce sorgulanmış tüm ürünlerin listesini döndürür - kullanıcı aynı ürünü tekrar
    yazmadan önceki bir sorguyu seçip devam edebilsin diye."""
    product_scores = lead_store.load_lead_product_scores()
    return {
        "queries": [
            {"product_key": key, "product_description": v.get("product_description", key), "count": len(v.get("scores", {}))}
            for key, v in product_scores.items()
        ]
    }

@app.post("/trigger-discovery")
def trigger_discovery(_: str = Depends(require_admin)):
    """GitHub Actions'taki haftalık tarama workflow'unu elle (anında) tetikler."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return {"status": "error", "message": "GitHub bağlantısı yapılandırılmamış."}
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/discovery-scan.yml/dispatches"
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
        res = requests.post(url, headers=headers, json={"ref": "main"}, timeout=15)
        if res.status_code == 204:
            return {"status": "success", "message": "Tarama tetiklendi. Birkaç dakika içinde bu sayfada yeni lead'ler görünecek."}
        return {"status": "error", "message": f"HTTP {res.status_code}: {res.text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ---------------------------------------------------------
# FAZ 1: MEVCUT MÜŞTERİLERE ÇEŞİTLİ İÇERİK (korumalı admin sayfaları)
# ---------------------------------------------------------
@app.get("/broadcast", response_class=HTMLResponse)
def read_broadcast(_: str = Depends(require_admin)):
    try:
        with open("broadcast.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "<h2>Sayfa bulunamadı</h2>"

@app.get("/broadcast-data")
def get_broadcast_data(_: str = Depends(require_admin)):
    return {
        "contacts": outreach.load_contacts(),
        "log": sorted(outreach.load_broadcast_log(), key=lambda x: x.get("created_at", ""), reverse=True),
    }

@app.post("/contacts/import")
def import_contacts(text: str = Form(...), _: str = Depends(require_admin)):
    """'İsim, Telefon' formatında satır satır yapıştırılan kişileri mevcut listeye ekler."""
    contacts = outreach.load_contacts()
    existing_phones = {c.get("phone", "").strip() for c in contacts}
    added = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        name = parts[0] if parts else ""
        phone = parts[1] if len(parts) > 1 else ""
        if not name:
            continue
        if phone and phone in existing_phones:
            continue
        contacts.append({"name": name, "phone": phone, "company": "", "opted_in_at": None})
        if phone:
            existing_phones.add(phone)
        added += 1
    outreach.save_contacts(contacts)
    outreach.sync_contacts_to_github()
    return {"status": "success", "message": f"{added} kişi eklendi. Toplam: {len(contacts)}"}

@app.post("/contacts/remove")
def remove_contact(phone: str = Form(...), _: str = Depends(require_admin)):
    """Kişi listesinden tek bir kaydı çıkarır (telefon numarasına göre) - örn. lead'den
    yanlışlıkla eklenmiş ya da artık iletişim kurulmak istenmeyen bir kişi için."""
    contacts = outreach.load_contacts()
    phone = phone.strip()
    remaining = [c for c in contacts if c.get("phone", "").strip() != phone]
    removed = len(contacts) - len(remaining)
    outreach.save_contacts(remaining)
    outreach.sync_contacts_to_github()
    return {"status": "success", "message": f"{removed} kişi çıkarıldı." if removed else "Kişi bulunamadı.", "removed": removed}

@app.post("/broadcast-generate")
def generate_broadcast(_: str = Depends(require_admin)):
    """Groq ile dönüşümlü (statik olmayan) bir taslak mesaj önerir - kullanıcı gönderim öncesi düzenleyebilir."""
    import random
    angle = random.choice(outreach.BROADCAST_ANGLES)
    prompt = f"""
    Sen ağır vasıta yedek parça sektöründe faaliyet gösteren kurumsal bir işletmenin WhatsApp içerik asistanısın.
    Görev: {angle}
    Kısa (en fazla 4-5 cümle), samimi ama profesyonel, WhatsApp'a doğrudan yapıştırılabilir formatta yaz.
    Fiyat listesi gibi durağan/kuru bir mesaj OLMASIN, gerçek bir insan yazmış gibi hissettirsin.
    """
    try:
        draft = call_groq_api(prompt, pool="bulk")  # admin-only taslak, canlı müşteri aramasıyla aynı kotayı paylaşmasın
        return {"status": "success", "draft": draft}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/broadcast/save")
def save_broadcast(message: str = Form(...), origin: str = Form("manuel"), _: str = Depends(require_admin)):
    """Bir taslağı (AI önerili veya sıfırdan yazılmış) geçmişe 'gönderildi' olarak kaydeder."""
    log = outreach.load_broadcast_log()
    entry = {
        "id": f"bc_{int(time.time() * 1000)}",
        "message": message,
        "origin": origin,  # "ai" veya "manuel"
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    log.append(entry)
    outreach.save_broadcast_log(log)
    outreach.sync_broadcast_log_to_github()
    return {"status": "success"}

@app.post("/process-part")
def process_part(
    file: UploadFile = File(None),
    query: str = Form("")
):
    if not file and not query.strip():
        return {"status": "error", "message": "Fotoğraf yükleyin veya OEM kodu / parça adı girin."}

    # Dosya adına rastgele önek eklenir: aynı anda gelen iki müşteri isteği aynı dosya adını
    # (örn. telefonun varsayılan "IMG_0001.jpg" adı) kullanırsa birbirinin fotoğrafının
    # üzerine yazıp yanlış/karışık sonuç üretmesin diye. Ayrıca path traversal'a karşı korur.
    file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{os.path.basename(file.filename)}") if file else None
    try:
        if file:
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            # Katalog yüklemesiyle aynı sınır (Groq görsel API'sinin kabul ettiği sabit boyut) -
            # eskiden burada hiç kontrol yoktu, modern bir telefonun ham/yüksek çözünürlüklü
            # fotoğrafı (20MB+) hem gereksiz yere yavaş base64 kodlamaya hem de Groq'tan anlamsız
            # bir hataya yol açabilirdi; müşteriye net bir mesajla erken kesiyoruz.
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if size_mb > MAX_UPLOAD_MB:
                return {"status": "error", "message": f"Fotoğraf çok büyük ({size_mb:.1f}MB, {MAX_UPLOAD_MB}MB sınırını aşıyor). Lütfen daha düşük çözünürlükte veya sıkıştırılmış bir fotoğraf yükleyin."}

            # 1. Aşama: Evrensel Endüstriyel Tarama & OCR
            vision_res = vision_agent(file_path)

            # Görselde hiç parça tespit edilemediyse (alakasız/bulanık/boş fotoğraf) katalogla
            # eşleştirmeye hiç girişme - aksi halde model olmayan bir parça için katalogdan
            # rastgele/hatalı bir eşleşme uydurmaya çalışabilir.
            if vision_res.get("is_part_detected") is False:
                return {
                    "status": "error",
                    "message": "Görselde bir yedek parça tespit edilemedi. Lütfen parçanın net, yakından çekilmiş bir fotoğrafını yükleyin veya OEM kodu/parça adını yazarak arayın."
                }

            # 2. Aşama: Matris Algoritmik Eşleştirme
            matched_prod = match_agent(vision_res)
        else:
            # Fotoğraf yok: OEM kodu veya parça adına göre doğrudan katalog araması
            vision_res = {"note": "Fotoğrafsız metin araması yapıldı.", "query": query}
            matched_prod = find_by_text(query)

        return {
            "status": "success",
            "agents_output": {
                "vision_analysis": vision_res,
                "matched_product": matched_prod
            }
        }
    except Exception as e:
        return {"status": "error", "message": f"Kritik Sistem Hatası: {str(e)}"}
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

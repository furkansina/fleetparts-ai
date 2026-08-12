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
import whatsapp_business_api
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
# SINIRSIZ EK HESAP DESTEĞİ: ileride kota gerçekten yetersiz kalırsa, KOD DEĞİŞİKLİĞİ YAPMADAN
# Render'a GROQ_API_KEY_4, GROQ_API_KEY_5, ... (20'ye kadar) isimli yeni bir env var eklemek
# yeterli - otomatik olarak zincire eklenir. Bu, "her yeni ihtiyaçta tekrar kod yazman gerekmesin"
# isteği için kalıcı bir çözüm.
GROQ_EXTRA_KEYS = []
for _i in range(4, 21):
    _extra = os.environ.get(f"GROQ_API_KEY_{_i}", "")
    if _extra:
        GROQ_EXTRA_KEYS.append((f"extra{_i}", _extra))
        usage_tracker.register_pool(f"extra{_i}")

# leads_ai (llama-3.3-70b-versatile - lead netleştirme) ÖNCEDEN sadece ana hesaba (GROQ_API_KEY)
# kilitliydi, zincirlemesi yoktu - bir tek ağır kullanım (2000+ lead'lik bir netleştirme) o
# hesabın günlük 100K token'lık llama kotasını tek seferde bitirebiliyordu (2026-08-11 canlıda
# tespit edildi). qwen havuzları (customer/bulk/extraN) İÇİN zaten var olan çoklu-hesap zincirleme
# mekanizması burada YOKTU. Aynı fiziksel hesapların HER BİRİNİN llama için de AYRI bir 100K'lık
# kotası var (farklı model = Groq'ta farklı sunucu taraflı limit) - bu yüzden qwen için zaten
# tanımlı her ek hesap, leads_ai_* etiketiyle İKİNCİ KEZ (llama bütçesiyle) kaydedilir. Aynı API
# anahtarı, iki AYRI kota için iki AYRI pool etiketi altında izlenir - karışmaz.
LEADS_AI_KEY_CHAIN = [("leads_ai", GROQ_API_KEY)]
if GROQ_API_KEY_BULK:
    LEADS_AI_KEY_CHAIN.append(("leads_ai_bulk", GROQ_API_KEY_BULK))
if GROQ_API_KEY_THIRD:
    LEADS_AI_KEY_CHAIN.append(("leads_ai_customer2", GROQ_API_KEY_THIRD))
for _label, _key in GROQ_EXTRA_KEYS:
    LEADS_AI_KEY_CHAIN.append((f"leads_ai_{_label}", _key))
for _label, _key in LEADS_AI_KEY_CHAIN:
    usage_tracker.register_pool(_label, token_budget=usage_tracker.LEADS_AI_DAILY_TOKEN_BUDGET, request_budget=usage_tracker.LEADS_AI_DAILY_REQUEST_BUDGET)
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

def _seed_catalog_from_github_or_default():
    """KRİTİK (2026-08-11/12'de bir kayıt kazasını araştırırken fark edildi): Render'ın diski her
    kod deploy'unda SIFIRLANIYOR - süreç yeniden başladığında catalog.json yerel diskte hiç yok.
    Silme/yükleme/geri-dönük-görsel-ekleme uçlarının HEPSİ ('_load_catalog_from_disk') performans
    için GitHub'a değil YEREL diske bakıyor (her sayfada GitHub'a gitmemek amaçlı, bilinçli bir
    tercih). Eskiden disk boşsa doğrudan 3 ürünlük DEFAULT_CATALOG ile dolduruluyordu - yani her kod
    deploy'undan SONRAKİ İLK yazma işlemi (bir admin bir kayıt silse veya yeni bir sayfa yüklese),
    gerçek 1000+ ürünlük kataloğun üzerine bu 3 ürünlük sahte kataloğu yazıp GitHub'daki asıl veriyi
    SİLEBİLİRDİ - sessiz, ciddi bir veri kaybı riski. Artık disk boşsa/bozuksa önce GitHub'daki
    GERÇEK kataloğu geri yüklemeye çalışılıyor, sadece GitHub'a hiç ulaşılamazsa (yapılandırılmamış
    ya da ağ hatası) örnek 3 ürünlük katalog kullanılıyor.

    BUG (2026-08-12'de canlıda tespit edildi, aynı gün ikinci düzeltme): burada ÖNCE raw.
    githubusercontent.com (CDN) kullanılıyordu - Render sık sık (görünürde birkaç dakikada bir)
    kendiliğinden yeniden başladığı için, her yeniden başlamada bu CDN'in güncel commit'i henüz
    yansıtmamış OLABİLECEĞİ (bilinen bir CDN gecikmesi) bir andan besleniyordu. Sonuç: geriye dönük
    görsel ekleme gibi sık sık senkronize eden bir iş sırasında Render arka arkaya birkaç kez
    yeniden başlayınca, her yeniden başlamada disk BİR ÖNCEKİ (eski) commit'ten dolduruluyor, o
    üzerine devam eden iş de bu eski taban üzerinden GitHub'a geri yazınca AZ ÖNCE eklenmiş
    görseller sessizce KAYBOLUYORDU - canlıda gerçekten yaşandı (682 ürünlük görsel kapsamı 614'e
    düştü). Artık GitHub'ın Contents API'si (api.github.com, CDN'siz, her zaman en güncel commit)
    kullanılıyor - GITHUB_TOKEN tanımlıysa. Token yoksa (ör. sadece herkese açık bir yedek senaryo)
    CDN'e düşülür, o da olmazsa örnek katalog kullanılır."""
    data = _fetch_catalog_from_github_api()
    if data:
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return
    seed_default_catalog()


def _fetch_catalog_from_github_api():
    """GitHub'daki catalog.json'ı Contents API'den (CDN'siz, her zaman en güncel commit) çeker.
    Başarısız olursa (GITHUB_TOKEN yok, ağ hatası, vb.) None döner - çağıran kendi yedek planını
    uygular."""
    if not GITHUB_REPO:
        return None
    try:
        if GITHUB_TOKEN:
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{CATALOG_FILE}"
            headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
            res = requests.get(api_url, headers=headers, timeout=15)
            if res.status_code == 200:
                content_b64 = res.json().get("content", "")
                data = json.loads(base64.b64decode(content_b64).decode("utf-8"))
                if isinstance(data, list) and data:
                    return data
        else:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{CATALOG_FILE}"
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and data:
                    return data
    except Exception:
        pass
    return None


def _refresh_catalog_disk_from_github() -> bool:
    """KRİTİK (2026-08-12'de canlıda İKİNCİ KEZ gerçek veri kaybına yol açtığı tespit edildi):
    silme/yükleme/geri-dönük-görsel-ekleme uçlarının hepsi ('_load_catalog_from_disk') performans
    için Render'ın YEREL diskine bakıyor - bu disk sadece süreç İLK başladığında GitHub'dan
    dolduruluyordu (bkz. _seed_catalog_from_github_or_default). Ama GitHub'a bu süreç DIŞINDAN bir
    değişiklik yapılırsa (ör. elle bir düzeltme/toplu ekleme scripti, ya da GitHub Actions), yerel
    disk bunu HİÇ görmüyor - ve bu uçlardan biri tetiklenince (ör. geriye dönük görsel ekleme),
    eski/haberdar olmadığı yerel kopyayı GitHub'a GERİ YAZIP dışarıdan yapılan değişikliği SESSİZCE
    SİLİYORDU (canlıda gerçekten yaşandı: elle eklenen 114 ürün, hemen ardından çalıştırılan bir
    görsel-ekleme işiyle geri silindi). Artık bu tür HER işlem (silme, yükleme, geriye dönük görsel
    ekleme) başlamadan ÖNCE yerel disk GitHub'ın Contents API'sinden (CDN'siz, her zaman güncel)
    tazeleniyor - böylece süreç kendi dışında yapılan değişikliklerden HABERSİZ kalıp onların
    üzerine yazamaz. GitHub'a ulaşılamazsa (ağ hatası) sessizce mevcut yerel diskle devam edilir -
    en azından süreç kendi bildiği veriyi kaybetmez, sadece dışarıdaki en taze hali göremez."""
    data = _fetch_catalog_from_github_api()
    if data:
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True
    return False

# Başlangıç Evrensel Katalog Veritabanı (dosya yoksa veya bozuk/boşsa GitHub'daki gerçek kataloğu
# geri yükle, o da olmazsa örnek kataloğu kullan)
if not os.path.exists(CATALOG_FILE) or os.path.getsize(CATALOG_FILE) == 0:
    _seed_catalog_from_github_or_default()

def _load_catalog_from_disk() -> list:
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        _seed_catalog_from_github_or_default()
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
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

# Yedekleme (GitHub'a gönderme) BAŞARISIZ olursa - GitHub token süresi dolmuş/iptal olmuş, GitHub
# geçici kesinti yaşıyor, ağ sorunu vb. - eskiden bu TAMAMEN SESSİZCE yutuluyordu (bare except).
# Render'ın diski her yeniden deploy'da sıfırlandığı için bu, "yedekleme aylarca bozuk kalır, kimse
# fark etmez, sonra bir deploy tetiklenir ve TÜM o süre boyunca eklenen ürünler kaybolur" demek -
# sessiz veri kaybı riski. Artık son senkronizasyon durumu (başarı/hata + üst üste kaç kez
# başarısız olduğu) izleniyor ve /usage üzerinden yönetim sayfalarında görünür hale getiriliyor.
_github_sync_status = {"last_success_at": None, "last_error": None, "consecutive_failures": 0}


def sync_catalog_to_github():
    """catalog.json'u GitHub'a yedekler; Render her yeniden deploy olduğunda diski sıfırladığı için kalıcılık böyle sağlanır."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        _github_sync_status["last_error"] = "GITHUB_TOKEN veya GITHUB_REPO tanımlı değil - yedekleme hiç aktif değil."
        _github_sync_status["consecutive_failures"] += 1
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
        put_res = requests.put(api_url, headers=headers, json=body, timeout=15)
        if put_res.status_code not in (200, 201):
            _github_sync_status["last_error"] = f"GitHub'a yazma başarısız: HTTP {put_res.status_code}"
            _github_sync_status["consecutive_failures"] += 1
            return
        _github_sync_status["last_success_at"] = time.time()
        _github_sync_status["last_error"] = None
        _github_sync_status["consecutive_failures"] = 0
    except Exception as e:
        _github_sync_status["last_error"] = f"GitHub'a bağlanılamadı: {str(e)}"
        _github_sync_status["consecutive_failures"] += 1

CATALOG_IMAGE_DIR = "catalog_page_images"
os.makedirs(CATALOG_IMAGE_DIR, exist_ok=True)


def sync_binary_file_to_github(local_path: str, github_path: str, message: str) -> bool:
    """catalog.json gibi JSON dosyalarının aksine, katalog SAYFA GÖRSELLERİ (2026-08-11'de
    eklendi - müşteri bir ürün bulduğunda o ürünün geçtiği gerçek katalog sayfasını/fotoğrafını
    da görebilsin diye) ikili (binary) dosya. Aynı 409-yeniden-deneme deseni (lead_store.py'deki
    aynı tarihli düzeltmeyle tutarlı) - bu depoya aynı anda birçok süreç yazabiliyor."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_path}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")
    for attempt in range(3):
        try:
            sha = None
            res = requests.get(api_url, headers=headers, timeout=15)
            if res.status_code == 200:
                sha = res.json().get("sha")
            body = {"message": message, "content": content_b64}
            if sha:
                body["sha"] = sha
            put_res = requests.put(api_url, headers=headers, json=body, timeout=30)
            if put_res.status_code in (200, 201):
                return True
            if put_res.status_code == 409 and attempt < 2:
                continue
            return False
        except Exception:
            continue
    return False

def _resolve_key_chain(pool: str, use_secondary_model: bool) -> list:
    """`pool`'a göre hangi Groq hesabının/hesaplarının hangi sırayla deneneceğini belirler.
    Her eleman (etiket, api_anahtarı) çifti - etiket hata mesajlarında/kullanım takibinde
    hangi hesabın kullanıldığını belirtmek için kullanılır."""
    if use_secondary_model:
        # llama modeli Groq'ta qwen'den TAMAMEN AYRI bir sunucu taraflı kotaya sahip (ayrı model),
        # ama kimlik bilgisi (API anahtarı) olarak hâlâ (varsa) BİRDEN FAZLA hesabı kullanabilir -
        # bkz. LEADS_AI_KEY_CHAIN. Önceden tek hesaba (GROQ_API_KEY) kilitliydi - büyük bir lead
        # netleştirme partisi (2000+ kayıt) tek hesabın 100K token'lık günlük llama kotasını tek
        # seferde bitiriyordu (2026-08-11 canlıda tespit edildi, kalıcı çözüm istendi). Artık qwen
        # havuzları gibi zincirleniyor: GROQ_API_KEY_BULK/GROQ_API_KEY_THIRD/GROQ_API_KEY_4.. hangi
        # ek hesap tanımlıysa, ana hesabın llama kotası bitince otomatik ona geçiliyor.
        return LEADS_AI_KEY_CHAIN
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
        chain.extend(GROQ_EXTRA_KEYS)
        return chain
    chain = [("customer", GROQ_API_KEY)]
    if GROQ_API_KEY_BULK:
        chain.append(("bulk", GROQ_API_KEY_BULK))
    if GROQ_API_KEY_THIRD:
        # 3. hesap - müşteri havuzunun 2. yedeği. Ana VE bulk hesabın ikisi de günlük kotasını
        # doldurursa devreye girer, kota tükenmesine karşı ekstra güvence.
        chain.append(("customer2", GROQ_API_KEY_THIRD))
    chain.extend(GROQ_EXTRA_KEYS)
    return chain

def _customer_safe_error(e: Exception) -> str:
    """Müşteriye gösterilecek hata mesajını temizler. `call_groq_api`'nin bazı dallarında Groq'un
    HAM HTTP yanıt gövdesi hata mesajına gömülüyor (ör. 'Groq isteği başarısız oldu: HTTP 500 ...:
    {ham json}') - bu, kurum/iç kimlik bilgisi gibi teknik detaylar içerebilir ve doğrudan
    müşteriye gösterilmemeli. Elle yazılmış, zaten müşteriye uygun olan mesajlar (ör. 'kota doldu')
    olduğu gibi geçer; ham/teknik olanlar genel, güvenli bir mesajla değiştirilir."""
    msg = str(e)
    raw_technical_markers = ("Groq isteği başarısız oldu", "Groq Bağlantı Hatası", "Model geçerli JSON")
    if any(marker in msg for marker in raw_technical_markers):
        return "Şu anda bu işlemi tamamlayamadık. Lütfen birkaç dakika sonra tekrar deneyin ya da OEM kodu/parça adıyla arayın."
    return msg


def call_groq_api(prompt: str, image_path: str = None, use_secondary_model: bool = False, pool: str = "customer", max_output_tokens: int = None) -> str:
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
        # max_output_tokens verilirse (ör. bir sayfa dolusu ürün listesi çıkarımı) bu varsayılan
        # aşılır - görsel girdi olmadığı için (bu dal sadece metin) TPM bütçesinde zaten daha çok
        # boşluk var, görsel yoldan (4500) daha fazlasına bile izin verilebilir.
        payload = {"model": model, "reasoning_effort": "none", "max_tokens": max_output_tokens or 2500, "messages": [{"role": "user", "content": prompt}]}
    last_error = ""
    daily_exhausted_count = 0
    for key_label, api_key in key_chain:
        # Groq panelinden doğrulanan günlük 1000 İSTEK sınırına (token'dan ayrı) bu hesap zaten
        # yaklaştıysa, boşuna bir istek daha atıp 429 almak yerine doğrudan zincirdeki bir sonraki
        # hesaba geç - hem zaman hem gereksiz bir başarısız istek kazandırır.
        today_usage = usage_tracker.get_today_usage()
        if today_usage.get(key_label, {}).get("requests_remaining_estimate", usage_tracker.DAILY_REQUEST_BUDGET) < 3:
            last_error = f"{key_label} hesabının günlük istek sayısı sınırına yaklaştı, atlanıyor."
            daily_exhausted_count += 1
            continue
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

    # 1b. Birebir eşleşme yoksa ama girilen kod, katalogdaki bir veya daha fazla ürünün kodunun
    # TAM ÖN EKİYSE (ör. müşteri '101042' yazdı, katalogda '101042-0'..'101042-7' gibi 8 renk/
    # ölçü/marka varyantı var - aynı ürün ailesinin ortak taban kodu) - bu ÇOK yaygın gerçek bir
    # durum. BUG (2026-08-12'de canlıda tespit edildi): eskiden bu durumda doğrudan yapay zekaya
    # gidiliyordu, o da "birden fazla aday var, belirsiz" deyip düz 'bulunamadı' dönüyordu -
    # müşteri hangi varyantı istediğini seçemiyor, sadece 'Katalogda Bulunamadı' görüyordu. Artık
    # böyle bir durumda yapay zekaya HİÇ gidilmeden (anında, ücretsiz) seçenek listesi sunuluyor.
    if len(q) >= 3:
        prefix_matches = [
            item for item in catalog
            if turkish_lower(str(item.get("oem", "")).strip()).startswith(q + "-")
            or turkish_lower(str(item.get("id", "")).strip()).startswith(q + "-")
        ]
        if len(prefix_matches) == 1:
            item_copy = prefix_matches[0].copy()
            item_copy["match_score"] = 95
            item_copy["match_evidence"] = f"'{query}' kodunun kataloğdaki tek varyantı - kısmi kod eşleşmesi"
            return item_copy
        if len(prefix_matches) > 1:
            return {
                "id": "MULTIPLE_MATCHES",
                "name": "Birden Fazla Seçenek Bulundu",
                "match_reason": f"'{query}' koduyla başlayan {len(prefix_matches)} farklı ürün varyantı var - lütfen doğru olanı seçin.",
                "candidates": [
                    {
                        "id": c.get("id"), "oem": c.get("oem"), "name": c.get("name"),
                        "specs": c.get("specs", ""), "price": c.get("price", ""), "stock": c.get("stock"),
                        "source_page_image": c.get("source_page_image"),
                    }
                    for c in prefix_matches
                ],
            }

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

@app.get("/catalog-page-image/{name}")
def get_catalog_page_image(name: str):
    """Bir katalog ürününün geldiği gerçek sayfa görselini döndürür (bkz. _scan_catalog_source'daki
    2026-08-11 notu). Önce Render'ın kendi diskindeki önbelleğe bakar (aynı görsel tekrar tekrar
    istenirse GitHub'ı yormasın diye), yoksa GitHub'dan çekip önbelleğe alır - tıpkı catalog.json
    için kullanılan aynı 'GitHub asıl kaynak, disk sadece önbellek' deseni."""
    safe_name = os.path.basename(name)  # path traversal'a karşı - sadece dosya adı, dizin bileşeni yok
    local_path = os.path.join(CATALOG_IMAGE_DIR, safe_name)
    if not os.path.exists(local_path):
        if not GITHUB_REPO:
            raise HTTPException(status_code=404, detail="Görsel bulunamadı.")
        try:
            url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{CATALOG_IMAGE_DIR}/{safe_name}"
            res = requests.get(url, timeout=15)
            if res.status_code != 200:
                raise HTTPException(status_code=404, detail="Görsel bulunamadı.")
            with open(local_path, "wb") as f:
                f.write(res.content)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=404, detail="Görsel bulunamadı.")
    return Response(content=open(local_path, "rb").read(), media_type="image/png")

@app.post("/delete-catalog-item")
def delete_catalog_item(item_id: str = Form(...), _: str = Depends(require_admin)):
    """Yapay zeka taramasının ürettiği hatalı/alakasız bir kaydı (ör. bir kapak sayfasındaki
    logonun yanlışlıkla parça sanılması) katalogdan çıkarmak için - eskiden bunun için hiçbir
    yol yoktu, kötü bir kayıt kalıcı olarak katalogda takılı kalıyordu."""
    with CATALOG_WRITE_LOCK:
        _refresh_catalog_disk_from_github()
        catalog = _load_catalog_from_disk()
        new_catalog = [item for item in catalog if str(item.get("id")) != str(item_id)]
        if len(new_catalog) == len(catalog):
            return {"status": "error", "message": f"'{item_id}' id'li bir kayıt bulunamadı."}
        save_catalog(new_catalog)
    sync_catalog_to_github()
    _catalog_cache["data"] = new_catalog
    _catalog_cache["fetched_at"] = time.time()
    return {"status": "success", "message": "Kayıt silindi.", "remaining": len(new_catalog)}

@app.get("/catalog-sources")
def list_catalog_sources():
    """Kaç FARKLI katalog dosyası yüklenmiş ve her birinde kaç ürün var - eski bir katalogu
    (ör. yeni bir fiyat listesiyle çakışmasın diye) toplu silmeden önce görebilmek için.
    Hassas veri içermediği için (sadece dosya adı + adet, /get-catalog ve /usage gibi) herkese
    açık - index.html sayfa yüklenirken otomatik çağırıyor, admin korumalı olsaydı her ziyaretçiye
    istenmeyen bir tarayıcı şifre penceresi çıkardı. Silme işlemi (aşağıdaki uç nokta) admin korumalı
    kalmaya devam ediyor - sadece görüntüleme herkese açık."""
    catalog = load_catalog()
    counts = {}
    for item in catalog:
        bases = {_catalog_base_name(s) for s in _item_sources(item) if s}
        for base in bases:
            counts[base] = counts.get(base, 0) + 1
    sources = [{"name": name, "count": count} for name, count in sorted(counts.items())]
    return {"sources": sources}

@app.post("/delete-catalog-source")
def delete_catalog_source(source_name: str = Form(...), _: str = Depends(require_admin)):
    """Belirli bir yüklenen katalog dosyasına ait TÜM ürünleri kaldırır - eski bir katalogu/fiyat
    listesini yenisiyle değiştirirken kullanmak için. Bir ürün BAŞKA bir katalogda da geçiyorsa
    (source_files birden fazla kaynak içeriyorsa) ürün SİLİNMEZ, sadece bu kataloğun referansı
    kaldırılır - ürün hâlâ geçerli olduğu diğer katalog(lar) üzerinden erişilebilir kalır."""
    with CATALOG_WRITE_LOCK:
        _refresh_catalog_disk_from_github()
        catalog = _load_catalog_from_disk()
        new_catalog = []
        removed_count = 0
        updated_count = 0
        for item in catalog:
            sources = _item_sources(item)
            remaining = [s for s in sources if _catalog_base_name(s) != source_name]
            if sources and not remaining:
                removed_count += 1
                continue  # bu ürünün TEK kaynağı silinen katalogdu - ürün tamamen çıkarılır
            if len(remaining) != len(sources):
                item["source_files"] = remaining
                if _catalog_base_name(item.get("source_file", "")) == source_name and remaining:
                    item["source_file"] = remaining[-1]
                updated_count += 1
            new_catalog.append(item)
        if removed_count == 0 and updated_count == 0:
            return {"status": "error", "message": f"'{source_name}' adlı bir katalog bulunamadı."}
        save_catalog(new_catalog)
    sync_catalog_to_github()
    _catalog_cache["data"] = new_catalog
    _catalog_cache["fetched_at"] = time.time()
    return {
        "status": "success",
        "message": f"'{source_name}' kaldırıldı: {removed_count} ürün tamamen silindi"
                   + (f", {updated_count} ürün (başka katalogda da olduğu için) güncellendi" if updated_count else "") + ".",
        "remaining_total": len(new_catalog),
    }

@app.post("/admin/fix-glued-oem")
def fix_glued_oem_codes(_: str = Depends(require_admin)):
    """BAKIM UÇ NOKTASI (2026-08-12'de canlıda tespit edildi): metin tablosu çıkarımındaki
    ('_try_extract_text_table' - aynı kod bir sayfada birden fazla satırda tekrar ediyormuş gibi
    göründüğünde koda ismi ekleyip 'benzersizleştiren' dal) bir kenar durumu, bazı ürünlerin OEM
    alanına yanlışlıkla ürün adının da yapışmasına yol açtı (örn. temiz 'ORP 4011' kaydının yanında
    bozuk 'ORP 4011 Test Aparatı Dişi...' kaydı da oluştu - aynı ürün iki kez, biri bozuk kodla).
    Bu hem kataloğu şişiriyor hem de metin/kod aramasında (find_by_text, match_agent) gereksiz
    belirsizlik yaratıyordu (ör. '101042' araması 16 yarı-çakışan aday görüp hiçbirini seçemiyordu).

    DÜZELTİLDİ (aynı gün, ikinci geçiş): bu uç noktanın İLK sürümü 'isim koddan ayrılır' mantığını
    kör bir şekilde uyguluyordu - bazı durumlarda (ör. 'PCM - G06 12 x 1,5' / '... 14 x 1,5' / ...
    16 x 1,5') 'isim' aslında çöp değil, GERÇEK bir ayırt edici (ölçü) bilgisiydi; onu koddan
    sökmek FARKLI ürünleri (farklı ölçüler) AYNI kısaltılmış koda ('PCM - G06') düşürüp YENİ bir
    çakışma yarattı - canlıda 8 grup, 36 kayıt etkilendi, hemen fark edilip burada düzeltildi.
    Artık iki adımlı: önce hangi 'true_code'ların BİRDEN FAZLA farklı kayıtta ortaya çıkacağı
    hesaplanır - bu durumda isim/ölçü GERÇEKTEN ayırt edici demektir, o kayıtlara DOKUNULMAZ
    (ne silinir ne kodu kısaltılır). Sadece true_code'u TEK bir kayıtta ortaya çıkan (gerçekten
    gereksiz/çöp isim eklenmiş) kayıtlar onarılır/silinir. Ayrıca bu ikinci geçiş, ilk sürümün
    yanlışlıkla çakıştırdığı 36 kaydı da (id alanı hiç dokunulmadığı için hâlâ orijinal/doğru
    değerini taşıyor) otomatik olarak eski haline getirir - id'leri hâlâ benzersiz olduğu için
    oem'i id ile eşitlemek güvenli bir geri alma sağlar. İdempotent."""
    with CATALOG_WRITE_LOCK:
        catalog = load_catalog()
        original_oems = {str(i.get("oem", "")).strip() for i in catalog if i.get("oem")}

        # Geri alma: onceki (hatali) calismanin caktirdigi kayitlari tespit et - oem'i baska
        # kayit(lar)la birebir ayni AMA id'si hala farkli/daha bilgili (id, oem'in dogal bir
        # uzantisi). Bu durumda id zaten hicbir zaman bozulmamisti, oem'i id'ye esitlemek guvenli.
        from collections import Counter
        oem_counts = Counter(str(i.get("oem", "")).strip() for i in catalog if i.get("oem"))
        reverted = []
        for item in catalog:
            oem = str(item.get("oem", "")).strip()
            iid = str(item.get("id", "")).strip()
            if oem and oem_counts.get(oem, 0) > 1 and oem != "OEM-BELİRSİZ" and iid and iid != oem and iid.startswith(oem):
                reverted.append({"id": iid, "eski_kod": oem, "yeni_kod": iid})
                item["oem"] = iid

        # Ana gecis: 'isim, oem'in icinde geciyor' seklindeki bozuk kayitlari bul, ama SADECE
        # true_code baska hicbir farkli kayitta tekrar etmiyorsa (yani gercekten gereksiz/cop
        # bir isim ekiyse) dokun - aksi halde farkli urunleri ayni koda dusurup yeni bir
        # cakisma yaratirdik (bkz. yukaridaki not).
        malformed = []
        for idx, item in enumerate(catalog):
            name = str(item.get("name", "")).strip()
            oem = str(item.get("oem", "")).strip()
            if not name or not oem or oem == name or name not in oem:
                continue
            pos = oem.find(name)
            true_code = oem[:pos].strip()
            if true_code:
                malformed.append((idx, oem, true_code))

        true_code_counts = Counter(tc for _, _, tc in malformed)
        to_delete_idx = []
        repaired = []
        for idx, oem, true_code in malformed:
            if true_code_counts[true_code] > 1:
                continue  # birden fazla FARKLI kayit ayni koda duserdi - dokunma, isim ayirt edici
            if true_code in original_oems and true_code != oem:
                to_delete_idx.append(idx)
            else:
                catalog[idx]["oem"] = true_code
                repaired.append({"id": catalog[idx].get("id"), "eski_kod": oem, "yeni_kod": true_code})

        if not to_delete_idx and not repaired and not reverted:
            return {"status": "success", "silinen": 0, "onarilan": 0, "geri_alinan": 0, "kalan_toplam": len(catalog), "message": "Düzeltilecek bozuk kayıt bulunamadı."}
        delete_set = set(to_delete_idx)
        deleted_info = [{"id": catalog[i].get("id"), "oem": catalog[i].get("oem")} for i in to_delete_idx]
        new_catalog = [item for i, item in enumerate(catalog) if i not in delete_set]
        save_catalog(new_catalog)
    sync_catalog_to_github()
    _catalog_cache["data"] = new_catalog
    _catalog_cache["fetched_at"] = time.time()
    return {
        "status": "success",
        "silinen": len(deleted_info),
        "onarilan": len(repaired),
        "geri_alinan": len(reverted),
        "silinen_detay": deleted_info[:30],
        "onarilan_detay": repaired[:30],
        "geri_alinan_detay": reverted[:30],
        "kalan_toplam": len(new_catalog),
    }

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
        "price": "Görselde/katalogda AÇIKÇA yazılı fiyat varsa (ör. '₺150,00' veya '150 TL') aynen yaz - yoksa/emin değilsen kesinlikle boş string, ASLA fiyat uydurma",
        "stock": 25
    }
]
"""

# KADEMELİ TARAMA - ORTA KATMAN: sabit regex kalıpları (_try_extract_text_table) her PDF
# düzenini tanıyamaz - kullanıcı "çok farklı katalog atacağım, her birinde çalışmalı" dediği için
# her yeni format için elle yeni bir regex eklemek ölçeklenmez (kök neden bu). Regex başarısız
# olduğunda ama sayfanın GERÇEK bir metin katmanı varsa, görseli Groq'a göndermek yerine
# ÇIKARILMIŞ METNİ gönderiyoruz - görsel girdi ~2500-3000 token tutarken metin girdisi genelde
# birkaç yüz token tutuyor (çok daha ucuz), ÜSTELİK Groq'un kendi anlama gücü sayede regex'in
# tanımadığı HERHANGİ bir tablo/liste düzenini de çözebiliyor - yeni bir kod yazmaya gerek kalmaz.
CATALOG_TEXT_SCAN_PROMPT_TEMPLATE = """
Aşağıda bir ağır vasıta yedek parça kataloğu/fiyat listesi PDF sayfasından çıkarılmış metin var.
Satırlar 'satır: hücre1 | hücre2 | hücre3' formatında - PDF'teki gerçek konuma göre satır/hücre
olarak yeniden yapılandırıldı (sütun sırası/sayısı düzenden düzene değişebilir).

METİN:
---
{page_text}
---

Bu metindeki HER ürünü/parçayı çıkar. Her satır genelde bir ürün kodu, ürün adı/ölçüsü ve
(varsa) fiyat içerir - ama düzen sayfadan sayfaya değişebilir, örüntüyü kendin anlamaya çalış.
Bir satırda birden fazla ürün yan yana olabilir (ör. iki mini-tablo yan yana duruyorsa). Metinde
hiç ürün/kod YOKSA (başlık sayfası, düz açıklama metni, teknik çizim notu vb.) boş liste döndür.

SADECE şu JSON yapısında bir DİZİ döndür, başka hiçbir şey yazma:
[
    {{
        "id": "PRC-" + rasgele 4 haneli sayı,
        "oem": "Parçanın kod/OEM numarası (Yoksa 'OEM-BELİRSİZ')",
        "name": "Parçanın adı (varsa ölçü/renk/varyant bilgisiyle birlikte)",
        "brand": "SADECE metinde açıkça yazılı üretici/marka adı - yoksa/emin değilsen kesinlikle boş string",
        "specs": "Ölçüler, bağlantı tipi, malzeme ve diğer teknik detaylar",
        "price": "Metinde açıkça yazılı fiyat varsa aynen yaz - yoksa kesinlikle boş string, ASLA fiyat uydurma",
        "stock": 1
    }}
]
"""


def _page_words_to_text_block(page_words: list) -> str:
    """Konumlu kelimeleri (satır/hücre olarak yeniden inşa edilmiş) Groq'un okuyabileceği düz bir
    metin bloğuna çevirir - _rows_by_position ile aynı satır/hücre mantığını kullanır, böylece
    regex'in çözemediği bir düzende bile Groq'a en azından DOĞRU SIRALANMIŞ satırlar gider."""
    row_cells = _rows_by_position(page_words)
    lines = [f"satır {i + 1}: " + " | ".join(cells) for i, cells in enumerate(row_cells)]
    return "\n".join(lines)

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

def call_groq_json_array(prompt: str, image_path: str = None, use_secondary_model: bool = False, pool: str = "customer", max_output_tokens: int = None) -> list:
    last_error = None
    for attempt in range(2):
        raw_text = call_groq_api(prompt, image_path, use_secondary_model=use_secondary_model, pool=pool, max_output_tokens=max_output_tokens)
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
            page = doc[page_index]
            page_words = page.get_text("words")  # konumlu kelimeler - ücretsiz/anında tablo çıkarımı için
            pix = page.get_pixmap(matrix=zoom_matrix)
            page_path = os.path.join(CATALOG_DIR, f"{base_name}_sayfa{page_num}.png")
            pix.save(page_path)
            del pix
            yield page_num, page_label, page_path, page_words
    finally:
        doc.close()


# Ürün kodu deseni: harf öneki olabilir (örn. "ORP 4011"), rakam bloğu (3-10 hane - gerçek OEM
# kodları hem kısa dahili kodlar "4011" hem uzun OEM numaraları "9433340115" olabiliyor, gerçek
# katalogda doğrulandı), aralarda boşluk/tire olabilir ("101001-0" / "101001 0"), sonda tek
# harf/hane eki olabilir ("4016 A"). İKİNCİ desen: harf-tire-harf+hane tip kodları ("PCM - G06",
# "PLM - G10" - metrik rekor/bağlantı tip+ölçü kodları, gerçek katalogda doğrulandı).
_CODE_RE = re.compile(
    r"^[A-ZÇĞİÖŞÜ]{0,5}\s?\d{3,10}([\s\-]\d{1,3})?\s?[A-Z]{0,2}$"
    r"|^[A-ZÇĞİÖŞÜ]{1,6}\s?-\s?[A-ZÇĞİÖŞÜ]{0,2}\d{1,4}$"
)
# Aynı hücrede kod ve isim bitişik kalmışsa ("ORP 4016 A Test Aparatı...") ayırmak için.
_CODE_PREFIX_RE = re.compile(r"^([A-ZÇĞİÖŞÜ]{0,5}\s?\d{3,10}([\s\-]\d{1,3})?\s?[A-Z]{0,2})\s+(.*)$")
_PRICE_RE = re.compile(r"^₺?\s?[\d.,]+\s?(TL)?$", re.IGNORECASE)
_TABLE_HEADER_WORDS = {"ürün kodu", "ürün adı", "birim fiyat", "fiyat", "kod", "açıklama", "adet", "stok"}
# Bazı bağlantı-parçası tablolarında "tip" (harf, hiç rakamsız - ör. "PC", "PL") ve "ölçü"
# (ör. "1/8 - 04") AYRI hücrelerde duruyor, ikisi birlikte gerçek ürün kodunu oluşturuyor -
# gerçek katalogda doğrulandı.
_TYPE_PREFIX_RE = re.compile(r"^[A-ZÇĞİÖŞÜ]{1,4}$")
_SIZE_SPEC_RE = re.compile(r"^[\d/]+(\s?[-x]\s?[\d/,.]+)+$")


def _rows_by_position(page_words: list) -> list:
    """Konumlu kelimeleri (page.get_text('words')) Y koordinatına göre satırlara, her satır
    içinde de X boşluğuna göre hücrelere gruplar. Sıralı düz metin çıkarımının (page.get_text())
    KARIŞTIRDIĞI çok sütunlu PDF tablolarını (gerçek bir örnekte doğrulandı: kod/fiyat sütunu ile
    ürün adı sütunu ayrı metin bloklarında olup satır sırası tamamen karışıyordu) satırların
    gerçek görsel konumuna bakarak doğru şekilde yeniden inşa eder - sütun sayısı/sırası PDF'ten
    PDF'e değişse bile çalışır."""
    if not page_words:
        return []
    rows = {}
    for w in page_words:
        y_key = round(w[1] / 3) * 3
        rows.setdefault(y_key, []).append(w)
    row_cells = []
    for y_key in sorted(rows.keys()):
        row_words = sorted(rows[y_key], key=lambda w: w[0])
        cells = [[row_words[0]]]
        for w in row_words[1:]:
            if w[0] - cells[-1][-1][2] > 12:
                cells.append([w])
            else:
                cells[-1].append(w)
        row_cells.append([" ".join(w[4] for w in cell).strip() for cell in cells])
    return row_cells


def _interpret_table_row(cells: list) -> list:
    """Bir satırın hücrelerini kod/isim/fiyat öğelerine yorumlar. TEK satırda BİRDEN FAZLA ürün
    olabilir (gerçek bir örnekte doğrulandı: sayfada 2 mini-tablo yan yana duruyor, her satırda
    'KOD1, ölçü1, KOD2, ölçü2' şeklinde 2 ayrı ürün art arda) - her yeni kod hücresi görüldüğünde
    önceki öğe kapatılıp yeni bir öğe başlatılır, böylece hem tek hem çoklu ürünlü satırlar aynı
    mantıkla doğru ayrıştırılır."""
    cells = [c for c in cells if turkish_lower(c) not in _TABLE_HEADER_WORDS]
    items = []
    current = None
    i = 0
    while i < len(cells):
        cell = cells[i]
        # "PC" + "1/8 - 04" gibi tip+ölçü ayrı hücrelerde ikili kod - birlikte tek kod say.
        if _TYPE_PREFIX_RE.match(cell) and i + 1 < len(cells) and _SIZE_SPEC_RE.match(cells[i + 1]):
            if current:
                items.append(current)
            current = {"code": f"{cell} {cells[i + 1]}", "name": "", "price": None}
            i += 2
            continue
        if _CODE_RE.match(cell):
            if current:
                items.append(current)
            current = {"code": cell, "name": "", "price": None}
            i += 1
            continue
        if current is None:
            m = _CODE_PREFIX_RE.match(cell)
            if m:
                current = {"code": m.group(1).strip(), "name": m.group(3).strip(), "price": None}
                i += 1
                continue
            i += 1
            continue  # kod hiç başlamadan gelen hücre (başlık artığı vb.) - atla
        if _PRICE_RE.match(cell):
            current["price"] = cell
        elif cell:
            current["name"] = (current["name"] + " " + cell).strip()
        i += 1
    if current:
        items.append(current)
    return items


def _try_extract_text_table(page_words: list, min_rows: int = 4, min_coverage: float = 0.6) -> list:
    """Sayfanın metin katmanından (varsa - taranmış bir görsel DEĞİL, dijital olarak oluşturulmuş
    bir PDF'se) doğrudan, konuma dayalı tablo yeniden inşasıyla ürün listesi çıkarmayı dener. Bu,
    ağır vasıta yedek parça/fiyat listesi PDF'lerinde ÇOK yaygın bir düzen (gerçek 2 katalogda da
    doğrulandı - biri basit tek sütun kod+isim, diğeri kod/isim/fiyatın AYRI metin bloklarında
    olduğu çok sütunlu bir tablo, ikisi de doğru ayrıştırıldı). Başarılı olursa Groq'a HİÇ görsel
    gönderilmez - o sayfa için sıfır token harcanır, sıfır kota riski, saniyeler içinde biter.

    GÜVENLİK İLKESİ: yanlış/uydurma veri üretmektense hiç veri üretmemek her zaman tercih edilir.
    Bu yüzden sadece satırların BÜYÜK ÇOĞUNLUĞU (>= %60) net bir kod içeren satır olarak
    yorumlanabilirse kabul edilir; aksi halde None döner ve çağıran normal (yapay zeka ile)
    taramaya güvenle düşer."""
    row_cells = _rows_by_position(page_words)
    if len(row_cells) < min_rows:
        return None
    row_items = [_interpret_table_row(r) for r in row_cells]  # her satır -> [] veya [öğe, ...]

    # Yetim satır birleştirme: bazı PDF'lerde kod ve isim aynı Y hizasında değil, art arda 2 AYRI
    # satırda duruyor (gerçek bir örnekte doğrulandı) - bir satırda SADECE (tek) kod (isim/fiyat
    # boş), bitişiğinde SADECE düz metin (kod OLMAYAN, boş satır) varsa bunları tek ürün say.
    for i, items in enumerate(row_items):
        if len(items) != 1 or items[0]["name"] or items[0]["price"]:
            continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(row_items) and row_items[j] == []:
                candidate = " ".join(row_cells[j]).strip()
                if candidate and not _CODE_RE.match(candidate) and not _PRICE_RE.match(candidate):
                    items[0]["name"] = candidate
                    row_items[j] = "_MERGED_"
                    break

    explained = sum(1 for items in row_items if items not in ([], "_MERGED_"))
    merged_count = sum(1 for items in row_items if items == "_MERGED_")
    coverage = (explained + merged_count) / len(row_cells) if row_cells else 0
    all_items = [x for items in row_items if items not in ([], "_MERGED_") for x in items]
    if coverage < min_coverage or not all_items:
        return None
    # Ne isim ne fiyat bulunan (sadece çıplak bir kod) öğeler anlamsız - muhtemelen karmaşık bir
    # çoklu-kod satırının ayrıştırma artığı, gerçek bir örnekte gözlemlendi. "kod (isim: kod)"
    # gibi anlamsız bir kayıt kataloğa eklenmesin diye bunlar sessizce atılır.
    #
    # ÖNEMLİ: bazı bağlantı-parçası tablolarında AYNI "kod" hücresi birden fazla satırda tekrar
    # eder, çünkü asıl benzersiz tanımlayıcı kod+ölçü İKİSİ birden (ör. "PCM - G06" satırı hem
    # "12 x 1,5" hem "14 x 1,5" ölçüsüyle ayrı ayrı geçiyor) - gerçek katalogda doğrulandı. Kod
    # tek başına tekrar edip OEM eşleşmesiyle kataloğun kendi üzerine yazmasını (veri kaybını)
    # önlemek için, aynı sayfada tekrar eden bir kod görülürse ölçü/isim koda eklenerek
    # benzersizleştirilir.
    seen_codes = {}
    for x in all_items:
        seen_codes[x["code"]] = seen_codes.get(x["code"], 0) + 1

    # BUG (2026-08-12'de canlıda tespit edildi): burası eskiden TÜM ürün adını koda
    # yapıştırıyordu (ör. '101042-0' yerine '101042-0 Kabin Temizleme Hortumu PE Plastik
    # Tabancalı Rekorsuz') - bu hem kod alanını anlamsızlaştırıyor hem de metin/kod aramasında
    # (find_by_text, match_agent) gereksiz belirsizlik yaratıyordu. Artık sadece kısa, açıkça
    # sentetik bir sıra eki ekleniyor - ürünün asıl ayırt edici bilgisi zaten 'name' alanında
    # duruyor, koda tekrar taşınmasına gerek yok.
    code_occurrence = {}
    result = []
    for x in all_items:
        if not x["name"] and not x["price"]:
            continue
        code = x["code"]
        if seen_codes.get(code, 0) > 1:
            code_occurrence[code] = code_occurrence.get(code, 0) + 1
            if code_occurrence[code] > 1:
                code = f"{code}-alt{code_occurrence[code]}"
        result.append({
            "id": code,
            "oem": code,
            "name": x["name"] or x["code"],
            "brand": "",
            "specs": "",
            "price": x["price"] or "",
            "stock": 1,
        })
    return result or None

CATALOG_WRITE_LOCK = threading.Lock()  # istekler arası paylaşılan kilit - iki ayrı yükleme isteği aynı anda gelirse birbirinin kaydını ezmesin diye

def _catalog_base_name(source_file) -> str:
    """'GÖÇMEN KATALOG .pdf (sayfa 3, metin katmanından...)' -> 'GÖÇMEN KATALOG .pdf' - sayfa/
    açıklama ekini atıp ürünün hangi YÜKLENEN DOSYADAN geldiğini bulur. Katalog bazında gruplama
    (kaç farklı katalog var) ve katalog bazında toplu silme için kullanılır."""
    if not isinstance(source_file, str) or not source_file:
        return ""
    idx = source_file.find(" (sayfa")
    return source_file[:idx].strip() if idx != -1 else source_file.strip()


def _item_sources(item: dict) -> list:
    """Bir ürünün geldiği TÜM kaynak dosyaları döndürür - yeni 'source_files' alanı varsa onu,
    yoksa (eski/tek kaynaklı kayıtlar) tekil 'source_file' alanından türetir."""
    sources = list(item.get("source_files") or [])
    if item.get("source_file") and item["source_file"] not in sources:
        sources.append(item["source_file"])
    return sources


def merge_catalog_items(catalog: list, new_items: list) -> tuple:
    """Yeni taranan parçaları kataloğa ekler. Aynı OEM koduna sahip bir parça (aynı ürün iki farklı
    katalog dosyasında/sayfasında geçmişse) tekrar eklenmez, mevcut kayıt güncellenir - katalogda
    aynı ürünün birden fazla kopyası birikmesin diye. OEM kodu boş/'OEM-BELİRSİZ' olan parçalar
    güvenilir şekilde eşleştirilemeyeceği için (yanlışlıkla farklı iki ürünü birleştirmemek adına)
    her zaman yeni kayıt olarak eklenir.

    Bir ürün BİRDEN FAZLA katalogda geçiyorsa (aynı OEM, farklı yükleme) - gerçek bir kullanımda
    istendi - tüm kaynaklar 'source_files' listesinde BİRİKTİRİLİR, üzerine yazılmaz. 'source_file'
    (tekil) alanı geriye dönük uyumluluk için en son/asıl kaynağı göstermeye devam eder."""
    oem_index = {
        str(item.get("oem", "")).strip().lower(): idx
        for idx, item in enumerate(catalog)
        if item.get("oem") and str(item.get("oem")).strip().upper() != "OEM-BELİRSİZ"
    }
    # BUG (2026-08-11, canlıda gerçekten yaşandı ve veri kaybına yol açtı): yapay zekanın ürettiği
    # id (PRC-XXXX, sadece 4 haneli rastgele sayı) katalog 1000+ kayda ulaştıkça çakışma ihtimali
    # ciddileşen düşük-entropili bir şema. Bir kayıt güncellenirken (aşağıda) veya YENİ eklenirken
    # id çakışırsa: admin panelindeki silme (id'ye göre TÜM eşleşenleri siler) yanlışlıkla alakasız
    # bir ürünü de siler, müşteri aramasındaki id eşleştirmesi de yanlış ürünü gösterebilir. Bu
    # yüzden id benzersizliği burada, kataloğa yazılmadan önce garanti edilir.
    existing_ids = {str(item.get("id")) for item in catalog if item.get("id")}

    def _unique_id(preferred: str) -> str:
        base = (preferred or "PRC").strip() or "PRC"
        if base not in existing_ids:
            return base
        suffix = 2
        candidate = f"{base}-{suffix}"
        while candidate in existing_ids:
            suffix += 1
            candidate = f"{base}-{suffix}"
        return candidate

    added = 0
    updated = 0
    for new_item in new_items:
        oem_key = str(new_item.get("oem", "")).strip().lower()
        is_known_oem = bool(oem_key) and oem_key != "oem-belirsiz"
        if is_known_oem and oem_key in oem_index:
            existing = catalog[oem_index[oem_key]]
            merged_sources = sorted(set(_item_sources(existing)) | set(_item_sources(new_item)))
            new_item["source_files"] = merged_sources
            # new_item mevcut kaydın YERİNE tamamen geçiyor (aşağıda) - ama new_item bu sayfa için
            # bir görsel taşımıyorsa (ör. GitHub'a görsel yedekleme o an başarısız oldu, ya da bu
            # ürün metin-katmanı/regex yoluyla çıkarıldığı başka bir sayfadan geldi) ve eldeki
            # kayıtta ZATEN iyi bir görsel varsa, o görsel burada SESSİZCE kaybolurdu - gerçek bir
            # kullanımda aynı OEM'in iki farklı katalogda/sayfada geçmesi sık olduğu için bu, daha
            # önce backfill ile eklenmiş bir görselin bir sonraki katalog yüklemesinde silinmesi
            # riski demek. Yeni kayıtta görsel yoksa eskisi korunur, varsa (daha güncel/doğru olma
            # ihtimaline karşı) yeni kayıt kazanır.
            if not new_item.get("source_page_image") and existing.get("source_page_image"):
                new_item["source_page_image"] = existing["source_page_image"]
            # id her zaman mevcut kayıttan korunur - bu slot zaten var olan bir ürünü temsil ediyor,
            # yeni taramanın kendi ürettiği (ve başka bir kayıtla çakışabilecek) rastgele id'yle
            # değiştirilmesinin hiçbir faydası yok, sadece risk taşır.
            new_item["id"] = existing.get("id") or _unique_id(str(new_item.get("id", "")))
            catalog[oem_index[oem_key]] = new_item
            updated += 1
        else:
            new_item["id"] = _unique_id(str(new_item.get("id", "")))
            existing_ids.add(new_item["id"])
            new_item["source_files"] = _item_sources(new_item)
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
            # BUG (2026-08-11 tespit edildi): bazı kayıtlarda source_file basit "dosya (sayfa N)"
            # formatında değil, "dosya (sayfa N, metin katmanından otomatik çıkarıldı)" gibi EK
            # açıklama içeriyor (muhtemelen bu kod bir önceki halindeyken taranmış eski kayıtlar).
            # Eskiden bu set TAM string eşleşmesi bekliyordu - page_label (her zaman basit format)
            # bu ek açıklamalı kayıtlarla HİÇ eşleşmiyordu, yani "zaten taranmış" kontrolü bu
            # sayfalar için hiç çalışmıyordu; aynı dosya tekrar yüklenince gereksiz yere yeniden
            # taranıp AI kotası boşa harcanıyordu. Artık sadece dosya adı+sayfa numarası
            # normalize edilerek karşılaştırılıyor, açıklama eki göz ardı ediliyor.
            already_scanned = set()
            for item in _load_catalog_from_disk():
                sf = item.get("source_file")
                if not isinstance(sf, str):
                    continue
                m = re.match(re.escape(filename) + r" \(sayfa (\d+)", sf)
                if m:
                    already_scanned.add(f"{filename} (sayfa {m.group(1)})")
            items = []
            for _page_num, page_label, page_path, page_words in render_pdf_pages_to_images(file_path, filename=filename, already_scanned=already_scanned):
                try:
                    # 3 KADEMELİ TARAMA (en ucuzdan en pahalıya):
                    # 1) ÜCRETSİZ: sabit regex/konum kalıpları (_try_extract_text_table) - sayfanın
                    #    gerçek bir metin katmanı varsa ve düzen daha önce görülmüş bir kalıba
                    #    uyuyorsa Groq'a HİÇ gitmeden anında ve bedavaya çıkarılır.
                    # 2) UCUZ AI: regex tanıyamadı ama metin katmanı VARSA, sayfanın GÖRSELİ yerine
                    #    ÇIKARILMIŞ METNİ Groq'a gönderilir - metin girdisi görselden çok daha az
                    #    token tutar, ÜSTELİK Groq regex'in tanımadığı HERHANGİ bir tablo düzenini
                    #    de anlayabilir - kullanıcı "çok farklı katalog atacağım" dediği için yeni
                    #    her format için elle regex eklemek yerine bu kademe genel çözümü sağlar.
                    # 3) PAHALI AI GÖRSEL: sayfada hiç metin katmanı yoksa (gerçekten taranmış bir
                    #    görsel/fotoğrafsa) tek çare budur, eskisi gibi çalışır.
                    page_items = _try_extract_text_table(page_words)
                    if page_items is None and page_words:
                        text_block = _page_words_to_text_block(page_words)
                        prompt = CATALOG_TEXT_SCAN_PROMPT_TEMPLATE.format(page_text=text_block)
                        try:
                            # Görsel yok, TPM bütçesinde daha fazla boşluk var - 4500'den (görsel
                            # yolu) daha yüksek bir çıktı sınırı verilebilir, yoğun sayfalar için.
                            page_items = call_groq_json_array(prompt, pool="bulk", max_output_tokens=6000)
                        except Exception:
                            page_items = None  # metin tabanlı deneme de başarısız oldu - görsel yola düş
                    if page_items is None:
                        page_items = call_groq_json_array(CATALOG_SCAN_PROMPT, page_path, pool="bulk")
                    # Sayfa görseli (2026-08-11'de eklendi): müşteri bir ürün bulduğunda o ürünün
                    # geçtiği GERÇEK katalog sayfasını/fotoğrafını da görsün istendi. page_path bu
                    # fonksiyon bitince (finally'de) silindiği için, gerçek fotoğrafı içeren bu
                    # sayfa GitHub'a kalıcı olarak yedeklenir - Render'ın ephemeral diski silse
                    # bile /catalog-page-image uç noktası GitHub'dan tekrar çekebilir. Sadece
                    # gerçekten ürün çıkan sayfalar için (boş/başlık sayfaları için gereksiz yere
                    # yedekleme yapılmaz).
                    if page_items:
                        image_name = re.sub(r"[^\w.-]", "_", f"{filename}_sayfa{_page_num}") + ".png"
                        try:
                            if sync_binary_file_to_github(page_path, f"{CATALOG_IMAGE_DIR}/{image_name}", f"Katalog sayfa görseli: {image_name}"):
                                for item in page_items:
                                    item["source_page_image"] = image_name
                        except Exception:
                            pass  # görsel yedekleme başarısız olsa bile ürün verisi kaybolmaz, sadece görselsiz kalır
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
    # KRİTİK (2026-08-12'de canlıda veri kaybına yol açtı - bkz. _refresh_catalog_disk_from_github
    # docstring'i): iş başlamadan önce yerel disk GitHub'ın en güncel haliyle tazelenir.
    with CATALOG_WRITE_LOCK:
        _refresh_catalog_disk_from_github()

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

    def _remaining_tokens(pool):
        real = usage_tracker.get_real_remaining_tokens(pool)
        return real if real is not None else usage.get(pool, {}).get("remaining_estimate", usage_tracker.DAILY_TOKEN_BUDGET)

    def _remaining_requests(pool):
        # Groq'un kendi panelinden doğrulandı (2026-08-10): günlük 1000 İSTEK sınırı da var, sadece
        # token değil - "kota bitti" hatalarının bir kısmı aslında bu sınırdan kaynaklanmış olabilir,
        # önceden hiç takip edilmiyordu. Katalog taraması sayfa başına 1 istek olduğu için (Tier 2/3),
        # yüzlerce sayfalı bir katalogda bu, token sınırından ÖNCE tükenebilir.
        return usage.get(pool, {}).get("requests_remaining_estimate", usage_tracker.DAILY_REQUEST_BUDGET)

    any_room = any(_remaining_tokens(p) >= 2000 and _remaining_requests(p) >= 5 for p in chain_pools)
    if not any_room:
        return {
            "status": "error",
            "message": "Bugünkü yapay zeka kullanım kotası (denenen tüm hesaplarda, token veya günlük istek sayısı) doldu. "
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


def _source_file_matches_page(source_file, filename: str, page_num: int) -> bool:
    """source_file bazen basit 'dosya (sayfa N)' formatında, bazen 'dosya (sayfa N, metin
    katmanından otomatik çıkarıldı)' gibi ek açıklamalı - bkz. _scan_catalog_source'daki
    2026-08-11 notu. İkisini de doğru eşleştirmek için tam eşitlik yerine önek karşılaştırması
    kullanılır (sadece dosya adı + '(sayfa N' önekine bakılır, sonrasında ne olduğu önemsiz)."""
    if not isinstance(source_file, str):
        return False
    prefix = f"{filename} (sayfa {page_num}"
    return source_file == f"{filename} (sayfa {page_num})" or source_file.startswith(prefix + ",") or source_file.startswith(prefix + ")")


def _run_backfill_images_job(job_id: str, saved_paths: list):
    """MEVCUT (daha önce taranmış) katalog ürünlerine geriye dönük sayfa görseli ekler - bkz.
    2026-08-11 notu (_scan_catalog_source). Orijinal PDF dosyaları hiçbir yerde kalıcı
    saklanmadığı için (işlendikten hemen sonra siliniyordu), görsel eklemek için tek yol PDF'in
    TEKRAR yüklenmesi. AMA bu adım HİÇ AI çağrısı yapmaz (token/kota maliyeti YOK) - sadece
    her sayfayı render edip, o sayfadan daha önce çıkarılmış (source_file eşleşen) MEVCUT
    kayıtlara görseli iliştirir. Zaten görseli olan veya bu dosyadan hiç ürün çıkmamış sayfalar
    atlanır."""
    job = _catalog_jobs[job_id]
    matched_pages = 0
    updated_items = 0
    already_ok_pages = 0  # eşleşen kayıt(lar) vardı ama HEPSİNİN zaten görseli vardı - yapacak iş yoktu
    skipped_non_pdf = []  # PDF olmayan bir dosya bu forma yüklenirse (kabul EDİLİR ama işlenmez) - kullanıcıya net bildirilsin diye
    # BUG (2026-08-12'de canlıda tespit edildi): GitHub'a gönderme eskiden SADECE tüm dosyalar
    # bitince (fonksiyonun en sonunda) yapılıyordu - Render bu tür uzun işler sırasında zaman
    # zaman kendiliğinden yeniden başladığı için (gerçek bir denemede 83 sayfalık bir PDF'in
    # 31. sayfasında oldu), o ana kadar yerel diske işlenmiş TÜM ilerleme (onlarca sayfanın
    # görseli) hiç GitHub'a gitmeden kayboluyordu - iş "kesintiye uğradı, ama şuna kadarki
    # sayfalar zaten kalıcı kaydedildi" diyordu, oysa hiçbiri kalıcı değildi. Artık ana katalog
    # yükleme işiyle AYNI desen: en fazla birkaç sayfada bir (ya da 20sn'de bir) senkronize edilir.
    pages_since_sync = 0
    last_sync_time = time.time()
    # KRİTİK (2026-08-12'de canlıda veri kaybına yol açtı - bkz. _refresh_catalog_disk_from_github
    # docstring'i): iş başlamadan önce yerel disk GitHub'ın en güncel haliyle tazelenir - aksi
    # halde bu süreç dışında (elle bir düzeltme/ekleme) yapılmış değişiklikler bu işin sonunda
    # sessizce geri silinebilir.
    with CATALOG_WRITE_LOCK:
        _refresh_catalog_disk_from_github()
    for filename, file_path in saved_paths:
        try:
            if not filename.lower().endswith(".pdf"):
                skipped_non_pdf.append(filename)
                continue
            doc = fitz.open(file_path)
            zoom_matrix = fitz.Matrix(2.0, 2.0)
            try:
                for page_index in range(len(doc)):
                    page_num = page_index + 1

                    def _item_matches(item):
                        if _source_file_matches_page(item.get("source_file"), filename, page_num):
                            return True
                        return any(_source_file_matches_page(sf, filename, page_num) for sf in (item.get("source_files") or []))

                    with CATALOG_WRITE_LOCK:
                        catalog = _load_catalog_from_disk()
                        matching_items = [item for item in catalog if _item_matches(item)]
                        needs_image = matching_items and not all(item.get("source_page_image") for item in matching_items)
                    with _catalog_jobs_lock:
                        job["pages_done"] += 1
                        job["current_file"] = f"{filename} (sayfa {page_num}/{len(doc)})"
                    if not needs_image:
                        if matching_items:
                            already_ok_pages += 1
                        continue
                    page = doc[page_index]
                    pix = page.get_pixmap(matrix=zoom_matrix)
                    base_name = os.path.splitext(os.path.basename(file_path))[0]
                    page_path = os.path.join(CATALOG_DIR, f"{base_name}_backfill_sayfa{page_num}.png")
                    pix.save(page_path)
                    del pix
                    try:
                        image_name = re.sub(r"[^\w.-]", "_", f"{filename}_sayfa{page_num}") + ".png"
                        if sync_binary_file_to_github(page_path, f"{CATALOG_IMAGE_DIR}/{image_name}", f"Katalog sayfa görseli (geriye dönük): {image_name}"):
                            with CATALOG_WRITE_LOCK:
                                catalog = _load_catalog_from_disk()
                                for item in catalog:
                                    if _item_matches(item):
                                        item["source_page_image"] = image_name
                                        updated_items += 1
                                save_catalog(catalog)
                            matched_pages += 1
                            pages_since_sync += 1
                            if pages_since_sync >= 3 or (time.time() - last_sync_time) >= 20:
                                sync_catalog_to_github()
                                pages_since_sync = 0
                                last_sync_time = time.time()
                    finally:
                        if os.path.exists(page_path):
                            os.remove(page_path)
            finally:
                doc.close()
        except Exception as e:
            with _catalog_jobs_lock:
                job["failed"].append(f"{filename}: {str(e)}")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
    sync_catalog_to_github()
    # Mesaj parça parça kurulur çünkü "eşleşme yok" ile "eşleşme var ama zaten görseli vardı" farklı
    # durumlar - eskiden ikisi de aynı "hiçbir sayfa için eşleşen kayıt bulunamadı" mesajını
    # veriyordu, bu da AYNI dosyayı ikinci kez yükleyen (ör. ilk seferde kısmi başarı sonrası tekrar
    # deneyen) bir kullanıcıyı "demek ki dosya adı yanlış" diye yanlış yönlendirebilirdi.
    parts = []
    if matched_pages:
        parts.append(f"{matched_pages} sayfanın görseli eklendi, {updated_items} ürün kaydı güncellendi.")
    if already_ok_pages:
        parts.append(f"{already_ok_pages} sayfada eşleşen kayıt bulundu ama zaten görseli vardı, değişiklik yapılmadı.")
    if skipped_non_pdf:
        parts.append(f"{len(skipped_non_pdf)} dosya PDF olmadığı için atlandı: {', '.join(skipped_non_pdf)}.")
    # BUG (2026-08-11 gerçek bir testte tespit edildi): job['failed'] (ör. bozuk/geçersiz bir PDF -
    # fitz.open() FileDataError fırlatıyor) DOLDURULUYORDU ama mesaja hiç yansımıyordu - kullanıcı
    # bozuk kendi PDF'ini yüklediğinde "✅ başarılı, hiçbir sayfa eşleşmedi" gibi YANILTICI bir
    # sonuç görüyordu, dosyanın aslında hiç AÇILAMADIĞINI fark etmesi imkansızdı.
    if job["failed"]:
        parts.append(f"{len(job['failed'])} dosya işlenemedi: " + "; ".join(job["failed"]))
    if not parts:
        parts.append("Bu dosyadaki hiçbir sayfa için eşleşen katalog kaydı bulunamadı (dosya adı ilk yüklendiğindekiyle birebir aynı olmalı).")
    with _catalog_jobs_lock:
        # Sadece bozuk/açılamayan dosyalar varsa VE hiçbir gerçek ilerleme (görsel eklenmesi/eşleşme/
        # atlanan sayfa) olmadıysa "error" (kırmızı) - kısmi başarıda (bazı dosyalar iyi, biri bozuk)
        # yine de "success" kalır, aksi halde iyi giden dosyaların sonucu da kırmızı kartla gizlenirdi.
        job["status"] = "error" if (job["failed"] and not (matched_pages or already_ok_pages or skipped_non_pdf)) else "success"
        job["message"] = " ".join(parts)


@app.post("/backfill-catalog-images")
def backfill_catalog_images(files: list[UploadFile] = File(...), _: str = Depends(require_admin)):
    """Zaten taranmış bir kataloğun (ör. GÖÇMEN KATALOG, ORPASAN) orijinal PDF'i tekrar
    yüklendiğinde, o kataloğun MEVCUT ürün kayıtlarına geriye dönük sayfa görseli ekler - HİÇ
    yeniden taramaz/AI kullanmaz, sadece görselleri eşleştirip iliştirir (bkz. _run_backfill_images_job)."""
    saved_paths = []
    for file in files:
        safe_name = f"{uuid.uuid4().hex}_{os.path.basename(file.filename)}"
        file_path = os.path.join(CATALOG_DIR, safe_name)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        saved_paths.append((file.filename, file_path))

    job_id = uuid.uuid4().hex
    with _catalog_jobs_lock:
        _catalog_jobs[job_id] = {
            "status": "processing",
            "total_files": len(saved_paths),
            "done_files": 0,
            "pages_done": 0,
            "current_file": saved_paths[0][0] if saved_paths else None,
            "failed": [],
            "message": "",
        }
    threading.Thread(target=_run_backfill_images_job, args=(job_id, saved_paths), daemon=True).start()
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
    # Katalog yedeklemesi (GitHub'a gönderme) sessizce bozulursa - Render'ın diski her yeniden
    # deploy'da sıfırlandığı için - o güne kadar eklenen HER ŞEY bir sonraki deploy'da kaybolur,
    # kimse fark etmeden. Bu yüzden son senkronizasyon durumu burada görünür kılınıyor - yönetim
    # sayfaları 3+ üst üste başarısızlıkta uyarı gösteriyor.
    result["backup_status"] = dict(_github_sync_status)
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
# "directory" (turkbusinesscenter.com, sanayisitesi.com.tr ağı) ve "tyres" kaynaklı lead'ler
# firma-beyanlı/kendiliğinden kategorize edilmiş veriye dayanıyor - gerçek bir kullanımda tespit
# edildi ki bu, skoru YÜKSEK çıkan ama aslında tamamen alakasız firmalar (örn. "Car Lease Rent A
# Car", bir araç kiralama şirketi) üretebiliyor. Kural bazlı sertleştirme (bkz. lead_scoring.py)
# bilinen örüntüleri şimdiden ayıklıyor ama önceden görülmemiş örüntülere karşı ikinci bir göz
# olarak, bu iki kaynak tipinden gelen lead'ler SKORLARI YÜKSEK OLSA BİLE (sadece 20-60 aralığı
# değil) AI netleştirme adayı sayılır - önceden sadece "belirsiz" (20-60) aralığındakiler
# gözden geçiriliyordu, yani bu iki kaynaktan gelen YÜKSEK skorlu (ve dolayısıyla hiç
# sorgulanmayan) yanlış pozitifler tamamen gözden kaçıyordu.
_RISKY_SELF_REPORTED_SHOP_TYPES = {"directory", "tyres"}


def _needs_ai_review(lead: dict, ai_scores: dict) -> bool:
    """Modül seviyesinde (closure değil) tutuluyor ki hem endpoint içinde hem izole testlerde
    aynı fonksiyon çağrılabilsin - davranış ile test edilen şey birebir aynı olsun diye."""
    if lead.get("lead_id") in ai_scores:
        return False
    score = lead.get("relevance_score") or 0
    if CLASSIFY_SCORE_MIN <= score <= CLASSIFY_SCORE_MAX:
        return True
    if lead.get("raw_shop_type") in _RISKY_SELF_REPORTED_SHOP_TYPES and score > CLASSIFY_SCORE_MAX:
        return True
    return False


def _ai_review_priority_key(lead: dict):
    # En riskli (yüksek skorlu ama kendiliğinden/firma-beyanlı kategorize) olanlar önce
    # değerlendirilsin - bunlar yanlış pozitif olduğunda en çok zarar veren (yüksek skorla üst
    # sıralarda görünüp hiç sorgulanmadan onaylanma riski taşıyan) kayıtlar.
    return (lead.get("raw_shop_type") not in _RISKY_SELF_REPORTED_SHOP_TYPES, -(lead.get("relevance_score") or 0))


@app.post("/leads/classify-ambiguous")
def classify_ambiguous_leads(_: str = Depends(require_admin)):
    """Kural bazlı skorlamanın net karar veremediği (ne çok yüksek ne çok düşük skorlu)
    lead'leri, VE kaynağı kendiliğinden/firma-beyanlı kategorize edilmiş (bu yüzden skor ne
    olursa olsun daha az güvenilir) lead'leri Groq ile toplu değerlendirir. Zaten
    değerlendirilmiş olanları tekrar sormaz."""
    leads = lead_store.load_leads()
    ai_scores = lead_store.load_lead_ai_scores()

    candidates = [l for l in leads if _needs_ai_review(l, ai_scores)]
    candidates.sort(key=_ai_review_priority_key)

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

def _safe_product_fit_score(value) -> int:
    """AI'dan gelen skor bazen sayı yerine metin/karışık alan olarak dönebiliyor (örn. reasoning
    metni score alanına kaymış) - int() direkt patlarsa tüm batch (20 lead) kaydedilmeden
    kayboluyordu. Artık geçersiz/aralık dışı değer tek bir lead'i 0'a düşürür, batch'in geri
    kalanını etkilemez."""
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 0

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
            if not isinstance(r, dict):
                continue
            lid = r.get("lead_id")
            if not lid:
                continue
            product_scores[product_key]["scores"][lid] = {
                "product_fit_score": _safe_product_fit_score(r.get("product_fit_score", 0)),
                "product_fit_reasoning": str(r.get("product_fit_reasoning", "")),
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
def save_broadcast(
    message: str = Form(...),
    origin: str = Form("manuel"),
    recipient_count: int = Form(0),
    _: str = Depends(require_admin),
):
    """Bir taslağı (AI önerili veya sıfırdan yazılmış) geçmişe 'gönderildi' olarak kaydeder.
    `recipient_count` (opsiyonel): WhatsApp gönderim kuyruğu (bkz. broadcast.html) ile kaç
    kişiye gönderim ONAYLANDIĞI - tek tek her alıcı için ayrı log satırı açmak yerine (100 kişilik
    bir kuyruk 100 neredeyse aynı log satırı üretirdi) tek bir özet satırda gösterilir."""
    log = outreach.load_broadcast_log()
    entry = {
        "id": f"bc_{int(time.time() * 1000)}",
        "message": message,
        "origin": origin,  # "ai" veya "manuel"
        "recipient_count": recipient_count,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    log.append(entry)
    outreach.save_broadcast_log(log)
    outreach.sync_broadcast_log_to_github()
    return {"status": "success"}

@app.get("/whatsapp-api-status")
def whatsapp_api_status(_: str = Depends(require_admin)):
    """broadcast.html'in, WhatsApp Business Platform (Cloud API) yapılandırılmış mı yoksa
    hâlâ elle wa.me akışı mı kullanılacak diye kontrol etmesi için."""
    return {"configured": whatsapp_business_api.is_configured()}

@app.post("/broadcast/send-via-api")
def send_broadcast_via_api(
    contacts: str = Form(...),  # JSON: [{"name": "...", "phone": "..."}]
    _: str = Depends(require_admin),
):
    """WhatsApp Business Platform (Cloud API) üzerinden ONAYLI ŞABLONLA otomatik gönderim -
    SADECE WHATSAPP_BUSINESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID tanımlıysa çalışır (aksi halde net
    bir hata döner, broadcast.html hâlâ elle wa.me akışına düşer). Soğuk lead'lere serbest metin
    DEĞİL, Meta'nın onayladığı şablon gönderilir - bkz. whatsapp_business_api.py."""
    if not whatsapp_business_api.is_configured():
        return {"status": "error", "message": "WhatsApp Business Platform henüz yapılandırılmamış (WHATSAPP_BUSINESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID gerekli)."}
    try:
        contact_list = json.loads(contacts)
    except Exception:
        return {"status": "error", "message": "Kişi listesi okunamadı."}

    results = []
    for c in contact_list:
        digits = re.sub(r"\D", "", str(c.get("phone", "")))
        if digits.startswith("0"):
            digits = "90" + digits[1:]
        elif len(digits) == 10:
            digits = "90" + digits
        name = c.get("name", "")
        if not digits:
            results.append({"name": name, "phone": "", "status": "error", "message": "Geçersiz telefon numarası."})
            continue
        res = whatsapp_business_api.send_template_message(digits, body_params=[name])
        results.append({"name": name, "phone": digits, "status": res.get("status"), "message": res.get("message", "")})

    success_count = sum(1 for r in results if r["status"] == "success")
    return {
        "status": "success",
        "sent": success_count,
        "failed": len(results) - success_count,
        "results": results,
    }

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
        return {"status": "error", "message": f"Kritik Sistem Hatası: {_customer_safe_error(e)}"}
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

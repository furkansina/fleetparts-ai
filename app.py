import os
import shutil
import json
import base64
import time
import requests
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse

app = FastAPI(title="FleetParts AI - Universal Heavy Duty Master Engine")

# API Anahtarı / Token (Render ortamından GROQ_API_KEY olarak çeker) - console.groq.com/keys, kredi kartı gerektirmez
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "qwen/qwen3.6-27b"

UPLOAD_DIR = "temp_images"
CATALOG_DIR = "sample_catalogs"
CATALOG_FILE = "catalog.json"

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

def load_catalog():
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        seed_default_catalog()
        return DEFAULT_CATALOG

def call_groq_api(prompt: str, image_path: str = None) -> str:
    """Groq (OpenAI uyumlu) chat completions uç noktasına istek atan evrensel bağlantı yöneticisi"""
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
    else:
        content = prompt

    payload = {"model": GROQ_MODEL, "reasoning_effort": "none", "messages": [{"role": "user", "content": content}]}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }

    last_error = ""
    for attempt in range(3):
        try:
            res = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=60)
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"]
            if res.status_code == 429:
                last_error = f"HTTP 429: {res.text}"
                time.sleep(10)
                continue
            raise Exception(f"HTTP {res.status_code}: {res.text}")
        except requests.RequestException as e:
            raise Exception(f"Groq Bağlantı Hatası: {str(e)}")

    raise Exception(f"Groq kota limiti aşıldı, tekrar denendi ama başarısız oldu: {last_error}")

def extract_json_object(raw_text: str) -> dict:
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()
    start = clean_text.find("{")
    end = clean_text.rfind("}") + 1
    if start == -1 or end <= start:
        raise ValueError(f"Yanıtta JSON bulunamadı: {raw_text[:200]!r}")
    return json.loads(clean_text[start:end])

def call_groq_json(prompt: str, image_path: str = None) -> dict:
    """JSON bekleyen çağrılar için: modelin bozuk/boş yanıt verdiği durumlarda bir kez daha dener."""
    last_error = None
    for attempt in range(2):
        raw_text = call_groq_api(prompt, image_path)
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
    Görseldeki parça ne kadar kirli, paslı, yağlı veya kötü açıyla çekilmiş olursa olsun odaklan ve şu teknik verileri çıkar:

    1. OCR Optik Karakter Taraması: Parça üzerindeki döküm yazılarını, OEM numaralarını, silik etiketleri ve seri numaralarını harf harf oku.
    2. Topolojik Mühendislik Haritası: Parçanın rekorlarını, dişli hatve yapılarını, cıvata/montaj delik sayısını, elektrik pin/soketlerini detaylı say.
    3. Geometrik Sınıflandırma: Parçanın ana kategorisini (Örn: Fren Sistemleri, Hava Valfleri, Filtrasyon, Hidrolik vb.) ve tam adını belirle.

    Çıktıyı SADECE ve kesinlikle şu JSON formatında ver:
    {
      "is_part_detected": true,
      "universal_category": "Fren / Hava Sistemi / Filtre / Süspansiyon / Diğer",
      "exact_name_classification": "Parçanın Sektörel Net Adı",
      "ocr_extracted_codes": ["Kod1", "Kod2", "Bulunamazsa Boş Liste"],
      "topology_map": {
        "ports_or_threads": "Rekor, boru veya dişli bağlantı detayları ve sayıları",
        "electrical_pins_or_sockets": "Elektronik soket, pin veya sensör uçları",
        "mounting_holes_and_flanges": "Civata delikleri, kulaklar veya flanş yapısı"
      },
      "geometry_and_material": "Malzeme cinsi (Alüminyum döküm, sac, plastik, balata materyali vb.) ve fiziksel form"
    }
    """
    try:
        return call_groq_json(prompt, image_path)
    except Exception as e:
        raise Exception(f"Universal Tarama Hatası: {str(e)}")

# ---------------------------------------------------------
# EVRENSEL AJAN 2: UNIVERSAL PRECISION MATCHER
# ---------------------------------------------------------
def match_agent(vision_data: dict) -> dict:
    catalog = load_catalog()
    if not catalog:
        return {"id": "NOT_IN_CATALOG", "name": "Katalog Boş", "match_reason": "Veritabanında kayıtlı ürün bulunamadı."}

    prompt = f"""
    Sen sıfır hata toleransına sahip kurumsal bir parça eşleştirme motorusun.
    Müşterinin sahadan gönderdiği parçanın tarama verisi:
    {json.dumps(vision_data, ensure_ascii=False)}

    Sistemimizdeki Tüm Parça Katalog Veritabanı:
    {json.dumps(catalog, ensure_ascii=False)}

    EŞLEŞTİRME PRENSİPLERİ:
    1. OEM / KOD EŞLEŞMESİ: Tarama verisindeki 'ocr_extracted_codes' içindeki herhangi bir kod katalogdaki 'oem' veya 'id' ile uyuşuyorsa güven skoru direkt %100'dür.
    2. TOPOLOJİK UYUM: Kod okunamadıysa; parça kategorisi, rekor/delik sayıları ve fiziksel özellikleri katalogdaki ürünlerin 'specs' bilgileriyle kıyaslanır. Uyum oranı hesaplanır.
    3. Eşleşme skoru %70'in altındaysa kesinlikle yanlış parça riskine girilmez ve 'NOT_IN_CATALOG' döndürülür.

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
            for item in catalog:
                if str(item.get("id")) == str(matched_id):
                    item_copy = item.copy()
                    item_copy["match_reason"] = f"Kesinlik Skoru: %{score} | Doğrulama: {decision}"
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

    q = query.strip().lower()

    # 1. Önce birebir OEM/ID eşleşmesi dene (hızlı ve %100 güvenilir, yapay zekaya gerek yok)
    for item in catalog:
        if q == str(item.get("oem", "")).strip().lower() or q == str(item.get("id", "")).strip().lower():
            item_copy = item.copy()
            item_copy["match_reason"] = "OEM/katalog kodu ile birebir eşleşme (%100)"
            return item_copy

    # 2. Birebir kod eşleşmesi yoksa, yapay zekaya isim/açıklama bazlı eşleştirt (örn: "DAF sol çamurluk")
    prompt = f"""
    Sen sıfır hata toleransına sahip kurumsal bir parça eşleştirme motorusun.
    Sahadaki kullanıcı, elinde fotoğraf olmadan şu metni yazdı: "{query}"
    Bu metin bir OEM kodu, marka+parça adı (örn: "DAF sol çamurluk") veya serbest bir açıklama olabilir.

    Sistemimizdeki Tüm Parça Katalog Veritabanı:
    {json.dumps(catalog, ensure_ascii=False)}

    EŞLEŞTİRME PRENSİPLERİ:
    1. Yazılan metin katalogdaki 'oem' veya 'id' alanına (kısmen de olsa) uyuyorsa güven skoru yüksek olmalıdır.
    2. Kod uyuşmuyorsa, metindeki marka/parça adı katalogdaki 'name', 'brand' ve 'specs' alanlarıyla anlam olarak kıyaslanır.
    3. Eşleşme skoru %70'in altındaysa kesinlikle yanlış parça riskine girilmez ve 'NOT_IN_CATALOG' döndürülür.

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
            for item in catalog:
                if str(item.get("id")) == str(matched_id):
                    item_copy = item.copy()
                    item_copy["match_reason"] = f"Kesinlik Skoru: %{score} | Doğrulama: {decision}"
                    return item_copy

        return {
            "id": "NOT_IN_CATALOG",
            "name": "Katalog Dışı / Eşleşme Sağlanamadı",
            "match_reason": f"Benzerlik skoru (%{score}) yeterli eşik değerinin altında kaldı. Kanıt: {decision}"
        }
    except Exception as e:
        raise Exception(f"Metin Arama Motoru Hatası: {str(e)}")

# ---------------------------------------------------------
# EVRENSEL AJAN 3: B2B PROFESSIONAL SALES AGENT
# ---------------------------------------------------------
def sales_agent(product_data: dict, customer_type: str, price_note: str) -> str:
    if product_data.get("id") == "NOT_IN_CATALOG":
        return "Sayın İş Ortağımız, gönderdiğiniz yedek parça görseli evrensel katalogumuzda yüksek doğrulukla eşleştirilememiştir. Yanlış sevkiyatın önüne geçmek adına lütfen parçanın OEM kodunu veya araç şase (VIN) numarasını iletiniz."

    fiyat = price_note if price_note else "Güncel kur ve iskonto oranları için iletişime geçiniz."

    prompt = f"""
    Sen ağır vasıta yedek parça sektöründe faaliyet gösteren kurumsal bir B2B tedarik sisteminin satış asistanısın.
    Müşteri Profili: {customer_type}
    Tespit Edilen Ürün: {product_data.get('name')}
    Marka / Kalite: {product_data.get('brand')}
    OEM Kodu: {product_data.get('oem')}
    Fiyat / Not: {fiyat}
    Stok Durumu: Mevcut ({product_data.get('stock', 'Hazır')} adet)

    Görev: WhatsApp ve kurumsal iletişim kanalları için; tamamen profesyonel, net, yorumsuz, parça durumu eleştirisi barındırmayan saf ticari sipariş/teklif mesajı oluştur.
    """
    try:
        return call_groq_api(prompt)
    except:
        return f"Ürün Başarıyla Tespit Edildi: {product_data.get('name')} (OEM: {product_data.get('oem')}). Stoklarımızda mevcuttur. Bilgilerinize sunarız."

# ---------------------------------------------------------
# FASTAPI ENDPOINT MİMARİSİ
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "<h2>FleetParts AI - Universal Heavy Duty Master Engine Aktif</h2>"

@app.get("/get-catalog")
async def get_catalog_endpoint():
    return {"catalog": load_catalog(), "files": os.listdir(CATALOG_DIR)}

@app.post("/upload-catalog-files")
async def upload_catalog_files(files: list[UploadFile] = File(...)):
    try:
        catalog = load_catalog()
        added_count = 0

        for file in files:
            file_path = os.path.join(CATALOG_DIR, file.filename)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            prompt = """
            Bu evrensel katalog dosyasından/görselinden her tür ağır vasıta yedek parçasını tara.
            SADECE şu JSON yapısında kusursuz veri çıkar:
            {
                "id": "PRC-" + rasgele 4 haneli sayı,
                "oem": "Parçanın OEM Kodu veya numarası (Yoksa 'OEM-BELİRSİZ')",
                "name": "Sektörel Resmi Parça Adı",
                "brand": "Üretici veya Marka",
                "specs": "Bağlantı rekorları, pinler, ölçüler ve teknik detaylar",
                "stock": 25
            }
            """
            item_data = call_groq_json(prompt, file_path)
            catalog.append(item_data)
            added_count += 1

        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=4)

        return {"status": "success", "message": f"{added_count} adet evrensel yedek parça kataloğa işlendi."}
    except Exception as e:
        return {"status": "error", "message": f"Katalog Yükleme Hatası: {str(e)}"}

@app.post("/process-part")
async def process_part(
    file: UploadFile = File(None),
    query: str = Form(""),
    customer_type: str = Form("Kurumsal Filo / Toptancı"),
    price_note: str = Form("")
):
    if not file and not query.strip():
        return {"status": "error", "message": "Fotoğraf yükleyin veya OEM kodu / parça adı girin."}

    file_path = os.path.join(UPLOAD_DIR, file.filename) if file else None
    try:
        if file:
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            # 1. Aşama: Evrensel Endüstriyel Tarama & OCR
            vision_res = vision_agent(file_path)

            # 2. Aşama: Matris Algoritmik Eşleştirme
            matched_prod = match_agent(vision_res)
        else:
            # Fotoğraf yok: OEM kodu veya parça adına göre doğrudan katalog araması
            vision_res = {"note": "Fotoğrafsız metin araması yapıldı.", "query": query}
            matched_prod = find_by_text(query)

        # 3. Aşama: B2B Kurumsal Satış Teklifi
        sales_msg = sales_agent(matched_prod, customer_type, price_note)

        return {
            "status": "success",
            "agents_output": {
                "vision_analysis": vision_res,
                "matched_product": matched_prod,
                "final_sales_message": sales_msg
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

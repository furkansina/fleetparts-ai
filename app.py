import os
import shutil
import json
import base64
import requests
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse

app = FastAPI(title="FleetParts AI - Universal Heavy Duty Master Engine")

# API Anahtarı / Token (Render ortamından çeker)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6IPhISGbVUlc0GZ_I28dwWjGZSNV37AjFt9gx-EvAVjAQ")

UPLOAD_DIR = "temp_images"
CATALOG_DIR = "sample_catalogs"
CATALOG_FILE = "catalog.json"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CATALOG_DIR, exist_ok=True)

# Başlangıç Evrensel Katalog Veritabanı
if not os.path.exists(CATALOG_FILE):
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump([
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
        ], f, ensure_ascii=False, indent=4)

def load_catalog():
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def call_gemini_api(prompt: str, image_path: str = None) -> str:
    """OAuth Token ve Standart API Key destekli evrensel bağlantı yöneticisi"""
    models_to_try = ["gemini-1.5-pro", "gemini-2.0-flash", "gemini-1.5-flash"]
    parts = [{"text": prompt}]
    
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as img_f:
            b64_img = base64.b64encode(img_f.read()).decode("utf-8")
        ext = image_path.split('.')[-1].lower()
        mime_type = "image/png" if ext == "png" else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime_type, "data": b64_img}})
        
    payload = {"contents": [{"parts": parts}]}
    headers = {"Content-Type": "application/json"}
    
    # Kimlik Doğrulama Türünü Otomatik Algıla (API Key vs OAuth Bearer Token)
    if GEMINI_API_KEY.startswith("AIza"):
        url_template = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=" + GEMINI_API_KEY
    else:
        # OAuth / Bearer Token desteği (AQ... vb.)
        headers["Authorization"] = f"Bearer {GEMINI_API_KEY}"
        url_template = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    last_error = ""
    for model in models_to_try:
        url = url_template.format(model=model)
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=50)
            if res.status_code == 200:
                data = res.json()
                return data['candidates'][0]['content']['parts'][0]['text']
            else:
                last_error = f"HTTP {res.status_code}: {res.text}"
        except Exception as e:
            last_error = str(e)
            continue
            
    raise Exception(f"Universal API Kritik Bağlantı Hatası: {last_error}")

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
        raw_text = call_gemini_api(prompt, image_path)
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        start = clean_text.find("{")
        end = clean_text.rfind("}") + 1
        return json.loads(clean_text[start:end])
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
        raw_text = call_gemini_api(prompt)
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        start = clean_text.find("{")
        end = clean_text.rfind("}") + 1
        result = json.loads(clean_text[start:end])
        
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
        return call_gemini_api(prompt)
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
            raw_res = call_gemini_api(prompt, file_path)
            clean_res = raw_res.replace("```json", "").replace("```", "").strip()
            start = clean_res.find("{")
            end = clean_res.rfind("}") + 1
            item_data = json.loads(clean_res[start:end])
            
            catalog.append(item_data)
            added_count += 1
            
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=4)
            
        return {"status": "success", "message": f"{added_count} adet evrensel yedek parça kataloğa işlendi."}
    except Exception as e:
        return {"status": "error", "message": f"Katalog Yükleme Hatası: {str(e)}"}

@app.post("/process-part")
async def process_part(
    file: UploadFile = File(...),
    customer_type: str = Form("Kurumsal Filo / Toptancı"),
    price_note: str = Form("")
):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # 1. Aşama: Evrensel Endüstriyel Tarama & OCR
        vision_res = vision_agent(file_path)
        
        # 2. Aşama: Matris Algoritmik Eşleştirme
        matched_prod = match_agent(vision_res)
        
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
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

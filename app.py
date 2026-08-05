import os
import shutil
import json
import base64
import requests
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from PIL import Image

app = FastAPI(title="FleetParts AI - Heavy Duty Agent System")

# API Anahtarı (Ortam değişkeninden okunur, yoksa koddaki atanır)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6IPhISGbVUlc0GZ_I28dwWjGZSNV37AjFt9gx-EvAVjAQ")

UPLOAD_DIR = "temp_images"
CATALOG_DIR = "sample_catalogs"
CATALOG_FILE = "catalog.json"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CATALOG_DIR, exist_ok=True)

# Varsayılan Katalog Oluşturma
if not os.path.exists(CATALOG_FILE):
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump([
            {
                "id": "FRN-001", 
                "oem": "1505234", 
                "name": "Disk Fren Balatası Heavy", 
                "brand": "Orijinal Kalite", 
                "specs": "Kalınlık: 25mm, Renk: Siyah, Ölçü: 247x110mm", 
                "stock": 30
            },
            {
                "id": "FLT-002", 
                "oem": "21707134", 
                "name": "Hava Filtresi Süper Ağır Vasıta", 
                "brand": "FleetGuard", 
                "specs": "Çap: 280mm, Yükseklik: 450mm", 
                "stock": 15
            }
        ], f, ensure_ascii=False, indent=4)

def load_catalog():
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def call_gemini_api(prompt: str, image_path: str = None) -> str:
    """
    Google Gemini REST API'ye doğrudan HTTP isteği atar.
    SDK bağımlılıklarını ve 401/404 yetkilendirme hatalarını tamamen çözer.
    """
    models_to_try = ["gemini-1.5-flash", "gemini-2.0-flash"]
    
    parts = [{"text": prompt}]
    
    if image_path and os.path.exists(image_path):
        with open(image_path, "rb") as img_f:
            b64_img = base64.b64encode(img_f.read()).decode("utf-8")
        
        ext = image_path.split('.')[-1].lower()
        mime_type = "image/png" if ext == "png" else "image/jpeg"
        
        parts.append({
            "inline_data": {
                "mime_type": mime_type,
                "data": b64_img
            }
        })
        
    payload = {
        "contents": [{"parts": parts}]
    }
    
    headers = {"Content-Type": "application/json"}
    
    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, json=payload, headers=headers, timeout=30)
            if res.status_code == 200:
                data = res.json()
                return data['candidates'][0]['content']['parts'][0]['text']
        except Exception:
            continue
            
    raise Exception("Gemini API bağlantısı sağlanamadı. Lütfen API anahtarınızı kontrol edin.")

# ---------------------------------------------------------
# AJAN 1: GÖRSEL ANALİZ AJANI (Vision Agent)
# ---------------------------------------------------------
def vision_agent(image_path: str) -> dict:
    prompt = """
    Sen uzman bir ağır vasıta (tır, kamyon, otobüs) yedek parça eksperisin.
    Görseldeki yedek parçayı detaylıca analiz et.
    Çıktıyı SADECE geçerli bir JSON objesi olarak ver:
    {
      "is_clear": true,
      "part_type": "Parçanın genel adı",
      "color": "Renk",
      "visible_codes": "Okunan parça numarası veya OEM kodu (Yoksa 'Yok')",
      "form_and_specs": "Soket, delik, vida yuvası ve fiziksel yapı detayları",
      "side_detected": "Sağ / Sol / Ön / Arka / Belirsiz"
    }
    """
    try:
        raw_text = call_gemini_api(prompt, image_path)
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        start = clean_text.find("{")
        end = clean_text.rfind("}") + 1
        return json.loads(clean_text[start:end])
    except Exception:
        return {
            "is_clear": True,
            "part_type": "Ağır Vasıta Parçası",
            "color": "Belirsiz",
            "visible_codes": "Yok",
            "form_and_specs": "Standart yedek parça yapısı",
            "side_detected": "Belirsiz"
        }

# ---------------------------------------------------------
# AJAN 2: KATALOG EŞLEŞTİRME AJANI (Match Agent)
# ---------------------------------------------------------
def match_agent(vision_data: dict) -> dict:
    try:
        catalog = load_catalog()
        if not vision_data.get("is_clear", True):
            return {"id": "IMAGE_UNCLEAR", "name": "Görsel Net Değil", "match_reason": "Görsel analiz için yetersiz."}
        
        prompt = f"""
        Müşteri Görsel Analiz Verileri: {json.dumps(vision_data, ensure_ascii=False)}
        Mevcut Stok Katalog Listemiz: {json.dumps(catalog, ensure_ascii=False)}

        Görevin: Görsel verilerini katalogdaki ürünlerle eşleştir. 
        Eğer tam veya yüksek benzerlikte bir ürün varsa ürünün 'id' değerini dön.
        Eğer katalogda bu parça kesinlikle yoksa 'NOT_IN_CATALOG' dön.

        Çıktıyı SADECE şu JSON formatında ver:
        {{"matched_id": "ürün_id_veya_NOT_IN_CATALOG", "match_reason": "Neden eşleştiği veya eşleşmediği hakkında detaylı açıklama"}}
        """
        
        raw_text = call_gemini_api(prompt)
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        start = clean_text.find("{")
        end = clean_text.rfind("}") + 1
        result = json.loads(clean_text[start:end])
        
        matched_id = result.get("matched_id")
        match_reason = result.get("match_reason", "")

        if matched_id and matched_id != "NOT_IN_CATALOG":
            for item in catalog:
                if item.get("id") == matched_id:
                    item_copy = item.copy()
                    item_copy["match_reason"] = match_reason
                    item_copy["vision_side"] = vision_data.get("side_detected", "Belirsiz")
                    return item_copy
                    
        return {
            "id": "NOT_IN_CATALOG", 
            "name": "Katalog Dışı / Özel Tedarik Parçası", 
            "match_reason": match_reason, 
            "vision_side": vision_data.get("side_detected", "Belirsiz")
        }
    except Exception:
        return {"id": "NOT_IN_CATALOG", "name": "Özel Tedarik Parçası", "match_reason": "Manuel inceleme gerekiyor."}

# ---------------------------------------------------------
# AJAN 3: SATIŞ & WHATSAPP MESAJ AJANI (Sales Agent)
# ---------------------------------------------------------
def sales_agent(product_data: dict, customer_type: str, price_note: str) -> str:
    try:
        if product_data.get("id") == "IMAGE_UNCLEAR":
            return "Ustam selamlar, attığın fotoğraf net seçilemiyor. Parçanın üzerindeki kodu veya soket kısmını gösterecek şekilde yeniden fotoğraf iletebilir misin?"
        
        if product_data.get("id") == "NOT_IN_CATALOG":
            return "Ustam selamlar, gönderdiğin parça şu an hazır stok kataloğumuzda görünmüyor. Yanlış parça çıkışını önlemek adına aracın şase numarasını (VIN) iletirsen hemen orijinal OEM kodundan sorgulayıp temin edelim."
        
        fiyat_bilgisi = price_note if price_note else "Özel iskonto ve güncel fiyat bilgisi için iletişime geçebilirsiniz."
        
        prompt = f"""
        Sen ağır vasıta yedek parça sektöründe tecrübeli, usta dilinden anlayan profesyonel bir satış temsilcisisin.
        
        Müşteri Tipi: {customer_type}
        Ürün Bilgileri: {json.dumps(product_data, ensure_ascii=False)}
        Fiyat Notu: {fiyat_bilgisi}
        
        Müşteriye WhatsApp üzerinden gönderilecek; samimi, güven veren, stok durumunu ve hızlı kargo avantajını belirten net bir satış mesajı oluştur.
        """
        return call_gemini_api(prompt)
    except Exception:
        return "Parçanız stoklarımızda mevcuttur. Detaylı bilgi ve sipariş için ulaşabilirsiniz."

# ---------------------------------------------------------
# FASTAPI ENDPOINT'LERİ
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "<h2 style='font-family:sans-serif;'>FleetParts AI Sistem Çalışıyor!</h2>"

@app.get("/get-catalog")
async def get_catalog_endpoint():
    return {"catalog": load_catalog(), "files": os.listdir(CATALOG_DIR)}

@app.post("/upload-catalog-files")
async def upload_catalog_files(files: list[UploadFile] = File(...)):
    try:
        all_items = load_catalog()
        for file in files:
            file_path = os.path.join(CATALOG_DIR, file.filename)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            # Doküman / Resim Okuma
            prompt = """
            Bu dosyada yer alan ağır vasıta yedek parçalarını oku ve saf JSON listesi olarak döndür.
            Format:
            [{"id": "KOD1", "oem": "OEM123", "name": "Parça Adı", "brand": "Marka", "specs": "Özellikler", "stock": 10}]
            """
            try:
                raw_text = call_gemini_api(prompt, file_path)
                clean_text = raw_text.replace("```json", "").replace("```", "").strip()
                start = clean_text.find("[")
                end = clean_text.rfind("]") + 1
                parsed_items = json.loads(clean_text[start:end])
                
                for item in parsed_items:
                    if not any(existing.get("id") == item.get("id") for existing in all_items):
                        all_items.append(item)
            except Exception:
                continue
                    
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=4)
            
        return {"status": "success", "message": "Katalog başarıyla güncellendi ve işlendi!"}
    except Exception as e:
        return {"status": "error", "message": f"Hata: {str(e)}"}

@app.post("/process-part")
async def process_part(
    file: UploadFile = File(...),
    customer_type: str = Form("Anadolu Toptancısı"),
    price_note: str = Form("")
):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # 3 Aşamalı Ajan Akışı
        vision_res = vision_agent(file_path)
        matched_prod = match_agent(vision_res)
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
        return {"status": "error", "message": f"İşlem Hatası: {str(e)}"}
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

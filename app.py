import os
import shutil
import json
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from google import genai
from PIL import Image

app = FastAPI(title="FleetParts AI - Heavy Duty Agent")

# Yeni oluşturduğun API Anahtarı eklendi
client = genai.Client(api_key="AQ.Ab8RN6KyNTs3BQaDoO599uL4Kxuy-hx9tMUKfi_YEcKh0ARDSg")

UPLOAD_DIR = "temp_images"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("sample_catalogs", exist_ok=True)

CATALOG_FILE = "catalog.json"
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
            }
        ], f, ensure_ascii=False, indent=4)

def load_catalog():
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def vision_agent(image_path: str) -> dict:
    try:
        image = Image.open(image_path)
        prompt = """
        Bu ağır vasıta yedek parça görselini detaylıca analiz et.
        Şu JSON formatında net bir çıktı ver:
        {
          "is_clear": true,
          "part_type": "Parçanın genel adı",
          "color": "Renk",
          "visible_codes": "Okunan numara veya OEM kodu yoksa 'Yok'",
          "form_and_specs": "Soket, delik ve fiziksel form",
          "side_detected": "Sağ / Sol / Ön / Arka / Belirsiz"
        }
        """
        response = client.models.generate_content(model="gemini-1.5-flash", contents=[image, prompt])
        if not response or not response.text:
            return {"is_clear": True, "part_type": "Parça", "color": "Belirsiz", "visible_codes": "Yok", "form_and_specs": "Standart", "side_detected": "Belirsiz"}
        
        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        return json.loads(raw_text[start:end])
    except Exception:
        return {"is_clear": True, "part_type": "Parça", "color": "Belirsiz", "visible_codes": "Yok", "form_and_specs": "Belirsiz", "side_detected": "Belirsiz"}

def match_agent(vision_data: dict) -> dict:
    try:
        catalog = load_catalog()
        if not vision_data.get("is_clear", True):
            return {"id": "IMAGE_UNCLEAR", "name": "Görsel Yetersiz", "risk_note": "Görsel net değil."}
        
        prompt = f"""
        Müşteri Görseli Analiz Verisi: {json.dumps(vision_data, ensure_ascii=False)}
        Mevcut Parça Katalog Listemiz: {json.dumps(catalog, ensure_ascii=False)}
        
        Görevin: Görsel verilerini katalogla karşılaştır, en yüksek eşleşmeyi bul. Eşleşme yoksa 'NOT_IN_CATALOG' döndür.
        Çıktıyı SADECE şu JSON formatında ver:
        {{"matched_id": "bulunan_id_yada_NOT_IN_CATALOG", "match_reason": "Kısa açıklama"}}
        """
        
        response = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
        if not response or not response.text:
            return {"id": "NOT_IN_CATALOG", "name": "Özel Tedarik Parçası"}

        raw_text = response.text.replace("```json", "").replace("

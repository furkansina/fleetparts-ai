import os
import shutil
import json
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from google import genai
from PIL import Image

app = FastAPI(title="FleetParts AI - Heavy Duty Agent")

client = genai.Client(api_key="AQ.Ab8RN6KDpRVhFJkFnmKXHd5pGMIETsr9gdserDGgjz8DKW_qdQ")

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
        response = client.models.generate_content(model="gemini-2.5-flash", contents=[image, prompt])
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
        
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        if not response or not response.text:
            return {"id": "NOT_IN_CATALOG", "name": "Özel Tedarik Parçası"}

        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        result = json.loads(raw_text[start:end])
        
        matched_id = result.get("matched_id")
        match_reason = result.get("match_reason", "")

        if matched_id and matched_id != "NOT_IN_CATALOG":
            for item in catalog:
                if item.get("id") == matched_id:
                    item["match_reason"] = match_reason
                    item["vision_side"] = vision_data.get("side_detected", "Belirsiz")
                    return item
                    
        return {"id": "NOT_IN_CATALOG", "name": "Katalog Dışı / Özel Tedarik Parçası", "match_reason": match_reason, "vision_side": vision_data.get("side_detected", "Belirsiz")}
    except Exception:
        return {"id": "NOT_IN_CATALOG", "name": "Özel Tedarik Parçası"}

def sales_agent(product_data: dict, customer_type: str, price_note: str) -> str:
    try:
        if product_data.get("id") == "IMAGE_UNCLEAR":
            return "Ustam selamlar, attığın fotoğraf net çıkmamış. Soket ve etiket kısmını gösterecek şekilde yeni bir fotoğraf atabilir misin?"
        if product_data.get("id") == "NOT_IN_CATALOG":
            return "Ustam selamlar, bu parça mevcut kataloğumuzda görünmüyor. Yanlış parça yollamayalım, aracın şase numarasını veya üzerindeki OEM kodunu iletebilir misin?"
        
        fiyat = price_note if price_note else "Özel iskonto ve fiyat için arayabilirsin"
        prompt = f"Ağır vasıta yedek parça piyasasında tecrübeli samimi bir satış yetkilisisin. Müşteri: {customer_type}, Ürün: {json.dumps(product_data, ensure_ascii=False)}, Fiyat: {fiyat}. WhatsApp satış mesajı yaz."
        
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text if response and response.text else "Parçanız stoklarımızda mevcuttur."
    except Exception:
        return "Parçanız incelenmiştir, detaylar için iletişime geçebilirsiniz."

@app.get("/", response_class=HTMLResponse)
async def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "<h3>FleetParts AI Çalışıyor!</h3>"

@app.get("/get-catalog")
async def get_catalog_endpoint():
    return {"catalog": load_catalog(), "files": os.listdir("sample_catalogs")}

@app.post("/upload-catalog-files")
async def upload_catalog_files(files: list[UploadFile] = File(...)):
    try:
        all_items = load_catalog()
        for file in files:
            file_path = os.path.join("sample_catalogs", file.filename)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            uploaded_file_ref = client.files.upload(file=file_path)
            prompt = """
            Bu dokümandan tüm ürünleri oku ve şu formatta JSON listesi çıkar:
            [{"id": "kod", "oem": "oem", "name": "ad", "brand": "marka", "specs": "detay", "stock": 10}]
            SADECE saf JSON dizisi döndür.
            """
            
            response = client.models.generate_content(model="gemini-2.5-flash", contents=[uploaded_file_ref, prompt])
            raw_text = response.text.replace("```json", "").replace("```", "").strip()
            
            start = raw_text.find("[")
            end = raw_text.rfind("]") + 1
            parsed_items = json.loads(raw_text[start:end])
            
            for item in parsed_items:
                if not any(existing.get("id") == item.get("id") for existing in all_items):
                    all_items.append(item)
                    
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=4)
            
        return {"status": "success", "message": "Katalog başarıyla işlendi!"}
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
        return {"status": "error", "message": str(e)}
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

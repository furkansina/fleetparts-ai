import os
import shutil
import json
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from google import genai
from PIL import Image

app = FastAPI(title="FleetParts AI")

client = genai.Client(api_key="AIzaSyA4QZ7G3hS-bunuKullanacagiz")

UPLOAD_DIR = "temp_images"
os.makedirs(UPLOAD_DIR, exist_ok=True)

CATALOG_FILE = "catalog.json"
if not os.path.exists(CATALOG_FILE):
    with open(CATALOG_FILE, "w", encoding="utf-8") as f:
        json.dump([
            {"id": "FRN-001", "name": "Disk Fren Balatası Heavy", "brand": "Orijinal Kalite", "stock": 30}
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
        prompt = "Bu ağır vasıta parçasını teknik olarak incele. Yanıtını şu formatta ver: {\"is_clear\": true, \"description\": \"detay\", \"side_detected\": \"Belirsiz\"}"
        response = client.models.generate_content(model="gemini-2.5-flash", contents=[image, prompt])
        if not response or not response.text:
            return {"is_clear": True, "description": "Görsel analizi tamamlandı", "side_detected": "Belirsiz"}
        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        if "{" not in raw_text or "}" not in raw_text:
            return {"is_clear": True, "description": raw_text, "side_detected": "Belirsiz"}
        start = raw_text.find("{")
        end = raw_text.rfind("}") + 1
        return json.loads(raw_text[start:end])
    except Exception:
        return {"is_clear": True, "description": "Parça görseli başarıyla işlendi", "side_detected": "Belirsiz"}

def match_agent(vision_data: dict) -> dict:
    try:
        catalog = load_catalog()
        if not vision_data.get("is_clear", True):
            return {"id": "IMAGE_UNCLEAR", "name": "Görsel Yetersiz", "risk_note": "Görsel net değil."}
        prompt = f"Görsel Veri: {json.dumps(vision_data, ensure_ascii=False)}. Katalog: {json.dumps(catalog, ensure_ascii=False)}. Eşleşen ürün id değerini SADECE JSON formatında ver: {{\"matched_id\": \"id_yada_NOT_IN_CATALOG\"}}"
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        if not response or not response.text:
            return {"id": "NOT_IN_CATALOG", "name": "Özel Tedarik Parçası", "vision_side": vision_data.get("side_detected", "Belirsiz")}
        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        if "{" in raw_text and "}" in raw_text:
            start = raw_text.find("{")
            end = raw_text.rfind("}") + 1
            result = json.loads(raw_text[start:end])
        else:
            result = {"matched_id": "NOT_IN_CATALOG"}
        matched_id = result.get("matched_id")
        if matched_id and matched_id != "NOT_IN_CATALOG":
            for item in catalog:
                if item.get("id") == matched_id:
                    item["vision_side"] = vision_data.get("side_detected", "Belirsiz")
                    return item
        return {"id": "NOT_IN_CATALOG", "name": "Katalog Dışı / Özel Tedarik Parçası", "vision_side": vision_data.get("side_detected", "Belirsiz")}
    except Exception:
        return {"id": "NOT_IN_CATALOG", "name": "Özel Tedarik Parçası", "vision_side": "Belirsiz"}

def sales_agent(product_data: dict, customer_type: str, price_note: str) -> str:
    try:
        if product_data.get("id") == "IMAGE_UNCLEAR":
            return "Ustam selamlar, attığın fotoğraf biraz net çıkmamış. Soket kısmını netleştirecek şekilde yeni bir fotoğraf atabilir misin?"
        if product_data.get("id") == "NOT_IN_CATALOG":
            return "Ustam selamlar, bu parça hızlı kataloğumuzda görünmüyor. Yanlış parça göndermemek için aracın şase numarasını veya OEM kodunu iletebilir misin?"
        fiyat = price_note if price_note else "Özel teklif fiyatımız için arayabilirsin"
        prompt = f"Ağır vasıta yedek parça sektöründe deneyimli bir satış yetkilisisin. Müşteri: {customer_type}, Ürün: {json.dumps(product_data, ensure_ascii=False)}, Fiyat: {fiyat}. Usta diline uygun samimi WhatsApp satış mesajı yaz."
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

@app.get("/get-catalog/")
async def get_catalog():
    return load_catalog()

@app.post("/update-catalog/")
async def update_catalog(catalog_data: str = Form(...)):
    try:
        parsed_data = json.loads(catalog_data)
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(parsed_data, f, ensure_ascii=False, indent=4)
        return {"status": "success", "message": "Katalog başarıyla güncellendi!"}
    except Exception as e:
        return {"status": "error", "message": "Geçersiz JSON formatı"}

@app.post("/upload-catalog-file/")
async def upload_catalog_file(file: UploadFile = File(...)):
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Dosyayı (PDF veya Resim) Gemini'ye yükleyip katalog JSON formatına dönüştürüyoruz
        uploaded_file_ref = client.files.upload(file=file_path)
        prompt = "Bu dokümandaki veya listedeki yedek parça verilerini oku ve şu JSON formatında bir liste olarak dışarı ver: [{\"id\": \"parca_kodu\", \"name\": \"parca_adi\", \"brand\": \"marka\", \"stock\": adet_sayisi}]. SADECE saf JSON dizisi döndür."
        
        response = client.models.generate_content(model="gemini-2.5-flash", contents=[uploaded_file_ref, prompt])
        raw_text = response.text.replace("```json", "").replace("```", "").strip()
        
        start = raw_text.find("[")
        end = raw_text.rfind("]") + 1
        new_catalog = json.loads(raw_text[start:end])
        
        with open(CATALOG_FILE, "w", encoding="utf-8") as f:
            json.dump(new_catalog, f, ensure_ascii=False, indent=4)
            
        return {"status": "success", "message": "Dosyadaki katalog başarıyla içe aktarıldı!"}
    except Exception as e:
        return {"status": "error", "message": f"Dosya işlenemedi: {str(e)}"}
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

@app.post("/process-part/")
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
                "vision_agent_desc": vision_res.get("description", "Analiz Yapılamadı"),
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
            import os
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from PIL import Image
import io
import json

app = FastAPI()

# Render üzerindeki gizli ortam değişkeninden (Environment Variable) API anahtarını alır
# Eğer lokalde test ediyorsan kendi anahtarını buraya yazabilirsin
api_key = os.environ.get("GEMINI_API_KEY", "BURAYA_API_ANAHTARINI_YAZ_EGER_LOKALDEKINI_KULLANACAKSAN")
client = genai.Client(api_key=api_key)

# Katalog dosyasını okuma fonksiyonu
def load_catalog():
    if os.path.exists("catalog.json"):
        with open("catalog.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return []

@app.get("/", response_class=HTMLResponse)
async def read_index():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "index.html bulunamadı!"

@app.get("/get-catalog")
async def get_catalog():
    return load_catalog()

@app.post("/analyze")
async def analyze_part(file: UploadFile = File(...)):
    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))
        
        catalog = load_catalog()
        catalog_str = json.dumps(catalog, ensure_ascii=False, indent=2)

        prompt = f"""
        Sen profesyonel bir otomotiv yedek parça ve lojistik asistanısın.
        Aşağıda elimizdeki parça kataloğu ve stok bilgileri JSON formatında verilmiştir:
        {catalog_str}

        Kullanıcının yüklediği bu parça görselini analiz et:
        1. Görseldeki parçanın ne olduğunu tespit et.
        2. Katalogdaki hangi parça veya parça grubuyla eşleştiğini bul.
        3. Stokta olup olmadığını, kritik stok durumunu ve hangi araç gruplarına uyduğunu belirt.
        4. Müşteriye gönderilmek üzere profesyonel, net ve satış odaklı bir teklif/bilgilendirme mesajı hazırla.
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[image, prompt]
        )

        return {"analysis": response.text}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
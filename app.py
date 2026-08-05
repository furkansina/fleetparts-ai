import os
import json
import io
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from google import genai
from PIL import Image

app = FastAPI()

api_key = os.environ.get("GEMINI_API_KEY", "")
client = genai.Client(api_key=api_key)

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
    return "<h3>index.html bulunamadı!</h3>"

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
        3. Stokta olup olmadığını ve hangi araç gruplarına uyduğunu belirt.
        4. Müşteriye gönderilmek üzere profesyonel, net ve satış odaklı bir teklif/bilgilendirme mesajı hazırla.
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[image, prompt]
        )

        return {"analysis": response.text}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

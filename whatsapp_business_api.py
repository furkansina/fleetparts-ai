import os

import requests

# WhatsApp Business Platform (Cloud API) - Meta'nın resmi, ücretli, sunucu tarafından mesaj
# atmaya izin veren API'si. "WhatsApp Business" TELEFON UYGULAMASI ile KARIŞTIRILMAMALI - o
# uygulamanın hiçbir otomasyon/API imkanı yok. Bu modül sadece Meta Business Platform (Cloud
# API) için yazıldı.
#
# ÖNEMLİ SINIRLAMA (kod değil, Meta'nın kuralı): işletmenin daha önce hiç mesajlaşmadığı bir
# numarayla "business-initiated" konuşma başlatmak (soğuk lead'e ilk mesaj) SADECE önceden
# Meta'ya onaylatılmış bir ŞABLON (template) ile mümkün - serbest metin YASAK. Şablonlar Meta
# Business Manager üzerinden oluşturulup onaylanmalı (genelde birkaç saat-birkaç gün sürer).
# Ayrıca yüksek engelleme/şikayet oranı Meta'nın kendi "kalite puanı" sistemini tetikler ve
# hesabı otomatik kısıtlar/kapatır - otomasyon bu riski ORTADAN KALDIRMIYOR, sadece elle tıklama
# zahmetini kaldırıyor. Bu yüzden bu modül serbest metin değil, ŞABLON mesajı gönderir.
#
# GEREKEN ORTAM DEĞİŞKENLERİ (Meta Business Manager -> WhatsApp -> API Setup'tan alınır):
#   WHATSAPP_BUSINESS_TOKEN       - kalıcı erişim token'ı (System User token, geçici olmayan)
#   WHATSAPP_PHONE_NUMBER_ID      - gönderen numaranın Phone Number ID'si (telefon numarasının kendisi değil)
#   WHATSAPP_TEMPLATE_NAME        - onaylanmış ilk-temas şablonunun adı (varsayılan: "ilk_temas")
#   WHATSAPP_TEMPLATE_LANG        - şablon dili (varsayılan: "tr")
API_VERSION = "v21.0"


def is_configured() -> bool:
    return bool(os.environ.get("WHATSAPP_BUSINESS_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))


def _phone_number_id() -> str:
    return os.environ["WHATSAPP_PHONE_NUMBER_ID"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['WHATSAPP_BUSINESS_TOKEN']}",
        "Content-Type": "application/json",
    }


def send_template_message(phone_intl: str, body_params: list = None) -> dict:
    """Onaylanmış bir ŞABLON ile ilk-temas mesajı gönderir (soğuk lead'ler için Meta'nın
    izin verdiği TEK yol). phone_intl: başında 0/+ olmadan tam uluslararası format (örn.
    905551234567). body_params: şablondaki {{1}}, {{2}} gibi değişkenlerin sırasıyla değerleri
    (örn. [firma_adi])."""
    if not is_configured():
        return {"status": "error", "message": "WhatsApp Business Platform yapılandırılmamış (WHATSAPP_BUSINESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID eksik)."}

    template_name = os.environ.get("WHATSAPP_TEMPLATE_NAME", "ilk_temas")
    template_lang = os.environ.get("WHATSAPP_TEMPLATE_LANG", "tr")

    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": phone_intl,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": template_lang},
            "components": components,
        },
    }
    url = f"https://graph.facebook.com/{API_VERSION}/{_phone_number_id()}/messages"
    try:
        res = requests.post(url, headers=_headers(), json=payload, timeout=20)
        data = res.json()
        if res.status_code == 200:
            return {"status": "success", "message_id": data.get("messages", [{}])[0].get("id", "")}
        return {"status": "error", "message": data.get("error", {}).get("message", f"HTTP {res.status_code}")}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def send_free_text_message(phone_intl: str, text: str) -> dict:
    """SERBEST METİN mesajı - SADECE karşı taraf son 24 saat içinde İLK ÖNCE mesaj attıysa
    (Meta'nın '24 saatlik müşteri hizmeti penceresi' kuralı) kullanılabilir. Soğuk/ilk temas
    için KULLANILAMAZ - Meta bunu reddeder. Bu yüzden broadcast akışında değil, sadece bir lead
    size WhatsApp'tan geri döndükten sonraki yanıtlarda kullanılmalı."""
    if not is_configured():
        return {"status": "error", "message": "WhatsApp Business Platform yapılandırılmamış."}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_intl,
        "type": "text",
        "text": {"body": text},
    }
    url = f"https://graph.facebook.com/{API_VERSION}/{_phone_number_id()}/messages"
    try:
        res = requests.post(url, headers=_headers(), json=payload, timeout=20)
        data = res.json()
        if res.status_code == 200:
            return {"status": "success", "message_id": data.get("messages", [{}])[0].get("id", "")}
        return {"status": "error", "message": data.get("error", {}).get("message", f"HTTP {res.status_code}")}
    except Exception as e:
        return {"status": "error", "message": str(e)}

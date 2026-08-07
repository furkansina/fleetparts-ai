import re

_SUFFIXES = ["LTD ŞTİ", "LİMİTED ŞİRKETİ", "A Ş", "AŞ", "TİC", "SAN", "TİCARET", "SANAYİ", "VE"]


def turkish_upper(text: str) -> str:
    """Python'un varsayılan .upper()'ı Türkçe küçük 'i' harfini normal (noktasız) İngilizce
    'I'ya çeviriyor - oysa Türkçe'de küçük 'i'nin büyük hali noktalı 'İ'dir. Bu fark, aynı
    firma adının kaynağa göre farklı şekilde büyütülüp (örn. "İş Kamyon" / "iş kamyon" iki
    farklı normalize sonucu üretip) yanlışlıkla tekrar (duplicate) eklenmesine yol açabiliyordu."""
    return (text or "").replace("i", "İ").replace("ı", "I").upper()


def turkish_lower(text: str) -> str:
    """turkish_upper'ın tersi - Python'un varsayılan .lower()'ı Türkçe büyük 'İ' harfini 'i' +
    görünmez bir noktalama işaretine (U+0307) çeviriyor, normal 'i' harfine değil. Bu da örneğin
    'İş Makinesi' gibi büyük İ ile başlayan isimlerin anahtar kelime eşleşmesinde sessizce
    kaçırılmasına sebep oluyordu (gerçek bir testte tespit edildi)."""
    return (text or "").replace("İ", "i").replace("I", "ı").lower()


def normalize_name(name: str) -> str:
    n = turkish_upper(name or "")
    n = re.sub(r"[.,]", " ", n)
    for suffix in _SUFFIXES:
        n = n.replace(suffix, "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else ""


def is_mobile_phone(phone: str) -> bool:
    """Türkiye'de cep telefonu numaraları 05XX ile başlar (WhatsApp'tan ulaşılabilir).
    Sabit hat numaraları (0212, 0312, 0362 gibi il/ilçe alan kodları) 5 ile başlamaz -
    bu numaralara WhatsApp mesajı gönderilemez, sadece telefonla aranabilir."""
    last10 = normalize_phone(phone)
    return last10.startswith("5")


def dedupe_key(name: str, phone: str) -> str:
    """Aynı firmanın birden fazla kaynaktan/farklı yazımla gelmesi durumunda
    tekilleştirme için kullanılan anahtar. Telefon varsa öncelik ondadır (daha güvenilir)."""
    phone_norm = normalize_phone(phone)
    if phone_norm:
        return f"phone_{phone_norm}"
    return f"name_{normalize_name(name)}"

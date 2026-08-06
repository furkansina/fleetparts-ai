import re

_SUFFIXES = ["LTD ŞTİ", "LİMİTED ŞİRKETİ", "A Ş", "AŞ", "TİC", "SAN", "TİCARET", "SANAYİ", "VE"]


def normalize_name(name: str) -> str:
    n = (name or "").upper()
    n = re.sub(r"[.,]", " ", n)
    for suffix in _SUFFIXES:
        n = n.replace(suffix, "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else ""


def dedupe_key(name: str, phone: str) -> str:
    """Aynı firmanın birden fazla kaynaktan/farklı yazımla gelmesi durumunda
    tekilleştirme için kullanılan anahtar. Telefon varsa öncelik ondadır (daha güvenilir)."""
    phone_norm = normalize_phone(phone)
    if phone_norm:
        return f"phone_{phone_norm}"
    return f"name_{normalize_name(name)}"

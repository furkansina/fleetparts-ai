import re

_SUFFIXES = ["LTD ŞTİ", "LİMİTED ŞİRKETİ", "A Ş", "AŞ", "TİC", "SAN", "TİCARET", "SANAYİ", "VE"]
# BUG (2026-08-12'de bir kod denetiminde tespit edildi): normalize_name eskiden bu listeyi sırayla
# n.replace(suffix, "") ile (kelime sınırı OLMADAN) uyguluyordu. "SAN" listede "SANAYİ"den ÖNCE
# geldiği için, "SAN" önce silinince geriye "SANAYİ"nin "AYİ" kalıntısı kalıyor, "SANAYİ" hiçbir
# zaman BÜTÜN kelime olarak eşleşemiyordu (aynı sorun "TİC"/"TİCARET" için de geçerliydi). Ayrıca
# sınır olmadan "SAN"/"VE" gibi kısa dizeler firma isminin İÇİNDE geçen alakasız bir kelimenin
# parçasını da silebiliyordu (örn. "SANDIKÇI OTOMOTİV" -> "SAN" kaybolup "DIKÇI OTOMOTİV" kalırdı).
# Artık TÜM ekler TEK bir regex geçişinde, kelime sınırlarıyla (\b...\b) ve UZUNDAN KISAYA
# sıralanarak (alternation "SANAYİ"yi "SAN"dan önce dener) siliniyor - hem sıra bağımlılığı hem
# yanlışlıkla kelime-içi eşleşme riski ortadan kalkıyor.
_SUFFIX_PATTERN = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(_SUFFIXES, key=len, reverse=True)) + r")\b")


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


_TR_FOLD_MAP = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u"})


def turkish_fold(text: str) -> str:
    """Türkçe özel karakterleri (ç,ğ,ı,ö,ş,ü) düz ASCII eşdeğerine çevirir - turkish_lower'dan
    SONRA uygulanmak üzere tasarlandı (zaten küçük harfli metin bekler, büyük harf formlarıyla
    ilgilenmez). BUG (2026-08-12'de canlı bir testte tespit edildi): katalog metin araması
    Türkçe karakterleri OLDUĞU GİBİ karşılaştırıyordu - müşterinin çoğu telefon klavyesi
    alışkanlığıyla 'çamurluk' yerine 'camurluk' yazması gayet yaygın bir gerçek kullanım
    senaryosu, ama bu durumda katalogdaki 'ÇAMURLUK' hiçbir zaman eşleşmiyordu (aynı kelimenin
    doğru Türkçe yazımı '177 aday' bulurken diyakritiksiz hali '0 aday' veriyordu - doğrulandı).
    Bu fonksiyon bir EK/yedek karşılaştırma katmanı olarak kullanılır - önce tam (diyakritikli)
    eşleşme denenir, o başarısız olursa bu katlanmış hale düşülür; mevcut doğru davranış hiçbir
    şekilde bozulmaz, sadece ek bir kurtarma yolu eklenir."""
    return (text or "").translate(_TR_FOLD_MAP)


def normalize_name(name: str) -> str:
    n = turkish_upper(name or "")
    n = re.sub(r"[.,]", " ", n)
    n = _SUFFIX_PATTERN.sub("", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else ""


# Rehber kaynaklarında (turkbusinesscenter.com, sanayi siteleri) bir firma birden fazla numara
# yayınladığında ham veri "+905323935693;+902428181153" gibi NOKTALI VİRGÜLLE AYRILMIŞ tek bir
# metin olarak geliyordu. normalize_phone() TÜM string'i tek bir rakam dizisine çevirip sondan
# 10 hane aldığı için bu durumda iki farklı numaranın rakamları birbirine karışıp GERÇEKTE HİÇBİR
# NUMARAYA ait olmayan uydurma bir sonuç üretiyordu - hem dedupe_key hem is_mobile_phone hem de
# panelin WhatsApp linki bu bozuk numarayı kullanıyordu (2026-08-11 canlı veri denetiminde
# tespit edildi). sanitize_phone() bunu kaynakta çözer: sadece İLK numarayı alır, belirgin
# sahte/placeholder numaraları (000-0000000, aynı rakamın tekrarı, "-") ve kesik/eksik haneli
# numaraları eler.
_FAKE_TAIL_RE = re.compile(r"^(\d)\1{6,}$")  # son 7+ hanenin hepsi aynı rakam (0000000, 2222222 vb.)


def sanitize_phone(phone: str) -> str:
    if not phone:
        return ""
    first_segment = re.split(r"[;,/]", phone)[0]
    digits = re.sub(r"\D", "", first_segment)

    if digits.startswith("90") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif len(digits) == 10 and not digits.startswith("0"):
        digits = "0" + digits

    if len(digits) not in (10, 11):
        return ""
    if _FAKE_TAIL_RE.match(digits[-7:]):
        return ""
    return digits


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

# Türkiye'nin 81 ili - OpenStreetMap admin_level=4 alan adlarıyla eşleşecek şekilde
PROVINCES = [
    "Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Amasya", "Ankara", "Antalya",
    "Artvin", "Aydın", "Balıkesir", "Bilecik", "Bingöl", "Bitlis", "Bolu", "Burdur",
    "Bursa", "Çanakkale", "Çankırı", "Çorum", "Denizli", "Diyarbakır", "Edirne",
    "Elazığ", "Erzincan", "Erzurum", "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane",
    "Hakkari", "Hatay", "Isparta", "Mersin", "İstanbul", "İzmir", "Kars", "Kastamonu",
    "Kayseri", "Kırklareli", "Kırşehir", "Kocaeli", "Konya", "Kütahya", "Malatya",
    "Manisa", "Kahramanmaraş", "Mardin", "Muğla", "Muş", "Nevşehir", "Niğde", "Ordu",
    "Rize", "Sakarya", "Samsun", "Siirt", "Sinop", "Sivas", "Tekirdağ", "Tokat",
    "Trabzon", "Tunceli", "Şanlıurfa", "Uşak", "Van", "Yozgat", "Zonguldak", "Aksaray",
    "Bayburt", "Karaman", "Kırıkkale", "Batman", "Şırnak", "Bartın", "Ardahan",
    "Iğdır", "Yalova", "Karabük", "Kilis", "Osmaniye", "Düzce",
]

# Lead adı/isim eşleşmesi için sektör anahtar kelimeleri.
# NOT: "toptan" (wholesale) kasıtlı olarak burada YOK - tek başına çok genel bir kelime,
# market/tekstil/gıda gibi tamamen alakasız toptancıları da yakalayıp yanlış pozitif üretiyordu
# (örn. "Bizim Toptan Market"). Diğer kelimelerin hepsi zaten araç/lojistik'e özgü.
# "otobüs" ve "iş makinesi/makinası" işletmenin kendi hedef araç kapsamında (ağır vasıta, tır,
# kamyon, iş makinesi ve otobüs - bkz. vision_agent prompt'u) olmasına rağmen aramaya hiç dahil
# edilmemişti, eklendi. "TIR" de aynı sebeple eklendi (kısa/büyük harf olduğu için kelime sınırı
# eşleşmesinde yanlış pozitif riski düşük - "tır" fiili farklı bağlamda genelde küçük harf kullanılır
# ama biz zaten isimleri lower() yapıp eşleştiriyoruz, bu riski kabul edilebilir kılıyor çünkü
# şirket isimlerinde "tır" geçen alakasız kelime son derece nadir).
NAME_KEYWORDS_HIGH_VALUE = [
    "nakliye", "lojistik", "dorse", "treyler", "taşımacılık", "transport",
    "yedek parça", "filo", "otobüs", "iş makinesi", "iş makinası", "tır",
]

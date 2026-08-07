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

# turkbusinesscenter.com'un il URL slug'ları - PROVINCES listesiyle TAM AYNI SIRADA (index'ler eşleşir).
# Sitenin kendi il seçim menüsünden alındı, uydurma değil.
PROVINCE_SLUGS_TBC = [
    "adana", "adiyaman", "afyon", "agri", "amasya", "ankara", "antalya",
    "artvin", "aydin", "balikesir", "bilecik", "bingol", "bitlis", "bolu", "burdur",
    "bursa", "canakkale", "cankiri", "corum", "denizli", "diyarbakir", "edirne",
    "elazig", "erzincan", "erzurum", "eskisehir", "gaziantep", "giresun", "gumushane",
    "hakkari", "hatay", "isparta", "mersin", "istanbul", "izmir", "kars", "kastamonu",
    "kayseri", "kirklareli", "kirsehir", "kocaeli", "konya", "kutahya", "malatya",
    "manisa", "kahramanmaras", "mardin", "mugla", "mus", "nevsehir", "nigde", "ordu",
    "rize", "sakarya", "samsun", "siirt", "sinop", "sivas", "tekirdag", "tokat",
    "trabzon", "tunceli", "sanliurfa", "usak", "van", "yozgat", "zonguldak", "aksaray",
    "bayburt", "karaman", "kirikkale", "batman", "sirnak", "bartin", "ardahan",
    "igdir", "yalova", "karabuk", "kilis", "osmaniye", "duzce",
]

# Lead adı/isim eşleşmesi için sektör anahtar kelimeleri.
# NOT: "toptan" (wholesale) kasıtlı olarak burada YOK - tek başına çok genel bir kelime,
# market/tekstil/gıda gibi tamamen alakasız toptancıları da yakalayıp yanlış pozitif üretiyordu
# (örn. "Bizim Toptan Market"). Diğer kelimelerin hepsi zaten araç/lojistik'e özgü.
# "otobüs" ve "iş makinesi/makinası" işletmenin kendi hedef araç kapsamında (ağır vasıta, tır,
# kamyon, iş makinesi ve otobüs - bkz. vision_agent prompt'u) olmasına rağmen aramaya hiç dahil
# edilmemişti, eklendi.
# NOT: "tır" DENENDİ ve KALDIRILDI - kelime sınırı koruması olmasına rağmen gerçek bir taramada
# yanlış pozitif ürettiği tespit edildi (örn. "Çamlık tır Kokoreç" bir kokoreç dükkanı, "Tırkaz"
# alakasız bir yer adı - Türkçe'de "-tır/-dır" eki son derece yaygın olduğu için OSM'deki isimlerde
# kelime olarak da karşımıza çıkabiliyor). "otobüs" ve "iş makinesi" gibi daha uzun/özgün kelimeler
# bu riski taşımıyor, bu yüzden tutuldu.
NAME_KEYWORDS_HIGH_VALUE = [
    "nakliye", "lojistik", "dorse", "treyler", "taşımacılık", "transport",
    "yedek parça", "filo", "otobüs", "iş makinesi", "iş makinası",
]

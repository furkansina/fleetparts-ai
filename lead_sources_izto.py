# -*- coding: utf-8 -*-
import html
import re
import time

import requests

# NOT: diğer kaynakların aksine burada projeye özel bir User-Agent KULLANILAMIYOR - gerçek bir
# testte tespit edildi: eoda.izto.org.tr'nin eski ASP.NET/DevExpress alt yapısı, tanımadığı bir
# User-Agent gördüğünde (muhtemelen ASP.NET'in dahili tarayıcı yetenek algılama sistemi üzerinden)
# sunucu tarafında hata fırlatıyor ve postback isteği "0|error|500||" ile başarısız oluyor. Gerçek
# bir masaüstü tarayıcı User-Agent'ı kullanıldığında sorun tamamen ortadan kalkıyor.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
}

# İzmir Ticaret Odası (İZTO) - RESMİ oda sicil kaydı (e-oda portalı, eoda.izto.org.tr) üzerinden
# çalışan "Üye Firma Sorgulama" aracı. Diğer tüm kaynaklardan (OSM, turkbusinesscenter.com, sanayi
# siteleri, find.com.tr) TAMAMEN FARKLI bir kaynak türü: harita/dizin taraması DEĞİL, bizzat
# TİCARET ODASI'nın kendi üye sicilinden gelen, "Meslek Grubu" (TOBB standart meslek grubu
# sınıflandırması) ile filtrelenebilen resmi bir kayıt.
#
# ARAŞTIRMA SÜRECİ (2026-08-12, gerçek denemelerle doğrulandı): TESK/il Esnaf Odaları Birlikleri
# (İSTESOB, BESOB, ANKESOB) kendi sitelerinde SADECE odanın/başkanın iletişim bilgilerini
# gösteriyor - üye firma arama/listeleme özelliği YOK (esnaf sicili sadece e-Devlet üzerinden,
# giriş gerektiriyor). Büyük Ticaret/Sanayi Odalarının üye sorgulama araçları da denendi:
# İSO (eoda.iso.org.tr) ve BTSO (btso.org.tr/.../kayitli-uyeler) resim CAPTCHA istiyor - ATLANDI.
# ATSO (atso.org.tr) hem CAPTCHA istiyor HEM DE sadece Firma Ünvanı/Vergi No/Mersis No gibi TAM
# eşleşme alanlarıyla arama yapıyor (kategori/sektör bazlı taranamaz) - ATLANDI. İZTO'nun KENDİ
# "Ticaret Sicil Rehberi" sayfası da (FirmaTescilSorgulama.aspx) sadece Ticaret Sicil No/Ünvan tam
# eşleşmesi istiyor - ATLANDI. Ama İZTO'nun AYRI bir aracı olan "Üye Firma Sorgulama"
# (uye_firmalar_yeni.aspx?id=286) CAPTCHA'sız, "Meslek Grubu" AÇILIR LİSTESİYLE arama yapıyor ve
# gerçek bir testte doğrulandı: "53-OTOMOTİV VE DİĞER ULAŞIM ARAÇLARI PARÇALARININ İMALATI VE
# TOPTAN SATIŞI GRUBU" TEK BAŞINA 737 gerçek firma döndürdü (hiç sayfalama/kesme yok - "Toplam Üye
# Firma Sayısı" ile parse edilen satır sayısı TAM eşleşti, 5 grup için de doğrulandı: 737+344+435+
# 1698+452 = 3666 ham kayıt).
#
# TEKNİK YAPI: Klasik ASP.NET WebForms + DevExpress ASPxGridView, AJAX UpdatePanel postback'i ile
# çalışıyor (JS render GEREKMİYOR - tam HTML normal bir POST isteğiyle dönüyor, requests ile
# taklit edilebiliyor). Her arama için ÖNCE bir GET ile taze __VIEWSTATE/__VIEWSTATEGENERATOR/
# __EVENTVALIDATION alınıyor (bunlar sayfa kontrollerine bağlı, isteğe özel değil - session'sız
# çalışıyor), SONRA aynı URL'ye "Meslek Grubu" seçili bir POST atılıyor. robots.txt: eoda.izto.
# org.tr'de robots.txt YOK (404 - tarama kısıtlaması tanımlanmamış), ana izto.org.tr'de de bu yolu
# engelleyen bir kural yok.
#
# ÖNEMLİ SINIRLAMA: bu kaynak da (find.com.tr gibi) TELEFON NUMARASI vermiyor - sadece Ünvan/İlçe/
# Tescilli Adres/varsa Web Adresi. Skorlama (score_lead) telefonsuz kayıtları zaten otomatik düşük
# data_completeness ile değerlendiriyor, ayrıca bir işlem gerekmiyor.
BASE_URL = "https://eoda.izto.org.tr/web/uye_firmalar_yeni.aspx?id=286"

# Hedef kitleyle DOĞRUDAN ilgili meslek grupları seçildi (İZTO'nun tam listesi 84 grup - alakasız
# olanlar, örn. "Turizm", "Gıda", "Mobilya" vb. kasıtlı dışarıda bırakıldı). "57-Motorlu Taşıt Satış
# Sonrası Hizmetler" (bağımsız tamirci/servis) ve "56-Ulaşım Araçları Satışı" (araç bayiliği, parça
# değil) BİLİNÇLİ OLARAK dışarıda bırakıldı - kullanıcı hedefinin "tamirci değil, parça toptancısı/
# perakendecisi ve parça stoklayan lojistik/filo firmaları" olduğu net (bkz. lead_scoring.py).
MESLEK_GRUPLARI = {
    "53": "53-OTOMOTİV VE DİĞER ULAŞIM ARAÇLARI PARÇALARININ İMALATI VE TOPTAN SATIŞI GRUBU",
    "84": "84-OTOMOTİV VE DİĞER ULAŞIM ARAÇLARI PARÇALARININ PERAKENDE SATIŞI GRUBU",
    "54": "54-LASTİK AKÜMÜLATÖR VE MOTORSİKLET GRUBU",
    "45": "45-YÜK TAŞIMA VE ANTREPO GRUBU",
    "50": "50-LOJİSTİK VE GÜMRÜK MÜŞAVİRLİĞİ GRUBU",
}

_ROW_RE = re.compile(
    r'<tr id="GrdUye_DXDataRow\d+" class="dxgvDataRow_Office2003Blue">\s*'
    r'<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*'
    r'<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*'
    r'<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>',
    re.DOTALL,
)


def _get_hidden(page_html: str, name: str) -> str:
    m = re.search(r'<input[^>]*name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', page_html)
    return m.group(1) if m else ""


def _build_form(kod: str, label: str, viewstate: str, viewstategen: str, eventvalidation: str) -> dict:
    """DevExpress'in ASPxGridView'i çok sayıda gizli alan bekliyor (çoğu diğer widget'ların
    varsayılan/boş durumu) - bunların hepsi gerçek bir tarayıcı isteğinden (Playwright ile
    doğrulandı) BİREBİR kopyalandı, sadece cbMeslekGrup* alanları değişiyor."""
    return {
        "ScriptManager1": "UpdatePanel1|btnsorgula",
        "__EVENTTARGET": "", "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate, "__VIEWSTATEGENERATOR": viewstategen, "__EVENTVALIDATION": eventvalidation,
        "txtOdaSicilNo_Raw": "0", "txtOdaSicilNo": "0", "txtOdaSicilNo$CVS": "",
        "txtIlceKodu_Raw": "00", "txtIlceKodu": "00", "txtIlceKodu$CVS": "",
        "txtTicSicNo_Raw": "0", "txtTicSicNo": "0", "txtTicSicNo$CVS": "",
        "xtunvan": "",
        "cbMeslekGrup_VI": kod, "cbMeslekGrup": label,
        "cbMeslekGrup_DDDWS": "0:0:12000:616:211:1:539:157:1:0:0:0",
        "cbMeslekGrup_DDD_LDeletedItems": "", "cbMeslekGrup_DDD_LInsertedItems": "",
        "cbMeslekGrup_DDD_LCustomCallback": "",
        "cbMeslekGrup$DDD$L": kod,
        "txtNace1_Raw": "00", "txtNace1": "00", "txtNace1$CVS": "",
        "txtNace2_Raw": "00", "txtNace2": "00", "txtNace2$CVS": "",
        "txtNace3_Raw": "00", "txtNace3": "00", "txtNace3$CVS": "",
        "cbIlce_VI": "0", "cbIlce": "Seçiniz",  # tüm ilçeler (filtre yok)
        "cbIlce_DDDWS": "0:0:-1:-10000:-10000:0:-10000:-10000:1:0:0:0",
        "cbIlce_DDD_LDeletedItems": "", "cbIlce_DDD_LInsertedItems": "", "cbIlce_DDD_LCustomCallback": "",
        "cbIlce$DDD$L": "0",
        "GrdUye$DXSelInput": "", "GrdUye$DXKVInput": "[]", "GrdUye$DXColResizedInput": "", "GrdUye$DXSyncInput": "",
        "MpDetayWS": "0:0:-1:-10000:-10000:0:770px:270px:1:0:0:0",
        "MpDetay_xtsicno_Raw": "0", "MpDetay$xtsicno": "0", "MpDetay$xtsicno$CVS": "",
        "MpDetay$funvan": "", "MpDetay$xtadres": "",
        "MpDetay_xtnsinif_Raw": "00", "MpDetay$xtnsinif": "00", "MpDetay$xtnsinif$CVS": "",
        "MpDetay_xtngrup_Raw": "00", "MpDetay$xtngrup": "00", "MpDetay$xtngrup$CVS": "",
        "MpDetay_xtnalt_Raw": "00", "MpDetay$xtnalt": "00", "MpDetay$xtnalt$CVS": "",
        "MpDetay$fnacead": "", "MpDetay$xtmgrup": "", "MpDetay$fmgrupad": "",
        "NaceKodyrdm_MpNaceKodYrdmWS": "0:0:-1:-10000:-10000:0:875px:550px:1:0:0:0",
        "NaceKodyrdm$MpNaceKodYrdm$GrdNaceKodYrdm$DXFREditorcol0": "",
        "NaceKodyrdm$MpNaceKodYrdm$GrdNaceKodYrdm$DXFREditorcol1": "",
        "NaceKodyrdm$MpNaceKodYrdm$GrdNaceKodYrdm$DXFREditorcol2": "",
        "NaceKodyrdm$MpNaceKodYrdm$GrdNaceKodYrdm$DXSelInput": "",
        "NaceKodyrdm$MpNaceKodYrdm$GrdNaceKodYrdm$DXKVInput": "[]",
        "NaceKodyrdm$MpNaceKodYrdm$GrdNaceKodYrdm$DXColResizedInput": "",
        "NaceKodyrdm$MpNaceKodYrdm$GrdNaceKodYrdm$DXSyncInput": "",
        "webalert_MpAlertWS": "0:0:-1:-10000:-10000:0:350px:190px:1:0:0:0",
        "__ASYNCPOST": "true", "btnsorgula": "",
    }


def _fetch_group(session: requests.Session, kod: str, label: str) -> str:
    """Her grup için ÖNCE taze bir GET ile VIEWSTATE alınır (aynı sayfa, session'sız çalışıyor),
    SONRA o grup seçiliyken AJAX postback POST'u atılır - dönen gövde tam sonuç tablosunu içerir."""
    try:
        get_res = session.get(BASE_URL, headers=HEADERS, timeout=30)
        if get_res.status_code != 200:
            return ""
        get_res.encoding = "utf-8"
        page_html = get_res.text
    except Exception as e:
        print(f"  [izto] {label}: GET hatası - {e}")
        return ""

    viewstate = _get_hidden(page_html, "__VIEWSTATE")
    viewstategen = _get_hidden(page_html, "__VIEWSTATEGENERATOR")
    eventvalidation = _get_hidden(page_html, "__EVENTVALIDATION")
    if not viewstate or not eventvalidation:
        print(f"  [izto] {label}: VIEWSTATE/EVENTVALIDATION bulunamadı, atlanıyor")
        return ""

    form = _build_form(kod, label, viewstate, viewstategen, eventvalidation)
    post_headers = dict(HEADERS)
    post_headers.update({
        "X-MicrosoftAjax": "Delta=true",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": BASE_URL,
    })
    try:
        post_res = session.post(BASE_URL, data=form, headers=post_headers, timeout=60)
        if post_res.status_code != 200:
            return ""
        post_res.encoding = "utf-8"
        return post_res.text
    except Exception as e:
        print(f"  [izto] {label}: POST hatası - {e}")
        return ""


def _parse_rows(resp_text: str, group_label: str) -> list:
    results = []
    for m in _ROW_RE.finditer(resp_text):
        cells = [html.unescape(c).strip() for c in m.groups()]
        oda_sicil_no, _ticari_sicil_no, _meslek_grubu, nace, unvan, ilce, adres, web = cells
        if not unvan or unvan == "&nbsp;" or not oda_sicil_no.isdigit():
            continue
        web = "" if web in ("", "&nbsp;") else web
        # NACE kodu satırı "46.72.12 - Motorlu kara taşıtlarının parçalarının toptan ticareti..."
        # şeklinde geliyor - koddan sonraki açıklama kısmı kategori etiketine ek bilgi olarak eklenir.
        nace_desc = nace.split(" - ", 1)[-1].strip() if " - " in nace else ""
        category_label = f"İzmir Ticaret Odası - {group_label.split('-', 1)[-1].strip()}"
        if nace_desc:
            category_label += f" ({nace_desc})"
        results.append({
            "site_id": f"izto_{oda_sicil_no}",
            "name": unvan,
            "shop_type": "directory",
            "category_label": category_label,
            "phone": "",
            "website": web,
            "address": f"{adres.strip()}" if adres and adres != "&nbsp;" else (f"{ilce.strip()} / İZMİR" if ilce else ""),
            "lat": None,
            "lon": None,
            "province": "İzmir",
        })
    return results


def search_all(delay: float = 0.6) -> list:
    """İzmir Ticaret Odası'nın resmi üye sicilinden, hedef kitleyle ilgili 5 meslek grubunu
    (otomotiv parça toptan/perakende, lastik-akü, yük taşıma, lojistik-gümrük) tarar. İl bağımsız
    değil (tek il: İzmir) ama sanayi_sitesi.py/find_com_tr.py gibi diğer 'tek seferlik' kaynaklarla
    aynı mantıkla (kendi search_all()'u) çalışır - run_discovery.py'de il döngüsüne girmez."""
    all_results = []
    session = requests.Session()
    for kod, label in MESLEK_GRUPLARI.items():
        resp_text = _fetch_group(session, kod, label)
        if not resp_text:
            print(f"  [izto] {label}: sonuç alınamadı")
            time.sleep(delay)
            continue
        items = _parse_rows(resp_text, label)
        print(f"  [izto] {label}: {len(items)} firma")
        all_results.extend(items)
        time.sleep(delay)
    return all_results

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sea & Stone Estates — Emlakjet portföy senkronizasyon robotu
================================================================
Mine Gürtuna'nın Emlakjet danışman profilini okur ve site için
data/listings.json dosyasını günceller. GitHub Actions üzerinden
haftada iki kez otomatik çalışır (.github/workflows/update-listings.yml).

GÜVENLİK İLKESİ: Bu script asla listings.json'ı BOŞ veya EKSİK veriyle
ezmez. Emlakjet sayfası okunamazsa veya hiç ilan bulunamazsa, script
hata koduyla durur ve mevcut dosyaya DOKUNMAZ — site bir önceki geçerli
haliyle yayında kalmaya devam eder.

Bu script "en iyi çaba" (best-effort) prensibiyle yazılmıştır: Emlakjet
sitesinin HTML yapısı değişirse ayarlanması gerekebilir. Böyle bir durumda
Claude'a "portföy robotu bozuldu, düzelt" demeniz yeterlidir.
"""
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import unquote

PROFILE_URL = "https://www.emlakjet.com/danismanlar/mine-gurtuna-2400237"
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "listings.json"
MAX_PAGES = 6
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Didim mahalle merkez koordinatları (yaklaşık). Bilinmeyen mahalle için
# genel Didim merkezine düşer; harita pini yine de anlamlı bir bölgeye oturur.
MAHALLE_CENTROIDS = {
    "hisar":       (37.3838, 27.2565),
    "efeler":      (37.3763, 27.2687),
    "camlik":      (37.3572, 27.2662),
    "cumhuriyet":  (37.3745, 27.2650),
    "yeni":        (37.3702, 27.2612),
    "altinkum":    (37.3600, 27.2510),
    "akbuk":       (37.3300, 27.3300),
    "mavisehir":   (37.3960, 27.2440),
    "mersindere":  (37.4010, 27.2380),
    "yalikoy":     (37.3450, 27.2900),
    "batikoy":     (37.3650, 27.2400),
    "denizkoy":    (37.3150, 27.2750),
    "fevzipasa":   (37.3790, 27.2600),
    "balat":       (37.2950, 27.3450),
    "didim":       (37.3720, 27.2610),  # genel merkez, eşleşme yoksa
}
# URL/slug içindeki anahtar kelime -> mahalle kodu eşlemesi (sırayla denenir)
MAHALLE_ALIASES = [
    ("hisar", "hisar"), ("efeler", "efeler"), ("camlik", "camlik"), ("çamlık", "camlik"),
    ("cumhuriyet", "cumhuriyet"), ("yeni-mah", "yeni"), ("yenimahalle", "yeni"),
    ("altinkum", "altinkum"), ("altınkum", "altinkum"), ("akbuk", "akbuk"), ("akbük", "akbuk"),
    ("mavisehir", "mavisehir"), ("mavişehir", "mavisehir"), ("mersindere", "mersindere"),
    ("yalikoy", "yalikoy"), ("yalıköy", "yalikoy"), ("batikoy", "batikoy"), ("batıköy", "batikoy"),
    ("denizkoy", "denizkoy"), ("denizköy", "denizkoy"), ("fevzipasa", "fevzipasa"),
    ("fevzipaşa", "fevzipasa"), ("balat", "balat"),
]

FLOOR_PATTERNS_EN = [
    (r"y[üu]ksek giri[sş]", "Elevated ground floor"),
    (r"bah[çc]e kat[ıi]", "Garden floor"),
    (r"kot\s*1", "Basement level 1"),
    (r"kot\s*2", "Basement level 2"),
    (r"[çc]at[ıi]\s*dubleks", "Top-floor duplex"),
    (r"zemin", "Ground floor"),
]
FLOOR_PATTERNS_DE = [
    (r"y[üu]ksek giri[sş]", "Hochparterre"),
    (r"bah[çc]e kat[ıi]", "Gartengeschoss"),
    (r"kot\s*1", "Untergeschoss 1"),
    (r"kot\s*2", "Untergeschoss 2"),
    (r"[çc]at[ıi]\s*dubleks", "Dachgeschoss-Maisonette"),
    (r"zemin", "Erdgeschoss"),
]


def slugify_ascii(text: str) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir (eşleştirme için)."""
    text = text.lower()
    text = (text.replace("ı", "i").replace("ğ", "g").replace("ü", "u")
                .replace("ş", "s").replace("ö", "o").replace("ç", "c"))
    return unicodedata.normalize("NFKD", text)


def fetch(url: str) -> str:
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urlopen(req, timeout=25) as resp:
        raw = resp.read()
        encoding = resp.headers.get("Content-Encoding", "")
        status = resp.status
        final_url = resp.geturl()

    if encoding == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    elif encoding == "br":
        try:
            import brotli
            raw = brotli.decompress(raw)
        except ImportError:
            print("[uyarı] içerik brotli ile sıkıştırılmış ama 'brotli' paketi kurulu değil", file=sys.stderr)
    elif encoding == "deflate":
        import zlib
        raw = zlib.decompress(raw)

    html = raw.decode("utf-8", errors="replace")
    # Teşhis: her zaman logla — sorun çıkarsa Actions loglarından tam olarak
    # sitenin ne döndürdüğünü görebilelim.
    print(f"[teşhis] GET {url}")
    print(f"[teşhis] durum kodu: {status} | son adres: {final_url} | encoding: {encoding or 'yok'}")
    print(f"[teşhis] ham boyut: {len(raw)} bayt | çözülmüş metin: {len(html)} karakter")
    print(f"[teşhis] içerik başlangıcı: {html[:300]!r}")
    if len(html) < 2000:
        print("[uyarı] sayfa içeriği çok kısa — bot engeli / CAPTCHA / boş kabuk olabilir", file=sys.stderr)
    return html


def guess_mahalle(text: str) -> str:
    t = slugify_ascii(text)
    for key, code in MAHALLE_ALIASES:
        if key in t:
            return code
    return "didim"


def guess_floor(text: str):
    t = slugify_ascii(text)
    m = re.search(r"(\d+)\s*\.\s*kat", t)
    if m:
        n = int(m.group(1))
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}. kat", f"{n}{suffix} floor", f"{n}. Etage"
    for pat, en in FLOOR_PATTERNS_EN:
        if re.search(pat, t):
            de = dict(FLOOR_PATTERNS_DE)[pat]
            tr_map = {
                r"y[üu]ksek giri[sş]": "Yüksek giriş", r"bah[çc]e kat[ıi]": "Bahçe katı",
                r"kot\s*1": "Kot 1", r"kot\s*2": "Kot 2",
                r"[çc]at[ıi]\s*dubleks": "Çatı dubleks", r"zemin": "Zemin kat",
            }
            return tr_map[pat], en, de
    return None, None, None


def clean_title(raw: str) -> str:
    # emoji ve fazla boşlukları temizle, baştaki/sondaki noktalama
    raw = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF✨🔥💥💫🚀🏡🔑💰📈🌅]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" !.-")
    return raw


def parse_cards(html: str):
    """Profil sayfasındaki ilan kartlarını (/ilan/ linki + görsel + fiyat) çıkarır."""
    results = {}
    all_links = list(re.finditer(r'/ilan/([a-z0-9\-şöçğüıİĞÜŞÖÇ]+)-(\d{6,})', html, re.IGNORECASE))
    print(f"[teşhis] '/ilan/' deseniyle eşleşen link sayısı: {len(all_links)}")
    skipped_no_image, skipped_no_price = 0, 0
    for idx, m in enumerate(all_links):
        listing_id = m.group(2)
        if listing_id in results:
            continue
        slug = m.group(1)
        url = f"https://www.emlakjet.com/ilan/{slug}-{listing_id}"

        # ÖNEMLİ: iki AYRI pencere kullanıyoruz, çünkü kart içindeki bilgiler
        # linkin hem önünde hem arkasında olabiliyor. Görsel açıklaması (alt
        # metni) linkten ÖNCE, fiyat/oda/kat bilgisi linkten SONRA geliyor.
        # Tek bir geniş pencere kullanmak, komşu kartların bilgilerinin
        # birbirine karışmasına yol açıyordu (gerçek veriyle test edilirken
        # bu hata tam olarak görüldü). Bu yüzden:
        #   back_window: sadece ÖNCEKİ ilanın bittiği yer ile bu linkin
        #                başlangıcı arası (görsel/başlık için)
        #   fwd_window : sadece bu linkin başlangıcı ile SONRAKİ ilanın
        #                başladığı yer arası (fiyat/oda/kat için)
        prev_end = all_links[idx - 1].end() if idx > 0 else 0
        next_start = all_links[idx + 1].start() if idx + 1 < len(all_links) else len(html)
        back_start = max(0, prev_end, m.start() - 800)
        fwd_end = min(next_start, m.start() + 4000)
        back_window = html[back_start:m.start()]
        fwd_window = html[m.start():fwd_end]
        # görsel araması için ayrı, daha geniş bir alan kullanıyoruz (Next.js
        # bazen görseli kart metninden bağımsız bir veri bloğunda tutuyor);
        # bu güvenli çünkü aşağıdaki desen listing_id'yi zaten şart koşuyor.
        img_window = html[back_start:m.start() + 4000]

        # görsel: aynı ilan id'sini taşıyan imaj.emlakjet.com bağlantısı.
        # Next.js bazen adresi %-kodlu (URL-encoded) yazıyor ve kart alanından
        # uzakta (ör. sayfa sonundaki veri bloğunda) olabiliyor — bu yüzden
        # önce pencerede, bulamazsa TÜM sayfada arıyoruz.
        img_pattern = (
            r'imaj\.emlakjet\.com(?:/|%2F)resize(?:/|%2F)\d+(?:/|%2F)\d+(?:/|%2F)listing'
            r'(?:/|%2F)' + listing_id + r'(?:/|%2F)[^\s"\'<>&]+?\.(?:jpg|jpeg|png|webp|avif)'
        )
        img_m = re.search(img_pattern, img_window) or re.search(img_pattern, html)
        if not img_m:
            skipped_no_image += 1
            continue  # görselsiz kartı güvenilir bulmuyoruz, atla
        img = unquote(img_m.group(0))
        if not img.startswith("http"):
            img = "https://" + img

        # başlık: önce <img alt="..."> (Emlakjet SEO için genelde doldurur,
        # linkten ÖNCE gelir), bulunamazsa linkin kendi metni ya da slug'dan
        # okunabilir bir başlık türet
        title_m = re.search(r'alt=["\']([^"\']{8,160})["\']', back_window)
        if title_m:
            title_raw = title_m.group(1)
        else:
            title_m2 = re.search(r'>([^<]{8,160})</a>', fwd_window[:200])
            title_raw = title_m2.group(1) if title_m2 else slug.replace("-", " ").title()
        title = clean_title(title_raw)

        # fiyat: "11.000.000 ₺" veya "28.500 ₺" — önce bu kartın kendi ileri
        # alanında ara; orada yoksa (bazı sayfa düzenlerinde fiyat linkten
        # önce de olabilir) güvenli geri alanda dene — back_window zaten
        # önceki karta taşmayacak şekilde sınırlı, o yüzden güvenli.
        price_m = re.search(r'([\d][\d\.]{2,})\s*₺', fwd_window) or re.search(r'([\d][\d\.]{2,})\s*₺', back_window)
        if not price_m:
            skipped_no_price += 1
            continue
        price = int(price_m.group(1).replace(".", ""))

        # oda + m² + kat — aynı mantık: önce ileri, yoksa güvenli geri alan
        room_m = re.search(r'\b([1-6])\s*\+\s*([0-2])\b', fwd_window) or re.search(r'\b([1-6])\s*\+\s*([0-2])\b', back_window)
        m2_m = re.search(r'(\d{2,4})\s*m²', fwd_window) or re.search(r'(\d{2,4})\s*m²', back_window)
        rooms = f"{room_m.group(1)}+{room_m.group(2)}" if room_m else None
        m2 = m2_m.group(1) if m2_m else None

        floor_tr, floor_en, floor_de = guess_floor(fwd_window + " " + back_window)
        is_rent = "kiralık" in slugify_ascii(title) or "kiralik" in slug or "/ay" in fwd_window[:400]
        mahalle = guess_mahalle(slug + " " + title)

        results[listing_id] = {
            "id": listing_id, "url": url, "img": img, "title_tr": title,
            "price": price, "cat": "rent" if is_rent else "sale",
            "rooms": rooms, "m2": m2, "floor_tr": floor_tr, "floor_en": floor_en, "floor_de": floor_de,
            "mahalle": mahalle,
        }
    print(f"[teşhis] görselsiz atlanan: {skipped_no_image} | fiyatsız atlanan: {skipped_no_price} | başarıyla ayrıştırılan: {len(results)}")
    return results


def build_listing(raw: dict) -> dict:
    lat, lng = MAHALLE_CENTROIDS.get(raw["mahalle"], MAHALLE_CENTROIDS["didim"])
    # id'ye göre deterministik küçük kayma: aynı ilan her seferinde aynı noktada kalsın
    h = int(raw["id"]) % 997
    jitter_lat = ((h % 41) - 20) / 20000.0
    jitter_lng = (((h // 41) % 41) - 20) / 20000.0

    rooms = raw["rooms"] or "?"
    m2 = raw["m2"] or "?"
    meta_tr = f"{rooms} · {m2} m²"
    meta_en = f"{rooms} bed · {m2} m²" if rooms != "?" else f"{m2} m²"
    meta_de = f"{rooms} SZ · {m2} m²" if rooms != "?" else f"{m2} m²"
    if raw["floor_tr"]:
        meta_tr += f" · {raw['floor_tr']}"
        meta_en += f" · {raw['floor_en']}"
        meta_de += f" · {raw['floor_de']}"

    mahalle_display = raw["mahalle"].capitalize()
    cat_word = {"sale": ("Satılık", "For Sale", "Zu verkaufen"), "rent": ("Kiralık", "For Rent", "Zu vermieten")}[raw["cat"]]
    title_en = f"{rooms} Apartment in {mahalle_display} — {cat_word[1]}" if rooms != "?" else f"Property in {mahalle_display} — {cat_word[1]}"
    title_de = f"{rooms}-Zimmer-Wohnung in {mahalle_display} — {cat_word[2]}" if rooms != "?" else f"Immobilie in {mahalle_display} — {cat_word[2]}"

    return {
        "id": raw["id"], "cat": raw["cat"], "mahalle": raw["mahalle"],
        "price": raw["price"], "currency": "₺", "period": "ay" if raw["cat"] == "rent" else None,
        "title": {"tr": raw["title_tr"], "en": title_en, "de": title_de},
        "meta": {"tr": meta_tr, "en": meta_en, "de": meta_de},
        "img": raw["img"], "url": raw["url"],
        "lat": round(lat + jitter_lat, 5), "lng": round(lng + jitter_lng, 5),
    }


def main():
    all_raw = {}
    for page in range(1, MAX_PAGES + 1):
        url = PROFILE_URL if page == 1 else f"{PROFILE_URL}?sayfa={page}"
        try:
            html = fetch(url)
        except (URLError, HTTPError) as e:
            print(f"[uyarı] sayfa {page} alınamadı: {e}", file=sys.stderr)
            break
        found = parse_cards(html)
        new_ids = [k for k in found if k not in all_raw]
        all_raw.update(found)
        print(f"sayfa {page}: {len(found)} kart bulundu, {len(new_ids)} yeni")
        if not new_ids and page > 1:
            break
        time.sleep(1)  # nazik ol

    if not all_raw:
        print("[HATA] Hiç ilan bulunamadı — Emlakjet sayfa yapısı değişmiş olabilir. "
              "listings.json DOKUNULMADAN bırakıldı.", file=sys.stderr)
        sys.exit(1)

    listings = [build_listing(r) for r in all_raw.values()]
    # en pahalı/öne çıkan üstte dursun diye fiyata göre değil, mevcut sırayı koru (Emlakjet "Önerilen" sırası)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {len(listings)} ilan data/listings.json dosyasına yazıldı.")


if __name__ == "__main__":
    main()

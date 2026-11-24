import sys
import os
import re
import json
import time
import hashlib
import tempfile
import difflib
import unicodedata
import requests
import subprocess
import shutil
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt6.QtWidgets import (QApplication, QMainWindow, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget, QMenu, QHeaderView, QToolBar,
                             QFileDialog, QMessageBox, QLineEdit, QAbstractItemView,
                             QInputDialog, QDialog, QLabel, QHBoxLayout, QPushButton,
                             QProgressDialog, QComboBox, QScrollArea, QCheckBox, QGroupBox,
                             QFormLayout, QFrame, QRadioButton, QButtonGroup, QSizePolicy,
                             QTextEdit, QSplitter)
from PyQt6.QtGui import QColor, QPixmap, QIcon, QShortcut, QKeySequence
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize

# =====================================================================
#  TMDB API ANAHTARINIZI BURAYA GİRİN
# =====================================================================
TMDB_API_KEY = "a7403e3d62a41bf85e1e53b9df4d684f"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w200"
# =====================================================================

# =====================================================================
#  EXTINF yardımcıları
# =====================================================================
def _extinf_name(line: str) -> str:
    """
    #EXTINF satırından içerik adını çıkarır.

    Ad, TIRNAK DIŞINDAKİ ilk virgülden sonra gelen kısımdır. Öznitelik
    değerleri (group-title, tvg-logo ...) virgül içerebilir; naif bir
    split(",", 1) bu durumda adı yanlış yerden böler.

        #EXTINF:-1 group-title="Love, Death & Robots",Love, Death & Robots S01E01
                                     ^ burada bölmemeli      ^ ad buradan başlar
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            return line[i + 1:].strip()
    return ""


def _norm_url(url: str) -> str:
    """
    URL'yi karşılaştırma için normalleştirir.

    Amaç: gerçekten AYNI adresi yakalamak, farklı kaynakları değil.
    - baştaki/sondaki boşluklar atılır
    - şema ve alan adı küçük harfe indirilir (yol kısmı büyük/küçük duyarlı kalır)
    - sondaki '/' yok sayılır
    Sorgu parametreleri KORUNUR; token/kalite farkı olan adresler ayrı sayılır.
    """
    u = (url or "").strip()
    if not u:
        return ""
    m = re.match(r'^([A-Za-z][A-Za-z0-9+.\-]*://)([^/?#]*)(.*)$', u, flags=re.DOTALL)
    if m:
        scheme, host, rest = m.group(1).lower(), m.group(2).lower(), m.group(3)
        u = scheme + host + rest
    while u.endswith("/"):
        u = u[:-1]
    return u


def _m3u_attr(value: str) -> str:
    """
    Öznitelik değerini M3U için güvenli hale getirir.

    Çift tırnak öznitelik sınırlayıcısıdır; değerin içinde kalırsa satır
    bozulur ve okuma sırasında ad/grup karışır. Virgül SORUN DEĞİLDİR
    (tırnak içinde kalır), bu yüzden dokunulmaz.
    """
    return str(value or "").replace('"', "'").replace("\n", " ").replace("\r", " ")


# =====================================================================
#  USER-AGENT / REFERRER yardımcıları
# =====================================================================
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36")


def find_vlc_path() -> str:
    env = (os.environ.get("VLC_PATH") or "").strip()
    if env and os.path.isfile(env):
        return env
    which = shutil.which("vlc") or shutil.which("vlc.exe")
    if which:
        return which
    candidates = []
    if sys.platform.startswith("win"):
        pf = os.environ.get("ProgramFiles", r"C:\\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(pf, "VideoLAN", "VLC", "vlc.exe"),
            os.path.join(pf86, "VideoLAN", "VLC", "vlc.exe"),
            os.path.join(local, "Programs", "VideoLAN", "VLC", "vlc.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/VLC.app/Contents/MacOS/VLC"]
    else:
        candidates = ["/usr/bin/vlc", "/usr/local/bin/vlc", "/snap/bin/vlc"]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def tmdb_tv_episodes(tmdb_id: str, language: str = "tr-TR"):
    out = []
    try:
        r = requests.get(
            f"{TMDB_BASE_URL}/tv/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": language}, timeout=12)
        for s in (r.json().get("seasons") or []):
            sn = int(s.get("season_number") or 0)
            if sn <= 0:
                continue
            rs = requests.get(
                f"{TMDB_BASE_URL}/tv/{tmdb_id}/season/{sn}",
                params={"api_key": TMDB_API_KEY, "language": language}, timeout=12)
            for ep in (rs.json().get("episodes") or []):
                en = int(ep.get("episode_number") or 0)
                if en <= 0:
                    continue
                out.append((sn, en, (ep.get("name") or "").strip(),
                            (ep.get("air_date") or "")[:4]))
    except Exception:
        pass
    return out


def tmdb_details(tmdb_id: str, media_type: str = "tv", language: str = "tr-TR"):
    try:
        r = requests.get(
            f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": language,
                    "append_to_response": "credits"}, timeout=12)
        data = r.json()
        overview = (data.get("overview") or "").strip()
        if not overview and language != "en-US":
            r2 = requests.get(
                f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}",
                params={"api_key": TMDB_API_KEY, "language": "en-US",
                        "append_to_response": "credits"}, timeout=12)
            data2 = r2.json()
            overview = (data2.get("overview") or "").strip()
            if not data.get("credits"):
                data["credits"] = data2.get("credits") or {}
        cast = []
        for c in (data.get("credits") or {}).get("cast") or []:
            nm = (c.get("name") or "").strip()
            if nm:
                cast.append(nm)
            if len(cast) >= 8:
                break
        title = data.get("name") or data.get("title") or ""
        return {"title": title, "overview": overview, "cast": cast,
                "media_type": media_type, "tmdb_id": str(tmdb_id)}
    except Exception:
        return None


# (görünen ad, User-Agent) — panelde "Hazır" açılır listesinde çıkar
UA_PRESETS = [
    ("Chrome (Windows)",  DEFAULT_UA),
    ("Firefox (Windows)", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
                          "Gecko/20100101 Firefox/128.0"),
    ("VLC",               "VLC/3.0.20 LibVLC/3.0.20"),
    ("okhttp (Android)",  "okhttp/4.12.0"),
]

# Satıra özel UA/Referrer, URL hücresinin item-data'sında saklanır
# (tabloda ayrı sütun açmadan; taşıma/sıralama/geri alma ile birlikte gider).
ROLE_MARK = Qt.ItemDataRole.UserRole          # TMDB işareti (tvg-id hücresinde)
ROLE_UA   = Qt.ItemDataRole.UserRole + 1      # (eski; artık COL_UA kullanılıyor)
ROLE_REF  = Qt.ItemDataRole.UserRole + 2      # (eski; artık COL_REF kullanılıyor)
ROLE_DOUBT = Qt.ItemDataRole.UserRole + 3     # Şüpheli TMDB aday dict (STATUS hücresinde)


def _hdr(value) -> str:
    """Header / M3U satırı için tek satıra indirger (satır sonu enjeksiyonunu önler)."""
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _latin1(value: str) -> str:
    """requests header değerlerini latin-1 ister; dışındaki karakterleri atar."""
    return (value or "").encode("latin-1", "ignore").decode("latin-1")


# Sütun indeksleri (kolay referans)
COL_CHECK  = 0   # ✔ checkbox sütunu
COL_LOGO   = 1
COL_GROUP  = 2
COL_NAME   = 3
COL_YEAR   = 4
COL_TMDBID = 5
COL_URL    = 6
COL_UA     = 7   # User-Agent (göster/gizle)
COL_REF    = 8   # Referrer   (göster/gizle)
COL_STATUS = 9

# TMDB tür ID → Türkçe karşılıkları
GENRE_MAP = {
    28: "Aksiyon", 12: "Macera", 16: "Animasyon", 35: "Komedi",
    80: "Suç", 99: "Belgesel", 18: "Drama", 10751: "Aile",
    14: "Fantezi", 36: "Tarih", 27: "Korku", 10402: "Müzik",
    9648: "Gizem", 10749: "Romantik", 878: "Bilim Kurgu",
    10770: "TV Film", 53: "Gerilim", 10752: "Savaş", 37: "Western",
    # TV türleri
    10759: "Aksiyon & Macera", 10762: "Çocuk", 10763: "Haberler",
    10764: "Reality", 10765: "Sci-Fi & Fantezi", 10766: "Pembe Dizi",
    10767: "Talk Show", 10768: "Savaş & Politika",
}


def clean_name(name: str) -> str:
    """
    Kanal/içerik adından teknik etiketleri ve bölüm bilgilerini temizler.
    Japonca/Çince/Kiril gibi Latin dışı karakterler korunur (TMDB araması için).
    """
    # S06-E10 / S01E01 / S01 E01 / S01E100 gibi bir kalıp varsa, ondan ÖNCE gelen kısmı al
    m = re.search(r'\bS\d{1,3}[\s\-]*E\d{1,3}\b', name, flags=re.IGNORECASE)
    if m:
        name = name[:m.start()]

    # Sezon/bölüm kalıpları: 1x05 / 1x100 vb.
    name = re.sub(r'\b\d{1,3}x\d{1,3}\b', '', name, flags=re.IGNORECASE)
    # Türkçe bölüm formatları: "1.Bölüm", "2. Sezon", "Sezon 1", "Bölüm 3"
    name = re.sub(r'\b\d+\.\s*(Bölüm|Sezon)\b', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\b(Bölüm|Sezon|Episode|Season)\s*\d+\b', '', name, flags=re.IGNORECASE)
    # Teknik etiketler
    name = re.sub(
        r'\b(HD|FHD|UHD|4K|SD|TR|EN|DE|HEVC|H264|H265|AAC|\d{3,4}p|x265|x264|BluRay|WEBRip|HDTV)\b',
        '', name, flags=re.IGNORECASE
    )
    # Nokta/alt çizgi dönüşümü — sadece Latin alfabesiyse yap (Japonca bozmamak için)
    latin_ratio = sum(1 for c in name if c.isascii() and c.isalpha()) / max(len([c for c in name if c.isalpha()]), 1)
    if latin_ratio > 0.7:
        name = re.sub(r'[._]', ' ', name)
    # Parantez içi yıl: (2023) veya [2023]
    name = re.sub(r'[\[\(]\d{4}[\]\)]', '', name)
    # Sonda kalan yılı sil
    name = re.sub(r'\b(19|20)\d{2}\b', '', name)
    # Birden fazla boşluğu tek boşluğa indir
    name = re.sub(r'\s{2,}', ' ', name)
    return name.strip(" .-_|")


# =====================================================================
#  SEZON/BÖLÜM BİÇİMİ NORMALLEŞTİRME
#  "Meghan 1. Sezon 1. Bölüm (2022)"  ->  "Meghan S01E01 (2022)"
#  Yalnızca sezon/bölüm ifadesi değişir; dizi adı ve yıl korunur.
# =====================================================================

# Desteklenen kalıplar (öncelik sırasıyla denenir).
# Her kalıp (sezon, bölüm) gruplarını yakalar.
_SE_PATTERNS = [
    # 1. Sezon 2. Bölüm  |  1.Sezon 2.Bolum  |  1 Sezon 2 Bölüm
    (re.compile(r'(?<!\d)(\d{1,3})\s*\.?\s*sezon\s*[-–—,:]?\s*(\d{1,3})\s*\.?\s*b[oö]l[uü]m(?!\w)',
                re.IGNORECASE), 'sezon_bolum'),
    # Sezon 1 Bölüm 2
    (re.compile(r'sezon\s*(\d{1,3})\s*[-–—,:]?\s*b[oö]l[uü]m\s*(\d{1,3})(?!\w)',
                re.IGNORECASE), 'sezon_bolum'),
    # Season 1 Episode 2
    (re.compile(r'season\s*(\d{1,3})\s*[-–—,:]?\s*episode\s*(\d{1,3})(?!\w)',
                re.IGNORECASE), 'sezon_bolum'),
    # 1. Season 2. Episode
    (re.compile(r'(?<!\d)(\d{1,3})\s*\.?\s*season\s*[-–—,:]?\s*(\d{1,3})\s*\.?\s*episode(?!\w)',
                re.IGNORECASE), 'sezon_bolum'),
    # 1x02  (yaygın kısa biçim)
    (re.compile(r'(?<![\w])(\d{1,3})\s*x\s*(\d{1,3})(?![\w])',
                re.IGNORECASE), 'sezon_bolum'),
]

# Yalnızca bölüm bilgisi olanlar (sezon yok) -> S01 varsayılır
_EP_ONLY_PATTERNS = [
    re.compile(r'(?<!\d)(\d{1,3})\s*\.?\s*b[oö]l[uü]m(?!\w)', re.IGNORECASE),
    re.compile(r'b[oö]l[uü]m\s*(\d{1,3})(?!\w)',              re.IGNORECASE),
    re.compile(r'episode\s*(\d{1,3})(?!\w)',                  re.IGNORECASE),
    re.compile(r'(?<!\d)(\d{1,3})\s*\.?\s*episode(?!\w)',     re.IGNORECASE),
]

# Zaten doğru biçimde mi?  (S01E01 / S1E1 / s01 e01)
_ALREADY_SE = re.compile(r'\bS\d{1,3}\s*[-_ ]?\s*E\d{1,3}\b', re.IGNORECASE)


def normalize_season_episode(name: str, default_season: int = 1):
    """
    Sezon/bölüm ifadesini S01E01 biçimine çevirir.

    Döner: (yeni_ad, degisti_mi)

    - "Meghan 1. Sezon 1. Bölüm (2022)" -> "Meghan S01E01 (2022)"
    - "Dizi Sezon 2 Bölüm 10"           -> "Dizi S02E10"
    - "Show 1x05"                       -> "Show S01E05"
    - "Dizi 7. Bölüm"                   -> "Dizi S01E07"   (sezon yoksa varsayılan)
    - Zaten "S01E01" ise dokunmaz.
    """
    if not name:
        return name, False
    original = name

    # Zaten doğru biçimdeyse karışma
    if _ALREADY_SE.search(name):
        return name, False

    season = episode = None
    for rx, _kind in _SE_PATTERNS:
        m = rx.search(name)
        if m:
            try:
                season  = int(m.group(1))
                episode = int(m.group(2))
            except (ValueError, IndexError):
                continue
            name = name[:m.start()] + '\x00' + name[m.end():]
            break

    if season is None:
        # Sezon yok, sadece bölüm var mı?
        for rx in _EP_ONLY_PATTERNS:
            m = rx.search(name)
            if m:
                try:
                    episode = int(m.group(1))
                except (ValueError, IndexError):
                    continue
                season = default_season
                name = name[:m.start()] + '\x00' + name[m.end():]
                break

    if season is None or episode is None:
        return original, False

    # Mantıksız değerleri reddet (yıl vb. yanlış yakalanmasın)
    if not (0 <= season <= 99) or not (0 <= episode <= 999):
        return original, False

    tag = 'S%02dE%02d' % (season, episode)
    name = name.replace('\x00', tag, 1)

    # Etiket çevresindeki artık ayraçları temizle
    name = re.sub(r'\s*[-–—|:]\s*(?=' + tag + r')', ' ', name)
    name = re.sub(r'(?<=' + tag + r')\s*[-–—|:]\s*', ' ', name)
    name = re.sub(r'\s{2,}', ' ', name).strip(' .-_|')

    return name, (name != original)


def is_latin(text: str) -> bool:
    """Metnin büyük çoğunluğu Latin alfabesiyse True döner."""
    if not text:
        return False
    latin_count = sum(1 for c in text if c.isascii() or '\u00C0' <= c <= '\u024F')
    return latin_count / len(text) > 0.6


def _detect_search_langs(q: str) -> list:
    """
    Sorgunun karakterlerine göre hangi TMDB arama dillerini deneyeceğimizi belirler.
    Her zaman en-US dahil edilir (TMDB'de en geniş kapsam).
    """
    langs = []
    for ch in q:
        cp = ord(ch)
        # Çince (CJK Unified)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            if "zh-CN" not in langs: langs.append("zh-CN")
            if "zh-TW" not in langs: langs.append("zh-TW")
        # Japonca (Hiragana / Katakana)
        elif 0x3040 <= cp <= 0x30FF:
            if "ja-JP" not in langs: langs.append("ja-JP")
        # Korece
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            if "ko-KR" not in langs: langs.append("ko-KR")
        # Kiril
        elif 0x0400 <= cp <= 0x04FF:
            if "ru-RU" not in langs: langs.append("ru-RU")
        # Arapça / Farsça
        elif 0x0600 <= cp <= 0x06FF:
            if "ar-AE" not in langs: langs.append("ar-AE")
    # en-US her zaman son sıraya ekle (fallback)
    if "en-US" not in langs:
        langs.append("en-US")
    return langs


def _tmdb_get_tr_name(tmdb_id: str, media_type: str, en_fallback: str) -> str:
    """
    Verilen TMDB ID için önce Türkçe adı çeker.
    Türkçe ad Latin değilse İngilizce ada döner.
    """
    try:
        r = requests.get(
            f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": "tr-TR"},
            timeout=6
        )
        data = r.json()
        tr = data.get("title") or data.get("name") or ""
        if tr and is_latin(tr):
            return tr
    except Exception:
        pass
    return en_fallback


# ─────────────────────────────────────────────────────────────────────
#  TMDB EŞLEŞTİRME — "emin değilsen değiştirme"
#
#  Eski davranış: arama sonucundaki İLK kayıt, benzeyip benzemediğine
#  bakılmadan alınıyordu (ve yıl yoksa dizi önce aranıyordu). Bu yüzden
#  alakasız film/dizi yazılabiliyordu.
#
#  Yeni davranış: her aday puanlanır; yalnızca güvenilir eşleşme uygulanır.
#    • başlık benzerliği  ≥ %92  (ön ek "The/A/Der…" ve noktalama yok sayılır)
#    • adda yıl varsa TMDB yılı aynı veya ±1 olmalı
#    • aynı adlı birden çok yapım varsa biri belirgin şekilde popüler olmalı
#  Güvenilir değilse veri "confident: False" ile döner, UYGULANMAZ ve durum
#  sütununa "ŞÜPHELİ ⚠️ TMDB" yazılır. Başlık neredeyse hiç benzemiyorsa
#  (%55 altı) sonuç yok sayılır.
# ─────────────────────────────────────────────────────────────────────
TMDB_CONF_SIM  = 0.92   # bu benzerlik ve üstü = başlık eşleşti
TMDB_MIN_SIM   = 0.55   # bunun altı = alakasız, aday bile sayılmaz
TMDB_POP_RATIO = 3.0    # aynı adlı rakipler arasında seçilmek için gereken popülerlik oranı

_YEAR_RE = re.compile(r'[\(\[]((?:19|20)\d{2})[\)\]]')
_EP_MARK_RE = re.compile(
    r'\bS\d{1,3}[\s\-]*E\d{1,3}\b|\b\d{1,3}x\d{1,3}\b'
    r'|\b\d+\.\s*(?:Bölüm|Sezon)\b|\b(?:Bölüm|Sezon|Episode|Season)\s*\d+\b',
    re.IGNORECASE)
_ARTICLES = ("the ", "a ", "an ", "der ", "die ", "das ", "le ", "la ", "les ",
             "el ", "los ", "il ")


def _extract_year(name: str):
    m = _YEAR_RE.search(name or "")
    return m.group(1) if m else None


def _has_episode_marker(name: str) -> bool:
    return bool(_EP_MARK_RE.search(name or ""))


def _norm_title(t: str) -> str:
    """Karşılaştırma için başlığı sadeleştirir (aksan, noktalama, büyük/küçük harf,
    baştaki 'The/A/Der…' yok sayılır; CJK/Kiril korunur)."""
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().replace("ı", "i").replace("&", " and ")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"[_\s]+", " ", t).strip()
    for a in _ARTICLES:
        if t.startswith(a) and len(t) > len(a):
            t = t[len(a):]
            break
    return t


def _title_sim(a: str, b: str) -> float:
    a, b = _norm_title(a), _norm_title(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    # Biri diğerinin alt kümesiyse ("Dune" / "Dune Part Two") kısmi puan ver,
    # ama asla "tam eşleşme" eşiğine çıkarma.
    if ta and tb and (ta <= tb or tb <= ta):
        short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        r = max(r, 0.55 + 0.35 * (len(short) / len(long_)) * 0.99)
    return min(r, 1.0)


def _tmdb_search(query, media_type, lang, year=None):
    params = {"api_key": TMDB_API_KEY, "query": query, "language": lang}
    if year:
        params["primary_release_year" if media_type == "movie"
               else "first_air_date_year"] = year
    try:
        r = requests.get(f"{TMDB_BASE_URL}/search/{media_type}", params=params, timeout=6)
        return r.json().get("results", []) or []
    except Exception:
        return []


def _year_of(item):
    d = item.get("release_date") or item.get("first_air_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None


def _score_candidate(item, media_type, q, search_year):
    titles = [item.get(k) for k in ("title", "name", "original_title", "original_name")]
    sim = max([_title_sim(q, t) for t in titles if t] or [0.0])
    cy = _year_of(item)
    if not search_year:
        ystate = "none"
    elif cy is None:
        ystate = "unknown"
    else:
        d = abs(cy - int(search_year))
        ystate = "exact" if d == 0 else ("near" if d == 1 else "far")
    bonus = {"none": 0.0, "exact": 0.10, "near": 0.04, "unknown": 0.0, "far": -0.15}[ystate]
    pop = float(item.get("popularity") or 0)
    return {"item": item, "media_type": media_type, "sim": sim, "ystate": ystate,
            "year": cy, "pop": pop, "score": sim + bonus + min(pop, 100.0) / 5000.0}


def tmdb_fetch(name: str):
    """
    TMDB'de arama yapar. Dönüş:
      None  → bulunamadı (veya aday başlık alakasız)
      dict  → aday; dict["confident"] True ise güvenle uygulanabilir,
              False ise "reason" alanında nedeni yazar (uygulanmamalı).

    Akış:
      1. Addan yıl ve bölüm işaretini çıkar (S01E01 varsa yalnız DİZİ aranır).
      2. Latin ad: tr-TR ile; Latin olmayan: özgün dil(ler)iyle ara.
         Yıl varsa yıl filtresiyle, sonuç yoksa yılsız dene.
      3. movie + tv adaylarını puanla, en iyisini seç, güveni hesapla.
      4. Seçilen aday için Türkçe/İngilizce ad ayrıntısını çek.
    """
    search_year = _extract_year(name)
    q = clean_name(name)
    if not q:
        return None

    has_ep = _has_episode_marker(name)
    q_is_latin = is_latin(q)
    media_order = ["tv"] if has_ep else ["movie", "tv"]
    langs = ["tr-TR"] if q_is_latin else _detect_search_langs(q)

    cands, seen = [], set()
    for media_type in media_order:
        for lang in langs:
            res = _tmdb_search(q, media_type, lang, search_year)
            if not res and search_year:
                res = _tmdb_search(q, media_type, lang, None)
            if res:
                for it in res[:5]:
                    key = (media_type, it.get("id"))
                    if key in seen:
                        continue
                    seen.add(key)
                    cands.append(_score_candidate(it, media_type, q, search_year))
                if not q_is_latin:
                    break          # Latin dışı: ilk başarılı dilde dur

    if not cands:
        return None
    cands.sort(key=lambda c: c["score"], reverse=True)
    best = cands[0]
    if best["sim"] < TMDB_MIN_SIM:
        return None

    # ── Güven değerlendirmesi ────────────────────────────────────────
    reasons = []
    if best["sim"] < TMDB_CONF_SIM:
        reasons.append(f"başlık tam eşleşmiyor (%{round(best['sim'] * 100)})")
    if best["ystate"] == "far":
        reasons.append(f"yıl uyuşmuyor (listede {search_year}, TMDB'de {best['year']})")
    elif best["ystate"] == "unknown":
        reasons.append("TMDB'de yıl bilgisi yok")
    if not reasons:
        rivals = [c for c in cands[1:]
                  if c["sim"] >= TMDB_CONF_SIM and c["ystate"] in ("none", "exact", "near")]
        if rivals:
            top_rival = max(c["pop"] for c in rivals)
            if best["pop"] < TMDB_POP_RATIO * max(top_rival, 0.0001):
                reasons.append("aynı adlı birden çok yapım var")

    # ── Ayrıntıları oluştur ──────────────────────────────────────────
    item, media_type = best["item"], best["media_type"]
    tmdb_id     = str(item.get("id", ""))
    poster_path = item.get("poster_path", "") or ""
    date_str    = item.get("first_air_date") or item.get("release_date") or ""
    genre_ids   = item.get("genre_ids", [])
    title_found = item.get("name") or item.get("title") or ""

    en_name = title_found
    if not is_latin(title_found):
        try:
            en_data = requests.get(
                f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}",
                params={"api_key": TMDB_API_KEY, "language": "en-US"}, timeout=6
            ).json()
            en_name = en_data.get("title") or en_data.get("name") or title_found
        except Exception:
            pass
    tr_name = en_name if q_is_latin else _tmdb_get_tr_name(tmdb_id, media_type, en_fallback=en_name)

    return {
        "logo":       (TMDB_IMAGE_BASE + poster_path) if poster_path else "",
        "group":      GENRE_MAP.get(genre_ids[0], "Diğer") if genre_ids else "Diğer",
        "tr_name":    tr_name,
        "year":       date_str[:4] if date_str else (search_year or ""),
        "tmdb_id":    tmdb_id,
        "media_type": media_type,
        "confident":  not reasons,
        "reason":     "; ".join(reasons),
        "match_sim":  best["sim"],
    }


# =====================================================================
#  TABLO POSTER ÖNİZLEMESİ
#  Logo sütunundaki URL'yi küçük bir görsele çevirir. Yalnızca EKRANDA
#  GÖRÜNEN satırlar indirilir; sonuçlar diskte önbelleğe alınır.
# =====================================================================
THUMB_W, THUMB_H = 40, 60
THUMB_DIR = os.path.join(tempfile.gettempdir(), "m3u_editor_thumbs")


def _thumb_file(url: str) -> str:
    """URL için sabit önbellek dosya yolu."""
    h = hashlib.md5(url.encode("utf-8", "ignore")).hexdigest()
    return os.path.join(THUMB_DIR, h + ".png")


class _ThumbLoader(QThread):
    """Verilen URL listesini paralel indirir, her biri hazır olunca sinyal yollar."""
    ready = pyqtSignal(str, QPixmap)   # (url, pixmap)

    def __init__(self, urls, parent=None):
        super().__init__(parent)
        self._urls = list(urls)
        self._stop = False

    def stop(self):
        self._stop = True

    def _one(self, url):
        if self._stop:
            return None
        path = _thumb_file(url)
        # 1) Diskte var mı?
        if os.path.exists(path):
            px = QPixmap(path)
            if not px.isNull():
                return px
        # 2) İndir
        try:
            raw = requests.get(url, timeout=6).content
            px = QPixmap()
            px.loadFromData(raw)
            if px.isNull():
                return None
            px = px.scaled(THUMB_W, THUMB_H,
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            try:
                os.makedirs(THUMB_DIR, exist_ok=True)
                px.save(path, "PNG")
            except Exception:
                pass
            return px
        except Exception:
            return None

    def run(self):
        if not self._urls:
            return
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(self._one, u): u for u in self._urls}
            for fut in as_completed(futs):
                if self._stop:
                    break
                url = futs[fut]
                try:
                    px = fut.result()
                except Exception:
                    px = None
                if px is not None and not px.isNull():
                    self.ready.emit(url, px)


class _PosterWorker(QThread):
    """Poster görselini arka planda indirir."""
    done = pyqtSignal(QPixmap)

    def __init__(self, url: str):
        super().__init__()
        self._url = url

    def run(self):
        try:
            raw = requests.get(self._url, timeout=5).content
            px  = QPixmap()
            px.loadFromData(raw)
            if not px.isNull():
                self.done.emit(px)
            else:
                self.done.emit(QPixmap())
        except Exception:
            self.done.emit(QPixmap())


# ──────────────────────────────────────────────────────────────────────
#  ARKA PLAN İŞÇİSİ
# ──────────────────────────────────────────────────────────────────────
class TMDBWorker(QThread):
    result_ready = pyqtSignal(int, dict)   # row, data_dict
    not_found    = pyqtSignal(int)         # row — TMDB'de bulunamadı
    uncertain    = pyqtSignal(int, dict)   # row, aday — emin değil, UYGULANMAZ
    progress     = pyqtSignal(int, str)    # done_count, current_name
    finished     = pyqtSignal(int, int, int)   # success, fail, doubt

    # TMDB API rate-limit güvenli paralel thread sayısı
    MAX_WORKERS = 10

    def __init__(self, rows_data, options):
        """
        rows_data : [(row_index, kanal_adı), ...]
        options   : dict — hangi alanlar güncellensin
        """
        super().__init__()
        self.rows_data = rows_data
        self.options   = options
        self._stop     = False
        self._executor = None

    def stop(self):
        self._stop = True
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def run(self):
        success = fail = doubt = 0
        done_lock = __import__('threading').Lock()
        done_count = [0]  # mutable counter for threads

        def fetch_one(row_index, channel_name):
            if self._stop:
                return
            data = tmdb_fetch(channel_name)
            with done_lock:
                done_count[0] += 1
                current_done = done_count[0]
            self.progress.emit(current_done, channel_name)
            return row_index, channel_name, data

        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            self._executor = executor
            futures = {
                executor.submit(fetch_one, row_index, channel_name): (row_index, channel_name)
                for row_index, channel_name in self.rows_data
            }
            for future in as_completed(futures):
                if self._stop:
                    break
                try:
                    result = future.result()
                    if result is None:
                        continue
                    row_index, channel_name, data = result
                    if data and data.get("confident", True):
                        self.result_ready.emit(row_index, data)
                        success += 1
                    elif data:
                        self.uncertain.emit(row_index, data)
                        doubt += 1
                    else:
                        self.not_found.emit(row_index)
                        fail += 1
                except Exception:
                    fail += 1

        self.finished.emit(success, fail, doubt)


# ──────────────────────────────────────────────────────────────────────
#  MANUEL TMDB ARAMA PENCERESİ  (tüm bilgileri seçebilir)
# ──────────────────────────────────────────────────────────────────────
class TMDBSearchDialog(QDialog):
    def __init__(self, parent=None, channel_name="", bulk_count=0):
        """
        bulk_count > 1 ise toplu mod: başlıkta kaç satıra uygulanacağı gösterilir
        ve bilgi bandı eklenir.
        """
        super().__init__(parent)
        self._bulk_count = bulk_count
        title = (f"TMDB'de Ara — {bulk_count} Satıra Uygula" if bulk_count > 1
                 else "TMDB'de Ara & Uygula")
        self.setWindowTitle(title)
        self.setMinimumSize(750, 520 if bulk_count > 1 else 480)
        self.is_dark = getattr(parent, "is_dark_mode", True)
        self.selected_data = {}

        layout = QVBoxLayout(self)

        # --- Toplu mod bilgi bandı ---
        if bulk_count > 1:
            info_bar = QLabel(
                f"ℹ️  Toplu mod aktif — seçtiğiniz TMDB sonucu <b>{bulk_count} satıra</b> uygulanacak.<br>"
                "Her satırın bölüm bilgisi (S01 E01 vb.) ayrı ayrı korunur; sadece dizi/film adı değişir."
            )
            info_bar.setTextFormat(Qt.TextFormat.RichText)
            info_bar.setWordWrap(True)
            info_bar.setStyleSheet(
                "background:#1a4a6e;color:#aed6f1;border-radius:5px;padding:8px;font-size:11px"
            )
            layout.addWidget(info_bar)

        # --- Arama satırı ---
        row1 = QHBoxLayout()
        self.search_input = QLineEdit(channel_name)
        self.type_combo   = QComboBox()
        self.type_combo.addItems(["TV Dizisi", "Film"])
        # Ada bakarak türü tahmin et: bölüm işareti yoksa ve yıl varsa → Film
        if not _has_episode_marker(channel_name) and _extract_year(channel_name):
            self.type_combo.setCurrentIndex(1)
        self.search_btn = QPushButton("🔍 Ara")
        self.search_btn.clicked.connect(self.do_search)
        self.search_input.returnPressed.connect(self.do_search)
        row1.addWidget(QLabel("Arama:"))
        row1.addWidget(self.search_input, 1)
        row1.addWidget(self.type_combo)
        row1.addWidget(self.search_btn)
        layout.addLayout(row1)

        # --- Sonuç kartları (kaydırmalı) ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(240)
        self.cards_widget = QWidget()
        self.cards_layout = QHBoxLayout(self.cards_widget)
        self.cards_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(self.cards_widget)
        layout.addWidget(scroll)

        # --- Seçilen bilgiler ---
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine); layout.addWidget(sep)
        self.info_label = QLabel("Henüz seçim yapılmadı.")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        # --- Butonlar ---
        btn_row = QHBoxLayout()
        ok_label   = f"✅ {bulk_count} Satıra Uygula" if bulk_count > 1 else "✅ Uygula"
        ok_btn     = QPushButton(ok_label)
        cancel_btn = QPushButton("İptal")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._apply_theme()

    def _apply_theme(self):
        if self.is_dark:
            self.setStyleSheet("""
                QDialog,QWidget{background:#2b2b2b;color:white}
                QLineEdit,QComboBox{background:#3c3f41;color:white;border:1px solid #555;padding:5px}
                QPushButton{background:#444;color:white;border:1px solid #666;padding:6px 14px}
                QPushButton:hover{background:#555}
                QLabel{color:white}
                QScrollArea{border:none}
            """)

    def _search_one(self, media_type, query, year):
        params = {"api_key": TMDB_API_KEY, "query": query, "language": "tr-TR"}
        if year:
            params["primary_release_year" if media_type == "movie"
                   else "first_air_date_year"] = year
        resp = requests.get(f"{TMDB_BASE_URL}/search/{media_type}", params=params, timeout=8)
        return resp.json().get("results", [])[:8]

    def do_search(self):
        """DÜZELTME (v28): Kutudaki metin olduğu gibi TMDB'ye gidiyordu; "Batman (2022)"
        parantezli haliyle hiç sonuç vermiyordu. Artık yıl ayrılır, bölüm/teknik
        etiketler temizlenir; yıl filtresiyle aranır, bulunamazsa yılsız denenir.
        Seçili tür (Dizi/Film) sonuç vermezse diğer tür de denenir."""
        # Eski kartları temizle
        while self.cards_layout.count():
            w = self.cards_layout.takeAt(0).widget()
            if w: w.deleteLater()

        raw = self.search_input.text().strip()
        if not raw:
            return
        year  = _extract_year(raw)
        query = clean_name(raw) or raw

        first = "tv" if self.type_combo.currentIndex() == 0 else "movie"
        other = "movie" if first == "tv" else "tv"
        attempts = []
        if year:
            attempts += [(first, year), (other, year)]
        attempts += [(first, None), (other, None)]

        results, media_type = [], first
        try:
            for mt, yr in attempts:
                results = self._search_one(mt, query, yr)
                if results:
                    media_type = mt
                    break
        except Exception as e:
            QMessageBox.critical(self, "Hata", str(e))
            return

        if not results:
            self.cards_layout.addWidget(QLabel("  Sonuç bulunamadı."))
            return

        if media_type != first:
            self.type_combo.setCurrentIndex(0 if media_type == "tv" else 1)
            self.info_label.setText(
                f"ℹ️ Sonuç {'TV Dizisi' if media_type == 'tv' else 'Film'} olarak bulundu "
                f"(tür otomatik değiştirildi).")

        for item in results:
            poster_path = item.get("poster_path", "")
            title_raw   = item.get("name") or item.get("title") or "?"
            date_str    = item.get("first_air_date") or item.get("release_date") or ""
            year        = date_str[:4] if date_str else ""
            genre_ids   = item.get("genre_ids", [])
            genre       = GENRE_MAP.get(genre_ids[0], "Diğer") if genre_ids else "Diğer"
            group       = genre
            tmdb_id     = str(item.get("id", ""))
            logo_url    = (TMDB_IMAGE_BASE + poster_path) if poster_path else ""
            # Eğer Türkçe ad Latin değilse (Japonca/Çince vb.), İngilizce detay çek
            if not is_latin(title_raw):
                try:
                    en_data = requests.get(
                        f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}",
                        params={"api_key": TMDB_API_KEY, "language": "en-US"}, timeout=6
                    ).json()
                    en_title = en_data.get("title") or en_data.get("name") or title_raw
                    # Türkçe detay da çek
                    tr_data = requests.get(
                        f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}",
                        params={"api_key": TMDB_API_KEY, "language": "tr-TR"}, timeout=6
                    ).json()
                    tr_title = tr_data.get("title") or tr_data.get("name") or ""
                    title = tr_title if (tr_title and is_latin(tr_title)) else en_title
                except Exception:
                    title = title_raw
            else:
                title = title_raw

            data = {
                "logo": logo_url, "group": group,
                "tr_name": title, "year": year,
                "tmdb_id": tmdb_id, "media_type": media_type,
            }

            card = QWidget()
            card.setFixedWidth(110)
            card.setStyleSheet("QWidget{border:2px solid #555;border-radius:4px;padding:2px}"
                               "QWidget:hover{border-color:#4a9fd4}")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(3, 3, 3, 3)
            cl.setSpacing(3)

            img = QLabel("⏳")
            img.setFixedSize(100, 148)
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img.setStyleSheet("border:none;background:#1a1a1a;font-size:28px")

            # Posteri arka planda indir
            if logo_url:
                self._load_poster_async(img, logo_url)

            lbl = QLabel(f"{title[:16]}{'…' if len(title)>16 else ''}\n{year}  |  {group}")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("border:none;font-size:9px;color:#ccc")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

            btn = QPushButton("Seç")
            btn.setStyleSheet("border:none;background:#0561a8;color:white;padding:3px;border-radius:3px")
            btn.clicked.connect(lambda _, d=data, t=title: self._select(d, t))

            cl.addWidget(img)
            cl.addWidget(lbl)
            cl.addWidget(btn)
            self.cards_layout.addWidget(card)

    def _load_poster_async(self, label: QLabel, url: str):
        """Posteri arka plan thread'inde indirir, hazır olunca QLabel'e yazar."""
        if not hasattr(self, '_poster_workers'):
            self._poster_workers = []
        worker = _PosterWorker(url)
        # Worker bitince hem posteri yaz hem listeden çıkar (GC'den koru)
        def _on_done(px, lbl=label, w=worker):
            self._set_poster(lbl, px)
            try:
                self._poster_workers.remove(w)
            except ValueError:
                pass
        worker.done.connect(_on_done)
        worker.finished.connect(worker.deleteLater)
        self._poster_workers.append(worker)
        worker.start()

    def _set_poster(self, label: QLabel, pixmap: QPixmap):
        if pixmap and not pixmap.isNull():
            label.setText("")
            label.setPixmap(pixmap.scaled(
                100, 148,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
        else:
            label.setText("🎬")

    def _select(self, data, title):
        self.selected_data = data
        self.info_label.setText(
            f"✅  <b>{data['tr_name']}</b>  ({data['year']})  |  "
            f"Tür: {data['group']}  |  TMDB ID: {data['tmdb_id']}"
        )
        self.info_label.setTextFormat(Qt.TextFormat.RichText)


# ──────────────────────────────────────────────────────────────────────
#  TOPLU BUL & DEĞİŞTİR PENCERESİ
# ──────────────────────────────────────────────────────────────────────
class FindReplaceDialog(QDialog):
    """
    Regex destekli toplu bul & değiştir.
    Sütun seçimi : İçerik Adı, URL, Tür/Grup, Logo, Tümü
    Kapsam       : Tikli / Görünür / Tüm satırlar
    """

    # (etiket, col_index)  — None = tümü
    COL_CHOICES = [
        ("İçerik Adı", COL_NAME),
        ("URL",        COL_URL),
        ("Tür/Grup",   COL_GROUP),
        ("Logo URL",   COL_LOGO),
        ("Tümü",       None),
    ]

    def __init__(self, parent=None, checked_count=0, visible_count=0, total_count=0):
        super().__init__(parent)
        self.setWindowTitle("Toplu Bul & Değiştir")
        self.setMinimumWidth(600)
        self.is_dark  = getattr(parent, "is_dark_mode", True)
        self._editor  = parent          # IPTVEditor referansı
        self._ready   = False           # __init__ tamamlanana kadar önizlemeyi engelle

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Bul / Değiştir alanları ───────────────────────────────────
        find_box = QGroupBox("Arama & Değiştirme")
        fl = QFormLayout(find_box)
        fl.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.find_input    = QLineEdit()
        self.find_input.setPlaceholderText("Aranacak metin veya regex  (örn:  TR \\|  veya  S\\d+E\\d+)")
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("Değiştirilecek metin  (boş bırakılırsa siler)")

        self.chk_regex = QCheckBox("Regex kullan")
        self.chk_case  = QCheckBox("Büyük/küçük harf duyarlı")
        opt_row = QHBoxLayout()
        opt_row.addWidget(self.chk_regex)
        opt_row.addWidget(self.chk_case)
        opt_row.addStretch()

        fl.addRow("Bul:",      self.find_input)
        fl.addRow("Değiştir:", self.replace_input)
        fl.addRow("",          opt_row)
        layout.addWidget(find_box)

        # ── Sütun seçimi ──────────────────────────────────────────────
        col_box = QGroupBox("Hangi Sütunda?")
        col_hl  = QHBoxLayout(col_box)
        self._col_group = QButtonGroup(self)
        for i, (label, _) in enumerate(self.COL_CHOICES):
            rb = QRadioButton(label)
            col_hl.addWidget(rb)
            self._col_group.addButton(rb, i)
        self._col_group.button(0).setChecked(True)
        layout.addWidget(col_box)

        # ── Kapsam seçimi ─────────────────────────────────────────────
        scope_box = QGroupBox("Hangi Satırlarda?")
        scope_hl  = QHBoxLayout(scope_box)
        self._scope_group = QButtonGroup(self)
        scopes = [
            f"Tikli ({checked_count})",
            f"Görünür ({visible_count})",
            f"Tümü ({total_count})",
        ]
        for i, lbl in enumerate(scopes):
            rb = QRadioButton(lbl)
            scope_hl.addWidget(rb)
            self._scope_group.addButton(rb, i)
        default_scope = 0 if checked_count > 0 else 1
        self._scope_group.button(default_scope).setChecked(True)
        layout.addWidget(scope_box)

        # ── Önizleme bandı ────────────────────────────────────────────
        self.preview_label = QLabel("Bul alanını doldurun…")
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumHeight(60)
        self._set_preview_style("idle")
        layout.addWidget(self.preview_label)

        # ── Butonlar ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton("✅ Uygula")
        can_btn        = QPushButton("Kapat")
        self.apply_btn.clicked.connect(self._apply)
        can_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(can_btn)
        layout.addLayout(btn_row)

        self._apply_theme()

        # Sinyalleri en sona bağla — __init__ tamamlandıktan sonra
        self._ready = True
        self.find_input.textChanged.connect(self._update_preview)
        self.replace_input.textChanged.connect(self._update_preview)
        self.chk_regex.toggled.connect(self._update_preview)
        self.chk_case.toggled.connect(self._update_preview)
        # QButtonGroup.idToggled: sadece seçilen buton için tetiklenir (PyQt6)
        self._col_group.idToggled.connect(self._on_group_toggled)
        self._scope_group.idToggled.connect(self._on_group_toggled)

    def _on_group_toggled(self, _id, checked):
        """QButtonGroup radio değişiminde — sadece 'checked=True' olanı işle."""
        if checked:
            self._update_preview()

    # ── Yardımcılar ───────────────────────────────────────────────────
    def _set_preview_style(self, kind):
        styles = {
            "idle":    "background:#1a3a1a;color:#aaffaa;border-radius:4px;padding:7px;font-size:11px",
            "error":   "background:#3a1a1a;color:#ffaaaa;border-radius:4px;padding:7px;font-size:11px",
            "nomatch": "background:#3a2a1a;color:#ffcc88;border-radius:4px;padding:7px;font-size:11px",
            "match":   "background:#1a3a1a;color:#aaffaa;border-radius:4px;padding:7px;font-size:11px",
        }
        self.preview_label.setStyleSheet(styles.get(kind, styles["idle"]))

    def _sub(self, pattern, repl, val):
        """DÜZELTME (v27): 'Regex' kapalıyken değiştirme metni olduğu gibi
        kullanılır. Önceden re.sub şablon sayılıyordu; içinde ters eğik çizgi
        (\\1, \\n, C:\\yol) olan metin ya bozuluyor ya da hata verip programı
        kapatıyordu. Regex açıkken geçersiz şablon re.error/IndexError fırlatır."""
        if not self.chk_regex.isChecked():
            return pattern.sub(lambda _m: repl, val)
        return pattern.sub(repl, val)

    def _get_selected_col(self):
        """Seçili sütun indexi. None = tümü."""
        return self.COL_CHOICES[self._col_group.checkedId()][1]

    def _get_target_rows(self):
        e     = self._editor
        scope = self._scope_group.checkedId()   # 0=tikli 1=görünür 2=tümü
        if scope == 0:
            return e._checked_rows()
        elif scope == 1:
            return [r for r in range(e.table.rowCount()) if not e.table.isRowHidden(r)]
        else:
            return list(range(e.table.rowCount()))

    def _get_target_cols(self):
        col = self._get_selected_col()
        return [COL_NAME, COL_URL, COL_GROUP, COL_LOGO] if col is None else [col]

    def _build_pattern(self):
        """Geçerli pattern döner, boş/hatalıysa None."""
        text = self.find_input.text()
        if not text:
            return None
        flags = 0 if self.chk_case.isChecked() else re.IGNORECASE
        try:
            src = text if self.chk_regex.isChecked() else re.escape(text)
            return re.compile(src, flags)
        except re.error:
            return None

    def _update_preview(self):
        if not self._ready:
            return
        pattern = self._build_pattern()
        if pattern is None:
            if self.find_input.text() and self.chk_regex.isChecked():
                self.preview_label.setText("⚠️  Geçersiz regex ifadesi")
                self._set_preview_style("error")
            else:
                self.preview_label.setText("Bul alanını doldurun…")
                self._set_preview_style("idle")
            return

        rows = self._get_target_rows()
        cols = self._get_target_cols()
        repl = self.replace_input.text()

        match_cells   = 0
        match_rows    = 0
        preview_lines = []

        for row in rows:
            row_hit = False
            for col in cols:
                val = self._editor._get_cell(row, col)
                if not pattern.search(val):
                    continue
                match_cells += 1
                row_hit = True
                if len(preview_lines) < 3:
                    try:
                        new_val = self._sub(pattern, repl, val)
                    except (re.error, IndexError):
                        new_val = "⚠ geçersiz değiştirme ifadesi"
                    col_name = next(l for l, c in self.COL_CHOICES if c == col) if col is not None else "?"
                    old_str  = val[:50] + ("…" if len(val) > 50 else "")
                    new_str  = new_val[:50] + ("…" if len(new_val) > 50 else "")
                    preview_lines.append(f"  [{col_name}]  {old_str}  →  {new_str}")
            if row_hit:
                match_rows += 1

        if match_rows == 0:
            self.preview_label.setText("🔍 Eşleşme bulunamadı")
            self._set_preview_style("nomatch")
        else:
            extra = f"\n  … ve {match_rows - 3} satır daha" if match_rows > 3 else ""
            txt = f"🔍 {match_rows} satır / {match_cells} hücre etkilenecek\n" \
                  + "\n".join(preview_lines) + extra
            self.preview_label.setText(txt)
            self._set_preview_style("match")

    def _apply(self):
        pattern = self._build_pattern()
        if pattern is None:
            QMessageBox.warning(self, "Uyarı", "Bul alanını doldurun veya geçerli bir regex girin.")
            return

        rows  = self._get_target_rows()
        cols  = self._get_target_cols()
        repl  = self.replace_input.text()

        # Önce hepsini hesapla; şablon geçersizse HİÇBİR hücre değişmesin.
        pending = []
        try:
            for row in rows:
                for col in cols:
                    val     = self._editor._get_cell(row, col)
                    new_val = self._sub(pattern, repl, val)
                    if new_val != val:
                        pending.append((row, col, new_val))
        except (re.error, IndexError) as e:
            QMessageBox.warning(self, "Uyarı", f"Değiştirme ifadesi geçersiz:\n{e}")
            return
        for row, col, new_val in pending:
            self._editor._set_cell(row, col, new_val)
        changed_cells = len(pending)
        changed_rows  = len({r for r, _c, _v in pending})

        if changed_rows == 0:
            QMessageBox.information(self, "Sonuç", "Eşleşen içerik bulunamadı, hiçbir şey değiştirilmedi.")
        else:
            QMessageBox.information(
                self, "✅ Tamamlandı",
                f"{changed_rows} satırda {changed_cells} hücre güncellendi."
            )
            self._update_preview()

    def _apply_theme(self):
        if self.is_dark:
            self.setStyleSheet("""
                QDialog,QWidget{background:#2b2b2b;color:white}
                QGroupBox{color:white;border:1px solid #555;margin-top:6px;padding:8px}
                QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px}
                QLineEdit{background:#3c3f41;color:white;border:1px solid #555;padding:5px}
                QPushButton{background:#444;color:white;border:1px solid #666;padding:6px 14px}
                QPushButton:hover{background:#555}
                QCheckBox{color:white}
                QRadioButton{color:white}
                QLabel{color:white}
            """)


# ──────────────────────────────────────────────────────────────────────
#  TOPLU DÜZENLEME PENCERESİ
# ──────────────────────────────────────────────────────────────────────
class BulkEditDialog(QDialog):
    """Seçili veya tüm görünür satırlarda Tür, Logo, Ad, User-Agent ve Referrer toplu değiştirir."""

    def __init__(self, parent=None, selected_count=0, visible_count=0):
        super().__init__(parent)
        self.setWindowTitle("Toplu Düzenle")
        self.setMinimumWidth(520)
        self.is_dark = getattr(parent, "is_dark_mode", True)
        self.result  = {}   # {"col": COL_X, "value": "..."} listesi döner

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Kapsam seçimi ──────────────────────────────────────────────
        scope_box = QGroupBox("Hangi Satırlara Uygulanacak?")
        scope_layout = QVBoxLayout(scope_box)
        self.rb_selected = QPushButton(f"☑  Tikli / Seçili Satırlar ({selected_count} satır)")
        self.rb_visible  = QPushButton(f"📋  Tüm Görünür Satırlar ({visible_count} satır)")
        self.rb_selected.setCheckable(True)
        self.rb_visible.setCheckable(True)
        self.rb_selected.setChecked(True)
        self._scope_btns = [self.rb_selected, self.rb_visible]
        self.rb_selected.clicked.connect(lambda: self._set_scope(0))
        self.rb_visible.clicked.connect(lambda:  self._set_scope(1))
        scope_layout.addWidget(self.rb_selected)
        scope_layout.addWidget(self.rb_visible)
        layout.addWidget(scope_box)

        # ── Alan giriş formu ───────────────────────────────────────────
        form_box = QGroupBox("Değiştirilecek Alanlar  (boş bırakılanlar değişmez)")
        form = QFormLayout(form_box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.chk_group = QCheckBox("Tür / Grup")
        self.inp_group = QLineEdit()
        self.inp_group.setPlaceholderText("örn:  Lucifer  veya  Drama")
        self.chk_group.toggled.connect(self.inp_group.setEnabled)
        self.inp_group.setEnabled(False)
        row_g = QHBoxLayout(); row_g.addWidget(self.chk_group); row_g.addWidget(self.inp_group, 1)
        form.addRow(row_g)

        self.chk_logo = QCheckBox("Logo URL")
        self.inp_logo = QLineEdit()
        self.inp_logo.setPlaceholderText("https://image.tmdb.org/…")
        self.chk_logo.toggled.connect(self.inp_logo.setEnabled)
        self.inp_logo.setEnabled(False)
        row_l = QHBoxLayout(); row_l.addWidget(self.chk_logo); row_l.addWidget(self.inp_logo, 1)
        form.addRow(row_l)

        self.chk_name = QCheckBox("İçerik Adı")
        self.inp_name = QLineEdit()
        self.inp_name.setPlaceholderText("Sadece dizi/film adı (bölüm bilgisi korunur)")
        self.chk_name.toggled.connect(self.inp_name.setEnabled)
        self.inp_name.setEnabled(False)
        # Bölüm bilgisini koru seçeneği
        self.chk_keep_ep = QCheckBox("Bölüm bilgisini koru  (S01 E01 gibi)")
        self.chk_keep_ep.setChecked(True)
        row_n = QHBoxLayout(); row_n.addWidget(self.chk_name); row_n.addWidget(self.inp_name, 1)
        form.addRow(row_n)
        form.addRow("", self.chk_keep_ep)

        self.chk_ua = QCheckBox("User-Agent")
        self.inp_ua = QLineEdit()
        self.inp_ua.setPlaceholderText("örn: Mozilla/5.0 ... veya VLC/3.0.20 LibVLC/3.0.20")
        self.chk_ua.toggled.connect(self.inp_ua.setEnabled)
        self.inp_ua.setEnabled(False)
        row_ua = QHBoxLayout(); row_ua.addWidget(self.chk_ua); row_ua.addWidget(self.inp_ua, 1)
        form.addRow(row_ua)

        self.chk_ref = QCheckBox("Referrer")
        self.inp_ref = QLineEdit()
        self.inp_ref.setPlaceholderText("örn: https://site.com/")
        self.chk_ref.toggled.connect(self.inp_ref.setEnabled)
        self.inp_ref.setEnabled(False)
        row_ref = QHBoxLayout(); row_ref.addWidget(self.chk_ref); row_ref.addWidget(self.inp_ref, 1)
        form.addRow(row_ref)

        layout.addWidget(form_box)

        # ── Butonlar ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        ok  = QPushButton("✅ Uygula")
        can = QPushButton("İptal")
        ok.clicked.connect(self._on_ok)
        can.clicked.connect(self.reject)
        btn_row.addStretch(); btn_row.addWidget(ok); btn_row.addWidget(can)
        layout.addLayout(btn_row)

        self._apply_theme()

    def _set_scope(self, idx):
        for i, btn in enumerate(self._scope_btns):
            btn.setChecked(i == idx)

    def scope(self):
        """'selected' veya 'visible' döner."""
        return "selected" if self.rb_selected.isChecked() else "visible"

    def _on_ok(self):
        fields = []
        if self.chk_group.isChecked() and self.inp_group.text().strip():
            fields.append({"col": COL_GROUP, "value": self.inp_group.text().strip()})
        if self.chk_logo.isChecked() and self.inp_logo.text().strip():
            fields.append({"col": COL_LOGO,  "value": self.inp_logo.text().strip()})
        if self.chk_name.isChecked() and self.inp_name.text().strip():
            fields.append({
                "col":      COL_NAME,
                "value":    self.inp_name.text().strip(),
                "keep_ep":  self.chk_keep_ep.isChecked(),
            })
        if self.chk_ua.isChecked():
            # boş değer de geçerli: UA silmek için
            fields.append({"col": COL_UA, "value": self.inp_ua.text().strip()})
        if self.chk_ref.isChecked():
            fields.append({"col": COL_REF, "value": self.inp_ref.text().strip()})
        if not fields:
            QMessageBox.warning(self, "Uyarı", "En az bir alan seçip değer girin.")
            return
        self.result = fields
        self.accept()

    def _apply_theme(self):
        if self.is_dark:
            self.setStyleSheet("""
                QDialog,QWidget{background:#2b2b2b;color:white}
                QGroupBox{color:white;border:1px solid #555;margin-top:6px;padding:6px}
                QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px}
                QLineEdit{background:#3c3f41;color:white;border:1px solid #555;padding:5px}
                QPushButton{background:#444;color:white;border:1px solid #666;padding:6px 14px}
                QPushButton:hover{background:#555}
                QPushButton:checked{background:#0561a8;border-color:#0461a0}
                QCheckBox{color:white}
                QLabel{color:white}
            """)



# ──────────────────────────────────────────────────────────────────────
#  POSTER BÜYÜK ÖNİZLEME
# ──────────────────────────────────────────────────────────────────────
def _hires_poster_url(url: str) -> str:
    """TMDB küçük poster URL'sini yüksek çözünürlüğe çevirir (w200/w92 → w780)."""
    if not url:
        return url
    # image.tmdb.org/t/p/w200/... veya w92, w154, w185, w342, w500
    url = re.sub(
        r'(image\.tmdb\.org/t/p/)w\d+/',
        r'\1w780/',
        url,
        count=1,
        flags=re.IGNORECASE,
    )
    # Bazı listelerde /t/p/original/ zaten vardır; dokunma
    return url


class PosterPreviewDialog(QDialog):
    """Logo URL'sinden yüksek çözünürlüklü poster gösterir."""

    def __init__(self, parent, url: str, title: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title or "Poster Önizleme")
        self.setMinimumSize(420, 620)
        self.resize(520, 780)
        is_dark = getattr(parent, "is_dark_mode", True)
        if is_dark:
            self.setStyleSheet("QDialog{background:#1e1e1e;color:white} QLabel{color:white}")
        lay = QVBoxLayout(self)
        self.lbl = QLabel("Yüksek çözünürlük yükleniyor…")
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl.setMinimumSize(400, 600)
        # Kaydırılabilir alan (çok büyük posterler için)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.lbl)
        lay.addWidget(scroll, 1)
        self._info = QLabel()
        self._info.setWordWrap(True)
        self._info.setStyleSheet("color:#888;font-size:11px")
        lay.addWidget(self._info)
        btn = QPushButton("Kapat")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn)
        self._url = url
        self._hires = _hires_poster_url(url)
        self._info.setText(self._hires[:100] + ("…" if len(self._hires) > 100 else ""))
        QTimer.singleShot(50, self._load)

    def _load(self):
        # Küçük thumbnail önbelleğini KULLANMA — bulanık olur.
        # Her zaman yüksek çözünürlüklü URL'yi indir.
        url = self._hires
        px = None
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": DEFAULT_UA})
            r.raise_for_status()
            px = QPixmap()
            px.loadFromData(r.content)
        except Exception as e:
            # w780 başarısızsa orijinal URL'yi dene
            if url != self._url:
                try:
                    r = requests.get(self._url, timeout=15, headers={"User-Agent": DEFAULT_UA})
                    r.raise_for_status()
                    px = QPixmap()
                    px.loadFromData(r.content)
                except Exception as e2:
                    self.lbl.setText(f"Yüklenemedi:\n{e2}")
                    return
            else:
                self.lbl.setText(f"Yüklenemedi:\n{e}")
                return
        if px is None or px.isNull():
            self.lbl.setText("Görsel yok")
            return
        # Ekrana sığdır ama kaynağın üstüne çıkarma (upscale yok)
        max_w, max_h = 500, 750
        if px.width() > max_w or px.height() > max_h:
            scaled = px.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        else:
            scaled = px
        self.lbl.setPixmap(scaled)
        self._info.setText(
            f"{px.width()}×{px.height()} px  ·  "
            + (self._hires[:90] + ("…" if len(self._hires) > 90 else ""))
        )


# ──────────────────────────────────────────────────────────────────────
#  ŞÜPHELİ TMDB ONAY EKRANI
# ──────────────────────────────────────────────────────────────────────
class DoubtReviewDialog(QDialog):
    """TMDB'nin emin olmadığı eşleşmeleri tek tek onayla / reddet / ara."""

    def __init__(self, parent, items):
        """
        items: list of dict
          {row, name, candidate: {tr_name, year, group, tmdb_id, logo, reason, ...}}
        """
        super().__init__(parent)
        self.setWindowTitle(f"Şüpheli TMDB Onayı — {len(items)} adet")
        self.setMinimumSize(640, 420)
        self.resize(720, 520)
        self._items = list(items)
        self._idx = 0
        self._editor = parent
        self._applied = 0
        self._skipped = 0
        self.is_dark = getattr(parent, "is_dark_mode", True)
        self._build()
        self._show_current()
        self._apply_theme()

    def _build(self):
        lay = QVBoxLayout(self)
        self.progress_lbl = QLabel()
        lay.addWidget(self.progress_lbl)

        body = QHBoxLayout()
        # Sol: mevcut satır
        left = QVBoxLayout()
        left.addWidget(QLabel("Listedeki ad:"))
        self.cur_name = QLabel()
        self.cur_name.setWordWrap(True)
        self.cur_name.setStyleSheet("font-size:14px;font-weight:bold")
        left.addWidget(self.cur_name)
        left.addWidget(QLabel("TMDB adayı (henüz uygulanmadı):"))
        self.cand_lbl = QLabel()
        self.cand_lbl.setWordWrap(True)
        self.cand_lbl.setStyleSheet("font-size:13px")
        left.addWidget(self.cand_lbl)
        self.reason_lbl = QLabel()
        self.reason_lbl.setWordWrap(True)
        self.reason_lbl.setStyleSheet("color:#ffaa00")
        left.addWidget(self.reason_lbl)
        left.addStretch()
        body.addLayout(left, 2)

        # Sağ: poster
        self.poster = QLabel("Poster")
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setFixedSize(180, 270)
        self.poster.setStyleSheet("background:#333;border:1px solid #555")
        body.addWidget(self.poster)
        lay.addLayout(body, 1)

        btns = QHBoxLayout()
        self.btn_apply = QPushButton("✅ Uygula (doğru)")
        self.btn_skip  = QPushButton("⏭ Atla")
        self.btn_search = QPushButton("🔍 TMDB'de Ara")
        self.btn_reject = QPushButton("❌ Reddet (bulunamadı işaretle)")
        self.btn_close = QPushButton("Kapat")
        self.btn_apply.clicked.connect(self._do_apply)
        self.btn_skip.clicked.connect(self._do_skip)
        self.btn_search.clicked.connect(self._do_search)
        self.btn_reject.clicked.connect(self._do_reject)
        self.btn_close.clicked.connect(self.accept)
        for b in (self.btn_apply, self.btn_skip, self.btn_search, self.btn_reject, self.btn_close):
            btns.addWidget(b)
        lay.addLayout(btns)

    def _apply_theme(self):
        if self.is_dark:
            self.setStyleSheet("""
                QDialog,QWidget{background:#2b2b2b;color:white}
                QLabel{color:white}
                QPushButton{background:#444;color:white;border:1px solid #666;padding:8px 12px}
                QPushButton:hover{background:#555}
            """)

    def _show_current(self):
        if self._idx >= len(self._items):
            self.progress_lbl.setText(
                f"Bitti — uygulanan: {self._applied}, atlanan: {self._skipped}")
            self.cur_name.setText("—")
            self.cand_lbl.setText("Tüm şüpheliler işlendi.")
            self.reason_lbl.setText("")
            self.poster.clear()
            self.btn_apply.setEnabled(False)
            self.btn_skip.setEnabled(False)
            self.btn_search.setEnabled(False)
            self.btn_reject.setEnabled(False)
            return
        it = self._items[self._idx]
        self.progress_lbl.setText(
            f"{self._idx + 1} / {len(self._items)}   "
            f"(uygulanan: {self._applied}, atlanan: {self._skipped})")
        self.cur_name.setText(it.get("name") or "?")
        c = it.get("candidate") or {}
        self.cand_lbl.setText(
            f"{c.get('tr_name', '?')}  ({c.get('year') or '?'})\n"
            f"Tür: {c.get('group', '')}\n"
            f"TMDB ID: {c.get('tmdb_id', '')}"
        )
        self.reason_lbl.setText(f"Sebep: {c.get('reason') or 'emin değil'}")
        logo = c.get("logo") or ""
        self.poster.clear()
        self.poster.setText("…")
        if logo:
            QTimer.singleShot(30, lambda u=logo: self._load_poster(u))

    def _load_poster(self, url):
        try:
            url = _hires_poster_url(url)
            r = requests.get(url, timeout=12, headers={"User-Agent": DEFAULT_UA})
            px = QPixmap()
            px.loadFromData(r.content)
            if not px.isNull():
                self.poster.setPixmap(px.scaled(
                    180, 270, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                return
        except Exception:
            pass
        self.poster.setText("Yok")

    def _advance(self):
        self._idx += 1
        self._show_current()

    def _do_apply(self):
        if self._idx >= len(self._items):
            return
        it = self._items[self._idx]
        row = it["row"]
        data = it.get("candidate") or {}
        opts = self._editor._get_opts()
        self._editor._snapshot("Şüpheli TMDB onay")
        self._editor._apply_tmdb_data(row, data, opts)
        # doubt işaretini temizle
        st = self._editor.table.item(row, COL_STATUS)
        if st:
            st.setData(ROLE_DOUBT, None)
        self._applied += 1
        self._advance()

    def _do_skip(self):
        self._skipped += 1
        self._advance()

    def _do_reject(self):
        if self._idx >= len(self._items):
            return
        it = self._items[self._idx]
        row = it["row"]
        self._editor._on_tmdb_not_found(row)
        st = self._editor.table.item(row, COL_STATUS)
        if st:
            st.setData(ROLE_DOUBT, None)
        self._skipped += 1
        self._advance()

    def _do_search(self):
        if self._idx >= len(self._items):
            return
        it = self._items[self._idx]
        row = it["row"]
        # Geçici olarak o satırı seç
        self._editor.table.selectRow(row)
        dlg = TMDBSearchDialog(self._editor, it.get("name") or "")
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_data:
            self._editor._snapshot("Şüpheli TMDB elle seç")
            self._editor._apply_tmdb_data(row, dlg.selected_data, self._editor._get_opts())
            st = self._editor.table.item(row, COL_STATUS)
            if st:
                st.setData(ROLE_DOUBT, None)
            self._applied += 1
            self._advance()


# ──────────────────────────────────────────────────────────────────────
#  UA / REFERRER ŞABLONLARI
# ──────────────────────────────────────────────────────────────────────
class UATemplateDialog(QDialog):
    """Hazır User-Agent / Referrer şablonlarını yönet ve uygula."""

    def __init__(self, parent, templates: list):
        super().__init__(parent)
        self.setWindowTitle("UA / Referrer Şablonları")
        self.setMinimumSize(560, 380)
        self.templates = [dict(t) for t in (templates or [])]
        self.is_dark = getattr(parent, "is_dark_mode", True)
        self.selected = None   # uygulanacak şablon
        self._build()
        self._apply_theme()
        self._refresh_list()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Şablon seç → Uygula  |  veya yeni şablon ekle"))

        self.list_w = QComboBox()
        self.list_w.currentIndexChanged.connect(self._on_select)
        lay.addWidget(self.list_w)

        form = QFormLayout()
        self.inp_name = QLineEdit()
        self.inp_ua = QLineEdit()
        self.inp_ref = QLineEdit()
        self.inp_ua.setPlaceholderText("User-Agent")
        self.inp_ref.setPlaceholderText("Referrer (opsiyonel)")
        form.addRow("Şablon adı:", self.inp_name)
        form.addRow("User-Agent:", self.inp_ua)
        form.addRow("Referrer:", self.inp_ref)
        lay.addLayout(form)

        # Hazır UA kısayolları
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Hazır UA:"))
        for pname, pua in UA_PRESETS:
            b = QPushButton(pname)
            b.setFixedHeight(26)
            b.clicked.connect(lambda _=False, u=pua: self.inp_ua.setText(u))
            preset_row.addWidget(b)
        preset_row.addStretch()
        lay.addLayout(preset_row)

        row = QHBoxLayout()
        btn_save = QPushButton("💾 Şablonu Kaydet")
        btn_del  = QPushButton("🗑️ Sil")
        btn_apply_sel = QPushButton("✅ Seçili satırlara uygula")
        btn_apply_vis = QPushButton("📋 Görünür satırlara uygula")
        btn_close = QPushButton("Kapat")
        btn_save.clicked.connect(self._save)
        btn_del.clicked.connect(self._delete)
        btn_apply_sel.clicked.connect(lambda: self._apply("selected"))
        btn_apply_vis.clicked.connect(lambda: self._apply("visible"))
        btn_close.clicked.connect(self.accept)
        for b in (btn_save, btn_del, btn_apply_sel, btn_apply_vis, btn_close):
            row.addWidget(b)
        lay.addLayout(row)

    def _apply_theme(self):
        if self.is_dark:
            self.setStyleSheet("""
                QDialog,QWidget{background:#2b2b2b;color:white}
                QLineEdit,QComboBox{background:#3c3f41;color:white;border:1px solid #555;padding:4px}
                QPushButton{background:#444;color:white;border:1px solid #666;padding:6px 10px}
                QPushButton:hover{background:#555}
                QLabel{color:white}
            """)

    def _refresh_list(self):
        self.list_w.blockSignals(True)
        self.list_w.clear()
        self.list_w.addItem("— Yeni şablon —")
        for t in self.templates:
            self.list_w.addItem(t.get("name") or "(isimsiz)")
        self.list_w.blockSignals(False)

    def _on_select(self, idx):
        if idx <= 0 or idx > len(self.templates):
            self.inp_name.clear()
            self.inp_ua.clear()
            self.inp_ref.clear()
            return
        t = self.templates[idx - 1]
        self.inp_name.setText(t.get("name", ""))
        self.inp_ua.setText(t.get("ua", ""))
        self.inp_ref.setText(t.get("ref", ""))

    def _save(self):
        name = self.inp_name.text().strip()
        ua = self.inp_ua.text().strip()
        ref = self.inp_ref.text().strip()
        if not name:
            QMessageBox.warning(self, "Uyarı", "Şablon adı gerekli.")
            return
        # Aynı isim varsa güncelle
        for t in self.templates:
            if t.get("name") == name:
                t["ua"] = ua
                t["ref"] = ref
                self._refresh_list()
                QMessageBox.information(self, "Tamam", "Şablon güncellendi.")
                return
        self.templates.append({"name": name, "ua": ua, "ref": ref})
        self._refresh_list()
        self.list_w.setCurrentIndex(len(self.templates))
        QMessageBox.information(self, "Tamam", "Şablon kaydedildi.")

    def _delete(self):
        idx = self.list_w.currentIndex()
        if idx <= 0 or idx > len(self.templates):
            return
        del self.templates[idx - 1]
        self._refresh_list()
        self.inp_name.clear()
        self.inp_ua.clear()
        self.inp_ref.clear()

    def _apply(self, scope):
        ua = self.inp_ua.text().strip()
        ref = self.inp_ref.text().strip()
        if not ua and not ref:
            QMessageBox.warning(self, "Uyarı", "User-Agent veya Referrer girin.")
            return
        self.selected = {"ua": ua, "ref": ref, "scope": scope}
        self.accept()


# ──────────────────────────────────────────────────────────────────────
#  PARALEL LİNK KONTROL İŞÇİSİ
# ──────────────────────────────────────────────────────────────────────
class LinkCheckWorker(QThread):
    """
    IPTV / VOD link kontrolü.

    Eski yöntem sorunları (VLC'de açılan linkleri ÖLÜ sayıyordu):
      • Range: bytes=0-1  → birçok CDN / HLS 403 veya 416 döner
      • Sadece birkaç HTTP kodu "canlı" sayılıyordu
      • 30 paralel istek → sunucu rate-limit / bağlantı reddi → yanlış KAPALI
      • Gövde hiç okunmuyordu; #EXTM3U / TS imzası doğrulanmıyordu

    Yeni yöntem:
      1) VLC benzeri UA ile GET (Range YOK)
      2) Gerekirse Range ile ikinci deneme
      3) .m3u8 ise içerikte #EXTM3U ara
      4) TS/medya imzası (0x47) veya yeterli bayt = canlı
      5) 401/403 tek başına ölü sayılmaz (çoğu token'lı yayın böyle başlar;
         kısa gövde + medya imzası varsa AKTİF)
    """
    result_ready = pyqtSignal(int, str, object)   # row, status_text, QColor
    progress     = pyqtSignal(int, int, str)      # done, total, url
    finished     = pyqtSignal(int, int, int)      # active, dead, error

    def __init__(self, rows_urls, workers=12, timeout=12, user_agent="", referrer=""):
        """
        rows_urls  : [(row_index, url) veya (row_index, url, ua, ref), ...]
                     Satıra özel ua/ref varsa genel değerlerin önüne geçer.
        workers    : eş zamanlı bağlantı (varsayılan 12 — 30 çok agresifti)
        timeout    : saniye
        user_agent : genel User-Agent (boş = VLC benzeri varsayılan)
        referrer   : genel Referer (boş = gönderilmez)
        """
        super().__init__()
        self.user_agent = user_agent or ""
        self.referrer   = referrer or ""
        self.rows_urls = rows_urls
        self.workers   = max(1, min(int(workers), 40))
        self.timeout   = max(4, int(timeout))
        self._stop     = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _headers(range_bytes=False, ua="", ref=""):
        h = {
            "User-Agent": _latin1(ua) or "VLC/3.0.20 LibVLC/3.0.20",
            "Accept": "*/*",
            "Connection": "close",
            "Icy-MetaData": "1",
        }
        if ref:
            h["Referer"] = _latin1(ref)
        if range_bytes:
            h["Range"] = "bytes=0-65535"
        return h

    def _probe(self, url: str, use_range: bool, ua: str = "", ref: str = ""):
        """
        (ok: bool, status_code: int|None, detail: str)
        ok=True  → gerçek medya/HLS kanıtı var
        ok=False → ölü, hata veya şüpheli (detail'e bak)
        """
        try:
            r = requests.get(
                url,
                timeout=(min(6, self.timeout), self.timeout),
                headers=self._headers(use_range, ua, ref),
                allow_redirects=True,
                stream=True,
            )
        except requests.exceptions.Timeout:
            return False, None, "zaman aşımı"
        except requests.exceptions.SSLError:
            try:
                r = requests.get(
                    url,
                    timeout=(min(6, self.timeout), self.timeout),
                    headers=self._headers(use_range, ua, ref),
                    allow_redirects=True,
                    stream=True,
                    verify=False,
                )
            except Exception as e:
                return False, None, f"ssl: {e.__class__.__name__}"
        except requests.exceptions.ConnectionError:
            return False, None, "bağlantı yok"
        except Exception as e:
            return False, None, e.__class__.__name__

        code = r.status_code
        chunk = b""
        try:
            for part in r.iter_content(65536):
                if part:
                    chunk = part
                    break
        except Exception:
            pass
        try:
            r.close()
        except Exception:
            pass

        ct = (r.headers.get("Content-Type") or "").lower()
        url_l = (url or "").lower()

        # HTML hata sayfası mı? (birçok ölü link 200 + HTML döner)
        head = chunk[:200].lstrip().lower()
        is_html = (
            head.startswith(b"<!doctype html") or head.startswith(b"<html")
            or head.startswith(b"<head") or b"<html" in chunk[:500].lower()
            or "text/html" in ct
        )

        # ── 200 / 206 ────────────────────────────────────────────────
        if code in (200, 206):
            # HTML = ölü (hata sayfası)
            if is_html:
                return False, code, "html-hata"

            # HLS oynatma listesi
            is_hls_url = (
                "mpegurl" in ct or "x-mpegURL" in ct
                or ".m3u8" in url_l or url_l.endswith(".m3u")
            )
            if is_hls_url or b"#EXT" in chunk[:64]:
                try:
                    text_body = chunk.decode("utf-8", "ignore")
                except Exception:
                    text_body = ""
                if "#EXTM3U" in text_body or "#EXT-X-" in text_body or "#EXTINF" in text_body:
                    # Boş / sadece başlık listeler genelde çalışmaz
                    # ama en azından geçerli HLS yapısı var
                    if "#EXT-X-ENDLIST" in text_body and text_body.count("http") == 0 and ".ts" not in text_body.lower():
                        # segment yok
                        if text_body.count("#EXTINF") == 0:
                            return False, code, "hls-bos"
                    return True, code, "hls"
                # HLS URL ama içerik HLS değil
                return False, code, "hls-gecersiz"

            # MPEG-TS: senkron baytı 0x47 (188 bayt paketler)
            if len(chunk) >= 188 and (chunk[0] == 0x47 or b"\x47" in chunk[:188]):
                # En az bir TS sync doğrula
                if chunk[0] == 0x47 or (len(chunk) > 188 and chunk[188] == 0x47):
                    return True, code, "ts"
                if chunk[0] == 0x47:
                    return True, code, "ts"

            # mp4 / fmp4
            if b"ftyp" in chunk[:64] or b"moof" in chunk[:64]:
                return True, code, "mp4"

            # Net video/audio content-type (genel octet-stream HARİÇ)
            media_cts = (
                "video/", "audio/", "application/mp2t", "application/mp4",
                "application/vnd.apple.mpegurl", "application/x-mpegurl",
            )
            if any(x in ct for x in media_cts):
                if len(chunk) >= 32:
                    return True, code, "media-ct"

            # octet-stream: sadece medya imzası varsa kabul
            if "octet-stream" in ct or "binary" in ct:
                if chunk[0:1] == b"\x47" or b"ftyp" in chunk[:64]:
                    return True, code, "binary-media"
                # rastgele binary / hata gövdesi
                return False, code, "binary-bilinmeyen"

            # Hiçbir medya kanıtı yok
            if len(chunk) == 0:
                return False, code, "bos-govde"
            return False, code, "medya-yok"

        # ── 401 / 403 ────────────────────────────────────────────────
        if code in (401, 403):
            try:
                text_body = chunk.decode("utf-8", "ignore")
            except Exception:
                text_body = ""
            if "#EXTM3U" in text_body or (len(chunk) >= 188 and chunk[0] == 0x47):
                return True, code, f"korumali-{code}"
            return False, code, f"yasak-{code}"

        # ── 404 / 410 / 451 ───────────────────────────────────────────
        if code in (404, 410, 451):
            return False, code, f"yok-{code}"

        if code == 416:
            return False, code, "range-uyumsuz"

        # Geçici sunucu hataları — AKTİF sayma, şüpheli bırak
        if code in (429, 500, 502, 503, 504):
            return False, code, f"gecici-{code}"

        if code and code >= 400:
            return False, code, f"http-{code}"

        return False, code, "bilinmeyen"

    def _check_one(self, row, url, ua="", ref=""):
        """Tek link — birkaç strateji dener, VLC ile uyumlu sonuç üretir."""
        if not url or not str(url).strip():
            return row, "KAPALI 🛑", QColor(100, 100, 100)
        url = str(url).strip()

        # 1) Range olmadan (asıl yöntem — VLC çoğu zaman böyle açar)
        ok, code, detail = self._probe(url, use_range=False, ua=ua, ref=ref)
        if ok:
            return row, "AKTİF ✅", QColor(0, 150, 0)

        # 2) Range ile (bazı sunucular Range ister)
        if detail in ("range-uyumsuz", "bağlantı yok", "zaman aşımı", "http-403", "yasak-403"):
            ok2, code2, detail2 = self._probe(url, use_range=True, ua=ua, ref=ref)
            if ok2:
                return row, "AKTİF ✅", QColor(0, 150, 0)
            code = code2 or code
            detail = detail2 or detail

        # 3) Bazı CDN / Cloudflare VLC UA'ya 403+HTML döner; tarayıcı UA ile HLS verir
        #    (ör. vixolity vb.). 403 / html-hata / yasak durumunda Chrome dene.
        need_browser = (
            code in (401, 403)
            or (detail and (detail.startswith("yasak") or detail in ("html-hata", "http-403")))
        )
        if need_browser:
            browser_uas = []
            # Kullanıcı UA'sı zaten Chrome değilse ekle
            chrome = DEFAULT_UA
            if (ua or "").strip() != chrome:
                browser_uas.append(chrome)
            browser_uas.append(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
                "Gecko/20100101 Firefox/128.0"
            )
            for bua in browser_uas:
                okb, codeb, detailb = self._probe(url, use_range=False, ua=bua, ref=ref)
                if okb:
                    return row, "AKTİF ✅", QColor(0, 150, 0)
                # 403 devam ediyorsa diğer UA'ya geç
                if codeb not in (401, 403) and detailb not in ("html-hata",):
                    code, detail = codeb or code, detailb or detail
                    break
                code, detail = codeb or code, detailb or detail

        # 4) Geçici hatalar (503/502/429/504) — 2 kez, artan bekleme
        if detail.startswith("gecici") or detail == "zaman aşımı":
            for bekle in (1.0, 2.0):
                time.sleep(bekle)
                ok3, code3, detail3 = self._probe(url, use_range=False, ua=ua or DEFAULT_UA, ref=ref)
                if ok3:
                    return row, "AKTİF ✅", QColor(0, 150, 0)
                code = code3 or code
                detail = detail3 or detail
                if not (detail.startswith("gecici") or detail == "zaman aşımı"):
                    break

        # ── Sonuç sınıflandırma ──────────────────────────────────────
        # Geçici sunucu hataları → ŞÜPHELİ (AKTİF değil!)
        if code in (429, 500, 502, 503, 504) or (detail and detail.startswith("gecici")):
            return row, f"ŞÜPHELİ ⚠️ {code or detail}", QColor(180, 120, 0)

        if detail in ("html-hata", "hls-gecersiz", "hls-bos", "medya-yok",
                      "bos-govde", "binary-bilinmeyen"):
            return row, "KAPALI 🛑", QColor(100, 100, 100)

        if detail.startswith("yasak") or (code in (401, 403)):
            return row, f"ŞÜPHELİ ⚠️ {code or ''}".strip(), QColor(180, 120, 0)

        if detail.startswith("yok") or (code in (404, 410, 451)):
            return row, f"HATA {code}", QColor(150, 0, 0)

        if detail in ("bağlantı yok",) or (detail and detail.startswith("ssl")):
            return row, "KAPALI 🛑", QColor(100, 100, 100)

        if detail == "zaman aşımı":
            return row, "ŞÜPHELİ ⚠️ timeout", QColor(180, 120, 0)

        if code and code >= 400:
            return row, f"HATA {code}", QColor(150, 0, 0)

        if code:
            return row, f"ŞÜPHELİ ⚠️ {code}", QColor(180, 120, 0)
        return row, "KAPALI 🛑", QColor(100, 100, 100)

    def run(self):
        total  = len(self.rows_urls)
        done   = 0
        active = dead = error = 0

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {}
            for item in self.rows_urls:
                row, url = item[0], item[1]
                ua  = (item[2] if len(item) > 2 else "") or self.user_agent
                ref = (item[3] if len(item) > 3 else "") or self.referrer
                futures[executor.submit(self._check_one, row, url, ua, ref)] = (row, url)
            for future in as_completed(futures):
                if self._stop:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    row, status, color = future.result()
                except Exception:
                    row, url = futures[future]
                    status, color = "KAPALI 🛑", QColor(100, 100, 100)
                done += 1
                _, url = futures[future]

                if "AKTİF" in status:
                    active += 1
                elif "KAPALI" in status or status.startswith("HATA"):
                    dead += 1
                else:
                    # ŞÜPHELİ vb.
                    error += 1

                self.result_ready.emit(row, status, color)
                self.progress.emit(done, total, url)

        self.finished.emit(active, dead, error)


# ──────────────────────────────────────────────────────────────────────
#  ANA PENCERE
# ──────────────────────────────────────────────────────────────────────
class IPTVEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pro M3U Editor v31 — TMDB")
        self.setGeometry(100, 100, 1400, 780)
        self.is_dark_mode = True
        self._ui_font_pt = 12
        self._info_visible = False
        self._detail_cache = {}
        self.tmdb_worker      = None
        self.link_check_worker = None
        self._check_running   = False
        self._check_paused_at = 0

        cw = QWidget()
        self.setCentralWidget(cw)
        vl = QVBoxLayout(cw)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # search_bar ribbon panel içinde _build_toolbar'da oluşturuluyor
        self.search_bar = None   # geçici placeholder — _build_toolbar override eder
        self._build_toolbar()   # panel vl'ye insertWidget(0,...) ile ekleniyor
        self._build_uaref_panel()   # en üstte aç/kapa User-Agent / Referrer şeridi

        # Tablo — 10 sütun (UA/REF varsayılan gizli)
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels(
            ["✔", "Logo (URL)", "Tür/Grup", "İçerik Adı", "Yıl", "tvg-id (TMDB)",
             "URL", "User-Agent", "Referrer", "Durum"]
        )
        # Header'a tümünü seç/kaldır tiki
        self._all_checked = False
        # DÜZELTME (v26): Qt'nin varsayılan başlık-tıklama sıralaması HER
        # ZAMAN tüm tabloyu sıralar (gizli/filtrelenmiş satırlar dahil).
        # Bunun yerine kendi sıralama fonksiyonumuzu kullanıyoruz; bu
        # sayede bir arama/filtre aktifken yapılan sıralama SADECE görünen
        # satırları etkiler, filtre dışındaki içerikler yerinde kalır.
        self._sort_order   = {}     # {col: ascending(bool)}
        self._last_sort_col = None
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_section_clicked)
        self.table.horizontalHeader().setSortIndicatorShown(True)

        self.table.setSortingEnabled(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        try:
            self.table.setUniformRowHeights(True)
        except Exception:
            pass
        hh = self.table.horizontalHeader()
        hh.setMinimumSectionSize(24)
        hh.setSectionResizeMode(COL_CHECK,  QHeaderView.ResizeMode.Fixed)
        for col in range(1, 7):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_CHECK, 30)
        self.table.setColumnWidth(COL_UA, 140)
        self.table.setColumnWidth(COL_REF, 140)
        # UA/REF sütunları varsayılan gizli
        self._uaref_cols_visible = False
        self._col_ratios = [None, 0.14, 0.08, 0.15, 0.04, 0.07, 0.30, 0.10, 0.10, 0.10]  # None = fixed
        # UA/REF sütunları: başlangıçta gizli (toggle ile açılır)
        self.table.setColumnHidden(COL_UA, True)
        self.table.setColumnHidden(COL_REF, True)
        self._fixing = False
        hh.sectionResized.connect(self._fix_last_col)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.viewport().customContextMenuRequested.connect(self._context_menu)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.itemSelectionChanged.connect(self._on_selection_for_info)
        self._info_panel = QTextEdit()
        self._info_panel.setReadOnly(True)
        self._info_panel.setMinimumWidth(220)
        self._info_panel.setMaximumWidth(360)
        self._info_panel.setPlaceholderText(
            "TMDB özet ve oyuncular burada görünür.\n"
            "Satır seçin veya TMDB → Özet / Oyuncu Getir.")
        self._info_panel.hide()
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self.table)
        self._splitter.addWidget(self._info_panel)
        self._splitter.setStretchFactor(0, 4)
        self._splitter.setStretchFactor(1, 1)
        vl.addWidget(self._splitter)

        # ── Canlı istatistik paneli (statusbar sağı) ──────────────────
        self._stat_total   = QLabel("Toplam: 0")
        self._stat_visible = QLabel("Görünür: 0")
        self._stat_checked = QLabel("Tikli: 0")
        self._stat_tmdb_ok = QLabel("TMDB ✅: 0")
        self._stat_tmdb_no = QLabel("❌: 0")
        self._stat_uaref   = QLabel("UA/Ref: 0")
        self._stat_uaref.setToolTip("Kendi User-Agent / Referrer değeri olan satır sayısı")
        for lbl in (self._stat_total, self._stat_visible,
                    self._stat_checked, self._stat_tmdb_ok, self._stat_tmdb_no,
                    self._stat_uaref):
            lbl.setStyleSheet("padding:0 8px;font-size:12px")
            self.statusBar().addPermanentWidget(lbl)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet("color:#666")
        self.statusBar().addPermanentWidget(sep1)

        # Tablo değişince istatistikleri güncelle
        self.table.itemChanged.connect(self._schedule_stats)
        self.table.model().rowsInserted.connect(self._schedule_stats)
        self.table.model().rowsRemoved.connect(self._schedule_stats)
        self._stats_timer = QTimer(self)
        self._stats_timer.setSingleShot(True)
        self._stats_timer.setInterval(350)   # debounce
        self._stats_timer.timeout.connect(self._update_stats)

        # ── Geri alma / otomatik kaydetme / sürükle-bırak ──
        self._init_undo()
        self._init_autosave()
        self._init_thumbs()
        self.table.setIconSize(QSize(THUMB_W, THUMB_H))
        self.table.verticalHeader().setDefaultSectionSize(THUMB_H + 6)
        self.setAcceptDrops(True)

        # Kısayollar
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo_action)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self.redo_action)
        QShortcut(QKeySequence("Ctrl+Y"),        self, activated=self.redo_action)
        QShortcut(QKeySequence("Ctrl+S"),        self, activated=self.save_m3u)

        self._apply_theme()
        # Açılışta kurtarılmamış çalışma varsa sor (pencere göründükten sonra)
        QTimer.singleShot(400, self._offer_recovery)
        QTimer.singleShot(900, self._load_visible_thumbs)
        if TMDB_API_KEY == "BURAYA_API_KEYINIZI_YAZIN":
            self.statusBar().showMessage(
                "⚠️  TMDB API Key girilmedi!  →  '🔑 API Key' butonuna tıklayın.", 12000
            )

    # ── Canlı İstatistik Paneli ───────────────────────────────────────
    def _schedule_stats(self, *_args):
        """Debounce: çok sık tetiklenmeyi önlemek için timer'ı yeniden başlat."""
        self._stats_timer.start()

    def _update_stats(self):
        """Statusbar sağ tarafındaki canlı sayaçları günceller."""
        total   = self.table.rowCount()
        visible = sum(1 for r in range(total) if not self.table.isRowHidden(r))
        checked = sum(
            1 for r in range(total)
            if self.table.item(r, COL_CHECK)
            and self.table.item(r, COL_CHECK).checkState() == Qt.CheckState.Checked
        )
        tmdb_ok = sum(1 for r in range(total) if self._tmdb_state(r) == "ok")
        tmdb_no = sum(1 for r in range(total) if self._tmdb_state(r) == "fail")
        tmdb_dbt = sum(1 for r in range(total) if self._tmdb_state(r) == "doubt")
        self._stat_total.setText(f"Toplam: {total}")
        vis_style = "padding:0 8px;font-size:12px;color:#ffcc00;font-weight:bold" if visible < total else "padding:0 8px;font-size:12px"
        self._stat_visible.setText(f"Görünür: {visible}")
        self._stat_visible.setStyleSheet(vis_style)
        chk_style = "padding:0 8px;font-size:12px;color:#4fc3f7;font-weight:bold" if checked > 0 else "padding:0 8px;font-size:12px"
        self._stat_checked.setText(f"Tikli: {checked}")
        self._stat_checked.setStyleSheet(chk_style)
        self._stat_tmdb_ok.setText(f"TMDB ✅: {tmdb_ok}")
        self._stat_tmdb_no.setText(f"❌: {tmdb_no}  ⚠️: {tmdb_dbt}")
        self._stat_tmdb_no.setToolTip("❌ TMDB'de bulunamadı   ⚠️ şüpheli (uygulanmadı)")
        uaref_n = sum(1 for r in range(total) if any(self._row_uaref(r)))
        self._stat_uaref.setText(f"UA/Ref: {uaref_n}")

    # ── Klavye kısayolları ────────────────────────────────────────────
    # ══════════════════════════════════════════════════════════════════
    #  TABLO POSTER ÖNİZLEMESİ
    #  Logo hücresine küçük görsel koyar. Sadece EKRANDA GÖRÜNEN satırlar
    #  indirilir (3000 satırlık listede 3000 istek atmamak için).
    # ══════════════════════════════════════════════════════════════════
    def _init_thumbs(self):
        self._thumbs_on   = True          # varsayılan: açık
        self._thumb_cache = {}            # url -> QPixmap (bellek içi)
        self._thumb_loader = None
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(400)   # kaydırma bitince yükle
        self._thumb_timer.timeout.connect(self._load_visible_thumbs)
        # Kaydırma / boyut değişince tetikle
        self.table.verticalScrollBar().valueChanged.connect(
            lambda *_: self._thumb_timer.start())
        self.table.model().rowsInserted.connect(lambda *_: self._thumb_timer.start())

    def _visible_row_range(self):
        """Görünen ilk ve son satır (biraz tamponla)."""
        vp = self.table.viewport()
        first = self.table.rowAt(0)
        last  = self.table.rowAt(vp.height() - 1)
        if first < 0:
            first = 0
        if last < 0:
            last = min(self.table.rowCount() - 1, first + 40)
        return max(0, first - 5), min(self.table.rowCount() - 1, last + 5)

    def _load_visible_thumbs(self):
        """Görünen satırların posterlerini yükle (önce önbellek, sonra indir)."""
        if not getattr(self, "_thumbs_on", False):
            return
        if self.table.rowCount() == 0:
            return
        first, last = self._visible_row_range()
        need = []
        for row in range(first, last + 1):
            if self.table.isRowHidden(row):
                continue
            url = (self._get_cell(row, COL_LOGO) or "").strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                continue
            px = self._thumb_cache.get(url)
            if px is not None:
                self._apply_thumb_to_row(row, px)
            elif url not in need:
                need.append(url)
        if not need:
            return
        # Önceki yükleyici hâlâ çalışıyorsa durdur
        if self._thumb_loader is not None and self._thumb_loader.isRunning():
            self._thumb_loader.stop()
        self._thumb_loader = _ThumbLoader(need, self)
        self._thumb_loader.ready.connect(self._on_thumb_ready)
        self._thumb_loader.start()

    def _on_thumb_ready(self, url, px):
        self._thumb_cache[url] = px
        # Bu URL'ye sahip TÜM satırlara uygula (aynı poster tekrarlanabilir)
        for row in range(self.table.rowCount()):
            if (self._get_cell(row, COL_LOGO) or "").strip() == url:
                self._apply_thumb_to_row(row, px)

    def _apply_thumb_to_row(self, row, px):
        it = self.table.item(row, COL_LOGO)
        if it is None:
            return
        it.setIcon(QIcon(px))

    def _clear_all_thumbs(self):
        for row in range(self.table.rowCount()):
            it = self.table.item(row, COL_LOGO)
            if it is not None:
                it.setIcon(QIcon())

    def _set_row_height(self, px):
        """
        Satır yüksekliğini değiştirir ve kaydırma aralığını YENİDEN HESAPLATIR.

        BUG (v24 ve öncesi): Sadece setDefaultSectionSize() çağrılıyordu.
        Tablo aşağı kaydırılmış durumdayken satırlar kısalınca toplam içerik
        yüksekliği düşer, ama Qt (ScrollPerItem modunda) kaydırma çubuğunun
        maksimumunu kendiliğinden güncellemez. Kaydırma konumu listenin
        sonunun ötesinde kalır ve TABLO TAMAMEN BOŞ görünür.

        Ölçüm (3000 satır, en alta kaydırılmış):
            dss 66 -> 24 :  sb.value=2991  sb.max=2991  rowAt(0) = -1  (BOŞ EKRAN)
            updateGeometries() sonrası: sb.max=2976  rowAt(0)=2975    (DÜZGÜN)

        Çözüm: yüksekliği değiştirdikten sonra updateGeometries() ile kaydırma
        aralığını tazele, sonra konumu geçerli aralığa kırp ve o an ekranda olan
        satırı koru (kullanıcı listede yerini kaybetmesin).
        """
        t  = self.table
        vh = t.verticalHeader()
        if vh.defaultSectionSize() == px:
            return
        # Değişimden önce ekranın en üstündeki satırı hatırla
        anchor = t.rowAt(0)
        if anchor < 0:
            anchor = t.currentRow()

        vh.setDefaultSectionSize(px)
        # Tek tek ayarlanmış (örn. resizeRowsToContents kalıntısı) satırları da hizala
        t.updateGeometries()          # ← kritik: kaydırma aralığını yeniden hesapla

        sb = t.verticalScrollBar()
        if anchor is not None and anchor >= 0:
            t.scrollToItem(t.item(anchor, COL_NAME) or t.item(anchor, COL_CHECK),
                           QAbstractItemView.ScrollHint.PositionAtTop)
        # Yine de aralık dışında kaldıysa güvenli sınıra çek
        if sb.value() > sb.maximum():
            sb.setValue(sb.maximum())
        t.viewport().update()

    def toggle_thumbs(self):
        """Poster önizlemesini aç/kapat."""
        self._thumbs_on = not getattr(self, "_thumbs_on", True)
        if self._thumbs_on:
            self.table.setIconSize(QSize(THUMB_W, THUMB_H))
            self._set_row_height(THUMB_H + 6)
            self._load_visible_thumbs()
            self.statusBar().showMessage("🖼️ Poster önizlemesi AÇIK", 4000)
        else:
            self._clear_all_thumbs()
            self._set_row_height(24)
            self.statusBar().showMessage(
                "🖼️ Poster önizlemesi KAPALI  (liste görünmeye devam eder)", 4000)

    def clear_thumb_cache(self):
        """Diskteki poster önbelleğini temizler."""
        n = 0
        try:
            if os.path.isdir(THUMB_DIR):
                for f in os.listdir(THUMB_DIR):
                    try:
                        os.remove(os.path.join(THUMB_DIR, f)); n += 1
                    except Exception:
                        pass
        except Exception:
            pass
        self._thumb_cache.clear()
        self._clear_all_thumbs()
        self.statusBar().showMessage(f"🧹 {n} önbellek görseli silindi.", 5000)
        if getattr(self, "_thumbs_on", False):
            self._load_visible_thumbs()

    # ══════════════════════════════════════════════════════════════════
    #  GERİ ALMA / YİNELEME  (Ctrl+Z / Ctrl+Y)
    #  Yıkıcı işlemden ÖNCE _snapshot("etiket") çağrılır.
    # ══════════════════════════════════════════════════════════════════
    UNDO_LIMIT = 25

    def _init_undo(self):
        self._undo_stack  = []
        self._redo_stack  = []
        self._undo_busy   = False   # geri alma sırasında yeni snapshot alma
        self._edit_cache  = None    # (row, col, eski_metin) — elle düzenleme takibi

    def _grab_table(self):
        """Tablonun tamamını hafif bir listeye kopyalar."""
        rows = []
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, COL_CHECK)
            checked = bool(chk and chk.checkState() == Qt.CheckState.Checked)
            cells = [self._get_cell(r, c) for c in
                     (COL_LOGO, COL_GROUP, COL_NAME, COL_YEAR, COL_TMDBID,
                      COL_URL, COL_UA, COL_REF, COL_STATUS)]
            # TMDB kalıcı işareti de saklanmalı; yoksa geri alma sonrası
            # "Bulamayanlar" filtresi yanlış sonuç verir.
            tit = self.table.item(r, COL_TMDBID)
            mark = tit.data(Qt.ItemDataRole.UserRole) if tit is not None else None
            rows.append((checked, cells, mark))
        return rows

    def _restore_table(self, rows):
        """_grab_table çıktısını tabloya geri yazar."""
        self._undo_busy = True
        try:
            was_sorting = self.table.isSortingEnabled()
            self.table.setSortingEnabled(False)
            self.table.setRowCount(0)
            for rec in rows:
                checked, cells = rec[0], rec[1]
                mark = rec[2] if len(rec) > 2 else None
                r = self.table.rowCount()
                self.table.insertRow(r)
                item = self._make_check_item()
                item.setCheckState(Qt.CheckState.Checked if checked
                                   else Qt.CheckState.Unchecked)
                self.table.setItem(r, COL_CHECK, item)
                cols = (COL_LOGO, COL_GROUP, COL_NAME, COL_YEAR,
                        COL_TMDBID, COL_URL, COL_UA, COL_REF, COL_STATUS)
                # Eski kayıtlarda (7 hücre) UA/REF yok — uyumluluk
                if len(cells) == 7:
                    cells = cells[:6] + ["", ""] + cells[6:]
                for col, val in zip(cols, cells):
                    self.table.setItem(r, col, self._make_item(val))
                if mark in ("ok", "fail", "doubt"):
                    self.table.item(r, COL_TMDBID).setData(
                        Qt.ItemDataRole.UserRole, mark)
            self.table.setSortingEnabled(was_sorting)
        finally:
            self._undo_busy = False
        self._schedule_stats()

    def _snapshot(self, label):
        """Yıkıcı işlemden önce çağrılır — mevcut durumu yığına iter."""
        if getattr(self, "_undo_busy", False):
            return
        if not hasattr(self, "_undo_stack"):
            self._init_undo()
        self._undo_stack.append((label, self._grab_table()))
        if len(self._undo_stack) > self.UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._mark_dirty()

    def undo_action(self):
        if not getattr(self, "_undo_stack", None):
            self.statusBar().showMessage("↶ Geri alınacak işlem yok.", 3000)
            return
        label, rows = self._undo_stack.pop()
        self._redo_stack.append((label, self._grab_table()))
        self._restore_table(rows)
        self._mark_dirty()
        self.statusBar().showMessage(f"↶ Geri alındı: {label}", 4000)

    # ── Elle hücre düzenlemesini geri almaya kaydet ─────────────────────
    # DÜZELTME (v26): Önceden yalnızca fonksiyon başında açıkça
    # _snapshot() çağrılan toplu işlemler (Toplu Düzenle, Bul&Değiştir…)
    # geri alınabiliyordu. Kullanıcı bir hücreye DOĞRUDAN yazı yazdığında
    # hiçbir snapshot alınmıyordu — bu yüzden "Geri Al" tuşu o değişikliği
    # atlıyor, ekranda hep en son (değiştirilmiş) veri kalıyordu ve
    # sanki geri alma hiç çalışmıyormuş gibi görünüyordu. Şimdi hücreye
    # girerken eski metni hafif bir önbelleğe alıyoruz; metin gerçekten
    # değiştiğinde bu bilgiyle doğru "önceki durum" anlık görüntüsü
    # oluşturup yığına ekliyoruz.
    _UNDO_COL_ORDER = (COL_LOGO, COL_GROUP, COL_NAME, COL_YEAR, COL_TMDBID, COL_URL, COL_UA, COL_REF, COL_STATUS)

    def _cache_current_cell(self, cur_row, cur_col, _prev_row=-1, _prev_col=-1):
        if getattr(self, "_undo_busy", False) or cur_row < 0 or cur_col in (-1, COL_CHECK):
            self._edit_cache = None
            return
        self._edit_cache = (cur_row, cur_col, self._get_cell(cur_row, cur_col))

    def _on_item_changed_for_undo(self, item):
        if getattr(self, "_undo_busy", False):
            return
        row, col = item.row(), item.column()
        if col == COL_CHECK:
            return
        cache = self._edit_cache
        if not cache or cache[0] != row or cache[1] != col:
            # Bu değişiklik başka bir fonksiyon tarafından programatik
            # olarak yapıldı — o fonksiyon kendi _snapshot()'ını zaten
            # aldı (veya _undo_busy ile bilinçli olarak bastırdı).
            return
        _, _, old_text = cache
        new_text = item.text()
        if new_text == old_text:
            return
        after  = self._grab_table()
        before = [(rec[0], list(rec[1])) + tuple(rec[2:]) for rec in after]
        try:
            idx = self._UNDO_COL_ORDER.index(col)
            before[row][1][idx] = old_text
        except ValueError:
            pass  # sütun snapshot'a dahil değilse (yalnızca metin geri yüklenemez)
        if not hasattr(self, "_undo_stack"):
            self._init_undo()
        self._undo_stack.append(("Hücre düzenle", before))
        if len(self._undo_stack) > self.UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._edit_cache = (row, col, new_text)

    def redo_action(self):
        if not getattr(self, "_redo_stack", None):
            self.statusBar().showMessage("↷ Yinelenecek işlem yok.", 3000)
            return
        label, rows = self._redo_stack.pop()
        self._undo_stack.append((label, self._grab_table()))
        self._restore_table(rows)
        self._mark_dirty()
        self.statusBar().showMessage(f"↷ Yinelendi: {label}", 4000)

    # ══════════════════════════════════════════════════════════════════
    #  OTOMATİK KAYDETME + KURTARMA
    #  60 sn'de bir geçici dosyaya yazar; çökme/kaza sonrası geri yükler.
    # ══════════════════════════════════════════════════════════════════
    AUTOSAVE_INTERVAL_MS = 60_000

    def _autosave_path(self):
        return os.path.join(tempfile.gettempdir(), "m3u_editor_autosave.json")

    def _init_autosave(self):
        self._dirty = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(self.AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._do_autosave)
        self._autosave_timer.start()
        # tablo her değiştiğinde "kaydedilmemiş" işaretle
        self.table.itemChanged.connect(self._mark_dirty)
        self.table.model().rowsInserted.connect(self._mark_dirty)
        self.table.model().rowsRemoved.connect(self._mark_dirty)
        # Elle hücre düzenlemelerini geri alma geçmişine yakala (bkz. yukarıdaki not)
        self.table.currentCellChanged.connect(self._cache_current_cell)
        self.table.itemChanged.connect(self._on_item_changed_for_undo)

    def _mark_dirty(self, *_a):
        self._dirty = True

    def _do_autosave(self):
        if not getattr(self, "_dirty", False):
            return
        if self.table.rowCount() == 0:
            return
        try:
            data = {
                "saved_at": time.time(),
                "count": self.table.rowCount(),
                "rows": [
                    {"checked": rec[0], "cells": rec[1],
                     "mark": (rec[2] if len(rec) > 2 else None),
                     "uaref": (list(rec[3]) if len(rec) > 3 and rec[3] else None)}
                    for rec in self._grab_table()
                ],
            }
            tmp = self._autosave_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._autosave_path())   # atomik yazma
            self._dirty = False
            self.statusBar().showMessage(
                f"💾 Otomatik kaydedildi ({data['count']} satır)", 2500)
        except Exception as e:
            self.statusBar().showMessage(f"⚠️ Otomatik kayıt başarısız: {e}", 4000)

    def _offer_recovery(self):
        """Açılışta kurtarılmamış çalışma varsa sorar."""
        path = self._autosave_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("rows") or []
            if not rows:
                return
            when = time.strftime("%d.%m.%Y %H:%M",
                                 time.localtime(data.get("saved_at", 0)))
            reply = QMessageBox.question(
                self, "Kurtarma",
                f"Kaydedilmemiş çalışma bulundu:\n\n"
                f"  {len(rows)} satır  —  {when}\n\n"
                f"Geri yüklensin mi?\n"
                f"(Hayır derseniz bu yedek silinir.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._restore_table([(r.get("checked", False), r.get("cells", []),
                                      r.get("mark"), r.get("uaref"))
                                     for r in rows])
                self.statusBar().showMessage(
                    f"✅ {len(rows)} satır geri yüklendi.", 6000)
            else:
                self._clear_autosave()
        except Exception as e:
            self.statusBar().showMessage(f"⚠️ Kurtarma okunamadı: {e}", 5000)

    def _clear_autosave(self):
        try:
            if os.path.exists(self._autosave_path()):
                os.remove(self._autosave_path())
        except Exception:
            pass

    def closeEvent(self, event):
        """Kaydedilmemiş değişiklik varsa uyar."""
        self._save_settings()
        if getattr(self, "_dirty", False) and self.table.rowCount() > 0:
            reply = QMessageBox.question(
                self, "Çıkış",
                "Kaydedilmemiş değişiklikler var.\n\n"
                "Yine de çıkmak istiyor musunuz?\n"
                "(Otomatik yedek saklanır, açılışta geri yükleyebilirsiniz.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._do_autosave()   # çıkmadan önce son hali yaz
        else:
            self._clear_autosave()
        event.accept()

    # ══════════════════════════════════════════════════════════════════
    #  SÜRÜKLE-BIRAK ile dosya açma
    # ══════════════════════════════════════════════════════════════════
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() and any(
                u.toLocalFile().lower().endswith((".m3u", ".m3u8", ".txt"))
                for u in md.urls()):
            event.acceptProposedAction()
            self.statusBar().showMessage(
                "📥 Bırakın — dosya yüklenecek (Shift basılıysa listeye eklenir)", 4000)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.toLocalFile().lower().endswith((".m3u", ".m3u8", ".txt"))]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        # Shift basılıysa mevcut listeye EKLE, değilse DEĞİŞTİR
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if not shift and self.table.rowCount() > 0:
            reply = QMessageBox.question(
                self, "Dosya Bırakıldı",
                f"{len(paths)} dosya bırakıldı.\n\n"
                f"EVET → mevcut listeyi değiştir\n"
                f"HAYIR → listenin en üstüne ekle",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            append_mode = (reply == QMessageBox.StandardButton.No)
        else:
            append_mode = shift

        self._snapshot("Dosya bırak")
        loaded = 0
        for i, fp in enumerate(paths):
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                QMessageBox.warning(self, "Hata", f"{os.path.basename(fp)} okunamadı:\n{e}")
                continue
            # ilk dosya modu belirler, sonrakiler her zaman ekleme
            self.process_m3u_content(content, append=(append_mode or i > 0))
            loaded += 1
        if loaded:
            self.statusBar().showMessage(
                f"📥 {loaded} dosya yüklendi ({'eklendi' if append_mode else 'değiştirildi'})", 6000)

    def keyPressEvent(self, event):
        """Alt+↑/↓ = bir adım taşı  |  Alt+Home/End = en üste/alta taşı"""
        mod = event.modifiers()
        key = event.key()
        if mod == Qt.KeyboardModifier.AltModifier:
            if key == Qt.Key.Key_Up:
                self.move_rows_up();     return
            if key == Qt.Key.Key_Down:
                self.move_rows_down();   return
            if key == Qt.Key.Key_Home:
                self.move_rows_top();    return
            if key == Qt.Key.Key_End:
                self.move_rows_bottom(); return
        super().keyPressEvent(event)

    # ── Tema ──────────────────────────────────────────────────────────
    def _apply_theme(self):
        fs = max(9, min(22, int(getattr(self, "_ui_font_pt", 12) or 12)))
        if self.is_dark_mode:
            self.setStyleSheet(f"""
                QMainWindow,QDialog{{background:#2b2b2b;color:white;font-size:{fs}px}}
                QMenuBar{{background:#2b2b2b;color:white;font-size:{fs}px}}
                QMenuBar::item{{background:transparent;color:white;padding:4px 10px}}
                QMenuBar::item:selected{{background:#0561a8;color:white}}
                QMenu{{background:#2b2b2b;color:white;font-size:{fs}px;border:1px solid #555}}
                QMenu::item{{color:white;padding:5px 18px}}
                QMenu::item:selected{{background:#0561a8;color:white}}
                QMenu::separator{{height:1px;background:#555}}
                QTableWidget{{background:#3c3f41;color:white;gridline-color:#555;font-size:{fs}px}}
                QHeaderView::section{{background:#4e5254;color:white;border:1px solid #333;font-size:{fs}px}}
                QLineEdit,QComboBox{{background:#3c3f41;color:white;border:1px solid #555;padding:3px 5px;border-radius:3px;font-size:{fs}px}}
                QComboBox QAbstractItemView{{background:#3c3f41;color:white;selection-background-color:#0561a8}}
                QStatusBar{{background:#323232;color:#ffcc00;font-size:{fs}px}}
                QTextEdit{{background:#2b2b2b;color:#ddd;border:1px solid #555;font-size:{fs}px}}
                QToolBar{{background:#2b2b2b;border:none;spacing:4px;padding:2px;font-size:{fs}px}}
                QToolBar QToolButton{{
                    background:#3a3a3a;color:#f0f0f0;
                    border:1px solid #555;border-radius:3px;
                    padding:4px 10px;margin:1px;font-size:{fs}px
                }}
                QToolBar QToolButton:hover{{background:#505050;color:white;border-color:#777}}
                QToolBar QToolButton:pressed{{background:#222;color:white}}
                QWidget#ribbonPanel{{background:#2b2b2b;border-bottom:1px solid #444}}
                QPushButton{{background:#3a3a3a;color:#eee;border:1px solid #555;border-radius:3px;padding:3px 8px;font-size:{fs}px}}
                QPushButton:hover{{background:#505050;color:white}}
                QTableWidget::item:selected{{background:#0561a8;color:white}}
                QCheckBox,QLabel,QRadioButton{{color:white;font-size:{fs}px}}
                QScrollBar:vertical{{background:#2b2b2b;width:12px}}
                QScrollBar::handle:vertical{{background:#555;border-radius:4px;min-height:24px}}
            """)
        else:
            # Açık (beyaz) tema — Windows'ta metin/arka plan kontrastı özellikle önemli
            self.setStyleSheet(f"""
                QMainWindow,QDialog{{background:#f0f0f0;color:#111;font-size:{fs}px}}
                QMenuBar{{background:#f0f0f0;color:#111;font-size:{fs}px}}
                QMenuBar::item{{background:transparent;color:#111;padding:4px 10px}}
                QMenuBar::item:selected{{background:#d0e3ff;color:#111}}
                QMenu{{background:#ffffff;color:#111;font-size:{fs}px;border:1px solid #bbb}}
                QMenu::item{{color:#111;padding:5px 18px}}
                QMenu::item:selected{{background:#d0e3ff;color:#111}}
                QMenu::separator{{height:1px;background:#ddd}}
                QTableWidget{{background:#ffffff;color:#111;gridline-color:#ddd;font-size:{fs}px}}
                QHeaderView::section{{background:#e8e8e8;color:#111;border:1px solid #ccc;font-size:{fs}px;padding:3px}}
                QLineEdit,QComboBox{{background:#ffffff;color:#111;border:1px solid #aaa;padding:3px 5px;border-radius:3px;font-size:{fs}px}}
                QComboBox QAbstractItemView{{background:#ffffff;color:#111;selection-background-color:#cde4ff}}
                QStatusBar{{background:#ececec;color:#a00;font-size:{fs}px}}
                QTextEdit{{background:#ffffff;color:#222;border:1px solid #bbb;font-size:{fs}px}}
                QToolBar{{background:#ececec;border:none;border-bottom:1px solid #ccc;spacing:4px;padding:3px;font-size:{fs}px}}
                QToolBar QToolButton{{
                    background:#ffffff;color:#111;
                    border:1px solid #bbb;border-radius:3px;
                    padding:4px 10px;margin:1px;font-size:{fs}px
                }}
                QToolBar QToolButton:hover{{background:#e8f0fe;color:#0b57d0;border-color:#8ab4f8}}
                QToolBar QToolButton:pressed{{background:#d2e3fc;color:#0b57d0}}
                QWidget#ribbonPanel{{background:#f5f5f5;border-bottom:1px solid #ccc}}
                QPushButton{{background:#ffffff;color:#111;border:1px solid #bbb;border-radius:3px;padding:3px 8px;font-size:{fs}px}}
                QPushButton:hover{{background:#e8f0fe;color:#0b57d0;border-color:#8ab4f8}}
                QTableWidget::item:selected{{background:#cde4ff;color:#111}}
                QCheckBox,QLabel,QRadioButton{{color:#111;font-size:{fs}px}}
                QScrollBar:vertical{{background:#f0f0f0;width:12px}}
                QScrollBar::handle:vertical{{background:#c0c0c0;border-radius:4px;min-height:24px}}
            """)
        if not getattr(self, "_thumbs_on", False):
            try:
                self._set_row_height(max(22, fs + 10))
            except Exception:
                pass

    # ── Araç paneli (2 satır ribbon; arama 2. satırda) ────────────────
    def _build_toolbar(self):
        """Klasik menü çubuğu (Dosya / Düzenle / …) + ince arama satırı."""
        from PyQt6.QtGui import QAction, QKeySequence

        def add_act(menu, text, slot, shortcut=None):
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(slot)
            menu.addAction(act)
            return act

        mb = self.menuBar()

        # ── Dosya ────────────────────────────────────────────────────
        m_file = mb.addMenu("Dosya")
        add_act(m_file, "Yeni Pencere (çoklu liste)", self.open_new_window, "Ctrl+N")
        add_act(m_file, "Aç…", self.open_m3u, "Ctrl+O")
        add_act(m_file, "Listeye Ekle…", self.append_m3u)
        add_act(m_file, "URL'den Aç…", self.import_from_url)
        add_act(m_file, "URL'den Ekle…", self.append_from_url)
        m_file.addSeparator()
        add_act(m_file, "Kaydet…", self.save_m3u, "Ctrl+S")
        m_file.addSeparator()
        add_act(m_file, "Seçilileri Dışa Aktar…", self.export_selected)
        add_act(m_file, "Klasöre Göre Dışa Aktar…", self.export_by_group)

        # ── Düzenle ──────────────────────────────────────────────────
        m_edit = mb.addMenu("Düzenle")
        add_act(m_edit, "Geri Al", self.undo_action, "Ctrl+Z")
        add_act(m_edit, "Yinele", self.redo_action, "Ctrl+Y")
        m_edit.addSeparator()
        add_act(m_edit, "Yeni Satır (üste)", self.add_new_row_top)
        add_act(m_edit, "Oto Ekle (URL → Ad → TMDB)…", self.quick_add_entry, "Ctrl+Shift+A")
        add_act(m_edit, "Seçili Satırı Sil", self.delete_row, "Delete")
        m_edit.addSeparator()
        add_act(m_edit, "Toplu Düzenle…", self.bulk_edit)
        add_act(m_edit, "UA / Referrer Şablonları…", self.ua_templates_dialog)
        add_act(m_edit, "Bul / Değiştir…", self.find_replace, "Ctrl+F")
        m_edit.addSeparator()
        add_act(m_edit, "Aynı Adı Sil (tekrarlar)", self.delete_duplicates)
        add_act(m_edit, "Aynı URL'yi Sil", self.delete_duplicate_urls)
        add_act(m_edit, "Sezon/Bölüm Biçimini Düzelt (S01E01)", self.normalize_episodes)
        m_edit.addSeparator()
        add_act(m_edit, "Yukarı Taşı", self.move_rows_up, "Alt+Up")
        add_act(m_edit, "Aşağı Taşı", self.move_rows_down, "Alt+Down")
        add_act(m_edit, "En Üste Taşı", self.move_rows_top)
        add_act(m_edit, "En Alta Taşı", self.move_rows_bottom)

        # ── Link ─────────────────────────────────────────────────────
        m_link = mb.addMenu("Link")
        add_act(m_link, "Linkleri Kontrol Et", self.check_links)
        add_act(m_link, "Kontrole Devam Et", self.resume_check_links)
        add_act(m_link, "Kontrolü Durdur", self.stop_check)
        m_link.addSeparator()
        add_act(m_link, "VLC ile Önizle", self.vlc_preview, "F5")
        add_act(m_link, "VLC Kısa Test (8 sn)", self.vlc_quick_test)
        m_link.addSeparator()
        add_act(m_link, "Paralel Bağlantı Ayarı…", self.set_link_workers)
        add_act(m_link, "Ölü Linkleri Sil", self.delete_dead_links)
        add_act(m_link, "Şüphelileri Sil", self.delete_doubtful_rows)
        add_act(m_link, "Sadece Ölüleri Göster", self.show_only_dead)
        add_act(m_link, "Sadece Şüphelileri Göster", self.show_only_link_doubtful)
        add_act(m_link, "Tüm Satırları Göster", self.show_all_rows)

        # ── TMDB ─────────────────────────────────────────────────────
        m_tmdb = mb.addMenu("TMDB")
        add_act(m_tmdb, "Seçili Satırda Ara…", self.tmdb_search_selected)
        add_act(m_tmdb, "Tikli Satırlarda Toplu Ara…", self.tmdb_search_bulk)
        add_act(m_tmdb, "Tümünü Otomatik Doldur…", self.tmdb_auto_all)
        m_tmdb.addSeparator()
        add_act(m_tmdb, "Eksik Posterleri Toplu Çek", self.bulk_fetch_posters)
        add_act(m_tmdb, "Seçili Dizinin Bölümlerini Ekle (S01E01…)", self.tmdb_expand_episodes)
        add_act(m_tmdb, "Özet / Oyuncu Getir", self.fetch_overview_cast)
        m_tmdb.addSeparator()
        add_act(m_tmdb, "Şüpheli Eşleşmeleri Onayla…", self.review_doubtful)
        add_act(m_tmdb, "Sadece Şüphelileri Göster", self.show_only_doubtful)
        add_act(m_tmdb, "Şüpheli TMDB Satırlarını Sil", self.delete_tmdb_doubtful)
        add_act(m_tmdb, "Bulamayanları Göster", self.show_only_not_found)
        add_act(m_tmdb, "Tüm Satırları Göster", self.show_all_rows)
        m_tmdb.addSeparator()
        add_act(m_tmdb, "Güncelleme Seçenekleri…", self.tmdb_options_dialog)
        add_act(m_tmdb, "API Key…", self.set_api_key)
        add_act(m_tmdb, "API Test", self.test_api_key)

        # ── Görünüm ──────────────────────────────────────────────────
        m_view = mb.addMenu("Görünüm")
        add_act(m_view, "Poster Önizleme Aç/Kapa", self.toggle_thumbs)
        add_act(m_view, "Poster Önbelleğini Temizle", self.clear_thumb_cache)
        m_view.addSeparator()
        self._act_uaref = QAction("User-Agent / Referrer Sütunları", self)
        self._act_uaref.setCheckable(True)
        self._act_uaref.setChecked(False)
        self._act_uaref.triggered.connect(
            lambda checked: self._set_uaref_visible(checked, save=True))
        m_view.addAction(self._act_uaref)
        m_view.addSeparator()
        add_act(m_view, "Açık / Koyu Tema", self._toggle_theme)
        add_act(m_view, "Yazı Boyutu…", self.set_font_size)
        add_act(m_view, "Özet Paneli Aç/Kapa", self.toggle_info_panel)

        # ── Hızlı erişim çubuğu (Görünüm'ün sağında / menü altında) ───
        tb = QToolBar("Hızlı erişim")
        tb.setObjectName("quickToolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setIconSize(QSize(16, 16))
        try:
            tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        except Exception:
            pass

        def tb_act(label, slot, tip=""):
            a = QAction(label, self)
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
            tb.addAction(a)
            return a

        tb_act("Aç", self.open_m3u, "M3U dosyası aç")
        tb_act("Listeye Ekle", self.append_m3u, "Mevcut listeye M3U ekle")
        tb_act("Kaydet", self.save_m3u, "Kaydet")
        tb.addSeparator()
        tb_act("Oto Ekle", self.quick_add_entry, "URL → film/dizi adı → TMDB (Ctrl+Shift+A)")
        tb_act("Yeni Satır", self.add_new_row_top, "Üste yeni satır ekle")
        tb_act("Toplu Düzenle", self.bulk_edit, "Seçili/görünür satırları toplu düzenle")
        tb_act("Bul / Değiştir", self.find_replace, "Bul ve değiştir")
        tb_act("Aynı URL Sil", self.delete_duplicate_urls, "Aynı URL'li tekrarları sil")
        tb.addSeparator()
        tb_act("TMDB Doldur", self.tmdb_auto_all, "Tümünü TMDB ile otomatik doldur")
        tb.addSeparator()
        tb_act("VLC", self.vlc_preview, "VLC ile önizle (F5)")
        tb_act("VLC Test", self.vlc_quick_test, "Kısa VLC testi")
        tb_act("Posterler", self.bulk_fetch_posters, "Eksik posterleri çek")

        self.addToolBar(tb)
        self._quick_toolbar = tb

        # ── İnce arama satırı ─────────────────────────────────────────
        search_row = QWidget()
        search_row.setObjectName("ribbonPanel")
        search_row.setFixedHeight(32)
        sh = QHBoxLayout(search_row)
        sh.setContentsMargins(6, 2, 6, 2)
        sh.setSpacing(6)

        self._search_lbl = QLabel("Ara:")
        self.search_bar = QLineEdit()
        self.search_bar.setFixedHeight(26)
        self.search_bar.setPlaceholderText("Ad, tür, yıl, TMDB ID, URL…")
        self.search_bar.textChanged.connect(self.filter_table)
        self._search_clear = QPushButton("Temizle")
        self._search_clear.setFixedHeight(26)
        self._search_clear.clicked.connect(self.search_bar.clear)

        sh.addWidget(self._search_lbl)
        sh.addWidget(self.search_bar, 1)
        sh.addWidget(self._search_clear)

        self._ribbon_buttons = search_row
        self._ribbon_panel = search_row
        self.centralWidget().layout().insertWidget(0, search_row)

    # ══════════════════════════════════════════════════════════════════
    #  USER-AGENT / REFERRER
    #  • Üstteki 🌐 düğmesi paneli açar/kapatır.
    #  • "Kullan" kutusu özelliği açar/kapatır (link kontrolü + URL'den açma).
    #  • Satıra özel değerler M3U'ya #EXTVLCOPT satırı olarak yazılır/okunur.
    # ══════════════════════════════════════════════════════════════════
    def _settings_path(self):
        return os.path.join(os.path.expanduser("~"), ".m3u_editor_settings.json")

    def _load_settings(self):
        try:
            with open(self._settings_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_settings(self):
        if not hasattr(self, "ua_edit"):
            return
        try:
            data = {
                "ua":         self.ua_edit.text(),
                "ref":        self.ref_edit.text(),
                "enabled":    bool(self._ua_enabled),
                "menu_open":  bool(self._menu_visible),
                "uaref_open": bool(self._uaref_visible),
            }
            tmp = self._settings_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._settings_path())
        except Exception:
            pass   # ayar yazılamıyorsa program çalışmaya devam eder

    def _build_uaref_panel(self):
        """UA ayar durumu; sütun göster/gizle Görünüm menüsünden yönetilir."""
        st = self._load_settings()
        self._ua_enabled    = bool(st.get("enabled", False))
        self._menu_visible  = True
        self._uaref_visible = bool(st.get("uaref_open", False))

        self.ua_edit = QLineEdit(st.get("ua", DEFAULT_UA))
        self.ua_edit.hide()
        self.ref_edit = QLineEdit(st.get("ref", ""))
        self.ref_edit.hide()
        self.uaref_enable = QCheckBox()
        self.uaref_enable.setChecked(self._ua_enabled)
        self.uaref_enable.hide()
        self.uaref_body = QWidget()
        self.uaref_body.hide()
        self.uaref_toggle = QPushButton()
        self.uaref_toggle.hide()
        self.menu_toggle = QPushButton()
        self.menu_toggle.hide()
        self._top_strip = None

        if hasattr(self, "_act_uaref"):
            self._act_uaref.setChecked(self._uaref_visible)
        self._set_uaref_visible(self._uaref_visible, save=False)

    def _set_menu_visible(self, show, save=True):
        # Menü artık tek satır ve her zaman görünür
        self._menu_visible = True
        if hasattr(self, "_ribbon_panel") and self._ribbon_panel is not None:
            self._ribbon_panel.setVisible(True)
        if save:
            self._save_settings()

    def _set_uaref_visible(self, show, save=True):
        """User-Agent ve Referrer sütunlarını URL'nin yanında göster / gizle."""
        self._uaref_visible = bool(show)
        self._uaref_cols_visible = bool(show)
        if hasattr(self, "table") and self.table is not None:
            self.table.setColumnHidden(COL_UA, not show)
            self.table.setColumnHidden(COL_REF, not show)
        if hasattr(self, "uaref_body"):
            self.uaref_body.setVisible(False)
        if hasattr(self, "_act_uaref"):
            self._act_uaref.blockSignals(True)
            self._act_uaref.setChecked(bool(show))
            self._act_uaref.blockSignals(False)
        if save and hasattr(self, "_save_settings"):
            self._save_settings()

    def _update_uaref_button(self):
        if hasattr(self, "_act_uaref"):
            self._act_uaref.blockSignals(True)
            self._act_uaref.setChecked(bool(getattr(self, "_uaref_visible", False)))
            self._act_uaref.blockSignals(False)

    def _on_uaref_enable(self, on):
        self._ua_enabled = bool(on)
        self._update_uaref_button()
        self._save_settings()
        self.statusBar().showMessage(
            "🌐 User-Agent / Referrer AÇIK — link kontrolü ve URL'den açmada kullanılacak."
            if on else "🌐 User-Agent / Referrer kapalı.", 4000)

    def _on_ua_preset(self, idx):
        if idx > 0:
            self.ua_edit.setText(UA_PRESETS[idx - 1][1])
            self._save_settings()
        self.ua_preset.setCurrentIndex(0)

    # ── Genel değerler ────────────────────────────────────────────────
    def _cur_uaref(self):
        if not hasattr(self, "ua_edit"):
            return "", ""
        return _hdr(self.ua_edit.text()), _hdr(self.ref_edit.text())

    def _global_uaref_for_check(self):
        return self._cur_uaref() if self._ua_enabled else ("", "")

    def _uaref_for_check(self, row):
        return self._row_uaref(row) if self._ua_enabled else ("", "")

    def _request_headers(self):
        """URL'den liste indirirken kullanılacak header'lar ('Kullan' kapalıysa boş)."""
        if not self._ua_enabled:
            return {}
        ua, ref = self._cur_uaref()
        h = {}
        if ua:
            h["User-Agent"] = _latin1(ua)
        if ref:
            h["Referer"] = _latin1(ref)
        return h

    # ── Satıra özel değerler ──────────────────────────────────────────
    def _row_uaref(self, row):
        """Satırdaki User-Agent / Referrer değerlerini döndürür (sütunlardan)."""
        ua  = self._get_cell(row, COL_UA)
        ref = self._get_cell(row, COL_REF)
        return (ua or ""), (ref or "")

    def _refresh_uaref_tip(self, item):
        pass  # artık ayrı sütunlarda; tooltip gerekmez

    def _set_row_uaref(self, row, ua, ref):
        """User-Agent / Referrer değerlerini URL yanındaki sütunlara yazar."""
        ua  = _hdr(ua)
        ref = _hdr(ref)
        self._set_cell(row, COL_UA,  ua)
        self._set_cell(row, COL_REF, ref)

    def _uaref_rows(self, scope):
        n = self.table.rowCount()
        if scope == "checked":
            return self._checked_rows()
        if scope == "visible":
            return [r for r in range(n) if not self.table.isRowHidden(r)]
        return list(range(n))

    _SCOPE_LABEL = {"checked": "tikli", "visible": "görünür", "all": "tüm"}

    def apply_uaref(self, scope="visible"):
        ua, ref = self._cur_uaref()
        if not ua and not ref:
            QMessageBox.information(self, "Bilgi",
                "Önce User-Agent ve/veya Referrer alanını doldurun.")
            return
        rows = self._uaref_rows(scope)
        if not rows:
            QMessageBox.information(self, "Bilgi",
                "Uygulanacak satır yok." + (
                    "\n(Tikli satır seçilmemiş — ✔ sütunundan işaretleyin.)"
                    if scope == "checked" else ""))
            return
        self._snapshot(f"UA/Referrer uygula ({len(rows)} satır)")
        for r in rows:
            cur_ua, cur_ref = self._row_uaref(r)
            # Boş bırakılan alan, satırdaki mevcut değeri silmez
            self._set_row_uaref(r, ua or cur_ua, ref or cur_ref)
        self._schedule_stats()
        self.statusBar().showMessage(
            f"🌐 {len(rows)} {self._SCOPE_LABEL[scope]} satıra User-Agent / Referrer yazıldı "
            f"(M3U'ya #EXTVLCOPT olarak kaydedilir).", 6000)

    def clear_uaref(self, scope="visible"):
        rows = [r for r in self._uaref_rows(scope) if any(self._row_uaref(r))]
        if not rows:
            QMessageBox.information(self, "Bilgi",
                "Temizlenecek User-Agent / Referrer değeri olan satır yok.")
            return
        self._snapshot(f"UA/Referrer temizle ({len(rows)} satır)")
        for r in rows:
            self._set_row_uaref(r, "", "")
        self._schedule_stats()
        self.statusBar().showMessage(
            f"🧹 {len(rows)} satırdan User-Agent / Referrer temizlendi.", 5000)

    def edit_row_uaref(self):
        """Geçerli satırın User-Agent / Referrer değerini düzenler."""
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Bilgi", "Önce bir satır seçin.")
            return
        ua, ref = self._row_uaref(row)
        dlg = QDialog(self)
        dlg.setWindowTitle("Bu satırın User-Agent / Referrer değeri")
        dlg.setMinimumWidth(520)
        if self.is_dark_mode:
            dlg.setStyleSheet("QDialog,QWidget{background:#2b2b2b;color:white}"
                              "QLineEdit{background:#3c3f41;color:white;border:1px solid #555;padding:3px}"
                              "QPushButton{background:#444;color:white;border:1px solid #666;padding:6px 16px}")
        form = QFormLayout(dlg)
        e_ua  = QLineEdit(ua)
        e_ref = QLineEdit(ref)
        e_ua.setPlaceholderText("boş = bu satır için UA yazılmaz")
        e_ref.setPlaceholderText("boş = bu satır için Referrer yazılmaz")
        form.addRow("User-Agent:", e_ua)
        form.addRow("Referrer:",   e_ref)
        btns = QHBoxLayout()
        ok  = QPushButton("Kaydet"); ok.clicked.connect(dlg.accept)
        can = QPushButton("İptal");  can.clicked.connect(dlg.reject)
        btns.addStretch(); btns.addWidget(ok); btns.addWidget(can)
        form.addRow(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._snapshot("UA/Referrer düzenle")
            self._set_row_uaref(row, e_ua.text(), e_ref.text())
            self._schedule_stats()

    def _make_item(self, text=""):
        return QTableWidgetItem(str(text))

    def _make_check_item(self):
        """Checkbox görevi gören, ortada hizalanmış boş item."""
        it = QTableWidgetItem()
        it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        it.setCheckState(Qt.CheckState.Unchecked)
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return it

    def _on_header_section_clicked(self, col):
        """Sütun başlığına tıklanınca: ✔ sütunuysa tikleri değiştir,
        diğer sütunlarsa SADECE görünür satırları sırala."""
        if col == COL_CHECK:
            self._header_check_toggle(col)
        else:
            self._sort_by_column(col)

    def _sort_by_column(self, col):
        """Tıklanan sütuna göre YALNIZCA görünür (filtreye takılmamış)
        satırları kendi aralarında sıralar; gizli satırlar olduğu
        pozisyonda kalır ve içerikleri değişmez.

        DÜZELTME (v26): Qt'nin yerleşik sıralaması aktif filtreden
        bağımsız olarak tüm tabloyu sıralıyordu (örn. arama kutusuna bir
        dizi adı yazıp o satırları sıralamaya çalışınca programdaki TÜM
        içerikler karışıyordu). Artık filtre aktifken sıralama sadece
        arama sonucundaki satırları etkiliyor.
        """
        visible_rows = [r for r in range(self.table.rowCount())
                        if not self.table.isRowHidden(r)]
        if len(visible_rows) < 2:
            return

        ascending = not self._sort_order.get(col, False) if self._last_sort_col == col \
            else True
        self._sort_order[col] = ascending
        self._last_sort_col   = col

        def sort_key(r):
            val = (self._get_cell(r, col) or "").strip()
            try:
                return (0, float(val.replace(",", ".")))
            except ValueError:
                return (1, val.lower())

        rows_data = [self._read_row(r) for r in visible_rows]
        order = sorted(range(len(visible_rows)),
                        key=lambda i: sort_key(visible_rows[i]),
                        reverse=not ascending)

        self._snapshot("Sırala")
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self._undo_busy = True  # _write_row'un tetiklediği itemChanged ayrı snapshot almasın
        try:
            for slot, src_idx in zip(visible_rows, order):
                self._write_row(slot, rows_data[src_idx])
        finally:
            self._undo_busy = False
            self.table.setUpdatesEnabled(True)

        self.table.horizontalHeader().setSortIndicator(
            col, Qt.SortOrder.AscendingOrder if ascending else Qt.SortOrder.DescendingOrder)
        self.statusBar().showMessage(
            f"↕️ {len(visible_rows)} görünür satır sıralandı (filtre dışındakiler etkilenmedi).",
            4000)

    def _header_check_toggle(self, col):
        """✔ sütun başlığına tıklanınca tüm görünür satırları seç/kaldır."""
        if col != COL_CHECK:
            return
        self._all_checked = not self._all_checked
        state = Qt.CheckState.Checked if self._all_checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                it = self.table.item(row, COL_CHECK)
                if it:
                    it.setCheckState(state)
        checked = sum(
            1 for r in range(self.table.rowCount())
            if not self.table.isRowHidden(r)
            and self.table.item(r, COL_CHECK)
            and self.table.item(r, COL_CHECK).checkState() == Qt.CheckState.Checked
        )
        self.statusBar().showMessage(
            f"{'✅' if self._all_checked else '☐'} {checked} satır {'seçildi' if self._all_checked else 'seçim kaldırıldı'}.", 3000
        )

    def _checked_rows(self) -> list[int]:
        """Tik işaretli VE şu an görünür (filtreye takılmamış) satırların
        index listesini döner.

        DÜZELTME (v26): Önceden gizli (filtrelenmiş) satırlar da tikli
        sayılıyordu. Bu yüzden bir arama/filtre sonrası unutulmuş eski
        tikler, sonraki bir filtrede yapılan "Toplu Düzenle" / "TMDB
        Toplu Arama" gibi işlemlere sızıyor ve ilgisiz satırları da
        değiştiriyordu (örn. tek bir dizi filtrelenip düzenlenince tüm
        programların etkilenmesi). Artık gizli satırlar tikli olsa bile
        hedef listesine girmiyor.
        """
        return [
            r for r in range(self.table.rowCount())
            if not self.table.isRowHidden(r)
            and self.table.item(r, COL_CHECK)
            and self.table.item(r, COL_CHECK).checkState() == Qt.CheckState.Checked
        ]

    def _get_cell(self, row, col, default=""):
        if col == COL_CHECK:
            it = self.table.item(row, col)
            return "1" if (it and it.checkState() == Qt.CheckState.Checked) else "0"
        it = self.table.item(row, col)
        return it.text() if it else default

    # ══════════════════════════════════════════════════════════════════
    #  TMDB DURUMU — tek kaynak
    #  Durum sütununu link kontrolü de kullanıyor ("AKTİF ✅" vb.) ve
    #  TMDB'nin yazdığı "TMDB ✅" işaretini EZİYOR. Bu yüzden durumu
    #  yalnızca metinden okumak güvenilir değil; kalıcı bir işaret +
    #  gerçek veriye (tvg-id dolu mu) bakılır.
    # ══════════════════════════════════════════════════════════════════
    def _mark_tmdb(self, row, state):
        """Satıra kalıcı TMDB işareti koyar ('ok' / 'fail')."""
        it = self.table.item(row, COL_TMDBID)
        if it is None:
            it = self._make_item("")
            self.table.setItem(row, COL_TMDBID, it)
        it.setData(Qt.ItemDataRole.UserRole, state)

    def _tmdb_state(self, row):
        """'ok' | 'fail' | 'doubt' | 'none'  —  satırın TMDB eşleşme durumu."""
        # 1) Kalıcı işaret (link kontrolü bunu ezemez)
        it = self.table.item(row, COL_TMDBID)
        if it is not None:
            mark = it.data(Qt.ItemDataRole.UserRole)
            if mark in ("ok", "fail", "doubt"):
                return mark
        # 2) tvg-id dolu ise eşleşme vardır
        if (self._get_cell(row, COL_TMDBID) or "").strip():
            return "ok"
        # 3) Son çare: durum metni (henüz ezilmemişse)
        st = self._get_cell(row, COL_STATUS) or ""
        if "TMDB ✅" in st:
            return "ok"
        if "TMDB ❌" in st:
            return "fail"
        if "ŞÜPHELİ ⚠️ TMDB" in st:
            return "doubt"
        return "none"

    def _set_cell(self, row, col, text):
        it = self.table.item(row, col)
        if it:
            it.setText(str(text))
        else:
            self.table.setItem(row, col, self._make_item(str(text)))

    # ── Sütun boyutlandırma ──────────────────────────────────────────
    def _fix_last_col(self, col, old_size, new_size):
        """Sütun sürüklenince Durum sütununu kalan alana sığdır."""
        if self._fixing or col == COL_STATUS or col == COL_CHECK:
            return
        self._fixing = True
        vw = self.table.viewport().width()
        MIN_STATUS = 80
        other_sum = sum(self.table.columnWidth(c) for c in range(1, 7) if c != col)
        other_sum += self.table.columnWidth(COL_CHECK)
        max_w = vw - other_sum - MIN_STATUS
        if new_size > max_w:
            self.table.setColumnWidth(col, max(40, max_w))
            self.table.setColumnWidth(COL_STATUS, MIN_STATUS)
        else:
            self.table.setColumnWidth(COL_STATUS, max(MIN_STATUS, vw - other_sum - new_size))
        self._fixing = False

    def _fit_columns(self):
        """Açılışta / pencere boyutu değişince tüm sütunları orantılı dağıt."""
        vw = self.table.viewport().width()
        if vw < 100:
            return
        self._fixing = True
        for col, ratio in enumerate(self._col_ratios):
            if ratio is None:   # fixed (checkbox)
                self.table.setColumnWidth(col, 30)
            else:
                self.table.setColumnWidth(col, max(40, int(vw * ratio)))
        self._fixing = False

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_columns()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()

    # ── Tema ──────────────────────────────────────────────────────────
    def _toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self._apply_theme()

    # ── Tablo işlemleri ───────────────────────────────────────────────
    def add_new_row_top(self):
        """
        Seçili satırın ÜSTÜNE yeni satır ekler. Hiçbir satır seçili
        değilse (tablo boşsa ya da seçim yoksa) en üste (0. satır) ekler
        — eski davranış yalnızca bu durumda korunur.
        """
        self._snapshot("Yeni satır ekle")
        secili = self.table.currentRow()
        hedef = secili if secili >= 0 else 0
        self.table.setSortingEnabled(False)
        self.table.insertRow(hedef)
        self.table.setItem(hedef, COL_CHECK, self._make_check_item())
        for col in range(1, self.table.columnCount()):
            self.table.setItem(hedef, col, self._make_item("Yeni" if col == COL_STATUS else ""))
        # Not: setSortingEnabled(True) kasıtlı yapılmıyor — artık sıralama
        # başlık tıklamasında kendi _sort_by_column() fonksiyonumuzla
        # (sadece görünür satırlarda) yapılıyor.
        self.table.scrollToItem(self.table.item(hedef, COL_CHECK))
        self.table.setCurrentCell(hedef, 0)

    def delete_row(self):
        indices = self.table.selectionModel().selectedRows()
        if indices or self.table.currentRow() >= 0:
            self._snapshot("Satır sil")
        if not indices:
            r = self.table.currentRow()
            if r >= 0: self.table.removeRow(r)
        else:
            for idx in sorted(indices, reverse=True):
                self.table.removeRow(idx.row())

    # ── Satır Taşıma ─────────────────────────────────────────────────
    # Satırla birlikte taşınması gereken item-data rolleri
    _ROW_ROLES = (ROLE_MARK, ROLE_UA, ROLE_REF)

    def _read_row(self, row):
        """Bir satırın tüm hücre verilerini (rol verileri + gizli durumu dahil) döner.

        DÜZELTME (v27): Önceden yalnızca metin/renk/flag okunuyordu; bu yüzden
        sıralama veya taşıma sonrası TMDB ❌/✅ kalıcı işareti kayboluyor
        ("Bulamayanlar" filtresi yanlış sonuç veriyordu). Artık rol verileri
        (TMDB işareti, User-Agent, Referrer) ve satırın gizli/görünür durumu da
        satırla birlikte taşınır.
        """
        data = {}
        for col in range(self.table.columnCount()):
            it = self.table.item(row, col)
            if it is None:
                data[col] = None
                continue
            if col == COL_CHECK:
                data[col] = it.checkState()
            else:
                roles = {}
                for role in self._ROW_ROLES:
                    v = it.data(role)
                    if v is not None:
                        roles[role] = v
                data[col] = {
                    "text":       it.text(),
                    "foreground": it.foreground(),
                    "background": it.background(),
                    "flags":      it.flags(),
                    "roles":      roles,
                    "tooltip":    it.toolTip(),
                }
        data["hidden"] = self.table.isRowHidden(row)
        return data

    def _write_row(self, row, data):
        """_read_row ile alınan veriyi belirtilen satıra yazar."""
        for col, val in data.items():
            if col == "hidden":
                continue
            if val is None:
                self.table.setItem(row, col, self._make_item(""))
                continue
            if col == COL_CHECK:
                it = self._make_check_item()
                it.setCheckState(val)
                self.table.setItem(row, col, it)
            else:
                it = QTableWidgetItem(val["text"])
                it.setForeground(val["foreground"])
                it.setBackground(val["background"])
                it.setFlags(val["flags"])
                it.setToolTip(val.get("tooltip", ""))
                for role, v in val.get("roles", {}).items():
                    it.setData(role, v)
                if col == COL_URL:
                    self._refresh_uaref_tip(it)
                self.table.setItem(row, col, it)
        if "hidden" in data:
            self.table.setRowHidden(row, bool(data["hidden"]))

    def _move_rows(self, rows: list[int], direction: int):
        """
        rows      : taşınacak satır indekslerinin sıralı listesi
        direction : -1 = yukarı, +1 = aşağı
        Bitişik veya dağınık satırları gruplar halinde taşır.
        Sınırda (en üst / en alt) ise sessizce durur.
        """
        if not rows:
            return
        total = self.table.rowCount()
        rows  = sorted(rows, reverse=(direction == 1))

        # Sınır kontrolü — hareket edemeyecek satır varsa hiç taşıma
        if direction == -1 and rows[0] == 0:
            return
        if direction == 1  and rows[-1] == total - 1:
            return

        self._snapshot("Satır taşı")

        # Sıralamayı kalıcı olarak kapat (taşıma boyunca açma)
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self._undo_busy = True  # _write_row'un tetiklediği itemChanged'ler ayrı snapshot almasın

        try:
            for row in rows:
                target = row + direction
                src_data = self._read_row(row)
                tgt_data = self._read_row(target)
                self._write_row(row,    tgt_data)
                self._write_row(target, src_data)
        finally:
            self._undo_busy = False
            self.table.setUpdatesEnabled(True)
            # NOT: setSortingEnabled(True) kasıtlı olarak buraya alınmadı
            # — açılırsa Qt satırları yeniden sıralar ve taşıma bozulur

        # Seçimi yeni pozisyonlara taşı (çoklu seçim korunacak şekilde)
        sm = self.table.selectionModel()
        sm.clearSelection()
        new_rows = [r + direction for r in rows]
        for r in new_rows:
            sm.select(
                self.table.model().index(r, 0),
                sm.SelectionFlag.Select | sm.SelectionFlag.Rows
            )
        scroll_to = new_rows[-1] if direction == 1 else new_rows[0]
        self.table.scrollTo(self.table.model().index(scroll_to, 0))
        self._schedule_stats()

    def _get_move_targets(self):
        """
        Taşınacak satırları belirler:
        1) Tikli (✔) satırlar varsa onlar,
        2) Yoksa Ctrl/Shift ile seçili satırlar,
        3) Yoksa sadece geçerli satır.
        """
        rows = self._checked_rows()
        if not rows:
            sel = self.table.selectionModel().selectedRows()
            rows = sorted({idx.row() for idx in sel})
        if not rows:
            r = self.table.currentRow()
            if r >= 0:
                rows = [r]
        return rows

    def move_rows_up(self):
        self._move_rows(self._get_move_targets(), -1)

    def move_rows_down(self):
        self._move_rows(self._get_move_targets(), +1)

    def move_rows_top(self):
        """Seçili/tikli satırları tablonun en üstüne taşır."""
        rows = self._get_move_targets()
        if not rows or rows[0] == 0:
            return
        self._snapshot("Satırları en üste taşı")
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self._undo_busy = True
        try:
            # Taşınacak satırların verilerini oku
            moving = [self._read_row(r) for r in rows]
            # Geri kalan satırları sırayla oku (taşınanlar hariç)
            remaining_indices = [r for r in range(self.table.rowCount()) if r not in set(rows)]
            remaining = [self._read_row(r) for r in remaining_indices]
            # Yeni sıra: önce taşınanlar, sonra kalanlar
            new_order = moving + remaining
            for i, data in enumerate(new_order):
                self._write_row(i, data)
        finally:
            self._undo_busy = False
            self.table.setUpdatesEnabled(True)
        # Seçimi güncelle
        sm = self.table.selectionModel()
        sm.clearSelection()
        for r in range(len(rows)):
            sm.select(
                self.table.model().index(r, 0),
                sm.SelectionFlag.Select | sm.SelectionFlag.Rows
            )
        self.table.scrollToTop()
        self._schedule_stats()

    def move_rows_bottom(self):
        """Seçili/tikli satırları tablonun en altına taşır."""
        rows = self._get_move_targets()
        total = self.table.rowCount()
        if not rows or rows[-1] == total - 1:
            return
        self._snapshot("Satırları en alta taşı")
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self._undo_busy = True
        try:
            moving = [self._read_row(r) for r in rows]
            remaining_indices = [r for r in range(total) if r not in set(rows)]
            remaining = [self._read_row(r) for r in remaining_indices]
            new_order = remaining + moving
            for i, data in enumerate(new_order):
                self._write_row(i, data)
        finally:
            self._undo_busy = False
            self.table.setUpdatesEnabled(True)
        # Seçimi güncelle
        sm = self.table.selectionModel()
        sm.clearSelection()
        start = total - len(rows)
        for r in range(start, total):
            sm.select(
                self.table.model().index(r, 0),
                sm.SelectionFlag.Select | sm.SelectionFlag.Rows
            )
        self.table.scrollToBottom()
        self._schedule_stats()

    def filter_table(self):
        text = self.search_bar.text().lower()
        self.table.setUpdatesEnabled(False)
        try:
            if not text:
                for row in range(self.table.rowCount()):
                    self.table.setRowHidden(row, False)
            else:
                cols = (COL_GROUP, COL_NAME, COL_YEAR, COL_TMDBID, COL_URL, COL_UA, COL_REF, COL_LOGO)
                for row in range(self.table.rowCount()):
                    match = False
                    for col in cols:
                        it = self.table.item(row, col)
                        if it and text in it.text().lower():
                            match = True
                            break
                    self.table.setRowHidden(row, not match)
        finally:
            self.table.setUpdatesEnabled(True)
        self._schedule_stats()

    # ── M3U Yükleme ───────────────────────────────────────────────────
    @staticmethod
    def _parse_entry_tail(lines, start):
        """#EXTINF satırından sonraki meta satırlarını ve URL'yi okur.

        DÜZELTME (v27): Önceden ilk 5 satırda 'http' ile başlayan herhangi bir
        satır URL sayılıyordu. URL'si olmayan bir kayıt, sonraki kaydın URL'sini
        'çalıyor' (aynı URL iki satırda görünüyordu). Artık başka bir #EXTINF'e
        gelince duruluyor. #EXTVLCOPT / #EXTHTTP ile verilen User-Agent ve
        Referrer da okunuyor (eskiden sessizce siliniyordu).
        Döner: (url, user_agent, referrer)
        """
        url = ua = ref = ""
        for j in range(start, min(start + 10, len(lines))):
            cand = lines[j].strip()
            if not cand:
                continue
            up = cand.upper()
            if up.startswith("#EXTINF"):
                break                      # sonraki kayıt — bu kaydın URL'si yok
            if up.startswith("#EXTVLCOPT:"):
                key, _, val = cand[len("#EXTVLCOPT:"):].partition("=")
                key = key.strip().lower()
                if key == "http-user-agent":
                    ua = _hdr(val)
                elif key in ("http-referrer", "http-referer"):
                    ref = _hdr(val)
                continue
            if up.startswith("#EXTHTTP:"):
                try:
                    obj = json.loads(cand[len("#EXTHTTP:"):])
                    for k, v in obj.items():
                        kl = str(k).lower()
                        if kl == "user-agent":
                            ua = _hdr(v)
                        elif kl in ("referer", "referrer"):
                            ref = _hdr(v)
                except Exception:
                    pass
                continue
            if cand.startswith("#"):
                continue                   # #EXTGRP vb. diğer meta satırları
            url = cand
            break
        return url, ua, ref

    def process_m3u_content(self, content, append=False):
        if self.table.rowCount() > 0:
            self._snapshot("Dosya ekle" if append else "Dosya aç")
        lines = content.splitlines()
        self.table.setSortingEnabled(False)
        if not append:
            self.table.setRowCount(0)
        added = 0
        # Append modunda: yeni içerikleri en üste eklemek için
        # önce tüm yeni satırları toplayıp sonra 0. pozisyondan itibaren ekle
        new_entries = []
        for i, line in enumerate(lines):
            line = line.strip()
            if not line.startswith("#EXTINF"):
                continue
            def _attr(tag, _line=line):
                m = re.search(rf'{tag}="([^"]+)"', _line)
                return m.group(1) if m else ""
            logo      = _attr("tvg-logo")
            group_raw = _attr("group-title")
            group     = " ".join(group_raw.split())
            tvg_id    = _attr("tvg-id")
            tmdb_id_v = _attr("tmdb-id")
            tmdb_id   = tvg_id or tmdb_id_v
            year      = _attr("tvg-year")
            # DÜZELTME: İçerik adı, TIRNAK DIŞINDAKİ ilk virgülden sonra başlar.
            # Eskiden split(",", 1) kullanılıyordu; group-title="Love, Death & Robots"
            # gibi virgül içeren bir öznitelik varsa ad yanlış yerden bölünüyordu.
            name      = _extinf_name(line)
            url, ua, ref = self._parse_entry_tail(lines, i + 1)
            new_entries.append((logo, group, name, year, tmdb_id, url, ua, ref))

        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            def _add(r, entry):
                logo, group, name, year, tmdb_id, url, ua, ref = entry
                self.table.insertRow(r)
                self.table.setItem(r, COL_CHECK,  self._make_check_item())
                self.table.setItem(r, COL_LOGO,   self._make_item(logo))
                self.table.setItem(r, COL_GROUP,  self._make_item(group))
                self.table.setItem(r, COL_NAME,   self._make_item(name))
                self.table.setItem(r, COL_YEAR,   self._make_item(year))
                self.table.setItem(r, COL_TMDBID, self._make_item(tmdb_id))
                self.table.setItem(r, COL_URL,    self._make_item(url))
                self.table.setItem(r, COL_UA,     self._make_item(_hdr(ua)))
                self.table.setItem(r, COL_REF,    self._make_item(_hdr(ref)))
                self.table.setItem(r, COL_STATUS, self._make_item("Bekliyor"))

            if append:
                # Ters sıradan ekle ki sonuçta orijinal sıra korunsun (en üste)
                for entry in reversed(new_entries):
                    _add(0, entry)
                    added += 1
            else:
                # Toplu ekleme: önce satır sayısını ayarla (daha hızlı)
                base = self.table.rowCount()
                self.table.setRowCount(base + len(new_entries))
                for i, entry in enumerate(new_entries):
                    r = base + i
                    logo, group, name, year, tmdb_id, url, ua, ref = entry
                    self.table.setItem(r, COL_CHECK,  self._make_check_item())
                    self.table.setItem(r, COL_LOGO,   self._make_item(logo))
                    self.table.setItem(r, COL_GROUP,  self._make_item(group))
                    self.table.setItem(r, COL_NAME,   self._make_item(name))
                    self.table.setItem(r, COL_YEAR,   self._make_item(year))
                    self.table.setItem(r, COL_TMDBID, self._make_item(tmdb_id))
                    self.table.setItem(r, COL_URL,    self._make_item(url))
                    self.table.setItem(r, COL_UA,     self._make_item(_hdr(ua)))
                    self.table.setItem(r, COL_REF,    self._make_item(_hdr(ref)))
                    self.table.setItem(r, COL_STATUS, self._make_item("Bekliyor"))
                    added += 1
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

        # Not: setSortingEnabled(True) kasıtlı yapılmıyor — bkz. _sort_by_column()
        if append:
            self.table.scrollToTop()
            self.statusBar().showMessage(
                f"✅ {added} içerik en üste eklendi. Toplam: {self.table.rowCount()}", 5000)
        else:
            self.statusBar().showMessage(f"✅ {added} içerik yüklendi.", 5000)
        self._schedule_stats()

    def open_m3u(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "M3U Aç (mevcut liste silinir)", "", "M3U Files (*.m3u *.m3u8)")
        if fp:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                self.process_m3u_content(f.read(), append=False)

    def append_m3u(self):
        fp, _ = QFileDialog.getOpenFileName(
            self, "M3U Ekle (mevcut listeye eklenir)", "", "M3U Files (*.m3u *.m3u8)")
        if fp:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                self.process_m3u_content(f.read(), append=True)

    def _download_playlist(self, url):
        """URL'den liste metnini indirir; başarısızsa kullanıcıya söyler, None döner.

        DÜZELTME (v27): (1) 200 dışındaki yanıtlarda hiçbir şey olmuyordu
        (sessiz başarısızlık); artık hata gösteriliyor. (2) r.text, sunucu
        charset bildirmezse ISO-8859-1 varsayıyor ve Türkçe karakterler
        bozuluyordu; içerik UTF-8 olarak çözülüyor. (3) "Kullan" açıksa
        User-Agent / Referrer gönderilir.
        """
        url = (url or "").strip()
        if not url:
            return None
        try:
            r = requests.get(url, timeout=20, headers=self._request_headers())
            r.raise_for_status()
            return r.content.decode("utf-8-sig", errors="ignore")
        except Exception as e:
            QMessageBox.critical(self, "Hata", f"Liste indirilemedi:\n{e}")
            return None

    def import_from_url(self):
        url, ok = QInputDialog.getText(self, "M3U URL Aç", "Playlist URL (mevcut liste silinir):")
        if ok and url:
            text = self._download_playlist(url)
            if text is not None:
                self.process_m3u_content(text, append=False)

    def append_from_url(self):
        url, ok = QInputDialog.getText(self, "M3U URL Ekle", "Playlist URL (mevcut listeye eklenir):")
        if ok and url:
            text = self._download_playlist(url)
            if text is not None:
                self.process_m3u_content(text, append=True)

    # ── M3U Kaydet ────────────────────────────────────────────────────
    def save_m3u(self):
        if self.table.rowCount() == 0:
            QMessageBox.warning(self, "Uyarı", "Kaydedilecek veri yok!")
            return
        fp, _ = QFileDialog.getSaveFileName(self, "M3U Kaydet", "", "M3U Files (*.m3u)")
        if not fp:
            return
        try:
            total = self.table.rowCount()
            saved = self._write_rows_to_file(fp, range(total))
            # DÜZELTME (v27): Kaydettikten sonra "kaydedilmemiş değişiklik" işareti
            # temizlenmiyordu; çıkarken hâlâ uyarı çıkıyordu.
            self._dirty = False
            msg = f"✅ Kaydedildi ({saved} içerik):\n{fp}"
            if saved < total:
                msg += f"\n\n⚠️ URL'si boş olan {total - saved} satır dosyaya yazılmadı."
            QMessageBox.information(self, "Başarılı", msg)
        except Exception as e:
            QMessageBox.critical(self, "Hata", str(e))

    # ── Seçilileri Dışa Aktar ─────────────────────────────────────────
    def export_selected(self):
        """Tik işaretli satırları (yoksa seçili satırları) tek bir .m3u dosyasına aktarır."""
        # Önce checkbox'lara bak
        checked = self._checked_rows()
        if checked:
            target_rows = checked
            label = f"tikli {len(target_rows)}"
        else:
            target_rows = sorted(set(
                idx.row() for idx in self.table.selectionModel().selectedRows()
            ))
            label = f"seçili {len(target_rows)}"

        if not target_rows:
            QMessageBox.warning(self, "Uyarı",
                "Dışa aktarmak için satır seçin.\n\n"
                "• Tik kutusu ile seçebilirsiniz\n"
                "• veya Ctrl+tıklama / Shift+tıklama ile satır seçebilirsiniz")
            return

        fp, _ = QFileDialog.getSaveFileName(
            self, f"{label} İçeriği Kaydet", "", "M3U Files (*.m3u)")
        if not fp:
            return

        try:
            saved = self._write_rows_to_file(fp, target_rows)
            QMessageBox.information(self, "Başarılı",
                f"✅ {saved} içerik dışa aktarıldı:\n{fp}")
        except Exception as e:
            QMessageBox.critical(self, "Hata", str(e))

    # ── Klasöre Göre Dışa Aktar ───────────────────────────────────────
    def export_by_group(self):
        """Her Tür/Grup için ayrı bir .m3u dosyası oluşturur, seçilen klasöre kaydeder.
        Filtre aktifse sadece görünür satırları kullanır."""
        if self.table.rowCount() == 0:
            QMessageBox.warning(self, "Uyarı", "Kaydedilecek veri yok!")
            return

        visible_rows = [r for r in range(self.table.rowCount())
                        if not self.table.isRowHidden(r)]
        if not visible_rows:
            QMessageBox.information(self, "Bilgi", "Görünür satır bulunamadı.")
            return

        # Grupları topla
        groups: dict[str, list[int]] = {}
        for row in visible_rows:
            grp = self._get_cell(row, COL_GROUP).strip() or "Diğer"
            groups.setdefault(grp, []).append(row)

        # Kullanıcıya önizleme göster
        preview = "\n".join(
            f"  📄 {grp}.m3u  ({len(rows)} içerik)"
            for grp, rows in sorted(groups.items())
        )
        reply = QMessageBox.question(
            self, "Klasöre Göre Dışa Aktar",
            f"{len(groups)} farklı tür/grup bulundu. Her biri için ayrı .m3u dosyası oluşturulacak:\n\n"
            f"{preview}\n\nKayıt klasörünü seçmek istiyor musunuz?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        folder = QFileDialog.getExistingDirectory(self, "Kayıt Klasörü Seç")
        if not folder:
            return

        import os, re as _re

        def safe_filename(name: str) -> str:
            """Dosya adı için geçersiz karakterleri temizler."""
            name = _re.sub(r'[\\/:*?"<>|]', '_', name)
            return name.strip('. ') or "Grup"

        total_files = 0
        total_items = 0
        errors = []
        for grp, rows in sorted(groups.items()):
            fname = safe_filename(grp) + ".m3u"
            fpath = os.path.join(folder, fname)
            try:
                saved = self._write_rows_to_file(fpath, rows)
                total_files += 1
                total_items += saved
            except Exception as e:
                errors.append(f"{fname}: {e}")

        msg = f"✅ {total_files} dosya, {total_items} içerik aktarıldı.\nKlasor: {folder}"
        if errors:
            msg += f"\n\n⚠️ Hatalar:\n" + "\n".join(errors)
        QMessageBox.information(self, "Tamamlandı", msg)

    # ── Ortak yazma yardımcısı ────────────────────────────────────────
    def _write_rows_to_file(self, filepath: str, rows) -> int:
        """Verilen satırları filepath'e .m3u formatında yazar. Yazılan satır sayısını döner.
        Satıra özel User-Agent / Referrer varsa #EXTVLCOPT satırları olarak eklenir."""
        saved = 0
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            for row in rows:
                logo    = self._get_cell(row, COL_LOGO).strip()
                group   = " ".join(self._get_cell(row, COL_GROUP).split())
                name    = self._get_cell(row, COL_NAME).strip() or "İçerik"
                year    = self._get_cell(row, COL_YEAR)
                tmdb_id = self._get_cell(row, COL_TMDBID)
                url     = self._get_cell(row, COL_URL).strip()
                if not url:
                    continue
                ua, ref = self._row_uaref(row)
                extinf = "#EXTINF:-1"
                if logo:    extinf += f' tvg-logo="{_m3u_attr(logo)}"'
                if group:   extinf += f' group-title="{_m3u_attr(group)}"'
                if year:    extinf += f' tvg-year="{_m3u_attr(year)}"'
                if tmdb_id: extinf += f' tvg-id="{_m3u_attr(tmdb_id)}"'
                f.write(f"{extinf},{name}\n")
                if ua:  f.write(f"#EXTVLCOPT:http-user-agent={_hdr(ua)}\n")
                if ref: f.write(f"#EXTVLCOPT:http-referrer={_hdr(ref)}\n")
                f.write(f"{url}\n")
                saved += 1
        return saved

    # ── Link Kontrol (Paralel) ────────────────────────────────────────
    def check_links(self):
        """
        Paralel ThreadPoolExecutor ile tüm görünür satırları eş zamanlı kontrol eder.
        Worker sayısı _link_workers özelliğiyle ayarlanabilir (varsayılan: 12).
        Bu, HER ZAMAN görünür satırların TAMAMINI yeniden kontrol eder
        (baştan başlar). Durdurulan bir kontrolü kaldığı yerden devam
        ettirmek için 'Devam Et' düğmesini kullanın.
        """
        total = self.table.rowCount()
        if total == 0:
            return

        # Eğer önceki worker hâlâ çalışıyorsa uyar
        if self.link_check_worker and self.link_check_worker.isRunning():
            QMessageBox.information(self, "Bilgi", "Link kontrol zaten çalışıyor.\n'⏹️ Durdur' ile durdurun.")
            return

        visible_rows = [r for r in range(total) if not self.table.isRowHidden(r)]
        if not visible_rows:
            QMessageBox.information(self, "Bilgi", "Görünür satır bulunamadı.")
            return

        rows_urls = []
        for row in visible_rows:
            url = self._get_cell(row, COL_URL)
            if url:
                rows_urls.append((row, url) + self._uaref_for_check(row))

        if not rows_urls:
            QMessageBox.information(self, "Bilgi", "Kontrol edilecek URL bulunamadı.")
            return

        self._kontrolu_baslat(rows_urls, total)

    def resume_check_links(self):
        """
        'Durdur'dan sonra kaldığı yerden devam eder: yalnızca HENÜZ
        DURUM SÜTUNU BOŞ olan (yani bir önceki taramada kontrol
        edilememiş) satırları kontrol eder. Zaten "AKTİF ✅ / HATA .. /
        KAPALI 🛑" durumu yazılmış satırlar tekrar kontrol edilmez.

        NEDEN GEREKLİ: Eskiden 'Kontrol Et'e her basışta TÜM satırlar
        (durdurulmadan önce kontrol edilmiş olanlar dahil) baştan
        taranıyordu — "durdurup tekrar kontrol et'e basınca yine
        baştan başlıyor" şikayeti buradan kaynaklanıyordu.
        """
        total = self.table.rowCount()
        if total == 0:
            return
        if self.link_check_worker and self.link_check_worker.isRunning():
            QMessageBox.information(self, "Bilgi", "Link kontrol zaten çalışıyor.\n'⏹️ Durdur' ile durdurun.")
            return

        visible_rows = [r for r in range(total) if not self.table.isRowHidden(r)]
        rows_urls = []
        for row in visible_rows:
            durum = (self._get_cell(row, COL_STATUS) or "").strip()
            # DİKKAT: Bu sütun link kontrolü DIŞINDA TMDB sonuçları için de
            # kullanılıyor ("TMDB ✅"/"TMDB ❌") ve yeni/boş satırlarda
            # "Yeni"/"Bekliyor" gibi yer tutucular içerebiliyor. Yalnızca
            # GERÇEK bir link kontrolü sonucu varsa (AKTİF/KAPALI/HATA)
            # bu satır "zaten kontrol edilmiş" sayılır — aksi hâlde
            # yeni eklenen ya da TMDB ile işlenmiş satırlar yanlışlıkla
            # atlanır.
            zaten_kontrol_edilmis = any(a in durum for a in ("AKTİF", "KAPALI", "HATA "))
            if zaten_kontrol_edilmis:
                continue
            url = self._get_cell(row, COL_URL)
            if url:
                rows_urls.append((row, url) + self._uaref_for_check(row))

        if not rows_urls:
            QMessageBox.information(
                self, "Bilgi",
                "Devam edilecek satır yok — görünür satırların tamamı zaten "
                "kontrol edilmiş.\nBaştan taramak için '🔍 Kontrol Et' kullanın.")
            return

        self._kontrolu_baslat(rows_urls, total, devam=True)

    def _kontrolu_baslat(self, rows_urls, total, devam: bool = False):
        """check_links() ve resume_check_links() için ORTAK başlatma kodu."""
        count   = len(rows_urls)
        workers = getattr(self, "_link_workers", 6)
        timeout = getattr(self, "_link_timeout", 15)

        filtered = count < total
        etiket = "devam ediliyor" if devam else "başladı"
        label = f"(filtreli: {count} link)" if filtered else f"({count} link)"
        self.statusBar().showMessage(
            f"🔍 Paralel link kontrol {etiket} {label} — {workers} eş zamanlı bağlantı"
        )

        baslik = "Linkler kontrol ediliyor (devam)…" if devam else "Linkler kontrol ediliyor…"
        self._link_prog = QProgressDialog(baslik, "Durdur", 0, count, self)
        self._link_prog.setWindowTitle("Link Kontrol")
        self._link_prog.setWindowModality(Qt.WindowModality.WindowModal)
        self._link_prog.show()

        g_ua, g_ref = self._global_uaref_for_check()
        self.link_check_worker = LinkCheckWorker(
            rows_urls, workers=workers, timeout=timeout,
            user_agent=g_ua, referrer=g_ref)
        self.link_check_worker.result_ready.connect(self._on_link_result)
        self.link_check_worker.progress.connect(self._on_link_progress)
        self.link_check_worker.finished.connect(self._on_link_done)
        self._link_prog.canceled.connect(self.link_check_worker.stop)
        self.link_check_worker.start()

    def _on_link_result(self, row, status, color):
        self._set_cell(row, COL_STATUS, status)
        it = self.table.item(row, COL_STATUS)
        if it:
            it.setBackground(color)
            it.setForeground(QColor("white"))

    def _on_link_progress(self, done, total, url):
        self._link_prog.setValue(done)
        self._link_prog.setLabelText(
            f"{done}/{total} kontrol edildi\n{url[:80]}"
        )

    def _on_link_done(self, active, dead, error):
        # close() 'canceled' sinyalini tetikleyip _stop'u True yapar; önce oku.
        stopped = bool(getattr(self.link_check_worker, "_stop", False))
        self._link_prog.close()
        total = active + dead + error
        self.statusBar().showMessage(
            f"{'⏹️ Link kontrol durduruldu' if stopped else '✅ Link kontrol tamamlandı'} — "
            f"Aktif: {active}  |  Kapalı: {dead}  |  Hata: {error}  |  Toplam: {total}",
            8000
        )
        QMessageBox.information(
            self, ("Link Kontrol Durduruldu" if stopped else "Link Kontrol Tamamlandı"),
            f"✅ Aktif  : {active}\n"
            f"🛑 Kapalı : {dead}\n"
            f"⚠️  Hata   : {error}\n"
            f"─────────────\n"
            f"📊 Toplam  : {total}"
        )

    def set_link_workers(self):
        """Eş zamanlı bağlantı (worker) sayısını kullanıcıdan al."""
        current = getattr(self, "_link_workers", 6)
        val, ok = QInputDialog.getInt(
            self, "Paralel Bağlantı Sayısı",
            "Eş zamanlı kontrol edilecek link sayısı:\n"
            "(Az: daha güvenli — 503 azalır  |  Çok: hızlı ama sahte ölü artar)\n"
            "Önerilen: 4-10",
            current, 1, 100, 5
        )
        if ok:
            self._link_workers = val
            self.statusBar().showMessage(f"⚙️ Paralel bağlantı sayısı: {val}", 4000)

    def set_link_timeout(self):
        """Bağlantı zaman aşımı süresini kullanıcıdan al."""
        current = getattr(self, "_link_timeout", 15)
        val, ok = QInputDialog.getInt(
            self, "Zaman Aşımı (saniye)",
            "Her link için maksimum bekleme süresi (saniye):",
            current, 1, 60, 1
        )
        if ok:
            self._link_timeout = val
            self.statusBar().showMessage(f"⚙️ Zaman aşımı: {val} sn", 4000)

    def stop_check(self):
        if self.link_check_worker and self.link_check_worker.isRunning():
            self.link_check_worker.stop()
        self._check_running = False

    def delete_dead_links(self):
        dead = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue        # DÜZELTME (v27): filtre dışındaki satırlara dokunma
            status = self._get_cell(row, COL_STATUS)
            # ŞÜPHELİ silinmez — VLC'de çalışıyor olabilir
            if "KAPALI" in status or status.startswith("HATA"):
                dead.append(row)
        if not dead:
            QMessageBox.information(
                self, "Bilgi",
                "Silinecek ölü link bulunamadı.\n"
                "Önce 'Link Kontrol' çalıştırın.\n"
                "(ŞÜPHELİ ⚠️ olanlar silinmez — VLC'de açılıyor olabilir.)"
            )
            return
        reply = QMessageBox.question(
            self, "Ölü Linkleri Sil",
            f"{len(dead)} adet çalışmayan link bulundu.\n"
            f"(ŞÜPHELİ olanlar hariç)\n"
            f"Hepsini silmek istiyor musunuz?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._snapshot(f"{len(dead)} ölü link sil")
            for row in sorted(dead, reverse=True):
                self.table.removeRow(row)
            self.statusBar().showMessage(f"🗑️ {len(dead)} ölü link silindi.", 5000)

    def delete_doubtful_rows(self):
        """Durum sütununda 'ŞÜPHELİ' geçen tüm görünür satırları siler
        (link kontrolü + TMDB şüphelileri)."""
        rows = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            status = self._get_cell(row, COL_STATUS) or ""
            if "ŞÜPHELİ" in status:
                rows.append(row)
        if not rows:
            QMessageBox.information(
                self, "Bilgi",
                "Silinecek şüpheli satır yok.\n"
                "Önce Link Kontrol veya TMDB Doldur çalıştırın.")
            return
        reply = QMessageBox.question(
            self, "Şüphelileri Sil",
            f"{len(rows)} adet şüpheli satır bulundu.\n"
            f"Hepsini silmek istiyor musunuz?\n\n"
            f"(Geri Al ile kurtarabilirsiniz.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._snapshot(f"{len(rows)} şüpheli satır sil")
        for row in sorted(rows, reverse=True):
            self.table.removeRow(row)
        self._schedule_stats()
        self.statusBar().showMessage(f"🗑️ {len(rows)} şüpheli satır silindi.", 5000)

    def delete_tmdb_doubtful(self):
        """Sadece TMDB şüpheli (doubt) satırlarını siler."""
        rows = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            if self._tmdb_state(row) == "doubt":
                rows.append(row)
        if not rows:
            QMessageBox.information(self, "Bilgi", "TMDB şüpheli satır yok.")
            return
        reply = QMessageBox.question(
            self, "TMDB Şüphelilerini Sil",
            f"{len(rows)} adet TMDB şüpheli satır silinecek.\n"
            f"Devam edilsin mi?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._snapshot(f"{len(rows)} TMDB şüpheli sil")
        for row in sorted(rows, reverse=True):
            self.table.removeRow(row)
        self._schedule_stats()
        self.statusBar().showMessage(f"🗑️ {len(rows)} TMDB şüpheli satır silindi.", 5000)

    def show_only_link_doubtful(self):
        """Sadece link kontrolü şüphelilerini göster."""
        found = 0
        for row in range(self.table.rowCount()):
            status = self._get_cell(row, COL_STATUS) or ""
            is_d = "ŞÜPHELİ" in status
            self.table.setRowHidden(row, not is_d)
            if is_d:
                found += 1
        if found == 0:
            QMessageBox.information(self, "Bilgi", "Şüpheli satır bulunamadı.")
            self.show_all_rows()
        else:
            self.statusBar().showMessage(f"⚠️ {found} şüpheli satır gösteriliyor.", 5000)
            self._schedule_stats()

    def show_only_dead(self):
        found = 0
        for row in range(self.table.rowCount()):
            status = self._get_cell(row, COL_STATUS)
            # ŞÜPHELİ gösterilmez — VLC'de çalışıyor olabilir
            is_dead = ("KAPALI" in status) or status.startswith("HATA")
            self.table.setRowHidden(row, not is_dead)
            if is_dead:
                found += 1
        if found == 0:
            QMessageBox.information(self, "Bilgi", "Çalışmayan link bulunamadı.\nÖnce 'Link Kontrol' çalıştırın.")
            self.show_all_rows()
        else:
            self.statusBar().showMessage(f"👁️ {found} çalışmayan link gösteriliyor. Tümünü görmek için '📋 Tümünü Göster'e basın.", 8000)
            self._schedule_stats()

    def show_only_not_found(self):
        """TMDB tarafından bulunamayan (TMDB ✅ işareti olmayan, dolu) satırları gösterir.
        TMDB otomatik doldur çalıştırıldıktan sonra kullanışlıdır."""
        total = self.table.rowCount()
        if total == 0:
            QMessageBox.information(self, "Bilgi", "Önce bir liste yükleyin.")
            return

        found = 0
        not_started = 0  # Henüz TMDB sorgusu yapılmamış satırlar
        for row in range(total):
            # DÜZELTME: Durum sütununu link kontrolü de kullanıyor ("AKTİF ✅",
            # "HATA 404" ...) ve TMDB'nin yazdığı işareti EZİYOR. Metne bakmak
            # yerine kalıcı işaret + tvg-id'ye bakan _tmdb_state kullanılır.
            st = self._tmdb_state(row)
            is_not_found = (st != "ok")
            self.table.setRowHidden(row, not is_not_found)
            if is_not_found:
                found += 1
                if st == "none":
                    not_started += 1

        if found == 0:
            QMessageBox.information(
                self, "Tebrikler! 🎉",
                "Tüm içerikler TMDB'de bulundu! Hiç eksik kalmadı."
            )
            self.show_all_rows()
        else:
            extra = f"\n({not_started} tanesi henüz sorgulanmadı)" if not_started else ""
            self.statusBar().showMessage(
                f"🔴 {found} içerik TMDB'de bulunamadı / şüpheli.{extra}  "
                f"Tümünü görmek için '📋 Tümünü Göster (TMDB)'ye basın.",
                10000
            )
            if not_started == found:
                QMessageBox.information(
                    self, "Bilgi",
                    f"{found} satır görüntüleniyor, ancak henüz TMDB sorgusu yapılmamış.\n\n"
                    f"Önce '🚀 TMDB: Tümünü Doldur' butonunu çalıştırın,\n"
                    f"ardından bu butona tekrar basın."
                )
            self._schedule_stats()

    def show_only_doubtful(self):
        """Yalnızca TMDB'nin emin olamadığı ('ŞÜPHELİ ⚠️ TMDB') satırları gösterir."""
        total = self.table.rowCount()
        if total == 0:
            QMessageBox.information(self, "Bilgi", "Önce bir liste yükleyin.")
            return
        found = 0
        for row in range(total):
            is_d = (self._tmdb_state(row) == "doubt")
            self.table.setRowHidden(row, not is_d)
            found += 1 if is_d else 0
        if found == 0:
            QMessageBox.information(self, "Bilgi",
                "Şüpheli TMDB eşleşmesi yok.\n(Önce '🚀 Tümünü Doldur' çalıştırın.)")
            self.show_all_rows()
        else:
            self.statusBar().showMessage(
                f"⚠️ {found} şüpheli satır gösteriliyor — sağ tık → 'TMDB'de Ara' ile elle seçin. "
                f"Fare ile Durum hücresinin üstüne gelince TMDB'nin bulduğu aday görünür.", 10000)
            self._schedule_stats()

    def show_all_rows(self):
        self.search_bar.clear()
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, False)
        self.statusBar().showMessage("📋 Tüm satırlar gösteriliyor.", 3000)
        self._schedule_stats()

    def normalize_episodes(self):
        """Sezon/bölüm ifadelerini S01E01 biçimine çevirir.

        "Meghan 1. Sezon 1. Bölüm (2022)" -> "Meghan S01E01 (2022)"
        Zaten S01E01 biçiminde olanlara ve bölüm bilgisi olmayanlara dokunmaz.
        Tikli satır varsa yalnızca onlarda, yoksa görünür satırlarda çalışır.
        """
        checked = self._checked_rows()
        if checked:
            target_rows, scope = checked, "tikli %d satır" % len(checked)
        else:
            target_rows = [r for r in range(self.table.rowCount())
                           if not self.table.isRowHidden(r)]
            scope = "görünür %d satır" % len(target_rows)

        if not target_rows:
            QMessageBox.information(self, "Bilgi", "İşlenecek satır bulunamadı.")
            return

        # Önce kuru çalıştır: neyin değişeceğini hesapla
        changes = []
        for row in target_rows:
            old = self._get_cell(row, COL_NAME)
            new, changed = normalize_season_episode(old)
            if changed:
                changes.append((row, old, new))

        if not changes:
            QMessageBox.information(
                self, "Bilgi",
                "Dönüştürülecek sezon/bölüm ifadesi bulunamadı.\n\n"
                "(%s tarandı. Zaten S01E01 biçiminde olanlar atlanır.)" % scope
            )
            return

        preview = []
        for _r, old, new in changes[:12]:
            o = old if len(old) <= 52 else old[:49] + "..."
            nn = new if len(new) <= 52 else new[:49] + "..."
            preview.append("  %s\n     ->  %s" % (o, nn))
        if len(changes) > 12:
            preview.append("  ... ve %d tane daha" % (len(changes) - 12))

        reply = QMessageBox.question(
            self, "Sezon/Bölüm Biçimini Düzelt",
            "%s içinde %d satır dönüştürülecek:\n\n%s\n\nUygulansın mı?"
            % (scope, len(changes), "\n".join(preview)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._snapshot("Sezon/Bölüm biçimi (%d satır)" % len(changes))
        for row, _old, new in changes:
            self._set_cell(row, COL_NAME, new)
        self.statusBar().showMessage(
            "✅ %d satırın sezon/bölüm biçimi S01E01 olarak düzeltildi." % len(changes), 7000)

    def delete_duplicate_urls(self):
        """Aynı URL'ye sahip satırlardan ilkini bırakır, kalanlarını siler.

        'Tekrarları Sil'den FARKI: bu fonksiyon içerik ADINA bakmaz, yalnızca
        URL'yi karşılaştırır. Böylece aynı filmin farklı kaynaklardaki
        alternatif linkleri KORUNUR; sadece birebir aynı adres tekrarlanmışsa
        temizlenir.
        """
        visible_rows = [r for r in range(self.table.rowCount())
                        if not self.table.isRowHidden(r)]
        filtered = len(visible_rows) < self.table.rowCount()

        if not visible_rows:
            QMessageBox.information(self, "Bilgi", "Görünür satır bulunamadı.")
            return

        seen   = {}   # normalize_url -> ilk görülen satır
        to_del = []
        blanks = 0
        for row in visible_rows:
            raw = self._get_cell(row, COL_URL)
            key = _norm_url(raw)
            if not key:
                blanks += 1          # URL'si boş satırlar dokunulmadan bırakılır
                continue
            if key in seen:
                to_del.append(row)
            else:
                seen[key] = row

        if not to_del:
            QMessageBox.information(
                self, "Bilgi",
                "Aynı URL'ye sahip tekrar bulunamadı."
                + (f"\n\n({blanks} satırın URL'si boş, dikkate alınmadı.)" if blanks else "")
            )
            return

        preview_lines = []
        for row in to_del[:10]:
            nm = self._get_cell(row, COL_NAME) or "(adsız)"
            ur = self._get_cell(row, COL_URL)
            if len(ur) > 60:
                ur = ur[:57] + "..."
            preview_lines.append(f"  • {nm}\n      {ur}")
        if len(to_del) > 10:
            preview_lines.append(f"  ... ve {len(to_del)-10} tane daha")

        reply = QMessageBox.question(
            self, "Aynı URL'leri Sil",
            f"{'Filtreli listede ' if filtered else ''}"
            f"{len(to_del)} adet birebir aynı URL bulundu.\n"
            f"Her adresten 1 tanesi korunacak, kalanlar silinecek.\n"
            f"(Farklı URL'ye sahip alternatifler KORUNUR.)\n\n"
            f"Silineceklerden ilk 10'u:\n" + "\n".join(preview_lines) +
            "\n\nDevam etmek istiyor musunuz?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._snapshot(f"{len(to_del)} yinelenen URL sil")
            for row in sorted(to_del, reverse=True):
                self.table.removeRow(row)
            msg = (f"🗑️ {len(to_del)} yinelenen URL silindi. "
                   f"{len(seen)} benzersiz adres kaldı.")
            if blanks:
                msg += f" ({blanks} boş URL atlandı.)"
            self.statusBar().showMessage(msg, 6000)

    def delete_duplicates(self):
        """Aynı İSİMDEN ilk karşılaşılanı bırakır, kalanları siler.

        DİKKAT: URL'ye bakmaz. Aynı filmin farklı kaynaklardaki alternatif
        linkleri de silinir. Alternatifleri korumak için 'Aynı URL'yi Sil'
        (delete_duplicate_urls) kullanın.
        Filtre aktifse sadece görünen satırlarda çalışır."""

        visible_rows = [r for r in range(self.table.rowCount())
                        if not self.table.isRowHidden(r)]
        filtered = len(visible_rows) < self.table.rowCount()

        if not visible_rows:
            QMessageBox.information(self, "Bilgi", "Görünür satır bulunamadı.")
            return

        # Önce kaç tane olduğunu hesapla (önizleme için)
        seen   = {}   # normalize_ad → ilk görülen satır
        to_del = []
        for row in visible_rows:
            name = self._get_cell(row, COL_NAME)
            # Bölüm bilgisini (S01E01 vb.) çıkar, sadece dizi/film adına bak
            base = clean_name(name).lower().strip()
            if not base:
                continue
            if base in seen:
                to_del.append(row)
            else:
                seen[base] = row

        if not to_del:
            QMessageBox.information(self, "Bilgi", "Tekrar eden içerik bulunamadı.")
            return

        # Kullanıcıya önizleme göster
        preview_lines = []
        for row in to_del[:10]:
            preview_lines.append(f"  • {self._get_cell(row, COL_NAME)}")
        if len(to_del) > 10:
            preview_lines.append(f"  ... ve {len(to_del)-10} tane daha")

        reply = QMessageBox.question(
            self, "Tekrarları Sil",
            f"{'Filtreli listede ' if filtered else ''}"
            f"{len(to_del)} adet AYNI İSİMDE içerik bulundu.\n"
            f"Her birinden 1 tanesi korunacak, kalanlar silinecek.\n"
            f"⚠️ Farklı URL'ye sahip alternatifler de silinir.\n"
            f"   Onları korumak için '🔗 Aynı URL'yi Sil' kullanın.\n\n"
            f"Silineceklerden ilk 10'u:\n" + "\n".join(preview_lines) +
            "\n\nDevam etmek istiyor musunuz?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._snapshot(f"{len(to_del)} tekrar sil")
            for row in sorted(to_del, reverse=True):
                self.table.removeRow(row)
            self.statusBar().showMessage(
                f"🗑️ {len(to_del)} tekrar eden içerik silindi. {len(seen)} benzersiz içerik kaldı.", 6000
            )

    # ── Toplu Bul & Değiştir ──────────────────────────────────────────
    def find_replace(self):
        """Regex destekli toplu bul & değiştir penceresini açar."""
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "Bilgi", "Önce bir liste yükleyin.")
            return
        checked_count = len(self._checked_rows())
        visible_count = sum(1 for r in range(self.table.rowCount())
                            if not self.table.isRowHidden(r))
        total_count   = self.table.rowCount()
        self._snapshot("Bul & Değiştir")
        dlg = FindReplaceDialog(self,
                                checked_count=checked_count,
                                visible_count=visible_count,
                                total_count=total_count)
        dlg.exec()   # modeless gibi davranır ama modal olarak açılır; Kapat ile çıkılır

    # ── Toplu Düzenle ─────────────────────────────────────────────────

    # ── Poster büyük önizleme (logo hücresine çift tık) ────────────────
    def _on_cell_double_clicked(self, row, col):
        if col != COL_LOGO:
            return
        url = (self._get_cell(row, COL_LOGO) or "").strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            return
        title = self._get_cell(row, COL_NAME) or "Poster"
        dlg = PosterPreviewDialog(self, url, title)
        dlg.exec()

    # ── Şüpheli TMDB onay ekranı ──────────────────────────────────────
    def review_doubtful(self):
        items = []
        for r in range(self.table.rowCount()):
            if self._tmdb_state(r) != "doubt":
                continue
            cand = None
            st = self.table.item(r, COL_STATUS)
            if st is not None:
                cand = st.data(ROLE_DOUBT)
            items.append({
                "row": r,
                "name": self._get_cell(r, COL_NAME),
                "candidate": cand if isinstance(cand, dict) else {},
            })
        if not items:
            QMessageBox.information(
                self, "Bilgi",
                "Şüpheli satır yok.\nÖnce 'Tümünü Doldur' çalıştırın; "
                "emin olunamayan eşleşmeler burada listelenir.")
            return
        # Aday verisi olmayanlar için tooltip'den kabaca bilgi yok —
        # yine de göster (kullanıcı TMDB'de Ara ile seçer)
        dlg = DoubtReviewDialog(self, items)
        dlg.exec()
        self._schedule_stats()

    # ── UA / Referrer şablonları ──────────────────────────────────────
    def _get_ua_templates(self):
        st = self._load_settings()
        t = st.get("ua_templates")
        if not isinstance(t, list):
            # Varsayılan şablonlar
            t = [
                {"name": "Chrome (Windows)", "ua": UA_PRESETS[0][1], "ref": ""},
                {"name": "VLC", "ua": UA_PRESETS[2][1], "ref": ""},
                {"name": "okhttp (Android)", "ua": UA_PRESETS[3][1], "ref": ""},
            ]
        return t

    def _save_ua_templates(self, templates):
        st = self._load_settings()
        st["ua_templates"] = templates
        try:
            tmp = self._settings_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._settings_path())
        except Exception as e:
            QMessageBox.warning(self, "Uyarı", f"Şablonlar kaydedilemedi:\n{e}")

    def ua_templates_dialog(self):
        templates = self._get_ua_templates()
        dlg = UATemplateDialog(self, templates)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            # Liste değişmiş olabilir (kaydet/sil) — her durumda kaydet
            self._save_ua_templates(dlg.templates)
            return
        self._save_ua_templates(dlg.templates)
        if not dlg.selected:
            return
        scope = dlg.selected.get("scope", "selected")
        ua = dlg.selected.get("ua", "")
        ref = dlg.selected.get("ref", "")
        if scope == "selected":
            checked = self._checked_rows()
            rows = checked if checked else sorted({
                idx.row() for idx in self.table.selectionModel().selectedRows()
            })
        else:
            rows = [r for r in range(self.table.rowCount())
                    if not self.table.isRowHidden(r)]
        if not rows:
            QMessageBox.information(self, "Bilgi", "Uygulanacak satır yok.")
            return
        self._snapshot(f"UA şablon uygula ({len(rows)} satır)")
        for r in rows:
            self._set_row_uaref(r, ua, ref)
        if not getattr(self, "_uaref_cols_visible", False):
            self._set_uaref_visible(True, save=False)
        self._schedule_stats()
        self.statusBar().showMessage(
            f"🌐 UA/Ref şablonu {len(rows)} satıra uygulandı.", 5000)

    def bulk_edit(self):

        """Tikli / seçili veya tüm görünür satırlarda alanları toplu değiştirir (UA/Ref dahil)."""
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "Bilgi", "Önce bir liste yükleyin.")
            return

        checked_rows = self._checked_rows()
        selected_rows = checked_rows if checked_rows else sorted(set(
            idx.row() for idx in self.table.selectionModel().selectedRows()
        ))
        visible_rows = [r for r in range(self.table.rowCount())
                        if not self.table.isRowHidden(r)]

        dlg = BulkEditDialog(self,
                             selected_count=len(selected_rows),
                             visible_count=len(visible_rows))
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result:
            return
        self._snapshot("Toplu düzenle")

        fields = dlg.result
        scope  = dlg.scope()
        target_rows = selected_rows if scope == "selected" else visible_rows

        if not target_rows:
            QMessageBox.information(self, "Bilgi", "Uygulanacak satır bulunamadı.")
            return

        changed = 0
        touched_uaref = False
        for row in target_rows:
            for field in fields:
                col   = field["col"]
                value = field["value"]
                if col in (COL_UA, COL_REF):
                    touched_uaref = True
                if col == COL_NAME and field.get("keep_ep"):
                    # Mevcut bölüm bilgisini (S01 E01 vb.) koru
                    original  = self._get_cell(row, col)
                    ep_info   = self._extract_episode_info(original)
                    new_val   = f"{value} {ep_info}".strip() if ep_info else value
                    self._set_cell(row, col, new_val)
                else:
                    self._set_cell(row, col, value)
            changed += 1

        # UA/Ref yazıldıysa sütunları otomatik göster
        if touched_uaref and not getattr(self, "_uaref_cols_visible", False):
            self._set_uaref_visible(True, save=False)

        self.statusBar().showMessage(
            f"✅ Toplu düzenleme tamamlandı — {changed} satır güncellendi.", 5000
        )

    # ── TMDB: API Test ────────────────────────────────────────────────
    def test_api_key(self):
        """Seçili satır varsa onun adıyla, yoksa 'The 100 S01 E01' ile test yapar.
        TMDB'den dönen tüm alanları gösterir."""
        row = self.table.currentRow()
        if row >= 0:
            test_name = self._get_cell(row, COL_NAME) or "The 100 S01 E01"
        else:
            test_name = "The 100 S01 E01"

        cleaned = clean_name(test_name)
        data    = tmdb_fetch(test_name)

        if data:
            QMessageBox.information(
                self, "✅ TMDB Test Sonucu",
                f"Test adı    : {test_name}\n"
                f"Temizlendi  : {cleaned}\n"
                f"─────────────────────────────\n"
                f"İçerik Adı  : {data.get('tr_name')}\n"
                f"Yıl         : {data.get('year')}\n"
                f"Grup        : {data.get('group')}\n"
                f"TMDB ID     : {data.get('tmdb_id')}\n"
                f"Logo URL    : {data.get('logo', '')[:60]}{'…' if len(data.get('logo',''))>60 else ''}\n"
                f"─────────────────────────────\n"
                f"Mevcut seçenekler: {self._get_opts()}"
            )
        else:
            QMessageBox.warning(
                self, "⚠️ Sonuç Bulunamadı",
                f"Test adı   : {test_name}\n"
                f"Temizlendi : {cleaned}\n\n"
                f"TMDB'den sonuç gelmedi.\n"
                f"API key doğru mu? İnternet bağlantısı var mı?"
            )

    # ── TMDB: API Key ─────────────────────────────────────────────────
    def set_api_key(self):
        global TMDB_API_KEY
        key, ok = QInputDialog.getText(self, "TMDB API Key", "API Key:", text=TMDB_API_KEY)
        if ok and key.strip():
            TMDB_API_KEY = key.strip()
            self.statusBar().showMessage("✅ API Key güncellendi!", 4000)

    # ── TMDB: Seçenekler ──────────────────────────────────────────────
    def tmdb_options_dialog(self):
        """Hangi alanların güncelleneceğini seçme penceresi"""
        dlg = QDialog(self)
        dlg.setWindowTitle("TMDB Güncelleme Seçenekleri")
        dlg.setMinimumWidth(320)
        if self.is_dark_mode:
            dlg.setStyleSheet("QDialog,QWidget{background:#2b2b2b;color:white}"
                              "QCheckBox{color:white} QPushButton{background:#444;color:white;"
                              "border:1px solid #666;padding:6px 16px}")
        vl = QVBoxLayout(dlg)
        vl.addWidget(QLabel("Otomatik doldurmada hangi alanlar güncellensin?"))

        opts = [
            ("logo",    "🖼️  Poster / Logo"),
            ("group",   "🏷️  Tür (group-title)"),
            ("tr_name", "📝  Türkçe İçerik Adı"),
            ("year",    "📅  Yıl"),
            ("tmdb_id", "🔢  TMDB ID"),
        ]
        self._opt_checks = {}
        saved = getattr(self, "_tmdb_opts",
                        {"logo": True, "group": True, "tr_name": True,
                         "year": True, "tmdb_id": True})
        for key, label in opts:
            cb = QCheckBox(label)
            cb.setChecked(saved.get(key, True))
            vl.addWidget(cb)
            self._opt_checks[key] = cb

        btn_row = QHBoxLayout()
        ok  = QPushButton("Kaydet"); ok.clicked.connect(dlg.accept)
        can = QPushButton("İptal");  can.clicked.connect(dlg.reject)
        btn_row.addStretch(); btn_row.addWidget(ok); btn_row.addWidget(can)
        vl.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._tmdb_opts = {k: cb.isChecked() for k, cb in self._opt_checks.items()}
            self.statusBar().showMessage("✅ Seçenekler kaydedildi.", 3000)

    def _get_opts(self):
        return getattr(self, "_tmdb_opts",
                       {"logo": True, "group": True, "tr_name": True,
                        "year": True, "tmdb_id": True})

    # ── TMDB: Manuel Arama (tek satır) ───────────────────────────────
    def quick_add_entry(self):
        """Hızlı ekle: 1) URL  2) içerik adı  3) TMDB ara & uygula → yeni satır."""
        # 1) URL
        url, ok = QInputDialog.getText(
            self, "Oto Ekle — 1/3  URL",
            "Film / dizi yayın adresini yapıştırın:\n"
            "(http://… veya https://…  ·  .m3u8 / .mp4 / stream linki)",
        )
        if not ok:
            return
        url = (url or "").strip()
        if not url:
            QMessageBox.information(self, "Bilgi", "URL boş olamaz.")
            return
        if not (url.startswith("http://") or url.startswith("https://")
                or url.startswith("rtmp://") or url.startswith("rtsp://")):
            reply = QMessageBox.question(
                self, "URL",
                "Adres http/https ile başlamıyor. Yine de devam edilsin mi?\n\n" + url[:120],
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return

        # 1b) URL sağlam mı? (link kontrolü — adım 2'ye geçmeden önce)
        self.statusBar().showMessage("URL kontrol ediliyor…", 10000)
        QApplication.processEvents()
        ua, ref = self._global_uaref_for_check()
        checker = LinkCheckWorker([])  # yalnızca _check_one / _probe için
        try:
            _row, status, color = checker._check_one(0, url, ua, ref)
        except Exception as e:
            status, color = f"HATA {e.__class__.__name__}", QColor(150, 0, 0)
        url_ok = "AKTİF" in (status or "")
        if url_ok:
            self.statusBar().showMessage(f"URL sağlam: {status}", 5000)
            QMessageBox.information(
                self, "URL kontrolü",
                f"URL sağlam görünüyor.\n\nDurum: {status}\n\n"
                f"Şimdi içerik adını girebilirsiniz.")
        else:
            self.statusBar().showMessage(f"URL sorunlu: {status}", 6000)
            reply = QMessageBox.question(
                self, "URL kontrolü",
                f"URL sağlam görünmüyor.\n\nDurum: {status}\n\n"
                f"Yine de ad + TMDB adımlarına devam etmek ister misiniz?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return

        # URL'den kaba ad tahmini (son path parçası)
        guess = ""
        try:
            from urllib.parse import unquote, urlparse
            path = unquote(urlparse(url).path or "")
            base = path.rstrip("/").split("/")[-1] if path else ""
            base = re.sub(r'\.(m3u8?|mp4|mkv|ts|avi|mov)(\?.*)?$', '', base, flags=re.I)
            base = re.sub(r'[_\.\-]+', ' ', base).strip()
            if base and not base.isdigit() and len(base) > 2:
                guess = base
        except Exception:
            pass

        # 2) İçerik adı
        name, ok = QInputDialog.getText(
            self, "Oto Ekle — 2/3  İçerik adı",
            "Film veya dizi adını yazın:\n"
            "(ör. Inception 2010  ·  The Last of Us S01E01)",
            text=guess,
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.information(self, "Bilgi", "İçerik adı boş olamaz.")
            return

        # 3) TMDB ara
        dlg = TMDBSearchDialog(self, name)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_data:
            # TMDB seçilmeden de eklenebilsin mi? sor
            reply = QMessageBox.question(
                self, "TMDB seçilmedi",
                "TMDB sonucu seçilmedi.\n"
                "Yine de yalnızca ad + URL ile satır eklensin mi?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            data = None
        else:
            data = dlg.selected_data

        # Satırı ekle (seçili satırın üstüne, yoksa en üste)
        self._snapshot("Oto Ekle")
        self.table.setSortingEnabled(False)
        secili = self.table.currentRow()
        hedef = secili if secili >= 0 else 0
        self.table.insertRow(hedef)
        self.table.setItem(hedef, COL_CHECK, self._make_check_item())
        for col in range(1, self.table.columnCount()):
            self.table.setItem(hedef, col, self._make_item(""))
        self._set_cell(hedef, COL_URL, url)
        self._set_cell(hedef, COL_NAME, name)
        self._set_cell(hedef, COL_STATUS, status if status else "Yeni")
        it_st = self.table.item(hedef, COL_STATUS)
        if it_st and color is not None:
            try:
                it_st.setBackground(color)
                it_st.setForeground(QColor("white"))
            except Exception:
                pass

        if data:
            self._apply_tmdb_data(hedef, data, self._get_opts())
            # URL TMDB ile ezilmesin; link durumu da korunsun
            self._set_cell(hedef, COL_URL, url)
            self._set_cell(hedef, COL_STATUS, status if status else "TMDB ✅")
            it_st = self.table.item(hedef, COL_STATUS)
            if it_st and color is not None:
                try:
                    it_st.setBackground(color)
                    it_st.setForeground(QColor("white"))
                except Exception:
                    pass
            # Bölüm bilgisi kullanıcı adındaysa koru
            ep = self._extract_episode_info(name)
            if ep and data.get("tr_name"):
                title = re.sub(r'\s*\(\d{4}\)', '', data["tr_name"]).strip()
                year = data.get("year") or ""
                if ep:
                    self._set_cell(hedef, COL_NAME, f"{title} {ep}".strip())
                elif year:
                    self._set_cell(hedef, COL_NAME, f"{title} ({year})")
        else:
            if not url_ok:
                self._set_cell(hedef, COL_STATUS, status or "TMDB yok")
            else:
                self._set_cell(hedef, COL_STATUS, "TMDB yok · " + (status or "AKTİF"))

        self.table.selectRow(hedef)
        self.table.scrollToItem(self.table.item(hedef, COL_NAME) or self.table.item(hedef, COL_CHECK))
        self._schedule_stats()
        if getattr(self, "_thumbs_on", False):
            self._load_visible_thumbs()
        self.statusBar().showMessage(
            f"Oto Ekle tamam — {(self._get_cell(hedef, COL_NAME) or name)[:50]}", 6000)


    def tmdb_search_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Bilgi", "Önce bir satır seçin.")
            return
        dlg = TMDBSearchDialog(self, self._get_cell(row, COL_NAME))
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_data:
            self._snapshot("TMDB bilgisi uygula")
            self._apply_tmdb_data(row, dlg.selected_data, self._get_opts())
            self.statusBar().showMessage("✅ TMDB bilgileri uygulandı.", 4000)

    # ── TMDB: Toplu Manuel Arama (işaretli/seçili satırlar) ──────────
    def tmdb_search_bulk(self):
        """
        Tikli (✔) satırlar varsa onları, yoksa tabloda ctrl/shift ile
        seçili satırları hedef alır.  Kullanıcı TEK BİR TMDB sonucu seçer
        ve bu sonuç tüm hedef satırlara uygulanır; her satırın bölüm
        bilgisi (S01 E01 gibi) ayrı ayrı korunur.
        """
        # Önce tikli satırlara bak
        target_rows = self._checked_rows()

        # Tikli yoksa, tabloda seçili (highlight) satırlara bak
        if not target_rows:
            selected_indexes = self.table.selectionModel().selectedRows()
            target_rows = sorted({idx.row() for idx in selected_indexes})

        # Hâlâ boşsa, sadece current row'u al
        if not target_rows:
            row = self.table.currentRow()
            if row < 0:
                QMessageBox.information(self, "Bilgi",
                    "Önce satırları tikleyin (✔ sütunu) veya Ctrl/Shift ile seçin.")
                return
            target_rows = [row]

        if len(target_rows) == 1:
            # Tek satır → normal tek satır araması gibi davran
            row = target_rows[0]
            dlg = TMDBSearchDialog(self, self._get_cell(row, COL_NAME))
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_data:
                self._snapshot("TMDB bilgisi uygula")
                self._apply_tmdb_data(row, dlg.selected_data, self._get_opts())
                self.statusBar().showMessage("✅ TMDB bilgileri uygulandı.", 4000)
            return

        # Çok satır → ilk satırın adını arama kutusuna doldur, başlıkta kaç satır olduğunu göster
        first_name = self._get_cell(target_rows[0], COL_NAME)
        dlg = TMDBSearchDialog(self, first_name, bulk_count=len(target_rows))
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_data:
            return

        self._snapshot(f"TMDB Toplu Arama ({len(target_rows)} satır)")
        opts = self._get_opts()
        for row in target_rows:
            self._apply_tmdb_data(row, dlg.selected_data, opts)

        self.statusBar().showMessage(
            f"✅ TMDB bilgileri {len(target_rows)} satıra uygulandı.", 5000
        )

    # ── TMDB: Tümünü Doldur ───────────────────────────────────────────
    def tmdb_auto_all(self):
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "Bilgi", "Önce bir liste yükleyin.")
            return

        # Sadece görünür satırları al (filtre aktifse sadece onlar)
        visible_rows = [r for r in range(self.table.rowCount())
                        if not self.table.isRowHidden(r)]
        total        = self.table.rowCount()
        filtered     = len(visible_rows) < total
        filtre_label = f"filtreli {len(visible_rows)}" if filtered else f"{total}"

        reply = QMessageBox.question(
            self, "TMDB Otomatik Doldur",
            f"{filtre_label} içerik işlenecek"
            f"{' (filtre aktif)' if filtered else ''}.\n\n"
            "Sadece eksik bilgisi olanları mı işleyelim?\n"
            "(Hayır = Hepsini yeniden çek)\n\n"
            "ℹ️ TMDB'nin emin olmadığı eşleşmeler DEĞİŞTİRİLMEZ;\n"
            "Durum sütununa 'ŞÜPHELİ ⚠️ TMDB' yazılır, elle karar verirsiniz.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
            QMessageBox.StandardButton.Cancel
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return

        opts = self._get_opts()
        rows_data = []
        for row in visible_rows:
            name = self._get_cell(row, COL_NAME)
            if not name:
                continue
            if reply == QMessageBox.StandardButton.Yes:
                # TMDB ❌ olan satırları her zaman tekrar dene
                is_failed = (self._tmdb_state(row) == "fail")
                missing = (
                    (opts.get("logo")    and not self._get_cell(row, COL_LOGO))   or
                    (opts.get("group")   and not self._get_cell(row, COL_GROUP))  or
                    (opts.get("year")    and not self._get_cell(row, COL_YEAR))   or
                    (opts.get("tmdb_id") and not self._get_cell(row, COL_TMDBID))
                )
                if not missing and not is_failed:
                    continue
            rows_data.append((row, name))

        if not rows_data:
            QMessageBox.information(self, "Bilgi", "Güncellenecek içerik bulunamadı.")
            return

        self._snapshot(f"TMDB Otomatik Doldur ({len(rows_data)} satır)")

        self._prog = QProgressDialog("TMDB'den bilgi çekiliyor…", "Durdur", 0, len(rows_data), self)
        self._prog.setWindowTitle("TMDB Otomatik Doldur")
        self._prog.setWindowModality(Qt.WindowModality.WindowModal)
        self._prog.show()

        self.tmdb_worker = TMDBWorker(rows_data, opts)
        self.tmdb_worker.result_ready.connect(lambda r, d: self._apply_tmdb_data(r, d, opts))
        self.tmdb_worker.not_found.connect(self._on_tmdb_not_found)
        self.tmdb_worker.uncertain.connect(self._on_tmdb_uncertain)
        self.tmdb_worker.progress.connect(self._on_prog)
        self.tmdb_worker.finished.connect(self._on_done)
        self._prog.canceled.connect(self.tmdb_worker.stop)
        self._prog_done = 0
        self.tmdb_worker.start()

    def _extract_episode_info(self, name: str) -> str:
        """Orijinal addan sadece bölüm/sezon bilgisini çıkarır ve normalize eder.
        'Lucifer S06-E10'  -> 'S06 E10'
        'The 100 S01 E01'  -> 'S01 E01'
        'Breaking Bad S03E07' -> 'S03 E07'
        'Dizi S01E100'     -> 'S01 E100'   (3 haneli bölüm)
        'Dizi 3.Bölüm'     -> '3.Bölüm'
        """
        # S01E01 / S01 E01 / S01-E01 / S01E100 formatlarını yakala (1–3 hane)
        m = re.search(r'S(\d{1,3})[\s\-]*E(\d{1,3})', name, flags=re.IGNORECASE)
        if m:
            s = m.group(1).zfill(2)
            e = m.group(2).zfill(2)  # 100 -> "100" (zfill sadece daha kısaysa doldurur)
            return f"S{s} E{e}"

        patterns = [
            r'\d{1,3}x\d{1,3}',
            r'\d+\.\s*(?:Bölüm|Sezon)',
            r'(?:Bölüm|Sezon|Episode|Season)\s*\d+',
        ]
        for pat in patterns:
            m = re.search(pat, name, flags=re.IGNORECASE)
            if m:
                return m.group(0).strip()
        return ""

    def _apply_tmdb_data(self, row, data, opts):
        # Orijinal adı al, bölüm bilgisini kaydet
        original_name = self._get_cell(row, COL_NAME)
        episode_info  = self._extract_episode_info(original_name)

        if opts.get("logo") and data.get("logo"):
            self._set_cell(row, COL_LOGO, data["logo"])
        if opts.get("group") and data.get("group"):
            self._set_cell(row, COL_GROUP, data["group"])

        # Yılı önce yaz (ad oluştururken lazım)
        year = data.get("year", "")
        if opts.get("year") and year:
            self._set_cell(row, COL_YEAR, year)

        # Ad: TMDB Türkçe adı + yıl (film) veya bölüm bilgisi (dizi)
        if opts.get("tr_name") and data.get("tr_name"):
            tmdb_title = data["tr_name"]
            # tmdb_title içindeki tüm (YYYY) parantezli yılları temizle (çift yıl önleme)
            tmdb_title_clean = re.sub(r'\s*\(\d{4}\)', "", tmdb_title).strip()
            if episode_info:
                # Dizi: "The 100 S01 E01" — yıl ada eklenmez, zaten yıl sütununda
                new_name = f"{tmdb_title_clean} {episode_info}"
            elif year:
                # Film: "Inception (2010)"
                new_name = f"{tmdb_title_clean} ({year})"
            else:
                new_name = tmdb_title_clean
            self._set_cell(row, COL_NAME, new_name)

        if opts.get("tmdb_id") and data.get("tmdb_id"):
            self._set_cell(row, COL_TMDBID, data["tmdb_id"])

        self._set_cell(row, COL_STATUS, "TMDB ✅")
        self._mark_tmdb(row, "ok")      # kalıcı işaret — link kontrolü ezemez
        it = self.table.item(row, COL_STATUS)
        if it:
            it.setBackground(QColor(0, 100, 60))
            it.setForeground(QColor("white"))
            it.setToolTip("")

    def _on_tmdb_not_found(self, row):
        """TMDB'de bulunamayan satırı kırmızı olarak işaretle."""
        self._set_cell(row, COL_STATUS, "TMDB ❌")
        self._mark_tmdb(row, "fail")    # kalıcı işaret — link kontrolü ezemez
        it = self.table.item(row, COL_STATUS)
        if it:
            it.setBackground(QColor(160, 30, 30))
            it.setForeground(QColor("white"))
            it.setToolTip("")

    def _on_tmdb_uncertain(self, row, data):
        """TMDB emin değil: hiçbir veriyi DEĞİŞTİRME, yalnızca 'ŞÜPHELİ' işaretle."""
        if self._tmdb_state(row) == "ok":
            return      # daha önce doğrulanmış/elle seçilmiş satırı bozma
        self._set_cell(row, COL_STATUS, "ŞÜPHELİ ⚠️ TMDB")
        self._mark_tmdb(row, "doubt")
        it = self.table.item(row, COL_STATUS)
        if it is None:
            it = self._make_item("ŞÜPHELİ ⚠️ TMDB")
            self.table.setItem(row, COL_STATUS, it)
        it.setBackground(QColor(180, 120, 0))
        it.setForeground(QColor("white"))
        # Aday verisini sakla (onay ekranı için)
        try:
            it.setData(ROLE_DOUBT, dict(data) if data else None)
        except Exception:
            pass
        it.setToolTip(
            "TMDB'nin bulduğu aday (UYGULANMADI):\n"
            f"  {data.get('tr_name', '?')} ({data.get('year') or '?'}) — {data.get('group', '')}\n"
            f"  TMDB ID: {data.get('tmdb_id', '')}\n"
            f"Sebep: {data.get('reason') or 'emin değil'}\n\n"
            "Onay için: ⚠️ Şüpheli Onay menüsü")

    def _on_prog(self, done, name):
        self._prog_done = done
        self._prog.setValue(done)
        self._prog.setLabelText(f"İşleniyor: {name}")

    def _on_done(self, success, fail, doubt=0):
        self._prog.close()
        msg = f"✅ Bilgi bulundu : {success}\n❌ Bulunamadı  : {fail}"
        if doubt:
            msg += (f"\n⚠️ Şüpheli     : {doubt}  (DEĞİŞTİRİLMEDİ)\n\n"
                    "Şüpheli eşleşmeleri şimdi onaylamak ister misiniz?")
            reply = QMessageBox.question(
                self, "TMDB Tamamlandı", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.review_doubtful()
            return
        QMessageBox.information(self, "TMDB Tamamlandı", msg)


    def _resolve_vlc(self):
        p = find_vlc_path()
        if not p:
            QMessageBox.warning(
                self, "VLC bulunamadı",
                "VLC yüklü değil veya PATH'te yok.\n\n"
                "https://www.videolan.org/vlc/ adresinden kurun\n"
                "veya VLC_PATH ortam değişkenine vlc.exe yolunu yazın.")
        return p

    def vlc_preview(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Bilgi", "Önce bir satır seçin.")
            return
        url = (self._get_cell(row, COL_URL) or "").strip()
        if not url:
            QMessageBox.information(self, "Bilgi", "Bu satırda URL yok.")
            return
        vlc = self._resolve_vlc()
        if not vlc:
            return
        ua, ref = self._uaref_for_check(row)
        if not ua and not ref:
            ua, ref = self._global_uaref_for_check()
        cmd = [vlc, "--no-video-title-show"]
        if ua:
            cmd.append(f"--http-user-agent={ua}")
        if ref:
            cmd.append(f"--http-referrer={ref}")
        cmd.append(url)
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.statusBar().showMessage(
                f"VLC açıldı: {(self._get_cell(row, COL_NAME) or '')[:60]}", 5000)
        except Exception as e:
            QMessageBox.critical(self, "VLC", f"Başlatılamadı:\n{e}")

    def vlc_quick_test(self):
        rows = self._checked_rows()
        if not rows:
            r = self.table.currentRow()
            if r >= 0:
                rows = [r]
        if not rows:
            QMessageBox.information(self, "Bilgi", "Test için satır seçin veya tikleyin.")
            return
        vlc = self._resolve_vlc()
        if not vlc:
            return
        secs, ok = QInputDialog.getInt(
            self, "VLC Kısa Test", "Her link kaç saniye denensin?", 8, 3, 30, 1)
        if not ok:
            return
        self._snapshot("VLC kısa test")
        prog = QProgressDialog("VLC ile test…", "Durdur", 0, len(rows), self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.show()
        active = dead = 0
        for i, row in enumerate(rows):
            if prog.wasCanceled():
                break
            prog.setValue(i)
            url = (self._get_cell(row, COL_URL) or "").strip()
            prog.setLabelText(f"{i+1}/{len(rows)}  {(self._get_cell(row, COL_NAME) or url)[:70]}")
            QApplication.processEvents()
            if not url:
                self._set_cell(row, COL_STATUS, "KAPALI 🛑")
                dead += 1
                continue
            ua, ref = self._uaref_for_check(row)
            if not ua and not ref:
                ua, ref = self._global_uaref_for_check()
            cmd = [vlc, "--intf", "dummy", "--play-and-exit",
                   f"--run-time={int(secs)}", "--no-video-title-show"]
            if ua:
                cmd.append(f"--http-user-agent={ua}")
            if ref:
                cmd.append(f"--http-referrer={ref}")
            cmd.append(url)
            try:
                kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=int(secs) + 12)
                if sys.platform.startswith("win"):
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                p = subprocess.run(cmd, **kwargs)
                ok_play = (p.returncode == 0)
            except subprocess.TimeoutExpired:
                ok_play = True
            except Exception:
                ok_play = False
            if ok_play:
                self._set_cell(row, COL_STATUS, "AKTİF ✅")
                it = self.table.item(row, COL_STATUS)
                if it:
                    it.setBackground(QColor(0, 150, 0))
                    it.setForeground(QColor("white"))
                active += 1
            else:
                self._set_cell(row, COL_STATUS, "KAPALI 🛑")
                it = self.table.item(row, COL_STATUS)
                if it:
                    it.setBackground(QColor(100, 100, 100))
                    it.setForeground(QColor("white"))
                dead += 1
        prog.close()
        QMessageBox.information(self, "VLC Test", f"Aktif: {active}\nKapalı: {dead}")

    def set_font_size(self):
        cur = int(getattr(self, "_ui_font_pt", 12) or 12)
        val, ok = QInputDialog.getInt(self, "Yazı Boyutu", "Arayüz yazı boyutu (px):", cur, 9, 22, 1)
        if ok:
            self._ui_font_pt = val
            self._apply_theme()

    def toggle_info_panel(self):
        self._info_visible = not getattr(self, "_info_visible", False)
        if hasattr(self, "_info_panel"):
            self._info_panel.setVisible(self._info_visible)

    def _on_selection_for_info(self):
        if not getattr(self, "_info_visible", False):
            return
        row = self.table.currentRow()
        if row < 0:
            return
        tid = (self._get_cell(row, COL_TMDBID) or "").strip()
        if tid and tid in getattr(self, "_detail_cache", {}):
            self._show_info_detail(self._detail_cache[tid], self._get_cell(row, COL_NAME))

    def _show_info_detail(self, detail, fallback_name=""):
        if not hasattr(self, "_info_panel") or not detail:
            return
        title = detail.get("title") or fallback_name or "?"
        cast = detail.get("cast") or []
        self._info_panel.setPlainText(
            f"{title}\nTMDB: {detail.get('tmdb_id','')} ({detail.get('media_type','')})\n\n"
            f"Özet:\n{detail.get('overview') or '(özet yok)'}\n\n"
            f"Oyuncular:\n{', '.join(cast) if cast else '(yok)'}"
        )

    def fetch_overview_cast(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Bilgi", "Önce bir satır seçin.")
            return
        tid = (self._get_cell(row, COL_TMDBID) or "").strip()
        name = self._get_cell(row, COL_NAME) or ""
        media = "tv" if _has_episode_marker(name) else "movie"
        if not tid:
            data = tmdb_fetch(name)
            if not data or not data.get("tmdb_id"):
                QMessageBox.information(self, "Bilgi", "TMDB ID yok. Önce TMDB doldurun.")
                return
            tid = data["tmdb_id"]
            media = data.get("media_type") or media
            self._set_cell(row, COL_TMDBID, tid)
        self.statusBar().showMessage("Özet/oyuncu çekiliyor…", 3000)
        QApplication.processEvents()
        detail = tmdb_details(tid, media) or tmdb_details(tid, "movie" if media == "tv" else "tv")
        if not detail:
            QMessageBox.warning(self, "TMDB", "Detay alınamadı.")
            return
        self._detail_cache[str(tid)] = detail
        tip = (detail.get("overview") or "")[:400]
        if detail.get("cast"):
            tip += "\n\nOyuncular: " + ", ".join(detail["cast"][:6])
        for col in (COL_NAME, COL_STATUS, COL_TMDBID):
            it = self.table.item(row, col)
            if it:
                it.setToolTip(tip)
        if not self._info_visible:
            self.toggle_info_panel()
        self._show_info_detail(detail, name)

    def open_new_window(self):
        w = IPTVEditor()
        w.show()
        if not hasattr(IPTVEditor, "_extra_windows"):
            IPTVEditor._extra_windows = []
        IPTVEditor._extra_windows.append(w)

    def bulk_fetch_posters(self):
        targets = []
        for r in range(self.table.rowCount()):
            if self.table.isRowHidden(r):
                continue
            if (self._get_cell(r, COL_LOGO) or "").strip():
                continue
            name = self._get_cell(r, COL_NAME) or ""
            tid = (self._get_cell(r, COL_TMDBID) or "").strip()
            if name or tid:
                targets.append((r, name, tid))
        if not targets:
            QMessageBox.information(self, "Bilgi", "Eksik posterli görünür satır yok.")
            return
        if QMessageBox.question(
            self, "Eksik Posterler",
            f"{len(targets)} satırda poster yok. TMDB'den çekilsin mi?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._snapshot(f"Eksik poster ({len(targets)})")
        prog = QProgressDialog("Posterler…", "Durdur", 0, len(targets), self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.show()
        ok = 0
        for i, (row, name, tid) in enumerate(targets):
            if prog.wasCanceled():
                break
            prog.setValue(i)
            prog.setLabelText((name or tid)[:60])
            QApplication.processEvents()
            poster = ""
            media = "tv" if _has_episode_marker(name) else "movie"
            try:
                if tid:
                    for mt in (media, "movie" if media == "tv" else "tv"):
                        d = requests.get(
                            f"{TMDB_BASE_URL}/{mt}/{tid}",
                            params={"api_key": TMDB_API_KEY}, timeout=8).json()
                        if d.get("poster_path"):
                            poster = TMDB_IMAGE_BASE + d["poster_path"]
                            break
                if not poster:
                    data = tmdb_fetch(name)
                    if data and data.get("logo"):
                        poster = data["logo"]
                        if data.get("tmdb_id") and not tid:
                            self._set_cell(row, COL_TMDBID, data["tmdb_id"])
            except Exception:
                pass
            if poster:
                self._set_cell(row, COL_LOGO, poster)
                ok += 1
        prog.close()
        self._load_visible_thumbs()
        QMessageBox.information(self, "Posterler", f"{ok} / {len(targets)} poster yazıldı.")

    def tmdb_expand_episodes(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Bilgi", "Bir dizi satırı seçin.")
            return
        tid = (self._get_cell(row, COL_TMDBID) or "").strip()
        name = self._get_cell(row, COL_NAME) or ""
        if not tid:
            data = tmdb_fetch(name)
            if not data or not data.get("tmdb_id"):
                QMessageBox.warning(self, "TMDB", "Önce TMDB eşleşmesi yapın.")
                return
            tid = data["tmdb_id"]
            self._set_cell(row, COL_TMDBID, tid)
            if data.get("logo"):
                self._set_cell(row, COL_LOGO, data["logo"])
            if data.get("group"):
                self._set_cell(row, COL_GROUP, data["group"])
        base_title = clean_name(name) or name
        logo = self._get_cell(row, COL_LOGO)
        group = self._get_cell(row, COL_GROUP) or "Dizi"
        try:
            info = requests.get(
                f"{TMDB_BASE_URL}/tv/{tid}",
                params={"api_key": TMDB_API_KEY, "language": "tr-TR"}, timeout=10).json()
            tr = info.get("name") or ""
            if tr and is_latin(tr):
                base_title = tr
            if info.get("poster_path"):
                logo = TMDB_IMAGE_BASE + info["poster_path"]
            genres = info.get("genres") or []
            if genres:
                group = GENRE_MAP.get(genres[0]["id"], group)
        except Exception:
            pass
        self.statusBar().showMessage("Bölüm listesi çekiliyor…", 4000)
        QApplication.processEvents()
        episodes = tmdb_tv_episodes(tid, "tr-TR") or tmdb_tv_episodes(tid, "en-US")
        if not episodes:
            QMessageBox.warning(self, "TMDB", "Bölüm listesi alınamadı.")
            return
        existing = set()
        for r in range(self.table.rowCount()):
            n = self._get_cell(r, COL_NAME) or ""
            m = re.search(r'S(\d{1,3})\s*E(\d{1,3})', n, re.I)
            if m and clean_name(n).lower() == clean_name(base_title).lower():
                existing.add((int(m.group(1)), int(m.group(2))))
        to_add = [x for x in episodes if (x[0], x[1]) not in existing]
        if not to_add:
            QMessageBox.information(self, "Bilgi", "Tüm bölümler zaten listede.")
            return
        preview = "\n".join(f"  {base_title} S{s:02d}E{e:02d}" for s, e, *_ in to_add[:12])
        if len(to_add) > 12:
            preview += f"\n  … +{len(to_add)-12}"
        if QMessageBox.question(
            self, "Bölümleri Ekle",
            f"{len(to_add)} yeni satır (URL boş).\n\n{preview}\n\nDevam?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._snapshot(f"TMDB bölüm ({len(to_add)})")
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            at = row + 1
            for s, e, en, yd in to_add:
                full = f"{base_title} S{s:02d}E{e:02d}"
                self.table.insertRow(at)
                self.table.setItem(at, COL_CHECK, self._make_check_item())
                self.table.setItem(at, COL_LOGO, self._make_item(logo or ""))
                self.table.setItem(at, COL_GROUP, self._make_item(group))
                self.table.setItem(at, COL_NAME, self._make_item(full))
                self.table.setItem(at, COL_YEAR, self._make_item(yd or ""))
                self.table.setItem(at, COL_TMDBID, self._make_item(tid))
                self.table.setItem(at, COL_URL, self._make_item(""))
                self.table.setItem(at, COL_UA, self._make_item(""))
                self.table.setItem(at, COL_REF, self._make_item(""))
                self.table.setItem(at, COL_STATUS, self._make_item("URL gerekli"))
                self._mark_tmdb(at, "ok")
                at += 1
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
        self._schedule_stats()
        self.statusBar().showMessage(f"{len(to_add)} bölüm eklendi (URL'leri doldurun).", 8000)

    # ── Bağlam menüsü ────────────────────────────────────────────────
    def _context_menu(self, pos):
        """Sağ tık menüsü — Windows uyumlu."""
        idx = self.table.indexAt(pos)
        if idx.isValid():
            sm = self.table.selectionModel()
            if not sm.isSelected(idx):
                self.table.selectRow(idx.row())
            self.table.setCurrentIndex(idx)
        if self.table.state() == QAbstractItemView.State.EditingState:
            self.table.closePersistentEditor(self.table.currentItem())

        checked = self._checked_rows()
        selected_indexes = self.table.selectionModel().selectedRows()
        sel_rows = sorted({i.row() for i in selected_indexes})
        bulk_count = len(checked) if checked else len(sel_rows)

        menu = QMenu(self)
        a1  = menu.addAction("Üste Yeni Satır")
        a_qa = menu.addAction("Oto Ekle (URL → Ad → TMDB)")
        a2  = menu.addAction("Sil")
        menu.addSeparator()
        a_up   = menu.addAction("Yukarı Taşı")
        a_dn   = menu.addAction("Aşağı Taşı")
        a_top  = menu.addAction("En Üste Taşı")
        a_bot  = menu.addAction("En Alta Taşı")
        menu.addSeparator()
        a3  = menu.addAction("TMDB'de Ara (Bu Satır)")
        a3b = menu.addAction(f"TMDB'de Ara — Toplu ({bulk_count} satır)") if bulk_count > 1 else None
        menu.addSeparator()
        a_vlc = menu.addAction("VLC ile Önizle")
        a_vlc_t = menu.addAction("VLC Kısa Test")
        a_info = menu.addAction("Özet / Oyuncu Getir")
        menu.addSeparator()
        a_ua = menu.addAction("UA / Referrer Düzenle (bu satır)")
        a5  = menu.addAction("Toplu Düzenle (Seçili Satırlar)")
        a5b = menu.addAction("Bul & Değiştir")
        menu.addSeparator()
        a6  = menu.addAction("Seçilileri Dışa Aktar")
        a7  = menu.addAction("Klasöre Göre Dışa Aktar")
        menu.addSeparator()
        a4  = menu.addAction("Aynı Adı Sil (tekrarlar)")
        a4b = menu.addAction("Aynı URL'yi Sil (alternatifler korunur)")
        a4c = menu.addAction("Sezon/Bölüm Biçimini Düzelt (S01E01)")

        try:
            global_pos = self.table.viewport().mapToGlobal(pos)
        except Exception:
            from PyQt6.QtGui import QCursor
            global_pos = QCursor.pos()
        act = menu.exec(global_pos)
        if act is None:
            return
        if   act == a1: self.add_new_row_top()
        elif act == a_qa: self.quick_add_entry()
        elif act == a2: self.delete_row()
        elif act == a_up: self.move_rows_up()
        elif act == a_dn: self.move_rows_down()
        elif act == a_top: self.move_rows_top()
        elif act == a_bot: self.move_rows_bottom()
        elif act == a3: self.tmdb_search_selected()
        elif a3b is not None and act == a3b: self.tmdb_search_bulk()
        elif act == a_vlc: self.vlc_preview()
        elif act == a_vlc_t: self.vlc_quick_test()
        elif act == a_info: self.fetch_overview_cast()
        elif act == a4: self.delete_duplicates()
        elif act == a4b: self.delete_duplicate_urls()
        elif act == a4c: self.normalize_episodes()
        elif act == a_ua: self.edit_row_uaref()
        elif act == a5: self.bulk_edit()
        elif act == a5b: self.find_replace()
        elif act == a6: self.export_selected()
        elif act == a7: self.export_by_group()


# ── Giriş noktası ─────────────────────────────────────────────────────
def _excepthook(exc_type, exc, tb):
    """PyQt6'da bir slot içindeki yakalanmamış hata programı sessizce kapatır.
    Bunun yerine hatayı gösterip çalışmaya devam ediyoruz."""
    import traceback
    txt = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        sys.stderr.write(txt)
    except Exception:
        pass
    try:
        QMessageBox.critical(
            None, "Beklenmeyen hata",
            f"{exc_type.__name__}: {exc}\n\nProgram çalışmaya devam ediyor. "
            f"Sorun tekrarlanırsa işlemi geri alıp (Ctrl+Z) tekrar deneyin.")
    except Exception:
        pass


if __name__ == "__main__":
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    w = IPTVEditor()
    w.show()
    sys.exit(app.exec())

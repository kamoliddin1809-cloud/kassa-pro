"""
Pul o'tkazmalari hisob-kitob boti (Telegram) + Mini App.

Har bir o'tkazma:
  Summa (¥)  ->  Kurs  ->  Dollar = Summa / Kurs
  O'tkazma xizmati = Dollar x 0.6%   (xarajat)
  Tannarx = Dollar + O'tkazma xizmati
  Foiz (0.8 / 0.9 / 1 ...)  ->  Mijozdan olingan = Dollar x (1 + foiz%)
  Foyda = Olingan - Tannarx

Chatda:
  60000 6.60 0.9    — summa, kurs, foiz -> yoziladi
  60000 6.60 9200   — summa, kurs, mijozdan olingan ($) -> yoziladi
  60000 6.60        — faqat hisoblab ko'rsatadi (yozilmaydi)
  /itog             — bugungi itog,  /itog 2026-11-03 — boshqa kun
  /hisobot          — kunlik hisobot (oxirgi 7 kun),  /hisobot 30
  /hamkor           — hamkor (faqat ko'radigan) qo'shish havolasi
  /ochir            — bugungi oxirgi yozuvni o'chirish
  /app              — Mini App

Hamkor: egasi /hamkor buyrug'i bilan havola oladi, hamkor havolani bosadi va
egasining hisobini (faqat ko'rish) ko'radi. Har kuni 22:00 da kunlik hisobot
egasiga va hamkorlarga avtomatik yuboriladi.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)

TOKEN = os.getenv("BOT_TOKEN", "BU_YERGA_TOKEN_YOZING")
BOT_NOMI = os.getenv("BOT_NOMI", "Kassa Pro")  # bot nomi (Telegram'da ko'rinadi)
TZ = ZoneInfo("Asia/Tashkent")
BASE = Path(__file__).resolve().parent
DB_PATH = os.getenv("DB_PATH", str(BASE / "hisob.db"))
PORT = int(os.getenv("PORT", "8765"))
WEBAPP_URL = None  # tunnel ishga tushgach to'ldiriladi
# Serverda (Railway/Render/VPS) doimiy manzil — tunnel kerak emas
_dom = (os.getenv("PUBLIC_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN")
        or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
PUBLIC_URL = ("https://" + _dom if _dom and not _dom.startswith("http") else _dom) or None
HOST = os.getenv("HOST", "0.0.0.0" if (PUBLIC_URL or os.getenv("RAILWAY_ENVIRONMENT")) else "127.0.0.1")
BOT_USERNAME = None
OTKAZMA_FOIZ = 0.6  # pul o'tkazish xizmati — doim 0.6%, xarajat
KUNLIK_HISOBOT_SOAT = (22, 0)
XARAJAT_BOLIMLARI = ("Kamoliddin", "Kozimjon", "Other")  # har kuni shu vaqtda kunlik hisobot yuboriladi

OYLAR = ("yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust",
         "sentabr", "oktabr", "noyabr", "dekabr")
HAFTA = ("dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba")

# ---------------- Baza ----------------

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)  # faqat shu odam bazani yuklay oladi
db = sqlite3.connect(DB_PATH)


def jadvallar_tayyorla():
    """Jadvallarni yaratadi / yangilaydi (yangi yoki yuklangan baza uchun)."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS sozlama ("
        " chat_id INTEGER PRIMARY KEY, kurs REAL, foiz REAL DEFAULT 1.0)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS amallar ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, sana TEXT, vaqt TEXT,"
        " yuan REAL, kurs REAL, foiz REAL, usd REAL, xizmat REAL, jami REAL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS hamkorlar ("
        " user_id INTEGER PRIMARY KEY, owner_id INTEGER, ism TEXT, qoshilgan TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS takliflar ("
        " kod TEXT PRIMARY KEY, owner_id INTEGER, yaratilgan TEXT)"
    )
    if "pin_hash" not in [r[1] for r in db.execute("PRAGMA table_info(sozlama)")]:
        db.execute("ALTER TABLE sozlama ADD COLUMN pin_hash TEXT")
    _ustunlar = [r[1] for r in db.execute("PRAGMA table_info(amallar)")]
    if db.execute("SELECT name FROM sqlite_master WHERE name='xarajatlar'").fetchone() and \
            "tur" not in [r[1] for r in db.execute("PRAGMA table_info(xarajatlar)")]:
        db.execute("ALTER TABLE xarajatlar ADD COLUMN tur TEXT DEFAULT 'cny'")
    for _u in ("foyda", "otkazma", "tannarx", "olingan"):
        if _u not in _ustunlar:
            db.execute(f"ALTER TABLE amallar ADD COLUMN {_u} REAL")
    # Eski (foizli) yozuvlar: mijozdan olingan = eski "jami"
    db.execute(
        f"UPDATE amallar SET otkazma = usd * {OTKAZMA_FOIZ} / 100,"
        f" tannarx = usd * (1 + {OTKAZMA_FOIZ} / 100.0), olingan = jami,"
        f" foyda = jami - usd * (1 + {OTKAZMA_FOIZ} / 100.0) WHERE olingan IS NULL"
    )
    db.execute("CREATE INDEX IF NOT EXISTS amallar_chat_sana ON amallar(chat_id, sana)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS usd_amallar ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, sana TEXT, vaqt TEXT,"
        " summa REAL, tan_foiz REAL, swift REAL, foiz REAL, xizmat REAL, tannarx REAL,"
        " olingan REAL, foyda REAL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS usd_chat_sana ON usd_amallar(chat_id, sana)")
    for _u in ("usd_tan", "usd_foiz"):
        if _u not in [r[1] for r in db.execute("PRAGMA table_info(sozlama)")]:
            db.execute(f"ALTER TABLE sozlama ADD COLUMN {_u} REAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS xarajatlar ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, sana TEXT, bolim TEXT,"
        " nomi TEXT, summa REAL, yaratilgan TEXT, tur TEXT DEFAULT 'cny')"
    )
    db.commit()


jadvallar_tayyorla()


def hozir():
    return datetime.now(TZ)


def bugun() -> str:
    return hozir().strftime("%Y-%m-%d")


def sana_togri(s) -> bool:
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def sana_nomi(sana: str, hafta=True) -> str:
    d = datetime.strptime(sana, "%Y-%m-%d")
    s = f"{d.day}-{OYLAR[d.month - 1]}"
    return f"{s}, {HAFTA[d.weekday()]}" if hafta else s


# ---------------- Hamkorlar ----------------

def egasi(uid: int):
    """(hisob egasining id si, rol). Hamkor bo'lsa — egasining hisobi, faqat ko'rish."""
    row = db.execute("SELECT owner_id FROM hamkorlar WHERE user_id=?", (uid,)).fetchone()
    return (row[0], "hamkor") if row else (uid, "ega")


def hamkorlar_royxati(owner_id: int):
    return [{"user_id": r[0], "ism": r[1], "qoshilgan": r[2]} for r in db.execute(
        "SELECT user_id, ism, qoshilgan FROM hamkorlar WHERE owner_id=? ORDER BY qoshilgan",
        (owner_id,))]


def taklif_yarat(owner_id: int) -> str:
    kod = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    db.execute("INSERT INTO takliflar(kod, owner_id, yaratilgan) VALUES(?,?,?)",
               (kod, owner_id, hozir().isoformat()))
    db.commit()
    return kod


def taklif_qabul(kod: str, user_id: int, ism: str):
    row = db.execute("SELECT owner_id, yaratilgan FROM takliflar WHERE kod=?", (kod,)).fetchone()
    if not row:
        return None, "Havola eskirgan yoki noto'g'ri."
    owner_id = row[0]
    if owner_id == user_id:
        return None, "Bu havola hamkoringiz uchun — uni o'zingiz bosmang."
    if datetime.fromisoformat(row[1]) < hozir() - timedelta(days=7):
        return None, "Havola muddati o'tgan (7 kun). Yangisini so'rang."
    db.execute("DELETE FROM takliflar WHERE kod=?", (kod,))
    db.execute("INSERT OR REPLACE INTO hamkorlar(user_id, owner_id, ism, qoshilgan) "
               "VALUES(?,?,?,?)", (user_id, owner_id, ism, hozir().isoformat()))
    db.commit()
    return owner_id, None


def oxirgi_kurs(chat_id: int):
    row = db.execute("SELECT kurs FROM sozlama WHERE chat_id=?", (chat_id,)).fetchone()
    return row[0] if row else None


def oxirgi_foiz(chat_id: int):
    row = db.execute("SELECT foiz FROM sozlama WHERE chat_id=?", (chat_id,)).fetchone()
    return row[0] if row and row[0] is not None else 1.0


def kurs_saqla(chat_id: int, kurs: float, foiz=None):
    db.execute(
        "INSERT INTO sozlama(chat_id, kurs) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET kurs=excluded.kurs",
        (chat_id, kurs),
    )
    if foiz is not None:
        db.execute("UPDATE sozlama SET foiz=? WHERE chat_id=?", (foiz, chat_id))
    db.commit()


# ---------------- Hisob ----------------

def hisobla(yuan: float, kurs: float, olingan=None):
    usd = yuan / kurs
    otkazma = usd * OTKAZMA_FOIZ / 100
    tannarx = usd + otkazma
    foyda = None if olingan is None else olingan - tannarx
    return {"usd": usd, "otkazma": otkazma, "tannarx": tannarx,
            "olingan": olingan, "foyda": foyda}


def yozuv_qosh(chat_id: int, yuan: float, kurs: float, olingan: float, foiz=None, sana=None):
    h = hisobla(yuan, kurs, olingan)
    if foiz is None:
        foiz = (olingan / h["usd"] - 1) * 100
    now = hozir()
    sana = sana or now.strftime("%Y-%m-%d")
    vaqt = now.strftime("%H:%M") if sana == now.strftime("%Y-%m-%d") else ""
    db.execute(
        "INSERT INTO amallar(chat_id, sana, vaqt, yuan, kurs, usd, otkazma, tannarx,"
        " olingan, foyda, foiz, xizmat, jami) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (chat_id, sana, vaqt, yuan, kurs, h["usd"], h["otkazma"], h["tannarx"],
         olingan, h["foyda"], foiz, h["otkazma"], olingan),
    )
    db.commit()
    kurs_saqla(chat_id, kurs, foiz)
    h["foiz"] = foiz
    return h


def yozuv_ozgartir(chat_id: int, rid: int, yuan, kurs, olingan, foiz, sana):
    row = db.execute("SELECT sana FROM amallar WHERE id=? AND chat_id=?",
                     (rid, chat_id)).fetchone()
    if not row:
        return False
    if olingan is None:
        if foiz is None:
            return False
        olingan = round(yuan / kurs * (1 + foiz / 100), 2)
    h = hisobla(yuan, kurs, olingan)
    if foiz is None:
        foiz = (olingan / h["usd"] - 1) * 100
    db.execute(
        "UPDATE amallar SET sana=?, yuan=?, kurs=?, usd=?, otkazma=?, tannarx=?, olingan=?,"
        " foyda=?, foiz=?, xizmat=?, jami=? WHERE id=? AND chat_id=?",
        (sana or row[0], yuan, kurs, h["usd"], h["otkazma"], h["tannarx"], olingan,
         h["foyda"], foiz, h["otkazma"], olingan, rid, chat_id),
    )
    db.commit()
    return True


KALITLAR = ("yuan", "usd", "otkazma", "tannarx", "olingan", "foyda")


def kun_malumoti(chat_id: int, sana: str):
    rows = db.execute(
        "SELECT id, vaqt, yuan, kurs, usd, otkazma, tannarx, olingan, foyda,"
        " (olingan / usd - 1) * 100 "
        "FROM amallar WHERE chat_id=? AND sana=? ORDER BY id",
        (chat_id, sana),
    ).fetchall()
    keys = ("id", "vaqt", "yuan", "kurs", "usd", "otkazma", "tannarx", "olingan", "foyda",
            "foiz")
    items = [dict(zip(keys, r)) for r in rows]
    jami = {k: sum(i[k] for i in items) for k in KALITLAR}
    return {"sana": sana, "items": items, "jami": jami, "otkazma_foiz": OTKAZMA_FOIZ}


def oraliq_yozuvlar(chat_id: int, dan: str, gacha: str):
    """Oraliqdagi barcha o'tkazmalar (yangi sanadan eskiga)."""
    rows = db.execute(
        "SELECT id, sana, vaqt, yuan, kurs, usd, otkazma, tannarx, olingan, foyda,"
        " (olingan / usd - 1) * 100 FROM amallar WHERE chat_id=? AND sana BETWEEN ? AND ?"
        " ORDER BY sana DESC, id",
        (chat_id, dan, gacha),
    ).fetchall()
    keys = ("id", "sana", "vaqt", "yuan", "kurs", "usd", "otkazma", "tannarx", "olingan",
            "foyda", "foiz")
    return [dict(zip(keys, r)) for r in rows]


# ---------------- Dollar o'tkazmalari ----------------
# Tannarx = summa + summa x tan_foiz% + SWIFT;  Olingan = summa x (1 + foiz%);  Foyda = olingan - tannarx

def usd_hisobla(summa, tan_foiz, swift, olingan):
    xizmat = summa * tan_foiz / 100
    tannarx = summa + xizmat + (swift or 0)
    return {"xizmat": xizmat, "tannarx": tannarx, "foyda": olingan - tannarx}


def usd_sozlama(chat_id: int):
    row = db.execute("SELECT usd_tan, usd_foiz FROM sozlama WHERE chat_id=?", (chat_id,)).fetchone()
    return {"tan": (row[0] if row and row[0] is not None else 0.7),
            "foiz": (row[1] if row and row[1] is not None else 1.0)}


def usd_yoz(chat_id, summa, tan_foiz, swift, olingan, foiz, sana, rid=None):
    h = usd_hisobla(summa, tan_foiz, swift, olingan)
    if foiz is None:
        foiz = (olingan / summa - 1) * 100
    if rid is None:
        now = hozir()
        vaqt = now.strftime("%H:%M") if sana == now.strftime("%Y-%m-%d") else ""
        db.execute(
            "INSERT INTO usd_amallar(chat_id, sana, vaqt, summa, tan_foiz, swift, foiz, xizmat,"
            " tannarx, olingan, foyda) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, sana, vaqt, summa, tan_foiz, swift, foiz, h["xizmat"], h["tannarx"],
             olingan, h["foyda"]))
    else:
        cur = db.execute(
            "UPDATE usd_amallar SET sana=?, summa=?, tan_foiz=?, swift=?, foiz=?, xizmat=?,"
            " tannarx=?, olingan=?, foyda=? WHERE id=? AND chat_id=?",
            (sana, summa, tan_foiz, swift, foiz, h["xizmat"], h["tannarx"], olingan,
             h["foyda"], rid, chat_id))
        if not cur.rowcount:
            return False
    db.execute("INSERT OR IGNORE INTO sozlama(chat_id) VALUES(?)", (chat_id,))
    db.execute("UPDATE sozlama SET usd_tan=?, usd_foiz=? WHERE chat_id=?", (tan_foiz, foiz, chat_id))
    db.commit()
    return True


USD_KEYS = ("id", "sana", "vaqt", "summa", "tan_foiz", "swift", "foiz", "xizmat", "tannarx",
            "olingan", "foyda")
USD_SUM = ("summa", "xizmat", "swift", "tannarx", "olingan", "foyda")


def usd_kun(chat_id: int, sana: str):
    rows = db.execute(f"SELECT {', '.join(USD_KEYS)} FROM usd_amallar WHERE chat_id=? AND sana=?"
                      " ORDER BY id", (chat_id, sana)).fetchall()
    items = [dict(zip(USD_KEYS, r)) for r in rows]
    jami = {k: sum((i[k] or 0) for i in items) for k in USD_SUM}
    return {"sana": sana, "items": items, "jami": jami}


def usd_hisobot(chat_id: int, dan: str, gacha: str):
    rows = db.execute(
        "SELECT sana, COUNT(*), SUM(summa), SUM(xizmat), SUM(COALESCE(swift,0)), SUM(tannarx),"
        " SUM(olingan), SUM(foyda) FROM usd_amallar WHERE chat_id=? AND sana BETWEEN ? AND ?"
        " GROUP BY sana ORDER BY sana DESC", (chat_id, dan, gacha)).fetchall()
    keys = ("sana", "soni") + USD_SUM
    days = [dict(zip(keys, r)) for r in rows]
    jami = {k: sum(d[k] for d in days) for k in keys[1:]}
    jami["kunlar"] = len(days)
    return {"days": days, "jami": jami}


def yuan_foyda_jami(chat_id, dan, gacha):
    r = db.execute("SELECT COALESCE(SUM(foyda),0) FROM amallar WHERE chat_id=? AND sana BETWEEN ? AND ?",
                   (chat_id, dan, gacha)).fetchone()
    return r[0]


def usd_foyda_jami(chat_id, dan, gacha):
    r = db.execute("SELECT COALESCE(SUM(foyda),0) FROM usd_amallar WHERE chat_id=? AND sana BETWEEN ? AND ?",
                   (chat_id, dan, gacha)).fetchone()
    return r[0]


def xarajatlar_royxati(chat_id: int, dan: str, gacha: str, tur: str = "cny"):
    """Xarajatlar bo'lim bo'yicha alohida: tur='cny' (yuan) yoki 'usd' (dollar)."""
    rows = db.execute(
        "SELECT id, sana, bolim, nomi, summa FROM xarajatlar WHERE chat_id=? AND sana BETWEEN ? AND ?"
        " AND COALESCE(tur,'cny')=? ORDER BY sana DESC, id DESC", (chat_id, dan, gacha, tur)).fetchall()
    items = [dict(zip(("id", "sana", "bolim", "nomi", "summa"), r)) for r in rows]
    jami = {b: 0.0 for b in XARAJAT_BOLIMLARI}
    for i in items:
        jami[i["bolim"]] = jami.get(i["bolim"], 0) + i["summa"]
    return {"items": items, "bolimlar": jami, "jami": sum(jami.values())}


def hisobot(chat_id: int, kunlar=None, dan=None, gacha=None):
    """Kunlik hisobot: har bir kun uchun o'tkazmalar soni va jami summalar."""
    sql = ("SELECT sana, COUNT(*), SUM(yuan), SUM(usd), SUM(otkazma), SUM(tannarx),"
           " SUM(olingan), SUM(foyda) FROM amallar WHERE chat_id=?")
    args = [chat_id]
    if kunlar:
        dan = (hozir() - timedelta(days=kunlar - 1)).strftime("%Y-%m-%d")
    if dan:
        sql += " AND sana >= ?"
        args.append(dan)
    if gacha:
        sql += " AND sana <= ?"
        args.append(gacha)
    sql += " GROUP BY sana ORDER BY sana DESC"
    keys = ("sana", "soni", "yuan", "usd", "otkazma", "tannarx", "olingan", "foyda")
    days = [dict(zip(keys, r)) for r in db.execute(sql, args).fetchall()]
    for d in days:
        d["foiz"] = (d["olingan"] / d["usd"] - 1) * 100 if d["usd"] else 0
    jami = {k: sum(d[k] for d in days) for k in keys[1:]}
    jami["foiz"] = (jami["olingan"] / jami["usd"] - 1) * 100 if jami["usd"] else 0
    jami["kunlar"] = len(days)
    return {"days": days, "jami": jami}


def f(x: float) -> str:
    """12345.6 -> '12 345.60'"""
    return f"{x:,.2f}".replace(",", " ")


def fy(x: float) -> str:
    """Yuan: 60000 -> '60 000'"""
    s = f"{x:,.2f}".replace(",", " ")
    return s[:-3] if s.endswith(".00") else s


def fz(x: float) -> str:
    """Foyda: belgisi bilan."""
    return ("+" if x >= 0 else "−") + "$" + f(abs(x))


def raqam(s: str) -> float:
    return float(s.replace(",", "."))


def matnni_ajrat(text: str):
    """'60 000 6,60 9 200' -> [60000.0, 6.6, 9200.0]. Xato bo'lsa None."""
    t = text.strip().rstrip("%").strip()
    t = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", t)
    t = re.sub(r"\b(\d{1,3})((?: \d{3})+)\b(?![.,]\d)",
               lambda m: m.group(1) + m.group(2).replace(" ", ""), t)
    qismlar = t.split()
    if not 1 <= len(qismlar) <= 3:
        return None
    try:
        sonlar = [raqam(q) for q in qismlar]
    except ValueError:
        return None
    if any(s <= 0 for s in sonlar):
        return None
    return sonlar


def kunlik_matn(owner_id: int, sana: str):
    """Yuan va dollar alohida — foydalar aralashtirilmaydi."""
    d = kun_malumoti(owner_id, sana)
    u = usd_kun(owner_id, sana)
    xc = xarajatlar_royxati(owner_id, sana, sana, "cny")
    xu = xarajatlar_royxati(owner_id, sana, sana, "usd")
    if not d["items"] and not u["items"] and not xc["jami"] and not xu["jami"]:
        return None
    qism = [f"📅 <b>{sana_nomi(sana)}</b>"]

    def xar(x, foyda):
        return (f"\n🧾 Xarajatlar: −${f(x['jami'])}\n✅ <b>Sof foyda: {fz(foyda - x['jami'])}</b>"
                if x["jami"] else "")

    if d["items"] or xc["jami"]:
        j = d["jami"]
        qism.append(
            f"\n<b>¥ Yuan</b> — {len(d['items'])} ta o'tkazma\n"
            f"Yuan: ¥{fy(j['yuan'])}\n"
            f"Mijozlardan olingan: ${f(j['olingan'])}\n"
            f"Tannarx (+{OTKAZMA_FOIZ}%): ${f(j['tannarx'])}\n"
            f"💵 Foyda: <b>{fz(j['foyda'])}</b>" + xar(xc, j["foyda"]))
    if u["items"] or xu["jami"]:
        j = u["jami"]
        qism.append(
            f"\n<b>$ Dollar</b> — {len(u['items'])} ta o'tkazma\n"
            f"Summa: ${f(j['summa'])}\n"
            f"Mijozlardan olingan: ${f(j['olingan'] - j['swift'])}\n"
            f"Tannarx: ${f(j['tannarx'] - j['swift'])}\n"
            f"💵 Foyda: <b>{fz(j['foyda'])}</b>" + xar(xu, j["foyda"]))
    return "\n".join(qism)


# ---------------- Mini App server ----------------

def init_data_tekshir(init_data: str):
    """Telegram initData imzosini tekshiradi, (user id, ism) qaytaradi yoki None."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    h = pairs.pop("hash", None)
    if not h:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, h):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > 7 * 24 * 3600:
        return None
    try:
        u = json.loads(pairs["user"])
        return int(u["id"]), (u.get("first_name") or "") + (" " + u["last_name"] if u.get("last_name") else "")
    except Exception:
        return None


def jerr(msg, status=400, **extra):
    return web.json_response({"error": msg, **extra}, status=status)


# ---------------- PIN (faqat hisob egasi uchun) ----------------

SESSIYALAR = {}          # token -> (uid, tugash_vaqti)
URINISHLAR = {}          # uid -> [xato_soni, bloklangan_gacha]
SESSIYA_MUDDATI = 6 * 3600


def _pin_hash(pin: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 120_000)
    return salt.hex() + "$" + h.hex()


def pin_bormi(uid: int) -> bool:
    row = db.execute("SELECT pin_hash FROM sozlama WHERE chat_id=?", (uid,)).fetchone()
    return bool(row and row[0])


def pin_saqla(uid: int, pin: str):
    db.execute("INSERT OR IGNORE INTO sozlama(chat_id) VALUES(?)", (uid,))
    db.execute("UPDATE sozlama SET pin_hash=? WHERE chat_id=?", (_pin_hash(pin), uid))
    db.commit()


def pin_tekshir(uid: int, pin: str) -> bool:
    row = db.execute("SELECT pin_hash FROM sozlama WHERE chat_id=?", (uid,)).fetchone()
    if not row or not row[0]:
        return False
    salt, h = row[0].split("$")
    return hmac.compare_digest(_pin_hash(pin, bytes.fromhex(salt)).split("$")[1], h)


def sessiya_och(uid: int) -> str:
    t = secrets.token_urlsafe(24)
    SESSIYALAR[t] = (uid, time.time() + SESSIYA_MUDDATI)
    for k, (_, exp) in list(SESSIYALAR.items()):
        if exp < time.time():
            SESSIYALAR.pop(k, None)
    return t


def sessiya_togri(uid: int, token) -> bool:
    v = SESSIYALAR.get(token) if isinstance(token, str) else None
    return bool(v and v[0] == uid and v[1] > time.time())


async def api_handler(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return jerr("Noto'g'ri so'rov")
    auth = init_data_tekshir(body.get("initData", ""))
    if auth is None:
        return jerr("Ruxsat yo'q. Ilovani bot orqali qayta oching.", 403)
    uid, _ism = auth
    owner, rol = egasi(uid)
    action = request.match_info["action"]

    def son(k):
        v = body.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def faqat_ega():
        return jerr("Siz faqat ko'rish huquqiga egasiz.", 403) if rol != "ega" else None

    def sana_ol(k="sana"):
        s = body.get(k)
        return s if sana_togri(s) else None

    # --- PIN: hisob egasi PIN bilan kiradi, hamkor PIN'siz faqat ko'radi ---
    def pin_ok(p):
        return isinstance(p, str) and re.fullmatch(r"\d{4}", p)

    if rol == "ega":
        if action == "pin_check":
            u = URINISHLAR.get(uid, [0, 0])
            if u[1] > time.time():
                return jerr(f"Ko'p xato. {int(u[1] - time.time()) // 60 + 1} daqiqadan keyin urinib ko'ring.", 429)
            if pin_tekshir(uid, str(body.get("pin", ""))):
                URINISHLAR.pop(uid, None)
                return web.json_response({"token": sessiya_och(uid)})
            u[0] += 1
            if u[0] >= 5:
                u = [0, time.time() + 300]
            URINISHLAR[uid] = u
            return jerr("PIN noto'g'ri", 401, qoldi=max(0, 5 - u[0]) if u[1] < time.time() else 0)
        if action == "pin_set":
            if pin_bormi(uid) and not sessiya_togri(uid, body.get("token")):
                return jerr("Avval joriy PIN bilan kiring", 401, pin=True)
            if not pin_ok(body.get("pin")):
                return jerr("PIN 4 ta raqamdan iborat bo'lsin")
            pin_saqla(uid, body["pin"])
            return web.json_response({"token": sessiya_och(uid)})
        if not sessiya_togri(uid, body.get("token")):
            return jerr("PIN kerak", 401, pin=True, pin_bor=pin_bormi(uid))

    if action == "settings":
        return web.json_response({
            "kurs": oxirgi_kurs(owner), "foiz": oxirgi_foiz(owner),
            "otkazma_foiz": OTKAZMA_FOIZ, "rol": rol, "bugun": bugun(),
            "hamkorlar": hamkorlar_royxati(owner) if rol == "ega" else [],
            "usd": usd_sozlama(owner),
        })

    if action == "day":
        sana = sana_ol() or bugun()
        return web.json_response(kun_malumoti(owner, sana))

    if action == "report":
        kun = body.get("kunlar")
        kun = int(kun) if isinstance(kun, int) and 0 < kun <= 3660 else None
        d = hisobot(owner, kun, sana_ol("dan"), sana_ol("gacha"))
        if sana_ol("dan") and sana_ol("gacha"):
            d["xarajat"] = xarajatlar_royxati(owner, sana_ol("dan"), sana_ol("gacha"), "cny")
        if body.get("items") and sana_ol("dan") and sana_ol("gacha"):
            d["items"] = oraliq_yozuvlar(owner, sana_ol("dan"), sana_ol("gacha"))
        return web.json_response(d)

    if action in ("add", "update"):
        if (e := faqat_ega()):
            return e
        yuan, kurs, olingan, foiz = son("yuan"), son("kurs"), son("olingan"), son("foiz")
        if not (yuan and yuan > 0 and kurs and kurs > 0 and olingan and olingan > 0):
            return jerr("Summa, kurs yoki olingan summa noto'g'ri")
        if foiz is not None and not -50 < foiz < 100:
            foiz = None
        sana = sana_ol() or bugun()
        if sana > bugun():
            return jerr("Kelajakdagi sanaga yozib bo'lmaydi")
        if action == "add":
            yozuv_qosh(owner, yuan, kurs, olingan, foiz, sana)
        else:
            if not yozuv_ozgartir(owner, body.get("id"), yuan, kurs, olingan, foiz, sana):
                return jerr("Yozuv topilmadi", 404)
        return web.json_response(kun_malumoti(owner, sana))

    if action == "delete":
        if (e := faqat_ega()):
            return e
        row = db.execute("SELECT sana FROM amallar WHERE id=? AND chat_id=?",
                         (body.get("id"), owner)).fetchone()
        if not row:
            return jerr("Yozuv topilmadi", 404)
        db.execute("DELETE FROM amallar WHERE id=? AND chat_id=?", (body.get("id"), owner))
        db.commit()
        return web.json_response(kun_malumoti(owner, row[0]))

    if action == "usd_day":
        return web.json_response(usd_kun(owner, sana_ol() or bugun()))

    if action == "usd_report":
        dan, gacha = sana_ol("dan"), sana_ol("gacha")
        if not (dan and gacha):
            return jerr("Sana oralig'i kerak")
        d = usd_hisobot(owner, dan, gacha)
        d["xarajat"] = xarajatlar_royxati(owner, dan, gacha, "usd")
        return web.json_response(d)

    if action in ("usd_add", "usd_update"):
        if (e := faqat_ega()):
            return e
        summa, tan, swift, olingan, foiz = (son("summa"), son("tan_foiz"), son("swift"),
                                            son("olingan"), son("foiz"))
        swift = swift or 0
        if not (summa and summa > 0 and tan is not None and 0 <= tan < 20
                and olingan and olingan > 0 and 0 <= swift < 100000):
            return jerr("Summa, foizlar yoki olingan summa noto'g'ri")
        if foiz is not None and not -50 < foiz < 100:
            foiz = None
        sana = sana_ol() or bugun()
        if sana > bugun():
            return jerr("Kelajakdagi sanaga yozib bo'lmaydi")
        ok = usd_yoz(owner, summa, tan, swift, olingan, foiz, sana,
                     body.get("id") if action == "usd_update" else None)
        if not ok:
            return jerr("Yozuv topilmadi", 404)
        return web.json_response(usd_kun(owner, sana))

    if action == "usd_delete":
        if (e := faqat_ega()):
            return e
        row = db.execute("SELECT sana FROM usd_amallar WHERE id=? AND chat_id=?",
                         (body.get("id"), owner)).fetchone()
        if not row:
            return jerr("Yozuv topilmadi", 404)
        db.execute("DELETE FROM usd_amallar WHERE id=? AND chat_id=?", (body.get("id"), owner))
        db.commit()
        return web.json_response(usd_kun(owner, row[0]))

    if action in ("exp_add", "exp_update", "exp_delete"):
        if (e := faqat_ega()):
            return e
        if action == "exp_delete":
            db.execute("DELETE FROM xarajatlar WHERE id=? AND chat_id=?", (body.get("id"), owner))
            db.commit()
            return web.json_response({"ok": True})
        summa, bolim = son("summa"), body.get("bolim")
        nomi = str(body.get("nomi") or "").strip()[:200]
        sana = sana_ol() or bugun()
        if not (summa and summa > 0) or bolim not in XARAJAT_BOLIMLARI or not nomi:
            return jerr("Bo'lim, nomi va summani to'g'ri kiriting")
        if sana > bugun():
            return jerr("Kelajakdagi sanaga yozib bo'lmaydi")
        if action == "exp_add":
            tur = body.get("tur") if body.get("tur") in ("cny", "usd") else "cny"
            db.execute("INSERT INTO xarajatlar(chat_id, sana, bolim, nomi, summa, yaratilgan, tur) "
                       "VALUES(?,?,?,?,?,?,?)", (owner, sana, bolim, nomi, summa, hozir().isoformat(), tur))
        else:
            cur = db.execute("UPDATE xarajatlar SET sana=?, bolim=?, nomi=?, summa=? "
                             "WHERE id=? AND chat_id=?", (sana, bolim, nomi, summa, body.get("id"), owner))
            if not cur.rowcount:
                return jerr("Xarajat topilmadi", 404)
        db.commit()
        return web.json_response({"ok": True})

    if action == "invite":
        if (e := faqat_ega()):
            return e
        if not BOT_USERNAME:
            return jerr("Bot hali tayyor emas, birozdan keyin urinib ko'ring")
        kod = taklif_yarat(owner)
        return web.json_response({"link": f"https://t.me/{BOT_USERNAME}?start=h_{kod}"})

    if action == "remove_partner":
        if (e := faqat_ega()):
            return e
        db.execute("DELETE FROM hamkorlar WHERE user_id=? AND owner_id=?",
                   (body.get("user_id"), owner))
        db.commit()
        return web.json_response({"hamkorlar": hamkorlar_royxati(owner)})

    return jerr("Noma'lum amal", 404)


async def index_handler(request):
    return web.FileResponse(BASE / "index.html", headers={"Cache-Control": "no-cache"})


async def health_handler(request):
    return web.Response(text="ok")


async def web_server():
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/api/{action}", api_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, HOST, PORT).start()
    except OSError:
        print(f"Port {PORT} band — bot allaqachon ishlayapti. Bu nusxa to'xtaydi.")
        raise SystemExit(3)
    print(f"[{hozir():%Y-%m-%d %H:%M:%S}] Mini App server: http://127.0.0.1:{PORT}")


def cloudflared_yol():
    p = shutil.which("cloudflared")
    if p:
        return p
    for c in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
              r"C:\Program Files\cloudflared\cloudflared.exe",
              str(BASE / "cloudflared.exe")):
        if os.path.exists(c):
            return c
    return None


def app_tugma(matn="📱 Hisobni ochish"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=matn, web_app=WebAppInfo(url=WEBAPP_URL))]])


def barcha_foydalanuvchilar():
    return {r[0] for r in db.execute(
        "SELECT chat_id FROM sozlama UNION SELECT DISTINCT chat_id FROM amallar "
        "UNION SELECT user_id FROM hamkorlar")}


async def yangi_tugma_yubor(bot: Bot):
    """Manzil o'zgarganda botdan foydalangan har bir chatga yangi tugma yuboradi."""
    for cid in barcha_foydalanuvchilar():
        await _qayta_urin(lambda: bot.send_message(
            cid, "🔄 Hisob manzili yangilandi. Shu tugmadan oching 👇\n"
                 "(pastdagi «Hisob» tugmasi ham yangilandi — eski xabarlardagi "
                 "tugmalar endi ishlamaydi)",
            reply_markup=app_tugma()), f"{cid} ga xabar", urinish=10)


async def _qayta_urin(coro_fn, nomi, urinish=30):
    """Internet uzilgan bo'lsa, amalni bir necha marta qayta urinadi."""
    for i in range(urinish):
        try:
            return await coro_fn()
        except Exception as e:
            if "blocked" in str(e) or "chat not found" in str(e):
                print(f"{nomi}: {e}")
                return
            print(f"{nomi}: {e} — 20 soniyadan keyin qayta urinaman")
            await asyncio.sleep(20)


async def tunnel_qorovul(proc, url):
    """Har daqiqada Mini App manzilini tekshiradi; 3 marta ketma-ket ochilmasa,
    tunnelni o'chiradi — keyin yangi tunnel va yangi tugma avtomatik keladi."""
    import aiohttp
    xato = 0
    await asyncio.sleep(60)
    while proc.returncode is None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(url + "/health") as r:
                    ok = r.status == 200 and (await r.text()) == "ok"
        except Exception:
            ok = False
        xato = 0 if ok else xato + 1
        if xato:
            print(f"[{hozir():%H:%M:%S}] Mini App manzili javob bermadi ({xato}/3)")
        if xato >= 3:
            print(f"[{hozir():%H:%M:%S}] Tunnel o'lgan — yangisi ochiladi")
            try:
                proc.kill()
            except Exception:
                pass
            return
        await asyncio.sleep(60)


async def tunnel_ishga_tushir(bot: Bot):
    """cloudflared orqali ommaviy https manzil oladi va «Hisob» tugmasini o'rnatadi.
    Tunnel uzilsa yoki ishlamay qolsa, yangisini ochadi va yangi tugmani yuboradi."""
    global WEBAPP_URL
    if PUBLIC_URL:
        WEBAPP_URL = PUBLIC_URL
        print(f"[{hozir():%Y-%m-%d %H:%M:%S}] Mini App (doimiy manzil): {WEBAPP_URL}")
        await _qayta_urin(lambda: bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Hisob", web_app=WebAppInfo(url=PUBLIC_URL))),
            "Menyu tugmasi")
        return
    exe = cloudflared_yol()
    if not exe:
        print("cloudflared topilmadi — Mini App ishlamaydi, oddiy bot ishlayveradi.")
        return
    while True:
        qorovul = None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            async for raw in proc.stderr:
                line = raw.decode(errors="ignore")
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and m.group(0) != WEBAPP_URL:
                    yangi = m.group(0)
                    await asyncio.sleep(8)  # DNS tarqalishi uchun
                    WEBAPP_URL = yangi
                    print(f"[{hozir():%Y-%m-%d %H:%M:%S}] Mini App tayyor: {WEBAPP_URL}")
                    await _qayta_urin(lambda: bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(text="Hisob",
                                                     web_app=WebAppInfo(url=yangi))),
                        "Menyu tugmasi")
                    # Xabar yuborilmaydi — «Hisob» tugmasi jimgina yangilanadi
                    if qorovul:
                        qorovul.cancel()
                    qorovul = asyncio.create_task(tunnel_qorovul(proc, yangi))
            await proc.wait()
        except Exception as e:
            print("Tunnel xatosi:", e)
        if qorovul:
            qorovul.cancel()
        print(f"[{hozir():%Y-%m-%d %H:%M:%S}] Tunnel uzildi, 10 soniyadan keyin qayta ulanadi...")
        WEBAPP_URL = None
        await asyncio.sleep(10)


async def kunlik_hisobot_yuborish(bot: Bot):
    """Har kuni belgilangan vaqtda egasiga va hamkorlarga kunlik hisobot yuboradi."""
    while True:
        now = hozir()
        keyin = now.replace(hour=KUNLIK_HISOBOT_SOAT[0], minute=KUNLIK_HISOBOT_SOAT[1],
                            second=0, microsecond=0)
        if keyin <= now:
            keyin += timedelta(days=1)
        await asyncio.sleep((keyin - now).total_seconds())
        sana = bugun()
        egalar = {r[0] for r in db.execute(
            "SELECT DISTINCT chat_id FROM amallar WHERE sana=? "
            "UNION SELECT DISTINCT chat_id FROM usd_amallar WHERE sana=?", (sana, sana))}
        for owner in egalar:
            matn = kunlik_matn(owner, sana)
            if not matn:
                continue
            qabul = [owner] + [h["user_id"] for h in hamkorlar_royxati(owner)]
            for cid in qabul:
                await _qayta_urin(lambda: bot.send_message(
                    cid, "🌙 <b>Kunlik hisobot</b>\n" + matn, parse_mode="HTML",
                    reply_markup=app_tugma("📊 Batafsil") if WEBAPP_URL else None),
                    f"{cid} ga kunlik hisobot", urinish=10)
        await asyncio.sleep(61)


# ---------------- Bot ----------------

dp = Dispatcher()

YORDAM = (
    "💱 <b>Kassa Pro</b> — pul o'tkazmalari hisobi\n\n"
    "📱 Eng qulayi: pastdagi <b>«Hisob»</b> tugmasi (yoki /app).\n\n"
    "Chatda bitta qatorga: <b>summa  kurs  foiz</b>\n"
    "<code>60000 6.60 0.9</code>  yoki olingan dollar bilan  <code>60000 6.60 9200</code>\n\n"
    "📊 /itog — bugungi itog\n"
    "📅 /hisobot — kunlik hisobot (oxirgi 7 kun), /hisobot 30\n"
    "🤝 /hamkor — hamkorni qo'shish (faqat ko'radi)\n"
    "↩️ /ochir — bugungi oxirgi yozuvni o'chirish"
)

YORDAM_HAMKOR = (
    "🤝 <b>Siz hamkor sifatida ulangansiz</b>\n\n"
    "Hisobni pastdagi <b>«Hisob»</b> tugmasidan ko'rasiz (faqat ko'rish).\n"
    "📊 /itog — bugungi itog\n"
    "📅 /hisobot — kunlik hisobot\n"
    "Har kuni kechqurun kunlik hisobot o'zi keladi."
)


def ism_ol(m: Message) -> str:
    u = m.from_user
    return " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or str(u.id))


@dp.message(Command("start", "help", "yordam"))
async def start(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if arg.startswith("h_"):
        owner, xato = taklif_qabul(arg[2:], m.from_user.id, ism_ol(m))
        if xato:
            await m.answer("⚠️ " + xato)
            return
        await m.answer("✅ Ulandingiz!\n\n" + YORDAM_HAMKOR, parse_mode="HTML",
                       reply_markup=app_tugma() if WEBAPP_URL else None)
        await _qayta_urin(lambda: m.bot.send_message(
            owner, f"🤝 <b>{ism_ol(m)}</b> hamkor sifatida ulandi (faqat ko'radi).",
            parse_mode="HTML"), "egaga xabar", urinish=3)
        return
    owner, rol = egasi(m.from_user.id)
    if rol == "hamkor":
        await m.answer(YORDAM_HAMKOR, parse_mode="HTML",
                       reply_markup=app_tugma() if WEBAPP_URL else None)
        return
    if oxirgi_kurs(m.chat.id) is None:  # tugma yuborish ro'yxatiga qo'shish
        db.execute("INSERT OR IGNORE INTO sozlama(chat_id) VALUES(?)", (m.chat.id,))
        db.commit()
    await m.answer(YORDAM, parse_mode="HTML",
                   reply_markup=app_tugma() if WEBAPP_URL else None)


@dp.message(Command("app"))
async def app_cmd(m: Message):
    if not WEBAPP_URL:
        await m.answer("Mini App hozir tayyor emas, bir daqiqadan keyin urinib ko'ring.")
        return
    await m.answer("Hisob ilovasini oching 👇", reply_markup=app_tugma())


@dp.message(Command("hamkor"))
async def hamkor_cmd(m: Message):
    owner, rol = egasi(m.from_user.id)
    if rol != "ega":
        await m.answer("Bu buyruq faqat hisob egasi uchun.")
        return
    if not BOT_USERNAME:
        await m.answer("Bot hali tayyor emas, birozdan keyin urinib ko'ring.")
        return
    kod = taklif_yarat(owner)
    link = f"https://t.me/{BOT_USERNAME}?start=h_{kod}"
    bor = hamkorlar_royxati(owner)
    royxat = ("\n\nHozirgi hamkorlar: " + ", ".join(h["ism"] for h in bor)) if bor else ""
    await m.answer(
        "🤝 <b>Hamkor qo'shish</b>\n\nShu havolani hamkoringizga yuboring. U bosganda "
        "hisobingizni <b>faqat ko'rish</b> huquqi bilan ulanadi (o'zgartira olmaydi).\n\n"
        f"{link}\n\n<i>Havola bir marta ishlaydi, 7 kun amal qiladi.</i>{royxat}",
        parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("itog"))
async def itog_cmd(m: Message, command: CommandObject):
    owner, _ = egasi(m.from_user.id)
    sana = (command.args or "").strip() or bugun()
    if not sana_togri(sana):
        await m.answer("Sana shunday yoziladi: <code>/itog 2026-11-03</code>", parse_mode="HTML")
        return
    d = kun_malumoti(owner, sana)
    if not d["items"]:
        await m.answer(f"📭 {sana_nomi(sana)} — yozuv yo'q.")
        return
    satrlar = [f"📊 <b>Itog — {sana_nomi(sana)}</b>\n"]
    for i, it in enumerate(d["items"], 1):
        satrlar.append(
            f"{i}. ¥{fy(it['yuan'])} ÷ {it['kurs']} · {round(it['foiz'], 3)}% → "
            f"olindi ${f(it['olingan'])}, foyda <b>{fz(it['foyda'])}</b>"
        )
    j = d["jami"]
    satrlar.append(
        f"\n<b>Jami {len(d['items'])} ta o'tkazma</b>\n"
        f"Yuan: <b>¥{fy(j['yuan'])}</b>\n"
        f"Dollar: ${f(j['usd'])}\n"
        f"O'tkazma xizmati {OTKAZMA_FOIZ}%: ${f(j['otkazma'])}\n"
        f"Tannarx: <b>${f(j['tannarx'])}</b>\n"
        f"Mijozlardan olingan: <b>${f(j['olingan'])}</b>\n"
        f"💵 Foyda: <b>{fz(j['foyda'])}</b>"
    )
    await m.answer("\n".join(satrlar), parse_mode="HTML")


@dp.message(Command("hisobot"))
async def hisobot_cmd(m: Message, command: CommandObject):
    owner, _ = egasi(m.from_user.id)
    try:
        kun = int((command.args or "7").strip())
    except ValueError:
        kun = 7
    d = hisobot(owner, kun)
    if not d["days"]:
        await m.answer(f"📭 Oxirgi {kun} kunda o'tkazma yo'q.")
        return
    satrlar = [f"📅 <b>Kunlik hisobot — oxirgi {kun} kun</b>\n"]
    for k in d["days"]:
        satrlar.append(
            f"<b>{sana_nomi(k['sana'], hafta=False)}</b> — foyda <b>{fz(k['foyda'])}</b>\n"
            f"   {k['soni']} ta o'tkazma · ¥{fy(k['yuan'])} · olingan ${f(k['olingan'])}"
        )
    j = d["jami"]
    satrlar.append(
        f"\n<b>Jami: {j['soni']} ta o'tkazma, {j['kunlar']} kun</b>\n"
        f"Yuan: ¥{fy(j['yuan'])}\n"
        f"Mijozlardan olingan: ${f(j['olingan'])}\n"
        f"💵 Foyda: <b>{fz(j['foyda'])}</b>"
    )
    await m.answer("\n".join(satrlar), parse_mode="HTML")


@dp.message(Command("ochir"))
async def ochir_cmd(m: Message):
    owner, rol = egasi(m.from_user.id)
    if rol != "ega":
        await m.answer("Siz faqat ko'rish huquqiga egasiz.")
        return
    row = db.execute(
        "SELECT id, yuan, olingan FROM amallar WHERE chat_id=? AND sana=? "
        "ORDER BY id DESC LIMIT 1",
        (owner, bugun()),
    ).fetchone()
    if not row:
        await m.answer("Bugun o'chiradigan yozuv yo'q.")
        return
    db.execute("DELETE FROM amallar WHERE id=?", (row[0],))
    db.commit()
    await m.answer(f"↩️ O'chirildi: ¥{fy(row[1])} (olingan ${f(row[2])})")


NAMUNA = ("Bitta qatorga yozing: <b>summa  kurs  foiz</b>\n"
          "Masalan: <code>60000 6.60 0.9</code>\n"
          "yoki olingan dollar bilan: <code>60000 6.60 9200</code>")


@dp.message(F.text & ~F.text.startswith("/"))
async def summa(m: Message):
    owner, rol = egasi(m.from_user.id)
    if rol != "ega":
        await m.answer("Siz faqat ko'rish huquqiga egasiz. Hisobni «Hisob» tugmasidan ko'ring.")
        return
    sonlar = matnni_ajrat(m.text)
    if sonlar is None or len(sonlar) < 2:
        await m.answer(NAMUNA, parse_mode="HTML")
        return

    yuan, kurs = sonlar[0], sonlar[1]
    if len(sonlar) == 2:
        h = hisobla(yuan, kurs)
        await m.answer(
            f"🧮 <i>Faqat hisob (yozilmadi)</i>\n"
            f"¥{fy(yuan)} ÷ {kurs} = ${f(h['usd'])}\n"
            f"O'tkazma xizmati {OTKAZMA_FOIZ}% = ${f(h['otkazma'])}\n"
            f"Tannarx: <b>${f(h['tannarx'])}</b>\n\n"
            f"Yozish uchun foizni ham qo'shing:\n"
            f"<code>{fy(yuan).replace(' ', '')} {kurs} {oxirgi_foiz(owner)}</code>",
            parse_mode="HTML",
        )
        return

    uchinchi = sonlar[2]
    if uchinchi < 20 or m.text.strip().endswith("%"):   # foiz yozilgan
        foiz = uchinchi
        olingan = round(yuan / kurs * (1 + foiz / 100), 2)
    else:                                              # olingan dollar yozilgan
        foiz = None
        olingan = uchinchi
    h = yozuv_qosh(owner, yuan, kurs, olingan, foiz)
    soni = db.execute(
        "SELECT COUNT(*) FROM amallar WHERE chat_id=? AND sana=?",
        (owner, bugun()),
    ).fetchone()[0]
    await m.answer(
        f"✅ <b>#{soni}</b> yozildi\n"
        f"¥{fy(yuan)} ÷ {kurs} = ${f(h['usd'])}\n"
        f"O'tkazma xizmati {OTKAZMA_FOIZ}% = ${f(h['otkazma'])}\n"
        f"Tannarx: ${f(h['tannarx'])}\n"
        f"Mijozdan olingan ({round(h['foiz'], 3)}%): ${f(olingan)}\n"
        f"💵 Foyda: <b>{fz(h['foyda'])}</b>",
        parse_mode="HTML",
    )


# ---------------- Bazani yuklash (kompyuterdan serverga ko'chirish) ----------------

@dp.message(F.document)
async def baza_yuklash(m: Message):
    global db
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".db"):
        return
    if not ADMIN_ID or m.from_user.id != ADMIN_ID:
        await m.answer("⛔ Bazani faqat bot egasi yuklay oladi.")
        return
    tmp = DB_PATH + ".yangi"
    await m.bot.download(doc, destination=tmp)
    try:
        t = sqlite3.connect(tmp)
        n = t.execute("SELECT COUNT(*) FROM amallar").fetchone()[0]
        t.execute("PRAGMA integrity_check").fetchone()
        t.close()
    except Exception as e:
        os.remove(tmp)
        await m.answer(f"❌ Fayl yaroqsiz: {e}")
        return
    db.close()
    if os.path.exists(DB_PATH):
        os.replace(DB_PATH, DB_PATH + f".zaxira-{hozir():%Y%m%d-%H%M%S}")
    os.replace(tmp, DB_PATH)
    db = sqlite3.connect(DB_PATH)
    jadvallar_tayyorla()
    await m.answer(f"✅ Baza yuklandi: {n} ta yuan o'tkazma. Eski baza zaxiraga olindi.")


async def main():
    global BOT_USERNAME
    if not TOKEN or TOKEN == "BU_YERGA_TOKEN_YOZING":
        raise SystemExit("BOT_TOKEN o'rnatilmagan (server sozlamalarida Variables ga qo'shing)")
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    bot = Bot(TOKEN)
    await web_server()

    async def tayyorla():
        global BOT_USERNAME
        me = await _qayta_urin(bot.get_me, "Bot ma'lumoti")
        if me:
            BOT_USERNAME = me.username
            print(f"Bot: @{BOT_USERNAME} ({me.first_name})")
            if BOT_NOMI and me.first_name != BOT_NOMI:
                await _qayta_urin(lambda: bot.set_my_name(name=BOT_NOMI), "Bot nomi", 3)
                print(f"Bot nomi o'zgartirildi: {BOT_NOMI}")

    asyncio.create_task(tayyorla())
    asyncio.create_task(tunnel_ishga_tushir(bot))
    asyncio.create_task(kunlik_hisobot_yuborish(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

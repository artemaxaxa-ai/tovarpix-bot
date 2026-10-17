# -*- coding: utf-8 -*-
"""
TovarPix — Telegram-бот карточек товаров для маркетплейсов.

Схема работы:
  фото -> Magic Hour вырезает товар -> Gemini чуть чистит пыль/царапины (по желанию)
  -> Pillow рисует фон, тень, текст по готовому шаблону стиля -> готовая карточка.
Текст рисует ТОЛЬКО Pillow, нейросеть текст не рисует.
"""
import os
import io
import json
import time
import base64
import logging
import threading
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests
import psycopg2
from flask import Flask, request
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageChops

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tovarpix")


# ============================================================
# 1. НАСТРОЙКИ (ключи берутся из Render -> Environment)
# ============================================================
def env(*names, default=""):
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return default


BOT_TOKEN = env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TOKEN")
MAGIC_KEY = env("MAGIC_HOUR_API_KEY", "MAGICHOUR_API_KEY", "MAGIC_HOUR_KEY", "MAGIC_API_KEY")
GEMINI_KEY = env("GEMINI_API_KEY", "GOOGLE_API_KEY")
DATABASE_URL = env("DATABASE_URL")
BASE_URL = env("WEBHOOK_URL", "RENDER_EXTERNAL_URL", "BASE_URL").rstrip("/")
ADMIN_ID = env("ADMIN_ID")

GEMINI_TEXT_MODEL = env("GEMINI_TEXT_MODEL", default="gemini-2.5-flash")
GEMINI_IMAGE_MODEL = env("GEMINI_IMAGE_MODEL", default="gemini-2.5-flash-image")
RETOUCH_ENABLED = env("RETOUCH_ENABLED", default="1") != "0"  # 0 = выключить чистку пыли

# 1 звезда = 1 карточка. Пакеты: сколько звезд платит человек (кредитов столько же).
PACKS = [7, 21, 70]
MAX_PHOTOS = 7
PARALLEL = 3  # сколько карточек делаем одновременно

API = "https://api.telegram.org/bot%s" % BOT_TOKEN
FILE_API = "https://api.telegram.org/file/bot%s" % BOT_TOKEN
MH = "https://api.magichour.ai/v1"

SIZES = {
    "3x4": ("3:4 · Wildberries / Ozon", 900, 1200),
    "1x1": ("1:1 · квадрат", 1000, 1000),
    "9x16": ("9:16 · TikTok / Stories", 1080, 1920),
}


# ============================================================
# 2. TELEGRAM (простые вызовы без лишних библиотек)
# ============================================================
def tg(method, **params):
    try:
        r = requests.post("%s/%s" % (API, method), json=params, timeout=60)
        return r.json()
    except Exception:
        log.error("tg %s failed: %s", method, traceback.format_exc())
        return {}


def send_doc(chat_id, data, filename, caption=""):
    r = requests.post(
        "%s/sendDocument" % API,
        data={"chat_id": chat_id, "caption": caption},
        files={"document": (filename, data, "image/jpeg")},
        timeout=120,
    )
    if not r.ok:
        raise RuntimeError("sendDocument: %s" % r.text[:300])


def tg_download(file_id):
    info = tg("getFile", file_id=file_id)
    path = info["result"]["file_path"]
    r = requests.get("%s/%s" % (FILE_API, path), timeout=60)
    r.raise_for_status()
    return r.content


def ikb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for (t, d) in row] for row in rows]}


def show(chat_id, text, kb=None, edit_mid=None):
    """Показать сообщение: если можно, правим старое, иначе шлем новое."""
    if edit_mid:
        params = {"chat_id": chat_id, "message_id": edit_mid, "text": text}
        if kb:
            params["reply_markup"] = kb
        res = tg("editMessageText", **params)
        if res.get("ok"):
            return edit_mid
    params = {"chat_id": chat_id, "text": text}
    if kb:
        params["reply_markup"] = kb
    res = tg("sendMessage", **params)
    return res.get("result", {}).get("message_id")


# ============================================================
# 3. БАЗА ДАННЫХ (PostgreSQL). Свои таблицы tp_*, старые не трогаем
# ============================================================
def q(sql, args=(), fetch=False):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                if fetch:
                    return cur.fetchall()
    finally:
        conn.close()
    return None


def init_db():
    q("""CREATE TABLE IF NOT EXISTS tp_users (
            uid BIGINT PRIMARY KEY,
            credits INTEGER NOT NULL DEFAULT 0,
            free_used BOOLEAN NOT NULL DEFAULT FALSE)""")
    q("""CREATE TABLE IF NOT EXISTS tp_sessions (
            uid BIGINT PRIMARY KEY,
            data JSONB NOT NULL)""")
    q("""CREATE TABLE IF NOT EXISTS tp_payments (
            charge_id TEXT PRIMARY KEY,
            uid BIGINT NOT NULL,
            stars INTEGER NOT NULL)""")


def get_user(uid):
    q("INSERT INTO tp_users(uid) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
    row = q("SELECT credits, free_used FROM tp_users WHERE uid=%s", (uid,), fetch=True)[0]
    return int(row[0]), bool(row[1])


def add_credits(uid, n):
    q("INSERT INTO tp_users(uid) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
    q("UPDATE tp_users SET credits=credits+%s WHERE uid=%s", (n, uid))


def spend(uid, n):
    rows = q("UPDATE tp_users SET credits=credits-%s WHERE uid=%s AND credits>=%s RETURNING credits",
             (n, uid, n), fetch=True)
    return bool(rows)


def take_free(uid):
    rows = q("UPDATE tp_users SET free_used=TRUE WHERE uid=%s AND free_used=FALSE RETURNING uid",
             (uid,), fetch=True)
    return bool(rows)


def give_back_free(uid):
    q("UPDATE tp_users SET free_used=FALSE WHERE uid=%s", (uid,))


def get_session(uid):
    rows = q("SELECT data FROM tp_sessions WHERE uid=%s", (uid,), fetch=True)
    if rows and rows[0][0]:
        d = rows[0][0]
        if isinstance(d, str):
            d = json.loads(d)
        return d
    return {"state": "idle", "photos": []}


def save_session(uid, data):
    q("""INSERT INTO tp_sessions(uid, data) VALUES(%s, %s::jsonb)
         ON CONFLICT (uid) DO UPDATE SET data=EXCLUDED.data""", (uid, json.dumps(data)))


# ============================================================
# 4. СТИЛИ: 10 готовых шаблонов (фон, свет и места текста всегда одни и те же)
# ============================================================
def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# Боксы задаются долями холста: (x, y, ширина, высота)
STYLES = {
    "minimal": dict(
        name="⚪ Минимализм", top="#F8F8F6", bottom="#E7E7E3", ambient="#F2F2EE", shadow=0.30,
        title_c="#161616", sub_c="#5C5C5C", bul_c="#2A2A2A", accent="#161616", upper=False,
        align="left", bullets="dots", chip_text="#FFFFFF",
        prod=(0.10, 0.30, 0.80, 0.54), title=(0.08, 0.06, 0.84, 0.15), sub=(0.08, 0.215, 0.84, 0.07),
        bul=(0.08, 0.855, 0.84, 0.12)),
    "luxury": dict(
        name="🖤 Люкс", top="#0C0C0E", bottom="#1E1A13", ambient="#2A2418", shadow=0.55,
        title_c="#E8C77A", sub_c="#CFC6B0", bul_c="#E6DFCC", accent="#C9A24B", upper=True,
        align="center", bullets="dots", chip_text="#101010",
        prod=(0.12, 0.31, 0.76, 0.50), title=(0.10, 0.08, 0.80, 0.14), sub=(0.10, 0.235, 0.80, 0.06),
        bul=(0.10, 0.84, 0.80, 0.11)),
    "cozy": dict(
        name="🧸 Уют", top="#F6EBDA", bottom="#EBD6B8", ambient="#E8D2B2", shadow=0.28,
        title_c="#5A3E2B", sub_c="#7B5A43", bul_c="#5A3E2B", accent="#B8774A", upper=False,
        align="left", bullets="dots", chip_text="#FFFFFF",
        prod=(0.10, 0.20, 0.80, 0.50), title=(0.08, 0.725, 0.84, 0.12), sub=(0.08, 0.85, 0.84, 0.06),
        bul=(0.08, 0.055, 0.84, 0.12)),
    "market": dict(
        name="🛒 Маркет", top="#FFFFFF", bottom="#EEF1FA", ambient="#F4F6FC", shadow=0.28,
        title_c="#FFFFFF", sub_c="#FFE3E8", bul_c="#FFFFFF", accent="#FF3355", upper=True,
        align="center", bullets="chips", chip_text="#FFFFFF",
        prod=(0.10, 0.26, 0.80, 0.57), title=(0.05, 0.03, 0.90, 0.12), sub=(0.05, 0.15, 0.90, 0.05),
        bul=(0.08, 0.855, 0.84, 0.12)),
    "tech": dict(
        name="⚡ Техно", top="#08101E", bottom="#14213D", ambient="#16294A", shadow=0.55,
        title_c="#FFFFFF", sub_c="#7DD3E8", bul_c="#DDEBF2", accent="#22D3EE", upper=True,
        align="left", bullets="dots", chip_text="#06202A",
        prod=(0.10, 0.30, 0.80, 0.54), title=(0.08, 0.07, 0.84, 0.14), sub=(0.08, 0.225, 0.84, 0.06),
        bul=(0.08, 0.855, 0.84, 0.12)),
    "eco": dict(
        name="🌿 Эко", top="#EAF4E3", bottom="#CFE6C4", ambient="#DDEED2", shadow=0.28,
        title_c="#24462A", sub_c="#41704A", bul_c="#24462A", accent="#4E9A58", upper=False,
        align="center", bullets="dots", chip_text="#FFFFFF",
        prod=(0.12, 0.18, 0.76, 0.54), title=(0.08, 0.74, 0.84, 0.12), sub=(0.08, 0.865, 0.84, 0.06),
        bul=(0.08, 0.05, 0.84, 0.12)),
    "fashion": dict(
        name="👗 Фэшн", top="#F1EAE7", bottom="#E8DDD8", ambient="#E8D3CC", shadow=0.30,
        title_c="#1F1F1F", sub_c="#6B5A55", bul_c="#2B2B2B", accent="#2B2B2B", upper=True,
        align="left", bullets="dots", chip_text="#FFFFFF",
        prod=(0.20, 0.20, 0.64, 0.50), title=(0.08, 0.76, 0.84, 0.12), sub=(0.08, 0.885, 0.84, 0.05),
        bul=(0.08, 0.05, 0.50, 0.12)),
    "info": dict(
        name="📊 Инфографика", top="#F4F6FA", bottom="#E6EBF3", ambient="#F4F6FA", shadow=0.22,
        title_c="#0F2A5C", sub_c="#52627F", bul_c="#FFFFFF", accent="#2563EB", upper=False,
        align="left", bullets="chips", chip_text="#FFFFFF",
        prod=(0.12, 0.27, 0.76, 0.50), title=(0.07, 0.05, 0.86, 0.13), sub=(0.07, 0.185, 0.86, 0.06),
        bul=(0.07, 0.80, 0.86, 0.17)),
    "pop": dict(
        name="🎉 Поп", top="#FFD60A", bottom="#FFC800", ambient="#FFE066", shadow=0.35,
        title_c="#111111", sub_c="#111111", bul_c="#111111", accent="#FF2E93", upper=True,
        align="center", bullets="dots", chip_text="#FFFFFF",
        prod=(0.10, 0.28, 0.80, 0.55), title=(0.06, 0.04, 0.88, 0.15), sub=(0.06, 0.195, 0.88, 0.06),
        bul=(0.08, 0.86, 0.84, 0.11)),
    "cinematic": dict(
        name="🎬 Кино", top="#05080B", bottom="#0E2A33", ambient="#123640", shadow=0.60,
        title_c="#FFFFFF", sub_c="#9FC9D3", bul_c="#D8E8EC", accent="#5EC4D6", upper=True,
        align="center", bullets="dots", chip_text="#04161B",
        prod=(0.12, 0.18, 0.76, 0.54), title=(0.08, 0.74, 0.84, 0.10), sub=(0.08, 0.845, 0.84, 0.05),
        bul=(0.10, 0.075, 0.80, 0.10)),
}
STYLE_ORDER = ["minimal", "luxury", "cozy", "market", "tech", "eco", "fashion", "info", "pop", "cinematic"]
NO_TEXT_BOX = (0.09, 0.10, 0.82, 0.80)  # где стоит товар, если текста нет


# ---------- шрифты ----------
FONT_URLS = [
    "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
]
_font_lock = threading.Lock()
_font_path = None


def ensure_font():
    global _font_path
    with _font_lock:
        if _font_path:
            return _font_path
        local = ["fonts/Montserrat.ttf", "fonts/font.ttf", "/tmp/Montserrat.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
        for p in local[:3]:
            if os.path.exists(p):
                _font_path = p
                return p
        for url in FONT_URLS:
            try:
                r = requests.get(url, timeout=40)
                if r.ok and len(r.content) > 50000:
                    with open("/tmp/Montserrat.ttf", "wb") as f:
                        f.write(r.content)
                    _font_path = "/tmp/Montserrat.ttf"
                    log.info("font downloaded")
                    return _font_path
            except Exception:
                log.warning("font download failed: %s", url)
        for p in local[3:]:
            if os.path.exists(p):
                _font_path = p
                return p
        return None


@lru_cache(maxsize=256)
def get_font(size, weight=700):
    path = ensure_font()
    if not path:
        return ImageFont.load_default()
    f = ImageFont.truetype(path, int(size))
    try:  # если шрифт переменный — выставляем жирность
        vals = []
        for a in f.get_variation_axes():
            nm = a.get("name", b"")
            nm = nm.decode() if isinstance(nm, bytes) else str(nm)
            if "eight" in nm:
                vals.append(max(a["minimum"], min(a["maximum"], weight)))
            else:
                vals.append(a["default"])
        f.set_variation_by_axes(vals)
    except Exception:
        pass
    return f


# ---------- рисование фона ----------
def gradient(W, H, top, bottom):
    mask = Image.linear_gradient("L").resize((W, H))
    return Image.composite(Image.new("RGB", (W, H), rgb(bottom)), Image.new("RGB", (W, H), rgb(top)), mask)


def glow(img, cx, cy, rx, ry, color, op=0.5):
    m = Image.new("L", img.size, 0)
    ImageDraw.Draw(m).ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=int(255 * op))
    m = m.filter(ImageFilter.GaussianBlur(max(2, min(rx, ry) * 0.6)))
    img.paste(Image.new("RGB", img.size, color), (0, 0), m)
    return img


def shapes(img, fn):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    fn(ImageDraw.Draw(layer))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def vignette(img, strength=0.7):
    m = Image.radial_gradient("L").resize(img.size)
    m = m.point(lambda v: int(max(0, v - 90) * strength * 1.6))
    img.paste(Image.new("RGB", img.size, (0, 0, 0)), (0, 0), m)
    return img


def decor(key, img, W, H, pb, has_text):
    st = STYLES[key]
    acc = rgb(st["accent"])
    px0, py0, px1, py1 = pb
    pcx, pcy = (px0 + px1) / 2, (py0 + py1) / 2
    pw, ph = px1 - px0, py1 - py0

    if key == "minimal":
        img = glow(img, pcx, pcy, pw * 0.62, ph * 0.62, (255, 255, 255), 0.9)
        if has_text:
            img = shapes(img, lambda d: d.rectangle([W * .08, H * .292, W * .17, H * .292 + 4], fill=acc + (255,)))
    elif key == "luxury":
        img = glow(img, pcx, pcy, pw * 0.65, ph * 0.6, rgb("#5A4720"), 0.55)
        g = acc

        def f(d):
            m = int(W * .035)
            d.rectangle([m, m, W - m, H - m], outline=g + (255,), width=3)
            m2 = int(W * .05)
            d.rectangle([m2, m2, W - m2, H - m2], outline=g + (120,), width=1)
            if has_text:
                d.line([W * .42, H * .31, W * .58, H * .31], fill=g + (255,), width=2)
        img = shapes(img, f)
    elif key == "cozy":
        def f(d):
            r = pw * 0.52
            d.ellipse([pcx - r, pcy - r, pcx + r, pcy + r], fill=rgb("#E9D3B2") + (255,))
            d.ellipse([W * .78, -H * .03, W * 1.0, H * .13], fill=rgb("#EFDDC0") + (255,))
            d.ellipse([-W * .06, H * .60, W * .14, H * .74], fill=rgb("#EFDDC0") + (255,))
        img = shapes(img, f)
    elif key == "market":
        if has_text:
            def f(d):
                d.polygon([(0, 0), (W, 0), (W, H * .205), (0, H * .235)], fill=acc + (255,))
                d.polygon([(0, H * .235), (W, H * .205), (W, H * .215), (0, H * .245)], fill=rgb("#FFB3C0") + (255,))
            img = shapes(img, f)
        img = glow(img, pcx, pcy, pw * 0.6, ph * 0.55, (255, 255, 255), 0.9)
        img = shapes(img, lambda d: d.rectangle([0, H * .985, W, H], fill=acc + (255,)))
    elif key == "tech":
        def f(d):
            step = max(30, W // 10)
            for x in range(0, W, step):
                d.line([x, 0, x, H], fill=acc + (22,), width=1)
            for y in range(0, H, step):
                d.line([0, y, W, y], fill=acc + (22,), width=1)
        img = shapes(img, f)
        img = glow(img, pcx, pcy, pw * 0.62, ph * 0.5, rgb("#0E7490"), 0.6)

        def g(d):
            L, m = W * .07, W * .04
            for (x, y, sx, sy) in [(m, m, 1, 1), (W - m, m, -1, 1), (m, H - m, 1, -1), (W - m, H - m, -1, -1)]:
                d.line([x, y, x + sx * L, y], fill=acc + (255,), width=3)
                d.line([x, y, x, y + sy * L], fill=acc + (255,), width=3)
        img = shapes(img, g)
    elif key == "eco":
        def f(d):
            d.ellipse([W * .55, H * .02, W * 1.15, H * .45], fill=rgb("#D8ECCB") + (255,))
            d.ellipse([-W * .25, H * .50, W * .35, H * .95], fill=rgb("#C4E1B6") + (255,))
        img = shapes(img, f)
        img = glow(img, pcx, pcy, pw * 0.6, ph * 0.55, (255, 255, 255), 0.85)
    elif key == "fashion":
        e = W * 0.03

        def f(d):
            d.rectangle([px0 - e, py0 - e, px1 + e, py1 + e], fill=rgb("#DDBDB4") + (255,))
            d.rectangle([px0 - e + W * .02, py0 - e + W * .02, px1 + e + W * .02, py1 + e + W * .02],
                        outline=rgb("#2B2B2B") + (255,), width=2)
        img = shapes(img, f)
    elif key == "info":
        e = W * 0.04

        def f(d):
            d.rectangle([0, 0, W * .02, H], fill=acc + (255,))
            d.rounded_rectangle([px0 - e + 6, py0 - e + 10, px1 + e + 6, py1 + e + 10], radius=int(W * .04),
                                fill=(20, 40, 90, 28))
            d.rounded_rectangle([px0 - e, py0 - e, px1 + e, py1 + e], radius=int(W * .04), fill=(255, 255, 255, 255))
        img = shapes(img, f)
    elif key == "pop":
        r = min(pw, ph) * 0.5

        def f(d):
            o = W * 0.014
            d.ellipse([pcx - r + o, pcy - r + o, pcx + r + o, pcy + r + o], fill=(17, 17, 17, 255))
            d.ellipse([pcx - r, pcy - r, pcx + r, pcy + r], fill=acc + (255,))
            gap = W * 0.035
            for i in range(5):
                for j in range(4):
                    x, y = W * .74 + i * gap, H * .015 + j * gap
                    d.ellipse([x, y, x + 6, y + 6], fill=(17, 17, 17, 255))
            d.rectangle([0, H * .975, W, H], fill=(17, 17, 17, 255))
        img = shapes(img, f)
    elif key == "cinematic":
        img = glow(img, pcx, pcy, pw * 0.62, ph * 0.5, rgb("#13525F"), 0.6)
        cone = Image.new("L", img.size, 0)
        ImageDraw.Draw(cone).polygon([(W * .5, 0), (W * .18, H * .72), (W * .82, H * .72)], fill=60)
        cone = cone.filter(ImageFilter.GaussianBlur(W * 0.03))
        fade = Image.linear_gradient("L").transpose(Image.FLIP_TOP_BOTTOM).resize(img.size)
        cone = ImageChops.multiply(cone, fade)
        img.paste(Image.new("RGB", img.size, (210, 235, 240)), (0, 0), cone)
        img = vignette(img, 0.7)
        img = shapes(img, lambda d: (d.rectangle([0, 0, W, H * .045], fill=(0, 0, 0, 255)),
                                     d.rectangle([0, H * .955, W, H], fill=(0, 0, 0, 255))))
    return img


# ---------- текст ----------
def wrap(text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if font.getlength(t) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit(text, max_w, max_h, start, minsize, weight, max_lines, lh=1.15):
    start, minsize = int(start), int(minsize)
    start = max(start, minsize)
    for size in range(start, minsize - 1, -2):
        f = get_font(size, weight)
        lines = wrap(text, f, max_w)
        if len(lines) <= max_lines and len(lines) * size * lh <= max_h and all(f.getlength(l) <= max_w for l in lines):
            return f, lines, size
    f = get_font(minsize, weight)
    return f, wrap(text, f, max_w)[:max_lines], minsize


def draw_lines(d, lines, f, size, x, y, w, align, fill, lh=1.15):
    for line in lines:
        lw = f.getlength(line)
        if align == "center":
            lx = x + (w - lw) / 2
        elif align == "right":
            lx = x + w - lw
        else:
            lx = x
        d.text((lx, y), line, font=f, fill=fill)
        y += size * lh
    return y


def draw_text(img, key, W, H, text):
    st = STYLES[key]
    d = ImageDraw.Draw(img)
    al = st["align"]
    up = st["upper"]

    def box(b):
        return b[0] * W, b[1] * H, b[2] * W, b[3] * H

    title = (text.get("title") or "").strip()
    sub = (text.get("sub") or "").strip()
    bullets = [b.strip() for b in (text.get("bullets") or []) if b and b.strip()][:3]

    if title:
        x, y, w, h = box(st["title"])
        t = title.upper() if up else title
        f, lines, size = fit(t, w, h, W * 0.095, W * 0.04, 800, 3)
        draw_lines(d, lines, f, size, x, y, w, al, rgb(st["title_c"]))
    if sub:
        x, y, w, h = box(st["sub"])
        f, lines, size = fit(sub, w, h, W * 0.042, W * 0.024, 500, 2)
        draw_lines(d, lines, f, size, x, y, w, al, rgb(st["sub_c"]))
    if bullets:
        x, y, w, h = box(st["bul"])
        n = len(bullets)
        n_all = 3  # место под три пункта всегда одно и то же
        if st["bullets"] == "chips":
            gap = h * 0.06
            ch = (h - gap * (n_all - 1)) / n_all
            pad = ch * 0.5
            for i, b in enumerate(bullets):
                size = ch * 0.5
                f = get_font(size, 700)
                while size > W * 0.02 and f.getlength(b) > w - 2 * pad:
                    size -= 2
                    f = get_font(size, 700)
                cw = min(w, f.getlength(b) + 2 * pad)
                cx = x + (w - cw) / 2 if al == "center" else x
                cy = y + i * (ch + gap)
                d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=int(ch / 2), fill=rgb(st["accent"]))
                d.text((cx + pad, cy + (ch - size) / 2 - size * 0.08), b, font=f, fill=rgb(st["chip_text"]))
        else:
            fs = min(W * 0.04, h / (n_all * 1.45))
            items = []
            maxw = 0
            for b in bullets:
                size = fs
                f = get_font(size, 600)
                while size > W * 0.02 and f.getlength(b) > w - size * 0.9:
                    size -= 2
                    f = get_font(size, 600)
                items.append((b, f, size))
                maxw = max(maxw, f.getlength(b) + size * 0.9)
            x0 = x + (w - maxw) / 2 if al == "center" else x
            yy = y
            for b, f, size in items:
                r = size * 0.16
                d.ellipse([x0, yy + size * 0.55 - r, x0 + 2 * r, yy + size * 0.55 + r], fill=rgb(st["accent"]))
                d.text((x0 + size * 0.9, yy), b, font=f, fill=rgb(st["bul_c"]))
                yy += size * 1.45
    return img


def watermark(img):
    W, H = img.size
    layer = Image.new("RGBA", (W * 2, H * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = get_font(max(28, W * 0.05), 800)
    label = "TovarPix"
    stepx, stepy = int(W * 0.42), int(W * 0.30)
    for yy in range(0, H * 2, stepy):
        for xx in range(0, W * 2, stepx):
            d.text((xx + 2, yy + 2), label, font=f, fill=(0, 0, 0, 60))
            d.text((xx, yy), label, font=f, fill=(255, 255, 255, 85))
    layer = layer.rotate(30, resample=Image.BICUBIC)
    left, top = (W * 2 - W) // 2, (H * 2 - H) // 2
    layer = layer.crop((left, top, left + W, top + H))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def compose(cutout, key, size_key, text, wm=False):
    st = STYLES[key]
    W, H = SIZES[size_key][1], SIZES[size_key][2]
    text = text or {}
    has_text = bool((text.get("title") or "").strip() or (text.get("sub") or "").strip()
                    or any((b or "").strip() for b in (text.get("bullets") or [])))
    frac = st["prod"] if has_text else NO_TEXT_BOX
    pb = (frac[0] * W, frac[1] * H, (frac[0] + frac[2]) * W, (frac[1] + frac[3]) * H)

    img = gradient(W, H, st["top"], st["bottom"])
    img = decor(key, img, W, H, pb, has_text)

    # --- подготовка товара ---
    cutout = cutout.convert("RGBA")
    alpha = cutout.getchannel("A")
    bbox = alpha.point(lambda v: 255 if v > 16 else 0).getbbox()
    if bbox:
        cutout = cutout.crop(bbox)
    a = cutout.getchannel("A").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.7))
    bw, bh = pb[2] - pb[0], pb[3] - pb[1]
    sc = min(bw / cutout.width, bh / cutout.height)
    nw, nh = max(1, int(cutout.width * sc)), max(1, int(cutout.height * sc))
    rgb_img = cutout.convert("RGB").resize((nw, nh), Image.LANCZOS)
    a = a.resize((nw, nh), Image.LANCZOS)
    # лёгкая подгонка под свет сцены (~5%)
    rgb_img = Image.blend(rgb_img, Image.new("RGB", (nw, nh), rgb(st["ambient"])), 0.05)
    prod = rgb_img.convert("RGBA")
    prod.putalpha(a)

    x = int(pb[0] + (bw - nw) / 2)
    y = int(pb[1] + (bh - nh) / 2)

    # --- тень ---
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh = Image.new("RGBA", (nw, nh), (0, 0, 0, 0))
    sh.putalpha(a.point(lambda v: int(v * st["shadow"])))
    layer.paste(sh, (x, y + int(H * 0.012)), sh)
    ImageDraw.Draw(layer).ellipse([x + nw * 0.12, y + nh - H * 0.012, x + nw * 0.88, y + nh + H * 0.014],
                                  fill=(0, 0, 0, int(255 * st["shadow"] * 0.9)))
    layer = layer.filter(ImageFilter.GaussianBlur(W * 0.012))
    base = Image.alpha_composite(img.convert("RGBA"), layer)
    base.paste(prod, (x, y), prod)
    img = base.convert("RGB")

    if has_text:
        img = draw_text(img, key, W, H, text)
    if wm:
        img = watermark(img)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95, subsampling=0)
    return out.getvalue()


# ============================================================
# 5. MAGIC HOUR: вырезание товара
# ============================================================
def mh_headers():
    return {"Authorization": "Bearer %s" % MAGIC_KEY}


def mh_upload(data, ext="jpg"):
    r = requests.post("%s/files/upload-urls" % MH, headers=mh_headers(),
                      json={"items": [{"type": "image", "extension": ext}]}, timeout=60)
    r.raise_for_status()
    item = r.json()["items"][0]
    put = requests.put(item["upload_url"], data=data, timeout=120)
    put.raise_for_status()
    return item["file_path"]


def mh_remove_bg(jpeg_bytes):
    path = mh_upload(jpeg_bytes, "jpg")
    r = requests.post("%s/image-background-remover" % MH, headers=mh_headers(),
                      json={"name": "tovarpix", "assets": {"image_file_path": path}}, timeout=60)
    if not r.ok:
        raise RuntimeError("MagicHour start: %s %s" % (r.status_code, r.text[:300]))
    pid = r.json()["id"]
    for _ in range(60):  # до ~3 минут
        time.sleep(3)
        g = requests.get("%s/image-projects/%s" % (MH, pid), headers=mh_headers(), timeout=60)
        g.raise_for_status()
        j = g.json()
        status = j.get("status")
        if status == "complete":
            url = j["downloads"][0]["url"]
            png = requests.get(url, timeout=120)
            png.raise_for_status()
            return Image.open(io.BytesIO(png.content)).convert("RGBA")
        if status in ("error", "canceled"):
            raise RuntimeError("MagicHour status: %s %s" % (status, str(j.get("error"))[:200]))
    raise RuntimeError("MagicHour timeout")


# ============================================================
# 6. GEMINI: текст и лёгкая чистка товара
# ============================================================
def gemini_call(model, parts, gen_config=None):
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
    body = {"contents": [{"parts": parts}]}
    if gen_config:
        body["generationConfig"] = gen_config
    r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)
    if not r.ok:
        raise RuntimeError("Gemini %s: %s" % (r.status_code, r.text[:300]))
    return r.json()


def to_b64_jpeg(img, maxside=768):
    im = img.convert("RGB")
    im.thumbnail((maxside, maxside))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def gemini_text(src_img):
    prompt = (
        "Ты копирайтер карточек товаров для Wildberries и Ozon. Посмотри на фото товара и верни ТОЛЬКО JSON "
        "без пояснений такого вида: {\"title\": \"...\", \"sub\": \"...\", \"bullets\": [\"...\", \"...\", \"...\"]}. "
        "title — название или главная выгода, 1-3 слова, до 20 символов. "
        "sub — короткая строка до 40 символов. "
        "bullets — ровно 3 коротких преимущества, каждое до 24 символов. "
        "Пиши по-русски. Не выдумывай характеристики, которых не видно на фото (мощность, объём, материалы, "
        "бренд, цифры). Пиши только то, что очевидно по виду товара."
    )
    j = gemini_call(GEMINI_TEXT_MODEL,
                    [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": to_b64_jpeg(src_img)}}],
                    {"responseMimeType": "application/json", "temperature": 0.7})
    raw = j["candidates"][0]["content"]["parts"][0]["text"].strip()
    if not raw.startswith("{"):
        raw = raw[raw.index("{"):raw.rindex("}") + 1]
    data = json.loads(raw)
    return {
        "title": str(data.get("title", ""))[:40],
        "sub": str(data.get("sub", ""))[:70],
        "bullets": [str(b)[:40] for b in (data.get("bullets") or [])][:3],
    }


def parse_own_text(raw):
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    return {
        "title": lines[0][:60] if lines else "",
        "sub": lines[1][:90] if len(lines) > 1 else "",
        "bullets": [l[:40] for l in lines[2:5]],
    }


def retouch(cutout):
    """Очень лёгкая чистка: пыль, царапины, грязь. Форма и цвет товара не меняются.
    Если что-то пошло не так или товар заметно изменился — берём оригинальную вырезку."""
    if not (RETOUCH_ENABLED and GEMINI_KEY):
        return cutout
    try:
        a = cutout.getchannel("A")
        bb = a.point(lambda v: 255 if v > 16 else 0).getbbox()
        if not bb:
            return cutout
        crop = cutout.crop(bb)
        white = Image.new("RGB", crop.size, (255, 255, 255))
        white.paste(crop, mask=crop.getchannel("A"))
        white.thumbnail((1280, 1280))
        buf = io.BytesIO()
        white.save(buf, "PNG")
        prompt = (
            "Edit this product photo with a very light retouch. Remove dust, lint, dirt, fingerprints, smudges and "
            "small scratches from the product surface so it looks clean and presentable. Keep the pure white "
            "background. DO NOT change the product's shape, proportions, colors, materials, logos, text, buttons, "
            "ports or any details. Do not add anything. Do not crop or move the product. Return the same photo, only cleaner."
        )
        j = gemini_call(GEMINI_IMAGE_MODEL,
                        [{"text": prompt}, {"inline_data": {"mime_type": "image/png",
                                                            "data": base64.b64encode(buf.getvalue()).decode()}}],
                        {"responseModalities": ["TEXT", "IMAGE"]})
        data = None
        for p in j["candidates"][0]["content"]["parts"]:
            blob = p.get("inlineData") or p.get("inline_data")
            if blob:
                data = base64.b64decode(blob["data"])
                break
        if not data:
            return cutout
        new = Image.open(io.BytesIO(data)).convert("RGB")
        if abs(new.width / new.height - white.width / white.height) > 0.04:
            return cutout
        new = new.resize(white.size, Image.LANCZOS)
        # защита: если товар сильно изменился — не берём результат
        s1 = white.resize((48, 48)).convert("L")
        s2 = new.resize((48, 48)).convert("L")
        diff = sum(abs(p1 - p2) for p1, p2 in zip(s1.getdata(), s2.getdata())) / (48 * 48)
        if diff > 14:
            log.info("retouch rejected (diff %.1f)", diff)
            return cutout
        new = new.resize(crop.size, Image.LANCZOS)
        out = new.convert("RGBA")
        out.putalpha(crop.getchannel("A"))
        return out
    except Exception:
        log.warning("retouch skipped: %s", traceback.format_exc()[-400:])
        return cutout


# ============================================================
# 7. ОДНА КАРТОЧКА ОТ НАЧАЛА ДО КОНЦА
# ============================================================
def prep_photo(raw):
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((1800, 1800))
    return im


def make_card(file_id, style, size_key, text_mode, own_text, wm, preset_text=None):
    raw = tg_download(file_id)
    src = prep_photo(raw)
    buf = io.BytesIO()
    src.save(buf, "JPEG", quality=92)
    cut = mh_remove_bg(buf.getvalue())
    cut = retouch(cut)
    text = preset_text
    if not text:
        if text_mode == "ai":
            try:
                text = gemini_text(src)
            except Exception:
                log.warning("gemini_text failed: %s", traceback.format_exc()[-300:])
                text = {"title": "", "sub": "", "bullets": []}
        elif text_mode == "own":
            text = parse_own_text(own_text)
        else:
            text = {"title": "", "sub": "", "bullets": []}
    data = compose(cut, style, size_key, text, wm)
    return data, text


# ============================================================
# 8. ДИАЛОГ С ПОЛЬЗОВАТЕЛЕМ
# ============================================================
HELP_TEXT = (
    "Как пользоваться TovarPix 📦\n\n"
    "1. Пришли фото товара (можно до 7 фото с разных ракурсов — каждое станет отдельной карточкой).\n"
    "2. Выбери размер и один из 10 стилей.\n"
    "3. Выбери: без текста, текст от ИИ или свой текст.\n"
    "4. Получи готовые карточки файлами в высоком качестве.\n\n"
    "Цена: 1 карточка = 1 ⭐. Первая карточка бесплатно, но с водяным знаком.\n"
    "Не понравился результат — кнопка «Переделать» (1 ⭐).\n\n"
    "Чтобы вышло лучше:\n"
    "• снимай при дневном свете, товар целиком в кадре\n"
    "• фон может быть любой, но однотонный лучше\n"
    "• протри товар перед съёмкой, ИИ уберёт только лёгкую пыль\n\n"
    "Команды: /new — новый товар, /balance — баланс, /buy — пополнить, /help — помощь."
)

WELCOME = (
    "Привет! Я TovarPix 📦\n\n"
    "Сфоткай товар на телефон и пришли мне фото — верну красивую карточку для Wildberries, Ozon, Авито или TikTok.\n\n"
    "Первая карточка бесплатно (с водяным знаком). Команды — в кнопке «Меню» слева от поля ввода."
)

_locks = defaultdict(threading.Lock)


def new_session():
    return {"state": "idle", "photos": []}


def style_kb():
    rows, row = [], []
    for k in STYLE_ORDER:
        row.append((STYLES[k]["name"], "style:%s" % k))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("❌ Отмена", "cancel")])
    return ikb(rows)


def size_kb():
    rows = [[(v[0], "size:%s" % k)] for k, v in SIZES.items()]
    rows.append([("❌ Отмена", "cancel")])
    return ikb(rows)


def shop_kb():
    rows = [[("%d ⭐  →  %d карточек" % (n, n), "buy:%d" % n)] for n in PACKS]
    rows.append([("← Назад", "back_confirm")])
    return ikb(rows)


def text_summary(s):
    tm = {"none": "без текста", "ai": "текст от ИИ", "own": "свой текст"}.get(s.get("text_mode"), "—")
    return "Размер: %s\nСтиль: %s\nТекст: %s\nКарточек: %d" % (
        SIZES[s["size"]][0], STYLES[s["style"]]["name"], tm, len(s["photos"]))


def send_confirm(uid, chat_id, s, edit_mid=None):
    credits, free_used = get_user(uid)
    n = len(s["photos"])
    s["state"] = "confirm"
    save_session(uid, s)
    txt = "Проверь настройки:\n\n%s\n\nСтоимость: %d ⭐ (у тебя %d ⭐)" % (text_summary(s), n, credits)
    rows = []
    if credits >= n:
        rows.append([("✅ Создать (%d ⭐)" % n, "run")])
    if not free_used:
        rows.append([("🎁 Бесплатно 1 карточка (с водяным знаком)", "free")])
    if credits < n:
        rows.append([("💳 Пополнить ⭐", "shop")])
    rows.append([("❌ Отмена", "cancel")])
    show(chat_id, txt, ikb(rows), edit_mid)


def handle_photo(uid, chat_id, file_id):
    s = get_session(uid)
    if s.get("state") == "busy":
        show(chat_id, "⏳ Я ещё делаю прошлые карточки. Подожди, пожалуйста.")
        return
    if s.get("state") != "collect":
        s = new_session()
        s["state"] = "collect"
    if len(s["photos"]) >= MAX_PHOTOS:
        show(chat_id, "Максимум %d фото за раз. Жми «Дальше»." % MAX_PHOTOS)
        return
    s["photos"].append(file_id)
    n = len(s["photos"])
    txt = ("📸 Фото принято: %d/%d.\nМожешь прислать ещё фото (другие ракурсы или другой товар) — "
           "каждое станет отдельной карточкой. Или жми «Дальше».") % (n, MAX_PHOTOS)
    kb = ikb([[("Дальше →", "go")], [("❌ Отмена", "cancel")]])
    mid = show(chat_id, txt, kb, s.get("status_mid"))
    s["status_mid"] = mid
    save_session(uid, s)


def on_message(msg):
    uid = msg["from"]["id"]
    chat_id = msg["chat"]["id"]

    if "successful_payment" in msg:
        pay = msg["successful_payment"]
        charge = pay.get("telegram_payment_charge_id", "")
        stars = int(pay.get("total_amount", 0))
        rows = q("INSERT INTO tp_payments(charge_id, uid, stars) VALUES(%s,%s,%s) "
                 "ON CONFLICT DO NOTHING RETURNING charge_id", (charge, uid, stars), fetch=True)
        if rows:
            add_credits(uid, stars)
        credits, _ = get_user(uid)
        show(chat_id, "✅ Оплата прошла! Баланс: %d ⭐" % credits)
        s = get_session(uid)
        if s.get("state") == "confirm":
            send_confirm(uid, chat_id, s)
        return

    if msg.get("photo"):
        handle_photo(uid, chat_id, msg["photo"][-1]["file_id"])
        return
    doc = msg.get("document")
    if doc and str(doc.get("mime_type", "")).startswith("image/"):
        handle_photo(uid, chat_id, doc["file_id"])
        return

    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd == "/start":
        get_user(uid)
        save_session(uid, new_session())
        tg("sendMessage", chat_id=chat_id, text=WELCOME, reply_markup={"remove_keyboard": True})
        return
    if cmd == "/help":
        show(chat_id, HELP_TEXT)
        return
    if cmd == "/new":
        save_session(uid, new_session())
        show(chat_id, "Ок! Пришли фото нового товара 📸")
        return
    if cmd == "/balance":
        credits, free_used = get_user(uid)
        extra = "" if free_used else "\nБесплатная карточка ещё не использована 🎁"
        show(chat_id, "Твой баланс: %d ⭐%s" % (credits, extra))
        return
    if cmd == "/buy":
        show(chat_id, "Выбери пакет. 1 ⭐ = 1 карточка:", shop_kb_simple())
        return
    if cmd == "/id":
        show(chat_id, "Твой ID: %s" % uid)
        return
    if cmd == "/give":
        parts = text.split()
        if ADMIN_ID and str(uid) == ADMIN_ID and len(parts) == 3:
            try:
                add_credits(int(parts[1]), int(parts[2]))
                show(chat_id, "Готово: пользователю %s добавлено %s ⭐" % (parts[1], parts[2]))
            except Exception:
                show(chat_id, "Формат: /give ID количество")
        return

    s = get_session(uid)
    if s.get("state") == "await_text" and text:
        s["own_text"] = text[:300]
        send_confirm(uid, chat_id, s)
        return
    if s.get("state") == "busy":
        show(chat_id, "⏳ Делаю карточки, скоро пришлю.")
        return
    show(chat_id, "Пришли фото товара 📸 — и я сделаю карточку. Подсказка: /help")


def shop_kb_simple():
    rows = [[("%d ⭐  →  %d карточек" % (n, n), "buy:%d" % n)] for n in PACKS]
    return ikb(rows)


def on_callback(cb):
    uid = cb["from"]["id"]
    chat_id = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    s = get_session(uid)

    if data == "cancel":
        save_session(uid, new_session())
        show(chat_id, "Отменено. Пришли новое фото, когда будешь готов 📸", None, mid)
        return

    if s.get("state") == "busy" and not data.startswith("buy:") and data != "shop":
        return

    if data == "go":
        if not s.get("photos"):
            show(chat_id, "Сначала пришли фото 📸", None, mid)
            return
        s["state"] = "size"
        save_session(uid, s)
        show(chat_id, "Выбери размер карточки:", size_kb(), mid)
    elif data.startswith("size:"):
        k = data.split(":", 1)[1]
        if k in SIZES:
            s["size"] = k
            s["state"] = "style"
            save_session(uid, s)
            show(chat_id, "Выбери стиль:", style_kb(), mid)
    elif data.startswith("style:"):
        k = data.split(":", 1)[1]
        if k in STYLES:
            s["style"] = k
            s["state"] = "textmode"
            save_session(uid, s)
            show(chat_id, "Что сделать с текстом на карточке?", ikb([
                [("🚫 Без текста", "tm:none")],
                [("🤖 Текст от ИИ", "tm:ai")],
                [("✍️ Свой текст", "tm:own")],
                [("❌ Отмена", "cancel")]]), mid)
    elif data.startswith("tm:"):
        mode = data.split(":", 1)[1]
        s["text_mode"] = mode
        s["own_text"] = ""
        if mode == "own":
            s["state"] = "await_text"
            save_session(uid, s)
            show(chat_id, "Напиши текст одним сообщением:\n\n"
                          "1-я строка — заголовок\n2-я строка — подзаголовок (по желанию)\n"
                          "3–5-я строки — преимущества (до 3 штук)\n\n"
                          "Пример:\nМини-ПК\nМощный и тихий\nЭкономит место\nЛегко подключить\nГарантия 1 год",
                 ikb([[("❌ Отмена", "cancel")]]), mid)
        else:
            send_confirm(uid, chat_id, s, mid)
    elif data == "back_confirm":
        send_confirm(uid, chat_id, s, mid)
    elif data == "shop":
        show(chat_id, "Выбери пакет. 1 ⭐ = 1 карточка:", shop_kb(), mid)
    elif data.startswith("buy:"):
        n = int(data.split(":", 1)[1])
        if n in PACKS:
            tg("sendInvoice", chat_id=chat_id, title="%d карточек TovarPix" % n,
               description="%d ⭐ = %d карточек товара" % (n, n), payload="pack:%d" % n,
               provider_token="", currency="XTR", prices=[{"label": "%d карточек" % n, "amount": n}])
    elif data == "run":
        if s.get("state") != "confirm":
            return
        n = len(s["photos"])
        if not spend(uid, n):
            show(chat_id, "Не хватает ⭐. Пополни баланс:", shop_kb(), mid)
            return
        start_job(uid, chat_id, s, list(range(n)), free=False)
    elif data == "free":
        if s.get("state") != "confirm":
            return
        if not take_free(uid):
            show(chat_id, "Бесплатная карточка уже использована.", None, mid)
            return
        s["photos"] = s["photos"][:1]
        start_job(uid, chat_id, s, [0], free=True)
    elif data.startswith("redo:"):
        last = s.get("last")
        if not last:
            return
        i = int(data.split(":", 1)[1])
        if i < 0 or i >= len(last["photos"]):
            return
        if not spend(uid, 1):
            show(chat_id, "Для переделки нужна 1 ⭐. Пополни баланс:", shop_kb_simple())
            return
        job = dict(last)
        start_job(uid, chat_id, job, [i], free=False, from_last=True)
    elif data == "restyle":
        last = s.get("last")
        if not last:
            return
        s2 = new_session()
        s2.update({"photos": last["photos"], "size": last["size"], "state": "style"})
        save_session(uid, s2)
        show(chat_id, "Выбери другой стиль:", style_kb())
    elif data == "newset":
        save_session(uid, new_session())
        show(chat_id, "Пришли фото нового товара 📸")


# ---------- запуск генерации ----------
def start_job(uid, chat_id, s, indices, free, from_last=False):
    if from_last:
        job = {k: s[k] for k in ("photos", "style", "size", "text_mode", "own_text", "texts")}
        cur = get_session(uid)
        cur["state"] = "busy"
        save_session(uid, cur)
    else:
        job = {"photos": s["photos"], "style": s["style"], "size": s["size"],
               "text_mode": s.get("text_mode", "none"), "own_text": s.get("own_text", ""),
               "texts": [None] * len(s["photos"])}
        s["state"] = "busy"
        save_session(uid, s)
    threading.Thread(target=run_job, args=(uid, chat_id, job, indices, free), daemon=True).start()


def run_job(uid, chat_id, job, indices, free):
    res = tg("sendMessage", chat_id=chat_id,
             text="⏳ Делаю карточки: 0/%d. Обычно 30–90 секунд на каждую." % len(indices))
    mid = res.get("result", {}).get("message_id")
    done, failed = 0, []

    def work(i):
        return make_card(job["photos"][i], job["style"], job["size"], job["text_mode"],
                         job.get("own_text", ""), free, job["texts"][i])

    try:
        with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            futs = {ex.submit(work, i): i for i in indices}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    data, text = fut.result()
                    job["texts"][i] = text
                    send_doc(chat_id, data, "card_%d.jpg" % (i + 1), "Карточка №%d" % (i + 1))
                    done += 1
                except Exception:
                    log.error("card %s failed: %s", i, traceback.format_exc())
                    failed.append(i)
                if mid:
                    tg("editMessageText", chat_id=chat_id, message_id=mid,
                       text="⏳ Делаю карточки: %d/%d." % (done + len(failed), len(indices)))
    finally:
        if failed:
            if free:
                give_back_free(uid)
            else:
                add_credits(uid, len(failed))
        s = get_session(uid)
        s["state"] = "done"
        s["last"] = job
        s["photos"] = []
        save_session(uid, s)

    credits, _ = get_user(uid)
    txt = "✅ Готово: %d из %d." % (done, len(indices))
    if failed:
        txt += "\nНе получилось сделать %d — ⭐ вернул на баланс." % len(failed)
    if free:
        txt += "\nЭто бесплатная версия с водяным знаком. Чистые карточки — за 1 ⭐."
    txt += "\nБаланс: %d ⭐" % credits
    rows = []
    redo = [("🔁 №%d (1 ⭐)" % (i + 1), "redo:%d" % i) for i in range(len(job["photos"]))]
    for k in range(0, len(redo), 3):
        rows.append(redo[k:k + 3])
    rows.append([("🎨 Другой стиль", "restyle")])
    rows.append([("➕ Новый товар", "newset")])
    if mid:
        tg("deleteMessage", chat_id=chat_id, message_id=mid)
    tg("sendMessage", chat_id=chat_id, text=txt + "\n\nНе понравилась карточка? Кнопка «🔁 №» переделает её.",
       reply_markup=ikb(rows))


# ============================================================
# 9. ВЕБХУК (Flask)
# ============================================================
app = Flask(__name__)


def process(update):
    try:
        if "pre_checkout_query" in update:
            tg("answerPreCheckoutQuery", pre_checkout_query_id=update["pre_checkout_query"]["id"], ok=True)
        elif "callback_query" in update:
            uid = update["callback_query"]["from"]["id"]
            with _locks[uid]:
                on_callback(update["callback_query"])
        elif "message" in update:
            uid = update["message"]["from"]["id"]
            with _locks[uid]:
                on_message(update["message"])
    except Exception:
        log.error("update failed: %s", traceback.format_exc())


@app.route("/")
def index():
    return "TovarPix OK"


@app.route("/webhook/<token>", methods=["POST"])
def webhook(token):
    if token != BOT_TOKEN:
        return "forbidden", 403
    update = request.get_json(force=True, silent=True) or {}
    threading.Thread(target=process, args=(update,), daemon=True).start()
    return "ok"


def init():
    try:
        if DATABASE_URL:
            init_db()
    except Exception:
        log.error("init_db failed: %s", traceback.format_exc())
    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан")
        return
    try:
        tg("setMyCommands", commands=[
            {"command": "new", "description": "Новый товар"},
            {"command": "buy", "description": "Пополнить ⭐"},
            {"command": "balance", "description": "Мой баланс"},
            {"command": "help", "description": "Помощь"},
        ])
        tg("setChatMenuButton", menu_button={"type": "commands"})
        if BASE_URL:
            r = tg("setWebhook", url="%s/webhook/%s" % (BASE_URL, BOT_TOKEN),
                   allowed_updates=["message", "callback_query", "pre_checkout_query"])
            log.info("setWebhook: %s", r)
        else:
            log.error("Не найден адрес сервиса (RENDER_EXTERNAL_URL / WEBHOOK_URL)")
    except Exception:
        log.error("init failed: %s", traceback.format_exc())


init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))

import os
import io
import re
import json
import uuid
import time
import base64
import logging
import threading
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque

import requests
import psycopg
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from magic_hour import Client

# ============================================================
# TovarPix Bot — Magic Hour делает только фото/обработку.
# Весь текст на рекламных карточках накладывается локально через PIL.
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAGIC_HOUR_API_KEY = os.environ["MAGIC_HOUR_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # Не используется для нанесения текста.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_ENDPOINT_SECRET = os.getenv("ADMIN_ENDPOINT_SECRET", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

MAX_REFERENCE_PHOTOS = int(os.getenv("MAX_REFERENCE_PHOTOS", "6"))
MAX_OUTPUT_CARDS = 7
MAX_CUSTOM_TEXT_CHARS = 220
PACKAGE_IMAGES = int(os.getenv("PACKAGE_IMAGES", "5"))
PACKAGE_STARS = int(os.getenv("PACKAGE_STARS", "5"))
FREE_SETS = 1

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("tovarpix")

app = Flask(__name__)
telegram_api = f"https://api.telegram.org/bot{BOT_TOKEN}"
magic_client = Client(token=MAGIC_HOUR_API_KEY)

state_lock = threading.RLock()
pending_photos = {}
pending_jobs = {}
busy_chats = set()
rate_events = defaultdict(deque)

STYLE_OPTIONS = {
    "minimal": {"name":"Чистый минимализм","emoji":"▫️","description":"Светлый фон, чистая композиция","top":"#F4F4F0","bottom":"#E3E5E1","panel":"#FFFFFF","text":"#202421","muted":"#606963","accent":"#2C7A62","kind":"minimal"},
    "luxury": {"name":"Luxury","emoji":"✨","description":"Чёрный фон и золотые акценты","top":"#24211D","bottom":"#090909","panel":"#151310","text":"#FFF8E8","muted":"#C7BDA8","accent":"#D7B56D","kind":"luxury"},
    "cozy": {"name":"Домашний уют","emoji":"🏡","description":"Тёплые кремовые оттенки","top":"#F7E9D5","bottom":"#E9D1B3","panel":"#FFF7EA","text":"#4A3528","muted":"#876E5B","accent":"#B56C43","kind":"cozy"},
    "market": {"name":"Яркий маркетинговый","emoji":"🛍️","description":"Контрастная коммерческая подача","top":"#FFF4DF","bottom":"#FFD7B5","panel":"#FFFFFF","text":"#20202A","muted":"#5C5B68","accent":"#F05A28","kind":"market"},
    "tech": {"name":"Tech","emoji":"🔷","description":"Графит, синий свет и техническая сетка","top":"#172A3A","bottom":"#080F1A","panel":"#111D2A","text":"#F2F8FF","muted":"#A9C0D4","accent":"#65D6FF","kind":"tech"},
    "eco": {"name":"Эко и натуральность","emoji":"🌿","description":"Природные зелёные и песочные оттенки","top":"#E8EBDD","bottom":"#C8D4B8","panel":"#F7F6E9","text":"#26392B","muted":"#5E725F","accent":"#4C8058","kind":"eco"},
    "fashion": {"name":"Fashion Editorial","emoji":"🖤","description":"Строгая редакционная композиция","top":"#F1F0EE","bottom":"#D5D3D0","panel":"#FAF9F7","text":"#151515","muted":"#5A5856","accent":"#C53A48","kind":"fashion"},
    "info": {"name":"Инфографика","emoji":"📊","description":"Светлые блоки и понятная иерархия","top":"#F1F6FC","bottom":"#DCE8F7","panel":"#FFFFFF","text":"#172A46","muted":"#526985","accent":"#2869C7","kind":"info"},
    "pop": {"name":"Pop & Fun","emoji":"🎨","description":"Яркие фиолетовые и жёлтые акценты","top":"#F2E8FF","bottom":"#D8C2FF","panel":"#FFFDF7","text":"#29213C","muted":"#66577E","accent":"#7546D8","kind":"pop"},
    "cinematic": {"name":"Cinematic","emoji":"🎬","description":"Тёмная кинематографичная студия","top":"#30343B","bottom":"#080A0D","panel":"#12151A","text":"#FFFFFF","muted":"#B5BBC4","accent":"#FFB86B","kind":"cinematic"},
}

BASE_PROMPT = """
Edit the supplied product photo into a polished, photorealistic commercial product photograph.
IMPORTANT: do not create, draw, render, or include ANY text, letters, numbers, words, logos,
watermarks, captions, labels, typography, UI, price tags, badges, or symbols in the image.
Leave clean negative space where a designer could later add text, but do not add text yourself.
Keep the exact same product identity, shape, color, material, proportions, buttons, camera count,
branding already physically present on the real product, and other defining details. Do not invent
extra objects or change the product model. Improve lighting, composition, background, shadows,
surface reflections, sharpness and professional studio quality. Product must remain the main subject.
"""

STYLE_PROMPTS = {
    "minimal": "Style: premium clean minimalism, warm off-white seamless studio, subtle natural shadow, balanced editorial composition, restrained and elegant.",
    "luxury": "Style: luxury campaign, deep black and charcoal studio, controlled warm gold-toned rim lighting, premium reflective surfaces, dramatic but realistic.",
    "cozy": "Style: warm home lifestyle, cream and beige palette, soft window light, tactile natural surfaces, inviting and believable.",
    "market": "Style: energetic retail campaign photography, bright high-contrast warm studio colors, clear silhouette, punchy but polished lighting.",
    "tech": "Style: modern technology campaign, graphite environment, cool blue edge lighting, clean geometric surfaces, realistic subtle reflections; no interface graphics.",
    "eco": "Style: natural sustainable aesthetic, muted sage green and sand tones, daylight, stone/wood/linen textures used subtly, photorealistic.",
    "fashion": "Style: fashion editorial photography, refined art direction, strong composition, sophisticated neutral background, premium magazine lighting.",
    "info": "Style: clean catalog product photography, pale cool background, even lighting, crisp details, orderly composition; absolutely no infographic graphics or text.",
    "pop": "Style: playful contemporary campaign, vibrant purple and yellow color accents in the environment, clean studio, polished realistic product photography.",
    "cinematic": "Style: cinematic product campaign, dark atmospheric studio, controlled warm highlights, deep shadows, realistic filmic contrast.",
}

POSITIONS = {
    "top": "Сверху",
    "left": "Слева",
    "right": "Справа",
    "bottom": "Снизу",
    "center": "По центру",
}

def db_connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=10)

def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                free_used INTEGER NOT NULL DEFAULT 0,
                credits INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                credits INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processing_events (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT,
                event TEXT NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

def db_event(user_id, event, details=None):
    try:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO processing_events (telegram_id,event,details) VALUES (%s,%s,%s)",
                (user_id, event, json.dumps(details or {}, ensure_ascii=False))
            )
    except Exception:
        log.exception("Could not record event")

def tg(method, payload=None, files=None, timeout=60):
    response = requests.post(f"{telegram_api}/{method}", data=payload or {}, files=files, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {data}")
    return data.get("result")

def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return tg("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return tg("editMessageText", payload)

def send_photo(chat_id, image_bytes, caption=None):
    files = {"photo": ("tovarpix.jpg", image_bytes, "image/jpeg")}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption[:1024]
    return tg("sendPhoto", payload, files=files, timeout=120)

def answer_callback(callback_id, text=""):
    try:
        tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except Exception:
        log.exception("Callback answer failed")

def main_keyboard():
    return {"keyboard":[[{"text":"🎨 Создать карточки"},{"text":"💳 Баланс"}],
                        [{"text":"🛍 Купить карточки"},{"text":"❓ Помощь"}]],
            "resize_keyboard":True}

def inline_keyboard(rows):
    return {"inline_keyboard": rows}

def ensure_user(user_id):
    with db_connect() as conn:
        conn.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))

def get_balance(user_id):
    ensure_user(user_id)
    with db_connect() as conn:
        row = conn.execute("SELECT free_used,credits FROM users WHERE telegram_id=%s", (user_id,)).fetchone()
    return row[0], row[1]

def reserve_credit(user_id):
    """Reserve one credit for a complete batch. Return 'admin', 'free', 'paid', or None."""
    ensure_user(user_id)
    if user_id in ADMIN_IDS:
        return "admin"
    with db_connect() as conn:
        row = conn.execute("SELECT free_used,credits FROM users WHERE telegram_id=%s FOR UPDATE", (user_id,)).fetchone()
        free_used, credits = row
        if free_used < FREE_SETS:
            conn.execute("UPDATE users SET free_used=free_used+1 WHERE telegram_id=%s", (user_id,))
            return "free"
        if credits > 0:
            conn.execute("UPDATE users SET credits=credits-1 WHERE telegram_id=%s", (user_id,))
            return "paid"
    return None

def refund_credit(user_id, source):
    if source == "free":
        with db_connect() as conn:
            conn.execute("UPDATE users SET free_used=GREATEST(0,free_used-1) WHERE telegram_id=%s", (user_id,))
    elif source == "paid":
        with db_connect() as conn:
            conn.execute("UPDATE users SET credits=credits+1 WHERE telegram_id=%s", (user_id,))

def create_invoice(chat_id, user_id):
    payload = f"tovarpix_{user_id}_{uuid.uuid4().hex[:10]}"
    tg("sendInvoice", {
        "chat_id": chat_id,
        "title": f"{PACKAGE_IMAGES} карточек TovarPix",
        "description": f"Пакет: {PACKAGE_IMAGES} генераций комплектов карточек.",
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": json.dumps([{"label": f"{PACKAGE_IMAGES} генераций", "amount": PACKAGE_STARS}]),
        "start_parameter": "tovarpix"
    })

def get_file_bytes(file_id):
    info = tg("getFile", {"file_id": file_id})
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

def load_font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()

def hex_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i+2], 16) for i in (0,2,4))

def gradient(size, top, bottom):
    w, h = size
    a, b = hex_rgb(top), hex_rgb(bottom)
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(h):
        t = y / max(1, h-1)
        c = tuple(int(a[i]*(1-t)+b[i]*t) for i in range(3))
        for x in range(w):
            px[x,y] = c
    return im

def crop_fill(image, size):
    image = image.convert("RGB")
    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    resized = image.resize((int(image.width*scale), int(image.height*scale)), Image.Resampling.LANCZOS)
    left = (resized.width-target_w)//2
    top = (resized.height-target_h)//2
    return resized.crop((left, top, left+target_w, top+target_h))

def wrap_text(draw, text, font, max_width):
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = words[0]
        for word in words[1:]:
            candidate = line + " " + word
            if draw.textbbox((0,0), candidate, font=font)[2] <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines

def fit_lines(draw, text, max_width, max_height, start_size=52, min_size=22, bold=True):
    for size in range(start_size, min_size-1, -2):
        font = load_font(size, bold=bold)
        lines = wrap_text(draw, text, font, max_width)
        line_h = int(size*1.24)
        if len(lines)*line_h <= max_height and all(draw.textbbox((0,0), line, font=font)[2] <= max_width for line in lines):
            return font, lines, line_h
    font = load_font(min_size, bold=bold)
    lines = wrap_text(draw, text, font, max_width)
    return font, lines, int(min_size*1.24)

def render_ad_card(product_bytes, style_id, custom_text, position):
    """Fixed reusable layout per style. Text is drawn only here, never by Magic Hour."""
    style = STYLE_OPTIONS[style_id]
    with Image.open(io.BytesIO(product_bytes)) as opened:
        product = opened.convert("RGB")
    W, H = 1080, 1350
    canvas = gradient((W,H), style["top"], style["bottom"]).convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # Style-specific fixed background motif, deterministic for each style.
    kind = style["kind"]
    accent = hex_rgb(style["accent"])
    if kind in ("tech", "info"):
        for x in range(40, W, 90):
            draw.line((x, 35, x, H-35), fill=(*accent, 22), width=1)
        for y in range(35, H, 90):
            draw.line((35, y, W-35, y), fill=(*accent, 22), width=1)
    elif kind == "luxury":
        draw.rounded_rectangle((28,28,W-28,H-28), radius=28, outline=(*accent,180), width=3)
        draw.rounded_rectangle((44,44,W-44,H-44), radius=22, outline=(*accent,65), width=1)
    elif kind == "eco":
        draw.ellipse((W-230,-80,W+70,220), fill=(*accent,28))
        draw.ellipse((-100,H-250,190,H+40), fill=(*accent,22))
    elif kind == "pop":
        draw.ellipse((-100,-90,240,250), fill=(*accent,38))
        draw.ellipse((W-210,H-220,W+100,H+90), fill=(255,190,40,75))
    elif kind == "cinematic":
        draw.rectangle((0,0,W,18), fill=(*accent,210))
        draw.rectangle((0,H-18,W,H), fill=(*accent,210))
    elif kind == "fashion":
        draw.line((72,80,300,80), fill=(*accent,255), width=6)
        draw.line((W-300,H-80,W-72,H-80), fill=(*accent,255), width=6)
    elif kind == "market":
        draw.rounded_rectangle((W-180,40,W-45,175), radius=30, fill=(*accent,35))

    # Main product area: one stable composition, consistent for the selected style.
    product_box = (70, 70, 1010, 1010)
    # Soft shadow under product frame.
    shadow = Image.new("RGBA", (W,H), (0,0,0,0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((product_box[0]+7,product_box[1]+12,product_box[2]+7,product_box[3]+12),
                         radius=34, fill=(0,0,0,35))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas = Image.alpha_composite(canvas, shadow)
    draw = ImageDraw.Draw(canvas)
    panel = hex_rgb(style["panel"])
    draw.rounded_rectangle(product_box, radius=34, fill=(*panel,245), outline=(*accent,100), width=2)
    inner = product.copy()
    inner.thumbnail((850,850), Image.Resampling.LANCZOS)
    px = product_box[0] + (product_box[2]-product_box[0]-inner.width)//2
    py = product_box[1] + (product_box[3]-product_box[1]-inner.height)//2
    canvas.alpha_composite(inner.convert("RGBA"), (px,py))
    draw = ImageDraw.Draw(canvas)

    text = (custom_text or "").strip()[:MAX_CUSTOM_TEXT_CHARS]
    if text:
        # Keep the exact user wording; only line wrapping is applied.
        text_color = hex_rgb(style["text"])
        muted = hex_rgb(style["muted"])
        accent = hex_rgb(style["accent"])
        if position in ("top","bottom"):
            region = (72, 1035, 1008, 1280)
            anchor = "top" if position == "top" else "bottom"
        elif position == "left":
            region = (72, 390, 420, 760)
            anchor = "center"
        elif position == "right":
            region = (660, 390, 1008, 760)
            anchor = "center"
        else:
            region = (190, 390, 890, 760)
            anchor = "center"

        x1,y1,x2,y2 = region
        # A translucent/high-contrast plate makes text readable over the product too.
        plate = (*panel, 235)
        draw.rounded_rectangle((x1,y1,x2,y2), radius=26, fill=plate, outline=(*accent,190), width=3)
        pad_x, pad_y = 24, 20
        max_h = y2-y1-2*pad_y
        font, lines, line_h = fit_lines(draw, text, x2-x1-2*pad_x, max_h, start_size=50, min_size=22, bold=True)
        total_h = len(lines)*line_h
        if anchor == "top":
            ty = y1 + pad_y
        elif anchor == "bottom":
            ty = y2 - pad_y - total_h
        else:
            ty = y1 + (y2-y1-total_h)//2
        for line in lines:
            bbox = draw.textbbox((0,0), line, font=font)
            tw = bbox[2]-bbox[0]
            tx = x1 + (x2-x1-tw)//2
            draw.text((tx,ty), line, font=font, fill=(*text_color,255), stroke_width=0)
            ty += line_h

    # Tiny brand signature is deliberately omitted: only user-supplied text appears.
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="JPEG", quality=94, optimize=True)
    return out.getvalue()

def generate_product_image(image_bytes_list, style_id):
    """Magic Hour receives only references + a no-text image-editing prompt."""
    with tempfile.TemporaryDirectory() as temp_dir:
        paths = []
        for i, raw in enumerate(image_bytes_list):
            path = os.path.join(temp_dir, f"reference_{i+1}.jpg")
            with Image.open(io.BytesIO(raw)) as im:
                im.convert("RGB").save(path, "JPEG", quality=95)
            paths.append(path)
        prompt = BASE_PROMPT + "\n" + STYLE_PROMPTS.get(style_id, STYLE_PROMPTS["minimal"])
        result = magic_client.v1.ai_image_editor.generate(
            name="TovarPix product photo — no text",
            assets={"image_file_paths": paths},
            style={"prompt": prompt},
            model="qwen-image-2.1",
            aspect_ratio="1:1",
            resolution="640px",
            image_count=1,
            wait_for_completion=True,
            download_outputs=True,
            download_directory=temp_dir,
        )
        # Magic Hour SDK versions may return output paths in slightly different shapes.
        candidates = []
        if isinstance(result, dict):
            for key in ("outputs","output","images","files"):
                value = result.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
                elif value:
                    candidates.append(value)
        else:
            for key in ("outputs","output","images","files"):
                value = getattr(result, key, None)
                if isinstance(value, list):
                    candidates.extend(value)
                elif value:
                    candidates.append(value)
        for item in candidates:
            if isinstance(item, str) and os.path.isfile(item):
                return Path(item).read_bytes()
            if isinstance(item, dict):
                for key in ("path","file_path","local_path"):
                    path = item.get(key)
                    if path and os.path.isfile(path):
                        return Path(path).read_bytes()
                for key in ("url","download_url"):
                    url = item.get(key)
                    if url:
                        r = requests.get(url, timeout=120)
                        r.raise_for_status()
                        return r.content
            if isinstance(item, str) and item.startswith("http"):
                r = requests.get(item, timeout=120)
                r.raise_for_status()
                return r.content
        # SDK often downloads files into download_directory without exposing paths in result.
        files = [p for p in Path(temp_dir).glob("*") if p.is_file() and p.suffix.lower() in (".png",".jpg",".jpeg",".webp") and "reference_" not in p.name]
        if files:
            return max(files, key=lambda p: p.stat().st_mtime).read_bytes()
        raise RuntimeError(f"Magic Hour did not return a downloadable image. Result type: {type(result).__name__}")

def start_photo_flow(chat_id, user_id):
    with state_lock:
        pending_photos[user_id] = []
        pending_jobs.pop(user_id, None)
    send_message(chat_id, "Пришли 1–6 фото товара. Можно отправить несколько фотографий, затем нажми «Готово».", {
        "keyboard":[[{"text":"✅ Готово"},{"text":"❌ Отмена"}]], "resize_keyboard":True
    })

def begin_job(chat_id, user_id, photos):
    rows = []
    for key, style in STYLE_OPTIONS.items():
        rows.append([{"text":f"{style['emoji']} {style['name']}", "callback_data":f"style:{key}"}])
    with state_lock:
        pending_jobs[user_id] = {"photos":photos, "stage":"style", "chat_id":chat_id}
    send_message(chat_id, "Выбери один из 10 стилей. Для каждого стиля используется постоянный макет, без случайных перестановок.", inline_keyboard(rows))

def ask_count(chat_id, user_id):
    rows = [
        [{"text":"1 карточка","callback_data":"count:1"},{"text":"2 карточки","callback_data":"count:2"}],
        [{"text":"3 карточки","callback_data":"count:3"},{"text":"4 карточки","callback_data":"count:4"}],
        [{"text":"5 карточек","callback_data":"count:5"},{"text":"6 карточек","callback_data":"count:6"},{"text":"7 карточек","callback_data":"count:7"}],
    ]
    send_message(chat_id, "Сколько итоговых карточек сделать? Каждая будет обработана и оформлена отдельно.", inline_keyboard(rows))

def ask_card_text(chat_id, user_id):
    with state_lock:
        job = pending_jobs.get(user_id)
        if not job:
            return
        i = job["card_index"] + 1
        total = job["count"]
        job["stage"] = "card_text"
    send_message(chat_id, f"Карточка {i} из {total}.\nНапиши точный текст, который нужно наложить поверх изображения.\n\nТекст не будет переписан нейросетью. Если текст не нужен — отправь «Пропустить».")

def ask_card_position(chat_id, user_id):
    with state_lock:
        job = pending_jobs.get(user_id)
        if not job:
            return
        job["stage"] = "card_position"
        i = job["card_index"] + 1
    rows = [
        [{"text":"⬆️ Сверху","callback_data":"pos:top"},{"text":"⬇️ Снизу","callback_data":"pos:bottom"}],
        [{"text":"⬅️ Слева","callback_data":"pos:left"},{"text":"➡️ Справа","callback_data":"pos:right"}],
        [{"text":"🎯 По центру","callback_data":"pos:center"}],
    ]
    send_message(chat_id, f"Куда поставить текст на карточке {i}?", inline_keyboard(rows))

def process_job(user_id):
    with state_lock:
        job = pending_jobs.get(user_id)
        if not job:
            return
        if user_id in busy_chats:
            return
        busy_chats.add(user_id)
        job_copy = dict(job)
    chat_id = job_copy["chat_id"]
    source = None
    delivered = 0
    try:
        source = reserve_credit(user_id)
        if not source:
            send_message(chat_id, "Бесплатная генерация уже использована, а карточки на балансе закончились. Нажми «🛍 Купить карточки».")
            return
        send_message(chat_id, "⏳ Начинаю обработку. Magic Hour улучшает только само фото — никаких надписей он не генерирует. Текст наложу отдельно, точно по твоему вводу.")
        photos = job_copy["photos"]
        style_id = job_copy["style"]
        cards = job_copy["cards"]
        for index, card in enumerate(cards, start=1):
            send_message(chat_id, f"Обрабатываю карточку {index}/{len(cards)}…")
            clean_image = generate_product_image(photos, style_id)
            final_image = render_ad_card(clean_image, style_id, card.get("text",""), card.get("position","bottom"))
            send_photo(chat_id, final_image, caption=f"Готово: карточка {index}/{len(cards)} · {STYLE_OPTIONS[style_id]['name']}")
            delivered += 1
        db_event(user_id, "generation_success", {"count":len(cards),"style":style_id})
        send_message(chat_id, "✅ Готово! Надписи нанесены поверх обработки и не передавались Magic Hour на генерацию.", main_keyboard())
    except Exception as exc:
        log.exception("Generation failed")
        if source and delivered == 0:
            try:
                refund_credit(user_id, source)
            except Exception:
                log.exception("Refund failed")
        send_message(chat_id, "Не получилось завершить генерацию. " + (f"Уже отправлено карточек: {delivered}. " if delivered else "Кредит возвращён, если генерация не отправила ни одной карточки. ") + "Попробуй ещё раз чуть позже.", main_keyboard())
        db_event(user_id, "generation_failed", {"error":str(exc)[:500],"delivered":delivered})
    finally:
        with state_lock:
            busy_chats.discard(user_id)
            pending_jobs.pop(user_id, None)
            pending_photos.pop(user_id, None)

def handle_callback_query(callback):
    callback_id = callback["id"]
    data = callback.get("data","")
    user = callback.get("from",{})
    user_id = user.get("id")
    message = callback.get("message",{})
    chat_id = message.get("chat",{}).get("id", user_id)
    answer_callback(callback_id)
    if not user_id:
        return
    with state_lock:
        job = pending_jobs.get(user_id)
    if data.startswith("style:"):
        style_id = data.split(":",1)[1]
        if style_id not in STYLE_OPTIONS or not job:
            send_message(chat_id, "Сессия устарела. Нажми «🎨 Создать карточки», чтобы начать заново.")
            return
        with state_lock:
            pending_jobs[user_id]["style"] = style_id
            pending_jobs[user_id]["stage"] = "count"
        ask_count(chat_id,user_id)
    elif data.startswith("count:"):
        if not job or job.get("stage") != "count":
            send_message(chat_id, "Сначала выбери стиль.")
            return
        count = int(data.split(":",1)[1])
        if not 1 <= count <= MAX_OUTPUT_CARDS:
            return
        with state_lock:
            pending_jobs[user_id]["count"] = count
            pending_jobs[user_id]["card_index"] = 0
            pending_jobs[user_id]["cards"] = [{"text":"","position":"bottom"} for _ in range(count)]
        ask_card_text(chat_id,user_id)
    elif data.startswith("pos:"):
        position = data.split(":",1)[1]
        if position not in POSITIONS or not job or job.get("stage") != "card_position":
            send_message(chat_id, "Выбор позиции устарел. Начни создание карточек заново.")
            return
        with state_lock:
            job = pending_jobs[user_id]
            job["cards"][job["card_index"]]["position"] = position
            job["card_index"] += 1
            done = job["card_index"] >= job["count"]
            if done:
                job["stage"] = "processing"
        if done:
            threading.Thread(target=process_job, args=(user_id,), daemon=True).start()
        else:
            ask_card_text(chat_id,user_id)

def handle_update(update):
    if "pre_checkout_query" in update:
        q = update["pre_checkout_query"]
        try:
            tg("answerPreCheckoutQuery", {"pre_checkout_query_id":q["id"],"ok":True})
        except Exception:
            log.exception("Pre-checkout failed")
        return
    if "message" in update:
        message = update["message"]
        chat = message.get("chat",{})
        chat_id = chat.get("id")
        user = message.get("from",{})
        user_id = user.get("id")
        if not user_id or not chat_id:
            return
        if "successful_payment" in message:
            payment = message["successful_payment"]
            charge_id = payment.get("telegram_payment_charge_id","")
            payload = payment.get("invoice_payload","")
            try:
                with db_connect() as conn:
                    exists = conn.execute("SELECT 1 FROM payments WHERE charge_id=%s",(charge_id,)).fetchone()
                    if not exists:
                        conn.execute("INSERT INTO payments (charge_id,telegram_id,credits,stars) VALUES (%s,%s,%s,%s)",
                                     (charge_id,user_id,PACKAGE_IMAGES,payment.get("total_amount",PACKAGE_STARS)))
                        conn.execute("INSERT INTO users (telegram_id,credits) VALUES (%s,%s) ON CONFLICT (telegram_id) DO UPDATE SET credits=users.credits+EXCLUDED.credits",
                                     (user_id,PACKAGE_IMAGES))
                send_message(chat_id, f"Оплата прошла! На баланс добавлено {PACKAGE_IMAGES} генераций комплектов карточек.", main_keyboard())
            except Exception:
                log.exception("Payment processing failed")
                send_message(chat_id, "Оплата получена, но баланс не обновился автоматически. Напиши в поддержку и укажи время платежа.")
            return

        text = (message.get("text") or "").strip()
        if text in ("/cancel","❌ Отмена"):
            with state_lock:
                pending_photos.pop(user_id,None)
                pending_jobs.pop(user_id,None)
            send_message(chat_id,"Текущий процесс отменён.",main_keyboard())
            return
        if text in ("/start",):
            ensure_user(user_id)
            send_message(chat_id, "Привет! Я TovarPix. Загрузи фото товара, выбери стиль и количество карточек. Magic Hour обрабатывает только фотографию, а твой текст накладывается отдельным слоем — без переписывания.",main_keyboard())
            return
        if text in ("/help","❓ Помощь"):
            send_message(chat_id, "Как пользоваться:\n1. Нажми «🎨 Создать карточки».\n2. Пришли 1–6 фото товара и нажми «✅ Готово».\n3. Выбери стиль и количество итоговых карточек (1–7).\n4. Для каждой карточки введи текст или нажми «Пропустить», затем выбери позицию.\n5. Бот наложит текст сам после обработки Magic Hour.",main_keyboard())
            return
        if text in ("/balance","💳 Баланс"):
            free_used, credits = get_balance(user_id)
            send_message(chat_id, f"Баланс:\nБесплатных комплектов использовано: {free_used}/{FREE_SETS}\nПлатных генераций комплектов: {credits}",main_keyboard())
            return
        if text in ("/buy","🛍 Купить карточки"):
            create_invoice(chat_id,user_id)
            return
        if text == "/features":
            send_message(chat_id, "10 фиксированных стилей, 1–7 итоговых карточек, до 6 референсных фото, отдельный точный текст для каждой карточки, выбор позиции текста, наложение текста локально через PIL. Magic Hour запрещено генерировать текст.")
            return
        if text == "/myid":
            send_message(chat_id, f"Твой Telegram ID: {user_id}")
            return
        if text == "/privacy":
            send_message(chat_id, "Фото используются для генерации карточек в рамках текущего запроса. Не отправляй конфиденциальные изображения.")
            return
        if text == "/status":
            send_message(chat_id, "Бот работает." if DATABASE_URL else "Не настроена база данных.")
            return
        if text == "/example":
            send_message(chat_id, "Пример: отправь фото товара → выбери Luxury → 2 карточки → для первой введи «Премиальный звук» и позицию «Сверху» → для второй введи «Новая коллекция» и позицию «Снизу».")
            return
        if text == "/admin":
            if user_id not in ADMIN_IDS:
                send_message(chat_id,"Команда доступна только администратору.")
            else:
                with db_connect() as conn:
                    count_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                    payments = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
                send_message(chat_id,f"Админ-статистика:\nПользователей: {count_users}\nПлатежей: {payments}")
            return
        if text == "/set_webhook":
            if user_id not in ADMIN_IDS or not RENDER_EXTERNAL_URL:
                send_message(chat_id,"Команда недоступна или не задан RENDER_EXTERNAL_URL.")
                return
            payload={"url":f"{RENDER_EXTERNAL_URL}/telegram-webhook"}
            if TELEGRAM_WEBHOOK_SECRET:
                payload["secret_token"]=TELEGRAM_WEBHOOK_SECRET
            result=tg("setWebhook",payload)
            send_message(chat_id,f"Webhook: {result}")
            return
        if text in ("/webhook_status","/runtime_stats","/bot_info"):
            if user_id not in ADMIN_IDS:
                send_message(chat_id,"Команда доступна только администратору.")
                return
            if text == "/webhook_status":
                send_message(chat_id,str(tg("getWebhookInfo")))
            elif text == "/bot_info":
                send_message(chat_id,str(tg("getMe")))
            else:
                send_message(chat_id,f"Активных обработок: {len(busy_chats)}\nОжидают фото: {len(pending_photos)}\nОжидают настройки: {len(pending_jobs)}")
            return

        if message.get("photo"):
            photos = message["photo"]
            file_id = photos[-1]["file_id"]
            with state_lock:
                stage = pending_jobs.get(user_id,{}).get("stage")
                if stage in ("card_text","card_position","processing"):
                    send_message(chat_id,"Сейчас я жду текст или выбор позиции для текущей карточки. Отправь текст, либо «Пропустить».")
                    return
                pending_photos.setdefault(user_id,[])
                if len(pending_photos[user_id]) >= MAX_REFERENCE_PHOTOS:
                    send_message(chat_id,f"Уже добавлено максимум {MAX_REFERENCE_PHOTOS} фото. Нажми «✅ Готово».")
                    return
            try:
                raw = get_file_bytes(file_id)
                with state_lock:
                    pending_photos.setdefault(user_id,[]).append(raw)
                    n = len(pending_photos[user_id])
                send_message(chat_id,f"Фото добавлено ({n}/{MAX_REFERENCE_PHOTOS}). Можешь прислать ещё или нажать «✅ Готово».")
            except Exception:
                log.exception("Could not download photo")
                send_message(chat_id,"Не удалось загрузить фото из Telegram. Попробуй отправить его ещё раз.")
            return

        if text == "✅ Готово":
            with state_lock:
                photos = list(pending_photos.get(user_id,[]))
            if not photos:
                send_message(chat_id,"Сначала пришли хотя бы одно фото товара.")
                return
            begin_job(chat_id,user_id,photos)
            return

        with state_lock:
            job = pending_jobs.get(user_id)
            stage = job.get("stage") if job else None
        if stage == "card_text":
            if text.lower() in ("пропустить","без текста","нет","skip","-"):
                custom_text = ""
            else:
                custom_text = text[:MAX_CUSTOM_TEXT_CHARS]
            with state_lock:
                job = pending_jobs[user_id]
                job["cards"][job["card_index"]]["text"] = custom_text
            if custom_text:
                ask_card_position(chat_id,user_id)
            else:
                with state_lock:
                    job = pending_jobs[user_id]
                    job["cards"][job["card_index"]]["position"] = "bottom"
                    job["card_index"] += 1
                    done = job["card_index"] >= job["count"]
                    if done:
                        job["stage"] = "processing"
                if done:
                    threading.Thread(target=process_job,args=(user_id,),daemon=True).start()
                else:
                    ask_card_text(chat_id,user_id)
            return

        if text in ("🎨 Создать карточки","/create"):
            start_photo_flow(chat_id,user_id)
            return
        if text == "/buy":
            create_invoice(chat_id,user_id)
            return
        if text == "/start":
            send_message(chat_id,"Нажми «🎨 Создать карточки», чтобы начать.",main_keyboard())
            return
        if text:
            send_message(chat_id,"Выбери действие в меню: «🎨 Создать карточки», «💳 Баланс» или «🛍 Купить карточки».",main_keyboard())
        return

    if "callback_query" in update:
        handle_callback_query(update["callback_query"])

@app.route("/", methods=["GET"])
def home():
    return "TovarPix is running", 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok":True,"service":"tovarpix"}), 200

@app.route("/ready", methods=["GET"])
def ready():
    try:
        with db_connect() as conn:
            conn.execute("SELECT 1")
        return jsonify({"ready":True}), 200
    except Exception as exc:
        return jsonify({"ready":False,"error":str(exc)[:200]}), 503

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if TELEGRAM_WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token","")
        if supplied != TELEGRAM_WEBHOOK_SECRET:
            return "forbidden", 403
    update = request.get_json(silent=True) or {}
    try:
        handle_update(update)
    except Exception:
        log.exception("Update handling failed")
    return "ok", 200

@app.route("/set_webhook", methods=["GET","POST"])
def set_webhook_endpoint():
    if not ADMIN_ENDPOINT_SECRET or request.args.get("secret") != ADMIN_ENDPOINT_SECRET:
        return jsonify({"ok":False,"error":"unauthorized"}), 401
    if not RENDER_EXTERNAL_URL:
        return jsonify({"ok":False,"error":"RENDER_EXTERNAL_URL is empty"}), 400
    payload={"url":f"{RENDER_EXTERNAL_URL}/telegram-webhook"}
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"]=TELEGRAM_WEBHOOK_SECRET
    try:
        result=tg("setWebhook",payload)
        return jsonify({"ok":True,"result":result})
    except Exception as exc:
        return jsonify({"ok":False,"error":str(exc)}), 500

@app.route("/webhook_status", methods=["GET"])
def webhook_status_endpoint():
    if not ADMIN_ENDPOINT_SECRET or request.args.get("secret") != ADMIN_ENDPOINT_SECRET:
        return jsonify({"ok":False,"error":"unauthorized"}), 401
    try:
        return jsonify(tg("getWebhookInfo"))
    except Exception as exc:
        return jsonify({"ok":False,"error":str(exc)}), 500

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT)
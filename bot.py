import os
import io
import uuid
import json
import base64
import logging
import threading
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque

import requests
import psycopg
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont, ImageOps
from magic_hour import Client

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("tovarpix")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MAGIC_HOUR_API_KEY = os.environ.get("MAGIC_HOUR_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_IMAGE_SIDE = int(os.environ.get("MAX_IMAGE_SIDE", "8000"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "30"))
RATE_LIMIT_MESSAGES = int(os.environ.get("RATE_LIMIT_MESSAGES", "12"))

ADMIN_IDS = {
    value.strip()
    for value in os.environ.get("ADMIN_IDS", "").split(",")
    if value.strip()
}

PACKAGE_IMAGES = 5
PACKAGE_STARS = 5
MAX_PROMPT_LENGTH = 1500
APP_STARTED_AT = time.time()

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not MAGIC_HOUR_API_KEY:
    raise RuntimeError("Не задан MAGIC_HOUR_API_KEY")
if not DATABASE_URL:
    raise RuntimeError("Не задан DATABASE_URL")

API_URL = f"https://api.telegram.org/bot{TOKEN}"
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
http = requests.Session()
http.headers.update({"User-Agent": "TovarPix/2.0"})
magic_client = Client(token=MAGIC_HOUR_API_KEY)

STYLE_OPTIONS = {
    "minimal": {
        "name": "Чистый минимализм", "emoji": "▫️",
        "description": "светлый фон, чистая композиция, минимум деталей",
        "top": "#F4F4F0", "bottom": "#E3E5E1", "panel": "#FFFFFF",
        "text": "#202421", "muted": "#606963", "accent": "#2C7A62",
        "kind": "minimal"
    },
    "luxury": {
        "name": "Luxury", "emoji": "✨",
        "description": "чёрный и графитовый фон, золотые акценты, премиальный свет",
        "top": "#24211D", "bottom": "#090909", "panel": "#151310",
        "text": "#FFF8E8", "muted": "#C7BDA8", "accent": "#D7B56D",
        "kind": "luxury"
    },
    "cozy": {
        "name": "Домашний уют", "emoji": "🏡",
        "description": "тёплые кремовые оттенки, мягкий дневной свет, уютная атмосфера",
        "top": "#F7E9D5", "bottom": "#E9D1B3", "panel": "#FFF7EA",
        "text": "#4A3528", "muted": "#876E5B", "accent": "#B56C43",
        "kind": "cozy"
    },
    "market": {
        "name": "Яркий маркетплейс", "emoji": "🛍️",
        "description": "контрастный фон, крупные акценты, энергичная коммерческая подача",
        "top": "#FFF4DF", "bottom": "#FFD7B5", "panel": "#FFFFFF",
        "text": "#20202A", "muted": "#5C5B68", "accent": "#F05A28",
        "kind": "market"
    },
    "tech": {
        "name": "Tech", "emoji": "🔷",
        "description": "холодная технологичная студия, графит, синий свет, точные линии",
        "top": "#172A3A", "bottom": "#080F1A", "panel": "#111D2A",
        "text": "#F2F8FF", "muted": "#A9C0D4", "accent": "#65D6FF",
        "kind": "tech"
    },
    "eco": {
        "name": "Эко и натуральность", "emoji": "🌿",
        "description": "натуральные зелёные и песочные оттенки, мягкий природный свет",
        "top": "#E8EBDD", "bottom": "#C8D4B8", "panel": "#F7F6E9",
        "text": "#26392B", "muted": "#5E725F", "accent": "#4C8058",
        "kind": "eco"
    },
    "fashion": {
        "name": "Fashion Editorial", "emoji": "🖤",
        "description": "редакционная fashion-съёмка, выразительная композиция, строгая палитра",
        "top": "#F1F0EE", "bottom": "#D5D3D0", "panel": "#FAF9F7",
        "text": "#151515", "muted": "#5A5856", "accent": "#C53A48",
        "kind": "fashion"
    },
    "info": {
        "name": "Инфографика PRO", "emoji": "📊",
        "description": "светлая инфографика, аккуратные блоки и чёткая иерархия",
        "top": "#F1F6FC", "bottom": "#DCE8F7", "panel": "#FFFFFF",
        "text": "#172A46", "muted": "#526985", "accent": "#2869C7",
        "kind": "info"
    },
    "pop": {
        "name": "Pop & Fun", "emoji": "🎨",
        "description": "яркие фиолетовые и жёлтые акценты, дружелюбная динамичная подача",
        "top": "#F2E8FF", "bottom": "#D8C2FF", "panel": "#FFFDF7",
        "text": "#29213C", "muted": "#66577E", "accent": "#7546D8",
        "kind": "pop"
    },
    "cinematic": {
        "name": "Cinematic", "emoji": "🎬",
        "description": "кинематографичный тёмный фон, драматичный мягкий свет и глубокие тени",
        "top": "#30343B", "bottom": "#080A0D", "panel": "#12151A",
        "text": "#FFFFFF", "muted": "#B5BBC4", "accent": "#FFB86B",
        "kind": "cinematic"
    },
}

CARD_ROLES = [
    "главная карточка: показать товар целиком, чистая hero-композиция",
    "карточка деталей: показать фактуру и детали без изменения конструкции",
    "карточка применения: показать реалистичный сценарий использования, если он очевиден",
    "карточка преимуществ: оставить свободное пространство для текста",
    "карточка материалов: подчеркнуть фактуру и отделку, не выдумывая свойства",
    "карточка атмосферы: выразительный контекст использования без лишнего реквизита",
    "финальная карточка: эффектная, но правдоподобная презентация товара целиком",
]

pending_photos = {}
pending_jobs = {}
busy_chats = set()
state_lock = threading.RLock()
rate_lock = threading.Lock()
message_times = defaultdict(deque)
runtime_stats = {
    "updates": 0,
    "images_started": 0,
    "images_succeeded": 0,
    "images_failed": 0,
    "cards_rendered": 0,
    "last_error": None,
    "last_success_at": None,
}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def is_admin(chat_id):
    return str(chat_id) in ADMIN_IDS


def clean_user_text(value, limit=MAX_PROMPT_LENGTH):
    value = str(value or "").replace("\x00", " ").strip()
    return " ".join(value.split())[:limit]


def note_runtime_error(error):
    with state_lock:
        runtime_stats["last_error"] = {
            "at": utc_now_iso(),
            "type": type(error).__name__,
            "message": str(error)[:300],
        }


def allow_message_rate(chat_id):
    now = time.monotonic()
    with rate_lock:
        bucket = message_times[chat_id]
        while bucket and now - bucket[0] > RATE_LIMIT_WINDOW:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_MESSAGES:
            return False
        bucket.append(now)
        return True


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} сек"
    return f"{secs} сек"


def user_balance_text(balance):
    if balance.get("admin"):
        return "👑 У тебя безлимит."
    free_text = "доступна" if not balance["free_used"] else "уже использована"
    return (
        f"🎁 Бесплатный комплект: {free_text}.\n"
        f"⭐ Купленных комплектов: {balance['credits']}."
    )


def db_connect():
    return psycopg.connect(DATABASE_URL, connect_timeout=15)


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                free_used BOOLEAN NOT NULL DEFAULT FALSE,
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
                telegram_id BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_processing_events_created
            ON processing_events(created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_processing_events_user
            ON processing_events(telegram_id, created_at)
        """)
    logger.info("PostgreSQL database is ready")


def record_event(chat_id, event_type, detail=""):
    try:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO processing_events (telegram_id, event_type, detail)
                VALUES (%s, %s, %s)
                """,
                (int(chat_id), str(event_type)[:40], str(detail)[:300])
            )
    except Exception:
        logger.exception("Could not record processing event")


init_db()


def ensure_user(chat_id):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id)
            VALUES (%s)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            (chat_id,)
        )


def get_balance(chat_id):
    ensure_user(chat_id)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT free_used, credits FROM users WHERE telegram_id = %s",
            (chat_id,)
        ).fetchone()
    return {"free_used": row[0], "credits": row[1], "admin": is_admin(chat_id)}


def reserve_credit(chat_id):
    if is_admin(chat_id):
        ensure_user(chat_id)
        return "admin"

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id)
            VALUES (%s)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            (chat_id,)
        )
        row = conn.execute(
            """
            SELECT free_used, credits FROM users
            WHERE telegram_id = %s FOR UPDATE
            """,
            (chat_id,)
        ).fetchone()
        free_used, credits = row

        if not free_used:
            conn.execute(
                "UPDATE users SET free_used = TRUE WHERE telegram_id = %s",
                (chat_id,)
            )
            return "free"

        if credits > 0:
            conn.execute(
                "UPDATE users SET credits = credits - 1 WHERE telegram_id = %s",
                (chat_id,)
            )
            return "paid"
        return None


def refund_credit(chat_id, credit_type):
    if credit_type in (None, "admin"):
        return
    with db_connect() as conn:
        if credit_type == "free":
            conn.execute(
                "UPDATE users SET free_used = FALSE WHERE telegram_id = %s",
                (chat_id,)
            )
        elif credit_type == "paid":
            conn.execute(
                "UPDATE users SET credits = credits + 1 WHERE telegram_id = %s",
                (chat_id,)
            )


def get_admin_stats():
    with db_connect() as conn:
        users = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(credits), 0) FROM users"
        ).fetchone()
        payments = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(stars), 0), COALESCE(SUM(credits), 0)
            FROM payments
            """
        ).fetchone()
        events = conn.execute(
            """
            SELECT event_type, COUNT(*) FROM processing_events
            WHERE created_at > NOW() - INTERVAL '7 days'
            GROUP BY event_type
            """
        ).fetchall()
    return {
        "users": users[0],
        "credits_remaining": users[1],
        "payment_count": payments[0],
        "stars_total": payments[1],
        "credits_sold": payments[2],
        "events_7d": dict(events),
    }


def telegram_api(method, data=None, timeout=35):
    response = http.post(
        f"{API_URL}/{method}", json=data or {}, timeout=timeout
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result.get("result")


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    data = {"chat_id": chat_id, "text": str(text)[:4096]}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        telegram_api("sendMessage", data)
    except Exception:
        logger.exception("Could not send Telegram message")


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "⭐ Купить 5 комплектов"}],
            [{"text": "💡 Пример промпта"}, {"text": "💰 Баланс"}],
            [{"text": "❓ Помощь"}, {"text": "📋 Возможности"}],
            [{"text": "❌ Отмена"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


def style_keyboard():
    rows = []
    items = list(STYLE_OPTIONS.items())
    for index in range(0, len(items), 2):
        row = []
        for style_id, style in items[index:index + 2]:
            row.append({
                "text": f"{style['emoji']} {style['name']}",
                "callback_data": f"style:{style_id}"
            })
        rows.append(row)
    return {"inline_keyboard": rows}


def count_keyboard():
    return {"inline_keyboard": [
        [{"text": str(n), "callback_data": f"count:{n}"} for n in (1, 2, 3, 4)],
        [{"text": str(n), "callback_data": f"count:{n}"} for n in (5, 6, 7)],
    ]}


def answer_callback_query(callback_id, text=None):
    try:
        data = {"callback_query_id": callback_id}
        if text:
            data["text"] = text[:180]
        telegram_api("answerCallbackQuery", data)
    except Exception:
        logger.exception("Could not answer callback query")


def reset_keyboard(chat_id):
    telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Обновляю меню...",
            "reply_markup": {"remove_keyboard": True}
        }
    )


def send_invoice(chat_id):
    payload = f"tovarpix_pack5_{chat_id}_{uuid.uuid4().hex}"
    telegram_api("sendInvoice", {
        "chat_id": chat_id,
        "title": "TovarPix — 5 комплектов карточек",
        "description": "Пять комплектов карточек товаров; каждый комплект от 1 до 7 карточек",
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": "5 комплектов", "amount": PACKAGE_STARS}]
    })


def answer_pre_checkout(query_id, ok, error_message=None):
    data = {"pre_checkout_query_id": query_id, "ok": ok}
    if error_message:
        data["error_message"] = error_message
    telegram_api("answerPreCheckoutQuery", data)


def handle_pre_checkout(query):
    try:
        payload = query.get("invoice_payload", "")
        sender_id = query.get("from", {}).get("id")
        valid = (
            payload.startswith(f"tovarpix_pack5_{sender_id}_")
            and query.get("currency") == "XTR"
            and query.get("total_amount") == PACKAGE_STARS
        )
        answer_pre_checkout(
            query["id"], valid,
            None if valid else "Не удалось проверить заказ. Попробуй снова."
        )
    except Exception:
        logger.exception("Pre-checkout validation failed")


def handle_successful_payment(message):
    payment = message.get("successful_payment", {})
    chat_id = message.get("chat", {}).get("id")
    payload = payment.get("invoice_payload", "")
    charge_id = payment.get("telegram_payment_charge_id")
    amount = payment.get("total_amount")
    currency = payment.get("currency")
    expected_prefix = f"tovarpix_pack5_{chat_id}_"

    if (
        not chat_id or not charge_id
        or not payload.startswith(expected_prefix)
        or currency != "XTR" or amount != PACKAGE_STARS
    ):
        logger.error("Payment validation failed")
        if chat_id:
            send_message(
                chat_id,
                "Не удалось автоматически проверить платёж. Обратись в поддержку."
            )
        return

    try:
        with db_connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO payments (charge_id, telegram_id, credits, stars)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (charge_id) DO NOTHING
                RETURNING charge_id
                """,
                (charge_id, chat_id, PACKAGE_IMAGES, amount)
            ).fetchone()

            if inserted:
                conn.execute(
                    """
                    INSERT INTO users (telegram_id, credits)
                    VALUES (%s, %s)
                    ON CONFLICT (telegram_id)
                    DO UPDATE SET credits = users.credits + EXCLUDED.credits
                    """,
                    (chat_id, PACKAGE_IMAGES)
                )

        if inserted:
            record_event(chat_id, "payment", f"{amount} Stars; {PACKAGE_IMAGES} credits")
            send_message(
                chat_id,
                "✅ Оплата получена!\n\n"
                f"Начислено: {PACKAGE_IMAGES} комплектов карточек.\n"
                "Отправь фотографию товара, чтобы начать.",
                main_keyboard()
            )
        else:
            send_message(chat_id, "Этот платёж уже был учтён.")
    except Exception:
        logger.exception("Could not credit successful payment")
        send_message(
            chat_id,
            "Платёж получен Telegram, но не удалось начислить комплекты. "
            "Не оплачивай повторно — обратись в поддержку."
        )


def download_telegram_photo(file_id):
    info = telegram_api("getFile", {"file_id": file_id})
    file_path = info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram did not return file path")
    response = http.get(
        f"https://api.telegram.org/file/bot{TOKEN}/{file_path}",
        timeout=45
    )
    response.raise_for_status()
    if len(response.content) > MAX_IMAGE_BYTES:
        raise RuntimeError("Фото больше допустимого размера")
    return response.content


def normalize_image_bytes(image_bytes, max_side=MAX_IMAGE_SIDE, quality=94):
    with Image.open(io.BytesIO(image_bytes)) as original:
        image = ImageOps.exif_transpose(original)
        if image.width > max_side or image.height > max_side:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image.convert("RGBA"))
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, "JPEG", quality=quality, optimize=True)
        return output.getvalue()


def inspect_image(image_bytes):
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
            image_format = image.format or "unknown"
            if width < 64 or height < 64:
                raise RuntimeError("Изображение слишком маленькое")
            if width > 30000 or height > 30000:
                raise RuntimeError("Размеры изображения слишком большие")
            return {
                "width": width,
                "height": height,
                "format": image_format,
                "pixels": width * height,
            }
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Не удалось прочитать изображение") from exc


BASE_PROMPT = """
You are a professional commercial product photographer and retoucher.
Edit the supplied photograph according to the user's instructions.
Prioritize realistic photography, coherent lighting, clean composition,
natural contact shadows, believable reflections and premium e-commerce
visual quality.

PRODUCT PRESERVATION IS THE HIGHEST PRIORITY:
Keep the original product recognizable and faithful to the input.
Preserve its exact shape, proportions, color, materials, buttons,
camera layout, ports, labels, branding, logos and small physical details.
Do not redesign, replace, duplicate or invent any product parts.
Do not change any text printed on the product. Preserve the exact number
and position of cameras, buttons, stickers, holes, lenses and accessories.

BACKGROUND AND LIGHTING:
Make the requested environment look like a real professional photo shoot.
Match the direction, softness and color of light on the product and its
environment. Add realistic contact shadows so the product feels grounded.
Use a natural photographic transition between product and background.
Avoid generic gray backgrounds unless requested.

COMPOSITION:
Keep the product as the clear focal point. Use realistic perspective,
balanced negative space and restrained styling. Add props only if the user
explicitly requests them and they do not obscure the product.

TEXT:
Do not add promotional text, invented specifications, prices, badges,
watermarks or typography inside the image unless the user specifically
requests it. If text is requested, do not invent product facts.

QUALITY:
Avoid halos around cut-out edges, floating products, fake reflections,
excessive glow, oversharpening, plastic-looking materials, distorted
geometry, blurry product details and unnecessary decorative elements.
The result should look like a polished real commercial product photograph.
"""


def generate_product_image(image_bytes, user_prompt):
    prompt = BASE_PROMPT + "\nUSER REQUEST:\n" + clean_user_text(user_prompt)
    normalized = normalize_image_bytes(image_bytes)

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / "input.jpg"
        input_path.write_bytes(normalized)

        result = magic_client.v1.ai_image_editor.generate(
            name="TovarPix product photo",
            assets={"image_file_paths": [str(input_path)]},
            style={"prompt": prompt},
            model="qwen-image-2.1",
            aspect_ratio="1:1",
            resolution="640px",
            image_count=1,
            wait_for_completion=True,
            download_outputs=True,
            download_directory=temp_dir
        )

        if result.status != "complete":
            error = getattr(result, "error", None)
            if error:
                raise RuntimeError(f"Magic Hour: {error.code}: {error.message}")
            raise RuntimeError(f"Generation status: {result.status}")

        paths = result.downloaded_paths
        if not paths or not Path(str(paths[0])).is_file():
            raise RuntimeError("Generated image file not found")

        output_bytes = Path(str(paths[0])).read_bytes()
        return io.BytesIO(
            normalize_image_bytes(output_bytes, max_side=3000, quality=94)
        )


def get_ad_copy(image_bytes, user_prompt):
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not configured")
        return None

    normalized = normalize_image_bytes(image_bytes, max_side=1000, quality=80)
    with Image.open(io.BytesIO(normalized)) as image:
        image.thumbnail((512, 512))
        image_buffer = io.BytesIO()
        image.save(image_buffer, format="JPEG", quality=78)

    instruction = f"""
Ты — арт-директор интернет-магазина и копирайтер рекламных карточек.
Запрос пользователя:
{clean_user_text(user_prompt)}

Верни только JSON:
{{
  "make_ad_card": true,
  "title": "Короткий заголовок",
  "subtitle": "Короткий подзаголовок",
  "badge": "Короткая плашка",
  "features": ["Особенность 1", "Особенность 2"],
  "accent": "#D9FF43"
}}

ПРАВИЛА:
1. make_ad_card=true для рекламной карточки, баннера, продающего дизайна,
   текста на фото, плашек или характеристик товара.
2. Не выдумывай цену, скидку, гарантию, технические параметры или факты.
3. В features используй только характеристики, явно указанные пользователем.
4. Если данных мало, используй нейтральный заголовок.
5. title до 34 символов; subtitle до 55; badge до 18.
6. features — максимум 3 пункта, каждый до 32 символов.
7. Пиши по-русски.
"""

    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{GEMINI_MODEL}:generateContent",
        params={"key": GEMINI_API_KEY},
        json={
            "contents": [{
                "parts": [
                    {"text": instruction},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64.b64encode(
                                image_buffer.getvalue()
                            ).decode("ascii")
                        }
                    }
                ]
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.25,
                "maxOutputTokens": 400
            }
        },
        timeout=35
    )
    response.raise_for_status()
    body = response.json()
    candidates = body.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    answer = next(
        (part.get("text", "") for part in parts if part.get("text")), ""
    )
    if not answer:
        raise RuntimeError("Gemini returned an empty response")

    answer = answer.strip()
    if answer.startswith("```"):
        answer = answer.split("\n", 1)[-1]
        if answer.endswith("```"):
            answer = answer[:-3].strip()
    data = json.loads(answer)

    if not data.get("make_ad_card"):
        return None

    allowed_accents = {
        "#D9FF43", "#FFFFFF", "#65D6FF", "#FFB86B", "#FF7A90"
    }
    accent = str(data.get("accent", "#D9FF43")).upper()
    if accent not in allowed_accents:
        accent = "#D9FF43"

    raw_features = data.get("features", [])
    if not isinstance(raw_features, list):
        raw_features = []

    return {
        "title": clean_user_text(data.get("title", ""), 34),
        "subtitle": clean_user_text(data.get("subtitle", ""), 55),
        "badge": clean_user_text(data.get("badge", ""), 18),
        "features": [
            clean_user_text(item, 32)
            for item in raw_features
            if isinstance(item, str) and clean_user_text(item, 32)
        ][:3],
        "accent": accent,
    }


def load_font(size, bold=False):
    if bold:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
    else:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def text_width(draw, text, font):
    box = draw.textbbox((0, 0), str(text), font=font)
    return box[2] - box[0]


def fit_text(draw, text, font, max_width):
    text = str(text or "").strip()
    if text_width(draw, text, font) <= max_width:
        return text
    while text:
        candidate = text.rstrip() + "…"
        if text_width(draw, candidate, font) <= max_width:
            return candidate
        text = text[:-1]
    return ""


def wrap_text(draw, text, font, max_width, max_lines=2):
    words = str(text or "").split()
    lines = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and words:
        joined = " ".join(lines)
        if len(joined) < len(str(text).strip()):
            lines[-1] = fit_text(draw, lines[-1], font, max_width)
    return lines


def hex_to_rgb(value, fallback=(217, 255, 67)):
    try:
        value = str(value).lstrip("#")
        if len(value) != 6:
            return fallback
        return tuple(
            int(value[index:index + 2], 16)
            for index in (0, 2, 4)
        )
    except Exception:
        return fallback


def draw_gradient(canvas, box, top_color, bottom_color):
    x0, y0, x1, y1 = box
    height = max(1, y1 - y0)
    top = hex_to_rgb(top_color, (24, 26, 32))
    bottom = hex_to_rgb(bottom_color, (13, 15, 20))
    draw = ImageDraw.Draw(canvas)
    for offset in range(height):
        ratio = offset / max(1, height - 1)
        color = tuple(
            int(top[channel] * (1 - ratio) + bottom[channel] * ratio)
            for channel in range(3)
        )
        draw.line((x0, y0 + offset, x1, y0 + offset), fill=color)


def render_ad_card(image_bytes, ad, style_id="minimal", card_index=1, total_cards=1):
    style = STYLE_OPTIONS.get(style_id, STYLE_OPTIONS["minimal"])
    normalized = normalize_image_bytes(image_bytes, max_side=1200, quality=93)
    with Image.open(io.BytesIO(normalized)) as source:
        product_image = source.convert("RGB")

    width = 1080
    margin = 58
    image_area_height = 820
    footer_height = 490
    total_height = image_area_height + footer_height
    product_image.thumbnail(
        (width - 2 * margin, image_area_height - 2 * margin),
        Image.Resampling.LANCZOS
    )

    canvas = Image.new("RGB", (width, total_height), style["panel"])
    draw_gradient(
        canvas, (0, 0, width, image_area_height),
        style["top"], style["bottom"]
    )
    draw = ImageDraw.Draw(canvas)
    kind = style["kind"]

    if kind == "luxury":
        draw.rectangle(
            (24, 24, width - 25, image_area_height - 24),
            outline=style["accent"], width=2
        )
        draw.line(
            (margin, 70, margin + 110, 70),
            fill=style["accent"], width=3
        )
    elif kind == "tech":
        for gx in range(35, width, 90):
            draw.line((gx, 0, gx, image_area_height), fill="#294255", width=1)
        for gy in range(35, image_area_height, 90):
            draw.line((0, gy, width, gy), fill="#20384A", width=1)
    elif kind == "eco":
        draw.ellipse(
            (width - 250, 35, width - 45, 240),
            outline="#A4B999", width=3
        )
        draw.ellipse(
            (width - 210, 70, width - 80, 200),
            outline="#B6C8A8", width=2
        )
    elif kind == "pop":
        draw.ellipse((28, 35, 130, 137), fill="#E8C84D")
        draw.ellipse((width - 150, 45, width - 55, 140), fill="#A48AF0")
    elif kind == "fashion":
        draw.line(
            (margin, 48, width - margin, 48),
            fill=style["text"], width=2
        )
        draw.line(
            (margin, image_area_height - 48, width - margin, image_area_height - 48),
            fill=style["text"], width=2
        )
    elif kind == "cinematic":
        draw.rectangle(
            (18, 18, width - 19, image_area_height - 19),
            outline="#555A63", width=2
        )
    elif kind == "market":
        draw.polygon([(0, 0), (260, 0), (0, 180)], fill="#FFE2A5")
    elif kind == "info":
        draw.rounded_rectangle(
            (28, 28, width - 28, image_area_height - 28),
            radius=28, outline="#B8CBE3", width=3
        )

    px = (width - product_image.width) // 2
    py = (image_area_height - product_image.height) // 2
    canvas.paste(product_image, (px, py))
    draw = ImageDraw.Draw(canvas)

    if kind in ("minimal", "cozy", "market", "fashion", "info", "eco", "pop"):
        draw.rectangle(
            (0, image_area_height, width, total_height),
            fill=style["panel"]
        )
    else:
        draw_gradient(
            canvas, (0, image_area_height, width, total_height),
            style["panel"], style["bottom"]
        )
        draw = ImageDraw.Draw(canvas)

    draw.line(
        (margin, image_area_height, width - margin, image_area_height),
        fill=style["accent"],
        width=3 if kind in ("luxury", "tech", "cinematic") else 2
    )

    x = margin
    y = image_area_height + 35
    content_width = width - margin * 2
    accent_rgb = hex_to_rgb(style["accent"])
    overline_font = load_font(19, bold=True)

    if total_cards > 1:
        overline = (
            f"TOVARPIX / {style['name'].upper()} / "
            f"{card_index} ИЗ {total_cards}"
        )
    else:
        overline = f"TOVARPIX / {style['name'].upper()}"

    draw.text(
        (x, y), overline[:68],
        font=overline_font, fill=style["accent"]
    )
    y += 43

    badge = clean_user_text(ad.get("badge", ""), 18)
    if badge:
        badge_font = load_font(22, bold=True)
        badge_text = fit_text(draw, badge, badge_font, content_width - 42)
        badge_width = min(
            content_width,
            text_width(draw, badge_text, badge_font) + 42
        )
        badge_height = 46

        if kind in ("minimal", "eco", "fashion", "info"):
            draw.rounded_rectangle(
                (x, y, x + badge_width, y + badge_height),
                radius=14, outline=style["accent"], width=2
            )
            badge_fill = style["text"]
        else:
            draw.rounded_rectangle(
                (x, y, x + badge_width, y + badge_height),
                radius=14, fill=style["accent"]
            )
            badge_fill = (
                "#FFFFFF"
                if kind in ("tech", "cinematic", "luxury")
                else "#171717"
            )

        draw.text(
            (x + 20, y + 8), badge_text,
            font=badge_font, fill=badge_fill
        )
        y += badge_height + 17

    title = clean_user_text(ad.get("title", ""), 34) or "Товар в деталях"
    title_font = load_font(45 if kind != "market" else 49, bold=True)
    for line in wrap_text(draw, title, title_font, content_width, max_lines=2):
        draw.text((x, y), line, font=title_font, fill=style["text"])
        y += 56
    y += 1

    subtitle = clean_user_text(ad.get("subtitle", ""), 55)
    subtitle_font = load_font(25)
    for line in wrap_text(draw, subtitle, subtitle_font, content_width, max_lines=2):
        draw.text((x, y), line, font=subtitle_font, fill=style["muted"])
        y += 34

    features = ad.get("features", [])[:3]
    if features:
        y += 13
        feature_font = load_font(21)
        for feature in features:
            if y + 38 > total_height - 53:
                break
            label = fit_text(
                draw, clean_user_text(feature, 32),
                feature_font, content_width - 45
            )
            if kind == "info":
                draw.rounded_rectangle(
                    (x, y, x + content_width, y + 36),
                    radius=10, fill="#E8F0FA"
                )
                draw.ellipse((x + 12, y + 12, x + 24, y + 24), fill=accent_rgb)
            elif kind == "market":
                draw.rounded_rectangle(
                    (x, y, x + content_width, y + 36),
                    radius=10, fill="#FFF0D9"
                )
                draw.ellipse((x + 12, y + 12, x + 24, y + 24), fill=accent_rgb)
            else:
                draw.rounded_rectangle(
                    (x, y, x + content_width, y + 36),
                    radius=10, outline=style["accent"], width=2
                )
                draw.ellipse((x + 12, y + 12, x + 22, y + 22), fill=accent_rgb)
            draw.text(
                (x + 32, y + 5), label,
                font=feature_font, fill=style["text"]
            )
            y += 42

    brand_font = load_font(16, bold=True)
    brand = "PRODUCT PHOTOGRAPHY • TOVARPIX"
    brand_width = text_width(draw, brand, brand_font)
    draw.text(
        (width - margin - brand_width, total_height - 28),
        brand, font=brand_font, fill=style["muted"]
    )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=93, optimize=True, progressive=True)
    output.seek(0)
    return output


def add_ad_layout_if_requested(
    image_bytes, user_prompt, style_id="minimal",
    card_index=1, total_cards=1
):
    style = STYLE_OPTIONS.get(style_id, STYLE_OPTIONS["minimal"])
    role = CARD_ROLES[min(card_index - 1, len(CARD_ROLES) - 1)]
    forced_prompt = (
        "Создай рекламную карточку товара. "
        + clean_user_text(user_prompt)
        + f" Роль этой карточки в комплекте: {role}. "
        "Не выдумывай характеристики, цену, скидку, гарантию или комплект поставки."
    )
    try:
        ad = get_ad_copy(image_bytes, forced_prompt)
        if not ad:
            ad = {
                "title": "Товар в деталях" if card_index != 1 else "Главный акцент",
                "subtitle": "Лаконичная презентация товара",
                "badge": style["name"][:18],
                "features": [],
                "accent": style["accent"],
            }
        ad["accent"] = style["accent"]
        result = render_ad_card(
            image_bytes, ad, style_id, card_index, total_cards
        )
        with state_lock:
            runtime_stats["cards_rendered"] += 1
        return result
    except Exception as exc:
        logger.exception("Ad card rendering failed; using a generic style card")
        note_runtime_error(exc)
        fallback_ad = {
            "title": "Товар в деталях",
            "subtitle": "Презентация товара",
            "badge": style["name"][:18],
            "features": [],
            "accent": style["accent"],
        }
        return render_ad_card(
            image_bytes, fallback_ad, style_id, card_index, total_cards
        )


def send_generated_photo(
    chat_id, image_buffer, caption=None, filename="tovarpix_result.jpg"
):
    image_buffer.seek(0)
    response = http.post(
        f"{API_URL}/sendPhoto",
        data={
            "chat_id": str(chat_id),
            "caption": caption or "✅ Готово! Вот твоя обработанная карточка."
        },
        files={"photo": (filename, image_buffer, "image/jpeg")},
        timeout=60
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram could not send result"))


def process_photo(chat_id, file_id, prompt, style_id="minimal", card_count=1):
    credit_type = None
    started = time.monotonic()
    style = STYLE_OPTIONS.get(style_id, STYLE_OPTIONS["minimal"])
    card_count = max(1, min(7, safe_int(card_count, 1)))

    with state_lock:
        runtime_stats["images_started"] += 1

    try:
        credit_type = reserve_credit(chat_id)
        if credit_type is None:
            send_message(
                chat_id,
                "🎁 Бесплатный комплект уже использован, а купленные комплекты закончились.\n\n"
                "Один комплект из 1–7 карточек стоит 1 кредит.\n"
                "Купи пакет из 5 комплектов за 5 ⭐, чтобы продолжить.",
                main_keyboard()
            )
            return

        prompt = clean_user_text(prompt) or (
            "Создай качественные карточки товара, сохранив товар без изменений."
        )
        record_event(
            chat_id, "processing_started",
            f"style={style_id};count={card_count};{prompt[:120]}"
        )
        send_message(
            chat_id,
            f"🎨 Начинаю комплект: {card_count} карточек.\n"
            f"Стиль: {style['emoji']} {style['name']}\n\n"
            "За весь комплект списывается 1 кредит. Сначала подготовлю все карточки; это может занять несколько минут."
        )

        image_bytes = download_telegram_photo(file_id)
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise RuntimeError("Фото больше допустимого размера")
        image_info = inspect_image(image_bytes)
        logger.info(
            "Image accepted: chat=%s format=%s size=%sx%s bytes=%s",
            chat_id, image_info["format"], image_info["width"],
            image_info["height"], len(image_bytes)
        )

        prepared = []
        for index in range(1, card_count + 1):
            role = CARD_ROLES[min(index - 1, len(CARD_ROLES) - 1)]
            style_prompt = (
                f"Создай карточку {index} из {card_count} для комплекта в стиле "
                f"«{style['name']}». Визуальное направление: {style['description']}. "
                f"Роль карточки: {role}. Сохрани тот же товар: точную форму, "
                "пропорции, цвет, материалы, логотипы, маркировки, камеры, "
                "кнопки и мелкие детали. Не добавляй текст, цены, скидки или "
                "вымышленные детали внутрь фотографии. Меняй композицию "
                "окружения, освещение и фон; изображение должно выглядеть "
                "как часть единого коммерческого комплекта. Дополнительные "
                "пожелания: " + prompt
            )
            result = generate_product_image(image_bytes, style_prompt)
            qwen_bytes = result.getvalue()
            card = add_ad_layout_if_requested(
                qwen_bytes,
                f"{prompt}. Роль карточки: {role}. Не выдумывай характеристики.",
                style_id=style_id,
                card_index=index,
                total_cards=card_count
            )
            prepared.append(card)
            send_message(chat_id, f"Подготовлена карточка {index}/{card_count}…")

        for index, card in enumerate(prepared, start=1):
            send_generated_photo(
                chat_id, card,
                caption=f"✅ Карточка {index}/{card_count} • {style['name']}",
                filename=f"tovarpix_{style_id}_{index}.jpg"
            )

        with state_lock:
            runtime_stats["images_succeeded"] += 1
            runtime_stats["last_success_at"] = utc_now_iso()
        record_event(
            chat_id, "processing_succeeded",
            f"style={style_id};count={card_count};duration={int(time.monotonic() - started)}s"
        )
        balance = get_balance(chat_id)
        balance_text = (
            "👑 У тебя безлимит."
            if balance["admin"]
            else f"⭐ Доступно комплектов карточек: {balance['credits']}."
        )
        send_message(
            chat_id,
            f"🎉 Комплект готов: {card_count} карточек.\n"
            + balance_text
            + "\n\nМожешь отправить следующее фото.",
            main_keyboard()
        )

    except Exception as exc:
        logger.exception("Image set processing failed")
        note_runtime_error(exc)
        with state_lock:
            runtime_stats["images_failed"] += 1
        record_event(
            chat_id, "processing_failed",
            f"style={style_id};count={card_count};{type(exc).__name__}"
        )
        if credit_type:
            try:
                refund_credit(chat_id, credit_type)
            except Exception:
                logger.exception("Credit refund failed")

        message = (
            "😕 Не удалось полностью подготовить комплект. "
            "Кредит возвращён, если база данных доступна.\n\n"
            "Попробуй ещё раз позже. Если ошибка повторяется, сообщи администратору."
        )
        if "больше" in str(exc).lower() or "маленькое" in str(exc).lower():
            message = f"⚠️ {str(exc)}"
        send_message(chat_id, message, main_keyboard())

    finally:
        with state_lock:
            busy_chats.discard(chat_id)


def start_processing(chat_id, file_id, prompt, style_id="minimal", card_count=1):
    prompt = clean_user_text(prompt) or (
        "Создай качественные карточки товара, сохранив товар без изменений."
    )
    with state_lock:
        if chat_id in busy_chats:
            send_message(chat_id, "⏳ Предыдущий комплект ещё обрабатывается. Дождись результата.")
            return
        busy_chats.add(chat_id)
    threading.Thread(
        target=process_photo,
        args=(chat_id, file_id, prompt, style_id, card_count),
        daemon=True,
        name=f"image-set-{chat_id}"
    ).start()


def help_text():
    return (
        "❓ Как пользоваться TovarPix\n\n"
        "1. Отправь фотографию товара.\n"
        "2. Выбери один из 10 стилей.\n"
        "3. Выбери от 1 до 7 карточек.\n"
        "4. Напиши пожелания к фону, свету или реальные характеристики товара.\n\n"
        "Один полный комплект из 1–7 карточек стоит 1 кредит.\n\n"
        "Примеры:\n"
        "• Белый фон, мягкая тень, как для маркетплейса.\n"
        "• Премиальная тёмная студия, товар не менять.\n"
        "• Рекламная карточка с заголовком и плашками.\n\n"
        "Не забудь указать реальные характеристики, если они нужны на карточке."
    )


def features_text():
    return (
        "📋 Возможности TovarPix\n\n"
        "📸 Улучшение товарных фотографий\n"
        "🎨 10 визуальных стилей\n"
        "🧩 Комплекты от 1 до 7 карточек\n"
        "🛍️ Рекламные карточки с заголовками и плашками\n"
        "🔤 Текст рисуется отдельно для лучшей читаемости\n"
        "💳 Оплата комплектов через Telegram Stars\n\n"
        "Важно: нейросеть может ошибаться в мелких деталях товара. "
        "Проверяй результат перед публикацией."
    )


def admin_text(chat_id):
    if not is_admin(chat_id):
        return "Команда доступна только администратору."
    try:
        stats = get_admin_stats()
        with state_lock:
            runtime = dict(runtime_stats)
            busy_count = len(busy_chats)
        event_lines = "\n".join(
            f"• {name}: {count}"
            for name, count in sorted(stats["events_7d"].items())
        ) or "Нет событий"
        return (
            "👑 Статистика TovarPix\n\n"
            f"Пользователей: {stats['users']}\n"
            f"Остаток купленных комплектов: {stats['credits_remaining']}\n"
            f"Платежей: {stats['payment_count']}\n"
            f"Получено Stars: {stats['stars_total']}\n"
            f"Продано комплектов: {stats['credits_sold']}\n\n"
            "События за 7 дней:\n" + event_lines + "\n\n"
            f"Сейчас обрабатывается: {busy_count}\n"
            f"Успешных комплектов: {runtime['images_succeeded']}\n"
            f"Ошибок: {runtime['images_failed']}\n"
            f"Последний успех: {runtime['last_success_at'] or 'ещё не было'}"
        )
    except Exception:
        logger.exception("Could not build admin stats")
        return "Не удалось загрузить статистику."


def handle_command_or_text(chat_id, message, text):
    command = text.split("@")[0].split()[0] if text else ""

    if command == "/start":
        try:
            reset_keyboard(chat_id)
        except Exception:
            logger.exception("Could not remove previous keyboard")
        send_message(
            chat_id,
            "Привет! 👋 Я TovarPix — AI-редактор фото товаров.\n\n"
            "📸 Отправь фотографию товара.\n"
            "🎨 Выбери один из 10 стилей и количество карточек от 1 до 7.\n"
            "✍️ Затем напиши пожелания или характеристики товара.\n\n"
            "Один полный комплект из 1–7 карточек стоит 1 кредит.\n\n"
            "🎁 Доступен один бесплатный комплект.\n"
            "⭐ Дополнительно: 5 комплектов за 5 Telegram Stars.\n\n"
            "Начни с простого: «Сделай премиальный тёмный фон, мягкий "
            "студийный свет и реалистичную тень. Сам товар не меняй».",
            main_keyboard()
        )
        return

    if command == "/myid":
        send_message(
            chat_id,
            f"Твой Telegram ID: {chat_id}\n"
            "Добавь его в Render в переменную ADMIN_IDS, чтобы получить безлимит."
        )
        return

    if command == "/help" or text == "❓ Помощь":
        send_message(chat_id, help_text(), main_keyboard())
        return

    if command == "/features" or text == "📋 Возможности":
        send_message(chat_id, features_text(), main_keyboard())
        return

    if command == "/balance" or text == "💰 Баланс":
        try:
            send_message(chat_id, user_balance_text(get_balance(chat_id)), main_keyboard())
        except Exception:
            logger.exception("Could not read balance")
            send_message(chat_id, "Не удалось загрузить баланс. Попробуй позже.")
        return

    if command == "/buy" or text == "⭐ Купить 5 комплектов":
        try:
            send_invoice(chat_id)
        except Exception:
            logger.exception("Could not send invoice")
            send_message(chat_id, "Не удалось создать счёт. Попробуй позже.")
        return

    if command == "/cancel" or text == "❌ Отмена":
        with state_lock:
            pending_photos.pop(chat_id, None)
            pending_jobs.pop(chat_id, None)
        send_message(
            chat_id,
            "Ожидание отменено. Можешь отправить новое фото.",
            main_keyboard()
        )
        return

    if command == "/example" or text == "💡 Пример промпта":
        send_message(
            chat_id,
            "💡 Примеры описаний:\n\n"
            "• Белый фон и мягкие реалистичные тени.\n"
            "• Премиальная тёмная студия с красивым освещением.\n"
            "• Светлый интерьер и естественный дневной свет.\n"
            "• Минималистичный фон для маркетплейса.\n"
            "• Рекламная карточка с заголовком и плашками.\n\n"
            "Для рекламной карточки укажи реальные характеристики товара. "
            "Я не должен выдумывать свойства, скидки или цены.",
            main_keyboard()
        )
        return

    if command == "/admin":
        send_message(chat_id, admin_text(chat_id), main_keyboard())
        return

    if command == "/privacy":
        send_message(
            chat_id,
            "🔒 Конфиденциальность\n\n"
            "Фото отправляется внешнему сервису обработки изображений для "
            "выполнения твоего запроса. Не отправляй документы, личные фото "
            "или изображения, которые не имеешь права обрабатывать. "
            "Не используй результат как подтверждение характеристик товара.",
            main_keyboard()
        )
        return

    if command == "/status":
        try:
            balance = get_balance(chat_id)
            send_message(
                chat_id,
                "📊 Статус\n\n" + user_balance_text(balance) +
                f"\n\n🕒 Время работы сервиса: {format_duration(time.time() - APP_STARTED_AT)}",
                main_keyboard()
            )
        except Exception:
            logger.exception("Status command failed")
            send_message(chat_id, "Статус временно недоступен.")
        return

    send_message(chat_id, "Не понял команду. Нажми «❓ Помощь».", main_keyboard())


def handle_callback_query(callback):
    callback_id = callback.get("id")
    data = str(callback.get("data", ""))
    chat_id = callback.get("message", {}).get("chat", {}).get("id")

    if not chat_id:
        answer_callback_query(callback_id, "Не удалось определить чат")
        return

    with state_lock:
        file_id = pending_photos.get(chat_id)
        job = pending_jobs.setdefault(
            chat_id,
            {"stage": "style", "style_id": None, "card_count": None}
        )

    if not file_id:
        answer_callback_query(callback_id, "Сначала отправь фото товара")
        send_message(chat_id, "Сначала отправь фотографию товара.", main_keyboard())
        return

    if data.startswith("style:"):
        style_id = data.split(":", 1)[1]
        if style_id not in STYLE_OPTIONS:
            answer_callback_query(callback_id, "Неизвестный стиль")
            return

        with state_lock:
            job["style_id"] = style_id
            job["stage"] = "count"
            pending_jobs[chat_id] = job

        answer_callback_query(
            callback_id, f"Выбран стиль: {STYLE_OPTIONS[style_id]['name']}"
        )
        send_message(
            chat_id,
            f"Выбран стиль: {STYLE_OPTIONS[style_id]['emoji']} "
            f"{STYLE_OPTIONS[style_id]['name']}\n\n"
            "Сколько карточек сделать? Весь комплект стоит 1 кредит.",
            count_keyboard()
        )
        return

    if data.startswith("count:"):
        try:
            count = int(data.split(":", 1)[1])
        except ValueError:
            count = 0

        if count < 1 or count > 7:
            answer_callback_query(callback_id, "Можно выбрать от 1 до 7")
            return

        with state_lock:
            if not job.get("style_id"):
                job["stage"] = "style"
                pending_jobs[chat_id] = job
                answer_callback_query(callback_id, "Сначала выбери стиль")
                send_message(chat_id, "Сначала выбери стиль карточек.", style_keyboard())
                return
            job["card_count"] = count
            job["stage"] = "prompt"
            pending_jobs[chat_id] = job

        answer_callback_query(callback_id, f"Выбрано карточек: {count}")
        send_message(
            chat_id,
            f"📦 Комплект: {count} карточек.\n"
            f"🎨 Стиль: {STYLE_OPTIONS[job['style_id']]['name']}\n"
            "💳 Стоимость всего комплекта — 1 кредит.\n\n"
            "Теперь напиши пожелания к фону/свету или реальные характеристики товара. "
            "Если особых пожеланий нет, напиши: «Стандартно».\n\n"
            "Для отмены нажми «❌ Отмена».",
            main_keyboard()
        )
        return

    answer_callback_query(callback_id, "Кнопка устарела. Отправь фото заново.")


def handle_update(update):
    with state_lock:
        runtime_stats["updates"] += 1

    if update.get("pre_checkout_query"):
        handle_pre_checkout(update["pre_checkout_query"])
        return

    if update.get("callback_query"):
        handle_callback_query(update["callback_query"])
        return

    message = update.get("message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    if message.get("successful_payment"):
        handle_successful_payment(message)
        return

    if not allow_message_rate(chat_id):
        send_message(chat_id, "⏳ Слишком много сообщений подряд. Подожди немного.")
        return

    text = (message.get("text") or "").strip()

    if message.get("photo"):
        with state_lock:
            if chat_id in busy_chats:
                send_message(chat_id, "⏳ Дождись завершения предыдущей обработки.")
                return
            pending_photos[chat_id] = message["photo"][-1]["file_id"]
            pending_jobs[chat_id] = {
                "stage": "style", "style_id": None, "card_count": None
            }

        send_message(
            chat_id,
            "📸 Фото получил!\n\n"
            "Выбери стиль оформления карточек. Потом укажешь количество от 1 до 7 и пожелания.\n"
            "Весь комплект стоит 1 кредит.",
            style_keyboard()
        )
        return

    if text and not text.startswith("/"):
        with state_lock:
            file_id = pending_photos.get(chat_id)
            job = pending_jobs.get(chat_id, {})
            ready = bool(
                file_id
                and job.get("stage") == "prompt"
                and job.get("style_id")
                and job.get("card_count")
            )
            if ready:
                pending_photos.pop(chat_id, None)
                pending_jobs.pop(chat_id, None)

        if ready:
            prompt = (
                ""
                if text.lower() in ("стандартно", "без пожеланий", "по умолчанию")
                else text
            )
            start_processing(
                chat_id, file_id, prompt,
                job["style_id"], job["card_count"]
            )
        elif file_id:
            stage = job.get("stage", "style")
            if stage == "style":
                send_message(chat_id, "Сначала выбери стиль кнопкой ниже.", style_keyboard())
            elif stage == "count":
                send_message(chat_id, "Сначала выбери количество карточек от 1 до 7.", count_keyboard())
            else:
                send_message(
                    chat_id,
                    "Выбери стиль и количество карточек, затем напиши пожелания."
                )
        else:
            handle_command_or_text(chat_id, message, text)
        return

    if text.startswith("/"):
        handle_command_or_text(chat_id, message, text)
        return

    send_message(chat_id, "Отправь фотографию товара, чтобы начать.", main_keyboard())


@app.route("/", methods=["GET"])
def home():
    return "TovarPix AI is running!", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "tovarpix",
        "uptime_seconds": int(time.time() - APP_STARTED_AT),
        "timestamp": utc_now_iso()
    }), 200


@app.route("/ready", methods=["GET"])
def ready():
    try:
        with db_connect() as conn:
            conn.execute("SELECT 1")
        return jsonify({"ready": True, "database": "ok"}), 200
    except Exception:
        logger.exception("Readiness check failed")
        return jsonify({"ready": False, "database": "error"}), 503


@app.route("/bot_info", methods=["GET"])
def bot_info():
    try:
        me = telegram_api("getMe")
        return jsonify({
            "username": me.get("username"),
            "first_name": me.get("first_name"),
            "id": me.get("id")
        }), 200
    except Exception:
        logger.exception("Bot info failed")
        return jsonify({"error": "Telegram API error"}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            logger.warning("Rejected webhook request with invalid secret")
            return "Forbidden", 403

    if not request.is_json:
        return "Expected JSON", 415

    try:
        payload = request.get_json(silent=False)
        if not isinstance(payload, dict):
            return "Expected JSON object", 400
        handle_update(payload)
        return "OK", 200
    except Exception:
        logger.exception("Webhook handler failed")
        return "Internal Server Error", 500


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    if not RENDER_URL:
        return "Render URL is not configured", 500
    data = {"url": f"{RENDER_URL}/webhook"}
    if WEBHOOK_SECRET:
        data["secret_token"] = WEBHOOK_SECRET
    try:
        telegram_api("setWebhook", data)
        return "Webhook configured", 200
    except Exception:
        logger.exception("Webhook setup failed")
        return "Webhook setup error", 500


@app.route("/webhook_status", methods=["GET"])
def webhook_status():
    try:
        info = telegram_api("getWebhookInfo")
        return jsonify({
            "url_configured": bool(info.get("url")),
            "url_matches_render": info.get("url") == f"{RENDER_URL}/webhook",
            "pending_update_count": info.get("pending_update_count"),
            "last_error_date": info.get("last_error_date"),
            "last_error_message": info.get("last_error_message"),
            "max_connections": info.get("max_connections")
        }), 200
    except Exception:
        logger.exception("Webhook status failed")
        return jsonify({"error": "Telegram API error"}), 500


@app.route("/runtime_stats", methods=["GET"])
def runtime_stats_route():
    with state_lock:
        stats = dict(runtime_stats)
        stats["busy_count"] = len(busy_chats)
    stats.pop("last_error", None)
    return jsonify({
        "uptime_seconds": int(time.time() - APP_STARTED_AT),
        "stats": stats
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
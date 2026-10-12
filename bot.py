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


# ============================================================
# TOVARPIX — AI-редактор фотографий товаров
# ============================================================

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

MAX_IMAGE_BYTES = int(os.environ.get(
    "MAX_IMAGE_BYTES", str(20 * 1024 * 1024)
))
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
http = requests.Session()
http.headers.update({"User-Agent": "TovarPix/2.0"})
magic_client = Client(token=MAGIC_HOUR_API_KEY)

pending_photos = {}
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


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_admin(chat_id):
    return str(chat_id) in ADMIN_IDS


def clean_user_text(value, limit=MAX_PROMPT_LENGTH):
    value = str(value or "").replace("\x00", " ").strip()
    value = " ".join(value.split())
    return value[:limit]


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

    free_text = (
        "доступна"
        if not balance["free_used"]
        else "уже использована"
    )

    return (
        f"🎁 Бесплатная обработка: {free_text}.\n"
        f"⭐ Купленных обработок: {balance['credits']}."
    )


# ============================================================
# DATABASE
# ============================================================

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
                INSERT INTO processing_events
                    (telegram_id, event_type, detail)
                VALUES (%s, %s, %s)
                """,
                (
                    int(chat_id),
                    str(event_type)[:40],
                    str(detail)[:300]
                )
            )
    except Exception:
        logger.exception("Could not record processing event")


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
            """
            SELECT free_used, credits
            FROM users
            WHERE telegram_id = %s
            """,
            (chat_id,)
        ).fetchone()

    return {
        "free_used": row[0],
        "credits": row[1],
        "admin": is_admin(chat_id)
    }


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
            SELECT free_used, credits
            FROM users
            WHERE telegram_id = %s
            FOR UPDATE
            """,
            (chat_id,)
        ).fetchone()

        free_used, credits = row

        if not free_used:
            conn.execute(
                """
                UPDATE users
                SET free_used = TRUE
                WHERE telegram_id = %s
                """,
                (chat_id,)
            )
            return "free"

        if credits > 0:
            conn.execute(
                """
                UPDATE users
                SET credits = credits - 1
                WHERE telegram_id = %s
                """,
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
                """
                UPDATE users
                SET free_used = FALSE
                WHERE telegram_id = %s
                """,
                (chat_id,)
            )

        elif credit_type == "paid":
            conn.execute(
                """
                UPDATE users
                SET credits = credits + 1
                WHERE telegram_id = %s
                """,
                (chat_id,)
            )


def get_admin_stats():
    with db_connect() as conn:
        users = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(credits), 0)
            FROM users
            """
        ).fetchone()

        payments = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(stars), 0),
                   COALESCE(SUM(credits), 0)
            FROM payments
            """
        ).fetchone()

        events = conn.execute(
            """
            SELECT event_type, COUNT(*)
            FROM processing_events
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


init_db()


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_api(method, data=None, timeout=35):
    response = http.post(
        f"{API_URL}/{method}",
        json=data or {},
        timeout=timeout
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Telegram API error")
        )

    return result.get("result")


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    data = {
        "chat_id": chat_id,
        "text": str(text)[:4096]
    }

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
            [{"text": "⭐ Купить 5 обработок"}],
            [
                {"text": "💡 Пример промпта"},
                {"text": "💰 Баланс"}
            ],
            [
                {"text": "❓ Помощь"},
                {"text": "📋 Возможности"}
            ],
            [{"text": "❌ Отмена"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


def reset_keyboard(chat_id):
    telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Обновляю меню...",
            "reply_markup": {"remove_keyboard": True}
        }
    )


# ============================================================
# TELEGRAM STARS PAYMENTS
# ============================================================

def send_invoice(chat_id):
    payload = f"tovarpix_pack5_{chat_id}_{uuid.uuid4().hex}"

    telegram_api("sendInvoice", {
        "chat_id": chat_id,
        "title": "TovarPix — 5 обработок",
        "description": "Пять дополнительных обработок фотографий товаров",
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": [
            {"label": "5 обработок", "amount": PACKAGE_STARS}
        ]
    })


def answer_pre_checkout(query_id, ok, error_message=None):
    data = {
        "pre_checkout_query_id": query_id,
        "ok": ok
    }

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
            query["id"],
            valid,
            None if valid else
            "Не удалось проверить заказ. Попробуй снова."
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
        not chat_id
        or not charge_id
        or not payload.startswith(expected_prefix)
        or currency != "XTR"
        or amount != PACKAGE_STARS
    ):
        logger.error("Payment validation failed")

        if chat_id:
            send_message(
                chat_id,
                "Не удалось автоматически проверить платёж. "
                "Обратись в поддержку."
            )

        return

    try:
        with db_connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO payments
                    (charge_id, telegram_id, credits, stars)
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
                    DO UPDATE SET
                        credits = users.credits + EXCLUDED.credits
                    """,
                    (chat_id, PACKAGE_IMAGES)
                )

        if inserted:
            record_event(
                chat_id,
                "payment",
                f"{amount} Stars; {PACKAGE_IMAGES} credits"
            )

            send_message(
                chat_id,
                "✅ Оплата получена!\n\n"
                f"Начислено: {PACKAGE_IMAGES} обработок.\n"
                "Отправь фотографию товара, чтобы начать.",
                main_keyboard()
            )
        else:
            send_message(chat_id, "Этот платёж уже был учтён.")

    except Exception:
        logger.exception("Could not credit successful payment")
        send_message(
            chat_id,
            "Платёж получен Telegram, но не удалось начислить обработки. "
            "Не оплачивай повторно — обратись в поддержку."
        )


# ============================================================
# IMAGE DOWNLOAD AND VALIDATION
# ============================================================

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


def normalize_image_bytes(
    image_bytes,
    max_side=MAX_IMAGE_SIDE,
    quality=94
):
    """Apply EXIF rotation and normalize image to a compatible JPEG."""
    with Image.open(io.BytesIO(image_bytes)) as original:
        image = ImageOps.exif_transpose(original)

        if image.width > max_side or image.height > max_side:
            image.thumbnail(
                (max_side, max_side),
                Image.Resampling.LANCZOS
            )

        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image.convert("RGBA"))
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")

        output = io.BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=quality,
            optimize=True
        )

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


# ============================================================
# QWEN IMAGE EDITOR
# ============================================================

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
environment. Add realistic contact shadows so the product feels grounded,
not cut out and pasted onto a flat background.
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
The result should look like a polished real commercial product photograph,
not a rough cutout pasted onto a background.
"""


def generate_product_image(image_bytes, user_prompt):
    prompt = (
        BASE_PROMPT
        + "\nUSER REQUEST:\n"
        + clean_user_text(user_prompt)
    )

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
                raise RuntimeError(
                    f"Magic Hour: {error.code}: {error.message}"
                )

            raise RuntimeError(
                f"Generation status: {result.status}"
            )

        paths = result.downloaded_paths

        if not paths or not Path(str(paths[0])).is_file():
            raise RuntimeError("Generated image file not found")

        output_bytes = Path(str(paths[0])).read_bytes()

        normalized_output = normalize_image_bytes(
            output_bytes,
            max_side=3000,
            quality=94
        )

        return io.BytesIO(normalized_output)


# ============================================================
# GEMINI — AD TEXT GENERATION
# ============================================================

def get_ad_copy(image_bytes, user_prompt):
    """Generate validated advertising copy only when requested."""
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not configured")
        return None

    normalized = normalize_image_bytes(
        image_bytes,
        max_side=1000,
        quality=80
    )

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
1. make_ad_card=true только если пользователь просит рекламу,
   рекламную карточку, баннер, продающий дизайн, текст на фото,
   плашки или характеристики товара.
2. Если запрос только про фон, свет, поворот или ретушь — false.
3. Не придумывай цену, скидку, гарантию, технические параметры,
   комплект поставки или другие факты.
4. В features используй только характеристики, явно указанные
   пользователем. Если их нет, верни пустой список.
5. Если данных мало, используй нейтральный заголовок.
6. title до 34 символов; subtitle до 55; badge до 18.
7. features — максимум 3 пункта, каждый до 32 символов.
8. Пиши по-русски, без эмодзи в заголовке.
9. Не выдавай догадки о товаре за факты.
10. accent — только #D9FF43, #FFFFFF, #65D6FF, #FFB86B или #FF7A90.
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
        (
            part.get("text", "")
            for part in parts
            if part.get("text")
        ),
        ""
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
        "#D9FF43",
        "#FFFFFF",
        "#65D6FF",
        "#FFB86B",
        "#FF7A90"
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
            if isinstance(item, str)
            and clean_user_text(item, 32)
        ][:3],
        "accent": accent,
    }


# ============================================================
# PILLOW — DESIGN AND TYPOGRAPHY
# ============================================================

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
            lines[-1] = fit_text(
                draw,
                lines[-1],
                font,
                max_width
            )

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
            int(
                top[channel] * (1 - ratio)
                + bottom[channel] * ratio
            )
            for channel in range(3)
        )

        draw.line(
            (x0, y0 + offset, x1, y0 + offset),
            fill=color
        )


def render_ad_card(image_bytes, ad):
    """
    Create a premium dark product card.
    The original product image is not cropped to fill the frame.
    Advertising text is rendered locally using Pillow.
    """
    normalized = normalize_image_bytes(
        image_bytes,
        max_side=1200,
        quality=93
    )

    with Image.open(io.BytesIO(normalized)) as source:
        product_image = source.convert("RGB")

    width = 1080
    margin = 58
    image_area_height = 880
    footer_height = 430
    total_height = image_area_height + footer_height

    product_image.thumbnail(
        (
            width - 2 * margin,
            image_area_height - 2 * margin
        ),
        Image.Resampling.LANCZOS
    )

    canvas = Image.new(
        "RGB",
        (width, total_height),
        "#101218"
    )

    draw_gradient(
        canvas,
        (0, 0, width, image_area_height),
        "#292D36",
        "#161920"
    )

    px = (width - product_image.width) // 2
    py = (image_area_height - product_image.height) // 2

    canvas.paste(product_image, (px, py))

    draw = ImageDraw.Draw(canvas)

    draw.line(
        (
            margin,
            image_area_height,
            width - margin,
            image_area_height
        ),
        fill="#383D47",
        width=2
    )

    footer_top = image_area_height

    draw_gradient(
        canvas,
        (0, footer_top, width, total_height),
        "#171A21",
        "#0E1015"
    )

    draw = ImageDraw.Draw(canvas)

    accent = ad.get("accent", "#D9FF43")
    accent_rgb = hex_to_rgb(accent)

    x = margin
    y = footer_top + 38
    content_width = width - margin * 2

    overline_font = load_font(19, bold=True)

    draw.text(
        (x, y),
        "TOVARPIX  /  PRODUCT EDITION",
        font=overline_font,
        fill=accent_rgb
    )

    y += 42

    badge = clean_user_text(ad.get("badge", ""), 18)

    if badge:
        badge_font = load_font(22, bold=True)

        badge_text = fit_text(
            draw,
            badge,
            badge_font,
            content_width - 42
        )

        badge_width = min(
            content_width,
            text_width(draw, badge_text, badge_font) + 42
        )

        badge_height = 48

        draw.rounded_rectangle(
            (
                x,
                y,
                x + badge_width,
                y + badge_height
            ),
            radius=15,
            fill=accent_rgb
        )

        draw.text(
            (x + 21, y + 9),
            badge_text,
            font=badge_font,
            fill="#111318"
        )

        y += badge_height + 20

    title = clean_user_text(ad.get("title", ""), 34)
    title_font = load_font(47, bold=True)

    title_lines = wrap_text(
        draw,
        title,
        title_font,
        content_width,
        max_lines=2
    )

    for line in title_lines:
        draw.text(
            (x, y),
            line,
            font=title_font,
            fill="#FFFFFF"
        )
        y += 58

    if title_lines:
        y += 2

    subtitle = clean_user_text(ad.get("subtitle", ""), 55)
    subtitle_font = load_font(25)

    for line in wrap_text(
        draw,
        subtitle,
        subtitle_font,
        content_width,
        max_lines=2
    ):
        draw.text(
            (x, y),
            line,
            font=subtitle_font,
            fill="#BFC4CE"
        )
        y += 35

    features = ad.get("features", [])[:3]

    if features:
        y += 17
        feature_font = load_font(22)

        for feature in features:
            label = clean_user_text(feature, 32)

            label = fit_text(
                draw,
                label,
                feature_font,
                content_width - 38
            )

            box_width = min(
                content_width,
                text_width(draw, label, feature_font) + 38
            )

            if y + 38 > total_height - 42:
                break

            draw.rounded_rectangle(
                (
                    x,
                    y,
                    x + box_width,
                    y + 36
                ),
                radius=10,
                outline="#424854",
                width=2
            )

            draw.ellipse(
                (
                    x + 12,
                    y + 13,
                    x + 22,
                    y + 23
                ),
                fill=accent_rgb
            )

            draw.text(
                (x + 31, y + 5),
                label,
                font=feature_font,
                fill="#E8EAF0"
            )

            y += 43

    brand_font = load_font(17, bold=True)
    brand = "PRODUCT PHOTOGRAPHY • TOVARPIX"
    brand_width = text_width(draw, brand, brand_font)

    draw.text(
        (
            width - margin - brand_width,
            total_height - 31
        ),
        brand,
        font=brand_font,
        fill="#777E8A"
    )

    output = io.BytesIO()

    canvas.save(
        output,
        format="JPEG",
        quality=93,
        optimize=True,
        progressive=True
    )

    output.seek(0)
    return output


def add_ad_layout_if_requested(image_bytes, user_prompt):
    """If ad rendering fails, return the processed Qwen image unchanged."""
    try:
        ad = get_ad_copy(image_bytes, user_prompt)

        if not ad:
            logger.info("Ad layout not requested; keeping Qwen image")
            return io.BytesIO(image_bytes)

        result = render_ad_card(image_bytes, ad)

        with state_lock:
            runtime_stats["cards_rendered"] += 1

        logger.info("Gemini ad card rendered successfully")
        return result

    except Exception as exc:
        logger.exception("Gemini ad layout failed; using Qwen image")
        note_runtime_error(exc)
        return io.BytesIO(image_bytes)


# ============================================================
# SEND PHOTO TO TELEGRAM
# ============================================================

def send_generated_photo(chat_id, image_buffer):
    image_buffer.seek(0)

    response = http.post(
        f"{API_URL}/sendPhoto",
        data={
            "chat_id": str(chat_id),
            "caption": "✅ Готово! Вот твоё обработанное фото."
        },
        files={
            "photo": (
                "tovarpix_result.jpg",
                image_buffer,
                "image/jpeg"
            )
        },
        timeout=60
    )

    response.raise_for_status()
    body = response.json()

    if not body.get("ok"):
        raise RuntimeError(
            body.get("description", "Telegram could not send result")
        )


# ============================================================
# PHOTO PROCESSING
# ============================================================

def process_photo(chat_id, file_id, prompt):
    credit_type = None
    started = time.monotonic()

    with state_lock:
        runtime_stats["images_started"] += 1

    try:
        credit_type = reserve_credit(chat_id)

        if credit_type is None:
            send_message(
                chat_id,
                "🎁 Бесплатная обработка уже использована, "
                "а купленные обработки закончились.\n\n"
                "Купи пакет из 5 обработок за 5 ⭐, чтобы продолжить.",
                main_keyboard()
            )
            return

        prompt = clean_user_text(prompt)

        if not prompt:
            raise RuntimeError("Пустое описание обработки")

        record_event(
            chat_id,
            "processing_started",
            prompt[:180]
        )

        send_message(
            chat_id,
            "🎨 Принял описание! Обрабатываю фотографию.\n"
            "Это может занять несколько минут."
        )

        image_bytes = download_telegram_photo(file_id)

        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise RuntimeError("Фото больше допустимого размера")

        image_info = inspect_image(image_bytes)

        logger.info(
            "Image accepted: chat=%s format=%s size=%sx%s bytes=%s",
            chat_id,
            image_info["format"],
            image_info["width"],
            image_info["height"],
            len(image_bytes)
        )

        result = generate_product_image(image_bytes, prompt)
        qwen_bytes = result.getvalue()

        final_result = add_ad_layout_if_requested(
            qwen_bytes,
            prompt
        )

        send_generated_photo(chat_id, final_result)

        with state_lock:
            runtime_stats["images_succeeded"] += 1
            runtime_stats["last_success_at"] = utc_now_iso()

        record_event(
            chat_id,
            "processing_succeeded",
            f"duration={int(time.monotonic() - started)}s"
        )

        balance = get_balance(chat_id)

        if balance["admin"]:
            balance_text = "👑 У тебя безлимит."
        else:
            balance_text = (
                "⭐ Купленных обработок осталось: "
                f"{balance['credits']}."
            )

        send_message(
            chat_id,
            balance_text + "\n\nМожешь отправить следующее фото.",
            main_keyboard()
        )

    except Exception as exc:
        logger.exception("Image processing failed")
        note_runtime_error(exc)

        with state_lock:
            runtime_stats["images_failed"] += 1

        record_event(
            chat_id,
            "processing_failed",
            type(exc).__name__
        )

        if credit_type:
            try:
                refund_credit(chat_id, credit_type)
            except Exception:
                logger.exception("Credit refund failed")

        message = (
            "😕 Не удалось обработать фото. Списание будет возвращено, "
            "если база данных доступна.\n\n"
            "Попробуй ещё раз позже. Если ошибка повторяется, "
            "сообщи администратору."
        )

        if (
            "больше" in str(exc).lower()
            or "маленькое" in str(exc).lower()
        ):
            message = f"⚠️ {str(exc)}"

        send_message(
            chat_id,
            message,
            main_keyboard()
        )

    finally:
        with state_lock:
            busy_chats.discard(chat_id)


def start_processing(chat_id, file_id, prompt):
    prompt = clean_user_text(prompt)

    if not prompt:
        send_message(
            chat_id,
            "Напиши, как обработать фото.",
            main_keyboard()
        )
        return

    with state_lock:
        if chat_id in busy_chats:
            send_message(
                chat_id,
                "⏳ Предыдущая фотография ещё обрабатывается. "
                "Дождись результата."
            )
            return

        busy_chats.add(chat_id)

    threading.Thread(
        target=process_photo,
        args=(chat_id, file_id, prompt),
        daemon=True,
        name=f"image-{chat_id}"
    ).start()


# ============================================================
# HELP, FEATURES AND ADMIN
# ============================================================

def help_text():
    return (
        "❓ Как пользоваться TovarPix\n\n"
        "1. Отправь фотографию товара.\n"
        "2. Напиши, что изменить: фон, свет, композицию или атмосферу.\n"
        "3. Для рекламного дизайна отдельно попроси карточку с заголовком "
        "и плашками.\n\n"
        "Примеры:\n"
        "• «Белый фон, мягкая тень, как для маркетплейса»\n"
        "• «Премиальная тёмная студия, товар не менять»\n"
        "• «Сделай рекламную карточку. Характеристики: матовый чехол, "
        "защита углов»\n\n"
        "Совет: если важны мелкие детали, прямо укажи, что их нельзя менять."
    )


def features_text():
    return (
        "📋 Возможности TovarPix\n\n"
        "📸 Улучшение товарных фотографий\n"
        "💡 Студийное освещение и реалистичные тени\n"
        "🧱 Замена или улучшение фона\n"
        "🛍️ Рекламные карточки с текстом, плашками и характеристиками\n"
        "🔤 Текст рекламной карточки рисуется отдельно, чтобы он был чётким\n"
        "💳 Оплата обработок через Telegram Stars\n\n"
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

        events = stats["events_7d"]

        event_lines = "\n".join(
            f"• {name}: {count}"
            for name, count in sorted(events.items())
        ) or "• Пока нет событий"

        last_error = runtime.get("last_error") or {}

        return (
            "🛠 TovarPix — админ-статистика\n\n"
            f"👥 Пользователей: {stats['users']}\n"
            f"⭐ Продано обработок: {stats['credits_sold']}\n"
            f"💫 Получено Stars: {stats['stars_total']}\n"
            f"🧾 Платежей: {stats['payment_count']}\n"
            f"🎟 Остаток кредитов всех пользователей: "
            f"{stats['credits_remaining']}\n\n"
            "📈 За последние 7 дней:\n"
            f"{event_lines}\n\n"
            "⚙️ Текущий процесс:\n"
            f"• Время работы: "
            f"{format_duration(time.time() - APP_STARTED_AT)}\n"
            f"• Обработок запущено: {runtime['images_started']}\n"
            f"• Успешно: {runtime['images_succeeded']}\n"
            f"• Ошибок: {runtime['images_failed']}\n"
            f"• Рекламных карточек: {runtime['cards_rendered']}\n"
            f"• Сейчас обрабатывается: {busy_count}\n"
            f"• Последний успешный результат: "
            f"{runtime['last_success_at'] or 'нет'}\n"
            f"• Последняя ошибка: {last_error.get('message', 'нет')}"
        )

    except Exception:
        logger.exception("Could not create admin report")
        return (
            "Не удалось получить статистику. "
            "Проверь подключение к PostgreSQL."
        )


# ============================================================
# COMMAND HANDLER
# ============================================================

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
            "✍️ Затем напиши, как её обработать.\n\n"
            "Я улучшаю фон и освещение, а по отдельному запросу оформляю "
            "рекламную карточку с заголовком и плашками.\n\n"
            "🎁 Доступна одна бесплатная обработка.\n"
            "⭐ Дополнительные: 5 обработок за 5 Telegram Stars.\n\n"
            "Начни с простого: «Сделай премиальный тёмный фон, мягкий "
            "студийный свет и реалистичную тень. Сам товар не меняй».",
            main_keyboard()
        )
        return

    if command == "/myid":
        send_message(
            chat_id,
            f"Твой Telegram ID: {chat_id}\n"
            "Добавь его в Render в переменную ADMIN_IDS, "
            "чтобы получить безлимит."
        )
        return

    if command == "/help" or text == "❓ Помощь":
        send_message(
            chat_id,
            help_text(),
            main_keyboard()
        )
        return

    if command == "/features" or text == "📋 Возможности":
        send_message(
            chat_id,
            features_text(),
            main_keyboard()
        )
        return

    if command == "/balance" or text == "💰 Баланс":
        try:
            send_message(
                chat_id,
                user_balance_text(get_balance(chat_id)),
                main_keyboard()
            )
        except Exception:
            logger.exception("Could not read balance")
            send_message(
                chat_id,
                "Не удалось загрузить баланс. Попробуй позже."
            )
        return

    if command == "/buy" or text == "⭐ Купить 5 обработок":
        try:
            send_invoice(chat_id)
        except Exception:
            logger.exception("Could not send invoice")
            send_message(
                chat_id,
                "Не удалось создать счёт. Попробуй позже."
            )
        return

    if command == "/cancel" or text == "❌ Отмена":
        with state_lock:
            pending_photos.pop(chat_id, None)

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
        send_message(
            chat_id,
            admin_text(chat_id),
            main_keyboard()
        )
        return

    if command == "/privacy":
        send_message(
            chat_id,
            "🔒 Конфиденциальность\n\n"
            "Фото отправляется внешнему сервису обработки изображений "
            "для выполнения твоего запроса. Не отправляй документы, "
            "личные фото или изображения, которые не имеешь права "
            "обрабатывать. Не используй результат как подтверждение "
            "характеристик товара.",
            main_keyboard()
        )
        return

    if command == "/status":
        try:
            balance = get_balance(chat_id)

            send_message(
                chat_id,
                "📊 Статус\n\n"
                + user_balance_text(balance)
                + "\n\n🕒 Время работы сервиса: "
                + format_duration(time.time() - APP_STARTED_AT),
                main_keyboard()
            )

        except Exception:
            logger.exception("Status command failed")
            send_message(
                chat_id,
                "Статус временно недоступен."
            )

        return

    send_message(
        chat_id,
        "Не понял команду. Нажми «❓ Помощь».",
        main_keyboard()
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

def handle_update(update):
    with state_lock:
        runtime_stats["updates"] += 1

    if update.get("pre_checkout_query"):
        handle_pre_checkout(update["pre_checkout_query"])
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
        send_message(
            chat_id,
            "⏳ Слишком много сообщений подряд. Подожди немного."
        )
        return

    text = (message.get("text") or "").strip()

    if message.get("photo"):
        with state_lock:
            if chat_id in busy_chats:
                send_message(
                    chat_id,
                    "⏳ Дождись завершения предыдущей обработки."
                )
                return

            pending_photos[chat_id] = message["photo"][-1]["file_id"]

        send_message(
            chat_id,
            "📸 Фото получил!\n\n"
            "Теперь напиши, что именно нужно сделать с ним.\n"
            "Например: «Сделай белый фон и мягкие тени, "
            "не меняя сам товар».\n\n"
            "Для рекламы напиши: «Сделай продающую карточку "
            "с заголовком и плашками».\n\n"
            "Обработка начнётся после твоего описания.",
            main_keyboard()
        )
        return

    if text and not text.startswith("/"):
        with state_lock:
            file_id = pending_photos.pop(chat_id, None)

        if file_id:
            start_processing(chat_id, file_id, text)
        else:
            handle_command_or_text(chat_id, message, text)

        return

    if text.startswith("/"):
        handle_command_or_text(chat_id, message, text)
        return

    send_message(
        chat_id,
        "Отправь фотографию товара, чтобы начать.",
        main_keyboard()
    )


# ============================================================
# FLASK ROUTES
# ============================================================

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

        return jsonify({
            "ready": True,
            "database": "ok"
        }), 200

    except Exception:
        logger.exception("Readiness check failed")

        return jsonify({
            "ready": False,
            "database": "error"
        }), 503


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
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if supplied != WEBHOOK_SECRET:
            logger.warning(
                "Rejected webhook request with invalid secret"
            )
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
            "url_matches_render": (
                info.get("url") == f"{RENDER_URL}/webhook"
            ),
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
    # Public endpoint deliberately excludes user IDs, balances and secrets.
    with state_lock:
        stats = dict(runtime_stats)
        stats["busy_count"] = len(busy_chats)

    stats.pop("last_error", None)

    return jsonify({
        "uptime_seconds": int(time.time() - APP_STARTED_AT),
        "stats": stats
    }), 200


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT
    )
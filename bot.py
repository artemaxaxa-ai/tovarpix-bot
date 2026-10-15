import os
import io
import uuid
import json
import hmac
import logging
import threading
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque

import requests
import psycopg
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFile, ImageFilter, ImageChops
from magic_hour import Client

# ============================================================
# TOVARPIX CONFIG
# ============================================================

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
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
ADMIN_ENDPOINT_SECRET = os.environ.get("ADMIN_ENDPOINT_SECRET", "").strip()

MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_IMAGE_SIDE = int(os.environ.get("MAX_IMAGE_SIDE", "8000"))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", "50000000"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "30"))
RATE_LIMIT_MESSAGES = int(os.environ.get("RATE_LIMIT_MESSAGES", "12"))

ADMIN_IDS = {
    x.strip()
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip()
}

PACKAGE_CREDITS = 5
PACKAGE_STARS = 5
MAX_REFERENCE_PHOTOS = 6
MAX_PROMPT_LENGTH = 1500
MAX_OVERLAY_TEXT = 400
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
http.headers.update({"User-Agent": "TovarPix/5.0"})
magic_client = Client(token=MAGIC_HOUR_API_KEY)

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False

# ============================================================
# 10 DISTINCT VISUAL STYLES
# ============================================================

STYLE_OPTIONS = {
    "minimal": {
        "name": "Чистый минимализм", "emoji": "▫️",
        "description": "Светлая минималистичная студия, мягкий свет",
        "top": "#F4F4F0", "bottom": "#E3E5E1",
        "panel": "#FFFFFF", "text": "#202421",
        "muted": "#606963", "accent": "#2C7A62",
        "bubble": "#FFFFFF", "border": "#D7DDD7", "kind": "minimal",
    },
    "luxury": {
        "name": "Luxury", "emoji": "✨",
        "description": "Премиальная тёмная студия, золотистый свет",
        "top": "#24211D", "bottom": "#090909",
        "panel": "#151310", "text": "#FFF8E8",
        "muted": "#C7BDA8", "accent": "#D7B56D",
        "bubble": "#171511", "border": "#D7B56D", "kind": "luxury",
    },
    "cozy": {
        "name": "Домашний уют", "emoji": "🏡",
        "description": "Тёплый дневной свет и уютная атмосфера",
        "top": "#F7E9D5", "bottom": "#E9D1B3",
        "panel": "#FFF7EA", "text": "#4A3528",
        "muted": "#876E5B", "accent": "#B56C43",
        "bubble": "#FFF7EA", "border": "#E2C5A5", "kind": "cozy",
    },
    "market": {
        "name": "Яркий маркетинговый", "emoji": "🛍️",
        "description": "Контрастная коммерческая предметная съёмка",
        "top": "#FFF4DF", "bottom": "#FFD7B5",
        "panel": "#FFFFFF", "text": "#20202A",
        "muted": "#5C5B68", "accent": "#F05A28",
        "bubble": "#FFFFFF", "border": "#F05A28", "kind": "market",
    },
    "tech": {
        "name": "Tech", "emoji": "🔷",
        "description": "Графит, холодный свет и техническая геометрия",
        "top": "#172A3A", "bottom": "#080F1A",
        "panel": "#111D2A", "text": "#F2F8FF",
        "muted": "#A9C0D4", "accent": "#65D6FF",
        "bubble": "#111D2A", "border": "#65D6FF", "kind": "tech",
    },
    "eco": {
        "name": "Эко и натуральность", "emoji": "🌿",
        "description": "Природные оттенки и естественное освещение",
        "top": "#E8EBDD", "bottom": "#C8D4B8",
        "panel": "#F7F6E9", "text": "#26392B",
        "muted": "#5E725F", "accent": "#4C8058",
        "bubble": "#F7F6E9", "border": "#91A783", "kind": "eco",
    },
    "fashion": {
        "name": "Fashion Editorial", "emoji": "🖤",
        "description": "Журнальная композиция и выразительная предметная съёмка",
        "top": "#F1F0EE", "bottom": "#D5D3D0",
        "panel": "#FAF9F7", "text": "#151515",
        "muted": "#5A5856", "accent": "#C53A48",
        "bubble": "#FAF9F7", "border": "#151515", "kind": "fashion",
    },
    "info": {
        "name": "Инфографика", "emoji": "📊",
        "description": "Чёткая иерархия, структурированные блоки",
        "top": "#F1F6FC", "bottom": "#DCE8F7",
        "panel": "#FFFFFF", "text": "#172A46",
        "muted": "#526985", "accent": "#2869C7",
        "bubble": "#FFFFFF", "border": "#B8CBE3", "kind": "info",
    },
    "pop": {
        "name": "Pop & Fun", "emoji": "🎨",
        "description": "Яркие цвета, динамичная креативная подача",
        "top": "#F2E8FF", "bottom": "#D8C2FF",
        "panel": "#FFFDF7", "text": "#29213C",
        "muted": "#66577E", "accent": "#7546D8",
        "bubble": "#FFFDF7", "border": "#7546D8", "kind": "pop",
    },
    "cinematic": {
        "name": "Cinematic", "emoji": "🎬",
        "description": "Тёмная кинематографичная студия и глубокие тени",
        "top": "#30343B", "bottom": "#080A0D",
        "panel": "#12151A", "text": "#FFFFFF",
        "muted": "#B5BBC4", "accent": "#FFB86B",
        "bubble": "#12151A", "border": "#FFB86B", "kind": "cinematic",
    },
}

CARD_ROLES = [
    "главная презентация товара",
    "другая композиция товара",
    "детали и фактура товара",
    "сценарий использования",
    "дополнительная рекламная композиция",
    "атмосферная предметная фотография",
    "финальная презентация товара",
]

POSITION_OPTIONS = {
    "bottom": "Снизу",
    "top": "Сверху",
    "left": "Слева",
    "right": "Справа",
}

MENU_TEXTS = {
    "⭐ Купить 5 комплектов",
    "💡 Пример промпта",
    "💰 Баланс",
    "❓ Помощь",
    "📋 Возможности",
    "❌ Отмена",
}

# Занятые чаты и статистика живут в памяти (это нормально: они не критичны
# после перезапуска). Сценарий диалога теперь хранится в PostgreSQL.
busy_chats = set()

state_lock = threading.RLock()
rate_lock = threading.Lock()
message_times = defaultdict(deque)

chat_locks = {}
chat_locks_guard = threading.Lock()

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


def clean_overlay_text(value):
    value = str(value or "").replace("\x00", "")
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())[
        :MAX_OVERLAY_TEXT
    ]


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
    if balance["admin"]:
        return "👑 У тебя безлимит."
    return (
        f"🎁 Бесплатный комплект: "
        f"{'уже использован' if balance['free_used'] else 'доступен'}.\n"
        f"⭐ Купленных комплектов: {balance['credits']}."
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id BIGINT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # НОВОЕ: состояние диалога хранится в базе и переживает перезапуск.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id BIGINT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Уборка старого мусора.
        conn.execute(
            "DELETE FROM processed_updates WHERE created_at < NOW() - INTERVAL '3 days'"
        )
        conn.execute(
            "DELETE FROM sessions WHERE updated_at < NOW() - INTERVAL '2 days'"
        )
    logger.info("PostgreSQL database is ready")


def record_event(chat_id, event_type, detail=""):
    try:
        with db_connect() as conn:
            conn.execute(
                """INSERT INTO processing_events
                   (telegram_id, event_type, detail)
                   VALUES (%s, %s, %s)""",
                (int(chat_id), str(event_type)[:40], str(detail)[:300]),
            )
    except Exception:
        logger.exception("Could not record processing event")


init_db()


# ------------------------------------------------------------
# Сессии диалога (фото + настройки комплекта)
# ------------------------------------------------------------

def new_job(stage):
    return {
        "stage": stage,
        "style_id": None,
        "card_count": None,
        "prompt": "",
        "cards": [],
        "card_index": 0,
    }


def get_chat_lock(chat_id):
    with chat_locks_guard:
        lock = chat_locks.get(chat_id)
        if lock is None:
            lock = threading.RLock()
            chat_locks[chat_id] = lock
        return lock


def load_session(chat_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT data FROM sessions WHERE chat_id = %s", (int(chat_id),)
        ).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Broken session data, resetting")
        return {}


def save_session(chat_id, session):
    with db_connect() as conn:
        if not session:
            conn.execute(
                "DELETE FROM sessions WHERE chat_id = %s", (int(chat_id),)
            )
        else:
            conn.execute(
                """INSERT INTO sessions (chat_id, data, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (chat_id)
                   DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()""",
                (int(chat_id), json.dumps(session, ensure_ascii=False)),
            )


@contextmanager
def session_edit(chat_id):
    """Читает сессию, даёт её менять и сохраняет. Один чат — одна очередь."""
    with get_chat_lock(chat_id):
        session = load_session(chat_id)
        yield session
        save_session(chat_id, session)


def clear_session(chat_id):
    with get_chat_lock(chat_id):
        save_session(chat_id, {})


# ------------------------------------------------------------
# Пользователи и кредиты
# ------------------------------------------------------------

def ensure_user(chat_id):
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO users (telegram_id) VALUES (%s)
               ON CONFLICT (telegram_id) DO NOTHING""",
            (int(chat_id),),
        )


def get_balance(chat_id):
    ensure_user(chat_id)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT free_used, credits FROM users WHERE telegram_id = %s",
            (int(chat_id),),
        ).fetchone()
    return {"free_used": row[0], "credits": row[1], "admin": is_admin(chat_id)}


def reserve_credit(chat_id):
    ensure_user(chat_id)
    if is_admin(chat_id):
        return "admin"

    with db_connect() as conn:
        row = conn.execute(
            """SELECT free_used, credits FROM users
               WHERE telegram_id = %s FOR UPDATE""",
            (int(chat_id),),
        ).fetchone()

        free_used, credits = row
        if not free_used:
            conn.execute(
                "UPDATE users SET free_used = TRUE WHERE telegram_id = %s",
                (int(chat_id),),
            )
            return "free"

        if credits > 0:
            conn.execute(
                "UPDATE users SET credits = credits - 1 WHERE telegram_id = %s",
                (int(chat_id),),
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
                (int(chat_id),),
            )
        elif credit_type == "paid":
            conn.execute(
                "UPDATE users SET credits = credits + 1 WHERE telegram_id = %s",
                (int(chat_id),),
            )


def get_admin_stats():
    with db_connect() as conn:
        users = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(credits), 0) FROM users"
        ).fetchone()
        payments = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(stars), 0),
                      COALESCE(SUM(credits), 0) FROM payments"""
        ).fetchone()
        events = conn.execute(
            """SELECT event_type, COUNT(*) FROM processing_events
               WHERE created_at > NOW() - INTERVAL '7 days'
               GROUP BY event_type"""
        ).fetchall()
    return {
        "users": users[0],
        "credits_remaining": users[1],
        "payment_count": payments[0],
        "stars_total": payments[1],
        "credits_sold": payments[2],
        "events_7d": dict(events),
    }


# ============================================================
# TELEGRAM API
# ============================================================

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
            [{"text": "❌ Отмена"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def photo_collection_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "✅ Фотографии загружены", "callback_data": "photos:done"}],
            [{"text": "ℹ️ Какой ракурс прислать?", "callback_data": "photos:help"}],
        ]
    }


def style_keyboard():
    items = list(STYLE_OPTIONS.items())
    rows = []
    for index in range(0, len(items), 2):
        rows.append([
            {
                "text": f"{style['emoji']} {style['name']}",
                "callback_data": f"style:{style_id}",
            }
            for style_id, style in items[index:index + 2]
        ])
    return {"inline_keyboard": rows}


def count_keyboard():
    return {
        "inline_keyboard": [
            [{"text": str(n), "callback_data": f"count:{n}"} for n in (1, 2, 3, 4)],
            [{"text": str(n), "callback_data": f"count:{n}"} for n in (5, 6, 7)],
        ]
    }


def position_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "⬆️ Сверху", "callback_data": "position:top"},
                {"text": "⬇️ Снизу", "callback_data": "position:bottom"},
            ],
            [
                {"text": "⬅️ Слева", "callback_data": "position:left"},
                {"text": "➡️ Справа", "callback_data": "position:right"},
            ],
            [{"text": "⏭️ Без текста", "callback_data": "position:none"}],
        ]
    }


def answer_callback_query(callback_id, text=None):
    try:
        data = {"callback_query_id": callback_id}
        if text:
            data["text"] = text[:180]
        telegram_api("answerCallbackQuery", data)
    except Exception:
        logger.exception("Could not answer callback query")


def reset_keyboard(chat_id):
    telegram_api("sendMessage", {
        "chat_id": chat_id,
        "text": "Обновляю меню...",
        "reply_markup": {"remove_keyboard": True},
    })


# ============================================================
# TELEGRAM STARS
# ============================================================

def send_invoice(chat_id):
    payload = f"tovarpix_pack5_{chat_id}_{uuid.uuid4().hex}"
    telegram_api("sendInvoice", {
        "chat_id": chat_id,
        "title": "TovarPix — 5 комплектов карточек",
        "description": (
            "Пять комплектов карточек товаров. "
            "Каждый комплект содержит от 1 до 7 карточек."
        ),
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": "5 комплектов", "amount": PACKAGE_STARS}],
    })


def answer_pre_checkout(query_id, ok, error_message=None):
    data = {"pre_checkout_query_id": query_id, "ok": ok}
    if error_message:
        data["error_message"] = error_message
    telegram_api("answerPreCheckoutQuery", data)


def handle_pre_checkout(query):
    try:
        sender_id = query.get("from", {}).get("id")
        payload = query.get("invoice_payload", "")
        valid = (
            bool(sender_id)
            and payload.startswith(f"tovarpix_pack5_{sender_id}_")
            and query.get("currency") == "XTR"
            and query.get("total_amount") == PACKAGE_STARS
        )
        answer_pre_checkout(
            query["id"], valid,
            None if valid else "Не удалось проверить заказ. Попробуй снова.",
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

    if (
        not chat_id or not charge_id
        or not payload.startswith(f"tovarpix_pack5_{chat_id}_")
        or currency != "XTR"
        or amount != PACKAGE_STARS
    ):
        logger.error("Payment validation failed")
        if chat_id:
            send_message(chat_id, "Не удалось проверить платёж. Обратись в поддержку.")
        return

    try:
        with db_connect() as conn:
            inserted = conn.execute(
                """INSERT INTO payments (charge_id, telegram_id, credits, stars)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (charge_id) DO NOTHING
                   RETURNING charge_id""",
                (charge_id, int(chat_id), PACKAGE_CREDITS, amount),
            ).fetchone()

            if inserted:
                conn.execute(
                    """INSERT INTO users (telegram_id, credits)
                       VALUES (%s, %s)
                       ON CONFLICT (telegram_id)
                       DO UPDATE SET credits = users.credits + EXCLUDED.credits""",
                    (int(chat_id), PACKAGE_CREDITS),
                )

        if inserted:
            record_event(chat_id, "payment", f"{amount} Stars; {PACKAGE_CREDITS} credits")
            send_message(
                chat_id,
                f"✅ Оплата получена!\n\nНачислено комплектов: {PACKAGE_CREDITS}.\n"
                "Отправь фотографию товара, чтобы начать.",
                main_keyboard(),
            )
        else:
            send_message(chat_id, "Этот платёж уже был учтён.")
    except Exception:
        logger.exception("Could not credit successful payment")
        send_message(
            chat_id,
            "Платёж получен Telegram, но начислить комплекты не удалось. "
            "Не оплачивай повторно — обратись в поддержку.",
        )


# ============================================================
# IMAGE VALIDATION AND DOWNLOAD
# ============================================================

def inspect_image(image_bytes):
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
            if width < 64 or height < 64:
                raise RuntimeError("Изображение слишком маленькое")
            if width > MAX_IMAGE_SIDE * 4 or height > MAX_IMAGE_SIDE * 4:
                raise RuntimeError("Размеры изображения слишком большие")
            if width * height > MAX_IMAGE_PIXELS:
                raise RuntimeError("Изображение слишком большое по количеству пикселей")
            image.verify()
            return {
                "width": width,
                "height": height,
                "format": image.format or "unknown",
                "pixels": width * height,
            }
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Не удалось прочитать изображение") from exc


def download_telegram_photo(file_id):
    info = telegram_api("getFile", {"file_id": file_id})
    file_path = info.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram не вернул путь к фотографии")

    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    with http.get(url, timeout=45, stream=True) as response:
        response.raise_for_status()
        chunks = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise RuntimeError("Фото больше допустимого размера")
            chunks.append(chunk)
        image_bytes = b"".join(chunks)

    inspect_image(image_bytes)
    return image_bytes


def normalize_image_bytes(image_bytes, max_side=MAX_IMAGE_SIDE, quality=94):
    inspect_image(image_bytes)
    with Image.open(io.BytesIO(image_bytes)) as original:
        image = ImageOps.exif_transpose(original)
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

        if image.mode in ("RGBA", "LA"):
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")

        output = io.BytesIO()
        image.save(output, "JPEG", quality=quality, optimize=True)
        return output.getvalue()


# ============================================================
# MAGIC HOUR — PRODUCT PHOTO GENERATION
# ============================================================

BASE_PROMPT = """
You are a professional commercial product photographer.

All reference images show the SAME physical product from different angles.
Use them as references for one product, never as separate products.

Preserve product identity: geometry, proportions, colors, materials,
buttons, ports, labels, branding, logos, stickers, camera count,
lens positions, accessories and visible details. Never redesign,
duplicate, replace or invent physical parts. Do not create a collage.

Create a realistic commercial product photo shoot with believable
lighting, contact shadows and coherent reflections. Keep the product
recognizable and physically plausible.

Do not add text, prices, discounts, specifications, watermarks or
invented badges inside the generated photograph. Avoid floating objects,
halos, fake reflections, distorted geometry and excessive glow.

Keep the product centered with generous empty space around it, so a
text panel can be placed next to the photo without covering the product.
"""


def generate_product_image(image_bytes_list, user_prompt):
    if not image_bytes_list:
        raise RuntimeError("Не передано ни одной фотографии")

    normalized_images = [normalize_image_bytes(data) for data in image_bytes_list]
    prompt = BASE_PROMPT + "\nVISUAL STYLE:\n" + clean_user_text(user_prompt)

    with tempfile.TemporaryDirectory() as temp_dir:
        paths = []
        for index, data in enumerate(normalized_images, start=1):
            path = Path(temp_dir) / f"reference_{index}.jpg"
            path.write_bytes(data)
            paths.append(str(path))

        result = magic_client.v1.ai_image_editor.generate(
            name="TovarPix product photo",
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

        if getattr(result, "status", None) != "complete":
            error = getattr(result, "error", None)
            if error:
                raise RuntimeError(
                    f"Magic Hour: {getattr(error, 'code', 'error')}: "
                    f"{getattr(error, 'message', str(error))}"
                )
            raise RuntimeError(
                f"Генерация завершилась со статусом: "
                f"{getattr(result, 'status', 'unknown')}"
            )

        output_paths = getattr(result, "downloaded_paths", None)
        if not output_paths:
            raise RuntimeError("Magic Hour не вернул готовое изображение")

        output_path = Path(str(output_paths[0]))
        if not output_path.is_file():
            raise RuntimeError("Не найден готовый файл изображения")

        return io.BytesIO(
            normalize_image_bytes(output_path.read_bytes(), max_side=3000, quality=94)
        )


# ============================================================
# FONT AND DRAWING HELPERS
# ============================================================

def load_font(size, bold=False):
    names = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
        if bold else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    )
    for path in names:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def hex_to_rgb(value, fallback=(217, 217, 217)):
    try:
        value = str(value).lstrip("#")
        if len(value) != 6:
            return fallback
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


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


def wrap_lines(draw, text, font, max_width, max_lines):
    """Переносит текст по словам. Возвращает (строки, было_ли_обрезано)."""
    words = str(text or "").split()
    lines = []
    current = ""

    for word in words:
        if text_width(draw, word, font) > max_width:
            word = fit_text(draw, word, font, max_width)
        candidate = word if not current else current + " " + word
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    truncated = len(lines) > max_lines
    lines = lines[:max_lines]
    if truncated and lines:
        lines[-1] = fit_text(
            draw, lines[-1].rstrip(" .,;:") + "…", font, max_width
        )
    return lines, truncated


def draw_gradient(canvas, box, top_color, bottom_color):
    x0, y0, x1, y1 = box
    height = max(1, y1 - y0)
    top = hex_to_rgb(top_color, (24, 26, 32))
    bottom = hex_to_rgb(bottom_color, (13, 15, 20))
    draw = ImageDraw.Draw(canvas)

    for offset in range(height):
        ratio = offset / max(1, height - 1)
        color = tuple(
            int(top[c] * (1 - ratio) + bottom[c] * ratio)
            for c in range(3)
        )
        draw.line((x0, y0 + offset, x1, y0 + offset), fill=color)


def rounded_panel(draw, box, fill, outline, radius=24, width=2):
    draw.rounded_rectangle(
        box, radius=radius, fill=fill, outline=outline, width=width
    )


# ============================================================
# CARD LAYOUT: ФОТО + ОТДЕЛЬНАЯ ЗОНА ПОД ТЕКСТ
# ============================================================

CARD_W, CARD_H = 1080, 1350
CARD_MARGIN = 36
SIDE_PANEL_W = 380
FEATHER = 130


def build_text_layout(text, position):
    """Заранее считает размеры текстовой плашки, чтобы выделить под неё место."""
    lines_raw = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines_raw:
        return None

    side = position in ("left", "right")
    panel_w = SIDE_PANEL_W if side else CARD_W - CARD_MARGIN * 2
    pad = 26
    inner_w = panel_w - pad * 2
    max_h = 1000 if side else 430

    measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    title = " ".join(lines_raw[0].split())[:100]
    title_sizes = (40, 36, 32, 28) if side else (52, 48, 44, 40, 36)
    title_font = None
    title_lines = []
    title_size = title_sizes[-1]
    for size in title_sizes:
        title_font = load_font(size, True)
        title_lines, truncated = wrap_lines(measure, title, title_font, inner_w, 3)
        title_size = size
        if not truncated:
            break

    body_size = 24 if side else 28
    body_font = load_font(body_size, False)
    bullet_indent = 24
    body_items = []
    for item in lines_raw[1:7]:
        item = " ".join(item.split())[:110]
        item_lines, _ = wrap_lines(
            measure, item, body_font, inner_w - bullet_indent, 2
        )
        if item_lines:
            body_items.append(item_lines)

    title_lh = int(title_size * 1.2)
    body_lh = int(body_size * 1.35)
    gap = 14
    item_gap = 10

    def total_height():
        h = pad * 2 + len(title_lines) * title_lh
        if body_items:
            h += gap
            h += sum(len(it) * body_lh for it in body_items)
            h += item_gap * (len(body_items) - 1)
        return h

    while body_items and total_height() > max_h:
        body_items.pop()

    return {
        "w": panel_w,
        "h": min(total_height(), max_h),
        "pad": pad,
        "title_lines": title_lines,
        "title_font": title_font,
        "title_lh": title_lh,
        "body_items": body_items,
        "body_font": body_font,
        "body_lh": body_lh,
        "bullet_indent": bullet_indent,
        "gap": gap,
        "item_gap": item_gap,
    }


def compute_geometry(position, layout):
    """Возвращает (рамка плашки или None, (x, y, сторона квадрата фото))."""
    W, H, M = CARD_W, CARD_H, CARD_MARGIN

    if not layout or position == "none":
        side = W
        return None, (0, (H - side) // 2, side)

    pw, ph = layout["w"], layout["h"]

    if position == "top":
        panel = (M, M, M + pw, M + ph)
        zone = ph + M * 2
        area_x, area_y, area_w, area_h = 0, zone, W, H - zone
    elif position == "bottom":
        panel = (M, H - M - ph, M + pw, H - M)
        zone = ph + M * 2
        area_x, area_y, area_w, area_h = 0, 0, W, H - zone
    elif position == "left":
        panel = (M, (H - ph) // 2, M + pw, (H - ph) // 2 + ph)
        side = min(H, W - (pw + M * 2) + 120)
        return panel, (W - side, (H - side) // 2, side)
    else:  # right
        panel = (W - M - pw, (H - ph) // 2, W - M, (H - ph) // 2 + ph)
        side = min(H, W - (pw + M * 2) + 120)
        return panel, (0, (H - side) // 2, side)

    side = min(area_w, area_h)
    px = area_x + (area_w - side) // 2
    py = area_y + (area_h - side) // 2
    return panel, (px, py, side)


def feather_mask(side, fade_left, fade_right, fade_top, fade_bottom):
    """Маска с мягкими краями: фото плавно растворяется в фоне."""
    mask = Image.new("L", (side, side), 255)
    f = max(1, min(FEATHER, side // 4))

    def strip(horizontal, reverse):
        g = Image.new("L", (side, side), 255)
        d = ImageDraw.Draw(g)
        for i in range(f):
            value = int(255 * i / f)
            pos = (side - 1 - i) if reverse else i
            if horizontal:
                d.line((pos, 0, pos, side), fill=value)
            else:
                d.line((0, pos, side, pos), fill=value)
        return g

    if fade_left:
        mask = ImageChops.multiply(mask, strip(True, False))
    if fade_right:
        mask = ImageChops.multiply(mask, strip(True, True))
    if fade_top:
        mask = ImageChops.multiply(mask, strip(False, False))
    if fade_bottom:
        mask = ImageChops.multiply(mask, strip(False, True))
    return mask


def draw_panel(draw, kind, box, style):
    x0, y0, x1, y1 = box
    if kind == "minimal":
        rounded_panel(draw, box, "#FFFFFF", "#D7DDD7", 24, 2)
    elif kind == "luxury":
        draw.rectangle(box, fill="#11100E", outline="#D7B56D", width=3)
        draw.rectangle((x0 + 9, y0 + 9, x1 - 9, y1 - 9), outline="#6F5B37", width=1)
    elif kind == "cozy":
        rounded_panel(draw, box, "#FFF7EA", "#E2C5A5", 32, 2)
    elif kind == "market":
        draw.rounded_rectangle(box, radius=12, fill="#FFFFFF")
        draw.rounded_rectangle((x0, y0, x0 + 12, y1), radius=6, fill="#F05A28")
    elif kind == "tech":
        draw.rectangle(box, fill="#111D2A", outline="#65D6FF", width=2)
        draw.line((x0 + 18, y0 + 12, x1 - 18, y0 + 12), fill="#65D6FF", width=2)
    elif kind == "eco":
        rounded_panel(draw, box, "#F7F6E9", "#91A783", 28, 2)
        draw.ellipse((x1 - 35, y0 + 10, x1 - 18, y0 + 27), fill="#4C8058")
    elif kind == "fashion":
        draw.rectangle(box, fill="#FAF9F7", outline="#151515", width=2)
        draw.line((x0 + 16, y0 + 12, x1 - 16, y0 + 12), fill="#C53A48", width=4)
    elif kind == "info":
        rounded_panel(draw, box, "#FFFFFF", "#B8CBE3", 18, 3)
        draw.rectangle((x0, y0, x0 + 10, y1), fill="#2869C7")
    elif kind == "pop":
        rounded_panel(draw, box, "#FFFDF7", "#7546D8", 30, 4)
        draw.ellipse((x1 - 32, y0 + 8, x1 - 12, y0 + 28), fill="#E8C84D")
    else:
        draw.rectangle(box, fill="#12151A", outline="#FFB86B", width=2)
        draw.line((x0 + 14, y0 + 12, x0 + 90, y0 + 12), fill="#FFB86B", width=3)


def draw_text_layout(draw, box, layout, style):
    x0, y0, x1, y1 = box
    pad = layout["pad"]
    text_color = style["text"]
    muted_color = style["muted"]
    accent = style["accent"]

    y = y0 + pad
    for line in layout["title_lines"]:
        draw.text((x0 + pad, y), line, font=layout["title_font"], fill=text_color)
        y += layout["title_lh"]

    if layout["body_items"]:
        y += layout["gap"]
        body_lh = layout["body_lh"]
        for item in layout["body_items"]:
            bullet_y = y + body_lh // 2 - 5
            draw.rounded_rectangle(
                (x0 + pad, bullet_y, x0 + pad + 9, bullet_y + 9),
                radius=2, fill=accent,
            )
            for line in item:
                draw.text(
                    (x0 + pad + layout["bullet_indent"], y),
                    line, font=layout["body_font"], fill=muted_color,
                )
                y += body_lh
            y += layout["item_gap"]


def render_ad_card(
    image_bytes,
    text,
    style_id="minimal",
    position="bottom",
    card_index=1,
    total_cards=1,
):
    style = STYLE_OPTIONS.get(style_id, STYLE_OPTIONS["minimal"])
    kind = style["kind"]
    W, H = CARD_W, CARD_H

    with Image.open(io.BytesIO(normalize_image_bytes(image_bytes, 1800, 93))) as source:
        photo = source.convert("RGB").copy()

    layout = build_text_layout(text, position) if (text and position != "none") else None
    if layout is None:
        position = "none"
    panel_box, (px, py, side) = compute_geometry(position, layout)

    # 1. Фон: размытая копия самой фотографии, подкрашенная цветами стиля.
    background = ImageOps.fit(
        photo, (W, H), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
    ).filter(ImageFilter.GaussianBlur(38))
    tint = Image.new("RGB", (W, H))
    draw_gradient(tint, (0, 0, W, H), style["top"], style["bottom"])
    canvas = Image.blend(background, tint, 0.3)

    # 2. Фото: квадрат целиком (без обрезки), с мягкими краями.
    product = ImageOps.fit(
        photo, (side, side), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
    ).filter(ImageFilter.UnsharpMask(radius=1.6, percent=70, threshold=3))
    mask = feather_mask(
        side,
        fade_left=px > 0,
        fade_right=px + side < W,
        fade_top=py > 0,
        fade_bottom=py + side < H,
    )
    canvas.paste(product, (px, py), mask)

    # 3. Декор стиля.
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    if kind == "luxury":
        od.rectangle((20, 20, W - 21, H - 21), outline=(215, 181, 109, 230), width=3)
        od.rectangle((34, 34, W - 35, H - 35), outline=(215, 181, 109, 100), width=1)
    elif kind == "tech":
        for gx in range(35, W, 90):
            od.line((gx, 0, gx, H), fill=(101, 214, 255, 38), width=1)
        for gy in range(35, H, 90):
            od.line((0, gy, W, gy), fill=(101, 214, 255, 32), width=1)
        od.line((35, 35, 180, 35), fill=(101, 214, 255, 220), width=4)
    elif kind == "eco":
        od.ellipse((W - 235, 35, W - 45, 225), outline=(164, 185, 153, 180), width=3)
    elif kind == "pop":
        od.ellipse((25, 30, 130, 135), fill=(232, 200, 77, 190))
        od.ellipse((W - 145, 45, W - 50, 140), fill=(164, 138, 240, 170))
    elif kind == "fashion":
        od.line((45, 42, W - 45, 42), fill=(255, 255, 255, 220), width=2)
        od.line((45, H - 42, W - 45, H - 42), fill=(255, 255, 255, 220), width=2)
    elif kind == "market":
        od.polygon([(0, 0), (230, 0), (0, 170)], fill=(255, 226, 165, 200))
    elif kind == "info":
        od.rounded_rectangle((25, 25, W - 25, H - 25), radius=28, outline=(184, 203, 227, 210), width=3)
    elif kind == "cinematic":
        for y in range(250):
            alpha = int(100 * (1 - y / 250))
            od.line((0, y, W, y), fill=(0, 0, 0, alpha))
            od.line((0, H - 1 - y, W, H - 1 - y), fill=(0, 0, 0, alpha))

    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    # 4. Текстовая плашка — строго в своей зоне, товар не перекрывает.
    draw = ImageDraw.Draw(canvas)
    if layout and panel_box:
        draw_panel(draw, kind, panel_box, style)
        draw_text_layout(draw, panel_box, layout, style)

    # 5. Номер карточки — в углу, где нет плашки.
    index_font = load_font(17, True)
    index_text = f"{card_index}/{total_cards}"
    iw = text_width(draw, index_text, index_font)
    badge_top = 36 if position == "bottom" else H - 52
    draw.rounded_rectangle(
        (W - iw - 42, badge_top, W - 20, badge_top + 32),
        radius=12,
        fill=style["panel"],
        outline=style["border"],
        width=1,
    )
    draw.text(
        (W - iw - 31, badge_top + 5),
        index_text, font=index_font, fill=style["text"],
    )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=93, optimize=True, progressive=True)
    output.seek(0)
    return output


# ============================================================
# TELEGRAM RESULT DELIVERY
# ============================================================

def send_generated_photo(
    chat_id, image_buffer, caption=None, filename="tovarpix_result.jpg"
):
    image_buffer.seek(0)
    response = http.post(
        f"{API_URL}/sendPhoto",
        data={
            "chat_id": str(chat_id),
            "caption": (caption or "✅ Готово!")[:1024],
        },
        files={"photo": (filename, image_buffer, "image/jpeg")},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram не смог отправить результат"))


# ============================================================
# IMAGE SET PROCESSING — ONE CREDIT FOR THE WHOLE SET
# ============================================================

def process_photo(chat_id, file_ids, prompt, style_id, cards):
    credit_type = None
    started = time.monotonic()
    sent_count = 0
    style = STYLE_OPTIONS.get(style_id, STYLE_OPTIONS["minimal"])
    cards = cards[:7]
    card_count = len(cards)

    with state_lock:
        runtime_stats["images_started"] += 1

    try:
        credit_type = reserve_credit(chat_id)
        if credit_type is None:
            send_message(
                chat_id,
                "🎁 Бесплатный комплект уже использован, а купленные комплекты закончились.\n\n"
                "Один комплект из 1–7 карточек стоит 1 кредит.\n"
                "Купи 5 комплектов за 5 ⭐.",
                main_keyboard(),
            )
            return

        prompt = clean_user_text(prompt) or (
            "Сохрани товар и создай качественную коммерческую предметную фотографию."
        )

        record_event(
            chat_id, "processing_started",
            f"style={style_id};count={card_count};photos={len(file_ids)}",
        )
        send_message(
            chat_id,
            f"🎨 Начинаю комплект: {card_count} карточек.\n"
            f"Стиль: {style['emoji']} {style['name']}\n"
            f"Фотографий-референсов: {len(file_ids)}\n\n"
            "За весь комплект списывается 1 кредит. Это может занять несколько минут. "
            "Карточки буду присылать по мере готовности.",
        )

        image_bytes_list = []
        for index, file_id in enumerate(file_ids, start=1):
            data = download_telegram_photo(file_id)
            info = inspect_image(data)
            logger.info(
                "Reference accepted: chat=%s photo=%s size=%sx%s format=%s",
                chat_id, index, info["width"], info["height"], info["format"],
            )
            image_bytes_list.append(data)

        photo_total = len(image_bytes_list)

        for index, card_data in enumerate(cards, start=1):
            role = CARD_ROLES[min(index - 1, len(CARD_ROLES) - 1)]

            # Какое фото — главное для этой карточки.
            primary = safe_int(card_data.get("photo_index"), (index - 1) % photo_total)
            if not 0 <= primary < photo_total:
                primary = (index - 1) % photo_total
            ordered = [image_bytes_list[primary]] + [
                data for i, data in enumerate(image_bytes_list) if i != primary
            ]

            if photo_total > 1:
                angle_note = (
                    "Reference image 1 is the PRIMARY view for this card: keep the "
                    "product at the same viewing angle and orientation as reference "
                    "image 1. Use the other references only to keep details accurate. "
                )
            else:
                angle_note = (
                    "Keep the product at the same viewing angle as the reference. "
                )

            style_prompt = (
                f"Create product photo {index} of {card_count}. "
                f"Visual style: {style['name']}. "
                f"Direction: {style['description']}. "
                f"Composition role: {role}. "
                + angle_note +
                "All references show the same product. Preserve its exact identity, "
                "geometry, proportions, color, materials, labels, logos, buttons, "
                "camera count and details. Do not add text, prices, discounts, "
                "badges or invented specifications inside the image. "
                "Change the background, environment, lighting and composition only. "
                "User's visual instructions: " + prompt
            )

            generated = generate_product_image(ordered, style_prompt)
            final_card = render_ad_card(
                generated.getvalue(),
                card_data.get("text", ""),
                style_id,
                card_data.get("position", "bottom"),
                index,
                card_count,
            )
            send_generated_photo(
                chat_id,
                final_card,
                caption=f"✅ Карточка {index}/{card_count} • {style['name']}",
                filename=f"tovarpix_{style_id}_{index}.jpg",
            )
            sent_count += 1
            with state_lock:
                runtime_stats["cards_rendered"] += 1

        with state_lock:
            runtime_stats["images_succeeded"] += 1
            runtime_stats["last_success_at"] = utc_now_iso()

        record_event(
            chat_id, "processing_succeeded",
            f"style={style_id};count={card_count};duration={int(time.monotonic()-started)}s",
        )

        balance = get_balance(chat_id)
        balance_text = (
            "👑 У тебя безлимит."
            if balance["admin"]
            else f"⭐ Осталось комплектов: {balance['credits']}."
        )
        send_message(
            chat_id,
            f"🎉 Комплект готов: {card_count} карточек.\n{balance_text}\n\n"
            "Можешь отправить следующий товар.",
            main_keyboard(),
        )

    except Exception as exc:
        logger.exception("Image set processing failed")
        note_runtime_error(exc)
        with state_lock:
            runtime_stats["images_failed"] += 1

        record_event(
            chat_id, "processing_failed",
            f"style={style_id};count={card_count};{type(exc).__name__}",
        )

        if credit_type and sent_count == 0:
            try:
                refund_credit(chat_id, credit_type)
            except Exception:
                logger.exception("Credit refund failed")

        if sent_count:
            message = (
                f"⚠️ Не удалось закончить комплект: доставлено {sent_count} "
                f"из {card_count} карточек. Кредит не возвращён, потому что часть "
                "результатов уже отправлена. Если нужна помощь, сообщи администратору."
            )
        else:
            message = (
                "😕 Не удалось подготовить комплект. Если база данных доступна, "
                "кредит возвращён автоматически.\n\n"
                "Попробуй позже. Если ошибка повторяется, сообщи администратору."
            )

        send_message(chat_id, message, main_keyboard())

    finally:
        with state_lock:
            busy_chats.discard(chat_id)


def start_processing(chat_id, file_ids, prompt, style_id, cards):
    file_ids = list(file_ids)[:MAX_REFERENCE_PHOTOS]
    cards = cards[:7]

    with state_lock:
        if chat_id in busy_chats:
            send_message(chat_id, "⏳ Предыдущий комплект ещё обрабатывается.")
            return
        busy_chats.add(chat_id)

    thread = threading.Thread(
        target=process_photo,
        args=(chat_id, file_ids, prompt, style_id, cards),
        daemon=True,
        name=f"image-set-{chat_id}",
    )
    thread.start()


# ============================================================
# HELP AND ADMIN
# ============================================================

def help_text():
    return (
        "❓ Как пользоваться TovarPix\n\n"
        "1. Отправь фото товара.\n"
        "2. При желании добавь другие ракурсы.\n"
        "3. Нажми «Фотографии загружены».\n"
        "4. Выбери один из 10 стилей.\n"
        "5. Укажи количество карточек от 1 до 7.\n"
        "6. Напиши пожелания к обработке фотографии.\n"
        "7. Для каждой карточки отдельно задай текст и его расположение.\n\n"
        "Если загружено несколько ракурсов, каждая карточка делается по своему фото: "
        "1-я по первому, 2-я по второму и так далее.\n"
        "Текст наносится программно, поэтому нейросеть не рисует русские надписи.\n"
        "Один комплект из 1–7 карточек стоит 1 кредит."
    )


def features_text():
    return (
        "📋 Возможности TovarPix\n\n"
        "📸 AI-обработка товарных фотографий\n"
        "🎨 10 визуальных стилей\n"
        "🧩 Комплекты от 1 до 7 карточек\n"
        "📷 Несколько ракурсов одного товара\n"
        "✍️ Отдельный текст для каждой карточки\n"
        "📍 Выбор положения текста: сверху, снизу, слева, справа\n"
        "🔤 Программная отрисовка русского текста\n"
        "💳 Оплата Telegram Stars\n\n"
        "Важно: AI может ошибаться в мелких деталях. "
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
            f"Остаток комплектов: {stats['credits_remaining']}\n"
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


# ============================================================
# COMMANDS
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
            "Привет! 👋 Я TovarPix — AI-редактор товарных фотографий.\n\n"
            "📸 Отправь фотографию товара.\n"
            "📷 При желании добавь другие ракурсы.\n"
            "🎨 Выбери один из 10 стилей.\n"
            "🧩 Укажи от 1 до 7 карточек.\n"
            "✍️ Для каждой карточки задай собственный текст и его положение.\n\n"
            "🎁 Доступен один бесплатный комплект.\n"
            "⭐ Пять дополнительных комплектов стоят 5 Stars.",
            main_keyboard(),
        )
        return

    if command == "/myid":
        send_message(
            chat_id,
            f"Твой Telegram ID: {chat_id}\nДля безлимита добавь ID в ADMIN_IDS в Render.",
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
            send_message(chat_id, "Не удалось загрузить баланс.")
        return

    if command == "/buy" or text == "⭐ Купить 5 комплектов":
        try:
            send_invoice(chat_id)
        except Exception:
            logger.exception("Could not send invoice")
            send_message(chat_id, "Не удалось создать счёт.")
        return

    if command == "/cancel" or text == "❌ Отмена":
        try:
            clear_session(chat_id)
        except Exception:
            logger.exception("Could not clear session")
        send_message(chat_id, "Ожидание отменено. Можешь начать заново.", main_keyboard())
        return

    if command == "/example" or text == "💡 Пример промпта":
        send_message(
            chat_id,
            "💡 Пример пожеланий к фотографии:\n\n"
            "• Белый фон и мягкая реалистичная тень.\n"
            "• Премиальная тёмная студия.\n"
            "• Светлый интерьер и дневной свет.\n"
            "• Минималистичная композиция.\n\n"
            "Пример текста карточки:\n"
            "Беспроводные наушники\n"
            "Компактный дизайн и удобный кейс\n\n"
            "Первая строка — заголовок, каждая следующая — отдельная характеристика.\n"
            "Указывай только характеристики, которые действительно известны.",
            main_keyboard(),
        )
        return

    if command == "/admin":
        send_message(chat_id, admin_text(chat_id), main_keyboard())
        return

    if command == "/privacy":
        send_message(
            chat_id,
            "🔒 Фотографии передаются внешнему сервису обработки. "
            "Не отправляй конфиденциальные изображения. Проверяй результат перед публикацией.",
            main_keyboard(),
        )
        return

    if command == "/status":
        try:
            send_message(
                chat_id,
                "📊 Статус\n\n" + user_balance_text(get_balance(chat_id))
                + "\n\nВремя работы: "
                + format_duration(time.time() - APP_STARTED_AT),
                main_keyboard(),
            )
        except Exception:
            logger.exception("Status command failed")
            send_message(chat_id, "Статус временно недоступен.")
        return

    send_message(chat_id, "Не понял команду. Нажми «❓ Помощь».", main_keyboard())


# ============================================================
# CALLBACKS (кнопки) — состояние хранится в PostgreSQL
# ============================================================

def card_text_prompt(card_index, card_count, photo_count):
    note = ""
    if photo_count > 1:
        note = f"\n📷 Эта карточка будет сделана по фото №{(card_index % photo_count) + 1}."
    return (
        f"✍️ Напиши текст для карточки {card_index + 1}/{card_count}.{note}\n\n"
        "Первая строка — заголовок. Каждая следующая строка — отдельная характеристика.\n"
        "Если текст не нужен, напиши «Без текста»."
    )


def handle_callback_query(callback):
    callback_id = callback.get("id")
    data = str(callback.get("data", ""))
    chat_id = callback.get("message", {}).get("chat", {}).get("id")

    if not chat_id:
        answer_callback_query(callback_id, "Не удалось определить чат")
        return

    if data == "photos:help":
        answer_callback_query(callback_id)
        send_message(
            chat_id,
            "📷 Лучше прислать фотографии одного товара:\n\n"
            "• Вид спереди.\n• Вид сзади.\n• Вид сбоку.\n"
            "• Крупный план логотипа или важных деталей.\n\n"
            "Каждая карточка комплекта будет сделана по своему фото. "
            "Если есть только одно фото, продолжай с ним.",
        )
        return

    toast = None
    messages = []
    start_args = None

    with session_edit(chat_id) as s:
        photos = list(s.get("photos", []))
        job = s.get("job")
        stage = job.get("stage") if job else None

        if data == "photos:done":
            if not photos:
                toast = "Сначала отправь фото"
            elif job and stage not in ("photos", "style"):
                toast = "Эта кнопка сейчас недоступна"
            else:
                s["job"] = new_job("style")
                toast = f"Фотографий: {len(photos)}"
                messages.append((
                    f"✅ Собрано фотографий: {len(photos)}.\n\nТеперь выбери стиль.",
                    style_keyboard(),
                ))

        elif not photos or not job:
            toast = "Сначала отправь фото товара"
            messages.append(("Сначала отправь фотографию товара.", main_keyboard()))

        elif data.startswith("style:"):
            style_id = data.split(":", 1)[1]
            if style_id not in STYLE_OPTIONS:
                toast = "Неизвестный стиль"
            elif stage not in ("style", "count"):
                toast = "Эта кнопка сейчас недоступна"
            else:
                job["style_id"] = style_id
                job["stage"] = "count"
                style = STYLE_OPTIONS[style_id]
                toast = f"Выбран стиль: {style['name']}"
                messages.append((
                    f"Выбран стиль: {style['emoji']} {style['name']}\n\n"
                    "Сколько карточек сделать? Весь комплект стоит 1 кредит.",
                    count_keyboard(),
                ))

        elif data.startswith("count:"):
            count = safe_int(data.split(":", 1)[1], 0)
            if not 1 <= count <= 7:
                toast = "Можно выбрать от 1 до 7"
            elif not job.get("style_id"):
                toast = "Сначала выбери стиль"
                messages.append(("Сначала выбери стиль.", style_keyboard()))
            elif stage not in ("count", "prompt"):
                toast = "Эта кнопка сейчас недоступна"
            else:
                job["card_count"] = count
                job["stage"] = "prompt"
                job["cards"] = []
                job["card_index"] = 0
                toast = f"Карточек: {count}"
                messages.append((
                    f"📦 Комплект: {count} карточек.\n"
                    f"🎨 Стиль: {STYLE_OPTIONS[job['style_id']]['name']}\n"
                    f"📷 Фотографий: {len(photos)}\n"
                    "💳 Стоимость: 1 кредит.\n\n"
                    "Теперь напиши пожелания к обработке фотографий.\n"
                    "Например: белая студия, мягкая тень, товар строго по центру.\n\n"
                    "Если пожеланий нет, напиши «Стандартно».",
                    main_keyboard(),
                ))

        elif data.startswith("position:"):
            position = data.split(":", 1)[1]
            if position not in ("top", "bottom", "left", "right", "none"):
                toast = "Неизвестное положение"
            elif stage != "card_position":
                toast = "Эта кнопка сейчас недоступна"
            else:
                index = safe_int(job.get("card_index"), 0)
                cards = job.get("cards", [])
                card_count = safe_int(job.get("card_count"), 0)

                if index < 0 or index >= len(cards):
                    toast = "Карточка не найдена. Нажми «❌ Отмена» и начни заново"
                else:
                    cards[index]["position"] = position
                    next_index = index + 1

                    if next_index < card_count:
                        job["card_index"] = next_index
                        job["stage"] = "card_text"
                        toast = "Положение сохранено"
                        messages.append((
                            f"✅ Карточка {index + 1}/{card_count}: положение сохранено.\n\n"
                            + card_text_prompt(next_index, card_count, len(photos)),
                            None,
                        ))
                    else:
                        with state_lock:
                            is_busy = chat_id in busy_chats
                        if is_busy:
                            toast = "Предыдущий комплект ещё обрабатывается"
                        else:
                            start_args = (
                                list(photos),
                                job.get("prompt", ""),
                                job.get("style_id"),
                                [dict(card) for card in cards],
                            )
                            s.clear()
                            toast = "Настройки сохранены"
                            messages.append((
                                "✅ Настройки всех карточек готовы!\n\n"
                                "Начинаю обработку фотографий. Не отправляй новые фото, "
                                "пока комплект не закончится.",
                                None,
                            ))
        else:
            toast = "Кнопка устарела"

    answer_callback_query(callback_id, toast)
    for text, markup in messages:
        send_message(chat_id, text, markup)
    if start_args:
        start_processing(chat_id, *start_args)


# ============================================================
# TELEGRAM UPDATE HANDLER
# ============================================================

def extract_chat_id(update):
    try:
        if update.get("message"):
            return update["message"].get("chat", {}).get("id")
        if update.get("callback_query"):
            return update["callback_query"].get("message", {}).get("chat", {}).get("id")
        if update.get("pre_checkout_query"):
            return update["pre_checkout_query"].get("from", {}).get("id")
    except Exception:
        pass
    return None


def handle_photo_message(chat_id, message):
    file_id = message["photo"][-1]["file_id"]
    reply = None
    count = 0

    with state_lock:
        is_busy = chat_id in busy_chats

    with session_edit(chat_id) as s:
        job = s.get("job")
        photos = list(s.get("photos", []))

        if is_busy:
            reply = "⏳ Дождись завершения предыдущей обработки."
        elif job and job.get("stage") not in (None, "photos"):
            reply = "Ты уже начал настройку комплекта. Нажми «❌ Отмена», чтобы начать заново."
        elif len(photos) >= MAX_REFERENCE_PHOTOS:
            reply = f"Можно использовать максимум {MAX_REFERENCE_PHOTOS} фотографий."
        else:
            photos.append(file_id)
            s["photos"] = photos
            s["job"] = new_job("photos")
            count = len(photos)

    if reply:
        send_message(chat_id, reply)
        return

    send_message(
        chat_id,
        f"📸 Фотография {count}/{MAX_REFERENCE_PHOTOS} получена.\n\n"
        "Можешь прислать другие ракурсы того же товара. Когда закончишь, нажми кнопку ниже.",
        photo_collection_keyboard(),
    )


def handle_plain_text(chat_id, message, text):
    action = None
    card_number = 0
    card_count = 0
    photo_count = 0
    stage = None

    with session_edit(chat_id) as s:
        photos = list(s.get("photos", []))
        job = s.get("job")
        photo_count = len(photos)

        if photos and job:
            stage = job.get("stage")

            if stage == "prompt" and job.get("style_id") and job.get("card_count"):
                job["prompt"] = "" if text.lower() in (
                    "стандартно", "без пожеланий", "по умолчанию"
                ) else text
                job["stage"] = "card_text"
                job["card_index"] = 0
                job["cards"] = []
                card_number = 1
                card_count = safe_int(job["card_count"], 1)
                action = "ask_first_text"

            elif stage == "card_text":
                index = safe_int(job.get("card_index"), 0)
                card_count = safe_int(job.get("card_count"), 0)
                card_text = clean_overlay_text(text)
                if card_text.lower().strip(" .!") in ("без текста", "нет", "пропустить"):
                    card_text = ""

                cards = job.setdefault("cards", [])
                entry = {
                    "text": card_text,
                    "position": "bottom",
                    "photo_index": index % max(1, len(photos)),
                }
                if index < len(cards):
                    cards[index] = entry
                else:
                    cards.append(entry)

                job["stage"] = "card_position"
                card_number = index + 1
                action = "ask_position"

            else:
                action = "remind"

    if action == "ask_first_text":
        send_message(
            chat_id,
            f"Карточка 1/{card_count}.\n\n"
            + card_text_prompt(0, card_count, photo_count),
        )
        return

    if action == "ask_position":
        send_message(
            chat_id,
            f"📍 Текст для карточки {card_number}/{card_count} принят.\n\n"
            "Теперь выбери, где разместить текст.",
            position_keyboard(),
        )
        return

    if action == "remind":
        if stage == "photos":
            send_message(
                chat_id,
                "Сначала закончи загрузку фотографий кнопкой ниже.",
                photo_collection_keyboard(),
            )
        elif stage == "style":
            send_message(chat_id, "Сначала выбери стиль.", style_keyboard())
        elif stage == "count":
            send_message(chat_id, "Выбери количество от 1 до 7.", count_keyboard())
        elif stage == "card_position":
            send_message(
                chat_id,
                "Сначала выбери положение текста кнопками выше.",
                position_keyboard(),
            )
        else:
            handle_command_or_text(chat_id, message, text)
        return

    handle_command_or_text(chat_id, message, text)


def process_update(update):
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
        handle_photo_message(chat_id, message)
        return

    # Кнопки нижнего меню и команды — ВСЕГДА команды, а не текст карточки.
    if text in MENU_TEXTS or text.startswith("/"):
        handle_command_or_text(chat_id, message, text)
        return

    if text:
        handle_plain_text(chat_id, message, text)
        return

    send_message(chat_id, "Отправь фотографию товара, чтобы начать.", main_keyboard())


def handle_update(update):
    with state_lock:
        runtime_stats["updates"] += 1

    try:
        process_update(update)
    except Exception as exc:
        logger.exception("Update handling failed")
        note_runtime_error(exc)
        chat_id = extract_chat_id(update)
        if chat_id:
            send_message(
                chat_id,
                "⚠️ Что-то пошло не так. Нажми «❌ Отмена» и начни заново.",
                main_keyboard(),
            )


# ============================================================
# WEBHOOK AND ADMIN ENDPOINTS
# ============================================================

def admin_endpoint_allowed():
    if not ADMIN_ENDPOINT_SECRET:
        return False
    supplied = (
        request.headers.get("X-Admin-Secret", "")
        or request.args.get("secret", "")
    )
    return hmac.compare_digest(supplied, ADMIN_ENDPOINT_SECRET)


@app.route("/", methods=["GET"])
def home():
    return "TovarPix AI is running!", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "tovarpix",
        "uptime_seconds": int(time.time() - APP_STARTED_AT),
        "timestamp": utc_now_iso(),
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
    if not admin_endpoint_allowed():
        return jsonify({"error": "Forbidden"}), 403
    try:
        me = telegram_api("getMe")
        return jsonify({
            "username": me.get("username"),
            "first_name": me.get("first_name"),
            "id": me.get("id"),
        }), 200
    except Exception:
        logger.exception("Bot info failed")
        return jsonify({"error": "Telegram API error"}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            logger.warning("Rejected webhook request with invalid secret")
            return "Forbidden", 403

    if not request.is_json:
        return "Expected JSON", 415

    try:
        payload = request.get_json(silent=False)
        if not isinstance(payload, dict):
            return "Expected JSON object", 400

        update_id = payload.get("update_id")
        if update_id is not None:
            with db_connect() as conn:
                inserted = conn.execute(
                    """INSERT INTO processed_updates (update_id)
                       VALUES (%s)
                       ON CONFLICT (update_id) DO NOTHING
                       RETURNING update_id""",
                    (int(update_id),),
                ).fetchone()

            if not inserted:
                return "OK", 200

        handle_update(payload)
        return "OK", 200
    except Exception:
        logger.exception("Webhook handler failed")
        return "Internal Server Error", 500


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    if not admin_endpoint_allowed():
        return "Forbidden", 403
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
    if not admin_endpoint_allowed():
        return jsonify({"error": "Forbidden"}), 403
    try:
        info = telegram_api("getWebhookInfo")
        return jsonify({
            "url_configured": bool(info.get("url")),
            "url_matches_render": info.get("url") == f"{RENDER_URL}/webhook",
            "pending_update_count": info.get("pending_update_count"),
            "last_error_date": info.get("last_error_date"),
            "last_error_message": info.get("last_error_message"),
            "max_connections": info.get("max_connections"),
        }), 200
    except Exception:
        logger.exception("Webhook status failed")
        return jsonify({"error": "Telegram API error"}), 500


@app.route("/runtime_stats", methods=["GET"])
def runtime_stats_route():
    if not admin_endpoint_allowed():
        return jsonify({"error": "Forbidden"}), 403
    with state_lock:
        stats = dict(runtime_stats)
        stats["busy_count"] = len(busy_chats)
    return jsonify({
        "uptime_seconds": int(time.time() - APP_STARTED_AT),
        "stats": stats,
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

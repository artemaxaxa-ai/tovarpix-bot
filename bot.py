
import os
import io
import uuid
import json
import base64
import logging
import threading
import tempfile
from pathlib import Path

import requests
import psycopg
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
from magic_hour import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
MAGIC_HOUR_API_KEY = os.environ.get("MAGIC_HOUR_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DATABASE_URL = os.environ.get("DATABASE_URL")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

ADMIN_IDS = {
    value.strip()
    for value in os.environ.get("ADMIN_IDS", "").split(",")
    if value.strip()
}

PACKAGE_IMAGES = 5
PACKAGE_STARS = 5

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not MAGIC_HOUR_API_KEY:
    raise RuntimeError("Не задан MAGIC_HOUR_API_KEY")
if not DATABASE_URL:
    raise RuntimeError("Не задан DATABASE_URL")

API_URL = f"https://api.telegram.org/bot{TOKEN}"

app = Flask(__name__)
http = requests.Session()
magic_client = Client(token=MAGIC_HOUR_API_KEY)

pending_photos = {}
busy_chats = set()
state_lock = threading.Lock()


# =========================
# DATABASE
# =========================

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
    logger.info("PostgreSQL database is ready")


init_db()


def is_admin(chat_id):
    return str(chat_id) in ADMIN_IDS


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


# =========================
# TELEGRAM API
# =========================

def telegram_api(method, data=None):
    response = http.post(
        f"{API_URL}/{method}",
        json=data or {},
        timeout=35
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Telegram API error")
        )

    return result.get("result")


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        data["reply_markup"] = reply_markup

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
            [{"text": "❌ Отмена"}]
        ],
        "resize_keyboard": True
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


# =========================
# TELEGRAM STARS PAYMENTS
# =========================

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
                "Не удалось автоматически проверить платёж. Обратись в поддержку."
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


# =========================
# IMAGE DOWNLOAD
# =========================

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
    return response.content


# =========================
# QWEN IMAGE EDITOR
# =========================

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
Do not change any text printed on the product.

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
        + user_prompt[:1500]
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / "input.jpg"

        with Image.open(io.BytesIO(image_bytes)) as image:
            image = image.convert("RGB")
            image.save(input_path, format="JPEG", quality=95)

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

            raise RuntimeError(f"Generation status: {result.status}")

        paths = result.downloaded_paths

        if not paths or not Path(str(paths[0])).is_file():
            raise RuntimeError("Generated image file not found")

        output = io.BytesIO(Path(str(paths[0])).read_bytes())
        output.seek(0)
        return output


# =========================
# GEMINI: AD TEXT GENERATION
# =========================

def get_ad_copy(image_bytes, user_prompt):
    """
    Gemini decides whether the user explicitly requested an ad card.
    Returns validated text fields, or None if no card is needed.
    """

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not configured")
        return None

    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        image.thumbnail((512, 512))
        image_buffer = io.BytesIO()
        image.save(image_buffer, format="JPEG", quality=78)

    instruction = f"""
Ты — арт-директор интернет-магазина и копирайтер рекламных карточек.

Запрос пользователя:
{user_prompt[:1500]}

Верни только JSON такого вида:
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
2. Если пользователь просит только заменить фон, улучшить свет,
   повернуть предмет или ретушировать фото — make_ad_card=false.
3. Не придумывай цену, скидку, гарантию, технические параметры,
   комплект поставки или другие факты.
4. В features используй только характеристики, явно указанные
   пользователем. Если их нет, верни пустой список.
5. Если пользователь просит рекламу, но данных мало, можно сделать
   нейтральный заголовок без выдуманных характеристик.
6. title — не более 34 символов.
7. subtitle — не более 55 символов.
8. badge — не более 18 символов.
9. features — максимум 3 пункта, каждый до 32 символов.
10. Пиши на русском языке.
11. Не утверждай, что видимые детали или характеристики подтверждены,
    если их нельзя достоверно определить по фото.
12. accent — только один из цветов #D9FF43, #FFFFFF, #65D6FF,
    #FFB86B или #FF7A90.
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
                "temperature": 0.3,
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
        (part.get("text", "") for part in parts if part.get("text")),
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
        "#D9FF43", "#FFFFFF", "#65D6FF", "#FFB86B", "#FF7A90"
    }
    accent = str(data.get("accent", "#D9FF43")).upper()
    if accent not in allowed_accents:
        accent = "#D9FF43"

    return {
        "title": str(data.get("title", ""))[:34],
        "subtitle": str(data.get("subtitle", ""))[:55],
        "badge": str(data.get("badge", ""))[:18],
        "features": [
            str(item)[:32]
            for item in data.get("features", [])
            if isinstance(item, str)
        ][:3],
        "accent": accent
    }


# =========================
# PILLOW: DRAW REAL TEXT
# =========================

def load_font(size, bold=False):
    if bold:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
        ]
    else:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
        ]

    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    return ImageFont.load_default()


def fit_text(draw, text, font, max_width):
    """
    Shorten a line only if it exceeds the available width.
    """
    text = str(text)
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text

    while text:
        candidate = text.rstrip() + "…"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            return candidate
        text = text[:-1]

    return ""


def render_ad_card(image_bytes, ad):
    with Image.open(io.BytesIO(image_bytes)) as source:
        product_image = source.convert("RGB")
        product_image.thumbnail((640, 640))

    width, image_height = product_image.size
    footer_height = 210
    accent = ad.get("accent", "#D9FF43")

    canvas = Image.new(
        "RGB",
        (width, image_height + footer_height),
        "#17191F"
    )
    canvas.paste(product_image, (0, 0))

    draw = ImageDraw.Draw(canvas)

    title_font = load_font(25, bold=True)
    subtitle_font = load_font(15)
    badge_font = load_font(13, bold=True)
    feature_font = load_font(13)

    x = 18
    y = image_height + 12
    max_width = width - 36

    badge = ad.get("badge", "").strip()
    if badge:
        badge_font_height = 25
        badge_text = fit_text(draw, badge, badge_font, max_width - 22)
        text_width = draw.textbbox((0, 0), badge_text, font=badge_font)[2]
        pill_width = min(width - 36, text_width + 22)

        draw.rounded_rectangle(
            (x, y, x + pill_width, y + badge_font_height),
            radius=8,
            fill=accent
        )
        draw.text(
            (x + 11, y + 5),
            badge_text,
            font=badge_font,
            fill="#17191F"
        )
        y += 32

    title = fit_text(
        draw,
        ad.get("title", "").strip(),
        title_font,
        max_width
    )
    if title:
        draw.text((x, y), title, font=title_font, fill="#FFFFFF")
        y += 34

    subtitle = fit_text(
        draw,
        ad.get("subtitle", "").strip(),
        subtitle_font,
        max_width
    )
    if subtitle:
        draw.text(
            (x, y),
            subtitle,
            font=subtitle_font,
            fill="#D2D4DA"
        )
        y += 24

    features = ad.get("features", [])
    if features and y + 20 <= image_height + footer_height - 8:
        feature_text = "  •  ".join(features)
        feature_text = fit_text(
            draw, feature_text, feature_font, max_width
        )
        draw.text(
            (x, y),
            feature_text,
            font=feature_font,
            fill=accent
        )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=92, optimize=True)
    output.seek(0)
    return output


def add_ad_layout_if_requested(image_bytes, user_prompt):
    """
    The Qwen image is always preserved as a fallback.
    A Gemini failure never fails the image-processing job.
    """
    try:
        ad = get_ad_copy(image_bytes, user_prompt)

        if not ad:
            logger.info("Ad layout not requested; keeping Qwen image")
            return io.BytesIO(image_bytes)

        result = render_ad_card(image_bytes, ad)
        logger.info("Gemini ad card rendered successfully")
        return result

    except Exception:
        logger.exception("Gemini ad layout failed; using Qwen image")
        return io.BytesIO(image_bytes)


# =========================
# SEND IMAGE TO TELEGRAM
# =========================

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

    if not response.json().get("ok"):
        raise RuntimeError("Telegram could not send result")


# =========================
# PROCESS USER PHOTO
# =========================

def process_photo(chat_id, file_id, prompt):
    credit_type = None

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

        send_message(
            chat_id,
            "🎨 Принял описание! Обрабатываю фотографию.\n"
            "Это может занять несколько минут."
        )

        image_bytes = download_telegram_photo(file_id)

        if len(image_bytes) > 20 * 1024 * 1024:
            raise RuntimeError("Фото больше 20 МБ")

        with Image.open(io.BytesIO(image_bytes)) as image:
            image.verify()

        # 1. Qwen edits the photograph.
        result = generate_product_image(image_bytes, prompt)
        qwen_bytes = result.getvalue()

        # 2. Gemini adds a designed ad footer only when requested.
        # If Gemini fails, the Qwen image is sent unchanged.
        final_result = add_ad_layout_if_requested(
            qwen_bytes,
            prompt
        )

        send_generated_photo(chat_id, final_result)

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

    except Exception:
        logger.exception("Image processing failed")

        if credit_type:
            try:
                refund_credit(chat_id, credit_type)
            except Exception:
                logger.exception("Credit refund failed")

        send_message(
            chat_id,
            "😕 Не удалось обработать фото. Если обработка "
            "не завершилась, лимит будет восстановлен, "
            "если база данных доступна.\n\nПопробуй позже.",
            main_keyboard()
        )

    finally:
        with state_lock:
            busy_chats.discard(chat_id)


def start_processing(chat_id, file_id, prompt):
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
        daemon=True
    ).start()


# =========================
# MESSAGE HANDLER
# =========================

def handle_update(update):
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

    text = (message.get("text") or "").strip()
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
            "Я могу улучшить фон и освещение, а по запросу — "
            "оформить рекламную карточку с заголовком и плашками.\n\n"
            "Каждому пользователю доступна одна бесплатная "
            "обработка. Дополнительные можно купить за Telegram Stars.\n\n"
            "Пример: «Сделай премиальный тёмный фон, мягкое "
            "студийное освещение и реалистичную тень. "
            "Сам товар не меняй».",
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

    if command == "/balance" or text == "💰 Баланс":
        try:
            balance = get_balance(chat_id)

            if balance["admin"]:
                answer = "👑 У тебя безлимит."
            else:
                free_text = (
                    "доступна"
                    if not balance["free_used"]
                    else "уже использована"
                )

                answer = (
                    f"🎁 Бесплатная обработка: {free_text}.\n"
                    f"⭐ Купленных обработок: {balance['credits']}."
                )

            send_message(chat_id, answer, main_keyboard())

        except Exception:
            logger.exception("Could not read balance")
            send_message(chat_id, "Не удалось загрузить баланс.")

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
            "Примеры описаний:\n\n"
            "• Белый фон и мягкие реалистичные тени.\n"
            "• Премиальная тёмная студия с красивым освещением.\n"
            "• Светлый интерьер и естественный дневной свет.\n"
            "• Сделай продающую рекламную карточку с заголовком "
            "и плашками. Используй только указанные характеристики.\n\n"
            "Отправь своё описание обычным сообщением.",
            main_keyboard()
        )
        return

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
            "Для рекламного оформления напиши: «Сделай "
            "продающую карточку товара с заголовком и плашками».\n\n"
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
            send_message(
                chat_id,
                "Сначала отправь фото товара, затем напиши промпт.",
                main_keyboard()
            )
        return

    send_message(
        chat_id,
        "Отправь фотографию товара, чтобы начать.",
        main_keyboard()
    )


# =========================
# FLASK ROUTES
# =========================

@app.route("/", methods=["GET"])
def home():
    return "TovarPix AI is running!", 200


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
    if not request.is_json:
        return "Expected JSON", 415

    try:
        handle_update(request.get_json())
        return "OK", 200

    except Exception:
        logger.exception("Webhook handler failed")
        return "Internal Server Error", 500


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    if not RENDER_URL:
        return "Render URL is not configured", 500

    try:
        telegram_api(
            "setWebhook",
            {"url": f"{RENDER_URL}/webhook"}
        )
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
            "last_error_message": info.get("last_error_message")
        }), 200

    except Exception:
        logger.exception("Webhook status failed")
        return jsonify({"error": "Telegram API error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

import os
import io
import uuid
import logging
import threading
import tempfile
from pathlib import Path

import requests
import psycopg
from flask import Flask, request, jsonify
from PIL import Image
from magic_hour import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
MAGIC_HOUR_API_KEY = os.environ.get("MAGIC_HOUR_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

ADMIN_IDS = {
    value.strip()
    for value in os.environ.get("ADMIN_IDS", "").split(",")
    if value.strip()
}

PACKAGE_IMAGES = 5
PACKAGE_STARS = 50

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
            "INSERT INTO users (telegram_id) VALUES (%s) "
            "ON CONFLICT (telegram_id) DO NOTHING",
            (chat_id,)
        )


def get_balance(chat_id):
    ensure_user(chat_id)
    with db_connect() as conn:
        row = conn.execute(
            "SELECT free_used, credits FROM users "
            "WHERE telegram_id = %s",
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
            "INSERT INTO users (telegram_id) VALUES (%s) "
            "ON CONFLICT (telegram_id) DO NOTHING",
            (chat_id,)
        )
        row = conn.execute(
            "SELECT free_used, credits FROM users "
            "WHERE telegram_id = %s FOR UPDATE",
            (chat_id,)
        ).fetchone()

        free_used, credits = row

        if not free_used:
            conn.execute(
                "UPDATE users SET free_used = TRUE "
                "WHERE telegram_id = %s",
                (chat_id,)
            )
            return "free"

        if credits > 0:
            conn.execute(
                "UPDATE users SET credits = credits - 1 "
                "WHERE telegram_id = %s",
                (chat_id,)
            )
            return "paid"

        return None


def refund_credit(chat_id, credit_type):
    if credit_type == "admin" or not credit_type:
        return

    with db_connect() as conn:
        if credit_type == "free":
            conn.execute(
                "UPDATE users SET free_used = FALSE "
                "WHERE telegram_id = %s",
                (chat_id,)
            )
        elif credit_type == "paid":
            conn.execute(
                "UPDATE users SET credits = credits + 1 "
                "WHERE telegram_id = %s",
                (chat_id,)
            )


def telegram_api(method, data=None):
    response = http.post(
        f"{API_URL}/{method}",
        json=data or {},
        timeout=35
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))

    return result.get("result")


def send_message(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text}
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
            [{"text": "💡 Пример промпта"}, {"text": "💰 Баланс"}],
            [{"text": "❌ Отмена"}],
            [{"text": "❤️ Катя"}]
        ],
        "resize_keyboard": True
    }


def send_invoice(chat_id):
    payload = f"tovarpix_pack5_{chat_id}_{uuid.uuid4().hex}"
    telegram_api("sendInvoice", {
        "chat_id": chat_id,
        "title": "TovarPix — 5 обработок",
        "description": "Пять дополнительных обработок фотографий товаров",
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": "5 обработок", "amount": PACKAGE_STARS}]
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
        not chat_id or not charge_id
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
            send_message(
                chat_id,
                f"✅ Оплата получена!\n\nНачислено: {PACKAGE_IMAGES} обработок.\n"
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
    prompt = BASE_PROMPT + "\nUSER REQUEST:\n" + user_prompt[:1500]

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / "input.jpg"

        with Image.open(io.BytesIO(image_bytes)) as image:
            image = image.convert("RGB")
            image.save(input_path, format="JPEG", quality=95)

        result = magic_client.v1.ai_image_editor.generate(
            name="TovarPix product photo",
            assets={"image_file_paths": [str(input_path)]},
            style={"prompt": prompt},
            model="qwen-edit",
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


def send_generated_photo(chat_id, image_buffer):
    response = http.post(
        f"{API_URL}/sendPhoto",
        data={
            "chat_id": str(chat_id),
            "caption": "✅ Готово! Вот твоё обработанное фото."
        },
        files={
            "photo": ("tovarpix_result.jpg", image_buffer, "image/jpeg")
        },
        timeout=60
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram could not send result")


def process_photo(chat_id, file_id, prompt):
    credit_type = None
    try:
        credit_type = reserve_credit(chat_id)

        if credit_type is None:
            send_message(
                chat_id,
                "🎁 Бесплатная обработка уже использована, "
                "а купленные обработки закончились.\n\n"
                "Купи пакет из 5 обработок за 50 ⭐, чтобы продолжить.",
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

        result = generate_product_image(image_bytes, prompt)
        send_generated_photo(chat_id, result)
        balance = get_balance(chat_id)

        if balance["admin"]:
            balance_text = "👑 У тебя безлимит."
        else:
            balance_text = f"⭐ Купленных обработок осталось: {balance['credits']}."

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
            "😕 Не удалось обработать фото. Если обработка не завершилась, "
            "лимит будет восстановлен, если база данных доступна.\n\n"
            "Попробуй позже.",
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
                "⏳ Предыдущая фотография ещё обрабатывается. Дождись результата."
            )
            return
        busy_chats.add(chat_id)

    threading.Thread(
        target=process_photo,
        args=(chat_id, file_id, prompt),
        daemon=True
    ).start()


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

    # Шуточная кнопка для Кати ❤️
    if text in ("❤️ Катя", "Катя"):
        send_message(chat_id, "Я ТЕБЯ ЛЮБЛЮ❤️", main_keyboard())
        return

    if command == "/start":
        send_message(
            chat_id,
            "Привет! 👋 Я TovarPix — AI-редактор фото товаров.\n\n"
            "📸 Отправь фотографию товара.\n"
            "✍️ Затем напиши, как её обработать.\n\n"
            "Каждому пользователю доступна одна бесплатная обработка. "
            "Дополнительные можно купить за Telegram Stars.\n\n"
            "Например: «Сделай премиальный тёмный фон, мягкое студийное "
            "освещение и реалистичную тень. Сам товар не меняй».",
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

    if command == "/balance" or text == "💰 Баланс":
        try:
            balance = get_balance(chat_id)
            if balance["admin"]:
                answer = "👑 У тебя безлимит."
            else:
                free_text = "доступна" if not balance["free_used"] else "уже использована"
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
            send_message(chat_id, "Не удалось создать счёт. Попробуй позже.")
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
            "• Удали фон и сделай чистый белый фон.\n\n"
            "Отправь своё описание обычным сообщением.",
            main_keyboard()
        )
        return

    if message.get("photo"):
        with state_lock:
            if chat_id in busy_chats:
                send_message(chat_id, "⏳ Дождись завершения предыдущей обработки.")
                return
            pending_photos[chat_id] = message["photo"][-1]["file_id"]

        send_message(
            chat_id,
            "📸 Фото получил!\n\n"
            "Теперь напиши, что именно нужно сделать с ним.\n"
            "Например: «Сделай белый фон и мягкие тени, не меняя сам товар».\n\n"
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

    send_message(chat_id, "Отправь фотографию товара, чтобы начать.", main_keyboard())


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
        telegram_api("setWebhook", {"url": f"{RENDER_URL}/webhook"})
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
            "last_error_message": info.get("last_error_message")
        }), 200
    except Exception:
        logger.exception("Webhook status failed")
        return jsonify({"error": "Telegram API error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

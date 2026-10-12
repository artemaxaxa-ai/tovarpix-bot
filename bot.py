import os
import io
import logging
import threading
import requests

from flask import Flask, request, jsonify
from huggingface_hub import InferenceClient
from PIL import Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

MODEL = "Qwen/Qwen-Image-Edit"
PROVIDER = "fal-ai"

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

API_URL = f"https://api.telegram.org/bot{TOKEN}"
app = Flask(__name__)

http = requests.Session()


def telegram_api(method, data=None):
    try:
        response = http.post(
            f"{API_URL}/{method}",
            json=data or {},
            timeout=30
        )
        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(
                result.get("description", "Ошибка Telegram API")
            )

        return result.get("result")

    except Exception:
        raise RuntimeError(
            f"Ошибка при обращении к Telegram API: {method}"
        ) from None


def send_message(chat_id, text):
    try:
        telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": text
        })
    except Exception:
        logger.error("Не удалось отправить сообщение в чат %s", chat_id)


def download_telegram_photo(file_id):
    file_info = telegram_api("getFile", {"file_id": file_id})
    file_path = file_info.get("file_path")

    if not file_path:
        raise RuntimeError("Telegram не вернул путь к фотографии")

    # Не выводим URL с токеном Telegram в логи.
    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

    try:
        response = http.get(url, timeout=40)
        response.raise_for_status()
        return response.content
    except Exception:
        raise RuntimeError("Не удалось скачать фотографию") from None


def generate_product_image(image_bytes, user_caption=""):
    if not HF_TOKEN:
        raise RuntimeError("В Render не задан HF_TOKEN")

    prompt = (
        "Edit this product photograph for a professional e-commerce "
        "listing on a marketplace. Preserve the original product's "
        "identity, shape, proportions, color, material, details, "
        "branding, labels and text as accurately as possible. "
        "Do not invent accessories or change the product itself. "
        "Improve the background, lighting, shadows, composition and "
        "overall studio-photo quality. Use a clean, attractive "
        "marketplace-ready presentation. Keep the product clearly "
        "visible and realistic. Do not add text or promotional "
        "graphics unless explicitly requested."
    )

    if user_caption.strip():
        prompt += (
            "\nAdditional user instructions: "
            + user_caption.strip()[:500]
        )

    client = InferenceClient(
        provider=PROVIDER,
        token=HF_TOKEN,
        timeout=180
    )

    result = client.image_to_image(
        image=image_bytes,
        prompt=prompt,
        model=MODEL
    )

    output = io.BytesIO()
    if result.mode not in ("RGB", "L"):
        result = result.convert("RGB")
    elif result.mode == "L":
        result = result.convert("RGB")

    result.save(output, format="JPEG", quality=92)
    output.seek(0)

    return output


def send_generated_photo(chat_id, image_buffer):
    try:
        response = http.post(
            f"{API_URL}/sendPhoto",
            data={"chat_id": str(chat_id)},
            files={
                "photo": ("tovarpix_result.jpg", image_buffer, "image/jpeg")
            },
            timeout=60
        )
        response.raise_for_status()

        if not response.json().get("ok"):
            raise RuntimeError("Telegram не принял готовое изображение")

    except Exception:
        raise RuntimeError("Не удалось отправить результат в Telegram") from None


def process_photo(chat_id, file_id, caption=""):
    try:
        send_message(
            chat_id,
            "🎨 Получил фото! Начинаю обработку нейросетью. "
            "Это может занять несколько минут."
        )

        image_bytes = download_telegram_photo(file_id)

        if len(image_bytes) > 20 * 1024 * 1024:
            send_message(
                chat_id,
                "Фото слишком большое для обработки через Telegram. "
                "Попробуй отправить изображение меньшего размера."
            )
            return

        # Проверяем, что файл действительно является изображением.
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except Exception:
            send_message(chat_id, "Не удалось прочитать изображение. Попробуй другое фото.")
            return

        result = generate_product_image(image_bytes, caption)
        send_generated_photo(chat_id, result)

        logger.info("Изображение успешно обработано")

    except Exception as error:
        # Не записываем токены и URL запросов в лог.
        logger.error(
            "Обработка изображения завершилась ошибкой: %s",
            str(error)[:300]
        )

        send_message(
            chat_id,
            "😕 Не удалось обработать фото.\n\n"
            "Возможные причины: модель временно недоступна, "
            "провайдер не поддерживает запрос, закончился бесплатный "
            "кредит или возникла ошибка API.\n\n"
            "Попробуй немного позже. Если ошибка повторится, "
            "мы проверим логи Render."
        )


def handle_update(update):
    message = update.get("message")

    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    text = message.get("text", "")
    command = text.split("@")[0].strip()

    if command == "/start":
        send_message(
            chat_id,
            "Привет! 👋 Я Генератор Изображений AI!\n\n"
            "Я помогу подготовить фотографии товаров "
            "для Wildberries и Ozon.\n\n"
            "📸 Отправь фотографию товара, чтобы начать!"
        )
        return

    if message.get("photo"):
        # Берём фотографию наибольшего доступного размера.
        photo = message["photo"][-1]
        caption = message.get("caption", "")

        # Обработка в отдельном потоке, чтобы вебхук быстро отвечал Telegram.
        worker = threading.Thread(
            target=process_photo,
            args=(chat_id, photo["file_id"], caption),
            daemon=True
        )
        worker.start()
        return

    send_message(
        chat_id,
        "Отправь /start или фотографию товара 📸"
    )


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
        logger.error("Ошибка получения информации о боте")
        return jsonify({
            "error": "Не удалось получить информацию о боте"
        }), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    if not request.is_json:
        return "Expected JSON", 415

    try:
        update = request.get_json()
        handle_update(update)
        return "OK", 200
    except Exception:
        logger.exception("Ошибка обработки обновления")
        return "Internal Server Error", 500


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    if not RENDER_URL:
        return "Render URL is not configured", 500

    try:
        telegram_api("setWebhook", {
            "url": f"{RENDER_URL}/webhook"
        })
        logger.info("Webhook установлен")
        return "Webhook configured", 200
    except Exception:
        logger.error("Ошибка установки webhook")
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
        logger.error("Ошибка проверки webhook")
        return jsonify({
            "error": "Не удалось проверить webhook"
        }), 500


if __name__ == "__main__":
    logger.info("Запуск TovarPix AI")
    app.run(host="0.0.0.0", port=PORT)

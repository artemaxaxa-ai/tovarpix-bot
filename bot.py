import os
import io
import logging
import threading
import tempfile
from pathlib import Path

import requests
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
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not MAGIC_HOUR_API_KEY:
    raise RuntimeError("Не задан MAGIC_HOUR_API_KEY")

API_URL = f"https://api.telegram.org/bot{TOKEN}"
app = Flask(__name__)
http = requests.Session()

magic_client = Client(token=MAGIC_HOUR_API_KEY)


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

    url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

    try:
        response = http.get(url, timeout=40)
        response.raise_for_status()
        return response.content
    except Exception:
        raise RuntimeError("Не удалось скачать фотографию") from None


def generate_product_image(image_bytes, user_caption=""):
    prompt = (
        "Edit this product photograph for a professional e-commerce "
        "listing on a Russian marketplace such as Wildberries or Ozon. "
        "Preserve the exact identity, shape, proportions, color, "
        "material, details, branding, labels and text of the original "
        "product. Do not replace, redesign or invent the product. "
        "Improve the background, lighting, soft shadows, composition "
        "and studio-photo quality. Make the result realistic, clean "
        "and attractive for a product listing. Keep the product "
        "clearly visible. Do not add text, logos, prices or promotional "
        "graphics. Keep the result square."
    )

    if user_caption.strip():
        prompt += (
            "\nAdditional user instructions: "
            + user_caption.strip()[:500]
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = Path(temp_dir) / "input.jpg"

        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            elif image.mode == "L":
                image = image.convert("RGB")

            image.save(input_path, format="JPEG", quality=95)

        result = magic_client.v1.ai_image_editor.generate(
            name="TovarPix product photo",
            assets={
                "image_file_paths": [str(input_path)]
            },
            style={
                "prompt": prompt
            },
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
            raise RuntimeError(
                f"Обработка не завершена. Статус: {result.status}"
            )

        output_paths = result.downloaded_paths

        if not output_paths:
            raise RuntimeError("Magic Hour не вернул готовое изображение")

        output_path = Path(str(output_paths[0]))

        if not output_path.is_file():
            raise RuntimeError("Не найден файл результата")

        output = io.BytesIO(output_path.read_bytes())
        output.seek(0)
        return output


def send_generated_photo(chat_id, image_buffer):
    try:
        response = http.post(
            f"{API_URL}/sendPhoto",
            data={"chat_id": str(chat_id)},
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
            raise RuntimeError("Telegram не принял готовое изображение")

    except Exception:
        raise RuntimeError(
            "Не удалось отправить результат в Telegram"
        ) from None


def process_photo(chat_id, file_id, caption=""):
    try:
        send_message(
            chat_id,
            "🎨 Получил фото! Начинаю обработку нейросетью. "
            "Обычно это занимает от нескольких секунд до нескольких минут."
        )

        image_bytes = download_telegram_photo(file_id)

        if len(image_bytes) > 20 * 1024 * 1024:
            send_message(
                chat_id,
                "Фото слишком большое. Попробуй отправить изображение "
                "меньшего размера."
            )
            return

        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except Exception:
            send_message(
                chat_id,
                "Не удалось прочитать изображение. Попробуй другое фото."
            )
            return

        result = generate_product_image(image_bytes, caption)
        send_generated_photo(chat_id, result)

        logger.info("Изображение успешно обработано")

    except Exception as error:
        logger.error(
            "Обработка изображения завершилась ошибкой: %s",
            str(error)[:500]
        )

        send_message(
            chat_id,
            "😕 Не удалось обработать фото.\n\n"
            "Возможные причины: закончились кредиты Magic Hour, "
            "временная ошибка сервиса или проблема с изображением.\n\n"
            "Попробуй позже. Если ошибка повторится, проверим логи Render."
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
        photo = message["photo"][-1]
        caption = message.get("caption", "")

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
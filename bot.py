import os
import logging
import requests
from flask import Flask, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

API_URL = f"https://api.telegram.org/bot{TOKEN}"
app = Flask(__name__)


def telegram_api(method, data=None):
    response = requests.post(
        f"{API_URL}/{method}",
        json=data or {},
        timeout=20
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Ошибка Telegram API"))

    return result.get("result")


def send_message(chat_id, text):
    try:
        telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": text
        })
        logger.info("Ответ отправлен в чат %s", chat_id)
    except Exception:
        logger.exception("Не удалось отправить сообщение в чат %s", chat_id)


def handle_update(update):
    message = update.get("message")

    if not message:
        logger.info(
            "Пропущено обновление без обычного сообщения: update_id=%s",
            update.get("update_id")
        )
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if not chat_id:
        logger.warning("У сообщения отсутствует chat_id")
        return

    text = message.get("text", "")

    logger.info(
        "Получено сообщение: update_id=%s, chat_id=%s, type=%s",
        update.get("update_id"),
        chat_id,
        "photo" if message.get("photo") else "text" if text else "other"
    )

    if text.split("@")[0].strip() == "/start":
        send_message(
            chat_id,
            "Привет! 👋 Я Генератор Изображений AI!\n\n"
            "Я помогу подготовить фотографии товаров "
            "для Wildberries и Ozon.\n\n"
            "📸 Отправь фотографию товара, чтобы начать!"
        )
        return

    if message.get("photo"):
        send_message(
            chat_id,
            "Фото получил! 📸\n\n"
            "Пока генерация изображений ещё не подключена."
        )
        return

    send_message(
        chat_id,
        "Сообщение получил! ✅\n\n"
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
        logger.exception("Ошибка получения информации о боте")
        return jsonify({"error": "Не удалось получить информацию о боте"}), 500


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
        result = telegram_api("setWebhook", {
            "url": f"{RENDER_URL}/webhook"
        })
        logger.info("Webhook установлен: %s", result)
        return "Webhook configured", 200
    except Exception:
        logger.exception("Ошибка установки webhook")
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
        logger.exception("Ошибка проверки webhook")
        return jsonify({"error": "Не удалось проверить webhook"}), 500


if __name__ == "__main__":
    logger.info("Запуск TovarPix AI")
    app.run(host="0.0.0.0", port=PORT)
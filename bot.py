import os
import logging
import telebot
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "Привет! 👋 Я Генератор Изображений AI!\n\n"
        "Я помогу подготовить фотографии товаров "
        "для Wildberries и Ozon.\n\n"
        "📸 Отправь фотографию товара, чтобы начать!"
    )


@bot.message_handler(content_types=["photo"])
def receive_photo(message):
    bot.send_message(
        message.chat.id,
        "Фото получил! 📸\n\n"
        "Пока генерация изображений ещё подключается."
    )


@bot.message_handler(func=lambda message: True)
def other_messages(message):
    bot.send_message(
        message.chat.id,
        "Отправь /start или фотографию товара 📸"
    )


@app.route("/", methods=["GET"])
def home():
    return "TovarPix AI is running!", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if not request.is_json:
            return "Expected JSON", 415

        update = telebot.types.Update.de_json(
            request.get_data(as_text=True)
        )
        bot.process_new_updates([update])
        return "OK", 200

    except Exception:
        logger.exception("Ошибка обработки webhook")
        return "Internal error", 500


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    if not RENDER_URL:
        return "Render URL is not configured", 500

    try:
        success = bot.set_webhook(url=f"{RENDER_URL}/webhook")
        if success:
            logger.info("Webhook установлен")
            return "Webhook configured", 200

        return "Webhook setup failed", 500

    except Exception:
        logger.exception("Не удалось установить webhook")
        return "Webhook setup error", 500


@app.route("/webhook_status", methods=["GET"])
def webhook_status():
    try:
        info = bot.get_webhook_info()

        return jsonify({
            "url_configured": bool(info.url),
            "url_matches_render": info.url == f"{RENDER_URL}/webhook",
            "pending_update_count": info.pending_update_count,
            "last_error_date": info.last_error_date,
            "last_error_message": info.last_error_message,
        }), 200

    except Exception:
        logger.exception("Не удалось проверить webhook")
        return jsonify({"error": "Webhook status check failed"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

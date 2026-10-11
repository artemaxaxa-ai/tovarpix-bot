import os
import logging
import telebot
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

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)


@bot.message_handler(commands=["start"])
def start(message):
    logger.info("СРАБОТАЛ ОБРАБОТЧИК START: chat_id=%s", message.chat.id)
    bot.send_message(
        message.chat.id,
        "Привет! 👋 Я Генератор Изображений AI!\n\n"
        "Я помогу подготовить фотографии товаров "
        "для Wildberries и Ozon.\n\n"
        "📸 Отправь фотографию товара, чтобы начать!"
    )


@bot.message_handler(content_types=["photo"])
def receive_photo(message):
    logger.info("СРАБОТАЛ ОБРАБОТЧИК ФОТО: chat_id=%s", message.chat.id)
    bot.send_message(
        message.chat.id,
        "Фото получил! 📸\n\n"
        "Пока генерация изображений ещё не подключена."
    )


@bot.message_handler(func=lambda message: True)
def other_messages(message):
    logger.info(
        "СРАБОТАЛ ОБРАБОТЧИК ТЕКСТА: text=%r chat_id=%s",
        message.text,
        message.chat.id
    )
    bot.send_message(
        message.chat.id,
        "Сообщение получил! ✅\n\n"
        "Отправь /start или фотографию товара 📸"
    )


@app.route("/", methods=["GET"])
def home():
    return "TovarPix AI is running!", 200


@app.route("/bot_info", methods=["GET"])
def bot_info():
    try:
        me = bot.get_me()
        return jsonify({
            "username": me.username,
            "first_name": me.first_name,
            "id": me.id
        }), 200
    except Exception:
        logger.exception("Ошибка получения информации о боте")
        return jsonify({"error": "Не удалось получить информацию о боте"}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        raw_data = request.get_data(as_text=True)
        logger.info("WEBHOOK ПОЛУЧЕН. JSON: %s", raw_data[:2000])

        if not request.is_json:
            logger.warning("Webhook получил запрос не в JSON")
            return "Expected JSON", 415

        update = telebot.types.Update.de_json(raw_data)

        if update.message:
            logger.info(
                "В ОБНОВЛЕНИИ ЕСТЬ MESSAGE: text=%r, chat_id=%s",
                update.message.text,
                update.message.chat.id
            )
        elif update.edited_message:
            logger.info("В ОБНОВЛЕНИИ ЕСТЬ EDITED_MESSAGE")
        elif update.callback_query:
            logger.info("В ОБНОВЛЕНИИ ЕСТЬ CALLBACK_QUERY")
        else:
            logger.info("ДРУГОЙ ТИП ОБНОВЛЕНИЯ: update_id=%s", update.update_id)

        bot.process_new_updates([update])
        logger.info("ОБНОВЛЕНИЕ ПЕРЕДАНО ОБРАБОТЧИКАМ")
        return "OK", 200

    except Exception:
        logger.exception("ОШИБКА ОБРАБОТКИ WEBHOOK")
        return "Internal Server Error", 500


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
        logger.exception("Ошибка установки webhook")
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
            "last_error_message": info.last_error_message
        }), 200
    except Exception:
        logger.exception("Ошибка проверки webhook")
        return jsonify({"error": "Не удалось проверить webhook"}), 500


if __name__ == "__main__":
    logger.info("Запуск TovarPix AI")
    app.run(host="0.0.0.0", port=PORT)
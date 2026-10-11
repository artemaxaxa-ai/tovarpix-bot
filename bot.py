
import os
import logging
import telebot
from flask import Flask, request, jsonify

# Логи
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# Настройки
TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not RENDER_URL:
    logger.warning("RENDER_EXTERNAL_URL пока не задан")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)


# Обработка команды /start
@bot.message_handler(commands=["start"])
def start(message):
    logger.info("Получена команда /start")
    bot.send_message(
        message.chat.id,
        "Привет! 👋 Я Генератор Изображений AI!\n\n"
        "Я помогу подготовить фотографии товаров "
        "для Wildberries и Ozon.\n\n"
        "📸 Отправь фотографию товара, чтобы начать!"
    )


# Обработка фотографий
@bot.message_handler(content_types=["photo"])
def receive_photo(message):
    logger.info("Получена фотография")
    bot.send_message(
        message.chat.id,
        "Фото получил! 📸\n\n"
        "Пока генерация изображений ещё не подключена."
    )


# Обработка остальных сообщений
@bot.message_handler(func=lambda message: True)
def other_messages(message):
    logger.info("Получено обычное сообщение")
    bot.send_message(
        message.chat.id,
        "Отправь /start или фотографию товара 📸"
    )


# Проверка сервиса
@app.route("/", methods=["GET"])
def home():
    return "TovarPix AI is running!", 200


# Telegram присылает сюда обновления
@app.route("/webhook", methods=["POST"])
def webhook():
    if not request.is_json:
        logger.warning("Webhook получил запрос не в JSON")
        return "Expected JSON", 415

    try:
        update = telebot.types.Update.de_json(
            request.get_data(as_text=True)
        )
        logger.info("Получено обновление Telegram")
        bot.process_new_updates([update])
        return "OK", 200

    except Exception:
        logger.exception("Ошибка обработки webhook")
        return "Internal Server Error", 500


# Первичная установка webhook
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


# Диагностика webhook
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
        logger.exception("Ошибка проверки webhook")
        return jsonify({"error": "Не удалось проверить webhook"}), 500


if __name__ == "__main__":
    logger.info("Запуск TovarPix AI")
    app.run(host="0.0.0.0", port=PORT)

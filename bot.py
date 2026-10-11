
import os
import logging
import telebot
from flask import Flask, request

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("BOT_TOKEN")
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
        "Пока обработка фотографий с помощью ИИ "
        "ещё не подключена."
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
    if request.headers.get("content-type", "").startswith("application/json"):
        update = telebot.types.Update.de_json(
            request.get_data().decode("utf-8")
        )
        bot.process_new_updates([update])
        return "OK", 200
    return "Unsupported Media Type", 415


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not webhook_url:
        return "Render URL is not configured", 500

    success = bot.set_webhook(url=f"{webhook_url}/webhook")
    return ("Webhook configured", 200) if success else ("Webhook setup failed", 500)

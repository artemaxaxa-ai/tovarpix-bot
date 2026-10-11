import os
import logging
import telebot

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "Привет! 👋 Я TovarPix AI!\n\n"
        "Я помогу подготовить фотографии товаров "
        "для Wildberries и Ozon.\n\n"
        "📸 Отправь фотографию товара, чтобы начать!"
    )


@bot.message_handler(content_types=["photo"])
def receive_photo(message):
    bot.send_message(
        message.chat.id,
        "Фото получил! 📸\n\n"
        "Скоро здесь появится обработка фотографий "
        "с помощью искусственного интеллекта.\n\n"
        "Пока я нахожусь в разработке."
    )


@bot.message_handler(func=lambda message: True)
def other_messages(message):
    bot.send_message(
        message.chat.id,
        "Отправь /start или фотографию товара 📸"
    )


if __name__ == "__main__":
    logging.info("TovarPix AI запущен")
    bot.infinity_polling(skip_pending=True)
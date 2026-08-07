import os
import threading
import urllib.parse
import requests
import telebot
from bs4 import BeautifulSoup
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

FLIBUSTA_SEARCH = "https://flibusta.is/opds/search?searchType=books&searchTerm="

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напишите название книги или автора, и я найду варианты на Флибусте.")

@bot.message_handler(func=lambda m: True)
def handle_search(message):
    query = message.text.strip()
    if not query:
        return

    bot.send_chat_action(message.chat.id, 'typing')

    try:
        url = FLIBUSTA_SEARCH + urllib.parse.quote(query)
        res = requests.get(url, timeout=15)
        soup = BeautifulSoup(res.content, 'xml')

        entries = soup.find_all('entry')[:5]
        if not entries:
            bot.send_message(message.chat.id, "📚 По вашему запросу ничего не найдено.")
            return

        text = f"🔍 **Результаты поиска «{query}»:**\n\n"
        for i, entry in enumerate(entries, 1):
            title = entry.find('title').text if entry.find('title') else "Без названия"
            author = entry.find('author').find('name').text if entry.find('author') and entry.find('author').find('name') else "Неизвестен"

            links = entry.find_all('link')
            download_link = None
            for link in links:
                if link.get('href') and ('/b/' in link.get('href') or 'fb2' in link.get('type', '')):
                    href = link.get('href')
                    download_link = f"https://flibusta.is{href}" if href.startswith('/') else href
                    break

            text += f"{i}. **{title}** — *{author}*\n"
            if download_link:
                text += f"📥 [Ссылка на книгу]({download_link})\n\n"
            else:
                text += "\n"

        bot.send_message(message.chat.id, text, parse_mode='Markdown', disable_web_page_preview=True)

    except Exception:
        bot.send_message(message.chat.id, "⚠️ Ошибка при обращении к Флибусте. Попробуйте чуть позже.")

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    print("Бот успешно запущен...")
    bot.infinity_polling()

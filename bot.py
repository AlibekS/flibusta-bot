import os
import io
import re
import threading
import urllib.parse
import requests
import telebot
from bs4 import BeautifulSoup
from flask import Flask

# --- Крошечный веб-сервер для Render ---
app = Flask(__name__)

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- Основной код бота ---
TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

FLIBUSTA_SEARCH = "https://flibusta.is/opds/search?searchType=books&searchTerm="
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def extract_book_id(entry):
    for link in entry.find_all('link'):
        href = link.get('href', '')
        match = re.search(r'/b/(\d+)', href)
        if match:
            return match.group(1)
    entry_id = entry.find('id')
    if entry_id:
        match = re.search(r'(\d+)', entry_id.text)
        if match:
            return match.group(1)
    return None

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напишите название книги или автора, и я найду её на Флибусте.")

@bot.message_handler(func=lambda m: True)
def handle_search(message):
    query = message.text.strip()
    if not query:
        return

    bot.send_chat_action(message.chat.id, 'typing')
    
    try:
        url = FLIBUSTA_SEARCH + urllib.parse.quote(query)
        res = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=15)
        soup = BeautifulSoup(res.content, 'xml')
        
        entries = soup.find_all('entry')[:5]
        if not entries:
            bot.send_message(message.chat.id, "📚 По вашему запросу ничего не найдено.")
            return

        text = f"🔍 **Результаты поиска «{query}»:**\n\n"
        markup = telebot.types.InlineKeyboardMarkup()
        row_buttons = []

        for i, entry in enumerate(entries, 1):
            title = entry.find('title').text if entry.find('title') else "Без названия"
            author = entry.find('author').find('name').text if entry.find('author') and entry.find('author').find('name') else "Неизвестен"
            book_id = extract_book_id(entry)
            
            text += f"**{i}. {title}** — *{author}*\n\n"
            
            if book_id:
                # Обрезаем длинный заголовок для кнопки
                short_title = title[:15] + "..." if len(title) > 15 else title
                row_buttons.append(
                    telebot.types.InlineKeyboardButton(f"📖 {i}. {short_title}", callback_data=f"book_{book_id}")
                )

        # Раскладываем кнопки выбора книги
        for btn in row_buttons:
            markup.add(btn)

        bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=markup)

    except Exception as e:
        bot.send_message(message.chat.id, "⚠️ Ошибка при обращении к Флибусте. Попробуйте чуть позже.")

# --- Обработчик нажатия на книгу (выбор формата) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('book_'))
def callback_book_select(call):
    book_id = call.data.split('_')[1]
    
    markup = telebot.types.InlineKeyboardMarkup()
    btn_epub = telebot.types.InlineKeyboardButton("📘 EPUB", callback_data=f"dl_{book_id}_epub")
    btn_fb2 = telebot.types.InlineKeyboardButton("📄 FB2", callback_data=f"dl_{book_id}_fb2")
    btn_mobi = telebot.types.InlineKeyboardButton("📱 MOBI", callback_data=f"dl_{book_id}_mobi")
    
    markup.add(btn_epub, btn_fb2, btn_mobi)
    
    bot.edit_message_text(
        "Выберите формат для скачивания:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

# --- Обработчик скачивания и отправки файла ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_'))
def callback_download(call):
    _, book_id, fmt = call.data.split('_')
    
    bot.answer_callback_query(call.id, f"Скачиваю в формате {fmt.upper()}...")
    bot.send_chat_action(call.message.chat.id, 'upload_document')
    
    url = f"https://flibusta.is/b/{book_id}/{fmt}"
    
    try:
        res = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=30)
        if res.status_code == 200:
            # Определение имени файла из заголовка или генерация
            cd = res.headers.get('content-disposition', '')
            filename = None
            if 'filename=' in cd:
                fname_match = re.findall('filename="?([^";]+)"?', cd)
                if fname_match:
                    filename = fname_match[0]
            
            if not filename:
                filename = f"book_{book_id}.{fmt}"

            # Загрузка файла в память и отправка в чат
            file_data = io.BytesIO(res.content)
            file_data.name = filename
            
            bot.send_document(call.message.chat.id, file_data)
        else:
            bot.send_message(
                call.message.chat.id, 
                f"⚠️ Формат **{fmt.upper()}** недоступен для этой книги или произошла ошибка скачивания."
            )
    except Exception as e:
        bot.send_message(call.message.chat.id, "⚠️ Ошибка при загрузке файла. Попробуйте другой формат.")

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    print("Бот успешно запущен...")
    bot.infinity_polling()

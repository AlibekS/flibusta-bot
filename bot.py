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

FLIBUSTA_SEARCH_OPDS = "https://flibusta.is/opds/search?searchType=books&searchTerm="
FLIBUSTA_SEARCH_WEB = "https://flibusta.is/booksearch?ask="
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

def search_books(query):
    results = []
    
    # 1. Первая попытка: быстрый OPDS поиск
    try:
        url = FLIBUSTA_SEARCH_OPDS + urllib.parse.quote(query)
        res = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=10)
        soup = BeautifulSoup(res.content, 'xml')
        entries = soup.find_all('entry')[:5]
        
        for entry in entries:
            book_id = extract_book_id(entry)
            title = entry.find('title').text if entry.find('title') else "Без названия"
            author = entry.find('author').find('name').text if entry.find('author') and entry.find('author').find('name') else "Неизвестен"
            if book_id:
                results.append({'id': book_id, 'title': title, 'author': author})
    except Exception:
        pass

    # 2. Вторая попытка (если OPDS ничего не нашел): умный веб-поиск сайта
    if not results:
        try:
            url = FLIBUSTA_SEARCH_WEB + urllib.parse.quote(query)
            res = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            
            main_div = soup.find('div', id='main') or soup
            for a in main_div.find_all('a', href=True):
                match = re.search(r'^/b/(\d+)$', a['href'])
                if match:
                    b_id = match.group(1)
                    if not any(b['id'] == b_id for b in results):
                        b_title = a.text.strip()
                        parent_li = a.find_parent('li')
                        b_author = "Неизвестен"
                        if parent_li:
                            author_a = parent_li.find('a', href=re.compile(r'^/a/'))
                            if author_a:
                                b_author = author_a.text.strip()
                        results.append({'id': b_id, 'title': b_title, 'author': b_author})
                        if len(results) >= 5:
                            break
        except Exception:
            pass

    return results

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напишите название книги или автора, и я найду её на Флибусте.")

@bot.message_handler(func=lambda m: True)
def handle_search(message):
    query = message.text.strip()
    if not query:
        return

    bot.send_chat_action(message.chat.id, 'typing')
    books = search_books(query)
    
    if not books:
        bot.send_message(message.chat.id, "📚 По вашему запросу ничего не найдено.")
        return

    text = f"🔍 **Результаты поиска «{query}»:**\n\n"
    markup = telebot.types.InlineKeyboardMarkup()
    row_buttons = []

    for i, book in enumerate(books, 1):
        text += f"**{i}. {book['title']}** — *{book['author']}*\n\n"
        short_title = book['title'][:15] + "..." if len(book['title']) > 15 else book['title']
        row_buttons.append(
            telebot.types.InlineKeyboardButton(f"📖 {i}. {short_title}", callback_data=f"book_{book['id']}")
        )

    for btn in row_buttons:
        markup.add(btn)

    bot.send_message(message.chat.id, text, parse_mode='Markdown', reply_markup=markup)

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

@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_'))
def callback_download(call):
    _, book_id, fmt = call.data.split('_')
    
    bot.answer_callback_query(call.id, f"Скачиваю в формате {fmt.upper()}...")
    bot.send_chat_action(call.message.chat.id, 'upload_document')
    
    url = f"https://flibusta.is/b/{book_id}/{fmt}"
    
    try:
        res = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=30)
        if res.status_code == 200:
            cd = res.headers.get('content-disposition', '')
            filename = None
            if 'filename=' in cd:
                fname_match = re.findall('filename="?([^";]+)"?', cd)
                if fname_match:
                    filename = fname_match[0]
            
            if not filename:
                filename = f"book_{book_id}.{fmt}"

            file_data = io.BytesIO(res.content)
            file_data.name = filename
            
            bot.send_document(call.message.chat.id, file_data)
        else:
            bot.send_message(
                call.message.chat.id, 
                f"⚠️ Формат **{fmt.upper()}** недоступен для этой книги или произошла ошибка скачивания."
            )
    except Exception:
        bot.send_message(call.message.chat.id, "⚠️ Ошибка при загрузке файла. Попробуйте другой формат.")

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    print("Бот успешно запущен...")
    bot.infinity_polling()

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

FLIBUSTA_BASE = "https://flibusta.is"
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

def fetch_opds_books(search_term):
    """Поиск книг по OPDS"""
    found = []
    try:
        url = f"{FLIBUSTA_BASE}/opds/search?searchType=books&searchTerm=" + urllib.parse.quote(search_term)
        res = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=10)
        soup = BeautifulSoup(res.content, 'xml')
        for entry in soup.find_all('entry'):
            b_id = extract_book_id(entry)
            b_title = entry.find('title').text if entry.find('title') else "Без названия"
            author_elem = entry.find('author')
            b_author = author_elem.find('name').text if author_elem and author_elem.find('name') else "Неизвестен"
            if b_id:
                found.append({'id': b_id, 'title': b_title, 'author': b_author})
    except Exception:
        pass
    return found

def fetch_author_books_opds(author_name, title_keywords):
    """Ищет автора по OPDS, берет его книги и фильтрует по ключевым словам из названия"""
    found = []
    try:
        # 1. Ищем автора
        search_url = f"{FLIBUSTA_BASE}/opds/search?searchType=authors&searchTerm=" + urllib.parse.quote(author_name)
        res = requests.get(search_url, headers={'User-Agent': USER_AGENT}, timeout=10)
        soup = BeautifulSoup(res.content, 'xml')
        
        author_href = None
        author_real_name = author_name

        for entry in soup.find_all('entry'):
            link = entry.find('link', href=re.compile(r'/opds/a/\d+'))
            if link:
                author_href = link.get('href')
                if entry.find('title'):
                    author_real_name = entry.find('title').text
                break

        # 2. Если автор найден, загружаем список его книг
        if author_href:
            author_books_url = FLIBUSTA_BASE + author_href
            res_books = requests.get(author_books_url, headers={'User-Agent': USER_AGENT}, timeout=10)
            soup_books = BeautifulSoup(res_books.content, 'xml')

            for entry in soup_books.find_all('entry'):
                b_id = extract_book_id(entry)
                b_title = entry.find('title').text if entry.find('title') else ""
                
                # Проверяем, содержатся ли ключевые слова в названии книги
                title_lower = b_title.lower()
                if b_id and any(kw.lower() in title_lower for kw in title_keywords):
                    found.append({
                        'id': b_id, 
                        'title': b_title, 
                        'author': author_real_name
                    })
    except Exception:
        pass
    return found

def search_books(query):
    query = query.strip()
    results = []
    seen_ids = set()

    def add_result(book_id, title, author):
        if book_id and book_id not in seen_ids:
            seen_ids.add(book_id)
            results.append({'id': book_id, 'title': title, 'author': author})

    # 1. Прямой поиск книг по OPDS
    for b in fetch_opds_books(query):
        add_result(b['id'], b['title'], b['author'])
        if len(results) >= 5:
            return results

    # 2. Прямой веб-поиск на сайте
    try:
        url = f"{FLIBUSTA_BASE}/booksearch?ask=" + urllib.parse.quote(query)
        res = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        main_div = soup.find('div', id='main') or soup
        for a in main_div.find_all('a', href=True):
            match = re.search(r'/b/(\d+)', a['href'])
            if match:
                b_id = match.group(1)
                b_title = a.text.strip()
                parent_li = a.find_parent('li')
                b_author = "Неизвестен"
                if parent_li:
                    author_a = parent_li.find('a', href=re.compile(r'/a/\d+'))
                    if author_a:
                        b_author = author_a.text.strip()
                if b_title and not b_title.lower().startswith(("скачать", "читать")):
                    add_result(b_id, b_title, b_author)
                    if len(results) >= 5:
                        return results
    except Exception:
        pass

    # 3. Поиск через Автора (решает проблему запросов "Рэй Далио Принципы")
    words = [w for w in re.split(r'\s+', query) if len(w) > 1]
    if len(words) > 1:
        # Пробуем первые N слов считать именем автора, а остальные — названием
        for i in range(len(words) - 1, 0, -1):
            author_candidate = " ".join(words[:i])
            title_keywords = words[i:]
            
            author_results = fetch_author_books_opds(author_candidate, title_keywords)
            for b in author_results:
                add_result(b['id'], b['title'], b['author'])
                if len(results) >= 5:
                    return results

    # 4. Умный поиск (Smart Fallback по одиночным словам)
    if not results and len(words) > 1:
        query_words_lower = [w.lower() for w in words]
        sorted_words = sorted(words, key=lambda x: len(x), reverse=True)
        
        for search_word in sorted_words:
            candidate_books = fetch_opds_books(search_word)
            scored_candidates = []
            
            for b in candidate_books:
                full_text_lower = f"{b['title']} {b['author']}".lower()
                match_count = sum(1 for qw in query_words_lower if qw in full_text_lower)
                if match_count > 1:
                    scored_candidates.append((match_count, b))
            
            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            for _, b in scored_candidates:
                add_result(b['id'], b['title'], b['author'])
                if len(results) >= 5:
                    return results

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
    
    url = f"{FLIBUSTA_BASE}/b/{book_id}/{fmt}"
    
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

import os
import io
import re
import html
import logging
import threading
import urllib.parse

import requests
import telebot
from bs4 import BeautifulSoup
from flask import Flask

import mirrors
from mirrors import MirrorUnavailable

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# --- Крошечный веб-сервер для Render ---
app = Flask(__name__)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return f"Bot is running! Mirror: {flibusta.base}"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# --- Основной код бота ---
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)
flibusta = mirrors.from_env()

MAX_RESULTS = 5
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # боты не могут отправлять файлы больше 50 МБ


def is_opds(res):
    return mirrors.looks_like_opds(res.text)


def get_xml(path):
    """OPDS-запрос через текущее зеркало. Возвращает soup или None."""
    res = flibusta.get(path, validate=is_opds)
    if res.status_code != 200:
        return None
    return BeautifulSoup(res.content, 'xml')


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
        soup = get_xml("/opds/search?searchType=books&searchTerm=" + urllib.parse.quote(search_term))
        if soup is None:
            return found
        for entry in soup.find_all('entry'):
            b_id = extract_book_id(entry)
            b_title = entry.find('title').text if entry.find('title') else "Без названия"
            author_elem = entry.find('author')
            b_author = author_elem.find('name').text if author_elem and author_elem.find('name') else "Неизвестен"
            if b_id:
                found.append({'id': b_id, 'title': b_title, 'author': b_author})
    except requests.RequestException as e:
        log.warning("OPDS-поиск книг '%s' не удался: %s", search_term, e)
    return found


def fetch_author_books_opds(author_name, title_keywords):
    """Ищет автора по OPDS, берет его книги и фильтрует по ключевым словам из названия"""
    found = []
    try:
        # 1. Ищем автора
        soup = get_xml("/opds/search?searchType=authors&searchTerm=" + urllib.parse.quote(author_name))
        if soup is None:
            return found

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
            # href может быть как относительным, так и абсолютным (со старым доменом),
            # поэтому берём только путь и запрашиваем через текущее зеркало
            soup_books = get_xml(urllib.parse.urlsplit(author_href).path)
            if soup_books is None:
                return found

            for entry in soup_books.find_all('entry'):
                b_id = extract_book_id(entry)
                b_title = entry.find('title').text if entry.find('title') else ""

                # Проверяем, содержатся ли ключевые слова в названии книги
                title_lower = b_title.lower()
                if b_id and any(kw.lower() in title_lower for kw in title_keywords):
                    found.append({'id': b_id, 'title': b_title, 'author': author_real_name})
    except requests.RequestException as e:
        log.warning("Поиск по автору '%s' не удался: %s", author_name, e)
    return found


def fetch_web_search(query):
    """Прямой веб-поиск на сайте (HTML)"""
    found = []
    try:
        res = flibusta.get("/booksearch?ask=" + urllib.parse.quote(query))
        if res.status_code != 200:
            return found
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
                    found.append({'id': b_id, 'title': b_title, 'author': b_author})
    except requests.RequestException as e:
        log.warning("Веб-поиск '%s' не удался: %s", query, e)
    return found


def search_books(query):
    query = query.strip()
    results = []
    seen_ids = set()

    def add_all(books):
        """Добавляет книги без дублей. True — если набрали нужное количество."""
        for b in books:
            if b['id'] not in seen_ids:
                seen_ids.add(b['id'])
                results.append(b)
                if len(results) >= MAX_RESULTS:
                    return True
        return False

    # 1. Прямой поиск книг по OPDS
    if add_all(fetch_opds_books(query)):
        return results

    # 2. Прямой веб-поиск на сайте
    if add_all(fetch_web_search(query)):
        return results

    # 3. Поиск через Автора (решает проблему запросов "Рэй Далио Принципы")
    words = [w for w in re.split(r'\s+', query) if len(w) > 1]
    if len(words) > 1:
        # Пробуем первые N слов считать именем автора, а остальные — названием
        for i in range(len(words) - 1, 0, -1):
            if add_all(fetch_author_books_opds(" ".join(words[:i]), words[i:])):
                return results

    # 4. Умный поиск (Smart Fallback по одиночным словам)
    if not results and len(words) > 1:
        query_words_lower = [w.lower() for w in words]
        for search_word in sorted(words, key=len, reverse=True):
            scored = []
            for b in fetch_opds_books(search_word):
                full_text_lower = f"{b['title']} {b['author']}".lower()
                match_count = sum(1 for qw in query_words_lower if qw in full_text_lower)
                if match_count > 1:
                    scored.append((match_count, b))
            scored.sort(key=lambda x: x[0], reverse=True)
            if add_all(b for _, b in scored):
                return results

    return results


UNAVAILABLE_TEXT = "😔 Библиотека сейчас недоступна. Попробуйте, пожалуйста, чуть позже."


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "👋 Привет! Напишите название книги или автора, и я найду её на Флибусте.")


@bot.message_handler(content_types=['text'])
def handle_search(message):
    query = message.text.strip()
    if not query or query.startswith('/'):
        return

    bot.send_chat_action(message.chat.id, 'typing')
    try:
        books = search_books(query)
    except MirrorUnavailable:
        bot.send_message(message.chat.id, UNAVAILABLE_TEXT)
        return
    except Exception:
        log.exception("Ошибка поиска '%s'", query)
        bot.send_message(message.chat.id, "⚠️ Что-то пошло не так. Попробуйте ещё раз.")
        return

    if not books:
        bot.send_message(message.chat.id, "📚 По вашему запросу ничего не найдено.")
        return

    # HTML + экранирование: в названиях книг часто встречаются *, _, [ и т.п.,
    # которые ломают Markdown и приводят к ошибке отправки сообщения.
    text = f"🔍 <b>Результаты поиска «{html.escape(query)}»:</b>\n\n"
    markup = telebot.types.InlineKeyboardMarkup()

    for i, book in enumerate(books, 1):
        text += f"<b>{i}. {html.escape(book['title'])}</b> — <i>{html.escape(book['author'])}</i>\n\n"
        short_title = book['title'][:25] + "…" if len(book['title']) > 25 else book['title']
        markup.add(
            telebot.types.InlineKeyboardButton(f"📖 {i}. {short_title}", callback_data=f"book_{book['id']}")
        )

    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('book_'))
def callback_book_select(call):
    book_id = call.data.split('_')[1]
    bot.answer_callback_query(call.id)

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("📘 EPUB", callback_data=f"dl_{book_id}_epub"),
        telebot.types.InlineKeyboardButton("📄 FB2", callback_data=f"dl_{book_id}_fb2"),
        telebot.types.InlineKeyboardButton("📱 MOBI", callback_data=f"dl_{book_id}_mobi"),
    )

    # Отправляем новым сообщением, чтобы не затирать список результатов —
    # так пользователь может вернуться и выбрать другую книгу.
    bot.send_message(call.message.chat.id, "Выберите формат для скачивания:", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('dl_'))
def callback_download(call):
    _, book_id, fmt = call.data.split('_')

    bot.answer_callback_query(call.id, f"Скачиваю в формате {fmt.upper()}...")
    bot.send_chat_action(call.message.chat.id, 'upload_document')

    def is_book_file(res):
        # Вместо файла может прийти HTML-страница (заглушка провайдера, капча)
        return 'text/html' not in res.headers.get('content-type', '')

    try:
        res = flibusta.get(f"/b/{book_id}/{fmt}", validate=is_book_file, timeout=60)
        if res.status_code != 200 or not is_book_file(res):
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Формат <b>{fmt.upper()}</b> недоступен для этой книги. Попробуйте другой формат.",
                parse_mode='HTML',
            )
            return

        if len(res.content) > TELEGRAM_FILE_LIMIT:
            bot.send_message(
                call.message.chat.id,
                "⚠️ Файл слишком большой для Telegram (больше 50 МБ). Попробуйте другой формат.",
            )
            return

        filename = None
        cd = res.headers.get('content-disposition', '')
        fname_match = re.findall(r'filename="?([^";]+)"?', cd)
        if fname_match:
            # Флибуста отдаёт имя в latin-1, восстанавливаем кириллицу
            try:
                filename = fname_match[0].encode('latin-1').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                filename = fname_match[0]
        if not filename:
            filename = f"book_{book_id}.{fmt}"

        file_data = io.BytesIO(res.content)
        file_data.name = filename
        bot.send_document(call.message.chat.id, file_data)
    except MirrorUnavailable:
        bot.send_message(call.message.chat.id, UNAVAILABLE_TEXT)
    except Exception:
        log.exception("Ошибка скачивания книги %s (%s)", book_id, fmt)
        bot.send_message(call.message.chat.id, "⚠️ Ошибка при загрузке файла. Попробуйте другой формат.")


if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    try:
        flibusta.ensure_healthy()
    except MirrorUnavailable:
        log.error("При старте ни одно зеркало не отвечает, буду пробовать дальше")
    flibusta.start_background_check()
    log.info("Бот успешно запущен, зеркало: %s", flibusta.base)
    bot.infinity_polling()

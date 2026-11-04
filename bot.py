import os
import io
import re
import html
import logging
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

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

# По умолчанию у telebot всего 2 потока-обработчика: третий пользователь ждал,
# пока закончатся чужие поиски. Поднимаем до 8.
bot = telebot.TeleBot(TOKEN, num_threads=int(os.environ.get("BOT_THREADS", "8")))
flibusta = mirrors.from_env()

MAX_RESULTS = 5
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # боты не могут отправлять файлы больше 50 МБ

# Общий пул для параллельных запросов к Флибусте во время поиска
search_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="search")


class TTLCache:
    """Потокобезопасный LRU-кэш с временем жизни записей."""

    def __init__(self, maxsize=500, ttl=3600):
        self.maxsize, self.ttl = maxsize, ttl
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, expires = item
            if expires < time.time():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (value, time.time() + self.ttl)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


# Результаты поиска: повторный запрос того же — мгновенно
search_cache = TTLCache(maxsize=1000, ttl=int(os.environ.get("SEARCH_CACHE_TTL", "3600")))
# file_id уже отправленных файлов: Telegram хранит их у себя, повторная
# отправка той же книги не требует скачивания с Флибусты вообще
file_id_cache = TTLCache(maxsize=5000, ttl=30 * 24 * 3600)


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
    key = re.sub(r'\s+', ' ', query.lower())
    cached = search_cache.get(key)
    if cached is not None:
        return cached
    results = _search_books(query)
    if results:  # пустой результат не кэшируем — вдруг это был сбой
        search_cache.set(key, results)
    return results


def _search_books(query):
    """Поиск в несколько стратегий. Независимые запросы выполняются
    параллельно, а результаты объединяются в порядке приоритета стратегий,
    поэтому выдача такая же, как при последовательном поиске, но время
    ответа — как у самого медленного запроса, а не сумма всех."""
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

    words = [w for w in re.split(r'\s+', query) if len(w) > 1]

    # Этапы 1–3 запускаем одновременно (в порядке приоритета):
    # 1. Прямой поиск книг по OPDS
    # 2. Прямой веб-поиск на сайте
    # 3. Поиск через автора (решает проблему запросов "Рэй Далио Принципы"):
    #    пробуем первые N слов считать именем автора, а остальные — названием
    futures = [
        search_pool.submit(fetch_opds_books, query),
        search_pool.submit(fetch_web_search, query),
    ]
    if len(words) > 1:
        # Ограничиваем число вариантов, чтобы длинный запрос не породил десятки запросов
        for i in range(len(words) - 1, max(0, len(words) - 4), -1):
            futures.append(search_pool.submit(fetch_author_books_opds, " ".join(words[:i]), words[i:]))

    for fut in futures:
        if add_all(fut.result()):
            return results

    # 4. Умный поиск (Smart Fallback по одиночным словам) — тоже параллельно
    if not results and len(words) > 1:
        query_words_lower = [w.lower() for w in words]
        sorted_words = sorted(words, key=len, reverse=True)[:4]
        word_futures = [search_pool.submit(fetch_opds_books, w) for w in sorted_words]
        for fut in word_futures:
            scored = []
            for b in fut.result():
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

    cached_id = file_id_cache.get((book_id, fmt))
    if cached_id:
        try:
            bot.send_document(call.message.chat.id, cached_id)
            return
        except telebot.apihelper.ApiTelegramException:
            pass  # file_id протух — скачаем заново

    def is_book_file(res):
        # Вместо файла может прийти HTML-страница (заглушка провайдера, капча)
        return 'text/html' not in res.headers.get('content-type', '')

    try:
        res = flibusta.get(f"/b/{book_id}/{fmt}", validate=is_book_file, timeout=90)
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
        sent = bot.send_document(call.message.chat.id, file_data)
        if sent.document:
            file_id_cache.set((book_id, fmt), sent.document.file_id)
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
    log.info("Бот успешно запущен, зеркало: %s, Tor: %s", flibusta.base,
             "включён" if flibusta.tor_proxy else "выключен")
    bot.infinity_polling()

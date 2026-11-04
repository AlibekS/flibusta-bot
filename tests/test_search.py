"""Параллельный поиск и кэш на фейковой сессии."""
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ["MIRRORS_URL"] = ""
os.environ["MIRROR_STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "s.json")
import bot  # noqa: E402

DELAY = 0.3


def feed(entries):
    body = "".join(
        f'<entry><title>{t}</title><author><name>{a}</name></author>'
        f'<link href="/b/{i}/fb2"/></entry>' for i, t, a in entries)
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="x">{body}</feed>'


class Resp:
    def __init__(self, url, text, status=200):
        self.url, self.text, self.status_code = url, text, status
        self.content = text.encode()
        self.headers = {"content-type": "application/atom+xml"}


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.count = 0
        self.lock = threading.Lock()

    def get(self, url, **kw):
        with self.lock:
            self.count += 1
        time.sleep(DELAY)
        q = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qs(q.query)
        if q.path == "/opds/search" and params.get("searchType") == ["authors"]:
            if params["searchTerm"] == ["Рэй Далио"]:
                return Resp(url, feed([]).replace(
                    "</feed>", '<entry><title>Рэй Далио</title><link href="/opds/a/42"/></entry></feed>'))
            return Resp(url, feed([]))
        if q.path == "/opds/a/42":
            return Resp(url, feed([(7, "Принципы", "Рэй Далио"), (8, "Другая книга", "Рэй Далио")]))
        if q.path in ("/opds/search", "/opds"):
            return Resp(url, feed([]))
        return Resp(url, "<html></html>")


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        bot.flibusta.session = self.s
        bot.search_cache = bot.TTLCache()

    def test_parallel_author_search(self):
        t = time.time()
        res = bot.search_books("Рэй Далио Принципы")
        elapsed = time.time() - t
        self.assertEqual([b["id"] for b in res], ["7"])
        # Последовательно было бы >= 5 запросов * DELAY; параллельно — ~2 * DELAY
        # (поиск автора + список его книг) + fallback по словам ещё ~DELAY.
        self.assertLess(elapsed, 4 * DELAY)

    def test_cache(self):
        bot.search_books("Рэй Далио Принципы")
        n = self.s.count
        t = time.time()
        res = bot.search_books("  рэй   далио ПРИНЦИПЫ ")
        self.assertLess(time.time() - t, 0.05)
        self.assertEqual(self.s.count, n)
        self.assertEqual(res[0]["id"], "7")

    def test_ttl_cache_expiry_and_lru(self):
        c = bot.TTLCache(maxsize=2, ttl=0.1)
        c.set("a", 1); c.set("b", 2); c.get("a"); c.set("c", 3)
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("a"), 1)
        time.sleep(0.15)
        self.assertIsNone(c.get("a"))


if __name__ == "__main__":
    unittest.main()

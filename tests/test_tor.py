"""Тесты Tor-резерва на подменённой HTTP-сессии (без настоящего Tor)."""
import os
import sys
import tempfile
import threading
import time
import unittest

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mirrors import MirrorManager, MirrorUnavailable  # noqa: E402

OPDS = '<feed xmlns:opds="http://opds-spec.org"><title>x</title></feed>'
ONION = "http://flibustaexample.onion"
TOR = "socks5h://127.0.0.1:9050"


class FakeResponse:
    def __init__(self, url, text=OPDS, status=200):
        self.url, self.text, self.status_code = url, text, status
        self.content = text.encode()
        self.headers = {}


class FakeSession:
    """alive: множество живых базовых адресов; delays: искусственная задержка."""

    def __init__(self, alive, delays=None):
        self.alive, self.delays = set(alive), delays or {}
        self.headers = {}
        self.calls = []
        self.lock = threading.Lock()

    def get(self, url, **kw):
        base = "/".join(url.split("/")[:3])
        with self.lock:
            self.calls.append((base, kw.get("proxies"), kw.get("timeout")))
        time.sleep(self.delays.get(base, 0))
        if base not in self.alive:
            raise requests.ConnectionError("dead")
        return FakeResponse(url)


class HalfAliveSession(FakeSession):
    """Главная /opds отвечает мгновенно, а поиск висит (как flibusta.is в логах)."""

    def __init__(self, half_alive, alive):
        super().__init__(alive)
        self.half_alive = half_alive

    def get(self, url, **kw):
        base = "/".join(url.split("/")[:3])
        if base == self.half_alive:
            with self.lock:
                self.calls.append((base, kw.get("proxies"), kw.get("timeout")))
            if "/opds/search" in url:
                raise requests.ReadTimeout("read timed out")
            return FakeResponse(url)
        return super().get(url, **kw)


class TorTests(unittest.TestCase):
    def mm(self, session, **kw):
        state = os.path.join(tempfile.mkdtemp(), "s.json")
        return MirrorManager(
            mirrors=["https://a.example", "https://b.example"],
            onion_mirrors=[ONION], state_file=state, session=session, **kw)

    def test_onion_ignored_without_tor(self):
        m = self.mm(FakeSession({ONION}))
        self.assertNotIn(ONION, m.candidates())
        with self.assertRaises(MirrorUnavailable):
            m.get("/opds")

    def test_onion_is_last_resort_and_uses_proxy(self):
        s = FakeSession({ONION})
        m = self.mm(s, tor_proxy=TOR)
        self.assertEqual(m.candidates()[-1], ONION)
        m.get("/b/1/epub")
        self.assertEqual(m.base, ONION)
        self.assertTrue(m.on_tor)
        onion_calls = [c for c in s.calls if c[0] == ONION]
        self.assertTrue(onion_calls)
        for _, proxies, timeout in onion_calls:
            self.assertEqual(proxies, {"http": TOR, "https": TOR})
            self.assertGreaterEqual(timeout, 45)
        # обычные зеркала — без прокси
        self.assertTrue(all(p is None for b, p, _ in s.calls if b != ONION))

    def test_slow_onion_does_not_delay_clearnet(self):
        s = FakeSession({"https://b.example", ONION}, delays={ONION: 3})
        m = self.mm(s, tor_proxy=TOR)
        t = time.time()
        m.get("/opds")
        self.assertEqual(m.base, "https://b.example")
        self.assertLess(time.time() - t, 1.5)

    def test_half_alive_mirror_is_replaced(self):
        s = HalfAliveSession("https://a.example", {"https://b.example"})
        m = self.mm(s)
        r = m.get("/opds/search?searchType=books&searchTerm=x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(m.base, "https://b.example")

    def test_failed_mirror_demoted_but_used_if_only_one(self):
        """Если «подведшее» зеркало — единственное живое, всё равно его берём."""
        s = FakeSession({"https://a.example"})
        m = self.mm(s)
        self.assertEqual(m.failover(failed_base="https://a.example"), "https://a.example")

    def test_fastest_mirror_wins(self):
        s = FakeSession({"https://a.example", "https://b.example"},
                        delays={"https://a.example": 1.0})
        m = self.mm(s)
        m._current = "https://dead.example"
        m.failover(failed_base="https://dead.example")
        self.assertEqual(m.base, "https://b.example")

    def test_returns_from_tor_when_clearnet_recovers(self):
        s = FakeSession({ONION})
        m = self.mm(s, tor_proxy=TOR)
        m.get("/opds")
        self.assertTrue(m.on_tor)
        self.assertFalse(m.try_leave_tor())  # обычные всё ещё лежат
        s.alive.add("https://a.example")
        self.assertTrue(m.try_leave_tor())
        self.assertEqual(m.base, "https://a.example")


if __name__ == "__main__":
    unittest.main()

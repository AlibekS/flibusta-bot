"""Тесты переключения зеркал на локальных фейковых серверах.
Запуск: python -m unittest discover tests
"""
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mirrors import MirrorManager, MirrorUnavailable  # noqa: E402

OPDS = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog"><title>Flibusta</title></feed>'


def make_server(mode):
    """mode: 'ok' | 'stub' (заглушка провайдера) | ('redirect', url)"""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            m = server.mode
            if isinstance(m, tuple):
                self.send_response(301)
                self.send_header("Location", m[1] + self.path)
                self.end_headers()
                return
            if m == "stub":
                body, ctype = b"<html>Access denied by ISP</html>", "text/html"
            elif self.path.startswith("/opds"):
                body, ctype = OPDS, "application/atom+xml"
            else:
                body, ctype = b"BOOK", "application/epub+zip"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), H)
    server.mode = mode
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.state = os.path.join(tempfile.mkdtemp(), "state.json")

    def mm(self, mirrors, **kw):
        kw.setdefault("probe_timeout", 2)
        return MirrorManager(mirrors=mirrors, state_file=self.state, **kw)

    def test_failover_on_dead_mirror(self):
        _, good = make_server("ok")
        m = self.mm([dead_url(), good])
        r = m.get("/b/1/epub")
        self.assertEqual(r.content, b"BOOK")
        self.assertEqual(m.base, good)

    def test_failover_on_isp_stub(self):
        _, stub = make_server("stub")
        _, good = make_server("ok")
        m = self.mm([stub, good])
        r = m.get("/opds/search?x=1", validate=lambda r: b"<feed" in r.content)
        self.assertIn(b"<feed", r.content)
        self.assertEqual(m.base, good)

    def test_follows_domain_move(self):
        _, good = make_server("ok")
        _, old = make_server(("redirect", good))
        m = self.mm([old])
        m.ensure_healthy()
        self.assertEqual(m.base, good)

    def test_state_persisted(self):
        _, good = make_server("ok")
        self.mm([dead_url(), good]).ensure_healthy()
        m2 = self.mm([dead_url()])
        self.assertEqual(m2.base, good)

    def test_mirror_dies_at_runtime(self):
        s1, first = make_server("ok")
        _, second = make_server("ok")
        m = self.mm([first, second])
        self.assertEqual(m.get("/b/1/fb2").content, b"BOOK")
        self.assertEqual(m.base, first)
        s1.mode = "stub"  # первое зеркало заблокировали
        r = m.get("/opds/x", validate=lambda r: b"<feed" in r.content)
        self.assertIn(b"<feed", r.content)
        self.assertEqual(m.base, second)

    def test_all_dead_fails_fast(self):
        m = self.mm([dead_url(), dead_url()])
        with self.assertRaises(MirrorUnavailable):
            m.get("/opds")
        with self.assertRaises(MirrorUnavailable):  # кулдаун — без ожидания
            m.get("/opds")

    def test_404_is_not_failover(self):
        s, good = make_server("ok")
        _, other = make_server("ok")
        m = self.mm([good, other])
        # HTML вместо книги (формат недоступен), но зеркало живое -> остаёмся
        s.mode = "ok"
        r = m.get("/b/1/mobi", validate=lambda r: False)
        self.assertEqual(m.base, good)
        self.assertEqual(r.status_code, 200)

    def test_remote_list(self):
        """Рабочее зеркало есть только в удалённом mirrors.txt."""
        _, good = make_server("ok")

        class ListHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = f"# comment\n{good}\n".encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), ListHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        m = self.mm([dead_url()], remote_list_url=f"http://127.0.0.1:{srv.server_port}/mirrors.txt")
        m.get("/b/1/epub")
        self.assertEqual(m.base, good)


if __name__ == "__main__":
    unittest.main()

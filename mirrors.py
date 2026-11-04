"""
Автоматический выбор рабочего зеркала Флибусты.

Как это работает:
  * Есть список кандидатов (переменная окружения FLIBUSTA_MIRRORS, удалённый
    список MIRRORS_URL, последнее рабочее зеркало из файла состояния и
    встроенный список по умолчанию).
  * Все запросы идут через MirrorManager.get(). Если текущее зеркало не
    отвечает (таймаут, ошибка соединения, 5xx, заглушка провайдера вместо
    OPDS), менеджер параллельно проверяет всех кандидатов, переключается на
    рабочее и прозрачно повторяет запрос. Пользователь ничего не замечает.
  * Фоновый поток периодически проверяет текущее зеркало, поэтому чаще всего
    переключение происходит ещё до того, как кто-то из пользователей
    наткнётся на мёртвую ссылку.
  * Если сайт отвечает редиректом на новый домен, бот запоминает новый домен.
  * Tor: если задан TOR_PROXY (например socks5h://127.0.0.1:9050), в список
    кандидатов добавляются .onion-зеркала с самым низким приоритетом. Они
    используются только когда все обычные зеркала недоступны, а фоновая
    проверка возвращает бота на обычное зеркало, как только оно оживёт.
"""

import json
import logging
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("mirrors")

DEFAULT_MIRRORS = [
    "https://flibusta.is",
    "https://flibusta.site",
    "https://flisland.net",
    "https://flibusta.net",
]

# Официальный onion-адрес Флибусты (v3). Доступен только через Tor.
DEFAULT_ONION_MIRRORS = [
    "http://flibustaongezhld6dibs2dps6vm4nvqg2kp7vgowbu76tzopgnhazqd.onion",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Коды, при которых считаем, что проблема в зеркале, а не в запросе
# (403/451 — типичный ответ блокировки, 52x — Cloudflare "origin down").
BAD_STATUSES = {403, 451, 429} | set(range(500, 600))


class MirrorUnavailable(Exception):
    """Ни одно зеркало не отвечает."""


def normalize(url):
    """'flibusta.is' / 'https://flibusta.is/' -> 'https://flibusta.is'"""
    url = url.strip().rstrip("/")
    if not url or url.startswith("#"):
        return None
    if "://" not in url:
        # onion-сайты Флибусты работают по http (шифрование и так даёт Tor)
        url = ("http://" if url.endswith(".onion") else "https://") + url
    parts = urllib.parse.urlsplit(url)
    if not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def is_onion(base):
    return urllib.parse.urlsplit(base).hostname.endswith(".onion")


def looks_like_opds(text):
    return "<feed" in text[:5000] and "opds" in text[:5000].lower()


class MirrorManager:
    def __init__(
        self,
        mirrors=None,
        onion_mirrors=None,
        remote_list_url=None,
        state_file="mirror_state.json",
        tor_proxy=None,
        probe_timeout=8,
        request_timeout=10,
        tor_timeout=45,
        check_interval=600,
        failover_cooldown=60,
        session=None,
    ):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.remote_list_url = remote_list_url
        self.state_file = state_file
        self.tor_proxy = tor_proxy
        self.probe_timeout = probe_timeout
        self.request_timeout = request_timeout
        self.tor_timeout = tor_timeout
        self.check_interval = check_interval
        self.failover_cooldown = failover_cooldown

        all_static = [m for m in map(normalize, mirrors or DEFAULT_MIRRORS) if m]
        onions = [m for m in map(normalize, onion_mirrors or DEFAULT_ONION_MIRRORS) if m]
        self._static = [m for m in all_static if not is_onion(m)]
        self._onion = [m for m in all_static + onions if is_onion(m)]
        self._remote = []
        self._remote_fetched_at = 0
        self._lock = threading.Lock()
        self._last_failed_failover = 0
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")

        saved = self._load_state()
        if saved and is_onion(saved) and not self.tor_proxy:
            saved = None
        self._current = saved or (self._static or self._onion)[0]

    # ---------- состояние ----------

    @property
    def base(self):
        return self._current

    @property
    def on_tor(self):
        return is_onion(self._current)

    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return normalize(json.load(f).get("current", ""))
        except (OSError, ValueError, AttributeError):
            return None

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"current": self._current, "updated": int(time.time())}, f)
        except OSError as e:
            log.warning("Не удалось сохранить состояние зеркала: %s", e)

    # ---------- Tor ----------

    def _request_kwargs(self, base, timeout):
        """Для .onion — прокси Tor и увеличенный таймаут."""
        if is_onion(base):
            return {
                "proxies": {"http": self.tor_proxy, "https": self.tor_proxy},
                "timeout": max(timeout, self.tor_timeout),
            }
        return {"timeout": timeout}

    # ---------- список кандидатов ----------

    def _refresh_remote_list(self, force=False):
        if not self.remote_list_url:
            return
        if not force and time.time() - self._remote_fetched_at < 3600:
            return
        try:
            r = self.session.get(self.remote_list_url, timeout=self.probe_timeout)
            r.raise_for_status()
            mirrors = [m for m in map(normalize, r.text.splitlines()) if m]
            if mirrors:
                self._remote = mirrors
                log.info("Загружен удалённый список зеркал: %s", mirrors)
        except requests.RequestException as e:
            log.warning("Не удалось загрузить список зеркал %s: %s", self.remote_list_url, e)
        self._remote_fetched_at = time.time()

    def candidates(self, include_current=True):
        """Кандидаты по приоритету: текущий (если это не onion), удалённый
        список, статический, http-варианты (на случай протухшего
        сертификата) и в самом конце — onion-зеркала (если настроен Tor)."""
        ordered = ([self._current] if include_current else []) + self._remote + self._static
        clearnet = []
        for m in ordered:
            if m and not is_onion(m) and m not in clearnet:
                clearnet.append(m)
        for m in list(clearnet):
            if m.startswith("https://"):
                http = "http://" + m[len("https://"):]
                if http not in clearnet:
                    clearnet.append(http)

        onions = []
        if self.tor_proxy:
            for m in self._remote + self._onion:
                if is_onion(m) and m not in onions:
                    onions.append(m)
        return clearnet + onions

    # ---------- проверка ----------

    def probe(self, base):
        """Возвращает фактический адрес зеркала (с учётом редиректа) или None."""
        try:
            r = self.session.get(
                base + "/opds",
                allow_redirects=True,
                **self._request_kwargs(base, self.probe_timeout),
            )
        except requests.RequestException as e:
            log.debug("Зеркало %s недоступно: %s", base, e)
            return None
        if r.status_code != 200 or not looks_like_opds(r.text):
            log.debug("Зеркало %s ответило некорректно (HTTP %s)", base, r.status_code)
            return None
        final = normalize(r.url) or base
        if final != base:
            log.info("Зеркало %s перенаправляет на %s", base, final)
        return final

    def _select(self, cands):
        """Проверяет кандидатов параллельно и возвращает первого рабочего ПО
        ПРИОРИТЕТУ, не дожидаясь медленных (например onion) кандидатов, если
        более приоритетный уже ответил."""
        futures = [self._pool.submit(self.probe, c) for c in cands]
        for fut in futures:
            final = fut.result()
            if final:
                return final
        return None

    def _switch(self, new):
        if new != self._current:
            log.warning("Переключаюсь на зеркало %s%s", new, " (через Tor)" if is_onion(new) else "")
        self._current = new
        self._last_failed_failover = 0
        self._save_state()

    def failover(self, failed_base=None):
        """Ищет рабочее зеркало. Потокобезопасно: если другой поток уже
        переключился, просто возвращает новое значение."""
        with self._lock:
            if failed_base and self._current != failed_base:
                return self._current  # кто-то уже переключил
            if time.time() - self._last_failed_failover < self.failover_cooldown:
                raise MirrorUnavailable("Все зеркала недоступны (кулдаун)")

            self._refresh_remote_list(force=True)
            cands = self.candidates()
            log.warning("Зеркало %s не отвечает, проверяю: %s", failed_base or self._current, cands)

            final = self._select(cands)
            if final:
                self._switch(final)
                return final

            self._last_failed_failover = time.time()
            log.error("Ни одно зеркало не отвечает")
            raise MirrorUnavailable("Все зеркала недоступны")

    def ensure_healthy(self):
        """Проверяет текущее зеркало и переключается при необходимости."""
        base = self._current
        final = self.probe(base)
        if final:
            if final != base:
                with self._lock:
                    if self._current == base:
                        self._switch(final)
            return self._current
        return self.failover(failed_base=base)

    def try_leave_tor(self):
        """Если сидим на onion (медленно), пробуем вернуться на обычное зеркало."""
        if not self.on_tor:
            return False
        clearnet = [c for c in self.candidates(include_current=False) if not is_onion(c)]
        final = self._select(clearnet)
        if final:
            with self._lock:
                self._switch(final)
            return True
        return False

    # ---------- запросы ----------

    def url(self, path_or_url):
        return urllib.parse.urljoin(self._current + "/", path_or_url)

    def get(self, path, validate=None, timeout=None, **kwargs):
        """GET относительно текущего зеркала с автоматическим переключением.

        validate(response) -> bool: доп. проверка содержимого. Если вернула
        False, проверяем здоровье зеркала и при необходимости переключаемся.
        404 и прочие «нормальные» ответы возвращаются как есть.
        """
        timeout = timeout or self.request_timeout
        if time.time() - self._last_failed_failover < self.failover_cooldown:
            # Только что убедились, что всё лежит — не заставляем пользователя ждать таймаут
            raise MirrorUnavailable("Все зеркала недоступны (кулдаун)")
        for attempt in range(2):
            base = self._current
            try:
                r = self.session.get(
                    urllib.parse.urljoin(base + "/", path),
                    **self._request_kwargs(base, timeout),
                    **kwargs,
                )
                bad = r.status_code in BAD_STATUSES or (
                    validate is not None and r.status_code == 200 and not validate(r)
                )
                if not bad:
                    return r
                log.info("Подозрительный ответ %s от %s%s", r.status_code, base, path)
                if attempt == 0:
                    # Это может быть и особенность конкретного запроса,
                    # поэтому сначала проверяем, живо ли зеркало в целом.
                    if self.ensure_healthy() == base:
                        return r
                else:
                    return r
            except requests.RequestException as e:
                log.info("Ошибка запроса к %s: %s", base, e)
                if attempt == 1:
                    raise MirrorUnavailable(str(e)) from e
                self.failover(failed_base=base)
        return r

    # ---------- фоновая проверка ----------

    def start_background_check(self):
        def loop():
            while True:
                # На Tor проверяем чаще, чтобы быстрее вернуться на обычное зеркало
                time.sleep(min(self.check_interval, 120) if self.on_tor else self.check_interval)
                try:
                    self._refresh_remote_list()
                    if not self.try_leave_tor():
                        self.ensure_healthy()
                except MirrorUnavailable:
                    pass
                except Exception:
                    log.exception("Ошибка фоновой проверки зеркал")

        threading.Thread(target=loop, daemon=True, name="mirror-check").start()


def from_env():
    env = os.environ.get("FLIBUSTA_MIRRORS")
    onion_env = os.environ.get("FLIBUSTA_ONION_MIRRORS")
    return MirrorManager(
        mirrors=env.split(",") if env else None,
        onion_mirrors=onion_env.split(",") if onion_env else None,
        remote_list_url=os.environ.get(
            "MIRRORS_URL",
            "https://raw.githubusercontent.com/AlibekS/flibusta-bot/main/mirrors.txt",
        ),
        state_file=os.environ.get("MIRROR_STATE_FILE", "mirror_state.json"),
        tor_proxy=os.environ.get("TOR_PROXY") or None,
        check_interval=int(os.environ.get("MIRROR_CHECK_INTERVAL", "600")),
    )

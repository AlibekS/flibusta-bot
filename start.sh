#!/bin/sh
# Запускает Tor в фоне и бота. Бот не ждёт, пока Tor подключится к сети
# (~10–30 с): onion-зеркало — лишь запасной вариант, обычные зеркала
# работают сразу.
set -e

if [ -n "$TOR_PROXY" ] && command -v tor >/dev/null 2>&1; then
    mkdir -p /tmp/tor
    tor --SocksPort 9050 --DataDirectory /tmp/tor --Log "notice stdout" --RunAsDaemon 1
fi

exec python bot.py

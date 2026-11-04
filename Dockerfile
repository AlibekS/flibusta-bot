# Образ с Tor для Render (или любого Docker-хостинга).
# Tor нужен как резервный путь к Флибусте через .onion, если все обычные
# зеркала недоступны. Без Tor бот тоже работает — просто без этого резерва.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV TOR_PROXY=socks5h://127.0.0.1:9050 \
    PYTHONUNBUFFERED=1

CMD ["./start.sh"]

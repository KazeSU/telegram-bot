# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY kazesu_bot.py ./

# Render (and most PaaS providers) inject PORT; the bot falls back to 8080.
EXPOSE 8080

RUN useradd --create-home --uid 10001 kazesu
USER kazesu

CMD ["python", "kazesu_bot.py"]

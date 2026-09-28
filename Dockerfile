FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow \
    BOT_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY telegram_bot.py vacancy_automation.py config.json vacancy_template.txt ./
RUN mkdir -p /data

CMD ["python", "telegram_bot.py"]

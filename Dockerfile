FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

RUN useradd -u 10001 -m bot
WORKDIR /app
COPY bot.py .
RUN mkdir -p /app/data && chown bot:bot /app/data

USER bot
CMD ["python", "-u", "bot.py"]

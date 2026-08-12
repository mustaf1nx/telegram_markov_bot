FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt \
    && addgroup --system bot \
    && adduser --system --ingroup bot bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY --chown=bot:bot bot.py markov.py greetings.txt op_admins.json ./

USER bot

CMD ["python", "bot.py"]

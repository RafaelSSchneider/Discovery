FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd -m -u 10001 bot && mkdir -p /data && chown bot /data
USER bot

EXPOSE 3978
CMD ["python", "-m", "app.bot"]

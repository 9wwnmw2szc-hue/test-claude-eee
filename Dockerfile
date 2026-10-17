FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

COPY requirements.txt .
# pytest и httpx нужны только для разработки, в образ их не ставим
RUN grep -v -i -E '^(pytest|httpx)' requirements.txt > runtime.txt \
    && pip install --no-cache-dir -r runtime.txt

COPY app ./app

# Не запускаем от root; база лежит в томе /data
RUN useradd --create-home app && mkdir /data && chown app /data
USER app

# По умолчанию — бот; админка запускается другой командой (см. docker-compose.yml)
CMD ["python", "-m", "app.bot"]

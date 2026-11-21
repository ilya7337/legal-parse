# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LEGITFEED_DATABASE_URL=${LEGITFEED_DATABASE_URL:-sqlite+pysqlite:////app/legitfeed.db}

WORKDIR /app

# Зависимости отдельным слоем для кэширования сборки
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN pip install --no-cache-dir -e .

# non-root пользователь (принцип наименьших привилегий)
RUN useradd --create-home appuser
RUN mkdir -p /app/data /app/legitfeed.db && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# По умолчанию — API; для разовой демонстрации: python -m legitfeed.cli demo
CMD ["uvicorn", "legitfeed.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

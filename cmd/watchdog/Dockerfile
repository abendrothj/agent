FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-watchdog.txt .
RUN pip install --no-cache-dir -r requirements-watchdog.txt

COPY cmd/watchdog /app/cmd/watchdog
COPY internal /app/internal

ENV PYTHONPATH=/app

CMD ["python", "-m", "cmd.watchdog.main"]

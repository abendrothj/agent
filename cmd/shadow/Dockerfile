FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-shadow.txt .
RUN pip install --no-cache-dir -r requirements-shadow.txt

COPY cmd/shadow /app/cmd/shadow
COPY internal /app/internal

ENV PYTHONPATH=/app

CMD ["python", "-m", "cmd.shadow.main"]

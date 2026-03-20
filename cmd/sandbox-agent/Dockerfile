FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-sandbox.txt .
RUN pip install --no-cache-dir -r requirements-sandbox.txt

COPY cmd/sandbox-agent /app/cmd/sandbox-agent
COPY internal /app/internal

CMD ["python", "-m", "cmd.sandbox-agent.main"]

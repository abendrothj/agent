FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-sandbox.txt .
RUN pip install --no-cache-dir -r requirements-sandbox.txt

COPY cmd/__init__.py /app/cmd/__init__.py
COPY cmd/sandbox-agent /app/cmd/sandbox_agent
COPY internal /app/internal

ENV PYTHONPATH=/app

# Note: directory is 'sandbox-agent' but Python module uses 'sandbox_agent'
# (hyphens are not valid in Python module names)
CMD ["python", "-m", "cmd.sandbox_agent.main"]

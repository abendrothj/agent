FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

# Proto stubs — copied adjacent to main.py so bare imports work
COPY internal/api/vault_pb2.py      ./cmd/api/vault_pb2.py
COPY internal/api/vault_pb2_grpc.py ./cmd/api/vault_pb2_grpc.py

COPY cmd/__init__.py ./cmd/__init__.py
COPY cmd/api/ ./cmd/api/

ENV PYTHONPATH=/app
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --retries=5 --start-period=15s \
  CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "cmd.api.main:app", "--host", "0.0.0.0", "--port", "8080"]

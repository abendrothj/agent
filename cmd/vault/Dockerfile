FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    redis-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements-vault.txt .
RUN pip install --no-cache-dir -r requirements-vault.txt

# Copy code
COPY cmd/vault /app/cmd/vault
COPY internal /app/internal

# Run service
CMD ["python", "-m", "cmd.vault.main"]

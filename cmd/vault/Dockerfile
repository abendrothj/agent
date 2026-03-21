FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    redis-tools \
    git \
    ssh \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements-vault.txt .
RUN pip install --no-cache-dir -r requirements-vault.txt

# Copy code
COPY cmd/__init__.py /app/cmd/__init__.py
COPY cmd/vault /app/cmd/vault
COPY internal /app/internal
COPY configs /app/configs
COPY policies /app/policies

# Ensure project root is on PYTHONPATH so `from internal.x import y` works
ENV PYTHONPATH=/app

# Run service
CMD ["python", "-m", "cmd.vault.main"]

# Dockerfile for MT5AutoTradingBot
FROM python:3.11-slim

# Basic Python runtime hygiene
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# (Optional) OS packages if you add native deps later
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential \
#  && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better caching
COPY requirements.full.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the app
COPY . /app

# Bot makes outbound connections to Telegram; no port needed.
# If you later add an HTTP health endpoint, you can EXPOSE a port.

# Start the bot
CMD ["./start.sh"]


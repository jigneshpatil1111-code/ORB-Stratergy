# ---------------------------------------------------------------------------
# Dockerfile — ORB Intraday Trading System
#
# Multi-service container:
#   • Python 3.11 slim base (Debian)
#   • FastAPI webhook on port 8000
#   • Streamlit dashboard on port 8501
#   • SQLite data persisted via volume mount
# ---------------------------------------------------------------------------

FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr (important for Docker logs)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

WORKDIR /app

# ── System dependencies ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        curl \
        tzdata \
        nginx \
        gettext-base \
    && ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime \
    && echo "Asia/Kolkata" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────────────────
COPY . .

# ── Directories for runtime data ───────────────────────────────────────────
RUN mkdir -p /app/data /app/logs

# ── Expose ports ───────────────────────────────────────────────────────────
#   8000 — FastAPI webhook / health / API
#   8501 — Streamlit dashboard
EXPOSE 8000 8501

# ── Health check ───────────────────────────────────────────────────────────
# Removed to prevent conflicts on Render when Webhook is disabled.

# ── Entrypoint ─────────────────────────────────────────────────────────────
RUN chmod +x start.sh
CMD ["./start.sh"]

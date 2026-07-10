# ── Frontend — Streamlit ─────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8501

# --server.address=0.0.0.0   → reachable outside the container
# --server.headless=true      → suppresses the "email" prompt on first launch
# --server.fileWatcherType=none → disables inotify (not needed in prod containers)
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.fileWatcherType=none"]

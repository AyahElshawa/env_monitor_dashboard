# ── Backend — FastAPI / uvicorn ──────────────────────────────────────────────
FROM python:3.12-slim

# Keeps Python from buffering stdout/stderr (important for Docker logs)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY main.py .

EXPOSE 8000

# Run uvicorn — host 0.0.0.0 required so the port is reachable outside the container
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir "paho-mqtt>=2.0.0"

COPY publisher.py .

CMD ["python", "publisher.py"]

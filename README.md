# Environmental Monitoring System

Real-time particulate matter monitoring dashboard backed by MQTT ingestion,
SQLite persistence, and AI-powered hazard explanations via Ollama.

```
project/
├── backend/
│   ├── main.py               # FastAPI + MQTT + SQLAlchemy + Ollama
│   └── requirements.txt
├── frontend/
│   ├── app.py                # Streamlit dashboard
│   └── requirements.txt
└── README.md
```

---

## Prerequisites

| Dependency | Notes |
|---|---|
| Python 3.11+ | Tested on 3.11 and 3.12 |
| MQTT broker | e.g. Mosquitto — `brew install mosquitto` or `apt install mosquitto` |
| Ollama endpoint | 
---

## Quick Start

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Start FastAPI (hot-reload)
python main.py
# or: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

API available at: `http://localhost:8000`
Swagger docs: `http://localhost:8000/docs`

### 2. Frontend

```bash
cd frontend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

streamlit run app.py
```

Dashboard at: `http://localhost:8501`

### 3. Send a test MQTT message

```bash
# Normal reading
mosquitto_pub -h localhost -t sensors/particles \
  -m '{"sensor_id": "room1", "value": 18.5}'

# Danger reading — triggers Ollama AI explanation
mosquitto_pub -h localhost -t sensors/particles \
  -m '{"sensor_id": "room1", "value": 72.3}'
```

---

## Environment Variables (Backend)

| Variable | Default | Description |
|---|---|---|
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_BROKER_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `sensors/particles` | Subscription topic |
| `MQTT_RECONNECT_DELAY` | `5` | Seconds between reconnect attempts |
| `OLLAMA_BASE_URL` | Ollama endpoint | Ollama `/api/generate` URL |
| `OLLAMA_PRIMARY_MODEL` | `llama3.1:8b` | Primary LLM model |
| `OLLAMA_FALLBACK_MODEL` | `llama3.2:3b` | Fallback LLM model |
| `OLLAMA_TIMEOUT` | `60` | HTTP timeout in seconds |
| `DATABASE_URL` | `sqlite+aiosqlite:///./env_monitor.db` | SQLAlchemy async DSN |
| `DANGER_THRESHOLD` | `50.0` | µg/m³ value triggering Danger |
| `WARNING_THRESHOLD` | `25.0` | µg/m³ value triggering Warning |

---

## Architecture

```
MQTT Broker  ──►  aiomqtt subscriber (async task)
                         │
                    Rule Engine
                    ┌────┴────┐
                 Normal    Danger / Warning
                    │          │
                    │    Ollama REST POST  ──►  llama3.1:8b
                    │          │                (fallback: llama3.2:3b)
                    └────┬─────┘
                  SQLite (SensorLog)
                         │
                  FastAPI GET /api/history
                         │
                  Streamlit Dashboard
                  ├─ Alert banner
                  ├─ Line chart
                  └─ Searchable data grid
```

---

## Data Model

```sql
CREATE TABLE sensor_logs (
    id              TEXT PRIMARY KEY,   -- UUID v4
    timestamp       DATETIME NOT NULL,  -- UTC
    sensor_id       TEXT NOT NULL,
    particle_value  REAL NOT NULL,
    status          TEXT NOT NULL,      -- Normal | Warning | Danger
    llm_explanation TEXT                -- NULL unless Danger
);
```

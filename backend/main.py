"""
Environmental Monitoring System — FastAPI Backend
==================================================
Responsibilities:
  • Async MQTT subscriber  (aiomqtt) on topic 'sensors/particles'
  • SQLAlchemy async SQLite persistence  (model: SensorLog)
  • Rule engine: Normal / Warning / Danger classification
  • Ollama REST integration for AI explanations on Danger readings
  • REST API: GET /api/history  (last 100 entries, newest-first)
"""

from __future__ import annotations

import asyncio
import json
import ssl
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import AsyncGenerator, List, Optional

import httpx
from aiomqtt import Client as MQTTClient, MqttError, TLSParameters
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Float, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
log = logging.getLogger("env_monitor.backend")

# ---------------------------------------------------------------------------
# Configuration  (override via environment variables)
# ---------------------------------------------------------------------------
MQTT_BROKER_HOST: str = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT: int = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC: str = os.getenv("MQTT_TOPIC", "sensors/particles")
MQTT_RECONNECT_DELAY: float = float(os.getenv("MQTT_RECONNECT_DELAY", "5"))

# ── Broker authentication (leave unset for anonymous brokers) ────────────────
# If the broker rejects you with "not authorised", set these.
MQTT_USERNAME: Optional[str] = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD: Optional[str] = os.getenv("MQTT_PASSWORD") or None

# ── TLS (set MQTT_TLS=true for encrypted brokers, usually on port 8883) ──────
MQTT_TLS: bool = os.getenv("MQTT_TLS", "false").strip().lower() in ("1", "true", "yes")
# Skip certificate verification (matches the bridge's tls_insecure_set(True)).
MQTT_TLS_INSECURE: bool = os.getenv("MQTT_TLS_INSECURE", "true").strip().lower() in ("1", "true", "yes")

# ── Payload parsing (configurable so the real sensor format needs no code change) ──
#
# VALUE_PATH is a slash-separated path into the JSON to the particulate value.
#   Flat test format {"sensor_id":"room1","value":17.3}  → VALUE_PATH = "value"
#   Tasmota nested   {"Time":...,"SDS0X1":{"PM2.5":12.3}} → VALUE_PATH = "SDS0X1/PM2.5"
# A slash (not a dot) separates segments, so field names that contain dots —
# like Tasmota's "PM2.5" — are not split incorrectly.
# Once you see the real air-quality payload, set MQTT_VALUE_PATH to the right path.
MQTT_VALUE_PATH: str = os.getenv("MQTT_VALUE_PATH", "value")
#
# Where the sensor ID comes from.
#   "topic"   → extract from the MQTT topic (Tasmota: InnoLab/tele/<ID>/SENSOR)
#   "payload" → read a field from the JSON body (set MQTT_SENSOR_FIELD to its name)
MQTT_SENSOR_SOURCE: str = os.getenv("MQTT_SENSOR_SOURCE", "payload")
MQTT_SENSOR_FIELD: str = os.getenv("MQTT_SENSOR_FIELD", "sensor_id")
#
# When extracting the ID from the topic, which slash-separated segment holds it.
# Topic "InnoLab/tele/tasmota_2B30CA/SENSOR" → index 2 gives "tasmota_2B30CA".
# Negative indexes count from the end (-2 also gives "tasmota_2B30CA" here).
MQTT_TOPIC_ID_INDEX: int = int(os.getenv("MQTT_TOPIC_ID_INDEX", "-2"))

OLLAMA_BASE_URL: str = os.getenv(
    "OLLAMA_BASE_URL",
    "",
)
OLLAMA_PRIMARY_MODEL: str = os.getenv("OLLAMA_PRIMARY_MODEL", "llama3.1:8b")
OLLAMA_FALLBACK_MODEL: str = os.getenv("OLLAMA_FALLBACK_MODEL", "llama3.2:3b")
OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "60"))

DATABASE_URL: str = os.getenv(
    "DATABASE_URL", "sqlite+aiosqlite:///./env_monitor.db"
)

# These are now only the INITIAL/seed values. After first startup the live
# thresholds are stored in the DB (settings table) and edited from the dashboard.
DEFAULT_DANGER_THRESHOLD: float = float(os.getenv("DANGER_THRESHOLD", "50.0"))
DEFAULT_WARNING_THRESHOLD: float = float(os.getenv("WARNING_THRESHOLD", "25.0"))

# How often (seconds) the aggregator averages buffered readings into one row
# per sensor. Raw individual readings are NOT stored — only these averages.
AGGREGATION_INTERVAL: float = float(os.getenv("AGGREGATION_INTERVAL", "10"))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
engine = create_async_engine(DATABASE_URL, echo=False, future=True)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class StatusEnum(str, PyEnum):
    NORMAL = "Normal"
    WARNING = "Warning"
    DANGER = "Danger"


class SensorLog(Base):
    __tablename__ = "sensor_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    timestamp = Column(DateTime(timezone=True), nullable=False)
    sensor_id = Column(String(128), nullable=False)
    particle_value = Column(Float, nullable=False)
    status = Column(String(16), nullable=False)          # Normal / Warning / Danger
    llm_explanation = Column(Text, nullable=True)


class Settings(Base):
    """
    Single-row table holding the live, dashboard-editable thresholds.
    We always use id=1 so there is exactly one settings row.
    """
    __tablename__ = "settings"

    id = Column(String(8), primary_key=True, default="main")
    danger_threshold = Column(Float, nullable=False)
    warning_threshold = Column(Float, nullable=False)


# ---------------------------------------------------------------------------
# In-memory reading buffer (for 10-second averaging)
# ---------------------------------------------------------------------------
# Raw MQTT readings are appended here and flushed/averaged by the aggregator
# task every AGGREGATION_INTERVAL seconds. A lock guards concurrent access
# since the MQTT handler and the aggregator both touch it.
_reading_buffer: dict[str, list[float]] = {}
_buffer_lock = asyncio.Lock()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed the single settings row if it doesn't exist yet
    async with AsyncSessionFactory() as session:
        existing = await session.get(Settings, "main")
        if existing is None:
            session.add(Settings(
                id="main",
                danger_threshold=DEFAULT_DANGER_THRESHOLD,
                warning_threshold=DEFAULT_WARNING_THRESHOLD,
            ))
            await session.commit()
            log.info("Settings seeded — danger=%.1f warning=%.1f",
                     DEFAULT_DANGER_THRESHOLD, DEFAULT_WARNING_THRESHOLD)
    log.info("Database initialised.")


async def get_thresholds() -> tuple[float, float]:
    """Read the live (danger, warning) thresholds from the settings table."""
    async with AsyncSessionFactory() as session:
        row = await session.get(Settings, "main")
        if row is None:
            return DEFAULT_DANGER_THRESHOLD, DEFAULT_WARNING_THRESHOLD
        return row.danger_threshold, row.warning_threshold


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionFactory() as session:
        yield session


# ---------------------------------------------------------------------------
# Pydantic schemas (API responses)
# ---------------------------------------------------------------------------
class SensorLogOut(BaseModel):
    id: str
    timestamp: datetime
    sensor_id: str
    particle_value: float
    status: str
    llm_explanation: Optional[str] = None

    model_config = {"from_attributes": True}


class SettingsOut(BaseModel):
    danger_threshold: float
    warning_threshold: float


class SettingsIn(BaseModel):
    danger_threshold: float
    warning_threshold: float


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------
def classify(value: float, danger_threshold: float, warning_threshold: float) -> str:
    if value > danger_threshold:
        return StatusEnum.DANGER
    if value > warning_threshold:
        return StatusEnum.WARNING
    return StatusEnum.NORMAL


# ---------------------------------------------------------------------------
# Ollama integration
# ---------------------------------------------------------------------------
def _build_ollama_prompt(sensor_id: str, value: float, danger_threshold: float) -> str:
    return (
        f"Du bist ein Experte für Umweltsicherheit. "
        f"Sensor '{sensor_id}' hat eine Feinstaubkonzentration von "
        f"{value:.2f} µg/m³ gemeldet, was den sicheren Schwellenwert von "
        f"{danger_threshold} µg/m³ überschreitet. "
        f"Bitte antworte ausschließlich auf Deutsch und gehe wie folgt vor:\n"
        f"1. Erkläre in einfacher Sprache, warum diese Konzentration gesundheitsgefährdend ist.\n"
        f"2. Beschreibe die konkreten Gesundheitsrisiken bei kurzfristiger und langfristiger Exposition.\n"
        f"3. Nenne sofortige praktische Vorsichtsmaßnahmen, die Betroffene jetzt ergreifen sollten.\n"
        f"4. Schlage Maßnahmen zur Reduzierung der Feinstaubwerte im betroffenen Bereich vor.\n"
        f"Antworte präzise, professionell und handlungsorientiert. Verwende Markdown-Formatierung. "
        f"Antworte ausschließlich auf Deutsch, auch wenn die Eingabe auf Englisch war."
    )


async def fetch_ollama_explanation(
    sensor_id: str, value: float, danger_threshold: float
) -> Optional[str]:
    """
    Call the Ollama endpoint.  Tries the primary model first;
    falls back to the secondary model on failure.  Returns None on total failure.
    """
    prompt = _build_ollama_prompt(sensor_id, value, danger_threshold)

    for model in (OLLAMA_PRIMARY_MODEL, OLLAMA_FALLBACK_MODEL):
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        try:
            log.info("Requesting Ollama explanation — model=%s sensor=%s value=%.2f",
                     model, sensor_id, value)
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                response = await client.post(OLLAMA_BASE_URL, json=payload)
                response.raise_for_status()
                data = response.json()
                explanation: str = data.get("response", "").strip()
                if explanation:
                    log.info("Ollama explanation received (model=%s, %d chars).",
                             model, len(explanation))
                    return explanation
                log.warning("Ollama returned empty response for model=%s.", model)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Ollama HTTP error (model=%s): %s — trying fallback.",
                model, exc.response.status_code,
            )
        except httpx.RequestError as exc:
            log.warning("Ollama request error (model=%s): %s — trying fallback.", model, exc)
        except (KeyError, ValueError) as exc:
            log.warning("Ollama response parse error (model=%s): %s.", model, exc)

    log.error("All Ollama model attempts exhausted — no explanation stored.")
    return None


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def _dig(data: dict, path: str):
    """
    Walk a slash-separated path into nested dicts.
    _dig({"SDS0X1": {"PM2.5": 12.3}}, "SDS0X1/PM2.5") -> 12.3

    A slash (not a dot) is the separator on purpose: real sensor field names
    often contain dots themselves (e.g. Tasmota's "PM2.5"), so a dot separator
    would wrongly split "PM2.5" into "PM2" and "5". Returns None if any segment
    is missing.
    """
    node = data
    for segment in path.split("/"):
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        else:
            return None
    return node


def _sensor_id_from_topic(topic: str) -> Optional[str]:
    """Extract the sensor ID from a slash-separated topic at MQTT_TOPIC_ID_INDEX."""
    parts = [p for p in topic.split("/") if p != ""]
    if not parts:
        return None
    try:
        return parts[MQTT_TOPIC_ID_INDEX]
    except IndexError:
        return None


# ---------------------------------------------------------------------------
# MQTT message handler — buffers readings (no per-message DB write)
# ---------------------------------------------------------------------------
async def handle_mqtt_message(payload: str, topic: str = "") -> None:
    """
    Parse one MQTT message and append the particulate value to the buffer.
    The aggregator flushes averaged rows every AGGREGATION_INTERVAL seconds.

    Designed to adapt to the real sensor format via env vars WITHOUT code changes:
      • MQTT_VALUE_PATH    — dotted path to the value (e.g. "SDS0X1.PM2.5")
      • MQTT_SENSOR_SOURCE — "topic" or "payload"
      • MQTT_SENSOR_FIELD  — field name if the ID is in the payload
      • MQTT_TOPIC_ID_INDEX— which topic segment is the ID (if source is "topic")
    Parsing is tolerant: a single malformed message is logged and skipped.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        log.warning("Non-JSON MQTT payload skipped: %r (%s)", payload[:200], exc)
        return

    if not isinstance(data, dict):
        log.warning("MQTT payload is not a JSON object, skipped: %r", payload[:200])
        return

    # ── Sensor ID ────────────────────────────────────────────────────────────
    if MQTT_SENSOR_SOURCE == "topic":
        sensor_id = _sensor_id_from_topic(topic)
        if sensor_id is None:
            log.warning("Could not extract sensor ID from topic '%s' (index %d).",
                        topic, MQTT_TOPIC_ID_INDEX)
            return
    else:
        raw_sensor = data.get(MQTT_SENSOR_FIELD)
        if raw_sensor is None:
            log.warning("MQTT message missing sensor field '%s'. Keys: %s",
                        MQTT_SENSOR_FIELD, list(data.keys()))
            return
        sensor_id = str(raw_sensor)

    # ── Particulate value (via configurable nested path) ─────────────────────
    raw_value = _dig(data, MQTT_VALUE_PATH)
    if raw_value is None:
        log.warning(
            "MQTT message has no value at path '%s'. Payload keys: %s",
            MQTT_VALUE_PATH, list(data.keys()),
        )
        return

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        log.warning("Value at '%s' for sensor '%s' is not numeric: %r — skipped.",
                    MQTT_VALUE_PATH, sensor_id, raw_value)
        return

    async with _buffer_lock:
        _reading_buffer.setdefault(sensor_id, []).append(value)


# ---------------------------------------------------------------------------
# Aggregator task — averages buffered readings every AGGREGATION_INTERVAL secs
# ---------------------------------------------------------------------------
async def aggregator_task() -> None:
    """
    Every AGGREGATION_INTERVAL seconds: take everything in the buffer, compute
    the mean per sensor, write ONE averaged SensorLog row per sensor, then clear
    the buffer. Danger classification (and the Ollama call) runs on the average.
    """
    while True:
        try:
            await asyncio.sleep(AGGREGATION_INTERVAL)

            # Atomically grab and clear the buffer
            async with _buffer_lock:
                if not _reading_buffer:
                    continue
                snapshot = {sid: vals[:] for sid, vals in _reading_buffer.items() if vals}
                _reading_buffer.clear()

            if not snapshot:
                continue

            # Read the live thresholds once for this whole flush
            danger_threshold, warning_threshold = await get_thresholds()

            for sensor_id, values in snapshot.items():
                avg_value = sum(values) / len(values)
                status = classify(avg_value, danger_threshold, warning_threshold)
                row_id = str(uuid.uuid4())

                log_entry = SensorLog(
                    id=row_id,
                    timestamp=datetime.now(timezone.utc),
                    sensor_id=sensor_id,
                    particle_value=round(avg_value, 2),
                    status=status,
                    llm_explanation=None,
                )
                try:
                    async with get_session() as session:
                        session.add(log_entry)
                        await session.commit()
                    log.info(
                        "Aggregated — sensor=%s  avg=%.2f  n=%d  status=%s",
                        sensor_id, avg_value, len(values), status,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("DB write error in aggregator: %s", exc, exc_info=True)
                    continue

                # Trigger Ollama on the averaged Danger value (fire-and-forget)
                if status == StatusEnum.DANGER:
                    asyncio.ensure_future(
                        _enrich_with_llm(row_id, sensor_id, avg_value, danger_threshold)
                    )

        except asyncio.CancelledError:
            log.info("Aggregator task cancelled — shutting down.")
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Aggregator unexpected error: %s", exc, exc_info=True)


async def _enrich_with_llm(
    row_id: str, sensor_id: str, value: float, danger_threshold: float
) -> None:
    """
    Fetch an Ollama explanation in the background and patch the existing DB row.
    The dashboard picks up the explanation on the next auto-refresh poll.
    """
    explanation = await fetch_ollama_explanation(sensor_id, value, danger_threshold)
    if not explanation:
        return
    try:
        async with get_session() as session:
            result = await session.execute(
                select(SensorLog).where(SensorLog.id == row_id)
            )
            row = result.scalar_one_or_none()
            if row:
                row.llm_explanation = explanation
                await session.commit()
                log.info("LLM explanation patched — id=%s (%d chars)", row_id, len(explanation))
    except Exception as exc:  # noqa: BLE001
        log.error("DB patch error for LLM explanation: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# MQTT background task
# ---------------------------------------------------------------------------
async def mqtt_listener() -> None:
    """
    Persistent MQTT subscriber.  Automatically reconnects on any MqttError
    after MQTT_RECONNECT_DELAY seconds.
    """
    while True:
        try:
            log.info(
                "Attempting MQTT connection → %s:%d  topic='%s'  auth=%s  tls=%s",
                MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_TOPIC,
                "yes" if MQTT_USERNAME else "no", MQTT_TLS,
            )

            # Build optional TLS parameters only when TLS is enabled.
            tls_params = None
            if MQTT_TLS:
                tls_params = TLSParameters(
                    cert_reqs=ssl.CERT_NONE if MQTT_TLS_INSECURE else ssl.CERT_REQUIRED,
                )

            async with MQTTClient(
                hostname=MQTT_BROKER_HOST,
                port=MQTT_BROKER_PORT,
                username=MQTT_USERNAME,      # None → anonymous connection
                password=MQTT_PASSWORD,
                tls_params=tls_params,        # None → plain TCP
                tls_insecure=MQTT_TLS_INSECURE if MQTT_TLS else None,
                identifier=f"env_monitor_{uuid.uuid4().hex[:8]}",
            ) as client:
                await client.subscribe(MQTT_TOPIC)
                log.info("MQTT subscribed to '%s'. Waiting for messages …", MQTT_TOPIC)
                async for message in client.messages:
                    try:
                        raw = message.payload.decode("utf-8", errors="replace")
                        topic = str(message.topic)
                        log.info("MQTT message on '%s': %s", topic, raw)
                        asyncio.ensure_future(handle_mqtt_message(raw, topic))
                    except Exception as exc:  # noqa: BLE001
                        log.error("Error dispatching MQTT message: %s", exc, exc_info=True)

        except MqttError as exc:
            log.error(
                "MQTT MqttError: %s — retrying in %.0fs …",
                exc, MQTT_RECONNECT_DELAY, exc_info=True,
            )
        except asyncio.CancelledError:
            log.info("MQTT listener cancelled — shutting down.")
            return
        except Exception as exc:  # noqa: BLE001
            log.error(
                "MQTT unexpected error — retrying in %.0fs …",
                MQTT_RECONNECT_DELAY, exc_info=True,
            )

        await asyncio.sleep(MQTT_RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# FastAPI lifespan (startup / shutdown)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Two background tasks: the MQTT listener (buffers readings) and the
    # aggregator (averages the buffer into the DB every AGGREGATION_INTERVAL).
    app.state.mqtt_task = asyncio.ensure_future(mqtt_listener())
    app.state.aggregator_task = asyncio.ensure_future(aggregator_task())
    log.info("Application startup complete — MQTT listener + aggregator scheduled "
             "(averaging every %.0fs).", AGGREGATION_INTERVAL)
    yield
    app.state.mqtt_task.cancel()
    app.state.aggregator_task.cancel()
    for task in (app.state.mqtt_task, app.state.aggregator_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await engine.dispose()
    log.info("Application shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Environmental Monitoring API",
    version="1.0.0",
    description="Real-time particulate sensor monitoring with AI-powered hazard analysis.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.get("/api/history", response_model=List[SensorLogOut], summary="Last 100 sensor readings")
async def get_history() -> List[SensorLogOut]:
    """Return the 100 most recent sensor log entries, sorted newest-first."""
    try:
        async with get_session() as session:
            result = await session.execute(
                select(SensorLog)
                .order_by(SensorLog.timestamp.desc())
                .limit(100)
            )
            rows: List[SensorLog] = result.scalars().all()
            return [SensorLogOut.model_validate(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to query history: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Database query failed.") from exc


@app.get("/api/stats", summary="Whole-database aggregate stats")
async def get_stats() -> dict:
    """
    Return aggregate counts over the ENTIRE database (not just the last 100),
    so the dashboard KPI cards reflect all stored readings. The /api/history
    endpoint stays capped at 100 for the table/chart, but totals come from here.
    """
    try:
        async with get_session() as session:
            total = await session.scalar(select(func.count()).select_from(SensorLog))
            danger = await session.scalar(
                select(func.count()).select_from(SensorLog).where(SensorLog.status == "Danger")
            )
            warning = await session.scalar(
                select(func.count()).select_from(SensorLog).where(SensorLog.status == "Warning")
            )
            normal = await session.scalar(
                select(func.count()).select_from(SensorLog).where(SensorLog.status == "Normal")
            )
            avg_value = await session.scalar(select(func.avg(SensorLog.particle_value)))
            max_value = await session.scalar(select(func.max(SensorLog.particle_value)))
            return {
                "total": total or 0,
                "danger": danger or 0,
                "warning": warning or 0,
                "normal": normal or 0,
                "avg_value": round(avg_value, 2) if avg_value is not None else 0.0,
                "max_value": round(max_value, 2) if max_value is not None else 0.0,
            }
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to query stats: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Stats query failed.") from exc


@app.get("/api/settings", response_model=SettingsOut, summary="Get live thresholds")
async def read_settings() -> SettingsOut:
    """Return the current danger/warning thresholds used for classification."""
    danger, warning = await get_thresholds()
    return SettingsOut(danger_threshold=danger, warning_threshold=warning)


@app.post("/api/settings", response_model=SettingsOut, summary="Update live thresholds")
async def update_settings(new: SettingsIn) -> SettingsOut:
    """
    Update the danger/warning thresholds. Takes effect on the next aggregation
    cycle — no restart needed. Validates that danger > warning > 0.
    """
    if new.warning_threshold <= 0 or new.danger_threshold <= 0:
        raise HTTPException(status_code=400, detail="Thresholds must be positive.")
    if new.danger_threshold <= new.warning_threshold:
        raise HTTPException(
            status_code=400,
            detail="Danger threshold must be greater than warning threshold.",
        )
    try:
        async with get_session() as session:
            row = await session.get(Settings, "main")
            if row is None:
                row = Settings(id="main")
                session.add(row)
            row.danger_threshold = new.danger_threshold
            row.warning_threshold = new.warning_threshold
            await session.commit()
        log.info("Thresholds updated — danger=%.1f warning=%.1f",
                 new.danger_threshold, new.warning_threshold)
        return SettingsOut(
            danger_threshold=new.danger_threshold,
            warning_threshold=new.warning_threshold,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to update settings: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update settings.") from exc


@app.get("/health", summary="Health check")
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Entry point (development)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )flla

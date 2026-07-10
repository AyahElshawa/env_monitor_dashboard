"""
Environmental Monitoring System — Streamlit Dashboard
======================================================
Features:
  • Auto-refreshing poll of GET /api/history every N seconds
  • Danger / Warning / Normal alert banner (top of page)
  • Interactive line chart of particle values over time
  • Searchable historical data grid with colour-coded status badges
  • Full AI explanation rendered in the alert box when status is Danger
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# BACKEND_URL is injected by Docker Compose as an env-var; falls back to
# localhost for direct (non-containerised) development runs.
BACKEND_URL: str = os.getenv("BACKEND_URL", "http://localhost:8000")
HISTORY_ENDPOINT: str = f"{BACKEND_URL}/api/history"
SETTINGS_ENDPOINT: str = f"{BACKEND_URL}/api/settings"
STATS_ENDPOINT: str = f"{BACKEND_URL}/api/stats"
REFRESH_INTERVAL: int = int(os.getenv("REFRESH_INTERVAL", "20"))
REQUEST_TIMEOUT: int = 8
# Fallback defaults only — the live values come from the backend settings API.
DEFAULT_DANGER_THRESHOLD: float = 50.0
DEFAULT_WARNING_THRESHOLD: float = 25.0

STATUS_EMOJI = {
    "Danger": "🔴",
    "Warning": "🟡",
    "Normal": "🟢",
}

# ---------------------------------------------------------------------------
# Page config  (must be first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Umweltüberwachung",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="collapsed",   # sidebar collapsed by default
)

# ---------------------------------------------------------------------------
# Custom CSS — minimal, tasteful
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
        /* ── Hide the entire sidebar and its open/close toggle ── */
        [data-testid="stSidebar"]          { display: none !important; }
        [data-testid="collapsedControl"]   { display: none !important; }

        /* ── Hide the top-right toolbar (Stop / Deploy / ⋮ menu) ── */
        [data-testid="stToolbar"]          { display: none !important; }
        [data-testid="stDecoration"]       { display: none !important; }
        header[data-testid="stHeader"]     { background: transparent;  }

        /* ── Layout ── */
        .block-container { padding-top: 1.5rem; padding-bottom: 1rem; }

        /* ── Metric cards ── */
        [data-testid="metric-container"] {
            background: #f8f9fb;
            border-radius: 8px;
            padding: 0.6rem 1rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

@st.cache_data(ttl=REFRESH_INTERVAL, show_spinner=False)
def fetch_history() -> tuple[pd.DataFrame, Optional[str]]:
    """
    Fetch /api/history from the backend.
    Returns (dataframe, error_message).  The dataframe is empty on error.
    """
    try:
        resp = requests.get(HISTORY_ENDPOINT, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        records = resp.json()
        if not records:
            return pd.DataFrame(), None
        df = pd.DataFrame(records)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df["timestamp"] = df["timestamp"].dt.tz_convert("Europe/Berlin")
        df.sort_values("timestamp", ascending=True, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df, None
    except requests.exceptions.ConnectionError:
        return pd.DataFrame(), "⚠️ Cannot reach the backend. Is it running on `localhost:8000`?"
    except requests.exceptions.Timeout:
        return pd.DataFrame(), "⚠️ Request to backend timed out."
    except requests.exceptions.HTTPError as exc:
        return pd.DataFrame(), f"⚠️ Backend returned HTTP {exc.response.status_code}."
    except (ValueError, KeyError) as exc:
        return pd.DataFrame(), f"⚠️ Unexpected response format: {exc}"


def fetch_thresholds() -> tuple[float, float]:
    """Read the live thresholds from the backend. Falls back to defaults on error."""
    try:
        resp = requests.get(SETTINGS_ENDPOINT, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return float(data["danger_threshold"]), float(data["warning_threshold"])
    except Exception:
        return DEFAULT_DANGER_THRESHOLD, DEFAULT_WARNING_THRESHOLD


def fetch_stats() -> Optional[dict]:
    """Read whole-database aggregate stats for the KPI cards. None on error."""
    try:
        resp = requests.get(STATS_ENDPOINT, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def update_thresholds(danger: float, warning: float) -> tuple[bool, str]:
    """Push new thresholds to the backend. Returns (success, message)."""
    try:
        resp = requests.post(
            SETTINGS_ENDPOINT,
            json={"danger_threshold": danger, "warning_threshold": warning},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "Schwellenwerte aktualisiert."
        # Surface validation messages from the backend
        detail = resp.json().get("detail", f"HTTP {resp.status_code}")
        return False, str(detail)
    except Exception as exc:
        return False, f"Fehler: {exc}"


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("🌿 Umwelt-Feinstaubmonitor")
st.caption(
    f"Dashboard aktualisiert sich automatisch alle **{REFRESH_INTERVAL}s**."
)

# Load the live thresholds from the backend on every run
DANGER_THRESHOLD, WARNING_THRESHOLD = fetch_thresholds()

# ---------------------------------------------------------------------------
# Threshold editor (expandable, so it doesn't clutter the 24/7 display)
# ---------------------------------------------------------------------------
with st.expander("⚙️ Schwellenwerte einstellen", expanded=False):
    st.caption(
        "Diese Werte bestimmen, ab wann eine Messung als *Warnung* oder *Gefahr* "
        "eingestuft wird. Änderungen wirken ab dem nächsten Mittelungsintervall."
    )
    ec1, ec2, ec3 = st.columns([2, 2, 1])
    with ec1:
        new_warning = st.number_input(
            "Warnschwelle (µg/m³)",
            min_value=1.0, max_value=500.0,
            value=float(WARNING_THRESHOLD), step=1.0,
        )
    with ec2:
        new_danger = st.number_input(
            "Gefahrenschwelle (µg/m³)",
            min_value=1.0, max_value=500.0,
            value=float(DANGER_THRESHOLD), step=1.0,
        )
    with ec3:
        st.write("")
        st.write("")
        if st.button("Speichern", use_container_width=True):
            ok, msg = update_thresholds(new_danger, new_warning)
            if ok:
                st.success(msg)
                DANGER_THRESHOLD, WARNING_THRESHOLD = new_danger, new_warning
            else:
                st.error(msg)


# ---------------------------------------------------------------------------
# Fetch data
# ---------------------------------------------------------------------------
df, fetch_error = fetch_history()

# ---------------------------------------------------------------------------
# Connection error fallback
# ---------------------------------------------------------------------------
if fetch_error:
    st.warning(fetch_error)
    st.info("Warte auf Verbindung zum Backend …")
    time.sleep(REFRESH_INTERVAL)
    st.rerun()

if df.empty:
    st.info("Noch keine Sensordaten empfangen. Warte auf MQTT-Nachrichten …")
    time.sleep(REFRESH_INTERVAL)
    st.rerun()

# ---------------------------------------------------------------------------
# Alert banner — based on the LATEST reading
# ---------------------------------------------------------------------------
latest = df.iloc[-1]  # df is sorted ascending → last row is newest
latest_status: str = latest.get("status", "Normal")
latest_value: float = float(latest.get("particle_value", 0.0))
latest_sensor: str = str(latest.get("sensor_id", "unknown"))
latest_ts: datetime = latest.get("timestamp")
latest_explanation: Optional[str] = latest.get("llm_explanation")

# Check if explanation exists and is not a NaN value
has_explanation = (
    pd.notna(latest_explanation) 
    if isinstance(latest_explanation, (str, float, type(None))) 
    else bool(latest_explanation)
)


if latest_status == "Danger":
    ts_str = latest_ts.strftime('%Y-%m-%d %H:%M:%S') if hasattr(latest_ts, 'strftime') else str(latest_ts)
    st.error(
        f"### {STATUS_EMOJI['Danger']}  GEFAHRENALARM — Sensor `{latest_sensor}`\n\n"
        f"**Feinstaubwert:** `{latest_value:.2f} µg/m³`  "
        f"**Schwellenwert:** `{DANGER_THRESHOLD:.1f} µg/m³`  "
        f"**Erfasst:** `{ts_str}`\n\n"
        + (
            f"---\n\n**🤖 KI-Sicherheitsanalyse**\n\n{latest_explanation}"
            if has_explanation and str(latest_explanation).strip().lower() != "nan"
            else "_KI-Erklärung wird generiert …_"
        )
    )
elif latest_status == "Warning":
    st.warning(
        f"### {STATUS_EMOJI['Warning']}  WARNUNG — Sensor `{latest_sensor}`\n\n"
        f"Feinstaubwert `{latest_value:.2f} µg/m³` ist erhöht. "
        f"Bitte genau beobachten und für Lüftung sorgen."
    )
else:
    st.success(
        f"{STATUS_EMOJI['Normal']}  Alle Sensoren im Normalbereich — "
        f"Letzter Messwert von **{latest_sensor}**: `{latest_value:.2f} µg/m³`"
    )

st.divider()

# ---------------------------------------------------------------------------
# KPI metrics row
# ---------------------------------------------------------------------------
# KPI metrics row — use whole-database stats so totals reflect ALL readings,
# not just the last 100 returned for the table/chart. Falls back to the window
# if the stats endpoint is unavailable.
stats = fetch_stats()
if stats:
    total_readings = stats["total"]
    danger_count   = stats["danger"]
    warning_count  = stats["warning"]
    normal_count   = stats["normal"]
    avg_value      = stats["avg_value"]
    max_value      = stats["max_value"]
    totals_scope = "gesamte Datenbank"
else:
    total_readings = len(df)
    danger_count   = int((df["status"] == "Danger").sum())
    warning_count  = int((df["status"] == "Warning").sum())
    normal_count   = int((df["status"] == "Normal").sum())
    avg_value      = df["particle_value"].mean()
    max_value      = df["particle_value"].max()
    totals_scope = "letzte 100"

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("📊 Messwerte gesamt",     str(total_readings))
c2.metric("🔴 Gefahren-Ereignisse",  str(danger_count))
c3.metric("🟡 Warnungen",            str(warning_count))
c4.metric("🟢 Normal",               str(normal_count))
c5.metric("📈 Durchschnitt (µg/m³)", f"{avg_value:.2f}")
c6.metric("⬆️ Spitzenwert (µg/m³)",  f"{max_value:.2f}")
st.caption(f"Kennzahlen berechnet über: **{totals_scope}**")

st.divider()

# ---------------------------------------------------------------------------
# Line chart — particle values over time
# ---------------------------------------------------------------------------
st.subheader("📉 Feinstaubkonzentration im Zeitverlauf")

# Build a chart where each sensor is its own line. Sensors report at slightly
# different times, so we round timestamps to the minute to give them shared
# x-axis points — otherwise every column is full of gaps and no line connects.
chart_src = df[["timestamp", "sensor_id", "particle_value"]].copy()
chart_src["timestamp"] = chart_src["timestamp"].dt.floor("1min")

try:
    pivot = chart_src.pivot_table(
        index="timestamp",
        columns="sensor_id",
        values="particle_value",
        aggfunc="mean",
    ).sort_index()
    # Forward-fill small gaps so each line stays continuous across the window
    pivot = pivot.ffill()
    if pivot.empty or pivot.dropna(how="all").empty:
        st.info("Noch nicht genügend Daten für das Diagramm.")
    else:
        st.line_chart(pivot, use_container_width=True)
except Exception:
    # Fallback: single combined series
    st.line_chart(
        chart_src.set_index("timestamp")[["particle_value"]]
                 .rename(columns={"particle_value": "µg/m³"}),
        use_container_width=True,
    )

# Threshold reference line note
st.caption(
    f"ℹ️  Gefahrenschwelle: **{DANGER_THRESHOLD:.1f} µg/m³**  |  "
    f"Warnschwelle: **{WARNING_THRESHOLD:.1f} µg/m³**"
)

st.divider()

# ---------------------------------------------------------------------------
# Incident log viewer
# ---------------------------------------------------------------------------
st.subheader("📋 Sensor-Verlaufsprotokoll")

# Search / filter controls
search_col, status_col, sensor_col = st.columns([3, 2, 2])
with search_col:
    search_query = st.text_input(
        "suche", placeholder="🔍 Erklärungstext durchsuchen …", label_visibility="collapsed"
    )
with status_col:
    status_filter_de = st.multiselect(
        "status",
        options=["Normal", "Warnung", "Gefahr"],
        default=["Normal", "Warnung", "Gefahr"],
        label_visibility="collapsed",
    )
    # Map German labels back to the English values stored in the database
    DE_TO_EN = {"Normal": "Normal", "Warnung": "Warning", "Gefahr": "Danger"}
    status_filter = [DE_TO_EN[s] for s in status_filter_de]
with sensor_col:
    sensor_options = ["Alle Sensoren"] + sorted(df["sensor_id"].unique().tolist())
    sensor_filter = st.selectbox("sensor", options=sensor_options, label_visibility="collapsed")

# Apply filters
display_df = df.copy()
display_df = display_df[display_df["status"].isin(status_filter)]
if sensor_filter != "Alle Sensoren":
    display_df = display_df[display_df["sensor_id"] == sensor_filter]
if search_query.strip():
    mask = display_df["llm_explanation"].fillna("").str.contains(
        search_query.strip(), case=False, regex=False
    )
    display_df = display_df[mask]

# Sort newest first for the table
display_df = display_df.sort_values("timestamp", ascending=False)

# Render readable table
table_df = display_df[["timestamp", "sensor_id", "particle_value", "status"]].copy()
table_df["timestamp"] = table_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
table_df["particle_value"] = table_df["particle_value"].round(2)
# Translate the English DB status values to German for display
STATUS_DE = {"Normal": "Normal", "Warning": "Warnung", "Danger": "Gefahr"}
table_df["status"] = table_df["status"].map(STATUS_DE).fillna(table_df["status"])
table_df.rename(
    columns={
        "timestamp": "Zeitstempel",
        "sensor_id": "Sensor",
        "particle_value": "Wert (µg/m³)",
        "status": "Status",
    },
    inplace=True,
)

st.dataframe(
    table_df,
    use_container_width=True,
    height=420,
    column_config={
        "Status": st.column_config.TextColumn(
            "Status",
            help="Normal · Warnung · Gefahr",
            width="small",
        ),
        "Wert (µg/m³)": st.column_config.NumberColumn(
            "Wert (µg/m³)",
            format="%.2f",
            width="small",
        ),
    },
    hide_index=True,
)

st.caption(f"Zeige **{len(display_df)}** von **{total_readings}** Einträgen.")

# ---------------------------------------------------------------------------
# display_df keeps the ORIGINAL English column names (the renaming applied only
# to the separate table_df), so we filter on the English "status" value here.
danger_display = display_df[display_df["status"] == "Danger"] \
    if "status" in display_df.columns \
    else pd.DataFrame()

if not danger_display.empty:
    st.divider()
    st.subheader("🤖 KI-Gefahrenanalysen")
    for _, row in danger_display.head(5).iterrows():
        ts_label = row.get("timestamp", "")
        sensor_label = row.get("sensor_id", "")
        value_label = row.get("particle_value", 0)
        expl = row.get("llm_explanation")
        ts_text = ts_label.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts_label, "strftime") else str(ts_label)
        with st.expander(
            f"🔴  {ts_text}  ·  Sensor {sensor_label}  ·  {float(value_label):.2f} µg/m³",
            expanded=False,
        ):
            if isinstance(expl, str) and expl.strip() and expl.strip().lower() != "nan":
                st.markdown(expl)
            else:
                st.info("KI-Erklärung wird generiert — erscheint nach dem nächsten Refresh.")

# ---------------------------------------------------------------------------
# Auto-refresh  (Streamlit experimental rerun loop)
# ---------------------------------------------------------------------------
st.divider()
refresh_placeholder = st.empty()

for remaining in range(REFRESH_INTERVAL, 0, -1):
    refresh_placeholder.caption(f"⏱ Nächste Aktualisierung in **{remaining}s** …")
    time.sleep(1)

st.cache_data.clear()
st.rerun()
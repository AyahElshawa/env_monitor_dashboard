"""
Environmental Monitor — Mock MQTT Publisher (Tasmota format, load-test version)
================================================================================
Publishes Tasmota-style air-quality messages every PUBLISH_INTERVAL seconds so
you can test the REAL production data path before connecting to live sensors.

Message shape mirrors the real Tasmota sensors:
  Topic   : InnoLab/tele/<deviceId>/SENSOR
  Payload : {"Time": "...", "SDS0X1": {"PM2.5": 12.3, "PM10": 20.1}}

The sensor ID comes from the TOPIC (the <deviceId> segment), and the particulate
value is nested under SDS0X1.PM2.5 — exactly what the backend is configured to
read via MQTT_SENSOR_SOURCE=topic and MQTT_VALUE_PATH=SDS0X1.PM2.5.

Run on the WSL2 HOST (broker is localhost there):
    python3 mock_publisher.py

NOTE: To use this format, the backend must be configured with:
    MQTT_TOPIC: "InnoLab/tele/+/SENSOR"
    MQTT_SENSOR_SOURCE: "topic"
    MQTT_TOPIC_ID_INDEX: "-2"
    MQTT_VALUE_PATH: "SDS0X1.PM2.5"
"""

import json
import random
import time
from datetime import datetime

import paho.mqtt.client as mqtt

# ── Configuration ────────────────────────────────────────────────────────────
BROKER = "localhost"
PORT = 1883
TOPIC_TEMPLATE = "InnoLab/tele/{device}/SENSOR"   # <device> filled per message
PUBLISH_INTERVAL = 3.0
# Simulated Tasmota device IDs (these become the sensor_id via the topic)
DEVICES = ["tasmota_room1", "tasmota_room2", "tasmota_room3"]

print("┌────────────────────────────────────────────────┐")
print("│  Environmental Monitor — Mock Tasmota Publisher  │")
print(f"│  Topic : InnoLab/tele/<device>/SENSOR{' ':<11}│")
print(f"│  Broker: {BROKER}:{PORT:<28}│")
print(f"│  Rate  : 1 message every {PUBLISH_INTERVAL}s (rotating devices){' ':<2}│")
print("└────────────────────────────────────────────────┘")

# ── Version-proof client creation (paho 1.x AND 2.x) ─────────────────────────
try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    print("Using paho-mqtt 2.x API")
except AttributeError:
    client = mqtt.Client()
    print("Using paho-mqtt 1.x API")


def on_connect(client, userdata, flags, rc, *args):
    print(f"✅ Connected to broker (rc={rc})" if rc == 0
          else f"❌ Connect failed (rc={rc})")


client.on_connect = on_connect

try:
    print(f"Connecting to {BROKER}:{PORT} …")
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()
    time.sleep(1)
    print("Publishing Tasmota-format messages… (Ctrl+C to stop)\n")

    cycle = 0
    while True:
        # Pick ONE device per cycle so exactly one message is sent every
        # PUBLISH_INTERVAL seconds. Rotating through the list keeps all sensors
        # represented over time without sending several messages at once.
        device = DEVICES[cycle % len(DEVICES)]
        cycle += 1

        # Random PM2.5: mostly normal, occasionally warning/danger
        pm25 = round(random.choices(
            population=[random.uniform(5, 25),    # normal
                        random.uniform(25, 50),   # warning band
                        random.uniform(50, 90)],  # danger
            weights=[0.6, 0.25, 0.15],
            k=1,
        )[0], 2)
        # PM10 is typically a bit higher than PM2.5
        pm10 = round(pm25 * random.uniform(1.2, 1.8), 2)

        # Real Tasmota-style nested payload
        payload = json.dumps({
            "Time": datetime.now().isoformat(timespec="seconds"),
            "SDS0X1": {
                "PM2.5": pm25,
                "PM10": pm10,
            },
        })

        topic = TOPIC_TEMPLATE.format(device=device)
        info = client.publish(topic, payload)
        status = "OK" if info.rc == mqtt.MQTT_ERR_SUCCESS else f"ERR {info.rc}"
        print(f"  [{status}] {topic}")
        print(f"         {payload}")

        print(f"  … sleeping {PUBLISH_INTERVAL}s\n")
        time.sleep(PUBLISH_INTERVAL)

except KeyboardInterrupt:
    print("\nStopped by user.")
except Exception as exc:
    print(f"\n❌ MQTT error: {exc}")
    print("Tip: ensure the stack is up (docker compose up -d) and 1883 is published.")
finally:
    try:
        client.loop_stop()
        client.disconnect()
    except Exception:
        pass
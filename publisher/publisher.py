"""
Mock MQTT Publisher — containerized test-data generator
========================================================
Publishes Tasmota-format air-quality messages so the dashboard can be tested
without any real sensors, and WITHOUT installing Python on the host machine.

Runs inside Docker, so it connects to the broker by its Compose service name
(mqtt_broker), not localhost.
"""

import json
import os
import random
import time
from datetime import datetime

import paho.mqtt.client as mqtt

# ── Configuration (from environment / docker-compose) ────────────────────────
BROKER = os.getenv("MQTT_BROKER_HOST", "mqtt_broker")
PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
TOPIC_TEMPLATE = os.getenv("TOPIC_TEMPLATE", "InnoLab/tele/{device}/SENSOR")
PUBLISH_INTERVAL = float(os.getenv("PUBLISH_INTERVAL", "3"))
DEVICES = os.getenv("DEVICES", "tasmota_room1,tasmota_room2,tasmota_room3").split(",")

print("=" * 60)
print("  Mock MQTT Publisher (Tasmota format)")
print(f"  Broker : {BROKER}:{PORT}")
print(f"  Topic  : {TOPIC_TEMPLATE}")
print(f"  Rate   : 1 message every {PUBLISH_INTERVAL}s")
print(f"  Devices: {', '.join(DEVICES)}")
print("=" * 60, flush=True)

# Version-proof client creation (works on paho 1.x and 2.x)
try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
except AttributeError:
    client = mqtt.Client()


def on_connect(client, userdata, flags, rc, *args):
    print(f"✅ Connected to broker (rc={rc})" if rc == 0
          else f"❌ Connect failed (rc={rc})", flush=True)


client.on_connect = on_connect

# Retry the initial connection — the broker may still be starting up
for attempt in range(1, 31):
    try:
        print(f"Connecting to {BROKER}:{PORT} (attempt {attempt}) …", flush=True)
        client.connect(BROKER, PORT, keepalive=60)
        break
    except Exception as exc:
        print(f"  not ready ({exc}); retrying in 2s", flush=True)
        time.sleep(2)
else:
    raise SystemExit("Could not reach the broker after 30 attempts.")

client.loop_start()
time.sleep(1)
print("Publishing… (stop with: docker compose stop mock_publisher)\n", flush=True)

cycle = 0
while True:
    device = DEVICES[cycle % len(DEVICES)]
    cycle += 1

    # Mostly normal, sometimes warning, occasionally danger
    pm25 = round(random.choices(
        population=[random.uniform(5, 25),
                    random.uniform(25, 50),
                    random.uniform(50, 90)],
        weights=[0.6, 0.25, 0.15],
        k=1,
    )[0], 2)
    pm10 = round(pm25 * random.uniform(1.2, 1.8), 2)

    payload = json.dumps({
        "Time": datetime.now().isoformat(timespec="seconds"),
        "SDS0X1": {"PM2.5": pm25, "PM10": pm10},
    })

    topic = TOPIC_TEMPLATE.format(device=device)
    info = client.publish(topic, payload)
    status = "OK" if info.rc == mqtt.MQTT_ERR_SUCCESS else f"ERR {info.rc}"
    print(f"[{status}] {topic}  PM2.5={pm25}", flush=True)

    time.sleep(PUBLISH_INTERVAL)

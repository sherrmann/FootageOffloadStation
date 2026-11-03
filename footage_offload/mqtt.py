"""Publish the live status to an MQTT broker (optional; needs python3-paho-mqtt).

Topics (below `mqtt_topic`): `status` (retained JSON, throttled to once per
2 s) and `availability` (`online` / `offline`, the latter as last will).
A broker that is down never affects the service.
"""
import json
import logging
import threading
import time

log = logging.getLogger(__name__)

MIN_PUBLISH_INTERVAL_S = 2.0
_MQTT_KEYS = ("mqtt_enabled", "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password",
              "mqtt_topic")


def _import_paho():
    try:
        import paho.mqtt.client as client
        return client
    except ImportError:
        return None


class MqttPublisher:
    def __init__(self, config, status):
        self.config = config
        self.status = status
        self._lock = threading.RLock()
        self._client = None
        self._topic = None
        self._last_publish = 0.0
        self._timer = None
        self._latest = None
        self._subscribed = False

    # lifecycle ---------------------------------------------------------------
    def start(self):
        if not self._subscribed:
            self.status.subscribe(self._on_status)
            self.config.on_change(self._on_config)
            self._subscribed = True
        self._connect()

    def stop(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._disconnect()
            if self._subscribed:
                self.status.unsubscribe(self._on_status)
                self._subscribed = False

    def _on_config(self, changes):
        if any(k in changes for k in _MQTT_KEYS):
            self._connect()

    # connection --------------------------------------------------------------
    def _disconnect(self):
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.publish(f"{self._topic}/availability", "offline", qos=1, retain=True)
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001
            log.debug("MQTT disconnect failed", exc_info=True)

    def _connect(self):
        with self._lock:
            self._disconnect()
            if not self.config.get("mqtt_enabled") or not self.config.get("mqtt_host"):
                return
            mqtt = _import_paho()
            if mqtt is None:
                log.warning("MQTT enabled but python3-paho-mqtt is not installed")
                return
            try:
                self._topic = self.config.get("mqtt_topic").strip("/")
                if hasattr(mqtt, "CallbackAPIVersion"):
                    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                else:
                    client = mqtt.Client()
                user = self.config.get("mqtt_username")
                if user:
                    client.username_pw_set(user, self.config.get("mqtt_password") or None)
                client.will_set(f"{self._topic}/availability", "offline", qos=1, retain=True)
                client.on_connect = self._on_connect
                client.reconnect_delay_set(min_delay=1, max_delay=120)
                client.connect_async(self.config.get("mqtt_host"), int(self.config.get("mqtt_port")),
                                     keepalive=60)
                client.loop_start()
                self._client = client
                self._last_publish = 0.0
            except Exception:  # noqa: BLE001 - never crash the service
                log.exception("MQTT setup failed")
                self._client = None

    def _on_connect(self, client, userdata, *args):
        # Signature differs between paho 1.x (flags, rc) and 2.x (flags, rc, properties).
        try:
            client.publish(f"{self._topic}/availability", "online", qos=1, retain=True)
            self._publish(self.status.snapshot())
        except Exception:  # noqa: BLE001
            log.debug("MQTT on_connect failed", exc_info=True)

    # publishing --------------------------------------------------------------
    def _on_status(self, snap):
        with self._lock:
            if self._client is None:
                return
            self._latest = snap
            wait = MIN_PUBLISH_INTERVAL_S - (time.monotonic() - self._last_publish)
            if wait <= 0:
                self._publish(snap)
            elif self._timer is None:
                self._timer = threading.Timer(wait, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self):
        with self._lock:
            self._timer = None
            if self._latest is not None:
                self._publish(self._latest)

    def _publish(self, snap):
        with self._lock:
            client = self._client
            if client is None:
                return
            self._last_publish = time.monotonic()
            self._latest = None
            try:
                payload = json.dumps(snap, default=str)
                client.publish(f"{self._topic}/status", payload, qos=0, retain=True)
            except Exception:  # noqa: BLE001
                log.debug("MQTT publish failed", exc_info=True)

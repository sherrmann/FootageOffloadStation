import json
import sys
import types

import pytest

from footage_offload import mqtt as mqtt_mod
from footage_offload.status import StatusBus


class FakeClient:
    instances = []

    def __init__(self, *args):
        self.args = args
        self.published = []
        self.will = None
        self.connected_to = None
        self.looping = False
        self.creds = None
        FakeClient.instances.append(self)

    def username_pw_set(self, u, p=None):
        self.creds = (u, p)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def reconnect_delay_set(self, **kw):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.connected_to = (host, port)

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))


def install(monkeypatch, v2):
    FakeClient.instances = []
    pkg = types.ModuleType("paho")
    sub = types.ModuleType("paho.mqtt")
    client = types.ModuleType("paho.mqtt.client")
    client.Client = FakeClient
    if v2:
        client.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
    pkg.mqtt = sub
    sub.client = client
    monkeypatch.setitem(sys.modules, "paho", pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt", sub)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client)


class Cfg(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.listeners = []

    def get(self, k):
        return self[k]

    def on_change(self, cb):
        self.listeners.append(cb)


@pytest.fixture
def mcfg(cfg):
    c = Cfg(cfg)
    c.update(mqtt_enabled=True, mqtt_host="broker", mqtt_username="u", mqtt_password="p")
    return c


@pytest.mark.parametrize("v2", [False, True])
def test_connect_and_publish(monkeypatch, mcfg, v2):
    install(monkeypatch, v2)
    status = StatusBus(min_interval=0)
    pub = mqtt_mod.MqttPublisher(mcfg, status)
    pub.start()
    c = FakeClient.instances[0]
    assert c.args == (("v2",) if v2 else ())
    assert c.connected_to == ("broker", 1883) and c.looping
    assert c.will == ("footage-offload/availability", "offline", True)
    assert c.creds == ("u", "p")
    pub._on_connect(c, None, {}, 0)
    assert ("footage-offload/availability", "online", True) in c.published
    status.update(state="transferring")
    topics = [t for t, _, _ in c.published]
    assert "footage-offload/status" in topics
    payload = [p for t, p, r in c.published if t.endswith("/status")][-1]
    assert json.loads(payload)["state"] in ("waiting", "transferring")
    pub.stop()
    assert not c.looping


def test_throttle(monkeypatch, mcfg):
    install(monkeypatch, False)
    status = StatusBus(min_interval=0)
    pub = mqtt_mod.MqttPublisher(mcfg, status)
    pub.start()
    c = FakeClient.instances[0]
    for i in range(5):
        status.update(files_done=i)
    n = len([1 for t, *_ in c.published if t.endswith("/status")])
    assert n == 1
    assert pub._timer is not None
    pub.stop()


def test_disabled_or_no_paho(monkeypatch, mcfg):
    install(monkeypatch, False)
    mcfg["mqtt_enabled"] = False
    pub = mqtt_mod.MqttPublisher(mcfg, StatusBus())
    pub.start()
    assert FakeClient.instances == []
    mcfg["mqtt_enabled"] = True
    monkeypatch.setattr(mqtt_mod, "_import_paho", lambda: None)
    pub._connect()
    assert pub._client is None
    pub.stop()


def test_reconfigure(monkeypatch, mcfg):
    install(monkeypatch, False)
    pub = mqtt_mod.MqttPublisher(mcfg, StatusBus())
    pub.start()
    mcfg["mqtt_host"] = "other"
    mcfg.listeners[0]({"mqtt_host": "other"})
    assert len(FakeClient.instances) == 2
    assert FakeClient.instances[1].connected_to[0] == "other"
    mcfg.listeners[0]({"language": "de"})
    assert len(FakeClient.instances) == 2
    pub.stop()


def test_broker_errors_do_not_crash(monkeypatch, mcfg):
    install(monkeypatch, False)
    def bad(self, *a, **k):
        raise OSError("down")
    monkeypatch.setattr(FakeClient, "connect_async", bad)
    pub = mqtt_mod.MqttPublisher(mcfg, StatusBus())
    pub.start()
    assert pub._client is None

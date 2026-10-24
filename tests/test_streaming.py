"""Streaming ingestion against real protocol implementations:
Kafka   - a Kafka-protocol broker (franz-go kfake, built by tests/kafka_broker/build.sh) with librdkafka clients
MQTT    - the amqtt broker in-process, paho client, QoS 1 with manual acknowledgement
Kinesis - moto's Kinesis implementation through boto3
HTTP    - the push endpoint with stream tokens
"""
import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import Client, add_user, do_setup

HERE = Path(__file__).parent
KFAKE = os.environ.get("TESSERA_TEST_KAFKA_BIN") or str(HERE / "kafka_broker" / "kfake")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_for(fn, timeout=20.0, every=0.1):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out; last={last!r}")


def rows(c, sql):
    r = c.post("/api/sql", json={"sql": sql})
    assert r.status_code == 200, r.text
    j = r.json()
    cols = [x["name"] if isinstance(x, dict) else x for x in j["columns"]]
    return [dict(zip(cols, row)) for row in j["rows"]]


def count(c, table):
    r = c.post("/api/sql", json={"sql": f"SELECT count(*) AS n FROM {table}"})
    return r.json()["rows"][0][0] if r.status_code == 200 else 0


# ------------------------------------------------------------------ HTTP push
def test_http_push_stream(admin, app):
    r = admin.post("/api/streams", json={"name": "web events", "kind": "webhook", "target": "raw.web_events",
                                          "config": {"dedupe_key": "event_id"}, "start": True})
    assert r.status_code == 200, r.text
    s = r.json()
    token, sid = s["token"], s["id"]
    assert token.startswith("tsk_") and "token_hash" not in s
    anon = Client(app)
    # wrong token, no token
    assert anon.post(f"/api/streams/{sid}/events", json={"a": 1}, csrf=False).status_code == 401
    assert anon.post(f"/api/streams/{sid}/events", json={"a": 1}, csrf=False,
                     headers={"Authorization": "Bearer tsk_nope"}).status_code == 401
    h = {"Authorization": f"Bearer {token}"}
    r = anon.post(f"/api/streams/{sid}/events", csrf=False, headers=h,
                  json=[{"event_id": "e1", "user": "u1", "amount": 5}, {"event_id": "e2", "user": "u2", "amount": 7}])
    assert r.status_code == 200, r.text and r.json()["rows"] == 2
    # NDJSON, a replay of e2 and a new field: dedupe drops the replay, schema grows
    body = '{"event_id":"e2","user":"u2","amount":7}\n{"event_id":"e3","user":"u3","amount":1,"country":"CA"}\n'
    r = anon.post(f"/api/streams/{sid}/events", csrf=False, content=body,
                  headers={**h, "Content-Type": "application/x-ndjson"})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 1
    got = rows(admin, "SELECT event_id, country, _ingested_at FROM raw.web_events ORDER BY event_id")
    assert [g["event_id"] for g in got] == ["e1", "e2", "e3"]
    assert got[2]["country"] == "CA" and got[0]["_ingested_at"]
    st = admin.get(f"/api/streams/{sid}").json()
    assert st["meta"]["rows_total"] == 3 and st["meta"]["events_total"] == 4 and st["events_per_min"] == 4
    # catalog and lineage know about it
    ds = admin.get("/api/datasets").json()
    d = next(x for x in ds if x["schema_name"] == "raw" and x["name"] == "web_events")
    full = admin.get(f"/api/datasets/{d['id']}").json()
    assert "stream" in json.dumps(full.get("source") or full)
    # paused streams refuse events; rotated tokens invalidate the old one
    admin.post(f"/api/streams/{sid}/stop")
    assert anon.post(f"/api/streams/{sid}/events", csrf=False, headers=h, json={"event_id": "x"}).status_code == 400
    admin.post(f"/api/streams/{sid}/start")
    new = admin.post(f"/api/streams/{sid}/token").json()["token"]
    assert anon.post(f"/api/streams/{sid}/events", csrf=False, headers=h, json={"event_id": "x"}).status_code == 401
    assert anon.post(f"/api/streams/{sid}/events", csrf=False, headers={"Authorization": f"Bearer {new}"},
                     json={"events": [{"event_id": "e4"}]}).status_code == 200
    # garbage and oversize
    assert anon.post(f"/api/streams/{sid}/events", csrf=False, content="not json",
                     headers={"Authorization": f"Bearer {new}"}).status_code == 400
    big = "x" * (5 * 1024 * 1024 + 10)
    assert anon.post(f"/api/streams/{sid}/events", csrf=False, content=big,
                     headers={"Authorization": f"Bearer {new}"}).status_code == 413


def test_stream_validation_and_roles(admin, app):
    bad = [
        ({"name": "", "kind": "webhook", "target": "raw.a"}, "name"),
        ({"name": "x", "kind": "carrier-pigeon", "target": "raw.a"}, "Kind"),
        ({"name": "x", "kind": "webhook", "target": "serving.a"}, "raw zone"),
        ({"name": "x", "kind": "kafka", "target": "raw.a"}, "connection"),
        ({"name": "x", "kind": "webhook", "target": "raw.a", "config": {"format": "avro"}}, "json or text"),
        ({"name": "x", "kind": "webhook", "target": "raw.\"; drop"}, ""),
    ]
    for body, msg in bad:
        r = admin.post("/api/streams", json=body)
        assert r.status_code == 400, (body, r.text)
        assert msg in r.json()["detail"], (body, r.text)
    # a file connection cannot back a Kafka stream
    fc = admin.post("/api/connections", json={"type": "file", "name": "files", "config": {}}).json()
    r = admin.post("/api/streams", json={"name": "x", "kind": "kafka", "connection_id": fc["id"], "target": "raw.a",
                                          "config": {"topic": "t"}})
    assert r.status_code == 400 and "not kafka" in r.json()["detail"]
    # batch ingest from a streaming connection explains itself
    kc = admin.post("/api/connections", json={"type": "kafka", "name": "k", "config": {"bootstrap_servers": "127.0.0.1:1"}}).json()
    r = admin.post(f"/api/connections/{kc['id']}/ingest", json={"source": {"path": "x"}, "target": "raw.k"})
    assert r.status_code == 400 and "streaming source" in r.json()["detail"]
    # roles: analysts can watch, not manage; viewers see nothing
    admin.post("/api/streams", json={"name": "ok", "kind": "webhook", "target": "raw.ok"})
    analyst = add_user(admin, app, "ana@acme.ca", "analyst")
    viewer = add_user(admin, app, "vic@acme.ca", "viewer")
    assert analyst.get("/api/streams").status_code == 200
    assert analyst.post("/api/streams", json={"name": "n", "kind": "webhook", "target": "raw.n"},
                        headers={"x-tessera-csrf": "1"}).status_code == 403
    assert viewer.get("/api/streams").status_code == 403
    # catalog shows streaming types
    cat = admin.get("/api/connectors").json()["types"]
    kinds = {c["type"]: c for c in cat}
    assert {"kafka", "eventhubs", "kinesis", "mqtt"} <= set(kinds)
    assert kinds["kafka"]["category"] == "Streaming"


def test_eventhubs_maps_to_kafka():
    from tessera.streaming import kafka_conf
    c = kafka_conf({"namespace": "contoso"}, {"connection_string": "Endpoint=sb://contoso/;SharedAccessKey=k"}, "eventhubs")
    assert c["bootstrap.servers"] == "contoso.servicebus.windows.net:9093"
    assert c["security.protocol"] == "SASL_SSL" and c["sasl.username"] == "$ConnectionString"
    c = kafka_conf({"bootstrap_servers": "b:9092", "security_protocol": "SASL_SSL", "sasl_mechanism": "SCRAM-SHA-512",
                    "username": "key"}, {"password": "sec"})
    assert c["sasl.mechanisms"] == "SCRAM-SHA-512" and c["sasl.password"] == "sec"


# ------------------------------------------------------------------ Kafka
@pytest.fixture
def kafka():
    if not Path(KFAKE).exists():
        pytest.skip("Kafka test broker not built (run tests/kafka_broker/build.sh)")
    pytest.importorskip("confluent_kafka")
    port = free_port()
    p = subprocess.Popen([KFAKE, str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert b"ready" in p.stdout.readline()
    yield f"127.0.0.1:{port}"
    p.terminate()
    p.wait(5)


def produce(bootstrap, topic, values, keys=None):
    from confluent_kafka import Producer
    pr = Producer({"bootstrap.servers": bootstrap})
    for i, v in enumerate(values):
        pr.produce(topic, v if isinstance(v, bytes) else json.dumps(v).encode(), key=(keys[i] if keys else None))
    assert pr.flush(10) == 0


def test_kafka_stream_end_to_end(admin, app, kafka):
    conn = admin.post("/api/connections", json={"type": "kafka", "name": "local kafka",
                                                 "config": {"bootstrap_servers": kafka}}).json()
    t = admin.post(f"/api/connections/{conn['id']}/test").json()
    assert t["ok"], t
    assert "orders" in t["message"]
    topics = {x["name"]: x for x in admin.get(f"/api/connections/{conn['id']}/browse").json()}
    assert topics["orders"]["partitions"] == 3
    produce(kafka, "orders", [{"order_id": i, "amount": i * 2.5, "region": "west" if i % 2 else "east"} for i in range(50)],
            keys=[str(i).encode() for i in range(50)])
    s = admin.post("/api/streams", json={"name": "orders live", "kind": "kafka", "connection_id": conn["id"],
                                          "target": "raw.orders_live",
                                          "config": {"topic": "orders", "max_wait_s": 0.5, "max_batch": 20}}).json()
    # preview reads without moving the position
    pv = admin.post(f"/api/streams/{s['id']}/preview").json()
    assert len(pv["rows"]) == 20 and "_offset" in pv["rows"][0]
    assert not (admin.get(f"/api/streams/{s['id']}").json()["state"] or {}).get("positions")
    admin.post(f"/api/streams/{s['id']}/start")
    wait_for(lambda: count(admin, "raw.orders_live") == 50)
    st = wait_for(lambda: (lambda x: x if x["lag"] == 0 else None)(admin.get(f"/api/streams/{s['id']}").json()))
    assert st["status"] == "running" and st["meta"]["batches"] >= 3  # max_batch 20 splits the backlog
    assert sum(int(v) for v in st["state"]["positions"].values()) == 50
    # keys, partitions and broker timestamps land as columns
    r = rows(admin, "SELECT count(DISTINCT _partition) AS p, min(_event_time) IS NOT NULL AS ts, count(DISTINCT _key) AS k FROM raw.orders_live")[0]
    assert r == {"p": 3, "ts": True, "k": 50}
    # new events keep flowing
    produce(kafka, "orders", [{"order_id": 100 + i, "amount": 1.0, "region": "north"} for i in range(5)])
    wait_for(lambda: count(admin, "raw.orders_live") == 55)
    # stop, produce while stopped, restart: resumes from saved offsets with no loss or duplicates
    admin.post(f"/api/streams/{s['id']}/stop")
    produce(kafka, "orders", [{"order_id": 200 + i, "amount": 1.0} for i in range(7)])
    time.sleep(0.5)
    assert count(admin, "raw.orders_live") == 55
    admin.post(f"/api/streams/{s['id']}/start")
    wait_for(lambda: count(admin, "raw.orders_live") == 62)
    assert rows(admin, "SELECT count(*) - count(DISTINCT order_id) AS d FROM raw.orders_live")[0]["d"] == 0
    # lineage
    lin = json.dumps(admin.get("/api/lineage").json())
    assert "stream:kafka/orders live" in lin
    # cannot edit a running stream; can after stop; reset to latest skips the backlog
    assert admin.put(f"/api/streams/{s['id']}", json={"config": {"max_batch": 10}}).status_code == 400
    admin.post(f"/api/streams/{s['id']}/stop")
    assert admin.put(f"/api/streams/{s['id']}", json={"config": {"max_batch": 10}}).json()["config"]["max_batch"] == 10
    assert admin.post(f"/api/streams/{s['id']}/reset", json={"to": "latest"}).status_code == 200
    produce(kafka, "orders", [{"order_id": 999}])  # arrives before restart; 'latest' was resolved at restart
    admin.post(f"/api/streams/{s['id']}/start")
    time.sleep(1.5)
    produce(kafka, "orders", [{"order_id": 1000}])
    wait_for(lambda: rows(admin, "SELECT count(*) AS n FROM raw.orders_live WHERE order_id=1000")[0]["n"] == 1)
    admin.post(f"/api/streams/{s['id']}/stop")


def test_kafka_rejects_bad_payloads(admin, kafka):
    conn = admin.post("/api/connections", json={"type": "kafka", "name": "k2", "config": {"bootstrap_servers": kafka}}).json()
    produce(kafka, "mixed", [{"id": 1}, b"{not json", {"id": 2}, b"\xff\xfe"])
    s = admin.post("/api/streams", json={"name": "mixed", "kind": "kafka", "connection_id": conn["id"],
                                          "target": "raw.mixed", "config": {"topic": "mixed", "max_wait_s": 0.5},
                                          "start": True}).json()
    wait_for(lambda: count(admin, "raw.mixed") == 2 and count(admin, "raw.mixed__rejects") == 2)
    rej = {r["payload"]: r["error"] for r in rows(admin, "SELECT payload, error FROM raw.mixed__rejects")}
    assert "JSON" in rej["{not json"] and len(rej) == 2
    st = admin.get(f"/api/streams/{s['id']}").json()
    assert st["meta"]["rejects_total"] == 2
    admin.post(f"/api/streams/{s['id']}/stop")


def test_kafka_missing_topic_reports_retrying(admin, app, kafka):
    """A stream on a topic that does not exist retries with a readable reason; other streams keep running."""
    conn = admin.post("/api/connections", json={"type": "kafka", "name": "k3", "config": {"bootstrap_servers": kafka}}).json()
    produce(kafka, "clicks", [{"n": i} for i in range(10)])
    s = admin.post("/api/streams", json={"name": "clicks", "kind": "kafka", "connection_id": conn["id"], "target": "raw.clicks",
                                          "config": {"topic": "clicks", "max_wait_s": 0.3}, "start": True}).json()
    wait_for(lambda: count(admin, "raw.clicks") == 10)
    bad = admin.post("/api/streams", json={"name": "ghost", "kind": "kafka", "connection_id": conn["id"], "target": "raw.ghost",
                                            "config": {"topic": "no_such_topic"}, "start": True}).json()
    st = wait_for(lambda: (lambda x: x if x["status"] == "retrying" else None)(admin.get(f"/api/streams/{bad['id']}").json()))
    assert "no_such_topic" in st["last_error"]
    produce(kafka, "clicks", [{"n": 10}])
    wait_for(lambda: count(admin, "raw.clicks") == 11)
    r = admin.delete(f"/api/streams/{bad['id']}")
    assert r.status_code == 200
    admin.post(f"/api/streams/{s['id']}/stop")


def test_streams_resume_after_restart(settings, kafka):
    """Streams that were running come back after a restart and continue from their saved offsets."""
    from tessera.app import create_app
    a = create_app(settings, start_scheduler=True)
    with Client(a) as c:
        do_setup(c, settings)
        conn = c.post("/api/connections", json={"type": "kafka", "name": "k", "config": {"bootstrap_servers": kafka}}).json()
        produce(kafka, "orders", [{"i": i} for i in range(5)])
        c.post("/api/streams", json={"name": "o", "kind": "kafka", "connection_id": conn["id"], "target": "raw.o",
                                     "config": {"topic": "orders", "max_wait_s": 0.3}, "start": True})
        wait_for(lambda: count(c, "raw.o") == 5)
        email, pw = "admin@acme.ca", "correct horse battery 9"
    produce(kafka, "orders", [{"i": 5 + i} for i in range(3)])
    b = create_app(settings, start_scheduler=True)
    with Client(b) as c:
        assert c.post("/api/auth/login", json={"email": email, "password": pw}).status_code == 200
        wait_for(lambda: count(c, "raw.o") == 8)
        assert rows(c, "SELECT count(DISTINCT i) AS n FROM raw.o")[0]["n"] == 8


# ------------------------------------------------------------------ MQTT
@pytest.fixture
def mqtt_broker():
    pytest.importorskip("amqtt")
    pytest.importorskip("paho.mqtt")
    from amqtt.broker import Broker
    port = free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}

    def run():
        asyncio.set_event_loop(loop)
        cfg = {"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
               "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}}
        b = Broker(cfg, loop=loop)
        holder["b"] = b
        loop.run_until_complete(b.start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(10)
    yield port
    fut = asyncio.run_coroutine_threadsafe(holder["b"].shutdown(), loop)
    try:
        fut.result(10)
    except Exception:  # noqa: BLE001, S110
        pass
    loop.call_soon_threadsafe(loop.stop)


def mqtt_publish(port, topic, payloads):
    import paho.mqtt.client as mqtt
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"pub-{time.time_ns()}")
    c.connect("127.0.0.1", port)
    c.loop_start()
    for p in payloads:
        c.publish(topic, json.dumps(p) if not isinstance(p, (bytes, str)) else p, qos=1).wait_for_publish(5)
    c.loop_stop()
    c.disconnect()


def test_mqtt_stream(admin, mqtt_broker):
    conn = admin.post("/api/connections", json={"type": "mqtt", "name": "plant floor",
                                                 "config": {"host": "127.0.0.1", "port": mqtt_broker}}).json()
    t = admin.post(f"/api/connections/{conn['id']}/test").json()
    assert t["ok"], t
    s = admin.post("/api/streams", json={"name": "sensors", "kind": "mqtt", "connection_id": conn["id"],
                                          "target": "raw.sensors", "config": {"topic": "plant/+/temp", "max_wait_s": 0.3},
                                          "start": True}).json()
    wait_for(lambda: admin.get(f"/api/streams/{s['id']}").json()["status"] == "running")
    time.sleep(0.3)
    mqtt_publish(mqtt_broker, "plant/line1/temp", [{"c": 21.5 + i} for i in range(5)])
    mqtt_publish(mqtt_broker, "plant/line2/temp", [{"c": 30.0}])
    mqtt_publish(mqtt_broker, "plant/line2/pressure", [{"kpa": 101}])  # not subscribed
    wait_for(lambda: count(admin, "raw.sensors") == 6)
    got = rows(admin, "SELECT _key, count(*) AS n FROM raw.sensors GROUP BY 1 ORDER BY 1")
    assert got == [{"_key": "plant/line1/temp", "n": 5}, {"_key": "plant/line2/temp", "n": 1}]
    admin.post(f"/api/streams/{s['id']}/stop")


# ------------------------------------------------------------------ Kinesis
@pytest.fixture
def kinesis_env(settings):
    moto = pytest.importorskip("moto")
    import boto3
    m = moto.mock_aws()
    m.start()
    cl = boto3.client("kinesis", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    cl.create_stream(StreamName="clickstream", ShardCount=2)
    from tessera.app import create_app
    a = create_app(settings, start_scheduler=False,
                   kinesis_factory=lambda cfg, sec: boto3.client("kinesis", region_name=cfg.get("region") or "us-east-1",
                                                                 aws_access_key_id="x", aws_secret_access_key="y"))
    with Client(a) as c:
        do_setup(c, settings)
        yield c, cl
    m.stop()


def test_kinesis_stream(kinesis_env):
    admin, cl = kinesis_env
    for i in range(30):
        cl.put_record(StreamName="clickstream", Data=json.dumps({"click": i, "page": f"/p{i % 3}"}).encode(), PartitionKey=f"u{i}")
    conn = admin.post("/api/connections", json={"type": "kinesis", "name": "aws", "config": {"region": "us-east-1"}}).json()
    s = admin.post("/api/streams", json={"name": "clicks", "kind": "kinesis", "connection_id": conn["id"],
                                          "target": "raw.kclicks", "config": {"stream": "clickstream", "max_wait_s": 0.5},
                                          "start": True}).json()
    wait_for(lambda: count(admin, "raw.kclicks") == 30)
    st = admin.get(f"/api/streams/{s['id']}").json()
    assert set(st["state"]["positions"]) == {x["ShardId"] for x in cl.list_shards(StreamName="clickstream")["Shards"]}
    admin.post(f"/api/streams/{s['id']}/stop")
    for i in range(30, 35):
        cl.put_record(StreamName="clickstream", Data=json.dumps({"click": i}).encode(), PartitionKey=f"u{i}")
    admin.post(f"/api/streams/{s['id']}/start")
    wait_for(lambda: count(admin, "raw.kclicks") == 35)
    assert rows(admin, "SELECT count(DISTINCT click) AS n FROM raw.kclicks")[0]["n"] == 35
    admin.post(f"/api/streams/{s['id']}/stop")


def test_retention_trims_old_events(admin, app):
    s = admin.post("/api/streams", json={"name": "short", "kind": "webhook", "target": "raw.short",
                                          "config": {"retention_days": 1}, "start": True}).json()
    anon = Client(app)
    anon.post(f"/api/streams/{s['id']}/events", csrf=False, headers={"Authorization": f"Bearer {s['token']}"},
              json=[{"i": 1}, {"i": 2}])
    mgr = app.state.streams
    mgr._internal("UPDATE raw.short SET _ingested_at = CAST(now() AS TIMESTAMP) - INTERVAL 3 DAY WHERE i = 1")
    assert mgr.apply_retention(mgr.get(s["id"])) == 1
    assert count(admin, "raw.short") == 1


def test_data_health_reports_streams(admin, app):
    """Agents and MCP clients see stream health through the data_health tool."""
    from tessera import agents
    s = admin.post("/api/streams", json={"name": "broken", "kind": "webhook", "target": "raw.broken", "start": True}).json()
    app.state.platform.store.update("streams", s["id"], {"status": "retrying", "last_error": "broker unreachable"})
    me = app.state.platform.store.one("SELECT * FROM users WHERE email='admin@acme.ca'")
    out = agents.t_data_health(agents.Ctx(app.state.platform, me), {})
    assert out["failing_streams"] == ["broken"] and out["healthy"] is False
    assert out["streams"][0]["error"] == "broker unreachable"


def test_demo_feed_runs_and_is_removed_with_demo(admin, app):
    r = admin.post("/api/demo")
    assert r.status_code == 200, r.text
    sid = r.json()["stream_id"]
    assert count(admin, "raw.checkout_events") == 720
    s = admin.get(f"/api/streams/{sid}").json()
    assert s["simulated"] and s["kind"] == "webhook"
    # the simulated feed has no push token and cannot be posted to
    assert Client(app).post(f"/api/streams/{sid}/events", csrf=False, headers={"Authorization": "Bearer tsk_x"},
                            json={"a": 1}).status_code == 401
    admin.post(f"/api/streams/{sid}/start")
    wait_for(lambda: count(admin, "raw.checkout_events") > 720, timeout=15)
    plan = admin.get("/api/demo/plan").json()
    assert plan["counts"].get("streams") == 1 and "raw.checkout_events" in plan["tables"]
    r = admin.delete("/api/demo")
    assert r.status_code == 200
    assert admin.get("/api/streams").json() == []
    assert count(admin, "raw.checkout_events") == 0
    assert not app.state.streams.running(sid)

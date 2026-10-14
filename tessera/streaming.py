"""Streaming ingestion: Kafka (and everything that speaks its protocol), Amazon Kinesis, MQTT and HTTP push.

How it works
  Each stream runs as a background worker that reads micro-batches (a few hundred to a few thousand events,
  or whatever arrived within `max_wait_s`) and appends them to a raw-zone table. Positions (Kafka offsets,
  Kinesis sequence numbers) are stored by Tessera and saved only after the batch is in the warehouse, so a
  crash replays the last batch rather than losing it: delivery is at-least-once. Set `dedupe_key` to make
  replays harmless for downstream models.

What it is not
  This is micro-batch ingestion into a single-node warehouse. End-to-end latency is a second or two plus the
  batch window. It is not a stream processor (no windowed joins over unbounded streams, no sub-second SLAs).
  For that, keep Flink/Spark/ksqlDB upstream and land their output here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets as pysecrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pyarrow as pa

from .connectors import ConnectorError, guard_url, normalize_records
from .store import now
from .warehouse import QueryError, qi, split_fqn

log = logging.getLogger("tessera.streaming")

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, connection_id INTEGER,
  config TEXT NOT NULL DEFAULT '{}', target TEXT NOT NULL, desired TEXT NOT NULL DEFAULT 'stopped',
  status TEXT NOT NULL DEFAULT 'stopped', state TEXT NOT NULL DEFAULT '{}', meta TEXT NOT NULL DEFAULT '{}',
  last_error TEXT, token_hash TEXT, token_prefix TEXT, workspace_id INTEGER NOT NULL DEFAULT 1,
  created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

KINDS = {
    "kafka": "Kafka-protocol topic (Apache Kafka, Confluent, Redpanda, Amazon MSK, Azure Event Hubs, Aiven, WarpStream)",
    "kinesis": "Amazon Kinesis Data Stream",
    "mqtt": "MQTT topic (IoT devices, AWS IoT Core, Azure IoT/Event Grid MQTT, HiveMQ, Mosquitto, EMQX)",
    "webhook": "HTTP push: any system that can POST JSON (webhooks, Segment, Snowplow, app events)",
}
CONNECTION_KIND = {"kafka": "kafka", "eventhubs": "kafka", "kinesis": "kinesis", "mqtt": "mqtt"}
FORMATS = ("json", "text")
META_COLS = ("_key", "_partition", "_offset", "_event_time", "_ingested_at")
DEFAULTS = {"format": "json", "start": "earliest", "max_batch": 5000, "max_wait_s": 2.0, "retention_days": None,
            "dedupe_key": None}


class StreamError(Exception):
    pass


def ensure_schema(store) -> None:
    with store.lock:
        c = store.conn()
        c.executescript(SCHEMA)
        c.commit()


# ---------------------------------------------------------------- connection definitions
F = lambda key, label, **kw: {"key": key, "label": label, **kw}  # noqa: E731
CONNECTOR_TYPES = {
    "kafka": {"label": "Apache Kafka / Confluent / Redpanda / MSK", "category": "Streaming", "stream": True,
              "verified": True, "driver": "confluent_kafka", "pip": "confluent-kafka",
              "description": "Any Kafka-protocol broker. Reads topics into raw tables continuously with stored offsets.",
              "fields": [F("bootstrap_servers", "Bootstrap servers", required=True, placeholder="broker1:9092,broker2:9092"),
                         F("security_protocol", "Security", type="select", options=["PLAINTEXT", "SSL", "SASL_SSL", "SASL_PLAINTEXT"]),
                         F("sasl_mechanism", "SASL mechanism", type="select", options=["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"]),
                         F("username", "User or API key"), F("password", "Password or API secret", secret=True)]},
    "eventhubs": {"label": "Azure Event Hubs", "category": "Streaming", "stream": True, "driver": "confluent_kafka",
                  "pip": "confluent-kafka",
                  "description": "Event Hubs through its Kafka endpoint (Standard tier and above). Each event hub is a topic.",
                  "fields": [F("namespace", "Namespace", required=True, placeholder="contoso-events"),
                             F("connection_string", "Connection string", secret=True, required=True,
                               placeholder="Endpoint=sb://…;SharedAccessKeyName=…;SharedAccessKey=…")]},
    "kinesis": {"label": "Amazon Kinesis Data Streams", "category": "Streaming", "stream": True, "verified": True,
                "driver": "boto3", "pip": "boto3",
                "fields": [F("region", "Region", required=True, placeholder="us-east-1"),
                           F("endpoint_url", "Endpoint URL", placeholder="Only for LocalStack or VPC endpoints"),
                           F("access_key_id", "Access key ID"), F("secret_access_key", "Secret access key", secret=True)]},
    "mqtt": {"label": "MQTT broker", "category": "Streaming", "stream": True, "verified": True,
             "driver": "paho", "pip": "paho-mqtt",
             "fields": [F("host", "Host", required=True), F("port", "Port", type="number", placeholder="1883 or 8883"),
                        F("tls", "TLS", type="select", options=["off", "on"]),
                        F("username", "User"), F("password", "Password", secret=True)]},
}


def kafka_conf(cfg: dict, sec: dict, ctype: str = "kafka") -> dict:
    if ctype == "eventhubs":
        ns = (cfg.get("namespace") or "").strip()
        if not ns:
            raise ConnectorError("Namespace is required.")
        host = ns if "." in ns else f"{ns}.servicebus.windows.net"
        return {"bootstrap.servers": f"{host}:9093", "security.protocol": "SASL_SSL", "sasl.mechanisms": "PLAIN",
                "sasl.username": "$ConnectionString", "sasl.password": sec.get("connection_string", "")}
    conf = {"bootstrap.servers": cfg.get("bootstrap_servers", "")}
    proto = cfg.get("security_protocol") or "PLAINTEXT"
    conf["security.protocol"] = proto
    if proto.startswith("SASL"):
        conf["sasl.mechanisms"] = cfg.get("sasl_mechanism") or "PLAIN"
        conf["sasl.username"] = cfg.get("username", "")
        conf["sasl.password"] = sec.get("password", "")
    return conf


def _guard_hosts(hosts: list[str], settings) -> None:
    for h in hosts:
        h = h.strip()
        if h:
            guard_url(f"http://{h}", getattr(settings, "block_private_http", False))


# ---------------------------------------------------------------- readers
class Reader:
    """A position-aware reader. poll() returns (records, next_positions); nothing is committed by the reader."""

    lag_supported = False

    def poll(self, max_records: int, max_wait_s: float) -> tuple[list[dict], dict]:
        raise NotImplementedError

    def lag(self) -> int | None:
        return None

    def ack(self) -> None:
        """Called after a batch is safely in the warehouse (MQTT acknowledges here)."""

    def close(self) -> None:
        pass


def decode(value: bytes | str | None, fmt: str) -> tuple[dict | None, str | None]:
    """Decode one payload. Returns (record, error)."""
    if value is None:
        return {"value": None}, None
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
    if fmt == "text":
        return {"value": text}, None
    try:
        v = json.loads(text)
    except ValueError as e:
        return None, f"Not valid JSON: {e}"
    return (v if isinstance(v, dict) else {"value": v}), None


def _iso_ms(ms: int | float | None) -> str | None:
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


class KafkaReader(Reader):
    lag_supported = True
    consumer_factory = None  # tests may inject

    def __init__(self, conf: dict, topic: str, positions: dict, start: str, fmt: str, group: str):
        try:
            from confluent_kafka import Consumer, TopicPartition
        except ImportError:
            raise ConnectorError("Install confluent-kafka to read Kafka streams: pip install confluent-kafka") from None
        self.TP = TopicPartition
        base = {**conf, "group.id": group, "enable.auto.commit": False, "enable.partition.eof": False,
                "auto.offset.reset": "earliest" if start == "earliest" else "latest", "session.timeout.ms": 10000,
                "log_level": 3}
        factory = type(self).consumer_factory or Consumer
        self.c = factory(base)
        self.topic, self.fmt = topic, fmt
        md = self.c.list_topics(topic, timeout=10)
        t = md.topics.get(topic)
        if t is None or t.error is not None or not t.partitions:
            self.c.close()
            raise StreamError(f"Topic '{topic}' was not found or has no partitions.")
        self.partitions = sorted(t.partitions)
        assign = []
        for p in self.partitions:
            off = positions.get(str(p))
            if off is None:
                lo, hi = self.c.get_watermark_offsets(self.TP(topic, p), timeout=10)
                off = lo if start == "earliest" else hi
            assign.append(self.TP(topic, p, int(off)))
        self.c.assign(assign)
        self.next = {str(tp.partition): tp.offset for tp in assign}

    def poll(self, max_records, max_wait_s):
        out, deadline = [], time.monotonic() + max_wait_s
        nxt = dict(self.next)
        while len(out) < max_records:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            msgs = self.c.consume(min(500, max_records - len(out)), timeout=min(left, 1.0))
            for m in msgs:
                if m.error():
                    if m.error().fatal():
                        raise StreamError(str(m.error()))
                    continue
                rec, err = decode(m.value(), self.fmt)
                ts_type, ts = m.timestamp()
                meta = {"_key": m.key().decode("utf-8", "replace") if m.key() else None, "_partition": str(m.partition()),
                        "_offset": str(m.offset()), "_event_time": _iso_ms(ts if ts_type else None)}
                out.append({"record": rec, "error": err, "raw": None if rec else _raw(m.value()), **meta})
                nxt[str(m.partition())] = m.offset() + 1
            if not msgs and out:
                break  # data stopped arriving: flush what we have rather than waiting out the window
        self.pending = nxt
        return out, nxt

    def commit_local(self, positions: dict) -> None:
        self.next = dict(positions)

    def lag(self):
        total = 0
        for p in self.partitions:
            try:
                _lo, hi = self.c.get_watermark_offsets(self.TP(self.topic, p), timeout=5, cached=False)
            except Exception:  # noqa: BLE001
                return None
            total += max(0, hi - int(self.next.get(str(p), hi)))
        return total

    def close(self):
        try:
            self.c.close()
        except Exception:  # noqa: BLE001, S110
            pass


class KinesisReader(Reader):
    lag_supported = True

    def __init__(self, client, stream: str, positions: dict, start: str, fmt: str):
        self.cl, self.stream, self.fmt = client, stream, fmt
        try:
            shards = []
            kw = {"StreamName": stream}
            while True:
                r = self.cl.list_shards(**kw)
                shards += r["Shards"]
                if not r.get("NextToken"):
                    break
                kw = {"NextToken": r["NextToken"]}
        except Exception as e:  # noqa: BLE001
            raise StreamError(f"Cannot list shards for '{stream}': {_short(e)}") from None
        self.iters, self.next, self.behind = {}, dict(positions), {}
        for s in shards:
            sid = s["ShardId"]
            seq = positions.get(sid)
            if seq:
                it = self.cl.get_shard_iterator(StreamName=stream, ShardId=sid, ShardIteratorType="AFTER_SEQUENCE_NUMBER",
                                                StartingSequenceNumber=seq)
            else:
                it = self.cl.get_shard_iterator(StreamName=stream, ShardId=sid,
                                                ShardIteratorType="TRIM_HORIZON" if start == "earliest" else "LATEST")
            self.iters[sid] = it["ShardIterator"]

    def poll(self, max_records, max_wait_s):
        out, nxt = [], dict(self.next)
        deadline = time.monotonic() + max_wait_s
        while len(out) < max_records and time.monotonic() < deadline:
            got = 0
            for sid, it in list(self.iters.items()):
                if it is None or len(out) >= max_records:
                    continue
                r = self.cl.get_records(ShardIterator=it, Limit=min(10000, max_records - len(out)))
                self.iters[sid] = r.get("NextShardIterator")
                self.behind[sid] = r.get("MillisBehindLatest")
                for rec in r["Records"]:
                    val, err = decode(rec["Data"], self.fmt)
                    at = rec.get("ApproximateArrivalTimestamp")
                    out.append({"record": val, "error": err, "raw": None if val else _raw(rec["Data"]),
                                "_key": rec.get("PartitionKey"), "_partition": sid, "_offset": rec["SequenceNumber"],
                                "_event_time": at.astimezone(timezone.utc).isoformat(timespec="milliseconds") if at else None})
                    nxt[sid] = rec["SequenceNumber"]
                    got += 1
            if not got:
                if out:
                    break
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return out, nxt

    def commit_local(self, positions):
        self.next = dict(positions)

    def lag(self):
        vals = [v for v in self.behind.values() if v is not None]
        return None if not vals else int(max(vals))  # milliseconds behind the newest record


class MqttReader(Reader):
    """MQTT with QoS 1 and a persistent session. Messages are acknowledged only after the batch is written,
    so the broker redelivers anything Tessera had not saved."""

    def __init__(self, cfg: dict, sec: dict, topic: str, fmt: str, client_id: str):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ConnectorError("Install paho-mqtt to read MQTT streams: pip install paho-mqtt") from None
        import queue
        self.q: queue.Queue = queue.Queue()
        self.fmt, self.topic, self.unacked = fmt, topic, []
        self.cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=False,
                              manual_ack=True)
        if cfg.get("username"):
            self.cl.username_pw_set(cfg["username"], sec.get("password"))
        if cfg.get("tls") == "on":
            self.cl.tls_set()
        self.ready = threading.Event()
        self.err: str | None = None

        def on_connect(cl, _u, _f, rc, _p=None):
            if rc.is_failure:
                self.err = f"MQTT connect refused: {rc}"
            else:
                cl.subscribe(topic, qos=1)
            self.ready.set()

        def on_message(_cl, _u, msg):
            self.q.put(msg)

        self.cl.on_connect, self.cl.on_message = on_connect, on_message
        port = int(cfg.get("port") or (8883 if cfg.get("tls") == "on" else 1883))
        try:
            self.cl.connect(cfg["host"], port, keepalive=30)
        except Exception as e:  # noqa: BLE001
            raise StreamError(f"Cannot reach MQTT broker {cfg.get('host')}:{port}: {_short(e)}") from None
        self.cl.loop_start()
        if not self.ready.wait(10) or self.err:
            self.close()
            raise StreamError(self.err or "MQTT broker did not answer within 10 seconds.")

    def poll(self, max_records, max_wait_s):
        import queue
        out, deadline = [], time.monotonic() + max_wait_s
        while len(out) < max_records:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                msg = self.q.get(timeout=min(left, 0.5))
            except queue.Empty:
                if out:
                    break
                continue
            val, err = decode(msg.payload, self.fmt)
            out.append({"record": val, "error": err, "raw": None if val else _raw(msg.payload), "_key": msg.topic,
                        "_partition": None, "_offset": None, "_event_time": None})
            self.unacked.append((msg.mid, msg.qos))
        return out, {}

    def ack(self):
        for mid, qos in self.unacked:
            if qos:
                self.cl.ack(mid, qos)
        self.unacked = []

    def close(self):
        try:
            self.cl.loop_stop()
            self.cl.disconnect()
        except Exception:  # noqa: BLE001, S110
            pass


class SimulatedReader(Reader):
    """Demo-mode feed for an HTTP push stream: a few store checkouts every couple of seconds, so the Streams page and
    live dashboards have something moving. Only used when a stream's config has simulate: true (the sample sets it)."""

    STORES = [("Vancouver", "BC"), ("Surrey", "BC"), ("Calgary", "AB"), ("Toronto", "ON"), ("Montréal", "QC"), ("Halifax", "NS")]
    CHANNELS = ["web", "mobile", "store"]

    def __init__(self, seed: int | None = None):
        import random
        self.rnd = random.Random(seed)

    def poll(self, max_records, max_wait_s):
        time.sleep(min(max_wait_s, 5.0))
        n = self.rnd.randint(2, 9)
        out = []
        for _ in range(min(n, max_records)):
            city, prov = self.rnd.choice(self.STORES)
            amt = round(self.rnd.lognormvariate(4.0, 0.6), 2)
            out.append({"record": {"event_id": pysecrets.token_hex(8), "event": "checkout", "store": city, "province": prov,
                                   "channel": self.rnd.choices(self.CHANNELS, [5, 4, 3])[0], "amount": amt,
                                   "items": self.rnd.randint(1, 6)},
                        "error": None, "raw": None, "_key": None, "_partition": None, "_offset": None,
                        "_event_time": datetime.now(timezone.utc).isoformat(timespec="milliseconds")})
        return out, {}


def _raw(v) -> str:
    if v is None:
        return ""
    s = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
    return s[:2000]


def _short(e: Exception) -> str:
    return str(e).splitlines()[0][:300] if str(e) else type(e).__name__


# ---------------------------------------------------------------- batch -> table
def to_tables(events: list[dict]) -> tuple[pa.Table | None, pa.Table | None]:
    """Split a batch into good rows (with metadata columns) and rejects (raw payload plus the reason)."""
    ts = now()
    good, bad = [], []
    for e in events:
        meta = {k: e.get(k) for k in META_COLS if k != "_ingested_at"} | {"_ingested_at": ts}
        if e.get("record") is None:
            bad.append({"payload": e.get("raw") or "", "error": e.get("error") or "Unreadable", **meta})
        else:
            rec = {k: v for k, v in e["record"].items() if k not in META_COLS}
            good.append({**rec, **meta})
    g = normalize_records(good) if good else None
    if g is not None:
        g = _cast_meta(g)
    b = pa.Table.from_pylist(bad) if bad else None
    return g, b


def _cast_meta(t: pa.Table) -> pa.Table:
    for name in ("_event_time", "_ingested_at"):
        if name in t.column_names:
            i = t.column_names.index(name)
            col = t.column(i)
            if pa.types.is_null(col.type) or pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
                try:
                    import pyarrow.compute as pc
                    # naive UTC: DuckDB returns TIMESTAMPTZ through pytz, which is not a dependency
                    conv = pc.cast(pc.cast(pc.cast(col, pa.string()), pa.timestamp("ms", tz="UTC")), pa.timestamp("ms")) \
                        if not pa.types.is_null(col.type) else pa.nulls(len(col), pa.timestamp("ms"))
                    t = t.set_column(i, name, conv)
                except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                    pass
    for name in ("_key", "_partition", "_offset"):
        if name in t.column_names:
            i = t.column_names.index(name)
            if not pa.types.is_string(t.column(i).type):
                t = t.set_column(i, name, t.column(i).cast(pa.string()))
    return t


# ---------------------------------------------------------------- manager
class StreamManager:
    """Owns the worker threads. One thread per running stream; the database holds desired and actual state, so
    streams that were running come back after a restart."""

    def __init__(self, plat, kinesis_factory=None):
        self.plat, self.store, self.wh = plat, plat.store, plat.wh
        ensure_schema(self.store)
        self.workers: dict[int, StreamWorker] = {}
        self.lock = threading.Lock()
        self.kinesis_factory = kinesis_factory

    # ---- records
    def get(self, sid: int) -> dict:
        s = self.store.one("SELECT * FROM streams WHERE id=?", (sid,))
        if not s:
            raise StreamError("Stream not found")
        return s

    def public(self, s: dict) -> dict:
        out = {k: v for k, v in s.items() if k != "token_hash"}
        out["simulated"] = bool((s.get("config") or {}).get("simulate"))
        w = self.workers.get(s["id"])
        out["running"] = bool(w and w.is_alive())
        out["config"] = {**DEFAULTS, **(s.get("config") or {})}
        if s["kind"] == "webhook":
            out["endpoint"] = f"/api/streams/{s['id']}/events"
        return out

    def list(self) -> list[dict]:
        return [self.public(s) for s in self.store.all("SELECT * FROM streams ORDER BY name")]

    def validate(self, b: dict, existing: dict | None = None) -> dict:
        name = (b.get("name") or (existing or {}).get("name") or "").strip()
        if not name or len(name) > 80:
            raise StreamError("Give the stream a name (up to 80 characters).")
        kind = existing["kind"] if existing else b.get("kind")
        if kind not in KINDS:
            raise StreamError(f"Kind must be one of: {', '.join(KINDS)}")
        target = b.get("target") or (existing or {}).get("target") or ""
        schema, table = split_fqn(target, "raw")
        if schema != "raw":
            raise StreamError("Streams land in the raw zone. Transform and publish from there.")
        cfg = {**DEFAULTS, **((existing or {}).get("config") or {}), **(b.get("config") or {})}
        cfg = {k: v for k, v in cfg.items() if k in DEFAULTS or k in ("topic", "stream", "group", "simulate")}
        if cfg["format"] not in FORMATS:
            raise StreamError("Format must be json or text.")
        if cfg["start"] not in ("earliest", "latest"):
            raise StreamError("Start must be earliest or latest.")
        cfg["max_batch"] = max(1, min(int(cfg["max_batch"]), 100_000))
        cfg["max_wait_s"] = max(0.2, min(float(cfg["max_wait_s"]), 300.0))
        if cfg.get("retention_days") not in (None, ""):
            cfg["retention_days"] = max(1, min(int(cfg["retention_days"]), 3650))
        else:
            cfg["retention_days"] = None
        if cfg.get("dedupe_key"):
            if not isinstance(cfg["dedupe_key"], str) or len(cfg["dedupe_key"]) > 64:
                raise StreamError("dedupe_key is one column name.")
        cid = b.get("connection_id", (existing or {}).get("connection_id"))
        if kind == "webhook":
            cid = None
        else:
            if not cid:
                raise StreamError("Pick a connection for this stream.")
            conn = self.plat.connection(int(cid))
            if CONNECTION_KIND.get(conn["type"]) != kind:
                raise StreamError(f"Connection '{conn['name']}' is a {conn['type']} connection, not {kind}.")
            need = {"kafka": "topic", "kinesis": "stream", "mqtt": "topic"}[kind]
            if not cfg.get(need):
                raise StreamError(f"Set config.{need}.")
        return {"name": name, "kind": kind, "connection_id": cid, "config": cfg, "target": f"raw.{table}"}

    def create(self, b: dict, user: dict, workspace_id: int = 1) -> tuple[dict, str | None]:
        v = self.validate(b)
        if self.store.one("SELECT id FROM streams WHERE name=?", (v["name"],)):
            raise StreamError("A stream with that name exists.")
        token = None
        row = {**v, "config": json.dumps(v["config"]), "workspace_id": workspace_id, "created_by": user["email"],
               "created_at": now(), "updated_at": now(), "state": "{}", "meta": "{}"}
        if v["kind"] == "webhook":
            token = "tsk_" + pysecrets.token_urlsafe(32)
            row["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
            row["token_prefix"] = token[:10]
        sid = self.store.insert("streams", row)
        return self.public(self.get(sid)), token

    def update(self, sid: int, b: dict) -> dict:
        s = self.get(sid)
        if self.running(sid):
            raise StreamError("Stop the stream before changing it.")
        v = self.validate(b, s)
        self.store.update("streams", sid, {**v, "config": json.dumps(v["config"]), "updated_at": now()})
        return self.public(self.get(sid))

    def delete(self, sid: int) -> None:
        self.stop(sid, wait=True)
        self.store.exec("DELETE FROM streams WHERE id=?", (sid,))

    def rotate_token(self, sid: int) -> str:
        s = self.get(sid)
        if s["kind"] != "webhook":
            raise StreamError("Only HTTP push streams have tokens.")
        token = "tsk_" + pysecrets.token_urlsafe(32)
        self.store.update("streams", sid, {"token_hash": hashlib.sha256(token.encode()).hexdigest(),
                                           "token_prefix": token[:10], "updated_at": now()})
        return token

    def reset(self, sid: int, to: str = "earliest") -> dict:
        if self.running(sid):
            raise StreamError("Stop the stream before resetting its position.")
        s = self.get(sid)
        cfg = {**(s.get("config") or {}), "start": to}
        self.store.update("streams", sid, {"state": "{}", "config": json.dumps(cfg), "updated_at": now()})
        return self.public(self.get(sid))

    # ---- lifecycle
    def running(self, sid: int) -> bool:
        w = self.workers.get(sid)
        return bool(w and w.is_alive())

    def start(self, sid: int) -> dict:
        s = self.get(sid)
        if s["kind"] == "webhook" and not (s.get("config") or {}).get("simulate"):
            self.store.update("streams", sid, {"desired": "running", "status": "running", "last_error": None, "updated_at": now()})
            return self.public(self.get(sid))
        with self.lock:
            if not self.running(sid):
                self.store.update("streams", sid, {"desired": "running", "status": "starting", "last_error": None, "updated_at": now()})
                w = StreamWorker(self, sid)
                self.workers[sid] = w
                w.start()
        return self.public(self.get(sid))

    def stop(self, sid: int, wait: bool = True) -> dict:
        self.get(sid)
        self.store.update("streams", sid, {"desired": "stopped", "updated_at": now()})
        w = self.workers.get(sid)
        if w:
            w.stop_event.set()
            if wait:
                w.join(timeout=30)
        self.store.update("streams", sid, {"status": "stopped"})
        return self.public(self.get(sid))

    def resume_all(self) -> None:
        for s in self.store.all("SELECT id, kind, config FROM streams WHERE desired='running'"):
            if s["kind"] != "webhook" or (s.get("config") or {}).get("simulate"):
                try:
                    self.start(s["id"])
                except Exception as e:  # noqa: BLE001
                    log.warning("stream %s did not resume: %s", s["id"], e)

    def shutdown(self) -> None:
        for w in list(self.workers.values()):
            w.stop_event.set()
        for w in list(self.workers.values()):
            w.join(timeout=15)

    # ---- readers
    def open_reader(self, s: dict) -> Reader:
        cfg = {**DEFAULTS, **(s.get("config") or {})}
        if cfg.get("simulate"):
            return SimulatedReader()
        conn = self.plat.connection(s["connection_id"])
        c_cfg = conn.get("config") or {}
        c_sec = self.plat.vault.decrypt(conn["secrets"]) if conn.get("secrets") else {}
        positions = (s.get("state") or {}).get("positions") or {}
        if s["kind"] == "kafka":
            conf = kafka_conf(c_cfg, c_sec, conn["type"])
            _guard_hosts([h.rsplit(":", 1)[0] for h in conf["bootstrap.servers"].split(",")], self.plat.settings)
            return KafkaReader(conf, cfg["topic"], positions, cfg["start"], cfg["format"],
                               cfg.get("group") or f"tessera-{s['name']}")
        if s["kind"] == "kinesis":
            return KinesisReader(self.kinesis_client(c_cfg, c_sec), cfg["stream"], positions, cfg["start"], cfg["format"])
        if s["kind"] == "mqtt":
            _guard_hosts([c_cfg.get("host", "")], self.plat.settings)
            return MqttReader(c_cfg, c_sec, cfg["topic"], cfg["format"], f"tessera-{s['id']}-{s['name']}"[:60])
        raise StreamError("HTTP push streams have no reader.")

    def kinesis_client(self, cfg: dict, sec: dict):
        if self.kinesis_factory:
            return self.kinesis_factory(cfg, sec)
        try:
            import boto3
        except ImportError:
            raise ConnectorError("Install boto3 to read Kinesis: pip install boto3") from None
        kw = {"region_name": cfg.get("region")}
        if cfg.get("endpoint_url"):
            guard_url(cfg["endpoint_url"], getattr(self.plat.settings, "block_private_http", False))
            kw["endpoint_url"] = cfg["endpoint_url"]
        if cfg.get("access_key_id"):
            kw["aws_access_key_id"] = cfg["access_key_id"]
            kw["aws_secret_access_key"] = sec.get("secret_access_key")
        return boto3.client("kinesis", **kw)

    def preview(self, sid: int, n: int = 20) -> dict:
        s = self.get(sid)
        if s["kind"] == "webhook" and not (s.get("config") or {}).get("simulate"):
            raise StreamError("HTTP push streams have nothing to preview until something posts to them.")
        if self.running(sid):
            raise StreamError("Stop the stream to preview from its saved position, or query its table instead.")
        if (s.get("config") or {}).get("simulate"):
            raise StreamError("This is the demo feed; query its table instead.")
        r = self.open_reader(s)
        try:
            events, _ = r.poll(max(1, min(n, 200)), 3.0)
        finally:
            r.close()  # nothing acknowledged or saved: a preview never moves the position
        good, bad = to_tables(events)
        return {"rows": good.to_pylist() if good is not None else [], "rejects": bad.to_pylist() if bad is not None else []}

    # ---- writing
    def write_batch(self, s: dict, events: list[dict], positions: dict | None) -> dict:
        """Append a batch, then record the new positions. Order matters: positions move only after data lands."""
        cfg = {**DEFAULTS, **(s.get("config") or {})}
        schema, table = split_fqn(s["target"], "raw")
        good, bad = to_tables(events)
        wrote = rejected = 0
        if good is not None and good.num_rows:
            if cfg.get("dedupe_key") and cfg["dedupe_key"] not in good.column_names:
                raise StreamError(f"dedupe_key '{cfg['dedupe_key']}' is not in the events.")
            wrote = self._append(good, schema, table, cfg.get("dedupe_key"))
        if bad is not None and bad.num_rows:
            self.wh.load_arrow(bad, schema, f"{table}__rejects", "append")
            rejected = bad.num_rows
        st = self.get(s["id"])
        state = st.get("state") or {}
        if positions:
            state["positions"] = {**(state.get("positions") or {}), **positions}
        meta = st.get("meta") or {}
        meta["events_total"] = int(meta.get("events_total", 0)) + len(events)
        meta["rows_total"] = int(meta.get("rows_total", 0)) + wrote
        meta["rejects_total"] = int(meta.get("rejects_total", 0)) + rejected
        meta["batches"] = int(meta.get("batches", 0)) + 1
        meta["last_batch_at"] = now()
        meta["last_batch_rows"] = len(events)
        rate = meta.get("recent") or []
        rate = (rate + [[time.time(), len(events)]])[-60:]
        meta["recent"] = rate
        self.store.update("streams", s["id"], {"state": json.dumps(state), "meta": json.dumps(meta)})
        self._catalog(s, schema, table, rejected)
        return {"rows": wrote, "rejected": rejected}

    def _append(self, t: pa.Table, schema: str, table: str, dedupe: str | None) -> int:
        if not dedupe or not self.wh.exists(schema, table):
            if dedupe:
                t = _dedupe_in_batch(t, dedupe)
            return self.wh.load_arrow(t, schema, table, "append")
        t = _dedupe_in_batch(t, dedupe)
        with self.wh.write_lock:  # stage, filter and insert as one unit so two writers cannot both pass the filter
            return self._append_dedupe(t, schema, table, dedupe)

    def _append_dedupe(self, t: pa.Table, schema: str, table: str, dedupe: str) -> int:
        stage = f"__stage_{table}"[:60]
        self.wh.load_arrow(t, schema, stage, "replace")
        target, st = f"{qi(schema)}.{qi(table)}", f"{qi(schema)}.{qi(stage)}"
        sql = (f"DELETE FROM {st} WHERE {qi(dedupe)} IN (SELECT {qi(dedupe)} FROM {target})")
        self._internal(sql)
        n = self._internal_scalar(f"SELECT count(*) FROM {st}")
        if n:
            have = {c["name"] for c in self.wh.columns(schema, table)}
            for c in self.wh.columns(schema, stage):
                if c["name"] not in have:
                    self._internal(f"ALTER TABLE {target} ADD COLUMN {qi(c['name'])} {c['type']}")
            self._internal(f"INSERT INTO {target} BY NAME SELECT * FROM {st}")
        self._internal(f"DROP TABLE IF EXISTS {st}")
        return int(n or 0)

    def _internal(self, sql: str):
        writing = getattr(self.wh, "writing", None)  # invalidates the query cache around the write
        with (writing() if writing else self.wh.write_lock):
            cur = self.wh.cursor()
            try:
                cur.execute(sql)
            finally:
                cur.close()

    def _internal_scalar(self, sql: str):
        cur = self.wh.cursor()
        try:
            r = cur.execute(sql).fetchone()
            return r[0] if r else None
        finally:
            cur.close()

    def _catalog(self, s: dict, schema: str, table: str, rejected: int) -> None:
        d = self.plat.dataset(schema, table, create=True)
        meta = d.get("meta") or {}
        first = not meta.get("stream_id")
        meta.update({"stream_id": s["id"], "streaming": True})
        self.store.update("datasets", d["id"], {"last_loaded_at": now(), "meta": json.dumps(meta),
                                                 "source": json.dumps({"stream": s["name"], "kind": s["kind"]})})
        cols = d.get("columns") or []
        if first or len(cols) != len(self.wh.columns(schema, table)):
            self.plat.refresh_columns(self.plat.dataset_by_id(d["id"]))
        if first:
            self.plat.add_lineage(f"stream:{s['kind']}/{s['name']}", f"{schema}.{table}", "ingest", None)
        if rejected:
            rd = self.plat.dataset(schema, f"{table}__rejects", create=True)
            if not rd.get("columns"):
                self.plat.refresh_columns(self.plat.dataset_by_id(rd["id"]))
            self.store.update("datasets", rd["id"], {"last_loaded_at": now()})

    def apply_retention(self, s: dict) -> int:
        days = ((s.get("config") or {}).get("retention_days"))
        schema, table = split_fqn(s["target"], "raw")
        if not days or not self.wh.exists(schema, table):
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).replace(tzinfo=None).isoformat(timespec="seconds")
        before = self.wh.row_count(schema, table)
        self._internal(f"DELETE FROM {qi(schema)}.{qi(table)} WHERE _ingested_at < TIMESTAMP '{cutoff}'")
        return before - self.wh.row_count(schema, table)

    # ---- HTTP push
    def check_token(self, sid: int, token: str) -> dict:
        s = self.store.one("SELECT * FROM streams WHERE id=?", (sid,))
        if not s or s["kind"] != "webhook" or not token or not s.get("token_hash"):
            raise PermissionError("Unknown stream or token.")
        if not pysecrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), s["token_hash"]):
            raise PermissionError("Unknown stream or token.")
        if s["desired"] != "running":
            raise StreamError("This stream is paused. Start it in Tessera to accept events.")
        if (s.get("config") or {}).get("simulate"):
            raise PermissionError("Unknown stream or token.")
        return s

    def push(self, s: dict, payload: Any) -> dict:
        events = payload if isinstance(payload, list) else [payload]
        if len(events) > 50_000:
            raise StreamError("Send at most 50,000 events per request.")
        evs = [{"record": e if isinstance(e, dict) else {"value": e}, "error": None, "raw": None, "_key": None,
                "_partition": None, "_offset": None, "_event_time": None} for e in events]
        res = self.write_batch(s, evs, None)
        return {"accepted": len(events), **res}

    # ---- status
    def status(self, sid: int) -> dict:
        s = self.get(sid)
        out = self.public(s)
        w = self.workers.get(sid)
        out["lag"] = w.last_lag if w else None
        out["lag_unit"] = {"kafka": "messages", "kinesis": "ms behind latest"}.get(s["kind"])
        recent = (s.get("meta") or {}).get("recent") or []
        cutoff = time.time() - 60
        out["events_per_min"] = sum(n for t, n in recent if t >= cutoff)
        return out


def _dedupe_in_batch(t: pa.Table, key: str) -> pa.Table:
    seen, keep = set(), []
    col = t.column(key).to_pylist()
    for i in range(len(col) - 1, -1, -1):  # keep the latest event for each key within the batch
        if col[i] not in seen:
            seen.add(col[i])
            keep.append(i)
    keep.reverse()
    return t.take(pa.array(keep, pa.int64()))


class StreamWorker(threading.Thread):
    MAX_BACKOFF = 60.0

    def __init__(self, mgr: StreamManager, sid: int):
        super().__init__(daemon=True, name=f"tessera-stream-{sid}")
        self.mgr, self.sid = mgr, sid
        self.stop_event = threading.Event()
        self.last_lag: int | None = None

    def run(self) -> None:
        backoff, reader, last_retention = 1.0, None, 0.0
        while not self.stop_event.is_set():
            try:
                s = self.mgr.get(self.sid)
                if reader is None:
                    reader = self.mgr.open_reader(s)
                    self.mgr.store.update("streams", self.sid, {"status": "running", "last_error": None})
                cfg = {**DEFAULTS, **(s.get("config") or {})}
                events, positions = reader.poll(int(cfg["max_batch"]), float(cfg["max_wait_s"]))
                if events:
                    self.mgr.write_batch(s, events, positions)
                    reader.ack()
                    if hasattr(reader, "commit_local"):
                        reader.commit_local(positions)
                if reader.lag_supported:
                    self.last_lag = reader.lag()
                if time.time() - last_retention > 3600:
                    self.mgr.apply_retention(s)
                    last_retention = time.time()
                backoff = 1.0
            except StreamError as e:
                if str(e) == "Stream not found":
                    break  # deleted while running
                reader = self._fail(reader, e, backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF)
            except Exception as e:  # noqa: BLE001
                reader = self._fail(reader, e, backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF)
        if reader is not None:
            reader.close()

    def _fail(self, reader, e: Exception, backoff: float):
        log.warning("stream %s: %s", self.sid, e)
        try:
            self.mgr.store.update("streams", self.sid, {"status": "retrying", "last_error": _short(e)[:500],
                                                         "updated_at": now()})
        except Exception:  # noqa: BLE001, S110
            pass
        if reader is not None:
            reader.close()
        self.stop_event.wait(backoff)
        return None  # reopen from the saved position: anything not written is read again


def connection_test(ctype: str, cfg: dict, sec: dict, settings, kinesis_factory=None) -> str:
    """Test a streaming connection and say what it can see."""
    if ctype in ("kafka", "eventhubs"):
        conf = kafka_conf(cfg, sec, ctype)
        _guard_hosts([h.rsplit(":", 1)[0] for h in conf["bootstrap.servers"].split(",")], settings)
        try:
            from confluent_kafka.admin import AdminClient
        except ImportError:
            raise ConnectorError("Install confluent-kafka: pip install confluent-kafka") from None
        md = AdminClient({**conf, "log_level": 3}).list_topics(timeout=10)
        topics = sorted(t for t in md.topics if not t.startswith("__"))
        return f"Connected. {len(md.brokers)} broker(s), {len(topics)} topic(s)" + (f": {', '.join(topics[:8])}" if topics else ".")
    if ctype == "kinesis":
        cl = kinesis_factory(cfg, sec) if kinesis_factory else None
        if cl is None:
            import boto3
            kw = {"region_name": cfg.get("region")}
            if cfg.get("endpoint_url"):
                guard_url(cfg["endpoint_url"], getattr(settings, "block_private_http", False))
                kw["endpoint_url"] = cfg["endpoint_url"]
            if cfg.get("access_key_id"):
                kw |= {"aws_access_key_id": cfg["access_key_id"], "aws_secret_access_key": sec.get("secret_access_key")}
            cl = boto3.client("kinesis", **kw)
        names = cl.list_streams().get("StreamNames", [])
        return f"Connected. {len(names)} stream(s)" + (f": {', '.join(names[:8])}" if names else ".")
    if ctype == "mqtt":
        r = MqttReader(cfg, sec, "$SYS/#", "text", f"tessera-test-{pysecrets.token_hex(4)}")
        r.close()
        return f"Connected to {cfg.get('host')}."
    raise ConnectorError("Not a streaming connection.")


def browse(ctype: str, cfg: dict, sec: dict, settings, kinesis_factory=None) -> list[dict]:
    if ctype in ("kafka", "eventhubs"):
        from confluent_kafka.admin import AdminClient
        conf = kafka_conf(cfg, sec, ctype)
        _guard_hosts([h.rsplit(":", 1)[0] for h in conf["bootstrap.servers"].split(",")], settings)
        md = AdminClient({**conf, "log_level": 3}).list_topics(timeout=10)
        return [{"name": t, "kind": "topic", "partitions": len(m.partitions)} for t, m in sorted(md.topics.items())
                if not t.startswith("__")]
    if ctype == "kinesis":
        cl = kinesis_factory(cfg, sec) if kinesis_factory else None
        if cl is None:
            import boto3
            cl = boto3.client("kinesis", region_name=cfg.get("region"), endpoint_url=cfg.get("endpoint_url") or None)
        return [{"name": n, "kind": "stream"} for n in cl.list_streams().get("StreamNames", [])]
    return []


_ = QueryError


def install_demo(plat, user: dict) -> int:
    """Sample workspace: a simulated checkout feed with two hours of history. The caller starts it."""
    import random
    mgr = StreamManager(plat)
    cfg = {**DEFAULTS, "simulate": True, "max_wait_s": 5.0, "retention_days": 7, "dedupe_key": "event_id"}
    sid = plat.store.insert("streams", {"name": "Store checkouts (demo feed)", "kind": "webhook", "connection_id": None,
                                        "config": json.dumps(cfg), "target": "raw.checkout_events", "desired": "running",
                                        "status": "stopped", "state": "{}", "meta": "{}", "workspace_id": 1,
                                        "created_by": user["email"], "created_at": now(), "updated_at": now()})
    rnd = random.Random(7)
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    events = []
    for i in range(720):
        city, prov = rnd.choice(SimulatedReader.STORES)
        t = start + timedelta(seconds=i * 10 + rnd.randint(0, 9))
        events.append({"record": {"event_id": f"demo-{i:05d}", "event": "checkout", "store": city, "province": prov,
                                  "channel": rnd.choices(SimulatedReader.CHANNELS, [5, 4, 3])[0],
                                  "amount": round(rnd.lognormvariate(4.0, 0.6), 2), "items": rnd.randint(1, 6)},
                       "error": None, "raw": None, "_key": None, "_partition": None, "_offset": None,
                       "_event_time": t.isoformat(timespec="milliseconds")})
    mgr.write_batch(mgr.get(sid), events, None)
    meta = mgr.get(sid).get("meta") or {}
    meta["recent"] = []  # history is backfill, not live throughput
    plat.store.update("streams", sid, {"meta": json.dumps(meta)})
    return sid

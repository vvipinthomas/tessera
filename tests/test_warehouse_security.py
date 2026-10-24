import threading
import time

import pyarrow as pa
import pytest

from tessera.warehouse import AccessDenied, QueryError, Warehouse


@pytest.fixture
def wh(tmp_path):
    w = Warehouse(tmp_path / "w.duckdb")
    w.load_arrow(pa.table({"id": [1, 2], "secret": ["a", "b"]}), "raw", "people")
    w.load_arrow(pa.table({"id": [1, 2], "masked": ["x", "y"]}), "serving", "people_pub")
    return w


@pytest.mark.parametrize("sql", [
    "insert into raw.people values (3,'c')",
    "drop table raw.people",
    "delete from raw.people",
    "create table raw.x as select 1",
    "copy raw.people to '/tmp/out.csv'",
    "attach '/tmp/other.db'",
    "install httpfs",
    "set enable_external_access=true",
    "select 1; drop table raw.people",
    "pragma database_list",
])
def test_writes_and_admin_statements_blocked(wh, sql):
    with pytest.raises(QueryError):
        wh.query(sql, restricted=False)
    assert wh.row_count("raw", "people") == 2


@pytest.mark.parametrize("sql", [
    "select * from read_csv('/etc/passwd')",
    "select * from read_text('/etc/hostname')",
    "select * from read_parquet('s3://bucket/x.parquet')",
])
def test_filesystem_and_network_unreachable_from_sql(wh, sql):
    with pytest.raises(QueryError) as e:
        wh.query(sql, restricted=False)
    assert "Permission" in str(e.value) or "external" in str(e.value).lower() or "file system" in str(e.value).lower()


@pytest.mark.parametrize("sql", [
    "select * from people",
    "select * from raw.people",
    "select * from serving.people_pub p join raw.people r using (id)",
    "with x as (select * from raw.people) select * from x",
    "select * from serving.people_pub where id in (select id from raw.people)",
    "select * from curated.nothing",
])
def test_restricted_roles_cannot_reach_unpublished_tables(wh, sql):
    with pytest.raises(QueryError):
        wh.query(sql, restricted=True)


@pytest.mark.parametrize("sql", ["select * from duckdb_tables()", "select * from information_schema.tables", "select getenv('HOME')"])
def test_restricted_roles_cannot_read_system_catalog(wh, sql):
    with pytest.raises(AccessDenied):
        wh.query(sql, restricted=True)


def test_restricted_roles_can_read_serving(wh):
    assert wh.query("select count(*) from people_pub", restricted=True)["rows"] == [[2]]
    assert wh.query("select masked from serving.people_pub order by id", restricted=True)["rows"] == [["x"], ["y"]]


def test_engineers_read_everything(wh):
    assert wh.query("select count(*) from raw.people", restricted=False)["rows"] == [[2]]


def test_row_limit_and_truncation(wh):
    r = wh.query("select * from range(100)", restricted=False, limit=10)
    assert r["row_count"] == 10 and r["truncated"]


def test_timeout_interrupts_long_queries(wh):
    t0 = time.time()
    with pytest.raises(QueryError, match="timeout"):
        wh.query("select count(*) from range(100000000000)", restricted=False, timeout=0.5)
    assert time.time() - t0 < 10


def test_concurrent_queries(wh):
    errors = []

    def run():
        try:
            for _ in range(20):
                assert wh.query("select sum(id) from raw.people", restricted=False)["rows"] == [[3]]
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors


def test_transform_cannot_write_serving(wh):
    with pytest.raises(AccessDenied):
        wh.create_table_as("select 1 as a", "serving", "evil")
    assert wh.create_table_as("select id from raw.people", "curated", "ids") == 2


def test_join_using_and_cte_for_restricted_and_lineage(wh):
    wh.load_arrow(pa.table({"id": [1, 2], "x": ["p", "q"]}), "serving", "other")
    q = "with t as (select * from serving.people_pub) select * from t join other using (id)"
    assert wh.query(q, restricted=True)["row_count"] == 2
    assert wh.referenced_tables("select * from raw.people p left join serving.people_pub s using (id)") == ["raw.people", "serving.people_pub"]
    with pytest.raises(AccessDenied):
        wh.query("with t as (select 1 id) select * from t join raw.people using (id)", restricted=True)
    with pytest.raises(AccessDenied):
        wh.query("select * from read_csv_auto('x.csv')", restricted=True)
    assert wh.query("select * from range(3)", restricted=True)["row_count"] == 3


def test_rls_fails_closed_if_the_rewrite_is_lost(tmp_path):
    """If DuckDB ever writes the rewritten query back without the filtered view (as 2.0 first did), refuse to run it."""
    from tessera.warehouse import AccessDenied, Warehouse
    wh = Warehouse(tmp_path / "w.duckdb")
    cur = wh.cursor()
    cur.execute("CREATE TABLE serving.t AS SELECT 'BC' AS province UNION ALL SELECT 'ON'")

    class LosesRewrite:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, params=None):
            if "json_deserialize_sql" in sql:  # pretend the engine ignored the rewritten table name
                return self.c.execute("SELECT ?", ["SELECT * FROM serving.t"])
            return self.c.execute(sql, params) if params is not None else self.c.execute(sql)

    rls = {"t": "province = 'BC'"}
    assert "__rls_t" in wh.apply_rls("SELECT * FROM serving.t", rls, cur)
    with pytest.raises(AccessDenied, match="could not be applied"):
        wh.apply_rls("SELECT * FROM serving.t", rls, LosesRewrite(cur))


def test_email_check_is_strict_and_fast():
    import time

    from tessera.security import valid_email
    for e in ("a@b.co", "first.last+tag@sub.example.ca"):
        assert valid_email(e), e
    for e in ("a@b", "a b@c.d", "a@b..c", "a@b.c,d@e.f", "@b.co", "x" * 250 + "@b.co", None):
        assert not valid_email(e), e
    t = time.perf_counter()
    assert not valid_email("a@" + "!." * 20000)
    assert time.perf_counter() - t < 0.5, "no catastrophic backtracking"


def test_today_follows_the_configured_time_zone(monkeypatch):
    from tessera.store import local_today
    monkeypatch.setenv("TESSERA_TIMEZONE", "Pacific/Kiritimati")  # UTC+14
    ahead = local_today()
    monkeypatch.setenv("TESSERA_TIMEZONE", "Pacific/Pago_Pago")  # UTC-11: always a different calendar day
    behind = local_today()
    assert (ahead - behind).days == 1
    monkeypatch.setenv("TESSERA_TIMEZONE", "Not/AZone")
    assert local_today()  # falls back to UTC


def test_readiness_does_not_leak_error_details(app, monkeypatch):
    from conftest import Client

    def boom(*a, **k):
        raise RuntimeError("disk I/O error at /secret/path/tessera.db")
    monkeypatch.setattr(app.state.platform.store, "scalar", boom)
    with Client(app) as c:
        r = c.get("/api/ready")
    assert r.status_code == 503 and r.json()["checks"]["metadata"] == "unavailable" and "/secret" not in r.text


def test_only_tessera_errors_reach_people():
    from tessera.connectors import ConnectorError
    from tessera.errors import GENERIC, public_message
    from tessera.odata import FilterError
    from tessera.warehouse import AccessDenied
    assert public_message(ConnectorError("Connection refused by db.example.com:5432")) == "Connection refused by db.example.com:5432"
    assert public_message(AccessDenied("Only published data")) == "Only published data"
    assert public_message(FilterError("Unknown property 'x'\nmore")) == "Unknown property 'x'"
    assert public_message(RuntimeError("disk I/O error at /var/lib/secret.db")) == GENERIC
    assert public_message(KeyError("orders")) == GENERIC
    assert public_message(KeyError("orders"), also=(KeyError,)) == "orders"

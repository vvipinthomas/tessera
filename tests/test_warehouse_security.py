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

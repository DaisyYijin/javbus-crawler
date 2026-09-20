"""DB layer tests: schema, upserts, filters, browse aggregates, deletes."""
import json

from app import db
from app.parser import Magnet, Movie


def make_conn(tmp_path):
    return db.connect(str(tmp_path / "t.db"))


def seed(conn):
    m1 = Movie(code="TEST-001", url="https://x/TEST-001", title="标题一",
               actors=["Alice", "Bob"], genres=["4K"], studio="片商A",
               director="导演D", release_date="2024-05-01", category="censored")
    m1.magnets = [Magnet(link="magnet:?xt=urn:btih:AAAA1111BBBB", name="4k rip",
                         size="5GB", date="2024-01-01")]
    m2 = Movie(code="TEST-002", url="https://x/TEST-002", title="标题二",
               actors=["Bob"], genres=["中文字幕"], studio="片商B",
               release_date="2024-06-15", category="uncensored")
    db.upsert_movie(conn, m1)
    db.insert_magnets(conn, m1.magnets, "TEST-001")
    db.upsert_movie(conn, m2)
    conn.commit()
    return m1, m2


def test_indexes_created(tmp_path):
    conn = make_conn(tmp_path)
    names = {r[1] for r in conn.execute("PRAGMA index_list(movies)")}
    assert {"idx_movies_category", "idx_movies_first_seen",
            "idx_movies_magnet_count", "idx_movies_matched_count",
            "idx_movies_release_date"} <= names
    conn.close()


def test_upsert_idempotent(tmp_path):
    conn = make_conn(tmp_path)
    m1, _ = seed(conn)
    db.upsert_movie(conn, m1)
    conn.commit()
    total, _ = db.list_movies(conn)
    assert total == 2
    conn.close()


def test_magnet_delete_sync(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    assert db.delete_magnets(conn, "TEST-001") == 1  # TEST-001 has one magnet
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM magnets").fetchone()[0] == 0
    assert db.delete_magnets(conn, "NOPE") == 0
    conn.close()


def test_insert_magnets_counts_only_new(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    # re-inserting the same magnet must count 0 new but still refresh fields
    m = Magnet(link="magnet:?xt=urn:btih:AAAA1111BBBB", name="4k rip v2",
               size="6GB", date="2024-02-02")
    assert db.insert_magnets(conn, [m], "TEST-001") == 0
    row = conn.execute("SELECT name, size FROM magnets WHERE hash = "
                       "'AAAA1111BBBB'").fetchone()
    assert row == ("4k rip v2", "6GB")
    m2 = Magnet(link="magnet:?xt=urn:btih:CCCC3333DDDD", name="1080p",
                size="3GB", date="2024-03-03")
    assert db.insert_magnets(conn, [m2], "TEST-001") == 1
    conn.commit()
    conn.close()


def test_list_movies_categories_and_q(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    assert db.list_movies(conn, category="censored,uncensored")[0] == 2
    assert db.list_movies(conn, category="censored")[0] == 1
    assert db.list_movies(conn, category="garbage")[0] == 2  # invalid -> all
    assert db.list_movies(conn, q="Alice")[0] == 1
    assert db.list_movies(conn, q="O'Brien'")[0] == 0  # quote-safe
    conn.close()


def test_list_movies_dimension_filters(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    assert db.list_movies(conn, actor="Bob")[0] == 2
    assert db.list_movies(conn, actor="Alice")[0] == 1
    assert db.list_movies(conn, actor="Ali")[0] == 0  # exact name, not substring
    assert db.list_movies(conn, genre="4K")[0] == 1
    assert db.list_movies(conn, studio="片商B")[0] == 1
    assert db.list_movies(conn, director="导演D")[0] == 1
    conn.close()


def test_list_movies_date_range(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    assert db.list_movies(conn, date_from="2024-06-01")[0] == 1
    assert db.list_movies(conn, date_to="2024-05-31")[0] == 1
    assert db.list_movies(conn, date_from="2024-01-01", date_to="2024-12-31")[0] == 2
    conn.close()


def test_list_movies_pagination_and_sort(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    total, rows = db.list_movies(conn, page=2, size=1)
    assert total == 2 and len(rows) == 1  # offset 1 of 2 rows
    total, rows = db.list_movies(conn, page=9, size=1)
    assert len(rows) == 0  # beyond the end
    conn.close()


def test_browse_counts(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    actors = db.browse_counts(conn, "actor")
    assert actors == [{"name": "Bob", "count": 2}, {"name": "Alice", "count": 1}]
    assert db.browse_counts(conn, "actor", q="Ali") == [{"name": "Alice", "count": 1}]
    studios = db.browse_counts(conn, "studio")
    assert [s["name"] for s in studios] == ["片商A", "片商B"]
    directors = db.browse_counts(conn, "director")
    assert directors == [{"name": "导演D", "count": 1}]
    assert db.browse_counts(conn, "series") == []
    conn.close()


def test_delete_movie(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    assert db.delete_movie(conn, "TEST-001") is True
    assert conn.execute("SELECT COUNT(*) FROM magnets").fetchone()[0] == 0
    assert db.list_movies(conn)[0] == 1
    assert db.delete_movie(conn, "TEST-001") is False
    conn.close()


def test_filter_chips(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    # seed magnet "4k rip" -> only the 4K marker hits (LIKE is case-insensitive)
    assert db.filter_chips(conn) == [{"kw": "4K", "count": 1}]
    conn.close()


def test_get_movie_and_recompute(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    gm = db.get_movie(conn, "TEST-001")
    assert gm["actors"] == ["Alice", "Bob"]
    assert len(gm["magnets"]) == 1
    assert db.get_movie(conn, "NOPE") is None
    assert db.recompute_matched(conn, ["4K", "中文字幕"]) == 2
    gm = db.get_movie(conn, "TEST-001")
    assert json.loads(gm["matched_tags"]) == ["4K"]
    conn.close()


def test_library_stats(tmp_path):
    conn = make_conn(tmp_path)
    seed(conn)
    st = db.library_stats(conn, category="censored,uncensored")
    assert st["total"] == 2
    assert st["by_category"] == {"censored": 1, "uncensored": 1}
    tags = {g["tag"] for g in st["top_genres"]}
    assert "高清画质" in tags  # taxonomy normalizes 4K -> 高清画质
    conn.close()


def test_meta_roundtrip(tmp_path):
    conn = make_conn(tmp_path)
    db.set_meta(conn, "max_page:censored", 7)
    conn.commit()
    assert db.get_meta(conn, "max_page:censored") == "7"
    assert db.get_meta(conn, "missing", "0") == "0"
    conn.close()

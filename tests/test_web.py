"""Web API tests via Flask's test client — no network, no live server."""
import json

import pytest

from app import db as appdb
from app import settings, web
from app.parser import Magnet, Movie


@pytest.fixture()
def client(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    conn = appdb.connect(dbp)
    m1 = Movie(code="TEST-001", url="https://x/TEST-001", title="标题一",
               actors=["Alice", "Bob"], genres=["4K"], studio="片商A",
               release_date="2024-05-01", category="censored")
    m1.matched_tags = ["4K"]
    m1.magnets = [Magnet(link="magnet:?xt=urn:btih:AAAA1111BBBB", name="4k rip",
                         size="5GB", date="2024-01-01")]
    m2 = Movie(code="TEST-002", url="https://x/TEST-002", title="标题二",
               actors=["Bob"], genres=["中文字幕"], studio="片商B",
               release_date="2024-06-15", category="uncensored")
    appdb.upsert_movie(conn, m1)
    appdb.insert_magnets(conn, m1.magnets, "TEST-001")
    appdb.upsert_movie(conn, m2)
    conn.commit()
    conn.close()
    with open(settings.CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump({"DB_PATH": dbp, "TAG_FILTERS": "4K,中文字幕"}, fh)

    monkeypatch.setattr(web, "ADMIN_USER", "admin", raising=False)
    monkeypatch.setattr(web, "ADMIN_PASSWORD", "test-pass", raising=False)
    web._stats_cache["ts"] = None  # never serve a previous test's cache
    web._chips_cache["ts"] = None
    web._login_clear("127.0.0.1")  # brute-force guard state from prior tests

    app = web.create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        r = c.post("/api/login", json={"user": "admin", "password": "test-pass"})
        assert r.status_code == 200
        yield c


@pytest.fixture()
def anon(tmp_path):
    app = web.create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---------- auth ----------

def test_anon_gets_login_page(anon):
    r = anon.get("/")
    assert r.status_code == 200 and "登录" in r.get_data(as_text=True)


def test_pages_sent_no_cache(anon, client):
    # the inline-CSS/JS UI must revalidate after every release, or a stale
    # cached page renders old styles against new data
    assert anon.get("/").headers["Cache-Control"] == "no-cache"
    assert client.get("/").headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("path", ["/api/config", "/api/movies", "/api/stats",
                                  "/api/export", "/api/browse"])
def test_anon_api_401(anon, path):
    assert anon.get(path).status_code == 401


def test_destructive_endpoints_401(anon):
    assert anon.post("/api/data/clear").status_code == 401
    assert anon.post("/api/crawl/stop").status_code == 401
    assert anon.post("/api/crawl/code", json={"code": "X-1"}).status_code == 401


def test_login_wrong_credentials(anon):
    assert anon.post("/api/login", json={"user": "admin", "password": "nope"}).status_code == 401
    assert anon.post("/api/login", json={"user": "who", "password": "test-pass"}).status_code == 401


def test_logout_invalidates(anon):
    assert anon.post("/api/login", json={"user": "admin", "password": "test-pass"}).status_code == 200
    assert anon.post("/api/logout").status_code == 200
    assert anon.get("/api/movies").status_code == 401


# ---------- config ----------

def test_config_get_and_save(client):
    cfg = client.get("/api/config").get_json()["config"]
    assert cfg["TAG_FILTERS"] == "4K,中文字幕"
    assert client.post("/api/config", json={"CATEGORY": "bogus"}).status_code == 400
    assert client.post("/api/config", json={"DELAY_SECONDS": -1}).status_code == 400
    r = client.post("/api/config", json={"CATEGORY": "censored,uncensored"})
    assert r.get_json()["ok"] is True


def test_metatube_test_validation(client):
    assert client.post("/api/metatube/test", json={"url": "ftp://x"}).get_json()["ok"] is False
    j = client.post("/api/metatube/test", json={}).get_json()
    assert j["ok"] is False and j["error"]


def test_site_test_validation(client):
    # invalid scheme is rejected before any network access
    assert client.post("/api/site/test", json={"url": "ftp://x"}).get_json()["ok"] is False


def test_filter_chips_endpoint(client):
    j = client.get("/api/filter/chips").get_json()
    assert {"kw": "4K", "count": 1} in j["items"]  # seeded magnet "4k rip"


def test_genres_endpoint(client, monkeypatch):
    from app import crawler as _crawler
    monkeypatch.setattr(_crawler, "fetch_genre_catalog",
                        lambda cfg: ({}, {"censored": "无法连接站点", "uncensored": "无法连接站点"}))
    j = client.get("/api/genres").get_json()
    assert j["ok"] is True
    assert set(j["items"].keys()) == {"censored", "uncensored"}
    assert isinstance(j["items"]["censored"], list)
    assert "无法连接站点" in j["status"]["censored"]


def test_genres_endpoint_catalog(client, monkeypatch):
    from app import crawler as _crawler
    monkeypatch.setattr(_crawler, "fetch_genre_catalog",
                        lambda cfg: ({"censored": [{"group": "主題", "genres": [{"name": "折磨", "id": "62"}]}],
                                      "uncensored": []},
                                     {"censored": "", "uncensored": ""}))
    j = client.get("/api/genres?refresh=1").get_json()
    assert j["items"]["censored"][0]["group"] == "主題"
    assert j["items"]["censored"][0]["genres"] == [{"name": "折磨", "id": "62"}]
    assert j["items"]["uncensored"] == []


def test_p115_endpoints_unauthenticated(client):
    j = client.get("/api/p115/status").get_json()
    assert j["available"] is True and j["logged_in"] is False
    r = client.get("/api/p115/dirs")
    assert r.status_code == 400
    assert "未登录" in r.get_json()["error"]
    assert client.post("/api/p115/tasks/del", json={"hashes": ["ABC"]}).status_code == 400
    assert client.post("/api/p115/magnet", json={"code": "TEST-001"}).status_code == 400
    assert client.get("/api/p115/tasks").status_code == 400


def test_login_bruteforce_lockout(tmp_path, monkeypatch):
    dbp = str(tmp_path / "t.db")
    conn = appdb.connect(dbp)
    conn.close()
    with open(settings.CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump({"DB_PATH": dbp}, fh)
    monkeypatch.setattr(web, "ADMIN_USER", "admin", raising=False)
    monkeypatch.setattr(web, "ADMIN_PASSWORD", "test-pass", raising=False)
    web._login_clear("127.0.0.1")
    app = web.create_app()
    app.config["TESTING"] = True
    try:
        with app.test_client() as c:
            for _ in range(web._LOGIN_MAX_FAILS):
                r = c.post("/api/login", json={"user": "admin", "password": "wrong"})
                assert r.status_code == 401
            # locked: even the right password is rejected with 429
            r = c.post("/api/login", json={"user": "admin", "password": "test-pass"})
            assert r.status_code == 429
            assert "锁定" in r.get_json()["error"]
    finally:
        web._login_clear("127.0.0.1")


def test_config_token_masked(client):
    settings.save({"METATUBE_TOKEN": "secret-token-123"})
    j = client.get("/api/config").get_json()
    assert j["config"]["METATUBE_TOKEN"] == "••••"
    # saving the sentinel back must keep the stored token (key is omitted
    # by the frontend; the server treats missing key as unchanged)
    r = client.post("/api/config", json={"METATUBE_URL": "http://x:8080"})
    assert r.get_json()["ok"] is True
    assert settings.load()["METATUBE_TOKEN"] == "secret-token-123"
    settings.save({"METATUBE_TOKEN": ""})  # explicit clear still works
    assert settings.load()["METATUBE_TOKEN"] == ""
    assert len(client.get("/api/p115/devices").get_json()["devices"]) > 0
    assert client.get("/api/p115/qr.svg").status_code == 404  # no active QR


# ---------- crawl control ----------

def test_crawl_start_rejects_bad_pages(client):
    assert client.post("/api/crawl/start", json={"mode": "new", "pages": "abc"}).status_code == 400
    assert client.post("/api/crawl/start", json={"mode": "new", "pages": "5-2"}).status_code == 400
    assert client.post("/api/crawl/start", json={"mode": "new", "pages": "1-99999"}).status_code == 400


def test_crawl_code_validation(client):
    # invalid codes are rejected before any network access
    for bad in ("", "../etc/passwd", "a b", "BC-1/../../x"):
        r = client.post("/api/crawl/code", json={"code": bad})
        assert r.status_code == 400, bad


# ---------- data ----------

def test_movies_filters(client):
    assert client.get("/api/movies").get_json()["total"] == 2
    assert client.get("/api/movies?category=censored").get_json()["total"] == 1
    assert client.get("/api/movies?actor=Bob").get_json()["total"] == 2
    assert client.get("/api/movies?actor=Alice").get_json()["total"] == 1
    assert client.get("/api/movies?genre=4K").get_json()["total"] == 1
    assert client.get("/api/movies?studio=%E7%89%87%E5%95%86B").get_json()["total"] == 1  # 片商B
    assert client.get("/api/movies?date_from=2024-06-01").get_json()["total"] == 1
    assert client.get("/api/movies?page=0&size=99999").get_json()["size"] == 100


def test_movie_detail_best_magnet_case_insensitive(client):
    j = client.get("/api/movies/TEST-001").get_json()
    assert j["best_magnet"] == "AAAA1111BBBB"  # kw '4K' matched name '4k rip'
    assert j["best_kw"] == "4K"


def test_movie_detail_404(client):
    assert client.get("/api/movies/NOPE-404").status_code == 404


def test_browse_endpoint(client):
    j = client.get("/api/browse?type=actor").get_json()
    assert j["items"] == [{"name": "Bob", "count": 2}, {"name": "Alice", "count": 1}]
    j = client.get("/api/browse?type=bogus").get_json()  # falls back to actor
    assert j["type"] == "actor"


def test_delete_movie_endpoint(client):
    assert client.delete("/api/movies/TEST-002").get_json()["ok"] is True
    assert client.get("/api/movies/TEST-002").status_code == 404
    assert client.delete("/api/movies/TEST-002").status_code == 404


def test_stats_endpoint(client):
    j = client.get("/api/stats?category=censored,uncensored").get_json()
    assert j["total"] == 2 and j["by_category"]["censored"] == 1


def test_export_streams_valid_json(client):
    r = client.get("/api/export")
    assert "attachment" in r.headers.get("Content-Disposition", "")
    j = json.loads(r.get_data(as_text=True))
    assert j["count"] == 2
    by_code = {m["code"]: m for m in j["movies"]}
    assert len(by_code["TEST-001"]["magnets"]) == 1
    assert by_code["TEST-001"]["category"] == "censored"
    assert by_code["TEST-001"]["matched_tags"] == ["4K"]


def test_data_clear_rejected_while_running(client):
    with web._job_lock:
        web._job["running"] = True
    try:
        assert client.post("/api/data/clear").status_code == 409
    finally:
        with web._job_lock:
            web._job["running"] = False

"""Crawler core tests: page specs, keyword matching, magnet picking."""
import pytest

from app import crawler


def test_parse_pages_single_and_range():
    assert crawler.parse_pages("3") == [3]
    assert crawler.parse_pages("2-5") == [2, 3, 4, 5]


@pytest.mark.parametrize("bad", ["2-1", "abc", "0", "-3", "1,,2", ""])
def test_parse_pages_rejects_invalid(bad):
    with pytest.raises(ValueError):
        crawler.parse_pages(bad)


def test_parse_pages_range_cap():
    assert len(crawler.parse_pages("1-500")) == 500
    with pytest.raises(ValueError):
        crawler.parse_pages("1-501")
    with pytest.raises(ValueError):
        crawler.parse_pages("1-1000000")


def test_compute_matched_case_insensitive():
    assert crawler.compute_matched([], ["xxx 4k yyy"], ["4K"]) == ["4K"]
    assert crawler.compute_matched(["高清"], [], ["高清"]) == ["高清"]


def test_pick_best_single_magnet():
    from app.parser import Magnet

    ms = [Magnet(link="magnet:?xt=urn:btih:AAA", name="1080p", size="", date=""),
          Magnet(link="magnet:?xt=urn:btih:BBB", name="4k rip", size="", date=""),
          Magnet(link="magnet:?xt=urn:btih:CCC", name="中文字幕", size="", date="")]
    # keyword priority: 4k beats later keywords
    assert crawler.pick_best(ms, ["4k", "字幕"])[0].name == "4k rip"
    # fall through to the next keyword when the first misses
    assert crawler.pick_best(ms, ["高清", "字幕"])[0].name == "中文字幕"
    # no match -> first magnet (newest) wins
    assert crawler.pick_best(ms, [])[0].name == "1080p"
    assert crawler.pick_best(ms, ["没有命中"])[0].name == "1080p"
    assert crawler.pick_best([], ["4k"]) == []
    # always exactly one result
    assert len(crawler.pick_best(ms, ["字幕"])) == 1


def test_pick_prefers_multi_keyword_match():
    """A magnet hitting BOTH keywords (中文+高清) outranks single-keyword hits,
    no matter its position in the list."""
    from app.parser import Magnet

    ms = [Magnet(link="magnet:?xt=urn:btih:A1", name="中文字幕 720p", size="", date=""),
          Magnet(link="magnet:?xt=urn:btih:A2", name="高清 1080p 无字", size="", date=""),
          Magnet(link="magnet:?xt=urn:btih:A3", name="高清 中文字幕 1080p", size="", date="")]
    assert crawler.pick_best(ms, ["中文", "高清"])[0].name == "高清 中文字幕 1080p"
    # dict flavour (web/115 path) agrees and reports the priority keyword
    dicts = [{"name": m.name} for m in ms]
    m, kw = crawler.pick_magnet(dicts, ["中文", "高清"])
    assert m["name"] == "高清 中文字幕 1080p" and kw == "中文"
    # single-keyword ties still respect keyword priority: 中文-only beats 高清-only
    m, kw = crawler.pick_magnet([{"name": "高清 无字"}, {"name": "中文 无码"}],
                                ["中文", "高清"])
    assert m["name"] == "中文 无码" and kw == "中文"


def test_parse_tiebreak_and_size():
    assert crawler.parse_tiebreak("size,date") == ["size", "date"]
    assert crawler.parse_tiebreak(" date , size ") == ["date", "size"]
    assert crawler.parse_tiebreak("size,size") == ["size"]
    assert crawler.parse_tiebreak("") == []
    with pytest.raises(ValueError):
        crawler.parse_tiebreak("size,颜色")
    assert crawler.parse_size("5.23GB") == pytest.approx(5.23 * 1024 ** 3)
    assert crawler.parse_size("890 MB") == pytest.approx(890 * 1024 ** 2)
    assert crawler.parse_size("1TB") == pytest.approx(1024 ** 4)
    assert crawler.parse_size("嗯?") == 0.0


def test_tiebreak_size_then_date():
    from app.parser import Magnet

    # same keyword score (中文+高清): bigger file wins
    ms = [Magnet(link="magnet:?xt=urn:btih:B1", name="高清 中文", size="3.5GB", date="2024-06-01"),
          Magnet(link="magnet:?xt=urn:btih:B2", name="中文 高清", size="7.9GB", date="2024-01-01")]
    assert crawler.pick_best(ms, ["中文", "高清"], ["size", "date"])[0].link.endswith("B2")
    # date-first config: newer wins instead
    assert crawler.pick_best(ms, ["中文", "高清"], ["date", "size"])[0].link.endswith("B1")
    # no tiebreakers: first in list wins
    assert crawler.pick_best(ms, ["中文", "高清"], [])[0].link.endswith("B1")
    # keyword score still outranks size: a huge 高清-only loses to a small 中文+高清
    ms2 = [Magnet(link="magnet:?xt=urn:btih:C1", name="高清 4K", size="30GB", date="2024-06-01"),
           Magnet(link="magnet:?xt=urn:btih:C2", name="中文 高清", size="2GB", date="2023-01-01")]
    assert crawler.pick_best(ms2, ["中文", "高清"], ["size", "date"])[0].link.endswith("C2")


def test_rank_magnets_full_order():
    magnets = [
        {"name": "FNS-220 HD", "size": "2GB", "date": "2024-01-01"},      # unmatched
        {"name": "FNS-220 中文 高清", "size": "5GB", "date": "2023-06-01"},  # double hit
        {"name": "FNS-220 中文", "size": "10GB", "date": "2025-01-01"},     # 中文 only
        {"name": "FNS-220 高清", "size": "8GB", "date": "2024-08-08"},      # 高清 only
    ]
    ranked = crawler.rank_magnets(magnets, ["中文", "高清"], ["size", "date"])
    assert [r["name"] for r in ranked] == [
        "FNS-220 中文 高清",  # 1: double hit wins over everything
        "FNS-220 中文",       # 2: keyword order (中文 first) beats 高清 despite size/date
        "FNS-220 高清",       # 3
        "FNS-220 HD",         # 4: unmatched keeps original list position
    ]
    assert ranked[0]["best"] is True and ranked[0]["hit_count"] == 2
    assert ranked[0]["hits"] == ["中文", "高清"]
    assert [r["best"] for r in ranked[1:]] == [False, False, False]
    assert ranked[3]["hits"] == [] and ranked[3]["hit_count"] == 0
    # all fields survive the round trip
    assert ranked[0]["size"] == "5GB" and ranked[0]["date"] == "2023-06-01"


def test_rank_magnets_no_match_falls_back_to_first():
    magnets = [{"name": "x2", "size": "1GB", "date": ""},
               {"name": "x1", "size": "9GB", "date": ""}]
    ranked = crawler.rank_magnets(magnets, ["中文"], [])
    assert ranked[0]["best"] is True and ranked[0]["hits"] == []
    assert ranked[0]["name"] == "x2"          # same fallback as pick_magnet
    assert crawler.rank_magnets([], ["中文"], []) == []


def test_rank_magnets_matches_pick():
    magnets = [
        {"name": "a 高清", "size": "3GB", "date": "2024-01-01"},
        {"name": "b 中文 高清", "size": "1GB", "date": "2022-05-05"},
        {"name": "c 中文", "size": "6GB", "date": "2025-02-02"},
        {"name": "d nothing", "size": "9GB", "date": "2025-12-31"},
        {"name": "e 高清", "size": "6GB", "date": "2025-03-03"},
    ]
    kws = ["中文", "高清"]
    for tb in (["size", "date"], ["date", "size"], [], ["size"], ["date"]):
        best, kw = crawler.pick_magnet(magnets, kws, tb)
        ranked = crawler.rank_magnets(magnets, kws, tb)
        assert ranked[0]["name"] == best["name"], tb
        assert (ranked[0]["hits"][0] if ranked[0]["hits"] else "") == kw, tb


def test_parse_fallback():
    assert crawler.parse_fallback("first") == "first"
    assert crawler.parse_fallback(" LARGEST ") == "largest"
    assert crawler.parse_fallback("none") == "none"
    assert crawler.parse_fallback("") == "first"
    assert crawler.parse_fallback("xxx") == "first"


def test_magnet_fallback_strategies():
    magnets = [
        {"name": "a plain", "size": "1GB", "date": "2025-01-01"},
        {"name": "b plain", "size": "8GB", "date": "2023-01-01"},
        {"name": "c 中文", "size": "2GB", "date": "2024-01-01"},
    ]
    # a hit exists: the fallback never kicks in, whatever it is
    for fb in ("first", "largest", "none"):
        m, kw = crawler.pick_magnet(magnets, ["中文"], [], fb)
        assert m["name"] == "c 中文" and kw == "中文", fb
        assert crawler.pick_best(magnets, ["中文"], [], fb)[0]["name"] == "c 中文"
    # no hit -> first: list head (newest)
    m, kw = crawler.pick_magnet(magnets, ["高清"], [], "first")
    assert m["name"] == "a plain" and kw == ""
    # no hit -> largest: biggest file
    m, _kw = crawler.pick_magnet(magnets, ["高清"], [], "largest")
    assert m["name"] == "b plain"
    # no hit -> none: nothing is picked, movie stored without a magnet
    m, kw = crawler.pick_magnet(magnets, ["高清"], [], "none")
    assert m is None and kw == ""
    assert crawler.pick_best(magnets, ["高清"], [], "none") == []
    # rank_magnets mirrors all of it
    ranked = crawler.rank_magnets(magnets, ["高清"], [], "largest")
    assert ranked[0]["best"] and ranked[0]["name"] == "b plain"
    ranked = crawler.rank_magnets(magnets, ["高清"], [], "first")
    assert ranked[0]["best"] and ranked[0]["name"] == "a plain"
    ranked = crawler.rank_magnets(magnets, ["高清"], [], "none")
    assert not any(r["best"] for r in ranked)


def test_compute_matched_keeps_original_spelling():
    hits = crawler.compute_matched(["VR専用"], [], ["vr专用"])
    assert hits == []
    hits = crawler.compute_matched(["VR専用"], [], ["VR専用"])
    assert hits == ["VR専用"]


def test_parse_genre_list():
    assert crawler.parse_genre_list("42,hd") == ["42", "hd"]
    assert crawler.parse_genre_list(" 42 , hd ") == ["42", "hd"]
    assert crawler.parse_genre_list("") == []
    assert crawler.parse_genre_list(None) == []
    assert crawler.parse_genre_list("a b,42,%%%,hd") == ["42", "hd"]


def test_listing_path():
    assert crawler.listing_path("", "", 1) == "/page/1"
    assert crawler.listing_path("", "42", 3) == "/genre/42/3"
    assert crawler.listing_path("/uncensored", "hd", 2) == "/uncensored/genre/hd/2"


def test_looks_blocked():
    assert crawler.looks_blocked("<html>... driver-verify?referer=... </html>")
    assert crawler.looks_blocked("<title>Age Verification JavBus</title>")
    assert not crawler.looks_blocked('<a class="movie-box">ok</a>')


def test_pick_magnet_left_to_right_priority():
    mags = [{"name": "1080p A", "hash": "a"},
            {"name": "4K B", "hash": "b"},
            {"name": "中文字幕 C", "hash": "c"}]
    best, kw = crawler.pick_magnet(mags, ["中文字幕", "4K"])
    assert best["hash"] == "c" and kw == "中文字幕"


def test_pick_magnet_case_insensitive():
    best, kw = crawler.pick_magnet([{"name": "abc 4k def", "hash": "h1"}], ["4K"])
    assert best["hash"] == "h1" and kw == "4K"


def test_pick_magnet_fallback_and_empty():
    best, kw = crawler.pick_magnet([{"name": "x", "hash": "a"}], [])
    assert best["hash"] == "a" and kw == ""
    assert crawler.pick_magnet([], ["4K"]) == (None, "")


def test_compute_matched_site_markers():
    """Real magnet names from seedmm.bond (FNS-248, 2026-09)."""
    names = [
        "第一會所新片@SIS001@FNS-248 高清",
        "第一會所新片@SIS001@FNS-248-U",
        "FNS-248-UC",
        "FNS-248-AI 高清",
        "fns-248ch 高清 字幕",
        "FNS-248-U",
        "FNS-248 高清",
    ]
    assert crawler.compute_matched([], names, ["字幕"]) == ["字幕"]
    assert crawler.compute_matched([], names, ["高清"]) == ["高清"]
    assert crawler.compute_matched([], names, ["-UC"]) == ["-UC"]      # only UC rows
    assert crawler.compute_matched([], names, ["AI"]) == ["AI"]        # only the AI row
    # -U is a substring of -UC: both uncensored leak variants hit
    assert crawler.compute_matched([], names, ["-U"]) == ["-U"]


def test_pick_magnet_prefers_priority_over_position():
    mags = [{"name": "FNS-248-U", "hash": "u"},
            {"name": "fns-248ch 高清 字幕", "hash": "ch"}]
    best, kw = crawler.pick_magnet(mags, ["字幕", "-U"])
    assert best["hash"] == "ch" and kw == "字幕"


def _movie(code, magnets):
    import types

    return types.SimpleNamespace(code=code, magnets=magnets)


def test_auto_download_noop_when_disabled_or_no_magnet(monkeypatch):
    from app import p115

    calls = []
    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "add_magnet", lambda link: calls.append(link))
    crawler.auto_download({"AUTO_DOWNLOAD": False}, _movie("FIT-008", [
        {"link": "magnet:?xt=urn:btih:X", "name": "n"}]))
    crawler.auto_download({}, _movie("FIT-008", [
        {"link": "magnet:?xt=urn:btih:X", "name": "n"}]))
    crawler.auto_download({"AUTO_DOWNLOAD": True}, _movie("FIT-008", []))
    assert calls == []


def test_auto_download_skips_without_115_auth(monkeypatch):
    from app import p115

    monkeypatch.setattr(p115, "has_auth", lambda: False)
    calls = []
    monkeypatch.setattr(p115, "add_magnet", lambda link: calls.append(link))
    crawler.auto_download({"AUTO_DOWNLOAD": True}, _movie("FIT-008", [
        {"link": "magnet:?xt=urn:btih:X", "name": "n"}]))
    assert calls == []


def test_auto_download_skips_code_already_tracked(monkeypatch):
    from app import p115

    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "track_records",
                        lambda: {"IH1": {"code": "FIT-008", "name": "x"}})
    calls = []
    monkeypatch.setattr(p115, "add_magnet", lambda link: calls.append(link))
    crawler.auto_download({"AUTO_DOWNLOAD": True}, _movie("FIT-008", [
        {"link": "magnet:?xt=urn:btih:X", "name": "n"}]))
    assert calls == []


def test_auto_download_serial_gate_waits_for_current(monkeypatch):
    from app import p115

    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "track_records",
                        lambda: {"IH0": {"code": "OTHER-1", "name": "busy"}})
    calls = []
    monkeypatch.setattr(p115, "add_magnet", lambda link: calls.append(link))
    crawler.auto_download({"AUTO_DOWNLOAD": True}, _movie("FIT-008", [
        {"link": "magnet:?xt=urn:btih:X", "name": "n"}]))
    assert calls == []  # serial: previous movie still downloading/organizing


def test_auto_download_skips_gaveup_code(monkeypatch):
    from app import p115

    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "track_records", lambda: {})
    monkeypatch.setattr(p115, "gaveup_codes", lambda: {"FIT-008"})
    calls = []
    monkeypatch.setattr(p115, "add_magnet", lambda link: calls.append(link))
    crawler.auto_download({"AUTO_DOWNLOAD": True}, _movie("FIT-008", [
        {"link": "magnet:?xt=urn:btih:X", "name": "n"}]))
    assert calls == []  # all magnets failed before: skip for good


def test_auto_download_waits_serial_interval(monkeypatch):
    import time
    from app import p115
    from app.parser import Magnet

    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "track_records", lambda: {})
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())
    monkeypatch.setattr(p115, "last_release_at", lambda: int(time.time()) - 60)
    calls = []
    monkeypatch.setattr(p115, "add_magnet", lambda link: calls.append(link))
    monkeypatch.setattr(p115, "track_add", lambda *a: None)
    mv = _movie("FIT-009", [Magnet(link="magnet:?xt=urn:btih:X", name="n")])
    cfg = {"AUTO_DOWNLOAD": True, "P115_DL_INTERVAL_SEC": 1800}
    crawler.auto_download(cfg, mv)
    assert calls == []  # last movie landed a minute ago: still cooling down
    monkeypatch.setattr(p115, "last_release_at", lambda: int(time.time()) - 3600)
    crawler.auto_download(cfg, mv)
    assert calls == ["magnet:?xt=urn:btih:X"]  # cooldown elapsed -> submit


def test_auto_download_submits_and_tracks(monkeypatch):
    from app import p115
    from app.parser import Magnet

    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "track_records", lambda: {})
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())
    submitted = {}

    def fake_add(link):
        submitted["link"] = link
        return {"info_hash": "IH9", "name": "seed name"}

    def fake_track(code, ih, link, name):
        submitted.update(code=code, ih=ih, track_link=link, track_name=name)

    monkeypatch.setattr(p115, "add_magnet", fake_add)
    monkeypatch.setattr(p115, "track_add", fake_track)
    mv = _movie("FIT-008", [Magnet(link="magnet:?xt=urn:btih:K9&dn=fit-008ch",
                                   name="fit-008ch 高清 字幕", size="5.23GB")])
    crawler.auto_download({"AUTO_DOWNLOAD": True}, mv)
    assert submitted["link"] == "magnet:?xt=urn:btih:K9&dn=fit-008ch"
    assert submitted["code"] == "FIT-008" and submitted["ih"] == "IH9"
    assert submitted["track_name"] == "seed name"


def test_auto_download_swallows_115_errors(monkeypatch):
    from app import p115

    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "track_records", lambda: {})
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())

    def boom(link):
        raise RuntimeError("115 down")

    monkeypatch.setattr(p115, "add_magnet", boom)
    crawler.auto_download({"AUTO_DOWNLOAD": True}, _movie("FIT-008", [
        {"link": "magnet:?xt=urn:btih:X", "name": "n"}]))


def test_run_job_stop_interrupts_immediately(monkeypatch, tmp_path):
    """用户停止是控制流，不是单条影片失败：StopRequested 必须立即冒泡结束
    任务，而不是把当前页剩余条目逐条烧成「单条处理失败」(v0.10.68 线上回归)。"""
    from app.fetcher import StopRequested

    cfg = {"DB_PATH": str(tmp_path / "t.db"), "BASE_URL": "https://x.example",
           "DELAY_SECONDS": 0, "JITTER_SECONDS": 0, "MAX_RETRIES": 1,
           "TIMEOUT": 5, "CATEGORY": "censored", "GENRE_CENSORED": "42"}

    class Item:
        code, url = "AAA-001", "https://x.example/AAA-001"

    monkeypatch.setattr(crawler, "parse_list", lambda html, base: [Item()] * 5)
    monkeypatch.setattr(crawler, "looks_blocked", lambda html: False)

    state = {"detail_calls": 0}

    class FakeFetcher:
        base_url = "https://x.example"
        delay = 0

        def __init__(self, *a, **k):
            pass

        def get(self, path, referer=None):
            if "/page/" in path:
                return "<html>listing</html>"
            state["detail_calls"] += 1
            raise StopRequested()  # stop flag flipped on mid-job

    monkeypatch.setattr(crawler, "Fetcher", FakeFetcher)
    stats = crawler.run_job(cfg, pages="1", magnets=True)
    assert stats["stopped"] is True
    assert state["detail_calls"] == 1  # 第一部即中断，无级联
    assert stats["errors"] == 0        # 不计入单条错误


# ---- v0.10.71: 类别中文标签 + 采集与云下载串行对齐 ----

def _full_movie(code, magnets=None):
    """A movie object complete enough for db.upsert_movie / run_job."""
    import types

    return types.SimpleNamespace(
        code=code, url=f"https://x.example/{code}", title=f"T {code}",
        cover="", release_date="", duration="", director="", studio="",
        label="", series="", actors=[], genres=[], samples=[],
        magnets=magnets if magnets is not None else [],
        matched_tags=[], matched_count=0, category="censored", genre_links=[])


def test_genre_name_map_merges_catalog_and_learned(tmp_path):
    """cached site catalog wins; learned genre_id:* rows fill the gaps."""
    import json

    from app import db

    conn = db.connect(str(tmp_path / "t.db"))
    db.set_meta(conn, "genre_catalog_v2", json.dumps({
        "censored": [{"group": "g", "genres": [
            {"name": "中文字幕", "id": "sub"}, {"name": "高清", "id": "hd"}]}],
        "uncensored": [],
    }, ensure_ascii=False))
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                 ("genre_id:censored:巨乳", "big"))
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                 ("genre_id:censored:字幕B", "sub"))  # stale learned row
    conn.commit()
    m = crawler.genre_name_map(conn)
    assert m[("censored", "sub")] == "中文字幕"   # catalog beats the stale row
    assert m[("censored", "hd")] == "高清"
    assert m[("censored", "big")] == "巨乳"        # learned fills catalog gaps
    assert ("uncensored", "sub") not in m          # channel-scoped
    conn.close()


def test_genre_name_map_survives_bad_catalog(tmp_path):
    from app import db

    conn = db.connect(str(tmp_path / "t.db"))
    db.set_meta(conn, "genre_catalog_v2", "not-json{")
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                 ("genre_id:uncensored:女优", "actress"))
    conn.commit()
    m = crawler.genre_name_map(conn)
    assert m == {("uncensored", "actress"): "女优"}
    conn.close()


def test_stored_movie_rebuilds_from_db(tmp_path):
    from types import SimpleNamespace

    from app import db

    db_path = str(tmp_path / "t.db")
    conn = db.connect(db_path)
    m = SimpleNamespace(link="magnet:?xt=urn:btih:X", name="n", size="1GB",
                        date="2025-01-01")
    db.upsert_movie(conn, _full_movie("FIT-008", [m]))
    db.insert_magnets(conn, [m], "FIT-008")
    conn.commit()
    conn.close()

    mv = crawler._stored_movie({"DB_PATH": db_path}, "FIT-008")
    assert mv is not None and mv.code == "FIT-008"
    assert mv.magnets[0].link == "magnet:?xt=urn:btih:X"
    assert mv.magnets[0].name == "n"
    assert crawler._stored_movie({"DB_PATH": db_path}, "NOPE-000") is None


def test_auto_download_reports_status(monkeypatch):
    """每个分支都有可判别的返回值——run_job 依赖它决定是否阻塞等整理。"""
    import time as time_mod
    from types import SimpleNamespace

    from app import p115

    mv = _movie("FIT-008", [
        SimpleNamespace(link="magnet:?xt=urn:btih:X", name="n")])
    monkeypatch.setattr(crawler, "_queued_update", lambda *a: None)

    assert crawler.auto_download({"AUTO_DOWNLOAD": False}, mv) == "off"
    assert crawler.auto_download(
        {"AUTO_DOWNLOAD": True}, _movie("FIT-008", [])) == "off"

    monkeypatch.setattr(p115, "has_auth", lambda: False)
    assert crawler.auto_download({"AUTO_DOWNLOAD": True}, mv) == "skip"

    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "track_records",
                        lambda: {"IH1": {"code": "FIT-008"}})
    assert crawler.auto_download({"AUTO_DOWNLOAD": True}, mv) == "tracked"

    monkeypatch.setattr(p115, "track_records",
                        lambda: {"IH0": {"code": "OTHER-1"}})
    assert crawler.auto_download({"AUTO_DOWNLOAD": True}, mv) == "busy"

    monkeypatch.setattr(p115, "track_records", lambda: {})
    monkeypatch.setattr(p115, "last_release_at", lambda: int(time_mod.time()))
    assert crawler.auto_download(
        {"AUTO_DOWNLOAD": True, "P115_DL_INTERVAL_SEC": 30}, mv) == "cooldown"

    monkeypatch.setattr(p115, "gaveup_codes", lambda: {"FIT-008"})
    assert crawler.auto_download({"AUTO_DOWNLOAD": True}, mv) == "skip"
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())

    monkeypatch.setattr(p115, "add_magnet",
                        lambda link: {"info_hash": "IH9", "name": "seed"})
    monkeypatch.setattr(p115, "track_add", lambda *a: None)
    assert crawler.auto_download({"AUTO_DOWNLOAD": True}, mv) == "submitted"

    def boom(link):
        raise RuntimeError("115 down")

    monkeypatch.setattr(p115, "add_magnet", boom)
    assert crawler.auto_download({"AUTO_DOWNLOAD": True}, mv) == "skip"


def test_serial_settle_returns_after_double_confirm(monkeypatch):
    """_swap_magnet 换磁力时会短暂删除记录再重加：消失必须连续两轮确认，
    一轮就返回会把「换磁力中」误判成「整理完毕」。"""
    from app import p115

    seq = [{"IH1": {"code": "FIT-008"}},   # poll 1: tracked
           {},                              # poll 2: gone once (swap window)
           {},                              # poll 3: gone twice -> settled
           {"IH1": {"code": "FIT-008"}}]    # must never be reached
    monkeypatch.setattr(p115, "track_records", lambda: seq.pop(0))
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())
    monkeypatch.setattr(p115, "last_release_at", lambda: 0)
    monkeypatch.setattr(crawler, "_SERIAL_POLL_S", 0)
    crawler._serial_settle({"P115_DL_INTERVAL_SEC": 0}, "FIT-008")
    assert len(seq) == 1  # settled exactly at the second clean poll


def test_serial_settle_waits_for_done_marker(monkeypatch):
    """watch_pass 因任务从 115 列表消失而放行槽位时，记录没了但整理从未
    发生：必须继续等（等记录回来或等 gaveup 终止标记），不能误报「整理
    刚完成」就直接采集下一部。"""
    from app import p115

    seq = [{"IH1": {"code": "FIT-008"}},   # poll 1: tracked
           {},                              # poll 2: gone once
           {},                              # poll 3: gone twice, no done -> wait
           {},                              # poll 4: still no proof -> wait
           {},                              # poll 5: gaveup marker -> release
           {}]                              # must never be reached
    monkeypatch.setattr(p115, "track_records", lambda: seq.pop(0))
    monkeypatch.setattr(p115, "gaveup_codes",
                        lambda: set() if len(seq) > 1 else {"FIT-008"})
    monkeypatch.setattr(p115, "is_done", lambda ih: False)
    monkeypatch.setattr(crawler, "_SERIAL_POLL_S", 0)
    crawler._serial_settle(
        {"P115_DL_INTERVAL_SEC": 0, "P115_AUTO_ORGANIZE": True}, "FIT-008")
    assert len(seq) == 1  # held until the gaveup marker showed up


def test_serial_settle_done_marker_releases(monkeypatch):
    """整理真实落库后（p115:done 已打标），双确认消失即放行。"""
    from app import p115

    seq = [{"IH1": {"code": "FIT-008"}}, {}, {}]
    monkeypatch.setattr(p115, "track_records", lambda: seq.pop(0))
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())
    monkeypatch.setattr(p115, "is_done", lambda ih: ih == "IH1")
    monkeypatch.setattr(crawler, "_SERIAL_POLL_S", 0)
    crawler._serial_settle(
        {"P115_DL_INTERVAL_SEC": 0, "P115_AUTO_ORGANIZE": True}, "FIT-008")
    assert len(seq) == 0  # released at the second clean poll, proof exists


def test_serial_settle_stop_request(monkeypatch):
    from app import p115
    from app.fetcher import StopRequested

    monkeypatch.setattr(p115, "track_records", lambda: {})
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())
    monkeypatch.setattr(crawler, "_SERIAL_POLL_S", 0)
    with pytest.raises(StopRequested):
        crawler._serial_settle({}, "FIT-008", stop_check=lambda: True)


def test_serial_settle_gaveup_releases(monkeypatch):
    from app import p115

    polls = []
    monkeypatch.setattr(p115, "track_records",
                        lambda: polls.append(1) or {})
    monkeypatch.setattr(p115, "gaveup_codes", lambda: {"FIT-008"})
    monkeypatch.setattr(crawler, "_SERIAL_POLL_S", 0)
    monkeypatch.setattr(crawler, "_SERIAL_WAIT_MAX_S", 0.05)
    crawler._serial_settle({}, "FIT-008")
    assert polls == [1]  # released on the first poll, no waiting


def test_serial_settle_resubmits_when_slot_frees(monkeypatch, tmp_path):
    """首轮 auto_download 撞上 busy 的影片：串行等待循环发现槽位空闲后，
    从数据库重建入库记录并顺延提交，然后等到它整理完毕才放行。"""
    from types import SimpleNamespace

    from app import db, p115

    db_path = str(tmp_path / "t.db")
    conn = db.connect(db_path)
    m = SimpleNamespace(link="magnet:?xt=urn:btih:X", name="n", size="1GB",
                        date="")
    db.upsert_movie(conn, _full_movie("FIT-008", [m]))
    db.insert_magnets(conn, [m], "FIT-008")
    conn.commit()
    conn.close()

    seq = [{"IH0": {"code": "OTHER-1"}},   # poll 1: busy with another movie
           {},                              # poll 2: slot free -> resubmit
           {"IH9": {"code": "FIT-008"}},    # poll 3: ours is tracked now
           {},                              # poll 4: gone once
           {},                              # poll 5: gone twice -> settled
           {"IH9": {"code": "FIT-008"}}]    # must never be reached
    monkeypatch.setattr(p115, "track_records", lambda: seq.pop(0))
    monkeypatch.setattr(p115, "gaveup_codes", lambda: set())
    monkeypatch.setattr(p115, "last_release_at", lambda: 0)
    monkeypatch.setattr(crawler, "_SERIAL_POLL_S", 0)
    monkeypatch.setattr(crawler, "_SERIAL_WAIT_MAX_S", 0.05)
    resub = []

    def fake_auto(cfg, movie):
        resub.append(movie.code)
        return "submitted"

    monkeypatch.setattr(crawler, "auto_download", fake_auto)
    crawler._serial_settle(
        {"DB_PATH": db_path, "P115_DL_INTERVAL_SEC": 0}, "FIT-008")
    assert resub == ["FIT-008"]  # resubmitted from the rebuilt DB record
    assert len(seq) == 1  # settled at the second clean poll; sentinel intact


def test_run_job_waits_for_pipeline_between_movies(monkeypatch, tmp_path):
    """入库后 auto_download 说「在管道里」就阻塞到整理完毕；返回 submitted
    则（错误地）不再等待——这里验证的是等待与否完全由返回值驱动。"""
    import types

    cfg = {"DB_PATH": str(tmp_path / "t.db"), "BASE_URL": "https://x.example",
           "DELAY_SECONDS": 0, "JITTER_SECONDS": 0, "MAX_RETRIES": 1,
           "TIMEOUT": 5, "CATEGORY": "censored", "GENRE_CENSORED": "42"}

    items = [types.SimpleNamespace(code="AAA-001", url="https://x.example/1"),
             types.SimpleNamespace(code="AAA-002", url="https://x.example/2")]
    monkeypatch.setattr(crawler, "parse_list", lambda html, base: items)
    monkeypatch.setattr(crawler, "looks_blocked", lambda html: False)
    monkeypatch.setattr(
        crawler, "parse_detail",
        lambda html, code, url: _full_movie(code))

    waits = []
    monkeypatch.setattr(
        crawler, "_serial_settle",
        lambda cfg, code, stop_check=None: waits.append(code))
    monkeypatch.setattr(
        crawler, "auto_download",
        lambda cfg, movie: "busy" if movie.code == "AAA-001" else "off")

    class FakeFetcher:
        base_url = "https://x.example"
        delay = 0

        def __init__(self, *a, **k):
            pass

        def get(self, path, referer=None):
            return "<html>detail</html>" if "/page/" not in path \
                else "<html>listing</html>"

    monkeypatch.setattr(crawler, "Fetcher", FakeFetcher)
    stats = crawler.run_job(cfg, pages="1", magnets=True)
    assert stats["new"] == 2 and stats["errors"] == 0
    assert waits == ["AAA-001"]  # busy -> hold the crawl; off -> straight on

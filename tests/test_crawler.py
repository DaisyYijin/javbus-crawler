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

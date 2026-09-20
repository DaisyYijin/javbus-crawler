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

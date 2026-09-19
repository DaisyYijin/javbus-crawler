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

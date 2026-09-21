"""Settings validation tests (writes go to the isolated JC_DATADIR)."""
import pytest

from app import settings


def test_load_defaults():
    cfg = settings.load()
    assert cfg["CATEGORY"] in ("censored", "uncensored", "censored,uncensored")
    assert cfg["TAG_FILTER_MODE"] in ("mark", "only")
    assert cfg["DELAY_SECONDS"] >= 0
    assert "USER_AGENT" not in cfg  # removed in v0.9.16; UA lives in fetcher now
    assert cfg["P115_REJECT_DIR"] == "冗余"


def test_load_cache_returns_copies_and_sees_external_writes():
    settings.save({"DELAY_SECONDS": 0.3})  # make sure the file exists on disk
    a = settings.load()
    a["DELAY_SECONDS"] = 99  # caller mutation must not poison the cache
    assert settings.load()["DELAY_SECONDS"] != 99
    # an external write to the file invalidates the cache via mtime change
    import json as _json
    import time as _time

    with open(settings.CONFIG_PATH, "r", encoding="utf-8") as fh:
        stored = _json.load(fh)
    stored["DELAY_SECONDS"] = 1.5
    _time.sleep(0.02)  # ensure a distinguishable mtime
    with open(settings.CONFIG_PATH, "w", encoding="utf-8") as fh:
        _json.dump(stored, fh)
    assert settings.load()["DELAY_SECONDS"] == 1.5


def test_category_multi_select():
    assert settings.save({"CATEGORY": "censored,uncensored"})["CATEGORY"] == "censored,uncensored"
    assert settings.save({"CATEGORY": "censored, uncensored"})["CATEGORY"] == "censored,uncensored"


def test_category_invalid_rejected():
    with pytest.raises(ValueError):
        settings.save({"CATEGORY": "bogus"})
    with pytest.raises(ValueError):
        settings.save({"CATEGORY": ""})


def test_genre_validation():
    assert settings.save({"GENRE_CENSORED": "42,hd"})["GENRE_CENSORED"] == "42,hd"
    assert settings.save({"GENRE_UNCENSORED": " 1 , 2 "})["GENRE_UNCENSORED"] == "1,2"
    assert settings.save({"GENRE_CENSORED": ""})["GENRE_CENSORED"] == ""
    with pytest.raises(ValueError):
        settings.save({"GENRE_CENSORED": "42,不良 id"})
    with pytest.raises(ValueError):
        settings.save({"GENRE_UNCENSORED": "%%%"})


def test_tag_filter_mode_legacy_migration():
    assert settings.save({"TAG_FILTER_MODE": "all"})["TAG_FILTER_MODE"] == "mark"
    assert settings.save({"TAG_FILTER_MODE": "only"})["TAG_FILTER_MODE"] == "only"
    with pytest.raises(ValueError):
        settings.save({"TAG_FILTER_MODE": "weird"})


def test_dl_interval_minutes_to_seconds_migration():
    """v0.10.72 把串行间隔从分钟改成秒：磁盘上的旧键按 ×60 换算带入。"""
    import json as _json
    import time as _time

    settings.save({"P115_DL_INTERVAL_SEC": 0})  # ensure the file exists
    with open(settings.CONFIG_PATH, "r", encoding="utf-8") as fh:
        stored = _json.load(fh)
    stored.pop("P115_DL_INTERVAL_SEC", None)
    stored["P115_DL_INTERVAL_MIN"] = 2  # legacy key from pre-0.10.72
    _time.sleep(0.02)  # ensure a distinguishable mtime
    with open(settings.CONFIG_PATH, "w", encoding="utf-8") as fh:
        _json.dump(stored, fh)
    assert settings.load()["P115_DL_INTERVAL_SEC"] == 120


@pytest.mark.parametrize("key,value", [
    ("DELAY_SECONDS", -1),
    ("JITTER_SECONDS", -0.5),
    ("WEB_PORT", 99999),
    ("WEB_PORT", 0),
    ("AUTO_CRAWL_INTERVAL_HOURS", 0),
    ("AUTO_CRAWL_INTERVAL_HOURS", 9999),
])
def test_bounds_rejected(key, value):
    with pytest.raises(ValueError):
        settings.save({key: value})


def test_db_path_locked_and_unknown_keys_ignored():
    with pytest.raises(ValueError):
        settings.save({"DB_PATH": "/etc/passwd"})
    assert "HACK" not in settings.save({"HACK": "x"})


def test_auto_download_toggle_and_fallback_default():
    cfg = settings.load()
    assert cfg["AUTO_DOWNLOAD"] is False
    assert cfg["MAGNET_FALLBACK"] == "largest"
    assert settings.save({"AUTO_DOWNLOAD": True})["AUTO_DOWNLOAD"] is True
    assert settings.save({"AUTO_DOWNLOAD": "no"})["AUTO_DOWNLOAD"] is False
    assert settings.save({"AUTO_DOWNLOAD": "yes"})["AUTO_DOWNLOAD"] is True
    assert settings.save({"MAGNET_FALLBACK": "none"})["MAGNET_FALLBACK"] == "none"
    assert settings.save({"MAGNET_FALLBACK": "first"})["MAGNET_FALLBACK"] == "first"

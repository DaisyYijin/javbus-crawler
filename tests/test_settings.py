"""Settings validation tests (writes go to the isolated JC_DATADIR)."""
import pytest

from app import settings


def test_load_defaults():
    cfg = settings.load()
    assert cfg["CATEGORY"] in ("censored", "uncensored", "censored,uncensored")
    assert cfg["TAG_FILTER_MODE"] in ("mark", "only")
    assert cfg["DELAY_SECONDS"] >= 0
    assert "USER_AGENT" not in cfg  # removed in v0.9.16; UA lives in fetcher now


def test_category_multi_select():
    assert settings.save({"CATEGORY": "censored,uncensored"})["CATEGORY"] == "censored,uncensored"
    assert settings.save({"CATEGORY": "censored, uncensored"})["CATEGORY"] == "censored,uncensored"


def test_category_invalid_rejected():
    with pytest.raises(ValueError):
        settings.save({"CATEGORY": "bogus"})
    with pytest.raises(ValueError):
        settings.save({"CATEGORY": ""})


def test_genre_validation():
    assert settings.save({"GENRE": "42,hd"})["GENRE"] == "42,hd"
    assert settings.save({"GENRE": " 42 , hd "})["GENRE"] == "42,hd"
    assert settings.save({"GENRE": ""})["GENRE"] == ""
    with pytest.raises(ValueError):
        settings.save({"GENRE": "42,不良 id"})


def test_tag_filter_mode_legacy_migration():
    assert settings.save({"TAG_FILTER_MODE": "all"})["TAG_FILTER_MODE"] == "mark"
    assert settings.save({"TAG_FILTER_MODE": "only"})["TAG_FILTER_MODE"] == "only"
    with pytest.raises(ValueError):
        settings.save({"TAG_FILTER_MODE": "weird"})


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

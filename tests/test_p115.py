"""115 集成的离线测试（无网络、无真实账号）。"""
from app import p115


def test_p115_installed():
    assert p115.HAS_P115 is True  # requirements include p115client


def test_devices_table():
    assert p115.DEVICES.get("android") == "安卓"
    assert "web" in p115.DEVICES


def test_cookies_str_all_shapes():
    # list of dicts (documented shape)
    assert p115._cookies_str([{"name": "UID", "value": "1"},
                              {"name": "CID", "value": "2"}]) == "UID=1; CID=2"
    # list of "k=v" strings (shape observed in production)
    assert p115._cookies_str(["UID=1", "CID=2"]) == "UID=1; CID=2"
    # plain dict
    assert p115._cookies_str({"UID": "1", "CID": "2"}) == "UID=1; CID=2"
    # junk is dropped, empty stays empty
    assert p115._cookies_str(["junk", "UID=1"]) == "UID=1"
    assert p115._cookies_str([]) == ""
    assert p115._cookies_str(None) == ""


def test_sanitize_name():
    s = p115._sanitize_name('a<b>c:d"e/f\\g|h?i*j')
    for ch in '<>:"/\\|?*':
        assert ch not in s
    assert p115._sanitize_name(None) == ""
    assert p115._sanitize_name("  ok  ") == "ok"


def test_split_path():
    assert p115._split_path("待整理") == ["待整理"]
    assert p115._split_path(" 一级 / 二级 ") == ["一级", "二级"]
    assert p115._split_path("a\\b\\c") == ["a", "b", "c"]
    assert p115._split_path(" // ") == []
    assert p115._split_path(None) == []


def test_looks_ad_extensions():
    assert p115.looks_ad("请支持.txt") is True
    assert p115.looks_ad("www.example.com.url") is True
    assert p115.looks_ad("readme.html") is True
    assert p115.looks_ad("setup.exe") is True


def test_looks_ad_names():
    assert p115.looks_ad("最新地址 www.abc.xyz") is True
    assert p115.looks_ad("【防失联】电报群 t.me/xxxx") is True
    assert p115.looks_ad("xxxx-宣传图.jpg") is True
    assert p115.looks_ad("中文不卡高清资源") is True


def test_looks_ad_legit_names():
    assert p115.looks_ad("BANK-248 1080p.mp4") is False
    assert p115.looks_ad("SSIS-100 中文字幕.mkv") is False
    assert p115.looks_ad("赌神大战拉斯维加斯.mp4") is False  # 赌 alone must not match
    assert p115.looks_ad("") is False
    assert p115.looks_ad(None) is False


def test_map_task_status():
    assert p115._map_task_status(2) == "已完成"
    assert p115._map_task_status(-1) == "失败"
    assert p115._map_task_status("1") == "下载中"
    assert p115._map_task_status(None) == "未知"
    assert "未知" in p115._map_task_status(99)


def test_no_auth_state():
    p115.logout()
    assert p115.has_auth() is False
    assert p115.get_client() is None
    assert p115.status()["logged_in"] is False
    assert p115.qr_poll() == {"status": "none"}


def test_auth_roundtrip():
    p115._save_auth("UID=1; CID=2", "android")
    assert p115._load_auth() == {"cookies": "UID=1; CID=2", "app": "android"}
    assert p115.has_auth() is True
    p115.logout()
    assert p115.has_auth() is False


def test_qr_svg():
    assert p115.qr_svg() == ""  # no active QR
    p115._qr["url"] = "https://115.com/scan/dg-test"
    try:
        svg = p115.qr_svg()
        assert "<svg" in svg and "</svg>" in svg and "path" in svg
    finally:
        p115._qr.clear()


def test_import_with_broken_home():
    """Regression: container runs as nobody (HOME=/nonexistent); importing
    app.p115 must not crash when the home dir cannot host the cache."""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["HOME"] = "/nonexistent"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", "import app.p115; print('OK')"],
                       cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


# ------------------------------------------------------- code extraction ----

def test_extract_code_decorated_variants():
    # user-reported shapes: trailing -ch / -C / -UC / -UCD / -4K decorations
    assert p115.extract_code("aaa-100-ch aaa.mp4") == "AAA-100"
    assert p115.extract_code("IPX-528-C.mp4") == "IPX-528"
    assert p115.extract_code("JUQ-417-UC.mp4") == "JUQ-417"
    assert p115.extract_code("MIDV-00218-UCD") == "MIDV-00218"
    assert p115.extract_code("abp-984-4K.mp4") == "ABP-984"
    assert p115.extract_code("SSIS-100 中文字幕.mkv") == "SSIS-100"
    assert p115.extract_code("BANK-248 1080p.mp4") == "BANK-248"


def test_extract_code_prefixed_and_fc2():
    # numeric prefixes glued to the label
    assert p115.extract_code("T28-619") == "T28-619"
    assert p115.extract_code("259LUXU-1234.mp4") == "259LUXU-1234"
    assert p115.extract_code("300MIUM-703") == "300MIUM-703"
    assert p115.extract_code("FC2-PPV-1234567.mp4") == "FC2-PPV-1234567"
    assert p115.extract_code("FC2PPV_1234567") == "FC2-PPV-1234567"


def test_extract_code_plain_and_garbage():
    assert p115.extract_code("AAA100.mp4") == "AAA-100"
    assert p115.extract_code("最新地址 www.abc.xyz") is None
    assert p115.extract_code("") is None
    assert p115.extract_code(None) is None


# ------------------------------------------------------------ del_tasks ----

class _FakeDelClient:
    def __init__(self):
        self.payloads = []

    def clouddownload_task_del(self, payload, timeout=None):
        self.payloads.append(dict(payload))
        return {"state": True}


def test_del_tasks_purge_payload(monkeypatch):
    fake = _FakeDelClient()
    monkeypatch.setattr(p115, "get_client", lambda: fake)
    assert p115.del_tasks(["ABC", "DEF"], purge_files=True) == 2
    assert fake.payloads[-1] == {"hash[0]": "abc", "hash[1]": "def", "flag": 1}
    assert p115.del_tasks(["GHI"]) == 1
    assert fake.payloads[-1] == {"hash[0]": "ghi"}  # no flag -> files kept
    assert p115.del_tasks(["  "]) == 0
    assert len(fake.payloads) == 2


# ------------------------------------------------------- track records ----

def test_track_roundtrip():
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    p115.track_add("AAA-100", "aabb", "magnet:?xt=urn:btih:aabb", "aaa-100.mp4")
    recs = p115.track_records()
    assert recs["AABB"]["code"] == "AAA-100"
    assert recs["AABB"]["tried"] == ["AABB"]
    assert recs["AABB"]["last_percent"] == -1
    p115.track_add("AAA-100", "CCDD", "magnet:?xt=urn:btih:ccdd", "",
                   tried={"aabb"}, retries=1)
    recs = p115.track_records()
    assert recs["CCDD"]["tried"] == ["AABB", "CCDD"]
    assert recs["CCDD"]["retries"] == 1
    p115._track_update("CCDD", last_percent=42, last_prog_at=1)
    assert p115.track_records()["CCDD"]["last_percent"] == 42
    p115.track_del("AABB")
    p115.track_del("CCDD")
    assert p115.track_records() == {}
    p115.track_add("AAA-100", "", "magnet:?xt=urn:btih:x")  # empty hash: no-op
    assert p115.track_records() == {}


# ------------------------------------------------------------ metatube ----

def test_metatube_title(monkeypatch):
    from app import settings
    settings.save({"METATUBE_URL": "", "METATUBE_TOKEN": ""})
    try:
        assert p115.metatube_title("AAA-100") is None  # unset -> no HTTP at all

        settings.save({"METATUBE_URL": "http://mt.test", "METATUBE_TOKEN": "t"})

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": {"title": "剧场版 主题"}}

        monkeypatch.setattr(p115.requests, "get", lambda *a, **k: _Resp())
        assert p115.metatube_title("AAA-100") == "剧场版 主题"

        class _Resp404:
            status_code = 404

            def json(self):
                return {}

        monkeypatch.setattr(p115.requests, "get", lambda *a, **k: _Resp404())
        assert p115.metatube_title("AAA-100") is None

        def _boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(p115.requests, "get", _boom)
        assert p115.metatube_title("AAA-100") is None
    finally:
        settings.save({"METATUBE_URL": "", "METATUBE_TOKEN": ""})


# ----------------------------------------------------------- watch loop ----

class _FakeWatchClient:
    def __init__(self, tasks):
        self._tasks = tasks

    def clouddownload_task_list(self, payload, timeout=None):
        if int(payload.get("page") or 1) == 1:
            return {"state": True, "count": len(self._tasks), "tasks": self._tasks}
        return {"state": True, "count": 0, "tasks": []}


def test_watch_pass_finished_organizes(monkeypatch):
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    p115.track_add("AAA-100", "FINI01", "magnet:?xt=urn:btih:fini01", "t.mp4")
    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                        _FakeWatchClient([
                            {"info_hash": "fini01", "status": 2,
                             "percentDone": 100, "name": "t.mp4"}]))
    calls = []
    monkeypatch.setattr(p115, "organize_pass",
                        lambda: calls.append(1) or {"organized": 1})
    stats = p115.watch_pass()
    assert stats["done"] == 1 and stats["organized"] == 1
    assert calls == [1]  # P115_AUTO_ORGANIZE is on by default
    assert p115.track_records() == {}


def test_watch_pass_failed_swaps_magnet(monkeypatch):
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    p115.track_add("AAA-100", "OLDF01", "magnet:?xt=urn:btih:oldf01", "t.mp4")
    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                        _FakeWatchClient([
                            {"info_hash": "oldf01", "status": -1,
                             "percentDone": 0, "name": "t.mp4"}]))
    monkeypatch.setattr(p115, "magnet_candidates", lambda code, exclude: {
        "hash": "NEWF02", "link": "magnet:?xt=urn:btih:newf02", "name": "n"})
    monkeypatch.setattr(p115, "add_magnet",
                        lambda link: {"info_hash": "newf02", "name": "n"})
    deleted = []
    monkeypatch.setattr(p115, "del_tasks",
                        lambda hashes, **k: deleted.extend(hashes) or 1)
    monkeypatch.setattr(p115, "organize_pass", lambda: {"organized": 0})
    stats = p115.watch_pass()
    assert stats["swapped"] == 1
    assert deleted == ["OLDF01"]
    recs = p115.track_records()
    assert "OLDF01" not in recs
    assert recs["NEWF02"]["retries"] == 1
    assert recs["NEWF02"]["tried"] == ["NEWF02", "OLDF01"]  # sorted set
    p115.track_del("NEWF02")


def test_watch_pass_stalled_and_progress(monkeypatch):
    import time as _time
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    p115.track_add("AAA-100", "STALL1", "magnet:?xt=urn:btih:stall1", "t.mp4")
    p115.track_add("AAA-100", "MOVING", "magnet:?xt=urn:btih:moving", "t.mp4")
    old = int(_time.time()) - 3600  # 1h without progress -> past the 30min stall
    p115._track_update("STALL1", last_percent=50, last_prog_at=old)
    p115._track_update("MOVING", last_percent=10, last_prog_at=old)
    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                        _FakeWatchClient([
                            {"info_hash": "stall1", "status": 1,
                             "percentDone": 50, "name": "a.mp4"},
                            {"info_hash": "moving", "status": 1,
                             "percentDone": 25, "name": "b.mp4"}]))
    swaps = []

    def _fake_swap(ih, rec, mx):
        swaps.append(ih)
        p115.track_del(ih)  # mirror the real swap's cleanup
        return "swapped"

    monkeypatch.setattr(p115, "_swap_magnet", _fake_swap)
    monkeypatch.setattr(p115, "organize_pass", lambda: {"organized": 0})
    stats = p115.watch_pass()
    assert swaps == ["STALL1"]          # no progress -> swap
    assert stats["swapped"] == 1
    recs = p115.track_records()
    assert "STALL1" not in recs
    assert recs["MOVING"]["last_percent"] == 25  # progress recorded, kept
    p115.track_del("MOVING")

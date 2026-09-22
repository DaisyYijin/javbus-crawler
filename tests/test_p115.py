"""115 集成的离线测试（无网络、无真实账号）。"""
import logging

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
    recs = p115.track_records()
    assert set(recs) == {"FINI01"}  # slot held until organize lands the file
    assert recs["FINI01"]["finished_at"] > 0
    p115.track_del("FINI01")


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


def test_watch_pass_over_total_time_swaps(monkeypatch):
    import time as _time
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    saved = settings.load().get("P115_DL_MAX_MIN")
    settings.save({"P115_DL_MAX_MIN": 60})
    try:
        p115.track_add("AAA-100", "SLOW01", "magnet:?xt=urn:btih:slow01", "t.mp4")
        old = int(_time.time()) - 2 * 3600  # 2h at it, cap is 60 min
        p115._track_update("SLOW01", submitted_at=old,
                           last_percent=10, last_prog_at=int(_time.time()))
        monkeypatch.setattr(p115, "has_auth", lambda: True)
        monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                            _FakeWatchClient([
                                {"info_hash": "slow01", "status": 1,
                                 "percentDone": 10, "name": "s.mp4"}]))
        swaps = []

        def _fake_swap(ih, rec, mx):
            swaps.append(ih)
            p115.track_del(ih)  # mirror the real swap's cleanup
            return "swapped"

        monkeypatch.setattr(p115, "_swap_magnet", _fake_swap)
        monkeypatch.setattr(p115, "organize_pass", lambda: {"organized": 0})
        stats = p115.watch_pass()
        assert swaps == ["SLOW01"]  # over the cap even though it progresses
        assert stats["swapped"] == 1
    finally:
        settings.save({"P115_DL_MAX_MIN": int(saved or 120)})
        p115.track_del("SLOW01")


def test_watch_pass_finished_no_auto_organize_frees_slot(monkeypatch):
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    saved = settings.load().get("P115_AUTO_ORGANIZE")
    settings.save({"P115_AUTO_ORGANIZE": False})
    try:
        p115.track_add("AAA-100", "FINI02", "magnet:?xt=urn:btih:fini02", "t.mp4")
        monkeypatch.setattr(p115, "has_auth", lambda: True)
        monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                            _FakeWatchClient([
                                {"info_hash": "fini02", "status": 2,
                                 "percentDone": 100, "name": "t.mp4"}]))
        called = []
        monkeypatch.setattr(p115, "organize_pass",
                            lambda: called.append(1) or {"organized": 0})
        stats = p115.watch_pass()
        assert stats["done"] == 1 and called == []
        assert p115.track_records() == {}  # no organize switch: slot freed
    finally:
        settings.save({"P115_AUTO_ORGANIZE": bool(saved)})
        p115.track_del("FINI02")


def test_watch_pass_vanished_fresh_task_keeps_slot(monkeypatch):
    """刚提交的任务还没出现在 115 任务列表（API 传播延迟）：宽限期内不得
    释放记录，否则串行等待会误报「整理刚完成」而目录里什么都没有。"""
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    p115.track_add("AAA-100", "VANISH1", "magnet:?xt=urn:btih:vanish1", "t.mp4")
    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                        _FakeWatchClient([]))  # task list is empty
    stats = p115.watch_pass()
    assert stats["checked"] == 1
    assert set(p115.track_records()) == {"VANISH1"}  # grace window: kept
    p115.track_del("VANISH1")


def test_watch_pass_vanished_old_task_releases_with_gaveup(monkeypatch):
    """超过宽限期任务仍不在列表（被手动删除或被 115 拒绝）：释放槽位并写
    gaveup 终止标记，串行等待才能及时感知并退出。"""
    import time as _time

    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    p115.track_add("AAA-100", "VANISH2", "magnet:?xt=urn:btih:vanish2", "t.mp4")
    p115._track_update("VANISH2",
                       submitted_at=int(_time.time()) - 3600)  # past grace
    monkeypatch.setattr(p115, "has_auth", lambda: True)
    monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                        _FakeWatchClient([]))
    stats = p115.watch_pass()
    assert stats["checked"] == 1
    assert p115.track_records() == {}  # slot released
    assert p115.gaveup_codes() == {"AAA-100"}  # termination marker for settle
    import sqlite3

    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        conn.execute("DELETE FROM meta WHERE key = ?", ("p115:gaveup:VANISH2",))
        conn.commit()
    finally:
        conn.close()


def test_track_add_clears_stale_done_marker():
    """重新提交同一磁力时，上一轮尝试留下的 done/fail 标记必须作废——
    过期 done 曾让 organize_pass 见标记即放行、串行门误信「已整理」。"""
    import sqlite3

    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                     "('p115:done:STALE1', '1')")
        conn.commit()
    finally:
        conn.close()
    assert p115.is_done("STALE1") is True
    p115.track_add("AAA-100", "STALE1", "magnet:?xt=urn:btih:stale1", "t.mp4")
    assert p115.is_done("STALE1") is False  # stale proof invalidated
    assert set(p115.track_records()) == {"STALE1"}
    p115.track_del("STALE1")


def test_organize_missing_fid_retries_then_gives_up(monkeypatch):
    """完成的任务在列表里还没带 file_id（字段滞后于状态翻转）：不能打假
    done 直接放行（串行门会误信「已整理」而目录没动），应按失败计数重试，
    5 次后才终结放行。"""
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    p115.track_add("AAA-100", "NOFID1", "magnet:?xt=urn:btih:nofid1", "t.mp4")
    monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                        _FakeWatchClient([
                            {"info_hash": "nofid1", "status": 2,
                             "percentDone": 100, "name": "t.mp4"}]))
    for _ in range(4):  # attempts 1-4: hold the slot, no fake done
        p115.organize_pass()
        assert set(p115.track_records()) == {"NOFID1"}
        assert p115.is_done("NOFID1") is False
    p115.organize_pass()  # 5th failure -> give up and release
    assert p115.track_records() == {}
    assert p115.is_done("NOFID1") is True  # terminal mark for the serial gate


def test_organize_fs_file_throttled_does_not_guess(monkeypatch):
    """fs_file 被限流时「文件夹还是文件」未知：绝不能猜「文件」——那会把
    整个下载文件夹当正片改名搬进 已整理/番号 剧名/（真正片没改名、广告
    全程随行、拒收 0）。应记一次失败，60 秒整理循环稍后重试。"""
    conn = _sweep_db(monkeypatch, movies=[("FIT-008", "初撮り")])
    try:
        renames, moves = [], []

        class _ThrottledClient:
            def clouddownload_task_list(self, payload, timeout=None):
                return {"state": True, "count": 1, "tasks": [
                    {"info_hash": "fit8hash", "status": 2, "name": "fit-008ch",
                     "file_id": 12345, "percentDone": 100}]}

            def fs_file(self, fid, timeout=None):
                raise RuntimeError("throttled (empty listing)")

            def fs_rename(self, payload, timeout=None):
                renames.append(payload)
                return {"state": True}

            def fs_move(self, fid, cid, timeout=None):
                moves.append((fid, cid))
                return {"state": True}

        monkeypatch.setattr(p115, "get_client",
                            lambda refresh=False: _ThrottledClient())
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: 10)
        monkeypatch.setattr(p115, "_SWEEP_LIST_GAP", 0)  # no pacing in tests
        p115.track_add("FIT-008", "FIT8HASH", "magnet:?x", "fit-008ch")
        stats = p115.organize_pass()
        assert renames == [] and moves == []      # nothing renamed or moved
        assert stats["organized"] == 0 and stats["rejected"] == 0
        assert set(p115.track_records()) == {"FIT8HASH"}  # slot still held
        assert p115.is_done("FIT8HASH") is False  # no fake done mark
        p115.track_del("FIT8HASH")
    finally:
        conn.close()


def test_organize_get_info_dir_wrapped_in_list(monkeypatch):
    """115 的 get_info 对目录会把信息包在裸列表里返回：解包后按 fc=0 判定
    走文件夹整理。此前 data.get 直接 AttributeError，被误当限流无限重试
    ——文件夹任务从未真正走通过任务整理。"""
    conn = _sweep_db(monkeypatch, movies=[("FIT-008", "初撮り")])
    try:
        class _DirInfoClient:
            def clouddownload_task_list(self, payload, timeout=None):
                return {"state": True, "count": 1, "tasks": [
                    {"info_hash": "dirih01", "status": 2, "name": "fit-008ch",
                     "file_id": 777, "percentDone": 100}]}

            def fs_file(self, fid, timeout=None):
                return {"state": True, "data": [{"file_name": "fit-008ch",
                                                 "fc": "0"}]}

        calls = {}
        monkeypatch.setattr(p115, "get_client",
                            lambda refresh=False: _DirInfoClient())
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: 10)
        monkeypatch.setattr(p115, "_SWEEP_LIST_GAP", 0)
        monkeypatch.setattr(p115, "_sweep_folder",
                            lambda *a, **k: calls.setdefault("folder", 1) or True)
        monkeypatch.setattr(p115, "_sweep_file",
                            lambda *a, **k: calls.setdefault("file", 1) or True)
        p115.track_add("FIT-008", "DIRIH01", "magnet:?x", "fit-008ch")
        p115.organize_pass()
        assert calls.get("folder") == 1 and "file" not in calls
        p115.track_del("DIRIH01")
    finally:
        conn.close()


def test_organize_get_info_file_without_fc(monkeypatch):
    """无 fc 字段的文件信息（带 sha1/体积）：按证据判为文件走单文件整理，
    不进入猜错文件夹的重试循环。"""
    conn = _sweep_db(monkeypatch, movies=[("FIT-008", "初撮り")])
    try:
        class _FileInfoClient:
            def clouddownload_task_list(self, payload, timeout=None):
                return {"state": True, "count": 1, "tasks": [
                    {"info_hash": "filih01", "status": 2, "name": "fit-008ch",
                     "file_id": 888, "percentDone": 100}]}

            def fs_file(self, fid, timeout=None):
                return {"state": True, "data": {"file_name": "fit-008ch",
                                                "sha1": "ABC", "size": 5}}

        calls = {}
        monkeypatch.setattr(p115, "get_client",
                            lambda refresh=False: _FileInfoClient())
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: 10)
        monkeypatch.setattr(p115, "_SWEEP_LIST_GAP", 0)

        def folder_attempt(*a, **k):
            calls.setdefault("folder", 1)
            return False  # empty listing -> not a folder, fall through

        monkeypatch.setattr(p115, "_sweep_folder", folder_attempt)
        monkeypatch.setattr(p115, "_sweep_file",
                            lambda *a, **k: calls.setdefault("file", 1) or True)
        p115.track_add("FIT-008", "FILIH01", "magnet:?x", "fit-008ch")
        p115.organize_pass()
        assert calls.get("file") == 1  # fs_file said file -> single-file path
        p115.track_del("FILIH01")
    finally:
        conn.close()


def test_list_gap_configurable(monkeypatch):
    """P115_LIST_GAP_SEC 覆盖列表间隔；0 回退内置默认（可被测试 monkeypatch）。"""
    from app import settings
    saved = settings.load().get("P115_LIST_GAP_SEC")
    try:
        settings.save({"P115_LIST_GAP_SEC": 20})
        assert p115._list_gap() == 20
        settings.save({"P115_LIST_GAP_SEC": 0})
        monkeypatch.setattr(p115, "_SWEEP_LIST_GAP", 7.0)
        assert p115._list_gap() == 7.0
    finally:
        settings.save({"P115_LIST_GAP_SEC": int(saved or 0)})


def test_sweep_folder_junk_move_conflict_still_handled(monkeypatch):
    """清空后的壳移入冗余失败（典型：10 分钟扫描已把它移进冗余，115 拒绝
    移到自身所在目录）：正片已归档就必须视为已处理并放行串行槽，不能让
    同一任务每轮抛异常无限占槽。"""
    conn = _sweep_db(monkeypatch, movies=[("MIDA-790", "下海")])
    try:
        fake = _FakeSweepClient({
            600: [
                {"n": "MIDA-790.mp4", "fid": 601, "s": 2 * GIB, "fc": "1"},
                {"n": "广告.png", "fid": 602, "s": 2048, "fc": "1"},
            ],
        })
        real_move = fake.fs_move

        def move(fid, cid, timeout=None):
            if int(fid) == 600:  # the shell: 115 refuses (already in reject)
                raise RuntimeError("目标目录与当前目录相同")
            return real_move(fid, cid, timeout=timeout)

        fake.fs_move = move
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        handled = p115._sweep_folder(conn, fake, 600, "MIDA-790ch", 10, 20,
                                     0, stats)
        assert handled is True                 # organized, not wedged
        assert (601, 10) in fake.moves         # feature still filed
        assert fake.renames == [(601, "MIDA-790 下海.mp4")]
        assert stats == {"organized": 1, "rejected": 0}  # junk move tolerated
    finally:
        conn.close()


def test_organize_pass_exception_counts_and_releases(monkeypatch):
    """整理过程持续抛异常的任务也要计数：5 次后打 done 并放行，不能无限
    占住串行槽（此前 pass 级异常不计入任何上限）。"""
    conn = _sweep_db(monkeypatch, movies=[("FIT-008", "初撮り")])
    try:
        class _BoomSweepClient:
            def clouddownload_task_list(self, payload, timeout=None):
                return {"state": True, "count": 1, "tasks": [
                    {"info_hash": "boomih1", "status": 2, "name": "fit-008ch",
                     "file_id": 999, "percentDone": 100}]}

            def fs_file(self, fid, timeout=None):
                return {"state": True, "data": {"file_name": "fit-008ch",
                                                "fc": "1", "sha1": "x",
                                                "size": 5}}

        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(p115, "get_client",
                            lambda refresh=False: _BoomSweepClient())
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: 10)
        monkeypatch.setattr(p115, "_SWEEP_LIST_GAP", 0)
        monkeypatch.setattr(p115, "_sweep_file", boom)
        p115.track_add("FIT-008", "BOOMIH1", "magnet:?x", "fit-008ch")
        for _ in range(4):
            p115.organize_pass()
            assert set(p115.track_records()) == {"BOOMIH1"}  # held, counting
        p115.organize_pass()  # 5th exception -> give up and release
        assert p115.track_records() == {}
        assert p115.is_done("BOOMIH1") is True
    finally:
        conn.close()


def test_sweep_folder_skips_ad_subdirs(monkeypatch):
    """广告名子目录（最新地址/宣传图等）不逐个列目录——每次省一个约 55 秒
    的列表间隔；正片照常归档，广告子目录随壳进冗余。"""
    conn = _sweep_db(monkeypatch, movies=[("MIDA-790", "下海")])
    try:
        fake = _FakeSweepClient({
            700: [
                {"n": "MIDA-790.mp4", "fid": 701, "s": 2 * GIB, "fc": "1"},
                {"n": "最新地址", "cid": 702, "fc": "0"},
                {"n": "宣传图", "cid": 703, "fc": "0"},
            ],
            702: [{"n": "广告.url", "fid": 704, "s": 64, "fc": "1"}],
            703: [{"n": "海报.jpg", "fid": 705, "s": 8192, "fc": "1"}],
        })
        asked = []

        def page(c, cid, off, limit=100):
            asked.append(cid)
            return fake.listings.get(cid, []), True

        monkeypatch.setattr(p115, "_fs_page", page)
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 700, "MIDA-790ch", 10, 20, 0, stats)
        assert 702 not in asked and 703 not in asked  # ad dirs never listed
        assert fake.renames == [(701, "MIDA-790 下海.mp4")]
        assert (701, 10) in fake.moves
        assert [f for f, t in fake.moves if t == 20] == [700]
    finally:
        conn.close()


def test_sweep_folder_ad_only_subdirs_junked(monkeypatch):
    """只有广告名子目录、无任何正片的下载文件夹：子目录不列表，整体直接
    进冗余——不能因「没列过子目录」误判为限流无限跳过。"""
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({
            800: [{"n": "最新网址", "cid": 801, "fc": "0"}],
            801: [{"n": "广告.url", "fid": 802, "s": 64, "fc": "1"}],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0, "skipped": 0}
        handled = p115._sweep_folder(conn, fake, 800, "垃圾下载", 10, 20,
                                     0, stats)
        assert handled is True                    # junked, not skipped
        assert fake.moves == [(800, 20)]          # whole folder -> reject
        assert stats["rejected"] == 1 and stats["skipped"] == 0
    finally:
        conn.close()


def test_sweep_defers_while_organizing():
    """目录扫描与任务整理共用同一把锁：整理进行中扫描必须让路——否则两条
    路径并发处理同一文件夹，各自创建 已整理/番号 剧名/（115 允许同名目录
    并存）产生重复目录与重复重命名/移动日志。"""
    p115._organize_lock.acquire()  # pretend an organize pass is running
    try:
        stats = p115.sweep_existing(sleep_s=0)
        assert stats.get("busy") is True
        assert p115.sweep_busy() is False  # early return reset the flag
    finally:
        p115._organize_lock.release()
    # lock free again -> a real pass runs (throttled-empty listing is fine)
    stats = p115.sweep_existing(sleep_s=0)
    assert "busy" not in stats


def test_gaveup_codes_and_done_mark_release_slot(monkeypatch):
    import json as _json
    import sqlite3
    from app import db, settings
    db.connect(settings.load()["DB_PATH"]).close()
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                     "('p115:gaveup:dead01', ?)",
                     (_json.dumps({"code": "GUV-001", "reason": "x"}),))
        conn.commit()
    finally:
        conn.close()
    assert p115.gaveup_codes() == {"GUV-001"}
    # a finished download still holding its slot is released by the done
    # mark (stamped after track_add — real order — e.g. when track_del
    # failed on a locked db right after organize succeeded)
    p115.track_add("GUV-001", "DONEDL", "magnet:?xt=urn:btih:donedl", "t.mp4")
    conn = sqlite3.connect(settings.load()["DB_PATH"], timeout=10)
    try:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                     "('p115:done:DONEDL', '1')")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(p115, "get_client", lambda refresh=False:
                        _FakeWatchClient([
                            {"info_hash": "donedl", "status": 2,
                             "percentDone": 100, "name": "t.mp4"}]))
    p115.organize_pass()
    assert p115.track_records() == {}


def test_track_del_stamps_release_time():
    import time as _time
    before = p115.last_release_at()
    p115.track_del("NOSUCH01")  # no-op: must not move the cooldown timer
    assert p115.last_release_at() == before
    p115.track_add("GUV-002", "RELTM01", "magnet:?xt=urn:btih:reltm01", "t.mp4")
    p115.track_del("RELTM01")
    rel = p115.last_release_at()
    assert before <= rel and _time.time() - rel < 120


# ------------------------------------------------------------ list files ----

def test_list_files_filters_and_pages(monkeypatch):
    def fake_page(c, cid, offset, limit=100):
        if offset == 0:
            return [
                {"n": "AAA-100.mp4", "fid": 111, "s": 4096, "t": 200, "fc": "1"},
                {"n": "新目录", "cid": 222, "fc": "0"},
                {"n": "BBB-200.mp4", "fid": 333, "s": 8192, "t": 100},
            ], False
        return [], True

    monkeypatch.setattr(p115, "get_client", lambda refresh=False: object())
    monkeypatch.setattr(p115, "_fs_page", fake_page)
    out = p115.list_files(7)
    assert out["cid"] == 7 and out["dir_count"] == 1
    assert out["files"] == [  # newest first, fid stringified for JS
        {"fid": "111", "name": "AAA-100.mp4", "size": 4096, "t": 200},
        {"fid": "333", "name": "BBB-200.mp4", "size": 8192, "t": 100},
    ]


def test_list_files_path_resolves_cid(monkeypatch):
    monkeypatch.setattr(p115, "get_client", lambda refresh=False: object())
    monkeypatch.setattr(p115, "resolve_dir", lambda c, p: 42)
    monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                        ([], True))
    out = p115.list_files(0, "JAV/待整理")
    assert out["cid"] == 42 and out["files"] == []


def test_list_files_requires_auth(monkeypatch):
    monkeypatch.setattr(p115, "get_client", lambda refresh=False: None)
    try:
        p115.list_files()
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "未登录" in str(exc)


def test_list_files_tolerates_string_timestamps(monkeypatch):
    def fake_page(c, cid, offset, limit=100):
        return [
            {"n": "a.mp4", "fid": 1, "s": "1024", "t": "2026-09-20 16:32",
             "fc": "1"},
            {"n": "b.mp4", "fid": 2, "s": 2048, "t": 123, "fc": "1"},
        ], True

    monkeypatch.setattr(p115, "get_client", lambda refresh=False: object())
    monkeypatch.setattr(p115, "_fs_page", fake_page)
    out = p115.list_files(7)
    assert out["files"] == [
        {"fid": "2", "name": "b.mp4", "size": 2048, "t": 123},
        {"fid": "1", "name": "a.mp4", "size": 1024, "t": 0},
    ]


# ---------------------------------------------------- sweep (backfill) ----

GIB = 1024 * 1024 * 1024


class _FakeSweepClient:
    """fs_files listings per cid + records fs_move/fs_rename calls."""

    def __init__(self, listings):
        self.listings = listings
        self.moves = []    # (fid, target_cid)
        self.renames = []  # (fid, new_name)

    def fs_move(self, fid, cid, timeout=None):
        self.moves.append((int(fid), int(cid)))
        return {"state": True}

    def fs_rename(self, payload, timeout=None):
        fid, name = payload
        self.renames.append((int(fid), name))
        return {"state": True}


def _sweep_db(monkeypatch, movies=()):
    """Test DB with tables set up, optional library rows; returns a conn."""
    from app import db, settings
    settings.save({"METATUBE_URL": "", "METATUBE_TOKEN": ""})
    conn = db.connect(settings.load()["DB_PATH"])
    for code, title in movies:
        conn.execute("INSERT OR REPLACE INTO movies(code, url, title, "
                     "first_seen, last_seen) VALUES (?, '', ?, '1', '1')",
                     (code, title))
    conn.commit()
    return conn


def test_looks_ad_padded_and_images():
    # spaces between characters no longer dodge the keyword filter
    assert p115.looks_ad("最 新 地 址 www.abc.xyz") is True
    assert p115.looks_ad("每 日 更 新") is True
    # promo images / posters are junk even without keywords
    assert p115.looks_ad("poster.png") is True
    assert p115.looks_ad("xxxx-剧照.jpg") is True
    assert p115.looks_ad("封面.webp") is True
    assert p115.looks_ad("BANK-248 1080p.mp4") is False  # videos stay legit


def test_ext_of():
    assert p115._ext_of("a.MP4") == ".mp4"
    assert p115._ext_of("x.rmvb") == ".rmvb"
    assert p115._ext_of("noext") == ""
    assert p115._ext_of("a.abcdef") == ""  # too long to be an extension
    assert p115._ext_of("") == ""


def test_item_fid_reads_cid_for_dirs():
    # fs_files lists dirs with "cid" (no "fid") and files with "fid"
    assert p115._item_fid({"n": "dir", "cid": 2621499, "fc": "0"}) == 2621499
    assert p115._item_fid({"n": "f.mp4", "fid": 111, "fc": "1"}) == 111
    assert p115._item_fid({"n": "x"}) is None


def test_sweep_file_feature_and_junk(monkeypatch):
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({})
        stats = {"organized": 0, "rejected": 0}
        # junk: ad image, non-video ext, sub-1GiB clip all go to reject
        p115._sweep_file(conn, fake, 1, "广告.png", 10, 10, 20, stats)
        p115._sweep_file(conn, fake, 2, "b.txt", 4096, 10, 20, stats)
        p115._sweep_file(conn, fake, 3, "c.mp4", 1024 * 1024, 10, 20, stats)
        assert fake.moves == [(1, 20), (2, 20), (3, 20)]
        assert stats == {"organized": 0, "rejected": 3}
        # library hit: renamed (spam stripped) then moved to target
        conn.execute("INSERT OR REPLACE INTO movies(code, url, title, "
                     "first_seen, last_seen) "
                     "VALUES ('SSIS-100', '', '标题X', '1', '1')")
        conn.commit()
        p115._sweep_file(conn, fake, 4, "SSIS-100 [1080p].mp4", 2 * GIB,
                         10, 20, stats)
        assert fake.renames == [(4, "SSIS-100 标题X.mp4")]
        assert fake.moves[-1] == (4, 10)
        assert stats["organized"] == 1
        # unknown big video without a library row: kept as-is -> target
        p115._sweep_file(conn, fake, 5, "UNKNOWN-9.mp4", 2 * GIB,
                         10, 20, stats)
        assert fake.moves[-1] == (5, 10)
        assert stats["organized"] == 2
    finally:
        conn.close()


def test_sweep_file_extensionless_download_gets_mp4(monkeypatch):
    """单文件离线任务以磁力 dn= 命名（无扩展名，如 fit-008ch）：命中库内
    番号时必须补 .mp4 再归档——裸名会让文件与父目录同名（已整理/X/X），
    既像双层嵌套又无法播放。"""
    conn = _sweep_db(monkeypatch, movies=[("FIT-008", "初撮り")])
    try:
        fake = _FakeSweepClient({})
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: 777)
        stats = {"organized": 0, "rejected": 0}
        handled = p115._sweep_file(conn, fake, 9, "fit-008ch", 2 * GIB,
                                   10, 20, stats, target_path="已整理",
                                   prefer_name="FIT-008 初撮り")
        assert handled is True
        assert fake.renames == [(9, "FIT-008 初撮り.mp4")]  # .mp4 appended
        assert fake.moves == [(9, 777)]
        assert stats["organized"] == 1
    finally:
        conn.close()


def test_sweep_folder_single_feature(monkeypatch):
    conn = _sweep_db(monkeypatch, movies=[("MIDA-790", "下海")])
    try:
        fake = _FakeSweepClient({
            100: [  # folder content: feature + ads + a subdir
                {"n": "MIDA-790.mp4", "fid": 101, "s": 2 * GIB, "fc": "1"},
                {"n": "广告.png", "fid": 102, "s": 2048, "fc": "1"},
                {"n": "宣传.url", "fid": 103, "s": 128, "fc": "1"},
                {"n": "CD1", "cid": 104, "fc": "0"},
            ],
            104: [{"n": "占位.txt", "fid": 105, "s": 8, "fc": "1"}],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 100, "MIDA-790ch", 10, 20, 0, stats)
        # feature renamed with the library title and moved to the target dir
        assert fake.renames == [(101, "MIDA-790 下海.mp4")]
        assert (101, 10) in fake.moves
        # ads + junk + subdir stay INSIDE the folder, which moves to reject
        # as ONE unit: 冗余/MIDA-790ch/广告.png … — junk from two downloads
        # keeps its own sub-dir instead of melting into same-named loose files
        assert [f for f, t in fake.moves if t == 20] == [100]
        assert stats == {"organized": 1, "rejected": 1}
    finally:
        conn.close()


def test_sweep_folder_all_junk_to_reject(monkeypatch):
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({
            200: [
                {"n": "广告.png", "fid": 201, "s": 2048, "fc": "1"},
                {"n": "www.spam.com.url", "fid": 202, "s": 64, "fc": "1"},
            ],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 200, "垃圾目录", 10, 20, 0, stats)
        assert fake.moves == [(200, 20)]  # whole folder, nothing listed inside
        assert stats == {"organized": 0, "rejected": 1}
    finally:
        conn.close()


def test_sweep_folder_multi_code_kept_intact(monkeypatch):
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({
            300: [
                {"n": "AAA-100.mp4", "fid": 301, "s": 2 * GIB, "fc": "1"},
                {"n": "BBB-200.mp4", "fid": 302, "s": 2 * GIB, "fc": "1"},
            ],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 300, "合集", 10, 20, 0, stats)
        assert fake.moves == [(300, 10)]  # intact to target, nothing renamed
        assert stats == {"organized": 1, "rejected": 0}
    finally:
        conn.close()


def test_sweep_folder_multititle_splits_per_code(monkeypatch):
    """捆绑多部影片的下载文件夹（每部恰好一个文件）：逐部按库内番号
    重命名、各自归档到 已整理/番号 剧名/，广告与未识别内容随壳进冗余。
    此前这种文件夹整体保留，捆绑的 fit-008ch.mp4 从未被重命名。"""
    conn = _sweep_db(monkeypatch, movies=[("FIT-008", "初撮り"),
                                          ("FNS-248", "不倫")])
    try:
        fake = _FakeSweepClient({
            400: [
                {"n": "fit-008ch.mp4", "fid": 401, "s": 2 * GIB, "fc": "1"},
                {"n": "FNS-248 不倫中で.mp4", "fid": 402, "s": 2 * GIB,
                 "fc": "1"},
                {"n": "宣传.url", "fid": 403, "s": 64, "fc": "1"},
            ],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        table = {"已整理": 10, "已整理/FIT-008 初撮り": 41,
                 "已整理/FNS-248 不倫": 42}
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: table[str(p)])
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 400, "FNS-248 不倫中で…", 10, 20,
                           0, stats, target_path="已整理")
        # both bundled features renamed via their OWN library entries
        assert fake.renames == [(401, "FIT-008 初撮り.mp4"),
                                (402, "FNS-248 不倫.mp4")]
        assert (401, 41) in fake.moves and (402, 42) in fake.moves
        # ads + shell travel to reject as ONE folder
        assert [f for f, t in fake.moves if t == 20] == [400]
        assert stats == {"organized": 2, "rejected": 1}
    finally:
        conn.close()


def test_sweep_folder_intact_no_double_nesting(monkeypatch):
    """多分段文件夹整体保留时直接落在 已整理/<目录名>/ 下——移进同名子
    目录曾造成 已整理/X/X/… 的双层嵌套。"""
    conn = _sweep_db(monkeypatch, movies=[("MIDA-790", "下海")])
    try:
        fake = _FakeSweepClient({
            500: [
                {"n": "MIDA-790 CD1.mp4", "fid": 501, "s": 4 * GIB, "fc": "1"},
                {"n": "MIDA-790 CD2.mp4", "fid": 502, "s": 4 * GIB, "fc": "1"},
            ],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        # any resolve_dir call for a per-movie path would mean double nesting
        monkeypatch.setattr(p115, "resolve_dir",
                            lambda c, p: (_ for _ in ()).throw(
                                AssertionError(f"unexpected nested: {p}")))
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 500, "MIDA-790ch", 10, 20, 0, stats,
                           target_path="已整理")
        assert fake.moves == [(500, 10)]  # straight under the target root
        assert stats == {"organized": 1, "rejected": 0}
    finally:
        conn.close()


def test_sweep_folder_prefers_largest(monkeypatch):
    conn = _sweep_db(monkeypatch, movies=[("AAA-100", "大片")])
    try:
        fake = _FakeSweepClient({
            400: [
                # same code twice: the low-res copy is listed first —
                # listing order must not decide which copy is kept
                {"n": "AAA-100 [HD].mp4", "fid": 401,
                 "s": 1500 * 1024 * 1024, "fc": "1"},
                {"n": "AAA-100 [4K].mp4", "fid": 402, "s": 10 * GIB, "fc": "1"},
            ],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 400, "AAA-100", 10, 20, 0, stats)
        # the LARGEST copy is kept and renamed with the library title
        assert fake.renames == [(402, "AAA-100 大片.mp4")]
        assert (402, 10) in fake.moves
        # the folder (smaller duplicate still inside) -> reject as one unit
        assert [f for f, t in fake.moves if t == 20] == [400]
        assert stats == {"organized": 1, "rejected": 1}
    finally:
        conn.close()


def test_sweep_folder_flat_multipart_kept_intact(monkeypatch):
    conn = _sweep_db(monkeypatch, movies=[("AAA-100", "大片")])
    try:
        fake = _FakeSweepClient({
            500: [
                # flat CD1/CD2 side by side: extract_code strips the tails,
                # so both files collapse to one code — they must stay intact
                {"n": "AAA-100 CD1.mp4", "fid": 501, "s": 4 * GIB, "fc": "1"},
                {"n": "AAA-100 CD2.mp4", "fid": 502, "s": 4 * GIB, "fc": "1"},
            ],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        p115._sweep_folder(conn, fake, 500, "AAA-100", 10, 20, 0, stats)
        # whole folder to target, nothing renamed or split apart
        assert fake.moves == [(500, 10)]
        assert fake.renames == []
        assert stats == {"organized": 1, "rejected": 0}
    finally:
        conn.close()


def test_sweep_existing_end_to_end(monkeypatch):
    conn = _sweep_db(monkeypatch, movies=[("mida-790", "下海")])  # lowercase row
    try:
        fake = _FakeSweepClient({
            900: [
                # library hit is case-insensitive (MIDA-790ch -> mida-790)
                {"n": "MIDA-790ch", "cid": 901, "fc": "0"},
                {"n": "垃圾合集", "cid": 902, "fc": "0"},
                {"n": "推广.txt", "fid": 903, "s": 88, "fc": "1"},
            ],
            901: [
                {"n": "MIDA-790.mp4", "fid": 911, "s": 2 * GIB, "fc": "1"},
                {"n": "宣传图.png", "fid": 912, "s": 4096, "fc": "1"},
            ],
            902: [{"n": "www.spam.com.url", "fid": 913, "s": 64, "fc": "1"}],
        })
        monkeypatch.setattr(p115, "get_client", lambda *a, **k: fake)
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        # resolve_dir now answers nested per-movie paths too
        table = {"待整理": 900, "已整理": 910, "冗余": 920,
                 "已整理/MIDA-790 下海": 930}
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: table[str(p)])
        stats = p115.sweep_existing(sleep_s=0)
        assert stats["errors"] == 0
        assert stats["organized"] == 1
        assert stats["skipped"] == 0
        # 901 (宣传图.png still inside) + junk folder 902 + loose txt:
        # folders travel as one unit, only the loose txt moves by itself
        assert stats["rejected"] == 3
        assert fake.renames == [(911, "MIDA-790 下海.mp4")]
        assert sorted(f for f, t in fake.moves if t == 920) == \
            [901, 902, 903]
        assert (911, 930) in fake.moves  # nested: 已整理/MIDA-790 下海/
        st = p115.sweep_status()
        assert st["running"] is False
        assert st["result"]["organized"] == 1
    finally:
        conn.close()


def test_sweep_existing_logs_start_and_finish(monkeypatch, caplog):
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({
            900: [{"n": "推广.txt", "fid": 903, "s": 88, "fc": "1"}],
        })
        monkeypatch.setattr(p115, "get_client", lambda *a, **k: fake)
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        table = {"待整理": 900, "已整理": 910, "冗余": 920}
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: table[str(p)])
        with caplog.at_level(logging.INFO, logger="seedmm.p115"):
            stats = p115.sweep_existing(sleep_s=0)
        assert stats["rejected"] == 1
        texts = [r.getMessage() for r in caplog.records]
        assert any(t.startswith("115 开始整理") for t in texts)
        assert any("115 整理完成" in t and "冗余 1" in t for t in texts)
    finally:
        conn.close()


def test_sweep_existing_empty_listing_logged(monkeypatch, caplog):
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({900: []})  # throttled: listing comes back empty
        monkeypatch.setattr(p115, "get_client", lambda *a, **k: fake)
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        table = {"待整理": 900, "已整理": 910, "冗余": 920}
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: table[str(p)])
        with caplog.at_level(logging.INFO, logger="seedmm.p115"):
            stats = p115.sweep_existing(sleep_s=0)
        assert stats["scanned"] == 0
        texts = [r.getMessage() for r in caplog.records]
        assert any("列表为空" in t for t in texts)
        assert any("115 整理完成" in t and "扫描 0" in t for t in texts)
    finally:
        conn.close()


def test_sweep_folder_nested_target(monkeypatch):
    conn = _sweep_db(monkeypatch, movies=[("MIDA-790", "下海")])
    try:
        fake = _FakeSweepClient({
            100: [{"n": "MIDA-790.mp4", "fid": 101, "s": 2 * GIB, "fc": "1"}],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        table = {"已整理": 10, "已整理/MIDA-790 下海": 55}
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: table[str(p)])
        stats = {"organized": 0, "rejected": 0, "skipped": 0}
        handled = p115._sweep_folder(conn, fake, 100, "MIDA-790ch", 10, 20,
                                     0, stats, target_path="已整理",
                                     prefer_name="MIDA-790 下海")
        assert handled is True
        # feature renamed and nested under 已整理/MIDA-790 下海/
        assert fake.renames == [(101, "MIDA-790 下海.mp4")]
        assert (101, 55) in fake.moves
        assert (100, 20) in fake.moves  # emptied shell -> reject
        assert stats == {"organized": 1, "rejected": 1, "skipped": 0}
    finally:
        conn.close()


def test_sweep_file_nested_target(monkeypatch):
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({})
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: 555)
        stats = {"organized": 0, "rejected": 0}
        handled = p115._sweep_file(conn, fake, 6, "MIDA-790.mp4", 2 * GIB,
                                   10, 20, stats, target_path="已整理",
                                   prefer_name="MIDA-790 下海")
        assert handled is True
        assert fake.renames == [(6, "MIDA-790 下海.mp4")]
        assert fake.moves == [(6, 555)]  # 已整理/MIDA-790 下海/
        assert stats["organized"] == 1
    finally:
        conn.close()


def test_sweep_folder_empty_listing_skipped(monkeypatch):
    conn = _sweep_db(monkeypatch)
    try:
        fake = _FakeSweepClient({})  # every listing comes back empty
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        handled = p115._sweep_folder(conn, fake, 400, "受限目录", 10, 20,
                                     0, stats)
        # throttled listing: no decision made, item retried on a later pass
        assert handled is False
        assert stats == {"organized": 0, "rejected": 0, "skipped": 1}
        assert fake.moves == []
    finally:
        conn.close()


def test_sweep_folder_subdirs_all_empty_skipped(monkeypatch):
    conn = _sweep_db(monkeypatch)
    try:
        # folder holding only subdirs whose listings came back empty
        fake = _FakeSweepClient({
            400: [{"n": "CD1", "cid": 401, "fc": "0"}],
            401: [],
        })
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        stats = {"organized": 0, "rejected": 0}
        handled = p115._sweep_folder(conn, fake, 400, "受限目录", 10, 20,
                                     0, stats)
        assert handled is False
        assert stats == {"organized": 0, "rejected": 0, "skipped": 1}
        assert fake.moves == []
    finally:
        conn.close()


# --------------------------------- manual/auto sweep trigger (fence) ----

def test_start_sweep_refuses_when_busy_or_logged_out(monkeypatch):
    saved = dict(p115._sweep_state)
    try:
        p115._sweep_state.update(running=True)
        assert p115.sweep_busy() is True
        assert p115.start_sweep() == (False, "整理已在进行中")
        p115._sweep_state.update(running=False)
        monkeypatch.setattr(p115, "has_auth", lambda: False)
        assert p115.start_sweep() == (False, "115 未登录")
        assert p115.sweep_busy() is False  # fence untouched on refusal
    finally:
        p115._sweep_state.clear()
        p115._sweep_state.update(saved)


def test_start_sweep_claims_fence_and_spawns(monkeypatch):
    saved = dict(p115._sweep_state)
    holder = {}

    class _Thread:  # stand-in for threading.Thread, never really starts
        def __init__(self, target=None, daemon=False):
            self.target, self.daemon, self.started = target, daemon, False
            holder["th"] = self

        def start(self):
            self.started = True

    try:
        monkeypatch.setattr(p115, "has_auth", lambda: True)
        calls = []
        monkeypatch.setattr(p115, "sweep_existing", lambda: calls.append(1))
        monkeypatch.setattr(p115, "threading", type(
            "threading", (), {"Thread": _Thread}))
        assert p115.start_sweep() == (True, "")
        th = holder["th"]
        assert th.started and th.daemon and th.target is not None
        assert p115.sweep_busy() is True  # fence held while the pass runs
        th.target()  # run the stubbed pass body
        assert calls == [1]
    finally:
        p115._sweep_state.clear()
        p115._sweep_state.update(saved)


# ------------------------------------------- real-time organize (tasks) ----

class _FakeOrgClient:
    """fs_file info for the finished task + sweep listings underneath."""

    def __init__(self, finfo, listings):
        self.finfo = finfo
        self.listings = listings
        self.moves = []
        self.renames = []

    def fs_file(self, fid, timeout=None):
        return {"state": True, "data": dict(self.finfo, fid=fid)}

    def fs_move(self, fid, cid, timeout=None):
        self.moves.append((int(fid), int(cid)))
        return {"state": True}

    def fs_rename(self, payload, timeout=None):
        fid, name = payload
        self.renames.append((int(fid), name))
        return {"state": True}


def test_organize_one_folder_delegates_and_marks_done(monkeypatch):
    conn = _sweep_db(monkeypatch, movies=[("AAA-100", "标题Y")])
    try:
        conn.execute("INSERT OR REPLACE INTO magnets(hash, code, name) "
                     "VALUES ('ih1', 'AAA-100', 'AAA-100.mp4')")
        conn.commit()
        # 150 MiB feature: above the 100 MiB organize floor, below the 1 GiB
        # backfill threshold — proves organize uses its own lower bar
        size = 150 * 1024 * 1024
        fake = _FakeOrgClient(
            {"file_name": "AAA-100ch", "fc": "0", "size": size},
            {700: [{"n": "AAA-100.mp4", "fid": 701, "s": size, "fc": "1"}]})
        monkeypatch.setattr(p115, "_pace_list", lambda sleep_s: None)
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        table = {"已整理": 10, "冗余": 20, "已整理/AAA-100 标题Y": 66}
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: table[str(p)])
        stats = {"organized": 0, "rejected": 0, "skipped": 0}
        p115._organize_one(conn, fake, {"file_id": 700, "name": "AAA-100ch"},
                           "ih1", stats)
        # nested per-movie dir: 已整理/AAA-100 标题Y/AAA-100 标题Y.mp4
        assert fake.renames == [(701, "AAA-100 标题Y.mp4")]
        assert (701, 66) in fake.moves
        assert (700, 20) in fake.moves  # emptied shell -> reject
        assert stats == {"organized": 1, "rejected": 1, "skipped": 0}
        done = conn.execute("SELECT value FROM meta WHERE key = "
                            "'p115:done:ih1'").fetchone()
        assert done == ("1",)
    finally:
        conn.close()


def test_organize_one_throttled_retries_without_done(monkeypatch):
    conn = _sweep_db(monkeypatch, movies=[("AAA-100", "标题Y")])
    try:
        conn.execute("INSERT OR REPLACE INTO magnets(hash, code, name) "
                     "VALUES ('ih2', 'AAA-100', 'AAA-100.mp4')")
        conn.commit()
        fake = _FakeOrgClient(
            {"file_name": "AAA-100ch", "fc": "0", "size": 150 * 1024 * 1024},
            {})  # every listing empty: 115 throttling
        monkeypatch.setattr(p115, "_pace_list", lambda sleep_s: None)
        monkeypatch.setattr(p115, "_fs_page", lambda c, cid, off, limit=100:
                            (fake.listings.get(cid, []), True))
        table = {"已整理": 10, "冗余": 20}
        monkeypatch.setattr(p115, "resolve_dir", lambda c, p: table[str(p)])
        stats = {"organized": 0, "rejected": 0}
        p115._organize_one(conn, fake, {"file_id": 700, "name": "AAA-100ch"},
                           "ih2", stats)
        assert stats == {"organized": 0, "rejected": 0, "skipped": 1}
        assert fake.moves == []  # nothing decided while throttled
        done = conn.execute("SELECT value FROM meta WHERE key = "
                            "'p115:done:ih2'").fetchone()
        assert done is None  # not marked done: picked up again next pass
    finally:
        conn.close()

"""115 集成的离线测试（无网络、无真实账号）。"""
from app import p115


def test_p115_installed():
    assert p115.HAS_P115 is True  # requirements include p115client


def test_devices_table():
    assert p115.DEVICES.get("android") == "安卓"
    assert "web" in p115.DEVICES


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

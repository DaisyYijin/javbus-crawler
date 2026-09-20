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

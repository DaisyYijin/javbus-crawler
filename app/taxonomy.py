"""Standard genre taxonomy.

Maps the various site tags (and magnet-name markers) onto a small set of
standard categories for the dashboard stats, in the spirit of
metatube-server's normalized metadata. Tags that match no rule keep their
original name, so nothing is ever lost.
"""

# (standard_name, [variant tags that fold into it])
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("高清画质", ("4K", "8K", "1080p", "高畫質", "高画質", "高清", "高品質",
                "フルハイビジョン(FHD)", "FHD", "60fps", "HD")),
    ("VR", ("VR専用", "VR專用", "ハイクオリティVR", "8KVR", "VR")),
    ("中文字幕", ("中文字幕", "中文", "字幕")),
    ("超长时长", ("4小時以上作品", "4時間以上")),
    ("角色扮演", ("角色扮演", "コスプレ", "Cosplay")),
    ("多人", ("多P", "4P", "3P", "乱交")),
    ("单体作品", ("單體作品", "単体作品", "DMM獨家")),
    ("企画", ("企畫", "企画")),
    ("素人", ("業餘", "业余", "素人")),
    ("人妻熟女", ("人妻", "熟女", "已婚婦女", "成熟的女人")),
    ("巨乳", ("巨乳", "爆乳")),
    ("美体", ("苗條", "苗条", "スレンダー", "美腿", "美臀", "美乳", "美白")),
    ("痴女", ("痴女", "ビッチ", "M女")),
    ("美少女", ("美少女", "ロリ")),
    ("OL", ("OL",)),
    ("辣妹", ("辣妹", "ギャル")),
]

_INDEX = {variant: std for std, variants in _RULES for variant in variants}


def normalize(tag: str) -> str:
    """Return the standard category for a raw tag (unchanged if unknown)."""
    t = tag.strip()
    return _INDEX.get(t, t)


def normalize_tags(tags: list[str]) -> list[str]:
    return [normalize(t) for t in tags]

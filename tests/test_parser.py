"""Parser tests: list pages, detail pages, magnet fragments."""
from app import parser


def test_parse_list_extracts_and_skips_empty_href():
    html = """
    <html><body>
    <a class="movie-box" href="https://x/BANK-248"><div class="photo-info"><span>T1</span></div></a>
    <a class="movie-box" href="https://x/ABC-001"><div class="photo-info"><span>T2</span></div></a>
    <a class="movie-box" href=""><div class="photo-info"><span>skip me</span></div></a>
    </body></html>"""
    items = parser.parse_list(html, "https://x")
    assert len(items) == 2
    assert items[0].code == "BANK-248"
    assert items[0].title == "T1"


def test_parse_detail_all_fields():
    html = """
    <html><body><div class="container"><h3> 標題 A </h3></div>
    <div class="bigImage"><img src="/pics/cover.jpg"></div>
    <div class="info">
    <p><span class="header">識別碼:</span> BANK-248</p>
    <p><span class="header">發行日期:</span> 2024-05-01</p>
    <p><span class="header">長度:</span> 120分鐘</p>
    <p><span class="header">導演:</span> 導演X</p>
    <p><span class="header">製作商:</span> 片商Y</p>
    <p><span class="header">發行商:</span> 發行Z</p>
    <p><span class="header">系列:</span> 系列W</p>
    </div>
    <a class="star-name"> 演員A </a><a class="star-name">演員B</a>
    <span class="genre"><label>巨乳</label></span><span class="genre"><label>4K</label></span>
    <div id="sample-waterfall"><a class="sample-box" href="/pics/s1.jpg"></a></div>
    </body></html>"""
    mv = parser.parse_detail(html, "BANK-248", "https://x/BANK-248")
    assert mv.title == "標題 A"
    assert mv.cover == "/pics/cover.jpg"
    assert mv.release_date == "2024-05-01"
    assert mv.duration == "120分鐘"
    assert mv.director == "導演X"
    assert mv.studio == "片商Y"
    assert mv.label == "發行Z"
    assert mv.series == "系列W"
    assert mv.actors == ["演員A", "演員B"]
    assert mv.genres == ["巨乳", "4K"]
    assert mv.samples == ["/pics/s1.jpg"]


def test_parse_detail_empty_page_is_blank_not_crash():
    mv = parser.parse_detail("<html><body></body></html>", "X-1", "https://x/X-1")
    assert mv.title == "" and mv.actors == [] and mv.genres == []


def test_parse_movie_script_vars():
    sv = parser.parse_movie_script_vars(
        "var gid = 69968188634; var uc = 0; var img = '/pics/cover/cj6t_b.jpg'; var lang = 'zh';")
    assert sv.get("gid") == "69968188634"
    assert sv.get("img") == "/pics/cover/cj6t_b.jpg"


def test_parse_magnets_dedupes_by_hash_and_reads_columns():
    html = """<table id="magnet-table">
    <tr><td><a href="magnet:?xt=urn:btih:AAAA1111BBBB">名1 4K</a></td><td>5.5GB</td><td>2024-01-01</td></tr>
    <tr><td><a href="magnet:?xt=urn:btih:aaaa1111bbbb">dup other case</a></td><td>x</td><td>y</td></tr>
    <tr><td><a href="magnet:?xt=urn:btih:CCCC2222">名2</a></td><td>2GB</td><td>2024-02-02</td></tr>
    <tr><td>no link here</td></tr>
    </table>"""
    mags = parser.parse_magnets(html)
    assert len(mags) == 2
    assert mags[0].name == "名1 4K"
    assert mags[0].size == "5.5GB"
    assert mags[0].date == "2024-01-01"


def test_parse_detail_genre_links_learned():
    html = """
    <html><body><div class="container"><h3>标题</h3></div>
    <span class="genre"><a href="https://x/genre/42"><label>巨乳</label></a></span>
    <span class="genre"><a href="https://x/uncensored/genre/hd"><label>高清</label></a></span>
    <span class="genre"><a href="https://x/other"><label>无ID类别</label></a></span>
    </body></html>"""
    mv = parser.parse_detail(html, "X-1", "https://x/X-1")
    assert mv.genres == ["巨乳", "高清", "无ID类别"]
    assert mv.genre_ids == {"巨乳": "42", "高清": "hd"}
    assert mv.genre_links == [("censored", "巨乳", "42"), ("uncensored", "高清", "hd")]


def test_magnet_hash_uppercases():
    assert parser.magnet_hash("magnet:?xt=urn:btih:AbCd1234&dn=x") == "ABCD1234"
    assert parser.magnet_hash("not a magnet link") is None

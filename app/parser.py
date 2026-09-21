"""HTML parsers for list pages, movie detail pages and magnet fragments."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup


# ---------------------------------------------------------------- models --
@dataclass
class ListItem:
    code: str          # movie id, e.g. "BANK-248"
    url: str           # absolute detail page URL
    title: str = ""


@dataclass
class Magnet:
    link: str          # full magnet:?xt=... URL
    name: str = ""
    size: str = ""
    date: str = ""


@dataclass
class Movie:
    code: str
    url: str
    title: str = ""
    cover: str = ""
    release_date: str = ""
    duration: str = ""
    director: str = ""
    studio: str = ""
    label: str = ""
    series: str = ""
    actors: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    genre_ids: dict[str, str] = field(default_factory=dict)  # name -> site genre slug/id
    genre_links: list[tuple[str, str, str]] = field(default_factory=list)  # (channel, name, id)
    samples: list[str] = field(default_factory=list)
    magnets: list[Magnet] = field(default_factory=list)
    matched_tags: list[str] = field(default_factory=list)  # tag-filter hits
    category: str = ""                                     # censored | uncensored


# ---------------------------------------------------------------- helpers --
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:([0-9A-Za-z]+)", re.IGNORECASE)

# Detail pages define the magnet AJAX params in an inline script:
#   var gid = 69968188634; var uc = 0; var img = '/pics/cover/cj6t_b.jpg';
_SCRIPT_VAR_RE = re.compile(
    r"var\s+(gid|uc|img|lang)\s*=\s*([0-9]+|'[^']*'|\"[^\"]*\")\s*;"
)


def parse_movie_script_vars(html: str) -> dict[str, str]:
    """Extract gid/uc/img/lang from the detail page's inline script block."""
    found: dict[str, str] = {}
    for m in _SCRIPT_VAR_RE.finditer(html):
        found[m.group(1)] = m.group(2).strip("'\"")
    return found


def magnet_hash(link: str) -> str | None:
    m = _MAGNET_RE.search(link)
    return m.group(1).upper() if m else None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------- parsers --
def parse_list(html: str, base_url: str) -> list[ListItem]:
    """Extract movie links from a /page/N listing."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[ListItem] = []
    for box in soup.select("a.movie-box"):
        href = box.get("href") or ""
        if not href:
            continue
        code = href.rstrip("/").rsplit("/", 1)[-1]
        title_el = box.select_one(".photo-info span")
        items.append(ListItem(code=code, url=href,
                              title=_clean(title_el.get_text()) if title_el else ""))
    return items


# Detail page: <p><span class="header">識別碼:</span> value</p> style rows.
_FIELD_MAP = {
    "識別碼": "code",
    "發行日期": "release_date",
    "長度": "duration",
    "導演": "director",
    "製作商": "studio",
    "發行商": "label",
    "系列": "series",
}


def parse_detail(html: str, code: str, url: str) -> Movie:
    movie = Movie(code=code, url=url)
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one(".container h3") or soup.select_one("h3")
    movie.title = _clean(title_el.get_text()) if title_el else ""

    img = soup.select_one(".bigImage img")
    if img and img.get("src"):
        movie.cover = img["src"]

    for p in soup.select(".info p"):
        header = p.select_one(".header")
        if not header:
            continue
        key = _clean(header.get_text()).rstrip(":")
        value = _clean(p.get_text().replace(header.get_text(), "", 1))
        attr = _FIELD_MAP.get(key)
        if attr and value:
            setattr(movie, attr, value)

    movie.actors = [
        _clean(a.get_text())
        for a in soup.select(".avatar-box .star-name, a.star-name")
        if _clean(a.get_text())
    ]
    movie.genres = []
    for a in soup.select("span.genre a[href]"):
        href = a.get("href", "")
        label = a.select_one("label") or a
        name = _clean(label.get_text())
        m = re.search(r"/genre/([A-Za-z0-9_-]+)", href)
        if name and m:
            cat = "uncensored" if "/uncensored/genre/" in href else "censored"
            movie.genres.append(name)
            movie.genre_ids[name] = m.group(1)
            movie.genre_links.append((cat, name, m.group(1)))
        elif name:
            movie.genres.append(name)
    if not movie.genres:  # older markup: plain labels without links
        movie.genres = [
            _clean(g.get_text())
            for g in soup.select("span.genre label")
            if _clean(g.get_text())
        ]
    movie.samples = [
        a["href"] for a in soup.select("#sample-waterfall a.sample-box[href]")
    ]
    return movie


def parse_genre_catalog(html: str) -> list[dict]:
    """Parse a site /genre index page into [{"group": 大标题, "genres": [{"name", "id"}]}].

    Structure (verified against seedmm.bond): <h4>主題</h4> followed by
    <div class="row genre-box"> whose <a> children link to /genre/{id}.
    Footer headings (聯絡我們) are skipped.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    current: dict | None = None
    for el in soup.select("h4, div.genre-box a[href]"):
        if el.name == "h4":
            title = _clean(el.get_text())
            if title and title not in ("聯絡我們", "联系我们"):
                current = {"group": title, "genres": []}
                out.append(current)
        elif current is not None:
            m = re.search(r"/genre/([A-Za-z0-9_-]+)", el.get("href", ""))
            name = _clean(el.get_text())
            if name and m:
                current["genres"].append({"name": name, "id": m.group(1)})
    return [g for g in out if g["genres"]]


def parse_magnets(html: str) -> list[Magnet]:
    """Parse magnet rows from an HTML fragment (AJAX response or detail page)."""
    soup = BeautifulSoup(html, "html.parser")
    magnets: list[Magnet] = []
    rows = soup.select("#magnet-table tr") or soup.select("tr")
    seen: set[str] = set()
    for row in rows:
        link_el = row.select_one("a[href^='magnet:']")
        if not link_el:
            continue
        link = link_el["href"].strip()
        h = magnet_hash(link)
        if not h or h in seen:
            continue
        seen.add(h)
        tds = row.select("td")
        name = _clean(tds[0].get_text(" ")) if tds else _clean(link_el.get_text())
        magnets.append(Magnet(
            link=link,
            name=name or h,
            size=_clean(tds[1].get_text()) if len(tds) > 1 else "",
            date=_clean(tds[2].get_text()) if len(tds) > 2 else "",
        ))
    return magnets

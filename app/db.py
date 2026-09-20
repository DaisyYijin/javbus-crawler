"""SQLite persistence: schema creation and idempotent upserts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .parser import Magnet, Movie, magnet_hash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    code         TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    cover        TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    duration     TEXT NOT NULL DEFAULT '',
    director     TEXT NOT NULL DEFAULT '',
    studio       TEXT NOT NULL DEFAULT '',
    label        TEXT NOT NULL DEFAULT '',
    series       TEXT NOT NULL DEFAULT '',
    actors       TEXT NOT NULL DEFAULT '[]',
    genres       TEXT NOT NULL DEFAULT '[]',
    samples      TEXT NOT NULL DEFAULT '[]',
    magnet_count INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS magnets (
    hash     TEXT PRIMARY KEY,
    code     TEXT NOT NULL REFERENCES movies(code) ON DELETE CASCADE,
    name     TEXT NOT NULL DEFAULT '',
    size     TEXT NOT NULL DEFAULT '',
    date     TEXT NOT NULL DEFAULT '',
    link     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_magnets_code ON magnets(code);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    # --- lightweight migrations (older databases) ---
    cols = {r[1] for r in conn.execute("PRAGMA table_info(movies)")}
    if "matched_tags" not in cols:
        conn.execute("ALTER TABLE movies ADD COLUMN matched_tags TEXT NOT NULL DEFAULT '[]'")
    if "matched_count" not in cols:
        conn.execute("ALTER TABLE movies ADD COLUMN matched_count INTEGER NOT NULL DEFAULT 0")
    if "category" not in cols:
        conn.execute("ALTER TABLE movies ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    # secondary indexes for the common sort/filter paths (after migrations so
    # the indexed columns are guaranteed to exist even on old databases)
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_movies_category ON movies(category);
    CREATE INDEX IF NOT EXISTS idx_movies_first_seen ON movies(first_seen);
    CREATE INDEX IF NOT EXISTS idx_movies_magnet_count ON movies(magnet_count);
    CREATE INDEX IF NOT EXISTS idx_movies_matched_count ON movies(matched_count);
    CREATE INDEX IF NOT EXISTS idx_movies_release_date ON movies(release_date);
    """)
    conn.commit()
    return conn


def upsert_movie(conn: sqlite3.Connection, movie: Movie) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO movies (code, url, title, cover, release_date, duration,
                            director, studio, label, series, actors, genres,
                            samples, magnet_count, matched_tags, matched_count,
                            category, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            url=excluded.url, title=excluded.title, cover=excluded.cover,
            release_date=excluded.release_date, duration=excluded.duration,
            director=excluded.director, studio=excluded.studio,
            label=excluded.label, series=excluded.series,
            actors=excluded.actors, genres=excluded.genres,
            samples=excluded.samples, magnet_count=excluded.magnet_count,
            matched_tags=excluded.matched_tags,
            matched_count=excluded.matched_count,
            category=excluded.category,
            last_seen=excluded.last_seen
        """,
        (
            movie.code, movie.url, movie.title, movie.cover,
            movie.release_date, movie.duration, movie.director, movie.studio,
            movie.label, movie.series,
            json.dumps(movie.actors, ensure_ascii=False),
            json.dumps(movie.genres, ensure_ascii=False),
            json.dumps(movie.samples, ensure_ascii=False),
            len(movie.magnets),
            json.dumps(movie.matched_tags, ensure_ascii=False),
            len(movie.matched_tags),
            movie.category,
            now, now,
        ),
    )


def insert_magnets(conn: sqlite3.Connection, magnets: list[Magnet], code: str) -> int:
    """Insert new magnet rows, refreshing existing ones. Returns the count of
    genuinely NEW hashes (updates don't inflate the per-run stats)."""
    n = 0
    for m in magnets:
        h = magnet_hash(m.link)
        if not h:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO magnets (hash, code, name, size, date, link) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (h, code, m.name, m.size, m.date, m.link),
        )
        if cur.rowcount:
            n += 1
        else:
            conn.execute(
                "UPDATE magnets SET name = ?, size = ?, date = ? WHERE hash = ?",
                (m.name, m.size, m.date, h),
            )
    return n


def delete_magnets(conn: sqlite3.Connection, code: str) -> int:
    """Remove all magnet rows for a movie (refresh drops links vanished from the site)."""
    cur = conn.execute("DELETE FROM magnets WHERE code = ?", (code,))
    return cur.rowcount


def known_codes(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT code FROM movies")}


# ------------------------------------------------------------ web queries --
_MOVIE_COLUMNS = "code, title, cover, release_date, duration, studio, label, series, magnet_count, matched_tags, matched_count, first_seen, category"


def list_movies(conn: sqlite3.Connection, q: str = "", page: int = 1, size: int = 20,
                sort: str = "new", category: str = "", actor: str = "", studio: str = "",
                label: str = "", series: str = "", genre: str = "", director: str = "",
                date_from: str = "", date_to: str = ""):
    """Paged movie list; sort: new (default) | match | magnets.

    Filters: free-text q; category (comma-separated); exact-name
    actor/studio/label/series/director/genre (actor & genre match the stored
    JSON arrays via a quoted substring); release_date range (ISO dates).
    """
    size = max(1, min(size, 100))
    clauses, params = [], []
    if q:
        like = f"%{q}%"
        clauses.append("(code LIKE ? OR title LIKE ? OR actors LIKE ?)")
        params += [like, like, like]
    cats = [c for c in str(category or "").split(",") if c in ("censored", "uncensored")]
    if cats:
        clauses.append("category IN (%s)" % ",".join("?" * len(cats)))
        params.extend(cats)
    if actor:
        clauses.append("actors LIKE ?")
        params.append(f'%"{actor.replace(chr(34), "")}"%')
    if genre:
        clauses.append("genres LIKE ?")
        params.append(f'%"{genre.replace(chr(34), "")}"%')
    for col, val in (("studio", studio), ("label", label),
                     ("series", series), ("director", director)):
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if date_from:
        clauses.append("release_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("release_date <= ?")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = conn.execute(f"SELECT COUNT(*) FROM movies {where}", params).fetchone()[0]
    offset = (max(1, page) - 1) * size
    order = {
        "match": "matched_count DESC, first_seen DESC, code DESC",
        "magnets": "magnet_count DESC, first_seen DESC, code DESC",
    }.get(sort, "first_seen DESC, code DESC")
    rows = conn.execute(
        f"SELECT {_MOVIE_COLUMNS} FROM movies {where} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        params + [size, offset],
    ).fetchall()
    cols = _MOVIE_COLUMNS.split(", ")
    return total, [dict(zip(cols, r)) for r in rows]


def delete_movie(conn: sqlite3.Connection, code: str) -> bool:
    """Delete one movie and its magnets. Returns False when the code was unknown."""
    conn.execute("DELETE FROM magnets WHERE code = ?", (code,))
    cur = conn.execute("DELETE FROM movies WHERE code = ?", (code,))
    conn.commit()
    return cur.rowcount > 0


def browse_counts(conn: sqlite3.Connection, kind: str, q: str = "", limit: int = 200) -> list[dict]:
    """Aggregate movie counts per name for a browse dimension.

    kind: actor (JSON array, counted in Python) | studio | label | series |
    director (plain columns via GROUP BY). `q` filters names by substring.
    """
    from collections import Counter

    q = q.strip()
    if kind == "actor":
        counter: Counter = Counter()
        for (raw,) in conn.execute("SELECT actors FROM movies"):
            try:
                counter.update(json.loads(raw or "[]"))
            except (TypeError, ValueError):
                continue
        pairs = [(n, c) for n, c in counter.items() if n and (not q or q in n)]
    else:
        col = {"studio": "studio", "label": "label", "series": "series",
               "director": "director"}[kind]
        pairs = [(n, c) for n, c in conn.execute(
            f"SELECT {col}, COUNT(*) FROM movies WHERE {col} != '' GROUP BY {col}"
        ) if not q or q in n]
    pairs.sort(key=lambda x: (-x[1], x[0]))
    return [{"name": n, "count": c} for n, c in pairs[:limit]]


# Known quality markers for the keyword quick-add chips. Counts come from the
# actual magnet names in the library, so the chips always reflect what the
# site really uses (SQLite LIKE is ASCII case-insensitive already).
_FILTER_MARKERS = ("字幕", "中字", "高清", "无码", "破解",
                   "4K", "8K", "1080p", "720p", "60fps", "VR", "HD",
                   "-U", "-UC", "-C", "AI")


def filter_chips(conn: sqlite3.Connection) -> list[dict]:
    """Count library magnets whose name contains each known marker."""
    out = []
    for marker in _FILTER_MARKERS:
        n = conn.execute(
            "SELECT COUNT(*) FROM magnets WHERE name LIKE ?", (f"%{marker}%",)
        ).fetchone()[0]
        if n:
            out.append({"kw": marker, "count": n})
    out.sort(key=lambda x: (-x["count"], x["kw"]))
    return out


def recompute_matched(conn: sqlite3.Connection, keywords: list[str]) -> int:
    """Recompute matched_tags/matched_count for every movie. Returns updated count."""
    from .crawler import compute_matched

    n = 0
    for code, genres, in conn.execute("SELECT code, genres FROM movies").fetchall():
        try:
            tags = json.loads(genres or "[]")
        except (TypeError, ValueError):
            tags = []
        names = [r[0] for r in conn.execute(
            "SELECT name FROM magnets WHERE code = ?", (code,))]
        matched = compute_matched(tags, names, keywords)
        conn.execute(
            "UPDATE movies SET matched_tags = ?, matched_count = ? WHERE code = ?",
            (json.dumps(matched, ensure_ascii=False), len(matched), code),
        )
        n += 1
    conn.commit()
    return n


def get_movie(conn: sqlite3.Connection, code: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_MOVIE_COLUMNS}, url, director, actors, genres, samples, last_seen "
        "FROM movies WHERE code = ?", (code,)
    ).fetchone()
    if not row:
        return None
    cols = (_MOVIE_COLUMNS + ", url, director, actors, genres, samples, last_seen").split(", ")
    movie = dict(zip(cols, row))
    for key in ("actors", "genres", "samples"):
        try:
            movie[key] = json.loads(movie[key])
        except (TypeError, ValueError):
            movie[key] = []
    movie["magnets"] = [
        {"hash": r[0], "name": r[1], "size": r[2], "date": r[3], "link": r[4]}
        for r in conn.execute(
            "SELECT hash, name, size, date, link FROM magnets WHERE code = ? ORDER BY date DESC",
            (code,),
        )
    ]
    return movie


def library_stats(conn: sqlite3.Connection, category: str = "") -> dict:
    """Counts per category + top genre tags for the given channel ('' = all)."""
    cats = [c for c in str(category or "").split(",") if c in ("censored", "uncensored")]
    where, params = "", []
    if cats:
        where = "WHERE category IN (%s)" % ",".join("?" * len(cats))
        params = cats

    by_cat = {"censored": 0, "uncensored": 0}
    for cat, n in conn.execute(
            "SELECT category, COUNT(*) FROM movies GROUP BY category"):
        by_cat[cat if cat in by_cat else "unknown"] = n

    total = conn.execute(f"SELECT COUNT(*) FROM movies {where}", params).fetchone()[0]
    magnets = conn.execute(
        f"SELECT COALESCE(SUM(magnet_count),0) FROM movies {where}", params
    ).fetchone()[0]

    genre_rows = conn.execute(
        f"SELECT genres FROM movies {where}", params).fetchall()
    from collections import Counter

    from . import taxonomy

    counter: Counter = Counter()
    for (g,) in genre_rows:
        try:
            # aggregate by the normalized (standard) category name
            counter.update(taxonomy.normalize_tags(json.loads(g or "[]")))
        except (TypeError, ValueError):
            pass
    top_genres = [{"tag": t, "count": n} for t, n in counter.most_common()]
    return {
        "total": total,
        "magnets": magnets,
        "by_category": by_cat,
        "top_genres": top_genres,
    }

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
    conn.commit()
    return conn


def upsert_movie(conn: sqlite3.Connection, movie: Movie) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO movies (code, url, title, cover, release_date, duration,
                            director, studio, label, series, actors, genres,
                            samples, magnet_count, matched_tags, matched_count,
                            first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            url=excluded.url, title=excluded.title, cover=excluded.cover,
            release_date=excluded.release_date, duration=excluded.duration,
            director=excluded.director, studio=excluded.studio,
            label=excluded.label, series=excluded.series,
            actors=excluded.actors, genres=excluded.genres,
            samples=excluded.samples, magnet_count=excluded.magnet_count,
            matched_tags=excluded.matched_tags,
            matched_count=excluded.matched_count,
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
            now, now,
        ),
    )


def insert_magnets(conn: sqlite3.Connection, magnets: list[Magnet], code: str) -> int:
    n = 0
    for m in magnets:
        h = magnet_hash(m.link)
        if not h:
            continue
        cur = conn.execute(
            """
            INSERT INTO magnets (hash, code, name, size, date, link)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                name=excluded.name, size=excluded.size, date=excluded.date
            """,
            (h, code, m.name, m.size, m.date, m.link),
        )
        n += cur.rowcount if cur.rowcount > 0 else 0
    return n


def known_codes(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT code FROM movies")}


# ------------------------------------------------------------ web queries --
_MOVIE_COLUMNS = "code, title, cover, release_date, duration, studio, label, series, magnet_count, matched_tags, matched_count, first_seen"


def list_movies(conn: sqlite3.Connection, q: str = "", page: int = 1, size: int = 20,
                sort: str = "new"):
    """Paged movie list; sort: new (default) | match | magnets."""
    size = max(1, min(size, 100))
    where, params = "", []
    if q:
        where = "WHERE code LIKE ? OR title LIKE ? OR actors LIKE ?"
        like = f"%{q}%"
        params = [like, like, like]
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

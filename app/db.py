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
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def upsert_movie(conn: sqlite3.Connection, movie: Movie) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO movies (code, url, title, cover, release_date, duration,
                            director, studio, label, series, actors, genres,
                            samples, magnet_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            url=excluded.url, title=excluded.title, cover=excluded.cover,
            release_date=excluded.release_date, duration=excluded.duration,
            director=excluded.director, studio=excluded.studio,
            label=excluded.label, series=excluded.series,
            actors=excluded.actors, genres=excluded.genres,
            samples=excluded.samples, magnet_count=excluded.magnet_count,
            last_seen=excluded.last_seen
        """,
        (
            movie.code, movie.url, movie.title, movie.cover,
            movie.release_date, movie.duration, movie.director, movie.studio,
            movie.label, movie.series,
            json.dumps(movie.actors, ensure_ascii=False),
            json.dumps(movie.genres, ensure_ascii=False),
            json.dumps(movie.samples, ensure_ascii=False),
            len(movie.magnets), now, now,
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

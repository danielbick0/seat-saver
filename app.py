#!/usr/bin/env python3
"""Seat Saver (שומר מקומות) — pilot app for saving lecture seats.

In plain words: students run between classrooms and want a good seat.
When someone arrives early, they offer to save up to 5 seats. Everyone
who follows that course gets an automatic phone notification (Web Push).
A follower can spend 1 coin to ask for a seat. The coin is frozen on
request and moves to the saver only when the requester arrives and
confirms "I arrived, seat was saved". If the requester does not arrive
in time, or the seat was not there, the coin goes back.

Stack, in one sentence Daniel can repeat: Python standard library
(http.server + SQLite) for the server and the rules, plus a tiny bit of
JavaScript only for Web Push notifications — a Service Worker that shows
the notification and a small script that asks permission and registers
the phone. Passwords are hashed with hashlib.pbkdf2_hmac (stdlib, no
new library). One justified external library, pywebpush, only for the
Web Push encryption (see EXPLANATION.md for why).

Run:  python3 app.py   (then open http://localhost:8000)
Tests: python3 -m unittest test_app -v
Push needs HTTPS + pywebpush — see README.md.
"""

import base64
import functools
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- rules
START_BALANCE = 3
MIN_BALANCE = -2          # floor: a balance may never go below -2
REQUEST_COST = 1
MAX_SEATS_PER_OFFER = 5
OFFER_WINDOW_MINUTES = 60   # offers open 60 minutes before class
# TEMPORARY — test aid, remove after pilot. The test-push button may
# send at most one test push per user per 60 seconds.
TEST_PUSH_COOLDOWN_SECONDS = 60
# Round 20, item 12: an admin broadcast is at most one per hour,
# so it cannot be spammed — a broadcast reaches everyone, so the
# limit is the whole point.
BROADCAST_COOLDOWN_SECONDS = 3600
CONFIRM_GRACE_MINUTES = 5   # requester has until start + 5 min to arrive
LATE_GRACE_MINUTES = 10   # declared late: until start + 10 min to arrive
REMINDER_MINUTES = 5        # reminder goes out 5 minutes before class
EARLY_REMINDER_MINUTES = 30  # "leave now" reminder, 30 minutes before class
SAVER_REMINDER_MINUTES = 10  # saver reminder, 10 minutes before class
MAX_REQUEST_SEATS = 2       # one request can ask for 1 or 2 seats
MIN_PASSWORD_LEN = 4        # pilot-simple; documented in EXPLANATION.md
SESSION_DAYS = 30           # a login stays valid for 30 days
PBKDF2_ITERATIONS = 200_000
# One fixed colour per course, so a course is recognisable at a
# glance on offer cards, follows and filters. No new table, no
# migration: the colour is a pure function of the course id — the
# palette index is course_id modulo the palette length. Two courses
# share a colour only when there are more courses than colours, and
# the colour is only a visual aid, never the sole identifier (the
# course name is always next to it).
COURSE_COLORS = ["#14532d", "#1d4ed8", "#b45309", "#be185d",
                 "#6d28d9", "#0e7490", "#b91c1c", "#4d7c0f"]
WEEKDAY_NAMES = ["שני", "שלישי", "רביעי", "חמישי",
                 "שישי", "שבת", "ראשון"]
# Display order: the week starts on Sunday (Israel), stored
# values stay Python 0 = Monday. (Daniel, 2026-11-10: the week
# starts on Sunday — the Monday-first change was my misreading
# of his report that the week was starting on Monday.)
DISPLAY_WEEKDAYS = [6, 0, 1, 2, 3, 4, 5]
# Human-readable status labels (DB stores the English codes).
REQUEST_STATUS_HE = {
    "frozen": "מוקפא",
    "confirmed": "אושר",
    "no_show": "לא הגיע",
    "not_saved": "המקום לא היה שם",
    "cancelled": "בוטל",
    "cancelled_by_requester": "בוטלה על ידי המבקש",
    "cancelled_by_saver": "בוטלה על ידי השומר",
    "refunded_by_admin": "הוחזר לאחר בדיקה",
    "late_declined": "האיחור נדחה",
}
# What each decided request MEANT for the coin, shown in the
# closed-offer history on /my: the status label alone does not say
# whether the coin moved to the saver or came back.
REQUEST_OUTCOME_HE = {
    "frozen": "מוקפא — מחכה להכרעה",
    "confirmed": "אושר — המטבע עבר לשומר",
    "no_show": "לא הגיע — המטבע חזר",
    "not_saved": "המקום לא היה שם — המטבע חזר",
    "cancelled": "בוטלה — המטבע חזר",
    "cancelled_by_requester": "בוטלה על ידי המבקש — המטבע חזר",
    "cancelled_by_saver": "בוטלה על ידי השומר — המטבע חזר",
    "refunded_by_admin": "הוחזר לאחר בדיקה",
    "late_declined": "האיחור נדחה — המטבע חזר",
}
OFFER_STATUS_HE = {
    "open": "פתוחה",
    "expired": "פגה",
    "cancelled": "בוטלה",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "seat_saver.db")
VAPID_KEYS_PATH = os.path.join(BASE_DIR, "vapid_keys.json")

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    balance INTEGER NOT NULL DEFAULT 3,
    sample INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    password_hash TEXT,
    -- Round 20, item 13: the phone number, mandatory at
    -- registration. Private: shown only to the matched pair (see
    -- the "your saved seat" card) and to the admin — never on a
    -- public list or offer card. Stored normalized (05XXXXXXXX).
    phone TEXT,
    -- Round 20, item 15: 1 when the admin confirmed this is a
    -- real person he knows (checked the name + phone against his
    -- own contacts). Only the admin sets it. A changed name or
    -- phone resets it to 0 — the old approval no longer applies.
    verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'lecture'
);
CREATE TABLE IF NOT EXISTS course_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    weekday INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    building TEXT NOT NULL DEFAULT '',
    room TEXT NOT NULL DEFAULT '',
    teacher TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS follows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    course_id INTEGER NOT NULL REFERENCES courses(id),
    session_id INTEGER REFERENCES course_sessions(id),
    weekday INTEGER,
    start_time TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, course_id, session_id)
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    session_id INTEGER REFERENCES course_sessions(id),
    saver_id INTEGER NOT NULL REFERENCES users(id),
    class_date TEXT NOT NULL,
    zone TEXT,
    note TEXT NOT NULL DEFAULT '',
    seats_total INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    requester_id INTEGER NOT NULL REFERENCES users(id),
    saver_id INTEGER NOT NULL REFERENCES users(id),
    seats_requested INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'frozen',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    on_the_way TEXT,
    reminded_at TEXT,
    reminded_30_at TEXT,
    reminded_arrival_at TEXT,
    late_declared_at TEXT,
    late_status TEXT
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    -- Round 14, item 8: which device/browser this subscription
    -- belongs to (see classify_user_agent). A subscription lives
    -- in one browser on one device, so the user can see on /push
    -- where their notifications actually go.
    device TEXT NOT NULL DEFAULT 'unknown',
    browser TEXT NOT NULL DEFAULT 'unknown',
    standalone INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
-- TEMPORARY — test aid, remove after pilot. One row per user: when
-- they last sent a test push, so the button is at most one per minute.
CREATE TABLE IF NOT EXISTS test_push_last (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coin_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    request_id INTEGER REFERENCES requests(id),
    created_at TEXT NOT NULL
);
-- Round 20, item 14: complaints. A requester says the seat was not
-- actually saved — while the coin is frozen, or after the request
-- resolved. One complaint per request (UNIQUE), so a requester
-- cannot stack complaints on the same seat. The admin reviews each
-- one: 'open' (waiting), 'upheld' (the saver's reliability drops)
-- or 'dismissed' (no drop). A false complaint cannot hurt a saver
-- without the admin seeing it first.
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    requester_id INTEGER NOT NULL REFERENCES users(id),
    saver_id INTEGER NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    admin_note TEXT NOT NULL DEFAULT '',
    resolved_at TEXT,
    UNIQUE(request_id)
);
-- Round 20, item 12: the last admin broadcast, one row per admin,
-- so a broadcast is at most one per hour (see send_broadcast).
-- The broadcast itself is logged per recipient in
-- notification_log, like every other push.
CREATE TABLE IF NOT EXISTS broadcast_last (
    admin_id INTEGER PRIMARY KEY REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES course_sessions(id),
    zone TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    seats INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, session_id)
);
-- UNUSED since round 13: weekly automatic offers were removed
-- (nobody can promise every week to arrive on time). The table
-- stays in SCHEMA so old databases keep working and the seed
-- cleanup can still read it — nothing writes to it any more.
-- Standing weekly requests: a user says "request a seat for this
-- session every week", and the background thread requests one seat
-- when an open offer for the session exists (see
-- auto_request_tick). One per user + session. The row persists
-- week to week — it is not deleted when a request resolves; only
-- the owner deletes it.
CREATE TABLE IF NOT EXISTS auto_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES course_sessions(id),
    created_at TEXT NOT NULL,
    UNIQUE(user_id, session_id)
);
CREATE TABLE IF NOT EXISTS waiting_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(offer_id, user_id)
);
-- The request board ("מחפש מקום"): demand before any offer exists.
-- A user posts "I need a seat" for a session; when a saver offers
-- that session, the poster gets a push. One OPEN request per user
-- per session — the partial index below is what enforces it, because
-- a plain UNIQUE would also block re-posting after an old request
-- was fulfilled or expired. The board never reserves anything and
-- never touches coins; it only decides who gets told first.
CREATE TABLE IF NOT EXISTS seat_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES course_sessions(id),
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_seat_requests_open
    ON seat_requests(user_id, session_id) WHERE status = 'open';
-- Saved offer templates: one per user per session. A template is a
-- saved note/seats for a recurring session, so next week the
-- saver offers in one tap instead of filling the form. Templates
-- never create offers by themselves — a template only prefills
-- the offer form, and the saver still taps once to offer.
CREATE TABLE IF NOT EXISTS offer_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES course_sessions(id),
    zone TEXT,
    note TEXT NOT NULL DEFAULT '',
    seats INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, session_id)
);
"""

# Daniel's real courses/sessions, from his BGU course pages
# (screenshots 2026-11-08). Weekday uses Python's convention: 0=Monday,
# ..., 6=Sunday. All sessions are weekly from week 1.
# NOTE: Daniel wrote "Beitan is Calculus 2", but the screenshots show
# Beitan as a תכנות מערכות tutorial teacher and Luzon as the
# חדו״א 2 tutorial teacher — the screenshots are authoritative here.
SEED_COURSES = [
    ("תכנון אלגוריתמים", "lecture", [
        # Lecture group 1, Dr. S. Ben Daniel
        (6, "18:00", "20:00", "35", "1", "ד״ר בן דניאל"),     # Sun lecture
        (2, "16:00", "18:00", "92", "002", "ד״ר בן דניאל"),   # Wed lecture
        # Lecture group 3, Dr. S. Ben Daniel
        (6, "16:00", "18:00", "38", "010", "ד״ר בן דניאל"),   # Sun lecture
        (2, "18:00", "20:00", "34", "10", "ד״ר בן דניאל"),    # Wed lecture
        # Tutorial group 31, Mr. L. Portal
        (6, "18:00", "20:00", "34", "14", "מר פורטל"),    # Sun tutorial
        # Tutorial group 21, Mr. L. Portal
        (6, "16:00", "18:00", "35", "213", "מר פורטל"),   # Sun tutorial
        # Tutorial group 22, Mr. M. Levi
        (0, "10:00", "12:00", "90", "222", "מר לוי"),   # Mon tutorial
    ]),
    ("חדו״א 2", "lecture", [
        # Lecture group 2, Dr. G. Landsman
        (0, "14:00", "16:00", "32", "307", "ד״ר לנדסמן"),   # Mon lecture
        (2, "08:00", "10:00", "35", "2", "ד״ר לנדסמן"),     # Wed lecture
        # Tutorial group 21, Mr. L. Luzon
        (0, "12:00", "14:00", "90", "223", "מר לוזון"),   # Mon tutorial
        # Tutorial group 22, Mr. L. Luzon
        (1, "10:00", "12:00", "90", "224", "מר לוזון"),   # Tue tutorial
    ]),
    ("תכנות מערכות", "lecture", [
        # Lecture group 1, Dr. M. Adler
        (6, "10:00", "12:00", "32", "306", "ד״ר אדלר"),   # Sun lecture
        (0, "14:00", "16:00", "35", "2", "ד״ר אדלר"),     # Mon lecture
        # Lecture group 2, Dr. M. Adler
        (3, "12:00", "14:00", "34", "2", "ד״ר אדלר"),     # Thu lecture
        (6, "12:00", "14:00", "35", "2", "ד״ר אדלר"),     # Sun lecture
        # Tutorial group 11, Mr. A. Beitan
        (1, "10:00", "12:00", "28", "104", "מר ביתן"),   # Tue tutorial
        # Tutorial group 12, Mr. D. Netai
        (1, "14:00", "16:00", "90", "234", "מר נתאי"),   # Tue tutorial
        # Tutorial group 13, Mr. A. Beitan
        (0, "10:00", "12:00", "90", "234", "מר ביתן"),   # Mon tutorial
    ]),
]

# Test users only — the pilot is open registration, anyone with the link
# can register. These five exist so the flow can be tried immediately.
# They have NO password yet: on first login each one sets a password once
# (see set_password / the /set-password page).
SEED_USERS = ["Daniel", "Lev", "Levi", "Shaked", "Yehonatan"]


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The background thread and the HTTP server write to the same DB;
    # wait up to 5 seconds for a lock instead of failing at once.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _remove_seed_test_users(conn):
    """One-time cleanup of the 5 seeded test users (Daniel, Lev,
    Levi, Shaked, Yehonatan) on a database that has moved to real
    registration. A fresh database keeps its seed users: they are
    the only users, and the pilot needs at least one follower per
    course until real people register — so the cleanup runs only
    once a real user exists (someone with a password). It removes
    only a user who (a) never set a password, (b) has no activity
    at all — no push subscriptions, no requests (as requester or
    saver), no offers, no coin-ledger rows, no board posts, no
    waiting-list entries, no auto-offers, no auto-requests, no
    templates, no login sessions — and (c) has exactly a seed name.
    Their follows rows go with them. A real user who happens to
    share a seed name is safe: registration always sets a password.
    Idempotent: a second run finds nothing to remove. Returns the
    number removed (0 when nothing was done)."""
    has_real_user = conn.execute(
        "SELECT COUNT(*) FROM users WHERE password_hash IS NOT NULL"
        " AND password_hash != ''").fetchone()[0]
    if not has_real_user:
        return 0
    activity_tables = (
        ("push_subscriptions", "user_id"),
        ("requests", "requester_id"),
        ("requests", "saver_id"),
        ("offers", "saver_id"),
        ("coin_ledger", "user_id"),
        ("seat_requests", "user_id"),
        ("waiting_list", "user_id"),
        ("auto_offers", "user_id"),
        ("auto_requests", "user_id"),
        ("offer_templates", "user_id"),
        ("sessions", "user_id"),
    )
    removed = []
    placeholders = ",".join("?" * len(SEED_USERS))
    for row in conn.execute(
            "SELECT * FROM users WHERE name IN (%s)"
            " AND (password_hash IS NULL OR password_hash = '')"
            % placeholders, SEED_USERS).fetchall():
        uid = row["id"]
        busy = False
        for table, column in activity_tables:
            if conn.execute(
                    "SELECT COUNT(*) FROM %s WHERE %s=?"
                    % (table, column), (uid,)).fetchone()[0]:
                busy = True
                break
        if busy:
            continue
        removed.append(row["name"])
        conn.execute("DELETE FROM follows WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    if removed:
        print("ניקוי משתמשי בדיקה: הוסרו %d (%s)."
              % (len(removed), ", ".join(removed)))
        conn.commit()
    return len(removed)


# Round 14: the course names are Hebrew now. Daniel asked why they
# were in English — the seed data was written in English while the
# whole UI is Hebrew. The Hebrew names live in SEED_COURSES (fresh
# databases get them from the seed); this map renames the three
# seeded English names in existing databases. The gereshayim in
# חדו״א is U+05F4, the same character as in ד״ר — plain ASCII
# quotes would break double-quoted Python strings everywhere the
# name appears. Follows and offers reference course_id, never the
# name, so renaming is safe.
COURSE_NAME_HEBREW = {
    "Algorithms": "תכנון אלגוריתמים",
    "Calculus 2": "חדו״א 2",
    "Systems Programming": "תכנות מערכות",
}


def _rename_courses_to_hebrew(conn):
    """Rename exactly the three seeded English course names to
    Hebrew, in an existing database. A name that is already Hebrew
    (or missing — an admin renamed or deleted the course) is left
    alone, and a Hebrew name that already exists is never
    overwritten (the name column is UNIQUE). Idempotent: a second
    run finds no English names left. Returns how many renamed."""
    renamed = 0
    for old, new in COURSE_NAME_HEBREW.items():
        row = conn.execute(
            "SELECT id FROM courses WHERE name=?", (old,)).fetchone()
        if row is None:
            continue
        taken = conn.execute(
            "SELECT id FROM courses WHERE name=?", (new,)).fetchone()
        if taken is not None:
            continue
        conn.execute("UPDATE courses SET name=? WHERE id=?",
                     (new, row["id"]))
        renamed += 1
    return renamed


def _migrate_follows_to_session_id(conn):
    """Round 14: a specific follow stores the exact session id, so
    a lecture and a tutorial at the same hour can be followed
    separately — until now a follow was saved by day + start time,
    and תכנון אלגוריתמים on Sunday 18:00 has both a lecture and a
    tutorial, so one follow covered both whether the user wanted
    that or not. Two steps, both guarded and idempotent:
    (1) add the session_id column and backfill it: a follow whose
    day + start time maps to exactly ONE real session gets that
    session's id; where the hour is ambiguous (two sessions), the
    follow keeps session_id NULL and keeps matching both, exactly
    as before — nothing is silently narrowed. (2) rebuild the
    table so the UNIQUE constraint becomes (user, course,
    session_id): the old UNIQUE(user, course, weekday, start_time)
    made following both sessions at the same hour impossible at
    the database level. The rebuild copies every row (ids
    included); nothing references the follows table, so no
    foreign-key dance is needed — but the foreign_key_check at
    the end still runs, like _make_zone_nullable. A fresh database
    already has the new shape from SCHEMA, so both guards skip
    it. Runs once: after the rebuild, the stored CREATE statement
    holds the new UNIQUE and the guard returns."""
    fcols = [r[1] for r in conn.execute("PRAGMA table_info(follows)")]
    if "session_id" not in fcols:
        conn.execute("ALTER TABLE follows ADD COLUMN session_id"
                     " INTEGER REFERENCES course_sessions(id)")
        for f in conn.execute(
                "SELECT * FROM follows WHERE weekday IS NOT NULL").fetchall():
            matches = conn.execute(
                "SELECT id FROM course_sessions WHERE course_id=?"
                " AND weekday=? AND start_time=?",
                (f["course_id"], f["weekday"],
                 f["start_time"])).fetchall()
            if len(matches) == 1:
                conn.execute("UPDATE follows SET session_id=? WHERE id=?",
                             (matches[0]["id"], f["id"]))
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='follows'").fetchone()
    sql = row[0] if row else ""
    if "UNIQUE(user_id, course_id, session_id)" in sql:
        return  # already the new shape
    if "UNIQUE(user_id, course_id, weekday, start_time)" not in sql:
        return  # some other shape — leave it alone, do not guess
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "CREATE TABLE follows_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL REFERENCES users(id),"
        " course_id INTEGER NOT NULL REFERENCES courses(id),"
        " session_id INTEGER REFERENCES course_sessions(id),"
        " weekday INTEGER, start_time TEXT, created_at TEXT NOT NULL,"
        " UNIQUE(user_id, course_id, session_id))")
    conn.execute(
        "INSERT INTO follows_new (id, user_id, course_id, session_id,"
        " weekday, start_time, created_at) SELECT id, user_id, course_id,"
        " session_id, weekday, start_time, created_at FROM follows")
    conn.execute("DROP TABLE follows")
    conn.execute("ALTER TABLE follows_new RENAME TO follows")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "follows migration left broken references: %r" % (violations,))


def _make_zone_nullable(conn, table, columns_sql):
    """Round 13: seat zones were removed from the app, so new offers
    and templates are stored with zone NULL — and the zone column
    must allow NULL, or every new offer would fail the NOT NULL
    check. SQLite cannot relax a NOT NULL constraint in place
    (ALTER TABLE only adds or renames columns), so a table whose
    zone is still NOT NULL is rebuilt: a new table with the same
    columns except zone nullable, every row copied over (ids
    included), the old table dropped, the new one renamed. No data
    is ever deleted — old rows keep their zone value, which
    nothing reads any more. Two details matter: (a) requests
    references offers, so foreign keys are switched off for the
    rebuild and back on after — and the pragma only takes effect
    OUTSIDE a transaction, so the open transaction is committed
    first and the rebuild committed before switching back on; the
    rebuild ends with a foreign_key_check, which must come back
    empty. (b) Fresh databases already have zone nullable in
    SCHEMA, so the guard skips them. The auto_offers table is NOT
    rebuilt: it is unused since round 13 (weekly auto-offers were
    removed) and its zone column is never written."""
    info = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    zone = [r for r in info if r[1] == "zone"]
    if not zone or zone[0][3] == 0:
        return  # no zone column, or already nullable
    cols = ", ".join(r[1] for r in info)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE %s_new (%s)" % (table, columns_sql))
    conn.execute("INSERT INTO %s_new (%s) SELECT %s FROM %s"
                 % (table, cols, cols, table))
    conn.execute("DROP TABLE %s" % table)
    conn.execute("ALTER TABLE %s_new RENAME TO %s" % (table, table))
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "zone migration left broken references in %s: %r"
            % (table, violations))


def init_db(conn):
    conn.executescript(SCHEMA)
    # Migration for databases created before passwords existed: the
    # users table then had no password_hash column. ADD COLUMN is safe
    # to run on a fresh DB too, because the guard below skips it when
    # the column is already there. Existing users keep NULL (no password
    # yet) and set one once on first login; no data is ever deleted.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "password_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    # Round 14: rename the seeded courses to Hebrew before anything
    # else looks them up by name — the teacher migration below
    # builds its map from SEED_COURSES (Hebrew now), so it must
    # find Hebrew names in the database too.
    _rename_courses_to_hebrew(conn)
    # Round 14: a specific follow stores the exact session id now
    # (see _migrate_follows_to_session_id) — the migration runs
    # once, on the first start after the update, and is a no-op
    # after that.
    _migrate_follows_to_session_id(conn)
    # Migration for databases created before teachers existed: the
    # course_sessions table then had no teacher column. Add it, then
    # fill it for the 18 seeded sessions. The match key is course +
    # weekday + start + building + room: weekday + start alone is NOT
    # unique — תכנון אלגוריתמים has a lecture and a tutorial both on Sunday
    # 18:00 (buildings 35/1 and 34/14), with different teachers.
    scols = [r[1] for r in conn.execute("PRAGMA table_info(course_sessions)")]
    if "teacher" not in scols:
        conn.execute("ALTER TABLE course_sessions ADD COLUMN teacher TEXT")
    teachers = {}
    for cname, _kind, _sessions in SEED_COURSES:
        for wd, start, _end, bldg, room, teacher in _sessions:
            teachers[(cname, wd, start, bldg, room)] = teacher
    for row in conn.execute(
            "SELECT s.id, c.name AS course_name, s.weekday, s.start_time,"
            " s.building, s.room FROM course_sessions s"
            " JOIN courses c ON c.id = s.course_id"
            " WHERE s.teacher IS NULL OR s.teacher = ''"):
        teacher = teachers.get((row["course_name"], row["weekday"],
                                row["start_time"], row["building"],
                                row["room"]))
        if teacher:
            conn.execute("UPDATE course_sessions SET teacher=? WHERE id=?",
                         (teacher, row["id"]))
    # Correction 2026-11-10 (Daniel): the two Dr. Adler lecture rooms
    # were swapped in the seed — Thursday 12:00 is building 34/2 and
    # Sunday 12:00 is building 35/2. The seed above is fixed; repair
    # existing databases too. Match the OLD (wrong) building+room so
    # the update is a no-op once fixed, and only touches the seeded
    # Adler lectures (course + weekday + start + building + room is
    # the session key — see the teacher migration above). The course
    # name is Hebrew by now: _rename_courses_to_hebrew ran first.
    conn.execute(
        "UPDATE course_sessions SET building='34'"
        " WHERE course_id=(SELECT id FROM courses"
        " WHERE name='תכנות מערכות')"
        " AND weekday=3 AND start_time='12:00'"
        " AND building='35' AND room='2'")
    conn.execute(
        "UPDATE course_sessions SET building='35'"
        " WHERE course_id=(SELECT id FROM courses"
        " WHERE name='תכנות מערכות')"
        " AND weekday=6 AND start_time='12:00'"
        " AND building='34' AND room='2'")
    # Migration for databases created before "on my way" existed: the
    # requests table then had no on_the_way column. Same pattern as
    # password_hash and teacher — guarded ALTER TABLE, no data lost.
    rcols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
    if "on_the_way" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN on_the_way TEXT")
    # Migration for databases created before multi-seat requests and
    # reminders existed: add seats_requested (old rows count as 1) and
    # reminded_at (old rows never reminded). Same guarded pattern.
    if "seats_requested" not in rcols:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN seats_requested"
            " INTEGER NOT NULL DEFAULT 1")
    if "reminded_at" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN reminded_at TEXT")
    # Migration for databases created before the 30-minute "leave
    # now" reminder existed: add reminded_30_at (old rows never got
    # one). Same guarded pattern.
    if "reminded_30_at" not in rcols:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN reminded_30_at TEXT")
    # Migration for databases created before the arrival-confirm
    # reminder existed (round 14): add reminded_arrival_at (old
    # rows never got one). Same guarded pattern.
    if "reminded_arrival_at" not in rcols:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN reminded_arrival_at TEXT")
    # Migration for databases created before "declared late"
    # existed: add late_declared_at (when the requester said they
    # are late, NULL = never) and late_status ('pending' while the
    # saver has not decided, 'accepted' once they allow until
    # start+10; a refusal resolves the request itself, so it needs
    # no flag). Same guarded pattern — old rows never declared late.
    if "late_declared_at" not in rcols:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN late_declared_at TEXT")
    if "late_status" not in rcols:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN late_status TEXT")
    # Migration for databases created before the saver reminder
    # existed: the offers table then had no saver_reminded_at column
    # (the flag that says the saver was already reminded for this
    # offer). Same guarded pattern — old offers simply count as
    # never reminded, which is true for them.
    ocols = [r[1] for r in conn.execute("PRAGMA table_info(offers)")]
    if "saver_reminded_at" not in ocols:
        conn.execute(
            "ALTER TABLE offers ADD COLUMN saver_reminded_at TEXT")
    # Migration for databases created before "already in class"
    # existed: the offers table then had no arrived_at column (the
    # time the saver tapped "אני כבר בכיתה"). Same guarded pattern.
    if "arrived_at" not in ocols:
        conn.execute("ALTER TABLE offers ADD COLUMN arrived_at TEXT")
    # Migration for round 13 (seat zones removed): new offers and
    # templates are stored with zone NULL, so the zone column must
    # be nullable — see _make_zone_nullable. The auto_offers table
    # is NOT rebuilt: it is unused since round 13.
    _make_zone_nullable(conn, "offers",
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " course_id INTEGER NOT NULL REFERENCES courses(id),"
        " session_id INTEGER REFERENCES course_sessions(id),"
        " saver_id INTEGER NOT NULL REFERENCES users(id),"
        " class_date TEXT NOT NULL, zone TEXT,"
        " note TEXT NOT NULL DEFAULT '',"
        " seats_total INTEGER NOT NULL DEFAULT 1,"
        " status TEXT NOT NULL DEFAULT 'open',"
        " created_at TEXT NOT NULL,"
        " saver_reminded_at TEXT, arrived_at TEXT")
    _make_zone_nullable(conn, "offer_templates",
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL REFERENCES users(id),"
        " session_id INTEGER NOT NULL REFERENCES course_sessions(id),"
        " zone TEXT, note TEXT NOT NULL DEFAULT '',"
        " seats INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL")
    # Migration for databases created before quiet hours and
    # vacation mode existed: four new columns on users. The
    # preferred_zone column is kept for old databases but unused
    # since round 13 (seat zones were removed). Old users simply
    # have no quiet hours and are not on vacation, which is exactly
    # the default.
    ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "preferred_zone" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN preferred_zone TEXT")
    if "quiet_start" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN quiet_start INTEGER")
    if "quiet_end" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN quiet_end INTEGER")
    if "on_vacation" not in ucols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN on_vacation"
            " INTEGER NOT NULL DEFAULT 0")
    # Migration for databases created before the vacation end date
    # existed: the users table then had no vacation_until column
    # (the date the vacation ends by itself, NULL = no end date).
    # Old users simply have no end date, which is exactly the
    # previous behaviour — vacation until turned off by hand.
    if "vacation_until" not in ucols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN vacation_until TEXT")
    # Round 20, items 13/15: phone + verified. Old users have no
    # phone (NULL) and are not verified (0) — they are prompted on
    # the home page to add a phone, and the admin verifies them
    # after checking name + number against his contacts. Nobody is
    # verified by a migration: only the admin sets it.
    if "phone" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if "verified" not in ucols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN verified"
            " INTEGER NOT NULL DEFAULT 0")
    # Migration for databases created before device detection
    # existed (round 14, item 8): three new columns on
    # push_subscriptions — device ('desktop' / 'iphone' / 'ipad' /
    # 'android' / 'unknown'), browser ('chrome' / 'safari' /
    # 'firefox' / 'edge' / 'opera' / 'other' / 'unknown') and
    # standalone (1 when the subscription was created from an
    # installed Home Screen app). Old rows get 'unknown' / 0, which
    # is honest: nobody knows which device made them.
    pcols = [r[1] for r in conn.execute(
        "PRAGMA table_info(push_subscriptions)")]
    if "device" not in pcols:
        conn.execute(
            "ALTER TABLE push_subscriptions ADD COLUMN device"
            " TEXT NOT NULL DEFAULT 'unknown'")
    if "browser" not in pcols:
        conn.execute(
            "ALTER TABLE push_subscriptions ADD COLUMN browser"
            " TEXT NOT NULL DEFAULT 'unknown'")
    if "standalone" not in pcols:
        conn.execute(
            "ALTER TABLE push_subscriptions ADD COLUMN standalone"
            " INTEGER NOT NULL DEFAULT 0")
    if conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 0:
        now = datetime.now().isoformat(timespec="seconds")
        for name, kind, sessions in SEED_COURSES:
            cur = conn.execute(
                "INSERT INTO courses (name, kind) VALUES (?, ?)", (name, kind))
            cid = cur.lastrowid
            for wd, start, end, bldg, room, teacher in sessions:
                conn.execute(
                    "INSERT INTO course_sessions "
                    "(course_id, weekday, start_time, end_time, building,"
                    " room, teacher) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cid, wd, start, end, bldg, room, teacher))
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        now = datetime.now().isoformat(timespec="seconds")
        for name in SEED_USERS:
            conn.execute(
                "INSERT INTO users (name, balance, sample, created_at)"
                " VALUES (?, ?, 1, ?)", (name, START_BALANCE, now))
        # Daniel follows his own courses generally, so the pilot has
        # at least one follower per course from the start.
        daniel = conn.execute(
            "SELECT id FROM users WHERE name='Daniel'").fetchone()
        for row in conn.execute("SELECT id FROM courses"):
            conn.execute(
                "INSERT OR IGNORE INTO follows "
                "(user_id, course_id, weekday, start_time, created_at)"
                " VALUES (?, ?, NULL, NULL, ?)",
                (daniel["id"], row["id"], now))
    # One-time cleanup of the seeded test users on databases that
    # have real registered users now (see _remove_seed_test_users).
    # On a fresh database this does nothing — the seed users stay
    # until real people arrive.
    _remove_seed_test_users(conn)
    conn.commit()


# ---------------------------------------------------------------- helpers
def _now_text(now=None):
    return (now or datetime.now()).isoformat(timespec="seconds")


def _to_minutes(time_text):
    """'14:00' -> 840. Returns None for garbage (never crashes the page)."""
    try:
        h, m = time_text.split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        return None
    return None


def _valid_time(text):
    return _to_minutes(text) is not None


def _valid_date(text):
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- round 20:
# identity — full name, phone, verified badge.
def _valid_full_name(name):
    """A full name (first + last), validated server-side. Returns an
    error string, or None when the name is fine. At least two words,
    each at least 2 characters — a single word is a nickname, and a
    nickname is exactly what strangers cannot identify ("who is
    דניאלוש?"). The 40-character cap is the old one, kept. Seed
    users (Daniel, Lev, ...) keep their single-word names — they
    were created before this rule, and the admin approves them
    as-is; the rule applies to new registrations and renames."""
    name = (name or "").strip()
    if not name:
        return "נא להכניס שם מלא."
    if len(name) > 40:
        return "השם ארוך מדי (עד 40 תווים)."
    words = name.split()
    if len(words) < 2 or any(len(w) < 2 for w in words):
        return ("נא להכניס שם מלא — שם פרטי ושם משפחה, כדי שמי שלא "
                "מכיר אותך יבין במי מדובר.")
    return None


def _valid_phone(phone):
    """An Israeli mobile number, validated server-side. Returns the
    normalized form (05XXXXXXXX, 10 digits), or None when it is not
    a mobile number. Accepts the usual ways people type it:
    05X-XXXXXXX, 05XXXXXXXX, +9725XXXXXXXX, 9725XXXXXXXX. Spaces,
    dashes and brackets are stripped first. A landline (03, 02, ...)
    or a too-short number is rejected — the number has to be one
    the matched pair can actually call."""
    if phone is None:
        return None
    digits = re.sub(r"[\s\-()]", "", str(phone)).strip()
    if digits.startswith("+972"):
        digits = "0" + digits[4:]
    elif digits.startswith("972"):
        digits = "0" + digits[3:]
    if not re.fullmatch(r"05\d{8}", digits):
        return None
    return digits


def _phone_display(phone):
    """05XXXXXXXX -> 05X-XXXXXXX, for display. Garbage in, '' out."""
    norm = _valid_phone(phone)
    if norm is None:
        return ""
    return "%s-%s" % (norm[:3], norm[3:])


def _wa_link(phone):
    """A WhatsApp link for a normalized phone: 05XXXXXXXX ->
    https://wa.me/9725XXXXXXXX. Garbage in, '' out — the link is
    only rendered when the phone is valid."""
    norm = _valid_phone(phone)
    if norm is None:
        return ""
    return "https://wa.me/972%s" % norm[1:]


def _user_field(row, name, default=None):
    """Read one column from a user row that may be a sqlite3.Row
    or a plain dict (tests pass dicts, and a dict built before a
    column existed has no such key). Missing means the default,
    never a crash."""
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def verified_badge_html(user):
    """The VERIFIED checkmark next to a name — only when the admin
    set verified=1. A plain span, no image, no JS: the checkmark
    is text, so it renders everywhere, including the notification
    log's plain text. Returns '' for an unverified user."""
    if user is None or not _user_field(user, "verified", 0):
        return ""
    return (' <span class="verified" title="מאומת על ידי דניאל">✓</span>')


def display_name_html(user):
    """A user's name as it is shown everywhere in the app: the
    full name, plus the VERIFIED badge when the admin approved
    them. One function, so a name can never appear with the badge
    in one place and without it in another."""
    if user is None:
        return "מישהו"
    return esc(_user_field(user, "name", "מישהו")) + verified_badge_html(user)


def user_needs_profile(user):
    """True when a logged-in user still has to finish their profile:
    a single-word name (a nickname from before full names), or no
    phone yet. The home page shows a banner for exactly these
    users — a prompt, never a block: they can still use the app
    while the banner is there."""
    if user is None:
        return False
    name = (_user_field(user, "name") or "").strip()
    if len(name.split()) < 2:
        return True
    return not _valid_phone(_user_field(user, "phone"))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_user(conn, user_id):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def get_user_by_name(conn, name):
    name = (name or "").strip()
    if not name:
        return None
    return conn.execute("SELECT * FROM users WHERE name=?",
                        (name,)).fetchone()


def get_course(conn, course_id):
    return conn.execute("SELECT * FROM courses WHERE id=?",
                        (course_id,)).fetchone()


def get_session(conn, session_id):
    return conn.execute("SELECT * FROM course_sessions WHERE id=?",
                        (session_id,)).fetchone()


def get_offer(conn, offer_id):
    return conn.execute("SELECT * FROM offers WHERE id=?",
                        (offer_id,)).fetchone()


def available_balance(conn, user_id):
    """Coins the user can still spend. The frozen coin is already
    deducted from `balance` when the request is made (that deduction
    IS the freeze), so balance itself is the spendable amount — do
    NOT subtract the frozen count again, or one coin counts twice."""
    user = get_user(conn, user_id)
    if user is None:
        return None
    return user["balance"]


# Coin-ledger reasons (stored as short English codes, shown in Hebrew).
# The row is always written from the point of view of the user it
# belongs to: "paid" only ever goes to a saver, "frozen" and the
# refunds only ever to the requester.
LEDGER_REASON_HE = {
    "frozen": "בקשת מקום — מטבע הוקפא",
    "paid": "מקום ששמרת אושר — מטבע התקבל",
    "no_show": "החזר — לא הגעת בזמן",
    "not_saved": "החזר — המקום לא היה שם",
    "cancelled": "החזר — ההצעה בוטלה",
    "cancelled_by_requester": "החזר — ביטלת את הבקשה",
    "cancelled_by_saver": "החזר — השומר ביטל את הבקשה",
    "admin_paid": "הכרעה: המטבע עבר לשומר",
    "refunded_by_admin": "הכרעה: המטבע הוחזר למבקש",
}


def _log_coin(conn, user_id, amount, reason, request_id=None, now=None):
    """One row in the coin ledger for every balance change. The
    ledger is the answer to 'where did my coins go': each row says
    when, how many, and why. No balance change without a row — the
    write sits next to the UPDATE in request_seat, confirm_arrival
    and _refund_request, and commits with it."""
    conn.execute(
        "INSERT INTO coin_ledger (user_id, amount, reason, request_id,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, reason, request_id, _now_text(now)))


# The pilot admin is a SEPARATE account, named "דניאל ביק" — Daniel
# registers it himself on the site (no account and no password is
# created in code). The seeded test account is the English "Daniel".
# Both spellings are admin, exactly — no substring matching, no
# other names. Daniel's everyday account, "דניאלוש", is a REGULAR
# user: it sees no "דף בקרה" link, gets 403 on /admin, and cannot
# add courses or reset passwords. When the admin account is logged
# in, the "דף בקרה" link in the nav is how Daniel knows he is the
# admin. A real Daniel was locked out of /admin (and with it the
# notification log, the password reset and the dispute tools) until
# round 13 fixed this — the gate had looked for the English name
# only, and his registered name is Hebrew.
ADMIN_NAMES = ("Daniel", "דניאל ביק")


def is_admin(user):
    """Pilot rule: only Daniel enters courses. Friends only follow.
    The name check is the whole rule — documented in EXPLANATION.md."""
    return user is not None and user["name"] in ADMIN_NAMES


def has_push_subscription(conn, user_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM push_subscriptions WHERE user_id=?",
        (user_id,)).fetchone()
    return row[0] > 0

# ---------------------------------------------------------------- auth
# Real accounts, pilot-simple. Why this exact design (full version in
# EXPLANATION.md): passwords are never stored in plain text — each one
# is hashed with PBKDF2-HMAC-SHA256, 200,000 iterations, with a fresh
# random 16-byte salt per user, all from the standard library. Login
# state is a random server-side session token in an HttpOnly cookie;
# the browser never carries a user id it can edit. No email and no
# phone: this is a pilot among friends, and there is no password reset
# by email — if someone forgets a password, Daniel clears the stored
# hash (sets password_hash to NULL) and the user sets a new one on
# next login, exactly like the first-login migration below.

def _hash_password(password, salt=None):
    """Returns 'salt_hex:hash_hex'. A fresh random salt per user, so
    two users with the same password still get different hashes."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             PBKDF2_ITERATIONS)
    return salt.hex() + ":" + dk.hex()


def verify_password(user, password):
    """True only if `password` matches the stored hash. Constant-time
    comparison, and a user with no hash yet never verifies (they must
    set a password first — see set_password)."""
    stored = user["password_hash"] if user is not None else None
    if not stored or not password:
        return False
    try:
        salt_hex, hash_hex = stored.split(":")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), PBKDF2_ITERATIONS)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# One lock for every check-then-change on money and seats. The server
# is threaded (one thread per request) and the background thread writes
# too, so two requests can otherwise both pass the same check ("one seat
# left", "request still frozen") and both change it — the last seat gets
# sold twice, one arrival gets paid twice. Caught by a QA thread test:
# double-confirm overpaid 20/20 times, the last-seat race overbooked
# 18/20, duplicate offers 10/20. A reentrant lock: re_offer calls
# create_offer, so the same thread may hold it twice. The lock is held
# only for the database change, never for the push network call
# afterwards — a slow push service must not freeze every other request.
# Functions that notify (cancel_request and friends) therefore take the
# lock by hand around the change only, and notify after releasing it.
_MUTEX = threading.RLock()


def _serialized(fn):
    """Run a money/seat function with the write lock held."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _MUTEX:
            return fn(*args, **kwargs)
    return wrapper


@_serialized
def create_user(conn, name, password, phone=None, now=None):
    """Open self-registration: anyone with the link can join, with a
    full name, a phone number and a password. Returns (user_row,
    error). A new user starts with START_BALANCE coins. Round 20:
    the name must be a full name (first + last) and the phone is
    mandatory and must be a real Israeli mobile — a nickname or a
    made-up number is exactly what strangers cannot identify, and
    the matched pair needs a number they can actually call. The
    phone is stored normalized (05XXXXXXXX); it is private — see
    the "your saved seat" card."""
    name = (name or "").strip()
    name_err = _valid_full_name(name)
    if name_err:
        return None, name_err
    password = password or ""
    if len(password) < MIN_PASSWORD_LEN:
        return None, ("הסיסמה קצרה מדי — לפחות %d תווים."
                      % MIN_PASSWORD_LEN)
    norm_phone = _valid_phone(phone)
    if norm_phone is None:
        return None, ("נא להכניס מספר טלפון נייד תקין (למשל "
                      "050-1234567) — הוא מוצג רק למי שהצעת לו "
                      "מקום או ביקשת ממנו, ולדניאל.")
    if get_user_by_name(conn, name) is not None:
        return None, "השם הזה כבר תפוס — בחר שם אחר."
    cur = conn.execute(
        "INSERT INTO users (name, balance, sample, created_at,"
        " password_hash, phone, verified)"
        " VALUES (?, ?, 0, ?, ?, ?, 0)",
        (name, START_BALANCE, _now_text(now), _hash_password(password),
         norm_phone))
    conn.commit()
    return get_user(conn, cur.lastrowid), None


@_serialized
def set_password(conn, user_id, password):
    """First-login migration: a user whose password_hash is still NULL
    (seeded test users, or anyone from a database created before
    passwords) sets a password once. Refuses when a password is
    already set — this page can never overwrite someone else's
    password. Returns an error string, or None on success."""
    user = get_user(conn, user_id)
    if user is None:
        return "משתמש לא נמצא."
    if user["password_hash"]:
        return "כבר נקבעה סיסמה לחשבון הזה — התחבר עם הסיסמה."
    if len(password or "") < MIN_PASSWORD_LEN:
        return ("הסיסמה קצרה מדי — לפחות %d תווים."
                % MIN_PASSWORD_LEN)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (_hash_password(password), user_id))
    conn.commit()
    return None


def clear_password(conn, user_id):
    """Password reset for the pilot, for Daniel to run (documented in
    README): clear the hash and the user sets a new password on next
    login, like a first login. No email involved."""
    conn.execute("UPDATE users SET password_hash=NULL WHERE id=?",
                 (user_id,))
    conn.commit()


@_serialized
def admin_reset_password(conn, admin_user, target_user_id):
    """Daniel resets a user's password from /admin: the password
    hash is cleared (NULL) so on the next login the user sets a new
    password through the existing /set-password flow, and every
    session row of that user is deleted — a reset must not leave old
    sessions alive, or the "reset" user would still be logged in on
    their phone. No ADMIN's password can be reset this way — not
    his own and not another admin's (the admin account is separate:
    "דניאל ביק", registered by Daniel himself) — an admin uses the
    normal flow; only Daniel can call it at all. No log
    table: in a pilot among friends, a reset is a help-desk action,
    not an audit event — the result is visible to the user the next
    time they log in. Returns an error string, or None on success."""
    if not is_admin(admin_user):
        return "רק דניאל יכול לאפס סיסמאות."
    try:
        target_id = int(target_user_id)
    except (TypeError, ValueError):
        return "משתמש לא נמצא."
    target = get_user(conn, target_id)
    if target is None:
        return "משתמש לא נמצא."
    if is_admin(target):
        return "אי אפשר לאפס את הסיסמה של מנהל מכאן — " \
            "קבע סיסמה חדשה בדרך הרגילה."
    clear_password(conn, target["id"])
    conn.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
    conn.commit()
    return None


@_serialized
def rename_user(conn, user_id, new_name):
    """Round 20, item 7: a user changes their display name. The new
    name must be a full name (first + last) — same rule as
    registration. Two guards, both about the admin gate (which is
    by name, see is_admin): an admin account cannot be renamed at
    all (renaming "דניאל ביק" would silently drop the admin), and
    nobody can rename TO an admin name (that would make them admin
    by typing a name). A rename resets verified to 0 — the name
    the admin approved is no longer the name, so it needs approving
    again. Coins, follows, offers and requests are untouched: they
    reference the user id, never the name. Returns an error string,
    or None on success."""
    user = get_user(conn, user_id)
    if user is None:
        return "משתמש לא נמצא."
    if is_admin(user):
        return "אי אפשר לשנות את השם של חשבון מנהל."
    new_name = (new_name or "").strip()
    name_err = _valid_full_name(new_name)
    if name_err:
        return name_err
    if new_name in ADMIN_NAMES:
        return "השם הזה שמור למנהל."
    taken = get_user_by_name(conn, new_name)
    if taken is not None and int(taken["id"]) != int(user_id):
        return "השם הזה כבר תפוס — בחר שם אחר."
    conn.execute("UPDATE users SET name=?, verified=0 WHERE id=?",
                 (new_name, user_id))
    conn.commit()
    return None


@_serialized
def set_phone(conn, user_id, phone):
    """Round 20, item 13: a user sets (or updates) their own phone
    number — the form for users who registered before phones
    existed. Validated server-side, stored normalized. Changing
    the number resets verified to 0: the number the admin checked
    is no longer the number. Returns an error string, or None on
    success."""
    user = get_user(conn, user_id)
    if user is None:
        return "משתמש לא נמצא."
    norm = _valid_phone(phone)
    if norm is None:
        return "מספר הטלפון לא תקין — נייד ישראלי, למשל 050-1234567."
    conn.execute("UPDATE users SET phone=?, verified=0 WHERE id=?",
                 (norm, user_id))
    conn.commit()
    return None


@_serialized
def set_verified(conn, admin_user, target_user_id, verified):
    """Round 20, item 15: the admin marks a user VERIFIED (or takes
    the badge away). Admin only, server-enforced. A verified user
    is one Daniel checked himself — name + phone against his own
    contacts, a real person he knows. An admin account cannot be
    (un)verified this way: the admin needs no badge, and the badge
    must never be a way to touch the admin account. Returns an
    error string, or None on success."""
    if not is_admin(admin_user):
        return "רק דניאל יכול לאשר משתמשים."
    try:
        target_id = int(target_user_id)
    except (TypeError, ValueError):
        return "משתמש לא נמצא."
    target = get_user(conn, target_id)
    if target is None:
        return "משתמש לא נמצא."
    if is_admin(target):
        return "חשבון מנהל לא צריך אישור."
    conn.execute("UPDATE users SET verified=? WHERE id=?",
                 (1 if verified else 0, target_id))
    conn.commit()
    return None


@_serialized
def delete_account(conn, user_id, now=None, sender=None):
    """Round 20, item 6: delete an account — your own, or (admin
    only) someone else's. An admin account can never be deleted
    this way: deleting the only admin would lock everyone out of
    /admin, and the admin is the one doing the deleting. What
    happens, in order: every frozen coin the user holds as a
    requester comes back to them; every frozen coin held by
    requesters of the user's open offers comes back to those
    requesters (the seat is freed — the requester is told the
    saver cancelled, same push as a saver-cancel); then the
    user's offers, requests, waiting-list entries, board posts,
    auto-requests, templates, follows, push subscriptions,
    sessions, coin history and complaints are deleted, and the
    user row itself. The money is settled BEFORE anything is
    deleted, so no coin is ever lost in the cleanup. Returns
    (deleted_name, error)."""
    user = get_user(conn, user_id)
    if user is None:
        return None, "משתמש לא נמצא."
    if is_admin(user):
        return None, "אי אפשר למחוק חשבון מנהל."
    uid = int(user_id)
    # 1. Frozen coins first. As requester: the coins come back to
    # this user (irrelevant to them — the account is gone — but the
    # ledger stays honest). As saver: the requesters get their
    # coins back and are told the seat is gone.
    frozen_as_requester = conn.execute(
        "SELECT * FROM requests WHERE requester_id=? AND status='frozen'",
        (uid,)).fetchall()
    for req in frozen_as_requester:
        _refund_request(conn, req, "cancelled", now)
    frozen_as_saver = conn.execute(
        "SELECT * FROM requests WHERE saver_id=? AND status='frozen'",
        (uid,)).fetchall()
    for req in frozen_as_saver:
        _refund_request(conn, req, "cancelled_by_saver", now)
    saver_cancelled = [dict(r) for r in frozen_as_saver]
    # 2. Everything the user touched, then the user row. The
    # notification log has no foreign key to users, so its rows
    # stay as diagnostic history (the name lookup at read time
    # simply skips a deleted user).
    conn.execute("DELETE FROM waiting_list WHERE user_id=?", (uid,))
    # Waiters on THIS user's offers are not the user themself —
    # their rows point at offers that are about to be deleted, so
    # they go too (the offer is gone, there is nothing to wait for).
    conn.execute(
        "DELETE FROM waiting_list WHERE offer_id IN"
        " (SELECT id FROM offers WHERE saver_id=?)", (uid,))
    conn.execute("DELETE FROM seat_requests WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM auto_requests WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM auto_offers WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM offer_templates WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM follows WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM push_subscriptions WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    # The coin ledger references requests (request_id) — the refund
    # rows written above for the OTHER side (e.g. a requester who
    # got their coin back from this saver's offer) point at requests
    # that are about to be deleted, so they go first. Deleting the
    # requests first would break the foreign key.
    conn.execute(
        "DELETE FROM coin_ledger WHERE request_id IN"
        " (SELECT id FROM requests WHERE requester_id=? OR saver_id=?)",
        (uid, uid))
    conn.execute("DELETE FROM coin_ledger WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM test_push_last WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM complaints WHERE requester_id=? OR saver_id=?",
                 (uid, uid))
    conn.execute("DELETE FROM requests WHERE requester_id=? OR saver_id=?",
                 (uid, uid))
    conn.execute("DELETE FROM offers WHERE saver_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    # 3. Tell the requesters whose saver disappeared. After the
    # lock is released (network I/O, see _MUTEX) — same shape as
    # cancel_request_by_saver.
    for req in saver_cancelled:
        notify_requester_of_saver_cancel(conn, req, sender, now)
    return user["name"], None


def authenticate(conn, name, password):
    """Check a login. Returns (user, error, needs_password):
    - (user, None, False): password correct, log them in.
    - (user, None, True): the account exists but has no password yet —
      send them to /set-password to set one once.
    - (None, error, False): wrong name or wrong password. The message
      never says which one, so it cannot be used to probe for names."""
    user = get_user_by_name(conn, name)
    if user is None:
        return None, "השם או הסיסמה לא נכונים.", False
    if not user["password_hash"]:
        return user, None, True
    if not verify_password(user, password):
        return None, "השם או הסיסמה לא נכונים.", False
    return user, None, False


def create_session(conn, user_id, now=None):
    """A login session: a random token, stored server-side with an
    expiry. The token is the only thing the browser holds, in an
    HttpOnly cookie — it cannot be edited into someone else's id,
    because it is not an id at all."""
    now = now or datetime.now()
    token = secrets.token_urlsafe(32)
    expires = now + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (token, user_id, now.isoformat(timespec="seconds"),
         expires.isoformat(timespec="seconds")))
    conn.commit()
    return token


def get_user_from_session(conn, token, now=None):
    """The user behind a session token, or None if the token is
    unknown or expired. Expired sessions are deleted on sight."""
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token=?",
                       (token,)).fetchone()
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if (now or datetime.now()) >= expires:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    return get_user(conn, row["user_id"])


def delete_session(conn, token):
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


# ---------------------------------------------------------------- users
@_serialized
def add_follow(conn, user_id, course_id, session_id=None, now=None):
    """A follow is either general (session_id=None: notify about any
    offer for this course) or specific (session_id: notify only for
    that one real session, e.g. the slot where the student always
    arrives late after a tight transition). The specific follow takes
    a session id, never a free weekday/time: the session is looked up
    and its weekday/start_time are derived from it, so an impossible
    combination (תכנון אלגוריתמים on Saturday evening, when no such class
    exists) cannot be created at all. Round 14: the exact session
    id is stored on the row, so a lecture and a tutorial at the
    same hour are two different follows — before, both shared one
    (weekday, start_time) and could not be followed separately.
    Returns (follow_row, error)."""
    if get_user(conn, user_id) is None:
        return None, "משתמש לא ידוע."
    try:
        course_id = int(course_id)
    except (TypeError, ValueError):
        return None, "קורס לא ידוע."
    if get_course(conn, course_id) is None:
        return None, "קורס לא ידוע."
    weekday = None
    start_time = None
    session_id_value = None
    if session_id is not None:
        try:
            session_id = int(session_id)
        except (TypeError, ValueError):
            return None, "שיעור לא ידוע."
        session = get_session(conn, session_id)
        if session is None:
            return None, "שיעור לא ידוע."
        if int(session["course_id"]) != course_id:
            return None, "השיעור הזה לא שייך לקורס שבחרת."
        weekday = session["weekday"]
        start_time = session["start_time"]
        session_id_value = session["id"]
    # SQLite UNIQUE treats NULL as distinct from NULL, so the table
    # constraint alone cannot stop a second identical general follow
    # (weekday NULL, start_time NULL). Check it explicitly instead.
    if weekday is None:
        existing = conn.execute(
            "SELECT id FROM follows WHERE user_id=? AND course_id=?"
            " AND weekday IS NULL AND start_time IS NULL",
            (user_id, course_id)).fetchone()
        if existing:
            return None, "כבר עוקבים אחרי הקורס הזה ככה."
    else:
        # A specific follow for the same session twice is a duplicate
        # — and so is one for a session an old (pre-round-14) follow
        # already covers by day + start time. The table UNIQUE
        # catches the first case; this check catches the second.
        existing = conn.execute(
            "SELECT id FROM follows WHERE user_id=? AND course_id=?"
            " AND (session_id=? OR (session_id IS NULL AND weekday=?"
            " AND start_time=?))",
            (user_id, course_id, session_id_value, weekday,
             start_time)).fetchone()
        if existing:
            return None, "כבר עוקבים אחרי הקורס הזה ככה."
    try:
        cur = conn.execute(
            "INSERT INTO follows (user_id, course_id, session_id, weekday,"
            " start_time, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, course_id, session_id_value, weekday, start_time,
             _now_text(now)))
        conn.commit()
    except sqlite3.IntegrityError:
        return None, "כבר עוקבים אחרי הקורס הזה ככה."
    return conn.execute("SELECT * FROM follows WHERE id=?",
                        (cur.lastrowid,)).fetchone(), None


def has_general_follow(conn, user_id, course_id):
    """Does this user already follow the whole course? Round 14:
    a general follow already covers every session of the course,
    so a specific follow for one of its sessions would be a
    redundant duplicate — creation paths skip those instead of
    storing them (see /register and /add-follow). Existing specific
    follows are never deleted by this: deleting a follow the user
    made on purpose is not this function's job."""
    row = conn.execute(
        "SELECT id FROM follows WHERE user_id=? AND course_id=?"
        " AND weekday IS NULL AND start_time IS NULL",
        (user_id, course_id)).fetchone()
    return row is not None


@_serialized
def remove_follow(conn, follow_id, user_id):
    row = conn.execute("SELECT * FROM follows WHERE id=?",
                       (follow_id,)).fetchone()
    if row is None or row["user_id"] != int(user_id):
        return "המעקב לא נמצא."
    conn.execute("DELETE FROM follows WHERE id=?", (follow_id,))
    conn.commit()
    return None


def follow_matches(follow, session):
    """Does this follow want a notification about an offer for `session`?
    General follows (weekday NULL) always match. A specific follow
    with an exact session id (round 14) matches only that session —
    so a lecture and a tutorial at the same hour are followed
    separately. A specific follow without a session id is an old
    (pre-round-14) or ambiguous-hour follow: it matches by day and
    start time, as before."""
    if follow["weekday"] is None:
        return True
    if session is None:
        return False
    try:
        sid = follow["session_id"]
    except (KeyError, IndexError, TypeError):
        sid = None
    if sid is not None:
        return int(sid) == int(session["id"])
    if int(follow["weekday"]) != int(session["weekday"]):
        return False
    if follow["start_time"]:
        return follow["start_time"] == session["start_time"]
    return True


def followers_for_offer(conn, offer):
    """Users (other than the saver) whose follows match this offer's
    session. One user is listed once, even with several matching follows."""
    session = None
    if offer["session_id"]:
        session = get_session(conn, offer["session_id"])
    found = {}
    for row in conn.execute(
            "SELECT f.*, u.name AS user_name FROM follows f"
            " JOIN users u ON u.id = f.user_id WHERE f.course_id=?",
            (offer["course_id"],)):
        if row["user_id"] == offer["saver_id"]:
            continue
        if follow_matches(row, session):
            found[row["user_id"]] = row["user_name"]
    return found


def followers_for_session(conn, course_id, session):
    """Users whose follows match this session — the same matching as
    for an offer (general follows always match; specific follows
    only if the session is on their day and time). Used when a new
    session is added: the followers of the course are told about
    it. One user is listed once, even with several matching follows."""
    found = {}
    for row in conn.execute(
            "SELECT f.*, u.name AS user_name FROM follows f"
            " JOIN users u ON u.id = f.user_id WHERE f.course_id=?",
            (course_id,)):
        if follow_matches(row, session):
            found[row["user_id"]] = row["user_name"]
    return found


def followers_count_for_session(conn, session):
    """How many users would be notified about an offer for this
    session — the demand signal the saver sees before offering.
    General follows for the course always count; specific follows
    count only if they match this session (same follow_matches
    logic as the notification itself). One user counts once, even
    with a general AND a specific follow. The saver is not excluded
    here: this is demand for the session, not a recipient list."""
    if session is None:
        return 0
    found = set()
    for row in conn.execute(
            "SELECT * FROM follows WHERE course_id=?",
            (session["course_id"],)):
        if follow_matches(row, session):
            found.add(row["user_id"])
    return len(found)


def followers_label(count):
    """Hebrew label for a follower count: none, one, many."""
    if count == 0:
        return "אין עוקבים"
    if count == 1:
        return "עוקב אחד"
    return "%d עוקבים" % count


def open_board_count_for_session(conn, session_id):
    """How many people are actively looking for a seat in this
    session — open board posts only. A fulfilled, expired or
    cancelled post is not demand any more. The saver sees this
    next to the follower count when offering: followers say who
    might come, board posts say who is already waiting."""
    if not session_id:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM seat_requests"
        " WHERE session_id=? AND status='open'",
        (session_id,)).fetchone()
    return row[0]


def board_demand_label(count):
    """Hebrew label for open board demand: one, many. Empty when
    0 — no demand, no noise on the option."""
    if count == 0:
        return ""
    if count == 1:
        return "מחפש מקום אחד"
    return "%d מחפשים מקום" % count


# ---------------------------------------------------------------- user settings
# Quiet hours: a small personal setting, set on /my. Quiet hours
# never queue anything — a skipped notification is skipped, not
# delayed (see _push_to_user).
def set_quiet_hours(conn, user_id, start, end):
    """Save quiet hours, or clear them. Both hours together, or both
    empty to clear — one hour alone is a mistake, not a setting.
    Hours are 0–23; equal start and end is rejected (a range, not
    a moment). Returns an error string, or None on success."""
    user = get_user(conn, user_id)
    if user is None:
        return "משתמש לא נמצא."
    # Strip strings only — int 0 (midnight) is a real hour, not an
    # empty value, so the old `(start or "")` trick would have
    # silently cleared midnight starts.
    if isinstance(start, str):
        start = start.strip()
    if isinstance(end, str):
        end = end.strip()
    if start in ("", None) and end in ("", None):
        conn.execute(
            "UPDATE users SET quiet_start=NULL, quiet_end=NULL WHERE id=?",
            (user_id,))
        conn.commit()
        return None
    if start in ("", None) or end in ("", None):
        return "מלא את שתי השעות, או השאר את שתיהן ריקות."
    try:
        start = int(start)
        end = int(end)
    except (TypeError, ValueError):
        return "השעות חייבות להיות מספרים בין 0 ל־23."
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return "השעות חייבות להיות מספרים בין 0 ל־23."
    if start == end:
        return ("שעת ההתחלה ושעת הסיום לא יכולות להיות זהות — "
                "שעות שקטות הן טווח, לא רגע.")
    conn.execute(
        "UPDATE users SET quiet_start=?, quiet_end=? WHERE id=?",
        (start, end, user_id))
    conn.commit()
    return None


def in_quiet_hours(user, now=None):
    """True when `now` falls inside the user's quiet hours. NULL
    hours mean no quiet hours at all. An overnight window (23:00–
    07:00) wraps past midnight: the hour is quiet when it is at or
    after the start OR before the end. A day window (08:00–22:00)
    is quiet between start (inclusive) and end (exclusive)."""
    if user is None:
        return False
    try:
        start = user["quiet_start"]
        end = user["quiet_end"]
    except (KeyError, IndexError, TypeError):
        return False
    if start is None or end is None:
        return False
    if start == end:
        # Not a range at all — the setter rejects it, but a hand-edited
        # DB could hold it; it must never mean "quiet all day".
        return False
    hour = (now or datetime.now()).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def quiet_hours_label(user):
    """'שעות שקטות: 23:00–07:00' or '' when the user set none."""
    if user is None:
        return ""
    try:
        start = user["quiet_start"]
        end = user["quiet_end"]
    except (KeyError, IndexError, TypeError):
        return ""
    if start is None or end is None:
        return ""
    return "שעות שקטות: %02d:00–%02d:00" % (start, end)


# ---------------------------------------------------------------- vacation mode
# Vacation mode: one toggle for a whole absence (holiday, reserve
# duty). While it is on, the user is out of the loop: no push
# is sent to them (see _push_to_user) — every skip is logged as
# skipped-vacation, same style as skipped-quiet-hours. Quiet hours
# cover a night; vacation covers a whole absence. Skipped means
# skipped, never queued: a notification that arrives during
# vacation is not sent later. Existing open offers are NOT touched:
# vacation pauses the future, it does not cancel what already
# exists — except the user's own frozen requests, which ARE
# cancelled the moment vacation turns on (see set_vacation):
# someone going on vacation is not coming to class, and a frozen
# request would otherwise hold their coin until class start + 5.
@_serialized
def set_vacation(conn, user_id, on, sender=None, now=None):
    """Turn vacation mode on or off. `on` is 1, True, "1" or "on"
    for on; anything else (0, "0", missing) for off. Returns an
    error string, or None on success. Turning off also clears the
    end date — a stale date must not linger into the next vacation.
    Turning on leaves the date alone (it is set separately, see
    set_vacation_until).

    While on: no pushes are sent to them. Existing open offers stay
    exactly as they were — vacation pauses the future only.

    One exception: turning vacation ON cancels the user's own
    FROZEN requests. Someone turning vacation on is probably not
    coming to class, but a frozen request would otherwise stay —
    and the coin would stay frozen — until class start + 5
    minutes. So each frozen request is cancelled the moment
    vacation turns on: the coin is refunded at once, the seat is
    freed, and it is treated exactly like a requester cancel —
    the saver gets the "request cancelled" push, the free-up path
    runs if the offer goes from 0 to 1 (waiting list first), and
    the requester gets one push of their own saying the request
    was cancelled because vacation was turned on. Already-resolved
    requests are untouched — nothing to refund, nothing to free.
    Turning vacation OFF does nothing to requests: a cancelled
    request stays cancelled (the coin is already back; nothing to
    resurrect). Requests of other users are untouched, whoever
    turns vacation on. The refunds run under the write lock (the
    same lock as confirm_arrival), so a vacation cancel racing an
    arrival confirm has exactly one winner: either the request is
    confirmed and the saver is paid, or it is cancelled and the
    coin is refunded — never both."""
    user = get_user(conn, user_id)
    if user is None:
        return "משתמש לא נמצא."
    if on is True or on == 1 or \
            str(on).strip().lower() in ("1", "on", "true"):
        value = 1
    else:
        value = 0
    conn.execute("UPDATE users SET on_vacation=? WHERE id=?",
                 (value, user_id))
    if value == 0:
        conn.execute("UPDATE users SET vacation_until=NULL WHERE id=?",
                     (user_id,))
    conn.commit()
    if value == 0:
        return None
    # Turning vacation ON: cancel this user's frozen requests.
    # Same lock as confirm_arrival — a simultaneous arrival
    # confirm and a vacation cancel cannot both win the same
    # request.
    cancelled = []
    with _MUTEX:
        frozen = conn.execute(
            "SELECT * FROM requests WHERE requester_id=?"
            " AND status='frozen'", (user_id,)).fetchall()
        for req in frozen:
            offer = get_offer(conn, req["offer_id"])
            seats_before = None
            if offer is not None:
                seats_before = seats_available(conn, offer)
            _refund_request(conn, req, "cancelled_by_requester", now)
            cancelled.append((dict(req), req["offer_id"], seats_before))
        conn.commit()
    # Outside the lock: the pushes do network I/O. Per request, in
    # the same order as a requester cancel: the free-up path first
    # (waiting list first, only when the offer went 0→1), then the
    # saver's "request cancelled" push, then the requester's own
    # "cancelled because you turned vacation on" push.
    for req, offer_id, seats_before in cancelled:
        if offer_id is not None and seats_before == 0:
            maybe_notify_seat_freed(conn, offer_id, seats_before, sender,
                                    now, exclude_user_id=user_id)
        notify_saver_of_cancel(conn, req, sender, now)
        notify_requester_of_vacation_cancel(conn, req, sender, now)
    return None


def is_on_vacation(user):
    """True when the user turned vacation mode on. A missing column
    (a hand-made old row) counts as not on vacation."""
    if user is None:
        return False
    try:
        return bool(user["on_vacation"])
    except (KeyError, IndexError, TypeError):
        return False


def _parse_vacation_until(value, today=None):
    """Validate an end date for vacation. Returns (date_str, error):
    ("YYYY-MM-DD", None) for a valid date, (None, None) for an empty
    value — empty means "clear the end date" — and (None, error) for
    anything else. A past date is rejected: the end of a vacation
    cannot be yesterday, and rejecting it must not change any state,
    so this parses only and never writes."""
    today = today or datetime.now().date()
    if value is None:
        return None, None
    if isinstance(value, str):
        value = value.strip()
    if not value:
        return None, None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None, "התאריך לא תקין — נא לבחור תאריך מהלוח."
    if parsed < today:
        return None, "תאריך הסיום חייב להיות היום או מאוחר יותר."
    return value.strip(), None


@_serialized
def set_vacation_until(conn, user_id, until):
    """Set or clear the vacation end date. `until` is a "YYYY-MM-DD"
    string, or empty to clear (no end date — vacation stays on until
    turned off by hand). The date only matters while vacation is on;
    it does not turn vacation on or off by itself. A past or invalid
    date is rejected and nothing changes. Returns an error string,
    or None on success."""
    user = get_user(conn, user_id)
    if user is None:
        return "משתמש לא נמצא."
    parsed, err = _parse_vacation_until(until)
    if err:
        return err
    conn.execute("UPDATE users SET vacation_until=? WHERE id=?",
                 (parsed, user_id))
    conn.commit()
    return None


def vacation_until_text(user):
    """'חופשה עד 12.11' for the end date, 'ללא תאריך סיום' when
    vacation is on with no date, '' when not on vacation. The date
    is shown day.month — the year is obvious for a vacation. A
    corrupt stored date shows nothing rather than crashing the page."""
    if not is_on_vacation(user):
        return ""
    try:
        until = user["vacation_until"]
    except (KeyError, IndexError, TypeError):
        return "ללא תאריך סיום"
    if not until:
        return "ללא תאריך סיום"
    try:
        return "חופשה עד %s" % datetime.strptime(
            until, "%Y-%m-%d").strftime("%d.%m")
    except (ValueError, TypeError):
        return ""


def clear_expired_vacations(conn, now=None):
    """The auto-off: clear vacations whose end date passed. Vacation
    is active through the END of the chosen date — a vacation ending
    12.11 is still on all of 12.11, and is cleared on the first tick
    of 13.11. Sets on_vacation=0 and the date to NULL, so nothing
    stale lingers. A corrupt stored date is skipped, never a crash:
    one bad row must not kill the background thread. Returns how
    many users were cleared. Called by the same daemon thread as
    the reminders and auto-offers — no new thread, no new service."""
    now = now or datetime.now()
    today = now.date()
    cleared = 0
    for row in conn.execute(
            "SELECT * FROM users WHERE on_vacation=1"
            " AND vacation_until IS NOT NULL").fetchall():
        try:
            end = datetime.strptime(
                row["vacation_until"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if end < today:
            conn.execute(
                "UPDATE users SET on_vacation=0, vacation_until=NULL"
                " WHERE id=?", (row["id"],))
            cleared += 1
    if cleared:
        conn.commit()
    return cleared


# ---------------------------------------------------------------- offers
def _session_start_datetime(offer, session):
    day = datetime.strptime(offer["class_date"], "%Y-%m-%d").date()
    h, m = session["start_time"].split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m))


def sort_offers_by_start(conn, offers):
    """Open offers, earliest class first. A saver with offers at
    10:00, 14:00 and 18:00 sees the 10:00 one first, not the one
    created last — creation order says nothing about urgency, class
    start does. Ties break by creation time. Offers with no session
    sort last (defensive; offers normally always have a session)."""
    def key(o):
        session = (get_session(conn, o["session_id"])
                   if o["session_id"] else None)
        if session is None:
            return (1, datetime.max, o["created_at"])
        return (0, _session_start_datetime(dict(o), dict(session)),
                o["created_at"])
    return sorted(offers, key=key)


def session_window_status(session, now=None):
    """Where is today's session relative to its offer window?
    Returns (status, opens_at_HHMM): "open" (window open now),
    "not_yet" (opens later today), "started" (class already started),
    "ended" (class over). Used by the home page and the offer form so
    the user sees the window instead of guessing it."""
    now = now or datetime.now()
    start_min = _to_minutes(session["start_time"])
    end_min = _to_minutes(session["end_time"])
    if start_min is None:
        return "started", ""
    today = now.date()
    start = datetime(today.year, today.month, today.day,
                     start_min // 60, start_min % 60)
    opens = start - timedelta(minutes=OFFER_WINDOW_MINUTES)
    if end_min is not None:
        end = datetime(today.year, today.month, today.day,
                       end_min // 60, end_min % 60)
        if now >= end:
            return "ended", opens.strftime("%H:%M")
    if now >= start:
        return "started", opens.strftime("%H:%M")
    if now >= opens:
        return "open", opens.strftime("%H:%M")
    return "not_yet", opens.strftime("%H:%M")


def offerable_sessions(conn, now=None):
    """Today's sessions whose offer window is open right now — the
    only sessions an offer can be made for. A session starting more
    than OFFER_WINDOW_MINUTES from now is never listed: a saver can
    only offer a class they are actually on their way to, so the
    offer form never shows far-away sessions at all."""
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT s.*, c.name AS course_name FROM course_sessions s"
        " JOIN courses c ON c.id=s.course_id"
        " WHERE s.weekday=? ORDER BY s.start_time",
        (now.weekday(),)).fetchall()
    return [r for r in rows
            if session_window_status(dict(r), now)[0] == "open"]


def session_for_follow(conn, follow):
    """The real session a specific follow points at, or None for a
    general follow / a session that no longer exists. Round 14: the
    follow stores the exact session id, so this looks it up
    directly; an old follow without one falls back to the day +
    start-time lookup, which returns the first session at that
    hour. Used to display the follow with its teacher and place,
    instead of a bare day and time."""
    if follow["weekday"] is None:
        return None
    try:
        sid = follow["session_id"]
    except (KeyError, IndexError, TypeError):
        sid = None
    if sid is not None:
        return get_session(conn, sid)
    return conn.execute(
        "SELECT * FROM course_sessions WHERE course_id=? AND weekday=?"
        " AND start_time=? LIMIT 1",
        (follow["course_id"], follow["weekday"],
         follow["start_time"])).fetchone()


@_serialized
def create_offer(conn, saver_id, session_id, note, seats,
                 now=None):
    """Saver checks in live. Offers open only in the 60 minutes before
    class, and only for today's session. Returns (offer, error)."""
    now = now or datetime.now()
    saver = get_user(conn, saver_id)
    if saver is None:
        return None, "משתמש לא ידוע."
    session = get_session(conn, session_id)
    if session is None:
        return None, "שיעור לא ידוע."
    try:
        seats = int(seats)
    except (TypeError, ValueError):
        return None, "מספר המקומות חייב להיות מספר."
    if seats < 1 or seats > MAX_SEATS_PER_OFFER:
        return None, ("אפשר להציע עד %d מקומות."
                      % MAX_SEATS_PER_OFFER)
    if session["weekday"] != now.weekday():
        return None, ("השיעור הזה לא היום — ההצעות חיות, "
                      "רק לשיעור של היום.")
    class_date = now.strftime("%Y-%m-%d")
    start = _session_start_datetime({"class_date": class_date}, session)
    opens = start - timedelta(minutes=OFFER_WINDOW_MINUTES)
    if now < opens:
        return None, ("ההצעות לשיעור הזה נפתחות %d דקות לפני שהוא "
                      "מתחיל (ב־%s)." % (OFFER_WINDOW_MINUTES,
                                          opens.strftime("%H:%M")))
    if now >= start:
        return None, "השיעור הזה כבר התחיל — מאוחר מדי להציע."
    # One open offer per saver per session per day (no duplicates).
    dup = conn.execute(
        "SELECT id FROM offers WHERE saver_id=? AND session_id=?"
        " AND class_date=? AND status='open'",
        (saver_id, session_id, class_date)).fetchone()
    if dup:
        return None, "כבר יש לך הצעה פתוחה לשיעור הזה."
    cur = conn.execute(
        "INSERT INTO offers (course_id, session_id, saver_id, class_date,"
        " note, seats_total, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
        (session["course_id"], session_id, saver_id, class_date,
         (note or "").strip()[:120], seats, _now_text(now)))
    conn.commit()
    return get_offer(conn, cur.lastrowid), None


def _request_seats(request):
    """How many seats one request covers. Old rows and dicts built in
    tests without the column count as 1 — the migration default."""
    try:
        return max(1, int(request["seats_requested"]))
    except (KeyError, IndexError, TypeError, ValueError):
        return 1


def seats_available(conn, offer):
    """Free seats left in an offer. Sums seats_requested, not rows:
    one request can cover 2 seats, so counting rows would undercount
    occupied seats. Rows from before the column existed count as 1
    (the migration default)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(seats_requested), 0) FROM requests"
        " WHERE offer_id=? AND status IN ('frozen', 'confirmed')",
        (offer["id"],)).fetchone()
    return offer["seats_total"] - row[0]


# ---------------------------------------------------------------- waiting list
# When an offer is full, followers can queue for it instead of
# refreshing the page. The list is a plain queue (first joined, first
# notified), one row per user per offer. A waiting user is NOT a
# reservation — the seat is still first come, first served when it
# frees up. The list only decides who gets told first.
def waiting_users_for_offer(conn, offer_id):
    """Waiting users for one offer, in join order (first joined first
    notified). Returns the user rows."""
    return conn.execute(
        "SELECT u.* FROM waiting_list w JOIN users u ON u.id = w.user_id"
        " WHERE w.offer_id=? ORDER BY w.created_at, w.id",
        (offer_id,)).fetchall()


def waiting_count_for_offer(conn, offer_id):
    row = conn.execute("SELECT COUNT(*) FROM waiting_list WHERE offer_id=?",
                       (offer_id,)).fetchone()
    return row[0]


def is_on_waiting_list(conn, user_id, offer_id):
    row = conn.execute(
        "SELECT id FROM waiting_list WHERE offer_id=? AND user_id=?",
        (offer_id, user_id)).fetchone()
    return row is not None


def has_frozen_request(conn, user_id, offer_id):
    """Does this user already hold a seat in this offer? A user who
    already requested cannot also queue for the same offer."""
    row = conn.execute(
        "SELECT id FROM requests WHERE offer_id=? AND requester_id=?"
        " AND status='frozen'", (offer_id, user_id)).fetchone()
    return row is not None


def waiting_position_for_user(conn, offer_id, user_id):
    """This user's place in a full offer's waiting list: 1-based,
    in join order (first joined first notified). None when the user
    is not on the list. The list is a plain queue — the position is
    just the count of entries created before theirs, plus one."""
    rows = waiting_users_for_offer(conn, offer_id)
    for i, u in enumerate(rows):
        if int(u["id"]) == int(user_id):
            return i + 1
    return None


def waiting_position_label(position):
    """'אתה ראשון בתור' for position 1, 'אתה מספר N בתור' after
    that, '' when not on the list."""
    if position is None:
        return ""
    if position == 1:
        return "אתה ראשון בתור"
    return "אתה מספר %d בתור" % position


def waiting_label(count):
    """Hebrew label for a waiting-list size."""
    if count == 0:
        return ""
    if count == 1:
        return "אחד ברשימת ההמתנה"
    return "%d ברשימת ההמתנה" % count


@_serialized
def join_waiting_list(conn, user_id, offer_id, now=None):
    """Queue for a full offer. Only a follower of the course may join
    — the same follow that decides who gets the offer notification
    (general follows always match; specific follows only for their
    session), so someone who would never be notified about this
    offer cannot queue for it either. Joining twice, joining a
    non-full offer, or joining your own offer is rejected. Returns
    (row, error)."""
    user = get_user(conn, user_id)
    if user is None:
        return None, "משתמש לא ידוע."
    offer = get_offer(conn, offer_id)
    if offer is None:
        return None, "ההצעה לא נמצאה."
    if offer["status"] != "open":
        return None, "ההצעה הזו כבר לא פתוחה."
    if int(offer["saver_id"]) == int(user_id):
        return None, "אי אפשר להצטרף לרשימת ההמתנה של הצעה שלך."
    if has_frozen_request(conn, user_id, offer_id):
        return None, "כבר ביקשת מקום בהצעה הזו."
    if seats_available(conn, offer) > 0:
        return None, "יש מקומות פנויים בהצעה הזו — אפשר לבקש מקום ישר."
    if int(user_id) not in followers_for_offer(conn, offer):
        return None, "רק מי שעוקב אחרי הקורס יכול להצטרף לרשימת ההמתנה."
    if is_on_waiting_list(conn, user_id, offer_id):
        return None, "אתה כבר ברשימת ההמתנה להצעה הזו."
    try:
        cur = conn.execute(
            "INSERT INTO waiting_list (offer_id, user_id, created_at)"
            " VALUES (?, ?, ?)",
            (offer_id, user_id, _now_text(now)))
        conn.commit()
    except sqlite3.IntegrityError:
        return None, "אתה כבר ברשימת ההמתנה להצעה הזו."
    return conn.execute("SELECT * FROM waiting_list WHERE id=?",
                        (cur.lastrowid,)).fetchone(), None


@_serialized
def leave_waiting_list(conn, user_id, offer_id):
    """Leave the queue. Anyone can leave at any time — a waiting list
    you cannot leave is a trap, and the offer may have changed."""
    row = conn.execute(
        "SELECT id FROM waiting_list WHERE offer_id=? AND user_id=?",
        (offer_id, user_id)).fetchone()
    if row is None:
        return "אתה לא ברשימת ההמתנה להצעה הזו."
    conn.execute("DELETE FROM waiting_list WHERE id=?", (row["id"],))
    conn.commit()
    return None


def _delete_waiting_entries_for_offer(conn, offer_id):
    """Clean up the queue when an offer closes or expires: entries
    for a closed offer are inert — nobody can be notified about a
    seat that no longer exists — so they are deleted, not left to
    pile up. Called from inside the lock, by the same code that
    closes the offer."""
    conn.execute("DELETE FROM waiting_list WHERE offer_id=?", (offer_id,))


# ---------------------------------------------------------------- request board
# "מחפש מקום": the flow reversed. Until now only a saver could start
# anything — a follower waited for an offer to appear. Now a student
# who knows they will be late can post "I need a seat for this
# session" in advance, and when a saver offers that session, the
# poster gets a push. The board is demand signal only: it never
# reserves a seat and never touches coins. A post lives until the
# next occurrence of its session starts, then it expires.
BOARD_STATUSES_HE = {
    "open": "פתוחה",
    "fulfilled": "קיבל מקום",
    "expired": "פגה",
    "cancelled": "בוטלה",
}


def next_session_start(session, now=None):
    """The next time this weekly session happens, as a datetime.
    Today at start time if today is the session's weekday and the
    class has not started yet; otherwise the next day with that
    weekday. A board post expires at this moment."""
    now = now or datetime.now()
    start_min = _to_minutes(session["start_time"])
    if start_min is None:
        return None
    days_ahead = (session["weekday"] - now.weekday()) % 7
    day = now.date() + timedelta(days=days_ahead)
    start = datetime(day.year, day.month, day.day,
                     start_min // 60, start_min % 60)
    if days_ahead == 0 and now >= start:
        start = start + timedelta(days=7)
    return start


@_serialized
def post_seat_request(conn, user_id, session_id, now=None):
    """Post "I need a seat" for a session. Any seeded session, any
    user — no follow needed, because the post itself says "tell me
    about this session". One open post per user per session: the
    partial unique index enforces it even under a race, and the
    explicit check gives the Hebrew error. Returns (row, error)."""
    if get_user(conn, user_id) is None:
        return None, "משתמש לא ידוע."
    session = get_session(conn, session_id)
    if session is None:
        return None, "שיעור לא ידוע."
    existing = conn.execute(
        "SELECT id FROM seat_requests WHERE user_id=? AND session_id=?"
        " AND status='open'", (user_id, session_id)).fetchone()
    if existing:
        return None, "כבר פרסמת שאתה מחפש מקום לשיעור הזה."
    # Already holding a seat for this session: the board is for
    # demand, and you are not demanding — you already have one. (A
    # cancelled or refunded request does not block: only frozen.)
    holding = conn.execute(
        "SELECT r.id FROM requests r JOIN offers o ON o.id = r.offer_id"
        " WHERE r.requester_id=? AND r.status='frozen'"
        " AND o.session_id=?", (user_id, session_id)).fetchone()
    if holding:
        return None, "כבר יש לך מקום שמור לשיעור הזה — אין צורך לחפש."
    try:
        cur = conn.execute(
            "INSERT INTO seat_requests (user_id, session_id, status,"
            " created_at) VALUES (?, ?, 'open', ?)",
            (user_id, session_id, _now_text(now)))
        conn.commit()
    except sqlite3.IntegrityError:
        return None, "כבר פרסמת שאתה מחפש מקום לשיעור הזה."
    return conn.execute("SELECT * FROM seat_requests WHERE id=?",
                        (cur.lastrowid,)).fetchone(), None


@_serialized
def cancel_seat_request(conn, request_id, user_id):
    """Take down your own board post. Only the poster, only while
    open — a fulfilled or expired post is history, not something
    to cancel."""
    row = conn.execute("SELECT * FROM seat_requests WHERE id=?",
                       (request_id,)).fetchone()
    if row is None or int(row["user_id"]) != int(user_id):
        return "הבקשה לא נמצאה."
    if row["status"] != "open":
        return "הבקשה הזו כבר לא פתוחה."
    conn.execute("UPDATE seat_requests SET status='cancelled' WHERE id=?",
                 (request_id,))
    conn.commit()
    return None


def open_seat_requests(conn):
    """All open board posts, with session, course and poster names
    attached. Callers group them by session for display."""
    return conn.execute(
        "SELECT sr.*, s.weekday, s.start_time, s.end_time, s.building,"
        " s.room, s.teacher, c.name AS course_name, u.name AS user_name,"
        " u.verified AS user_verified"
        " FROM seat_requests sr"
        " JOIN course_sessions s ON s.id = sr.session_id"
        " JOIN courses c ON c.id = s.course_id"
        " JOIN users u ON u.id = sr.user_id"
        " WHERE sr.status='open' ORDER BY s.weekday, s.start_time,"
        " sr.created_at").fetchall()


def expire_seat_requests(conn, now=None):
    """Close board posts whose session's next occurrence already
    started: the class is over, nobody can save a seat for it any
    more. Runs inside settle_expired, so every page load refreshes
    the board. The occurrence a post refers to is the next one from
    when it was POSTED — a post made after today's class started
    means next week's class, so its start is computed from
    created_at, not from now. Returns how many posts expired."""
    now = now or datetime.now()
    count = 0
    for row in conn.execute(
            "SELECT * FROM seat_requests WHERE status='open'").fetchall():
        session = get_session(conn, row["session_id"])
        if session is None:
            continue
        try:
            posted = datetime.fromisoformat(row["created_at"])
        except (ValueError, TypeError):
            continue  # A post with no usable timestamp stays open.
        start = next_session_start(dict(session), posted)
        if start is not None and now >= start:
            conn.execute("UPDATE seat_requests SET status='expired'"
                         " WHERE id=?", (row["id"],))
            count += 1
    if count:
        conn.commit()
    return count


def _fulfill_seat_requests(conn, user_id, session_id):
    """A board poster got their seat: mark their open post for this
    session fulfilled. Called from inside the lock, by request_seat,
    in the same commit as the request — the post and the seat can
    never disagree. Only the requester's own post is fulfilled."""
    conn.execute(
        "UPDATE seat_requests SET status='fulfilled'"
        " WHERE user_id=? AND session_id=? AND status='open'",
        (user_id, session_id))


def prepare_board_offer_payload(offer, course, session):
    """The push a board poster gets when a saver offers the session
    they asked for. Short, because a phone notification is one
    glance: the class you wanted now has an offer."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "מישהו מציע מקום לשיעור שביקשת",
        "body": "הצעה חדשה ב%s%s — לחץ לבקשה, מטבע אחד."
                % (course["name"], where),
        "url": "/offers",
        "tag": "seat-wanted-%s" % offer["id"],
    }


def board_requester_ids(conn, session_id):
    """User ids with an open board post for one session. Used by the
    follower notification: a poster gets the board message for an
    offer, not the follower message as well — one offer, one push."""
    if not session_id:
        return set()
    return {int(r["user_id"]) for r in conn.execute(
        "SELECT DISTINCT user_id FROM seat_requests"
        " WHERE session_id=? AND status='open'",
        (session_id,)).fetchall()}


def notify_board_requesters(conn, offer, sender=None, now=None):
    """When an offer is created: push to everyone with an open board
    post for this exact session. These users asked for this session
    by name — they are told even if they do not follow the course,
    because the post itself is the request. The saver is excluded:
    nobody needs a push about their own offer. Returns the log rows."""
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    payload = prepare_board_offer_payload(dict(offer), course, session)
    results = []
    for user_id in sorted(board_requester_ids(conn, offer["session_id"])):
        if user_id == int(offer["saver_id"]):
            continue
        results.extend(_push_to_user(conn, user_id, offer["id"],
                                     payload, sender, now))
    conn.commit()
    return results


@_serialized
def request_seat(conn, requester_id, offer_id, now=None, seats=1):
    """Spend coins (frozen, not yet transferred): 1 coin per seat, so
    a 2-seat request freezes 2. The requester's balance after this
    request may never go below MIN_BALANCE — the floor check uses the
    multiplied amount, so a requester at -1 can ask for 1 seat (lands
    on -2) but not for 2 (would land on -3)."""
    now = now or datetime.now()
    offer = get_offer(conn, offer_id)
    if offer is None:
        return None, "ההצעה לא נמצאה."
    if offer["status"] != "open":
        return None, "ההצעה הזו כבר לא פתוחה."
    if int(requester_id) == int(offer["saver_id"]):
        return None, "אי אפשר לבקש מקום שהצעת בעצמך."
    if seats is None:
        seats = 1
    try:
        seats = int(seats)
    except (TypeError, ValueError):
        return None, "מספר המקומות חייב להיות מספר."
    if seats < 1 or seats > MAX_REQUEST_SEATS:
        return None, ("אפשר לבקש 1 או %d מקומות בבקשה אחת."
                      % MAX_REQUEST_SEATS)
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    if session is not None:
        start = _session_start_datetime(offer, session)
        if now >= start:
            return None, "השיעור הזה כבר התחיל."
    if seats_available(conn, offer) < seats:
        return None, "לא נשארו מספיק מקומות בהצעה הזו."
    existing = conn.execute(
        "SELECT id FROM requests WHERE offer_id=? AND requester_id=?"
        " AND status='frozen'", (offer_id, requester_id)).fetchone()
    if existing:
        return None, "כבר ביקשת מקום בהצעה הזו."
    user = get_user(conn, requester_id)
    if user is None:
        return None, "משתמש לא ידוע."
    cost = REQUEST_COST * seats
    if user["balance"] - cost < MIN_BALANCE:
        if user["balance"] <= MIN_BALANCE:
            return None, ("המאזן שלך ברצפה (%d מטבעות) — שמור מקום למישהו "
                          "אחר קודם כדי להרוויח מטבע."
                          % MIN_BALANCE)
        return None, ("בקשה של %d מקומות עולה %d מטבעות — זה יוריד אותך "
                      "מתחת לרצפה (%d מטבעות)."
                      % (seats, cost, MIN_BALANCE))
    # Freeze: deduct now, refund if it does not complete. The saver is
    # paid only on arrival confirmation (see confirm_arrival).
    conn.execute("UPDATE users SET balance = balance - ? WHERE id=?",
                 (cost, requester_id))
    cur = conn.execute(
        "INSERT INTO requests (offer_id, requester_id, saver_id,"
        " seats_requested, status, created_at)"
        " VALUES (?, ?, ?, ?, 'frozen', ?)",
        (offer_id, requester_id, offer["saver_id"], seats, _now_text(now)))
    # Got the seat — no longer waiting for it. The queue entry is
    # removed in the same lock as the request, so a request and a
    # free-up notification can never disagree about who is waiting.
    conn.execute(
        "DELETE FROM waiting_list WHERE offer_id=? AND user_id=?",
        (offer_id, requester_id))
    # And a board post for this session is fulfilled by the same
    # request — the poster asked for a seat and got one.
    if offer["session_id"]:
        _fulfill_seat_requests(conn, requester_id, offer["session_id"])
    _log_coin(conn, requester_id, -cost, "frozen", cur.lastrowid, now)
    conn.commit()
    return conn.execute("SELECT * FROM requests WHERE id=?",
                        (cur.lastrowid,)).fetchone(), None


def _refund_request(conn, request, new_status, now=None, ledger_reason=None):
    """Return the frozen coins for one request. The amount is the
    request's own size: a 2-seat request returns 2 coins, not 1.
    The ledger reason defaults to the new status, except for admin
    decisions, which are marked as such (see admin_force_refund)."""
    amount = REQUEST_COST * _request_seats(request)
    conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                 (amount, request["requester_id"]))
    conn.execute(
        "UPDATE requests SET status=?, resolved_at=?,"
        " late_declared_at=NULL, late_status=NULL WHERE id=?",
        (new_status, _now_text(now), request["id"]))
    _log_coin(conn, request["requester_id"], amount,
              ledger_reason or new_status, request["id"], now)
    conn.commit()


@_serialized
def confirm_arrival(conn, request_id, user_id, now=None):
    """Requester arrived and the seat was saved: the frozen coins move
    to the saver — all of them, so a 2-seat request pays 2. Only the
    requester can confirm, only while frozen."""
    req = conn.execute("SELECT * FROM requests WHERE id=?",
                       (request_id,)).fetchone()
    if req is None:
        return "הבקשה לא נמצאה."
    if int(req["requester_id"]) != int(user_id):
        return "רק מי שביקש יכול לאשר הגעה."
    if req["status"] != "frozen":
        return "הבקשה הזו כבר טופלה."
    amount = REQUEST_COST * _request_seats(req)
    conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                 (amount, req["saver_id"]))
    _log_coin(conn, req["saver_id"], amount, "paid", req["id"], now)
    conn.execute(
        "UPDATE requests SET status='confirmed', resolved_at=? WHERE id=?",
        (_now_text(now), request_id))
    conn.commit()
    return None


def report_not_saved(conn, request_id, user_id, now=None, sender=None):
    """The seat was not actually there: coin returns, saver gets 0.
    If this frees the last seat of a fully booked offer, followers
    are told a seat freed up (see maybe_notify_seat_freed)."""
    # Check and refund under one lock: two taps at once must not
    # refund twice. The notification goes after the lock is released
    # (it does network I/O — see _MUTEX).
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return "הבקשה לא נמצאה."
        if int(req["requester_id"]) != int(user_id):
            return "רק מי שביקש יכול לדווח על זה."
        if req["status"] != "frozen":
            return "הבקשה הזו כבר טופלה."
        offer = get_offer(conn, req["offer_id"])
        seats_before = seats_available(conn, offer) if offer else 0
        _refund_request(conn, req, "not_saved", now)
    if offer is not None:
        # The requester just reported the seat was not there — they
        # do not need a "seat freed" push about it.
        maybe_notify_seat_freed(conn, offer["id"], seats_before, sender,
                                now, exclude_user_id=user_id)
    return None


# ---------------------------------------------------------------- round 20:
# complaints — "the seat was not actually saved", reviewed by Daniel.
COMPLAINTABLE_REQUEST_STATUSES = ("frozen", "confirmed", "no_show",
                                  "not_saved", "late_declined")


def file_complaint(conn, request_id, user_id, reason, now=None,
                   sender=None):
    """Round 20, item 14: the requester files a complaint that a
    seat was not actually saved — while the coin is frozen, or
    after the request resolved. One complaint per request (the
    table enforces it), so the same seat cannot be complained
    about twice. A complaint does NOT change the rating by itself:
    it only lands on /admin for Daniel to review (see
    decide_complaint). The admin gets a push, so a complaint is
    never silently waiting. Returns (complaint_row, error)."""
    # Check and insert under one lock: two taps at once must not
    # file twice. The notification goes after the lock is released
    # (it does network I/O — see _MUTEX).
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return None, "הבקשה לא נמצאה."
        if int(req["requester_id"]) != int(user_id):
            return None, "רק מי שביקש יכול להתלונן על הבקשה הזו."
        if req["status"] not in COMPLAINTABLE_REQUEST_STATUSES:
            return None, ("על בקשה שבוטלה אי אפשר להתלונן — המטבע כבר "
                          "חזר והמקום שוחרר.")
        reason = (reason or "").strip()
        if not reason:
            return None, "נא לכתוב בקצרה מה קרה."
        if len(reason) > 300:
            return None, "התלונה ארוכה מדי (עד 300 תווים)."
        existing = conn.execute(
            "SELECT * FROM complaints WHERE request_id=?",
            (request_id,)).fetchone()
        if existing is not None:
            return None, "כבר הוגשה תלונה על הבקשה הזו."
        cur = conn.execute(
            "INSERT INTO complaints (request_id, offer_id, requester_id,"
            " saver_id, reason, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (req["id"], req["offer_id"], req["requester_id"],
             req["saver_id"], reason, _now_text(now)))
        conn.commit()
        complaint = conn.execute("SELECT * FROM complaints WHERE id=?",
                                 (cur.lastrowid,)).fetchone()
    notify_admins_of_complaint(conn, complaint, sender, now)
    return complaint, None


def prepare_complaint_payload(complaint, requester_name, saver_name,
                              course_name):
    """The push the admin gets when a complaint is filed: who
    complained about whom, for which course. Short, because a
    phone notification is one glance. Same four fields sw.js reads."""
    return {
        "title": "תלונה חדשה",
        "body": "%s התלונן על %s (%s): המקום לא נשמר."
                % (requester_name, saver_name, course_name),
        "url": "/admin",
        "tag": "complaint-%s" % complaint["id"],
    }


def admin_user_ids(conn):
    """The ids of the admin accounts (by name, see ADMIN_NAMES).
    Used to notify the admin — complaints and broadcasts go to
    Daniel, and only Daniel is an admin."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE name IN (%s)"
        % ",".join("?" * len(ADMIN_NAMES)), ADMIN_NAMES).fetchall()]


def notify_admins_of_complaint(conn, complaint, sender=None, now=None):
    """Push the complaint to every admin (Daniel). The /admin list
    is the other channel — the push is so he does not have to keep
    checking the page. A failed push is logged, never raised: a
    complaint must never fail because a notification failed."""
    requester = get_user(conn, complaint["requester_id"])
    saver = get_user(conn, complaint["saver_id"])
    offer = get_offer(conn, complaint["offer_id"])
    course = get_course(conn, offer["course_id"]) if offer else None
    payload = prepare_complaint_payload(
        complaint,
        requester["name"] if requester else "מישהו",
        saver["name"] if saver else "מישהו",
        course["name"] if course else "")
    for admin_id in admin_user_ids(conn):
        _push_to_user(conn, admin_id, 0, payload, sender, now)
    conn.commit()


@_serialized
def decide_complaint(conn, complaint_id, admin_user, decision,
                     admin_note=None, now=None, sender=None):
    """Round 20, item 14: the admin decides a complaint. Admin
    only, server-enforced. 'upheld' means the saver really did not
    save the seat: if the request is still frozen, it is resolved
    as 'not_saved' (coin back to the requester, same as tapping
    "המקום לא היה שם"); the saver's reliability drops — either
    through that resolution, or, for an already-confirmed request,
    through the upheld complaint itself (see reliability). A
    'dismissed' complaint changes nothing: no coin moves, no
    rating moves. The admin's note is stored with the decision, so
    the reasoning is visible later. Returns an error string, or
    None on success."""
    if not is_admin(admin_user):
        return "רק דניאל יכול להכריע בתלונות."
    try:
        complaint_id = int(complaint_id)
    except (TypeError, ValueError):
        return "תלונה לא נמצאה."
    complaint = conn.execute("SELECT * FROM complaints WHERE id=?",
                             (complaint_id,)).fetchone()
    if complaint is None:
        return "תלונה לא נמצאה."
    if complaint["status"] != "open":
        return "התלונה הזו כבר הוכרעה."
    if decision not in ("upheld", "dismissed"):
        return "החלטה לא ידועה."
    if decision == "upheld":
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (complaint["request_id"],)).fetchone()
        if req is not None and req["status"] == "frozen":
            # The seat was not saved and the coin is still frozen:
            # give it back now, exactly like "המקום לא היה שם".
            _refund_request(conn, req, "not_saved", now)
    conn.execute(
        "UPDATE complaints SET status=?, admin_note=?, resolved_at=?"
        " WHERE id=?",
        (decision, (admin_note or "").strip()[:200], _now_text(now),
         complaint_id))
    conn.commit()
    return None


def cancel_request(conn, request_id, user_id, now=None, sender=None):
    """The requester changed their mind: the frozen coins come back
    immediately (no waiting for start + 5), and the seat is freed at
    once. Only the requester, only while frozen. If this frees the
    last seat of a fully booked offer, followers are told a seat
    freed up (see maybe_notify_seat_freed)."""
    # Check and refund under one lock: two taps at once must not
    # refund twice. The notification goes after the lock is released
    # (it does network I/O — see _MUTEX).
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return "הבקשה לא נמצאה."
        if int(req["requester_id"]) != int(user_id):
            return "רק מי שביקש יכול לבטל את הבקשה."
        if req["status"] != "frozen":
            return "הבקשה הזו כבר טופלה."
        offer = get_offer(conn, req["offer_id"])
        seats_before = seats_available(conn, offer) if offer else 0
        _refund_request(conn, req, "cancelled_by_requester", now)
    if offer is not None:
        # The requester just freed this seat themselves — they do not
        # need a "seat freed" push about it.
        maybe_notify_seat_freed(conn, offer["id"], seats_before, sender,
                                now, exclude_user_id=user_id)
        # And the saver needs to know the request is gone: they kept
        # a seat for someone who is no longer coming.
        notify_saver_of_cancel(conn, req, sender, now)
    return None


def cancel_request_by_saver(conn, request_id, saver_id, now=None,
                            sender=None):
    """The saver cancels ONE request, not the whole offer: the frozen
    coins come back to that requester immediately, the seat frees up,
    and the offer STAYS OPEN — if it was full, the free-up path runs
    exactly like a requester-cancel (waiting list first, then
    followers, only on 0→1). Only the saver of the offer, only while
    the request is frozen. The cancelled requester gets a push, so
    they know to look for another seat. A second cancel of the same
    request is rejected — the frozen-only rule plus the lock mean the
    refund can happen exactly once."""
    # Check and refund under one lock: two taps at once must not
    # refund twice. The notifications go after the lock is released
    # (they do network I/O — see _MUTEX).
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return "הבקשה לא נמצאה."
        offer = get_offer(conn, req["offer_id"])
        if offer is None or int(offer["saver_id"]) != int(saver_id):
            return "רק השומר יכול לבטל בקשה בהצעה הזו."
        if req["status"] != "frozen":
            return "הבקשה הזו כבר טופלה."
        seats_before = seats_available(conn, offer)
        _refund_request(conn, req, "cancelled_by_saver", now)
    # The requester just lost their seat — they do not need a "seat
    # freed" push about it, but they do need to know it was cancelled.
    maybe_notify_seat_freed(conn, offer["id"], seats_before, sender,
                            now, exclude_user_id=req["requester_id"])
    notify_requester_of_saver_cancel(conn, req, sender, now)
    return None


def cancel_offer(conn, offer_id, saver_id, now=None, sender=None):
    """Saver cancels before the requester arrives: every frozen coin
    returns to its requester, saver gets 0. The frozen requesters
    are also told the offer was cancelled - without a push they
    might still walk to class expecting a seat that no longer
    exists. The offer itself closes, so there is no seat to notify
    about - a cancelled offer has nothing free to offer. The
    free-up check still runs: the offer is closed by then, so it
    stays silent by design."""
    # Check, refunds and close under one lock, so two taps cannot
    # refund the same frozen requests twice. The free-up check runs
    # after the lock is released (see _MUTEX) - it stays silent here
    # anyway, because the offer is closed by then.
    with _MUTEX:
        offer = get_offer(conn, offer_id)
        if offer is None:
            return "ההצעה לא נמצאה."
        if int(offer["saver_id"]) != int(saver_id):
            return "רק השומר יכול לבטל את ההצעה הזו."
        if offer["status"] != "open":
            return "ההצעה הזו כבר סגורה."
        seats_before = seats_available(conn, offer)
        # Who held a frozen request when the offer was cancelled:
        # they are refunded below and told about the cancel after
        # the lock is released. Captured here, because the refunds
        # change their status and the rows would be gone afterwards.
        # One id per requester - a 2-seat request is one push, not two.
        frozen_ids = [r["requester_id"] for r in conn.execute(
            "SELECT DISTINCT requester_id FROM requests"
            " WHERE offer_id=? AND status='frozen'",
            (offer_id,))]
        for req in conn.execute(
                "SELECT * FROM requests WHERE offer_id=? AND status='frozen'",
                (offer_id,)):
            _refund_request(conn, req, "cancelled", now)
        conn.execute("UPDATE offers SET status='cancelled' WHERE id=?",
                     (offer_id,))
        # The offer is gone — its waiting list goes with it. Capture
        # who was waiting first: they are told the offer was
        # cancelled, after the lock is released (see below).
        waiting_ids = [u["id"] for u in
                       waiting_users_for_offer(conn, offer_id)]
        _delete_waiting_entries_for_offer(conn, offer_id)
        conn.commit()
    # A user who is both a frozen requester and a waiter gets exactly
    # one push - the waiting-list notice. The frozen list is filtered
    # to exclude waiters (and the saver, who never gets a push about
    # their own offer) before anyone is notified.
    frozen_to_notify = [uid for uid in frozen_ids
                        if int(uid) not in [int(w) for w in waiting_ids]
                        and int(uid) != int(offer["saver_id"])]
    if frozen_to_notify:
        notify_frozen_requesters_of_cancel(conn, dict(offer),
                                           frozen_to_notify, sender, now)
    if waiting_ids:
        notify_waiting_users_of_cancel(conn, dict(offer), waiting_ids,
                                       sender, now)
    maybe_notify_seat_freed(conn, offer_id, seats_before, sender, now)
    return None


def declare_late(conn, request_id, user_id, now=None, sender=None):
    """Requester says "I'm late": the no-show deadline moves from
    start+5 to start+10, and the saver is told (one push, plus the
    decision buttons on /my) so they can accept or refuse. Only the
    requester, only while frozen, only once per request, and only
    before start+5 — after that the normal expiry has already
    fired. Independent of "on my way": a requester can tap both,
    and neither one blocks the other. The stamp runs under the
    write lock (it is a state change on a frozen request, the same
    lock as confirm_arrival); the push goes after the lock, because
    it does network I/O."""
    now = now or datetime.now()
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return "הבקשה לא נמצאה."
        if int(req["requester_id"]) != int(user_id):
            return "רק מי שביקש יכול להודיע על איחור."
        if req["status"] != "frozen":
            return "הבקשה הזו כבר טופלה."
        try:
            declared = req["late_declared_at"]
        except (KeyError, IndexError, TypeError):
            declared = None
        if declared:
            return "כבר הודעת על איחור בבקשה הזו."
        offer = get_offer(conn, req["offer_id"])
        session = (get_session(conn, offer["session_id"])
                   if offer is not None and offer["session_id"] else None)
        if offer is None or session is None:
            return "לבקשה הזו אין שיעור, אי אפשר להודיע על איחור."
        start = _session_start_datetime(offer, session)
        if now >= start + timedelta(minutes=CONFIRM_GRACE_MINUTES):
            return "מאוחר מדי להודיע על איחור — חלון ה־5 דקות עבר."
        conn.execute(
            "UPDATE requests SET late_declared_at=?, late_status='pending'"
            " WHERE id=?", (_now_text(now), request_id))
        conn.commit()
    # Outside the lock: tell the saver the requester is late.
    notify_saver_of_late(conn, dict(req), sender, now)
    return None


def decide_late(conn, request_id, saver_id, decision, now=None,
                sender=None):
    """The saver answers a late declaration. Accept: the deadline
    stays at start+10 (it moved there when the declaration was
    made) and the coin stays frozen — if the requester confirms
    arrival by start+10 the saver is paid as normal. Refuse: the
    coin is refunded at once, the seat is freed, status
    late_declined. Only the saver of the offer, only while the
    request is frozen with a pending declaration — a second
    decision is rejected, the declaration is one-shot. If the
    decision comes after the deadline itself, the request has
    already expired: it is refunded as a no-show instead, so an
    accept can never revive a request whose time is up. The
    refund, when there is one, runs under the write lock; the
    free-up check goes after the lock, because it sends pushes."""
    now = now or datetime.now()
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return "הבקשה לא נמצאה."
        offer = get_offer(conn, req["offer_id"])
        if offer is None or int(offer["saver_id"]) != int(saver_id):
            return "רק השומר יכול להחליט על איחור בהצעה הזו."
        if req["status"] != "frozen":
            return "הבקשה הזו כבר טופלה."
        try:
            late = req["late_status"]
        except (KeyError, IndexError, TypeError):
            late = None
        if late != "pending":
            return "אין הודעת איחור ממתינה בבקשה הזו."
        session = (get_session(conn, offer["session_id"])
                   if offer["session_id"] else None)
        if session is not None and \
                now >= _no_show_deadline(offer, session, req):
            _refund_request(conn, req, "no_show", now)
            return "הבקשה הזו כבר פגה — חלון האיחור עבר."
        if decision == "accept":
            conn.execute("UPDATE requests SET late_status='accepted'"
                         " WHERE id=?", (request_id,))
            conn.commit()
            return None
        if decision == "refuse":
            seats_before = seats_available(conn, offer)
            _refund_request(conn, req, "late_declined", now)
        else:
            return "החלטה לא ידועה."
    if decision == "refuse":
        # The requester just lost their seat — they do not need a
        # "seat freed" push about it. The offer may still be open
        # (declared before class start), so the free-up path runs
        # when a seat actually freed up; on a closed offer it stays
        # silent by design.
        maybe_notify_seat_freed(conn, offer["id"], seats_before, sender,
                                now, exclude_user_id=req["requester_id"])
    return None


def mark_on_my_way(conn, request_id, user_id, now=None, sender=None):
    """Requester is on their way to the class. Informational only:
    it does NOT change the start+5-min refund rule or any coin rule —
    a requester who taps it and then does not arrive still gets a
    no-show refund at start+5. Only the requester, only while frozen.
    Tapping twice is harmless — it just updates the time, and sends
    another push to the saver (the requester is still coming). The
    saver gets the push: before this they only saw it on the /my
    page, with no push — and a saver who is not looking at the app
    would not know to keep holding the seat. The stamp runs under
    the write lock (it is a state change on a frozen request, the
    same lock as confirm_arrival); the push goes after the lock,
    because it does network I/O."""
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return "הבקשה לא נמצאה."
        if int(req["requester_id"]) != int(user_id):
            return "רק מי שביקש יכול לסמן שהוא בדרך."
        if req["status"] != "frozen":
            return "הבקשה הזו כבר טופלה."
        conn.execute("UPDATE requests SET on_the_way=? WHERE id=?",
                     (_now_text(now), request_id))
        conn.commit()
    # Outside the lock: tell the saver the requester is on the way.
    notify_saver_of_on_the_way(conn, dict(req), sender, now)
    return None


@_serialized
def re_offer(conn, old_offer_id, saver_id, now=None):
    """One-tap re-offer: a new offer for the SAME session with the
    SAME note/seats as an old one. The weekly lesson recurs, so
    the saver should not fill the form from scratch. The new offer
    goes through exactly the same validation as a fresh one (60-min
    window, today only, no duplicate open offer), so a re-offer
    outside the window or for a session that is not today is rejected
    with the same Hebrew error. Returns (offer, error)."""
    old = get_offer(conn, old_offer_id)
    if old is None:
        return None, "ההצעה לא נמצאה."
    if int(old["saver_id"]) != int(saver_id):
        return None, "רק השומר יכול להציע שוב."
    return create_offer(conn, saver_id, old["session_id"],
                        old["note"], old["seats_total"], now=now)


@_serialized
def mark_arrived_in_class(conn, offer_id, saver_id, now=None):
    """The saver is physically in the classroom: stamp arrived_at on
    their open offer, so the card shows "השומר כבר בכיתה (HH:MM)".
    Different from the offer itself — the offer is a promise made up
    to 60 minutes ahead ("I will be there"), this is "I am here
    now". Informational only: it changes nothing about seats, coins
    or the arrival-confirmation flow. Only the saver, only while the
    offer is open. Tapping again just updates the time."""
    offer = get_offer(conn, offer_id)
    if offer is None:
        return "ההצעה לא נמצאה."
    if int(offer["saver_id"]) != int(saver_id):
        return "רק השומר יכול לסמן שהוא כבר בכיתה."
    if offer["status"] != "open":
        return "ההצעה הזו כבר לא פתוחה."
    conn.execute("UPDATE offers SET arrived_at=? WHERE id=?",
                 (_now_text(now), offer_id))
    conn.commit()
    return None


def edit_offer(conn, offer_id, saver_id, note, seats, now=None,
               sender=None):
    """Edit an open offer: the note freely, the seat
    count with one limit — it can go up to 5, but it can only come
    down to the number of seats already frozen by requests. Below
    that, the offer would promise less than it already sold, so the
    edit is rejected and nothing changes. Only the saver, only while
    the offer is open. Followers are NOT re-notified: it is the same
    offer, not a new one — except when the edit frees a seat that
    was not free before (a full offer raised), where the existing
    free-up path runs (waiting list first), exactly as a cancel does.
    Frozen requesters ARE told when the note changes:
    the seat they asked for just changed, and they should know
    before they walk to class. A seats-only change tells them
    nothing, so it stays silent. Returns an error string, or None
    on success."""
    with _MUTEX:
        offer = get_offer(conn, offer_id)
        if offer is None:
            return "ההצעה לא נמצאה."
        if int(offer["saver_id"]) != int(saver_id):
            return "רק השומר יכול לערוך את ההצעה הזו."
        if offer["status"] != "open":
            return "ההצעה הזו כבר לא פתוחה."
        try:
            seats = int(seats)
        except (TypeError, ValueError):
            return "מספר המקומות חייב להיות מספר."
        if seats < 1 or seats > MAX_SEATS_PER_OFFER:
            return "אפשר להציע עד %d מקומות." % MAX_SEATS_PER_OFFER
        occupied = offer["seats_total"] - seats_available(conn, offer)
        if seats < occupied:
            return "אי אפשר להוריד מתחת ל־%d מקומות שכבר תפוסים." \
                % occupied
        seats_before = seats_available(conn, offer)
        old_note = offer["note"] or ""
        new_note = (note or "").strip()[:120]
        conn.execute(
            "UPDATE offers SET note=?, seats_total=? WHERE id=?",
            (new_note, seats, offer_id))
        conn.commit()
    # Outside the lock: the notifications do network I/O. The
    # free-up notification does not change: the 0→1 rule lives in
    # maybe_notify_seat_freed, so passing seats_before is enough —
    # when it was not full, nothing fires. The edit notification
    # fires only when the note actually changed — a
    # seats-only edit, or an edit that changes nothing, stays
    # silent for the frozen requesters.
    if seats_before == 0:
        maybe_notify_seat_freed(conn, offer_id, seats_before, sender, now)
    if new_note != old_note:
        notify_frozen_requesters_of_edit(conn, dict(offer), new_note,
                                         sender, now)
    return None


def admin_stuck_requests(conn, now=None):
    """Requests Daniel may need to decide on: frozen ones whose class
    already started, plus requests resolved in the last 7 days
    (confirmed / not_saved) — the disputes. Anything older is
    history, not a decision. Returns rows with course, requester and
    saver names attached."""
    now = now or datetime.now()
    week_ago = now - timedelta(days=7)
    out = []
    for r in conn.execute(
            "SELECT r.*, o.class_date, o.session_id, c.name AS course_name,"
            " u1.name AS requester_name, u2.name AS saver_name"
            " FROM requests r JOIN offers o ON o.id = r.offer_id"
            " JOIN courses c ON c.id = o.course_id"
            " JOIN users u1 ON u1.id = r.requester_id"
            " JOIN users u2 ON u2.id = r.saver_id"
            " WHERE r.status IN ('frozen', 'confirmed', 'not_saved')"
            " ORDER BY r.created_at DESC").fetchall():
        if r["status"] == "frozen":
            session = get_session(conn, r["session_id"]) \
                if r["session_id"] else None
            if session is None:
                continue
            start = _session_start_datetime(dict(r), dict(session))
            if now < start:
                continue
        else:
            try:
                resolved = datetime.fromisoformat(r["resolved_at"])
            except (ValueError, TypeError):
                continue
            if resolved < week_ago:
                continue
        out.append(r)
    return out


def admin_open_offers(conn):
    """Every open offer right now, with saver, course and session
    names attached — the top half of the admin dashboard. Read-only:
    the dashboard shows, it does not decide anything."""
    return conn.execute(
        "SELECT o.*, c.name AS course_name, u.name AS saver_name, u.verified AS saver_verified"
        " FROM offers o JOIN courses c ON c.id = o.course_id"
        " JOIN users u ON u.id = o.saver_id"
        " WHERE o.status='open' ORDER BY o.created_at").fetchall()


def admin_frozen_requests(conn):
    """Every frozen request right now, with requester, saver, course
    and session names attached — the bottom half of the admin
    dashboard. Read-only; disputes are decided on /my, not here."""
    return conn.execute(
        "SELECT r.*, o.class_date, o.session_id, c.name AS course_name,"
        " u1.name AS requester_name, u1.verified AS requester_verified,"
        " u2.name AS saver_name, u2.verified AS saver_verified"
        " FROM requests r JOIN offers o ON o.id = r.offer_id"
        " JOIN courses c ON c.id = o.course_id"
        " JOIN users u1 ON u1.id = r.requester_id"
        " JOIN users u2 ON u2.id = r.saver_id"
        " WHERE r.status='frozen' ORDER BY r.created_at").fetchall()


def _frozen_duration_text(created_at, now=None):
    """How long a request has been frozen, in plain words: 'לפני 5
    דקות', 'לפני שעתיים'. A bad timestamp is data, not a crash —
    it shows nothing instead of breaking the page."""
    if not created_at:
        return ""
    try:
        created = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return ""
    minutes = int(((now or datetime.now()) - created).total_seconds() // 60)
    if minutes < 1:
        return "ממש עכשיו"
    if minutes < 60:
        return "לפני %d דקות" % minutes
    hours = minutes // 60
    if hours == 1:
        return "לפני שעה"
    if hours == 2:
        return "לפני שעתיים"
    return "לפני %d שעות" % hours


@_serialized
def admin_force_confirm(conn, request_id, admin_user, now=None):
    """Daniel decides a stuck request: the seat WAS saved, pay the
    saver. For a frozen request the held coins move to the saver.
    For a request already refunded (no-show / not saved / cancelled),
    the coins are taken back from the requester first — otherwise the
    saver would be paid twice over. An already confirmed request
    needs no decision. Only Daniel; the server enforces it, not
    just the UI. The admin override can move a balance below the
    floor — a manual decision is not blocked by the floor rule."""
    if not is_admin(admin_user):
        return "אין לך הרשאה לפעולה הזו."
    req = conn.execute("SELECT * FROM requests WHERE id=?",
                       (request_id,)).fetchone()
    if req is None:
        return "הבקשה לא נמצאה."
    if req["status"] == "confirmed":
        return "הבקשה כבר אושרה — אין מה להכריע."
    amount = REQUEST_COST * _request_seats(req)
    if req["status"] != "frozen":
        conn.execute("UPDATE users SET balance = balance - ? WHERE id=?",
                     (amount, req["requester_id"]))
        _log_coin(conn, req["requester_id"], -amount, "admin_paid",
                  req["id"], now)
    conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                 (amount, req["saver_id"]))
    _log_coin(conn, req["saver_id"], amount, "admin_paid", req["id"], now)
    conn.execute(
        "UPDATE requests SET status='confirmed', resolved_at=? WHERE id=?",
        (_now_text(now), request_id))
    conn.commit()
    return None


def admin_force_refund(conn, request_id, admin_user, now=None, sender=None):
    """Daniel decides a stuck request: the seat was NOT saved, return
    the coin. For a frozen request the held coins go back to the
    requester. For a confirmed request the coins are taken back from
    the saver first. A request already refunded needs no decision.
    Only Daniel; the server enforces it. Frees the seat, so the
    free-up check runs too (see maybe_notify_seat_freed)."""
    if not is_admin(admin_user):
        return "אין לך הרשאה לפעולה הזו."
    # Check and move coins under one lock: two decisions at once must
    # not move the same coin twice. The notification goes after the
    # lock is released (it does network I/O — see _MUTEX).
    with _MUTEX:
        req = conn.execute("SELECT * FROM requests WHERE id=?",
                           (request_id,)).fetchone()
        if req is None:
            return "הבקשה לא נמצאה."
        if req["status"] in ("no_show", "not_saved", "cancelled",
                             "cancelled_by_requester", "refunded_by_admin"):
            return "המטבע כבר הוחזר למבקש."
        amount = REQUEST_COST * _request_seats(req)
        offer = get_offer(conn, req["offer_id"])
        seats_before = seats_available(conn, offer) if offer else 0
        if req["status"] == "confirmed":
            conn.execute("UPDATE users SET balance = balance - ? WHERE id=?",
                         (amount, req["saver_id"]))
            _log_coin(conn, req["saver_id"], -amount, "refunded_by_admin",
                      req["id"], now)
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                     (amount, req["requester_id"]))
        _log_coin(conn, req["requester_id"], amount, "refunded_by_admin",
                  req["id"], now)
        conn.execute(
            "UPDATE requests SET status='refunded_by_admin', resolved_at=?"
            " WHERE id=?", (_now_text(now), request_id))
        conn.commit()
    if offer is not None:
        # The requester just got their coin back — they do not need a
        # "seat freed" push about it.
        maybe_notify_seat_freed(conn, offer["id"], seats_before, sender,
                                now, exclude_user_id=req["requester_id"])
    return None


# ---------------------------------------------------------------- standing auto-requests
# A standing weekly request: a user says "request a seat for this
# session every week", and the background thread requests one seat
# when an open offer for the session exists. The request goes
# through request_seat itself — same floor check, same duplicate
# check, same freeze — so there is no second request path
# to drift. The user is always told with a push (see
# prepare_auto_request_payload): an automatic request nobody knows
# about would send nobody to class to confirm arrival.
@_serialized
def create_auto_request(conn, user_id, session_id, now=None):
    """A standing weekly request: every week, when this session's
    60-minute window opens and an open offer for it exists, one seat
    is requested automatically (see auto_request_tick). One per user
    + session. Returns (row, error). The validation is the static
    part — the window, the offer and the floor are checked at request
    time by the background thread, not here: the user sets this up
    days in advance, when the window is obviously closed."""
    user = get_user(conn, user_id)
    if user is None:
        return None, "משתמש לא ידוע."
    session = get_session(conn, session_id)
    if session is None:
        return None, "שיעור לא ידוע."
    try:
        cur = conn.execute(
            "INSERT INTO auto_requests (user_id, session_id, created_at)"
            " VALUES (?, ?, ?)",
            (user_id, session_id, _now_text(now)))
        conn.commit()
    except sqlite3.IntegrityError:
        return None, "כבר יש בקשה אוטומטית לשיעור הזה."
    return conn.execute("SELECT * FROM auto_requests WHERE id=?",
                        (cur.lastrowid,)).fetchone(), None


@_serialized
def delete_auto_request(conn, auto_id, user_id):
    row = conn.execute("SELECT * FROM auto_requests WHERE id=?",
                       (auto_id,)).fetchone()
    if row is None or int(row["user_id"]) != int(user_id):
        return "הבקשה האוטומטית לא נמצאה."
    conn.execute("DELETE FROM auto_requests WHERE id=?", (auto_id,))
    conn.commit()
    return None


def auto_requests_due(conn, now=None):
    """Standing requests whose turn it is right now: the session is
    today, its 60-minute window is open, an open offer for it exists
    with a free seat (not the user's own offer), and the user has no
    frozen request for that session today. Returns (auto_requests
    row, offer) pairs — the offer is the earliest created one with a
    free seat, so the choice is deterministic. Vacation users are
    skipped, like automatic offers: vacation means out of the loop."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    due = []
    for row in conn.execute("SELECT * FROM auto_requests").fetchall():
        session = get_session(conn, row["session_id"])
        if session is None:
            continue
        user = get_user(conn, row["user_id"])
        if user is None:
            continue
        if is_on_vacation(user):
            continue
        if int(session["weekday"]) != now.weekday():
            continue
        if session_window_status(dict(session), now)[0] != "open":
            continue
        # A frozen request blocks a second one — and so does a request
        # the user or the saver already cancelled today: a manual
        # cancel is a decision ("I don't want this seat"), and the
        # thread must not undo it a minute later by re-freezing the
        # coin. A request refunded because the whole offer was
        # cancelled ('cancelled') does NOT block: the offer is gone,
        # the user still wants a seat, and another offer may have one.
        existing = conn.execute(
            "SELECT r.id FROM requests r JOIN offers o ON o.id = r.offer_id"
            " WHERE r.requester_id=? AND o.session_id=? AND o.class_date=?"
            " AND r.status IN ('frozen', 'cancelled_by_requester',"
            " 'cancelled_by_saver')",
            (row["user_id"], row["session_id"], today)).fetchone()
        if existing:
            continue
        offer = None
        for cand in conn.execute(
                "SELECT * FROM offers WHERE session_id=? AND class_date=?"
                " AND status='open' AND saver_id != ?"
                " ORDER BY created_at, id",
                (row["session_id"], today, row["user_id"])).fetchall():
            if seats_available(conn, cand) >= 1:
                offer = cand
                break
        if offer is None:
            continue
        due.append((row, offer))
    return due


def prepare_auto_request_payload(offer, course, session):
    """The push the user gets when the thread requested a seat for
    them: without it they would never know to go to class and confirm
    arrival. Short, because a phone notification is one glance."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "ביקשתי בשבילך מקום",
        "body": "ביקשתי מקום בשבילך ב%s%s. המטבע הוקפא — לך לשיעור "
                "ואשר הגעה כשתגיע." % (course["name"], where),
        "url": "/my",
        "tag": "seat-auto-request-%s" % offer["id"],
    }


def auto_request_tick(conn, sender=None, now=None):
    """One automatic-request pass: for every due standing request,
    request exactly 1 seat through request_seat itself — same floor
    check, same duplicate check, same freeze — and tell the user with
    a push. A request that fails (floor, offer filled in the same
    minute) is simply skipped this minute and retried next minute:
    a balance can change when the user saves a seat. Returns how many
    seats were requested."""
    now = now or datetime.now()
    requested = 0
    for row, offer in auto_requests_due(conn, now):
        req, err = request_seat(conn, row["user_id"], offer["id"],
                                now=now, seats=1)
        if err is None and req is not None:
            course = get_course(conn, offer["course_id"])
            session = get_session(conn, offer["session_id"]) \
                if offer["session_id"] else None
            payload = prepare_auto_request_payload(
                dict(offer), course, session)
            _push_to_user(conn, row["user_id"], offer["id"], payload,
                          sender, now)
            # Commit the log row here, like the reminder ticks do:
            # the background thread closes its connection without
            # committing, so a row left uncommitted is rolled back
            # and lost — the request itself survives (request_seat
            # commits), but the record that the user was told would
            # vanish. Caught live: the auto-request worked, yet no
            # notification_log row for it existed afterwards.
            conn.commit()
            requested += 1
    return requested


# ---------------------------------------------------------------- offer templates
# A template is a saved note/seats for one recurring session:
# "every Monday חדו״א 2, row 3". One per user
# per session. A template NEVER creates anything
# by itself — it only prefills the offer form, and the saver still
# taps once to offer. The offer itself goes through the same
# validation as any fresh offer.
@_serialized
def save_offer_template(conn, user_id, session_id, note, seats,
                        now=None):
    """Save (or update — one template per user per session) a template
    for a session. Returns (row, error)."""
    if get_user(conn, user_id) is None:
        return None, "משתמש לא ידוע."
    session = get_session(conn, session_id)
    if session is None:
        return None, "שיעור לא ידוע."
    try:
        seats = int(seats)
    except (TypeError, ValueError):
        return None, "מספר המקומות חייב להיות מספר."
    if seats < 1 or seats > MAX_SEATS_PER_OFFER:
        return None, ("אפשר להציע עד %d מקומות."
                      % MAX_SEATS_PER_OFFER)
    existing = conn.execute(
        "SELECT id FROM offer_templates WHERE user_id=? AND session_id=?",
        (user_id, session_id)).fetchone()
    note = (note or "").strip()[:120]
    if existing:
        conn.execute(
            "UPDATE offer_templates SET note=?, seats=?"
            " WHERE id=?", (note, seats, existing["id"]))
        conn.commit()
        return conn.execute("SELECT * FROM offer_templates WHERE id=?",
                            (existing["id"],)).fetchone(), None
    try:
        cur = conn.execute(
            "INSERT INTO offer_templates (user_id, session_id,"
            " note, seats, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, session_id, note, seats, _now_text(now)))
        conn.commit()
    except sqlite3.IntegrityError:
        return None, "כבר יש תבנית לשיעור הזה."
    return conn.execute("SELECT * FROM offer_templates WHERE id=?",
                        (cur.lastrowid,)).fetchone(), None


@_serialized
def delete_offer_template(conn, template_id, user_id):
    """Delete a template. Only the owner — a template is personal."""
    row = conn.execute("SELECT * FROM offer_templates WHERE id=?",
                       (template_id,)).fetchone()
    if row is None or int(row["user_id"]) != int(user_id):
        return "התבנית לא נמצאה."
    conn.execute("DELETE FROM offer_templates WHERE id=?", (template_id,))
    conn.commit()
    return None


def get_template_for_session(conn, user_id, session_id):
    """The user's template for one session, or None."""
    return conn.execute(
        "SELECT * FROM offer_templates WHERE user_id=? AND session_id=?",
        (user_id, session_id)).fetchone()


def user_templates(conn, user_id):
    """All of one user's templates, with session and course names
    attached, in a stable display order."""
    return conn.execute(
        "SELECT t.*, s.weekday, s.start_time, s.end_time, s.building,"
        " s.room, s.teacher, c.name AS course_name FROM offer_templates t"
        " JOIN course_sessions s ON s.id = t.session_id"
        " JOIN courses c ON c.id = s.course_id WHERE t.user_id=?"
        " ORDER BY c.name, s.weekday, s.start_time",
        (user_id,)).fetchall()


@_serialized
def _no_show_deadline(offer, session, request):
    """The moment a frozen request is refunded as a no-show: class
    start + 10 minutes when a late declaration is pending or was
    accepted, otherwise class start + 5. A pending declaration
    PAUSES the +5 expiry — the request stays frozen until +10, so
    the saver still has time to decide and the requester still has
    time to arrive. A refusal resolves the request at once, so it
    never reaches a deadline."""
    start = _session_start_datetime(offer, session)
    try:
        late = request["late_status"]
    except (KeyError, IndexError, TypeError):
        late = None
    if late in ("pending", "accepted"):
        return start + timedelta(minutes=LATE_GRACE_MINUTES)
    return start + timedelta(minutes=CONFIRM_GRACE_MINUTES)


def settle_expired(conn, now=None, sender=None):
    """Close what time has closed: offers past class start expire, and
    frozen requests past their no-show deadline are refunded (the
    seat is freed). The deadline is per request — start + 5 minutes
    normally, start + 10 when a late declaration is pending or was
    accepted (see _no_show_deadline). Returns (offers_expired,
    requests_refunded). Call on every page that lists offers, so the
    list is never stale. A no-show refund frees a seat in an offer
    that already expired at class start, so the free-up check stays
    silent there by design (see maybe_notify_seat_freed) — it still
    runs, for the same reason."""
    now = now or datetime.now()
    expired_offers = 0
    refunded = 0
    # Board posts whose session already started are over too.
    expire_seat_requests(conn, now)
    # Close and refund under the write lock: two page loads at once
    # must not refund the same request twice, and a declare-late
    # tap racing the expiry must resolve to exactly one outcome —
    # either the declaration lands first (the deadline moves to
    # +10, so this pass refunds nothing) or the refund lands first
    # (the declaration is then rejected — the request is no longer
    # frozen). The lock is reentrant, so callers that already hold
    # it are fine. The free-up notifications go after the lock is
    # released (they do network I/O — see _MUTEX).
    freed = []
    with _MUTEX:
        # Open AND already-expired offers: an offer expires at class
        # start, but its frozen requests are only refunded at their
        # deadline (start+5, or start+10 after a late declaration).
        # A pass at start+3 expires the offer; the refund is due on
        # a LATER pass — so expired offers must be revisited here,
        # or the coin would stay frozen forever. Cancelled offers
        # cannot hold frozen requests (cancel refunds them all at
        # once), so they are not listed.
        for offer in conn.execute(
                "SELECT * FROM offers"
                " WHERE status IN ('open', 'expired')").fetchall():
            session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
            if session is None:
                continue
            start = _session_start_datetime(offer, session)
            if offer["status"] == "open" and now >= start:
                conn.execute("UPDATE offers SET status='expired' WHERE id=?",
                             (offer["id"],))
                # The class started — its waiting list goes with it.
                _delete_waiting_entries_for_offer(conn, offer["id"])
                expired_offers += 1
            seats_before = seats_available(conn, offer)
            refunded_here = 0
            for req in conn.execute(
                    "SELECT * FROM requests WHERE offer_id=? AND"
                    " status='frozen'", (offer["id"],)):
                if now >= _no_show_deadline(offer, session, req):
                    _refund_request(conn, req, "no_show", now)
                    refunded += 1
                    refunded_here += 1
            if refunded_here:
                freed.append((offer["id"], seats_before))
        conn.commit()
    for offer_id, seats_before in freed:
        maybe_notify_seat_freed(conn, offer_id, seats_before,
                                sender, now)
    return expired_offers, refunded

# ---------------------------------------------------------------- Web Push
# Why a library, in one paragraph (full version in EXPLANATION.md):
# Web Push encrypts each notification with ECDH (P-256) + AES-128-GCM.
# Python's standard library has no elliptic-curve crypto and no AES-GCM,
# so implementing it by hand would mean inventing crypto — never sensible.
# pywebpush does only that one job: it takes a subscription + a payload
# and sends it encrypted to the browser vendor's push service.

def _load_vapid_keys():
    """Returns (private_key, public_key) as urlsafe-base64 strings, or
    (None, None) if no keys exist yet. Keys live in vapid_keys.json next
    to app.py — that file is gitignored and NEVER shipped or committed,
    because the private key is a secret. The public key is safe to share:
    the browser needs it to subscribe."""
    if os.path.exists(VAPID_KEYS_PATH):
        try:
            with open(VAPID_KEYS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("private_key"), data.get("public_key")
        except (OSError, ValueError, AttributeError):
            return None, None
    return None, None


def _decode_b64url(text):
    """Decode a urlsafe-base64 string without padding; None when the
    text is not valid base64 at all. Never raises — a corrupt key
    file is data, not a crash."""
    if not isinstance(text, str):
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return None


def _save_vapid_keys(priv, pub):
    """Write vapid_keys.json (the private key stays secret: 0600)."""
    with open(VAPID_KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump({"private_key": priv, "public_key": pub}, f)
    try:
        os.chmod(VAPID_KEYS_PATH, 0o600)
    except OSError:
        pass


def generate_vapid_keys():
    """Generate a fresh VAPID pair and save it to vapid_keys.json.
    Needs the `cryptography` library (installed with pywebpush).
    Returns (private_key, public_key). Raises RuntimeError if the
    library is missing — push then stays disabled with a clear message.
    The public key is the full uncompressed EC point: 0x04 || X ||
    Y, 65 bytes. The 0x04 byte marks the point as uncompressed —
    without it the browser cannot tell what the bytes are and
    rejects the key at subscribe time ("applicationServerKey is not
    valid"). An earlier build stored X||Y only (64 bytes); see
    get_vapid_keys for the repair of those files."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        raise RuntimeError(
            "cryptography is not installed — run: pip install -r requirements.txt")
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.private_numbers()
    priv = _b64url(numbers.private_value.to_bytes(32, "big"))
    pub_numbers = key.public_key().public_numbers()
    pub = _b64url(b"\x04" + pub_numbers.x.to_bytes(32, "big")
                   + pub_numbers.y.to_bytes(32, "big"))
    _save_vapid_keys(priv, pub)
    return priv, pub


def get_vapid_keys():
    """Load existing keys, or generate a pair on first run if possible.
    If generation is impossible (no cryptography lib), returns
    (None, None) and push is disabled — the app still works, it just
    cannot send notifications, and the UI says so.
    Repair of pre-fix key files: a file written before the 0x04 fix
    stores the public key as X||Y (64 bytes) instead of the full
    uncompressed point (65 bytes starting with 0x04). Such a file is
    repaired here on load — same private key, public key rewritten
    with the missing byte, file saved. Nobody has to delete anything
    by hand. No push subscription exists yet for the old key (the
    browser rejected it at subscribe time), so nothing else breaks.
    A stored public key that is missing, undecodable, or any other
    length is not repairable — a fresh pair is generated instead."""
    priv, pub = _load_vapid_keys()
    if priv and pub:
        raw = _decode_b64url(pub)
        if raw is not None and len(raw) == 64:
            # Legacy key: X||Y without the 0x04 uncompressed-point
            # marker. Repair it in place, same private key.
            pub = _b64url(b"\x04" + raw)
            try:
                _save_vapid_keys(priv, pub)
            except OSError:
                pass  # repaired in memory for this run; retry next start
            return priv, pub
        if raw is not None and len(raw) == 65 and raw[0] == 0x04:
            return priv, pub
    try:
        return generate_vapid_keys()
    except RuntimeError:
        return None, None


@_serialized
def classify_user_agent(ua):
    """(device, browser) from a User-Agent string. device: 'desktop'
    (Windows/macOS/Linux, no mobile markers), 'iphone', 'ipad',
    'android', or 'unknown' when the string says nothing usable.
    browser: 'chrome', 'safari', 'firefox', 'edge', 'opera',
    'other', or 'unknown' for an empty string. The server only has
    the UA string — an iPad in desktop mode sends a Macintosh UA,
    so it classifies as a desktop here; app.js uses the same
    UA-only rule for its warning, so both sides always agree."""
    ua = ua or ""
    if not ua:
        return "unknown", "unknown"
    if "iPhone" in ua:
        device = "iphone"
    elif "iPad" in ua:
        device = "ipad"
    elif "Android" in ua:
        device = "android"
    elif any(m in ua for m in ("Windows", "Macintosh", "Linux",
                               "X11", "CrOS")):
        device = "desktop"
    else:
        device = "unknown"
    if "Edg/" in ua:
        browser = "edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "opera"
    elif "Firefox/" in ua:
        browser = "firefox"
    elif "Chrome/" in ua:
        browser = "chrome"
    elif "Safari/" in ua:
        browser = "safari"
    else:
        browser = "other"
    return device, browser


_BROWSER_HE = {"chrome": "כרום", "safari": "ספארי",
               "firefox": "פיירפוקס", "edge": "אדג'", "opera": "אופרה"}


def subscription_device_label(sub):
    """One Hebrew line describing a push subscription: which device
    and browser it belongs to, e.g. 'כרום במחשב', 'אייפון — מסך
    הבית'. `sub` is a dict (from dict(row)). A subscription created
    before round 14 has device 'unknown' and shows 'לא ידוע'."""
    device = sub.get("device") or "unknown"
    browser = sub.get("browser") or "unknown"
    standalone = bool(sub.get("standalone"))
    if device == "unknown":
        return "לא ידוע"
    bname = _BROWSER_HE.get(browser, "")
    if device == "desktop":
        return ("%s במחשב" % bname) if bname else "מחשב"
    if device == "iphone":
        return "אייפון — מסך הבית" if standalone else "אייפון — ספארי"
    if device == "ipad":
        return "אייפד — מסך הבית" if standalone else "אייפד — ספארי"
    if device == "android":
        return ("%s באנדרואיד" % bname) if bname else "אנדרואיד"
    return "לא ידוע"


def save_subscription(conn, user_id, sub, now=None,
                      device="unknown", browser="unknown",
                      standalone=False):
    """Store a browser push subscription for a user. `sub` is the JSON
    the browser gives us: endpoint + keys.p256dh + keys.auth.
    `device`/`browser`/`standalone` say where it came from (see
    classify_user_agent) — a subscription belongs to one browser on
    one device, so the user can see on /push where their
    notifications actually go."""
    if get_user(conn, user_id) is None:
        return "משתמש לא ידוע."
    try:
        endpoint = sub["endpoint"]
        p256dh = sub["keys"]["p256dh"]
        auth = sub["keys"]["auth"]
    except (KeyError, TypeError):
        return "במנוי חסרים כתובת או מפתחות."
    if not endpoint.startswith("https://"):
        return "כתובת הדחיפה חייבת להיות https."
    conn.execute(
        "INSERT OR REPLACE INTO push_subscriptions "
        "(user_id, endpoint, p256dh, auth, device, browser,"
        " standalone, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, endpoint, p256dh, auth, device or "unknown",
         browser or "unknown", 1 if standalone else 0, _now_text(now)))
    conn.commit()
    return None


def prepare_push_payload(offer, course, session):
    """The notification text. Short, because a phone notification is
    one glance: who, which course, where, how many seats."""
    saver_name = offer["saver_name"] if "saver_name" in offer.keys() else "מישהו"
    where = ""
    if session is not None:
        teacher = ""
        try:
            teacher = (session["teacher"] or "").strip()
        except (KeyError, IndexError, TypeError):
            teacher = ""
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "מקום פנוי: %s" % course["name"],
        "body": "%s יכול לשמור מקום%s. לחץ לבקשה — מטבע אחד."
                % (saver_name, where),
        "url": "/offers",
        "tag": "seat-offer-%s" % offer["id"],
    }


# Reasons a push service gives when the subscription was created
# under a different VAPID keypair than this server's current one -
# the subscription is dead for good. Google's push service says
# BadJwtToken, Apple's says InvalidProviderToken: same meaning,
# different wording, so both count.
_DEAD_KEY_REASONS = ("badjwttoken", "invalidprovidertoken")


def _text_mentions_dead_key(text):
    """True when `text` mentions one of the dead-key reasons (case
    insensitive). Used for the body text and the exception text,
    where the reason sits inside a longer string."""
    lowered = (text or "").lower()
    return any(reason in lowered for reason in _DEAD_KEY_REASONS)


def _response_is_wrong_key(response, exc):
    """Is this 403 a wrong-key reason - the subscription was created
    under a different VAPID keypair than the server's current one,
    so no retry will ever succeed? The push service says so in the
    body: {"reason":"BadJwtToken"} from Google,
    {"reason":"InvalidProviderToken"} from Apple. pywebpush also
    puts the body in the exception text, so that is the last resort.
    A 403 without one of those exact reasons is NOT one - it might
    be transient, so it stays. Deliberately NOT counted:
    ExpiredProviderToken - pywebpush generates a fresh JWT per push,
    so an expired provider token at send time points at clock skew
    (a transient, server-wide condition), not a dead subscription;
    deleting subscriptions for it would be wrong. "Expired..." never
    contains either dead-key reason as a substring, so it cannot
    slip in through the text checks either."""
    try:
        body = response.json()
        if isinstance(body, dict):
            reason = body.get("reason")
            if isinstance(reason, str) and \
                    reason.lower() in _DEAD_KEY_REASONS:
                return True
    except Exception:
        pass
    try:
        if _text_mentions_dead_key(response.text):
            return True
    except Exception:
        pass
    return _text_mentions_dead_key(str(exc))


def _subscription_is_gone(exc):
    """True when the push service says the subscription is dead for
    good - the browser uninstalled the app, cleared the site, or the
    subscription expired (HTTP 410 Gone or 404 Not Found), OR the
    subscription was created under a different VAPID keypair than
    the server's current one (HTTP 403 with reason BadJwtToken from
    Google or InvalidProviderToken from Apple: the user subscribed
    while the server ran from a second folder with its own keys, or
    after the key file was regenerated). A wrong-key subscription
    can never work again, so it is deleted just like a 410 -
    otherwise it would fail on every offer forever. Anything else
    (network error, 400, other 403s - including ExpiredProviderToken,
    which points at clock skew, not a dead subscription - 5xx) is
    transient: the subscription might work again next time, so it
    stays. This is the ONLY place that decides 'dead' - one small
    testable function."""
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if code in (404, 410):
            return True
        if code == 403:
            return _response_is_wrong_key(response, exc)
        return False
    code = getattr(exc, "status_code", None)
    if code in (404, 410):
        return True
    if code == 403:
        return _text_mentions_dead_key(str(exc))
    return False
def send_push_to_subscription(subscription, payload, vapid_private,
                              vapid_public, sender=None):
    """Send one push. `sender`, when given, is a test double called as
    sender(subscription, payload) — tests never touch the real network.
    Otherwise pywebpush sends it. Returns (ok, detail, gone): `gone`
    is True only when the push service answered 404/410, or 403 with
    reason BadJwtToken, meaning the subscription will never work
    again and can be deleted."""
    if sender is not None:
        try:
            sender(subscription, payload)
            return True, "sent (test sender)", False
        except Exception as exc:  # noqa: BLE001 — report, don't crash
            return False, "test sender failed: %s" % exc, \
                _subscription_is_gone(exc)
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return False, "pywebpush is not installed", False
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"],
                         "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=vapid_private,
            vapid_claims={"sub": "https://my-seat-saver.duckdns.org"},
        )
        return True, "sent", False
    except Exception as exc:  # noqa: BLE001 — a dead subscription is data
        return False, "push failed: %s" % exc, _subscription_is_gone(exc)


_URL_RE = re.compile(r"https?://\S+")


def _safe_detail(detail):
    """Strip anything that looks like a URL from a log detail before
    it is shown on a page. The detail strings the app writes never
    contain URLs, but a push-service error could echo one - and a
    subscription endpoint rendered in the admin log would let anyone
    who can open that page copy it. So any http(s)://... becomes
    '[כתובת]'. Used by the admin log view and the test-push result."""
    if not detail:
        return ""
    return _URL_RE.sub("[כתובת]", str(detail))


_LOG_STATUS_HE = {
    "sent": "נשלחה",
    "failed": "נכשלה",
    "skipped-no-subscription": "דולגה — אין מנוי",
    "skipped-quiet-hours": "דולגה — שעות שקטות",
    "skipped-vacation": "דולגה — חופשה",
    "removed-dead-subscription": "מנוי הוסר",
}


def _log_status_label(status):
    """Hebrew label for a notification_log status. Unknown statuses
    are shown as-is (escaped by the caller) so a new status never
    renders as a blank."""
    return _LOG_STATUS_HE.get(status, status)


def recent_notification_log(conn, limit=50):
    """The latest notification_log rows, with user names, newest
    first. The admin log view reads through here."""
    return conn.execute(
        "SELECT l.*, u.name AS user_name FROM notification_log l"
        " JOIN users u ON u.id = l.user_id"
        " ORDER BY l.id DESC LIMIT ?", (limit,)).fetchall()


def recent_dead_removal(conn, user_id, now=None, days=7):
    """Round 20, item 5: the latest auto-removal of one of this
    user's subscriptions ("removed-dead-subscription"), within the
    last `days` days — or None when there is none. The /push page
    shows a banner for it: a subscription that died silently is a
    mystery, but a subscription that died with a banner is a
    one-tap fix ("turn notifications on again"). A removal older
    than a week is not shown — by then the user either re-enabled
    or stopped caring, and the banner would be stale noise."""
    now = now or datetime.now()
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM notification_log WHERE user_id=?"
        " AND status='removed-dead-subscription' AND created_at>=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (user_id, since)).fetchone()


def send_broadcast(conn, admin_user, title, body, now=None, sender=None):
    """Round 20, item 12: the admin sends ONE push to ALL users
    (an announcement, e.g. "the pilot starts tomorrow"). Admin
    only, server-enforced, at most one per hour per admin (see
    BROADCAST_COOLDOWN_SECONDS) — a broadcast reaches everyone,
    so the rate limit is the whole point. Only users who actually
    have a push subscription get it; quiet hours and vacation
    still apply (via _push_to_user), because a broadcast is not
    more urgent than the user's own settings. Each attempt is
    logged in notification_log with offer_id=0, like a test push.
    Returns (sent_count, error)."""
    if not is_admin(admin_user):
        return 0, "רק דניאל יכול לשלוח הודעה לכולם."
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        return 0, "נא לכתוב כותרת."
    if not body:
        return 0, "נא לכתוב הודעה."
    if len(title) > 80:
        return 0, "הכותרת ארוכה מדי (עד 80 תווים)."
    if len(body) > 200:
        return 0, "ההודעה ארוכה מדי (עד 200 תווים)."
    now = now or datetime.now()
    last = conn.execute(
        "SELECT created_at FROM broadcast_last WHERE admin_id=?",
        (admin_user["id"],)).fetchone()
    if last is not None:
        try:
            last_time = datetime.fromisoformat(last["created_at"])
        except (ValueError, TypeError):
            last_time = None
        if last_time is not None and \
                (now - last_time).total_seconds() < BROADCAST_COOLDOWN_SECONDS:
            return 0, ("כבר שלחת הודעה לכולם בשעה האחרונה — חכה "
                       "קצת לפני הבאה.")
    payload = {
        "title": title,
        "body": body,
        "url": "/",
        "tag": "broadcast-%s" % now.strftime("%Y%m%d%H%M"),
    }
    sent = 0
    user_ids = [r["user_id"] for r in conn.execute(
        "SELECT DISTINCT user_id FROM push_subscriptions").fetchall()]
    for user_id in sorted(user_ids):
        results = _push_to_user(conn, user_id, 0, payload, sender, now)
        if any(status == "sent" for _uid, status, _d in results):
            sent += 1
    conn.execute(
        "INSERT OR REPLACE INTO broadcast_last (admin_id, created_at)"
        " VALUES (?, ?)", (admin_user["id"], _now_text(now)))
    conn.commit()
    return sent, None


def _push_to_user(conn, user_id, offer_id, payload, sender=None, now=None,
                  force=False):
    """Send `payload` to every subscription of one user, and log one
    notification_log row per attempt. A subscription the push service
    says is gone (404/410, or 403 with a wrong-key reason -
    BadJwtToken from Google, InvalidProviderToken from Apple) is
    deleted right here and its removal is logged — a dead
    subscription must not fill the log with failures forever. Any
    other failure leaves the row alone. Returns the log rows as
    (user_id, status, detail).

    `force` bypasses the quiet-hours and vacation checks. It is
    used by exactly one caller: the vacation-cancel confirmation
    push to the requester themself. That push is a direct answer
    to an action the user took seconds ago — they just tapped the
    button — so "don't disturb me" does not apply to it, and
    without the bypass the confirmation would be suppressed by
    the very vacation mode it confirms. Every other push goes
    through the checks."""
    priv, pub = get_vapid_keys()
    if sender is not None and priv is None:
        # Tests inject a fake sender: it needs no real keys, and must not
        # trigger key generation on disk.
        priv, pub = "test-private", "test-public"
    results = []
    subs = conn.execute(
        "SELECT * FROM push_subscriptions WHERE user_id=?",
        (user_id,)).fetchall()
    if not subs:
        status, detail = "skipped-no-subscription", \
            "למשתמש אין התראות מופעלות באף מכשיר."
        conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (offer_id, user_id, status, detail, _now_text(now)))
        results.append((user_id, status, detail))
        return results
    # Quiet hours: a notification inside the recipient's quiet
    # window is skipped, not sent and not queued for later — skipped
    # means skipped. One log row per user (not per subscription),
    # so the skip is visible instead of silent. This is the ONLY
    # place that enforces quiet hours, so it covers every push
    # type: offers, requests, cancels, reminders, free-ups,
    # waiting list, and new sessions. A forced push (a confirmation
    # of the user's own tap, seconds ago) skips this check — the
    # user is awake and using the app right now.
    user = get_user(conn, user_id)
    if not force and in_quiet_hours(user, now):
        status = "skipped-quiet-hours"
        detail = ("שעות שקטות (%s) — ההתראה לא נשלחה ולא תישלח מאוחר יותר."
                  % quiet_hours_label(user))
        conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (offer_id, user_id, status, detail, _now_text(now)))
        results.append((user_id, status, detail))
        return results
    # Vacation mode: no push at all while the user is away — every
    # push type, same as quiet hours. Skipped means skipped, never
    # queued. One log row per user, so the skip is visible instead
    # of silent. A forced push (a confirmation of the user's own
    # tap, seconds ago) skips this check too — otherwise the
    # vacation-cancel confirmation would be suppressed by the very
    # vacation mode it confirms.
    if not force and is_on_vacation(user):
        status = "skipped-vacation"
        detail = ("מצב חופשה פעיל — ההתראה לא נשלחה ולא תישלח מאוחר יותר.")
        conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (offer_id, user_id, status, detail, _now_text(now)))
        results.append((user_id, status, detail))
        return results
    if priv is None:
        status, detail = "failed", "אין מפתחות VAPID (הדחיפה לא הוגדרה)."
        conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (offer_id, user_id, status, detail, _now_text(now)))
        results.append((user_id, status, detail))
        return results
    for sub in subs:
        ok, detail, gone = send_push_to_subscription(
            dict(sub), payload, priv, pub, sender=sender)
        if ok:
            status = "sent"
        elif gone:
            conn.execute("DELETE FROM push_subscriptions WHERE id=?",
                         (sub["id"],))
            status = "removed-dead-subscription"
            if "badjwttoken" in detail.lower():
                # The subscription was created under a different VAPID
                # keypair than this server's — it can never work
                # again, so it is removed and the user re-subscribes
                # (turn notifications off and on) to get a fresh one.
                detail = ("המנוי נוצר עם מפתח ישן והוסר (הדחיפה נכשלה עם "
                          "403 BadJwtToken — צריך להפעיל מחדש התראות).")
            elif "invalidprovidertoken" in detail.lower():
                # Same story, Apple's wording: the subscription was
                # created under a different VAPID keypair than this
                # server's. The user re-subscribes to get a fresh one.
                detail = ("המנוי נוצר עם מפתח ישן והוסר (הדחיפה נכשלה עם "
                          "403 InvalidProviderToken של אפל — צריך להפעיל "
                          "מחדש התראות).")
            else:
                detail = "המנוי פג והוסר (הדחיפה נכשלה עם 404/410)."
        else:
            status = "failed"
        conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (offer_id, user_id, status, detail, _now_text(now)))
        results.append((user_id, status, detail))
    return results


def notify_followers(conn, offer, sender=None, now=None):
    """When an offer is created: find every follower of that course
    (general follows always match; specific follows only if the session
    matches their day/time), and push to each of their subscriptions.
    Every attempt is written to notification_log, so a missed
    notification is visible instead of silent. One offer, one push:
    a user with an open board post for this exact session is skipped
    here — they get the board message ("מישהו מציע מקום לשיעור
    שביקשת") from notify_board_requesters instead, never both.
    Returns the log rows."""
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    saver = get_user(conn, offer["saver_id"])
    offer_dict = dict(offer)
    offer_dict["saver_name"] = saver["name"] if saver else "מישהו"
    payload = prepare_push_payload(offer_dict, course, session)
    board_ids = board_requester_ids(conn, offer["session_id"])
    results = []
    for user_id in sorted(followers_for_offer(conn, offer)):
        if user_id in board_ids:
            continue  # Gets the board message instead — see above.
        results.extend(_push_to_user(conn, user_id, offer["id"], payload,
                                     sender, now))
    conn.commit()
    return results


def prepare_new_session_payload(session, course):
    """The push followers get when a new session is added to a course
    they follow. Short, because a phone notification is one glance:
    which course, when, who teaches, where."""
    teacher = _teacher_of(session)
    where = "בניין %s חדר %s" % (session["building"], session["room"])
    if teacher:
        where = "%s, %s" % (teacher, where)
    return {
        "title": "נוסף שיעור חדש: %s" % course["name"],
        "body": "%s %s–%s, %s."
                % (WEEKDAY_NAMES[session["weekday"]],
                   session["start_time"], session["end_time"], where),
        "url": "/courses",
        "tag": "new-session-%s" % session["id"],
    }


def notify_followers_of_new_session(conn, session, sender=None, now=None,
                                    exclude_user_id=None):
    """When a session is added to a course: push to every follower
    of that course whose follow matches the new session (general
    follows always match; specific follows only if the day and time
    match — the same follow_matches logic as an offer). Only the
    add-session path calls this — the seed does not notify anyone,
    and there is no edit path. The admin who added the session
    (exclude_user_id) is not notified about their own action.
    Returns the log rows. The log's offer_id is 0 here: there is no
    offer yet, this notification is about a session — the log is
    diagnostic only, so 0 means 'no offer' unambiguously."""
    course = get_course(conn, session["course_id"])
    payload = prepare_new_session_payload(dict(session), course)
    results = []
    for user_id in sorted(followers_for_session(
            conn, session["course_id"], dict(session))):
        if exclude_user_id is not None and int(user_id) == int(exclude_user_id):
            continue
        results.extend(_push_to_user(conn, user_id, 0, payload,
                                     sender, now))
    conn.commit()
    return results


def prepare_request_payload(request, course, session):
    """The notification the SAVER gets when someone requests a seat
    in their offer. Short, because a phone notification is one
    glance: who asked, which course, where."""
    requester_name = "מישהו"
    try:
        requester_name = request["requester_name"] or "מישהו"
    except (KeyError, IndexError, TypeError):
        pass
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "מישהו ביקש מקום",
        "body": "%s ביקש מקום ב%s%s. המטבע מוקפא עד שיאשר הגעה."
                % (requester_name, course["name"], where),
        "url": "/my",
        "tag": "seat-request-%s" % request["id"],
    }


def notify_saver_of_request(conn, request, sender=None, now=None):
    """When a seat is requested: push to the SAVER, so they know
    someone asked and can keep the seat. Until now only followers
    got a push when an offer was created, and the saver had to keep
    checking the app. The saver is notified the same way followers
    are — same log, same skipped-no-subscription when they never
    enabled notifications. Returns the log rows."""
    offer = get_offer(conn, request["offer_id"])
    if offer is None:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    requester = get_user(conn, request["requester_id"])
    req_dict = dict(request)
    req_dict["requester_name"] = requester["name"] if requester else "מישהו"
    payload = prepare_request_payload(req_dict, course, session)
    results = _push_to_user(conn, offer["saver_id"], offer["id"], payload,
                            sender, now)
    conn.commit()
    return results


def prepare_on_the_way_payload(request, course, session):
    """The push the SAVER gets when a requester taps "I'm on my
    way". Before this the saver only saw it on the /my page, with
    no push — and a saver who is not looking at the app would not
    know to keep holding the seat. Informational only: it changes
    no rule, no coin, no deadline. Same four fields sw.js reads."""
    requester_name = "מישהו"
    try:
        requester_name = request["requester_name"] or "מישהו"
    except (KeyError, IndexError, TypeError):
        pass
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "%s בדרך לשיעור" % requester_name,
        "body": "%s בדרך ל%s%s — המקום שמור לו."
                % (requester_name, course["name"], where),
        "url": "/my",
        "tag": "seat-on-the-way-%s" % request["id"],
    }


def notify_saver_of_on_the_way(conn, request, sender=None, now=None):
    """When a requester taps "I'm on my way": push to the SAVER, so
    they know to keep holding the seat instead of finding out on
    the /my page. Only the on-the-way path calls it. A second tap
    sends another push (the time updates, same as today) — the
    saver hears about it again, which is the point: the requester
    is still coming. Returns the log rows."""
    offer = get_offer(conn, request["offer_id"])
    if offer is None:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    requester = get_user(conn, request["requester_id"])
    req_dict = dict(request)
    req_dict["requester_name"] = requester["name"] if requester else "מישהו"
    payload = prepare_on_the_way_payload(req_dict, course, session)
    results = _push_to_user(conn, offer["saver_id"], offer["id"], payload,
                            sender, now)
    conn.commit()
    return results


def prepare_late_payload(request, course, session):
    """The push the SAVER gets when a requester declares "I'm
    late". Same four fields sw.js reads. The saver has to decide:
    accept (the seat is held until start+10) or refuse (the coin
    is refunded at once) — the decision buttons are on /my."""
    requester_name = "מישהו"
    try:
        requester_name = request["requester_name"] or "מישהו"
    except (KeyError, IndexError, TypeError):
        pass
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "%s מאחר לשיעור" % requester_name,
        "body": "%s מאחר ל%s%s — אשר או דחה את האיחור בדף המקומות שלי."
                % (requester_name, course["name"], where),
        "url": "/my",
        "tag": "seat-late-%s" % request["id"],
    }


def notify_saver_of_late(conn, request, sender=None, now=None):
    """When a requester declares "I'm late": push to the SAVER, so
    they know to decide — accept and hold the seat until start+10,
    or refuse and free it. Only the declare-late path calls it.
    Quiet hours and vacation still apply (no force bypass): this
    is a notice about someone else's lateness, not a confirmation
    of the saver's own tap. Returns the log rows."""
    offer = get_offer(conn, request["offer_id"])
    if offer is None:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    requester = get_user(conn, request["requester_id"])
    req_dict = dict(request)
    req_dict["requester_name"] = requester["name"] if requester else "מישהו"
    payload = prepare_late_payload(req_dict, course, session)
    results = _push_to_user(conn, offer["saver_id"], offer["id"], payload,
                            sender, now)
    conn.commit()
    return results


def prepare_cancel_payload(request, course, session):
    """The notification the SAVER gets when a requester cancels their
    own request. Same shape as the request notification, but the
    title says the request was cancelled — the saver should know
    the seat they kept is unclaimed again, instead of arriving to
    class with a seat saved for nobody."""
    requester_name = "מישהו"
    try:
        requester_name = request["requester_name"] or "מישהו"
    except (KeyError, IndexError, TypeError):
        pass
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "הבקשה בוטלה",
        "body": "%s ביטל את הבקשה למקום ב%s%s. המקום פנוי שוב."
                % (requester_name, course["name"], where),
        "url": "/my",
        "tag": "seat-cancel-%s" % request["id"],
    }


def notify_saver_of_cancel(conn, request, sender=None, now=None):
    """When a requester cancels their own request: push to the SAVER,
    so they know the seat is unclaimed again. Two paths call it:
    cancel_request (the requester's own button) and the vacation
    cancel (turning vacation on cancels frozen requests, treated
    as a requester cancel). A saver cancelling the offer, or an
    admin force-refund, are different paths and do not notify the
    saver about a cancellation of their own action. Returns the
    log rows."""
    offer = get_offer(conn, request["offer_id"])
    if offer is None:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    requester = get_user(conn, request["requester_id"])
    req_dict = dict(request)
    req_dict["requester_name"] = requester["name"] if requester else "מישהו"
    payload = prepare_cancel_payload(req_dict, course, session)
    results = _push_to_user(conn, offer["saver_id"], offer["id"], payload,
                            sender, now)
    conn.commit()
    return results


def prepare_saver_cancel_request_payload(request, course, session):
    """The notification the REQUESTER gets when the saver cancels
    their request (not the whole offer — just this one request). The
    coin already came back; the push is so the requester knows they
    need another seat, instead of walking to class expecting one."""
    saver_name = "מישהו"
    try:
        saver_name = request["saver_name"] or "מישהו"
    except (KeyError, IndexError, TypeError):
        pass
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "הבקשה שלך בוטלה על ידי השומר",
        "body": "%s ביטל את הבקשה שלך למקום ב%s%s. המטבע חזר אליך."
                % (saver_name, course["name"], where),
        "url": "/my",
        "tag": "seat-saver-cancel-%s" % request["id"],
    }


def notify_requester_of_saver_cancel(conn, request, sender=None, now=None):
    """When the saver cancels one request: push to the REQUESTER, so
    they know the seat is gone and the coin is back. Only the
    saver-cancel path calls it. Returns the log rows."""
    offer = get_offer(conn, request["offer_id"])
    if offer is None:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    saver = get_user(conn, request["saver_id"])
    req_dict = dict(request)
    req_dict["saver_name"] = saver["name"] if saver else "מישהו"
    payload = prepare_saver_cancel_request_payload(req_dict, course, session)
    results = _push_to_user(conn, request["requester_id"], offer["id"],
                            payload, sender, now)
    conn.commit()
    return results


def prepare_vacation_cancel_payload(request, course, session):
    """The push the REQUESTER gets when their own frozen request is
    cancelled because they turned vacation mode on. The coin already
    came back; the push is so they know the seat is gone — they
    declared themselves away, so the seat was freed for someone
    else. Same four fields sw.js reads."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "הבקשה בוטלה",
        "body": "הבקשה למקום ב%s%s בוטלה כי הפעלת מצב חופשה."
                " המטבע חזר אליך." % (course["name"], where),
        "url": "/my",
        "tag": "seat-vacation-cancel-%s" % request["id"],
    }


def notify_requester_of_vacation_cancel(conn, request, sender=None,
                                        now=None):
    """When turning vacation on cancels a frozen request: push to the
    REQUESTER themself, so they know the seat is gone and the coin
    is back. Only the vacation-cancel path calls it. The saver gets
    their own push separately (notify_saver_of_cancel, as if the
    requester cancelled by hand). The push is forced (see
    _push_to_user): the user just turned vacation on, so the
    vacation check would otherwise suppress the very confirmation
    of that tap. Returns the log rows."""
    offer = get_offer(conn, request["offer_id"])
    if offer is None:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    payload = prepare_vacation_cancel_payload(dict(request), course, session)
    results = _push_to_user(conn, request["requester_id"], offer["id"],
                            payload, sender, now, force=True)
    conn.commit()
    return results


def prepare_freed_payload(offer, course, session):
    """The notification followers get when a seat frees up again.
    Same shape as the offer payload, but the title says a seat
    freed up — the follower who missed the first offer gets a
    second chance."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "התפנה מקום: %s" % course["name"],
        "body": "התפנה מקום ב%s%s — לחץ לבקשה, מטבע אחד."
                % (course["name"], where),
        "url": "/offers",
        "tag": "seat-freed-%s" % offer["id"],
    }


def prepare_freed_waiting_payload(offer, course, session):
    """The notification a WAITING user gets when a seat frees up.
    Distinct from the general followers' "seat freed" push: it says
    they are on the waiting list, so they know they are first in
    line — and the tag is different, so the phone does not collapse
    it into the offer notification."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "התפנה מקום — אתה ברשימת ההמתנה",
        "body": "התפנה מקום ב%s%s — אתה ראשון בתור, לחץ לבקשה, מטבע אחד."
                % (course["name"], where),
        "url": "/offers",
        "tag": "seat-freed-waiting-%s" % offer["id"],
    }


def notify_waiting_users(conn, offer, sender=None, now=None):
    """Push the waiting list that a seat freed up, in join order.
    Waiting users are told FIRST — they queued for this exact offer,
    so they get the first chance before the general followers.
    Returns the log rows."""
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    payload = prepare_freed_waiting_payload(dict(offer), course, session)
    results = []
    for user in waiting_users_for_offer(conn, offer["id"]):
        results.extend(_push_to_user(conn, user["id"], offer["id"], payload,
                                     sender, now))
    conn.commit()
    return results


def prepare_offer_cancelled_payload(offer, course, session):
    """The push waiting-list users get when the saver cancels the
    offer they queued for. The seat is gone — they should know not
    to wait for it any more. Short, because a phone notification is
    one glance: which course, when, where."""
    where = ""
    when = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
        when = "%s %s" % (WEEKDAY_NAMES[session["weekday"]],
                          session["start_time"])
    return {
        "title": "ההצעה בוטלה",
        "body": "ההצעה ל%s (%s%s) בוטלה — אין צורך לחכות לה."
                % (course["name"], when, where),
        "url": "/offers",
        "tag": "offer-cancelled-%s" % offer["id"],
    }


# TEMPORARY — test aid, remove after pilot. The test-push payload
# and sender below exist only so Daniel can check push delivery
# without waiting for a real offer. Same four fields sw.js reads
# (title, body, url, tag), nothing else.
def prepare_test_push_payload(now=None):
    """TEMPORARY — test aid, remove after pilot. The test push:
    one fixed text, so the user can tell at a glance that it is a
    test and not a real offer."""
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    return {
        "title": "בדיקת התראות",
        "body": "אם אתה רואה את זה, ההתראות עובדות",
        "url": "/push",
        "tag": "test-%s" % stamp,
    }


def send_test_push(conn, user_id, sender=None, now=None):
    """TEMPORARY — test aid, remove after pilot. Send ONE test push
    to one user, through the normal _push_to_user path (no new push
    code), and log it in notification_log with offer_id=0 ('no
    offer', same convention as the new-session notification). At
    most one test push per user per 60 seconds: a second tap inside
    the minute is refused with a Hebrew 'try again in X seconds'
    message and no push is sent. Only real send attempts start the
    cooldown - a tap that was skipped (no subscription, quiet
    hours, vacation) does not, so the user can retry right after
    fixing the cause. The cooldown is kept in test_push_last, one
    row per user. Returns (sent, message): sent is True only when
    the push went out; message is the Hebrew text for the page,
    success or plain-words failure reason."""
    now = now or datetime.now()
    user = get_user(conn, user_id)
    if user is None:
        return False, "משתמש לא נמצא."
    row = conn.execute(
        "SELECT created_at FROM test_push_last WHERE user_id=?",
        (user_id,)).fetchone()
    if row is not None:
        try:
            last = datetime.fromisoformat(row["created_at"])
        except ValueError:
            last = None
        if last is not None:
            remaining = TEST_PUSH_COOLDOWN_SECONDS - \
                (now - last).total_seconds()
            if remaining > 0:
                wait = int(remaining)
                if wait < remaining:
                    wait += 1
                return False, "נסה שוב בעוד %d שניות." % wait
    payload = prepare_test_push_payload(now)
    results = _push_to_user(conn, user_id, 0, payload, sender, now)
    attempted = any(r[1] in ("sent", "failed",
                             "removed-dead-subscription")
                    for r in results)
    if attempted:
        conn.execute(
            "INSERT OR REPLACE INTO test_push_last (user_id, created_at)"
            " VALUES (?, ?)", (user_id, _now_text(now)))
    conn.commit()
    statuses = [r[1] for r in results]
    details = {r[1]: r[2] for r in results}
    if "sent" in statuses:
        return True, "דחיפת הבדיקה נשלחה — בדוק את הטלפון."
    if "removed-dead-subscription" in statuses:
        return False, "המנוי הישן הוסר — הפעל התראות מחדש ואז נסה שוב."
    if "skipped-no-subscription" in statuses:
        return False, "אין לך התראות מופעלות — הפעל אותן קודם."
    if "skipped-quiet-hours" in statuses:
        return False, details["skipped-quiet-hours"]
    if "skipped-vacation" in statuses:
        return False, details["skipped-vacation"]
    if "failed" in statuses:
        detail = details["failed"]
        if "pywebpush is not installed" in detail:
            # The server itself is missing the one library: telling
            # the user to toggle notifications would be a lie.
            return False, ("השליחה נכשלה כי pywebpush לא מותקן — "
                           "הרץ pip install -r requirements.txt ואז "
                           "הפעל מחדש את השרת.")
        if "אין מפתחות VAPID" in detail:
            # Same: no keys yet — re-subscribing cannot help, the
            # server has to restart and generate them.
            return False, ("השליחה נכשלה כי אין מפתחות VAPID — "
                           "הפעל מחדש את השרת כדי ליצור אותם.")
        return False, ("השליחה נכשלה. נסה שוב, ואם זה חוזר, כבה "
                       "והדלק התראות מחדש. (%s)"
                       % _safe_detail(detail).replace("\n", " "))
    return False, "השליחה נכשלה. נסה שוב מאוחר יותר."


def notify_waiting_users_of_cancel(conn, offer, user_ids, sender=None,
                                   now=None):
    """Push the waiting list that the saver cancelled the offer they
    queued for. The user ids are passed in — captured before the
    entries are deleted, inside the lock in cancel_offer — because
    the notify itself runs after the lock is released: the lock is
    held only for the DB change, never for the network send.
    Saver-cancel only: when an offer expires at class start the
    seat is gone anyway and a push then is noise, so settle_expired
    deletes the entries silently. Returns the log rows."""
    if not user_ids:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    payload = prepare_offer_cancelled_payload(dict(offer), course, session)
    results = []
    for user_id in sorted(user_ids):
        results.extend(_push_to_user(conn, user_id, offer["id"], payload,
                                     sender, now))
    conn.commit()
    return results


def prepare_offer_edited_payload(offer, course, session, note):
    """The push a frozen requester gets when the saver edits the
    offer they requested: the note of the seat they asked for just
    changed, and they should know before they walk to class. Only
    the note is in the text: a seats-only change does not change
    the requester's seat, so it never sends this push. Same four
    fields sw.js reads."""
    where = ""
    when = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
        when = "%s %s" % (WEEKDAY_NAMES[session["weekday"]],
                          session["start_time"])
    note_part = ""
    if note:
        note_part = ", הערה: %s" % note
    return {
        "title": "ההצעה עודכנה",
        "body": "ההצעה ל%s (%s%s) עודכנה%s."
                % (course["name"], when, where, note_part),
        "url": "/my",
        "tag": "seat-offer-edited-%s" % offer["id"],
    }


def notify_frozen_requesters_of_edit(conn, offer, note, sender=None,
                                     now=None):
    """Push the frozen requesters of an offer that the saver edited
    it: the note changed, and the seat they asked for is not
    quite the seat they asked for any more. One push per requester
    (a 2-seat request is one push, not two) — the caller passes the
    offer and the NEW note, already stored. Only frozen
    requesters: anyone whose request already resolved gets nothing.
    Followers who never requested get nothing either — it is the
    same offer, not a new one. Returns the log rows."""
    frozen_ids = [r["requester_id"] for r in conn.execute(
        "SELECT DISTINCT requester_id FROM requests"
        " WHERE offer_id=? AND status='frozen'",
        (offer["id"],))]
    if not frozen_ids:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    payload = prepare_offer_edited_payload(dict(offer), course, session,
                                           note)
    results = []
    for user_id in sorted(frozen_ids):
        results.extend(_push_to_user(conn, user_id, offer["id"], payload,
                                     sender, now))
    conn.commit()
    return results


def notify_frozen_requesters_of_cancel(conn, offer, user_ids, sender=None,
                                       now=None):
    """Push the frozen requesters that the saver cancelled the whole
    offer. They were refunded already, but without a push they might
    still walk to class expecting a seat that no longer exists. One
    push per requester (a 2-seat request is one push, not two) - the
    caller passes distinct user ids, already excluding anyone who is
    also on the waiting list (they get the waiting-list notice
    instead, never both). Only frozen requesters: anyone whose
    request already resolved gets nothing - the caller captures the
    frozen ids before the refunds change their status. The payload
    is the same "offer cancelled" one as the waiting list: title,
    course, when, teacher and place. Returns the log rows."""
    if not user_ids:
        return []
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    payload = prepare_offer_cancelled_payload(dict(offer), course, session)
    results = []
    for user_id in sorted(user_ids):
        results.extend(_push_to_user(conn, user_id, offer["id"], payload,
                                     sender, now))
    conn.commit()
    return results


def notify_followers_seat_freed(conn, offer, sender=None, now=None,
                                exclude_user_id=None, exclude_user_ids=None):
    """Push the followers of an offer that a seat freed up in. Same
    recipients as the offer notification (general follows always
    match; specific follows only if the session matches), same log.
    The user whose action freed the seat (exclude_user_id) is skipped:
    someone who just cancelled their own request does not need a push
    telling them a seat freed up. Waiting-list users
    (exclude_user_ids) are skipped too: they already got their own
    "you are on the waiting list" push for the same seat, and nobody
    gets two pushes for one freed seat. Returns the log rows."""
    course = get_course(conn, offer["course_id"])
    session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
    saver = get_user(conn, offer["saver_id"])
    offer_dict = dict(offer)
    offer_dict["saver_name"] = saver["name"] if saver else "מישהו"
    payload = prepare_freed_payload(offer_dict, course, session)
    excluded = set()
    if exclude_user_id is not None:
        excluded.add(int(exclude_user_id))
    if exclude_user_ids:
        excluded.update(int(u) for u in exclude_user_ids)
    results = []
    for user_id in sorted(followers_for_offer(conn, offer)):
        if int(user_id) in excluded:
            continue
        results.extend(_push_to_user(conn, user_id, offer["id"], payload,
                                     sender, now))
    conn.commit()
    return results


def maybe_notify_seat_freed(conn, offer_id, seats_before, sender=None,
                            now=None, exclude_user_id=None):
    """After a seat is freed: if the offer is still open AND it went
    from fully booked (0 available) to having a free seat, tell the
    followers. If seats were already available, stay silent — a
    notification about a seat that was free all along is spam. If
    the offer is closed (saver cancelled it, or it expired at class
    start before a no-show refund), there is nothing to offer, so
    stay silent too. Waiting-list users are told FIRST, with their
    own message, and are then excluded from the general followers'
    notification — one freed seat, one push per person. Returns the
    log rows, or [] when silent."""
    offer = get_offer(conn, offer_id)
    if offer is None or offer["status"] != "open":
        return []
    if seats_before != 0:
        return []
    if seats_available(conn, offer) < 1:
        return []
    waiting_ids = [u["id"] for u in waiting_users_for_offer(conn, offer_id)]
    results = []
    if waiting_ids:
        results.extend(notify_waiting_users(conn, offer, sender, now))
    results.extend(notify_followers_seat_freed(
        conn, offer, sender, now, exclude_user_id,
        exclude_user_ids=waiting_ids))
    return results


def prepare_reminder_payload(request, course, session):
    """The reminder a requester gets 5 minutes before class: come
    on time, and confirm arrival when you get there. Short, because
    a phone notification is one glance. Round 20, item 11: the
    notification carries action buttons — "אני בדרך" and "ביטול"
    — so the requester can act without opening the app. The
    Service Worker (sw.js) posts the tap to the matching endpoint
    with this request's id, which rides along as "request_id" (the
    four display fields stay the same)."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "תזכורת: השיעור מתחיל בעוד 5 דקות",
        "body": "%s מתחיל בעוד 5 דקות%s. אל תשכח לאשר הגעה כשתגיע."
                % (course["name"], where),
        "url": "/my",
        "tag": "seat-reminder-%s" % request["id"],
        "request_id": request["id"],
        "actions": [{"action": "on-my-way", "title": "אני בדרך"},
                    {"action": "cancel-request", "title": "ביטול"}],
    }


def find_reminders_due(conn, now=None):
    """Frozen requests whose class starts within the next
    REMINDER_MINUTES minutes and that have no reminder yet. A class
    that already started is not due — the reminder is for arriving
    on time, not for after the fact. Returns the request rows."""
    now = now or datetime.now()
    due = []
    for req in conn.execute(
            "SELECT * FROM requests WHERE status='frozen'"
            " AND reminded_at IS NULL").fetchall():
        offer = get_offer(conn, req["offer_id"])
        if offer is None:
            continue
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        if session is None:
            continue
        start = _session_start_datetime(dict(offer), dict(session))
        delta = (start - now).total_seconds()
        if 0 <= delta <= REMINDER_MINUTES * 60:
            due.append(req)
    return due


def reminder_tick(conn, sender=None, now=None):
    """One reminder pass: every due request gets a push to the
    requester, and reminded_at is set in the same commit as the send
    attempt — so a server restart never double-reminds, and a
    requester with no subscription is not retried every minute
    (the attempt itself is logged, so nothing is silent). Returns
    how many reminders were attempted."""
    now = now or datetime.now()
    count = 0
    for req in find_reminders_due(conn, now):
        offer = get_offer(conn, req["offer_id"])
        if offer is None:
            continue
        course = get_course(conn, offer["course_id"])
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        payload = prepare_reminder_payload(dict(req), course, session)
        _push_to_user(conn, req["requester_id"], offer["id"], payload,
                      sender, now)
        conn.execute("UPDATE requests SET reminded_at=? WHERE id=?",
                     (_now_text(now), req["id"]))
        conn.commit()
        count += 1
    return count


def prepare_early_reminder_payload(request, course, session):
    """The 30-minute "leave now" reminder. The 5-minute reminder is
    too late to leave home — this one goes out half an hour before
    class, while there is still time to walk. Only for frozen
    requests: a request that already resolved needs no reminder.
    Round 20, item 11: it carries the same action buttons as the
    5-minute reminder ("אני בדרך", "ביטול") and the request id —
    the buttons are useful half an hour before class too, when
    the requester is deciding whether to go at all."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "צא עכשיו",
        "body": "השיעור ב%s%s מתחיל בעוד חצי שעה —"
                " זה הזמן לצאת." % (course["name"], where),
        "url": "/my",
        "tag": "seat-early-reminder-%s" % request["id"],
        "request_id": request["id"],
        "actions": [{"action": "on-my-way", "title": "אני בדרך"},
                    {"action": "cancel-request", "title": "ביטול"}],
    }


def find_early_reminders_due(conn, now=None):
    """Which frozen requests are due their 30-minute "leave now"
    reminder: class starts in 30 minutes or less, no early reminder
    sent yet. One more rule, and it matters: a request created
    less than 30 minutes before class missed the 30-minute point
    already — it gets no early reminder (the 5-minute one still
    applies). The request's created_at is the clock for that: a
    request created at start-20 was never 30 minutes away, so the
    point never existed for it. Resolved or cancelled requests
    are never due — only frozen ones."""
    now = now or datetime.now()
    due = []
    for req in conn.execute(
            "SELECT * FROM requests WHERE status='frozen'"
            " AND reminded_30_at IS NULL").fetchall():
        offer = get_offer(conn, req["offer_id"])
        if offer is None:
            continue
        session = get_session(conn, offer["session_id"]) if offer["session_id"] else None
        if session is None:
            continue
        start = _session_start_datetime(dict(offer), dict(session))
        delta = (start - now).total_seconds()
        if not 0 <= delta <= EARLY_REMINDER_MINUTES * 60:
            continue
        try:
            created = datetime.fromisoformat(req["created_at"])
        except (ValueError, TypeError):
            continue
        if created > start - timedelta(minutes=EARLY_REMINDER_MINUTES):
            continue
        due.append(req)
    return due


def early_reminder_tick(conn, sender=None, now=None):
    """One 30-minute-reminder pass, same pattern as the 5-minute
    one: every due request gets a push to the requester, and
    reminded_30_at is set in the same commit as the send attempt
    — so a server restart never double-reminds, and a requester
    with no subscription is not retried every minute (the attempt
    itself is logged, so nothing is silent). Quiet hours and
    vacation are respected inside _push_to_user: a skipped push
    still sets the flag, so the requester does not get the reminder
    later — a 30-minute reminder sent at class start would be
    noise. The 5-minute reminder still runs separately: both can
    fire for the same request (30-minute, then 5-minute). Returns
    how many early reminders were attempted."""
    now = now or datetime.now()
    count = 0
    for req in find_early_reminders_due(conn, now):
        offer = get_offer(conn, req["offer_id"])
        if offer is None:
            continue
        course = get_course(conn, offer["course_id"])
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        payload = prepare_early_reminder_payload(dict(req), course, session)
        _push_to_user(conn, req["requester_id"], offer["id"], payload,
                      sender, now)
        conn.execute("UPDATE requests SET reminded_30_at=? WHERE id=?",
                     (_now_text(now), req["id"]))
        conn.commit()
        count += 1
    return count


def prepare_arrival_confirm_payload(request, course, session):
    """The "did you arrive?" reminder, at class start: a frozen
    requester who has not confirmed arrival yet gets one push, so
    the saver gets paid for a seat that was actually saved — the
    requester only forgot to tap "הגעתי". Round 20, item 11: this
    is the notification where buttons matter most — "הגעתי" and
    "המקום לא היה שם" — because the whole point of the push is
    the tap that settles the coin. The request id rides along."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    return {
        "title": "הגעת? אשר את ההגעה",
        "body": "%s מתחיל עכשיו%s — אם הגעת, אשר את ההגעה כדי"
                " שהשומר יקבל את המטבע." % (course["name"], where),
        "url": "/my",
        "tag": "seat-arrival-confirm-%s" % request["id"],
        "request_id": request["id"],
        "actions": [{"action": "confirm-arrival", "title": "הגעתי"},
                    {"action": "report-not-saved",
                     "title": "המקום לא היה שם"}],
    }


def find_arrival_confirm_due(conn, now=None):
    """Which frozen requests are due their arrival-confirm reminder:
    class has started, the no-show deadline (start+5, or start+10
    for a pending or accepted late declaration) has not passed yet,
    and no arrival-confirm reminder was sent. The deadline bound
    matters: past it the request is refunded as a no-show anyway,
    so reminding to confirm would be noise for a seat already
    lost. A request created after class start is due at the next
    tick — it still has until its deadline to confirm. Resolved
    or cancelled requests are never due — only frozen ones."""
    now = now or datetime.now()
    due = []
    for req in conn.execute(
            "SELECT * FROM requests WHERE status='frozen'"
            " AND reminded_arrival_at IS NULL").fetchall():
        offer = get_offer(conn, req["offer_id"])
        if offer is None:
            continue
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        if session is None:
            continue
        start = _session_start_datetime(dict(offer), dict(session))
        deadline = _no_show_deadline(dict(offer), dict(session), dict(req))
        if start <= now <= deadline:
            due.append(req)
    return due


def arrival_confirm_tick(conn, sender=None, now=None):
    """One arrival-confirm pass, same pattern as the other
    reminders: every due request gets a push to the requester, and
    reminded_arrival_at is set in the same commit as the send
    attempt — so a server restart never double-reminds, and a
    requester with no subscription is not retried every minute
    (the attempt itself is logged, so nothing is silent). Quiet
    hours and vacation are respected inside _push_to_user: a
    skipped push still sets the flag, so the requester does not
    get the reminder later — an arrival-confirm sent after the
    deadline would be noise. Returns how many were attempted."""
    now = now or datetime.now()
    count = 0
    for req in find_arrival_confirm_due(conn, now):
        offer = get_offer(conn, req["offer_id"])
        if offer is None:
            continue
        course = get_course(conn, offer["course_id"])
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        payload = prepare_arrival_confirm_payload(dict(req), course, session)
        _push_to_user(conn, req["requester_id"], offer["id"], payload,
                      sender, now)
        conn.execute("UPDATE requests SET reminded_arrival_at=? WHERE id=?",
                     (_now_text(now), req["id"]))
        conn.commit()
        count += 1
    return count


def find_saver_reminders_due(conn, now=None):
    """Open offers whose class starts within the next
    SAVER_REMINDER_MINUTES minutes, that still have at least one
    frozen request, and whose saver has not been reminded yet. One
    reminder per offer, not per request — the saver gets a single
    push listing everyone they are saving a seat for. A class that
    already started is not due: reminding a saver after the fact
    helps nobody. Returns the offer rows."""
    now = now or datetime.now()
    due = []
    for offer in conn.execute(
            "SELECT * FROM offers WHERE status='open'"
            " AND saver_reminded_at IS NULL").fetchall():
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        if session is None:
            continue
        start = _session_start_datetime(dict(offer), dict(session))
        delta = (start - now).total_seconds()
        if 0 <= delta <= SAVER_REMINDER_MINUTES * 60:
            frozen = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE offer_id=?"
                " AND status='frozen'", (offer["id"],)).fetchone()[0]
            if frozen:
                due.append(offer)
    return due


def prepare_saver_reminder_payload(offer, course, session, requester_names):
    """The reminder a SAVER gets 10 minutes before class: you promised
    to save seats for these people, do not forget. The requesters'
    names are in the body, so the saver knows exactly who is coming."""
    where = ""
    if session is not None:
        teacher = _teacher_of(session)
        where = " — בניין %s חדר %s" % (session["building"], session["room"])
        if teacher:
            where = " — %s, בניין %s חדר %s" % (
                teacher, session["building"], session["room"])
    names = ", ".join(requester_names) if requester_names else "מישהו"
    return {
        "title": "תזכורת: אתה שומר מקום",
        "body": "%s מתחיל בעוד 10 דקות%s — שמרת מקום ל: %s."
                % (course["name"], where, names),
        "url": "/my",
        "tag": "seat-saver-reminder-%s" % offer["id"],
    }


def saver_reminder_tick(conn, sender=None, now=None):
    """One saver-reminder pass: every due offer gets one push to its
    saver, and saver_reminded_at is set in the same commit as the
    send attempt — so a server restart never double-reminds, and a
    saver with no subscription is not retried every minute (the
    attempt itself is logged, so nothing is silent). Returns how
    many reminders were attempted."""
    now = now or datetime.now()
    count = 0
    for offer in find_saver_reminders_due(conn, now):
        course = get_course(conn, offer["course_id"])
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        names = [r["name"] for r in conn.execute(
            "SELECT u.name FROM requests r JOIN users u"
            " ON u.id = r.requester_id WHERE r.offer_id=?"
            " AND r.status='frozen' ORDER BY r.created_at",
            (offer["id"],)).fetchall()]
        payload = prepare_saver_reminder_payload(
            dict(offer), course, session, names)
        _push_to_user(conn, offer["saver_id"], offer["id"], payload,
                      sender, now)
        conn.execute("UPDATE offers SET saver_reminded_at=? WHERE id=?",
                     (_now_text(now), offer["id"]))
        conn.commit()
        count += 1
    return count


def background_loop(db_path=DB_PATH, interval_seconds=60):
    """The background thread: every minute, send due reminders and
    clear expired vacations. One daemon thread inside this same
    Python process — no cron, no Celery, no new service (see
    EXPLANATION.md for why). Each pass opens its own DB connection,
    because SQLite connections are not shared between threads. The
    loop never dies: a failure in one pass is printed and the next
    minute retries, so one bad row cannot kill the thread — and a
    daemon thread never blocks stopping the server."""
    while True:
        try:
            conn = connect(db_path)
            try:
                try:
                    early_reminder_tick(conn)
                except Exception as exc:  # noqa: BLE001 — survive
                    print("שגיאה בתזכורת מוקדמת: %s" % exc)
                try:
                    reminder_tick(conn)
                except Exception as exc:  # noqa: BLE001 — survive
                    print("שגיאה בתזכורות: %s" % exc)
                try:
                    arrival_confirm_tick(conn)
                except Exception as exc:  # noqa: BLE001 — survive
                    print("שגיאה בתזכורת אישור הגעה: %s" % exc)
                try:
                    saver_reminder_tick(conn)
                except Exception as exc:  # noqa: BLE001 — survive
                    print("שגיאה בתזכורות לשומר: %s" % exc)
                try:
                    auto_request_tick(conn)
                except Exception as exc:  # noqa: BLE001 — survive
                    print("שגיאה בבקשות אוטומטיות: %s" % exc)
                try:
                    clear_expired_vacations(conn)
                except Exception as exc:  # noqa: BLE001 — survive
                    print("שגיאה בניקוי חופשות שפגו: %s" % exc)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — survive
            print("שגיאה בחוט הרקע: %s" % exc)
        time.sleep(interval_seconds)


# ---------------------------------------------------------------- courses (admin)
@_serialized
def add_course(conn, name, kind="lecture"):
    name = (name or "").strip()
    if not name:
        return None, "נא להכניס שם קורס."
    if kind not in ("lecture", "tutorial"):
        kind = "lecture"
    try:
        cur = conn.execute("INSERT INTO courses (name, kind) VALUES (?, ?)",
                           (name, kind))
        conn.commit()
    except sqlite3.IntegrityError:
        return None, "הקורס הזה כבר קיים."
    return get_course(conn, cur.lastrowid), None


@_serialized
def add_session(conn, course_id, weekday, start_time, end_time,
                building, room, teacher=""):
    if get_course(conn, course_id) is None:
        return None, "קורס לא ידוע."
    try:
        weekday = int(weekday)
    except (TypeError, ValueError):
        return None, "היום לא תקין."
    if weekday not in range(7):
        return None, "היום לא תקין."
    if not _valid_time(start_time) or not _valid_time(end_time):
        return None, "השעות חייבות להיות HH:MM."
    if _to_minutes(end_time) <= _to_minutes(start_time):
        return None, "שעת הסיום חייבת להיות אחרי שעת ההתחלה."
    cur = conn.execute(
        "INSERT INTO course_sessions (course_id, weekday, start_time,"
        " end_time, building, room, teacher) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (course_id, weekday, start_time, end_time,
         (building or "").strip()[:20], (room or "").strip()[:20],
         (teacher or "").strip()[:40]))
    conn.commit()
    return get_session(conn, cur.lastrowid), None

# ---------------------------------------------------------------- HTML
# One page builder for every page. The CSS is ALWAYS inside <style> tags
# in <head> (the study planner once shipped CSS as raw page text on five
# pages because the tags were missing on some templates — this builder
# makes that class of bug impossible: there is only one place CSS lives).
# Round 20, item 10: manual theme (binary light/dark, fixed after a
# bug report 2026-11-10). The dark colours are one string, used once —
# only under html[data-theme="dark"] (the manual "dark" choice). There
# used to be a third "system" choice plus a prefers-color-scheme media
# query; Daniel reported that with the system on dark, asking for light
# kept flickering on every transition (the media query and the stored
# choice fought, and each page load flashed the wrong theme first). So
# the system mode is gone entirely: only light and dark, the choice is
# stored on the device, and the default (no stored choice) is light.
# {P} is the selector prefix, 'html[data-theme="dark"]'. One source of
# truth — change a colour in one place and it follows. The light
# colours are the base rules in CSS below; "light" means the dark
# block does not apply.
_DARK_BODY = """
{P}body{background:#0f172a;color:#f9fafb}
{P}nav{background:#1e293b;box-shadow:0 2px 8px rgba(0,0,0,.4)}
{P}nav a:hover{background:#14532d;color:#fff}
{P}a{color:#86efac}
{P}h2{border-right-color:#22c55e}
{P}.muted{color:#9ca3af}
{P}.offer{border-right-color:#22c55e}
{P}.offer-time{color:#d1d5db}
{P}.offer-seats{color:#86efac}
{P}.onboarding{background:#052e16;border-color:#22c55e}
{P}.card{border-color:#374151;background:#1e293b}
{P}.ok{background:#14532d;color:#fff}
{P}.err{background:#7f1d1d;color:#fff}
{P}button.secondary{background:#374151;color:#fff}
{P}button.secondary:hover{background:#4b5563}
{P}input, {P}select{background:#1e293b;color:#f9fafb;border-color:#4b5563}
{P}.push-card{background:#052e16;border-color:#22c55e}
{P}.badge{background:#14532d;color:#fff}
{P}.badge.soon{background:#713f12;color:#fff}
{P}.warn{background:#451a03;border-color:#f59e0b;color:#fff}
{P}.muted-banner{background:#1e293b;border-color:#94a3b8;color:#f9fafb}
{P}.today-list li{border-color:#374151}
{P}details{background:#1e293b;border-color:#374151}
{P}.logout-form button{color:#86efac}
{P}.verified{color:#4ade80}
{P}.profile-banner{background:#451a03;border-color:#f59e0b;color:#fff}
{P}.saved-seat-card{background:#052e16;border-color:#22c55e}
{P}.danger-zone{border-color:#ef4444}
{P}.theme-toggle{background:#374151;color:#fff}
{P}.theme-toggle:hover{background:#4b5563}
{P}.admin-table th,.admin-table td{border-color:#374151}
"""

CSS = """
/* Round 13 design polish (CSS only — no flow or markup changes):
   warmer page background, cards with a soft shadow, clearer
   hierarchy (bigger h1, accented h2), roomier buttons with a hover
   state, inputs with a visible focus ring, and a .btn class so a
   link can look like a button (used by the post-register banner
   on /follows). Dark mode keeps the same shapes in dark colours.
   Round 19: offer-card classes (.offer-title/.offer-time/
   .offer-seats), the course colour dot (.course-dot), the
   onboarding box (.onboarding), and the dark mode completed —
   links, muted text, h2 accent and the new classes now all have
   dark colours too (before, links stayed dark green on a dark
   background and were hard to read). Round 20: the VERIFIED badge
   (.verified), the profile banner (.profile-banner), the "your
   saved seat" card (.saved-seat-card), the danger zone
   (.danger-zone), the one-tap offer button (.offer-btn), the theme
   toggle (.theme-toggle) and the admin user table (.admin-table).
   The dark colours live in _DARK_BODY (see above) — one source of
   truth, used both for the system preference and the manual
   choice. */
body{font-family:system-ui,Arial,sans-serif;direction:rtl;max-width:880px;
margin:0 auto;padding:16px 20px 32px;line-height:1.65;color:#1a1a1a;background:#f7f8f5}
header{margin-bottom:16px}
nav{position:sticky;top:0;background:#fff;padding:10px 14px;border-bottom:2px solid #14532d;
display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;border-radius:0 0 12px 12px;
box-shadow:0 2px 8px rgba(0,0,0,.08)}
nav a{color:#14532d;font-weight:600;text-decoration:none;padding:4px 6px;border-radius:6px}
nav a:hover{background:#dcfce7}
.skip{position:absolute;right:-9999px;top:auto;width:1px;height:1px;overflow:hidden}
.skip:focus{position:static;width:auto;height:auto;padding:8px 12px;background:#14532d;color:#fff;border-radius:8px}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #14532d;outline-offset:2px}
h1{font-size:30px;margin:8px 0 16px;letter-spacing:-.5px}
h2{font-size:22px;margin:24px 0 10px;padding-right:10px;border-right:4px solid #14532d}
h3{font-size:18px;margin:18px 0 6px}
p{margin:10px 0}
ul{margin:10px 0;padding-right:22px}
button,input,select{font-size:18px;padding:10px 14px;margin:4px 0;
min-height:44px;box-sizing:border-box;max-width:100%}
input,select{border:1px solid #9ca3af;border-radius:8px;background:#fff}
input[type=checkbox]{min-height:0;accent-color:#14532d;width:20px;height:20px;vertical-align:middle}
button{background:#14532d;color:#fff;border:none;border-radius:10px;cursor:pointer;
transition:background .15s ease,transform .05s ease}
button:hover{background:#166534}
button:active{transform:scale(.98)}
button.big{font-size:20px;padding:14px 20px;width:100%}
button.secondary{background:#e5e7eb;color:#111}
button.secondary:hover{background:#d1d5db}
a{color:#14532d}
a.btn{display:inline-block;background:#14532d;color:#fff;border-radius:10px;
padding:10px 18px;font-size:18px;font-weight:700;text-decoration:none}
a.btn:hover{background:#166534}
.card{border:1px solid #d1d5db;border-radius:12px;padding:16px 18px;margin:14px 0;
background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.offer{border-right:6px solid #14532d}
/* Round 19: clearer offer cards. The course name is the biggest
   thing on the card, the time and place sit right under it, and the
   seats-left count is large and bold — the three facts a requester
   decides on. The saver line and the note stay, smaller. */
.offer-title{font-size:22px;font-weight:700;line-height:1.3}
.offer-time{font-size:17px;color:#374151;margin:2px 0}
.offer-seats{font-size:20px;font-weight:700;color:#14532d;margin:6px 0}
.offer-saver{margin:4px 0}
/* Round 19: the course colour dot (see COURSE_COLORS) — a small
   solid disc next to a course name. The name is always the real
   label; the dot is only a fast visual cue. */
.course-dot{display:inline-block;width:14px;height:14px;border-radius:50%;vertical-align:middle;margin-left:6px}
/* Round 19: the new-user onboarding box — same shape as the push
   card, so a new user recognises it as "do this next". */
.onboarding{border:2px solid #14532d;background:#f0fdf4}
.push-card{border:2px solid #14532d;background:#f0fdf4}
.coin{font-weight:700}
.badge{display:inline-block;background:#dcfce7;border-radius:6px;padding:2px 8px;font-weight:600}
.badge.soon{background:#fef3c7}
.ok{background:#dcfce7;padding:12px 14px;border-radius:10px;border-right:4px solid #14532d}
.err{background:#fee2e2;padding:12px 14px;border-radius:10px;border-right:4px solid #b91c1c}
.warn{background:#fef3c7;border:2px solid #d97706;padding:12px 14px;border-radius:10px;margin:12px 0}
.muted-banner{background:#f1f5f9;border-right:4px solid #64748b;padding:12px 14px;border-radius:10px;margin:12px 0}
.today-list{list-style:none;padding:0;margin:8px 0}
.today-list li{padding:12px 4px;border-bottom:1px solid #e5e7eb}
.today-list li:last-child{border-bottom:none}
.muted{color:#6b7280}
details{border:1px solid #d1d5db;border-radius:12px;padding:12px 16px;margin:14px 0;background:#fff}
summary{cursor:pointer;font-weight:700;font-size:18px;padding:4px 0}
.logout-form{display:inline;margin:0}
.logout-form button{background:none;color:#14532d;text-decoration:underline;
padding:0;min-height:0;font-size:16px}
.logout-form button:hover{background:none}
/* Round 20: the VERIFIED badge — a green checkmark next to a name
   the admin approved. Text, not an image, so it renders everywhere
   (including the plain-text notification log). */
.verified{color:#15803d;font-weight:700}
/* Round 20: the profile-completion banner — a soft amber box on the
   home page for users who still need a full name or a phone. A
   prompt, never a block: the app still works while it is there. */
.profile-banner{background:#fef3c7;border:2px solid #d97706;padding:12px 14px;border-radius:10px;margin:12px 0}
/* Round 20: the "your saved seat" card — the one card a requester
   sees after acceptance, with the saver's name, phone and where to
   meet. Green border, so it reads as "this is your seat". */
.saved-seat-card{border:2px solid #14532d;background:#f0fdf4}
/* Round 20: the danger zone — account deletion. Red border, so it
   reads as "think before you touch this". */
.danger-zone{border:2px solid #b91c1c}
/* Round 20: the one-tap offer button next to a lesson on /today —
   a small button, not a full-width one, so it sits on the same
   line as the lesson it belongs to. */
.offer-btn{font-size:16px;padding:6px 12px;min-height:36px;margin-right:8px}
/* Round 20: the theme toggle in the nav — a small grey button, so
   it does not look like a call to action. */
.theme-toggle{background:#e5e7eb;color:#111;font-size:15px;padding:6px 10px;min-height:36px}
.theme-toggle:hover{background:#d1d5db}
/* Round 20: the admin user table — a plain table, one row per user
   (name, phone, coins, verified, actions). */
.admin-table{width:100%;border-collapse:collapse}
.admin-table th,.admin-table td{border-bottom:1px solid #d1d5db;padding:8px 6px;text-align:right;vertical-align:top}
.admin-table th{font-size:15px;color:#6b7280}
""" + _DARK_BODY.replace(
    "{P}", 'html[data-theme="dark"] ')


def esc(text):
    return html.escape(str(text if text is not None else ""))


def page_html(title, body, user=None, message=None, error=None):
    msg = ""
    if message:
        msg += '<p class="ok" role="status">%s</p>' % esc(message)
    if error:
        msg += '<p class="err" role="alert">%s</p>' % esc(error)
    who = ""
    if user is not None:
        who = ('<p>מחובר כ־<strong>%s</strong>%s — מאזן: '
               '<span class="coin">%s מטבעות</span> '
               '<form class="logout-form" method="post" action="/logout">'
               '<button type="submit">התנתק</button></form></p>'
               % (esc(user["name"]), verified_badge_html(user),
                  user["balance"]))
    else:
        who = ('<p><a href="/login">התחברות</a> · '
               '<a href="/register">הרשמה</a></p>')
    return ("<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            # Round 19: the app icon. apple-touch-icon is the icon
            # iOS puts on the Home Screen (180x180); the manifest
            # tells Android the same, and theme-color colours the
            # browser chrome around the app. The favicon is the
            # small icon in the browser tab.
            "<link rel=\"apple-touch-icon\" href=\"/static/icon-180.png\">"
            "<link rel=\"icon\" type=\"image/png\" href=\"/static/favicon.png\">"
            "<link rel=\"manifest\" href=\"/static/manifest.json\">"
            "<meta name=\"theme-color\" content=\"#14532d\">"
            "<title>%s — שומר מקומות</title><style>%s</style>"
            "<script src=\"/static/app.js?v=21\" defer></script></head>"
            "<body><a class=\"skip\" href=\"#main\">דלג לתוכן</a>"
            "<header><nav><a href=\"/\">בית</a><a href=\"/today\">היום</a>"
            # Round 20, item 19: the weekly view, next to "today".
            "<a href=\"/week\">השבוע</a>"
            "<a href=\"/offers\">הצעות</a>"
            "<a href=\"/offer\">הצע מקום</a><a href=\"/wanted\">מחפשים מקום</a>"
            "<a href=\"/courses\">קורסים</a>"
            "<a href=\"/follows\">המעקבים שלי</a><a href=\"/my\">המקומות שלי</a>"
            # The profile (name + phone) lives on /my, but it was
            # buried at the bottom — Daniel, 2026-11-10: it was not
            # in an accessible place. A nav link jumps straight to
            # the profile section (#profile).
            "<a href=\"/my#profile\">הפרופיל שלי</a>"
            "<a href=\"/push\">התראות</a><a href=\"/help\">איך זה עובד?</a>"
            # Round 20, item 10: the theme toggle. A plain button;
            # app.js (deferred) sets its label and wires the click
            # — without JS it is inert and the page stays light.
            # The label is set by JS because only JS knows which
            # theme is active right now. Binary light/dark only —
            # no system mode (see the _DARK_BODY comment).
            "<button type=\"button\" class=\"theme-toggle\" "
            "id=\"theme-toggle\" title=\"החלפת ערכת נושא: "
            "בהיר / כהה\">🌓 ערכת נושא</button>"
            "%s</nav>%s</header>"
            "<main id=\"main\"><h1>%s</h1>%s%s</main></body></html>"
            % (esc(title), CSS,
               '<a href="/admin">דף בקרה</a>' if is_admin(user) else "",
               who, esc(title), msg, body))


def _teacher_of(session):
    """Teacher name, or '' when the row has none. Works for both
    sqlite3.Row and plain dict (tests pass dicts)."""
    try:
        return (session["teacher"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def course_color(course_id):
    """The fixed colour of one course — see COURSE_COLORS. Works
    with an int id or a row/dict carrying one; an unknown id falls
    back to the first colour instead of crashing the page."""
    try:
        cid = int(course_id["id"])
    except (TypeError, KeyError, IndexError):
        try:
            cid = int(course_id)
        except (TypeError, ValueError):
            cid = 0
    return COURSE_COLORS[cid % len(COURSE_COLORS)]


def course_dot_html(course_id):
    """A small coloured dot marking a course name — the visual half
    of the course colour (the name next to it is the real label)."""
    return ('<span class="course-dot" style="background:%s" '
            'aria-hidden="true"></span>' % course_color(course_id))


def _session_label(session, course_name=None):
    label = "%s %s–%s" % (WEEKDAY_NAMES[session["weekday"]],
                          session["start_time"], session["end_time"])
    if course_name:
        label = "%s: %s" % (course_name, label)
    teacher = _teacher_of(session)
    place = "בניין %s חדר %s" % (session["building"], session["room"])
    details = (teacher + ", " + place) if teacher else place
    return label + " (" + details + ")"


def floor_warning_html(user):
    """Warning shown on home and /my when the logged-in user's balance
    is at the floor (MIN_BALANCE): they cannot request a seat until
    they save one for someone else and earn a coin. Returns '' at any
    higher balance — the floor is not mentioned until it matters.
    The full explanation of the floor lives only on /help."""
    if user is None or user["balance"] > MIN_BALANCE:
        return ""
    return ('<div class="warn" role="alert"><strong>המאזן שלך על '
            'מינוס %d — אי אפשר לבקש מקום עכשיו.</strong> שמור מקום '
            'למישהו אחר, וכשיאשרו הגעה תקבל מטבע ותוכל לבקש שוב. '
            '<a href="/help">איך זה עובד?</a></div>'
            % -MIN_BALANCE)


def muted_banner_html(user, now=None):
    """A small banner on the home page when the logged-in user's
    notifications are muted RIGHT NOW — quiet hours active, or
    vacation on — so they are never surprised by silence. Returns
    '' when nothing mutes them (or when nobody is logged in).
    Vacation wins over quiet hours: on vacation, the hours do not
    matter. Quiet hours say until when (the end hour); vacation
    says until when only if an end date was set. The banner links
    to /my, where both settings live."""
    if user is None:
        return ""
    if is_on_vacation(user):
        try:
            until = user["vacation_until"]
        except (KeyError, IndexError, TypeError):
            until = None
        if until:
            try:
                until_text = datetime.strptime(
                    until, "%Y-%m-%d").strftime("%d.%m")
                text = ("במצב חופשה — ההתראות מושתקות עד %s."
                        % until_text)
            except (ValueError, TypeError):
                text = "במצב חופשה — ההתראות מושתקות עד שתחזור."
        else:
            text = "במצב חופשה — ההתראות מושתקות עד שתחזור."
        return ('<div class="muted-banner" role="status">%s '
                '<a href="/my">ניהול חופשה</a></div>' % text)
    if in_quiet_hours(user, now):
        try:
            end = user["quiet_end"]
        except (KeyError, IndexError, TypeError):
            end = None
        if end is None:
            return ""
        return ('<div class="muted-banner" role="status">ההתראות '
                'מושתקות עד %02d:00. <a href="/my">שינוי שעות '
                'שקטות</a></div>' % int(end))
    return ""


def onboarding_html(conn, user):
    """Round 19: the 3-step start guide on the home page, shown
    only to a logged-in user who follows nothing yet — that is
    exactly a new user (or someone who removed all follows). Once
    they follow one course it disappears: they know the flow by
    then. The steps are links, not just text, so each step is one
    tap away."""
    if user is None:
        return ""
    n = conn.execute("SELECT COUNT(*) FROM follows WHERE user_id=?",
                     (user["id"],)).fetchone()[0]
    if n:
        return ""
    return ('<div class="card onboarding"><h2>מתחילים? 3 צעדים</h2><ol>'
            '<li><a href="/follows">עקוב אחרי הקורסים שלך</a> — כדי '
            'לקבל התראה כשמציעים מקום.</li>'
            '<li><a href="/push">הפעל התראות</a> — אחרת לא תדע '
            'כשמציעים.</li>'
            '<li><a href="/offers">בקש מקום</a> כשמישהו מציע — מטבע '
            'אחד, שעובר לשומר רק כשתגיע ותאשר.</li>'
            '</ol></div>')


def _countdown_text(offer, session, now=None):
    """How long until this offer's class starts, in plain words —
    rendered server-side at page load, no JavaScript timer. Only
    open offers get a countdown: a closed offer has nothing to
    count down to. A class that already started says so plainly
    (in practice settle_expired closes such offers before the page
    renders, so this is a safety net, not a state the user should
    see). Minutes are floored, exactly like the offer-window math:
    25 minutes 50 seconds shows as 25 minutes, never 26."""
    if session is None or offer["status"] != "open":
        return ""
    now = now or datetime.now()
    try:
        start = _session_start_datetime(offer, session)
    except (ValueError, KeyError, TypeError, AttributeError):
        return ""
    delta = (start - now).total_seconds()
    if delta <= 0:
        return "השיעור התחיל"
    if delta < 60:
        return "השיעור מתחיל בעוד פחות מדקה"
    return "השיעור מתחיל בעוד %d דקות" % int(delta // 60)


# A request only counts towards reliability once it reached a
# decision: the requester either arrived (confirmed) or did not
# (not_saved / no_show). Cancelling in time is fine, not a broken
# promise, so cancelled requests — by the requester, by the saver
# cancelling the offer, or by an admin refund — are excluded from
# both the numerator and the denominator.
DECIDED_REQUEST_STATUSES = ("confirmed", "not_saved", "no_show")


def reliability(conn, user_id, role):
    """(kept, decided) for one user in one role — 'saver' or
    'requester'. `kept` is how many of their decided requests ended
    in a confirmed arrival; `decided` is how many requests reached
    a decision at all. Returns None when the user has no decided
    requests yet in that role — no rating is shown then, not 0/0.
    Computed from the requests table only; no new columns. Round
    20, item 14: for a saver, an upheld complaint on a request
    that still counts as confirmed (the coin already moved) adds
    one "not kept" decision — the admin said the seat was not
    saved, so the confirmed arrival is overturned. A complaint on
    a frozen request resolves the request itself (see
    decide_complaint), so it is already counted once and adds
    nothing here. A complaint on an already not-kept request adds
    nothing either — it was counted when the request resolved."""
    col = "saver_id" if role == "saver" else "requester_id"
    placeholders = ",".join("?" * len(DECIDED_REQUEST_STATUSES))
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM requests WHERE %s=?"
        " AND status IN (%s) GROUP BY status"
        % (col, placeholders),
        (user_id,) + DECIDED_REQUEST_STATUSES).fetchall()
    total = sum(r["n"] for r in rows)
    kept = sum(r["n"] for r in rows if r["status"] == "confirmed")
    if role == "saver":
        try:
            overturned = conn.execute(
                "SELECT COUNT(*) FROM complaints c JOIN requests r"
                " ON r.id = c.request_id"
                " WHERE c.saver_id=? AND c.status='upheld'"
                " AND r.status='confirmed'", (user_id,)).fetchone()[0]
        except sqlite3.OperationalError:
            # A database from before complaints existed (the table
            # is created by init_db, but a raw connection skips
            # it): no complaints, so nothing to overturn.
            overturned = 0
        total += overturned
    if total == 0:
        return None
    return kept, total


def reliability_label(conn, user_id, role):
    """'אמינות: 83% (5/6)' for one user in one role, or '' when
    they have no decided requests yet — an empty rating says
    nothing, so the page shows nothing instead of a misleading
    0/0. Round 20, item 8: the percentage is the number a
    requester decides on at a glance; the count stays next to it,
    so "100% (1/1)" is not confused with "100% (20/20)"."""
    result = reliability(conn, user_id, role)
    if result is None:
        return ""
    kept, total = result
    pct = round(kept * 100 / total) if total else 0
    return "אמינות: %d%% (%d/%d)" % (pct, kept, total)

# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "SeatSaver/0.1"

    def log_message(self, *args):  # keep the console quiet
        pass

    # -- plumbing
    def _conn(self):
        conn = connect()
        init_db(conn)
        return conn

    def _current_user(self, conn):
        """The logged-in user, from the session cookie ONLY. There is
        no user_id in any URL or form any more: a forged user_id used
        to let anyone act as anyone else, and that hole is closed.
        Returns the user row, or None when not logged in."""
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"seat_session=([A-Za-z0-9_\-]+)", cookie)
        if not m:
            return None
        return get_user_from_session(conn, m.group(1))

    def _session_token(self):
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"seat_session=([A-Za-z0-9_\-]+)", cookie)
        return m.group(1) if m else None

    def _send_html(self, body, status=200, cookie=None):
        # דפי HTML לעולם לא נשמרים במטמון: דף /push ישן שנטען
        # מהמטמון מפנה ל־/static/app.js בלי הגרסה, והטלפון מריץ
        # קוד ישן אחרי פריסה (נמצא בשידור חי 2026-11-10 — אפילו
        # אחרי מחיקה והוספה מחדש של אייקון מסך הבית, הדף הישן
        # חזר, וההפעלה מחדש יצרה שוב מנוי עם מפתח ישן). הכתובת
        # הממוספרת (?v=21) עוזרת רק כשהדף עצמו טרי — אז גם הדף
        # נשלח עם no-store, כמו קובצי ה־.js מסבב 16.
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data, ctype, status=200, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200, extra_headers=None):
        self._send_bytes(json.dumps(obj).encode("utf-8"),
                         "application/json", status, extra_headers)

    def _redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return urllib.parse.parse_qs(raw)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _login_cookie(self, conn, user_id):
        token = create_session(conn, user_id)
        return ("seat_session=%s; HttpOnly; Path=/; Max-Age=%d"
                % (token, SESSION_DAYS * 24 * 3600))

    def _login_redirect(self, error=None):
        loc = "/login"
        if error:
            loc += "?error=" + urllib.parse.quote(error)
        self._redirect(loc)

    # -- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, params = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path.startswith("/static/"):
            return self._serve_static(path)
        if path == "/vapid-public-key":
            _, pub = get_vapid_keys()
            # The public key must NEVER be cached: a stale cached
            # key makes the browser subscribe under a dead keypair,
            # and every push to that subscription 403s and gets
            # auto-removed — re-enabling only recreates the same
            # broken subscription (found live 2026-11-10). So the
            # response says no-store, and app.js asks for no-store
            # too; the key always comes fresh from the server.
            return self._send_json(
                {"public_key": pub},
                extra_headers={"Cache-Control": "no-store",
                               "Pragma": "no-cache"})
        if path == "/sw.js":
            return self._serve_static("/static/sw.js")
        conn = self._conn()
        try:
            settle_expired(conn)
            user = self._current_user(conn)
            msg = params.get("msg", [None])[0]
            saved = params.get("saved", [None])[0]
            err = params.get("error", [None])[0]
            message = None
            if msg:
                message = urllib.parse.unquote(msg)
            elif saved:
                message = "נשמר ✓"
            error = urllib.parse.unquote(err) if err else None
            if path in ("/", ""):
                return self._send_html(self._home(conn, user, message, error))
            if path == "/today":
                return self._send_html(self._today(conn, user, message, error))
            # Round 20, item 19: the weekly view.
            if path == "/week":
                return self._send_html(self._week(conn, user, message, error))
            if path == "/register":
                return self._send_html(self._register(conn, user, message, error))
            if path == "/login":
                return self._send_html(self._login(conn, user, message, error))
            if path == "/set-password":
                return self._send_html(self._set_password_page(
                    conn, user, params.get("name", [""])[0], message, error))
            if path == "/courses":
                return self._send_html(self._courses(conn, user, message, error))
            if path == "/offers":
                return self._send_html(self._offers(
                    conn, user, message, error, params))
            if path == "/wanted":
                return self._send_html(self._wanted(
                    conn, user, message, error, params))
            if path == "/admin":
                if not is_admin(user):
                    return self._send_html(
                        page_html("אין גישה",
                                  "<p>הדף הזה לא בשבילך.</p>", user),
                        status=403)
                return self._send_html(self._admin(conn, user, message, error))
            if path == "/offer":
                # Round 20, item 3: one-tap offer from /today — the
                # button links here with ?session_id=N, and the form
                # opens with that session already selected. A bad or
                # out-of-window id is simply ignored (the form shows
                # its normal list); the POST handler enforces the
                # window anyway.
                prefill = params.get("session_id", [""])[0]
                try:
                    prefill = int(prefill) if prefill else None
                except (TypeError, ValueError):
                    prefill = None
                return self._send_html(self._offer_form(
                    conn, user, message, error, prefill))
            if path == "/edit-offer":
                return self._send_html(self._edit_offer_form(
                    conn, user, params.get("offer_id", [""])[0], message,
                    error))
            if path == "/follows":
                return self._send_html(self._follows(
                    conn, user, message, error, params))
            if path == "/my":
                return self._send_html(self._my(conn, user, message, error))
            if path == "/push":
                return self._send_html(self._push(conn, user, message, error))
            # Round 20, item 6: the account-deletion confirmation
            # pages. GET only shows them — nothing is deleted by
            # opening the page.
            if path == "/delete-account":
                return self._send_html(self._delete_account_page(
                    conn, user, message, error))
            if path == "/admin-delete-account":
                if not is_admin(user):
                    return self._send_html(
                        page_html("אין גישה",
                                  "<p>הדף הזה לא בשבילך.</p>", user),
                        status=403)
                return self._send_html(self._admin_delete_account_page(
                    conn, user, params.get("user_id", [""])[0],
                    message, error))
            if path == "/help":
                return self._send_html(self._help(conn, user, message, error))
            self._send_html(page_html("לא נמצא", "<p>אין דף כזה.</p>",
                                      user), status=404)
        finally:
            conn.close()

    def _serve_static(self, path):
        # A version query (?v=21) forces the browser to fetch a
        # fresh copy instead of reusing a cached one — strip it
        # before resolving the file. And the .js files are never
        # cached at all: a cached app.js kept the phone running old
        # code after a deploy, so the round-15 unsubscribe fix
        # never ran on the phone (found live 2026-11-10 — even
        # after deleting and re-adding the Home Screen app, the
        # old file came back). A new URL breaks the cache; the
        # no-store headers keep it broken for the next round.
        path = path.split("?", 1)[0]
        name = path.rsplit("/", 1)[-1]
        # Round 19: the Home Screen icon files and the manifest join
        # the two JavaScript files here. Same rule for all of them:
        # no-store, so a deploy always reaches the phone — a cached
        # icon would keep showing the old letter (ש) after the real
        # icon exists. The manifest is tiny and fetched on every
        # page load, so not caching it costs nothing.
        allowed = {"app.js": "application/javascript",
                   "sw.js": "application/javascript",
                   "icon-180.png": "image/png",
                   "icon-192.png": "image/png",
                   "icon-512.png": "image/png",
                   "favicon.png": "image/png",
                   "manifest.json": "application/manifest+json"}
        if name not in allowed:
            return self._send_bytes(b"Not found", "text/plain", 404)
        full = os.path.join(BASE_DIR, "static", name)
        if not os.path.exists(full):
            return self._send_bytes(b"Not found", "text/plain", 404)
        with open(full, "rb") as f:
            data = f.read()
        return self._send_bytes(
            data, allowed[name],
            extra_headers={"Cache-Control": "no-store",
                           "Pragma": "no-cache"})

    def _need_login(self, user, message, error):
        return page_html("התחברות",
                         "<p>כדי להשתמש בזה צריך להתחבר קודם. "
                         "<a href=\"/login\">התחברות</a> או "
                         "<a href=\"/register\">הרשמה</a>.</p>",
                         user, message, error)

    # -- pages
    def _home(self, conn, user, message, error):
        now = datetime.now()
        offers = conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name, u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id"
            " WHERE o.status='open' ORDER BY o.created_at DESC").fetchall()
        # Earliest class first, not newest first — creation order says
        # nothing about urgency, class start does.
        offers = sort_offers_by_start(conn, offers)
        body = ["<p>מישהו מגיע מוקדם, מציע לשמור מקום, וכל מי שעוקב "
                "אחרי הקורס מקבל התראה אוטומטית לטלפון. מטבע אחד "
                "לבקשה, שעובר לשומר רק כשמגיעים ומאשרים.</p>"]
        # Round 19: a new user who follows nothing yet gets the
        # 3-step start guide first, before any other box.
        body.append(onboarding_html(conn, user))
        # Round 20, items 13/15: a user who still needs a full name
        # or a phone gets a banner — a prompt, never a block. The
        # app still works while it is there; the banner just says
        # what is missing and where to fix it (/my).
        if user is not None and user_needs_profile(user):
            missing = []
            name = (_user_field(user, "name") or "").strip()
            if len(name.split()) < 2:
                missing.append("שם מלא (שם פרטי ושם משפחה)")
            if not _valid_phone(_user_field(user, "phone")):
                missing.append("מספר טלפון נייד")
            body.append('<div class="profile-banner" role="status">'
                        '<strong>הפרופיל שלך לא מלא.</strong> חסר: %s. '
                        '<a href="/my#profile">השלם בפרופיל שלי</a> — בלי שם '
                        'מלא וטלפון, אף אחד לא יידע במי מדובר כשתציע '
                        'או תבקש מקום.</div>' % esc(" ו".join(missing)))
        if user is None:
            body.append("<p><strong>צעד ראשון:</strong> <a href=\"/register\">"
                        "הירשם עם שם וסיסמה</a> (הרשמה פתוחה — כל מי "
                        "שיש לו את הקישור יכול להצטרף). אחר כך עוקבים "
                        "אחרי הקורסים ו<a href=\"/push\">מפעילים "
                        "התראות</a>.</p>")
        elif not has_push_subscription(conn, user["id"]):
            body.append('<div class="card push-card"><strong>ההתראות '
                        'עדיין כבויות.</strong> בלי התראות לא תדע כשמישהו '
                        'מציע מקום. <a href="/push">הפעל התראות עכשיו'
                        '</a></div>')
        # At the floor: warn here, so the user learns what to do next.
        # At any higher balance this returns '' and adds nothing.
        body.append(floor_warning_html(user))
        # Round 14: muted right now (quiet hours / vacation)? Say so
        # here, so silence is never a surprise.
        body.append(muted_banner_html(user, now))
        # Today's sessions first: what is happening today, and which
        # offer window is open right now.
        body.append(self._today_sessions_html(conn, now, user))
        if offers:
            body.append("<h2>הצעות פתוחות עכשיו</h2>")
            for o in offers:
                body.append(self._offer_card(conn, o, user))
        else:
            body.append("<p class=\"muted\">אין הצעות פתוחות כרגע. "
                        "ההצעות נפתחות 60 דקות לפני השיעור.</p>")
        body.append("<h2>קורסים</h2><ul>")
        for c in conn.execute("SELECT * FROM courses ORDER BY name"):
            n = conn.execute("SELECT COUNT(*) FROM course_sessions"
                             " WHERE course_id=?", (c["id"],)).fetchone()[0]
            body.append("<li>%s %s — %d שיעורים</li>"
                        % (course_dot_html(c["id"]), esc(c["name"]), n))
        body.append("</ul><p><a href=\"/courses\">כל השיעורים והוספת "
                    "קורס</a></p>")
        return page_html("שומר מקומות", "".join(body), user, message, error)

    def _today_sessions_html(self, conn, now, user=None):
        """Today's lessons, with the offer-window badge per lesson
        (open now / opens at / started / finished). Shared by home
        and /today so the two pages can never drift apart. Round 20,
        item 3: when the offer window is open and a user is logged
        in, the lesson also gets a "הצע מקום" button — one tap
        opens the offer form with this session already selected,
        so offering a seat is one tap instead of a form and a
        dropdown. The button only appears while the window is open:
        offering for a lesson that already started, or whose window
        has not opened yet, makes no sense."""
        today = now.weekday()
        sessions = conn.execute(
            "SELECT s.*, c.name AS course_name FROM course_sessions s"
            " JOIN courses c ON c.id=s.course_id"
            " WHERE s.weekday=? ORDER BY s.start_time", (today,)).fetchall()
        parts = ["<h2>השיעורים של היום</h2>"]
        if sessions:
            parts.append('<ul class="today-list">')
            for s in sessions:
                status, opens = session_window_status(dict(s), now)
                if status == "open":
                    badge = ('<span class="badge">חלון ההצעה פתוח '
                             'עכשיו</span>')
                elif status == "not_yet":
                    badge = ('<span class="badge soon">ההצעה נפתחת '
                             'ב־%s</span>' % opens)
                elif status == "started":
                    badge = '<span class="badge soon">השיעור התחיל</span>'
                else:
                    badge = '<span class="muted">השיעור הסתיים</span>'
                offer_btn = ""
                if user is not None and status == "open":
                    offer_btn = (' <a class="btn offer-btn" '
                                 'href="/offer?session_id=%d">הצע מקום</a>'
                                 % s["id"])
                parts.append("<li>%s %s <span class=\"muted\">%s</span> %s%s</li>"
                             % (course_dot_html(s["course_id"]),
                                esc(_session_label(s, s["course_name"])),
                                esc(followers_label(
                                    followers_count_for_session(conn, dict(s)))),
                                badge, offer_btn))
            parts.append("</ul>")
        else:
            parts.append("<p class=\"muted\">אין שיעורים היום.</p>")
        return "".join(parts)

    def _today(self, conn, user, message, error):
        """Round 19: one focused page for today — today's lessons
        and today's open offers, next lesson first. The home page
        shows the same pieces among everything else; this page is
        only today, for the moment you open the app before class.
        The sessions come from the shared _today_sessions_html and
        the offers from the open-offers query, filtered to sessions
        of today."""
        now = datetime.now()
        today = now.weekday()
        offers = conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name, u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id"
            " JOIN course_sessions s ON s.id=o.session_id"
            " WHERE o.status='open' AND s.weekday=?"
            " ORDER BY o.created_at DESC", (today,)).fetchall()
        offers = sort_offers_by_start(conn, offers)
        body = [self._today_sessions_html(conn, now, user)]
        body.append("<h2>הצעות פתוחות להיום</h2>")
        if offers:
            for o in offers:
                body.append(self._offer_card(conn, o, user))
        else:
            body.append("<p class=\"muted\">אין הצעות פתוחות כרגע. "
                        "ההצעות נפתחות 60 דקות לפני השיעור.</p>")
        body.append('<p><a href="/offers">כל ההצעות</a></p>')
        return page_html("היום", "".join(body), user, message, error)

    def _week(self, conn, user, message, error):
        """Round 20, item 19: the weekly view — the week's sessions
        and open offers, like /today but for the whole week. Server
        side, reusing the same session/offer helpers. Days are a
        calendar week starting Sunday (Daniel, 2026-11-10: the week
        starts on Sunday — the Monday-first version was my misread
        of his report that the week was starting on Monday), each
        with its sessions and the open offers for that day. Open offers only ever exist
        for today (born 60 minutes before class), so the offers
        section is honest about that: it lists the open offers of
        the week (today's, in practice) and the sessions of the
        coming days, so a user can see what is ahead and when to
        follow it."""
        now = datetime.now()
        today = now.weekday()
        # A calendar week, Sunday first: DISPLAY_WEEKDAYS.
        days = DISPLAY_WEEKDAYS
        body = ["<p>כל השיעורים של השבוע, וההצעות הפתוחות. ההצעות "
                "נפתחות 60 דקות לפני השיעור, אז הצעות פתוחות יש רק "
                "להיום — השאר כאן כדי לתכנן מראש.</p>"]
        for day in days:
            sessions = conn.execute(
                "SELECT s.*, c.name AS course_name FROM course_sessions s"
                " JOIN courses c ON c.id=s.course_id"
                " WHERE s.weekday=? ORDER BY s.start_time",
                (day,)).fetchall()
            offers = conn.execute(
                "SELECT o.*, c.name AS course_name,"
                " u.name AS saver_name, u.verified AS saver_verified"
                " FROM offers o JOIN courses c ON c.id=o.course_id"
                " JOIN users u ON u.id=o.saver_id"
                " JOIN course_sessions s ON s.id=o.session_id"
                " WHERE o.status='open' AND s.weekday=?"
                " ORDER BY o.created_at DESC", (day,)).fetchall()
            offers = sort_offers_by_start(conn, offers)
            body.append("<h2>%s</h2>" % WEEKDAY_NAMES[day])
            if sessions:
                body.append('<ul class="today-list">')
                for s in sessions:
                    # Only today's sessions get a live window badge;
                    # a future day's window has not opened yet, so
                    # its state is just the time.
                    if day == today:
                        status, opens = session_window_status(dict(s), now)
                        if status == "open":
                            badge = ('<span class="badge">חלון ההצעה '
                                     'פתוח עכשיו</span>')
                        elif status == "not_yet":
                            badge = ('<span class="badge soon">ההצעה '
                                     'נפתחת ב־%s</span>' % opens)
                        elif status == "started":
                            badge = ('<span class="badge soon">השיעור '
                                     'התחיל</span>')
                        else:
                            badge = ('<span class="muted">השיעור '
                                     'הסתיים</span>')
                        offer_btn = ""
                        if user is not None and status == "open":
                            offer_btn = (' <a class="btn offer-btn" '
                                         'href="/offer?session_id=%d">'
                                         'הצע מקום</a>' % s["id"])
                    else:
                        badge = ""
                        offer_btn = ""
                    body.append("<li>%s %s <span class=\"muted\">%s</span> %s%s</li>"
                                % (course_dot_html(s["course_id"]),
                                   esc(_session_label(s, s["course_name"])),
                                   esc(followers_label(
                                       followers_count_for_session(
                                           conn, dict(s)))),
                                   badge, offer_btn))
                body.append("</ul>")
            else:
                body.append("<p class=\"muted\">אין שיעורים ביום הזה.</p>")
            if offers:
                for o in offers:
                    body.append(self._offer_card(conn, o, user))
        return page_html("השבוע", "".join(body), user, message, error)

    def _on_way_time(self, request):
        """HH:MM of the 'on my way' tap, or '' when not tapped. The
        stored value is a full timestamp; the display is just the time."""
        try:
            value = request["on_the_way"]
        except (KeyError, IndexError, TypeError):
            return ""
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%H:%M")
        except (ValueError, TypeError):
            return ""


    def _arrived_time(self, offer):
        """HH:MM of the 'already in class' tap, or '' when not
        tapped. Same shape as _on_way_time: full timestamp stored,
        only the time shown."""
        try:
            value = offer["arrived_at"]
        except (KeyError, IndexError, TypeError):
            return ""
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).strftime("%H:%M")
        except (ValueError, TypeError):
            return ""


    def _offer_requesters_html(self, conn, offer, show_saver_actions=False):
        """Who requested a seat in this offer, for the saver's eyes:
        requester name, Hebrew status, and 'on the way' with the time
        when the requester tapped it. Empty when nobody requested yet —
        the saver then sees nothing extra, not an empty box. On /my
        (show_saver_actions) a frozen request also gets a 'בטל בקשה
        זו' button, so the saver can cancel ONE request without
        cancelling the whole offer — the button is not shown on the
        public offer cards, where it does not belong. Round 20,
        item 16 ("who's coming"): the requester names are the point
        — the saver sees WHO is coming, with the VERIFIED badge
        when the admin approved them, so they know who to expect.
        This list is only ever rendered for the saver (see
        _offer_card and /my), never for other requesters."""
        reqs = conn.execute(
            "SELECT r.*, u.name AS requester_name,"
            " u.verified AS requester_verified FROM requests r"
            " JOIN users u ON u.id = r.requester_id"
            " WHERE r.offer_id=? ORDER BY r.created_at", (offer["id"],)).fetchall()
        if not reqs:
            return ""
        lines = ["<p>ביקשו מקום:</p><ul>"]
        for r in reqs:
            status = REQUEST_STATUS_HE.get(r["status"], r["status"])
            extra = ""
            late_buttons = ""
            if r["status"] == "frozen":
                when = self._on_way_time(r)
                if when:
                    extra = " — בדרך (%s)" % when
                try:
                    late = r["late_status"]
                except (KeyError, IndexError, TypeError):
                    late = None
                if late == "pending":
                    extra += " — מאחר (מחכה להחלטה)"
                    if show_saver_actions:
                        late_buttons = (
                            ' <form method="post" action="/decide-late" '
                            'style="display:inline"><input type="hidden" '
                            'name="request_id" value="%s"><input '
                            'type="hidden" name="decision" value="accept">'
                            '<button type="submit">אשר איחור</button></form>'
                            ' <form method="post" action="/decide-late" '
                            'style="display:inline"><input type="hidden" '
                            'name="request_id" value="%s"><input '
                            'type="hidden" name="decision" value="refuse">'
                            '<button type="submit" class="secondary">דחה '
                            'איחור</button></form>' % (r["id"], r["id"]))
                elif late == "accepted":
                    extra += " — מאחר (אושר)"
            cancel = ""
            if show_saver_actions and r["status"] == "frozen":
                cancel = (' <form method="post" '
                          'action="/cancel-request-by-saver" '
                          'style="display:inline"><input type="hidden" '
                          'name="request_id" value="%s">'
                          '<button type="submit" class="secondary">בטל '
                          'בקשה זו</button></form>' % r["id"])
            lines.append("<li>%s%s — %s%s%s%s</li>"
                         % (esc(r["requester_name"]),
                            (' <span class="verified" '
                             'title="מאומת על ידי דניאל">✓</span>'
                             if _user_field(r, "requester_verified", 0)
                             else ""),
                            esc(status), extra,
                            cancel, late_buttons))
        lines.append("</ul>")
        return "".join(lines)


    def _offer_card(self, conn, o, user):
        session = get_session(conn, o["session_id"]) if o["session_id"] else None
        # Round 19: the card leads with what a requester decides on.
        # Course first and biggest, with its colour dot and the card
        # border in the course colour; then time and place; then
        # seats left, big and bold. The saver line and the note stay,
        # smaller. The single action is one full-width button.
        where = _session_label(session) if session else ""
        left = seats_available(conn, o)
        saver_rel = reliability_label(conn, o["saver_id"], "saver")
        # Round 20, item 15: the saver's name with the VERIFIED badge
        # when the admin approved them — so a requester can tell at
        # a glance that the saver is a real person Daniel knows.
        saver_text = "שומר: %s" % esc(o["saver_name"])
        if _user_field(o, "saver_verified", 0):
            saver_text += (' <span class="verified" '
                           'title="מאומת על ידי דניאל">✓</span>')
        if saver_rel:
            saver_text += " (%s)" % esc(saver_rel)
        wait_n = waiting_count_for_offer(conn, o["id"])
        wait_text = ""
        if wait_n:
            wait_text = " · %s" % esc(waiting_label(wait_n))
        card = ['<div class="card offer" style="border-right-color:%s">'
                % course_color(o["course_id"]),
                '<div class="offer-title">%s %s</div>'
                % (course_dot_html(o["course_id"]),
                   esc(o["course_name"]))]
        if where:
            card.append('<div class="offer-time">%s</div>' % esc(where))
        card.append('<div class="offer-seats">נשארו %d מתוך %d מקומות%s</div>'
                    % (left, o["seats_total"], wait_text))
        card.append('<div class="offer-saver">%s</div>' % saver_text)
        # "Already in class": the saver tapped it, so everyone sees
        # the saver is physically there — not a promise, a fact.
        arrived = self._arrived_time(o)
        if arrived:
            card.append("<p>השומר כבר בכיתה (%s)</p>" % arrived)
        countdown = _countdown_text(o, session)
        if countdown:
            card.append("<p>%s</p>" % esc(countdown))
        if o["note"]:
            card.append('<p class="muted">הערה: %s</p>' % esc(o["note"]))
        if user and left > 0 and user["id"] != o["saver_id"]:
            qty = "".join('<option>%d</option>' % i
                          for i in range(1, min(MAX_REQUEST_SEATS, left) + 1))
            card.append('<form method="post" action="/request-seat">'
                        '<input type="hidden" name="offer_id" value="%s">'
                        '<label>כמה מקומות? <select name="seats">%s'
                        '</select></label> '
                        '<button type="submit" class="big">בקש מקום — מטבע אחד למקום'
                        '</button></form>' % (o["id"], qty))
        elif user and user["id"] == o["saver_id"]:
            card.append('<form method="post" action="/cancel-offer">'
                        '<input type="hidden" name="offer_id" value="%s">'
                        '<button type="submit" class="secondary">בטל את ההצעה'
                        '</button></form>' % o["id"])
            if o["status"] == "open":
                card.append('<form method="post" action="/mark-arrived">'
                            '<input type="hidden" name="offer_id" value="%s">'
                            '<button type="submit" class="secondary">אני כבר '
                            'בכיתה</button></form>' % o["id"])
        elif user and left == 0 and o["status"] == "open":
            # Full offer: the waiting list, not the request form.
            if is_on_waiting_list(conn, user["id"], o["id"]):
                pos = waiting_position_for_user(conn, o["id"], user["id"])
                card.append('<p>%s ברשימת ההמתנה להצעה הזו — נודיע לך '
                            'כשהתפנה מקום.</p>'
                            % esc(waiting_position_label(pos))
                            + '<form method="post" action="/leave-waiting-list">'
                            '<input type="hidden" name="offer_id" value="%s">'
                            '<button type="submit" class="secondary">צא '
                            'מרשימת ההמתנה</button></form>' % o["id"])
            elif has_frozen_request(conn, user["id"], o["id"]):
                pass  # Already holds a seat here — nothing to queue for.
            elif int(user["id"]) in followers_for_offer(conn, o):
                card.append('<p>ההצעה מלאה.</p>'
                            '<form method="post" action="/join-waiting-list">'
                            '<input type="hidden" name="offer_id" value="%s">'
                            '<button type="submit">הצטרף לרשימת המתנה'
                            '</button></form>' % o["id"])
            else:
                card.append('<p class="muted">ההצעה מלאה — עקוב אחרי '
                            'הקורס כדי להצטרף לרשימת ההמתנה.</p>')
        if user and user["id"] == o["saver_id"]:
            card.append(self._offer_requesters_html(conn, o))
        card.append("</div>")
        return "".join(card)

    def _register(self, conn, user, message, error):
        if user is not None:
            return page_html("הרשמה",
                             "<p>אתה כבר מחובר. "
                             "<a href=\"/\">חזרה לבית</a>.</p>",
                             user, message, error)
        courses = conn.execute("SELECT * FROM courses ORDER BY name").fetchall()
        boxes = []
        for c in courses:
            boxes.append('<label><input type="checkbox" name="course_id" '
                         'value="%s"> %s (להתראות על כל הצעה)'
                         '</label><br>' % (c["id"], esc(c["name"])))
        # Specific sessions, grouped by course — the same choice as
        # on /follows, but right here at registration, so a new user
        # does not have to find the follow page afterwards. Round 14:
        # a general follow wins over specific ones for the same
        # course — specific sessions of a generally-followed course
        # are skipped at registration, because the general follow
        # already covers them.
        session_boxes = []
        for c in courses:
            sessions = conn.execute(
                "SELECT * FROM course_sessions WHERE course_id=?"
                " ORDER BY weekday, start_time",
                (c["id"],)).fetchall()
            if not sessions:
                continue
            session_boxes.append("<h3>%s</h3>" % esc(c["name"]))
            for sess in sessions:
                session_boxes.append(
                    '<label><input type="checkbox" name="session_id" '
                    'value="%s"> %s</label><br>'
                    % (sess["id"], esc(_session_label(sess))))
        body = ('<p>הרשמה פתוחה — לא צריך הזמנה. מתחילים עם '
                '%d מטבעות.</p>'
                '<form method="post" action="/register">'
                # Round 20, items 13/15: full name + phone, both
                # mandatory. The full name is what strangers see —
                # a nickname identifies nobody. The phone is what
                # the matched pair calls — and it is private: only
                # the matched pair and Daniel ever see it. The
                # VERIFIED badge comes later, from Daniel, after he
                # checks the name and number against his contacts.
                '<label>שם מלא — שם פרטי ושם משפחה<br><input name="name" '
                'required maxlength="40" autocomplete="username" '
                'placeholder="למשל: דניאל ביק"></label><br>'
                '<label>טלפון נייד (חובה)<br><input name="phone" required '
                'maxlength="20" dir="ltr" autocomplete="tel" '
                'placeholder="050-1234567"></label><br>'
                '<p class="muted">הטלפון מוצג רק למי שהצעת לו מקום או '
                'ביקשת ממנו, ולדניאל — אף פעם לא ברשימה ציבורית.</p>'
                '<label>סיסמה (לפחות %d תווים)<br>'
                '<input type="password" name="password" required minlength="%d"'
                ' autocomplete="new-password"></label><br>'
                '<label>סיסמה שוב<br><input type="password" '
                'name="password2" required autocomplete="new-password">'
                '</label><br><h2>אילו קורסים לעקוב אחריהם?</h2>'
                '%s<p class="muted">מעקב כללי: התראה על כל הצעה בקורס.'
                '</p>'
                '<h2>או שיעורים מסוימים?</h2>'
                '%s<p class="muted">מעקב ספציפי: התראה רק על השיעורים '
                'שסימנת — למשל רק ימי רביעי 14:00, השעה שבה תמיד '
                'מגיעים מאוחר. אם סימנת מעקב כללי לקורס, אין צורך '
                'לסמן את השיעורים שלו — הם כבר מכוסים. אפשר לשנות '
                'הכל אחר כך בדף '
                '<a href="/follows">המעקבים שלי</a>.</p>'
                '<button type="submit">הירשם</button></form>'
                '<p>כבר רשום? <a href="/login">התחבר כאן</a>.</p>'
                % (START_BALANCE, MIN_PASSWORD_LEN,
                   MIN_PASSWORD_LEN, "".join(boxes),
                   "".join(session_boxes)))
        return page_html("הרשמה", body, user, message, error)

    def _login(self, conn, user, message, error):
        if user is not None:
            return page_html("התחברות",
                             "<p>אתה כבר מחובר. "
                             "<a href=\"/\">חזרה לבית</a>.</p>",
                             user, message, error)
        body = ('<form method="post" action="/login">'
                '<label>שם<br><input name="name" required maxlength="40"'
                ' autocomplete="username"></label><br>'
                '<label>סיסמה<br><input type="password" name="password" '
                'required autocomplete="current-password"></label><br>'
                '<button type="submit">התחבר</button></form>'
                '<p>עוד לא רשום? <a href="/register">הירשם כאן</a>.</p>'
                '<p class="muted">נרשמת לפני שהיו סיסמאות, ועוד לא קבעת '
                'סיסמה? <a href="/set-password">קבע סיסמה כאן</a> — פעם '
                'אחת, ואז מתחברים איתה.</p>')
        return page_html("התחברות", body, user, message, error)

    def _set_password_page(self, conn, user, name, message, error):
        if user is not None:
            return page_html("קביעת סיסמה",
                             "<p>אתה כבר מחובר. "
                             "<a href=\"/\">חזרה לבית</a>.</p>",
                             user, message, error)
        body = ('<p>זה לחשבון שעוד אין לו סיסמה — משתמשים שנרשמו לפני '
                'שהוספנו סיסמאות. קובעים סיסמה פעם אחת, ואחר כך מתחברים '
                'איתה. אם כבר יש סיסמה לחשבון, הדף הזה לא יחליף אותה.</p>'
                '<form method="post" action="/set-password">'
                '<label>שם<br><input name="name" required maxlength="40" '
                'value="%s" autocomplete="username"></label><br>'
                '<label>סיסמה חדשה (לפחות %d תווים)<br>'
                '<input type="password" name="password" required '
                'minlength="%d" autocomplete="new-password"></label><br>'
                '<label>סיסמה שוב<br><input type="password" '
                'name="password2" required autocomplete="new-password">'
                '</label><br><button type="submit">קבע סיסמה</button></form>'
                % (esc(name), MIN_PASSWORD_LEN, MIN_PASSWORD_LEN))
        return page_html("קביעת סיסמה", body, user, message, error)

    def _courses(self, conn, user, message, error):
        body = ["<p>הקורסים והשיעורים — יום, שעה, מרצה, בניין וחדר. "
                "עוקבים אחרי הקורסים שרשומים אליהם בדף "
                "\"המעקבים שלי\".</p>"]
        for c in conn.execute("SELECT * FROM courses ORDER BY name"):
            body.append("<h2>%s %s</h2><ul>"
                        % (course_dot_html(c["id"]), esc(c["name"])))
            for s in conn.execute(
                    "SELECT * FROM course_sessions WHERE course_id=?",
                    (c["id"],)):
                body.append("<li>%s — %s</li>"
                            % (esc(_session_label(s)),
                               esc(followers_label(
                                   followers_count_for_session(conn, dict(s))))))
            body.append("</ul>")
        if is_admin(user):
            body.append('<h2>הוספת קורס</h2><form method="post" '
                        'action="/add-course"><label>שם<br>'
                        '<input name="name" required></label><br>'
                        '<button type="submit">הוסף קורס</button></form>')
            body.append('<h2>הוספת שיעור</h2><form method="post" '
                        'action="/add-session"><label>קורס <select name="course_id">')
            for c in conn.execute("SELECT * FROM courses ORDER BY name"):
                body.append('<option value="%s">%s</option>' % (c["id"], esc(c["name"])))
            body.append('</select></label><br><label>יום <select name="weekday">')
            for wd in DISPLAY_WEEKDAYS:
                body.append('<option value="%s">%s</option>'
                            % (wd, WEEKDAY_NAMES[wd]))
            body.append('</select></label><br><label>התחלה <input name="start_time"'
                        ' placeholder="14:00" required></label> '
                        '<label>סיום <input name="end_time" placeholder="16:00"'
                        ' required></label><br><label>בניין <input name="building"'
                        '></label> <label>חדר <input name="room"></label><br>'
                        '<label>מרצה <input name="teacher" '
                        'placeholder="ד״ר בן דניאל"></label><br>'
                        '<button type="submit">הוסף שיעור</button></form>')
        return page_html("קורסים", "".join(body), user, message, error)

    def _offers(self, conn, user, message, error, params=None):
        # Round 14: filter by course (?course_id=N), server-side, no
        # JS. An unknown or missing course id means "all courses".
        # Round 19: a time filter too (?when=today / ?when=week /
        # all). The two filters combine: ?course_id=N&when=today
        # shows only that course's offers for today.
        params = params or {}
        course_id = None
        raw = params.get("course_id", [None])[0]
        try:
            course_id = int(raw) if raw else None
        except (TypeError, ValueError):
            course_id = None
        if course_id is not None and get_course(conn, course_id) is None:
            course_id = None
        # Round 19: the time filter. "today" is offers for today's
        # class; "week" is offers for classes in the next 7 days
        # (today included). Anything else, or nothing, means all.
        # Open offers only ever exist for today — an offer is born
        # in the 60 minutes before its class and dies when the class
        # starts — so today and week show the same list right now.
        # The filter is still wired correctly on the offer's class
        # date, so it stays right if advance offers are ever allowed.
        when = params.get("when", [None])[0]
        if when not in ("today", "week"):
            when = None
        today = datetime.now().date()
        conds = ["o.status='open'"]
        args = []
        if course_id is not None:
            conds.append("o.course_id=?")
            args.append(course_id)
        if when == "today":
            conds.append("o.class_date=?")
            args.append(today.strftime("%Y-%m-%d"))
        elif when == "week":
            conds.append("o.class_date BETWEEN ? AND ?")
            args.append(today.strftime("%Y-%m-%d"))
            args.append((today + timedelta(days=6)).strftime("%Y-%m-%d"))
        rows = conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name, u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE "
            + " AND ".join(conds) +
            " ORDER BY o.created_at DESC LIMIT 30", args).fetchall()
        # Open offers only, earliest class first. Closed offers do
        # not belong here — each saver has their own closed-offer
        # history on /my (see _my), and a list of everyone else's
        # closed offers is just noise.
        rows = sort_offers_by_start(conn, rows)
        body = ["<p>רק ההצעות הפתוחות כרגע.</p>"]
        # Filter links, only when more than one course has open
        # offers — with a single course there is nothing to filter.
        offered = conn.execute(
            "SELECT DISTINCT c.id, c.name FROM offers o"
            " JOIN courses c ON c.id=o.course_id"
            " WHERE o.status='open' ORDER BY c.name").fetchall()
        if len(offered) > 1:
            links = ['<a href="/offers">הכל</a>' if course_id is not None
                     else '<strong>הכל</strong>']
            for c in offered:
                label = "%s %s" % (course_dot_html(c["id"]),
                                   esc(c["name"]))
                if course_id == c["id"]:
                    links.append('<strong>%s</strong>' % label)
                else:
                    links.append('<a href="/offers?course_id=%s">%s</a>'
                                 % (c["id"], label))
            body.append("<p>סינון לפי קורס: %s</p>" % " · ".join(links))
        if course_id is not None:
            course = get_course(conn, course_id)
            body.append("<p>מסונן: <strong>%s</strong></p>"
                        % esc(course["name"]))
        # Round 19: time filter links. They keep the course filter
        # when one is active, so the two filters combine.
        def _when_link(label, value):
            qs = []
            if course_id is not None:
                qs.append("course_id=%s" % course_id)
            if value is not None:
                qs.append("when=%s" % value)
            href = "/offers" + ("?" + "&".join(qs) if qs else "")
            if when == value:
                return "<strong>%s</strong>" % label
            return '<a href="%s">%s</a>' % (href, label)
        body.append("<p>סינון לפי זמן: %s · %s · %s</p>"
                    % (_when_link("הכל", None),
                       _when_link("היום", "today"),
                       _when_link("השבוע", "week")))
        if not rows:
            body.append("<p>אין הצעות פתוחות כרגע.</p>")
        # Round 19: no summary line above each card any more — the
        # card itself shows course, time, seats and saver, so the
        # line was a duplicate of the card right under it.
        for o in rows:
            body.append(self._offer_card(conn, o, user))
        return page_html("הצעות", "".join(body), user, message, error)

    def _wanted(self, conn, user, message, error, params=None):
        """The request board: who needs a seat, for which session.
        Posts are grouped by session, with a count and the posters'
        names. A post is demand signal only — it never reserves a
        seat and never touches coins. Posting is one click; only the
        poster can take their own post down.
        Two-step post form (Daniel, 2026-11-10): first pick a course,
        then the lesson — one dropdown with every session of every
        course was too crowded to read. The course is picked with
        links (server-side, no JS, like the /offers filter);
        the session dropdown then lists only that course's sessions.
        ?course_id=N; unknown/missing means "no course picked yet"
        and shows no session dropdown, only a hint."""
        if user is None:
            return self._need_login(user, message, error)
        rows = open_seat_requests(conn)
        body = ["<p>יודעים שתגיעו מאוחר? פרסמו שאתם מחפשים מקום לשיעור, "
                "וכשמישהו יציע מקום לאותו שיעור תקבלו התראה. הפרסום לא "
                "שומר לכם מקום — הוא רק אומר לשומרים שיש ביקוש.</p>"]
        # Step 1: pick a course. Same safe parsing as the /offers
        # filter — an unknown or missing id means "no course yet".
        course_id = None
        if params:
            raw = params.get("course_id", [None])[0]
            try:
                course_id = int(raw) if raw else None
            except (TypeError, ValueError):
                course_id = None
            if course_id is not None and get_course(conn, course_id) is None:
                course_id = None
        courses = conn.execute(
            "SELECT * FROM courses ORDER BY name").fetchall()
        links = []
        for c in courses:
            label = "%s %s" % (course_dot_html(c["id"]), esc(c["name"]))
            if course_id == c["id"]:
                links.append('<strong>%s</strong>' % label)
            else:
                links.append('<a href="/wanted?course_id=%s">%s</a>'
                             % (c["id"], label))
        body.append("<p>בחר קורס: %s</p>" % " · ".join(links))
        if course_id is None:
            body.append('<p class="muted">בחר קורס למעלה כדי לראות '
                        'את השיעורים שלו.</p>')
        else:
            # Step 2: the chosen course's sessions only, Sunday
            # first, then by start time.
            sessions = conn.execute(
                "SELECT s.*, c.name AS course_name FROM course_sessions s"
                " JOIN courses c ON c.id = s.course_id"
                " WHERE s.course_id=?", (course_id,)).fetchall()
            sessions = sorted(
                sessions,
                key=lambda s: (DISPLAY_WEEKDAYS.index(s["weekday"]),
                               s["start_time"]))
            body.append('<form method="post" action="/post-seat-request">'
                        '<label>שיעור <select name="session_id">')
            for s in sessions:
                body.append('<option value="%s">%s</option>'
                            % (s["id"], esc(_session_label(s, s["course_name"]))))
            body.append('</select></label><br>'
                        '<button type="submit">פרסם שאני מחפש מקום</button>'
                        '</form>')
        # My own open posts, with a cancel button each.
        mine = [r for r in rows if int(r["user_id"]) == int(user["id"])]
        if mine:
            body.append("<h2>הפרסומים שלי</h2><ul>")
            for r in mine:
                body.append("<li>%s "
                            '<form method="post" action="/cancel-seat-request" '
                            'style="display:inline">'
                            '<input type="hidden" name="request_id" value="%s">'
                            '<button type="submit" class="secondary">בטל'
                            '</button></form></li>'
                            % (esc(_session_label(r, r["course_name"])), r["id"]))
            body.append("</ul>")
        # Everyone else's posts, grouped by session.
        body.append("<h2>מי מחפש מקום</h2>")
        if not rows:
            body.append("<p class=\"muted\">אף אחד לא מחפש מקום כרגע.</p>")
        else:
            groups = {}
            for r in rows:
                groups.setdefault(r["session_id"], []).append(r)
            body.append("<ul>")
            for session_id, group in groups.items():
                first = group[0]
                # Round 20, item 15: the VERIFIED badge next to each
                # poster's name.
                names = ", ".join(
                    esc(r["user_name"]) + (
                        ' <span class="verified" '
                        'title="מאומת על ידי דניאל">✓</span>'
                        if _user_field(r, "user_verified", 0) else "")
                    for r in group)
                count = len(group)
                count_text = ("מחפש אחד" if count == 1
                              else "%d מחפשים" % count)
                body.append("<li><strong>%s</strong> — %s: %s</li>"
                            % (esc(_session_label(first, first["course_name"])),
                               count_text, names))
            body.append("</ul>")
        return page_html("מחפשים מקום", "".join(body), user, message, error)

    def _admin(self, conn, user, message, error):
        """The admin dashboard: everything live, in one place. Open
        offers with their seats and request counts, and frozen
        requests with how long each has been frozen. Round 20: the
        full user list (phone, coins, verified — this is where
        Daniel verifies people and deletes accounts), the open
        complaints (decided here, by Daniel, with a note), and the
        broadcast form. The one action that stays on /my is the
        dispute buttons for frozen requests (confirm / refund) —
        those are per-request, not per-user. Daniel only — do_GET
        enforces it before this method is ever called."""
        body = ["<p>כל מה שחי כרגע, במקום אחד. הדף הזה רק מציג — "
                "הכרעות מתקבלות בדף \"המקומות שלי\".</p>"]
        offers = admin_open_offers(conn)
        body.append("<h2>הצעות פתוחות (%d)</h2>" % len(offers))
        if not offers:
            body.append("<p class=\"muted\">אין הצעות פתוחות כרגע.</p>")
        else:
            body.append("<ul>")
            for o in offers:
                session = get_session(conn, o["session_id"]) \
                    if o["session_id"] else None
                where = _session_label(session, o["course_name"]) \
                    if session else o["course_name"]
                req_count = conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE offer_id=?"
                    " AND status='frozen'", (o["id"],)).fetchone()[0]
                # Round 20, item 15: the VERIFIED badge next to the
                # saver's name.
                sav_badge = (' <span class="verified" '
                             'title="מאומת על ידי דניאל">✓</span>'
                             if _user_field(o, "saver_verified", 0) else "")
                body.append("<li><strong>%s</strong> — %s — שומר %s%s — "
                            "מקומות %d מתוך %d — %d בקשות מוקפאות</li>"
                            % (esc(o["course_name"]), esc(where),
                               esc(o["saver_name"]), sav_badge,
                               seats_available(conn, o), o["seats_total"],
                               req_count))
            body.append("</ul>")
        frozen = admin_frozen_requests(conn)
        body.append("<h2>בקשות מוקפאות (%d)</h2>" % len(frozen))
        if not frozen:
            body.append("<p class=\"muted\">אין בקשות מוקפאות כרגע.</p>")
        else:
            body.append("<ul>")
            for r in frozen:
                session = get_session(conn, r["session_id"]) \
                    if r["session_id"] else None
                where = _session_label(session, r["course_name"]) \
                    if session else r["course_name"]
                # Round 20, item 15: the VERIFIED badge next to both
                # names.
                req_badge = (' <span class="verified" '
                             'title="מאומת על ידי דניאל">✓</span>'
                             if _user_field(r, "requester_verified", 0)
                             else "")
                sav_badge = (' <span class="verified" '
                             'title="מאומת על ידי דניאל">✓</span>'
                             if _user_field(r, "saver_verified", 0) else "")
                body.append("<li><strong>%s</strong> — %s — מבקש %s%s — "
                            "שומר %s%s — %d מקומות מוקפאים — %s</li>"
                            % (esc(r["course_name"]), esc(where),
                               esc(r["requester_name"]), req_badge,
                               esc(r["saver_name"]), sav_badge,
                               _request_seats(r),
                               esc(_frozen_duration_text(r["created_at"]))))
            body.append("</ul>")
            body.append("<p><a href=\"/my\">להכרעות, בדף \"המקומות שלי\""
                        "</a></p>")
        # Users and password reset: a forgotten password is a
        # help-desk fix, not a code problem. Resetting clears the
        # password AND every session of that user, so they set a new
        # password on next login and old logins stop working. No
        # ADMIN user has a reset button next to their name — an
        # admin's password is never reset from here (see
        # admin_reset_password); the button is hidden for every
        # is_admin user, not just the logged-in one, so one admin
        # cannot reset another admin either. Round 20, item 9: the
        # list is now a table — name (+ VERIFIED badge), phone,
        # coins, password state — because this is where Daniel
        # manages the pilot: he checks each name + phone against
        # his own contacts, marks VERIFIED (item 15), and deletes
        # test accounts (item 6). The phone is visible here ONLY
        # because this page is admin-only; it never appears on a
        # public page. An admin row has no verify or delete button:
        # the admin needs no badge, and the admin account is never
        # deleted from here.
        users = conn.execute(
            "SELECT id, name, balance, phone, verified, password_hash"
            " FROM users ORDER BY name").fetchall()
        body.append("<h2>משתמשים (%d)</h2>" % len(users))
        body.append('<table class="admin-table"><tr><th>שם</th>'
                    '<th>טלפון</th><th>מטבעות</th><th>סיסמה</th>'
                    '<th>אימות</th><th>פעולות</th></tr>')
        for u in users:
            has_password = bool(_user_field(u, "password_hash"))
            phone = _user_field(u, "phone")
            phone_text = esc(_phone_display(phone)) if _valid_phone(phone) \
                else '<span class="muted">—</span>'
            name_html = esc(u["name"]) + verified_badge_html(u)
            if is_admin(u):
                verify = '<span class="muted">מנהל</span>'
                delete = ""
                reset = ""
            else:
                if _user_field(u, "verified", 0):
                    verify = ('<form method="post" action="/admin-verify" '
                              'style="display:inline"><input type="hidden" '
                              'name="user_id" value="%s">'
                              '<input type="hidden" name="verified" value="0">'
                              '<button type="submit" class="secondary">בטל '
                              'אימות</button></form>' % u["id"])
                else:
                    verify = ('<form method="post" action="/admin-verify" '
                              'style="display:inline"><input type="hidden" '
                              'name="user_id" value="%s">'
                              '<input type="hidden" name="verified" value="1">'
                              '<button type="submit">אמת ✓</button></form>'
                              % u["id"])
                delete = (' <a href="/admin-delete-account?user_id=%s">'
                          'מחק</a>' % u["id"])
                reset = (' <form method="post" '
                         'action="/admin-reset-password" '
                         'style="display:inline"><input type="hidden" '
                         'name="user_id" value="%s">'
                         '<button type="submit" class="secondary">אפס '
                         'סיסמה</button></form>' % u["id"])
            body.append("<tr><td>%s</td><td dir=\"ltr\">%s</td><td>%s</td>"
                        "<td>%s</td><td>%s</td><td>%s%s</td></tr>"
                        % (name_html, phone_text, u["balance"],
                           "יש סיסמה" if has_password else "אין סיסמה",
                           verify, reset, delete))
        body.append("</table><p class=\"muted\">איפוס מוחק את הסיסמה "
                    "ומנתק את המשתמש בכל המכשירים; בכניסה הבאה הוא "
                    "קובע סיסמה חדשה. ״אמת ✓״ מסמן שהאדם אמיתי — "
                    "אחרי שבדקת את השם והטלפון מול אנשי הקשר שלך. "
                    "״מחק״ פותח דף אישור נפרד — שום דבר לא נמחק "
                    "בלחיצה אחת.</p>")
        # Round 20, item 14: open complaints. A requester said a seat
        # was not actually saved. Daniel decides each one here: he
        # upholds it (the saver's reliability drops — see
        # decide_complaint) or dismisses it (nothing changes). A
        # false complaint cannot hurt a saver without Daniel seeing
        # it first. Decided complaints are listed below the open
        # ones, with the decision and the note, so the reasoning is
        # visible later.
        complaints = conn.execute(
            "SELECT c.*, u1.name AS requester_name,"
            " u2.name AS saver_name, co.name AS course_name"
            " FROM complaints c"
            " JOIN users u1 ON u1.id = c.requester_id"
            " JOIN users u2 ON u2.id = c.saver_id"
            " LEFT JOIN offers o ON o.id = c.offer_id"
            " LEFT JOIN courses co ON co.id = o.course_id"
            " ORDER BY c.created_at DESC, c.id DESC").fetchall()
        open_complaints = [c for c in complaints if c["status"] == "open"]
        body.append("<h2>תלונות פתוחות (%d)</h2>" % len(open_complaints))
        if not open_complaints:
            body.append("<p class=\"muted\">אין תלונות פתוחות.</p>")
        else:
            for c in open_complaints:
                body.append("<p><strong>%s</strong> התלונן על "
                            "<strong>%s</strong> (%s): %s</p>"
                            '<form method="post" action="/decide-complaint">'
                            '<input type="hidden" name="complaint_id" '
                            'value="%s">'
                            '<label>הערה (לא חובה)<br><input '
                            'name="admin_note" maxlength="200"></label> '
                            '<button type="submit" name="decision" '
                            'value="upheld">קבל תלונה</button> '
                            '<button type="submit" name="decision" '
                            'value="dismissed" class="secondary">דחה '
                            'תלונה</button></form>'
                            % (esc(c["requester_name"]),
                               esc(c["saver_name"]),
                               esc(c["course_name"]), esc(c["reason"]),
                               c["id"]))
                body.append('<p class="muted">״קבל תלונה״ — האמינות של '
                            'השומר יורדת; אם המטבע עוד קפוא, הוא חוזר '
                            'למבקש. ״דחה תלונה״ — שום דבר לא משתנה.</p>')
        decided = [c for c in complaints if c["status"] != "open"]
        if decided:
            body.append("<details><summary>תלונות שהוכרעו (%d)</summary>"
                        % len(decided))
            for c in decided:
                note = (" — הערה: %s" % esc(c["admin_note"])
                        if c["admin_note"] else "")
                body.append("<p>%s התלונן על %s (%s): %s — %s%s</p>"
                            % (esc(c["requester_name"]),
                               esc(c["saver_name"]),
                               esc(c["course_name"]), esc(c["reason"]),
                               "התקבלה" if c["status"] == "upheld"
                               else "נדחתה", note))
            body.append("</details>")
        # Round 20, item 12: broadcast — one push to ALL users.
        # At most one per hour (see send_broadcast), so it cannot
        # be spammed. Use it for announcements — "the pilot starts
        # tomorrow", "the site moved". Quiet hours and vacation
        # still apply to each recipient.
        body.append('<h2>הודעה לכולם</h2>'
                    '<form method="post" action="/admin-broadcast">'
                    '<label>כותרת<br><input name="title" maxlength="80" '
                    'required></label><br>'
                    '<label>הודעה<br><input name="body" maxlength="200" '
                    'required></label><br>'
                    '<button type="submit">שלח לכולם</button></form>'
                    '<p class="muted">הודעה אחת בשעה לכל היותר. מי '
                    'שכבוי לו (שעות שקטות / חופשה) לא מקבל.</p>')
        # TEMPORARY — test aid, remove after pilot. The recent push
        # log, so a missed notification can be diagnosed without
        # sqlite commands in CMD. Read-only: this page never changes
        # anything. Details are rendered through _safe_detail, so a
        # subscription endpoint can never leak into the page.
        body.append("<h2>יומן התראות אחרונות</h2>")
        log_rows = recent_notification_log(conn)
        if not log_rows:
            body.append("<p class=\"muted\">אין ניסיונות דחיפה עדיין.</p>")
        else:
            body.append("<ul>")
            for r in log_rows:
                offer = ("בדיקה" if int(r["offer_id"]) == 0
                         else "הצעה %s" % r["offer_id"])
                when = str(r["created_at"]).replace("T", " ")
                body.append("<li>%s — %s — %s — %s — %s</li>"
                            % (esc(when), esc(r["user_name"]),
                               esc(_log_status_label(r["status"])),
                               esc(offer),
                               esc(_safe_detail(r["detail"]))))
            body.append("</ul>")
        return page_html("דף בקרה", "".join(body), user, message, error)

    def _offer_form(self, conn, user, message, error, prefill_session_id=None):
        if user is None:
            return self._need_login(user, message, error)
        now = datetime.now()
        today = now.weekday()
        sessions = conn.execute(
            "SELECT s.*, c.name AS course_name FROM course_sessions s"
            " JOIN courses c ON c.id=s.course_id"
            " WHERE s.weekday=? ORDER BY s.start_time", (today,)).fetchall()
        in_window = offerable_sessions(conn, now)
        # The window list below is informative: every session today
        # with its window state. The form itself only ever offers
        # sessions whose window is open right now — a far-away session
        # is not listed at all, so it cannot be offered by mistake.
        window_list = []
        for s in sessions:
            status, opens = session_window_status(dict(s), now)
            if status == "open":
                state = "חלון ההצעה פתוח עכשיו"
            elif status == "not_yet":
                state = "חלון ההצעה נפתח ב־%s" % opens
            elif status == "started":
                state = "השיעור התחיל"
            else:
                state = "השיעור הסתיים"
            window_list.append("<li>%s — %s — %s</li>"
                               % (esc(_session_label(s, s["course_name"])), state,
                                  esc(followers_label(
                                      followers_count_for_session(conn, dict(s))))))
        if in_window:
            opts = []
            for s in in_window:
                # Demand at the decision moment: followers say who
                # might come, open board posts say who is already
                # waiting for this exact session. Shown only when
                # someone is actually looking (count > 0).
                label = "%s — %s" % (
                    _session_label(s, s["course_name"]),
                    followers_label(
                        followers_count_for_session(conn, dict(s))))
                demand = board_demand_label(
                    open_board_count_for_session(conn, s["id"]))
                if demand:
                    label += " — %s" % demand
                # Round 20, item 3: the session the /today button
                # pointed at is pre-selected. Only when it is
                # actually in-window — a stale or out-of-window id
                # is ignored, and the first option stays selected.
                selected = (" selected" if prefill_session_id is not None
                            and int(s["id"]) == int(prefill_session_id)
                            else "")
                opts.append('<option value="%s"%s>%s</option>'
                            % (s["id"], selected, esc(label)))
            # Round 20, item 3: when a session is pre-selected and
            # the user has a saved template for it, the note and
            # seats are prefilled too — the one-tap offer is then
            # really one tap (the submit button), not a form to fill.
            prefill_note = ""
            prefill_seats = 1
            if prefill_session_id is not None:
                tmpl = conn.execute(
                    "SELECT * FROM offer_templates WHERE user_id=?"
                    " AND session_id=?",
                    (user["id"], prefill_session_id)).fetchone()
                if tmpl is not None:
                    prefill_note = tmpl["note"] or ""
                    prefill_seats = tmpl["seats"]
            seats = "".join(
                '<option%s>%d</option>' % (
                    " selected" if i == prefill_seats else "", i)
                for i in range(1, MAX_SEATS_PER_OFFER + 1))
            # One-tap offers from saved templates: only templates
            # whose session is in-window right now — a template for
            # a far-away session cannot be offered by mistake, same
            # rule as the form itself.
            template_html = []
            in_window_ids = {int(s["id"]) for s in in_window}
            for t in user_templates(conn, user["id"]):
                if int(t["session_id"]) not in in_window_ids:
                    continue
                template_html.append(
                    '<form method="post" action="/create-offer">'
                    '<input type="hidden" name="session_id" value="%s">'
                    '<input type="hidden" name="note" value="%s">'
                    '<input type="hidden" name="seats" value="%s">'
                    '<button type="submit" class="secondary">הצע לפי '
                    'התבנית: %s — %d מקומות</button></form>'
                    % (t["session_id"], esc(t["note"]),
                       t["seats"],
                       esc(_session_label(t, t["course_name"])),
                       t["seats"]))
            template_block = ""
            if template_html:
                template_block = ('<h2>הצע לפי תבנית שמורה</h2><p>תבנית היא '
                                  'הערה ומספר מקומות ששמרת לשיעור '
                                  'קבוע — לחיצה אחת מציעה לפיה.</p>'
                                  + "".join(template_html))
            form = (template_block +
                    '<form method="post" action="/create-offer">'
                    '<label>שיעור שמתחיל בשעה הקרובה<br><select '
                    'name="session_id">%s</select></label><br>'
                    '<label>איפה בדיוק? (טקסט חופשי)<br><input name="note" '
                    'placeholder="שורה 2, ליד שקע" maxlength="120" '
                    'value="%s"></label><br>'
                    '<label>מקומות <select name="seats">%s</select></label><br>'
                    '<label><input type="checkbox" name="save_template" '
                    'value="1"> שמור כתבנית לשיעור הזה לפעם הבאה'
                    '</label><br>'
                    '<button type="submit">הצע מקומות — לשלוח התראה לעוקבים'
                    '</button></form>'
                    % ("".join(opts), esc(prefill_note), seats))
        else:
            # No class starting within the hour: say so plainly, and
            # say when the next one starts, instead of an empty form.
            nxt = None
            for s in sessions:
                start_min = _to_minutes(s["start_time"])
                if start_min is None:
                    continue
                start = datetime(now.year, now.month, now.day,
                                 start_min // 60, start_min % 60)
                if now < start:
                    nxt = s
                    break
            if nxt is not None:
                _, opens = session_window_status(dict(nxt), now)
                form = ("<p>אין שיעור שמתחיל בשעה הקרובה — אי אפשר "
                        "להציע מקום עכשיו.</p><p>השיעור הבא: %s — חלון "
                        "ההצעה נפתח ב־%s.</p>"
                        % (esc(_session_label(nxt, nxt["course_name"])), opens))
            else:
                form = ("<p>אין שיעור שמתחיל בשעה הקרובה — אי אפשר "
                        "להציע מקום עכשיו.</p><p>אין עוד שיעורים היום.</p>")
        body = ('<p>ההצעות נפתחות רק ב־60 הדקות לפני השיעור, ורק לשיעור '
                'של היום. אפשר להציע עד %d מקומות. מוצגים כאן רק שיעורים '
                'שמתחילים בשעה הקרובה — שיעור רחוק יותר לא מופיע בכלל, '
                'כי הצעה לשיעור כזה היא הבטחה ריקה.</p>'
                '%s'
                '<h2>חלונות ההצעה היום</h2><ul class="today-list">%s</ul>'
                % (MAX_SEATS_PER_OFFER, form, "".join(window_list)))
        return page_html("הצע מקום", body, user, message, error)

    def _edit_offer_form(self, conn, user, offer_id, message, error):
        """Edit an open offer: note and seat count, prefilled
        with the current values. Only the saver, only while the offer
        is open — anyone else sees an error, not the form. The seat
        count can only come down to the number of seats already
        frozen; the form says so, and edit_offer enforces it again
        on the server. Followers are not re-notified by an edit —
        except when the edit frees a seat that was not free before."""
        if user is None:
            return self._need_login(user, message, error)
        offer = get_offer(conn, offer_id)
        if offer is None:
            return page_html("ערוך הצעה",
                             "<p class=\"err\" role=\"alert\">ההצעה לא "
                             "נמצאה.</p>", user, message, error)
        if int(offer["saver_id"]) != int(user["id"]):
            return page_html("ערוך הצעה",
                             "<p class=\"err\" role=\"alert\">רק השומר יכול "
                             "לערוך את ההצעה הזו.</p>", user, message, error)
        if offer["status"] != "open":
            return page_html("ערוך הצעה",
                             "<p class=\"err\" role=\"alert\">ההצעה הזו כבר "
                             "לא פתוחה.</p>", user, message, error)
        course = get_course(conn, offer["course_id"])
        session = get_session(conn, offer["session_id"]) \
            if offer["session_id"] else None
        where = _session_label(session, course["name"]) if session else \
            course["name"]
        occupied = offer["seats_total"] - seats_available(conn, offer)
        seat_opts = "".join(
            '<option%s>%d</option>'
            % (" selected" if i == offer["seats_total"] else "", i)
            for i in range(1, MAX_SEATS_PER_OFFER + 1))
        body = ('<p><strong>%s</strong> — %s</p>'
                '<form method="post" action="/edit-offer">'
                '<input type="hidden" name="offer_id" value="%s">'
                '<label>איפה בדיוק? (טקסט חופשי)<br><input name="note" '
                'value="%s" maxlength="120"></label><br>'
                '<label>מקומות <select name="seats">%s</select></label><br>'
                '<button type="submit">שמור שינויים</button></form>'
                '<p class="muted">אפשר להוריד את מספר המקומות רק עד '
                '%d מקומות שכבר תפוסים. עריכה לא שולחת התראה חדשה '
                'לעוקבים — זו אותה הצעה.</p>'
                '<p><a href="/my">חזרה למקומות שלי</a></p>'
                % (esc(where), esc(offer["class_date"]), offer["id"],
                   esc(offer["note"] or ""),
                   seat_opts, occupied))
        return page_html("ערוך הצעה", body, user, message, error)

    def _follows(self, conn, user, message, error, params=None):
        if user is None:
            return self._need_login(user, message, error)
        params = params or {}
        rows = conn.execute(
            "SELECT f.*, c.name AS course_name FROM follows f"
            " JOIN courses c ON c.id=f.course_id WHERE f.user_id=?"
            " ORDER BY c.name", (user["id"],)).fetchall()
        body = []
        if params.get("registered", [None])[0]:
            # Fresh registration lands here: pick the lessons to
            # follow first, then turn notifications on. The link
            # is styled as a button (see .btn in the CSS).
            body.append('<p class="ok" role="status">נרשמת! בחר את '
                        'השיעורים לעקוב אחריהם — ואחר כך הפעל התראות, '
                        'אחרת לא תדע כשמציעים מקום.</p>'
                        '<p><a class="btn" href="/push">הפעל התראות</a></p>')
        body.append("<h2>המעקבים שלך</h2>")
        if rows:
            body.append("<ul>")
            for f in rows:
                if f["weekday"] is None:
                    desc = "כל הצעה בקורס"
                else:
                    sess = session_for_follow(conn, f)
                    if sess is not None:
                        desc = "רק %s" % _session_label(sess)
                    else:
                        desc = "רק %s %s" % (WEEKDAY_NAMES[f["weekday"]],
                                              f["start_time"] or "")
                body.append("<li>%s %s — %s "
                            '<form method="post" action="/remove-follow" '
                            'style="display:inline">'
                            '<input type="hidden" name="follow_id" value="%s">'
                            '<button type="submit" class="secondary">הסר'
                            '</button></form></li>'
                            % (course_dot_html(f["course_id"]),
                               esc(f["course_name"]), esc(desc), f["id"]))
            body.append("</ul>")
        else:
            body.append("<p class=\"muted\">עוד לא עוקבים אחרי אף קורס.</p>")
        # One-click general follow: any offer for the course. A
        # general follow covers every session, so specific follows
        # for the same course become redundant (they stay — only
        # new ones are skipped — and matching counts one user once
        # anyway).
        body.append('<h2>מעקב כללי</h2><p>התראה על כל הצעה בקורס.</p>'
                    '<form method="post" action="/add-follow">'
                    '<label>קורס <select name="course_id">')
        for c in conn.execute("SELECT * FROM courses ORDER BY name"):
            body.append('<option value="%s">%s</option>' % (c["id"], esc(c["name"])))
        body.append('</select></label><br>'
                    '<button type="submit">עקוב אחרי כל ההצעות בקורס'
                    '</button></form>'
                    '<p class="muted">מעקב כללי מכסה את כל השיעורים של '
                    'הקורס — מעקבים ספציפיים לאותו קורס מיותרים אחריו.</p>')
        # Specific follow, two steps: first pick a course, then only
        # that course's real sessions are offered — an impossible
        # combination cannot be picked at all.
        body.append('<h2 id="follow">מעקב ספציפי</h2><p>התראה רק על '
                    'השיעורים שבוחרים. בוחרים קודם קורס, ואז מסמנים '
                    'את כל השיעורים שרוצים — עם המרצה, היום, השעה '
                    'והמקום.</p>'
                    '<form method="get" action="/follows#follow">'
                    '<label>קורס <select name="course_id">')
        for c in conn.execute("SELECT * FROM courses ORDER BY name"):
            body.append('<option value="%s">%s</option>' % (c["id"], esc(c["name"])))
        body.append('</select></label> '
                    '<button type="submit">בחר קורס</button></form>')
        chosen = params.get("course_id", [None])[0]
        if chosen is not None:
            course = None
            try:
                course = get_course(conn, int(chosen))
            except (TypeError, ValueError):
                course = None
            if course is None:
                body.append('<p class="err" role="alert">קורס לא ידוע.</p>')
            else:
                sessions = conn.execute(
                    "SELECT * FROM course_sessions WHERE course_id=?",
                    (course["id"],)).fetchall()
                sessions = sorted(
                    sessions,
                    key=lambda s: (DISPLAY_WEEKDAYS.index(s["weekday"]),
                                   s["start_time"]))
                if sessions:
                    # Which sessions are already followed, so they
                    # show pre-checked: a specific follow is one row
                    # per session, matched by exact session id
                    # (round 14). An old follow without a session
                    # id — an ambiguous hour that still matches
                    # both sessions — pre-checks every session at
                    # its day + start time.
                    followed_ids = {
                        f["session_id"]
                        for f in rows
                        if f["course_id"] == course["id"]
                        and f["session_id"] is not None}
                    followed_times = {
                        (f["weekday"], f["start_time"])
                        for f in rows
                        if f["course_id"] == course["id"]
                        and f["weekday"] is not None
                        and f["session_id"] is None}
                    body.append('<form method="post" action="/add-follow">'
                                '<input type="hidden" name="course_id" '
                                'value="%s">'
                                '<input type="hidden" name="specific" '
                                'value="1">'
                                '<h3>השיעורים של %s — סמן את כולם</h3>'
                                % (course["id"], esc(course["name"])))
                    for s in sessions:
                        checked = (" checked" if s["id"] in followed_ids
                                   or (s["weekday"], s["start_time"])
                                   in followed_times else "")
                        already = (" (כבר עוקב)" if checked else "")
                        body.append('<label><input type="checkbox" '
                                    'name="session_id" value="%s"%s> '
                                    '%s%s</label><br>'
                                    % (s["id"], checked,
                                       esc(_session_label(s)), already))
                    body.append('<button type="submit">עקוב אחרי השיעורים '
                                'האלה</button></form>')
                else:
                    body.append('<p class="muted">לקורס הזה אין עדיין '
                                'שיעורים.</p>')
        body.append('<p class="muted">דוגמה: עוקבים אחרי חדו״א 2 באופן '
                    'כללי, אבל אחרי תכנון אלגוריתמים רק בשיעורים מסוימים — '
                    'השעות שבהן תמיד מגיעים מאוחר בגלל מעבר צפוף.</p>')
        return page_html("המעקבים שלי", "".join(body), user, message, error)

    def _saved_seat_card_html(self, conn, r):
        """Round 20, item 18: the "your saved seat" card — after a
        request is accepted (frozen), the requester sees ONE card
        with everything needed to find the saver: the saver's full
        name (+ VERIFIED badge), the saver's phone as a call link
        and a WhatsApp link, and where to meet (the offer's note).
        Only this matched pair sees the phone — the card is rendered
        on /my, which only the requester themself can open, and the
        phone never appears on a public offer card. When the saver
        has no phone yet (registered before phones), the card says
        so and shows the name only."""
        saver_name = esc(r["saver_name"])
        badge = (' <span class="verified" title="מאומת על ידי דניאל">✓</span>'
                 if _user_field(r, "saver_verified", 0) else "")
        phone = _user_field(r, "saver_phone")
        if _valid_phone(phone):
            phone_html = ('<br>טלפון: <a href="tel:%s">%s</a> · '
                          '<a href="%s">וואטסאפ</a>'
                          % (esc(_valid_phone(phone)),
                             esc(_phone_display(phone)),
                             esc(_wa_link(phone))))
        else:
            phone_html = ('<br><span class="muted">לשומר אין עדיין מספר '
                          'טלפון — תגיעו למקום ותשאלו.</span>')
        meet = ""
        if r["note"]:
            meet = '<br>איפה נפגשים: %s' % esc(r["note"])
        return ('<div class="card saved-seat-card"><strong>המקום שלך '
                'נשמר ✓</strong><br>שומר: %s%s%s%s</div>'
                % (saver_name, badge, phone_html, meet))

    def _complaint_form_html(self, conn, r):
        """Round 20, item 14: the complaint form on /my, on a
        request. Shown while the coin is frozen, or after the
        request resolved (confirmed / not saved / no show / late
        declined) — but not on a cancelled request (the coin is
        already back) and not when a complaint was already filed.
        One complaint per request, enforced by the table, so the
        same seat cannot be complained about twice."""
        if r["status"] not in COMPLAINTABLE_REQUEST_STATUSES:
            return ""
        existing = conn.execute(
            "SELECT * FROM complaints WHERE request_id=?",
            (r["id"],)).fetchone()
        if existing is not None:
            status_he = {"open": "בטיפול", "upheld": "התקבלה",
                         "dismissed": "נדחתה"}.get(existing["status"],
                                                  existing["status"])
            return ('<p class="muted">התלונה שלך על הבקשה הזו %s.</p>'
                    % status_he)
        return ('<form method="post" action="/file-complaint">'
                '<input type="hidden" name="request_id" value="%s">'
                '<label>המקום לא נשמר? ספר בקצרה מה קרה<br>'
                '<input name="reason" maxlength="300" '
                'placeholder="למשל: הגעתי והמקום היה תפוס"></label> '
                '<button type="submit" class="secondary">שלח תלונה'
                '</button></form>' % r["id"])

    def _my(self, conn, user, message, error):
        if user is None:
            return self._need_login(user, message, error)
        body = ["<p>מאזן: <span class=\"coin\">%s מטבעות</span>.</p>"
                % user["balance"],
                floor_warning_html(user)]
        if is_on_vacation(user):
            body.append("<p><strong>מצב חופשה פעיל</strong> — לא מגיעות "
                        "לך התראות, עד שתכבה אותו.</p>")
        # Reliability, both roles — only the parts that have decided
        # requests. A user with no history sees no rating at all.
        rel_parts = []
        for role, label in (("saver", "אמינות כשומר"),
                            ("requester", "אמינות כמבקש")):
            result = reliability(conn, user["id"], role)
            if result is not None:
                rel_parts.append("%s: %d/%d" % (label, result[0], result[1]))
        if rel_parts:
            body.append("<p>%s.</p>" % " · ".join(rel_parts))
        # Personal settings: quiet hours. Quiet hours skip
        # notifications instead of delaying them.
        quiet_label = quiet_hours_label(user)
        try:
            qs = user["quiet_start"]
            qe = user["quiet_end"]
        except (KeyError, IndexError, TypeError):
            qs = qe = None
        body.append('<h2>ההעדפות שלי</h2>'
                    '<form method="post" action="/set-quiet-hours">'
                    '<label>שעות שקטות: מ־<input name="quiet_start" '
                    'type="number" min="0" max="23" value="%s" '
                    'style="width:70px"> עד <input name="quiet_end" '
                    'type="number" min="0" max="23" value="%s" '
                    'style="width:70px"></label> '
                    '<button type="submit">שמור שעות שקטות</button>'
                    '</form><p class="muted">%s</p>'
                    % ("" if qs is None else qs,
                       "" if qe is None else qe,
                       esc(quiet_label) if quiet_label else
                       "אין שעות שקטות כרגע — התראות מגיעות בכל שעה. "
                       "השאר את שתי השעות ריקות כדי לנקות. התראה שמגיעה "
                       "בשעות שקטות לא נשלחת, וגם לא נשמרת לאחר כך."))
        if is_on_vacation(user):
            until_text = vacation_until_text(user)
            try:
                until_value = user["vacation_until"] or ""
            except (KeyError, IndexError, TypeError):
                until_value = ""
            body.append('<form method="post" action="/set-vacation">'
                        '<input type="hidden" name="on" value="0">'
                        '<button type="submit">חזרתי — כבה מצב חופשה'
                        '</button></form>'
                        '<form method="post" action="/set-vacation-until">'
                        '<label>חופשה עד <input type="date" '
                        'name="vacation_until" value="%s"></label> '
                        '<button type="submit" class="secondary">שמור תאריך '
                        'סיום</button></form>'
                        '<p class="muted">מצב חופשה פעיל%s: לא מגיעות '
                        'התראות. כשהפעלת את החופשה, הבקשות המוקפאות שלך '
                        'בוטלו והמטבעות הוחזרו; הצעות פתוחות שלך נשארות. '
                        'החופשה מסתיימת אוטומטית בסוף התאריך שנבחר; השאר '
                        'את התאריך ריק אם אין תאריך סיום.</p>'
                        % (until_value,
                           " — " + esc(until_text) if until_text else ""))
        else:
            body.append('<form method="post" action="/set-vacation">'
                        '<input type="hidden" name="on" value="1">'
                        '<label>חופשה עד (לא חובה) <input type="date" '
                        'name="vacation_until"></label> '
                        '<button type="submit">אני בחופשה / במילואים'
                        '</button></form><p class="muted">נוסעים, או '
                        'במילואים? כפתור אחד משהה הכל: לא מגיעות התראות, '
                        'עד שתחזרו ותכבו — או עד סוף תאריך הסיום, אם '
                        'בחרתם אחד. כשתפעילו, הבקשות המוקפאות שלכם '
                        'יבוטלו והמטבעות יוחזרו; הצעות פתוחות לא '
                        'משתנות.</p>')
        reqs = conn.execute(
            "SELECT r.*, o.note, o.class_date, c.name AS course_name,"
            " u.name AS saver_name, u.verified AS saver_verified,"
            " u.phone AS saver_phone FROM requests r"
            " JOIN offers o ON o.id=r.offer_id"
            " JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=r.saver_id"
            " WHERE r.requester_id=? ORDER BY r.created_at DESC",
            (user["id"],)).fetchall()
        body.append("<h2>מקומות שביקשת</h2>")
        if not reqs:
            body.append("<p class=\"muted\">עוד אין.</p>")
        for r in reqs:
            status = REQUEST_STATUS_HE.get(r["status"], r["status"])
            seats_text = ""
            if _request_seats(r) > 1:
                seats_text = " — %d מקומות" % _request_seats(r)
            # Round 20, item 15: the saver's name with the VERIFIED
            # badge when the admin approved them.
            saver_badge = (' <span class="verified" '
                           'title="מאומת על ידי דניאל">✓</span>'
                           if _user_field(r, "saver_verified", 0) else "")
            body.append("<p><strong>%s</strong> %s — שומר %s%s — %s%s</p>"
                        % (esc(r["course_name"]), esc(r["class_date"]),
                           esc(r["saver_name"]), saver_badge,
                           esc(status), seats_text))
            # Round 20, item 18: the "your saved seat" card, while
            # the request is frozen (accepted, waiting for arrival).
            if r["status"] == "frozen":
                body.append(self._saved_seat_card_html(conn, r))
            # Round 20, item 14: the complaint form, on every
            # request it applies to (frozen, or resolved).
            body.append(self._complaint_form_html(conn, r))
            if r["status"] == "frozen":
                when = self._on_way_time(r)
                if when:
                    body.append("<p>בדרך מאז %s.</p>" % when)
                body.append('<form method="post" action="/on-my-way">'
                            '<input type="hidden" name="request_id" value="%s">'
                            '<button type="submit" class="secondary">אני בדרך'
                            '</button></form>'
                            '<form method="post" action="/confirm-arrival">'
                            '<input type="hidden" name="request_id" value="%s">'
                            '<button type="submit" class="big">הגעתי, המקום נשמר'
                            '</button></form>'
                            '<form method="post" action="/report-not-saved">'
                            '<input type="hidden" name="request_id" value="%s">'
                            '<button type="submit" class="secondary">המקום '
                            'לא היה שם</button></form>'
                            '<form method="post" action="/cancel-request">'
                            '<input type="hidden" name="request_id" value="%s">'
                            '<button type="submit" class="secondary">בטל בקשה'
                            '</button></form>'
                            % (r["id"], r["id"], r["id"], r["id"]))
                # Declared late: one declaration per request, and it
                # is independent of "on my way" — both can exist.
                try:
                    late = r["late_status"]
                except (KeyError, IndexError, TypeError):
                    late = None
                if late == "pending":
                    body.append("<p>הודעת שאתה מאחר — מחכה להחלטת "
                                "השומר.</p>")
                elif late == "accepted":
                    body.append("<p>השומר אישר את האיחור — יש לך עד "
                                "10 דקות אחרי תחילת השיעור להגיע.</p>")
                else:
                    body.append('<form method="post" action="/declare-late">'
                                '<input type="hidden" name="request_id" '
                                'value="%s">'
                                '<button type="submit" class="secondary">'
                                'אני מאחר</button></form>' % r["id"])
        mine = conn.execute(
            "SELECT o.*, c.name AS course_name FROM offers o"
            " JOIN courses c ON c.id=o.course_id"
            " WHERE o.saver_id=? AND o.status='open'"
            " ORDER BY o.created_at DESC LIMIT 10", (user["id"],)).fetchall()
        body.append("<h2>מקומות שהצעת</h2>")
        if not mine:
            body.append("<p class=\"muted\">עוד אין.</p>")
        for o in mine:
            countdown = _countdown_text(
                o, get_session(conn, o["session_id"])
                if o["session_id"] else None)
            countdown_text = (" — %s" % countdown) if countdown else ""
            arrived = self._arrived_time(o)
            arrived_text = (" — כבר בכיתה (%s)" % arrived) if arrived else ""
            body.append("<p><strong>%s</strong> %s — %s — מקומות %d מתוך %d%s%s</p>"
                        % (esc(o["course_name"]), esc(o["class_date"]),
                           esc(OFFER_STATUS_HE.get(o["status"], o["status"])),
                           seats_available(conn, o), o["seats_total"],
                           countdown_text, arrived_text))
            body.append(self._offer_requesters_html(
                conn, o, show_saver_actions=True))
            body.append('<form method="post" action="/mark-arrived">'
                        '<input type="hidden" name="offer_id" value="%s">'
                        '<button type="submit" class="secondary">אני כבר '
                        'בכיתה</button></form>' % o["id"])
            body.append('<p><a href="/edit-offer?offer_id=%s">ערוך '
                        'הצעה</a></p>' % o["id"])
        # Closed-offer history, per user: only MY closed offers as
        # saver, newest first — each with what happened to every
        # request on it. The general list of everyone's closed
        # offers used to live on /offers; it was noise, so it is
        # gone — this is the only closed-offer history now.
        closed = conn.execute(
            "SELECT o.*, c.name AS course_name FROM offers o"
            " JOIN courses c ON c.id=o.course_id"
            " WHERE o.saver_id=? AND o.status != 'open'"
            " ORDER BY o.created_at DESC", (user["id"],)).fetchall()
        body.append("<details><summary>היסטוריית הצעות סגורות"
                    "</summary>")
        if not closed:
            body.append("<p class=\"muted\">עוד אין הצעות סגורות.</p>")
        for o in closed:
            session = (get_session(conn, o["session_id"])
                       if o["session_id"] else None)
            where = (_session_label(session, o["course_name"])
                     if session else o["course_name"])
            line = ("<p><strong>%s</strong> %s — %s — %s"
                    % (esc(o["course_name"]), esc(o["class_date"]),
                       esc(OFFER_STATUS_HE.get(o["status"], o["status"])),
                       esc(where)))
            if o["note"]:
                line += " — הערה: %s" % esc(o["note"])
            line += "</p>"
            body.append(line)
            reqs = conn.execute(
                "SELECT r.*, u.name AS requester_name,"
                " u.verified AS requester_verified FROM requests r"
                " JOIN users u ON u.id = r.requester_id"
                " WHERE r.offer_id=? ORDER BY r.created_at",
                (o["id"],)).fetchall()
            if reqs:
                body.append("<ul>")
                for r in reqs:
                    # Round 20, item 15: the VERIFIED badge.
                    badge = (' <span class="verified" '
                             'title="מאומת על ידי דניאל">✓</span>'
                             if _user_field(r, "requester_verified", 0)
                             else "")
                    body.append("<li>%s%s — %s</li>"
                                % (esc(r["requester_name"]), badge,
                                   esc(REQUEST_OUTCOME_HE.get(
                                       r["status"], r["status"]))))
                body.append("</ul>")
            else:
                body.append("<p class=\"muted\">אף אחד לא ביקש מקום "
                            "בהצעה הזו.</p>")
            body.append('<form method="post" action="/re-offer">'
                        '<input type="hidden" name="offer_id" value="%s">'
                        '<button type="submit" class="secondary">הצע שוב'
                        '</button></form>' % o["id"])
        body.append("</details>")
        # Every real session, for the create forms below (standing
        # auto-request and new template). Sorted by course, then by
        # day of week in Israeli order, then by start time.
        all_sessions = conn.execute(
            "SELECT s.*, c.name AS course_name FROM course_sessions s"
            " JOIN courses c ON c.id = s.course_id").fetchall()
        all_sessions = sorted(
            all_sessions,
            key=lambda s: (s["course_name"],
                           DISPLAY_WEEKDAYS.index(s["weekday"]),
                           s["start_time"]))
        # Automatic weekly requests: a standing request asks for one
        # seat every week when an open offer for the session exists.
        # (Weekly automatic OFFERS were removed in round 13 — nobody
        # can promise every week to arrive on time — but a standing
        # request only takes a seat when someone actually offers one,
        # so it stays.)
        auto_reqs = conn.execute(
            "SELECT a.*, s.weekday, s.start_time, s.end_time, s.building,"
            " s.room, s.teacher, c.name AS course_name FROM auto_requests a"
            " JOIN course_sessions s ON s.id = a.session_id"
            " JOIN courses c ON c.id = s.course_id WHERE a.user_id=?"
            " ORDER BY s.weekday, s.start_time", (user["id"],)).fetchall()
        body.append("<h2>בקשות אוטומטיות כל שבוע</h2>")
        if not auto_reqs:
            body.append("<p class=\"muted\">אין בקשות אוטומטיות. בקשה "
                        "אוטומטית מבקשת מקום אחד לבד כל שבוע, כשנפתחת "
                        "הצעה לשיעור — לשיעור קבוע, בלי ללחוץ כל פעם. "
                        "כשהיא מבקשת בשבילך, מגיעה התראה, כדי שתדע ללכת "
                        "ולאשר הגעה.</p>")
        for a in auto_reqs:
            body.append("<p><strong>%s</strong>"
                        % esc(_session_label(a, a["course_name"])))
            body.append('<form method="post" action="/delete-auto-request" '
                        'style="display:inline">'
                        '<input type="hidden" name="auto_id" value="%s">'
                        '<button type="submit" class="secondary">מחק'
                        '</button></form></p>' % a["id"])
        body.append('<h2>בקשה אוטומטית חדשה</h2><form method="post" '
                    'action="/create-auto-request">'
                    '<label>שיעור <select name="session_id">')
        for s in all_sessions:
            body.append('<option value="%s">%s</option>'
                        % (s["id"], esc(_session_label(s, s["course_name"]))))
        body.append('</select></label><br>'
                    '<button type="submit">בקש אוטומטית כל שבוע'
                    '</button></form>')
        # Saved offer templates: note/seats for a recurring
        # session, offered in one tap from the offer form. A template
        # never offers by itself — it only prefills.
        templates = user_templates(conn, user["id"])
        body.append("<h2>תבניות הצעה שמורות</h2>")
        if not templates:
            body.append("<p class=\"muted\">אין תבניות שמורות. תבנית היא "
                        "הערה ומספר מקומות לשיעור קבוע — שומרים "
                        "אותה כאן או מהטופס של \"הצע מקום\", ואחר כך "
                        "מציעים לפיה בלחיצה אחת.</p>")
        for t in templates:
            body.append("<p><strong>%s</strong> — %d מקומות"
                        % (esc(_session_label(t, t["course_name"])),
                           t["seats"]))
            if t["note"]:
                body.append(" — הערה: %s" % esc(t["note"]))
            body.append('<form method="post" action="/delete-template" '
                        'style="display:inline">'
                        '<input type="hidden" name="template_id" value="%s">'
                        '<button type="submit" class="secondary">מחק'
                        '</button></form></p>' % t["id"])
        body.append('<h2>תבנית חדשה</h2><form method="post" '
                    'action="/save-template">'
                    '<label>שיעור <select name="session_id">')
        for s in all_sessions:
            body.append('<option value="%s">%s</option>'
                        % (s["id"], esc(_session_label(s, s["course_name"]))))
        body.append('</select></label><br>'
                    '<label>מקומות <select name="seats">')
        for i in range(1, MAX_SEATS_PER_OFFER + 1):
            body.append('<option>%d</option>' % i)
        body.append('</select></label><br>'
                    '<label>הערה (אופציונלי)<br><input name="note" '
                    'maxlength="120"></label><br>'
                    '<button type="submit">שמור תבנית</button></form>')
        # Admin disputes: only Daniel sees this, and only Daniel can
        # decide — the server enforces it, not just the page.
        if is_admin(user):
            stuck = admin_stuck_requests(conn)
            body.append("<h2>הכרעות מנהל — בקשות תקועות</h2>"
                        "<p>בקשות מוקפאות שהשיעור שלהן כבר התחיל, "
                        "ובקשות שהוכרעו בשבוע האחרון. אם שומר ומבקש "
                        "חולקים, אתה מכריע: אשר הגעה (המטבע עובר "
                        "לשומר) או החזר מטבע (המטבע חוזר למבקש).</p>")
            if not stuck:
                body.append("<p class=\"muted\">אין בקשות תקועות כרגע.</p>")
            for r in stuck:
                status = REQUEST_STATUS_HE.get(r["status"], r["status"])
                body.append("<p><strong>%s</strong> %s — מבקש %s — שומר "
                            "%s — %s — %d מקומות</p>"
                            % (esc(r["course_name"]), esc(r["class_date"]),
                               esc(r["requester_name"]),
                               esc(r["saver_name"]), esc(status),
                               _request_seats(r)))
                body.append('<form method="post" action="/admin-confirm">'
                            '<input type="hidden" name="request_id" value="%s">'
                            '<button type="submit">אשר הגעה</button></form>'
                            '<form method="post" action="/admin-refund">'
                            '<input type="hidden" name="request_id" value="%s">'
                            '<button type="submit" class="secondary">החזר '
                            'מטבע</button></form>'
                            % (r["id"], r["id"]))
        # Coin history: every movement, newest first. The ledger only
        # covers movements after this update — older balances have no
        # rows, and the empty state says so plainly.
        ledger = conn.execute(
            "SELECT * FROM coin_ledger WHERE user_id=?"
            " ORDER BY created_at DESC, id DESC LIMIT 30",
            (user["id"],)).fetchall()
        body.append("<h2>היסטוריית מטבעות</h2>")
        if not ledger:
            body.append("<p class=\"muted\">עוד אין תנועות.</p>")
        else:
            body.append("<ul>")
            for row in ledger:
                when = ""
                try:
                    when = datetime.fromisoformat(
                        row["created_at"]).strftime("%d/%m %H:%M")
                except (ValueError, TypeError):
                    when = row["created_at"]
                amount = row["amount"]
                if amount == 1:
                    amount_text = "+1 מטבע"
                elif amount == -1:
                    amount_text = "-1 מטבע"
                else:
                    amount_text = "%+d מטבעות" % amount
                reason = LEDGER_REASON_HE.get(row["reason"], row["reason"])
                body.append("<li>%s — %s — %s</li>"
                            % (esc(when), esc(amount_text), esc(reason)))
            body.append("</ul>")
        # Round 20, items 7/13/15: the profile — name and phone.
        # The name must stay a full name (first + last); changing
        # either the name or the phone resets the VERIFIED badge to
        # 0, because the identity the admin approved is no longer
        # the identity. An admin account has no rename form here:
        # the admin gate is by name, so renaming the admin would
        # silently drop the admin.
        body.append('<h2 id="profile">הפרופיל שלי</h2>')
        if is_admin(user):
            body.append("<p>אתה מנהל — את שם המנהל לא משנים מכאן.</p>")
        else:
            body.append('<form method="post" action="/rename">'
                        '<label>שם מלא<br><input name="name" '
                        'maxlength="40" value="%s"></label> '
                        '<button type="submit">שמור שם</button></form>'
                        '<p class="muted">שם מלא — שם פרטי ושם משפחה, '
                        'כדי שמי שלא מכיר אותך יבין במי מדובר. שינוי '
                        'השם מבטל את ה־✓ עד שדניאל יאשר מחדש.</p>'
                        % esc(user["name"]))
        phone_now = _user_field(user, "phone") or ""
        body.append('<form method="post" action="/set-phone">'
                    '<label>טלפון נייד<br><input name="phone" '
                    'maxlength="20" value="%s" dir="ltr" '
                    'placeholder="050-1234567"></label> '
                    '<button type="submit">שמור טלפון</button></form>'
                    '<p class="muted">הטלפון מוצג רק למי שהצעת לו מקום '
                    'או ביקשת ממנו, ולדניאל — אף פעם לא ברשימה '
                    'ציבורית. שינוי הטלפון מבטל את ה־✓ עד שדניאל '
                    'יאשר מחדש.</p>'
                    % esc(_phone_display(phone_now)))
        # Round 20, item 6: account deletion. A link to a separate
        # confirmation page — the link itself deletes nothing (GET
        # never deletes), and the confirmation page explains what
        # happens to the offers, requests and frozen coins before
        # the user confirms with a POST.
        if not is_admin(user):
            body.append('<div class="card danger-zone"><h2>מחיקת החשבון'
                        '</h2><p>מוחק את החשבון, ההצעות, הבקשות, '
                        'המעקבים וההתראות. מטבעות קפואים חוזרים למי '
                        'שביקש — שום מטבע לא הולך לאיבוד.</p>'
                        '<p><a href="/delete-account">מחק את החשבון שלי'
                        '</a></p></div>')
        return page_html("המקומות שלי", "".join(body), user, message, error)

    def _delete_account_page(self, conn, user, message, error):
        """Round 20, item 6: the account-deletion confirmation page
        for your own account. GET only shows this page — it deletes
        nothing (Daniel's hard rule: GET never deletes). The page
        says exactly what happens, and the confirm button POSTs to
        /delete-account. An admin account has no delete button at
        all (see delete_account)."""
        if user is None:
            return self._need_login(user, message, error)
        if is_admin(user):
            return page_html("מחיקת חשבון",
                             "<p>חשבון מנהל לא מוחקים מכאן.</p>",
                             user, message, error)
        frozen = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE requester_id=?"
            " AND status='frozen'", (user["id"],)).fetchone()[0]
        frozen_as_saver = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE saver_id=?"
            " AND status='frozen'", (user["id"],)).fetchone()[0]
        open_offers = conn.execute(
            "SELECT COUNT(*) FROM offers WHERE saver_id=?"
            " AND status='open'", (user["id"],)).fetchone()[0]
        body = ('<p>אתה עומד למחוק את החשבון <strong>%s</strong>. '
                'זה לא הפיך.</p>'
                '<p>מה יקרה:</p><ul>'
                '<li>החשבון, ההצעות, הבקשות, המעקבים וההתראות שלך '
                'יימחקו.</li>'
                '<li>מטבעות קפואים חוזרים למי שביקש — שום מטבע לא '
                'הולך לאיבוד.</li>'
                '<li>תתנתק מיד.</li></ul>'
                % esc(user["name"]))
        if frozen or frozen_as_saver or open_offers:
            body += ('<p>כרגע: %d בקשות מוקפאות שלך, %d בקשות מוקפאות '
                     'של אחרים בהצעות שלך, %d הצעות פתוחות שלך.</p>'
                     % (frozen, frozen_as_saver, open_offers))
        body += ('<form method="post" action="/delete-account">'
                 '<button type="submit">כן, מחק את החשבון שלי</button>'
                 '</form>'
                 '<p><a href="/my">ביטול — חזרה ל״המקומות שלי״</a></p>')
        return page_html("מחיקת חשבון", body, user, message, error)

    def _admin_delete_account_page(self, conn, user, target_id, message,
                                   error):
        """Round 20, item 6: the admin's confirmation page for
        deleting someone else's account (e.g. a test account). Same
        shape as the self-delete page: GET only shows it, POST
        confirms. Admin only — do_GET enforces it before this
        method is ever called; an admin account can never be
        deleted this way."""
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return page_html("מחיקת חשבון",
                             "<p>משתמש לא נמצא.</p>", user, message, error)
        target = get_user(conn, target_id)
        if target is None:
            return page_html("מחיקת חשבון",
                             "<p>משתמש לא נמצא.</p>", user, message, error)
        if is_admin(target):
            return page_html("מחיקת חשבון",
                             "<p>חשבון מנהל לא מוחקים.</p>",
                             user, message, error)
        frozen = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE requester_id=?"
            " AND status='frozen'", (target_id,)).fetchone()[0]
        frozen_as_saver = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE saver_id=?"
            " AND status='frozen'", (target_id,)).fetchone()[0]
        open_offers = conn.execute(
            "SELECT COUNT(*) FROM offers WHERE saver_id=?"
            " AND status='open'", (target_id,)).fetchone()[0]
        body = ('<p>אתה עומד למחוק את החשבון <strong>%s</strong> '
                '(מאזן: %s מטבעות). זה לא הפיך.</p>'
                '<p>מה יקרה:</p><ul>'
                '<li>החשבון, ההצעות, הבקשות, המעקבים וההתראות שלו '
                'יימחקו.</li>'
                '<li>מטבעות קפואים חוזרים למי שביקש — שום מטבע לא '
                'הולך לאיבוד.</li></ul>'
                % (esc(target["name"]), target["balance"]))
        if frozen or frozen_as_saver or open_offers:
            body += ('<p>כרגע: %d בקשות מוקפאות שלו, %d בקשות מוקפאות '
                     'של אחרים בהצעות שלו, %d הצעות פתוחות שלו.</p>'
                     % (frozen, frozen_as_saver, open_offers))
        body += ('<form method="post" action="/admin-delete-account">'
                 '<input type="hidden" name="user_id" value="%s">'
                 '<button type="submit">כן, מחק את החשבון</button>'
                 '</form>'
                 '<p><a href="/admin">ביטול — חזרה לדף הבקרה</a></p>'
                 % target_id)
        return page_html("מחיקת חשבון", body, user, message, error)

    def _push(self, conn, user, message, error):
        if user is None:
            return self._need_login(user, message, error)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (user["id"],)).fetchall()]
        subs = len(rows)
        status = ("ההתראות מופעלות למשתמש הזה (%d מנויים)."
                  % subs if subs else
                  "ההתראות כבויות — לחץ על הכפתור למטה כדי להפעיל אותן.")
        body = ('<p>%s</p>'
                '<p>כשמישהו מציע מקום בקורס שאתה עוקב אחריו, תקבל '
                'התראה אוטומטית לטלפון.</p>'
                '<button id="enable-push">הפעל התראות</button>'
                '<p id="push-status" role="status"></p>'
                # TEMPORARY — test aid, remove after pilot. The
                # test-push button posts to /test-push; the result
                # comes back as the page message (?msg= / ?error=).
                '<form method="post" action="/test-push">'
                '<button type="submit">שלח לי דחיפת בדיקה</button></form>'
                '<p class="muted">דחיפת בדיקה — רק לבדוק שההתראות '
                'מגיעות לטלפון. אפשר לשלוח אחת בדקה.</p>'
                '<p class="muted">אייפון: דחיפת Web עובדת רק אם מוסיפים '
                'את האתר למסך הבית קודם (Share → Add to Home Screen), '
                'ואז פותחים אותו ממסך הבית ולוחצים על הכפתור. אנדרואיד '
                '(כרום): אישור כאן מספיק.</p>'
                % esc(status))
        # Round 20, item 5: a dead subscription was just removed.
        # The server deleted it because the push service said it
        # will never work again — but the user only sees "off".
        # The banner says what happened and what to do: turn
        # notifications on again from the phone, one tap. Only
        # removals from the last 7 days are shown, so a user who
        # already re-enabled (or stopped caring) does not keep
        # seeing a stale banner.
        removed = recent_dead_removal(conn, user["id"])
        if removed is not None:
            when = ""
            try:
                when = datetime.fromisoformat(
                    removed["created_at"]).strftime("%d/%m %H:%M")
            except (ValueError, TypeError):
                when = ""
            body += ('<div class="warn" role="alert"><strong>המנוי הוסר '
                     'כי הוא ישן — הפעל התראות מחדש מהטלפון.</strong> '
                     'ההתראה האחרונה נכשלה כי המנוי כבר לא תקין%s, '
                     'אז המערכת מחקה אותו. לחץ על "הפעל התראות" כאן '
                     'למעלה כדי ליצור מנוי חדש.</div>'
                     % (" (%s)" % when if when else ""))
        # Round 14, item 8: list the user's subscriptions by device
        # (this is also the connected-device count from round 13's
        # proposal 1). A subscription belongs to one browser on one
        # device — if every one of them is a desktop, the
        # notifications go to the computer, never the phone, so say
        # so plainly instead of letting the user wonder why the
        # phone stays silent.
        if rows:
            items = "".join("<li>%s</li>"
                            % esc(subscription_device_label(r))
                            for r in rows)
            body += ('<h2>המכשירים המחוברים (%d)</h2>'
                     '<ul>%s</ul>'
                     '<p class="muted">כל מנוי שייך לדפדפן אחד במכשיר '
                     'אחד — אם הפעלת מהמחשב, ההתראות יגיעו למחשב, לא '
                     'לטלפון.</p>' % (subs, items))
            if all((r.get("device") or "unknown") == "desktop"
                   for r in rows):
                body += ('<p class="err" role="alert">שים לב: כל '
                         'המנויים שלך הם מהמחשב — ההתראות יגיעו '
                         'למחשב, לא לטלפון. כדי לקבל התראות בטלפון, '
                         'פתח את האתר בטלפון והפעל שם התראות.</p>')
        return page_html("התראות", body, user, message, error)

    def _help(self, conn, user, message, error):
        """Rewrite 2026-11-10 (Daniel): the round-19 page was still
        far too long — the rules list had grown to ~27 paragraphs of
        obvious and irrelevant detail ("press I'm on my way" needs
        no explanation). Now it is four short parts: how it works in
        3 short numbered steps (Daniel's own wording, 2026-11-10), the
        rules in one short line each, a FAQ (collapsed, so it adds
        no visual length), and the problem -> what-to-do
        troubleshooting list. The details that left the rules live
        in the FAQ, not on the page itself. The floor rule was also
        WRONG in the old text: it said (in the "with floor 0"
        hypothetical) that balance 0 cannot request — it can. Only
        AT -2 is requesting blocked; the floor warning
        (floor_warning_html) already had this right."""
        body = """
        <h2>איך זה עובד</h2>
        <ol>
        <li>נרשמים עם שם מלא וטלפון, ועוקבים אחרי הקורסים שלכם
        או אחרי השיעורים המועדפים עליכם.</li>
        <li>מפעילים התראות בדף <a href="/push">התראות</a>. מי
        שמגיע מוקדם לשיעור מציע לשמור מקום, עד %(max_seats)d
        מקומות. כל מי שעוקב אחרי השיעור מקבל התראה לטלפון.</li>
        <li>מבקשים מקום, ומשלמים מטבע. אם הגעתם ולא באמת שמרו
        לכם מקום, לוחצים "המקום לא היה שם" והמטבע חוזר
        אליכם.</li>
        </ol>
        <h2>כללים בקצרה</h2>
        <ul>
        <li><strong>מטבעות:</strong> מתחילים עם %(start)d. כל בקשה
        עולה מטבע אחד, שמוקפא מיד ועובר לשומר רק אחרי שמאשרים
        הגעה. השומר מקבל מטבע על כל שמירת כיסא, מי שמבקש כיסא
        מבזבז מטבע.</li>
        <li><strong>הרצפה:</strong> המאזן יכול לרדת עד מינוס
        %(floor)d. כשמגיעים למינוס %(floor)d אי אפשר לבקש, עד
        ששומרים מקום למישהו אחר.</li>
        <li><strong>חלון ההצעה:</strong> אפשר להציע מקום רק
        %(window)d דקות לפני השיעור, ורק לשיעור של היום.</li>
        <li><strong>איחור:</strong> אפשר להגיע עד %(grace)d דקות
        אחרי תחילת השיעור. אחרת המטבע חוזר אליכם והמקום
        משתחרר.</li>
        <li><strong>שם מלא ו־✓:</strong> נרשמים עם שם מלא. ✓ ליד
        שם אומר שהמשתמש נבדק ואושר שהוא אמיתי.</li>
        <li><strong>טלפון:</strong> הטלפון חובה בהרשמה. הוא מוצג
        רק למי שהצעתם לו מקום או ביקשתם ממנו, ואף פעם לא
        ברשימה ציבורית.</li>
        </ul>
        <h2>שאלות נפוצות</h2>
        <details><summary>מה זה "מטבע מוקפא"?</summary>
        <p>מטבע שירד מהמאזן בעת בקשת מקום, אבל עוד לא עבר לשומר.
        הוא נשאר מוקפא עד שמאשרים הגעה (ואז הוא עובר לשומר), או
        עד ביטול, אי הגעה, או מקום שלא באמת היה שם (ואז הוא חוזר
        אליכם).</p>
        </details>
        <details><summary>ביקשתי מקום והשיעור כבר התחיל, מה קורה למטבע?</summary>
        <p>אם לא אישרתם הגעה עד %(grace)d דקות אחרי תחילת השיעור,
        המטבע חוזר אליכם לבד והמקום משתחרר. לא צריך לעשות
        כלום.</p>
        </details>
        <details><summary>מה זה ✓ ליד שם?</summary>
        <p>המשתמש נבדק לפי שם וטלפון ואושר כאמיתי. מי שאין לו ✓
        פשוט עוד לא אומת, לא בהכרח שהוא לא אמיתי.</p>
        </details>
        <details><summary>הגעתי והמקום לא היה שם, מה לעשות?</summary>
        <p>לוחצים "המקום לא היה שם" בדף "המקומות שלי". המטבע חוזר
        אליכם מיד. אפשר גם להתלונן על אותה בקשה, התלונה תיבדק
        והאמינות של השומר תרד בהתאם.</p>
        </details>
        <details><summary>איך מבטלים בקשה?</summary>
        <p>בדף "המקומות שלי", ליד הבקשה המוקפאת, יש כפתור ביטול.
        המטבע חוזר מיד והשומר מקבל התראה שבוטלה הבקשה.</p>
        </details>
        <details><summary>איך מוסיפים את האתר למסך הבית באייפון?</summary>
        <p>פותחים את האתר בספארי, לוחצים Share, ואז "הוסף למסך
        הבית". אחר כך פותחים אותו ממסך הבית ומפעילים שם התראות.</p>
        </details>
        <details><summary>למה אני לא מקבל התראות על כל הצעה?</summary>
        <p>התראה מגיעה רק על שיעורים שעוקבים אחריהם. מעקב כללי
        מתריע על כל הצעה בקורס; מעקב ספציפי מתריע רק על שיעורים
        שסימנתם. בודקים בדף "המעקבים שלי".</p>
        </details>
        <details><summary>מה זה "אני מאחר"?</summary>
        <p>יודעים שתאחרו? לוחצים "אני מאחר" בדף "המקומות שלי",
        כל עוד הבקשה מוקפאת ועד %(grace)d דקות אחרי תחילת
        השיעור. השומר מחליט: מאשר, והמקום נשמר עד
        %(late_grace)d דקות אחרי תחילת השיעור; דוחה, הבקשה
        מבוטלת והמטבע חוזר מיד. אם השומר לא החליט עד
        %(late_grace)d דקות אחרי תחילת השיעור, הבקשה פגה
        לבד.</p>
        </details>
        <details><summary>מה קורה אם השומר מבטל את ההצעה?</summary>
        <p>כל המטבעות המוקפאים חוזרים, וכל מי שביקש מקבל
        התראה.</p>
        </details>
        <details><summary>מה זה רשימת המתנה?</summary>
        <p>כשההצעה מלאה, מצטרפים לרשימת המתנה. כשמתפנה מקום
        מקבלים התראה לפני כולם.</p>
        </details>
        <details><summary>מה זה בקשה אוטומטית כל שבוע?</summary>
        <p>לשיעור קבוע שתמיד מגיעים אליו מאוחר, מגדירים בקשה
        אוטומטית בדף "המקומות שלי": כל שבוע, כשהחלון נפתח ויש
        הצעה פתוחה עם מקום פנוי, תתבצע אוטומטית בקשה לשמירת
        מקום. מי שבחופשה לא מקבל בקשות אוטומטיות.</p>
        </details>
        <details><summary>איך עובדת האמינות?</summary>
        <p>ליד שם השומר כתוב אחוז: כמה פעמים הוא באמת שמר את
        המקום שהבטיח. ביטול בזמן לא נספר.</p>
        </details>
        <details><summary>איך משתיקים התראות?</summary>
        <p>בדף "המקומות שלי": שעות שקטות (התראות לא נשלחות
        בשעות האלה) או חופשה (לא מגיעות התראות בכלל).</p>
        </details>
        <details><summary>שכחתי סיסמה, מה עושים?</summary>
        <p>מבקשים לאפס אותה, וקובעים חדשה בכניסה הבאה.</p>
        </details>
        <h2>התראה לא הגיעה? מה לבדוק</h2>
        <p>בודקים לפי הסדר — כל שורה היא בעיה אחת, ומה לעשות איתה:</p>
        <ul>
        <li><strong>פתחתם מהמקום הלא נכון:</strong> באייפון, פתחתם את
        האתר <strong>ממסך הבית</strong>, לא מהדפדפן? דחיפת Web עובדת
        רק כשפותחים ממסך הבית.</li>
        <li><strong>מצב "נא לא להפריע" או פוקוס דלוק:</strong> כבו אותו
        ובדקו שוב. פוקוס חוסם התראות גם כשהכל מופעל נכון.</li>
        </ul>
        """ % {"start": START_BALANCE, "max_seats": MAX_SEATS_PER_OFFER,
               "grace": CONFIRM_GRACE_MINUTES, "floor": -MIN_BALANCE,
               "window": OFFER_WINDOW_MINUTES,
               "late_grace": LATE_GRACE_MINUTES}
        return page_html("איך זה עובד?", body, user, message, error)
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/subscribe":
            return self._api_subscribe()
        # Round 20, item 11: the Service Worker posts a notification
        # action tap here (JSON, not a form). The user comes from the
        # session cookie — the fetch is same-origin, so the cookie
        # rides along, and nothing in the body can say who you are.
        if path == "/api/action":
            return self._api_action()
        form = self._read_form()
        get = lambda k, d=None: (form.get(k, [d])[0])
        conn = self._conn()
        try:
            settle_expired(conn)
            # SECURITY: the acting user comes ONLY from the session
            # cookie. Nothing in the form (no user_id field any more)
            # can change who you are.
            user = self._current_user(conn)
            def fail(msg):
                back = {"register": "/register", "login": "/login",
                        "set-password": "/set-password",
                        "add-course": "/courses",
                        "add-session": "/courses", "create-offer": "/offer",
                        "request-seat": "/offers", "add-follow": "/follows",
                        "join-waiting-list": "/offers",
                        "leave-waiting-list": "/offers",
                        "re-offer": "/my", "on-my-way": "/my",
                        "declare-late": "/my", "decide-late": "/my",
                        "cancel-request": "/my", "admin-confirm": "/my",
                        "admin-refund": "/my",
                        "create-auto-request": "/my",
                        "delete-auto-request": "/my",
                        "cancel-request-by-saver": "/my",
                        "edit-offer": "/my",
                        "admin-reset-password": "/admin",
                        "post-seat-request": "/wanted",
                        "cancel-seat-request": "/wanted",
                        "mark-arrived": "/offers",
                        "save-template": "/my", "delete-template": "/my",
                        "set-quiet-hours": "/my",
                        "set-vacation": "/my",
                        "set-vacation-until": "/my",
                        "test-push": "/push",
                        # Round 20: an error lands back on the page
                        # the user came from, not on the home page.
                        "rename": "/my", "set-phone": "/my",
                        "admin-verify": "/admin",
                        "delete-account": "/delete-account",
                        "admin-delete-account": "/admin",
                        "file-complaint": "/my",
                        "decide-complaint": "/admin",
                        "admin-broadcast": "/admin",
                        }.get(path.strip("/"), "/")
                # Round 13, item B: an error on the follow page must
                # land on the follow form too, not at the top of the
                # page — the fragment goes after the error query.
                fragment = ("#follow" if path in ("/add-follow",
                                                   "/remove-follow")
                            else "")
                self._redirect(back + "?error="
                               + urllib.parse.quote(msg) + fragment)
            if path == "/register":
                if get("password") != get("password2"):
                    return fail("הסיסמאות לא תואמות.")
                # Round 20, item 13: the phone is mandatory at
                # registration — create_user validates it (and the
                # full name) server-side, so a browser that skips
                # the `required` attribute cannot register without
                # one.
                new_user, err = create_user(conn, get("name"),
                                            get("password"), get("phone"))
                if err:
                    return fail(err)
                general_ids = set()
                for cid in form.get("course_id", []):
                    added, _err = add_follow(conn, new_user["id"], cid)
                    if added is not None:
                        general_ids.add(int(cid))
                for sid in form.get("session_id", []):
                    # Specific follows picked at registration: the
                    # session id is the whole request — add_follow
                    # looks the session up and derives the course
                    # from it, and rejects anything that is not a
                    # real session. A bad id is skipped, not an
                    # error: registration itself already succeeded.
                    # Round 14: a session of a course the user
                    # already follows generally is skipped — the
                    # general follow covers it, so a specific one
                    # would be a redundant duplicate.
                    session = get_session(conn, sid)
                    if session is not None and \
                            int(session["course_id"]) not in general_ids:
                        add_follow(conn, new_user["id"],
                                   session["course_id"], session_id=sid)
                cookie = self._login_cookie(conn, new_user["id"])
                # Registration ends at the follow page, not at /push:
                # the new user picks the lessons to follow first,
                # and the follow page itself links on to /push
                # (enable notifications). Existing users are
                # unaffected — this is only the post-register
                # redirect.
                return self._redirect(
                    "/follows?registered=1#follow", cookie=cookie)
            if path == "/login":
                u, err, needs_password = authenticate(
                    conn, get("name"), get("password"))
                if needs_password:
                    return self._redirect(
                        "/set-password?name=" + urllib.parse.quote(get("name") or ""))
                if err:
                    return fail(err)
                cookie = self._login_cookie(conn, u["id"])
                return self._redirect("/?saved=1", cookie=cookie)
            if path == "/set-password":
                if get("password") != get("password2"):
                    return fail("הסיסמאות לא תואמות.")
                u = get_user_by_name(conn, get("name"))
                if u is None:
                    return fail("משתמש לא נמצא.")
                err = set_password(conn, u["id"], get("password"))
                if err:
                    return fail(err)
                cookie = self._login_cookie(conn, u["id"])
                return self._redirect(
                    "/push?msg=" + urllib.parse.quote(
                        "הסיסמה נקבעה! עכשיו הפעל התראות."),
                    cookie=cookie)
            if path == "/logout":
                delete_session(conn, self._session_token())
                return self._redirect(
                    "/?msg=" + urllib.parse.quote("התנתקת."),
                    cookie="seat_session=; HttpOnly; Path=/; Max-Age=0")
            if path == "/add-course":
                if not is_admin(user):
                    return fail("אין לך הרשאה לפעולה הזו.")
                _, err = add_course(conn, get("name"))
                if err:
                    return fail(err)
                return self._redirect("/courses?saved=1")
            if path == "/add-session":
                if not is_admin(user):
                    return fail("אין לך הרשאה לפעולה הזו.")
                session, err = add_session(conn, get("course_id"), get("weekday"),
                                     get("start_time"), get("end_time"),
                                     get("building"), get("room"),
                                     get("teacher"))
                if err:
                    return fail(err)
                # A new session is news for the course's followers —
                # they may have been waiting for exactly this slot.
                notify_followers_of_new_session(
                    conn, session, exclude_user_id=user["id"])
                return self._redirect("/courses?saved=1")
            if path == "/add-follow":
                if user is None:
                    return fail("הירשם קודם.")
                if get("specific") or get("session_id"):
                    # Round 14: a course the user already follows
                    # generally needs no specific follows — the
                    # general one covers every session of it.
                    # Existing specific follows are NOT deleted
                    # (deleting a follow the user made on purpose
                    # is not this handler's job); only new
                    # redundant ones are skipped.
                    if has_general_follow(conn, user["id"],
                                          get("course_id")):
                        return self._redirect(
                            "/follows?msg=" + urllib.parse.quote(
                                "כבר עוקב אחרי כל הקורס הזה — אין צורך"
                                " במעקבים ספציפיים לשיעורים שלו.")
                            + "#follow")
                if get("specific"):
                    # Specific follow, several sessions at once: one
                    # checkbox per session, one follow per checked
                    # session. add_follow looks each session up and
                    # rejects anything that is not a real session of
                    # this course; already-followed sessions are
                    # skipped, not errors. No session checked at
                    # all is a mistake, not a follow.
                    session_ids = form.get("session_id", [])
                    if not session_ids:
                        return fail("בחר לפחות שיעור אחד.")
                    created = 0
                    for sid in session_ids:
                        _, err = add_follow(conn, user["id"],
                                            get("course_id"),
                                            session_id=sid)
                        if err is None:
                            created += 1
                    if created == 0:
                        return self._redirect(
                            "/follows?msg=" + urllib.parse.quote(
                                "כבר עוקב אחרי כל השיעורים שבחרת.")
                            + "#follow")
                    return self._redirect("/follows?saved=1#follow")
                if get("session_id"):
                    # Specific follow, one session (the old form):
                    # the session id is the whole request — add_follow
                    # looks the session up and rejects anything that
                    # is not a real session of this course.
                    _, err = add_follow(conn, user["id"], get("course_id"),
                                        session_id=get("session_id"))
                else:
                    _, err = add_follow(conn, user["id"], get("course_id"))
                if err:
                    return fail(err)
                return self._redirect("/follows?saved=1#follow")
            if path == "/remove-follow":
                if user is None:
                    return fail("הירשם קודם.")
                err = remove_follow(conn, get("follow_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/follows?saved=1#follow")
            if path == "/create-offer":
                if user is None:
                    return fail("הירשם קודם.")
                offer, err = create_offer(conn, user["id"], get("session_id"),
                                          get("note"), get("seats"))
                if err:
                    return fail(err)
                if get("save_template"):
                    # Save the same values as a template for next
                    # week. A duplicate template just updates — the
                    # offer is what matters, so the error is ignored.
                    save_offer_template(conn, user["id"], get("session_id"),
                                        get("note"), get("seats"))
                notify_followers(conn, offer)
                # And the board posters who asked for this exact
                # session — they wanted to know first.
                notify_board_requesters(conn, offer)
                return self._redirect("/?saved=1")
            if path == "/request-seat":
                if user is None:
                    return fail("הירשם קודם.")
                req, err = request_seat(conn, user["id"], get("offer_id"),
                                        seats=get("seats"))
                if err:
                    return fail(err)
                # The saver gets a push too, so they know someone asked
                # and can keep the seat — no more silent requests.
                notify_saver_of_request(conn, req)
                return self._redirect("/my?saved=1")
            if path == "/join-waiting-list":
                if user is None:
                    return fail("הירשם קודם.")
                _, err = join_waiting_list(conn, user["id"], get("offer_id"))
                if err:
                    return fail(err)
                return self._redirect("/offers?saved=1")
            if path == "/leave-waiting-list":
                if user is None:
                    return fail("הירשם קודם.")
                err = leave_waiting_list(conn, user["id"], get("offer_id"))
                if err:
                    return fail(err)
                return self._redirect("/offers?saved=1")
            if path == "/re-offer":
                if user is None:
                    return fail("הירשם קודם.")
                offer, err = re_offer(conn, get("offer_id"), user["id"])
                if err:
                    return fail(err)
                notify_followers(conn, offer)
                notify_board_requesters(conn, offer)
                return self._redirect("/?saved=1")
            if path == "/edit-offer":
                if user is None:
                    return fail("הירשם קודם.")
                err = edit_offer(conn, get("offer_id"), user["id"],
                                 get("note"), get("seats"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/on-my-way":
                if user is None:
                    return fail("הירשם קודם.")
                err = mark_on_my_way(conn, get("request_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/declare-late":
                if user is None:
                    return fail("הירשם קודם.")
                err = declare_late(conn, get("request_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/decide-late":
                if user is None:
                    return fail("הירשם קודם.")
                err = decide_late(conn, get("request_id"), user["id"],
                                  get("decision"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/confirm-arrival":
                if user is None:
                    return fail("הירשם קודם.")
                err = confirm_arrival(conn, get("request_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/report-not-saved":
                if user is None:
                    return fail("הירשם קודם.")
                err = report_not_saved(conn, get("request_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/cancel-offer":
                if user is None:
                    return fail("הירשם קודם.")
                err = cancel_offer(conn, get("offer_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/?saved=1")
            if path == "/cancel-request":
                if user is None:
                    return fail("הירשם קודם.")
                err = cancel_request(conn, get("request_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/cancel-request-by-saver":
                if user is None:
                    return fail("הירשם קודם.")
                err = cancel_request_by_saver(conn, get("request_id"),
                                              user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/admin-confirm":
                if user is None:
                    return fail("הירשם קודם.")
                err = admin_force_confirm(conn, get("request_id"), user)
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/admin-refund":
                if user is None:
                    return fail("הירשם קודם.")
                err = admin_force_refund(conn, get("request_id"), user)
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/admin-reset-password":
                if user is None:
                    return fail("הירשם קודם.")
                err = admin_reset_password(conn, user, get("user_id"))
                if err:
                    return fail(err)
                return self._redirect("/admin?saved=1")
            if path == "/test-push":
                # TEMPORARY — test aid, remove after pilot.
                if user is None:
                    return fail("הירשם קודם.")
                ok, msg = send_test_push(conn, user["id"])
                if ok:
                    return self._redirect(
                        "/push?msg=" + urllib.parse.quote(msg))
                return fail(msg)
            # Round 20: profile, verification, complaints, broadcast,
            # account deletion. Each handler is one function call
            # plus a redirect — the validation lives in the
            # function, not here.
            if path == "/rename":
                if user is None:
                    return fail("הירשם קודם.")
                err = rename_user(conn, user["id"], get("name"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/set-phone":
                if user is None:
                    return fail("הירשם קודם.")
                err = set_phone(conn, user["id"], get("phone"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/admin-verify":
                if user is None:
                    return fail("הירשם קודם.")
                err = set_verified(conn, user, get("user_id"),
                                   get("verified") == "1")
                if err:
                    return fail(err)
                return self._redirect("/admin?saved=1")
            if path == "/delete-account":
                if user is None:
                    return fail("הירשם קודם.")
                name, err = delete_account(conn, user["id"])
                if err:
                    return fail(err)
                # Deleting your own account logs you out: the
                # session cookie is cleared (same as /logout), so
                # the next page load is anonymous. The name is only
                # used in the message, never in a query.
                return self._redirect("/?msg=" + urllib.parse.quote(
                    "החשבון %s נמחק." % name),
                    cookie="seat_session=; HttpOnly; Path=/; Max-Age=0")
            if path == "/admin-delete-account":
                if user is None:
                    return fail("הירשם קודם.")
                try:
                    target_id = int(get("user_id"))
                except (TypeError, ValueError):
                    return fail("משתמש לא נמצא.")
                name, err = delete_account(conn, target_id)
                if err:
                    return fail(err)
                return self._redirect("/admin?msg=" + urllib.parse.quote(
                    "החשבון %s נמחק." % name))
            if path == "/file-complaint":
                if user is None:
                    return fail("הירשם קודם.")
                _, err = file_complaint(conn, get("request_id"),
                                        user["id"], get("reason"))
                if err:
                    return fail(err)
                return self._redirect("/my?msg=" + urllib.parse.quote(
                    "התלונה נשלחה לדניאל."))
            if path == "/decide-complaint":
                if user is None:
                    return fail("הירשם קודם.")
                err = decide_complaint(conn, get("complaint_id"), user,
                                       get("decision"), get("admin_note"))
                if err:
                    return fail(err)
                return self._redirect("/admin?saved=1")
            if path == "/admin-broadcast":
                if user is None:
                    return fail("הירשם קודם.")
                sent, err = send_broadcast(conn, user, get("title"),
                                           get("body"))
                if err:
                    return fail(err)
                return self._redirect("/admin?msg=" + urllib.parse.quote(
                    "ההודעה נשלחה ל־%d משתמשים." % sent))
            if path == "/create-auto-request":
                if user is None:
                    return fail("הירשם קודם.")
                _, err = create_auto_request(conn, user["id"],
                                             get("session_id"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/delete-auto-request":
                if user is None:
                    return fail("הירשם קודם.")
                err = delete_auto_request(conn, get("auto_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/post-seat-request":
                if user is None:
                    return fail("הירשם קודם.")
                _, err = post_seat_request(conn, user["id"], get("session_id"))
                if err:
                    return fail(err)
                return self._redirect("/wanted?saved=1")
            if path == "/cancel-seat-request":
                if user is None:
                    return fail("הירשם קודם.")
                err = cancel_seat_request(conn, get("request_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/wanted?saved=1")
            if path == "/mark-arrived":
                if user is None:
                    return fail("הירשם קודם.")
                err = mark_arrived_in_class(conn, get("offer_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/?saved=1")
            if path == "/save-template":
                if user is None:
                    return fail("הירשם קודם.")
                _, err = save_offer_template(conn, user["id"], get("session_id"),
                                             get("note"),
                                             get("seats"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/delete-template":
                if user is None:
                    return fail("הירשם קודם.")
                err = delete_offer_template(conn, get("template_id"), user["id"])
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/set-quiet-hours":
                if user is None:
                    return fail("הירשם קודם.")
                err = set_quiet_hours(conn, user["id"], get("quiet_start"),
                                      get("quiet_end"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/set-vacation":
                if user is None:
                    return fail("הירשם קודם.")
                # An end date rides along when turning vacation on
                # (the form on /my has the field). Validate it FIRST:
                # a bad date must not change anything — neither the
                # date nor the vacation state. The turn-off form has
                # no date field, so `until` is None there.
                until = get("vacation_until")
                if until is not None:
                    _, perr = _parse_vacation_until(until)
                    if perr:
                        return fail(perr)
                err = set_vacation(conn, user["id"], get("on"))
                if err:
                    return fail(err)
                if until:
                    err = set_vacation_until(conn, user["id"], until)
                    if err:
                        return fail(err)
                return self._redirect("/my?saved=1")
            if path == "/set-vacation-until":
                if user is None:
                    return fail("הירשם קודם.")
                err = set_vacation_until(conn, user["id"],
                                         get("vacation_until"))
                if err:
                    return fail(err)
                return self._redirect("/my?saved=1")
            self._send_html(page_html("לא נמצא", "<p>אין פעולה כזו.</p>",
                                      user), status=404)
        finally:
            conn.close()

    def _api_subscribe(self):
        data = self._read_json()
        sub = data.get("subscription") or {}
        conn = self._conn()
        try:
            # SECURITY: the subscription belongs to the logged-in user
            # from the session — the browser must not say whose it is.
            user = self._current_user(conn)
            if user is None:
                return self._send_json({"ok": False,
                                        "error": "לא מחובר."}, status=401)
            # Device detection (round 14, item 8): the User-Agent
            # says which device and browser the subscription belongs
            # to, and app.js reports whether the page runs as an
            # installed Home Screen app. Stored with the
            # subscription so /push can list the user's devices.
            device, browser = classify_user_agent(
                self.headers.get("User-Agent"))
            err = save_subscription(conn, user["id"], sub,
                                    device=device, browser=browser,
                                    standalone=bool(data.get("standalone")))
            if err:
                return self._send_json({"ok": False, "error": err}, status=400)
            return self._send_json({"ok": True})
        finally:
            conn.close()

    def _api_action(self):
        """Round 20, item 11: the Service Worker posts a notification
        action tap here — {action, request_id} as JSON. The action
        is one of the four buttons the reminder payloads carry
        ("on-my-way", "cancel-request", "confirm-arrival",
        "report-not-saved"), and it runs the SAME function the
        button on /my runs — no second implementation, so the
        action and the button can never disagree. The user comes
        from the session cookie; a tap on someone else's request
        fails inside those functions (they check ownership). The
        response is JSON: {ok: true} or {ok: false, error}. The
        Service Worker opens the app either way, so the user sees
        the result."""
        data = self._read_json()
        action = data.get("action") or ""
        request_id = data.get("request_id")
        conn = self._conn()
        try:
            user = self._current_user(conn)
            if user is None:
                return self._send_json({"ok": False,
                                        "error": "לא מחובר."}, status=401)
            handlers = {
                "on-my-way": mark_on_my_way,
                "cancel-request": cancel_request,
                "confirm-arrival": confirm_arrival,
                "report-not-saved": report_not_saved,
            }
            handler = handlers.get(action)
            if handler is None:
                return self._send_json({"ok": False,
                                        "error": "פעולה לא ידועה."},
                                       status=400)
            try:
                request_id = int(request_id)
            except (TypeError, ValueError):
                return self._send_json({"ok": False,
                                        "error": "בקשה לא נמצאה."},
                                       status=400)
            err = handler(conn, request_id, user["id"])
            if err:
                return self._send_json({"ok": False, "error": err},
                                       status=400)
            return self._send_json({"ok": True})
        finally:
            conn.close()


def _server_port():
    """The port the server listens on: the PORT environment variable
    (what real hosts set — Oracle, Render and friends hand the app a
    port instead of letting it pick one), default 8000. A bad value
    never crashes startup: it falls back to 8000 with a clear log
    line, so a typo in the environment costs a warning, not the app."""
    raw = os.environ.get("PORT")
    if raw is None or str(raw).strip() == "":
        return 8000
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        print("PORT לא תקין (%r) — משתמשים ב־8000." % raw)
        return 8000
    if not 0 < port < 65536:
        print("PORT מחוץ לטווח (%r) — משתמשים ב־8000." % raw)
        return 8000
    return port


def main():
    conn = connect()
    init_db(conn)
    conn.close()
    priv, pub = get_vapid_keys()
    port = _server_port()
    print("שומר מקומות רץ ב־http://localhost:%d" % port)
    if pub:
        print("Web Push: מפתח VAPID ציבורי מוגדר.")
    else:
        print("Web Push: כבוי — התקן את requirements.txt כדי להפעיל דחיפה.")
        print("  python3 -m venv venv && venv/bin/pip install -r requirements.txt")
    print("Database: %s" % DB_PATH)
    # Background thread: reminders + automatic weekly requests,
    # checked every 60 seconds. Daemon, so stopping the server stops
    # it too.
    # One thread inside this same Python process — no cron, no new
    # service (see EXPLANATION.md for why).
    thread = threading.Thread(target=background_loop, daemon=True,
                              name="seat-saver-background")
    thread.start()
    print("חוט רקע התחיל: תזכורות, בקשות אוטומטיות וניקוי "
          "חופשות נבדקים כל 60 שניות.")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

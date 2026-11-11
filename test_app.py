#!/usr/bin/env python3
"""Tests for Seat Saver. Run: python3 -m unittest test_app -v
No network is used: push delivery is verified with a fake sender.
No real VAPID keys are needed: the fake sender path uses dummy keys.
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app


def _mins_from_now(minutes):
    t = datetime.now() + timedelta(minutes=minutes)
    return t.strftime("%H:%M")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.conn = app.connect(self.db)
        app.init_db(self.conn)
        # Tests must never write a real vapid_keys.json into the
        # project folder: any key generation during a test (a push
        # path with no fake sender) lands in the temp dir instead.
        # Before this, running the suite with cryptography installed
        # created a real key file next to app.py.
        self._orig_keys_path = app.VAPID_KEYS_PATH
        app.VAPID_KEYS_PATH = os.path.join(self.tmp.name, "vapid_keys.json")
        # A session TODAY, starting in 10 minutes (inside the 60-min window).
        self.today = datetime.now().weekday()
        self.start = _mins_from_now(10)
        self.end = _mins_from_now(70)
        self.course = self.conn.execute(
            "SELECT * FROM courses WHERE name='תכנון אלגוריתמים'").fetchone()
        self.session = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, self.start, self.end)).fetchone()
        self.conn.commit()
        self.daniel = self.conn.execute(
            "SELECT * FROM users WHERE name='Daniel'").fetchone()
        self.lev = self.conn.execute(
            "SELECT * FROM users WHERE name='Lev'").fetchone()

    def tearDown(self):
        app.VAPID_KEYS_PATH = self._orig_keys_path
        self.conn.close()
        self.tmp.cleanup()

    def _offer(self, saver=None, seats=1):
        offer, err = app.create_offer(
            self.conn, (saver or self.daniel)["id"], self.session["id"], "שורה 2, ליד שקע", seats)
        self.assertIsNone(err, err)
        return offer

    def _request(self, offer, requester=None):
        req, err = app.request_seat(
            self.conn, (requester or self.lev)["id"], offer["id"])
        self.assertIsNone(err, err)
        return req


class RegistrationTest(Base):
    def test_seed_courses_and_users(self):
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0], 3)
        # Exactly the 18 sessions from Daniel's BGU course pages
        # (screenshots 2026-11-08): 7 תכנון אלגוריתמים, 4 חדו״א 2, 7 Systems.
        # (Base.setUp inserts one extra "today" session for the offer
        # flow, so it is excluded from the seed count here.)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM course_sessions WHERE id != ?",
                (self.session["id"],)).fetchone()[0], 18)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 5)

    def _seed_session_exists(self, course, weekday, start, building, room):
        row = self.conn.execute(
            "SELECT COUNT(*) FROM course_sessions s JOIN courses c"
            " ON s.course_id = c.id WHERE c.name=? AND s.weekday=?"
            " AND s.start_time=? AND s.building=? AND s.room=?",
            (course, weekday, start, building, room)).fetchone()[0]
        return row > 0

    def test_seed_sessions_match_screenshots(self):
        # תכנון אלגוריתמים: Ben Daniel lectures, Portal/Levi tutorials.
        self.assertTrue(self._seed_session_exists(
            "תכנון אלגוריתמים", 6, "18:00", "35", "1"))      # Sun lecture g1
        self.assertTrue(self._seed_session_exists(
            "תכנון אלגוריתמים", 2, "16:00", "92", "002"))    # Wed lecture g1
        self.assertTrue(self._seed_session_exists(
            "תכנון אלגוריתמים", 6, "16:00", "38", "010"))    # Sun lecture g3
        self.assertTrue(self._seed_session_exists(
            "תכנון אלגוריתמים", 2, "18:00", "34", "10"))     # Wed lecture g3
        self.assertTrue(self._seed_session_exists(
            "תכנון אלגוריתמים", 0, "10:00", "90", "222"))    # Mon tutorial Levi
        # חדו״א 2: Landsman lectures, Luzon tutorials.
        self.assertTrue(self._seed_session_exists(
            "חדו״א 2", 2, "08:00", "35", "2"))      # Wed lecture
        self.assertTrue(self._seed_session_exists(
            "חדו״א 2", 0, "14:00", "32", "307"))    # Mon lecture
        self.assertTrue(self._seed_session_exists(
            "חדו״א 2", 1, "10:00", "90", "224"))    # Tue tutorial Luzon
        # Systems: Adler lectures, Beitan/Netai tutorials.
        self.assertTrue(self._seed_session_exists(
            "תכנות מערכות", 6, "10:00", "32", "306"))  # Sun lecture
        self.assertTrue(self._seed_session_exists(
            "תכנות מערכות", 1, "10:00", "28", "104"))  # Tue Beitan
        self.assertTrue(self._seed_session_exists(
            "תכנות מערכות", 1, "14:00", "90", "234"))  # Tue Netai
        # Correction 2026-11-10 (Daniel): the Adler lecture rooms
        # were swapped — Thu 12:00 is bldg 34/2, Sun 12:00 is bldg
        # 35/2 (not the other way round).
        self.assertTrue(self._seed_session_exists(
            "תכנות מערכות", 3, "12:00", "34", "2"))  # Thu Adler
        self.assertTrue(self._seed_session_exists(
            "תכנות מערכות", 6, "12:00", "35", "2"))  # Sun Adler

    def test_old_wrong_seed_sessions_are_gone(self):
        # The first build seeded תכנון אלגוריתמים from the calendar, with the
        # wrong lecture: Wed 14:00-16:00 bldg 34/214 (Moshiov) and a
        # Mon 10:00 tutorial in bldg 90/139. The screenshots show
        # neither — תכנון אלגוריתמים lectures are Wed 16:00 and 18:00.
        self.assertFalse(self._seed_session_exists(
            "תכנון אלגוריתמים", 2, "14:00", "34", "214"))
        self.assertFalse(self._seed_session_exists(
            "תכנון אלגוריתמים", 0, "10:00", "90", "139"))

    def test_new_user_starts_with_3_coins(self):
        user, err = app.create_user(self.conn, "Noa Malka", "secret123", "0501234567")
        self.assertIsNone(err)
        self.assertEqual(user["balance"], app.START_BALANCE)

    def test_duplicate_empty_and_short_password_rejected(self):
        # Round 20: names are full names now — "Daniel" (seeded,
        # single word) no longer registers at all, so the duplicate
        # is tested against a name created first in this test.
        first, err = app.create_user(self.conn, "Noa Malka", "secret123",
                                     "0501234567")
        self.assertIsNone(err, err)
        _, err = app.create_user(self.conn, "Noa Malka", "secret123",
                                 "0501234567")
        self.assertIsNotNone(err)
        _, err = app.create_user(self.conn, "   ", "secret123")
        self.assertIsNotNone(err)
        _, err = app.create_user(self.conn, "Maya Levi", "abc", "0501234567")
        self.assertIsNotNone(err)  # shorter than MIN_PASSWORD_LEN (4)
        user, err = app.create_user(self.conn, "Maya Levi", "abcd",
                                    "0501234567")
        self.assertIsNone(err, err)
        self.assertEqual(user["name"], "Maya Levi")

    def test_open_registration_no_invite_needed(self):
        # Twenty friends, unknown in advance: anyone with the link joins.
        # Two-letter suffixes — a one-character word (like "A") is
        # not a real name (see _valid_full_name).
        for i in range(20):
            suffix = chr(65 + i // 26) + chr(65 + i % 26)
            user, err = app.create_user(
                self.conn, "Test Friend " + suffix, "pw1234",
                "0501234567")
            self.assertIsNone(err, err)
            self.assertEqual(user["balance"], 3)


class AuthTest(Base):
    def test_password_is_hashed_not_plaintext(self):
        user, _ = app.create_user(self.conn, "Noa Malka", "secret123", "0501234567")
        stored = user["password_hash"]
        self.assertIsNotNone(stored)
        self.assertNotIn("secret123", stored)
        self.assertIn(":", stored)  # salt_hex:hash_hex
        self.assertTrue(app.verify_password(user, "secret123"))
        self.assertFalse(app.verify_password(user, "wrongpass"))
        # Same password, different user -> different hash (fresh salt).
        user2, _ = app.create_user(self.conn, "Maya Levi", "secret123", "0501234567")
        self.assertNotEqual(stored, user2["password_hash"])

    def test_authenticate_correct_wrong_and_unknown(self):
        app.create_user(self.conn, "Noa Malka", "secret123", "0501234567")
        user, err, needs = app.authenticate(self.conn, "Noa Malka", "secret123")
        self.assertIsNone(err)
        self.assertFalse(needs)
        self.assertEqual(user["name"], "Noa Malka")
        user, err, needs = app.authenticate(self.conn, "Noa Malka", "nope123")
        self.assertIsNone(user)
        self.assertIsNotNone(err)
        # The error never says whether the name or the password was wrong.
        self.assertNotIn("Noa", err)
        user, err, needs = app.authenticate(self.conn, "Ghost", "secret123")
        self.assertIsNone(user)
        self.assertIsNotNone(err)

    def test_migration_user_without_password_sets_once(self):
        # Seeded users were created before passwords existed.
        self.assertIsNone(self.daniel["password_hash"])
        user, err, needs = app.authenticate(self.conn, "Daniel", "anything")
        self.assertTrue(needs)
        self.assertEqual(user["name"], "Daniel")
        # Set a password once, then it is required.
        err = app.set_password(self.conn, self.daniel["id"], "daniel123")
        self.assertIsNone(err)
        user, err, needs = app.authenticate(self.conn, "Daniel", "daniel123")
        self.assertIsNone(err)
        self.assertFalse(needs)
        user, err, needs = app.authenticate(self.conn, "Daniel", "wrong123")
        self.assertIsNone(user)
        self.assertIsNotNone(err)
        # Second set_password must refuse — it can never overwrite a
        # password that is already set.
        err = app.set_password(self.conn, self.daniel["id"], "other123")
        self.assertIsNotNone(err)

    def test_session_token_roundtrip_expiry_and_logout(self):
        token = app.create_session(self.conn, self.lev["id"])
        self.assertTrue(token)
        user = app.get_user_from_session(self.conn, token)
        self.assertEqual(user["name"], "Lev")
        # A made-up token gives no one.
        self.assertIsNone(app.get_user_from_session(self.conn, "bogus"))
        # An expired session is rejected (and deleted).
        old = app.create_session(
            self.conn, self.lev["id"],
            now=datetime.now() - timedelta(days=app.SESSION_DAYS + 1))
        self.assertIsNone(app.get_user_from_session(self.conn, old))
        self.assertIsNone(
            self.conn.execute("SELECT * FROM sessions WHERE token=?",
                              (old,)).fetchone())
        # Logout deletes the session: the token stops working.
        app.delete_session(self.conn, token)
        self.assertIsNone(app.get_user_from_session(self.conn, token))

    def test_clear_password_resets_to_migration(self):
        app.set_password(self.conn, self.daniel["id"], "daniel123")
        app.clear_password(self.conn, self.daniel["id"])
        row = app.get_user(self.conn, self.daniel["id"])
        self.assertIsNone(row["password_hash"])
        _, _, needs = app.authenticate(self.conn, "Daniel", "daniel123")
        self.assertTrue(needs)


class FollowTest(Base):
    def test_general_follow_matches_any_session(self):
        follow, err = app.add_follow(self.conn, self.lev["id"],
                                     self.course["id"])
        self.assertIsNone(err)
        self.assertTrue(app.follow_matches(dict(follow), dict(self.session)))

    def test_specific_follow_matches_only_its_day_and_time(self):
        follow, err = app.add_follow(self.conn, self.lev["id"],
                                     self.course["id"],
                                     session_id=self.session["id"])
        self.assertIsNone(err, err)
        self.assertTrue(app.follow_matches(dict(follow), dict(self.session)))
        # A different session on another day, or at another time, does
        # not match — even one that shares the day+time shape. The
        # follow stores the exact session id (round 14), so the
        # comparison is by id; the dicts below get a different id,
        # as a real different session would have.
        other_day = dict(self.session)
        other_day["id"] = self.session["id"] + 1000
        other_day["weekday"] = (self.today + 1) % 7
        self.assertFalse(app.follow_matches(dict(follow), other_day))
        other_time = dict(self.session)
        other_time["id"] = self.session["id"] + 1000
        other_time["start_time"] = "08:00"
        self.assertFalse(app.follow_matches(dict(follow), other_time))

    def test_specific_follow_requires_real_session(self):
        # A session of another course: rejected.
        calc = self.conn.execute(
            "SELECT * FROM courses WHERE name='חדו״א 2'").fetchone()
        calc_session = self.conn.execute(
            "SELECT * FROM course_sessions WHERE course_id=? LIMIT 1",
            (calc["id"],)).fetchone()
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"],
                                session_id=calc_session["id"])
        self.assertIsNotNone(err)
        # A session that does not exist: rejected.
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"], session_id=999999)
        self.assertIsNotNone(err)
        # Garbage: rejected, never crashes.
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"], session_id="nope")
        self.assertIsNotNone(err)
        # A real session of this course works, and derives day + time.
        follow, err = app.add_follow(self.conn, self.lev["id"],
                                     self.course["id"],
                                     session_id=self.session["id"])
        self.assertIsNone(err, err)
        self.assertEqual(follow["weekday"], self.today)
        self.assertEqual(follow["start_time"], self.start)
        # The same specific follow twice: rejected (duplicate).
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"],
                                session_id=self.session["id"])
        self.assertIsNotNone(err)

    def test_general_follow_still_one_click(self):
        follow, err = app.add_follow(self.conn, self.lev["id"],
                                     self.course["id"])
        self.assertIsNone(err, err)
        self.assertIsNone(follow["weekday"])
        self.assertIsNone(follow["start_time"])
        # The same general follow twice: rejected. (SQLite UNIQUE
        # treats NULL as distinct, so this needs an explicit check —
        # a second identical general follow used to slip through.)
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"])
        self.assertIsNotNone(err)

    def test_followers_exclude_saver_and_dedupe(self):
        # Lev follows generally AND specifically: listed once, not twice.
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"],
                       session_id=self.session["id"])
        offer = self._offer()
        followers = app.followers_for_offer(self.conn, offer)
        self.assertIn(self.lev["id"], followers)
        self.assertNotIn(self.daniel["id"], followers)  # saver excluded
        self.assertEqual(len(followers), 1)  # Daniel follows too, but is saver

    def test_specific_follow_gets_no_notification_for_other_session(self):
        # Lev follows a session of the same course, but on another day.
        other = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], (self.today + 1) % 7, self.start,
             self.end)).fetchone()
        self.conn.commit()
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"], session_id=other["id"])
        self.assertIsNone(err, err)
        offer = self._offer()
        self.assertNotIn(self.lev["id"],
                         app.followers_for_offer(self.conn, offer))


class OfferTest(Base):
    def test_offer_opens_only_60_minutes_before(self):
        far = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, _mins_from_now(120),
             _mins_from_now(180))).fetchone()
        self.conn.commit()
        _, err = app.create_offer(self.conn, self.daniel["id"], far["id"], "", 1)
        self.assertIsNotNone(err)
        self.assertIn("60 דקות", err)

    def test_offer_window_boundaries(self):
        # Session starting in 2 hours, called with explicit `now`.
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, _mins_from_now(120),
             _mins_from_now(180))).fetchone()
        self.conn.commit()
        # 61 minutes before start: too early, window not open yet.
        _, err = app.create_offer(
            self.conn, self.daniel["id"], s["id"], "", 1,
            now=datetime.now() + timedelta(minutes=59))
        self.assertIsNotNone(err)
        # Exactly 60 minutes before start: window just opened.
        offer, err = app.create_offer(
            self.conn, self.daniel["id"], s["id"], "", 1,
            now=datetime.now() + timedelta(minutes=60))
        self.assertIsNone(err, err)
        self.assertEqual(offer["seats_total"], 1)
        # After class start: too late.
        _, err = app.create_offer(
            self.conn, self.lev["id"], s["id"], "", 1,
            now=datetime.now() + timedelta(minutes=121))
        self.assertIsNotNone(err)

    def test_max_five_seats_per_offer(self):
        # 2026-11-10: Daniel reduced the maximum from 10 to 5.
        _, err = app.create_offer(self.conn, self.daniel["id"],
                                  self.session["id"], "", 11)
        self.assertIsNotNone(err)
        _, err = app.create_offer(self.conn, self.daniel["id"],
                                  self.session["id"], "", 6)
        self.assertIsNotNone(err)
        _, err = app.create_offer(self.conn, self.daniel["id"],
                                  self.session["id"], "", 0)
        self.assertIsNotNone(err)
        offer, err = app.create_offer(self.conn, self.daniel["id"],
                                      self.session["id"], "", 5)
        self.assertIsNone(err, err)
        self.assertEqual(offer["seats_total"], 5)

    def test_no_duplicate_open_offer(self):
        self._offer()
        _, err = app.create_offer(self.conn, self.daniel["id"],
                                  self.session["id"], "", 1)
        self.assertIsNotNone(err)

    def test_offer_expires_at_class_start(self):
        offer = self._offer()
        start = datetime.now() + timedelta(minutes=10)
        expired, _ = app.settle_expired(self.conn, now=start + timedelta(minutes=1))
        self.assertEqual(expired, 1)
        self.assertEqual(app.get_offer(self.conn, offer["id"])["status"],
                         "expired")
        _, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                  now=start + timedelta(minutes=1))
        self.assertIsNotNone(err)

    def test_session_window_status(self):
        # Session starts in 10 min: window (60 min before) is open now.
        status, opens = app.session_window_status(dict(self.session))
        self.assertEqual(status, "open")
        # Starts in 2 hours: window not open yet, opens at start - 60.
        far = dict(self.session)
        far["start_time"] = _mins_from_now(120)
        far["end_time"] = _mins_from_now(180)
        status, opens = app.session_window_status(far)
        self.assertEqual(status, "not_yet")
        start_min = app._to_minutes(far["start_time"]) - 60
        self.assertEqual(opens, "%02d:%02d" % (start_min // 60, start_min % 60))
        # Started 10 min ago, ends later: started.
        past = dict(self.session)
        past["start_time"] = _mins_from_now(-10)
        past["end_time"] = _mins_from_now(50)
        self.assertEqual(app.session_window_status(past)[0], "started")
        # Ended already: ended.
        done = dict(self.session)
        done["start_time"] = _mins_from_now(-70)
        done["end_time"] = _mins_from_now(-10)
        self.assertEqual(app.session_window_status(done)[0], "ended")


class OfferableTest(Base):
    def _session_in(self, minutes_from_now, duration=60):
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today,
             _mins_from_now(minutes_from_now),
             _mins_from_now(minutes_from_now + duration))).fetchone()
        self.conn.commit()
        return s

    def test_only_in_window_sessions_are_offerable(self):
        near = self._session_in(30)    # starts in 30 min: in window
        far = self._session_in(120)    # starts in 2 hours: not yet
        past = self._session_in(-30)   # started 30 min ago: too late
        ids = [s["id"] for s in app.offerable_sessions(self.conn)]
        self.assertIn(self.session["id"], ids)  # starts in 10 min
        self.assertIn(near["id"], ids)
        self.assertNotIn(far["id"], ids)
        self.assertNotIn(past["id"], ids)

    def test_offerable_at_exact_window_open(self):
        edge = self._session_in(60)  # window opens exactly now
        ids = [s["id"] for s in app.offerable_sessions(self.conn)]
        self.assertIn(edge["id"], ids)


class TeacherTest(Base):
    def _teacher_of(self, course, weekday, start, building, room):
        row = self.conn.execute(
            "SELECT teacher FROM course_sessions s JOIN courses c"
            " ON s.course_id = c.id WHERE c.name=? AND s.weekday=?"
            " AND s.start_time=? AND s.building=? AND s.room=?",
            (course, weekday, start, building, room)).fetchone()
        return row["teacher"] if row else None

    def test_all_seeded_sessions_have_teachers(self):
        # 18 seeded sessions, each with a teacher. (Base adds one
        # extra session without a teacher, excluded here.)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM course_sessions WHERE teacher != ''"
            " AND id != ?", (self.session["id"],)).fetchone()[0]
        self.assertEqual(n, 18)

    def test_teacher_values_match_screenshots(self):
        # Same day + time, different rooms: lecture vs tutorial.
        self.assertEqual(self._teacher_of(
            "תכנון אלגוריתמים", 6, "18:00", "35", "1"), "ד״ר בן דניאל")
        self.assertEqual(self._teacher_of(
            "תכנון אלגוריתמים", 6, "18:00", "34", "14"), "מר פורטל")
        self.assertEqual(self._teacher_of(
            "תכנון אלגוריתמים", 6, "16:00", "38", "010"), "ד״ר בן דניאל")
        self.assertEqual(self._teacher_of(
            "תכנון אלגוריתמים", 6, "16:00", "35", "213"), "מר פורטל")
        self.assertEqual(self._teacher_of(
            "תכנון אלגוריתמים", 0, "10:00", "90", "222"), "מר לוי")
        self.assertEqual(self._teacher_of(
            "חדו״א 2", 2, "08:00", "35", "2"), "ד״ר לנדסמן")
        self.assertEqual(self._teacher_of(
            "חדו״א 2", 0, "12:00", "90", "223"), "מר לוזון")
        self.assertEqual(self._teacher_of(
            "תכנות מערכות", 6, "10:00", "32", "306"), "ד״ר אדלר")
        self.assertEqual(self._teacher_of(
            "תכנות מערכות", 1, "10:00", "28", "104"), "מר ביתן")
        self.assertEqual(self._teacher_of(
            "תכנות מערכות", 1, "14:00", "90", "234"), "מר נתאי")

    def test_session_label_shows_teacher_and_place(self):
        row = self.conn.execute(
            "SELECT * FROM course_sessions s JOIN courses c"
            " ON s.course_id = c.id WHERE c.name='תכנון אלגוריתמים'"
            " AND s.weekday=6 AND s.start_time='18:00'"
            " AND s.building='35'").fetchone()
        label = app._session_label(dict(row), "תכנון אלגוריתמים")
        self.assertIn("ד״ר בן דניאל", label)
        self.assertIn("בניין 35 חדר 1", label)
        # A session without a teacher still labels fine.
        label2 = app._session_label(dict(self.session), "תכנון אלגוריתמים")
        self.assertIn("בניין 34 חדר 214", label2)

    def test_migration_fills_teachers_on_old_db(self):
        # A database from before teachers existed: no teacher column.
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "old.db")
        conn = app.connect(db)
        schema_old = app.SCHEMA.replace(
            "    room TEXT NOT NULL DEFAULT '',\n"
            "    teacher TEXT NOT NULL DEFAULT ''",
            "    room TEXT NOT NULL DEFAULT ''")
        conn.executescript(schema_old)
        now = datetime.now().isoformat(timespec="seconds")
        for name, kind, sessions in app.SEED_COURSES:
            cur = conn.execute(
                "INSERT INTO courses (name, kind) VALUES (?, ?)",
                (name, kind))
            cid = cur.lastrowid
            for wd, start, end, bldg, room, _teacher in sessions:
                conn.execute(
                    "INSERT INTO course_sessions (course_id, weekday,"
                    " start_time, end_time, building, room)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (cid, wd, start, end, bldg, room))
        conn.execute(
            "INSERT INTO users (name, balance, sample, created_at)"
            " VALUES ('Daniel', 3, 1, ?)", (now,))
        conn.commit()
        # Before migration: no teacher column, data intact.
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(course_sessions)")]
        self.assertNotIn("teacher", cols)
        # Run init_db: column appears, teachers filled, nothing lost.
        app.init_db(conn)
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(course_sessions)")]
        self.assertIn("teacher", cols)
        n = conn.execute(
            "SELECT COUNT(*) FROM course_sessions"
            " WHERE teacher != ''").fetchone()[0]
        self.assertEqual(n, 18)
        row = conn.execute(
            "SELECT teacher FROM course_sessions s JOIN courses c"
            " ON s.course_id = c.id WHERE c.name='תכנון אלגוריתמים'"
            " AND s.weekday=6 AND s.start_time='18:00'"
            " AND s.building='34'").fetchone()
        self.assertEqual(row["teacher"], "מר פורטל")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        conn.close()
        tmp.cleanup()

    def test_migration_fixes_swapped_adler_rooms(self):
        # Correction 2026-11-10 (Daniel): an old DB has the Adler
        # lecture rooms swapped (Thu 35/2, Sun 34/2). init_db swaps
        # them back; a DB that is already right is untouched, and a
        # second run is a no-op.
        cid = self.conn.execute(
            "SELECT id FROM courses WHERE name='תכנות מערכות'"
            ).fetchone()[0]
        # Put the wrong rooms back in.
        self.conn.execute(
            "UPDATE course_sessions SET building='35'"
            " WHERE course_id=? AND weekday=3 AND start_time='12:00'",
            (cid,))
        self.conn.execute(
            "UPDATE course_sessions SET building='34'"
            " WHERE course_id=? AND weekday=6 AND start_time='12:00'",
            (cid,))
        self.conn.commit()
        app.init_db(self.conn)
        thu = self.conn.execute(
            "SELECT building, room FROM course_sessions"
            " WHERE course_id=? AND weekday=3 AND start_time='12:00'",
            (cid,)).fetchone()
        self.assertEqual((thu["building"], thu["room"]), ("34", "2"))
        sun = self.conn.execute(
            "SELECT building, room FROM course_sessions"
            " WHERE course_id=? AND weekday=6 AND start_time='12:00'",
            (cid,)).fetchone()
        self.assertEqual((sun["building"], sun["room"]), ("35", "2"))
        app.init_db(self.conn)
        thu = self.conn.execute(
            "SELECT building, room FROM course_sessions"
            " WHERE course_id=? AND weekday=3 AND start_time='12:00'",
            (cid,)).fetchone()
        self.assertEqual((thu["building"], thu["room"]), ("34", "2"))


class HelpAndFloorTest(Base):
    def _handler(self):
        return app.Handler.__new__(app.Handler)

    def _set_balance(self, user_id, balance):
        self.conn.execute("UPDATE users SET balance=? WHERE id=?",
                          (balance, user_id))
        self.conn.commit()
        return app.get_user(self.conn, user_id)

    def test_floor_warning_appears_only_at_floor(self):
        lev = self._set_balance(self.lev["id"], -2)
        warn = app.floor_warning_html(dict(lev))
        self.assertIn("מינוס 2", warn)
        self.assertIn("אי אפשר לבקש מקום", warn)
        for balance in (3, 0, -1, 1):
            user = self._set_balance(self.lev["id"], balance)
            self.assertEqual(app.floor_warning_html(dict(user)), "")

    def test_no_page_outside_help_mentions_the_floor(self):
        h = self._handler()
        lev = self._set_balance(self.lev["id"], 3)
        for html in (app.page_html("כותרת", "<p>גוף</p>", user=dict(lev)),
                     h._home(self.conn, dict(lev), None, None),
                     h._register(self.conn, None, None, None),
                     h._my(self.conn, dict(lev), None, None),
                     h._offer_form(self.conn, dict(lev), None, None),
                     h._courses(self.conn, dict(lev), None, None),
                     h._offers(self.conn, dict(lev), None, None)):
            self.assertNotIn("רצפה", html)
        # ...but the warning DOES appear on home and /my at -2.
        at_floor = self._set_balance(self.lev["id"], -2)
        self.assertIn("מינוס 2",
                      h._home(self.conn, dict(at_floor), None, None))
        self.assertIn("מינוס 2",
                      h._my(self.conn, dict(at_floor), None, None))

    def test_help_page_explains_everything_including_floor(self):
        h = self._handler()
        html = h._help(self.conn, None, None, None)
        for word in ("איך זה עובד?", "מטבע", "רצפה", "60", "10",
                     "מעקב", "התראות", "אייפון", "סיסמה"):
            self.assertIn(word, html)
        self.assertIn("מינוס 2", html)
        # The help link is in the nav of every page.
        self.assertIn('href="/help"', app.page_html("x", "y"))


class CoinTest(Base):
    def test_request_freezes_one_coin(self):
        offer = self._offer()
        self._request(offer)
        lev = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(lev["balance"], 2)  # 3 - 1 frozen
        self.assertEqual(app.available_balance(self.conn, self.lev["id"]), 2)

    def test_arrival_transfers_coin_to_saver(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 4)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "confirmed")

    def test_only_requester_can_confirm(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.confirm_arrival(self.conn, req["id"], self.daniel["id"])
        self.assertIsNotNone(err)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 3)

    def test_floor_minus_two_blocks_request(self):
        # Lev: request, confirm (balance 2), then drive to the floor.
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        # Balance 2. Spend down to -2 by requesting from other savers.
        for i in range(4):
            saver = self.conn.execute(
                "SELECT * FROM users WHERE name=?", ("Levi",)).fetchone() \
                if i % 2 == 0 else self.conn.execute(
                "SELECT * FROM users WHERE name=?", ("Shaked",)).fetchone()
            # fresh offer each time (different session to avoid duplicates)
            s = self.conn.execute(
                "INSERT INTO course_sessions (course_id, weekday, start_time,"
                " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
                " RETURNING *",
                (self.course["id"], self.today, self.start,
                 self.end)).fetchone()
            self.conn.commit()
            o, err = app.create_offer(self.conn, saver["id"], s["id"], "", 1)
            self.assertIsNone(err, err)
            r, err = app.request_seat(self.conn, self.lev["id"], o["id"])
            self.assertIsNone(err, err)
            app.confirm_arrival(self.conn, r["id"], self.lev["id"])
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], -2)
        o2, _ = app.create_offer(
            self.conn,
            self.conn.execute("SELECT * FROM users WHERE name='Yehonatan'").fetchone()["id"],
            self.conn.execute(
                "INSERT INTO course_sessions (course_id, weekday, start_time,"
                " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
                " RETURNING *",
                (self.course["id"], self.today, self.start,
                 self.end)).fetchone()["id"], "", 1)
        self.conn.commit()
        _, err = app.request_seat(self.conn, self.lev["id"], o2["id"])
        self.assertIsNotNone(err)
        self.assertIn("רצפה", err)

    def test_no_show_refunds_after_start_plus_five(self):
        offer = self._offer()
        self._request(offer)
        start = datetime.now() + timedelta(minutes=10)
        _, refunded = app.settle_expired(
            self.conn, now=start + timedelta(minutes=6))
        self.assertEqual(refunded, 1)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 3)
        row = self.conn.execute(
            "SELECT * FROM requests WHERE offer_id=?", (offer["id"],)).fetchone()
        self.assertEqual(row["status"], "no_show")

    def test_not_saved_refunds(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.report_not_saved(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 3)

    def test_saver_cancel_refunds(self):
        offer = self._offer()
        self._request(offer)
        err = app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["status"], "cancelled")

    def test_cannot_request_own_offer_or_twice_or_when_full(self):
        offer = self._offer(seats=1)
        _, err = app.request_seat(self.conn, self.daniel["id"], offer["id"])
        self.assertIsNotNone(err)
        self._request(offer)
        _, err = app.request_seat(self.conn, self.lev["id"], offer["id"])
        self.assertIsNotNone(err)  # already requested
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        _, err = app.request_seat(self.conn, levi["id"], offer["id"])
        self.assertIsNotNone(err)  # seat full


class PushTest(Base):
    def test_subscription_storage(self):
        sub = {"endpoint": "https://example.com/push/abc",
               "keys": {"p256dh": "AAA", "auth": "BBB"}}
        err = app.save_subscription(self.conn, self.lev["id"], sub)
        self.assertIsNone(err)
        row = self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(row["endpoint"], "https://example.com/push/abc")
        # Bad subscription rejected, never stored.
        err = app.save_subscription(self.conn, self.lev["id"],
                                    {"endpoint": "http://x"})
        self.assertIsNotNone(err)

    def test_has_push_subscription(self):
        self.assertFalse(app.has_push_subscription(self.conn, self.lev["id"]))
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        self.assertTrue(app.has_push_subscription(self.conn, self.lev["id"]))

    def test_notify_calls_sender_only_for_matching_followers(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        # Levi follows a different course only: no notification.
        calc = self.conn.execute(
            "SELECT * FROM courses WHERE name='חדו״א 2'").fetchone()
        app.add_follow(self.conn, levi["id"], calc["id"])
        for u in (self.lev, levi):
            app.save_subscription(self.conn, u["id"], {
                "endpoint": "https://example.com/push/%s" % u["id"],
                "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        sent = []
        app.notify_followers(self.conn, offer,
                             sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(len(sent), 1)
        self.assertIn("example.com/push/%s" % self.lev["id"],
                      sent[0]["endpoint"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchall()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["status"], "sent")

    def test_notify_skips_follower_without_subscription(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()
        results = app.notify_followers(
            self.conn, offer, sender=lambda sub, payload: None)
        self.assertEqual(results[0][1], "skipped-no-subscription")
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-no-subscription")

    def test_push_payload_names_course_and_saver(self):
        offer = self._offer()
        d = dict(offer)
        d["saver_name"] = "Daniel"
        payload = app.prepare_push_payload(d, dict(self.course),
                                           dict(self.session))
        self.assertIn("תכנון אלגוריתמים", payload["title"])
        self.assertIn("מקום פנוי", payload["title"])
        self.assertIn("Daniel", payload["body"])
        # Round 13: zones are gone — the payload names the saver and
        # the place, never a zone.
        self.assertNotIn("אזור", payload["body"])


class AdminTest(Base):
    def test_add_course_and_session(self):
        course, err = app.add_course(self.conn, "Linear Algebra")
        self.assertIsNone(err)
        sess, err = app.add_session(self.conn, course["id"], 0, "10:00",
                                    "12:00", "90", "100", "ד״ר חדש")
        self.assertIsNone(err)
        self.assertEqual(sess["building"], "90")
        self.assertEqual(sess["teacher"], "ד״ר חדש")
        _, err = app.add_session(self.conn, course["id"], 0, "12:00",
                                 "10:00", "90", "100")
        self.assertIsNotNone(err)  # end before start

    def test_only_daniel_is_admin(self):
        self.assertTrue(app.is_admin(dict(self.daniel)))
        self.assertFalse(app.is_admin(dict(self.lev)))
        self.assertFalse(app.is_admin(None))

    def test_admin_is_daniel_bik_not_danielush(self):
        # Round 13: the pilot admin is a SEPARATE account named
        # "דניאל ביק" (Daniel registers it himself — no password
        # is ever written in code). The seeded English "Daniel"
        # stays admin so tests keep working. Daniel's everyday
        # account (here "דניאל לוי" — the old nickname "דניאלוש"
        # no longer registers, round 20 requires a full name) is a
        # REGULAR user — not admin.
        daniel_bik, err = app.create_user(self.conn, "דניאל ביק",
                                          "admin1234", "0501234567")
        self.assertIsNone(err, err)
        self.assertTrue(app.is_admin(dict(daniel_bik)))
        self.assertTrue(app.is_admin(dict(self.daniel)))
        danielush, err = app.create_user(self.conn, "דניאל לוי", "user1234", "0501234567")
        self.assertIsNone(err, err)
        self.assertFalse(app.is_admin(dict(danielush)))
        self.assertFalse(app.is_admin(dict(self.lev)))
        self.assertFalse(app.is_admin(None))
        # Exact match only: "דניאל ביק כהן" merely contains the
        # admin name — it is not admin.
        other, _ = app.create_user(self.conn, "דניאל ביק כהן", "user1234", "0501234567")
        self.assertFalse(app.is_admin(dict(other)))

    def test_danielush_sees_no_admin_section_on_my(self):
        danielush, _ = app.create_user(self.conn, "דניאל לוי", "user1234", "0501234567")
        h = app.Handler.__new__(app.Handler)
        html = h._my(self.conn, dict(danielush), None, None)
        self.assertNotIn("הכרעות מנהל", html)
        self.assertNotIn("דף בקרה", html)


class HebrewTest(Base):
    def test_page_html_is_hebrew_rtl_with_landmarks(self):
        html = app.page_html("כותרת", "<p>גוף</p>")
        self.assertIn('lang="he" dir="rtl"', html)
        self.assertIn("שומר מקומות", html)
        self.assertIn('<a class="skip" href="#main">', html)
        self.assertIn("<header>", html)
        self.assertIn('<main id="main">', html)
        # Exactly one <style> in <head> — the raw-CSS bug class stays dead.
        self.assertEqual(html.count("<style>"), 1)
        # The only <script> is the existing app.js include, never inline.
        self.assertEqual(html.count("<script"), 1)
        self.assertIn("/static/app.js", html)
        # Hebrew nav links, no English nav left — including the help page.
        for word in ("בית", "הצעות", "הצע מקום", "קורסים",
                     "המעקבים שלי", "המקומות שלי", "התראות", "איך זה עובד?"):
            self.assertIn(word, html)
        self.assertIn('href="/help"', html)
        self.assertNotIn(">Home<", html)
        self.assertNotIn(">Offers<", html)

    def test_hebrew_weekdays_and_status_labels(self):
        # Round 13: seat zones are gone — app.ZONES no longer exists.
        self.assertFalse(hasattr(app, "ZONES"))
        self.assertEqual(app.WEEKDAY_NAMES[6], "ראשון")
        self.assertEqual(app.WEEKDAY_NAMES[0], "שני")
        self.assertEqual(app.REQUEST_STATUS_HE["frozen"], "מוקפא")
        self.assertEqual(app.REQUEST_STATUS_HE["confirmed"], "אושר")
        self.assertEqual(app.REQUEST_STATUS_HE["no_show"], "לא הגיע")
        self.assertEqual(app.REQUEST_STATUS_HE["not_saved"], "המקום לא היה שם")
        self.assertEqual(app.REQUEST_STATUS_HE["cancelled"], "בוטל")
        self.assertEqual(app.OFFER_STATUS_HE["open"], "פתוחה")


class SaverNotifyTest(Base):
    def _subscribe(self, user_id, endpoint):
        app.save_subscription(self.conn, user_id, {
            "endpoint": endpoint,
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_saver_gets_push_when_seat_requested(self):
        self._subscribe(self.daniel["id"],
                        "https://example.com/push/daniel")
        offer = self._offer()
        req = self._request(offer)
        sent = []
        results = app.notify_saver_of_request(
            self.conn, req, sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(len(sent), 1)
        self.assertIn("מישהו ביקש מקום", sent[0]["title"])
        self.assertIn("Lev", sent[0]["body"])
        self.assertIn("תכנון אלגוריתמים", sent[0]["body"])
        self.assertEqual(results[0][1], "sent")
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=? AND user_id=?",
            (offer["id"], self.daniel["id"])).fetchone()
        self.assertEqual(log["status"], "sent")

    def test_saver_without_subscription_is_skipped_not_failed(self):
        offer = self._offer()
        req = self._request(offer)
        results = app.notify_saver_of_request(
            self.conn, req, sender=lambda sub, payload: None)
        self.assertEqual(results[0][1], "skipped-no-subscription")
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-no-subscription")

    def test_request_payload_names_requester_course_and_place(self):
        offer = self._offer()
        req = self._request(offer)
        d = dict(req)
        d["requester_name"] = "Lev"
        payload = app.prepare_request_payload(
            d, dict(self.course), dict(self.session))
        self.assertIn("Lev", payload["body"])
        self.assertIn("תכנון אלגוריתמים", payload["body"])
        self.assertIn("בניין 34 חדר 214", payload["body"])
        self.assertEqual(payload["url"], "/my")


class ReOfferTest(Base):
    def test_re_offer_copies_session_note_seats(self):
        offer = self._offer(seats=3)
        # Cancel the first offer so a re-offer is allowed (one open
        # offer per saver per session per day).
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        new, err = app.re_offer(self.conn, offer["id"], self.daniel["id"])
        self.assertIsNone(err, err)
        self.assertEqual(new["session_id"], offer["session_id"])
        # Round 13: zones are gone — a re-offer stores NULL zone.
        self.assertIsNone(new["zone"])
        self.assertEqual(new["note"], offer["note"])
        self.assertEqual(new["seats_total"], 3)
        self.assertNotEqual(new["id"], offer["id"])
        self.assertEqual(new["status"], "open")

    def test_re_offer_blocked_while_first_is_open(self):
        offer = self._offer()
        _, err = app.re_offer(self.conn, offer["id"], self.daniel["id"])
        self.assertIsNotNone(err)

    def test_re_offer_blocked_outside_window(self):
        # Old offer for a session starting in 2 hours, made when the
        # window was open (called with now = 60 min before start).
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, _mins_from_now(120),
             _mins_from_now(180))).fetchone()
        self.conn.commit()
        old, err = app.create_offer(self.conn, self.daniel["id"], s["id"], "", 1,
                                    now=datetime.now() + timedelta(minutes=60))
        self.assertIsNone(err, err)
        app.cancel_offer(self.conn, old["id"], self.daniel["id"])
        # Now it is 120 min before start again: window closed.
        _, err = app.re_offer(self.conn, old["id"], self.daniel["id"])
        self.assertIsNotNone(err)

    def test_re_offer_only_by_saver(self):
        offer = self._offer()
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        _, err = app.re_offer(self.conn, offer["id"], self.lev["id"])
        self.assertIsNotNone(err)

    def test_re_offer_for_session_not_today(self):
        # An old offer for a session on another weekday: the session
        # recurs weekly, but a re-offer only works when it is today.
        other = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], (self.today + 1) % 7, self.start,
             self.end)).fetchone()
        self.conn.commit()
        old = self.conn.execute(
            "INSERT INTO offers (course_id, session_id, saver_id,"
            " class_date, zone, note, seats_total, status, created_at)"
            " VALUES (?, ?, ?, ?, 'קדמי', '', 1, 'cancelled', ?)"
            " RETURNING *",
            (self.course["id"], other["id"], self.daniel["id"],
             datetime.now().strftime("%Y-%m-%d"),
             app._now_text())).fetchone()
        self.conn.commit()
        _, err = app.re_offer(self.conn, old["id"], self.daniel["id"])
        self.assertIsNotNone(err)

    def test_re_offer_button_only_on_closed_offers(self):
        h = app.Handler.__new__(app.Handler)
        open_offer = self._offer()
        html = h._my(self.conn, dict(self.daniel), None, None)
        # The open offer has no re-offer button; cancel it and it does.
        self.assertNotIn("הצע שוב", html)
        app.cancel_offer(self.conn, open_offer["id"], self.daniel["id"])
        html = h._my(self.conn, dict(self.daniel), None, None)
        self.assertIn("הצע שוב", html)


class OnMyWayTest(Base):
    def test_mark_on_my_way_sets_time(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.mark_on_my_way(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertTrue(row["on_the_way"])
        # Still frozen, balances unchanged — informational only.
        self.assertEqual(row["status"], "frozen")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)

    def test_on_my_way_double_tap_updates_time(self):
        offer = self._offer()
        req = self._request(offer)
        t1 = datetime.now() - timedelta(minutes=5)
        self.assertIsNone(app.mark_on_my_way(self.conn, req["id"],
                                             self.lev["id"], now=t1))
        t2 = datetime.now()
        self.assertIsNone(app.mark_on_my_way(self.conn, req["id"],
                                             self.lev["id"], now=t2))
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["on_the_way"], t2.isoformat(timespec="seconds"))

    def test_on_my_way_only_requester_and_only_frozen(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.mark_on_my_way(self.conn, req["id"], self.daniel["id"])
        self.assertIsNotNone(err)  # saver cannot mark it
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        err = app.mark_on_my_way(self.conn, req["id"], self.lev["id"])
        self.assertIsNotNone(err)  # resolved: too late
        err = app.mark_on_my_way(self.conn, 999999, self.lev["id"])
        self.assertIsNotNone(err)

    def test_on_my_way_shows_for_saver_and_requester(self):
        offer = self._offer()
        req = self._request(offer)
        app.mark_on_my_way(self.conn, req["id"], self.lev["id"],
                           now=datetime.now())
        h = app.Handler.__new__(app.Handler)
        # Saver's /my lists the requester as on the way.
        html = h._my(self.conn, dict(self.daniel), None, None)
        self.assertIn("בדרך", html)
        self.assertIn("Lev", html)
        # Requester's /my shows the button and the time.
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("אני בדרך", html)
        self.assertIn("בדרך מאז", html)

    def test_migration_adds_on_the_way_column(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "old.db")
        conn = app.connect(db)
        schema_old = app.SCHEMA.replace(
            "    resolved_at TEXT,\n    on_the_way TEXT",
            "    resolved_at TEXT")
        conn.executescript(schema_old)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        self.assertNotIn("on_the_way", cols)
        app.init_db(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        self.assertIn("on_the_way", cols)
        conn.close()
        tmp.cleanup()


class FollowerCountTest(Base):
    def test_counts_general_and_specific_with_dedupe(self):
        # Nobody follows the extra session yet, except Daniel, who
        # follows all courses generally (seeded) — so count is 1.
        self.assertEqual(
            app.followers_count_for_session(self.conn, dict(self.session)), 1)
        # Lev follows generally AND specifically: still one user.
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"],
                       session_id=self.session["id"])
        self.assertEqual(
            app.followers_count_for_session(self.conn, dict(self.session)), 2)
        # A specific follow for another day does not count here.
        other = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], (self.today + 1) % 7, self.start,
             self.end)).fetchone()
        self.conn.commit()
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"],
                       session_id=other["id"])
        self.assertEqual(
            app.followers_count_for_session(self.conn, dict(self.session)), 2)
        # General follows match any session of the course: Daniel and
        # Lev follow generally, Levi specifically — three in total.
        self.assertEqual(
            app.followers_count_for_session(self.conn, dict(other)), 3)

    def test_follower_labels(self):
        self.assertEqual(app.followers_label(0), "אין עוקבים")
        self.assertEqual(app.followers_label(1), "עוקב אחד")
        self.assertEqual(app.followers_label(3), "3 עוקבים")

    def test_counts_shown_on_home_and_courses(self):
        h = app.Handler.__new__(app.Handler)
        html = h._home(self.conn, dict(self.daniel), None, None)
        self.assertIn("עוקב אחד", html)
        html = h._courses(self.conn, dict(self.daniel), None, None)
        self.assertIn("עוקב אחד", html)


class LedgerTest(Base):
    def test_request_and_confirm_are_logged(self):
        offer = self._offer()
        req = self._request(offer)
        rows = self.conn.execute(
            "SELECT * FROM coin_ledger WHERE user_id=?",
            (self.lev["id"],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], -1)
        self.assertEqual(rows[0]["reason"], "frozen")
        self.assertEqual(rows[0]["request_id"], req["id"])
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        saver_rows = self.conn.execute(
            "SELECT * FROM coin_ledger WHERE user_id=?",
            (self.daniel["id"],)).fetchall()
        self.assertEqual(len(saver_rows), 1)
        self.assertEqual(saver_rows[0]["amount"], 1)
        self.assertEqual(saver_rows[0]["reason"], "paid")

    def test_refunds_are_logged(self):
        offer = self._offer()
        req = self._request(offer)
        app.report_not_saved(self.conn, req["id"], self.lev["id"])
        reasons = [r["reason"] for r in self.conn.execute(
            "SELECT reason FROM coin_ledger WHERE user_id=? ORDER BY id",
            (self.lev["id"],)).fetchall()]
        self.assertEqual(reasons, ["frozen", "not_saved"])
        # No-show refund, on a second offer.
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, self.start,
             self.end)).fetchone()
        self.conn.commit()
        offer2, err = app.create_offer(self.conn, self.daniel["id"],
                                       s["id"], "", 1)
        self.assertIsNone(err, err)
        self._request(offer2)
        start = datetime.now() + timedelta(minutes=10)
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        reasons = [r["reason"] for r in self.conn.execute(
            "SELECT reason FROM coin_ledger WHERE user_id=? ORDER BY id",
            (self.lev["id"],)).fetchall()]
        self.assertEqual(reasons,
                         ["frozen", "not_saved", "frozen", "no_show"])

    def test_ledger_shown_on_my(self):
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        h = app.Handler.__new__(app.Handler)
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("היסטוריית מטבעות", html)
        self.assertIn("בקשת מקום", html)
        self.assertIn("-1 מטבע", html)
        html = h._my(self.conn, dict(self.daniel), None, None)
        self.assertIn("מטבע התקבל", html)
        self.assertIn("+1 מטבע", html)

    def test_ledger_table_created_on_old_db(self):
        # A database from before the ledger existed: no table at all.
        # init_db must create it, empty — old balances have no rows.
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "old.db")
        conn = app.connect(db)
        schema_old = app.SCHEMA.split(
            "CREATE TABLE IF NOT EXISTS coin_ledger")[0]
        conn.executescript(schema_old)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertNotIn("coin_ledger", tables)
        app.init_db(conn)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("coin_ledger", tables)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM coin_ledger").fetchone()[0], 0)
        conn.close()
        tmp.cleanup()


class DeadSubscriptionTest(Base):
    class _Gone(Exception):
        def __init__(self, status):
            super().__init__("push service answered %d" % status)
            self.response = type("Resp", (),
                                 {"status_code": status})()

    def _subscribe(self, user_id):
        app.save_subscription(self.conn, user_id, {
            "endpoint": "https://example.com/push/%s" % user_id,
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_gone_detection(self):
        self.assertTrue(app._subscription_is_gone(self._Gone(410)))
        self.assertTrue(app._subscription_is_gone(self._Gone(404)))
        self.assertFalse(app._subscription_is_gone(self._Gone(500)))
        self.assertFalse(app._subscription_is_gone(self._Gone(400)))
        self.assertFalse(app._subscription_is_gone(Exception("boom")))

    def test_410_deletes_the_subscription(self):
        self._subscribe(self.lev["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()

        def gone_sender(sub, payload):
            raise self._Gone(410)

        results = app.notify_followers(self.conn, offer, sender=gone_sender)
        self.assertEqual(results[0][1], "removed-dead-subscription")
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone())
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "removed-dead-subscription")

    def test_transient_failure_keeps_the_subscription(self):
        self._subscribe(self.lev["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()

        def flaky_sender(sub, payload):
            raise self._Gone(500)

        results = app.notify_followers(self.conn, offer, sender=flaky_sender)
        self.assertEqual(results[0][1], "failed")
        self.assertIsNotNone(self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone())
        # A plain network error also keeps it.
        def down_sender(sub, payload):
            raise ConnectionError("network down")

        results = app.notify_followers(self.conn, offer, sender=down_sender)
        self.assertEqual(results[0][1], "failed")
        self.assertIsNotNone(self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone())


class OfferSortTest(Base):
    def _session_in(self, minutes_from_now, duration=60):
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today,
             _mins_from_now(minutes_from_now),
             _mins_from_now(minutes_from_now + duration))).fetchone()
        self.conn.commit()
        return s

    def test_offers_sorted_by_class_start_not_creation(self):
        # Later class created FIRST, earlier class created second.
        late = self._session_in(50)
        early = self._session_in(10)
        offer_late, err = app.create_offer(self.conn, self.daniel["id"],
                                           late["id"], "מאוחר", 1)
        self.assertIsNone(err, err)
        offer_early, err = app.create_offer(self.conn, self.daniel["id"],
                                            early["id"], "מוקדם", 1)
        self.assertIsNone(err, err)
        ordered = app.sort_offers_by_start(
            self.conn, [dict(offer_late), dict(offer_early)])
        self.assertEqual([o["id"] for o in ordered],
                         [offer_early["id"], offer_late["id"]])
        # And the pages show them in that order too.
        h = app.Handler.__new__(app.Handler)
        html = h._home(self.conn, dict(self.daniel), None, None)
        self.assertLess(html.index("מוקדם"), html.index("מאוחר"))
        html = h._offers(self.conn, dict(self.daniel), None, None)
        self.assertLess(html.index("מוקדם"), html.index("מאוחר"))

    def test_offer_without_session_sorts_last(self):
        offer = self._offer()
        orphan = self.conn.execute(
            "INSERT INTO offers (course_id, session_id, saver_id,"
            " class_date, zone, note, seats_total, status, created_at)"
            " VALUES (?, NULL, ?, ?, 'קדמי', '', 1, 'open', ?)"
            " RETURNING *",
            (self.course["id"], self.daniel["id"],
             datetime.now().strftime("%Y-%m-%d"),
             app._now_text())).fetchone()
        self.conn.commit()
        ordered = app.sort_offers_by_start(
            self.conn, [dict(orphan), dict(offer)])
        self.assertEqual(ordered[-1]["id"], orphan["id"])
        self.assertEqual(ordered[0]["id"], offer["id"])


class CancelRequestTest(Base):
    def test_cancel_refunds_and_frees_seat(self):
        offer = self._offer()
        req = self._request(offer)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)
        err = app.cancel_request(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled_by_requester")
        self.assertEqual(app.seats_available(self.conn, offer), 1)
        # Ledger says why the coin came back.
        entry = self.conn.execute(
            "SELECT * FROM coin_ledger WHERE user_id=? AND amount=1",
            (self.lev["id"],)).fetchone()
        self.assertEqual(entry["reason"], "cancelled_by_requester")

    def test_cancel_refunds_two_seats(self):
        offer = self._offer(seats=2)
        req, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                    seats=2)
        self.assertIsNone(err, err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 1)
        err = app.cancel_request(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_only_requester_can_cancel(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.cancel_request(self.conn, req["id"], self.daniel["id"])
        self.assertIsNotNone(err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)

    def test_cancel_only_while_frozen(self):
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        err = app.cancel_request(self.conn, req["id"], self.lev["id"])
        self.assertIsNotNone(err)
        # The coin stays with the saver — a confirmed arrival is done.
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 4)


class SeatFreedTest(Base):
    def _follower_with_sub(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        app.save_subscription(self.conn, levi["id"], {
            "endpoint": "https://example.com/push/levi",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        return levi

    def test_freed_notification_only_when_fully_booked_then_free(self):
        self._follower_with_sub()
        offer = self._offer()  # 1 seat
        req = self._request(offer)  # fully booked now
        sent = []
        err = app.cancel_request(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        self.assertIn("התפנה מקום", sent[0]["title"])
        self.assertIn("תכנון אלגוריתמים", sent[0]["title"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=? AND"
            " user_id=?", (offer["id"],
                           self.conn.execute(
                               "SELECT * FROM users WHERE name='Levi'"
                               ).fetchone()["id"])).fetchone()
        self.assertEqual(log["status"], "sent")

    def test_no_freed_notification_when_seats_were_available(self):
        self._follower_with_sub()
        offer = self._offer(seats=2)
        req = self._request(offer)  # 1 of 2 taken — a seat was free anyway
        sent = []
        err = app.cancel_request(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_no_freed_notification_when_saver_cancels_offer(self):
        self._follower_with_sub()
        offer = self._offer()
        self._request(offer)  # fully booked
        sent = []
        err = app.cancel_offer(
            self.conn, offer["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        # The offer is closed — there is nothing to offer anymore.
        self.assertEqual(sent, [])

    def test_no_freed_notification_on_not_saved_when_seats_free(self):
        self._follower_with_sub()
        offer = self._offer(seats=2)
        req = self._request(offer)
        sent = []
        err = app.report_not_saved(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_no_freed_notification_after_no_show_expiry(self):
        # A no-show refund happens after the offer already expired at
        # class start, so the offer is closed — no free-up notice.
        self._follower_with_sub()
        offer = self._offer()
        self._request(offer)
        start = datetime.now() + timedelta(minutes=10)
        sent = []
        app.settle_expired(self.conn, now=start + timedelta(minutes=6),
                           sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(sent, [])

    def test_canceller_is_not_notified_about_own_seat(self):
        # The requester who cancelled is also a follower — but the
        # freed seat is the one they just gave up, so they are skipped.
        # Another follower still gets the notice.
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        for u in (self.lev, levi):
            app.add_follow(self.conn, u["id"], self.course["id"])
            app.save_subscription(self.conn, u["id"], {
                "endpoint": "https://example.com/push/%s" % u["id"],
                "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        req = self._request(offer)  # Lev takes the only seat
        sent = []
        err = app.cancel_request(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(sub["endpoint"]))
        self.assertIsNone(err)
        self.assertEqual(sent, ["https://example.com/push/%s" % levi["id"]])

    def test_reporter_is_not_notified_about_own_seat(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        for u in (self.lev, levi):
            app.add_follow(self.conn, u["id"], self.course["id"])
            app.save_subscription(self.conn, u["id"], {
                "endpoint": "https://example.com/push/%s" % u["id"],
                "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        req = self._request(offer)
        sent = []
        err = app.report_not_saved(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(sub["endpoint"]))
        self.assertIsNone(err)
        self.assertEqual(sent, ["https://example.com/push/%s" % levi["id"]])


class TwoSeatRequestTest(Base):
    def test_two_seats_freeze_two_and_pay_two(self):
        offer = self._offer(seats=2)
        req, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                    seats=2)
        self.assertIsNone(err, err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 1)
        self.assertEqual(app.seats_available(self.conn, offer), 0)
        err = app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 5)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 1)

    def test_cannot_request_more_seats_than_available(self):
        offer = self._offer()  # 1 seat
        _, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                  seats=2)
        self.assertIsNotNone(err)

    def test_cannot_request_zero_or_three_seats(self):
        offer = self._offer(seats=3)
        for bad in (0, 3, "x"):
            _, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                      seats=bad)
            self.assertIsNotNone(err)

    def test_floor_math_uses_multiplied_amount(self):
        # Lev at -1: one seat is fine (lands on -2), two seats would
        # land on -3 — below the floor, so the 2-seat request is refused.
        self.conn.execute("UPDATE users SET balance=-1 WHERE id=?",
                          (self.lev["id"],))
        self.conn.commit()
        offer = self._offer(seats=2)
        _, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                  seats=2)
        self.assertIsNotNone(err)
        self.assertIn("רצפה", err)
        req, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                    seats=1)
        self.assertIsNone(err, err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], -2)

    def test_ledger_amounts_multiply(self):
        offer = self._offer(seats=2)
        req, err = app.request_seat(self.conn, self.lev["id"], offer["id"],
                                    seats=2)
        self.assertIsNone(err, err)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        entry = self.conn.execute(
            "SELECT * FROM coin_ledger WHERE user_id=? AND reason='paid'",
            (self.daniel["id"],)).fetchone()
        self.assertEqual(entry["amount"], 2)


class ReminderTest(Base):
    def test_find_reminders_due_only_within_five_minutes(self):
        offer = self._offer()
        req = self._request(offer)
        now = datetime.now()
        # Class in 10 min — too early for a reminder.
        self.assertEqual(app.find_reminders_due(self.conn, now), [])
        # Class in 4 min — due.
        due = app.find_reminders_due(self.conn,
                                     now + timedelta(minutes=6))
        self.assertEqual([r["id"] for r in due], [req["id"]])
        # Class already started — too late, not due.
        self.assertEqual(
            app.find_reminders_due(self.conn,
                                   now + timedelta(minutes=11)), [])

    def test_reminder_tick_sends_once_and_marks_reminded(self):
        offer = self._offer()
        self._request(offer)
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        sent = []
        now = datetime.now() + timedelta(minutes=6)
        count = app.reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=now)
        self.assertEqual(count, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("תזכורת", sent[0]["title"])
        self.assertIn("5 דקות", sent[0]["title"])
        row = self.conn.execute(
            "SELECT reminded_at FROM requests WHERE requester_id=?",
            (self.lev["id"],)).fetchone()
        self.assertIsNotNone(row["reminded_at"])
        # Second tick: no double reminder, even for the same minute.
        count = app.reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=now)
        self.assertEqual(count, 0)
        self.assertEqual(len(sent), 1)

    def test_reminder_not_sent_for_confirmed_request(self):
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        now = datetime.now() + timedelta(minutes=6)
        self.assertEqual(app.find_reminders_due(self.conn, now), [])

    def test_reminder_tick_marks_even_without_subscription(self):
        # No subscription: the attempt is logged as skipped and the
        # flag is still set, so the thread does not retry every minute.
        offer = self._offer()
        self._request(offer)
        now = datetime.now() + timedelta(minutes=6)
        count = app.reminder_tick(
            self.conn, sender=lambda sub, payload: None, now=now)
        self.assertEqual(count, 1)
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-no-subscription")


class AdminDisputeTest(Base):
    def _stuck(self):
        offer = self._offer()
        req = self._request(offer)
        # Move the session to the past, so the request is stuck.
        self.conn.execute(
            "UPDATE course_sessions SET start_time=?, end_time=? WHERE id=?",
            (_mins_from_now(-30), _mins_from_now(30), self.session["id"]))
        self.conn.commit()
        return offer, req

    def test_admin_stuck_lists_frozen_past_start(self):
        _, req = self._stuck()
        stuck = app.admin_stuck_requests(self.conn)
        self.assertEqual([r["id"] for r in stuck], [req["id"]])

    def test_admin_force_confirm_pays_saver(self):
        _, req = self._stuck()
        err = app.admin_force_confirm(self.conn, req["id"], dict(self.daniel))
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 4)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "confirmed")

    def test_admin_force_refund_returns_coin(self):
        _, req = self._stuck()
        err = app.admin_force_refund(self.conn, req["id"], dict(self.daniel))
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "refunded_by_admin")

    def test_admin_force_refund_takes_back_from_saver_if_paid(self):
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 4)
        err = app.admin_force_refund(self.conn, req["id"], dict(self.daniel))
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 3)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "refunded_by_admin")

    def test_admin_force_confirm_after_refund_pays_again(self):
        # Refunded by mistake? Admin can still confirm: the coin is
        # taken back from the requester first, then paid to the saver.
        offer = self._offer()
        req = self._request(offer)
        app.report_not_saved(self.conn, req["id"], self.lev["id"])
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        err = app.admin_force_confirm(self.conn, req["id"], dict(self.daniel))
        self.assertIsNone(err)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 4)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)

    def test_non_daniel_cannot_decide(self):
        _, req = self._stuck()
        for fn in (app.admin_force_confirm, app.admin_force_refund):
            err = fn(self.conn, req["id"], dict(self.lev))
            self.assertIsNotNone(err)
            # Neutral wording: the error says "no permission", never
            # who the admin is.
            self.assertEqual(err, "אין לך הרשאה לפעולה הזו.")
        # Nothing moved.
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)

    def test_admin_stuck_includes_recently_resolved(self):
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        stuck = app.admin_stuck_requests(self.conn)
        self.assertEqual([r["id"] for r in stuck], [req["id"]])
        # A request resolved more than 7 days ago drops off the list.
        old = datetime.now() - timedelta(days=8)
        self.conn.execute(
            "UPDATE requests SET resolved_at=? WHERE id=?",
            (old.strftime("%Y-%m-%d %H:%M:%S"), req["id"]))
        self.conn.commit()
        self.assertEqual(app.admin_stuck_requests(self.conn), [])

    def test_admin_cannot_confirm_twice(self):
        _, req = self._stuck()
        app.admin_force_confirm(self.conn, req["id"], dict(self.daniel))
        err = app.admin_force_confirm(self.conn, req["id"], dict(self.daniel))
        self.assertIsNotNone(err)
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"], 4)


class Round4MigrationTest(Base):
    def test_new_columns_and_table_exist_after_migration(self):
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(requests)").fetchall()}
        self.assertIn("seats_requested", cols)
        self.assertIn("reminded_at", cols)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("auto_offers", tables)

    def test_migration_adds_columns_to_old_db(self):
        # Simulate an old DB: drop the new columns/table, re-run init.
        self.conn.execute("ALTER TABLE requests DROP COLUMN seats_requested")
        self.conn.execute("ALTER TABLE requests DROP COLUMN reminded_at")
        self.conn.execute("DROP TABLE auto_offers")
        self.conn.commit()
        app.init_db(self.conn)
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(requests)").fetchall()}
        self.assertIn("seats_requested", cols)
        self.assertIn("reminded_at", cols)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("auto_offers", tables)
        # And the app still works on the migrated DB.
        offer = self._offer()
        req = self._request(offer)
        self.assertEqual(app._request_seats(dict(req)), 1)

    def test_request_seats_defaults_to_one_without_column(self):
        self.assertEqual(app._request_seats({}), 1)
        self.assertEqual(app._request_seats({"seats_requested": None}), 1)
        self.assertEqual(app._request_seats({"seats_requested": 2}), 2)


class ConcurrencyTest(Base):
    """Two threads at once, one DB file, one connection each — exactly
    what the threaded server does. Without the write lock these all
    failed (double-confirm overpaid 20/20, last-seat race overbooked
    18/20, duplicate offers 10/20)."""

    def _run_two(self, fn):
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(i):
            conn = app.connect(self.db)
            barrier.wait(timeout=5)
            results[i] = fn(conn)
            conn.close()

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return results

    def _reset(self):
        # Ledger and notification log reference requests/offers, so
        # they go first — otherwise the foreign keys complain.
        self.conn.execute("DELETE FROM coin_ledger")
        self.conn.execute("DELETE FROM notification_log")
        self.conn.execute("DELETE FROM requests")
        self.conn.execute("DELETE FROM offers")
        self.conn.execute("UPDATE users SET balance=3")
        self.conn.commit()

    def test_double_confirm_pays_once(self):
        for _ in range(10):
            offer = self._offer()
            req = self._request(offer)
            self._run_two(
                lambda c: app.confirm_arrival(c, req["id"], self.lev["id"]))
            self.assertEqual(
                app.get_user(self.conn, self.daniel["id"])["balance"], 4)
            self._reset()

    def test_last_seat_race_one_winner(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        for _ in range(10):
            offer = self._offer()
            barrier = threading.Barrier(2)
            outs = [None, None]

            def w(i, uid):
                c = app.connect(self.db)
                barrier.wait(timeout=5)
                outs[i] = app.request_seat(c, uid, offer["id"])
                c.close()

            threads = [threading.Thread(target=w, args=(0, self.lev["id"])),
                       threading.Thread(target=w, args=(1, levi["id"]))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            self.assertEqual(sum(1 for _, e in outs if e is None), 1)
            self._reset()

    def test_double_offer_one_open(self):
        for _ in range(10):
            results = self._run_two(
                lambda c: app.create_offer(c, self.daniel["id"],
                                           self.session["id"], "", 1))
            self.assertEqual(sum(1 for _, e in results if e is None), 1)
            self._reset()

    def test_double_cancel_refunds_once(self):
        for _ in range(10):
            offer = self._offer()
            req = self._request(offer)
            self._run_two(
                lambda c: app.cancel_request(c, req["id"], self.lev["id"]))
            self.assertEqual(
                app.get_user(self.conn, self.lev["id"])["balance"], 3)
            self._reset()


class CancelNotifyTest(Base):
    def _saver_with_sub(self):
        app.save_subscription(self.conn, self.daniel["id"], {
            "endpoint": "https://example.com/push/daniel",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_saver_notified_when_requester_cancels(self):
        self._saver_with_sub()
        offer = self._offer()
        req = self._request(offer)
        sent = []
        err = app.cancel_request(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append((sub, payload)))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        sub, payload = sent[0]
        self.assertEqual(sub["endpoint"], "https://example.com/push/daniel")
        self.assertEqual(payload["title"], "הבקשה בוטלה")
        self.assertIn("Lev", payload["body"])
        self.assertIn("תכנון אלגוריתמים", payload["body"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=? AND user_id=?",
            (offer["id"], self.daniel["id"])).fetchone()
        self.assertEqual(log["status"], "sent")

    def test_no_cancel_notice_on_admin_refund(self):
        # An admin force-refund is a different path — the saver is not
        # told "the request was cancelled" about their own dispute.
        self._saver_with_sub()
        offer = self._offer()
        req = self._request(offer)
        sent = []
        err = app.admin_force_refund(
            self.conn, req["id"], self.daniel,
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_no_cancel_notice_when_saver_cancels_offer(self):
        self._saver_with_sub()
        offer = self._offer()
        self._request(offer)
        sent = []
        err = app.cancel_offer(
            self.conn, offer["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_cancel_still_notifies_saver_when_offer_had_free_seats(self):
        # The saver notice is not the free-up notice: it goes out
        # even when the offer was never full (no followers told).
        self._saver_with_sub()
        offer = self._offer(seats=2)
        req = self._request(offer)
        sent = []
        err = app.cancel_request(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "הבקשה בוטלה")


class WaitingListTest(Base):
    def _users(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        return levi, shaked

    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _full_offer(self):
        offer = self._offer()  # 1 seat
        self._request(offer)   # Lev takes it — fully booked
        return offer

    def test_join_only_when_full(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._offer(seats=2)
        self._request(offer)  # 1 of 2 taken — a seat is still free
        _, err = app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNotNone(err)
        self.assertIn("פנויים", err)

    def test_join_requires_follow(self):
        levi, _ = self._users()
        offer = self._full_offer()
        _, err = app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNotNone(err)
        app.add_follow(self.conn, levi["id"], self.course["id"])
        row, err = app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNone(err, err)
        self.assertIsNotNone(row)

    def test_specific_follow_for_other_session_cannot_join(self):
        # A specific follow for a different session of the same course
        # does not match this offer — so it cannot queue for it.
        levi, _ = self._users()
        other = self.conn.execute(
            "SELECT * FROM course_sessions WHERE course_id=? AND id!=?"
            " LIMIT 1", (self.course["id"], self.session["id"])).fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"],
                       session_id=other["id"])
        offer = self._full_offer()
        _, err = app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNotNone(err)

    def test_join_twice_rejected(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._full_offer()
        _, err = app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNone(err, err)
        _, err = app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNotNone(err)

    def test_saver_cannot_join_own_offer(self):
        offer = self._full_offer()
        _, err = app.join_waiting_list(
            self.conn, self.daniel["id"], offer["id"])
        self.assertIsNotNone(err)

    def test_requester_with_seat_cannot_join(self):
        # Lev already holds the only seat — nothing to queue for.
        offer = self._full_offer()
        _, err = app.join_waiting_list(self.conn, self.lev["id"], offer["id"])
        self.assertIsNotNone(err)

    def test_join_closed_offer_rejected(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._full_offer()
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        _, err = app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNotNone(err)

    def test_leave_waiting_list(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._full_offer()
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertTrue(app.is_on_waiting_list(
            self.conn, levi["id"], offer["id"]))
        err = app.leave_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNone(err)
        self.assertFalse(app.is_on_waiting_list(
            self.conn, levi["id"], offer["id"]))
        # Leaving twice is an error, not a silent no-op.
        err = app.leave_waiting_list(self.conn, levi["id"], offer["id"])
        self.assertIsNotNone(err)

    def test_successful_request_removes_from_list(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._full_offer()
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        req = self.conn.execute(
            "SELECT * FROM requests WHERE offer_id=?",
            (offer["id"],)).fetchone()
        app.cancel_request(self.conn, req["id"], self.lev["id"])
        # The seat is free now — Levi requests it and leaves the list.
        req2, err = app.request_seat(self.conn, levi["id"], offer["id"])
        self.assertIsNone(err, err)
        self.assertFalse(app.is_on_waiting_list(
            self.conn, levi["id"], offer["id"]))

    def test_free_up_notifies_waiting_first_and_only_once(self):
        levi, shaked = self._users()
        for u in (levi, shaked):
            app.add_follow(self.conn, u["id"], self.course["id"])
            self._sub(u)
        offer = self._full_offer()
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        req = self.conn.execute(
            "SELECT * FROM requests WHERE offer_id=?",
            (offer["id"],)).fetchone()
        sent = []
        err = app.cancel_request(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append((sub, payload)))
        self.assertIsNone(err)
        # Two pushes, one per person: waiting user first with their
        # own message, then the general follower — never two for one.
        self.assertEqual(len(sent), 2)
        (sub1, pay1), (sub2, pay2) = sent
        self.assertEqual(sub1["endpoint"],
                         "https://example.com/push/%s" % levi["id"])
        self.assertIn("רשימת ההמתנה", pay1["title"])
        self.assertEqual(sub2["endpoint"],
                         "https://example.com/push/%s" % shaked["id"])
        self.assertNotIn("רשימת ההמתנה", pay2["title"])
        self.assertIn("התפנה מקום", pay2["title"])

    def test_waiting_list_cleaned_when_offer_cancelled(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._full_offer()
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        self.assertEqual(app.waiting_count_for_offer(self.conn, offer["id"]),
                         0)

    def test_waiting_list_cleaned_when_offer_expires(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._full_offer()
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        start = datetime.now() + timedelta(minutes=10)
        app.settle_expired(self.conn, now=start + timedelta(minutes=1))
        self.assertEqual(app.waiting_count_for_offer(self.conn, offer["id"]),
                         0)

    def test_waiting_join_button_only_on_full_offer_card(self):
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        h = app.Handler.__new__(app.Handler)
        offer = self._offer(seats=2)
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        html = h._offer_card(self.conn, row, dict(levi))
        self.assertNotIn("רשימת המתנה", html)
        self.assertIn("בקש מקום", html)
        # Now it is full: the join button appears instead.
        self._request(offer)
        self._request(offer, requester=self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone())
        html = h._offer_card(self.conn, row, dict(levi))
        self.assertIn("הצטרף לרשימת המתנה", html)
        self.assertNotIn("בקש מקום", html)
        # After joining, the card says so — with the queue position —
        # and offers leaving.
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        html = h._offer_card(self.conn, row, dict(levi))
        self.assertIn("אתה ראשון בתור", html)
        self.assertIn("צא מרשימת ההמתנה", html)

    def test_double_join_race_one_entry(self):
        # Two threads joining at once: the UNIQUE constraint plus
        # the lock allow exactly one row.
        levi, _ = self._users()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        offer = self._full_offer()
        barrier = threading.Barrier(2)
        outs = [None, None]

        def w(i):
            c = app.connect(self.db)
            barrier.wait(timeout=5)
            outs[i] = app.join_waiting_list(c, levi["id"], offer["id"])
            c.close()

        threads = [threading.Thread(target=w, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(sum(1 for _, e in outs if e is None), 1)
        self.assertEqual(app.waiting_count_for_offer(self.conn, offer["id"]),
                         1)


class SaverReminderTest(Base):
    def _saver_with_sub(self):
        app.save_subscription(self.conn, self.daniel["id"], {
            "endpoint": "https://example.com/push/daniel",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_find_due_only_within_ten_minutes(self):
        offer = self._offer()
        self._request(offer)
        start = datetime.now() + timedelta(minutes=10)
        # 11 minutes before class — too early.
        self.assertEqual(
            app.find_saver_reminders_due(
                self.conn, now=start - timedelta(minutes=11)), [])
        # 9 minutes before — due.
        due = app.find_saver_reminders_due(
            self.conn, now=start - timedelta(minutes=9))
        self.assertEqual([o["id"] for o in due], [offer["id"]])
        # Class already started — too late.
        self.assertEqual(
            app.find_saver_reminders_due(
                self.conn, now=start + timedelta(minutes=1)), [])

    def test_no_reminder_without_frozen_requests(self):
        offer = self._offer()  # nobody requested
        start = datetime.now() + timedelta(minutes=10)
        self.assertEqual(
            app.find_saver_reminders_due(
                self.conn, now=start - timedelta(minutes=9)), [])

    def test_saver_reminder_tick_sends_once_and_marks(self):
        self._saver_with_sub()
        offer = self._offer()
        self._request(offer)
        start = datetime.now() + timedelta(minutes=10)
        now = start - timedelta(minutes=9)
        sent = []
        count = app.saver_reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=now)
        self.assertEqual(count, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("אתה שומר מקום", sent[0]["title"])
        self.assertIn("10 דקות", sent[0]["body"])
        self.assertIn("Lev", sent[0]["body"])  # who the seat is for
        row = self.conn.execute(
            "SELECT saver_reminded_at FROM offers WHERE id=?",
            (offer["id"],)).fetchone()
        self.assertIsNotNone(row["saver_reminded_at"])
        # Second tick: no double reminder, even for the same minute.
        count = app.saver_reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=now)
        self.assertEqual(count, 0)
        self.assertEqual(len(sent), 1)

    def test_saver_reminder_lists_all_requesters(self):
        self._saver_with_sub()
        offer = self._offer(seats=3)
        self._request(offer)
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        self._request(offer, requester=levi)
        start = datetime.now() + timedelta(minutes=10)
        sent = []
        app.saver_reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=start - timedelta(minutes=9))
        self.assertEqual(len(sent), 1)
        self.assertIn("Lev", sent[0]["body"])
        self.assertIn("Levi", sent[0]["body"])

    def test_saver_reminder_tick_marks_even_without_subscription(self):
        # No subscription: the attempt is logged as skipped and the
        # flag is still set, so the thread does not retry every minute.
        offer = self._offer()
        self._request(offer)
        start = datetime.now() + timedelta(minutes=10)
        count = app.saver_reminder_tick(
            self.conn, now=start - timedelta(minutes=9))
        self.assertEqual(count, 1)
        count = app.saver_reminder_tick(
            self.conn, now=start - timedelta(minutes=9))
        self.assertEqual(count, 0)


class CountdownTest(Base):
    def _start(self, offer=None):
        # The exact class start: today's date + the session's HH:MM.
        # (datetime.now() + 10 minutes is NOT exact — the session
        # start is a minute string, so up to 59 seconds earlier.)
        if offer is None:
            offer = self._offer()
        session = self.session
        day = datetime.strptime(offer["class_date"], "%Y-%m-%d").date()
        h, m = session["start_time"].split(":")
        return datetime(day.year, day.month, day.day, int(h), int(m)), offer

    def test_countdown_minutes(self):
        start, offer = self._start()
        self.assertEqual(
            app._countdown_text(dict(offer), dict(self.session),
                                now=start - timedelta(minutes=25)),
            "השיעור מתחיל בעוד 25 דקות")
        # 25 minutes 50 seconds floors to 25, never rounds up to 26.
        self.assertEqual(
            app._countdown_text(dict(offer), dict(self.session),
                                now=start - timedelta(minutes=25,
                                                      seconds=50)),
            "השיעור מתחיל בעוד 25 דקות")

    def test_countdown_under_a_minute(self):
        start, offer = self._start()
        self.assertEqual(
            app._countdown_text(dict(offer), dict(self.session),
                                now=start - timedelta(seconds=30)),
            "השיעור מתחיל בעוד פחות מדקה")

    def test_countdown_after_start(self):
        start, offer = self._start()
        self.assertEqual(
            app._countdown_text(dict(offer), dict(self.session),
                                now=start + timedelta(minutes=1)),
            "השיעור התחיל")

    def test_countdown_empty_for_closed_offer_or_no_session(self):
        offer = self._offer()
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        closed = self.conn.execute("SELECT * FROM offers WHERE id=?",
                                   (offer["id"],)).fetchone()
        self.assertEqual(
            app._countdown_text(dict(closed), dict(self.session)), "")
        offer2 = self._offer()
        self.assertEqual(
            app._countdown_text(dict(offer2), None), "")

    def test_countdown_shown_on_offer_card(self):
        h = app.Handler.__new__(app.Handler)
        offer = self._offer()
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        html = h._offer_card(self.conn, row, dict(self.lev))
        # Class starts in ~10 minutes from setUp.
        self.assertIn("השיעור מתחיל בעוד", html)
        self.assertIn("דקות", html)
        # No JavaScript timer was added for this — static server text.
        self.assertNotIn("<script", html)


class ReliabilityTest(Base):
    def _extra_session(self, minutes_from_now):
        start = _mins_from_now(minutes_from_now)
        end = _mins_from_now(minutes_from_now + 60)
        return self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, start, end)).fetchone()

    def _offer_for(self, session, saver=None, seats=1):
        offer, err = app.create_offer(
            self.conn, (saver or self.daniel)["id"], session["id"], "", seats)
        self.assertIsNone(err, err)
        return offer

    def _decide(self, req, how, requester_id=None):
        requester_id = requester_id or self.lev["id"]
        if how == "confirmed":
            return app.confirm_arrival(self.conn, req["id"], requester_id)
        if how == "not_saved":
            return app.report_not_saved(self.conn, req["id"], requester_id)
        if how == "cancelled":
            return app.cancel_request(self.conn, req["id"], requester_id)
        if how == "admin_refund":
            return app.admin_force_refund(self.conn, req["id"], self.daniel)
        raise ValueError(how)

    def test_no_decided_requests_shows_nothing(self):
        self.assertIsNone(app.reliability(
            self.conn, self.daniel["id"], "saver"))
        self.assertEqual(app.reliability_label(
            self.conn, self.daniel["id"], "saver"), "")

    def test_saver_reliability_counts_only_decided(self):
        # Daniel saves: 1 confirmed, 1 not-saved, 1 cancelled (excluded).
        offer = self._offer_for(self.session, seats=3)
        r1 = self._request(offer)
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        r2 = self._request(offer, requester=levi)
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        r3 = self._request(offer, requester=shaked)
        self.assertIsNone(self._decide(r1, "confirmed"))
        # report_not_saved takes the requester id from the request.
        self.assertIsNone(self._decide(r2, "not_saved", levi["id"]))
        self.assertIsNone(self._decide(r3, "cancelled", shaked["id"]))
        self.assertEqual(app.reliability(
            self.conn, self.daniel["id"], "saver"), (1, 2))
        # Round 20, item 8: the label is now a percentage with the
        # count next to it — the number a requester decides on.
        self.assertEqual(app.reliability_label(
            self.conn, self.daniel["id"], "saver"), "אמינות: 50% (1/2)")

    def test_no_show_counts_as_not_kept(self):
        offer = self._offer()
        req = self._request(offer)
        start = datetime.now() + timedelta(minutes=10)
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "no_show")
        self.assertEqual(app.reliability(
            self.conn, self.daniel["id"], "saver"), (0, 1))

    def test_admin_refund_excluded_from_reliability(self):
        # A dispute decided by the admin is not a broken promise by
        # either side — it does not count for or against anyone.
        offer = self._offer()
        req = self._request(offer)
        self._decide(req, "admin_refund")
        self.assertIsNone(app.reliability(
            self.conn, self.daniel["id"], "saver"))
        self.assertIsNone(app.reliability(
            self.conn, self.lev["id"], "requester"))

    def test_requester_reliability(self):
        # Lev requests in three offers by different savers/sessions:
        # confirmed once, not-saved once, cancelled once (excluded).
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        s2 = self._extra_session(30)
        s3 = self._extra_session(45)
        o1 = self._offer_for(self.session)
        o2 = self._offer_for(s2, saver=levi)
        o3 = self._offer_for(s3, saver=levi)
        r1 = self._request(o1)
        r2 = self._request(o2)
        r3 = self._request(o3)
        self.assertIsNone(self._decide(r1, "confirmed"))
        self.assertIsNone(self._decide(r2, "not_saved"))
        self.assertIsNone(self._decide(r3, "cancelled"))
        self.assertEqual(app.reliability(
            self.conn, self.lev["id"], "requester"), (1, 2))

    def test_reliability_shown_on_card_and_my(self):
        h = app.Handler.__new__(app.Handler)
        offer = self._offer()
        req = self._request(offer)
        self._decide(req, "confirmed")
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        html = h._offer_card(self.conn, row, dict(self.lev))
        # Round 20, item 8: the card shows the percentage with the
        # count — "100% (1/1)", not just "1/1".
        self.assertIn("אמינות: 100% (1/1)", html)
        my = h._my(self.conn, dict(self.daniel), None, None)
        self.assertIn("אמינות כשומר: 1/1", my)
        my_lev = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("אמינות כמבקש: 1/1", my_lev)

    def test_no_reliability_text_when_no_history(self):
        h = app.Handler.__new__(app.Handler)
        self.assertNotIn("אמינות", h._my(self.conn, dict(self.lev),
                                          None, None))


class Round5MigrationTest(Base):
    def test_new_table_and_column_exist_after_migration(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("waiting_list", tables)
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(offers)").fetchall()}
        self.assertIn("saver_reminded_at", cols)

    def test_migration_adds_table_and_column_to_old_db(self):
        # Simulate an old DB: drop the new table/column, re-run init.
        self.conn.execute("DROP TABLE waiting_list")
        self.conn.execute(
            "ALTER TABLE offers DROP COLUMN saver_reminded_at")
        self.conn.commit()
        app.init_db(self.conn)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("waiting_list", tables)
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(offers)").fetchall()}
        self.assertIn("saver_reminded_at", cols)
        # And the app still works on the migrated DB.
        offer = self._offer()
        req = self._request(offer)
        self.assertEqual(app._request_seats(dict(req)), 1)


def _parse_vapid_public_key(pub_b64):
    """Decode a VAPID public key and prove it is a real P-256 point:
    65 bytes, first byte 0x04 (uncompressed point), X and Y on the
    curve. Raises if any of that is false. Returns the raw bytes."""
    raw = app._decode_b64url(pub_b64)
    assert raw is not None, "public key is not valid base64"
    assert len(raw) == 65, "public key must be 65 bytes, got %d" % len(raw)
    assert raw[0] == 0x04, "first byte must be 0x04, got 0x%02x" % raw[0]
    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:65], "big")
    # Raises ValueError if (x, y) is not on the P-256 curve.
    ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    return raw


@unittest.skipUnless(HAS_CRYPTO, "cryptography not installed")
class VapidKeyTest(unittest.TestCase):
    """The 0x04 bug: an earlier build stored the VAPID public key as
    X||Y (64 bytes) instead of the full uncompressed point
    (0x04 || X || Y, 65 bytes), and every browser rejected it at
    subscribe time ("applicationServerKey is not valid")."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keys_path = os.path.join(self.tmp.name, "vapid_keys.json")
        self._orig = app.VAPID_KEYS_PATH
        app.VAPID_KEYS_PATH = self.keys_path

    def tearDown(self):
        app.VAPID_KEYS_PATH = self._orig
        self.tmp.cleanup()

    def _legacy_key_file(self):
        # A key file as the pre-fix build wrote it: public key X||Y,
        # 64 bytes, no 0x04 prefix.
        key = ec.generate_private_key(ec.SECP256R1())
        nums = key.private_numbers()
        priv = app._b64url(nums.private_value.to_bytes(32, "big"))
        pub_nums = key.public_key().public_numbers()
        legacy_pub = app._b64url(pub_nums.x.to_bytes(32, "big")
                                 + pub_nums.y.to_bytes(32, "big"))
        with open(self.keys_path, "w", encoding="utf-8") as f:
            json.dump({"private_key": priv, "public_key": legacy_pub}, f)
        return priv, legacy_pub

    def _read_keys_file(self):
        with open(self.keys_path, encoding="utf-8") as f:
            return json.load(f)

    def test_fresh_key_is_65_bytes_with_04_prefix_and_valid_point(self):
        priv, pub = app.generate_vapid_keys()
        _parse_vapid_public_key(pub)
        self.assertEqual(len(app._decode_b64url(priv)), 32)
        # And the file on disk holds the same pair.
        data = self._read_keys_file()
        self.assertEqual(data["public_key"], pub)
        self.assertEqual(data["private_key"], priv)

    def test_legacy_64_byte_key_is_repaired_on_load(self):
        priv, legacy_pub = self._legacy_key_file()
        self.assertEqual(len(app._decode_b64url(legacy_pub)), 64)
        priv2, pub2 = app.get_vapid_keys()
        # The private key is untouched by the repair.
        self.assertEqual(priv2, priv)
        raw = _parse_vapid_public_key(pub2)
        # Same point, only the missing 0x04 byte added.
        self.assertEqual(raw[1:], app._decode_b64url(legacy_pub))
        data = self._read_keys_file()
        self.assertEqual(data["public_key"], pub2)
        self.assertEqual(data["private_key"], priv)

    def test_repaired_key_is_stable_on_second_load(self):
        self._legacy_key_file()
        _, pub1 = app.get_vapid_keys()
        _, pub2 = app.get_vapid_keys()
        self.assertEqual(pub1, pub2)

    def test_undecodable_public_key_regenerates_fresh_pair(self):
        priv, _ = self._legacy_key_file()
        with open(self.keys_path, "w", encoding="utf-8") as f:
            json.dump({"private_key": priv, "public_key": 12345}, f)
        priv2, pub2 = app.get_vapid_keys()
        self.assertNotEqual(priv2, priv)
        _parse_vapid_public_key(pub2)

    def test_65_byte_key_with_wrong_prefix_regenerates(self):
        priv, _ = self._legacy_key_file()
        # 65 bytes but starting with 0x05: not a valid uncompressed
        # point, so it is not trusted — a fresh pair is generated.
        bad = app._b64url(b"\x05" + b"\x00" * 64)
        with open(self.keys_path, "w", encoding="utf-8") as f:
            json.dump({"private_key": priv, "public_key": bad}, f)
        priv2, pub2 = app.get_vapid_keys()
        self.assertNotEqual(priv2, priv)
        _parse_vapid_public_key(pub2)

    def test_missing_key_file_generates_fresh_pair(self):
        priv, pub = app.get_vapid_keys()
        _parse_vapid_public_key(pub)
        self.assertTrue(os.path.exists(self.keys_path))


class BoardTest(Base):
    def _other_session(self):
        # A second session of the same course, today, starting in
        # 30 minutes — a different slot to post about.
        return self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '35', '2')"
            " RETURNING *",
            (self.course["id"], self.today, _mins_from_now(30),
             _mins_from_now(90))).fetchone()

    def test_post_creates_open_request(self):
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["user_id"], self.lev["id"])

    def test_duplicate_open_post_rejected(self):
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(err, err)
        row2, err2 = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(row2)
        self.assertIsNotNone(err2)
        # Another user may post for the same session, though.
        row3, err3 = app.post_seat_request(
            self.conn, self.daniel["id"], self.session["id"])
        self.assertIsNone(err3, err3)

    def test_repost_allowed_after_fulfilled(self):
        app.post_seat_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer()
        self._request(offer)
        # The post is fulfilled — but Lev still holds a seat for
        # today's class, so posting again right now would be false
        # demand for a class he already has a seat for.
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(row)
        self.assertIn("כבר יש לך מקום שמור", err)
        # Once the request is cancelled (no seat held any more),
        # posting again is fine: a fulfilled post never blocks you
        # permanently.
        req = self.conn.execute(
            "SELECT * FROM requests WHERE offer_id=?",
            (offer["id"],)).fetchone()
        err = app.cancel_request(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err, err)
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "open")

    def test_post_unknown_session_or_user_rejected(self):
        _, err = app.post_seat_request(self.conn, self.lev["id"], 99999)
        self.assertIsNotNone(err)
        _, err = app.post_seat_request(self.conn, 99999, self.session["id"])
        self.assertIsNotNone(err)

    def test_post_does_not_touch_coins(self):
        before = app.get_user(self.conn, self.lev["id"])["balance"]
        app.post_seat_request(self.conn, self.lev["id"], self.session["id"])
        after = app.get_user(self.conn, self.lev["id"])["balance"]
        self.assertEqual(before, after)

    def test_cancel_only_by_poster_and_only_while_open(self):
        row, _ = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        err = app.cancel_seat_request(
            self.conn, row["id"], self.daniel["id"])
        self.assertIsNotNone(err)
        err = app.cancel_seat_request(
            self.conn, row["id"], self.lev["id"])
        self.assertIsNone(err)
        self.assertEqual(
            self.conn.execute("SELECT status FROM seat_requests WHERE id=?",
                              (row["id"],)).fetchone()[0], "cancelled")
        # Cancelling again is rejected — it is already closed.
        err = app.cancel_seat_request(
            self.conn, row["id"], self.lev["id"])
        self.assertIsNotNone(err)

    def test_expire_at_next_class_start(self):
        row, _ = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        start = app.next_session_start(dict(self.session))
        # Before the start: still open.
        self.assertEqual(app.expire_seat_requests(
            self.conn, now=start - timedelta(minutes=1)), 0)
        # After the start: expired.
        self.assertEqual(app.expire_seat_requests(
            self.conn, now=start + timedelta(minutes=1)), 1)
        self.assertEqual(
            self.conn.execute("SELECT status FROM seat_requests WHERE id=?",
                              (row["id"],)).fetchone()[0], "expired")

    def test_successful_request_fulfills_post(self):
        app.post_seat_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer()
        self._request(offer)
        row = self.conn.execute(
            "SELECT * FROM seat_requests WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(row["status"], "fulfilled")

    def test_offer_notifies_board_posters(self):
        app.post_seat_request(self.conn, self.lev["id"], self.session["id"])
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        sent = []
        app.notify_board_requesters(
            self.conn, offer,
            sender=lambda sub, payload: sent.append((sub, payload)))
        self.assertEqual(len(sent), 1)
        self.assertIn("שביקשת", sent[0][1]["title"])
        self.assertIn("תכנון אלגוריתמים", sent[0][1]["body"])

    def test_board_poster_notified_without_follow(self):
        # Lev never followed the course — the post itself is the
        # request, so he is still told about the offer.
        app.post_seat_request(self.conn, self.lev["id"], self.session["id"])
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        sent = []
        app.notify_board_requesters(
            self.conn, offer,
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(len(sent), 1)

    def test_saver_not_notified_about_own_offer(self):
        app.post_seat_request(
            self.conn, self.daniel["id"], self.session["id"])
        app.save_subscription(self.conn, self.daniel["id"], {
            "endpoint": "https://example.com/push/daniel",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        sent = []
        app.notify_board_requesters(
            self.conn, offer,
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(sent, [])

    def test_board_page_lists_posts_grouped_by_session(self):
        h = app.Handler.__new__(app.Handler)
        app.post_seat_request(self.conn, self.lev["id"], self.session["id"])
        app.post_seat_request(
            self.conn, self.daniel["id"], self.session["id"])
        html = h._wanted(self.conn, dict(self.lev), None, None)
        self.assertIn("מחפשים מקום", html)
        self.assertIn("2 מחפשים", html)
        self.assertIn("Lev", html)
        self.assertIn("Daniel", html)

    def test_wanted_post_form_is_two_step(self):
        # Daniel, 2026-11-10: first pick a course, then the lesson —
        # one dropdown with every session was too crowded.
        h = app.Handler.__new__(app.Handler)
        # No course picked: course links + hint, no session dropdown.
        html = h._wanted(self.conn, dict(self.lev), None, None)
        self.assertIn("בחר קורס:", html)
        self.assertIn("בחר קורס למעלה כדי לראות את השיעורים שלו", html)
        self.assertNotIn('name="session_id"', html)
        self.assertNotIn("פרסם שאני מחפש מקום", html)
        # A course picked: only that course's sessions in the
        # dropdown, with the submit button. (The course picker
        # lists every course as a link, so check the session
        # labels — "Course:" with a colon — not the bare names.)
        course = self.conn.execute(
            "SELECT * FROM courses WHERE name='תכנות מערכות'"
            ).fetchone()
        html = h._wanted(self.conn, dict(self.lev), None, None,
                         {"course_id": [str(course["id"])]})
        self.assertIn('name="session_id"', html)
        self.assertIn("פרסם שאני מחפש מקום", html)
        self.assertIn("תכנות מערכות:", html)
        self.assertNotIn("תכנון אלגוריתמים:", html)
        self.assertNotIn("חדו״א 2:", html)
        # An unknown course id behaves like no course picked.
        html = h._wanted(self.conn, dict(self.lev), None, None,
                         {"course_id": ["9999"]})
        self.assertNotIn('name="session_id"', html)
        self.assertIn("בחר קורס למעלה כדי לראות את השיעורים שלו", html)

    def test_wanted_sessions_are_sunday_first(self):
        # The filtered session list is Sunday first (the week
        # starts on Sunday, 2026-11-10), not Monday first.
        h = app.Handler.__new__(app.Handler)
        course = self.conn.execute(
            "SELECT * FROM courses WHERE name='תכנות מערכות'"
            ).fetchone()
        html = h._wanted(self.conn, dict(self.lev), None, None,
                         {"course_id": [str(course["id"])]})
        # Sunday 10:00 (Adler lecture) before Monday 14:00.
        self.assertLess(html.index("ראשון 10:00"),
                        html.index("שני 14:00"))


class ArrivedTest(Base):
    def test_mark_arrived_sets_time(self):
        offer = self._offer()
        err = app.mark_arrived_in_class(
            self.conn, offer["id"], self.daniel["id"])
        self.assertIsNone(err)
        row = app.get_offer(self.conn, offer["id"])
        self.assertTrue(row["arrived_at"])

    def test_mark_arrived_only_by_saver_and_only_while_open(self):
        offer = self._offer()
        err = app.mark_arrived_in_class(
            self.conn, offer["id"], self.lev["id"])
        self.assertIsNotNone(err)
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        err = app.mark_arrived_in_class(
            self.conn, offer["id"], self.daniel["id"])
        self.assertIsNotNone(err)

    def test_mark_arrived_double_tap_updates_time(self):
        offer = self._offer()
        t1 = datetime.now() - timedelta(minutes=5)
        app.mark_arrived_in_class(self.conn, offer["id"], self.daniel["id"],
                                  now=t1)
        t2 = datetime.now()
        app.mark_arrived_in_class(self.conn, offer["id"], self.daniel["id"],
                                  now=t2)
        row = app.get_offer(self.conn, offer["id"])
        self.assertEqual(row["arrived_at"], t2.isoformat(timespec="seconds"))

    def test_arrived_shown_on_offer_card(self):
        h = app.Handler.__new__(app.Handler)
        offer = self._offer()
        app.mark_arrived_in_class(self.conn, offer["id"], self.daniel["id"])
        fresh = app.get_offer(self.conn, offer["id"])
        row = dict(fresh)
        row["course_name"] = "תכנון אלגוריתמים"
        row["saver_name"] = "Daniel"
        html = h._offer_card(self.conn, row, dict(self.lev))
        self.assertIn("השומר כבר בכיתה", html)
        # Before marking, the card says nothing about arriving.
        offer2 = self._offer(saver=self.lev)
        row2 = dict(offer2)
        row2["course_name"] = "תכנון אלגוריתמים"
        row2["saver_name"] = "Lev"
        html2 = h._offer_card(self.conn, row2, dict(self.daniel))
        self.assertNotIn("כבר בכיתה", html2)


class TemplateTest(Base):
    def test_save_create_update_and_delete(self):
        row, err = app.save_offer_template(
            self.conn, self.daniel["id"], self.session["id"], "שורה 2", 2)
        self.assertIsNone(err, err)
        self.assertEqual(row["note"], "שורה 2")
        self.assertIsNone(row["zone"])
        # Saving again for the same session updates, not duplicates.
        row2, err2 = app.save_offer_template(
            self.conn, self.daniel["id"], self.session["id"], "שורה 9", 3)
        self.assertIsNone(err2, err2)
        self.assertEqual(row2["id"], row["id"])
        self.assertEqual(row2["note"], "שורה 9")
        self.assertIsNone(row2["zone"])
        self.assertEqual(row2["seats"], 3)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM offer_templates WHERE user_id=?",
            (self.daniel["id"],)).fetchone()[0]
        self.assertEqual(count, 1)
        # Only the owner can delete.
        err = app.delete_offer_template(
            self.conn, row["id"], self.lev["id"])
        self.assertIsNotNone(err)
        err = app.delete_offer_template(
            self.conn, row["id"], self.daniel["id"])
        self.assertIsNone(err)
        self.assertIsNone(app.get_template_for_session(
            self.conn, self.daniel["id"], self.session["id"]))

    def test_template_validation(self):
        # Round 13: there is no zone to validate any more — the
        # validation left is user, session and seat count.
        _, err = app.save_offer_template(
            self.conn, 99999, self.session["id"], "", 1)
        self.assertIsNotNone(err)
        _, err = app.save_offer_template(
            self.conn, self.daniel["id"], self.session["id"], "", 11)
        self.assertIsNotNone(err)
        _, err = app.save_offer_template(
            self.conn, self.daniel["id"], 99999, "", 1)
        self.assertIsNotNone(err)

    def test_template_prefills_a_real_offer(self):
        app.save_offer_template(
            self.conn, self.daniel["id"], self.session["id"], "שורה 9", 2)
        t = app.get_template_for_session(
            self.conn, self.daniel["id"], self.session["id"])
        offer, err = app.create_offer(
            self.conn, self.daniel["id"], t["session_id"],
            t["note"], t["seats"])
        self.assertIsNone(err, err)
        self.assertIsNone(offer["zone"])
        self.assertEqual(offer["note"], "שורה 9")
        self.assertEqual(offer["seats_total"], 2)

    def test_template_never_creates_offers_by_itself(self):
        # Round 13: weekly auto-offers are gone entirely, so there
        # is no tick to run — a template alone must create nothing.
        app.save_offer_template(
            self.conn, self.daniel["id"], self.session["id"], "", 1)
        self.assertFalse(hasattr(app, "auto_offer_tick"))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM offers").fetchone()[0], 0)

    def test_template_offer_button_only_for_in_window_templates(self):
        h = app.Handler.__new__(app.Handler)
        app.save_offer_template(
            self.conn, self.daniel["id"], self.session["id"], "", 1)
        html = h._offer_form(self.conn, dict(self.daniel), None, None)
        self.assertIn("הצע לפי התבנית", html)


class NewSessionNotifyTest(Base):
    def _new_session(self):
        # A new session of תכנון אלגוריתמים, today, starting in 2 hours.
        return self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room, teacher)"
            " VALUES (?, ?, ?, ?, '35', '2', 'ד״ר חדש')"
            " RETURNING *",
            (self.course["id"], self.today, _mins_from_now(120),
             _mins_from_now(180))).fetchone()

    def test_general_follower_notified_specific_matching_too(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        session = self._new_session()
        app.add_follow(self.conn, levi["id"], self.course["id"],
                       session_id=session["id"])
        for u in (self.lev, levi, self.daniel):
            app.save_subscription(self.conn, u["id"], {
                "endpoint": "https://example.com/push/%s" % u["id"],
                "keys": {"p256dh": "AAA", "auth": "BBB"}})
        sent = []
        app.notify_followers_of_new_session(
            self.conn, session,
            sender=lambda sub, payload: sent.append((sub, payload)),
            exclude_user_id=self.daniel["id"])
        endpoints = [s[0]["endpoint"] for s in sent]
        self.assertIn("https://example.com/push/%s" % self.lev["id"],
                      endpoints)
        self.assertIn("https://example.com/push/%s" % levi["id"],
                      endpoints)
        # Daniel added it himself: excluded.
        self.assertNotIn("https://example.com/push/%s" % self.daniel["id"],
                         endpoints)
        payload = sent[0][1]
        self.assertIn("נוסף שיעור חדש", payload["title"])
        self.assertIn("תכנון אלגוריתמים", payload["title"])
        self.assertIn("ד״ר חדש", payload["body"])
        self.assertIn("35", payload["body"])

    def test_specific_follow_for_other_session_not_notified(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"],
                       session_id=self.session["id"])
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        session = self._new_session()
        sent = []
        app.notify_followers_of_new_session(
            self.conn, session,
            sender=lambda sub, payload: sent.append(sub))
        # Lev follows the base session (starts in 10 min), not the
        # new one (starts in 2 hours): no notification.
        self.assertEqual(sent, [])

    def test_non_follower_not_notified(self):
        session = self._new_session()
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        sent = []
        app.notify_followers_of_new_session(
            self.conn, session,
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(sent, [])


class QuietHoursTest(Base):
    def _at_hour(self, hour):
        return datetime.now().replace(hour=hour, minute=0, second=0,
                                      microsecond=0)

    def test_set_clear_and_validation(self):
        err = app.set_quiet_hours(self.conn, self.lev["id"], "23", "7")
        self.assertIsNone(err)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual((user["quiet_start"], user["quiet_end"]), (23, 7))
        # One hour alone is rejected.
        err = app.set_quiet_hours(self.conn, self.lev["id"], "23", "")
        self.assertIsNotNone(err)
        # Equal hours are rejected.
        err = app.set_quiet_hours(self.conn, self.lev["id"], "8", "8")
        self.assertIsNotNone(err)
        # Out of range and non-numeric are rejected.
        for bad in (("24", "7"), ("-1", "7"), ("x", "7")):
            err = app.set_quiet_hours(self.conn, self.lev["id"], *bad)
            self.assertIsNotNone(err)
        # Clearing works.
        err = app.set_quiet_hours(self.conn, self.lev["id"], "", "")
        self.assertIsNone(err)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertIsNone(user["quiet_start"])
        self.assertIsNone(user["quiet_end"])

    def test_in_quiet_hours_day_window(self):
        app.set_quiet_hours(self.conn, self.lev["id"], "8", "22")
        user = dict(app.get_user(self.conn, self.lev["id"]))
        self.assertTrue(app.in_quiet_hours(user, self._at_hour(8)))
        self.assertTrue(app.in_quiet_hours(user, self._at_hour(21)))
        self.assertFalse(app.in_quiet_hours(user, self._at_hour(22)))
        self.assertFalse(app.in_quiet_hours(user, self._at_hour(7)))

    def test_in_quiet_hours_overnight_window(self):
        app.set_quiet_hours(self.conn, self.lev["id"], "23", "7")
        user = dict(app.get_user(self.conn, self.lev["id"]))
        self.assertTrue(app.in_quiet_hours(user, self._at_hour(23)))
        self.assertTrue(app.in_quiet_hours(user, self._at_hour(0)))
        self.assertTrue(app.in_quiet_hours(user, self._at_hour(6)))
        self.assertFalse(app.in_quiet_hours(user, self._at_hour(7)))
        self.assertFalse(app.in_quiet_hours(user, self._at_hour(22)))

    def test_no_quiet_hours_means_never_quiet(self):
        user = dict(app.get_user(self.conn, self.lev["id"]))
        self.assertFalse(app.in_quiet_hours(user, self._at_hour(3)))

    def test_push_skipped_and_logged_during_quiet_hours(self):
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        now = datetime.now()
        start = now.hour
        end = (now.hour + 2) % 24
        app.set_quiet_hours(self.conn, self.lev["id"], str(start), str(end))
        offer = self._offer()
        sent = []
        results = app._push_to_user(
            self.conn, self.lev["id"], offer["id"],
            {"title": "t", "body": "b", "url": "/", "tag": "x"},
            sender=lambda sub, payload: sent.append(sub), now=now)
        self.assertEqual(sent, [])
        self.assertEqual(results[0][1], "skipped-quiet-hours")
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-quiet-hours")
        # Outside quiet hours the same push goes through.
        app.set_quiet_hours(self.conn, self.lev["id"], "", "")
        results = app._push_to_user(
            self.conn, self.lev["id"], offer["id"],
            {"title": "t", "body": "b", "url": "/", "tag": "x"},
            sender=lambda sub, payload: sent.append(sub), now=now)
        self.assertEqual(len(sent), 1)
        self.assertEqual(results[0][1], "sent")

    def test_reminder_not_retried_when_quiet(self):
        # Skipped means skipped: the reminder is marked attempted
        # even when quiet hours ate it, so it is never retried.
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        now = datetime.now()
        start = now.hour
        end = (now.hour + 3) % 24
        app.set_quiet_hours(self.conn, self.lev["id"], str(start), str(end))
        offer = self._offer()
        req = self._request(offer)
        sent = []
        tick_now = now + timedelta(minutes=6)
        count = app.reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(sub),
            now=tick_now)
        self.assertEqual(count, 1)
        self.assertEqual(sent, [])
        row = self.conn.execute(
            "SELECT reminded_at FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertIsNotNone(row["reminded_at"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-quiet-hours")


class AdminDashboardTest(Base):
    def test_open_offers_and_frozen_requests_listed(self):
        offer = self._offer()
        req = self._request(offer)
        offers = app.admin_open_offers(self.conn)
        self.assertEqual([o["id"] for o in offers], [offer["id"]])
        self.assertEqual(offers[0]["saver_name"], "Daniel")
        frozen = app.admin_frozen_requests(self.conn)
        self.assertEqual([r["id"] for r in frozen], [req["id"]])
        self.assertEqual(frozen[0]["requester_name"], "Lev")
        # After confirming, the frozen list is empty.
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self.assertEqual(app.admin_frozen_requests(self.conn), [])
        # After cancelling the offer, the open list is empty too.
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        self.assertEqual(app.admin_open_offers(self.conn), [])

    def test_frozen_duration_text(self):
        now = datetime.now()
        self.assertEqual(app._frozen_duration_text(
            now.isoformat(timespec="seconds"), now), "ממש עכשיו")
        self.assertEqual(app._frozen_duration_text(
            (now - timedelta(minutes=5)).isoformat(timespec="seconds"),
            now), "לפני 5 דקות")
        self.assertEqual(app._frozen_duration_text(
            (now - timedelta(minutes=60)).isoformat(timespec="seconds"),
            now), "לפני שעה")
        self.assertEqual(app._frozen_duration_text(
            (now - timedelta(minutes=120)).isoformat(timespec="seconds"),
            now), "לפני שעתיים")
        self.assertEqual(app._frozen_duration_text(
            (now - timedelta(minutes=300)).isoformat(timespec="seconds"),
            now), "לפני 5 שעות")
        self.assertEqual(app._frozen_duration_text("garbage", now), "")

    def test_admin_page_renders_for_daniel(self):
        h = app.Handler.__new__(app.Handler)
        offer = self._offer()
        req = self._request(offer)
        html = h._admin(self.conn, dict(self.daniel), None, None)
        self.assertIn("דף בקרה", html)
        self.assertIn("תכנון אלגוריתמים", html)
        self.assertIn("Lev", html)
        self.assertIn("Daniel", html)
        self.assertIn("1 מקומות מוקפאים", html)

    def test_admin_link_only_for_daniel(self):
        html_d = app.page_html("x", "y", user=dict(self.daniel))
        self.assertIn('href="/admin"', html_d)
        html_l = app.page_html("x", "y", user=dict(self.lev))
        self.assertNotIn('href="/admin"', html_l)


class Round6MigrationTest(Base):
    def test_new_tables_and_columns_exist(self):
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("seat_requests", tables)
        self.assertIn("offer_templates", tables)
        ocols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(offers)").fetchall()}
        self.assertIn("arrived_at", ocols)
        ucols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(users)").fetchall()}
        for col in ("preferred_zone", "quiet_start", "quiet_end"):
            self.assertIn(col, ucols)

    def test_migration_adds_tables_and_columns_to_old_db(self):
        # Simulate an old DB: drop the new tables/columns, re-run init.
        self.conn.execute("DROP TABLE seat_requests")
        self.conn.execute("DROP TABLE offer_templates")
        self.conn.execute("ALTER TABLE offers DROP COLUMN arrived_at")
        self.conn.execute("ALTER TABLE users DROP COLUMN preferred_zone")
        self.conn.execute("ALTER TABLE users DROP COLUMN quiet_start")
        self.conn.execute("ALTER TABLE users DROP COLUMN quiet_end")
        self.conn.commit()
        app.init_db(self.conn)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("seat_requests", tables)
        self.assertIn("offer_templates", tables)
        ocols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(offers)").fetchall()}
        self.assertIn("arrived_at", ocols)
        ucols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(users)").fetchall()}
        for col in ("preferred_zone", "quiet_start", "quiet_end"):
            self.assertIn(col, ucols)
        # And the app still works on the migrated DB.
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "open")


class CopyCleanupTest(Base):
    """Daniel's wording cleanup: the site keeps only text that helps
    the user DO something. Meta-commentary about what the app does
    NOT use, internal admin facts, and implementation explanations
    are gone — and this test makes sure they cannot come back."""

    BANNED = ("טלגרם", "רק דניאל מוסיף", "דניאל מכניס את הקורסים",
              "לא באג באפליקציה", "מה אין כאן בכוונה", "הכרעת מנהל",
              "מפתח VAPID", "ראה README", "הסיסמה נשמרת מוצפנת",
              "רמת פיילוט", "אין אימייל", "ההיסטוריה מתחילה מהעדכון")

    def _pages_as(self, user):
        h = app.Handler.__new__(app.Handler)
        lev = dict(user)
        return [
            h._home(self.conn, lev, None, None),
            h._register(self.conn, None, None, None),
            h._login(self.conn, None, None, None),
            h._courses(self.conn, lev, None, None),
            h._offers(self.conn, lev, None, None),
            h._offer_form(self.conn, lev, None, None),
            h._follows(self.conn, lev, None, None),
            h._my(self.conn, lev, None, None),
            h._push(self.conn, lev, None, None),
            h._help(self.conn, lev, None, None),
            h._wanted(self.conn, lev, None, None),
            app.page_html("כותרת", "<p>גוף</p>", user=lev),
        ]

    def test_removed_phrases_appear_on_no_page(self):
        for html in self._pages_as(self.lev):
            for phrase in self.BANNED:
                self.assertNotIn(phrase, html, phrase)

    def test_help_still_keeps_the_real_rules(self):
        h = app.Handler.__new__(app.Handler)
        html = h._help(self.conn, None, None, None)
        for word in ("מטבע", "רצפה", "מינוס 2", "אייפון", "סיסמה",
                     "60", "10", "מעקב", "התראות", "שעות שקטות",
                     "מחפשים מקום"):
            self.assertIn(word, html)


class QAFixTest(Base):
    """Bugs found by the round-6 QA sweep, and their fixes. Each test
    fails on the pre-fix code."""

    def test_board_poster_who_follows_gets_one_push(self):
        # Lev follows תכנון אלגוריתמים generally AND posted on the board for
        # this session. One offer must produce exactly ONE push to
        # him — the board message — not the follower message as well.
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(err, err)
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        sent = []
        # The route order: followers first, board second.
        app.notify_followers(
            self.conn, offer,
            sender=lambda sub, payload: sent.append(payload))
        app.notify_board_requesters(
            self.conn, offer,
            sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(len(sent), 1)
        self.assertIn("מישהו מציע מקום לשיעור שביקשת", sent[0]["title"])

    def test_follower_without_board_post_still_notified(self):
        # Regression guard for the dedup above: a plain follower
        # (no board post) still gets the follower push.
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        sent = []
        app.notify_followers(
            self.conn, offer,
            sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(len(sent), 1)
        self.assertIn("מקום פנוי", sent[0]["title"])
        self.assertIn("תכנון אלגוריתמים", sent[0]["title"])

    def test_quiet_hours_midnight_start_as_int(self):
        # set_quiet_hours(conn, uid, 0, 7): hour 0 is midnight, a real
        # hour — not an empty value. The old `(start or "")` code
        # silently cleared the setting instead.
        err = app.set_quiet_hours(self.conn, self.lev["id"], 0, 7)
        self.assertIsNone(err, err)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(user["quiet_start"], 0)
        self.assertEqual(user["quiet_end"], 7)
        self.assertTrue(app.in_quiet_hours(
            user, datetime.now().replace(hour=0, minute=30)))
        self.assertFalse(app.in_quiet_hours(
            user, datetime.now().replace(hour=8, minute=0)))

    def test_quiet_hours_equal_start_end_never_quiet(self):
        # start == end is rejected by the setter, but a hand-edited DB
        # could hold it — it must never mean "quiet all day".
        user = {"quiet_start": 8, "quiet_end": 8}
        for hour in (0, 8, 12, 20, 23):
            self.assertFalse(app.in_quiet_hours(
                user, datetime.now().replace(hour=hour, minute=0)))

    def test_board_post_blocked_while_holding_seat(self):
        # Lev holds a frozen seat for this session: posting "I need a
        # seat" for the same session is false demand.
        offer = self._offer()
        self._request(offer)
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(row)
        self.assertIn("כבר יש לך מקום שמור", err)
        # After the request is cancelled, the post is allowed again.
        req = self.conn.execute(
            "SELECT * FROM requests WHERE offer_id=?",
            (offer["id"],)).fetchone()
        err = app.cancel_request(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err, err)
        row, err = app.post_seat_request(
            self.conn, self.lev["id"], self.session["id"])
        self.assertIsNone(err, err)
        self.assertIsNotNone(row)


class Round6RaceTest(Base):
    """The new round-6 writes, hit from two threads at once — the
    same pattern as ConcurrencyTest (one connection per thread)."""

    def _run_two(self, fn):
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(i):
            conn = app.connect(self.db)
            barrier.wait(timeout=5)
            results[i] = fn(conn)
            conn.close()

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return results

    def test_parallel_board_posts_one_open(self):
        for _ in range(5):
            results = self._run_two(
                lambda c: app.post_seat_request(
                    c, self.lev["id"], self.session["id"]))
            self.assertEqual(
                sum(1 for row, err in results if err is None), 1)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM seat_requests WHERE user_id=?"
                    " AND session_id=? AND status='open'",
                    (self.lev["id"], self.session["id"])).fetchone()[0], 1)
            self.conn.execute("DELETE FROM seat_requests")
            self.conn.commit()

    def test_parallel_template_saves_one_row(self):
        for _ in range(5):
            results = self._run_two(
                lambda c: app.save_offer_template(
                    c, self.lev["id"], self.session["id"], "", 1))
            self.assertEqual(
                sum(1 for row, err in results if err is None), 2)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM offer_templates WHERE user_id=?"
                    " AND session_id=?",
                    (self.lev["id"], self.session["id"])).fetchone()[0], 1)
            self.conn.execute("DELETE FROM offer_templates")
            self.conn.commit()

    def test_parallel_last_seat_board_poster_fulfilled_once(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        for _ in range(5):
            # Lev posted on the board, then races Levi for the last seat.
            row, err = app.post_seat_request(
                self.conn, self.lev["id"], self.session["id"])
            self.assertIsNone(err, err)
            offer = self._offer(seats=1)
            barrier = threading.Barrier(2)
            outs = [None, None]

            def w(i, uid):
                c = app.connect(self.db)
                barrier.wait(timeout=5)
                outs[i] = app.request_seat(c, uid, offer["id"])
                c.close()

            threads = [threading.Thread(target=w, args=(0, self.lev["id"])),
                       threading.Thread(target=w, args=(1, levi["id"]))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            # Exactly one winner, one frozen request, balances sane.
            self.assertEqual(sum(1 for _, e in outs if e is None), 1)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE offer_id=?"
                    " AND status='frozen'", (offer["id"],)).fetchone()[0], 1)
            winner = self.conn.execute(
                "SELECT requester_id FROM requests WHERE offer_id=?",
                (offer["id"],)).fetchone()[0]
            # The board post is fulfilled exactly when its poster won.
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM seat_requests WHERE user_id=?"
                    " AND session_id=? AND status='fulfilled'",
                    (self.lev["id"], self.session["id"])).fetchone()[0],
                1 if winner == self.lev["id"] else 0)
            self.conn.execute("DELETE FROM coin_ledger")
            self.conn.execute("DELETE FROM notification_log")
            self.conn.execute("DELETE FROM seat_requests")
            self.conn.execute("DELETE FROM requests")
            self.conn.execute("DELETE FROM offers")
            self.conn.execute("UPDATE users SET balance=3")
            self.conn.commit()

    def test_parallel_mark_arrived_no_error(self):
        for _ in range(5):
            offer = self._offer()
            results = self._run_two(
                lambda c: app.mark_arrived_in_class(
                    c, offer["id"], self.daniel["id"]))
            self.assertEqual(
                sum(1 for err in results if err is None), 2)
            row = self.conn.execute(
                "SELECT arrived_at FROM offers WHERE id=?",
                (offer["id"],)).fetchone()
            self.assertIsNotNone(row["arrived_at"])
            self.conn.execute("DELETE FROM offers")
            self.conn.commit()


class VacationTest(Base):
    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _my_html(self, user):
        h = app.Handler.__new__(app.Handler)
        return h._my(self.conn, dict(user), None, None)

    def test_default_off_and_toggle(self):
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(user["on_vacation"], 0)
        err = app.set_vacation(self.conn, self.lev["id"], 1)
        self.assertIsNone(err)
        self.assertTrue(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))
        err = app.set_vacation(self.conn, self.lev["id"], 0)
        self.assertIsNone(err)
        self.assertFalse(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))
        # Unknown user is rejected.
        err = app.set_vacation(self.conn, 99999, 1)
        self.assertIsNotNone(err)

    def test_set_vacation_accepts_forms(self):
        for on, expected in ((1, 1), (True, 1), ("1", 1), ("on", 1),
                             (0, 0), (False, 0), ("0", 0), (None, 0)):
            err = app.set_vacation(self.conn, self.lev["id"], on)
            self.assertIsNone(err)
            user = app.get_user(self.conn, self.lev["id"])
            self.assertEqual(user["on_vacation"], expected)

    def test_push_skipped_and_logged_on_vacation(self):
        self._sub(self.lev)
        app.set_vacation(self.conn, self.lev["id"], 1)
        offer = self._offer()
        sent = []
        results = app._push_to_user(
            self.conn, self.lev["id"], offer["id"],
            {"title": "t", "body": "b", "url": "/", "tag": "x"},
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(sent, [])
        self.assertEqual(results[0][1], "skipped-vacation")
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-vacation")
        # Back from vacation: the same push goes through.
        app.set_vacation(self.conn, self.lev["id"], 0)
        results = app._push_to_user(
            self.conn, self.lev["id"], offer["id"],
            {"title": "t", "body": "b", "url": "/", "tag": "x"},
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(len(sent), 1)
        self.assertEqual(results[0][1], "sent")

    def test_quiet_hours_checked_before_vacation(self):
        # Both set: the quiet-hours reason is logged, because the
        # quiet-hours check runs first in _push_to_user. Either
        # reason is true; the test pins the chosen order.
        self._sub(self.lev)
        now = datetime.now()
        app.set_quiet_hours(self.conn, self.lev["id"], str(now.hour),
                            str((now.hour + 2) % 24))
        app.set_vacation(self.conn, self.lev["id"], 1)
        offer = self._offer()
        sent = []
        results = app._push_to_user(
            self.conn, self.lev["id"], offer["id"],
            {"title": "t", "body": "b", "url": "/", "tag": "x"},
            sender=lambda sub, payload: sent.append(sub), now=now)
        self.assertEqual(sent, [])
        self.assertEqual(results[0][1], "skipped-quiet-hours")

    def test_reminder_skipped_on_vacation(self):
        # Skipped means skipped: the reminder is marked attempted
        # even when vacation ate it, so it is never retried.
        self._sub(self.lev)
        app.set_vacation(self.conn, self.lev["id"], 1)
        offer = self._offer()
        req = self._request(offer)
        sent = []
        tick_now = datetime.now() + timedelta(minutes=6)
        count = app.reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(sub),
            now=tick_now)
        self.assertEqual(count, 1)
        self.assertEqual(sent, [])
        row = self.conn.execute(
            "SELECT reminded_at FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertIsNotNone(row["reminded_at"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-vacation")

    def test_vacation_does_not_touch_existing_offers(self):
        offer = self._offer()
        req = self._request(offer)
        # The SAVER turning vacation on touches nothing: the offer
        # stays open, and the requester's frozen request stays
        # frozen — vacation pauses the saver's future, not someone
        # else's seat.
        app.set_vacation(self.conn, self.daniel["id"], 1)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["status"], "open")
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertEqual(row["status"], "frozen")
        # The REQUESTER turning vacation on cancels their own frozen
        # request (round 11): the coin is refunded at once, the
        # seat is freed, the offer itself stays open.
        app.set_vacation(self.conn, self.lev["id"], 1)
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled_by_requester")
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["status"], "open")
        self.assertEqual(
            app.seats_available(self.conn,
                                app.get_offer(self.conn, offer["id"])), 1)

    def test_my_page_shows_indicator_and_toggle(self):
        app.set_vacation(self.conn, self.lev["id"], 1)
        user = app.get_user(self.conn, self.lev["id"])
        html = self._my_html(user)
        self.assertIn("מצב חופשה פעיל", html)
        self.assertIn("חזרתי", html)
        self.assertIn('action="/set-vacation"', html)
        app.set_vacation(self.conn, self.lev["id"], 0)
        user = app.get_user(self.conn, self.lev["id"])
        html = self._my_html(user)
        self.assertNotIn("מצב חופשה פעיל", html)
        self.assertIn("אני בחופשה", html)


class CancelWaitingNotifyTest(Base):
    def _users(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        return levi, shaked

    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _full_offer_with_waiters(self):
        levi, shaked = self._users()
        offer = self._offer()  # 1 seat
        self._request(offer)   # Lev takes it — fully booked
        for u in (levi, shaked):
            app.add_follow(self.conn, u["id"], self.course["id"])
            self._sub(u)
            row, err = app.join_waiting_list(
                self.conn, u["id"], offer["id"])
            self.assertIsNone(err, err)
        return offer

    def test_waiters_notified_when_saver_cancels(self):
        offer = self._full_offer_with_waiters()
        sent = []
        err = app.cancel_offer(
            self.conn, offer["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append((sub, payload)))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 2)
        for sub, payload in sent:
            self.assertEqual(payload["title"], "ההצעה בוטלה")
            self.assertIn("תכנון אלגוריתמים", payload["body"])
            self.assertIn("בוטלה", payload["body"])
        logs = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchall()
        # Two sent rows (the waiters) plus one skipped row: the frozen
        # requester Lev is also told about the cancel since round 10,
        # but he has no subscription, so his row is a skip, not a send.
        self.assertEqual(len(logs), 3)
        self.assertEqual(
            sum(1 for l in logs if l["status"] == "sent"), 2)
        skipped = [l for l in logs
                   if l["status"] == "skipped-no-subscription"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["user_id"], self.lev["id"])
        # And the waiting entries are gone, as before.
        self.assertEqual(app.waiting_count_for_offer(self.conn, offer["id"]),
                         0)

    def test_no_notify_without_waiters(self):
        offer = self._offer()
        sent = []
        err = app.cancel_offer(
            self.conn, offer["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_waiter_gets_one_push_not_two(self):
        # A waiter who also follows the course gets exactly one
        # push on saver-cancel (the cancel notice) — the free-up
        # path stays silent because the offer is closed.
        offer = self._full_offer_with_waiters()
        sent = []
        err = app.cancel_offer(
            self.conn, offer["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append((sub, payload)))
        self.assertIsNone(err)
        endpoints = [sub["endpoint"] for sub, _ in sent]
        levi, shaked = self._users()
        for u in (levi, shaked):
            self.assertEqual(
                endpoints.count("https://example.com/push/%s" % u["id"]), 1)

    def test_expiry_does_not_notify_waiters(self):
        # Expiry is not a cancel: at class start the seat is gone
        # anyway, and a push then is noise — entries deleted silently.
        offer = self._full_offer_with_waiters()
        levi, shaked = self._users()
        app.settle_expired(
            self.conn, now=datetime.now() + timedelta(minutes=30))
        logs = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id IN (?, ?)",
            (levi["id"], shaked["id"])).fetchall()
        self.assertEqual(logs, [])
        self.assertEqual(app.waiting_count_for_offer(self.conn, offer["id"]),
                         0)

    def test_vacation_waiter_skipped(self):
        offer = self._full_offer_with_waiters()
        levi, _ = self._users()
        app.set_vacation(self.conn, levi["id"], 1)
        sent = []
        err = app.cancel_offer(
            self.conn, offer["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append((sub, payload)))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)  # only Shaked
        self.assertEqual(
            sent[0][0]["endpoint"],
            "https://example.com/push/%s" % self.conn.execute(
                "SELECT id FROM users WHERE name='Shaked'").fetchone()["id"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (levi["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-vacation")


class BoardDemandTest(Base):
    def _form_html(self):
        h = app.Handler.__new__(app.Handler)
        return h._offer_form(self.conn, dict(self.daniel), None, None)

    def test_count_only_open_posts(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        self.assertEqual(
            app.open_board_count_for_session(self.conn, self.session["id"]),
            0)
        app.post_seat_request(self.conn, levi["id"], self.session["id"])
        app.post_seat_request(self.conn, shaked["id"], self.session["id"])
        self.assertEqual(
            app.open_board_count_for_session(self.conn, self.session["id"]),
            2)
        # A cancelled post is not demand any more.
        row = self.conn.execute(
            "SELECT * FROM seat_requests WHERE user_id=?",
            (levi["id"],)).fetchone()
        app.cancel_seat_request(self.conn, row["id"], levi["id"])
        self.assertEqual(
            app.open_board_count_for_session(self.conn, self.session["id"]),
            1)
        # A fulfilled post is not demand either.
        offer = self._offer()
        req, err = app.request_seat(
            self.conn, shaked["id"], offer["id"])
        self.assertIsNone(err, err)
        self.assertEqual(
            app.open_board_count_for_session(self.conn, self.session["id"]),
            0)

    def test_labels(self):
        self.assertEqual(app.board_demand_label(0), "")
        self.assertEqual(app.board_demand_label(1), "מחפש מקום אחד")
        self.assertEqual(app.board_demand_label(2), "2 מחפשים מקום")
        self.assertEqual(app.board_demand_label(5), "5 מחפשים מקום")

    def test_offer_form_shows_demand(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        app.post_seat_request(self.conn, levi["id"], self.session["id"])
        app.post_seat_request(self.conn, shaked["id"], self.session["id"])
        html = self._form_html()
        self.assertIn("2 מחפשים מקום", html)
        # Twice on the page: once in the nav link "מחפשים מקום",
        # once in the form option — the window list below does not
        # repeat it (the demand signal belongs to the decision moment).
        self.assertEqual(html.count("מחפשים מקום"), 2)

    def test_offer_form_hides_demand_when_zero(self):
        html = self._form_html()
        self.assertNotIn("מחפש מקום אחד", html)
        # Only the nav link "מחפשים מקום" — no demand on any option.
        self.assertEqual(html.count("מחפשים מקום"), 1)


class Round7MigrationTest(Base):
    def test_on_vacation_column_exists_with_default_off(self):
        ucols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(users)").fetchall()}
        self.assertIn("on_vacation", ucols)
        for row in self.conn.execute("SELECT on_vacation FROM users"):
            self.assertEqual(row["on_vacation"], 0)

    def test_migration_adds_column_to_old_db(self):
        # Simulate an old DB: drop the column, re-run init.
        self.conn.execute("ALTER TABLE users DROP COLUMN on_vacation")
        self.conn.commit()
        app.init_db(self.conn)
        ucols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(users)").fetchall()}
        self.assertIn("on_vacation", ucols)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(user["on_vacation"], 0)
        # And the app still works on the migrated DB.
        err = app.set_vacation(self.conn, self.lev["id"], 1)
        self.assertIsNone(err)
        self.assertTrue(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))


class VacationEndDateTest(Base):
    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _my_html(self, user):
        h = app.Handler.__new__(app.Handler)
        return h._my(self.conn, dict(user), None, None)

    def test_set_and_clear_end_date(self):
        today = datetime.now().date()
        app.set_vacation(self.conn, self.lev["id"], 1)
        err = app.set_vacation_until(self.conn, self.lev["id"],
                                     (today + timedelta(days=5)).isoformat())
        self.assertIsNone(err)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(user["vacation_until"],
                         (today + timedelta(days=5)).isoformat())
        self.assertTrue(app.is_on_vacation(user))
        # Clearing the date keeps vacation on indefinitely.
        err = app.set_vacation_until(self.conn, self.lev["id"], "")
        self.assertIsNone(err)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertIsNone(user["vacation_until"])
        self.assertTrue(app.is_on_vacation(user))
        # Whitespace-only clears too, it is not an error.
        app.set_vacation_until(self.conn, self.lev["id"],
                               (today + timedelta(days=5)).isoformat())
        err = app.set_vacation_until(self.conn, self.lev["id"], "   ")
        self.assertIsNone(err)
        self.assertIsNone(
            app.get_user(self.conn, self.lev["id"])["vacation_until"])

    def test_end_date_today_and_future_and_unknown_user(self):
        today = datetime.now().date()
        err = app.set_vacation_until(self.conn, self.lev["id"],
                                     today.isoformat())
        self.assertIsNone(err)
        err = app.set_vacation_until(self.conn, self.lev["id"],
                                     (today + timedelta(days=30)).isoformat())
        self.assertIsNone(err)
        err = app.set_vacation_until(self.conn, 99999, today.isoformat())
        self.assertIsNotNone(err)

    def test_past_and_invalid_dates_rejected_state_unchanged(self):
        today = datetime.now().date()
        app.set_vacation(self.conn, self.lev["id"], 1)
        future = (today + timedelta(days=5)).isoformat()
        app.set_vacation_until(self.conn, self.lev["id"], future)
        for bad in ((today - timedelta(days=1)).isoformat(),
                    "2026-02-30", "12.11", "tomorrow", "2026/11/12",
                    "2026-11-12T00:00", "2026-13-01", "abc"):
            err = app.set_vacation_until(self.conn, self.lev["id"], bad)
            self.assertIsNotNone(err, bad)
            user = app.get_user(self.conn, self.lev["id"])
            self.assertTrue(app.is_on_vacation(user))
            self.assertEqual(user["vacation_until"], future)

    def test_turning_off_clears_end_date(self):
        today = datetime.now().date()
        app.set_vacation(self.conn, self.lev["id"], 1)
        app.set_vacation_until(self.conn, self.lev["id"],
                               (today + timedelta(days=5)).isoformat())
        app.set_vacation(self.conn, self.lev["id"], 0)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertFalse(app.is_on_vacation(user))
        self.assertIsNone(user["vacation_until"])

    def test_auto_off_today_kept_tomorrow_cleared(self):
        today = datetime.now().date()
        app.set_vacation(self.conn, self.lev["id"], 1)
        app.set_vacation_until(self.conn, self.lev["id"], today.isoformat())
        # Through the END of the chosen date: still on today.
        cleared = app.clear_expired_vacations(self.conn, now=datetime.now())
        self.assertEqual(cleared, 0)
        self.assertTrue(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))
        # From the next morning it is off automatically.
        cleared = app.clear_expired_vacations(
            self.conn, now=datetime.now() + timedelta(days=1))
        self.assertEqual(cleared, 1)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertFalse(app.is_on_vacation(user))
        self.assertIsNone(user["vacation_until"])
        # A second tick finds nothing to clear.
        cleared = app.clear_expired_vacations(
            self.conn, now=datetime.now() + timedelta(days=1))
        self.assertEqual(cleared, 0)

    def test_auto_off_future_date_and_no_date_kept(self):
        today = datetime.now().date()
        app.set_vacation(self.conn, self.lev["id"], 1)
        app.set_vacation_until(self.conn, self.lev["id"],
                               (today + timedelta(days=5)).isoformat())
        cleared = app.clear_expired_vacations(
            self.conn, now=datetime.now() + timedelta(days=1))
        self.assertEqual(cleared, 0)
        self.assertTrue(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))
        # No end date at all: never auto-cleared.
        app.set_vacation_until(self.conn, self.lev["id"], "")
        cleared = app.clear_expired_vacations(
            self.conn, now=datetime.now() + timedelta(days=365))
        self.assertEqual(cleared, 0)
        self.assertTrue(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))

    def test_auto_off_skips_corrupt_date_and_clears_others(self):
        today = datetime.now().date()
        app.set_vacation(self.conn, self.lev["id"], 1)
        self.conn.execute(
            "UPDATE users SET vacation_until='garbage' WHERE id=?",
            (self.lev["id"],))
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.set_vacation(self.conn, levi["id"], 1)
        app.set_vacation_until(self.conn, levi["id"], today.isoformat())
        self.conn.commit()
        cleared = app.clear_expired_vacations(
            self.conn, now=datetime.now() + timedelta(days=1))
        self.assertEqual(cleared, 1)
        self.assertTrue(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))
        self.assertFalse(app.is_on_vacation(
            app.get_user(self.conn, levi["id"])))

    def test_auto_off_resumes_pushes(self):
        # Round 13: weekly auto-offers are gone, so auto-off resumes
        # PUSHES only — the offer part of this test no longer exists.
        today = datetime.now().date()
        self._sub(self.daniel)
        offer = self._offer()
        app.set_vacation(self.conn, self.daniel["id"], 1)
        app.set_vacation_until(self.conn, self.daniel["id"],
                               today.isoformat())
        # While on vacation: pushes are skipped, never queued.
        sent = []
        results = app._push_to_user(
            self.conn, self.daniel["id"], offer["id"],
            {"title": "t", "body": "b", "url": "/", "tag": "x"},
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(sent, [])
        self.assertEqual(results[0][1], "skipped-vacation")
        # The next morning the vacation is gone — no user action —
        # and the same push sends again.
        app.clear_expired_vacations(
            self.conn, now=datetime.now() + timedelta(days=1))
        sent = []
        results = app._push_to_user(
            self.conn, self.daniel["id"], offer["id"],
            {"title": "t", "body": "b", "url": "/", "tag": "x"},
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(len(sent), 1)
        self.assertEqual(results[0][1], "sent")

    def test_vacation_until_text(self):
        today = datetime.now().date()
        self.assertEqual(app.vacation_until_text(self.lev), "")
        app.set_vacation(self.conn, self.lev["id"], 1)
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(app.vacation_until_text(user), "ללא תאריך סיום")
        app.set_vacation_until(self.conn, self.lev["id"], today.isoformat())
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(app.vacation_until_text(user),
                         "חופשה עד %s" % today.strftime("%d.%m"))
        self.conn.execute(
            "UPDATE users SET vacation_until='garbage' WHERE id=?",
            (self.lev["id"],))
        self.conn.commit()
        user = app.get_user(self.conn, self.lev["id"])
        self.assertEqual(app.vacation_until_text(user), "")

    def test_my_page_shows_end_date_and_date_form(self):
        today = datetime.now().date()
        app.set_vacation(self.conn, self.lev["id"], 1)
        app.set_vacation_until(self.conn, self.lev["id"], today.isoformat())
        user = app.get_user(self.conn, self.lev["id"])
        html = self._my_html(user)
        self.assertIn("חופשה עד %s" % today.strftime("%d.%m"), html)
        self.assertIn('action="/set-vacation-until"', html)
        self.assertIn('name="vacation_until"', html)
        app.set_vacation_until(self.conn, self.lev["id"], "")
        user = app.get_user(self.conn, self.lev["id"])
        html = self._my_html(user)
        self.assertIn("ללא תאריך סיום", html)
        app.set_vacation(self.conn, self.lev["id"], 0)
        user = app.get_user(self.conn, self.lev["id"])
        html = self._my_html(user)
        self.assertIn('name="vacation_until"', html)
        self.assertIn("חופשה עד", html)


class SaverCancelRequestTest(Base):
    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _my_html(self, user):
        h = app.Handler.__new__(app.Handler)
        return h._my(self.conn, dict(user), None, None)

    def _two_requests(self, seats=2):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        offer = self._offer(seats=seats)
        req1 = self._request(offer, self.lev)
        req2 = self._request(offer, levi)
        return offer, req1, req2, levi

    def test_saver_cancels_one_request_refund_and_offer_stays_open(self):
        offer, req1, req2, levi = self._two_requests()
        err = app.cancel_request_by_saver(self.conn, req1["id"],
                                          self.daniel["id"])
        self.assertIsNone(err)
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?", (req1["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled_by_saver")
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["status"], "open")
        self.assertEqual(app.seats_available(self.conn, offer), 1)
        # The other request is untouched.
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?", (req2["id"],)).fetchone()
        self.assertEqual(row["status"], "frozen")
        self.assertEqual(
            app.get_user(self.conn, levi["id"])["balance"], 2)
        # The ledger records the refund like any other refund.
        rows = self.conn.execute(
            "SELECT * FROM coin_ledger WHERE user_id=? AND reason=?",
            (self.lev["id"], "cancelled_by_saver")).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 1)

    def test_requester_gets_push(self):
        self._sub(self.lev)
        offer = self._offer()
        req = self._request(offer)
        sent = []
        err = app.cancel_request_by_saver(
            self.conn, req["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append((sub, payload)))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        sub, payload = sent[0]
        self.assertEqual(sub["endpoint"], "https://example.com/push/%s"
                         % self.lev["id"])
        self.assertEqual(payload["title"], "הבקשה שלך בוטלה על ידי השומר")
        self.assertIn("Daniel", payload["body"])
        self.assertIn("תכנון אלגוריתמים", payload["body"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=? AND user_id=?",
            (offer["id"], self.lev["id"])).fetchone()
        self.assertEqual(log["status"], "sent")

    def test_second_cancel_rejected_no_double_refund(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.cancel_request_by_saver(self.conn, req["id"],
                                          self.daniel["id"])
        self.assertIsNone(err)
        err = app.cancel_request_by_saver(self.conn, req["id"],
                                          self.daniel["id"])
        self.assertIsNotNone(err)
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_only_saver_and_only_frozen(self):
        offer, req1, req2, levi = self._two_requests()
        # Another requester cannot cancel someone else's request.
        err = app.cancel_request_by_saver(self.conn, req1["id"],
                                          self.lev["id"])
        self.assertIsNotNone(err)
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 2)
        # A request already resolved cannot be cancelled.
        app.confirm_arrival(self.conn, req2["id"], levi["id"])
        err = app.cancel_request_by_saver(self.conn, req2["id"],
                                          self.daniel["id"])
        self.assertIsNotNone(err)
        # Unknown request.
        err = app.cancel_request_by_saver(self.conn, 99999,
                                          self.daniel["id"])
        self.assertIsNotNone(err)

    def test_cancel_last_request_keeps_offer_open_all_free(self):
        offer = self._offer()
        req = self._request(offer)
        err = app.cancel_request_by_saver(self.conn, req["id"],
                                          self.daniel["id"])
        self.assertIsNone(err)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["status"], "open")
        self.assertEqual(app.seats_available(self.conn, offer), 1)

    def test_full_offer_cancel_frees_seat_waiting_list_first(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        self._sub(levi)
        offer = self._offer()  # 1 seat
        req = self._request(offer)  # fully booked now
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        sent = []
        err = app.cancel_request_by_saver(
            self.conn, req["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        # The waiting user is told first, with the waiting-list
        # message — not the plain follower message.
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"],
                         "התפנה מקום — אתה ברשימת ההמתנה")

    def test_no_free_up_notice_when_seats_were_available(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        self._sub(levi)
        # 3 seats, 2 taken: a seat was free all along.
        offer, req1, req2, _ = self._two_requests(seats=3)
        sent = []
        err = app.cancel_request_by_saver(
            self.conn, req1["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        # Nobody gets a "seat freed" push — a seat was free all along.
        # (The cancelled requester has no subscription in this test,
        # so the saver-cancel push is skipped, not sent.)
        self.assertEqual(sent, [])

    def test_my_page_shows_cancel_button_only_for_frozen(self):
        offer, req1, req2, levi = self._two_requests()
        html = self._my_html(self.daniel)
        self.assertIn('action="/cancel-request-by-saver"', html)
        self.assertIn("בטל בקשה זו", html)
        app.confirm_arrival(self.conn, req2["id"], levi["id"])
        html = self._my_html(self.daniel)
        # One frozen request left — exactly one cancel button.
        self.assertEqual(html.count("בטל בקשה זו"), 1)


class AutoRequestTest(Base):
    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _my_html(self, user):
        h = app.Handler.__new__(app.Handler)
        return h._my(self.conn, dict(user), None, None)

    def _session_in(self, minutes_from_now, duration=60):
        start = (datetime.now() + timedelta(minutes=minutes_from_now)
                 ).strftime("%H:%M")
        end = (datetime.now() + timedelta(minutes=minutes_from_now + duration)
               ).strftime("%H:%M")
        return self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], datetime.now().weekday(), start,
             end)).fetchone()

    def test_create_list_and_delete(self):
        auto, err = app.create_auto_request(self.conn, self.lev["id"],
                                            self.session["id"])
        self.assertIsNone(err, err)
        rows = self.conn.execute(
            "SELECT * FROM auto_requests WHERE user_id=?",
            (self.lev["id"],)).fetchall()
        self.assertEqual(len(rows), 1)
        err = app.delete_auto_request(self.conn, auto["id"], self.lev["id"])
        self.assertIsNone(err)
        rows = self.conn.execute(
            "SELECT * FROM auto_requests WHERE user_id=?",
            (self.lev["id"],)).fetchall()
        self.assertEqual(rows, [])

    def test_one_auto_request_per_user_and_session(self):
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        _, err = app.create_auto_request(self.conn, self.lev["id"],
                                         self.session["id"])
        self.assertIsNotNone(err)
        # A different user may auto-request the same session.
        auto, err = app.create_auto_request(
            self.conn, self.daniel["id"], self.session["id"])
        self.assertIsNone(err, err)

    def test_create_rejects_unknown_session_and_user(self):
        _, err = app.create_auto_request(self.conn, self.lev["id"], 99999)
        self.assertIsNotNone(err)
        _, err = app.create_auto_request(self.conn, 99999, self.session["id"])
        self.assertIsNotNone(err)

    def test_only_owner_can_delete(self):
        auto, _ = app.create_auto_request(self.conn, self.lev["id"],
                                          self.session["id"])
        err = app.delete_auto_request(self.conn, auto["id"],
                                      self.daniel["id"])
        self.assertIsNotNone(err)
        self.assertEqual(
            len(self.conn.execute("SELECT * FROM auto_requests").fetchall()),
            1)
        err = app.delete_auto_request(self.conn, 99999, self.lev["id"])
        self.assertIsNotNone(err)

    def test_tick_requests_when_offer_opens_and_notifies(self):
        self._sub(self.lev)
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer()
        sent = []
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(requested, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "ביקשתי בשבילך מקום")
        self.assertIn("תכנון אלגוריתמים", sent[0]["body"])
        req = self.conn.execute(
            "SELECT * FROM requests WHERE requester_id=?",
            (self.lev["id"],)).fetchone()
        self.assertIsNotNone(req)
        self.assertEqual(req["status"], "frozen")
        self.assertEqual(req["offer_id"], offer["id"])
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 2)
        # The standing request persists — it is not deleted when the
        # request resolves.
        self.assertEqual(
            len(self.conn.execute("SELECT * FROM auto_requests").fetchall()),
            1)
        # A second tick the same day does nothing: the frozen request
        # already exists.
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(requested, 0)
        self.assertEqual(len(sent), 1)

    def test_tick_notification_log_row_is_committed(self):
        # The auto-request's log row ("I requested a seat for you")
        # must survive on disk. The background thread closes its
        # connection without committing, so a row the tick does not
        # commit itself is rolled back and lost — the request still
        # happens (request_seat commits), but the record that the
        # user was told vanishes. A second connection sees only
        # committed rows, so it catches exactly that.
        self._sub(self.lev)
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer()
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None)
        self.assertEqual(requested, 1)
        other = sqlite3.connect(self.db)
        try:
            rows = other.execute(
                "SELECT * FROM notification_log WHERE user_id=?",
                (self.lev["id"],)).fetchall()
        finally:
            other.close()
        self.assertEqual(len(rows), 1)
        # Columns: id, offer_id, user_id, status, detail, created_at.
        self.assertEqual(rows[0][3], "sent")

    def test_tick_skips_no_offer_closed_window_and_own_offer(self):
        # No offer at all.
        auto, _ = app.create_auto_request(self.conn, self.lev["id"],
                                          self.session["id"])
        self.assertEqual(app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None), 0)
        # An offer that is the user's own does not count — and Lev's
        # standing request is deleted first, so it cannot fire on
        # the same tick and mask the result.
        app.delete_auto_request(self.conn, auto["id"], self.lev["id"])
        app.create_auto_request(self.conn, self.daniel["id"],
                                self.session["id"])
        self._offer(saver=self.daniel)
        self.assertEqual(app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None), 0)
        # A session whose window is not open yet.
        far = self._session_in(120)
        app.create_auto_request(self.conn, self.lev["id"], far["id"])
        self.assertEqual(app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None), 0)
        self.assertEqual(
            len(self.conn.execute("SELECT * FROM requests").fetchall()), 0)

    def test_tick_picks_offer_with_free_seat(self):
        # Two offers for the same session: the first is full, the
        # second has a seat. The request must land on the second.
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        full = self._offer(saver=self.daniel, seats=1)
        self._request(full, levi)
        free = self._offer(saver=shaked, seats=1)
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None)
        self.assertEqual(requested, 1)
        req = self.conn.execute(
            "SELECT * FROM requests WHERE requester_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(req["offer_id"], free["id"])

    def test_tick_skips_floor_and_vacation(self):
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        self._offer()
        # At the floor: freezing one more coin would go below -2.
        self.conn.execute("UPDATE users SET balance=-2 WHERE id=?",
                          (self.lev["id"],))
        self.conn.commit()
        self.assertEqual(app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None), 0)
        self.assertEqual(
            len(self.conn.execute("SELECT * FROM requests").fetchall()), 0)
        # Back above the floor, but on vacation: still skipped.
        self.conn.execute("UPDATE users SET balance=3 WHERE id=?",
                          (self.lev["id"],))
        self.conn.commit()
        app.set_vacation(self.conn, self.lev["id"], 1)
        self.assertEqual(app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None), 0)
        app.set_vacation(self.conn, self.lev["id"], 0)
        self.assertEqual(app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None), 1)

    def test_tick_does_not_rerequest_after_manual_cancel(self):
        # A manual cancel is a decision: the thread must not undo it
        # a minute later by re-freezing the coin.
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer()
        req = self._request(offer)
        app.cancel_request(self.conn, req["id"], self.lev["id"])
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None)
        self.assertEqual(requested, 0)
        self.assertEqual(
            len(self.conn.execute(
                "SELECT * FROM requests WHERE requester_id=?",
                (self.lev["id"],)).fetchall()), 1)
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_tick_does_not_rerequest_after_saver_cancel(self):
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer()
        req = self._request(offer)
        app.cancel_request_by_saver(self.conn, req["id"], self.daniel["id"])
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None)
        self.assertEqual(requested, 0)

    def test_tick_rerequests_after_offer_cancelled(self):
        # The whole offer was cancelled: the user still wants a seat,
        # and another offer for the same session may have one.
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer()
        req = self._request(offer)
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        other = self._offer(saver=shaked)
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None)
        self.assertEqual(requested, 1)
        req = self.conn.execute(
            "SELECT * FROM requests WHERE requester_id=? AND status='frozen'",
            (self.lev["id"],)).fetchone()
        self.assertEqual(req["offer_id"], other["id"])

    def test_tick_two_seats_request_only_one(self):
        # An automatic request is always exactly 1 seat, even when
        # the offer has more.
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        offer = self._offer(seats=3)
        requested = app.auto_request_tick(
            self.conn, sender=lambda sub, payload: None)
        self.assertEqual(requested, 1)
        req = self.conn.execute(
            "SELECT * FROM requests WHERE requester_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(app._request_seats(req), 1)
        self.assertEqual(app.seats_available(self.conn, offer), 2)

    def test_my_page_shows_auto_requests_and_create_form(self):
        app.create_auto_request(self.conn, self.lev["id"], self.session["id"])
        user = app.get_user(self.conn, self.lev["id"])
        html = self._my_html(user)
        self.assertIn("בקשות אוטומטיות כל שבוע", html)
        self.assertIn('action="/create-auto-request"', html)
        self.assertIn('action="/delete-auto-request"', html)
        self.assertIn("בקש אוטומטית כל שבוע", html)


class EditOfferTest(Base):
    def _edit_form_html(self, user, offer_id):
        h = app.Handler.__new__(app.Handler)
        return h._edit_offer_form(self.conn, dict(user), str(offer_id),
                                  None, None)

    def _my_html(self, user):
        h = app.Handler.__new__(app.Handler)
        return h._my(self.conn, dict(user), None, None)

    def test_edit_note_no_renotify(self):
        offer = self._offer()
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        app.save_subscription(self.conn, levi["id"], {
            "endpoint": "https://example.com/push/levi",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        sent = []
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "שורה 5, ליד החלון", 1,
                             sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        row = app.get_offer(self.conn, offer["id"])
        self.assertEqual(row["note"], "שורה 5, ליד החלון")
        self.assertEqual(row["seats_total"], 1)
        # Same offer, not a new one: followers are not re-notified.
        self.assertEqual(sent, [])

    def test_seats_raise_and_lower_to_frozen_floor(self):
        offer = self._offer(seats=1)
        req = self._request(offer)
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 3)
        self.assertIsNone(err)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["seats_total"], 3)
        # Lowering to exactly the frozen count is fine.
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 1)
        self.assertIsNone(err)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["seats_total"], 1)
        # Below the frozen count is rejected, and nothing changes.
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 0)
        self.assertIsNotNone(err)
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 11)
        self.assertIsNotNone(err)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["seats_total"], 1)

    def test_lower_below_two_frozen_seats_rejected(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        offer = self._offer(seats=3)
        self._request(offer, self.lev)
        self._request(offer, levi)
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 1)
        self.assertIsNotNone(err)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["seats_total"], 3)
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 2)
        self.assertIsNone(err)

    def test_raise_from_full_frees_seat_waiting_list_first(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        app.save_subscription(self.conn, levi["id"], {
            "endpoint": "https://example.com/push/levi",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer(seats=1)
        self._request(offer)  # fully booked now
        app.join_waiting_list(self.conn, levi["id"], offer["id"])
        sent = []
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 2,
                             sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"],
                         "התפנה מקום — אתה ברשימת ההמתנה")

    def test_edit_rejects_non_saver_closed_and_bad_values(self):
        offer = self._offer(seats=2)
        err = app.edit_offer(self.conn, offer["id"], self.lev["id"], "", 2)
        self.assertIsNotNone(err)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["note"],
            "שורה 2, ליד שקע")
        # Round 13: no zone to validate — the bad values left are
        # seat counts (0, 11, non-numeric).
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 0)
        self.assertIsNotNone(err)
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 11)
        self.assertIsNotNone(err)
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", "two")
        self.assertIsNotNone(err)
        err = app.edit_offer(self.conn, 99999, self.daniel["id"], "", 2)
        self.assertIsNotNone(err)
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "", 2)
        self.assertIsNotNone(err)

    def test_edit_form_prefills_and_enforces_saver(self):
        offer = self._offer(seats=2)
        html = self._edit_form_html(self.daniel, offer["id"])
        self.assertIn("שורה 2, ליד שקע", html)
        self.assertIn("selected", html)
        self.assertIn('action="/edit-offer"', html)
        self.assertIn("אפשר להוריד את מספר המקומות רק עד", html)
        html = self._edit_form_html(self.lev, offer["id"])
        self.assertIn("רק השומר יכול לערוך", html)
        html = self._edit_form_html(self.daniel, 99999)
        self.assertIn("לא נמצאה", html)

    def test_my_page_shows_edit_link_on_open_offers(self):
        offer = self._offer()
        html = self._my_html(self.daniel)
        self.assertIn("/edit-offer?offer_id=%s" % offer["id"], html)
        self.assertIn("ערוך", html)


class AdminResetPasswordTest(Base):
    def _admin_html(self, user):
        h = app.Handler.__new__(app.Handler)
        return h._admin(self.conn, dict(user), None, None)

    def _lev_with_password_and_sessions(self):
        app.set_password(self.conn, self.lev["id"], "lev1234")
        app.create_session(self.conn, self.lev["id"])
        app.create_session(self.conn, self.lev["id"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                (self.lev["id"],)).fetchone()[0], 2)

    def test_reset_clears_password_and_kills_sessions(self):
        self._lev_with_password_and_sessions()
        err = app.admin_reset_password(self.conn, self.daniel,
                                       self.lev["id"])
        self.assertIsNone(err)
        self.assertIsNone(
            app.get_user(self.conn, self.lev["id"])["password_hash"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                (self.lev["id"],)).fetchone()[0], 0)
        # Next login: must set a new password, the old one is dead.
        _, _, needs = app.authenticate(self.conn, "Lev", "lev1234")
        self.assertTrue(needs)
        # And the user can set a new password and log in again.
        err = app.set_password(self.conn, self.lev["id"], "newpass9")
        self.assertIsNone(err)
        user, err, needs = app.authenticate(self.conn, "Lev", "newpass9")
        self.assertIsNone(err)
        self.assertFalse(needs)
        self.assertEqual(user["id"], self.lev["id"])

    def test_reset_does_not_touch_other_users_sessions(self):
        self._lev_with_password_and_sessions()
        app.set_password(self.conn, self.daniel["id"], "daniel123")
        app.create_session(self.conn, self.daniel["id"])
        err = app.admin_reset_password(self.conn, self.daniel,
                                       self.lev["id"])
        self.assertIsNone(err)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                (self.daniel["id"],)).fetchone()[0], 1)
        user, err, needs = app.authenticate(
            self.conn, "Daniel", "daniel123")
        self.assertIsNone(err)
        self.assertFalse(needs)

    def test_non_admin_and_self_reset_rejected(self):
        self._lev_with_password_and_sessions()
        app.set_password(self.conn, self.daniel["id"], "daniel123")
        app.create_session(self.conn, self.daniel["id"])
        err = app.admin_reset_password(self.conn, self.lev,
                                       self.daniel["id"])
        self.assertIsNotNone(err)
        self.assertIsNotNone(
            app.get_user(self.conn, self.daniel["id"])["password_hash"])
        err = app.admin_reset_password(self.conn, self.daniel,
                                       self.daniel["id"])
        self.assertIsNotNone(err)
        self.assertIsNotNone(
            app.get_user(self.conn, self.daniel["id"])["password_hash"])
        # Daniel's own session survives the rejected reset.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                (self.daniel["id"],)).fetchone()[0], 1)
        err = app.admin_reset_password(self.conn, self.daniel, 99999)
        self.assertIsNotNone(err)

    def test_reset_user_without_password_or_sessions(self):
        # A user who never set a password: reset is a no-op, not an
        # error — Daniel may still want to force a re-login.
        err = app.admin_reset_password(self.conn, self.daniel,
                                       self.lev["id"])
        self.assertIsNone(err)

    def test_reset_button_hidden_for_admin_users(self):
        # Round 13: no admin user gets a reset button — not the
        # logged-in admin and not the other admin either — and the
        # server refuses to reset an admin's password too.
        daniel_bik, _ = app.create_user(self.conn, "דניאל ביק",
                                        "admin1234", "0501234567")
        html = self._admin_html(dict(daniel_bik))
        self.assertIn("יומן התראות אחרונות", html)
        non_admins = self.conn.execute(
            "SELECT COUNT(*) FROM users"
            " WHERE name NOT IN ('Daniel', 'דניאל ביק')"
            ).fetchone()[0]
        self.assertEqual(html.count("אפס סיסמה"), non_admins)
        # The seeded Daniel has no password yet — give him one
        # first, so "still set after the refused reset" means
        # something.
        app.set_password(self.conn, self.daniel["id"], "secret123")
        err = app.admin_reset_password(self.conn, dict(daniel_bik),
                                       self.daniel["id"])
        self.assertIsNotNone(err)
        self.assertIsNotNone(
            app.get_user(self.conn, self.daniel["id"])["password_hash"])

    def test_admin_page_shows_reset_button_except_for_daniel(self):
        html = self._admin_html(self.daniel)
        self.assertIn('action="/admin-reset-password"', html)
        self.assertIn("אפס סיסמה", html)
        # Exactly one button per user except Daniel himself.
        count = self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE name != 'Daniel'"
            ).fetchone()[0]
        self.assertEqual(html.count("אפס סיסמה"), count)


class Round8MigrationTest(Base):
    def test_vacation_until_column_added_to_old_db(self):
        self.conn.execute("ALTER TABLE users DROP COLUMN vacation_until")
        self.conn.commit()
        app.init_db(self.conn)
        ucols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(users)").fetchall()}
        self.assertIn("vacation_until", ucols)
        for row in self.conn.execute("SELECT vacation_until FROM users"):
            self.assertIsNone(row["vacation_until"])
        # And the app still works on the migrated DB.
        app.set_vacation(self.conn, self.lev["id"], 1)
        err = app.set_vacation_until(
            self.conn, self.lev["id"],
            (datetime.now().date() + timedelta(days=3)).isoformat())
        self.assertIsNone(err)
        self.assertTrue(app.is_on_vacation(
            app.get_user(self.conn, self.lev["id"])))

    def test_auto_requests_table_created_on_old_db(self):
        self.conn.execute("DROP TABLE auto_requests")
        self.conn.commit()
        app.init_db(self.conn)
        cols = {r[1] for r in self.conn.execute(
            "PRAGMA table_info(auto_requests)").fetchall()}
        self.assertEqual(cols, {"id", "user_id", "session_id", "created_at"})
        auto, err = app.create_auto_request(self.conn, self.lev["id"],
                                            self.session["id"])
        self.assertIsNone(err, err)


class Round8RaceTest(Base):
    """Two threads at once, one DB file, one connection each — the
    same setup as ConcurrencyTest, for the new round-8 paths."""

    def _run_two(self, fn):
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(i):
            conn = app.connect(self.db)
            barrier.wait(timeout=5)
            results[i] = fn(conn)
            conn.close()

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return results

    def _reset(self):
        self.conn.execute("DELETE FROM coin_ledger")
        self.conn.execute("DELETE FROM notification_log")
        self.conn.execute("DELETE FROM requests")
        self.conn.execute("DELETE FROM offers")
        self.conn.execute("DELETE FROM auto_requests")
        self.conn.execute("UPDATE users SET balance=3")
        self.conn.commit()

    def test_double_saver_cancel_refunds_once(self):
        for _ in range(10):
            offer = self._offer()
            req = self._request(offer)
            results = self._run_two(
                lambda c: app.cancel_request_by_saver(
                    c, req["id"], self.daniel["id"]))
            self.assertEqual(sum(1 for e in results if e is None), 1)
            self.assertEqual(
                app.get_user(self.conn, self.lev["id"])["balance"], 3)
            self._reset()

    def test_double_auto_request_tick_requests_once(self):
        for _ in range(10):
            app.create_auto_request(self.conn, self.lev["id"],
                                    self.session["id"])
            self._offer()
            results = self._run_two(
                lambda c: app.auto_request_tick(
                    c, sender=lambda sub, payload: None))
            self.assertEqual(sum(results), 1)
            self.assertEqual(
                len(self.conn.execute("SELECT * FROM requests").fetchall()),
                1)
            self._reset()

    def test_double_edit_offer_keeps_one_final_state(self):
        for _ in range(10):
            offer = self._offer(seats=1)
            results = self._run_two(
                lambda c: app.edit_offer(
                    c, offer["id"], self.daniel["id"], "x", 5))
            self.assertEqual(sum(1 for e in results if e is None), 2)
            self.assertEqual(
                app.get_offer(self.conn, offer["id"])["seats_total"], 5)
            self._reset()


class BadJwtTest(Base):
    """Round 9, item 1: a 403 BadJwtToken subscription is dead — it
    was created under a different VAPID keypair than the server's
    current one — so it is deleted just like a 410. Any other 403
    keeps retrying: it might be transient."""

    class _Resp:
        def __init__(self, status, text="", reason=None):
            self.status_code = status
            self.text = text
            self._reason = reason

        def json(self):
            if self._reason is None:
                raise ValueError("no JSON body")
            return {"reason": self._reason}

    class _JwtExc(Exception):
        def __init__(self, status, text="", reason=None, message=None):
            super().__init__(message or "push service answered %d" % status)
            self.response = BadJwtTest._Resp(status, text, reason)

    class _Bare(Exception):
        """An exception with a status code but no response object."""
        def __init__(self, status, message=None):
            super().__init__(message or "failed %d" % status)
            self.status_code = status

    def _subscribe(self, user_id):
        app.save_subscription(self.conn, user_id, {
            "endpoint": "https://example.com/push/%s" % user_id,
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_bad_jwt_detection(self):
        # Reason carried in the JSON body.
        self.assertTrue(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"BadJwtToken"}', reason="BadJwtToken")))
        # Reason only in the body text (no JSON parse).
        self.assertTrue(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"BadJwtToken"}')))
        # Reason only in the exception text (pywebpush puts the body
        # in str(exc)).
        self.assertTrue(app._subscription_is_gone(self._JwtExc(
            403, message='Push failed: 403 Forbidden\nResponse '
                         'body:{"reason":"BadJwtToken"}')))
        # No response object at all, reason in the message.
        self.assertTrue(app._subscription_is_gone(
            self._Bare(403, "Push failed: 403 BadJwtToken")))
        # A 403 for any other reason is NOT dead.
        self.assertFalse(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"Unauthorized"}', reason="Unauthorized")))
        # A bare 403 with no reason anywhere is NOT dead either.
        self.assertFalse(app._subscription_is_gone(self._JwtExc(403)))
        self.assertFalse(app._subscription_is_gone(
            self._Bare(403, "forbidden")))
        # Other statuses unchanged.
        self.assertTrue(app._subscription_is_gone(self._JwtExc(410)))
        self.assertFalse(app._subscription_is_gone(self._JwtExc(500)))

    def test_403_bad_jwt_deletes_the_subscription(self):
        self._subscribe(self.lev["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()

        def bad_jwt_sender(sub, payload):
            raise self._JwtExc(
                403, text='{"reason":"BadJwtToken"}', reason="BadJwtToken",
                message='Push failed: 403 Forbidden\nResponse '
                        'body:{"reason":"BadJwtToken"}')

        results = app.notify_followers(self.conn, offer,
                                       sender=bad_jwt_sender)
        self.assertEqual(results[0][1], "removed-dead-subscription")
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone())
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "removed-dead-subscription")
        self.assertIn("403", log["detail"])

    def test_other_403_keeps_the_subscription(self):
        self._subscribe(self.lev["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()

        def other_403_sender(sub, payload):
            raise self._JwtExc(
                403, text='{"reason":"Unauthorized"}',
                reason="Unauthorized",
                message="Push failed: 403 Forbidden")

        results = app.notify_followers(self.conn, offer,
                                       sender=other_403_sender)
        self.assertEqual(results[0][1], "failed")
        self.assertIsNotNone(self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone())

    # Round 10: Apple says the same thing with different words.
    # InvalidProviderToken is APNs' BadJwtToken - the subscription
    # was created under a different VAPID keypair - so it is dead
    # the same way. ExpiredProviderToken is NOT: pywebpush mints a
    # fresh JWT per push, so an expired provider token at send time
    # points at clock skew, not a dead subscription.
    def test_invalid_provider_token_detection(self):
        # Reason carried in the JSON body.
        self.assertTrue(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"InvalidProviderToken"}',
            reason="InvalidProviderToken")))
        # Reason only in the body text (no JSON parse).
        self.assertTrue(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"InvalidProviderToken"}')))
        # Reason only in the exception text.
        self.assertTrue(app._subscription_is_gone(self._JwtExc(
            403, message='Push failed: 403 Forbidden\nResponse '
                         'body:{"reason":"InvalidProviderToken"}')))
        # No response object at all, reason in the message.
        self.assertTrue(app._subscription_is_gone(
            self._Bare(403, "Push failed: 403 InvalidProviderToken")))
        # ExpiredProviderToken is NOT dead - in any of the three
        # places. (It never contains either dead-key reason, so it
        # cannot slip in through a substring match.)
        self.assertFalse(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"ExpiredProviderToken"}',
            reason="ExpiredProviderToken")))
        self.assertFalse(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"ExpiredProviderToken"}')))
        self.assertFalse(app._subscription_is_gone(self._JwtExc(
            403, message='Push failed: 403 ExpiredProviderToken')))
        self.assertFalse(app._subscription_is_gone(
            self._Bare(403, "Push failed: 403 ExpiredProviderToken")))
        # Another Apple reason is NOT dead either.
        self.assertFalse(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"BadDeviceToken"}',
            reason="BadDeviceToken")))
        # Google's wording still works exactly as before.
        self.assertTrue(app._subscription_is_gone(self._JwtExc(
            403, text='{"reason":"BadJwtToken"}', reason="BadJwtToken")))

    def test_403_invalid_provider_token_deletes_the_subscription(self):
        self._subscribe(self.lev["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()

        def apple_sender(sub, payload):
            raise self._JwtExc(
                403, text='{"reason":"InvalidProviderToken"}',
                reason="InvalidProviderToken",
                message='Push failed: 403 Forbidden\nResponse '
                        'body:{"reason":"InvalidProviderToken"}')

        results = app.notify_followers(self.conn, offer,
                                       sender=apple_sender)
        self.assertEqual(results[0][1], "removed-dead-subscription")
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone())
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "removed-dead-subscription")
        self.assertIn("InvalidProviderToken", log["detail"])

    def test_expired_provider_token_keeps_the_subscription(self):
        self._subscribe(self.lev["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()

        def expired_sender(sub, payload):
            raise self._JwtExc(
                403, text='{"reason":"ExpiredProviderToken"}',
                reason="ExpiredProviderToken",
                message='Push failed: 403 Forbidden\nResponse '
                        'body:{"reason":"ExpiredProviderToken"}')

        results = app.notify_followers(self.conn, offer,
                                       sender=expired_sender)
        self.assertEqual(results[0][1], "failed")
        self.assertIsNotNone(self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone())
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "failed")


class PortTest(unittest.TestCase):
    """Round 9, item 2: the port comes from the PORT environment
    variable, default 8000. A bad value falls back with a warning,
    never a crash."""

    def test_server_port(self):
        old = os.environ.pop("PORT", None)
        try:
            self.assertEqual(app._server_port(), 8000)
            os.environ["PORT"] = "9000"
            self.assertEqual(app._server_port(), 9000)
            os.environ["PORT"] = " 9001 "
            self.assertEqual(app._server_port(), 9001)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                os.environ["PORT"] = "abc"
                self.assertEqual(app._server_port(), 8000)
            self.assertIn("8000", buf.getvalue())
            os.environ["PORT"] = "0"
            self.assertEqual(app._server_port(), 8000)
            os.environ["PORT"] = "70000"
            self.assertEqual(app._server_port(), 8000)
            os.environ["PORT"] = ""
            self.assertEqual(app._server_port(), 8000)
        finally:
            if old is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = old


class SaverSelfNotifyTest(Base):
    """Round 9, item 3: the saver is already excluded from
    notifications about their own offer — in the followers path and
    in the board path. These tests prove it, so a future change
    cannot silently start notifying the saver about their own offer."""

    def _subscribe(self, user_id):
        app.save_subscription(self.conn, user_id, {
            "endpoint": "https://example.com/push/%s" % user_id,
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_saver_gets_no_notification_for_own_offer(self):
        # Daniel (saver) follows תכנון אלגוריתמים generally from the seed;
        # Lev follows it too. Both subscribed.
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        for u in (self.daniel, self.lev):
            self._subscribe(u["id"])
        offer = self._offer()
        sent = []
        app.notify_followers(self.conn, offer,
                             sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(len(sent), 1)
        self.assertIn("example.com/push/%s" % self.lev["id"],
                      sent[0]["endpoint"])
        rows = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchall()
        self.assertEqual([r["user_id"] for r in rows], [self.lev["id"]])

    def test_saver_gets_no_board_notification_for_own_offer(self):
        for u in (self.daniel, self.lev):
            self._subscribe(u["id"])
            post, err = app.post_seat_request(
                self.conn, u["id"], self.session["id"])
            self.assertIsNone(err, err)
        offer = self._offer()
        sent = []
        app.notify_board_requesters(
            self.conn, offer,
            sender=lambda sub, payload: sent.append(sub))
        self.assertEqual(len(sent), 1)
        self.assertIn("example.com/push/%s" % self.lev["id"],
                      sent[0]["endpoint"])
        rows = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchall()
        self.assertEqual([r["user_id"] for r in rows], [self.lev["id"]])


class SeedCleanupTest(Base):
    """Round 9, item 4: the one-time cleanup of the 5 seeded test
    users. It runs only once real registration has begun (someone
    has a password) — a fresh database keeps its seed users."""

    def _seed_names(self):
        return [r["name"] for r in self.conn.execute(
            "SELECT name FROM users ORDER BY id").fetchall()]

    def test_fresh_db_keeps_seed_users(self):
        self.assertEqual(
            self._seed_names(),
            ["Daniel", "Lev", "Levi", "Shaked", "Yehonatan"])
        self.assertEqual(app._remove_seed_test_users(self.conn), 0)
        app.init_db(self.conn)
        self.assertEqual(
            self._seed_names(),
            ["Daniel", "Lev", "Levi", "Shaked", "Yehonatan"])

    def test_cleanup_removes_seed_users_when_real_users_exist(self):
        noa, err = app.create_user(self.conn, "Noa Malka", "secret123", "0501234567")
        self.assertIsNone(err, err)
        self.assertEqual(app._remove_seed_test_users(self.conn), 5)
        self.assertEqual(self._seed_names(), ["Noa Malka"])
        # Their follows went with them.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0],
            0)

    def test_real_user_with_seed_name_and_password_is_kept(self):
        self.assertIsNone(app.set_password(
            self.conn, self.lev["id"], "secret123"))
        self._offer(saver=self.lev)
        app.init_db(self.conn)
        self.assertEqual(self._seed_names(), ["Lev"])
        # Lev's offer survived.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
            1)

    def test_seed_user_with_activity_is_kept(self):
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        self._offer(saver=shaked)
        noa, err = app.create_user(self.conn, "Noa Malka", "secret123", "0501234567")
        self.assertIsNone(err, err)
        app.init_db(self.conn)
        names = self._seed_names()
        self.assertIn("Shaked", names)
        self.assertIn("Noa Malka", names)
        self.assertNotIn("Daniel", names)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
            1)

    def test_cleanup_is_idempotent(self):
        noa, err = app.create_user(self.conn, "Noa Malka", "secret123", "0501234567")
        self.assertIsNone(err, err)
        self.assertEqual(app._remove_seed_test_users(self.conn), 5)
        self.assertEqual(app._remove_seed_test_users(self.conn), 0)
        app.init_db(self.conn)
        self.assertEqual(self._seed_names(), ["Noa Malka"])


class PushPayloadShapeTest(Base):
    """Round 9, item 5: every notification payload carries exactly
    the fields static/sw.js reads — title, body, url, tag — so the
    service worker never shows an empty notification. If a builder
    gains or loses a field, this test catches the drift. Round 20,
    item 11: the three reminder payloads (5-minute, 30-minute,
    arrival-confirm) carry two more — request_id and actions — so
    the Service Worker can show action buttons and post the tap.
    Those three are checked separately, below."""

    def _check(self, payload):
        self.assertEqual(set(payload.keys()),
                         {"title", "body", "url", "tag"})
        for value in payload.values():
            self.assertIsInstance(value, str)
            self.assertTrue(value)

    def _check_with_actions(self, payload, request_id, actions):
        """A reminder payload: the four display fields, plus the
        request id (an int) and the action buttons (a list of
        {action, title} pairs) — exactly what sw.js reads."""
        self.assertEqual(set(payload.keys()),
                         {"title", "body", "url", "tag",
                          "request_id", "actions"})
        for key in ("title", "body", "url", "tag"):
            self.assertIsInstance(payload[key], str)
            self.assertTrue(payload[key])
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["actions"], actions)

    def test_all_payloads_have_the_same_four_fields(self):
        offer = self._offer()
        req = self._request(offer)
        offer_d = dict(offer)
        offer_d["saver_name"] = "Daniel"
        req_d = dict(req)
        req_d["requester_name"] = "Lev"
        req_d["saver_name"] = "Daniel"
        course = dict(self.course)
        session = dict(self.session)
        self._check(app.prepare_push_payload(offer_d, course, session))
        self._check(app.prepare_board_offer_payload(
            offer_d, course, session))
        self._check(app.prepare_auto_request_payload(
            offer_d, course, session))
        self._check(app.prepare_new_session_payload(session, course))
        self._check(app.prepare_request_payload(req_d, course, session))
        self._check(app.prepare_cancel_payload(req_d, course, session))
        self._check(app.prepare_saver_cancel_request_payload(
            req_d, course, session))
        self._check(app.prepare_freed_payload(offer_d, course, session))
        self._check(app.prepare_freed_waiting_payload(
            offer_d, course, session))
        self._check(app.prepare_offer_cancelled_payload(
            offer_d, course, session))
        self._check(app.prepare_test_push_payload())
        # Round 20, item 11: the three reminder payloads carry
        # request_id + actions on top of the four display fields —
        # checked separately, so the four-field rule stays exact for
        # every other payload.
        self._check_with_actions(
            app.prepare_reminder_payload(req_d, course, session),
            req["id"],
            [{"action": "on-my-way", "title": "אני בדרך"},
             {"action": "cancel-request", "title": "ביטול"}])
        self._check_with_actions(
            app.prepare_early_reminder_payload(req_d, course, session),
            req["id"],
            [{"action": "on-my-way", "title": "אני בדרך"},
             {"action": "cancel-request", "title": "ביטול"}])
        self._check_with_actions(
            app.prepare_arrival_confirm_payload(req_d, course, session),
            req["id"],
            [{"action": "confirm-arrival", "title": "הגעתי"},
             {"action": "report-not-saved", "title": "המקום לא היה שם"}])
        self._check(app.prepare_saver_reminder_payload(
            offer_d, course, session, ["Lev"]))
        # Round 13: the edited-offer payload (zone removed from it).
        self._check(app.prepare_offer_edited_payload(
            offer_d, course, session, "שורה 5"))


class TestPushTest(Base):
    """Round 10, item 1 [TEMPORARY]: the test-push button on /push.
    One push per user per 60 seconds; the attempt is logged with
    offer_id=0; skipped attempts do not start the cooldown."""

    def _subscribe(self, user_id):
        app.save_subscription(self.conn, user_id, {
            "endpoint": "https://example.com/push/%s" % user_id,
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _fake(self, sent):
        def sender(sub, payload):
            sent.append((sub, payload))
        return sender

    def test_payload_shape(self):
        payload = app.prepare_test_push_payload()
        self.assertEqual(set(payload.keys()),
                         {"title", "body", "url", "tag"})
        for value in payload.values():
            self.assertIsInstance(value, str)
            self.assertTrue(value)
        self.assertEqual(payload["title"], "בדיקת התראות")
        self.assertEqual(payload["url"], "/push")
        self.assertTrue(payload["tag"].startswith("test-"))

    def test_first_tap_sends_and_logs(self):
        self._subscribe(self.lev["id"])
        sent = []
        ok, msg = app.send_test_push(self.conn, self.lev["id"],
                                     sender=self._fake(sent))
        self.assertTrue(ok)
        self.assertEqual(msg, "דחיפת הבדיקה נשלחה — בדוק את הטלפון.")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["title"], "בדיקת התראות")
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(log["offer_id"], 0)
        self.assertEqual(log["status"], "sent")

    def test_second_tap_inside_60s_refused(self):
        self._subscribe(self.lev["id"])
        sent = []
        now = datetime.now()
        ok, _ = app.send_test_push(self.conn, self.lev["id"],
                                   sender=self._fake(sent), now=now)
        self.assertTrue(ok)
        ok, msg = app.send_test_push(
            self.conn, self.lev["id"], sender=self._fake(sent),
            now=now + timedelta(seconds=30))
        self.assertFalse(ok)
        self.assertEqual(msg, "נסה שוב בעוד 30 שניות.")
        # No second push, no second log row.
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM notification_log").fetchone()[0], 1)

    def test_allowed_after_61s(self):
        self._subscribe(self.lev["id"])
        sent = []
        now = datetime.now()
        app.send_test_push(self.conn, self.lev["id"],
                           sender=self._fake(sent), now=now)
        ok, _ = app.send_test_push(
            self.conn, self.lev["id"], sender=self._fake(sent),
            now=now + timedelta(seconds=61))
        self.assertTrue(ok)
        self.assertEqual(len(sent), 2)

    def test_no_subscription_then_subscribe_no_cooldown(self):
        sent = []
        ok, msg = app.send_test_push(self.conn, self.lev["id"],
                                     sender=self._fake(sent))
        self.assertFalse(ok)
        self.assertEqual(msg, "אין לך התראות מופעלות — הפעל אותן קודם.")
        self.assertEqual(len(sent), 0)
        # The skipped attempt did not start a cooldown: after
        # subscribing, the same minute still works.
        self._subscribe(self.lev["id"])
        ok, _ = app.send_test_push(self.conn, self.lev["id"],
                                   sender=self._fake(sent))
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)

    def test_quiet_hours_then_clear_no_cooldown(self):
        self._subscribe(self.lev["id"])
        now = datetime.now()
        err = app.set_quiet_hours(self.conn, self.lev["id"], now.hour,
                                  (now.hour + 2) % 24)
        self.assertIsNone(err)
        sent = []
        ok, msg = app.send_test_push(self.conn, self.lev["id"],
                                     sender=self._fake(sent), now=now)
        self.assertFalse(ok)
        self.assertIn("שעות שקטות", msg)
        self.assertEqual(len(sent), 0)
        # Clearing quiet hours and retrying in the same minute works:
        # a skipped attempt never started a cooldown.
        err = app.set_quiet_hours(self.conn, self.lev["id"], "", "")
        self.assertIsNone(err)
        ok, _ = app.send_test_push(self.conn, self.lev["id"],
                                   sender=self._fake(sent), now=now)
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)

    def test_push_page_shows_button(self):
        h = app.Handler.__new__(app.Handler)
        html = h._push(self.conn, dict(self.lev), None, None)
        self.assertIn("שלח לי דחיפת בדיקה", html)
        self.assertIn('action="/test-push"', html)

    def test_failed_sender_plain_message(self):
        self._subscribe(self.lev["id"])

        def boom(sub, payload):
            raise RuntimeError("boom")

        ok, msg = app.send_test_push(self.conn, self.lev["id"],
                                     sender=boom)
        self.assertFalse(ok)
        self.assertIn("השליחה נכשלה", msg)
        self.assertIn("נסה שוב", msg)

    def test_pywebpush_missing_message(self):
        # A server-side failure must not tell the user to toggle
        # notifications — that cannot fix a missing library.
        self._subscribe(self.lev["id"])

        def missing(sub, payload):
            raise RuntimeError("pywebpush is not installed")

        ok, msg = app.send_test_push(self.conn, self.lev["id"],
                                     sender=missing)
        self.assertFalse(ok)
        self.assertIn("pip install -r requirements.txt", msg)
        self.assertNotIn("כבה", msg)

    def test_no_vapid_keys_message(self):
        self._subscribe(self.lev["id"])

        def no_keys(sub, payload):
            raise RuntimeError("אין מפתחות VAPID (הדחיפה לא הוגדרה).")

        ok, msg = app.send_test_push(self.conn, self.lev["id"],
                                     sender=no_keys)
        self.assertFalse(ok)
        self.assertIn("הפעל מחדש את השרת", msg)
        self.assertNotIn("כבה", msg)

    def test_push_goes_to_every_subscription(self):
        # Round 13, item E (verification only — no push logic
        # changed): Daniel's phone got nothing though the log said
        # "sent". Verify the test push really sends to EVERY
        # subscription of the user, and logs one row per
        # subscription, all with offer_id=0 and exactly the 4
        # fields sw.js reads.
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/phone",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/desktop",
            "keys": {"p256dh": "CCC", "auth": "DDD"}})
        sent = []
        ok, _ = app.send_test_push(self.conn, self.lev["id"],
                                   sender=self._fake(sent))
        self.assertTrue(ok)
        self.assertEqual(len(sent), 2)
        endpoints = {sub["endpoint"] for sub, _ in sent}
        self.assertEqual(endpoints, {"https://example.com/push/phone",
                                     "https://example.com/push/desktop"})
        for _, payload in sent:
            self.assertEqual(set(payload.keys()),
                             {"title", "body", "url", "tag"})
        logs = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (self.lev["id"],)).fetchall()
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertEqual(log["offer_id"], 0)
            self.assertEqual(log["status"], "sent")

    def test_safe_detail_strips_urls(self):
        self.assertEqual(
            app._safe_detail("push failed: https://example.com/push/abc"),
            "push failed: [כתובת]")
        self.assertEqual(app._safe_detail("plain text"), "plain text")
        self.assertEqual(app._safe_detail(None), "")
        self.assertEqual(app._safe_detail(""), "")


class AdminLogTest(Base):
    """Round 10, item 2 [TEMPORARY]: the visible notification log
    on /admin. Latest 50 rows, 'בדיקה' for offer_id=0, and no
    subscription endpoint can ever leak onto the page."""

    def _subscribe(self, user_id):
        app.save_subscription(self.conn, user_id, {
            "endpoint": "https://example.com/push/%s" % user_id,
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _fake(self, sent):
        def sender(sub, payload):
            sent.append((sub, payload))
        return sender

    def _admin_html(self):
        h = app.Handler.__new__(app.Handler)
        return h._admin(self.conn, dict(self.daniel), None, None)

    def test_log_section_renders(self):
        self._subscribe(self.lev["id"])
        app.send_test_push(self.conn, self.lev["id"],
                           sender=self._fake([]))
        html = self._admin_html()
        self.assertIn("יומן התראות אחרונות", html)
        self.assertIn("בדיקה", html)
        self.assertIn("נשלחה", html)
        self.assertIn("Lev", html)

    def test_real_offer_shows_offer_number(self):
        self._subscribe(self.lev["id"])
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        offer = self._offer()
        app.notify_followers(self.conn, offer, sender=self._fake([]))
        html = self._admin_html()
        self.assertIn("הצעה %s" % offer["id"], html)
        self.assertIn("נשלחה", html)

    def test_endpoint_url_never_renders(self):
        self.conn.execute(
            "INSERT INTO notification_log (user_id, offer_id, status,"
            " detail, created_at) VALUES (?, 0, 'failed',"
            " 'push failed: https://example.com/push/secret-abc123',"
            " '2026-11-09T10:00:00')", (self.lev["id"],))
        self.conn.commit()
        html = self._admin_html()
        self.assertNotIn("example.com", html)
        self.assertNotIn("secret-abc123", html)
        self.assertIn("[כתובת]", html)

    def test_empty_log_message(self):
        html = self._admin_html()
        self.assertIn("אין ניסיונות דחיפה עדיין.", html)

    def test_recent_notification_log_limit(self):
        for i in range(55):
            self.conn.execute(
                "INSERT INTO notification_log (user_id, offer_id,"
                " status, detail, created_at) VALUES (?, 0, 'sent',"
                " 'ok', '2026-11-09T10:00:00')", (self.lev["id"],))
        self.conn.commit()
        rows = app.recent_notification_log(self.conn)
        self.assertEqual(len(rows), 50)
        self.assertGreater(rows[0]["id"], rows[-1]["id"])
        self.assertEqual(rows[0]["user_name"], "Lev")

    def test_log_status_labels(self):
        self.assertEqual(app._log_status_label("sent"), "נשלחה")
        self.assertEqual(app._log_status_label("failed"), "נכשלה")
        self.assertEqual(app._log_status_label("skipped-no-subscription"),
                         "דולגה — אין מנוי")
        self.assertEqual(app._log_status_label("skipped-quiet-hours"),
                         "דולגה — שעות שקטות")
        self.assertEqual(app._log_status_label("skipped-vacation"),
                         "דולגה — חופשה")
        self.assertEqual(app._log_status_label("removed-dead-subscription"),
                         "מנוי הוסר")
        self.assertEqual(app._log_status_label("weird-status"),
                         "weird-status")


class CancelFrozenNotifyTest(Base):
    """Round 10, item 3: when the saver cancels the WHOLE offer,
    every frozen requester gets one push — and the refund still
    happens. Resolved requesters get nothing."""

    def _subscribe(self, user_id):
        app.save_subscription(self.conn, user_id, {
            "endpoint": "https://example.com/push/%s" % user_id,
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _fake(self, sent):
        def sender(sub, payload):
            sent.append((sub, payload))
        return sender

    def _user(self, name):
        return self.conn.execute(
            "SELECT * FROM users WHERE name=?", (name,)).fetchone()

    def _setup_offer(self):
        """3 seats: Lev takes 2, Yehonatan takes 1 then cancels his
        own request (resolved), Levi takes the freed seat, Shaked
        queues on the waiting list (offer is full)."""
        levi = self._user("Levi")
        shaked = self._user("Shaked")
        yehonatan = self._user("Yehonatan")
        offer = self._offer(seats=3)
        _, err = app.request_seat(self.conn, self.lev["id"],
                                  offer["id"], seats=2)
        self.assertIsNone(err)
        req_y, err = app.request_seat(self.conn, yehonatan["id"],
                                      offer["id"])
        self.assertIsNone(err)
        err = app.cancel_request(self.conn, req_y["id"],
                                 yehonatan["id"])
        self.assertIsNone(err)
        _, err = app.request_seat(self.conn, levi["id"], offer["id"])
        self.assertIsNone(err)
        # The waiting list is for followers of the course only.
        app.add_follow(self.conn, shaked["id"], self.course["id"])
        _, err = app.join_waiting_list(self.conn, shaked["id"],
                                       offer["id"])
        self.assertIsNone(err)
        for u in (self.lev, levi, shaked, yehonatan):
            self._subscribe(u["id"])
        return offer, levi, shaked, yehonatan

    def test_frozen_requesters_notified_and_refunded(self):
        offer, levi, shaked, yehonatan = self._setup_offer()
        sent = []
        err = app.cancel_offer(self.conn, offer["id"],
                               self.daniel["id"],
                               sender=self._fake(sent))
        self.assertIsNone(err)
        # Refunds: Lev back to 3 (was 1), Levi back to 3 (was 2).
        self.assertEqual(app.get_user(self.conn, self.lev["id"])
                         ["balance"], 3)
        self.assertEqual(app.get_user(self.conn, levi["id"])
                         ["balance"], 3)
        # Exactly 3 pushes, one per person: Lev (2 seats, one push),
        # Levi, Shaked (waiting list). Yehonatan: nothing.
        self.assertEqual(len(sent), 3)
        endpoints = [s[0]["endpoint"] for s in sent]
        self.assertIn("https://example.com/push/%s" % self.lev["id"],
                      endpoints)
        self.assertIn("https://example.com/push/%s" % levi["id"],
                      endpoints)
        self.assertIn("https://example.com/push/%s" % shaked["id"],
                      endpoints)
        self.assertNotIn(
            "https://example.com/push/%s" % yehonatan["id"], endpoints)
        for _, payload in sent:
            self.assertEqual(payload["title"], "ההצעה בוטלה")
            self.assertIn("תכנון אלגוריתמים", payload["body"])
        # The log agrees: 3 sent rows for this offer, none for
        # Yehonatan. (One more row exists for the offer - the
        # 'failed' row for Daniel from Yehonatan's own cancel in
        # the setup, sent with no fake sender and no VAPID keys -
        # so count the sent rows, not all rows.)
        logs = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchall()
        sent_logs = [l for l in logs if l["status"] == "sent"]
        self.assertEqual(len(sent_logs), 3)
        self.assertEqual(
            {l["user_id"] for l in sent_logs},
            {self.lev["id"], levi["id"], shaked["id"]})
        self.assertNotIn(yehonatan["id"], [l["user_id"] for l in logs])

    def test_frozen_requester_who_is_also_waiter_gets_one_push(self):
        offer, levi, shaked, yehonatan = self._setup_offer()
        # Lev is frozen AND on the waiting list: the DB normally
        # forbids that (join_waiting_list rejects a frozen
        # requester), so insert the row directly to test the dedup.
        self.conn.execute(
            "INSERT INTO waiting_list (offer_id, user_id, created_at)"
            " VALUES (?, ?, ?)",
            (offer["id"], self.lev["id"],
             datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        sent = []
        err = app.cancel_offer(self.conn, offer["id"],
                               self.daniel["id"],
                               sender=self._fake(sent))
        self.assertIsNone(err)
        endpoints = [s[0]["endpoint"] for s in sent]
        self.assertEqual(
            endpoints.count("https://example.com/push/%s"
                            % self.lev["id"]), 1)
        self.assertEqual(len(sent), 3)

    def test_quiet_hours_frozen_requester_skipped_but_refunded(self):
        offer, levi, shaked, yehonatan = self._setup_offer()
        now = datetime.now()
        err = app.set_quiet_hours(self.conn, self.lev["id"], now.hour,
                                  (now.hour + 2) % 24)
        self.assertIsNone(err)
        sent = []
        err = app.cancel_offer(self.conn, offer["id"],
                               self.daniel["id"],
                               sender=self._fake(sent), now=now)
        self.assertIsNone(err)
        endpoints = [s[0]["endpoint"] for s in sent]
        self.assertNotIn("https://example.com/push/%s" % self.lev["id"],
                         endpoints)
        self.assertEqual(len(sent), 2)
        # Lev is still refunded even though the push was skipped.
        self.assertEqual(app.get_user(self.conn, self.lev["id"])
                         ["balance"], 3)
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=? AND"
            " user_id=?", (offer["id"], self.lev["id"])).fetchone()
        self.assertEqual(log["status"], "skipped-quiet-hours")


class EditNotifyTest(Base):
    """Round 11 item 1: a frozen requester is told when the saver
    edits the offer — the note changed, so the seat they asked
    for is not quite the seat they asked for any more. (Round 13
    removed zones, so a note change is the only trigger left.)"""

    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_note_change_notifies_frozen_requester_only(self):
        offer = self._offer()
        self._request(offer)  # Lev, frozen
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        self._sub(self.lev)
        self._sub(levi)
        self._sub(self.daniel)
        sent = []
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "שורה 5, ליד החלון", 1,
                             sender=lambda sub, payload: sent.append(
                                 (sub["endpoint"], payload)))
        self.assertIsNone(err)
        # Exactly one push, to the frozen requester only — not to
        # the follower (Levi) and not to the saver themself.
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            sent[0][0], "https://example.com/push/%s" % self.lev["id"])
        self.assertEqual(sent[0][1]["title"], "ההצעה עודכנה")
        self.assertIn("שורה 5, ליד החלון", sent[0][1]["body"])

    def test_note_only_change_notifies_too(self):
        offer = self._offer()
        self._request(offer)
        self._sub(self.lev)
        sent = []
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "שורה 9, ליד הדלת", 1,
                             sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        self.assertIn("שורה 9, ליד הדלת", sent[0]["body"])

    def test_seats_only_change_stays_silent(self):
        offer = self._offer(seats=2)
        self._request(offer)  # 1 of 2 seats: not full, no free-up
        self._sub(self.lev)
        sent = []
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "שורה 2, ליד שקע", 3,
                             sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_no_change_stays_silent(self):
        offer = self._offer()
        self._request(offer)
        self._sub(self.lev)
        sent = []
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "שורה 2, ליד שקע", 1,
                             sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_two_seat_request_gets_one_push(self):
        offer = self._offer(seats=2)
        req, err = app.request_seat(self.conn, self.lev["id"],
                                    offer["id"], seats=2)
        self.assertIsNone(err, err)
        self._sub(self.lev)
        sent = []
        # Round 13: the note must actually change — the old test
        # relied on a zone change, which no longer exists.
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "שורה 5, ליד החלון", 2,
                             sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)

    def test_resolved_requester_gets_nothing(self):
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self._sub(self.lev)
        sent = []
        err = app.edit_offer(self.conn, offer["id"], self.daniel["id"], "שורה 2, ליד שקע", 1,
                             sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(sent, [])


class VacationCancelTest(Base):
    """Round 11 item 2: turning vacation ON cancels the user's own
    frozen requests — refund at once, seat freed, saver and
    requester told."""

    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _fake(self, sent):
        return lambda sub, payload: sent.append((sub["endpoint"], payload))

    def test_vacation_on_refunds_and_notifies_both(self):
        offer = self._offer()
        req = self._request(offer)
        self._sub(self.daniel)
        self._sub(self.lev)
        sent = []
        err = app.set_vacation(self.conn, self.lev["id"], 1,
                               sender=self._fake(sent))
        self.assertIsNone(err)
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled_by_requester")
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)
        self.assertEqual(
            app.get_offer(self.conn, offer["id"])["status"], "open")
        self.assertEqual(
            app.seats_available(self.conn,
                                app.get_offer(self.conn, offer["id"])), 1)
        # One push to the saver ("request cancelled"), one to the
        # requester themself ("cancelled because you turned vacation
        # on"). No free-up push: nobody follows or waits here.
        by_endpoint = {e: p for e, p in sent}
        self.assertEqual(len(sent), 2)
        daniel_push = by_endpoint[
            "https://example.com/push/%s" % self.daniel["id"]]
        self.assertEqual(daniel_push["title"], "הבקשה בוטלה")
        lev_push = by_endpoint[
            "https://example.com/push/%s" % self.lev["id"]]
        self.assertEqual(lev_push["title"], "הבקשה בוטלה")
        self.assertIn("חופשה", lev_push["body"])

    def test_vacation_on_frees_full_offer_waiting_list_first(self):
        offer = self._offer()  # 1 seat
        self._request(offer)   # Lev takes it — fully booked
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        app.add_follow(self.conn, levi["id"], self.course["id"])
        self._sub(levi)
        row, err = app.join_waiting_list(
            self.conn, levi["id"], offer["id"])
        self.assertIsNone(err, err)
        self._sub(self.daniel)
        self._sub(self.lev)
        sent = []
        err = app.set_vacation(self.conn, self.lev["id"], 1,
                               sender=self._fake(sent))
        self.assertIsNone(err)
        by_endpoint = {e: p for e, p in sent}
        # The waiter is told first, with the waiting-list message.
        self.assertEqual(
            by_endpoint["https://example.com/push/%s" % levi["id"]]
            ["title"], "התפנה מקום — אתה ברשימת ההמתנה")
        # The saver and the requester are told too.
        self.assertIn("https://example.com/push/%s" % self.daniel["id"],
                      by_endpoint)
        self.assertIn("https://example.com/push/%s" % self.lev["id"],
                      by_endpoint)

    def test_other_users_requests_untouched(self):
        offer = self._offer(seats=2)
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        req_lev = self._request(offer)
        req_levi, err = app.request_seat(self.conn, levi["id"],
                                         offer["id"])
        self.assertIsNone(err, err)
        app.set_vacation(self.conn, self.lev["id"], 1)
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req_lev["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled_by_requester")
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req_levi["id"],)).fetchone()
        self.assertEqual(row["status"], "frozen")
        self.assertEqual(
            app.get_user(self.conn, levi["id"])["balance"], 2)
        self.assertEqual(
            app.get_user(self.conn, self.daniel["id"])["balance"], 3)

    def test_vacation_off_does_nothing_to_requests(self):
        offer = self._offer()
        req = self._request(offer)
        # Turning vacation off while it is already off: nothing.
        app.set_vacation(self.conn, self.lev["id"], 0)
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertEqual(row["status"], "frozen")
        # On cancels it; off again does not resurrect it.
        app.set_vacation(self.conn, self.lev["id"], 1)
        app.set_vacation(self.conn, self.lev["id"], 0)
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled_by_requester")
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_vacation_confirmation_arrives_despite_quiet_hours(self):
        # The confirmation is a forced push: the user just tapped
        # the button, so quiet hours and vacation must not suppress
        # the very confirmation of that tap.
        offer = self._offer()
        self._request(offer)
        now = datetime.now()
        err = app.set_quiet_hours(self.conn, self.lev["id"], now.hour,
                                  (now.hour + 2) % 24)
        self.assertIsNone(err)
        self._sub(self.lev)
        sent = []
        err = app.set_vacation(self.conn, self.lev["id"], 1,
                               sender=self._fake(sent))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        self.assertIn("חופשה", sent[0][1]["body"])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(log["status"], "sent")

    def _session_in(self, minutes_from_now, duration=60):
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today,
             _mins_from_now(minutes_from_now),
             _mins_from_now(minutes_from_now + duration))).fetchone()
        self.conn.commit()
        return s

    def _offer_for(self, session, saver=None, seats=1):
        offer, err = app.create_offer(
            self.conn, (saver or self.daniel)["id"], session["id"], "שורה 2, ליד שקע", seats)
        self.assertIsNone(err, err)
        return offer

    def test_vacation_on_cancels_all_frozen_requests(self):
        # Two frozen requests on two different offers: both are
        # cancelled, both coins come back, both savers are told.
        offer1 = self._offer()
        offer2 = self._offer_for(self._session_in(40))
        req1 = self._request(offer1)
        req2 = self._request(offer2)
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 1)
        self._sub(self.daniel)
        self._sub(self.lev)
        sent = []
        err = app.set_vacation(self.conn, self.lev["id"], 1,
                               sender=self._fake(sent))
        self.assertIsNone(err)
        for req in (req1, req2):
            row = self.conn.execute(
                "SELECT status FROM requests WHERE id=?",
                (req["id"],)).fetchone()
            self.assertEqual(row["status"], "cancelled_by_requester")
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)
        # Two pushes to the saver (one per request), two to the
        # requester themself (one per request).
        daniel_pushes = [p for e, p in sent
                         if e == "https://example.com/push/%s"
                         % self.daniel["id"]]
        lev_pushes = [p for e, p in sent
                      if e == "https://example.com/push/%s"
                      % self.lev["id"]]
        self.assertEqual(len(daniel_pushes), 2)
        self.assertEqual(len(lev_pushes), 2)

    def test_vacation_on_twice_refunds_once(self):
        offer = self._offer()
        self._request(offer)
        self._sub(self.daniel)
        self._sub(self.lev)
        sent = []
        app.set_vacation(self.conn, self.lev["id"], 1,
                         sender=self._fake(sent))
        # First turn-on: saver push + requester confirmation.
        self.assertEqual(len(sent), 2)
        # Second turn-on: nothing left to cancel, nothing sent, and
        # the coin is still refunded exactly once.
        sent.clear()
        app.set_vacation(self.conn, self.lev["id"], 1,
                         sender=self._fake(sent))
        self.assertEqual(sent, [])
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_vacation_on_with_no_frozen_requests_is_quiet(self):
        self._sub(self.lev)
        sent = []
        err = app.set_vacation(self.conn, self.lev["id"], 1,
                               sender=self._fake(sent))
        self.assertIsNone(err)
        self.assertEqual(sent, [])

    def test_vacation_on_does_not_touch_resolved_requests(self):
        offer = self._offer()
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self.assertEqual(
            app.get_user(self.conn, self.daniel["id"])["balance"], 4)
        app.set_vacation(self.conn, self.lev["id"], 1)
        row = self.conn.execute(
            "SELECT status FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(
            app.get_user(self.conn, self.daniel["id"])["balance"], 4)
        self.assertEqual(
            app.get_user(self.conn, self.lev["id"])["balance"], 2)

    def _run_two(self, fn):
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(i):
            conn = app.connect(self.db)
            barrier.wait(timeout=5)
            results[i] = fn(conn)
            conn.close()

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return results

    def test_vacation_cancel_races_arrival_confirm(self):
        # One winner only: either the arrival is confirmed and the
        # saver is paid, or the vacation cancel refunds the coin —
        # never both, never neither.
        for _ in range(10):
            offer = self._offer()
            req = self._request(offer)
            results = self._run_two(lambda c: (
                app.set_vacation(
                    c, self.lev["id"], 1,
                    sender=lambda sub, payload: None),
                app.confirm_arrival(c, req["id"], self.lev["id"])))
            status = self.conn.execute(
                "SELECT status FROM requests WHERE id=?",
                (req["id"],)).fetchone()["status"]
            self.assertIn(status, ("confirmed", "cancelled_by_requester"))
            lev = app.get_user(self.conn, self.lev["id"])["balance"]
            daniel = app.get_user(self.conn,
                                  self.daniel["id"])["balance"]
            if status == "confirmed":
                self.assertEqual((lev, daniel), (2, 4))
            else:
                self.assertEqual((lev, daniel), (3, 3))
            # Exactly one resolution in the ledger: frozen, then
            # either the payment or the refund — never two.
            rows = self.conn.execute(
                "SELECT * FROM coin_ledger WHERE request_id=?",
                (req["id"],)).fetchall()
            self.assertEqual(len(rows), 2)
            self.conn.execute("DELETE FROM coin_ledger")
            self.conn.execute("DELETE FROM notification_log")
            self.conn.execute("DELETE FROM requests")
            self.conn.execute("DELETE FROM offers")
            self.conn.execute("UPDATE users SET balance=3,"
                              " on_vacation=0")
            self.conn.commit()


class OnTheWayNotifyTest(Base):
    """Round 11 item 3: tapping "I'm on my way" pushes the saver."""

    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_on_my_way_pushes_saver(self):
        offer = self._offer()
        req = self._request(offer)
        self._sub(self.daniel)
        sent = []
        err = app.mark_on_my_way(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        self.assertIn("בדרך לשיעור", sent[0]["title"])
        self.assertIn("Lev", sent[0]["title"])
        self.assertIn("תכנון אלגוריתמים", sent[0]["body"])
        # Informational only: still frozen, balances unchanged.
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "frozen")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)
        self.assertEqual(
            app.get_user(self.conn, self.daniel["id"])["balance"], 3)

    def test_on_my_way_second_tap_pushes_again(self):
        offer = self._offer()
        req = self._request(offer)
        self._sub(self.daniel)
        sent = []
        t1 = datetime.now() - timedelta(minutes=5)
        self.assertIsNone(app.mark_on_my_way(
            self.conn, req["id"], self.lev["id"], now=t1,
            sender=lambda sub, payload: sent.append(payload)))
        t2 = datetime.now()
        self.assertIsNone(app.mark_on_my_way(
            self.conn, req["id"], self.lev["id"], now=t2,
            sender=lambda sub, payload: sent.append(payload)))
        self.assertEqual(len(sent), 2)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["on_the_way"], t2.isoformat(timespec="seconds"))

    def test_on_my_way_saver_without_subscription_logs_skip(self):
        offer = self._offer()
        req = self._request(offer)
        # Daniel never subscribed: the push is skipped, logged, and
        # the tap still succeeds.
        err = app.mark_on_my_way(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err)
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-no-subscription")

    def test_on_my_way_rejected_tap_sends_nothing(self):
        offer = self._offer()
        req = self._request(offer)
        self._sub(self.daniel)
        sent = []
        # The saver cannot tap it for the requester.
        err = app.mark_on_my_way(
            self.conn, req["id"], self.daniel["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNotNone(err)
        self.assertEqual(sent, [])
        # A resolved request cannot be tapped either.
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        err = app.mark_on_my_way(
            self.conn, req["id"], self.lev["id"],
            sender=lambda sub, payload: sent.append(payload))
        self.assertIsNotNone(err)
        self.assertEqual(sent, [])


class EarlyReminderTest(Base):
    """Round 11 item 5: the 30-minute "leave now" reminder."""

    def _session_in(self, minutes_from_now, duration=60):
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today,
             _mins_from_now(minutes_from_now),
             _mins_from_now(minutes_from_now + duration))).fetchone()
        self.conn.commit()
        return s

    def _offer_for(self, session, saver=None, seats=1):
        offer, err = app.create_offer(
            self.conn, (saver or self.daniel)["id"], session["id"], "שורה 2, ליד שקע", seats)
        self.assertIsNone(err, err)
        return offer

    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def test_early_reminder_fires_at_thirty_minutes_once(self):
        session = self._session_in(40)
        offer = self._offer_for(session)
        self._request(offer)  # created now: 40 min before class
        self._sub(self.lev)
        sent = []
        now = datetime.now() + timedelta(minutes=10)  # class in 30 min
        self.assertEqual(
            [r["id"] for r in app.find_early_reminders_due(self.conn, now)],
            [self.conn.execute("SELECT id FROM requests").fetchone()["id"]])
        count = app.early_reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=now)
        self.assertEqual(count, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "צא עכשיו")
        self.assertIn("תכנון אלגוריתמים", sent[0]["body"])
        row = self.conn.execute(
            "SELECT reminded_30_at FROM requests").fetchone()
        self.assertIsNotNone(row["reminded_30_at"])
        # Second tick, same minute: no double reminder.
        count = app.early_reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=now)
        self.assertEqual(count, 0)
        self.assertEqual(len(sent), 1)

    def test_early_reminder_not_due_after_class_started(self):
        session = self._session_in(40)
        offer = self._offer_for(session)
        self._request(offer)
        # Class started 5 minutes ago: too late, not due.
        now = datetime.now() + timedelta(minutes=45)
        self.assertEqual(app.find_early_reminders_due(self.conn, now), [])

    def test_early_reminder_not_due_for_base_session(self):
        # The base session starts in 10 minutes: the 30-minute
        # point already passed before the request existed.
        offer = self._offer()
        self._request(offer)
        now = datetime.now()
        self.assertEqual(app.find_early_reminders_due(self.conn, now), [])
        self.assertEqual(
            app.find_early_reminders_due(
                self.conn, now + timedelta(minutes=5)), [])

    def test_early_reminder_skipped_when_request_created_late(self):
        session = self._session_in(40)
        offer = self._offer_for(session)
        # Request created 15 minutes before class: the 30-minute
        # point never existed for it.
        req, err = app.request_seat(
            self.conn, self.lev["id"], offer["id"],
            now=datetime.now() + timedelta(minutes=25))
        self.assertIsNone(err, err)
        now = datetime.now() + timedelta(minutes=30)  # class in 10 min
        self.assertEqual(app.find_early_reminders_due(self.conn, now), [])
        # The 5-minute reminder still applies to it.
        due = app.find_reminders_due(
            self.conn, datetime.now() + timedelta(minutes=35))
        self.assertEqual([r["id"] for r in due], [req["id"]])

    def test_early_reminder_not_sent_for_resolved_request(self):
        session = self._session_in(40)
        offer = self._offer_for(session)
        req = self._request(offer)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        now = datetime.now() + timedelta(minutes=10)
        self.assertEqual(app.find_early_reminders_due(self.conn, now), [])
        # Same for a cancelled request.
        offer2 = self._offer_for(self._session_in(40))
        req2 = self._request(offer2)
        app.cancel_request(self.conn, req2["id"], self.lev["id"])
        self.assertEqual(app.find_early_reminders_due(self.conn, now), [])

    def test_early_reminder_flag_survives_restart(self):
        session = self._session_in(40)
        offer = self._offer_for(session)
        self._request(offer)
        self._sub(self.lev)
        now = datetime.now() + timedelta(minutes=10)
        count = app.early_reminder_tick(
            self.conn, sender=lambda sub, payload: None, now=now)
        self.assertEqual(count, 1)
        # A fresh connection (a restart) sees the flag and does not
        # remind again.
        conn2 = app.connect(self.db)
        row = conn2.execute(
            "SELECT reminded_30_at FROM requests").fetchone()
        self.assertIsNotNone(row["reminded_30_at"])
        self.assertEqual(app.find_early_reminders_due(conn2, now), [])
        conn2.close()

    def test_both_reminders_fire_for_same_request(self):
        session = self._session_in(40)
        offer = self._offer_for(session)
        self._request(offer)
        self._sub(self.lev)
        sent = []
        count = app.early_reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=datetime.now() + timedelta(minutes=10))
        self.assertEqual(count, 1)
        self.assertEqual(sent[0]["title"], "צא עכשיו")
        count = app.reminder_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=datetime.now() + timedelta(minutes=35))
        self.assertEqual(count, 1)
        self.assertIn("5 דקות", sent[1]["title"])

    def test_migration_adds_reminded_30_at_column(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "old.db")
        conn = app.connect(db)
        schema_old = app.SCHEMA.replace(
            "    reminded_at TEXT,\n    reminded_30_at TEXT",
            "    reminded_at TEXT")
        conn.executescript(schema_old)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        self.assertNotIn("reminded_30_at", cols)
        app.init_db(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        self.assertIn("reminded_30_at", cols)
        conn.close()
        tmp.cleanup()


class HelpChecklistTest(Base):
    """Round 11 item 4: the "notification didn't arrive?" checklist
    on /help, and the updated help lines for the other items."""

    def _help_html(self):
        h = app.Handler.__new__(app.Handler)
        # Collapse whitespace: the page source wraps long lines, so
        # a phrase can span a line break in the HTML.
        return " ".join(h._help(self.conn, dict(self.daniel), None, None)
                        .split())

    def test_help_has_troubleshooting_checklist(self):
        html = self._help_html()
        self.assertIn("התראה לא הגיעה", html)
        self.assertIn("מסך הבית", html)
        self.assertIn("נא לא להפריע", html)
        # Daniel's own wording (2026-11-10): the checklist is now
        # just these 2 items. He dropped the locked-phone line,
        # the old-subscription line, and the test-button line
        # (the test button itself still exists on /push — it is
        # TEMPORARY and goes away after the pilot).
        self.assertNotIn("נעול", html)
        self.assertNotIn("התראת בדיקה", html)
        self.assertNotIn("מנוי ישן", html)
        checks = html.split("<h2>התראה לא הגיעה? מה לבדוק</h2>")[1]
        self.assertEqual(checks.count("<li>"), 2)

    def test_help_mentions_new_round_11_behaviour(self):
        html = self._help_html()
        # Daniel's own FAQ wording (2026-11-10) dropped the
        # vacation-cancels sentence from "איך משתיקים התראות?" —
        # the behaviour itself is unchanged (vacation-on still
        # cancels frozen requests, covered by the vacation tests),
        # it is just no longer documented on /help.
        self.assertNotIn("בקשות מוקפאות שלך מתבטלות ברגע שמדליקים חופשה",
                         html)
        # Rewrite 2026-11-10 (Daniel): the /help page was shortened
        # significantly. The 30-minute reminder, the on-the-way push
        # and the edit notification still WORK, but they are no
        # longer explained on /help — the details are obvious from
        # the UI, and Daniel asked for the page to stop rambling.
        self.assertNotIn("צא עכשיו", html)
        self.assertNotIn("ומקבל גם התראה על זה", html)
        self.assertNotIn("מי שכן מקבל התראה על עריכה הוא מי שכבר ביקש מקום",
                         html)


class DeclaredLateTest(Base):
    """Round 12 item 3: declared late — the requester can say "I'm
    late" and the no-show deadline moves from start+5 to start+10
    (Daniel changed the window from 15 to 10 minutes)."""

    def _sub(self, user):
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/%s" % user["id"],
            "keys": {"p256dh": "AAA", "auth": "BBB"}})

    def _start(self, offer):
        session = app.get_session(self.conn, offer["session_id"])
        return app._session_start_datetime(dict(offer), dict(session))

    def _row(self, req_id):
        return self.conn.execute("SELECT * FROM requests WHERE id=?",
                                 (req_id,)).fetchone()

    def _declare(self, req, now=None, sender=None):
        return app.declare_late(self.conn, req["id"], self.lev["id"],
                                now=now, sender=sender)

    def _decide(self, req, decision, now=None):
        return app.decide_late(self.conn, req["id"], self.daniel["id"],
                               decision, now=now)

    def test_settle_revisits_expired_offers(self):
        """Regression (found in round 12): an offer expires at class
        start, but its frozen requests are only refunded at their
        deadline (start+5). A pass at start+3 expires the offer;
        the refund is due on a LATER pass — and the old code only
        ever looked at open offers, so the coin stayed frozen
        forever. The deadline is now checked on expired offers
        too, so the refund fires on the second pass."""
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        app.settle_expired(self.conn, now=start + timedelta(minutes=3))
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        self.assertEqual(self._row(req["id"])["status"], "no_show")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_declare_after_class_start_before_plus_five(self):
        """The point of declaring late: class already started (the
        offer expired on an earlier pass), but the +5 deadline has
        not passed yet — the declaration still lands."""
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        app.settle_expired(self.conn, now=start + timedelta(minutes=1))
        self.assertEqual(
            self.conn.execute("SELECT status FROM offers WHERE id=?",
                              (offer["id"],)).fetchone()["status"],
            "expired")
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        self.assertIsNone(self._declare(
            req, now=start + timedelta(minutes=3)))
        # Deadline is now +10: still frozen at +6, refunded at +11.
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        app.settle_expired(self.conn, now=start + timedelta(minutes=11))
        self.assertEqual(self._row(req["id"])["status"], "no_show")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_declare_pauses_expiry_until_plus_ten(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        # At start+6 the old deadline (+5) is past — still frozen.
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)
        # At start+11 the new deadline (+10) is past — refunded.
        app.settle_expired(self.conn, now=start + timedelta(minutes=11))
        self.assertEqual(self._row(req["id"])["status"], "no_show")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_accept_extends_deadline_and_arrival_by_plus_ten_pays(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        self.assertIsNone(self._decide(req, "accept", now=start))
        self.assertEqual(self._row(req["id"])["late_status"], "accepted")
        # Still frozen at +6, so arrival at +8 is in time.
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        err = app.confirm_arrival(self.conn, req["id"], self.lev["id"],
                                  now=start + timedelta(minutes=8))
        self.assertIsNone(err)
        self.assertEqual(self._row(req["id"])["status"], "confirmed")
        self.assertEqual(
            app.get_user(self.conn, self.daniel["id"])["balance"], 4)
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)

    def test_no_arrival_by_plus_ten_refunds(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        self.assertIsNone(self._decide(req, "accept", now=start))
        app.settle_expired(self.conn, now=start + timedelta(minutes=11))
        self.assertEqual(self._row(req["id"])["status"], "no_show")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        self.assertEqual(
            app.get_user(self.conn, self.daniel["id"])["balance"], 3)

    def test_undecided_expires_at_plus_ten(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        # At +9 the saver still has not decided — still frozen.
        app.settle_expired(self.conn, now=start + timedelta(minutes=9))
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        # At +10 with no decision, the request expires.
        app.settle_expired(self.conn, now=start + timedelta(minutes=10))
        self.assertEqual(self._row(req["id"])["status"], "no_show")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_refuse_refunds_immediately_with_late_declined(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        self.assertIsNone(self._decide(req, "refuse", now=start))
        row = self._row(req["id"])
        self.assertEqual(row["status"], "late_declined")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)
        self.assertEqual(app.seats_available(self.conn, offer), 1)
        entry = self.conn.execute(
            "SELECT * FROM coin_ledger WHERE user_id=? AND amount=1",
            (self.lev["id"],)).fetchone()
        self.assertEqual(entry["reason"], "late_declined")

    def test_declare_after_plus_five_impossible(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        # The time check alone rejects it, even before any settle.
        err = self._declare(req, now=start + timedelta(minutes=6))
        self.assertIsNotNone(err)
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        # And once the expiry has fired, the request is resolved.
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        self.assertEqual(self._row(req["id"])["status"], "no_show")
        err = self._declare(req, now=start + timedelta(minutes=6))
        self.assertIsNotNone(err)

    def test_double_declare_impossible(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        err = self._declare(req, now=start - timedelta(minutes=1))
        self.assertIsNotNone(err)
        # And no re-declare after an acceptance either.
        self.assertIsNone(self._decide(req, "accept", now=start))
        err = self._declare(req, now=start)
        self.assertIsNotNone(err)

    def test_declare_pushes_saver_with_four_fields(self):
        offer = self._offer()
        req = self._request(offer)
        self._sub(self.daniel)
        sent = []
        err = self._declare(
            req, sender=lambda sub, payload: sent.append(payload))
        self.assertIsNone(err)
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(sent[0].keys()),
                         {"title", "body", "url", "tag"})
        self.assertIn("מאחר לשיעור", sent[0]["title"])
        self.assertIn("Lev", sent[0]["title"])
        self.assertIn("תכנון אלגוריתמים", sent[0]["body"])
        # Informational only: still frozen, balances unchanged.
        self.assertEqual(self._row(req["id"])["status"], "frozen")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 2)

    def test_declare_only_requester_and_only_frozen(self):
        offer = self._offer()
        req = self._request(offer)
        self._sub(self.daniel)
        sent = []
        # The saver cannot declare for the requester.
        err = app.declare_late(self.conn, req["id"], self.daniel["id"],
                                sender=lambda sub, payload: sent.append(payload))
        self.assertIsNotNone(err)
        self.assertEqual(sent, [])
        # A resolved request cannot be declared late either.
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        err = self._declare(req)
        self.assertIsNotNone(err)
        err = app.declare_late(self.conn, 999999, self.lev["id"])
        self.assertIsNotNone(err)

    def test_decide_only_saver_and_only_when_pending(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        # No declaration yet: nothing to decide.
        err = self._decide(req, "accept", now=start)
        self.assertIsNotNone(err)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        # Someone else cannot decide.
        err = app.decide_late(self.conn, req["id"], self.lev["id"],
                              "accept", now=start)
        self.assertIsNotNone(err)
        # A bad decision value is rejected.
        err = app.decide_late(self.conn, req["id"], self.daniel["id"],
                              "maybe", now=start)
        self.assertIsNotNone(err)
        # One decision only: after an accept, a refuse is rejected.
        self.assertIsNone(self._decide(req, "accept", now=start))
        err = self._decide(req, "refuse", now=start)
        self.assertIsNotNone(err)

    def test_decide_after_deadline_refunds_as_no_show(self):
        offer = self._offer()
        req = self._request(offer)
        start = self._start(offer)
        self.assertIsNone(self._declare(
            req, now=start - timedelta(minutes=2)))
        # The saver answers at +11, after the deadline: the request
        # has already expired, and an accept cannot revive it.
        err = self._decide(req, "accept",
                           now=start + timedelta(minutes=11))
        self.assertIsNotNone(err)
        self.assertEqual(self._row(req["id"])["status"], "no_show")
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"], 3)

    def test_declare_and_on_the_way_coexist(self):
        offer = self._offer()
        req = self._request(offer)
        self.assertIsNone(app.mark_on_my_way(self.conn, req["id"],
                                             self.lev["id"]))
        self.assertIsNone(self._declare(req))
        row = self._row(req["id"])
        self.assertTrue(row["on_the_way"])
        self.assertEqual(row["late_status"], "pending")
        self.assertEqual(row["status"], "frozen")

    def test_late_push_respects_quiet_hours_and_vacation(self):
        offer = self._offer()
        req = self._request(offer)
        self._sub(self.daniel)
        # Quiet hours covering right now: the push is skipped.
        now = datetime.now()
        app.set_quiet_hours(self.conn, self.daniel["id"], now.hour,
                            (now.hour + 2) % 24)
        self.assertIsNone(self._declare(req))
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-quiet-hours")
        # Vacation on the saver: skipped too, no force bypass.
        app.set_quiet_hours(self.conn, self.daniel["id"], None, None)
        app.set_vacation(self.conn, self.daniel["id"], 1)
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])
        offer2 = self._offer()
        req2 = self._request(offer2)
        self.assertIsNone(self._declare(req2))
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?",
            (offer2["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-vacation")

    def test_buttons_render_on_my(self):
        offer = self._offer()
        req = self._request(offer)
        h = app.Handler.__new__(app.Handler)
        # The requester sees the declare button.
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("אני מאחר", html)
        self.assertIsNone(self._declare(req))
        # Now the requester sees the pending state, and the saver
        # sees the label plus both decision buttons.
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("הודעת שאתה מאחר", html)
        self.assertNotIn("אני מאחר", html)
        html = h._my(self.conn, dict(self.daniel), None, None)
        self.assertIn("מאחר (מחכה להחלטה)", html)
        self.assertIn("אשר איחור", html)
        self.assertIn("דחה איחור", html)
        self.assertIsNone(self._decide(req, "accept"))
        html = h._my(self.conn, dict(self.daniel), None, None)
        self.assertIn("מאחר (אושר)", html)
        self.assertNotIn("אשר איחור", html)

    def test_declare_expiry_race_one_outcome(self):
        """A declare racing the expiry: exactly one outcome — either
        the declaration lands first (request stays frozen, deadline
        +10) or the refund lands first (declaration rejected). Never
        a double refund, never a late flag on a resolved request."""
        def run_two(fn_a, fn_b):
            barrier = threading.Barrier(2)
            results = [None, None]

            def worker(i, fn):
                conn = app.connect(self.db)
                barrier.wait(timeout=5)
                results[i] = fn(conn)
                conn.close()

            threads = [threading.Thread(target=worker, args=(0, fn_a)),
                       threading.Thread(target=worker, args=(1, fn_b))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            return results

        def reset():
            self.conn.execute("DELETE FROM coin_ledger")
            self.conn.execute("DELETE FROM notification_log")
            self.conn.execute("DELETE FROM requests")
            self.conn.execute("DELETE FROM offers")
            self.conn.execute("UPDATE users SET balance=3")
            self.conn.commit()

        for _ in range(20):
            offer = self._offer()
            req = self._request(offer)
            start = self._start(offer)
            declare_err, _ = run_two(
                lambda c: app.declare_late(
                    c, req["id"], self.lev["id"],
                    now=start + timedelta(minutes=3)),
                lambda c: app.settle_expired(
                    c, now=start + timedelta(minutes=6)))
            row = self._row(req["id"])
            balance = app.get_user(self.conn, self.lev["id"])["balance"]
            if declare_err is None:
                # Declaration won: still frozen, deadline +10.
                self.assertEqual(row["status"], "frozen")
                self.assertEqual(row["late_status"], "pending")
                self.assertEqual(balance, 2)
            else:
                # Expiry won: refunded once, no late flag left over.
                self.assertEqual(row["status"], "no_show")
                self.assertIsNone(row["late_declared_at"])
                self.assertEqual(balance, 3)
            reset()

    def test_migration_adds_late_columns(self):
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "old.db")
        conn = app.connect(db)
        schema_old = app.SCHEMA.replace(
            "    reminded_30_at TEXT,\n    reminded_arrival_at TEXT,\n"
            "    late_declared_at TEXT,\n    late_status TEXT",
            "    reminded_30_at TEXT")
        conn.executescript(schema_old)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        self.assertNotIn("late_declared_at", cols)
        self.assertNotIn("late_status", cols)
        self.assertNotIn("reminded_arrival_at", cols)
        app.init_db(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
        self.assertIn("late_declared_at", cols)
        self.assertIn("late_status", cols)
        self.assertIn("reminded_arrival_at", cols)
        conn.close()
        tmp.cleanup()


class ClosedHistoryTest(Base):
    """Round 12 item 4: closed-offer history per user on /my,
    replacing the general closed list on /offers."""

    def _close(self, offer):
        app.cancel_offer(self.conn, offer["id"], self.daniel["id"])

    def _history_block(self, user):
        h = app.Handler.__new__(app.Handler)
        html = h._my(self.conn, dict(user), None, None)
        self.assertIn("היסטוריית הצעות סגורות", html)
        start = html.index("היסטוריית הצעות סגורות")
        end = html.index("</details>", start)
        return html[start:end]

    def test_history_shows_only_own_closed_offers(self):
        mine = self._offer()
        self.conn.execute("UPDATE offers SET note='ההצעה שלי' WHERE id=?",
                          (mine["id"],))
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        s = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, self.start,
             self.end)).fetchone()
        self.conn.commit()
        other, err = app.create_offer(self.conn, levi["id"], s["id"], "ההצעה של לב", 1)
        self.assertIsNone(err, err)
        self._close(mine)
        app.cancel_offer(self.conn, other["id"], levi["id"])
        block = self._history_block(self.daniel)
        self.assertIn("ההצעה שלי", block)
        self.assertNotIn("ההצעה של לב", block)
        # And the other user's own history shows only theirs.
        block = self._history_block(levi)
        self.assertIn("ההצעה של לב", block)
        self.assertNotIn("ההצעה שלי", block)

    def test_closed_offers_not_on_offers_page(self):
        h = app.Handler.__new__(app.Handler)
        open_offer = self._offer()
        self.conn.execute("UPDATE offers SET note='פתוחה' WHERE id=?",
                          (open_offer["id"],))
        # The closed offer needs its own session row: one open
        # offer per saver per session per day.
        s2 = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, self.start,
             self.end)).fetchone()
        self.conn.commit()
        closed, err = app.create_offer(self.conn, self.daniel["id"],
                                       s2["id"], "סגורה", 1)
        self.assertIsNone(err, err)
        self._close(closed)
        html = h._offers(self.conn, dict(self.daniel), None, None)
        self.assertIn("פתוחה", html)
        self.assertNotIn("סגורה", html)
        self.assertNotIn("בוטלה", html)

    def test_history_lists_outcomes(self):
        # Confirmed: coin paid to the saver.
        paid = self._offer()
        req = self._request(paid)
        app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self._close(paid)
        # No-show: refunded at start+5.
        missed = self._offer()
        self._request(missed)
        start = app._session_start_datetime(
            dict(missed),
            dict(app.get_session(self.conn, missed["session_id"])))
        app.settle_expired(self.conn, now=start + timedelta(minutes=6))
        # Late-declined: declared, then refused.
        late = self._offer()
        req = self._request(late)
        app.declare_late(self.conn, req["id"], self.lev["id"])
        app.decide_late(self.conn, req["id"], self.daniel["id"], "refuse")
        self._close(late)
        # No requests at all.
        empty = self._offer()
        self._close(empty)
        block = self._history_block(self.daniel)
        self.assertIn("אושר — המטבע עבר לשומר", block)
        self.assertIn("לא הגיע — המטבע חזר", block)
        self.assertIn("האיחור נדחה — המטבע חזר", block)
        self.assertIn("אף אחד לא ביקש מקום", block)
        self.assertIn("Lev", block)

    def test_re_offer_from_history_entry(self):
        offer = self._offer()
        self._close(offer)
        block = self._history_block(self.daniel)
        self.assertIn('action="/re-offer"', block)
        self.assertIn('value="%s"' % offer["id"], block)
        new, err = app.re_offer(self.conn, offer["id"], self.daniel["id"])
        self.assertIsNone(err, err)
        self.assertEqual(new["status"], "open")

    def test_open_offers_not_in_history(self):
        open_offer = self._offer()
        self.conn.execute("UPDATE offers SET note='פתוחה עכשיו' WHERE id=?",
                          (open_offer["id"],))
        # The closed offer needs its own session row: one open
        # offer per saver per session per day.
        s2 = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], self.today, self.start,
             self.end)).fetchone()
        self.conn.commit()
        closed, err = app.create_offer(self.conn, self.daniel["id"],
                                       s2["id"], "סגורה כבר", 1)
        self.assertIsNone(err, err)
        self._close(closed)
        block = self._history_block(self.daniel)
        self.assertIn("סגורה כבר", block)
        self.assertNotIn("פתוחה עכשיו", block)

    def test_coin_history_still_on_my(self):
        offer = self._offer()
        self._request(offer)
        h = app.Handler.__new__(app.Handler)
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("היסטוריית מטבעות", html)
        self.assertIn("היסטוריית הצעות סגורות", html)



class Round13Test(Base):
    """Round 13: seat zones removed, multi-session specific follow,
    follows at registration, weekly auto-offers removed, design
    polish (CSS only), and the separate admin account."""

    def _html(self, fn, *args):
        h = app.Handler.__new__(app.Handler)
        return fn(h, *args)

    # --- A. Zones are gone from every user-facing surface. ---

    def test_offer_form_and_my_have_no_zone(self):
        form = self._html(app.Handler._offer_form, self.conn,
                          dict(self.daniel), None, None)
        self.assertNotIn('name="zone"', form)
        self.assertNotIn("אזור", form)
        self.assertNotIn("הצע אוטומטית כל שבוע", form)
        self.assertNotIn("auto_weekly", form)
        my = self._html(app.Handler._my, self.conn, dict(self.daniel),
                        None, None)
        self.assertNotIn("/set-zone-preference", my)
        self.assertNotIn("אזור מועדף", my)
        self.assertNotIn("create-auto-offer", my)
        self.assertNotIn("delete-auto-offer", my)

    def test_edit_form_has_no_zone(self):
        offer = self._offer()
        h = app.Handler.__new__(app.Handler)
        html = h._edit_offer_form(self.conn, dict(self.daniel),
                                  offer["id"], None, None)
        self.assertNotIn('name="zone"', html)
        self.assertNotIn("אזור", html)

    def test_help_mentions_no_zone_and_no_auto_offer(self):
        html = self._html(app.Handler._help, self.conn, None, None, None)
        self.assertNotIn("אזור", html)
        self.assertNotIn("הצעה אוטומטית כל שבוע", html)
        # The standing auto-REQUEST stays — it is still documented.
        self.assertIn("בקשה אוטומטית כל שבוע", html)

    def test_payloads_carry_no_zone(self):
        offer = self._offer()
        payload = app.prepare_push_payload(
            dict(offer), dict(self.course), dict(self.session))
        self.assertNotIn("אזור", payload["body"])
        edited = app.prepare_offer_edited_payload(
            dict(offer), dict(self.course), dict(self.session),
            "שורה 5")
        self.assertNotIn("אזור", edited["body"])

    def test_zone_migration_rebuilds_old_tables(self):
        # An old DB still has zone NOT NULL on offers and
        # offer_templates. init_db must relax both in place,
        # keeping every row and every foreign key intact.
        import os, sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        conn = app.connect(path)
        old = app.SCHEMA.replace(
            "    class_date TEXT NOT NULL,\n    zone TEXT,",
            "    class_date TEXT NOT NULL,\n    zone TEXT NOT NULL,")
        old = old.replace(
            "CREATE TABLE IF NOT EXISTS offer_templates (\n"
            "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "    user_id INTEGER NOT NULL REFERENCES users(id),\n"
            "    session_id INTEGER NOT NULL REFERENCES course_sessions(id),\n"
            "    zone TEXT,",
            "CREATE TABLE IF NOT EXISTS offer_templates (\n"
            "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "    user_id INTEGER NOT NULL REFERENCES users(id),\n"
            "    session_id INTEGER NOT NULL REFERENCES course_sessions(id),\n"
            "    zone TEXT NOT NULL,")
        conn.executescript(old)
        conn.execute(
            "INSERT INTO users (name, password_hash, balance, created_at)"
            " VALUES ('Daniel', NULL, 3, '2026-11-10T10:00:00')")
        conn.execute(
            "INSERT INTO courses (name) VALUES ('תכנון אלגוריתמים')")
        conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (1, ?, '10:00', '12:00',"
            " '34', '214')", (self.today,))
        conn.execute(
            "INSERT INTO offers (saver_id, course_id, session_id,"
            " class_date, zone, note, seats_total, status, created_at)"
            " VALUES (1, 1, 1, '2026-11-10', 'קדמי', 'שורה 2', 2,"
            " 'open', '2026-11-10T09:00:00')")
        conn.execute(
            "INSERT INTO offer_templates (user_id, session_id, zone,"
            " note, seats, created_at) VALUES (1, 1, 'אמצעי', 'שורה 5',"
            " 1, '2026-11-10T09:00:00')")
        conn.commit()
        app.init_db(conn)
        for table in ("offers", "offer_templates"):
            row = conn.execute(
                "SELECT \"notnull\" FROM pragma_table_info(?)"
                " WHERE name='zone'", (table,)).fetchone()
            self.assertEqual(row["notnull"], 0, table)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT zone FROM offers").fetchone()["zone"],
            "קדמי")
        self.assertEqual(
            conn.execute("SELECT zone FROM offer_templates")
            .fetchone()["zone"], "אמצעי")
        self.assertEqual(
            conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        # A fresh offer on the migrated DB works with NULL zone.
        # (A second session, so it does not collide with the open
        # offer above — one open offer per saver+session.)
        conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (1, ?, '14:00', '16:00',"
            " '35', '2')", (self.today,))
        conn.commit()
        # Inside the offer window (60 minutes before 14:00).
        now = datetime.now().replace(hour=13, minute=30, second=0, microsecond=0)
        offer, err = app.create_offer(conn, 1, 2, "שורה 9", 1, now=now)
        self.assertIsNone(err, err)
        self.assertIsNone(offer["zone"])
        conn.close()

    # --- B + C. Follow page: fragment, checkboxes, several sessions. ---

    def test_specific_follow_page_offers_checkboxes(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"],
                       session_id=self.session["id"])
        html = self._html(app.Handler._follows, self.conn, dict(self.lev),
                          None, None,
                          {"course_id": [str(self.course["id"])]})
        self.assertIn('id="follow"', html)
        self.assertIn('action="/follows#follow"', html)
        self.assertIn('type="checkbox"', html)
        self.assertNotIn('type="radio"', html)
        self.assertIn('name="specific"', html)
        self.assertIn("checked", html)
        self.assertIn("(כבר עוקב)", html)

    def test_several_specific_follows_for_one_course(self):
        other = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], (self.today + 1) % 7, self.start,
             self.end)).fetchone()
        self.conn.commit()
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"],
                                session_id=self.session["id"])
        self.assertIsNone(err, err)
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"], session_id=other["id"])
        self.assertIsNone(err, err)
        rows = self.conn.execute(
            "SELECT * FROM follows WHERE user_id=? AND course_id=?",
            (self.lev["id"], self.course["id"])).fetchall()
        self.assertEqual(len(rows), 2)
        # The same UNIQUE rule still blocks a duplicate.
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"],
                                session_id=self.session["id"])
        self.assertIsNotNone(err)

    # --- D. Registration offers specific follows. ---

    def test_register_page_offers_specific_sessions(self):
        html = self._html(app.Handler._register, self.conn, None, None,
                          None)
        self.assertIn('name="session_id"', html)
        self.assertIn("או שיעורים מסוימים?", html)
        self.assertIn('name="course_id"', html)

    def test_follows_page_shows_registered_banner(self):
        html = self._html(app.Handler._follows, self.conn, dict(self.lev),
                          None, None, {"registered": ["1"]})
        self.assertIn("נרשמת!", html)
        self.assertIn('href="/push"', html)
        self.assertIn('class="btn"', html)

    # --- F. Design polish: CSS only, no new JS. ---

    def test_css_has_button_class_and_no_new_js(self):
        self.assertIn(".btn", app.CSS)
        self.assertIn("accent-color", app.CSS)
        self.assertNotIn("<script", app.CSS)


class Round14Test(Base):
    """Round 14: Hebrew course names, exact-session follows,
    general follow overrides specific at creation, muted-
    notifications banner, offer filter by course, arrival-confirm
    reminder, waiting-list position."""

    def _html(self, fn, *args):
        h = app.Handler.__new__(app.Handler)
        return fn(h, *args)

    def _post(self, path, fields, cookie=None):
        # Drive do_POST in-process: a bare Handler with a BytesIO
        # body, and connect() pointed at this test's database.
        # connect() binds its db_path default at definition time,
        # so patching app.DB_PATH alone does nothing — the
        # __defaults__ patch is what routes the handler at the
        # test DB. Returns (status, headers, raw_response).
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "POST"
        h.requestline = "POST %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        headers = {"Content-Length": str(len(body))}
        if cookie is not None:
            headers["Cookie"] = cookie
        h.headers = headers
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_POST()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        heads = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        return status, heads, raw

    def _sun_1800_pair(self):
        # The two תכנון אלגוריתמים sessions at Sunday 18:00: the
        # lecture (building 35/1) and the tutorial (building 34/14).
        lec = self.conn.execute(
            "SELECT * FROM course_sessions WHERE course_id=?"
            " AND weekday=6 AND start_time='18:00' AND building='35'",
            (self.course["id"],)).fetchone()
        tut = self.conn.execute(
            "SELECT * FROM course_sessions WHERE course_id=?"
            " AND weekday=6 AND start_time='18:00' AND building='34'",
            (self.course["id"],)).fetchone()
        self.assertIsNotNone(lec)
        self.assertIsNotNone(tut)
        return lec, tut

    def _started_request(self):
        # A frozen request whose class started 1 minute ago: inside
        # the arrival-confirm window (start .. start+5).
        offer = self._offer()
        req = self._request(offer)
        self.conn.execute(
            "UPDATE course_sessions SET start_time=? WHERE id=?",
            (_mins_from_now(-1), self.session["id"]))
        self.conn.commit()
        return req, offer

    def _two_offers(self):
        # One open offer per course: תכנון אלגוריתמים (today's
        # session from setUp) and חדו״א 2 (a session added today).
        calc = self.conn.execute(
            "SELECT * FROM courses WHERE name='חדו״א 2'").fetchone()
        calc_session = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '35', '2')"
            " RETURNING *",
            (calc["id"], self.today, self.start, self.end)).fetchone()
        self.conn.commit()
        alg_offer = self._offer()
        calc_offer, err = app.create_offer(
            self.conn, self.daniel["id"], calc_session["id"], "", 1)
        self.assertIsNone(err, err)
        return alg_offer, calc_offer, calc

    def _full_offer_with_waiters(self):
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        yehonatan = self.conn.execute(
            "SELECT * FROM users WHERE name='Yehonatan'").fetchone()
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        offer = self._offer(seats=2)
        self._request(offer)  # Lev takes seat 1
        self._request(offer, requester=shaked)  # Shaked takes seat 2
        for u in (levi, yehonatan):
            app.add_follow(self.conn, u["id"], self.course["id"])
            row, err = app.join_waiting_list(
                self.conn, u["id"], offer["id"])
            self.assertIsNone(err, err)
        return offer, levi, yehonatan

    def _offer_row(self, offer_id):
        return self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer_id,)).fetchone()

    # --- A. Course names are Hebrew now. ---

    def test_seed_course_names_are_hebrew(self):
        names = sorted(
            r["name"] for r in self.conn.execute("SELECT name FROM courses"))
        self.assertEqual(
            names, ["חדו״א 2", "תכנון אלגוריתמים", "תכנות מערכות"])

    def test_rename_migration_renames_english_and_leaves_custom(self):
        # Pretend this is an old database: English names back.
        self.conn.execute(
            "UPDATE courses SET name='Algorithms'"
            " WHERE name='תכנון אלגוריתמים'")
        self.conn.execute(
            "UPDATE courses SET name='Calculus 2' WHERE name='חדו״א 2'")
        self.conn.execute(
            "UPDATE courses SET name='Systems Programming'"
            " WHERE name='תכנות מערכות'")
        self.conn.execute(
            "INSERT INTO courses (name) VALUES ('Linear Algebra')")
        self.conn.commit()
        self.assertEqual(app._rename_courses_to_hebrew(self.conn), 3)
        names = sorted(
            r["name"] for r in self.conn.execute("SELECT name FROM courses"))
        self.assertEqual(names, ["Linear Algebra", "חדו״א 2",
                                 "תכנון אלגוריתמים", "תכנות מערכות"])
        # Idempotent: a second run renames nothing.
        self.assertEqual(app._rename_courses_to_hebrew(self.conn), 0)

    def test_rename_migration_never_overwrites_existing_hebrew(self):
        self.conn.execute(
            "UPDATE courses SET name='Algorithms'"
            " WHERE name='תכנון אלגוריתמים'")
        self.conn.execute(
            "INSERT INTO courses (name) VALUES ('תכנון אלגוריתמים')")
        self.conn.commit()
        self.assertEqual(app._rename_courses_to_hebrew(self.conn), 0)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM courses WHERE name='Algorithms'"
            ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_rename_runs_before_teacher_fill(self):
        # An old database: English names, teachers wiped. init_db
        # must rename first, then fill teachers from the Hebrew
        # seed map — otherwise the teacher lookup (keyed by the
        # Hebrew name) finds nothing.
        self.conn.execute(
            "UPDATE courses SET name='Algorithms'"
            " WHERE name='תכנון אלגוריתמים'")
        self.conn.execute(
            "UPDATE courses SET name='Calculus 2' WHERE name='חדו״א 2'")
        self.conn.execute(
            "UPDATE courses SET name='Systems Programming'"
            " WHERE name='תכנות מערכות'")
        self.conn.execute("UPDATE course_sessions SET teacher=''")
        self.conn.commit()
        app.init_db(self.conn)
        names = sorted(
            r["name"] for r in self.conn.execute("SELECT name FROM courses"))
        self.assertEqual(
            names, ["חדו״א 2", "תכנון אלגוריתמים", "תכנות מערכות"])
        row = self.conn.execute(
            "SELECT s.teacher FROM course_sessions s"
            " JOIN courses c ON c.id=s.course_id"
            " WHERE c.name='תכנון אלגוריתמים' AND s.weekday=6"
            " AND s.start_time='18:00' AND s.building='35'").fetchone()
        self.assertEqual(row["teacher"], "ד״ר בן דניאל")

    # --- B. A specific follow stores the exact session id. ---

    def test_specific_follows_for_same_hour_are_separate(self):
        lec, tut = self._sun_1800_pair()
        f1, err = app.add_follow(self.conn, self.lev["id"],
                                 self.course["id"], session_id=lec["id"])
        self.assertIsNone(err, err)
        f2, err = app.add_follow(self.conn, self.lev["id"],
                                 self.course["id"], session_id=tut["id"])
        self.assertIsNone(err, err)
        self.assertEqual(f1["session_id"], lec["id"])
        self.assertEqual(f2["session_id"], tut["id"])
        # Matching is exact: each follow matches only its session.
        self.assertTrue(app.follow_matches(dict(f1), dict(lec)))
        self.assertFalse(app.follow_matches(dict(f1), dict(tut)))
        self.assertTrue(app.follow_matches(dict(f2), dict(tut)))
        self.assertFalse(app.follow_matches(dict(f2), dict(lec)))

    def test_follower_counts_differ_for_same_hour_sessions(self):
        lec, tut = self._sun_1800_pair()
        # Daniel's seeded general follow counts for both sessions;
        # Lev's specific follow counts only for the lecture.
        app.add_follow(self.conn, self.lev["id"], self.course["id"],
                       session_id=lec["id"])
        self.assertEqual(
            app.followers_count_for_session(self.conn, dict(lec)), 2)
        self.assertEqual(
            app.followers_count_for_session(self.conn, dict(tut)), 1)

    def test_session_for_follow_uses_session_id(self):
        _lec, tut = self._sun_1800_pair()
        f, _err = app.add_follow(self.conn, self.lev["id"],
                                 self.course["id"], session_id=tut["id"])
        s = app.session_for_follow(self.conn, dict(f))
        self.assertEqual(s["id"], tut["id"])
        self.assertEqual(s["building"], "34")

    def test_follows_page_prechecks_only_the_followed_session(self):
        lec, tut = self._sun_1800_pair()
        app.add_follow(self.conn, self.lev["id"], self.course["id"],
                       session_id=lec["id"])
        html = self._html(app.Handler._follows, self.conn, dict(self.lev),
                          None, None,
                          {"course_id": [str(self.course["id"])]})
        self.assertIn('value="%s" checked' % lec["id"], html)
        self.assertNotIn('value="%s" checked' % tut["id"], html)

    def test_legacy_follow_without_session_id_matches_by_time(self):
        # An old follow dict with no session_id key at all still
        # matches by day + start time, as before round 14.
        lec, tut = self._sun_1800_pair()
        old_follow = {"weekday": 6, "start_time": "18:00"}
        self.assertTrue(app.follow_matches(old_follow, dict(lec)))
        self.assertTrue(app.follow_matches(old_follow, dict(tut)))
        other = self.conn.execute(
            "SELECT * FROM course_sessions WHERE course_id=?"
            " AND weekday=2 LIMIT 1", (self.course["id"],)).fetchone()
        self.assertFalse(app.follow_matches(old_follow, dict(other)))

    def test_old_follow_migration_backfills_unique_and_keeps_ambiguous(self):
        # A database from before round 14: follows have no
        # session_id column and the old UNIQUE. init_db must
        # backfill the session where the hour maps to exactly one
        # session, keep NULL where it is ambiguous, and rebuild
        # the UNIQUE — without losing any follow.
        old = app.SCHEMA.replace(
            "CREATE TABLE IF NOT EXISTS follows (\n"
            "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "    user_id INTEGER NOT NULL REFERENCES users(id),\n"
            "    course_id INTEGER NOT NULL REFERENCES courses(id),\n"
            "    session_id INTEGER REFERENCES course_sessions(id),\n"
            "    weekday INTEGER,\n"
            "    start_time TEXT,\n"
            "    created_at TEXT NOT NULL,\n"
            "    UNIQUE(user_id, course_id, session_id)\n"
            ");",
            "CREATE TABLE IF NOT EXISTS follows (\n"
            "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "    user_id INTEGER NOT NULL REFERENCES users(id),\n"
            "    course_id INTEGER NOT NULL REFERENCES courses(id),\n"
            "    weekday INTEGER,\n"
            "    start_time TEXT,\n"
            "    created_at TEXT NOT NULL,\n"
            "    UNIQUE(user_id, course_id, weekday, start_time)\n"
            ");", 1)
        path = os.path.join(self.tmp.name, "old-follows.db")
        conn = app.connect(path)
        conn.executescript(old)
        conn.execute(
            "INSERT INTO users (name, balance, sample, created_at)"
            " VALUES ('Lev', 3, 1, '2026-11-10T09:00:00')")
        conn.execute(
            "INSERT INTO courses (name, kind)"
            " VALUES ('תכנון אלגוריתמים', 'lecture')")
        lec = conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (1, 6, '18:00', '20:00',"
            " '35', '1') RETURNING *").fetchone()
        tut = conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (1, 6, '18:00', '20:00',"
            " '34', '14') RETURNING *").fetchone()
        solo = conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (1, 0, '10:00', '12:00',"
            " '90', '222') RETURNING *").fetchone()
        conn.execute(
            "INSERT INTO follows (user_id, course_id, weekday, start_time,"
            " created_at) VALUES (1, 1, 6, '18:00', '2026-11-10T09:00:00')")
        conn.execute(
            "INSERT INTO follows (user_id, course_id, weekday, start_time,"
            " created_at) VALUES (1, 1, 0, '10:00', '2026-11-10T09:00:00')")
        conn.commit()
        app.init_db(conn)
        amb = conn.execute(
            "SELECT * FROM follows WHERE weekday=6").fetchone()
        self.assertIsNone(amb["session_id"])
        one = conn.execute(
            "SELECT * FROM follows WHERE weekday=0").fetchone()
        self.assertEqual(one["session_id"], solo["id"])
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='follows'").fetchone()[0]
        self.assertIn("UNIQUE(user_id, course_id, session_id)", sql)
        # The ambiguous follow still matches both sessions.
        self.assertTrue(app.follow_matches(dict(amb), dict(lec)))
        self.assertTrue(app.follow_matches(dict(amb), dict(tut)))
        self.assertTrue(app.follow_matches(dict(one), dict(solo)))
        # A second init_db is a no-op for the migration.
        app.init_db(conn)
        one2 = conn.execute(
            "SELECT * FROM follows WHERE weekday=0").fetchone()
        self.assertEqual(one2["session_id"], solo["id"])
        conn.close()

    # --- C. Muted-notifications banner. ---

    def _muted_user(self, quiet=None, vacation=False, until=None):
        if quiet is not None:
            self.conn.execute(
                "UPDATE users SET quiet_start=?, quiet_end=? WHERE id=?",
                (quiet[0], quiet[1], self.lev["id"]))
        if vacation:
            self.conn.execute(
                "UPDATE users SET on_vacation=1, vacation_until=?"
                " WHERE id=?", (until, self.lev["id"]))
        self.conn.commit()
        return dict(app.get_user(self.conn, self.lev["id"]))

    def test_muted_banner_quiet_hours(self):
        now = datetime.now().replace(minute=30, second=0, microsecond=0)
        h = now.hour
        user = self._muted_user(quiet=(h, (h + 1) % 24))
        html = app.muted_banner_html(user, now)
        self.assertIn("muted-banner", html)
        self.assertIn("מושתקות עד", html)
        self.assertIn("%02d:00" % ((h + 1) % 24), html)
        self.assertIn('href="/my"', html)

    def test_muted_banner_vacation(self):
        user = self._muted_user(vacation=True)
        html = app.muted_banner_html(user)
        self.assertIn("במצב חופשה", html)
        self.assertIn("מושתקות", html)

    def test_muted_banner_vacation_with_end_date(self):
        user = self._muted_user(vacation=True, until="2026-11-20")
        html = app.muted_banner_html(user)
        self.assertIn("במצב חופשה", html)
        self.assertIn("20.11", html)

    def test_muted_banner_vacation_wins_over_quiet_hours(self):
        now = datetime.now().replace(minute=30, second=0, microsecond=0)
        h = now.hour
        user = self._muted_user(quiet=(h, (h + 1) % 24),
                                vacation=True)
        html = app.muted_banner_html(user, now)
        self.assertIn("במצב חופשה", html)
        # The vacation banner links to vacation settings, not to
        # quiet-hours settings — and it carries no end hour.
        self.assertIn("ניהול חופשה", html)
        self.assertNotIn("שינוי שעות שקטות", html)

    def test_muted_banner_hidden_when_not_muted(self):
        self.assertEqual(app.muted_banner_html(dict(self.lev)), "")
        self.assertEqual(app.muted_banner_html(None), "")

    def test_home_shows_muted_banner(self):
        now = datetime.now().replace(minute=30, second=0, microsecond=0)
        h = now.hour
        user = self._muted_user(quiet=(h, (h + 1) % 24))
        html = self._html(app.Handler._home, self.conn, user, None, None)
        self.assertIn("מושתקות עד", html)

    # --- D. A general follow overrides specific ones at creation. ---

    def test_has_general_follow(self):
        self.assertFalse(app.has_general_follow(
            self.conn, self.lev["id"], self.course["id"]))
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        self.assertTrue(app.has_general_follow(
            self.conn, self.lev["id"], self.course["id"]))

    def test_register_general_overrides_specific(self):
        calc = self.conn.execute(
            "SELECT * FROM courses WHERE name='חדו״א 2'").fetchone()
        calc_session = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '35', '2')"
            " RETURNING *",
            (calc["id"], self.today, self.start, self.end)).fetchone()
        self.conn.commit()
        status, heads, _raw = self._post("/register", {
            # Round 20: registration needs a full name and a phone.
            "name": "רוני כהן",
            "phone": "0501234567",
            "password": "secret123",
            "password2": "secret123",
            "course_id": [str(self.course["id"])],
            "session_id": [str(self.session["id"]),
                           str(calc_session["id"])],
        })
        self.assertEqual(status, 303)
        self.assertEqual(heads["Location"], "/follows?registered=1#follow")
        user = self.conn.execute(
            "SELECT * FROM users WHERE name='רוני כהן'").fetchone()
        self.assertIsNotNone(user)
        rows = self.conn.execute(
            "SELECT * FROM follows WHERE user_id=?",
            (user["id"],)).fetchall()
        # General תכנון אלגוריתמים + specific חדו״א 2. The specific
        # תכנון אלגוריתמים session was skipped — already covered.
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(r["course_id"] == self.course["id"]
                            and r["weekday"] is None for r in rows))
        self.assertTrue(any(r["course_id"] == calc["id"]
                            and r["session_id"] == calc_session["id"]
                            for r in rows))
        self.assertFalse(any(r["course_id"] == self.course["id"]
                             and r["session_id"] is not None
                             for r in rows))

    def test_add_follow_post_skips_specific_when_general(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        token = app.create_session(self.conn, self.lev["id"])
        status, heads, _raw = self._post("/add-follow", {
            "course_id": str(self.course["id"]),
            "specific": "1",
            "session_id": str(self.session["id"]),
        }, cookie="seat_session=%s" % token)
        self.assertEqual(status, 303)
        self.assertIn("/follows?msg=", heads["Location"])
        self.assertTrue(heads["Location"].endswith("#follow"))
        rows = self.conn.execute(
            "SELECT * FROM follows WHERE user_id=? AND course_id=?",
            (self.lev["id"], self.course["id"])).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["weekday"])

    def test_specific_kept_when_general_added_later(self):
        # Only NEW redundant follows are skipped — an existing
        # specific follow is never deleted by adding a general one.
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"],
                                session_id=self.session["id"])
        self.assertIsNone(err, err)
        _, err = app.add_follow(self.conn, self.lev["id"],
                                self.course["id"])
        self.assertIsNone(err, err)
        rows = self.conn.execute(
            "SELECT * FROM follows WHERE user_id=? AND course_id=?",
            (self.lev["id"], self.course["id"])).fetchall()
        self.assertEqual(len(rows), 2)

    # --- E. Filter offers by course. ---

    def test_offers_filter_by_course(self):
        _alg, _calc_offer, calc = self._two_offers()
        html = self._html(app.Handler._offers, self.conn, dict(self.daniel),
                          None, None,
                          {"course_id": [str(calc["id"])]})
        self.assertIn("מסונן:", html)
        self.assertIn("חדו״א 2", html)
        self.assertIn("סינון לפי קורס:", html)
        # Exactly one offer row on the page.
        self.assertEqual(html.count('class="card offer"'), 1)

    def test_offers_unfiltered_shows_all_and_filter_links(self):
        self._two_offers()
        html = self._html(app.Handler._offers, self.conn, dict(self.daniel),
                          None, None, {})
        self.assertNotIn("מסונן:", html)
        self.assertIn("סינון לפי קורס:", html)
        self.assertEqual(html.count('class="card offer"'), 2)

    def test_offers_filter_unknown_course_shows_all(self):
        self._two_offers()
        html = self._html(app.Handler._offers, self.conn, dict(self.daniel),
                          None, None, {"course_id": ["99999"]})
        self.assertNotIn("מסונן:", html)
        self.assertEqual(html.count('class="card offer"'), 2)

    def test_offers_filter_links_hidden_with_one_course(self):
        self._offer()
        html = self._html(app.Handler._offers, self.conn, dict(self.daniel),
                          None, None, {})
        self.assertNotIn("סינון לפי קורס:", html)
        self.assertEqual(html.count('class="card offer"'), 1)

    # --- F. Arrival-confirm reminder at class start. ---

    def test_arrival_confirm_payload_has_four_fields(self):
        req, _offer = self._started_request()
        session = self.conn.execute(
            "SELECT * FROM course_sessions WHERE id=?",
            (self.session["id"],)).fetchone()
        payload = app.prepare_arrival_confirm_payload(
            dict(req), dict(self.course), dict(session))
        # Round 20, item 11: the arrival-confirm reminder carries
        # request_id + actions on top of the four display fields.
        self.assertEqual(set(payload.keys()),
                         {"title", "body", "url", "tag",
                          "request_id", "actions"})
        self.assertEqual(payload["title"], "הגעת? אשר את ההגעה")
        self.assertIn("תכנון אלגוריתמים", payload["body"])
        self.assertEqual(payload["url"], "/my")
        self.assertIn(str(req["id"]), payload["tag"])
        self.assertEqual(payload["request_id"], req["id"])
        self.assertEqual(payload["actions"],
                         [{"action": "confirm-arrival", "title": "הגעתי"},
                          {"action": "report-not-saved",
                           "title": "המקום לא היה שם"}])

    def test_arrival_confirm_due_only_between_start_and_deadline(self):
        req, _offer = self._started_request()
        start = datetime.now() - timedelta(minutes=1)
        self.assertEqual(len(app.find_arrival_confirm_due(
            self.conn, now=datetime.now())), 1)
        # Two minutes before the class starts: not yet due.
        self.assertEqual(app.find_arrival_confirm_due(
            self.conn, now=start - timedelta(minutes=2)), [])
        # Six minutes after the start — past the +5 no-show
        # deadline: too late to remind, the seat is lost anyway.
        self.assertEqual(app.find_arrival_confirm_due(
            self.conn, now=start + timedelta(minutes=6)), [])

    def test_arrival_confirm_tick_sends_once(self):
        req, offer = self._started_request()
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        sent = []
        n = app.arrival_confirm_tick(
            self.conn,
            sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(n, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["title"], "הגעת? אשר את ההגעה")
        row = self.conn.execute(
            "SELECT reminded_arrival_at FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertIsNotNone(row["reminded_arrival_at"])
        # A second tick sends nothing — the flag survives.
        n2 = app.arrival_confirm_tick(
            self.conn,
            sender=lambda sub, payload: sent.append(payload))
        self.assertEqual(n2, 0)
        self.assertEqual(len(sent), 1)

    def test_arrival_confirm_tick_respects_quiet_hours_but_marks(self):
        req, offer = self._started_request()
        app.save_subscription(self.conn, self.lev["id"], {
            "endpoint": "https://example.com/push/lev",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        now = datetime.now()
        h = now.hour
        self.conn.execute(
            "UPDATE users SET quiet_start=?, quiet_end=? WHERE id=?",
            (h, (h + 1) % 24, self.lev["id"]))
        self.conn.commit()
        sent = []
        n = app.arrival_confirm_tick(
            self.conn, sender=lambda sub, payload: sent.append(payload),
            now=now)
        self.assertEqual(n, 1)
        self.assertEqual(sent, [])
        log = self.conn.execute(
            "SELECT * FROM notification_log WHERE offer_id=?"
            " ORDER BY id DESC LIMIT 1", (offer["id"],)).fetchone()
        self.assertEqual(log["status"], "skipped-quiet-hours")
        # The flag is still set: a skipped reminder is never
        # retried later, when it would be noise.
        row = self.conn.execute(
            "SELECT reminded_arrival_at FROM requests WHERE id=?",
            (req["id"],)).fetchone()
        self.assertIsNotNone(row["reminded_arrival_at"])

    def test_arrival_confirm_tick_skips_confirmed(self):
        req, _offer = self._started_request()
        err = app.confirm_arrival(self.conn, req["id"], self.lev["id"])
        self.assertIsNone(err, err)
        n = app.arrival_confirm_tick(
            self.conn, sender=lambda sub, payload: None)
        self.assertEqual(n, 0)

    # --- G. Waiting-list position. ---

    def test_waiting_position_labels(self):
        self.assertEqual(app.waiting_position_label(1), "אתה ראשון בתור")
        self.assertEqual(app.waiting_position_label(2), "אתה מספר 2 בתור")
        self.assertEqual(app.waiting_position_label(None), "")

    def test_waiting_position_for_user(self):
        offer, levi, yehonatan = self._full_offer_with_waiters()
        self.assertEqual(app.waiting_position_for_user(
            self.conn, offer["id"], levi["id"]), 1)
        self.assertEqual(app.waiting_position_for_user(
            self.conn, offer["id"], yehonatan["id"]), 2)
        self.assertIsNone(app.waiting_position_for_user(
            self.conn, offer["id"], self.lev["id"]))

    def test_offer_card_shows_waiting_position(self):
        offer, levi, yehonatan = self._full_offer_with_waiters()
        row = self._offer_row(offer["id"])
        h = app.Handler.__new__(app.Handler)
        self.assertIn("אתה ראשון בתור",
                      h._offer_card(self.conn, row, dict(levi)))
        self.assertIn("אתה מספר 2 בתור",
                      h._offer_card(self.conn, row, dict(yehonatan)))

class DeviceDetectionTest(Base):
    """Round 14, item 8: device detection + warning at subscribe
    time. The subscription is classified server-side from the
    User-Agent, the standalone flag comes from app.js, and /push
    lists the user's subscriptions by device."""

    # Real-world User-Agent strings (desktop and mobile).
    WIN_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36")
    MAC_SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  "Version/17.0 Safari/605.1.15")
    LINUX_FIREFOX = ("Mozilla/5.0 (X11; Linux x86_64; rv:132.0) "
                     "Gecko/20100101 Firefox/132.0")
    WIN_EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
    IPHONE_SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like "
                     "Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                     "Version/17.0 Mobile/15E148 Safari/604.1")
    IPAD_SAFARI = ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/17.0 Mobile/15E148 Safari/604.1")
    ANDROID_CHROME = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0.0.0 Mobile Safari/537.36")

    def _sub(self, user_id, endpoint="https://example.com/push/x",
             device="unknown", browser="unknown", standalone=False):
        err = app.save_subscription(
            self.conn, user_id,
            {"endpoint": endpoint,
             "keys": {"p256dh": "k", "auth": "a"}},
            device=device, browser=browser, standalone=standalone)
        self.assertIsNone(err, err)

    def _cookie(self, user_id):
        return "seat_session=%s" % app.create_session(self.conn, user_id)

    def _post_json(self, path, obj, cookie=None, user_agent=None):
        # Like Round14Test._post, but a JSON body (for
        # /api/subscribe) and an optional User-Agent header.
        # Returns (status, parsed_json).
        body = json.dumps(obj).encode("utf-8")
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "POST"
        h.requestline = "POST %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        headers = {"Content-Length": str(len(body)),
                   "Content-Type": "application/json"}
        if cookie is not None:
            headers["Cookie"] = cookie
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        h.headers = headers
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_POST()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        payload = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""
        return status, json.loads(payload) if payload else {}

    def _push_html(self, user):
        h = app.Handler.__new__(app.Handler)
        return h._push(self.conn, dict(user), None, None)

    # --- A. User-Agent classification. ---

    def test_classify_desktop_user_agents(self):
        self.assertEqual(app.classify_user_agent(self.WIN_CHROME),
                         ("desktop", "chrome"))
        self.assertEqual(app.classify_user_agent(self.MAC_SAFARI),
                         ("desktop", "safari"))
        self.assertEqual(app.classify_user_agent(self.LINUX_FIREFOX),
                         ("desktop", "firefox"))
        # Edge's UA also contains Chrome/ — Edge must win.
        self.assertEqual(app.classify_user_agent(self.WIN_EDGE),
                         ("desktop", "edge"))

    def test_classify_mobile_user_agents(self):
        self.assertEqual(app.classify_user_agent(self.IPHONE_SAFARI),
                         ("iphone", "safari"))
        self.assertEqual(app.classify_user_agent(self.IPAD_SAFARI),
                         ("ipad", "safari"))
        self.assertEqual(app.classify_user_agent(self.ANDROID_CHROME),
                         ("android", "chrome"))

    def test_classify_unknown_user_agents(self):
        self.assertEqual(app.classify_user_agent(""), ("unknown", "unknown"))
        self.assertEqual(app.classify_user_agent(None),
                         ("unknown", "unknown"))
        # A bare UA says nothing usable — never guessed.
        self.assertEqual(app.classify_user_agent("Mozilla/5.0"),
                         ("unknown", "other"))

    # --- B. Device labels. ---

    def test_device_labels(self):
        cases = [
            ({"device": "desktop", "browser": "chrome"}, "כרום במחשב"),
            ({"device": "desktop", "browser": "safari"}, "ספארי במחשב"),
            ({"device": "desktop", "browser": "firefox"},
             "פיירפוקס במחשב"),
            ({"device": "desktop", "browser": "other"}, "מחשב"),
            ({"device": "iphone", "standalone": 1}, "אייפון — מסך הבית"),
            ({"device": "iphone", "standalone": 0}, "אייפון — ספארי"),
            ({"device": "ipad", "standalone": 1}, "אייפד — מסך הבית"),
            ({"device": "android", "browser": "chrome"},
             "כרום באנדרואיד"),
            ({"device": "android", "browser": "other"}, "אנדרואיד"),
            ({"device": "unknown"}, "לא ידוע"),
            ({}, "לא ידוע"),
        ]
        for sub, expected in cases:
            self.assertEqual(app.subscription_device_label(sub), expected)

    # --- C. Storage. ---

    def test_save_subscription_stores_device(self):
        self._sub(self.lev["id"], device="iphone", browser="safari",
                  standalone=True)
        row = self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(row["device"], "iphone")
        self.assertEqual(row["browser"], "safari")
        self.assertEqual(row["standalone"], 1)

    def test_save_subscription_defaults_to_unknown(self):
        # Old-style call (no device info): the row is honest about
        # not knowing, instead of guessing.
        err = app.save_subscription(self.conn, self.lev["id"],
                                    {"endpoint": "https://example.com/push/lev",
                                     "keys": {"p256dh": "k", "auth": "a"}})
        self.assertIsNone(err, err)
        row = self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(row["device"], "unknown")
        self.assertEqual(row["browser"], "unknown")
        self.assertEqual(row["standalone"], 0)

    def test_resubscribe_updates_device(self):
        # Re-subscribing from the same browser replaces the row —
        # the device info follows the newest subscribe, it does not
        # pile up a second row.
        self._sub(self.lev["id"], device="desktop", browser="chrome")
        self._sub(self.lev["id"], device="iphone", browser="safari",
                  standalone=True)
        rows = self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["device"], "iphone")
        self.assertEqual(rows[0]["standalone"], 1)

    def test_migration_adds_device_columns_to_old_db(self):
        # A database from before device detection: no device /
        # browser / standalone columns on push_subscriptions.
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, "old.db")
        conn = app.connect(db)
        schema_old = app.SCHEMA.replace(
            "    device TEXT NOT NULL DEFAULT 'unknown',\n"
            "    browser TEXT NOT NULL DEFAULT 'unknown',\n"
            "    standalone INTEGER NOT NULL DEFAULT 0,\n", "")
        conn.executescript(schema_old)
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO users (name, balance, sample, created_at)"
            " VALUES ('TestUser', 3, 1, ?)", (now,))
        uid = conn.execute(
            "SELECT id FROM users WHERE name='TestUser'").fetchone()[0]
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint,"
            " p256dh, auth, created_at) VALUES (?,"
            " 'https://example.com/push/old', 'k', 'a', ?)",
            (uid, now))
        conn.commit()
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(push_subscriptions)")]
        self.assertNotIn("device", cols)
        self.assertNotIn("browser", cols)
        self.assertNotIn("standalone", cols)
        # Run init_db: columns appear, the old row says "unknown".
        app.init_db(conn)
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(push_subscriptions)")]
        self.assertIn("device", cols)
        self.assertIn("browser", cols)
        self.assertIn("standalone", cols)
        row = conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (uid,)).fetchone()
        self.assertEqual(row["device"], "unknown")
        self.assertEqual(row["browser"], "unknown")
        self.assertEqual(row["standalone"], 0)
        # Second init_db is a no-op: nothing lost, nothing doubled.
        app.init_db(conn)
        rows = conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (uid,)).fetchall()
        self.assertEqual(len(rows), 1)
        conn.close()
        tmp.cleanup()

    # --- D. The subscribe API. ---

    def test_api_subscribe_stores_iphone_standalone(self):
        status, data = self._post_json(
            "/api/subscribe",
            {"subscription": {
                "endpoint": "https://example.com/push/iphone",
                "keys": {"p256dh": "k", "auth": "a"}},
             "standalone": True},
            cookie=self._cookie(self.lev["id"]),
            user_agent=self.IPHONE_SAFARI)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"), data)
        row = self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(row["device"], "iphone")
        self.assertEqual(row["browser"], "safari")
        self.assertEqual(row["standalone"], 1)

    def test_api_subscribe_stores_desktop(self):
        # The server never refuses a desktop subscription — the
        # warning is client-side. It records where it came from,
        # so /push can warn about it afterwards.
        status, data = self._post_json(
            "/api/subscribe",
            {"subscription": {
                "endpoint": "https://example.com/push/pc",
                "keys": {"p256dh": "k", "auth": "a"}},
             "standalone": False},
            cookie=self._cookie(self.lev["id"]),
            user_agent=self.WIN_CHROME)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"), data)
        row = self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(row["device"], "desktop")
        self.assertEqual(row["browser"], "chrome")
        self.assertEqual(row["standalone"], 0)

    def test_api_subscribe_without_user_agent_is_unknown(self):
        status, data = self._post_json(
            "/api/subscribe",
            {"subscription": {
                "endpoint": "https://example.com/push/noua",
                "keys": {"p256dh": "k", "auth": "a"}}},
            cookie=self._cookie(self.lev["id"]))
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"), data)
        row = self.conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (self.lev["id"],)).fetchone()
        self.assertEqual(row["device"], "unknown")

    def test_api_subscribe_requires_login(self):
        status, data = self._post_json(
            "/api/subscribe",
            {"subscription": {
                "endpoint": "https://example.com/push/anon",
                "keys": {"p256dh": "k", "auth": "a"}}},
            user_agent=self.IPHONE_SAFARI)
        self.assertEqual(status, 401)
        self.assertFalse(data.get("ok"))

    def test_api_subscribe_rejects_bad_subscription(self):
        status, data = self._post_json(
            "/api/subscribe", {"subscription": {}},
            cookie=self._cookie(self.lev["id"]),
            user_agent=self.WIN_CHROME)
        self.assertEqual(status, 400)
        self.assertFalse(data.get("ok"))

    # --- E. The /push page. ---

    def test_push_page_lists_devices_and_count(self):
        self._sub(self.lev["id"], "https://example.com/push/pc",
                  device="desktop", browser="chrome")
        self._sub(self.lev["id"], "https://example.com/push/phone",
                  device="iphone", browser="safari", standalone=True)
        html = self._push_html(self.lev)
        self.assertIn("המכשירים המחוברים (2)", html)
        self.assertIn("כרום במחשב", html)
        self.assertIn("אייפון — מסך הבית", html)
        # One phone is enough — no all-desktop warning.
        self.assertNotIn("כל המנויים שלך הם מהמחשב", html)

    def test_push_page_warns_when_all_desktop(self):
        self._sub(self.lev["id"], "https://example.com/push/pc1",
                  device="desktop", browser="chrome")
        self._sub(self.lev["id"], "https://example.com/push/pc2",
                  device="desktop", browser="firefox")
        html = self._push_html(self.lev)
        self.assertIn("כל המנויים שלך הם מהמחשב", html)
        self.assertIn("לא לטלפון", html)

    def test_push_page_no_warning_for_unknown_only(self):
        # Old rows (device unknown): the server cannot tell where
        # they are, so it must not warn — a wrong warning is worse
        # than none.
        self._sub(self.lev["id"], "https://example.com/push/old")
        html = self._push_html(self.lev)
        self.assertIn("לא ידוע", html)
        self.assertNotIn("כל המנויים שלך הם מהמחשב", html)

    def test_push_page_no_device_section_without_subscriptions(self):
        html = self._push_html(self.lev)
        self.assertNotIn("המכשירים המחוברים", html)
        self.assertNotIn("כל המנויים שלך הם מהמחשב", html)
        self.assertIn("ההתראות כבויות", html)

class VapidKeyNoCacheTest(Base):
    """Round 14, item 9: the VAPID public key must never be cached.
    A stale cached key makes the browser subscribe under a dead
    keypair, and every push to that subscription 403s and gets
    auto-removed — re-enabling only recreates the same broken
    subscription (found live 2026-11-10). The server sends
    no-store headers and app.js asks for no-store. What the
    browser's cache actually does with that cannot be unit-tested
    — these tests check only that the code asks for no-store."""

    def _get(self, path):
        # Drive do_GET in-process and return (status, headers, body).
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "GET"
        h.requestline = "GET %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        h.headers = {}
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_GET()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        heads = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        body = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""
        return status, heads, body

    def test_vapid_public_key_returns_no_store(self):
        status, heads, body = self._get("/vapid-public-key")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Cache-Control"), "no-store")
        self.assertEqual(heads.get("Pragma"), "no-cache")
        self.assertIn("public_key", json.loads(body))

    def test_other_json_responses_do_not_add_no_store(self):
        # The no-store is scoped to the key endpoint only — the
        # subscribe API must not gain headers it does not need.
        # (Driven through the same in-process path as the API
        # tests: a logged-in POST to /api/subscribe.)
        token = app.create_session(self.conn, self.lev["id"])
        body = json.dumps({"subscription": {
            "endpoint": "https://example.com/push/x",
            "keys": {"p256dh": "k", "auth": "a"}}}).encode("utf-8")
        h = app.Handler.__new__(app.Handler)
        h.path = "/api/subscribe"
        h.command = "POST"
        h.requestline = "POST /api/subscribe HTTP/1.1"
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.headers = {"Content-Length": str(len(body)),
                     "Content-Type": "application/json",
                     "Cookie": "seat_session=%s" % token}
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_POST()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        heads = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        self.assertNotIn("Cache-Control", heads)
        self.assertNotIn("Pragma", heads)

    def test_app_js_fetches_key_with_no_store(self):
        app_js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static", "app.js")
        with open(app_js, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('fetch("/vapid-public-key", {cache: "no-store"})',
                      src)
        # The old bare fetch must be gone — a cached key is exactly
        # the bug this item fixes.
        self.assertNotIn('fetch("/vapid-public-key")', src)

class StaleSubscriptionTest(Base):
    """Round 15: drop a stale browser-side subscription before
    subscribing fresh. The server deleting its own subscription row
    never touches the subscription the browser keeps, so
    pushManager.subscribe() hands the same old subscription back on
    every re-enable — created under a different VAPID keypair, every
    push to it 403s, it gets auto-removed, and the loop repeats
    (found live 2026-11-10, even after clearing the cache and
    re-adding the Home Screen app). app.js must compare the old
    subscription's key with the current one and unsubscribe first
    when they differ. A real browser subscription cannot be
    exercised here — these tests check the code does the check, in
    the right order, and keeps the round-14 behaviour intact."""

    def _src(self):
        app_js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static", "app.js")
        with open(app_js, encoding="utf-8") as f:
            return f.read()

    def test_checks_existing_subscription_before_subscribing(self):
        src = self._src()
        self.assertIn("pushManager.getSubscription()", src)
        self.assertLess(src.index("pushManager.getSubscription()"),
                        src.index("pushManager.subscribe("))

    def test_unsubscribes_on_key_mismatch_before_subscribing(self):
        src = self._src()
        self.assertIn("unsubscribe()", src)
        # Order: look up the old subscription, drop it, then
        # subscribe fresh — never the other way round.
        self.assertLess(src.index("pushManager.getSubscription()"),
                        src.index("unsubscribe()"))
        self.assertLess(src.index("unsubscribe()"),
                        src.index("pushManager.subscribe("))

    def test_unsubscribe_is_conditional_on_the_key_differing(self):
        # A matching key is kept, not churned: the unsubscribe runs
        # only when the old key does NOT match the current one.
        src = self._src()
        self.assertIn("if (existing && !sameKey(oldKey, keyBytes)) {",
                      src)
        self.assertIn("await existing.unsubscribe();", src)

    def test_key_comparison_is_bytewise_and_null_is_different(self):
        src = self._src()
        self.assertIn("function sameKey(a, b) {", src)
        # A missing key cannot be proven equal — it counts as
        # different, so the safe side (re-subscribe) wins.
        self.assertIn("if (!a || !b) return false;", src)
        self.assertIn("if (x.length !== y.length) return false;", src)
        self.assertIn("if (x[i] !== y[i]) return false;", src)
        # The old key is read defensively: a browser without
        # subscription.options must not crash the flow.
        self.assertIn("existing.options", src)

    def test_subscribe_uses_the_freshly_fetched_key(self):
        src = self._src()
        self.assertIn("const keyBytes = base64ToBytes(keyData.public_key);",
                      src)
        self.assertIn("applicationServerKey: keyBytes,", src)

    def test_round14_warning_and_standalone_untouched(self):
        # The device warning (confirm on desktop / iPhone Safari),
        # the standalone flag, and the honest success messages
        # must survive this change untouched.
        src = self._src()
        self.assertIn("const warning = deviceWarning();", src)
        self.assertIn("if (warning && !window.confirm(warning)) {", src)
        self.assertIn("standalone: isStandalone()", src)
        self.assertIn('"ההתראות מופעלות בטלפון הזה."', src)
        self.assertIn('"ההתראות מופעלות במחשב הזה."', src)
        self.assertIn('fetch("/vapid-public-key", {cache: "no-store"})',
                      src)

class UnsupportedBrowserMessageTest(Base):
    """Round 16: when the browser cannot do Web Push at all, say
    what to do instead of a dead-end "not supported". On iOS, push
    works only from a Home Screen web app added through Safari
    (iOS 16.4+); Chrome/Firefox/Edge on iOS (CriOS/FxiOS/EdgiOS)
    have no Service Worker, so the message must name the browser
    the user is actually in and give the exact Safari steps
    (found live 2026-11-10: Daniel opened the site in iOS Chrome
    by mistake and got only "הדפדפן הזה לא תומך"). A real browser
    cannot be exercised here — these tests check the code picks
    the right message, and keeps the round-14/15 behaviour
    intact."""

    def _src(self):
        app_js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static", "app.js")
        with open(app_js, encoding="utf-8") as f:
            return f.read()

    def test_unsupported_branch_shows_helpful_message(self):
        src = self._src()
        self.assertIn("say(unsupportedPushMessage());", src)
        # The old dead-end line must be gone.
        self.assertNotIn('say("הדפדפן הזה לא תומך בהתראות דחיפה.");',
                         src)
        self.assertIn("function unsupportedPushMessage() {", src)

    def test_ios_chrome_names_browser_and_gives_safari_steps(self):
        src = self._src()
        self.assertIn('if (/CriOS/.test(ua)) browser = "כרום";', src)
        # The message names Safari, the Share step and the Home
        # Screen app — the whole way out, not just the problem.
        self.assertIn("ספארי", src)
        self.assertIn("הוסף למסך הבית", src)
        self.assertIn("הפעל התראות", src)
        self.assertIn("מסך הבית", src)

    def test_ios_firefox_and_edge_detected(self):
        src = self._src()
        self.assertIn('else if (/FxiOS/.test(ua)) browser = "פיירפוקס";',
                      src)
        self.assertIn('else if (/EdgiOS/.test(ua)) browser = "אדג׳";',
                      src)

    def test_ios_safari_old_version_message(self):
        # iOS Safari without push (too old): same Home Screen
        # steps, no browser switch, plus update iOS.
        src = self._src()
        self.assertIn("16.4", src)
        self.assertIn("עדכן את iOS לגרסה האחרונה", src)

    def test_generic_unsupported_browser_message(self):
        # Any other unsupported browser: still says what to do —
        # a current desktop browser, or the phone via Safari +
        # Home Screen.
        src = self._src()
        self.assertIn("הדפדפן הזה לא תומך בהתראות דחיפה.", src)
        self.assertIn("בכרום, אדג׳ או פיירפוקס עדכניים במחשב", src)
        self.assertIn("דרך ספארי ואז ״הוסף למסך הבית״", src)

    def test_round14_and_round15_logic_untouched(self):
        # The device warning, the standalone flag, the no-store
        # key fetch, and the unsubscribe-before-subscribe logic
        # must survive this change untouched.
        src = self._src()
        self.assertIn("const warning = deviceWarning();", src)
        self.assertIn("standalone: isStandalone()", src)
        self.assertIn('fetch("/vapid-public-key", {cache: "no-store"})',
                      src)
        self.assertIn("pushManager.getSubscription()", src)
        self.assertIn("if (existing && !sameKey(oldKey, keyBytes)) {",
                      src)
        self.assertIn("await existing.unsubscribe();", src)

class VersionedStaticTest(Base):
    """Round 16, second fix: the phone kept running CACHED OLD
    app.js after a deploy, so the round-15 unsubscribe fix never
    ran on the phone — even after deleting and re-adding the Home
    Screen app, the old file came back (found live 2026-11-10;
    the server was verified serving round-15 code while the
    phone still recreated the broken old-key subscription). A
    no-store header on future responses cannot invalidate a copy
    the phone already cached — only a NEW URL can. So the script
    tag and the Service Worker registration carry ?v=21, and
    _serve_static strips the query, serves the right file, and
    sends no-store so this cannot happen again next round."""

    def _get(self, path):
        # Same in-process driver as VapidKeyNoCacheTest.
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "GET"
        h.requestline = "GET %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        h.headers = {}
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_GET()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        heads = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        body = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""
        return status, heads, body

    def _app_src(self):
        with open(os.path.abspath(app.__file__),
                  encoding="utf-8") as f:
            return f.read()

    def _js_src(self):
        app_js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "static", "app.js")
        with open(app_js, encoding="utf-8") as f:
            return f.read()

    def test_script_tag_is_versioned(self):
        src = self._app_src()
        # The file holds the tag escaped (\"), so match that form.
        self.assertIn('<script src=\\"/static/app.js?v=21\\" defer>',
                      src)
        # No unversioned script tag left anywhere.
        self.assertNotIn('<script src=\\"/static/app.js\\" defer>',
                         src)

    def test_service_worker_registration_is_versioned(self):
        src = self._js_src()
        self.assertIn('register("/sw.js?v=21")', src)
        self.assertNotIn('register("/sw.js")', src)

    def test_versioned_app_js_served_with_no_store(self):
        status, heads, body = self._get("/static/app.js?v=21")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Cache-Control"), "no-store")
        self.assertEqual(heads.get("Pragma"), "no-cache")
        with open(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "static", "app.js"), encoding="utf-8") as f:
            self.assertEqual(body, f.read())
        # The served file is the new one: the round-16 message
        # and the round-15 unsubscribe logic are both in it.
        self.assertIn("unsupportedPushMessage", body)
        self.assertIn("pushManager.getSubscription()", body)
        self.assertIn("unsubscribe()", body)

    def test_versioned_sw_js_served_with_no_store(self):
        status, heads, body = self._get("/sw.js?v=21")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Cache-Control"), "no-store")
        self.assertEqual(heads.get("Pragma"), "no-cache")
        with open(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "static", "sw.js"), encoding="utf-8") as f:
            self.assertEqual(body, f.read())

    def test_unversioned_static_still_served(self):
        # The plain URL keeps working (backward compatible) —
        # only the query is new.
        status, heads, body = self._get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Cache-Control"), "no-store")

    def test_unknown_static_still_404(self):
        # The query must not smuggle a different file past the
        # allow-list: only app.js and sw.js are ever served.
        status, heads, body = self._get("/static/evil.js?v=21")
        self.assertEqual(status, 404)

    def test_round16_message_and_round14_15_intact(self):
        src = self._js_src()
        self.assertIn("function unsupportedPushMessage() {", src)
        self.assertIn('if (/CriOS/.test(ua)) browser = "כרום";', src)
        self.assertIn("const warning = deviceWarning();", src)
        self.assertIn("standalone: isStandalone()", src)
        self.assertIn('fetch("/vapid-public-key", {cache: "no-store"})',
                      src)
        self.assertIn("if (existing && !sameKey(oldKey, keyBytes)) {",
                      src)
        self.assertIn("await existing.unsubscribe();", src)

class HtmlNoStoreTest(Base):
    """Round 17: no-store on HTML pages too. Round 16 versioned
    the JavaScript (?v=21) so the phone cannot reuse a cached
    app.js — but the versioned URL only helps once the page
    itself is fresh. Live 2026-11-10, after the round-16 deploy:
    Daniel deleted and re-added the Home Screen icon, opened
    fresh, tapped enable, and the test still 403d — the phone
    loaded a STALE CACHED /push page, which still references the
    unversioned /static/app.js, so the old code ran and created
    another old-key subscription. The old subscription was then
    auto-removed (14:33:57), so the next test (14:36:07) had no
    phone subscription left to send to at all — only the PC's
    "sent" row. So every HTML page goes out with no-store, the
    same way the .js files do since round 16. A real browser
    push subscription cannot be exercised here — these tests
    check the headers on the served pages."""

    def _get(self, path, cookie=None):
        # Same in-process driver as VersionedStaticTest, with an
        # optional session cookie for logged-in pages.
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "GET"
        h.requestline = "GET %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        headers = {}
        if cookie is not None:
            headers["Cookie"] = cookie
        h.headers = headers
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_GET()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        heads = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        body = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""
        return status, heads, body

    def _assert_no_store(self, heads):
        self.assertEqual(heads.get("Cache-Control"), "no-store")
        self.assertEqual(heads.get("Pragma"), "no-cache")

    def test_all_page_routes_send_no_store(self):
        # Every page route, anonymous: the home page, the forms,
        # the lists, the forbidden admin page (403) and the
        # unknown page (404) — all HTML, all no-store.
        for path in ("/", "/register", "/login", "/set-password",
                     "/courses", "/offers", "/wanted", "/admin",
                     "/offer", "/edit-offer", "/follows", "/my",
                     "/push", "/help", "/no-such-page"):
            status, heads, body = self._get(path)
            self.assertIn(status, (200, 403, 404), path)
            self.assertEqual(heads.get("Content-Type"),
                             "text/html; charset=utf-8", path)
            self._assert_no_store(heads)

    def test_logged_in_pages_send_no_store(self):
        cookie = "seat_session=%s" % app.create_session(
            self.conn, self.daniel["id"])
        for path in ("/", "/push", "/my", "/admin", "/offers"):
            status, heads, body = self._get(path, cookie=cookie)
            self.assertEqual(status, 200, path)
            self._assert_no_store(heads)

    def test_push_page_references_versioned_script(self):
        # The fresh page must point at the versioned script —
        # that is what the no-store on HTML protects.
        cookie = "seat_session=%s" % app.create_session(
            self.conn, self.daniel["id"])
        status, heads, body = self._get("/push", cookie=cookie)
        self.assertEqual(status, 200)
        self._assert_no_store(heads)
        self.assertIn('<script src="/static/app.js?v=21" defer>',
                      body)
        self.assertNotIn('<script src="/static/app.js" defer>', body)

    def test_versioned_js_still_served_with_no_store(self):
        # Regression guard: the round-16 .js behaviour is intact.
        status, heads, body = self._get("/static/app.js?v=21")
        self.assertEqual(status, 200)
        self._assert_no_store(heads)
        self.assertIn("unsupportedPushMessage", body)
        self.assertIn("pushManager.getSubscription()", body)

class Round19Test(Base):
    """Round 19: Home Screen icon, help rewrite, dark mode, /today,
    clearer offer cards, course colours, onboarding, when filter."""

    def _handler(self):
        return app.Handler.__new__(app.Handler)

    def _get_raw(self, path):
        # Like VapidKeyNoCacheTest._get, but the body comes back as
        # raw bytes — the icon PNGs are binary, and decoding them as
        # UTF-8 would corrupt the magic bytes we check.
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "GET"
        h.requestline = "GET %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        h.headers = {}
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_GET()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split()[1])
        heads = {}
        for line in head.decode("latin-1").split("\r\n")[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        return status, heads, body

    def _page(self, fn, user=None, params=None):
        # Call a page method and collapse whitespace, like the help
        # tests do — the page source wraps long lines.
        h = self._handler()
        if params is None:
            html = fn(h, self.conn, user, None, None)
        else:
            html = fn(h, self.conn, user, None, None, params)
        return " ".join(html.split())

    def _offer_row(self, offer_id):
        return self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer_id,)).fetchone()

    def _two_offers(self):
        # One open offer per course: תכנון אלגוריתמים (today's
        # session from setUp) and חדו״א 2 (a session added today).
        calc = self.conn.execute(
            "SELECT * FROM courses WHERE name='חדו״א 2'").fetchone()
        calc_session = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '35', '2')"
            " RETURNING *",
            (calc["id"], self.today, self.start, self.end)).fetchone()
        self.conn.commit()
        alg_offer = self._offer()
        calc_offer, err = app.create_offer(
            self.conn, self.daniel["id"], calc_session["id"], "", 1)
        self.assertIsNone(err, err)
        return alg_offer, calc_offer, calc

    # --- 1. Home Screen icon. ---

    def test_icon_180_served_as_png(self):
        status, heads, body = self._get_raw("/static/icon-180.png")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Content-Type"), "image/png")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(heads.get("Cache-Control"), "no-store")

    def test_favicon_served_as_png(self):
        status, heads, body = self._get_raw("/static/favicon.png")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Content-Type"), "image/png")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_manifest_served_as_json(self):
        status, heads, body = self._get_raw("/static/manifest.json")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Content-Type"),
                         "application/manifest+json")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["name"], "שומר מקומות")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(data["theme_color"], "#14532d")
        self.assertTrue(any(i["sizes"] == "192x192"
                            for i in data["icons"]))
        self.assertTrue(any(i["sizes"] == "512x512"
                            for i in data["icons"]))

    def test_page_links_icon_manifest_theme_and_today(self):
        html = self._page(app.Handler._home, None)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("/static/icon-180.png", html)
        self.assertIn('rel="manifest"', html)
        self.assertIn("/static/manifest.json", html)
        self.assertIn('name="theme-color"', html)
        self.assertIn('href="/today"', html)

    def test_unknown_static_still_404(self):
        status, _heads, _body = self._get_raw("/static/not-here.png")
        self.assertEqual(status, 404)

    # --- 2. Help rewrite: 4 steps, rules, FAQ, troubleshooting. ---

    def test_help_has_how_it_works_faq_and_troubleshooting(self):
        html = self._page(app.Handler._help, None)
        # Short intro, not a wall of text.
        self.assertIn("<h2>איך זה עובד</h2>", html)
        self.assertIn("שאלות נפוצות", html)
        self.assertIn("<details>", html)
        self.assertIn("התראה לא הגיעה? מה לבדוק", html)
        # Problem -> what to do: each troubleshooting line names
        # the problem first.
        self.assertIn("מה לעשות", html)
        # Four sections only (the old page had about twenty).
        self.assertEqual(html.count("<h2>"), 4)
        # The "how it works" intro is Daniel's own wording
        # (2026-11-10): three short numbered steps — the design
        # is unchanged (numbered list, bold headings, collapsed
        # FAQ), only the words changed.
        self.assertIn("נרשמים עם שם מלא וטלפון", html)
        self.assertIn("עוקבים אחרי הקורסים שלכם", html)
        self.assertIn("משלמים מטבע", html)
        self.assertIn("המקום לא היה שם", html)
        self.assertIn("<ol>", html)

    def test_help_keeps_all_rules(self):
        html = self._page(app.Handler._help, None)
        for word in ("מטבע", "רצפה", "מינוס 2", "אייפון", "סיסמה",
                     "60", "10", "מעקב", "התראות", "שעות שקטות",
                     "מחפשים מקום",
                     "בקשה אוטומטית כל שבוע"):
            self.assertIn(word, html)
        self.assertNotIn("אזור", html)
        self.assertNotIn("הצעה אוטומטית כל שבוע", html)

    def test_help_rewrite_is_short_and_floor_is_right(self):
        # Daniel, 2026-11-10: the page was far too long and the
        # floor wording was wrong (it read as if balance 0 cannot
        # request). The rewrite is short — 6 one-line rules — and
        # the floor is stated plainly in Daniel's own wording:
        # the balance can go down to -2, and only AT -2 is
        # requesting blocked. (His edit dropped the explicit
        # "balance 0 can request" line — the wording no longer
        # says anything wrong about balance 0.)
        html = self._page(app.Handler._help, None)
        self.assertIn("כשמגיעים למינוס 2 אי אפשר לבקש", html)
        self.assertIn("המאזן יכול לרדת עד מינוס 2", html)
        self.assertNotIn("עם רצפה 0", html)
        # Daniel's coins line: the saver earns, the requester spends.
        self.assertIn("השומר מקבל מטבע על כל שמירת כיסא", html)
        self.assertIn("מי שמבקש כיסא מבזבז מטבע", html)
        # The rules list is 6 items, not ~27 paragraphs.
        rules = html.split("<h2>כללים בקצרה</h2>")[1]
        rules = rules.split("<h2>שאלות נפוצות</h2>")[0]
        self.assertEqual(rules.count("<li>"), 6)
        # Cut in the rewrite: the obvious "I'm on my way" rule, and
        # the theme item (which also still named the removed system
        # mode).
        self.assertNotIn("אני בדרך", html)
        self.assertNotIn("מערכת / בהיר / כהה", html)
        # The details moved to the FAQ, collapsed.
        self.assertIn("מה זה בקשה אוטומטית כל שבוע?", html)
        self.assertIn("איך משתיקים התראות?", html)
        # Daniel's own FAQ wording (2026-11-10): 14 items — the
        # "מה קורה כשחולקים?" item was dropped by him.
        self.assertIn("התלונה תיבדק", html)
        self.assertIn("תתבצע אוטומטית בקשה לשמירת מקום", html)
        self.assertNotIn("מה קורה כשחולקים", html)
        faq = html.split("<h2>שאלות נפוצות</h2>")[1]
        faq = faq.split("<h2>התראה לא הגיעה? מה לבדוק</h2>")[0]
        self.assertEqual(faq.count("<details>"), 14)

    # --- 3. Dark mode completed. ---

    def test_dark_mode_covers_links_and_new_classes(self):
        css = app.CSS
        # No system mode any more (bug report 2026-11-10): the dark
        # colours live ONLY under html[data-theme="dark"].
        self.assertIn('html[data-theme="dark"]', css)
        self.assertNotIn("prefers-color-scheme", css)
        # Links were dark green on a dark background before.
        self.assertIn("a{color:#86efac}", css)
        self.assertIn(".muted{color:#9ca3af}", css)
        # The new round-19 classes have dark colours too.
        self.assertIn(".offer-time{color:#d1d5db}", css)
        self.assertIn(".offer-seats{color:#86efac}", css)
        self.assertIn(".onboarding{background:#052e16", css)

    # --- 4. /today page. ---

    def test_today_page_shows_today_sessions_and_offers(self):
        self._offer()
        html = self._page(app.Handler._today, dict(self.lev))
        self.assertIn("השיעורים של היום", html)
        self.assertIn("הצעות פתוחות להיום", html)
        self.assertIn('class="card offer"', html)
        self.assertIn("תכנון אלגוריתמים", html)

    def test_today_page_excludes_other_day_offers(self):
        # An open offer for a session on another weekday must not
        # appear on /today — the page is only today.
        other_wd = (self.today + 1) % 7
        sess = self.conn.execute(
            "INSERT INTO course_sessions (course_id, weekday, start_time,"
            " end_time, building, room) VALUES (?, ?, ?, ?, '34', '214')"
            " RETURNING *",
            (self.course["id"], other_wd, self.start, self.end)).fetchone()
        tomorrow = (datetime.now().date()
                    + timedelta(days=1)).strftime("%Y-%m-%d")
        self.conn.execute(
            "INSERT INTO offers (course_id, session_id, saver_id,"
            " class_date, note, seats_total, status, created_at)"
            " VALUES (?, ?, ?, ?, 'מחר-בלבד', 1, 'open', ?)",
            (self.course["id"], sess["id"], self.daniel["id"], tomorrow,
             datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
        self.conn.commit()
        html = self._page(app.Handler._today, dict(self.lev))
        self.assertNotIn("מחר-בלבד", html)
        # ...but the same offer IS on the offers page (it is open).
        html = self._page(app.Handler._offers, dict(self.lev))
        self.assertIn("מחר-בלבד", html)

    def test_today_route_exists(self):
        status, _heads, body = self._get_raw("/today")
        self.assertEqual(status, 200)
        self.assertIn("השיעורים של היום", body.decode("utf-8"))

    # --- 5. Clearer offer cards. ---

    def test_offer_card_structure(self):
        offer = self._offer()
        row = self._offer_row(offer["id"])
        html = self._handler()._offer_card(self.conn, row, dict(self.lev))
        # Course first and biggest, then time, then seats (big).
        self.assertIn('class="offer-title"', html)
        self.assertIn('class="offer-time"', html)
        self.assertIn('class="offer-seats"', html)
        self.assertIn("נשארו 1 מתוך 1 מקומות", html)
        self.assertIn("שומר: Daniel", html)
        # One clear action: a single full-width request button.
        self.assertIn('class="big"', html)
        self.assertIn("בקש מקום", html)

    # --- 6. Fixed colour per course. ---

    def test_course_color_deterministic_and_hex(self):
        c1 = app.course_color(1)
        self.assertEqual(c1, app.course_color(1))
        self.assertRegex(c1, r"^#[0-9a-f]{6}$")

    def test_seed_courses_get_different_colors(self):
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM courses ORDER BY id LIMIT 3")]
        colors = [app.course_color(i) for i in ids]
        self.assertEqual(len(set(colors)), 3)

    def test_offer_card_shows_course_color(self):
        offer = self._offer()
        row = self._offer_row(offer["id"])
        html = self._handler()._offer_card(self.conn, row, dict(self.lev))
        self.assertIn(app.course_color(row["course_id"]), html)
        self.assertIn('class="course-dot"', html)

    def test_course_dot_on_follows_courses_and_home(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        html = self._page(app.Handler._follows, dict(self.lev))
        self.assertIn('class="course-dot"', html)
        html = self._page(app.Handler._courses, dict(self.lev))
        self.assertIn('class="course-dot"', html)
        html = self._page(app.Handler._home, dict(self.lev))
        self.assertIn('class="course-dot"', html)

    # --- 7. New-user onboarding. ---

    def test_onboarding_shown_when_no_follows(self):
        # Lev follows nothing in a fresh test DB.
        html = self._page(app.Handler._home, dict(self.lev))
        self.assertIn("מתחילים? 3 צעדים", html)
        self.assertIn("/follows", html)
        self.assertIn("/push", html)
        self.assertIn("/offers", html)

    def test_onboarding_hidden_when_has_follow(self):
        app.add_follow(self.conn, self.lev["id"], self.course["id"])
        html = self._page(app.Handler._home, dict(self.lev))
        self.assertNotIn("מתחילים? 3 צעדים", html)

    def test_onboarding_hidden_when_logged_out(self):
        html = self._page(app.Handler._home, None)
        self.assertNotIn("מתחילים? 3 צעדים", html)

    # --- 8. Filter offers by today / week. ---

    def test_offers_when_filter_today_and_week(self):
        self._offer()
        # This offer is for today, so today and week both show it.
        for when in ("today", "week"):
            html = self._page(app.Handler._offers, dict(self.daniel),
                              {"when": [when]})
            self.assertEqual(html.count('class="card offer"'), 1)
            self.assertIn("סינון לפי זמן:", html)
        # An unknown value means "all", like an unknown course id.
        html = self._page(app.Handler._offers, dict(self.daniel),
                          {"when": ["bogus"]})
        self.assertEqual(html.count('class="card offer"'), 1)

    def test_offers_when_filter_excludes_other_dates(self):
        # An open offer dated yesterday is hidden by ?when=today
        # but shown with no time filter — the filter reads class_date.
        offer = self._offer()
        yesterday = (datetime.now().date()
                     - timedelta(days=1)).strftime("%Y-%m-%d")
        self.conn.execute("UPDATE offers SET class_date=? WHERE id=?",
                          (yesterday, offer["id"]))
        self.conn.commit()
        html = self._page(app.Handler._offers, dict(self.daniel),
                          {"when": ["today"]})
        self.assertEqual(html.count('class="card offer"'), 0)
        html = self._page(app.Handler._offers, dict(self.daniel), {})
        self.assertEqual(html.count('class="card offer"'), 1)

    def test_offers_when_and_course_filters_combine(self):
        _alg, _calc_offer, calc = self._two_offers()
        html = self._page(app.Handler._offers, dict(self.daniel),
                          {"course_id": [str(calc["id"])],
                           "when": ["today"]})
        self.assertEqual(html.count('class="card offer"'), 1)
        self.assertIn("חדו״א 2", html)
        self.assertIn("מסונן:", html)
        # The time-filter links keep the course filter.
        self.assertIn("course_id=%s" % calc["id"], html)

class Round20Test(Base):
    """Round 20: full name + phone + VERIFIED badge, account deletion,
    complaints, broadcast, one-tap offer, dead-subscription banner,
    dark-mode toggle, push action buttons, "who's coming", "your saved
    seat" card, weekly view. Every new behaviour has a test here —
    the unit tests are the first line of defence, and the multi-account
    live QA (see EXPLANATION.md) is the second."""

    def _new_user(self, name, phone="0501234567", password="secret123"):
        user, err = app.create_user(self.conn, name, password, phone)
        self.assertIsNone(err, err)
        return user

    def _admin(self):
        return self._new_user("דניאל ביק", "0509999999", "admin1234")

    def _cookie(self, user):
        return "seat_session=%s" % app.create_session(self.conn, user["id"])

    # In-process HTTP drivers, copied from Round14Test / HtmlNoStoreTest
    # / Round19Test: a bare Handler with a BytesIO body, and connect()
    # pointed at this test's database. Returns (status, headers, body).
    def _post(self, path, fields, cookie=None):
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "POST"
        h.requestline = "POST %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        headers = {"Content-Length": str(len(body))}
        if cookie is not None:
            headers["Cookie"] = cookie
        h.headers = headers
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_POST()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        heads = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        return status, heads, raw

    def _post_json(self, path, obj, cookie=None):
        body = json.dumps(obj).encode("utf-8")
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "POST"
        h.requestline = "POST %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        headers = {"Content-Length": str(len(body)),
                   "Content-Type": "application/json"}
        if cookie is not None:
            headers["Cookie"] = cookie
        h.headers = headers
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_POST()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        payload = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""
        return status, json.loads(payload) if payload else {}

    def _get(self, path, cookie=None):
        h = app.Handler.__new__(app.Handler)
        h.path = path
        h.command = "GET"
        h.requestline = "GET %s HTTP/1.1" % path
        h.request_version = "HTTP/1.1"
        h.rfile = io.BytesIO(b"")
        h.wfile = io.BytesIO()
        headers = {}
        if cookie is not None:
            headers["Cookie"] = cookie
        h.headers = headers
        h._headers_buffer = []
        orig = app.connect.__defaults__
        app.connect.__defaults__ = (self.db,)
        try:
            h.do_GET()
        finally:
            app.connect.__defaults__ = orig
        raw = h.wfile.getvalue().decode("utf-8", "replace")
        status = int(raw.split()[1])
        heads = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            if ": " in line:
                k, v = line.split(": ", 1)
                heads[k] = v
        body = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""
        return status, heads, body

    def _decide(self, req, how, requester_id=None):
        requester_id = requester_id or self.lev["id"]
        if how == "confirmed":
            return app.confirm_arrival(self.conn, req["id"], requester_id)
        if how == "not_saved":
            return app.report_not_saved(self.conn, req["id"], requester_id)
        if how == "cancelled":
            return app.cancel_request(self.conn, req["id"], requester_id)
        raise ValueError(how)

    def _fake_sender(self, sent):
        def fake(subscription, payload):
            sent.append((subscription, payload))
        return fake

    # --- Item 13/15: full name + phone validation. ---

    def test_full_name_validation(self):
        # Single word, one-character word, empty, too long: rejected.
        for bad in ("Noa", "דניאל", "   ", "", "אב",
                    "Noa M", "x" * 41):
            _, err = app.create_user(self.conn, bad, "secret123",
                                     "0501234567")
            self.assertIsNotNone(err, bad)
        # Two words, each at least 2 characters: accepted.
        user, err = app.create_user(self.conn, "Noa Malka", "secret123",
                                    "0501234567")
        self.assertIsNone(err, err)
        self.assertEqual(user["name"], "Noa Malka")

    def test_phone_validation(self):
        for bad in (None, "", "abc", "050-123", "03-1234567",
                    "050123456", "05012345678"):
            _, err = app.create_user(self.conn, "Noa Malka", "secret123",
                                     bad)
            self.assertIsNotNone(err, bad)
        # The usual ways people type it: all accepted, all stored
        # normalized (05XXXXXXXX).
        for good in ("050-1234567", "0501234567", "+972501234567",
                     "972501234567", "050 1234567"):
            name = "Test User %s" % good.replace("+", "p").replace(" ", "s").replace("-", "d")
            user, err = app.create_user(self.conn, name, "secret123", good)
            self.assertIsNone(err, (good, err))
            self.assertEqual(user["phone"], "0501234567")

    def test_phone_display_and_whatsapp_link(self):
        self.assertEqual(app._phone_display("0501234567"), "050-1234567")
        self.assertEqual(app._phone_display("050-1234567"), "050-1234567")
        self.assertEqual(app._phone_display("garbage"), "")
        self.assertEqual(app._wa_link("0501234567"),
                         "https://wa.me/972501234567")
        self.assertEqual(app._wa_link("garbage"), "")

    def test_registration_http_requires_phone(self):
        # No phone in the form: the registration fails, server-side
        # (a browser that skips the `required` attribute cannot
        # register without one).
        status, heads, _raw = self._post("/register", {
            "name": "רוני כהן",
            "password": "secret123",
            "password2": "secret123",
        })
        self.assertEqual(status, 303)
        self.assertIn("/register?error=", heads["Location"])
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM users WHERE name='רוני כהן'").fetchone())
        # A single-word name fails too.
        status, heads, _raw = self._post("/register", {
            "name": "רוני",
            "phone": "0501234567",
            "password": "secret123",
            "password2": "secret123",
        })
        self.assertIn("/register?error=", heads["Location"])

    def test_user_needs_profile(self):
        # A seeded user (single-word name, no phone) needs a profile.
        self.assertTrue(app.user_needs_profile(dict(self.daniel)))
        # A full-name user with a phone does not.
        user = self._new_user("Noa Malka")
        self.assertFalse(app.user_needs_profile(dict(user)))
        # A full-name user without a phone does.
        self.conn.execute("UPDATE users SET phone=NULL WHERE id=?",
                          (user["id"],))
        self.conn.commit()
        user = app.get_user(self.conn, user["id"])
        self.assertTrue(app.user_needs_profile(dict(user)))

    def test_home_banner_for_missing_profile(self):
        h = app.Handler.__new__(app.Handler)
        html = h._home(self.conn, dict(self.daniel), None, None)
        self.assertIn("הפרופיל שלך לא מלא", html)
        # Bug report 2026-11-10: the banner must jump straight to
        # the profile section, not to the top of /my.
        self.assertIn("/my#profile", html)
        user = self._new_user("Noa Malka")
        html = h._home(self.conn, dict(user), None, None)
        self.assertNotIn("הפרופיל שלך לא מלא", html)

    def test_profile_link_in_nav_and_anchor_on_my(self):
        # Bug report 2026-11-10: the profile was not in an
        # accessible place. The nav links straight to the profile
        # section, and /my has the matching anchor.
        html = app.page_html("כותרת", "<p>גוף</p>")
        self.assertIn('href="/my#profile"', html)
        self.assertIn("הפרופיל שלי", html)
        h = app.Handler.__new__(app.Handler)
        my_html = h._my(self.conn, dict(self.daniel), None, None)
        self.assertIn('<h2 id="profile">הפרופיל שלי</h2>', my_html)

    # --- Item 7: change display name. ---

    def test_rename_to_full_name(self):
        user = self._new_user("Noa Malka")
        app.add_follow(self.conn, user["id"], self.course["id"])
        offer = self._offer(saver=user)
        self.assertIsNone(app.rename_user(self.conn, user["id"],
                                          "Noa Cohen"))
        row = app.get_user(self.conn, user["id"])
        self.assertEqual(row["name"], "Noa Cohen")
        # The account is untouched: same coins, same follows, same offer.
        self.assertEqual(row["balance"], app.START_BALANCE)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM follows WHERE user_id=?",
            (user["id"],)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM offers WHERE saver_id=?",
            (user["id"],)).fetchone()[0], 1)

    def test_rename_rejects_nickname_and_taken_name(self):
        user = self._new_user("Noa Malka")
        self.assertIsNotNone(app.rename_user(self.conn, user["id"], "Noa"))
        self.assertIsNotNone(app.rename_user(self.conn, user["id"], ""))
        other = self._new_user("Maya Levi")
        self.assertIsNotNone(app.rename_user(self.conn, user["id"],
                                             "Maya Levi"))
        # The name did not change.
        self.assertEqual(app.get_user(self.conn, user["id"])["name"],
                         "Noa Malka")

    def test_rename_to_admin_name_rejected(self):
        user = self._new_user("Noa Malka")
        self.assertIsNotNone(app.rename_user(self.conn, user["id"],
                                             "דניאל ביק"))

    def test_rename_admin_rejected(self):
        admin = self._admin()
        self.assertIsNotNone(app.rename_user(self.conn, admin["id"],
                                             "דניאל אחר"))

    def test_rename_resets_verified(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           user["id"], True))
        self.assertEqual(app.get_user(self.conn, user["id"])["verified"], 1)
        self.assertIsNone(app.rename_user(self.conn, user["id"],
                                          "Noa Cohen"))
        self.assertEqual(app.get_user(self.conn, user["id"])["verified"], 0)

    def test_rename_http(self):
        user = self._new_user("Noa Malka")
        status, heads, _raw = self._post("/rename", {"name": "Noa Cohen"},
                                         cookie=self._cookie(user))
        self.assertEqual(status, 303)
        self.assertEqual(heads["Location"], "/my?saved=1")
        self.assertEqual(app.get_user(self.conn, user["id"])["name"],
                         "Noa Cohen")

    # --- Item 13: phone. ---

    def test_set_phone_validates_and_resets_verified(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           user["id"], True))
        self.assertIsNone(app.set_phone(self.conn, user["id"],
                                        "052-7654321"))
        row = app.get_user(self.conn, user["id"])
        self.assertEqual(row["phone"], "0527654321")
        # Changing the number resets the badge — the number the
        # admin checked is no longer the number.
        self.assertEqual(row["verified"], 0)
        self.assertIsNotNone(app.set_phone(self.conn, user["id"],
                                           "03-1234567"))

    # --- Item 15: VERIFIED badge. ---

    def test_admin_verify_sets_badge(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           user["id"], True))
        self.assertEqual(app.get_user(self.conn, user["id"])["verified"], 1)
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           user["id"], False))
        self.assertEqual(app.get_user(self.conn, user["id"])["verified"], 0)

    def test_verify_requires_admin_and_rejects_admin(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        other = self._new_user("Maya Levi")
        # A non-admin cannot verify anyone.
        self.assertIsNotNone(app.set_verified(self.conn, dict(other),
                                              user["id"], True))
        self.assertEqual(app.get_user(self.conn, user["id"])["verified"], 0)
        # An admin account cannot be (un)verified.
        self.assertIsNotNone(app.set_verified(self.conn, dict(admin),
                                              admin["id"], True))

    def test_verified_badge_html(self):
        self.assertEqual(app.verified_badge_html({"verified": 0}), "")
        self.assertEqual(app.verified_badge_html({"verified": None}), "")
        self.assertEqual(app.verified_badge_html(None), "")
        badge = app.verified_badge_html({"verified": 1})
        self.assertIn("✓", badge)
        self.assertIn('class="verified"', badge)

    def test_badge_shown_on_offer_card_when_verified(self):
        admin = self._admin()
        saver = self._new_user("Noa Malka")
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           saver["id"], True))
        offer = self._offer(saver=saver)
        h = app.Handler.__new__(app.Handler)
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name,"
            " u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        html = h._offer_card(self.conn, row, dict(self.lev))
        self.assertIn('class="verified"', html)
        self.assertIn("✓", html)

    def test_badge_not_shown_when_unverified(self):
        saver = self._new_user("Noa Malka")
        offer = self._offer(saver=saver)
        h = app.Handler.__new__(app.Handler)
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name,"
            " u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        html = h._offer_card(self.conn, row, dict(self.lev))
        self.assertNotIn('class="verified"', html)

    def test_admin_user_list_shows_phone_coins_verified(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           user["id"], True))
        h = app.Handler.__new__(app.Handler)
        html = h._admin(self.conn, dict(admin), None, None)
        self.assertIn("Noa Malka", html)
        self.assertIn("050-1234567", html)
        self.assertIn("3", html)
        self.assertIn('class="verified"', html)
        self.assertIn("אמת", html)  # the unverify button for a verified user
        self.assertIn("מחק", html)

    def test_admin_verify_http(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        status, heads, _raw = self._post(
            "/admin-verify", {"user_id": str(user["id"]), "verified": "1"},
            cookie=self._cookie(admin))
        self.assertEqual(status, 303)
        self.assertEqual(heads["Location"], "/admin?saved=1")
        self.assertEqual(app.get_user(self.conn, user["id"])["verified"], 1)

    # --- Item 6: account deletion. ---

    def test_delete_saver_refunds_frozen_coins(self):
        saver = self._new_user("Noa Malka")
        requester = self._new_user("Maya Levi")
        offer = self._offer(saver=saver)
        req = self._request(offer, requester=requester)
        self.assertEqual(app.get_user(self.conn, requester["id"])["balance"],
                         app.START_BALANCE - 1)
        name, err = app.delete_account(self.conn, saver["id"])
        self.assertIsNone(err, err)
        self.assertEqual(name, "Noa Malka")
        # The requester got their coin back, the request and the
        # offer are gone, and the saver is gone.
        self.assertEqual(app.get_user(self.conn, requester["id"])["balance"],
                         app.START_BALANCE)
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM offers WHERE id=?", (offer["id"],)).fetchone())
        self.assertIsNone(app.get_user(self.conn, saver["id"]))

    def test_delete_requester_frees_seat(self):
        saver = self._new_user("Noa Malka")
        requester = self._new_user("Maya Levi")
        offer = self._offer(saver=saver, seats=1)
        req = self._request(offer, requester=requester)
        self.assertEqual(app.seats_available(self.conn, offer), 0)
        name, err = app.delete_account(self.conn, requester["id"])
        self.assertIsNone(err, err)
        self.assertEqual(app.seats_available(self.conn, offer), 1)
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM requests WHERE id=?", (req["id"],)).fetchone())

    def test_delete_account_removes_everything(self):
        user = self._new_user("Noa Malka")
        app.add_follow(self.conn, user["id"], self.course["id"])
        app.save_subscription(self.conn, user["id"], {
            "endpoint": "https://example.com/push/1",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer(saver=user)
        self._request(offer, requester=self.lev)
        app.post_seat_request(self.conn, user["id"], self.session["id"])
        name, err = app.delete_account(self.conn, user["id"])
        self.assertIsNone(err, err)
        for table, col in (("follows", "user_id"),
                           ("push_subscriptions", "user_id"),
                           ("seat_requests", "user_id"),
                           ("offers", "saver_id")):
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM %s WHERE %s=?" % (table, col),
                (user["id"],)).fetchone()[0], 0, table)

    def test_delete_admin_rejected(self):
        admin = self._admin()
        name, err = app.delete_account(self.conn, admin["id"])
        self.assertIsNotNone(err)
        self.assertIsNone(name)
        self.assertIsNotNone(app.get_user(self.conn, admin["id"]))

    def test_delete_confirmation_page_get_deletes_nothing(self):
        user = self._new_user("Noa Malka")
        status, heads, body = self._get(
            "/delete-account", cookie=self._cookie(user))
        self.assertEqual(status, 200)
        self.assertIn("לא הפיך", body)
        self.assertIn("מחק את החשבון שלי", body)
        # Still there after the GET.
        self.assertIsNotNone(app.get_user(self.conn, user["id"]))

    def test_delete_own_account_http_logs_out(self):
        user = self._new_user("Noa Malka")
        status, heads, _raw = self._post(
            "/delete-account", {}, cookie=self._cookie(user))
        self.assertEqual(status, 303)
        # The session cookie is cleared — the user is logged out.
        self.assertIn("seat_session=; ", heads.get("Set-Cookie", ""))
        self.assertIn("Max-Age=0", heads.get("Set-Cookie", ""))
        self.assertIsNone(app.get_user(self.conn, user["id"]))

    def test_admin_deletes_test_account_http(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        status, heads, _raw = self._post(
            "/admin-delete-account", {"user_id": str(user["id"])},
            cookie=self._cookie(admin))
        self.assertEqual(status, 303)
        self.assertEqual(heads["Location"][:7], "/admin?")
        self.assertIsNone(app.get_user(self.conn, user["id"]))

    def test_admin_delete_page_requires_admin(self):
        user = self._new_user("Noa Malka")
        other = self._new_user("Maya Levi")
        status, heads, body = self._get(
            "/admin-delete-account?user_id=%s" % other["id"],
            cookie=self._cookie(user))
        self.assertEqual(status, 403)
        self.assertIsNotNone(app.get_user(self.conn, other["id"]))

    # --- Item 14: complaints. ---

    def test_file_complaint_on_frozen_request(self):
        admin = self._admin()
        offer = self._offer()
        req = self._request(offer)
        sent = []
        complaint, err = app.file_complaint(
            self.conn, req["id"], self.lev["id"], "הגעתי והמקום היה תפוס",
            sender=self._fake_sender(sent))
        self.assertIsNone(err, err)
        self.assertEqual(complaint["status"], "open")
        self.assertEqual(complaint["request_id"], req["id"])
        # The admin gets a push about it.
        self.assertEqual(len(sent), 0)  # the admin has no subscription yet
        # …but the complaint is on /admin.
        h = app.Handler.__new__(app.Handler)
        html = h._admin(self.conn, dict(admin), None, None)
        self.assertIn("הגעתי והמקום היה תפוס", html)
        self.assertIn("תלונות פתוחות", html)

    def test_complaint_notifies_admin_with_subscription(self):
        admin = self._admin()
        app.save_subscription(self.conn, admin["id"], {
            "endpoint": "https://example.com/push/admin",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        offer = self._offer()
        req = self._request(offer)
        sent = []
        complaint, err = app.file_complaint(
            self.conn, req["id"], self.lev["id"], "המקום לא היה שם",
            sender=self._fake_sender(sent))
        self.assertIsNone(err, err)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1]["title"], "תלונה חדשה")
        self.assertIn("/admin", sent[0][1]["url"])

    def test_complaint_one_per_request(self):
        offer = self._offer()
        req = self._request(offer)
        _, err = app.file_complaint(self.conn, req["id"], self.lev["id"],
                                    "פעם ראשונה")
        self.assertIsNone(err, err)
        _, err = app.file_complaint(self.conn, req["id"], self.lev["id"],
                                    "פעם שנייה")
        self.assertIsNotNone(err)

    def test_complaint_rejects_cancelled_and_other_user(self):
        offer = self._offer()
        req = self._request(offer)
        self._decide(req, "cancelled")
        _, err = app.file_complaint(self.conn, req["id"], self.lev["id"],
                                    "מאוחר מדי")
        self.assertIsNotNone(err)
        # A different user cannot complain about someone else's request.
        # A second offer for the same session needs a different saver.
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        req2 = self._request(self._offer(saver=self.lev), requester=shaked)
        _, err = app.file_complaint(self.conn, req2["id"],
                                    self.daniel["id"], "לא שלי")
        self.assertIsNotNone(err)

    def test_complaint_after_resolution(self):
        offer = self._offer()
        req = self._request(offer)
        self._decide(req, "confirmed")
        complaint, err = app.file_complaint(
            self.conn, req["id"], self.lev["id"], "הוא לא באמת שמר")
        self.assertIsNone(err, err)
        self.assertEqual(complaint["status"], "open")

    def test_decide_upheld_on_frozen_refunds_and_drops_rating(self):
        admin = self._admin()
        offer = self._offer()
        req = self._request(offer)
        complaint, err = app.file_complaint(
            self.conn, req["id"], self.lev["id"], "המקום לא היה שם")
        self.assertIsNone(err, err)
        self.assertIsNone(app.decide_complaint(
            self.conn, complaint["id"], dict(admin), "upheld",
            "בדקתי — המקום היה תפוס"))
        # The coin is back with the requester, the request resolved
        # as not_saved, and the saver's reliability dropped.
        self.assertEqual(app.get_user(self.conn, self.lev["id"])["balance"],
                         app.START_BALANCE)
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "not_saved")
        self.assertEqual(app.reliability(
            self.conn, self.daniel["id"], "saver"), (0, 1))
        row = self.conn.execute("SELECT * FROM complaints WHERE id=?",
                                (complaint["id"],)).fetchone()
        self.assertEqual(row["status"], "upheld")
        self.assertIn("בדקתי", row["admin_note"])

    def test_decide_upheld_on_confirmed_overturns_rating(self):
        admin = self._admin()
        offer = self._offer()
        req = self._request(offer)
        self._decide(req, "confirmed")
        # Before the complaint: 1/1, the coin already moved.
        self.assertEqual(app.reliability(
            self.conn, self.daniel["id"], "saver"), (1, 1))
        complaint, err = app.file_complaint(
            self.conn, req["id"], self.lev["id"], "הוא לא באמת שמר")
        self.assertIsNone(err, err)
        self.assertIsNone(app.decide_complaint(
            self.conn, complaint["id"], dict(admin), "upheld"))
        # After the upheld complaint: the confirmed arrival is
        # overturned — 1 kept out of 2 decided.
        self.assertEqual(app.reliability(
            self.conn, self.daniel["id"], "saver"), (1, 2))
        self.assertEqual(app.reliability_label(
            self.conn, self.daniel["id"], "saver"), "אמינות: 50% (1/2)")

    def test_decide_dismissed_changes_nothing(self):
        admin = self._admin()
        offer = self._offer()
        req = self._request(offer)
        self._decide(req, "confirmed")
        complaint, err = app.file_complaint(
            self.conn, req["id"], self.lev["id"], "סתם")
        self.assertIsNone(err, err)
        self.assertIsNone(app.decide_complaint(
            self.conn, complaint["id"], dict(admin), "dismissed"))
        self.assertEqual(app.reliability(
            self.conn, self.daniel["id"], "saver"), (1, 1))
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"],
                         app.START_BALANCE + 1)

    def test_decide_complaint_requires_admin_and_open(self):
        admin = self._admin()
        offer = self._offer()
        req = self._request(offer)
        complaint, _ = app.file_complaint(
            self.conn, req["id"], self.lev["id"], "המקום לא היה שם")
        # A non-admin cannot decide.
        self.assertIsNotNone(app.decide_complaint(
            self.conn, complaint["id"], dict(self.lev), "upheld"))
        # A decided complaint cannot be decided again.
        self.assertIsNone(app.decide_complaint(
            self.conn, complaint["id"], dict(admin), "dismissed"))
        self.assertIsNotNone(app.decide_complaint(
            self.conn, complaint["id"], dict(admin), "upheld"))

    def test_complaint_form_on_my(self):
        h = app.Handler.__new__(app.Handler)
        offer = self._offer()
        req = self._request(offer)
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("/file-complaint", html)
        self.assertIn("המקום לא נשמר?", html)
        # After filing, the form is replaced by the status.
        app.file_complaint(self.conn, req["id"], self.lev["id"],
                           "המקום לא היה שם")
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertNotIn("/file-complaint", html)
        self.assertIn("התלונה שלך על הבקשה הזו בטיפול", html)

    # --- Item 12: admin broadcast. ---

    def test_broadcast_sends_to_all_with_subscriptions(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        for uid in (admin["id"], user["id"]):
            app.save_subscription(self.conn, uid, {
                "endpoint": "https://example.com/push/%s" % uid,
                "keys": {"p256dh": "AAA", "auth": "BBB"}})
        sent = []
        count, err = app.send_broadcast(
            self.conn, dict(admin), "הפיילוט מתחיל",
            "מחר ב־08:00 כולם מציעים מקום", sender=self._fake_sender(sent))
        self.assertIsNone(err, err)
        self.assertEqual(count, 2)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1]["title"], "הפיילוט מתחיל")
        self.assertEqual(sent[0][1]["url"], "/")
        # Logged in notification_log, like every other push.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM notification_log WHERE status='sent'"
            ).fetchone()[0], 2)

    def test_broadcast_rate_limit(self):
        admin = self._admin()
        app.save_subscription(self.conn, admin["id"], {
            "endpoint": "https://example.com/push/admin",
            "keys": {"p256dh": "AAA", "auth": "BBB"}})
        sent = []
        count, err = app.send_broadcast(
            self.conn, dict(admin), "אחת", "ראשונה",
            sender=self._fake_sender(sent))
        self.assertIsNone(err, err)
        self.assertEqual(count, 1)
        # A second broadcast inside the hour is refused.
        count, err = app.send_broadcast(
            self.conn, dict(admin), "שתיים", "שנייה",
            sender=self._fake_sender(sent))
        self.assertIsNotNone(err)
        self.assertEqual(count, 0)
        # An hour later it works again.
        later = datetime.now() + timedelta(hours=1, minutes=1)
        count, err = app.send_broadcast(
            self.conn, dict(admin), "שלוש", "שלישית", now=later,
            sender=self._fake_sender(sent))
        self.assertIsNone(err, err)
        self.assertEqual(count, 1)

    def test_broadcast_requires_admin_and_validates(self):
        admin = self._admin()
        user = self._new_user("Noa Malka")
        count, err = app.send_broadcast(self.conn, dict(user), "כותרת",
                                        "גוף")
        self.assertIsNotNone(err)
        self.assertEqual(count, 0)
        for title, body in (("", "גוף"), ("כותרת", ""),
                            ("כ" * 81, "גוף"), ("כותרת", "ג" * 201)):
            count, err = app.send_broadcast(self.conn, dict(admin),
                                            title, body)
            self.assertIsNotNone(err, (title, body))
            self.assertEqual(count, 0)

    def test_broadcast_http(self):
        admin = self._admin()
        status, heads, _raw = self._post(
            "/admin-broadcast", {"title": "הודעה", "body": "לכולם"},
            cookie=self._cookie(admin))
        self.assertEqual(status, 303)
        self.assertIn("/admin?msg=", heads["Location"])

    # --- Item 3: one-tap offer from /today. ---

    def test_today_offer_button_links_to_prefilled_form(self):
        h = app.Handler.__new__(app.Handler)
        html = h._today(self.conn, dict(self.daniel), None, None)
        # The session from setUp starts in 10 minutes — in-window.
        self.assertIn("/offer?session_id=%s" % self.session["id"], html)
        self.assertIn("הצע מקום", html)
        # Anonymous users get no button.
        html = h._today(self.conn, None, None, None)
        self.assertNotIn("/offer?session_id=", html)

    def test_offer_form_prefills_session(self):
        h = app.Handler.__new__(app.Handler)
        html = h._offer_form(self.conn, dict(self.daniel), None, None,
                             self.session["id"])
        self.assertIn('<option value="%s" selected>' % self.session["id"],
                      html)

    def test_offer_form_prefills_template(self):
        h = app.Handler.__new__(app.Handler)
        app.save_offer_template(self.conn, self.daniel["id"],
                                self.session["id"], "שורה 3, ליד החלון", 2)
        html = h._offer_form(self.conn, dict(self.daniel), None, None,
                             self.session["id"])
        self.assertIn("שורה 3, ליד החלון", html)
        self.assertIn('<option selected>2</option>', html)

    def test_offer_form_ignores_bad_prefill(self):
        h = app.Handler.__new__(app.Handler)
        # A session that does not exist: the form shows its normal
        # list, and no SESSION option is pre-selected. (The seats
        # dropdown always has its default "1" selected — that is
        # not a prefill, so it does not count here.)
        html = h._offer_form(self.conn, dict(self.daniel), None, None,
                             99999)
        self.assertNotIn('<option value="%s" selected>'
                         % self.session["id"], html)
        self.assertIn('<option value="%s">' % self.session["id"], html)

    # --- Item 5: dead-subscription banner on /push. ---

    def test_recent_dead_removal(self):
        user = self._new_user("Noa Malka")
        self.assertIsNone(app.recent_dead_removal(self.conn, user["id"]))
        now = datetime.now()
        one_day = (now - timedelta(days=1)).isoformat(timespec="seconds")
        eight_days = (now - timedelta(days=8)).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (0, ?,"
            " 'removed-dead-subscription', 'x', ?)",
            (user["id"], one_day))
        self.conn.commit()
        row = app.recent_dead_removal(self.conn, user["id"], now=now)
        self.assertIsNotNone(row)
        # An 8-day-old removal next to it: the latest (1-day-old)
        # is still returned.
        self.conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (0, ?,"
            " 'removed-dead-subscription', 'x', ?)",
            (user["id"], eight_days))
        self.conn.commit()
        self.assertIsNotNone(app.recent_dead_removal(self.conn, user["id"],
                                                     now=now))
        # Only an 8-day-old removal: too old, nothing is shown.
        self.conn.execute("DELETE FROM notification_log WHERE created_at=?",
                          (one_day,))
        self.conn.commit()
        self.assertIsNone(app.recent_dead_removal(self.conn, user["id"],
                                                  now=now))

    def test_push_banner_after_dead_removal(self):
        user = self._new_user("Noa Malka")
        self.conn.execute(
            "INSERT INTO notification_log (offer_id, user_id, status,"
            " detail, created_at) VALUES (0, ?,"
            " 'removed-dead-subscription', 'x', ?)",
            (user["id"], datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        h = app.Handler.__new__(app.Handler)
        html = h._push(self.conn, dict(user), None, None)
        self.assertIn("המנוי הוסר כי הוא ישן", html)
        self.assertIn("הפעל התראות מחדש מהטלפון", html)

    def test_push_no_banner_without_removal(self):
        user = self._new_user("Noa Malka")
        h = app.Handler.__new__(app.Handler)
        html = h._push(self.conn, dict(user), None, None)
        self.assertNotIn("המנוי הוסר כי הוא ישן", html)

    # --- Item 10: dark-mode manual toggle. ---

    def test_theme_toggle_present_on_every_page(self):
        html = app.page_html("כותרת", "<p>גוף</p>",
                             dict(self.daniel))
        self.assertIn('id="theme-toggle"', html)
        self.assertIn('class="theme-toggle"', html)

    def test_theme_js(self):
        with open(os.path.join(os.path.dirname(app.__file__),
                               "static", "app.js"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("seat-theme", src)
        self.assertIn("applyTheme", src)
        self.assertIn("toggleTheme", src)
        self.assertNotIn("cycleTheme", src)
        # Binary light/dark only — the "system" choice is gone (the
        # system label never appears as a theme option any more).
        self.assertNotIn("🖥️ מערכת", src)
        self.assertIn("localStorage", src)
        self.assertIn("theme-toggle", src)

    def test_dark_css_dark_selector_only(self):
        # No system mode any more (bug report 2026-11-10): the dark
        # colours apply ONLY under html[data-theme="dark"]; the
        # prefers-color-scheme media query is gone entirely.
        self.assertIn('html[data-theme="dark"]', app.CSS)
        self.assertNotIn("prefers-color-scheme", app.CSS)
        self.assertNotIn("html:not([data-theme='light'])", app.CSS)
        self.assertIn(".verified{", app.CSS)
        self.assertIn(".saved-seat-card{", app.CSS)

    # --- Item 11: push action buttons. ---

    def test_sw_js_handles_actions(self):
        with open(os.path.join(os.path.dirname(app.__file__),
                               "static", "sw.js"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("actions", src)
        self.assertIn("/api/action", src)
        self.assertIn("request_id", src)

    def test_api_action_on_my_way(self):
        offer = self._offer()
        req = self._request(offer)
        status, data = self._post_json(
            "/api/action", {"action": "on-my-way", "request_id": req["id"]},
            cookie=self._cookie(self.lev))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertIsNotNone(row["on_the_way"])

    def test_api_action_confirm_arrival(self):
        offer = self._offer()
        req = self._request(offer)
        status, data = self._post_json(
            "/api/action",
            {"action": "confirm-arrival", "request_id": req["id"]},
            cookie=self._cookie(self.lev))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(app.get_user(self.conn, self.daniel["id"])["balance"],
                         app.START_BALANCE + 1)

    def test_api_action_cancel_request(self):
        offer = self._offer()
        req = self._request(offer)
        status, data = self._post_json(
            "/api/action",
            {"action": "cancel-request", "request_id": req["id"]},
            cookie=self._cookie(self.lev))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        row = self.conn.execute("SELECT * FROM requests WHERE id=?",
                                (req["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled_by_requester")

    def test_api_action_rejects_unknown_and_other_users(self):
        offer = self._offer()
        req = self._request(offer)
        status, data = self._post_json(
            "/api/action", {"action": "explode", "request_id": req["id"]},
            cookie=self._cookie(self.lev))
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        # Someone else's request: the handler refuses (ownership).
        status, data = self._post_json(
            "/api/action", {"action": "confirm-arrival",
                            "request_id": req["id"]},
            cookie=self._cookie(self.daniel))
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_api_action_requires_login(self):
        offer = self._offer()
        req = self._request(offer)
        status, data = self._post_json(
            "/api/action", {"action": "on-my-way", "request_id": req["id"]})
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    # --- Item 16: "who's coming". ---

    def test_saver_sees_requester_names_with_badge(self):
        admin = self._admin()
        requester = self._new_user("Noa Malka")
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           requester["id"], True))
        offer = self._offer()
        self._request(offer, requester=requester)
        h = app.Handler.__new__(app.Handler)
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name,"
            " u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        html = h._offer_card(self.conn, row, dict(self.daniel))
        self.assertIn("ביקשו מקום", html)
        self.assertIn("Noa Malka", html)
        self.assertIn('class="verified"', html)

    def test_other_requesters_do_not_see_who_is_coming(self):
        offer = self._offer()
        self._request(offer)
        h = app.Handler.__new__(app.Handler)
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name,"
            " u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        # Lev requested; Shaked (another requester) sees the card
        # without the requester list.
        shaked = self.conn.execute(
            "SELECT * FROM users WHERE name='Shaked'").fetchone()
        html = h._offer_card(self.conn, row, dict(shaked))
        self.assertNotIn("ביקשו מקום", html)

    # --- Item 18: "your saved seat" card. ---

    def test_saved_seat_card_shows_phone_and_meeting_point(self):
        admin = self._admin()
        saver = self._new_user("Noa Malka", "0527654321")
        self.assertIsNone(app.set_verified(self.conn, dict(admin),
                                           saver["id"], True))
        offer = self._offer(saver=saver)
        req = self._request(offer)
        h = app.Handler.__new__(app.Handler)
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertIn("המקום שלך נשמר", html)
        self.assertIn("Noa Malka", html)
        self.assertIn('class="verified"', html)
        self.assertIn("tel:0527654321", html)
        self.assertIn("https://wa.me/972527654321", html)
        self.assertIn("שורה 2, ליד שקע", html)

    def test_saved_seat_card_hidden_after_resolution(self):
        offer = self._offer()
        req = self._request(offer)
        self._decide(req, "confirmed")
        h = app.Handler.__new__(app.Handler)
        html = h._my(self.conn, dict(self.lev), None, None)
        self.assertNotIn("המקום שלך נשמר", html)

    def test_phone_never_on_offer_card(self):
        saver = self._new_user("Noa Malka", "0527654321")
        offer = self._offer(saver=saver)
        h = app.Handler.__new__(app.Handler)
        row = self.conn.execute(
            "SELECT o.*, c.name AS course_name, u.name AS saver_name,"
            " u.verified AS saver_verified"
            " FROM offers o JOIN courses c ON c.id=o.course_id"
            " JOIN users u ON u.id=o.saver_id WHERE o.id=?",
            (offer["id"],)).fetchone()
        html = h._offer_card(self.conn, row, dict(self.lev))
        self.assertNotIn("0527654321", html)
        self.assertNotIn("052-7654321", html)
        self.assertNotIn("wa.me", html)

    # --- Item 19: weekly view. ---

    def test_week_page_shows_all_days(self):
        h = app.Handler.__new__(app.Handler)
        html = h._week(self.conn, dict(self.daniel), None, None)
        # All seven weekday names appear.
        for name in app.WEEKDAY_NAMES:
            self.assertIn(name, html)
        # Today's session is there, with the one-tap offer button.
        self.assertIn("הצע מקום", html)
        self.assertIn("/offer?session_id=%s" % self.session["id"], html)

    def test_week_starts_on_sunday(self):
        # Daniel, 2026-11-10: the week starts on Sunday (Israel).
        # An earlier Monday-first change was my misreading of his
        # report that the week was starting on Monday — that was
        # the BUG he asked to fix, not the fix. Sunday's heading
        # comes before Monday's. (Compare the <h2> headings, not
        # the bare words: "ראשון" also appears in "ראשון בתור",
        # the waiting-list position.)
        self.assertEqual(app.DISPLAY_WEEKDAYS, [6, 0, 1, 2, 3, 4, 5])
        h = app.Handler.__new__(app.Handler)
        html = h._week(self.conn, dict(self.daniel), None, None)
        self.assertLess(html.index("<h2>ראשון</h2>"),
                        html.index("<h2>שני</h2>"))

    def test_week_route(self):
        status, heads, body = self._get("/week")
        self.assertEqual(status, 200)
        self.assertEqual(heads.get("Cache-Control"), "no-store")
        self.assertIn("השבוע", body)

    def test_week_nav_link(self):
        html = app.page_html("כותרת", "<p>גוף</p>")
        self.assertIn('href="/week"', html)
        self.assertIn("השבוע", html)

    def test_error_redirects_land_on_the_right_page(self):
        # Round 20: an error on a round-20 action lands back on the
        # page the user came from, not on the home page.
        admin = self._admin()
        user = self._new_user("Noa Malka")
        # A bad rename lands on /my.
        status, heads, _raw = self._post(
            "/rename", {"name": "Noa"}, cookie=self._cookie(user))
        self.assertEqual(status, 303)
        self.assertTrue(heads["Location"].startswith("/my?error="),
                        heads["Location"])
        # A bad broadcast lands on /admin.
        status, heads, _raw = self._post(
            "/admin-broadcast", {"title": "", "body": "x"},
            cookie=self._cookie(admin))
        self.assertTrue(heads["Location"].startswith("/admin?error="),
                        heads["Location"])

    # --- Item 8: reliability percentage on the card. ---

    def test_reliability_label_has_percentage(self):
        self.assertEqual(app.reliability_label(
            self.conn, self.daniel["id"], "saver"), "")
        offer = self._offer(seats=3)
        r1 = self._request(offer)
        levi = self.conn.execute(
            "SELECT * FROM users WHERE name='Levi'").fetchone()
        r2 = self._request(offer, requester=levi)
        self._decide(r1, "confirmed")
        self._decide(r2, "not_saved", levi["id"])
        self.assertEqual(app.reliability_label(
            self.conn, self.daniel["id"], "saver"), "אמינות: 50% (1/2)")

    # --- Item 5/13/15: migration keeps old users working. ---

    def test_migration_adds_phone_and_verified(self):
        # A database created before round 20 has no phone/verified
        # columns — init_db adds them, and the old users work.
        self.conn.execute("ALTER TABLE users DROP COLUMN phone")
        self.conn.execute("ALTER TABLE users DROP COLUMN verified")
        self.conn.commit()
        app.init_db(self.conn)
        cols = [r[1] for r in self.conn.execute(
            "PRAGMA table_info(users)").fetchall()]
        self.assertIn("phone", cols)
        self.assertIn("verified", cols)
        row = app.get_user(self.conn, self.daniel["id"])
        self.assertIsNone(row["phone"])
        self.assertEqual(row["verified"], 0)


if __name__ == "__main__":
    unittest.main()

# Seat Saver (שומר מקומות)

Students run between classrooms to catch a good seat. Seat Saver is a
small live marketplace where they save seats for each other, with a
coin balance so the favour is always returned.

When someone arrives early, they offer to save up to 5 seats. Everyone
following that course gets an automatic phone notification (Web Push).
A follower spends 1 coin to request a seat. The coin is frozen on
request and moves to the saver only when the requester arrives and
confirms "I arrived, seat was saved". If the requester does not arrive
in time, or the seat was not there, the coin goes back. The saver is
notified too when someone requests, and can see on "My seats" who
requested and who is on their way.

The interface is in Hebrew (RTL). Accounts are real: name + password,
with server-side sessions. There is no "switch user" any more — the
first build let anyone act as anyone, and that hole is closed.

## Rules (pilot)

- Everyone starts with **3 coins**. A balance can never go below **-2**.
- A request costs **1 coin** (frozen, then transferred on arrival).
- A saver can offer at most **5 seats** per class.
- Offers open **60 minutes before** class and expire at class start.
  The offer form only lists classes starting within the hour — a
  class further away is not offered at all, so a saver can only offer
  a class they are actually on their way to.
- If the requester has not arrived by **start + 5 minutes**, the coin
  is refunded and the seat is freed.
- "Good place" is free text from the saver ("Row 2, near outlet").
  No seat map yet.
- Registration is open: anyone with the link registers with a name and
  a password (at least 4 characters), picks the courses they are
  registered to, and picks which courses to get notifications about.
  A follow can be general (any offer for the course) or specific
  (one real session of the course). A specific follow is two steps:
  pick the course first, then pick the session from that course's
  real sessions — an impossible day/time combination cannot be made.
- Every session shows its teacher as well as its building and room.
- Every session shows how many users follow it ("3 עוקבים"), so the
  saver sees the demand before offering.
- When someone requests a seat, the saver gets a push notification
  too ("מישהו ביקש מקום"), and sees on "My seats" who requested and
  who tapped "I'm on my way".
- On the way to class, the requester can tap "אני בדרך" ("I'm on my
  way") — the saver sees it and gets a push ("לב בדרך לשיעור").
  It changes nothing about the rules: it is information only, and a
  requester who does not arrive still gets a no-show refund at start
  + 5 minutes. Tapping again updates the time and sends another push.
- A past offer can be re-offered in one tap ("הצע שוב") for the same
  session, with the same note and seats — as long as the
  60-minute window is open and there is no other open offer for
  that session. Followers are notified exactly like for a fresh offer.
- Every coin movement is written to a ledger and shown on "My seats"
  as "היסטוריית מטבעות": when, how many, and why (request, arrival
  payment, or refund). The history only covers movements after the
  update that added it.
- A push subscription the push service says is gone is deleted
  automatically and its removal is logged: HTTP 404 or 410, or HTTP
  403 with a wrong-key reason — `BadJwtToken` from Google's push
  service, `InvalidProviderToken` from Apple's: both mean the
  subscription was created under a different VAPID keypair than the
  server's current one (e.g. the user subscribed while the server
  ran from a second folder), so it can never work again; the user
  re-enables notifications to get a fresh one. `ExpiredProviderToken`
  is deliberately NOT deleted: the server mints a fresh token per
  push, so an expired one at send time points at clock skew, not a
  dead subscription. Any other failure (network error, 400, other
  403s, 5xx) leaves the subscription alone, since those are
  transient.
- **Test push (temporary pilot test aid, on `/push`).** A
  "שלח לי דחיפת בדיקה" button sends one test push to the
  logged-in user only, through the normal push path, and logs
  it with offer_id 0. At most one per user per 60 seconds; a
  second tap inside the minute is refused with a "try again in
  X seconds" message. The result is shown in Hebrew — success,
  or the failure reason in plain words. The button and its
  handler are isolated and marked temporary, so they are easy
  to remove after the pilot.
- Open offers are listed earliest class first, not newest first —
  creation order says nothing about urgency, class start does.
- A requester can **cancel their own request** while it is frozen
  (a "בטל בקשה" button on "My seats"). The frozen coin(s) come
  back immediately and the seat is freed. Only the requester can
  do this, and only while frozen — a confirmed arrival is done.
- When a seat frees up, followers are told — but **only when the
  offer was fully booked** (0 seats available → 1 or more). If
  seats were already available, no one is notified: a notification
  about a seat that was free all along is spam. The offer must
  still be open; a saver cancel or a no-show expiry closes the
  offer first, so nothing is announced then. The notification says
  "התפנה מקום" with the course, session, teacher and place. The
  user who freed the seat is not notified about it — someone who
  just cancelled their own request does not need a push telling
  them a seat freed up.
- A request can be for **1 or 2 seats** at once (for going with a
  friend). Everything multiplies: the freeze is 1 coin per seat,
  the floor check uses the multiplied amount (at -1, two seats
  would land on -3 and are refused; one seat is fine), arrival
  confirmation pays the saver 1 coin per seat, and refunds return
  1 coin per seat. "I'm on my way", cancel and confirm apply to
  the whole request, never per seat.
- **A reminder 30 minutes before class** ("צא עכשיו" — "leave now")
  is pushed to a requester with a frozen request, because 5 minutes
  is too late to leave home. A request created less than 30 minutes
  before class gets no early reminder — the 30-minute point already
  passed when it was created — but the 5-minute reminder still
  applies. **A reminder 5 minutes before class** is pushed to a
  requester with a frozen request ("תזכורת: השיעור מתחיל בעוד 5
  דקות"). Both are sent by a background thread inside the same
  Python server (see Stack below) — no cron, no external scheduler.
  Each reminder flag is stored in the database, so a server restart
  never double-reminds.
- **Admin dispute resolution (Daniel only).** If saver and
  requester disagree — the requester says "I arrived" and the
  saver says the seat was not there — Daniel decides on "My
  seats": "אשר הגעה" pays the coin to the saver, "החזר מטבע"
  returns it to the requester (taking it back from the saver if
  it was already paid). The section lists frozen requests past
  class start and requests resolved in the last 7 days. Only
  Daniel sees it, and the server enforces it — a non-Daniel
  request to those endpoints is rejected. It is a pilot trust
  mechanism, and every admin decision is written to the coin
  ledger as "הכרעת מנהל".
- **Weekly automatic offers — removed in round 13.** Daniel decided a
  weekly standing offer is a promise nobody can keep; the
  standing auto-request (next bullet) stays, because it promises
  nothing to anyone — it only takes a seat when someone really
  offered one.
- **Weekly automatic requests.** The mirror of automatic offers,
  for the other side: a user can set a standing request for a
  regular class ("בקש אוטומטית כל שבוע", from "My seats"), one
  per user and session. The same background thread checks every
  minute: the session is today, its 60-minute window is open,
  an open offer for it exists with a free seat (not the user's
  own), and the user has no frozen request — and no request the
  user or the saver already cancelled today — for that session.
  Then it requests exactly 1 seat through the same request
  function as a manual request (same floor check, same freeze),
  and pushes "ביקשתי בשבילך מקום" to the user, so they know to
  go and confirm arrival. A standing request persists week to
  week and is deleted only by its owner. Vacation users are
  skipped, like automatic offers; during quiet hours the request
  still happens and only the push is skipped, like any other
  notification.
- When a requester cancels their own request, the **saver gets a
  push "הבקשה בוטלה"** (requester, course, teacher, place) — so
  they do not arrive to class with a seat saved for nobody. Only
  requester-initiated cancels notify; a saver cancelling the
  offer or an admin force-refund do not.
- **The saver can cancel ONE request, not the whole offer.** On
  "My seats", each frozen request in the saver's own offer has a
  "בטל בקשה זו" button (saver only, server-enforced). The frozen
  coin returns to that requester immediately (ledger row, same as
  any refund), the request becomes "בוטלה על ידי השומר", the seat
  frees up, and the requester gets a push ("הבקשה שלך בוטלה על
  ידי השומר"). The offer stays open — if it was full, the freed
  seat runs the normal free-up path (waiting list first, then
  followers, only on 0→1). A second cancel of the same request is
  rejected: the frozen-only rule plus the write lock mean the
  refund can happen exactly once.
- **An open offer can be edited.** On "My seats", each open offer
  has an "ערוך" button (saver only, server-enforced): note
  and seat count. Seats can go up freely (to 10) and down only to
  the number of seats already frozen — below that the edit is
  rejected, because the offer cannot promise less than it already
  sold. Followers are NOT re-notified: it is the same offer, not
  a new one — except when the edit frees a seat that was not free
  before (a full offer raised), where the normal free-up path
  runs (waiting list first). **Frozen requesters ARE notified when
  the note changes** ("ההצעה עודכנה", with the new note) — the seat they asked for just changed, and they
  should know before they walk to class. A seats-only change sends
  them nothing. One push per requester, even for a 2-seat request.
- **Waiting list for a full offer.** When an offer has 0 free
  seats, a follower of the course can join its waiting list
  (one row per user per offer, first joined first notified).
  When a seat frees up, waiting users are notified first with
  their own message ("התפנה מקום — אתה ברשימת ההמתנה") and are
  excluded from the general followers' free-up notification, so
  nobody gets two pushes for one seat. A waiting user who
  successfully requests a seat leaves the list automatically,
  and anyone can leave at any time. When the offer closes or
  expires, its waiting list is deleted with it. **When the saver
  cancels the offer, the waiting users are notified** ("ההצעה
  לשיעור שביקשת בוטלה", with the course, teacher, building/room
  and day/time) before the entries are deleted — so they stop
  waiting instead of learning nothing. This is saver-cancel
  only: when the offer simply expires at class start the seat
  is gone anyway and a notification then would be noise. The
  list is not a reservation — the seat is still first come,
  first served; it only decides who is told first. **When the
  saver cancels the whole offer, the frozen requesters are
  notified too** ("ההצעה בוטלה", with the course, teacher,
  building/room and day/time) — one push per requester, even for
  a 2-seat request — so nobody walks to class expecting a seat
  that no longer exists. The refund is unchanged (every frozen
  coin returns); already-resolved requesters get nothing, and a
  user who is both a frozen requester and a waiter gets exactly
  one push, never two.
- **A reminder to the saver, 10 minutes before class** ("תזכורת:
  אתה שומר מקום", with the course, teacher, place and the
  requesters' names), sent by the same background thread. One
  reminder per offer, flagged in the database so a restart never
  double-reminds.
- Every offer card shows a **countdown** — "השיעור מתחיל בעוד
  X דקות" — computed server-side at render time, no JavaScript
  timer. Closed offers show none.
- **Reliability: "אמינות: X/Y"** next to the saver's name on
  offer cards and on "My seats" (as saver and as requester).
  Only requests that reached a decision count: confirmed
  arrivals / (confirmed + not-saved + no-show). Cancelling in
  time is excluded — it is not a broken promise — and neither
  are admin refunds. A user with no decided requests shows
  nothing, not "0/0".
- **Request board ("מחפשים מקום", `/wanted`).** The flow
  reversed: a student who knows they will be late can post "I
  need a seat" for a session in advance. One open post per user
  per session. When a saver offers that session, the posters
  get a push ("מישהו מציע מקום לשיעור שביקשת") — even if they
  do not follow the course, because the post itself is the
  request. When a poster successfully requests a seat on an
  offer for that session, their post is marked fulfilled
  automatically. A post expires at the start of the next
  occurrence of its session and is then hidden. Only the
  poster can cancel their own post. The board is demand signal
  only: it never reserves a seat and never touches coins. A
  poster who already holds a frozen seat for that session
  cannot post for it — that would be false demand — and can
  post again once the request is resolved. One offer, one
  push: a poster who also follows the course gets the board
  message, not the follower message as well.
- **Board demand on the offer form.** The offer form shows,
  next to each in-window session, how many people are actively
  looking for a seat for it ("2 מחפשים מקום לשיעור הזה") —
  counting only OPEN board posts for that session — so the
  saver can see real demand at the decision moment, next to
  the follower count that was already there. Nothing is shown
  when the count is zero. Offer form only; the automatic-offer
  path has no UI.
- **"I'm already in class" ("אני כבר בכיתה").** On their own
  open offer, the saver can tap a button that stamps the time;
  the card then shows "השומר כבר בכיתה (HH:MM)". Only the
  saver, only while the offer is open, and tapping again just
  updates the time. Information only — it changes nothing
  about seats, coins, or the arrival-confirmation flow.
- **Saved offer templates.** A saver can save a note and
  seat count for a session ("שמור כתבנית"), one template per
  user per session, editable and deletable by the owner only.
  On the offer form, templates for sessions starting within
  the hour appear as one-tap buttons ("הצע לפי התבנית") that
  create the offer with the saved values — still through the
  same offer validation. A template never creates an offer by
  itself.
- **Notify followers when a session is added.** When Daniel
  adds a session, every follower of that course gets a push
  ("נוסף שיעור חדש" + course, teacher, building/room,
  day/time) — general follows always, specific follows only
  if the new session matches their day/time. Only the
  add-session path triggers it, not the seed.
- **Quiet hours.** Set on "My seats" as a start and end hour
  (0–23, both together or both empty to clear; equal values
  rejected). While inside the window, the push-sending helper
  skips the send and logs it as `skipped-quiet-hours`. Overnight
  windows (e.g. 23:00–07:00) wrap past midnight. Skipped means
  skipped: nothing is queued for later — a notification that
  was not sent in its moment is gone. Applies to every push
  type (offers, requests, cancels, reminders, free-ups,
  waiting list, new sessions).
- **Vacation mode.** Set on "My seats" with one toggle ("אני
  בחופשה / במילואים" to turn it on, "חזרתי" to turn it off).
  While it is on, a clear indicator ("מצב חופשה פעיל") shows on
  "My seats", the weekly automatic offers are NOT created for
  that user (the standing offer stays and resumes when they are
  back), and NO pushes are sent to them — the check lives in
  the push-sending helper, next to quiet hours, so it covers
  every push type, and each skipped send is logged as
  `skipped-vacation`. Skipped means skipped, like quiet hours:
  nothing is queued for later. Existing open offers are NOT
  touched — vacation pauses the future, it does not cancel what
  already exists. **One exception: turning vacation on cancels
  the user's own frozen requests.** Someone turning vacation on
  is probably not coming to class, so each frozen request is
  cancelled at once: the coin is refunded immediately, the seat
  is freed, the saver gets the "request cancelled" push, the
  free-up path runs if the offer was full (waiting list first),
  and the requester gets one push confirming the request was
  cancelled because they turned vacation on. Already-resolved
  requests are untouched, and turning vacation off does nothing
  to requests. Quiet hours stay as
  they are: they cover a night, vacation covers a whole
  absence. **An end date can be set** ("חופשה עד…", a date field
  next to the toggle, set when turning vacation on or changed
  while it is on; empty means no end date): vacation is active
  through the END of the chosen date, and from the next morning
  it turns off automatically — the same background thread clears
  it on its minute tick (`clear_expired_vacations`), so pushes
  and automatic offers resume without the user doing anything.
  A past date is rejected and changes nothing. Turning vacation
  off clears the date, so no stale date survives into the next
  vacation.
- **Admin dashboard (`/admin`, Daniel only).** A read-only
  overview in one place: all currently open offers (saver,
  course, session, teacher, place, free/total seats, request
  count) and all currently frozen requests (requester, saver,
  course, session, coins frozen, how long frozen). Links to
  the existing "My seats" admin section for dispute
  resolution — no new admin powers on this page. Anyone else
  gets a 403 and never sees the page. **Notification log
  (temporary pilot test aid):** the latest 50 push attempts —
  when, to whom, sent / failed / skipped, and which offer (or
  "test" for a test push) — so a missed notification can be
  diagnosed without database commands. Log details are scrubbed
  of URLs before display, so no subscription endpoint can leak
  onto the page. **Password reset:** next
  to each user (except Daniel himself) there is an "אפס סיסמה"
  button: it clears that user's password hash, so they set a new
  password on next login through the existing first-login flow,
  AND deletes all their sessions, so old logins stop working
  everywhere. Daniel cannot reset his own password this way.
  No log table: in a pilot among friends a reset is a help-desk
  action, and the user sees the result the next time they log
  in.
- **VAPID public key format.** The public key is the full
  uncompressed P-256 point: `0x04 || X || Y` (65 bytes). An
  earlier build stored X||Y only (64 bytes), and every browser
  rejected it at subscribe time ("applicationServerKey is not
  valid") — push was completely broken. Key files from that
  build are repaired automatically on first start: same private
  key, public key rewritten with the missing byte. Nobody has
  to delete anything by hand.
- The **Help page** (`/help`, "איך זה עובד?" in the nav) explains the
  interface page by page and all the rules in one place. The balance
  floor (-2) is explained there in full and nowhere else in the app:
  other pages show only the balance itself. The two exceptions are
  the server-side error when requesting a seat at the floor, and a
  warning box on home and "My seats" when a user's balance is at -2,
  saying they cannot request until they save a seat and earn a coin.
  It also has a short "התראה לא הגיעה?" checklist (Home Screen, Focus
  mode, locked phone, re-enable notifications, test push — the last
  line points at the temporary test button and is marked temporary
  in the code, so it goes away with the button after the pilot).

## Accounts and passwords

- Passwords are never stored in plain text. Each one is hashed with
  `hashlib.pbkdf2_hmac` (SHA-256, 200,000 iterations, a fresh random
  salt per user) — Python standard library, no new dependency.
- Login creates a server-side session: a random token in an HttpOnly
  cookie, valid for 30 days. The browser never carries a user id it
  can edit, and every action uses the user from the session.
- No email, no phone: this is a pilot among friends, and there is no
  password reset by email. If someone forgets their password, Daniel
  clears the stored hash (`UPDATE users SET password_hash=NULL WHERE
  name='...';`) and the user sets a new one on next login.
- Users from before passwords existed (the 5 seeded test users, and
  anyone already in an existing `seat_saver.db`) have no password yet:
  on first login they are sent to set one once, then it is required.
- Only Daniel can add courses and sessions (a simple pilot rule, on
  the Courses page). Everyone else just registers and follows.

## Stack

- **Python** backend (standard library `http.server` + `sqlite3`),
  one file (`app.py`) — the same stack as the author's study planner.
- **A little JavaScript, only for Web Push**: `static/app.js` (ask
  permission, register the Service Worker, send the subscription) and
  `static/sw.js` (show the notification, open the app when tapped).
- **One background thread, inside the same Python server** — for
  the 30-minute "leave now" reminders, the 5-minute reminders, the
  10-minute saver reminders, the weekly automatic offers, the weekly
  automatic requests, and the vacation auto-off. It is a
  standard-library `threading.Thread` (daemon) started in `main()`,
  waking every 60 seconds to run six small checks
  (`early_reminder_tick`, `reminder_tick`, `saver_reminder_tick`,
  `auto_offer_tick`, `auto_request_tick`,
  `clear_expired_vacations`) and then sleeping. It opens
  its own SQLite connection each pass (connections are not shared
  between threads), never crashes the server (each check is wrapped
  in try/except and logged), and never double-reminds (the reminder
  flags live in the database). There is no cron, no Celery, no
  Redis, no external task queue and no new framework — a single
  pilot process does not need a scheduler, and adding one would
  mean a second thing to learn, deploy and explain in an interview.
- **One write lock for money and seats.** The server is threaded
  (one thread per request) and the background thread writes too,
  so two requests can otherwise both pass the same check ("one
  seat left", "request still frozen") and both change it. A QA
  thread test caught it: a double-tap on "confirm arrival" paid
  the saver twice 20/20 times, two users racing for the last seat
  both got it 18/20, duplicate offers 10/20. Every
  check-then-change on coins and seats now runs under one
  reentrant lock (`_MUTEX`, decorator `_serialized`) — the check
  and the change are atomic. The lock is held only for the
  database change, never for the push network call afterwards.
- **One external library, `pywebpush`** (`requirements.txt`) — only
  for Web Push encryption (ECDH + AES-GCM), which Python's standard
  library does not have. Everything else is standard library.

## Run it (locally, no push)

```bash
python3 app.py
# open http://localhost:8000
```

The database (`seat_saver.db`) is created on first run, seeded with
three courses (תכנון אלגוריתמים, תכנות מערכות, חדו״א 2) with their
real sessions, and five test users (Daniel, Lev, Levi, Shaked,
Yehonatan). It is gitignored — it is data, not the project. Once real
users have registered (someone has a password), the five test users
are removed automatically on the next start, with their follows —
they only ever existed so the pilot had followers before anyone
registered.

Seeded sessions (from Daniel's BGU course pages, screenshots
2026-11-08; weekday is Python's 0=Monday, 18 sessions in total):

- **תכנון אלגוריתמים** — lectures (Dr. S. Ben Daniel): group 1 Sun
  18:00–20:00 bldg 35/1, Wed 16:00–18:00 bldg 92/002; group 3 Sun
  16:00–18:00 bldg 38/010, Wed 18:00–20:00 bldg 34/10. Tutorials:
  Portal g31 Sun 18:00–20:00 bldg 34/14, Portal g21 Sun 16:00–18:00
  bldg 35/213, Levi g22 Mon 10:00–12:00 bldg 90/222.
- **חדו״א 2** — lectures (Dr. G. Landsman), group 2: Mon
  14:00–16:00 bldg 32/307, Wed 08:00–10:00 bldg 35/2. Tutorials
  (Mr. L. Luzon): g21 Mon 12:00–14:00 bldg 90/223, g22 Tue
  10:00–12:00 bldg 90/224.
- **תכנות מערכות** — lectures (Dr. M. Adler): group 1 Sun
  10:00–12:00 bldg 32/306, Mon 14:00–16:00 bldg 35/2; group 2 Thu
  12:00–14:00 bldg 35/2, Sun 12:00–14:00 bldg 34/2. Tutorials:
  Beitan g11 Tue 10:00–12:00 bldg 28/104, Netai g12 Tue 14:00–16:00
  bldg 90/234, Beitan g13 Mon 10:00–12:00 bldg 90/234.

Note: Daniel wrote "Beitan is Calculus 2", but the screenshots show
Beitan as a תכנות מערכות tutorial teacher and Luzon as the חדו״א 2
tutorial teacher — the screenshots are authoritative here; Daniel
confirmed 2026-11-08 that the extraction is exactly right ("לוזון זה
חדו"א, ביתן זה מערכות").

To enable push locally:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
```

On first run with `pywebpush` installed, the app generates a VAPID
key pair and saves it to `vapid_keys.json` (gitignored, **never
commit or share the private key**). To regenerate keys, delete
`vapid_keys.json` and restart the app.

## Pilot setup: making it live (HTTPS is required for push)

Web Push only works over HTTPS. Two options:

1. **For the pilot hour (recommended first): Cloudflare Tunnel (free).**
   Run the app locally, then expose it with a public HTTPS link:
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
   Share the `https://...trycloudflare.com` link with the pilot
   friends. Your PC must stay on during the pilot hour. The link
   changes every time you restart the tunnel.
2. **For real use: deploy to a free host with HTTPS** (e.g. Oracle
   Cloud free tier): copy this folder (plus `seat_saver.db` and
   `vapid_keys.json` — the keys must travel with the app, or every
   existing subscription stops working), install dependencies with
   `pip install -r requirements.txt` and start with `python3 app.py`.
   The app reads its port from the host's `PORT` environment variable
   (default 8000; an invalid value falls back to 8000 with a warning).
   The server's timezone must be Asia/Jerusalem: the app reads the
   system clock (`datetime.now()`, no timezone handling) for every
   time decision — offer windows, reminders, deadlines and log
   timestamps. An Oracle VM defaults to UTC, which shifts everything
   2 hours late in real time (and log timestamps 2 hours early).
   Fix on the server with `sudo timedatectl set-timezone
   Asia/Jerusalem` and restart the service; no code change is needed.
   The app then stays live even when your PC is off. No account has
   been created for you — this is a recipe, not a deployment.

### Updating the live server

Extract the new zip locally (replace only the project files, keep
`seat_saver.db` and `vapid_keys.json`), then copy only the project
files to the server — never the whole folder, or the server's
database gets overwritten:

```bash
scp -i C:\Users\danie\oracle-key.key C:\Users\danie\seat-saver\app.py C:\Users\danie\seat-saver\test_app.py C:\Users\danie\seat-saver\README.md C:\Users\danie\seat-saver\EXPLANATION.md C:\Users\danie\seat-saver\requirements.txt C:\Users\danie\seat-saver\.gitignore ubuntu@82.70.214.183:/home/ubuntu/seat-saver/
scp -i C:\Users\danie\oracle-key.key C:\Users\danie\seat-saver\static\app.js C:\Users\danie\seat-saver\static\sw.js C:\Users\danie\seat-saver\static\icon-180.png C:\Users\danie\seat-saver\static\icon-192.png C:\Users\danie\seat-saver\static\icon-512.png C:\Users\danie\seat-saver\static\favicon.png C:\Users\danie\seat-saver\static\manifest.json ubuntu@82.70.214.183:/home/ubuntu/seat-saver/static/
```

Then, in SSH: `sudo systemctl restart seat-saver`, and check
`sudo systemctl status seat-saver` says active (running).
Migrations run automatically on restart.

## Notifications on phones

- **Android Chrome:** open the link, tap "Enable notifications",
  grant permission — done.
- **iPhone (Safari):** Web Push works only if the site is added to
  the Home Screen first (Share → Add to Home Screen), then opened
  from the Home Screen, and then notifications are enabled there.
  This is an Apple limitation (since iOS 16.4), not a bug in this app.
- **One subscription per device:** a subscription belongs to one
  browser on one device — enabling notifications on the computer
  sends them to the computer, not the phone. Before subscribing,
  the site warns (desktop browser, or iPhone/iPad Safari outside
  the Home Screen app) and asks for confirmation; cancelling
  subscribes nothing. The **Notifications** page lists the
  connected devices ("Chrome on computer", "iPhone — Home Screen",
  "unknown", ...) with the count, and warns when every subscription
  is a desktop. Subscriptions created before round 14 show
  "unknown" — the server never guesses. Re-enabling also drops a
  stale browser-side subscription first: if the browser still holds
  a subscription made under a different VAPID key, it is
  unsubscribed before a fresh one is created (round 15) — otherwise
  the same broken subscription comes back on every re-enable.

## Admin: courses

Daniel enters the courses once, on the **Courses** page (add a course,
then add its sessions: day, start/end time, teacher, building, room). The seed
data already contains his real lectures and tutorials. The add forms
are only visible to Daniel; anyone else just registers and follows.

## Tests

```bash
python3 -m unittest test_app -v
```

536 tests, covering registration and passwords (hashing, login,
migration, sessions, logout), follows (general vs specific two-step,
impossible sessions rejected, follower counts with dedupe), teachers
(seeded values, labels, migration from a pre-teacher database),
the help page and the floor (at-floor warning only at -2, floor
mentioned nowhere else), balances (start 3, floor -2), freezing /
transfer / refunds, the coin ledger (every movement, all three refund
reasons), the saver notification on request (including the
no-subscription case), one-tap re-offer (in and out of the window,
duplicate, wrong saver, session not today), "I'm on my way"
(flag, double-tap, only requester and only while frozen, migration),
dead push-subscription cleanup (404/410 delete, transient failures
keep), offers sorted by class start (including a session-less offer
sorting last), the 60-minute offer window (including its boundaries)
and the 10-seat limit, the offer form listing only in-window sessions,
the Hebrew RTL UI, push subscription storage + notification
matching, request cancellation (refund, seat freed, only requester
and only while frozen), seat-freed notifications (only on the
fully-booked → free transition, never on saver cancel, not-saved
with seats free, or no-show expiry), 2-seat requests (freeze/pay/
refund multiply, floor math with 2 coins, quantity limits), the
5-minute reminder (due only within 5 minutes, no double-remind,
marked even without a subscription), admin dispute resolution
(force confirm/refund, non-Daniel rejected, stuck list with the
7-day window, no double confirm), weekly automatic offers
(create, one per user+session, delete by owner only, tick creates
in-window and notifies, skips out-of-window and other days —
removed in round 13, see below), and
the round-4 migration (new columns and table on an old database),
concurrency (two threads confirming the same request, racing for
the last seat, creating the same offer, or cancelling the same
request — exactly one wins), and the seat-freed notification
skipping the user who freed the seat themselves, the saver
notification on requester cancel (only that path — not admin
refund, not saver cancel, not a not-saved report), the waiting
list (join only when full, only for followers, no double join,
leave any time, request removes, free-up notifies waiting first
with their own message and never twice, list cleaned when the
offer closes or expires, double-join race allows one row), the
10-minute saver reminder (due only within 10 minutes, one per
offer, requesters' names in the body, no double-remind, flagged
even without a subscription), the countdown (exact minutes,
under-a-minute, after start, empty for closed offers, shown on
the card with no JavaScript), reliability (only decided requests
count, cancels and admin refunds excluded, no-show counts as
not kept, nothing shown without history, shown on the card and
on "My seats"), the VAPID key fix (fresh key is 65 bytes with
the 0x04 prefix and a valid P-256 point, a legacy 64-byte key
file is repaired on load with the private key untouched, an
undecodable or wrong-prefix key regenerates), and the round-5
migration (waiting_list table and saver_reminded_at column on
an old database), the request board (post, duplicate rejected,
cancel only by poster and only while open, expiry at next class
start — computed from when the post was made — board notify on
offer, fulfil on successful request, repost blocked while
holding a seat and allowed after the request is resolved, board
never touches coins), "I'm already in class" (only the saver,
only while open, double-tap updates, shown on the card, changes
nothing about coins or seats), offer templates (save, update,
delete by owner only, one per user per session, one-tap button
only for in-window sessions, templates never create offers),
zone preference (set/clear, invalid rejected, badge only when
the zone matches, display only — removed in round 13, see below),
new-session notifications
(general and matching specific follows notified, others not,
saver excluded, only the add-session path), quiet hours (set
and clear, one hour alone rejected, equal values rejected,
midnight start as 0, overnight windows, skip logged, no
queueing, applies to every push type), the admin dashboard
(open offers and frozen requests listed, 403 for non-Daniel
and for anonymous users, no new powers), the round-6 migration
(new tables and columns on an old database), the site copy
cleanup (no "Telegram", "רק דניאל מוסיף", "הכרעת מנהל" or
VAPID mentions on any rendered page, real rules kept on
/help), the round-7 features (vacation mode: default off,
toggle, auto-offers skipped and resumed (removed in round 13),
pushes skipped and
logged, quiet-hours checked first; waiting-list cancel
notification: saver-cancel only, exact payloads, no notify
without waiters, one push for a waiter who also follows,
expiry silent; board demand in the offer form: only open
posts, labels, shown and hidden correctly; the round-7
migration: `on_vacation` column on an old database), and the
QA fixes (one push per offer for a poster who also follows,
midnight quiet-hours start, equal quiet hours never quiet,
board post blocked while holding a seat, and races: parallel
board posts, parallel template saves, parallel last-seat
requests with a board poster, parallel "already in class"
taps, parallel vacation toggles, parallel offer cancels —
exactly one wins where one must), and the round-8 features
(vacation end date: set/clear, past and invalid dates rejected
with state unchanged, auto-off through the end of the chosen
date, corrupt dates skipped, pushes resume
after auto-off (auto-offers removed in round 13), end date shown on "My seats"; saver cancels
one request: refund with ledger row, offer stays open, other
requests untouched, requester pushed, no double refund, free-up
only on 0→1; standing auto-requests: create/delete/duplicate,
tick requests exactly 1 seat and pushes the user, no duplicate
in a day, floor and vacation skipped, own offer skipped,
closed window skipped, no re-request after a manual or saver
cancel but re-request after the whole offer was cancelled;
edit offer: note/seats (zone removed in round 13), no re-notify,
frozen floor
enforced, free-up when a full offer is raised; admin password
reset: hash cleared and sessions deleted, self-reset and
non-admin rejected, user sets a new password on next login;
the round-8 migration: `vacation_until` column and the
`auto_requests` table on an old database; and the races:
parallel saver-cancels of one request, parallel auto-request
ticks, parallel offer edits — exactly one wins where one must)
— all passing, no real network used. Round 11 adds: edit
notifications (note change pushes the frozen requesters
only, one push per requester, seats-only and no-change edits stay
silent, resolved requesters get nothing), vacation-on cancel
(refund at once, seat freed, saver and requester pushed, free-up
with waiting list first when the offer was full, other users'
requests untouched, vacation-off does nothing, double turn-on
refunds once, the confirmation push arrives despite quiet hours
and vacation, and the race between vacation-cancel and arrival
confirm has exactly one winner with consistent balances), the
on-the-way push to the saver (one push per tap, informational
only, skipped and logged without a subscription, rejected taps
send nothing), the 30-minute "leave now" reminder (fires at 30
minutes and once only, not for a request created less than 30
minutes before class while the 5-minute reminder still applies,
not for resolved requests, not after class started, flag
survives a restart, both reminders fire for the same request,
and the `reminded_30_at` migration on an old database), and the
/help troubleshooting checklist (all five checks present, the
test-button line marked temporary in the code, and the updated
help lines for the other four items). Round 12 adds: declared
late (declare only while frozen and before start+5, one
declaration per request, the declaration pushes the saver with
the four payload fields, accept moves the deadline to start+10
and arrival by +10 pays, no arrival by +10 refunds, undecided
expires at +10, refuse refunds at once with the new
late_declined status, declare after +5 impossible, double-declare
impossible, declare coexists with on-the-way, declare works
after class started as long as +5 has not passed, the push
respects quiet hours and vacation with no force bypass, and the
races — declare vs expiry, double declare, accept vs refuse —
exactly one wins where one must; the `late_declared_at` /
`late_status` migration on an old database), per-user closed-offer
history (only the saver's own closed offers, newest first, the
outcome of each request including late-declined, re-offer works
from a history entry, closed offers no longer render on /offers,
the coin history untouched), and the QA fixes (settle_expired now
revisits already-expired offers, so a request whose deadline falls
after the offer expired is still refunded on a later pass instead
of staying frozen forever; _refund_request clears the late columns
on every resolution, so a pending declaration never survives a
refund; an empty-state line when /offers has no open offers).
Round 13 adds: seat zones removed (no zone selector on the offer
form, no preferred-zone setting, no zone badge or zone display on
cards, no zone in push payloads or templates, no zone on /help —
the columns stay in the database, nullable, and the migration
rebuilds both tables on an old database keeping every row and
every foreign key intact, with a fresh offer working after the
migration), multi-session specific follow (checkboxes for a
course's sessions with already-followed ones pre-checked,
duplicates still rejected, the follow page keeps its place with
the #follow fragment on the course picker, on every redirect and
on the error redirect too), follows at registration (session
checkboxes on the register page, bad ids skipped, redirect to
the follow page with a banner and a link on to /push), weekly
auto-offers removed entirely (the standing auto-request stays),
the separate admin account "דניאל ביק" (the seeded English
"Daniel" stays admin; "דניאלוש" is a regular user with no admin
link, no admin section and a 403 on /admin; no admin's password
can be reset from /admin), the test push reaching every
subscription of the user with one log row per subscription and
offer_id=0, and the QA fix (the follow-page error redirect now
carries the #follow fragment, so an error lands on the form
instead of at the top of the page). Round 14 adds: Hebrew course
names (תכנון אלגוריתמים, תכנות מערכות, חדו״א 2) in the seed data
and a migration that renames the three English names in an
existing database (exact-name match, never overwriting a Hebrew
name that already exists, idempotent, and running before the
teacher fill so the teacher lookup finds the renamed courses),
exact-session follows (a specific follow stores the session id,
so the Sunday 18:00 lecture and tutorial are two separate follows
with separate follower counts; the migration backfills the session
id where the hour maps to exactly one session and keeps an
ambiguous follow matching both; a general follow for a course
makes new specific follows for it redundant — skipped at
registration and on the follow page, existing specific follows
never deleted), the muted-notifications banner on the home page
(quiet hours show until when, vacation shows until its end date
when set, vacation wins over quiet hours), filtering /offers by
course (server-side, links only when more than one course has
open offers), the arrival-confirm reminder at class start (one
push to each frozen requester who has not confirmed yet,
persisted flag, quiet hours and vacation respected, the +5
no-show deadline unchanged), and the waiting-list position on
the card ("אתה ראשון בתור" / "אתה מספר N בתור"), and device
detection at subscribe time (User-Agent classification into
desktop / iPhone / iPad / Android / unknown, the standalone flag
from app.js, the confirm-before-subscribing warning for desktop
and for iPhone/iPad Safari outside the Home Screen app, the
device list with the count and the all-desktop warning on /push,
and the migration — old subscriptions show "unknown"), and the
VAPID public key is never cached (the /vapid-public-key response
sends Cache-Control: no-store, and app.js fetches it with
{cache: "no-store"} — a stale cached key used to re-create broken
subscriptions). The suite also
gained an in-process POST driver for the register and add-follow
handlers, and the QA pass fixed the stale zone mentions round 13
left in the docs (the rules, re-offer, edit, templates and
zone-preference bullets in README, and the intro, re-offer, edit
and closed-history lines in EXPLANATION.md) — no app bugs were
found in round 14. Round 15 fixes a live push bug found the same
day: a stale browser-side subscription (created under a different
VAPID keypair) kept coming back on every re-enable, because the
server deleting its own row never touches the browser's copy — so
app.js now checks for an existing subscription, compares its key
with the current one, and unsubscribes first when they differ
(matching keys are kept); no app bugs were found in round 15
either. Round 16 is a small UX fix Daniel asked for the same day:
when the browser cannot do Web Push at all (e.g. Chrome on iOS,
which has no Service Worker), the app no longer says only "not
supported" — it names the browser and tells the user to open the
site in Safari, add it to the Home Screen, and enable notifications
from the app there; the same round also fixes a live cache trap —
the phone kept running old app.js after a deploy, so the round-15
fix never ran on it — by serving the JavaScript at versioned URLs
(/static/app.js?v=16, /sw.js?v=16) with Cache-Control: no-store;
no app bugs were found in round 16 either. Round 17 closes the
hole round 16 left: the versioned JavaScript only helps once the
page itself is fresh, but the phone loaded a stale cached /push
page that still referenced the unversioned script — so all HTML
pages are now served with Cache-Control: no-store as well; no app
bugs were found in round 17 either. Round 18 fixes the push bug
that kept the phone silent: the VAPID JWT was sent with
`sub` = `mailto:seat-saver@localhost`, which Google accepts but
Apple rejects with 403 BadJwtToken — the `sub` is now the site's
own URL, `https://my-seat-saver.duckdns.org`. Round 19 is the
interface round: a real Home Screen icon (a white chair on the
app's green, drawn with Pillow at build time, with a manifest and
favicon), the /help page rewritten as 4 steps + concise rules +
FAQ + problem → what-to-do troubleshooting, dark mode completed
(CSS `prefers-color-scheme` only), a /today page (today's lessons
and offers, next first), clearer offer cards (course big, time
under it, seats left big, one full-width request button, no
duplicate summary line on /offers), one fixed colour per course
(palette index = course id, no new table), a 3-step onboarding box
on the home page for users who follow nothing yet, and an offers
time filter (`?when=today` / `?when=week`, combinable with the
course filter). Tests grew 445 → 467, all passing. Round 20 is the
identity-and-trust round: registration requires a full name (first
+ last) and an Israeli mobile number, and the admin marks users
VERIFIED by hand (a ✓ badge next to the name) after checking the
number against his own contacts — no SMS service, no cost; the
admin sees all users with coins, phones and verification on
/admin, and can delete any account there (a user can delete their
own account too, through a confirmation page; frozen coins are
always refunded first); the requester can complain that a seat was
not actually saved, and the admin upholds or dismisses it — only
an upheld complaint lowers the saver's reliability, which is now
shown as a percentage on the offer card; after a request is
accepted the requester sees a "your saved seat" card with the
saver's name, a call link and a WhatsApp link, and the saver sees
who is coming; /today gained a one-tap "offer a seat" button that
prefills the offer form, /push shows a banner when a subscription
was recently auto-removed, notifications carry action buttons
("I'm on my way", "cancel", "I arrived", "the seat wasn't there")
handled by the Service Worker, the admin can broadcast one push
to everyone (at most once an hour), there is a /week view, and
dark mode has a manual light/dark/system switch remembered on the
device. Round 20 adds 69 tests: full-name validation (single word,
one-letter word, empty, too long — all rejected; two words accepted),
phone validation (missing, non-numeric, landline, too short, too long
rejected; 05X-XXXXXXX, 05XXXXXXXX, +972 and 972 forms accepted and
normalised to 05XXXXXXXX), the phone display and wa.me link formats,
registration over HTTP requiring a phone, the incomplete-profile
banner on the home page (single-word name or no phone), renaming
(success keeps coins/follows/offers, nickname/empty/taken/admin-name
rejected, an admin cannot rename, renaming resets verification),
setting a phone (validated, resets verification), admin verification
(admin sets and clears, a non-admin cannot, an admin cannot be
verified, the ✓ badge on offer cards and the admin list, the admin
user list showing phone/coins/verification), account deletion (a
saver delete refunds frozen coins and frees the seat, a requester
delete frees the seat, follows/subscriptions/requests/offers are
removed, an admin account cannot be deleted, the GET confirmation
page deletes nothing, self-delete over HTTP clears the session
cookie, the admin deletes a test account over HTTP, the admin-delete
page requires admin), complaints (file on a frozen request with an
admin push, one per request, cancelled and other users' requests
rejected, file after resolution, an upheld complaint on a frozen
request refunds and marks it not-saved, an upheld complaint on a
confirmed request moves reliability 1/1 to 1/2, a dismissed complaint
changes nothing, deciding requires admin and an open complaint, the
form on /my replaced by the status text after filing), the
reliability label as a percentage ("אמינות: 50% (1/2)"), the
broadcast (sends to all subscribed users, logged, one per hour per
admin, works again after the hour, requires admin, title/body
validated, over HTTP), the one-tap offer (button on /today linking
to /offer?session_id=N, hidden for anonymous users, the form
pre-selects the session, a template prefills the note and seats, a
bad session id selects nothing), the dead-subscription banner
(recent removal returned, an 8-day-old one ignored, shown on /push
and absent without a removal), dark mode (the toggle button in the
page, the theme code in app.js, both dark selectors and the
not-light guard in the CSS), push actions (sw.js carries actions
and /api/action, the API runs on-my-way / confirm-arrival /
cancel-request end to end with coin movement, unknown action 400,
another user's request 400, no login 401), "who's coming" (the
saver sees requester names with badges on the card, another
requester does not), the saved-seat card (name + badge + tel: +
wa.me + meeting point, hidden after resolution, the phone never on
the offer card), the /week view (all 7 days, one-tap button, the
nav link), the migration (dropping phone/verified columns then
init_db re-adds them, old users get phone NULL and verified 0),
and the error redirects landing on the right page (/my for rename,
/admin for broadcast). Tests grew 467 → 536, all passing.

## Project structure

- `app.py` — the whole backend: SQLite schema, password hashing and
  sessions, coin rules, offers, follows, and Web Push sending (one file)
- `test_app.py` — the test suite
- `static/app.js` — minimal JS: permission + Service Worker + subscription
- `static/sw.js` — Service Worker: show the notification on push
- `static/icon-180.png`, `static/icon-192.png`, `static/icon-512.png` — the
  Home Screen / manifest app icon (a white chair on the app's green),
  drawn with Pillow at build time; `static/favicon.png` (32×32) and
  `static/manifest.json` (name, icons, theme colour, standalone)
- `requirements.txt` — `pywebpush` only (Web Push encryption)
- `README.md` — this file
- `.gitignore` — keeps `seat_saver.db`, `vapid_keys.json` and `venv/` out of git

> ⚠️ **Pilot only.** Passwords here are pilot-level (minimum 4
> characters, no email reset, no rate limiting on login). That is
> enough for 20 friends, and it must be hardened before any wider
> use. Do not expose the local server to the internet except through
> the tunnel/hosting steps above.

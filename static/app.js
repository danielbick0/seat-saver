// Seat Saver — the ONLY JavaScript in the app, and only for Web Push.
// Job 1: ask the user for notification permission.
// Job 2: register the Service Worker (sw.js).
// Job 3: ask the browser for a push subscription and send it to the server.
// The server then uses that subscription to push a notification to this
// phone when someone offers a seat for a followed course. Nothing else
// here does any app logic — the app itself is plain HTML forms + Python.
// Note: the subscription is sent WITHOUT a user id. The server takes
// the user from the login session cookie, so a forged id cannot attach
// a subscription to someone else's account.

function base64ToBytes(base64) {
  // VAPID public keys are url-safe base64 without padding; the Push API
  // wants them as raw bytes.
  const padded = base64.replace(/-/g, "+").replace(/_/g, "/") +
    "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = window.atob(padded);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

async function enablePush() {
  const status = document.getElementById("push-status");
  function say(text) { if (status) status.textContent = text; }
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    say(unsupportedPushMessage());
    return;
  }
  // A subscription belongs to one browser on one device — if it is
  // created on the computer, the notifications go to the computer,
  // not the phone. Warn BEFORE subscribing and let the user cancel,
  // before any permission is even asked. Same UA-only rule as the
  // server's classify_user_agent, so both sides always agree on
  // what the device is.
  const warning = deviceWarning();
  if (warning && !window.confirm(warning)) {
    say("ביטלת — ההתראות לא הופעלו במכשיר הזה. אפשר לנסות שוב מהטלפון.");
    return;
  }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      say("ההתראות לא אושרו. אפשר לאשר אותן בהגדרות הדפדפן.");
      return;
    }
    const registration = await navigator.serviceWorker.register("/sw.js?v=21");
    // no-store: a cached public key would subscribe this browser
    // under a dead keypair (found live 2026-11-10) — the key must
    // always come fresh from the server.
    const keyRes = await fetch("/vapid-public-key", {cache: "no-store"});
    const keyData = await keyRes.json();
    if (!keyData.public_key) {
      say("הדחיפה עוד לא הוגדרה בשרת (אין מפתחות VAPID). האפליקציה עובדת, אבל לא יגיעו התראות.");
      return;
    }
    const keyBytes = base64ToBytes(keyData.public_key);
    // The browser keeps its own push subscription: the server
    // deleting its row never touches it. If the browser hands back
    // an old subscription made under a different keypair, every
    // push to it 403s and gets auto-removed — and re-enabling only
    // re-saves the same broken one (found live 2026-11-10, even
    // after clearing the cache and re-adding the Home Screen app).
    // So compare the old subscription's key with the current one
    // and drop it before subscribing fresh when they differ. A
    // matching key is kept — no churn of a good subscription.
    const existing = await registration.pushManager.getSubscription();
    const oldKey = existing && existing.options
      ? existing.options.applicationServerKey : null;
    if (existing && !sameKey(oldKey, keyBytes)) {
      await existing.unsubscribe();
    }
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes,
    });
    const res = await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription, standalone: isStandalone() }),
    });
    const data = await res.json();
    if (data.ok) {
      // Say honestly where the notifications will arrive: a
      // subscription made on the computer goes to the computer.
      say(isMobileDevice() ? "ההתראות מופעלות בטלפון הזה."
                           : "ההתראות מופעלות במחשב הזה.");
    } else {
      say("לא הצלחנו לשמור את המנוי: " + (data.error || "שגיאה לא ידועה"));
    }
  } catch (err) {
    say("לא הצלחנו להפעיל התראות: " + err.message);
  }
}

// What to tell the user when this browser cannot do Web Push at
// all. A bare "not supported" is a dead end — the user has no idea
// what to do instead. On iOS, push works only from a Home Screen
// web app added through Safari (iOS 16.4+); third-party browsers
// on iOS (Chrome = CriOS, Firefox = FxiOS, Edge = EdgiOS) have no
// Service Worker, so they can never receive a push. Name the
// browser the user is actually in and give the exact steps: open
// the site in Safari, Share, "הוסף למסך הבית", open the app from
// the Home Screen, tap "הפעל התראות" there.
function unsupportedPushMessage() {
  const ua = navigator.userAgent || "";
  const isIPhone = /iPhone/.test(ua);
  const isIPad = /iPad/.test(ua);
  if (isIPhone || isIPad) {
    const device = isIPad ? "אייפד" : "אייפון";
    let browser = null;
    if (/CriOS/.test(ua)) browser = "כרום";
    else if (/FxiOS/.test(ua)) browser = "פיירפוקס";
    else if (/EdgiOS/.test(ua)) browser = "אדג׳";
    if (browser) {
      return "אתה ב" + browser + " על " + device + " — בדפדפן הזה אי אפשר לקבל התראות. "
        + "התראות עובדות רק מאפליקציית מסך הבית, שאפשר להוסיף רק דרך ספארי. "
        + "פתח את האתר בספארי, לחץ על שיתוף, בחר ״הוסף למסך הבית״, "
        + "פתח את האפליקציה ממסך הבית, ולחץ שם על ״הפעל התראות״.";
    }
    // iOS Safari itself, but too old (or otherwise without push):
    // same steps, no browser switch, plus update iOS.
    return "ב" + device + " הזה התראות עובדות רק מאפליקציית מסך הבית, וצריך iOS מעודכן (16.4 ומעלה). "
      + "פתח את האתר בספארי, לחץ על שיתוף, בחר ״הוסף למסך הבית״, "
      + "פתח את האפליקציה ממסך הבית, ולחץ שם על ״הפעל התראות״. "
      + "אם זה לא עובד, עדכן את iOS לגרסה האחרונה.";
  }
  return "הדפדפן הזה לא תומך בהתראות דחיפה. "
    + "פתח את האתר בכרום, אדג׳ או פיירפוקס עדכניים במחשב, "
    + "או בטלפון דרך ספארי ואז ״הוסף למסך הבית״.";
}

// True when two applicationServerKey values are the same bytes.
// The subscription's key is an ArrayBuffer (or null when the
// browser does not report it); the fetched key is a Uint8Array.
// A missing key cannot be proven equal, so it counts as different
// — the safe side is to re-subscribe under the current key.
function sameKey(a, b) {
  if (!a || !b) return false;
  const x = new Uint8Array(a);
  const y = new Uint8Array(b);
  if (x.length !== y.length) return false;
  for (let i = 0; i < x.length; i++) {
    if (x[i] !== y[i]) return false;
  }
  return true;
}

// Is the page running as an installed Home Screen app (not in the
// browser)? iOS reports navigator.standalone; Android/desktop PWAs
// report display-mode: standalone. Sent with the subscription so
// the server can tell a Home Screen app from plain Safari.
function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
}

// True for a phone/tablet User-Agent, false for a desktop one.
function isMobileDevice() {
  return /iPhone|iPad|Android|Mobile|Mobi|Tablet/
    .test(navigator.userAgent || "");
}

// The warning text when the subscription would go to the wrong
// place, or null when there is nothing to warn about: a desktop
// browser (notifications go to the computer, not the phone), or
// an iPhone/iPad in Safari instead of the Home Screen app (on
// iOS, Web Push only works from the installed app).
function deviceWarning() {
  const ua = navigator.userAgent || "";
  if (!isMobileDevice()) {
    return "נראה שהפעלת התראות מהמחשב — ההתראות יגיעו למחשב, לא לטלפון. להמשיך בכל זאת?";
  }
  const isIPhone = /iPhone/.test(ua);
  const isIPad = /iPad/.test(ua);
  if ((isIPhone || isIPad) && !isStandalone()) {
    const device = isIPad ? "אייפד" : "אייפון";
    return "ב" + device + " התראות עובדות רק מאפליקציית מסך הבית — כרגע אתה בדפדפן, וההתראה לא תגיע לטלפון. להמשיך בכל זאת?";
  }
  return null;
}

// Round 20, item 10 (fixed 2026-11-10): the manual theme toggle —
// LIGHT or DARK only, remembered on the device. Why localStorage and
// not a server setting: the theme is a property of the DEVICE (this
// phone is dark, that computer is light), not of the account, so it
// is stored in the browser and never sent to the server. The colours
// stay CSS-only (see _DARK_BODY in app.py): this code only sets
// data-theme on <html> and remembers the choice. There used to be a
// third "system" choice (no data-theme, a prefers-color-scheme media
// query decided). Daniel reported that with the system on dark,
// asking for light kept flickering on every transition — the media
// query and the stored choice fought, and each page load flashed the
// wrong theme first. So the system mode is gone: two states, each tap
// flips directly to the other, and the default (no stored choice) is
// light. A stored "system" from the old version is treated as light.
const THEME_KEY = "seat-theme";
const THEME_LABELS = { light: "☀️ בהיר", dark: "🌙 כהה" };

function applyTheme(theme) {
  const html = document.documentElement;
  if (theme !== "dark") {
    theme = "light";
  }
  html.setAttribute("data-theme", theme);
  const button = document.getElementById("theme-toggle");
  if (button) {
    button.textContent = THEME_LABELS[theme];
  }
}

function currentTheme() {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "dark" || stored === "light") {
      return stored;
    }
  } catch (e) {
    // Private browsing can block localStorage — the page stays
    // light, the toggle just does not remember.
  }
  return "light";
}

function toggleTheme() {
  const theme = currentTheme() === "dark" ? "light" : "dark";
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch (e) {
    // See currentTheme: no storage, no memory — the choice still
    // applies for this page load.
  }
  applyTheme(theme);
}

document.addEventListener("DOMContentLoaded", () => {
  const button = document.getElementById("enable-push");
  if (button) {
    button.addEventListener("click", () => enablePush());
  }
  // Apply the stored theme as early as possible, and wire the
  // toggle. The button is in the nav on every page (see page_html).
  applyTheme(currentTheme());
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => toggleTheme());
  }
});

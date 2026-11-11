// Seat Saver — Service Worker. This file runs in the background, even
// when the browser tab is closed, and its only job is to show a push
// notification when one arrives, and to open the app when it is tapped.
// It does no app logic and never talks to the database directly.
//
// Round 20, item 11: the notification can carry action buttons
// ("אני בדרך", "ביטול", "הגעתי", "המקום לא היה שם"). The server
// sends them in the payload (an "actions" list and a "request_id");
// this file passes the buttons to showNotification, and when one is
// tapped it POSTs the action to /api/action — the same endpoint the
// button on /my runs — and then opens the app. Tapping the
// notification body itself (no button) only opens the app, as before.

self.addEventListener("push", (event) => {
  let data = { title: "שומר מקומות", body: "יש מקום פנוי.", url: "/offers" };
  try {
    if (event.data) data = Object.assign(data, event.data.json());
  } catch (e) {
    // A push without JSON still shows a generic notification.
  }
  // The buttons, when the payload carries them — a plain list of
  // {action, title} pairs. A payload without actions shows no
  // buttons at all, exactly as before.
  const options = {
    body: data.body,
    tag: data.tag || "seat-saver",
    data: { url: data.url || "/offers", request_id: data.request_id || null },
  };
  if (Array.isArray(data.actions) && data.actions.length > 0) {
    options.actions = data.actions;
  }
  event.waitUntil(self.registration.showNotification(data.title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/offers";
  const requestId =
    event.notification.data && event.notification.data.request_id;
  // A button tap (event.action is set) runs the action on the server
  // first, then opens the app — the user sees the result there.
  // The fetch is same-origin, so the session cookie rides along and
  // the server knows who tapped. A failed POST changes nothing: the
  // app still opens, and the button on /my does the same thing.
  // A body tap (no action) only opens the app, as before.
  if (event.action && requestId) {
    event.waitUntil(
      fetch("/api/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: event.action, request_id: requestId }),
      })
        .catch(() => null)
        .then(() =>
          clients.matchAll({ type: "window", includeUncontrolled: true })
        )
        .then((list) => {
          for (const client of list) {
            if (new URL(client.url).pathname === url) return client.focus();
          }
          return clients.openWindow(url);
        })
    );
    return;
  }
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (new URL(client.url).pathname === url) return client.focus();
      }
      return clients.openWindow(url);
    })
  );
});

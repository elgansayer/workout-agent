self.addEventListener("push", function (event) {
  let payload = event.data ? event.data.text() : "no payload";
  let title = "Workout Agent";
  let options = {
    body: payload,
    icon: "/favicon.ico",
    badge: "/favicon.ico",
    data: { url: "/" },
  };
  
  try {
    const data = JSON.parse(payload);
    if (data.title) {
        title = data.title;
    }
    if (data.body) {
        options.body = data.body;
    }
    if (data.url) {
        options.data.url = data.url;
    }
  } catch (e) {
    // If it's not JSON, just show the plain payload
  }

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const urlToOpen = event.notification.data.url || "/";

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      // Check if there is already a window/tab open with the target URL
      for (let i = 0; i < windowClients.length; i++) {
        const client = windowClients[i];
        // If so, just focus it.
        if (client.url === urlToOpen && "focus" in client) {
          return client.focus();
        }
      }
      // If not, then open the target URL in a new window/tab.
      if (clients.openWindow) {
        return clients.openWindow(urlToOpen);
      }
    })
  );
});

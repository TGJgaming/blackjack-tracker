// Localhost-only WebSocket bridge for native Brave tab reloading.
// Connects in parallel to both Blackjack (8765) and Baccarat (8766) companions.
// Runs purely in the MV3 service worker with ONLY `tabs` and `alarms` permissions
// (zero content scripts, zero host permissions, zero DOM/page access).

const WS_URLS = ["ws://127.0.0.1:8765", "ws://127.0.0.1:8766"];
const HEARTBEAT_MS = 20000;
const RECONNECT_MS = 4000;

// Per-endpoint connection state
const endpoints = new Map(
  WS_URLS.map((url) => [
    url,
    { url, ws: null, heartbeatTimer: null, reconnectTimer: null },
  ])
);

// Tracks active reload requests keyed by tabId so onUpdated reports load progress back to the requesting companion
const watchedTabs = new Map();

function updateBadge() {
  let openCount = 0;
  for (const ep of endpoints.values()) {
    if (ep.ws && ep.ws.readyState === WebSocket.OPEN) openCount++;
  }
  try {
    if (openCount >= 2) {
      chrome.action.setBadgeText({ text: "ON2" });
      chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
    } else if (openCount === 1) {
      chrome.action.setBadgeText({ text: "ON" });
      chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
    } else {
      chrome.action.setBadgeText({ text: "OFF" });
      chrome.action.setBadgeBackgroundColor({ color: "#64748b" });
    }
  } catch (_) {}
}

function sendJson(ep, payload) {
  if (ep && ep.ws && ep.ws.readyState === WebSocket.OPEN) {
    try {
      ep.ws.send(JSON.stringify(payload));
      return true;
    } catch (_) {
      return false;
    }
  }
  return false;
}

function cleanupSocket(ep) {
  if (ep.heartbeatTimer) {
    clearInterval(ep.heartbeatTimer);
    ep.heartbeatTimer = null;
  }
  ep.ws = null;
  updateBadge();
}

function scheduleReconnect(ep, delayMs = RECONNECT_MS) {
  if (ep.reconnectTimer) return;
  ep.reconnectTimer = setTimeout(() => {
    ep.reconnectTimer = null;
    connectEndpoint(ep);
  }, delayMs);
}

function connectEndpoint(ep) {
  if (ep.ws && (ep.ws.readyState === WebSocket.CONNECTING || ep.ws.readyState === WebSocket.OPEN)) {
    return;
  }
  try {
    const sock = new WebSocket(ep.url);
    ep.ws = sock;

    sock.addEventListener("open", () => {
      if (ep.ws !== sock) return;
      updateBadge();
      sendJson(ep, { type: "browser_ext_hello", agent: "brave_ext", version: "1.1.0" });
      if (ep.heartbeatTimer) clearInterval(ep.heartbeatTimer);
      ep.heartbeatTimer = setInterval(() => {
        if (!sendJson(ep, { type: "browser_ext_ping", ts: Date.now() })) {
          cleanupSocket(ep);
          scheduleReconnect(ep, RECONNECT_MS);
        }
      }, HEARTBEAT_MS);
    });

    sock.addEventListener("message", (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "ext_reload_tab") {
        handleReloadCommand(ep, msg).catch((err) => {
          sendJson(ep, {
            type: "ext_reload_ack",
            requestId: msg.requestId || "",
            ok: false,
            reason: String(err && err.message ? err.message : err || "reload_exception"),
          });
        });
      }
    });

    sock.addEventListener("close", () => {
      if (ep.ws === sock) {
        cleanupSocket(ep);
        scheduleReconnect(ep, RECONNECT_MS);
      }
    });

    sock.addEventListener("error", () => {
      if (ep.ws === sock) {
        try { sock.close(); } catch (_) {}
        cleanupSocket(ep);
        scheduleReconnect(ep, RECONNECT_MS);
      }
    });
  } catch (_) {
    cleanupSocket(ep);
    scheduleReconnect(ep, RECONNECT_MS);
  }
}

function connectAll() {
  for (const ep of endpoints.values()) {
    connectEndpoint(ep);
  }
}

async function selectTargetTab(msg) {
  const subs = Array.isArray(msg.titleSubstrings)
    ? msg.titleSubstrings.map((s) => String(s || "").toLowerCase()).filter(Boolean)
    : ["live games", "all bets blackjack", "epicworld", "playtech"];
  const excludes = Array.isArray(msg.excludeTitles)
    ? msg.excludeTitles.map((s) => String(s || "").toLowerCase()).filter(Boolean)
    : ["advantage & wonging tracker", "baccarat tracker", "playwright", "companion calibrator"];
  const preferLeft = msg.preferLeft !== false;

  const tabs = await chrome.tabs.query({});
  const winLeftCache = new Map();
  const candidates = [];

  for (const tab of tabs) {
    const url = String(tab.url || "");
    if (!/^https?:\/\//i.test(url)) continue;
    const titleL = String(tab.title || "").toLowerCase();
    const urlL = url.toLowerCase();
    const hay = `${titleL} ${urlL}`;
    if (excludes.some((ex) => hay.includes(ex))) continue;

    let matchRank = -1;
    for (let i = 0; i < subs.length; i++) {
      if (hay.includes(subs[i])) {
        matchRank = i;
        break;
      }
    }
    if (matchRank < 0) continue;

    let winLeft = 0;
    if (typeof tab.windowId === "number") {
      if (!winLeftCache.has(tab.windowId)) {
        try {
          const w = await chrome.windows.get(tab.windowId);
          winLeftCache.set(tab.windowId, typeof w.left === "number" ? w.left : 0);
        } catch (_) {
          winLeftCache.set(tab.windowId, 0);
        }
      }
      winLeft = winLeftCache.get(tab.windowId);
    }

    candidates.push({
      tab,
      matchRank,
      activeOrder: tab.active ? 0 : 1,
      winLeft,
    });
  }

  if (!candidates.length) return null;

  candidates.sort((a, b) => {
    if (a.matchRank !== b.matchRank) return a.matchRank - b.matchRank;
    if (preferLeft && a.winLeft !== b.winLeft) return a.winLeft - b.winLeft;
    if (a.activeOrder !== b.activeOrder) return a.activeOrder - b.activeOrder;
    return (a.tab.index || 0) - (b.tab.index || 0);
  });

  return candidates[0].tab;
}

async function handleReloadCommand(ep, msg) {
  const requestId = String(msg.requestId || "");
  const target = await selectTargetTab(msg);
  if (!target || typeof target.id !== "number") {
    sendJson(ep, {
      type: "ext_reload_ack",
      requestId,
      ok: false,
      reason: "tab_not_found",
    });
    return;
  }

  watchedTabs.set(target.id, {
    ep,
    requestId,
    startedAt: Date.now(),
    title: target.title || "",
  });

  await chrome.tabs.reload(target.id, { bypassCache: !!msg.bypassCache });

  sendJson(ep, {
    type: "ext_reload_ack",
    requestId,
    ok: true,
    tabId: target.id,
    windowId: target.windowId,
    title: target.title || "",
  });
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  const info = watchedTabs.get(tabId);
  if (!info) return;
  if (Date.now() - info.startedAt > 35000) {
    watchedTabs.delete(tabId);
    return;
  }
  if (changeInfo.status === "loading" || changeInfo.status === "complete") {
    sendJson(info.ep, {
      type: "ext_tab_status",
      tabId,
      requestId: info.requestId,
      status: changeInfo.status,
      title: (tab && tab.title) || info.title || "",
    });
    if (changeInfo.status === "complete") {
      watchedTabs.delete(tabId);
    }
  }
});

chrome.alarms.get("companionReconnect", (existing) => {
  if (!existing) {
    chrome.alarms.create("companionReconnect", { periodInMinutes: 1 });
  }
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === "companionReconnect") {
    connectAll();
  }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("companionReconnect", { periodInMinutes: 1 });
  connectAll();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("companionReconnect", { periodInMinutes: 1 });
  connectAll();
});

connectAll();

// Localhost-only WebSocket bridge for native Brave tab reloading.
// Runs purely in the MV3 service worker with ONLY `tabs` and `alarms` permissions
// (zero content scripts, zero host permissions, zero DOM/page access).

const WS_URL = "ws://127.0.0.1:8765";
const HEARTBEAT_MS = 20000;
const RECONNECT_MS = 4000;

let ws = null;
let heartbeatTimer = null;
let reconnectTimer = null;

// Tracks active reload requests keyed by tabId so onUpdated reports load progress cleanly
const watchedTabs = new Map();

function setBadge(text, color) {
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
  } catch (_) {}
}

function sendJson(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify(payload));
      return true;
    } catch (_) {
      return false;
    }
  }
  return false;
}

function cleanupSocket() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  ws = null;
  setBadge("OFF", "#64748b");
}

function scheduleReconnect(delayMs = RECONNECT_MS) {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectCompanion();
  }, delayMs);
}

function connectCompanion() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return;
  }
  try {
    const sock = new WebSocket(WS_URL);
    ws = sock;

    sock.addEventListener("open", () => {
      if (ws !== sock) return;
      setBadge("ON", "#16a34a");
      sendJson({ type: "browser_ext_hello", agent: "brave_ext", version: "1.0.0" });
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(() => {
        if (!sendJson({ type: "browser_ext_ping", ts: Date.now() })) {
          cleanupSocket();
          scheduleReconnect(RECONNECT_MS);
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
        handleReloadCommand(msg).catch((err) => {
          sendJson({
            type: "ext_reload_ack",
            requestId: msg.requestId || "",
            ok: false,
            reason: String(err && err.message ? err.message : err || "reload_exception"),
          });
        });
      }
    });

    sock.addEventListener("close", () => {
      if (ws === sock) {
        cleanupSocket();
        scheduleReconnect(RECONNECT_MS);
      }
    });

    sock.addEventListener("error", () => {
      if (ws === sock) {
        try { sock.close(); } catch (_) {}
        cleanupSocket();
        scheduleReconnect(RECONNECT_MS);
      }
    });
  } catch (_) {
    cleanupSocket();
    scheduleReconnect(RECONNECT_MS);
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

async function handleReloadCommand(msg) {
  const requestId = String(msg.requestId || "");
  const target = await selectTargetTab(msg);
  if (!target || typeof target.id !== "number") {
    sendJson({
      type: "ext_reload_ack",
      requestId,
      ok: false,
      reason: "tab_not_found",
    });
    return;
  }

  watchedTabs.set(target.id, {
    requestId,
    startedAt: Date.now(),
    title: target.title || "",
  });

  await chrome.tabs.reload(target.id, { bypassCache: !!msg.bypassCache });

  sendJson({
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
    sendJson({
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
    connectCompanion();
  }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("companionReconnect", { periodInMinutes: 1 });
  connectCompanion();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("companionReconnect", { periodInMinutes: 1 });
  connectCompanion();
});

connectCompanion();

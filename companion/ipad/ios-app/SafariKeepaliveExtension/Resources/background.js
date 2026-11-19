/**
 * Newport / Playtech Background Tab Reload Helper (iPadOS Safari MV3 Event Page)
 *
 * Security / Anti-Detection Design:
 * - Zero content_scripts, zero scripting permission.
 * - Never touches or inspects the DOM, window, or network requests of the game page.
 * - Only invokes browser.tabs.reload(tabId, { bypassCache: false }) when commanded by the
 *   local companion relay (ws://127.0.0.1:8765) during a safe Place Bets break.
 */

const extApi = typeof browser !== "undefined" ? browser : chrome;
const WS_URL = "ws://127.0.0.1:8765";
const DEFAULT_KEYWORDS = [
  "live games",
  "all bets blackjack",
  "unlimited blackjack",
  "epicworld",
  "playtech",
  "newport",
  "blackjack",
  "casino"
];

let ws = null;
let reconnectTimer = null;
let heartbeatTimer = null;
let lastPingTs = 0;

function scheduleReconnect(delayMs = 2000) {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectCompanion, delayMs);
}

function sendJson(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify(payload));
      return true;
    } catch (_) {}
  }
  return false;
}

function isTrackerTab(titleLower, urlLower) {
  return (
    titleLower.includes("advantage & wonging tracker") ||
    urlLower.includes("blackjack-tracker") ||
    urlLower.includes("baccarat-tracker") ||
    urlLower.startsWith("bjtracker:")
  );
}

function isHttpUrl(urlLower) {
  return urlLower.startsWith("http://") || urlLower.startsWith("https://");
}

async function inspectTabs(customKeywords) {
  const kws = (Array.isArray(customKeywords) && customKeywords.length ? customKeywords : DEFAULT_KEYWORDS)
    .map((s) => String(s || "").toLowerCase().trim())
    .filter(Boolean);

  const tabs = await extApi.tabs.query({});
  const matched = [];
  const httpCandidates = [];
  let anyUrlOrTitle = false;

  for (const tab of tabs) {
    if (!tab || typeof tab.id !== "number") continue;
    const rawTitle = String(tab.title || "");
    const rawUrl = String(tab.url || "");
    if (rawTitle.trim() || rawUrl.trim()) anyUrlOrTitle = true;

    const title = rawTitle.toLowerCase();
    const url = rawUrl.toLowerCase();
    if (isTrackerTab(title, url)) continue;

    const idx = typeof tab.index === "number" ? tab.index : 999;
    const active = Boolean(tab.active);
    const httpOk = isHttpUrl(url);
    if (httpOk) {
      httpCandidates.push({ tab, active, index: idx });
    }
    const isKeywordMatch = kws.some((k) => title.includes(k) || url.includes(k));
    if (isKeywordMatch) {
      matched.push({ tab, active, index: idx, httpOk });
    }
  }

  matched.sort((a, b) => {
    if (a.httpOk !== b.httpOk) return a.httpOk ? -1 : 1;
    if (a.active !== b.active) return a.active ? -1 : 1;
    return a.index - b.index;
  });
  httpCandidates.sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1;
    return a.index - b.index;
  });

  // On Safari iPadOS without "Allow" website permission, tabs.query({}) returns empty url/title strings.
  const permissionOk = tabs.length === 0 ? true : anyUrlOrTitle;
  return {
    tabCount: tabs.length,
    permissionOk,
    matchedCount: matched.length,
    httpCount: httpCandidates.length,
    matchedTab: matched.length ? matched[0].tab : null,
    fallbackTab: httpCandidates.length ? httpCandidates[0].tab : null
  };
}

async function emitDiagStatus(kind = "ext_ping", customKeywords = null) {
  try {
    const info = await inspectTabs(customKeywords);
    sendJson({
      type: kind,
      role: "keepalive_ext",
      platform: "ipados_safari",
      version: "1.0.1",
      ts: Date.now(),
      permissionOk: info.permissionOk,
      tabCount: info.tabCount,
      matchedCount: info.matchedCount,
      httpCount: info.httpCount
    });
  } catch (err) {
    sendJson({
      type: kind,
      role: "keepalive_ext",
      platform: "ipados_safari",
      version: "1.0.1",
      ts: Date.now(),
      permissionOk: false,
      error: String((err && err.message) || err || "inspect_failed")
    });
  }
}

async function handleReloadCommand(msg) {
  const testImmediate = Boolean(msg && msg.testImmediate);
  try {
    const info = await inspectTabs(msg && msg.keywords);
    if (!info.permissionOk && info.tabCount > 0) {
      sendJson({
        type: "ext_reload_ack",
        ok: false,
        reason: "missing_tab_permissions",
        tabCount: info.tabCount,
        matchedCount: 0,
        permissionOk: false,
        testImmediate
      });
      return;
    }

    const allowFallback = Boolean(msg && msg.allowActiveFallback === true);
    const targetTab = info.matchedTab || (allowFallback ? info.fallbackTab : null);
    if (!targetTab || typeof targetTab.id !== "number") {
      sendJson({
        type: "ext_reload_ack",
        ok: false,
        reason: "no_matching_tab",
        tabCount: info.tabCount,
        matchedCount: 0,
        permissionOk: info.permissionOk,
        testImmediate
      });
      return;
    }

    await extApi.tabs.reload(targetTab.id, { bypassCache: false });
    sendJson({
      type: "ext_reload_ack",
      ok: true,
      tabId: targetTab.id,
      matchedBy: info.matchedTab ? "keyword" : "active_http_fallback",
      title: String(targetTab.title || "").slice(0, 80),
      tabCount: info.tabCount,
      matchedCount: info.matchedCount,
      permissionOk: info.permissionOk,
      testImmediate
    });
  } catch (err) {
    sendJson({
      type: "ext_reload_ack",
      ok: false,
      reason: String((err && err.message) || err || "reload_exception"),
      testImmediate
    });
  }
}

function ensureHeartbeatRunning() {
  if (heartbeatTimer) return;
  heartbeatTimer = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      connectCompanion();
    } else {
      emitDiagStatus("ext_ping");
    }
  }, 18000);
}

function connectCompanion() {
  ensureHeartbeatRunning();
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return;
  }
  try {
    ws = new WebSocket(WS_URL);
  } catch (_) {
    scheduleReconnect(3000);
    return;
  }

  ws.onopen = () => {
    lastPingTs = Date.now();
    emitDiagStatus("ext_hello");
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (!msg || !msg.type) return;
      if (msg.type === "ext_reload") {
        handleReloadCommand(msg);
      } else if (msg.type === "ext_status_req") {
        emitDiagStatus("ext_ping", msg.keywords);
      }
    } catch (_) {}
  };

  ws.onerror = () => {};
  ws.onclose = () => {
    ws = null;
    scheduleReconnect(2500);
  };
}

function wakeAndSync(forcePing = false) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    connectCompanion();
  } else if (forcePing && (Date.now() - lastPingTs) >= 2000) {
    lastPingTs = Date.now();
    emitDiagStatus("ext_ping");
  }
}

if (extApi && extApi.alarms) {
  try {
    extApi.alarms.create("companion_ws_keepalive", { delayInMinutes: 0.1, periodInMinutes: 1 });
  } catch (_) {}
  extApi.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === "companion_ws_keepalive") {
      wakeAndSync(true);
    }
  });
}

if (extApi && extApi.runtime && extApi.runtime.onMessage) {
  extApi.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "popup_reload") {
      handleReloadCommand({ testImmediate: true }).then(() => sendResponse({ ok: true }), () => sendResponse({ ok: false }));
      return true;
    }
    return false;
  });
}

if (extApi && extApi.runtime) {
  if (extApi.runtime.onStartup) extApi.runtime.onStartup.addListener(() => wakeAndSync(true));
  if (extApi.runtime.onInstalled) extApi.runtime.onInstalled.addListener(() => wakeAndSync(true));
}
if (extApi && extApi.tabs) {
  if (extApi.tabs.onActivated) extApi.tabs.onActivated.addListener(() => wakeAndSync(true));
  if (extApi.tabs.onUpdated) {
    extApi.tabs.onUpdated.addListener((_, changeInfo) => {
      if (changeInfo && changeInfo.status === "complete") {
        wakeAndSync(true);
      }
    });
  }
}
if (extApi && extApi.windows && extApi.windows.onFocusChanged) {
  extApi.windows.onFocusChanged.addListener(() => wakeAndSync(true));
}

connectCompanion();


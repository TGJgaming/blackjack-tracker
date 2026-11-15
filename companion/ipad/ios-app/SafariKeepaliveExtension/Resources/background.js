/**
 * Newport / Playtech Background Tab Reload Helper (iPadOS Safari MV3 Service Worker)
 *
 * Security / Anti-Detection Design:
 * - Zero content_scripts, zero scripting permission, zero host_permissions.
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
  "blackjack"
];

let ws = null;
let reconnectTimer = null;

function scheduleReconnect(delayMs = 3000) {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectCompanion, delayMs);
}

async function findPlaytechTab(customKeywords) {
  const kws = (Array.isArray(customKeywords) && customKeywords.length ? customKeywords : DEFAULT_KEYWORDS)
    .map((s) => String(s || "").toLowerCase().trim())
    .filter(Boolean);

  const tabs = await extApi.tabs.query({});
  const candidates = [];

  for (const tab of tabs) {
    if (!tab || typeof tab.id !== "number") continue;
    const title = String(tab.title || "").toLowerCase();
    const url = String(tab.url || "").toLowerCase();
    if (title.includes("advantage & wonging tracker") || url.includes("blackjack-tracker")) {
      continue;
    }
    const matched = kws.some((k) => title.includes(k) || url.includes(k));
    if (!matched) continue;
    candidates.push({
      tab,
      active: Boolean(tab.active),
      index: typeof tab.index === "number" ? tab.index : 999
    });
  }

  if (!candidates.length) return null;
  candidates.sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1;
    return a.index - b.index;
  });
  return candidates[0].tab;
}

async function handleReloadCommand(msg) {
  try {
    const tab = await findPlaytechTab(msg.keywords);
    if (!tab || typeof tab.id !== "number") {
      sendJson({
        type: "ext_reload_ack",
        ok: false,
        reason: "no_matching_tab"
      });
      return;
    }
    await extApi.tabs.reload(tab.id, { bypassCache: false });
    sendJson({
      type: "ext_reload_ack",
      ok: true,
      tabId: tab.id,
      title: String(tab.title || "").slice(0, 80)
    });
  } catch (err) {
    sendJson({
      type: "ext_reload_ack",
      ok: false,
      reason: String((err && err.message) || err || "reload_exception")
    });
  }
}

function sendJson(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify(payload));
    } catch (_) {}
  }
}

function connectCompanion() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return;
  }
  try {
    ws = new WebSocket(WS_URL);
  } catch (_) {
    scheduleReconnect(4000);
    return;
  }

  ws.onopen = () => {
    sendJson({
      type: "ext_hello",
      role: "keepalive_ext",
      platform: "ipados_safari",
      version: "1.0.0"
    });
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg && msg.type === "ext_reload") {
        handleReloadCommand(msg);
      }
    } catch (_) {}
  };

  ws.onerror = () => {};
  ws.onclose = () => {
    ws = null;
    scheduleReconnect(3500);
  };
}

if (extApi && extApi.alarms) {
  extApi.alarms.create("companion_ws_keepalive", { periodInMinutes: 0.4 });
  extApi.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === "companion_ws_keepalive") {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        connectCompanion();
      } else {
        sendJson({ type: "ext_ping", ts: Date.now() });
      }
    }
  });
}

connectCompanion();

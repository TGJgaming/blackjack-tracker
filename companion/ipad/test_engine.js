"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// Load default_pack.js into global scope
const packCode = fs.readFileSync(path.join(__dirname, "default_pack.js"), "utf8");
const sandbox = { window: global, global, console, performance, setTimeout, clearTimeout, Uint8Array, Float32Array, Float64Array, Int32Array, Buffer, Math, Object, Array, String, Number, Boolean, Set, Map, Date, JSON };
vm.runInNewContext(packCode, sandbox);

const Engine = require("./companion_engine.js");

function pasteImage(dst, src, x0, y0) {
  for (let y = 0; y < src.h; y++) {
    const dy = y0 + y;
    if (dy < 0 || dy >= dst.h) continue;
    for (let x = 0; x < src.w; x++) {
      const dx = x0 + x;
      if (dx < 0 || dx >= dst.w) continue;
      const sIdx = (y * src.w + x) * 3;
      const dIdx = (dy * dst.w + dx) * 3;
      dst.data[dIdx] = src.data[sIdx];
      dst.data[dIdx + 1] = src.data[sIdx + 1];
      dst.data[dIdx + 2] = src.data[sIdx + 2];
    }
  }
}

function fillRect(img, x0, y0, w, h, b, g, r) {
  for (let y = Math.max(0, y0); y < Math.min(img.h, y0 + h); y++) {
    for (let x = Math.max(0, x0); x < Math.min(img.w, x0 + w); x++) {
      const idx = (y * img.w + x) * 3;
      img.data[idx] = b;
      img.data[idx + 1] = g;
      img.data[idx + 2] = r;
    }
  }
}

console.log("1. Testing Default Companion Pack loading...");
const pack = global.DEFAULT_COMPANION_PACK;
assert(pack && pack.templates, "DEFAULT_COMPANION_PACK must be loaded");
const cfg = Engine.defaultConfig(pack.config);
const detector = new Engine.PlaytechDetector(cfg, pack);
const counts = detector.listTemplateCounts();
const totalTemplates = Object.values(detector.banks).reduce((acc, arr) => acc + arr.length, 0);
console.log("   Loaded templates:", totalTemplates, "across banks:", JSON.stringify(counts));
assert(totalTemplates >= 70, `Expected >= 70 templates loaded, got ${totalTemplates}`);

console.log("2. Testing iPad Split View Left-Pane 16:9 Viewport Isolation...");
// Simulate a 1366x1024 iPad Pro screen in Split View:
// Left pane: x=0..820 (Safari with top bar y=0..90, 16:9 game viewport y=200..661, dark pillars)
// Divider: x=820..832 (near-black iPadOS Split View divider)
// Right pane: x=832..1366 (Bright UI elements of Tracker)
const fullScreen = Engine.createImage(1366, 1024, 3, 8);
// Safari top bar
fillRect(fullScreen, 0, 0, 820, 90, 35, 35, 38);
// Active 16:9 Playtech game frame inside left Safari pane: x=0..820, y=200..661 (820x461)
for (let y = 200; y < 661; y++) {
  for (let x = 0; x < 820; x++) {
    const idx = (y * 1366 + x) * 3;
    fullScreen.data[idx] = 35 + ((x * 7 + y * 3) % 90);
    fullScreen.data[idx + 1] = 65 + ((x * 5 + y * 11) % 110);
    fullScreen.data[idx + 2] = 40 + ((x * 13 + y * 7) % 85);
  }
}
// Black iPadOS Split View divider at x=820..832
fillRect(fullScreen, 820, 0, 12, 1024, 2, 2, 2);
// Right pane tracker UI
fillRect(fullScreen, 832, 0, 534, 1024, 28, 34, 48);
fillRect(fullScreen, 860, 120, 480, 300, 180, 210, 190);

const [vx, vy, vw, vh] = Engine.detectSplitViewLeftPane(fullScreen);
console.log(`   Split-View isolated rect: x=${vx}, y=${vy}, w=${vw}, h=${vh}`);
assert(vx + vw <= 832, `Left pane width ${vx + vw} should not cross Split View divider at 832`);
assert(Math.abs(vy - 200) <= 25 && Math.abs(vh - 461) <= 40, `Expected y~200, h~461, got y=${vy}, h=${vh}`);

console.log("3. Testing HUD Card + Suit Detection with Real User Templates...");
const hudStrip = Engine.createImage(240, 70, 3, 25);
const tplA = detector.banks.hud.find((t) => t.rank === "A");
const tplK = detector.banks.hud.find((t) => t.rank === "K");
const suitS = detector.banks.suit.find((t) => t.rank === "S");
const suitH = detector.banks.suit.find((t) => t.rank === "H");
assert(tplA && tplK && suitS && suitH, "Must have A, K, S, H templates");

// Build two white 32x45 HUD cards on dark felt background
function drawHudCard(strip, x0, y0, rankTpl, suitTpl, isRed) {
  fillRect(strip, x0, y0, 32, 45, 248, 248, 248);
  // Paste rank glyph at (x0+3, y0+2)
  for (let y = 0; y < rankTpl.bgr.h; y++) {
    for (let x = 0; x < rankTpl.bgr.w; x++) {
      const v = rankTpl.min_gray.data[y * rankTpl.bgr.w + x];
      const idx = ((y0 + 2 + y) * strip.w + (x0 + 3 + x)) * 3;
      if (v < 180) {
        strip.data[idx] = isRed ? 25 : 15;
        strip.data[idx + 1] = isRed ? 25 : 15;
        strip.data[idx + 2] = isRed ? 215 : 15;
      }
    }
  }
  // Paste suit glyph at (x0+3, y0+28)
  for (let y = 0; y < suitTpl.bgr.h; y++) {
    for (let x = 0; x < suitTpl.bgr.w; x++) {
      const v = suitTpl.min_gray.data[y * suitTpl.bgr.w + x];
      const idx = ((y0 + 28 + y) * strip.w + (x0 + 3 + x)) * 3;
      if (v < 180) {
        strip.data[idx] = isRed ? 25 : 15;
        strip.data[idx + 1] = isRed ? 25 : 15;
        strip.data[idx + 2] = isRed ? 215 : 15;
      }
    }
  }
}

drawHudCard(hudStrip, 20, 12, tplA, suitS, false);
drawHudCard(hudStrip, 56, 12, tplK, suitH, true);

const [pCards, pHands, isSplit] = detector.detectHudStrip(hudStrip, false);
console.log("   Detected HUD cards:", pCards.map((c) => `${c.rank}${c.suit || ""} (${c.conf.toFixed(2)})`).join(", "));
assert.strictEqual(pCards.length, 2, "Should detect 2 player HUD cards");
assert.strictEqual(pCards[0].rank, "A");
assert.strictEqual(pCards[0].suit, "S");
assert.strictEqual(pCards[1].rank, "K");
assert.strictEqual(pCards[1].suit, "H");
assert.strictEqual(isSplit, false);

console.log("4. Testing CompanionStateMachine & WatchKeepaliveManager...");
const sm = new Engine.CompanionStateMachine(cfg);
const ka = new Engine.WatchKeepaliveManager(cfg);

// Step 1: WAIT banner starts round
let evts = sm.step({ banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
assert(evts.some((e) => e.type === "phase" && e.phase === "WAIT"), "WAIT phase should emit");

// Step 2: 3 frames of A♠, K♥ for Player and 10♠ for Dealer
const dCard10 = [{ rank: "10", val: 0, conf: 0.94, suit: "S", x: 10, y: 10, w: 14, h: 16, hand_index: 0 }];
let cardEvents = [];
for (let f = 0; f < 3; f++) {
  cardEvents.push(
    ...sm.step({
      banner_state: "WAIT",
      banner_conf: 0.95,
      player_cards: pCards,
      dealer_cards: dCard10,
      player_hands: { 0: pCards },
      split_detected: false,
    })
  );
}
const emittedCards = cardEvents.filter((e) => e.type === "card");
console.log("   Emitted card events:", emittedCards.map((e) => `${e.target}:${e.rankLabel}${e.suit || ""}`).join(", "));
assert.strictEqual(emittedCards.length, 3, "Should emit 2 Player cards + 1 Dealer card");

// Step 3: PLACE_BETS banner finalizes watched round and queues auto_watch, then NO_MORE_BETS flushes auto_watch
const pbEvents = sm.step({ banner_state: "PLACE_BETS", banner_conf: 0.93, player_cards: [], dealer_cards: [], player_hands: {} });
const nmbEvents = sm.step({ banner_state: "NO_MORE_BETS", banner_conf: 0.93, player_cards: [], dealer_cards: [], player_hands: {} });
const allEndEvents = [...pbEvents, ...nmbEvents];
assert(allEndEvents.some((e) => e.type === "auto_watch"), "Watched round should emit auto_watch");

// Step 4: Keepalive arming & triggering during PLACE_BETS
ka.armNow(100.0);
let refreshed = false;
const kaEvts = ka.step({
  now: 101.0,
  windowFound: true,
  obs: { banner_state: "PLACE_BETS", place_bets_conf: 0.91, player_cards: [], dealer_cards: [] },
  smState: "BETTING",
  playMode: "watch",
  draftCount: 0,
  hasPrompt: false,
  betRoundsCount: 0,
  triggerRefreshCb: () => {
    refreshed = true;
    return [true, "safari_ext_reload"];
  },
});
assert(refreshed && kaEvts.some((e) => e.event === "refreshing"), "Keepalive should trigger Safari reload during PLACE_BETS");

console.log("   All iPad JS CV & State Machine parity checks PASSED!");

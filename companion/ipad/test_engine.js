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
const totalTemplates = Object.values(counts).reduce(
  (acc, m) => acc + Object.values(m).reduce((a, b) => a + b, 0),
  0
);
console.log("   Loaded templates:", totalTemplates, "across banks:", JSON.stringify(counts));
assert(totalTemplates >= 70, `Expected >= 70 templates loaded, got ${totalTemplates}`);
assert.strictEqual(detector.banks.felt.length, 0, "Expected felt bank to be lazy-decoded on startup");
assert.strictEqual(detector.banks.ui.length, 0, "Expected ui bank to be lazy-decoded on startup");

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

console.log("5. Testing fromRGBA & IpadCompanionRuntime.ingestRGBA (non-square w != h)...");
const emptyBadRgba = Engine.fromRGBA(null, 10, 10);
assert.strictEqual(emptyBadRgba.w, 0, "Null RGBA should safely return empty image");
const shortRgba = Engine.fromRGBA(new Uint8Array(8), 4, 4);
assert.strictEqual(shortRgba.w, 0, "Short RGBA buffer should safely return empty image");

const nonSqW = 5, nonSqH = 3;
const testRgba = new Uint8Array(nonSqW * nonSqH * 4);
for (let i = 0; i < nonSqW * nonSqH; i++) {
  testRgba[i * 4] = 200;     // R
  testRgba[i * 4 + 1] = 100; // G
  testRgba[i * 4 + 2] = 50;  // B
  testRgba[i * 4 + 3] = 255; // A
}
const bgrConv = Engine.fromRGBA(testRgba, nonSqW, nonSqH);
assert.strictEqual(bgrConv.w, nonSqW);
assert.strictEqual(bgrConv.h, nonSqH);
assert.strictEqual(bgrConv.data[0], 50, "Channel 0 must be B");
assert.strictEqual(bgrConv.data[1], 100, "Channel 1 must be G");
assert.strictEqual(bgrConv.data[2], 200, "Channel 2 must be R");

// Build non-square 1200x900 RGBA Split-View frame with left 16:9 game viewport + dark divider
const rtW = 1200, rtH = 900;
const rtRgba = new Uint8Array(rtW * rtH * 4);
for (let y = 0; y < rtH; y++) {
  for (let x = 0; x < rtW; x++) {
    const p = (y * rtW + x) * 4;
    let r = 8, g = 8, b = 10;
    if (x < 720 && y >= 180 && y < 585) {
      r = 40 + ((x * 13 + y * 7) % 85);
      g = 65 + ((x * 5 + y * 11) % 110);
      b = 35 + ((x * 7 + y * 3) % 90);
    } else if (x >= 720 && x < 732) {
      r = 2; g = 2; b = 2;
    } else if (x >= 732) {
      r = 48; g = 34; b = 28;
    }
    rtRgba[p] = r;
    rtRgba[p + 1] = g;
    rtRgba[p + 2] = b;
    rtRgba[p + 3] = 255;
  }
}
const rt = new Engine.IpadCompanionRuntime({ initialPack: pack });
rt.ingestRGBA(rtRgba, rtW, rtH, 200.0);
assert(rt.latestRawFrame, "ingestRGBA should populate latestRawFrame (arg order bug check)");
assert.strictEqual(rt.latestRawFrame.w, rtW, "ingestRGBA must preserve non-square width");
assert.strictEqual(rt.latestRawFrame.h, rtH, "ingestRGBA must preserve non-square height");
let calibSnap = null;
rt.handleClientMessage(
  { readyState: 1, onmessage: ({ data }) => { calibSnap = JSON.parse(data); } },
  JSON.stringify({ type: "calib_grab", anchorMode: "ipad_split_left" })
);
assert(calibSnap && calibSnap.type === "calib_snapshot", "calib_grab should succeed after ingestRGBA");
assert(calibSnap.width <= 732, `Isolated split pane width ${calibSnap.width} should stay left of divider`);

console.log("6. Testing zero-arg IpadCompanionRuntime & corrupt localStorage guard...");
const memStore = new Map();
global.localStorage = {
  getItem: (k) => (memStore.has(k) ? memStore.get(k) : null),
  setItem: (k, v) => memStore.set(k, String(v)),
  removeItem: (k) => memStore.delete(k),
};
memStore.set("bjPlaytechTracker.ipadCompanionPack.v1", JSON.stringify({ templates: {} }));
const zeroArgRt = new Engine.IpadCompanionRuntime();
const zeroCounts = zeroArgRt.detector.listTemplateCounts();
const zeroTotal = Object.values(zeroCounts).reduce((a, b) => a + Object.values(b).reduce((x, y) => x + y, 0), 0);
assert(zeroTotal >= 70, `Zero-arg runtime must reject empty saved pack and load PRELOADED_PACK (got ${zeroTotal})`);

console.log("7. Testing keepalive set_config, hostInfo, extConnected & native loopback polling...");
(async () => {
  const readyMsgs = [];
  global.window = {
    location: { protocol: "bjtracker:" },
    webkit: {
      messageHandlers: {
        ipadCompanion: { postMessage: (m) => readyMsgs.push(m) },
      },
    },
  };

  let mockPollMode = "idle";
  let mockSeq = 42;
  const requestedUrls = [];
  global.XMLHttpRequest = class MockXHR {
    open(method, url) {
      this._url = url;
      requestedUrls.push(url);
      this._headers = {};
    }
    send() {
      if (mockPollMode === "idle") {
        this.status = 204;
        this._headers = {
          "X-BJ-Transport": "idle",
          "X-BJ-Broadcast-Active": "0",
          "X-BJ-AppGroup-Ok": "0",
          "X-BJ-BroadcastExt-Present": "1",
        };
      } else if (mockPollMode === "waiting") {
        this.status = 204;
        this._headers = {
          "X-BJ-Transport": "loopback_waiting",
          "X-BJ-Broadcast-Active": "1",
          "X-BJ-AppGroup-Ok": "0",
          "X-BJ-BroadcastExt-Present": "1",
        };
      } else if (mockPollMode === "frame") {
        const w = 120, h = 90;
        const ab = new ArrayBuffer(16 + w * h * 4);
        const hdr = new Uint32Array(ab, 0, 4);
        hdr[0] = mockSeq;
        hdr[1] = w;
        hdr[2] = h;
        hdr[3] = 4;
        this.status = 200;
        this.response = ab;
        this._headers = {
          "X-BJ-Transport": "loopback",
          "X-BJ-Broadcast-Active": "1",
          "X-BJ-AppGroup-Ok": "0",
          "X-BJ-BroadcastExt-Present": "1",
          "X-BJ-Frame-Info": "900x1200,ori=6->120x90",
        };
      }
      if (this.onload) this.onload();
    }
    getResponseHeader(name) {
      return this._headers[name] || null;
    }
  };

  const pollRt = new Engine.IpadCompanionRuntime({ initialPack: pack });
  const received = [];
  const sock = new Engine.IpadCompanionSocket(pollRt);
  sock.onmessage = ({ data }) => received.push(JSON.parse(data));
  await new Promise((r) => setTimeout(r, 35));
  assert.strictEqual(sock.readyState, 1, "IpadCompanionSocket should open automatically");
  assert(readyMsgs.some((m) => m.action === "companionReady"), "startNativePolling must notify native companionReady");
  assert(received.some((m) => m.type === "status" && m.ipadMode === true), "Initial status message must arrive on open");

  // Update keepalive config via nested UI format ("set_config")
  sock.send(JSON.stringify({
    type: "keepalive_action",
    action: "set_config",
    keepalive: {
      enabled: true,
      mode: "alert_only",
      method: "ext_reload",
      min_watch_sec: 180,
      max_watch_sec: 240,
      reset_on_bet_round: false,
    },
  }));
  const kaStatus = pollRt.keepalive.getStatus(10.0);
  assert.strictEqual(kaStatus.mode, "alert_only");
  assert.strictEqual(kaStatus.targetSec, 240);
  assert.strictEqual(kaStatus.resetOnBetRound, false);

  // Trigger host info + extension hooks
  globalThis.__ipadCompanionSetHostInfo({ appGroupAvailable: false, broadcastExtPresent: true, loopbackPort: 8766 });
  globalThis.__ipadCompanionSetExtConnected(true);
  assert.strictEqual(pollRt.extConnected, true);
  assert.strictEqual(pollRt.hostInfo.loopbackPort, 8766);

  // Test loopback_waiting -> frame (200) -> ?since=42 -> idle reset
  mockPollMode = "waiting";
  await new Promise((r) => setTimeout(r, 140));
  assert.strictEqual(pollRt.broadcastActive, true, "loopback_waiting should keep broadcastActive true");
  assert.strictEqual(pollRt.frameTransport, "loopback_waiting");

  mockPollMode = "frame";
  await new Promise((r) => setTimeout(r, 140));
  assert(pollRt.latestRawFrame && pollRt.latestRawFrame.w === 120, "Native poll should ingest 200 RGBA frame");
  assert.strictEqual(pollRt._lastFrameSeq, 42);
  assert.strictEqual(pollRt.frameInfo, "900x1200,ori=6->120x90", "Poll must parse X-BJ-Frame-Info");
  assert.strictEqual(pollRt.buildStatusPayload().frameInfo, "900x1200,ori=6->120x90");
  await new Promise((r) => setTimeout(r, 140));
  assert(requestedUrls.some((u) => u.endsWith("?since=42")), "Subsequent polls must pass ?since=42 to skip duplicate buffers");

  mockPollMode = "idle";
  await new Promise((r) => setTimeout(r, 140));
  assert.strictEqual(pollRt.broadcastActive, false, "Returning to idle must clear broadcastActive");
  assert.strictEqual(pollRt._lastFrameSeq, 0, "Returning to idle must reset _lastFrameSeq");
  assert.strictEqual(pollRt.frameInfo, "", "Returning to idle must clear frameInfo");
  sock.close();

  console.log("8. Testing FFT CCoeffNormed numerical parity (< 1e-5) vs spatial baseline...");
  {
    // Non-power-of-two grayscale image with patterned card-like features and flat border
    const srcW = 97, srcH = 61;
    const srcGray = Engine.createImage(srcW, srcH, 1, 0);
    let seed = 1337;
    const randByte = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return (seed >>> 16) & 255;
    };
    for (let y = 0; y < srcH; y++) {
      for (let x = 0; x < srcW; x++) {
        srcGray.data[y * srcW + x] = (y < 10 && x < 10) ? 128 : randByte();
      }
    }
    const tplW = 19, tplH = 15;
    const tplGray = Engine.createImage(tplW, tplH, 1, 0);
    for (let y = 0; y < tplH; y++) {
      for (let x = 0; x < tplW; x++) {
        tplGray.data[y * tplW + x] = srcGray.data[(22 + y) * srcW + (41 + x)];
      }
    }
    // Reference exact double-precision scalar CCoeffNormed
    let tSum = 0;
    const nTpl = tplW * tplH;
    for (let i = 0; i < nTpl; i++) tSum += tplGray.data[i];
    const tMean = tSum / nTpl;
    const tZero = new Float32Array(nTpl);
    let tSq = 0;
    for (let i = 0; i < nTpl; i++) {
      const d = tplGray.data[i] - tMean;
      tZero[i] = d;
      tSq += d * d;
    }
    const tNorm = Math.sqrt(tSq);

    const prepGray = Engine.prepareSourceForMatch(srcGray, false);
    const fftRes = Engine.matchPreparedTemplate(prepGray, tplGray, tZero, tNorm);
    const scalarRes = Engine.matchTemplateCCoeffNormed(srcGray, tplGray);
    assert.strictEqual(fftRes.w, scalarRes.w);
    assert.strictEqual(fftRes.h, scalarRes.h);
    let maxErr = 0;
    for (let y = 0; y < scalarRes.h; y++) {
      for (let x = 0; x < scalarRes.w; x++) {
        const idx = y * scalarRes.w + x;
        let pSum = 0;
        for (let ty = 0; ty < tplH; ty++) {
          for (let tx = 0; tx < tplW; tx++) {
            pSum += srcGray.data[(y + ty) * srcW + (x + tx)];
          }
        }
        const pMean = pSum / nTpl;
        let num = 0, pVar = 0;
        for (let ty = 0; ty < tplH; ty++) {
          for (let tx = 0; tx < tplW; tx++) {
            const pd = srcGray.data[(y + ty) * srcW + (x + tx)] - pMean;
            const td = tZero[ty * tplW + tx];
            num += pd * td;
            pVar += pd * pd;
          }
        }
        const ref = (tNorm > 1e-6 && pVar > 1e-6) ? Math.max(-1, Math.min(1, num / (tNorm * Math.sqrt(pVar)))) : 0;
        maxErr = Math.max(maxErr, Math.abs(fftRes.data[idx] - ref), Math.abs(scalarRes.data[idx] - ref));
      }
    }
    console.log(`   Max FFT/Spatial CCoeffNormed error vs exact scalar: ${maxErr.toExponential(2)}`);
    assert(maxErr < 1e-5, `Expected CCoeffNormed maxErr < 1e-5, got ${maxErr}`);
    assert(fftRes.maxVal > 0.999 && fftRes.maxX === 41 && fftRes.maxY === 22, "Exact template match location mismatch");

    // Flat template guard + oversized template guard
    const flatTpl = Engine.createImage(5, 5, 1, 77);
    const flatMatch = Engine.matchTemplateCCoeffNormed(srcGray, flatTpl);
    assert.strictEqual(flatMatch.maxVal, 0);
    const bigTpl = Engine.createImage(200, 200, 1, 50);
    const bigMatch = Engine.matchTemplateCCoeffNormed(srcGray, bigTpl);
    assert.strictEqual(bigMatch.maxVal, -1);

    // Small-work spatial-integral path check (work <= 12000)
    const smSrc = Engine.cropImage(srcGray, 10, 10, 23, 17);
    const smTpl = Engine.cropImage(srcGray, 15, 13, 6, 5);
    const smPrep = Engine.prepareSourceForMatch(smSrc, false);
    const { tZero: smZ, tNorm: smN } = (() => {
      let s = 0;
      const n = smTpl.w * smTpl.h;
      for (let i = 0; i < n; i++) s += smTpl.data[i];
      const m = s / n;
      const z = new Float32Array(n);
      let q = 0;
      for (let i = 0; i < n; i++) {
        const d = smTpl.data[i] - m;
        z[i] = d;
        q += d * d;
      }
      return { tZero: z, tNorm: Math.sqrt(q) };
    })();
    const smRes = Engine.matchPreparedTemplate(smPrep, smTpl, smZ, smN);
    assert(smRes.maxVal > 0.999 && smRes.maxX === 5 && smRes.maxY === 3);
  }

  console.log("9. Testing rotateImage (0/90/180/270) & calib_set_rotation persistence...");
  {
    const img = Engine.createImage(5, 3, 3, 0);
    for (let y = 0; y < 3; y++) {
      for (let x = 0; x < 5; x++) {
        const p = (y * 5 + x) * 3;
        img.data[p] = x;
        img.data[p + 1] = y;
        img.data[p + 2] = y * 10 + x;
      }
    }
    const r0 = Engine.rotateImage(img, 0);
    assert.strictEqual(r0.w, 5);
    assert.strictEqual(r0.h, 3);
    const r90 = Engine.rotateImage(img, 90);
    assert.strictEqual(r90.w, 3);
    assert.strictEqual(r90.h, 5);
    // Original top-left (0, 0) moves to top-right (w'=2, y'=0) on 90° CW
    assert.strictEqual(r90.data[(0 * 3 + 2) * 3 + 2], 0);
    // Original bottom-left (0, 2) moves to top-left (x'=0, y'=0) on 90° CW
    assert.strictEqual(r90.data[(0 * 3 + 0) * 3 + 2], 20);
    // Four 90° CW rotations equal identity
    const r360 = Engine.rotateImage(Engine.rotateImage(Engine.rotateImage(r90, 90), 90), 90);
    assert.strictEqual(r360.w, 5);
    assert.strictEqual(r360.h, 3);
    assert.deepStrictEqual(Array.from(r360.data), Array.from(img.data));

    // Persistence + headless snapRgba output
    memStore.delete("bjPlaytechTracker.ipadCompanionPack.v1");
    const rotRt = new Engine.IpadCompanionRuntime({ initialPack: pack, isNativeHost: false });
    const frameW = 240, frameH = 180;
    const rawFrame = Engine.createImage(frameW, frameH, 3, 30);
    fillRect(rawFrame, 10, 10, 60, 40, 200, 180, 160);
    rotRt.ingestFrameBgr(rawFrame, 50.0);

    let rotSnap = null;
    const rotClient = {
      readyState: 1,
      onRawMessage(obj) { rotSnap = obj; },
      onmessage({ data }) { rotSnap = JSON.parse(data); },
    };
    rotRt.clients.add(rotClient);
    rotRt.handleClientMessage(rotClient, JSON.stringify({ type: "calib_set_rotation", rotation: 90, anchorMode: "window" }));
    assert(rotSnap && rotSnap.type === "calib_snapshot", "calib_set_rotation must emit rotated calib_snapshot");
    assert.strictEqual(rotSnap.frameRotation, 90);
    assert.strictEqual(rotSnap.width, frameH, "90° CW rotation must swap width and height");
    assert.strictEqual(rotSnap.height, frameW);
    assert(rotSnap.snapRgba instanceof Uint8Array && rotSnap.snapRgba.length === frameH * frameW * 4, "Headless calib_grab must emit snapRgba");

    // Rotate back 270 -> preserves clean unrotated dimensions
    rotRt.handleClientMessage(rotClient, JSON.stringify({ type: "calib_set_rotation", rotation: 270, anchorMode: "window" }));
    assert.strictEqual(rotRt._unrotatedRawFrame.w, frameW, "_unrotatedRawFrame must stay unrotated");
    assert.strictEqual(rotSnap.width, frameH);
    rotRt.handleClientMessage(rotClient, JSON.stringify({ type: "calib_set_rotation", rotation: 90, anchorMode: "window" }));

    // Fresh runtime loads saved rotation from localStorage
    const restoredRt = new Engine.IpadCompanionRuntime({ isNativeHost: false });
    assert.strictEqual(restoredRt.cfg.frame_rotation, 90, "New runtime must restore persisted frame_rotation");
    memStore.delete("bjPlaytechTracker.ipadCompanionPack.v1");
  }

  console.log("10. Testing ViewportSmoother & PlaytechDetector ROI caches...");
  {
    const smth = new Engine.ViewportSmoother(0.35, 18);
    smth.resolveAnchor(fullScreen, "ipad_split_left");
    assert.strictEqual(smth._framesSinceDetect, 0);
    smth.resolveAnchor(fullScreen, "ipad_split_left");
    assert.strictEqual(smth._framesSinceDetect, 1, "Second resolveAnchor must reuse steady cached rect");
    smth.reset();
    assert.strictEqual(smth._rect, null);
    assert.strictEqual(smth._cachedRect, null);
    assert.strictEqual(smth._cachedArgs, null);
    assert.strictEqual(smth._framesSinceDetect, 0);

    const cachedDet = new Engine.PlaytechDetector(cfg, pack);
    const dummyImg = Engine.createImage(160, 40, 3, 20);
    const r1 = cachedDet.processCrops({
      playerImg: hudStrip,
      dealerImg: dummyImg,
      shuffleImg: dummyImg,
      bannerImg: dummyImg,
      placeBetsImg: dummyImg,
      ts: 10.0,
    });
    assert.strictEqual(r1.player_cards.length, 2);
    // Spy on detectHudStrip to ensure static frame hits cache instead of rescanning
    let hudScanCalls = 0;
    const origDetectHud = cachedDet.detectHudStrip.bind(cachedDet);
    cachedDet.detectHudStrip = (...args) => { hudScanCalls++; return origDetectHud(...args); };

    const r2 = cachedDet.processCrops({
      playerImg: hudStrip,
      dealerImg: dummyImg,
      shuffleImg: dummyImg,
      bannerImg: dummyImg,
      placeBetsImg: dummyImg,
      ts: 10.2,
    });
    assert.strictEqual(hudScanCalls, 0, "Identical ROI frames must hit signature cache without calling detectHudStrip");
    assert.strictEqual(r2.player_cards.length, 2);

    // Changing HUD strip (clearing cards) must invalidate signature cache immediately
    const r3 = cachedDet.processCrops({
      playerImg: dummyImg,
      dealerImg: dummyImg,
      shuffleImg: dummyImg,
      bannerImg: dummyImg,
      placeBetsImg: dummyImg,
      ts: 10.4,
    });
    assert.strictEqual(hudScanCalls, 1, "Changing ROI content must bypass cache and rescan immediately");
    assert.strictEqual(r3.player_cards.length, 0);
  }

  console.log("11. Testing WorkerCompanionSocket fallback + mock Worker zero-copy snapshot...");
  {
    // Node has no global Worker -> promotes cleanly to IpadCompanionSocket
    const fbRt = new Engine.IpadCompanionRuntime({ initialPack: pack, isNativeHost: false });
    const fbSock = new Engine.WorkerCompanionSocket({ fallbackRuntime: fbRt, isNativeHost: false });
    const fbMsgs = [];
    fbSock.onmessage = ({ data }) => fbMsgs.push(JSON.parse(data));
    await new Promise((r) => setTimeout(r, 40));
    assert.strictEqual(fbSock.readyState, 1);
    assert.strictEqual(fbSock.fallbackReason, "worker_unavailable");
    assert(fbMsgs.some((m) => m.type === "status"));
    globalThis.__ipadCompanionSetHostInfo({ broadcastExtPresent: true, appGroupAvailable: false });
    globalThis.__ipadCompanionSetExtConnected(true);
    assert.strictEqual(fbRt.extConnected, true);
    fbSock.close();

    // Mock Worker + mock canvas toDataURL -> verifies worker_ready, snapshot reconstruction, and xhr_blocked fallback
    let activeMockWorker = null;
    class MockWorker {
      constructor(url) {
        this.url = url;
        this.posted = [];
        this.terminated = false;
        activeMockWorker = this;
      }
      postMessage(msg) {
        this.posted.push(msg);
        if (msg.type === "init") {
          setTimeout(() => {
            if (this.terminated || !this.onmessage) return;
            this.onmessage({
              data: {
                type: "worker_ready",
                status: { type: "status", connected: true, windowFound: false, paused: false, state: "IDLE", ipadMode: true, frameRotation: 0 },
              },
            });
          }, 10);
        }
      }
      terminate() {
        this.terminated = true;
      }
    }
    const prevDoc = global.document;
    global.document = {
      createElement(tag) {
        assert.strictEqual(tag, "canvas");
        return {
          width: 0,
          height: 0,
          getContext() {
            return {
              createImageData: (w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }),
              putImageData() {},
            };
          },
          toDataURL: () => "data:image/jpeg;base64,MOCKSNAP",
        };
      },
    };
    const wSock = new Engine.WorkerCompanionSocket({
      WorkerClass: MockWorker,
      workerUrl: "companion_worker.js",
      isNativeHost: true,
    });
    const wMsgs = [];
    wSock.onmessage = ({ data }) => wMsgs.push(JSON.parse(data));
    await new Promise((r) => setTimeout(r, 30));
    assert.strictEqual(wSock.readyState, 1, "WorkerCompanionSocket should open after worker_ready");
    assert(wMsgs.some((m) => m.type === "status"));

    wSock.send(JSON.stringify({ type: "sync_state", playMode: "bet" }));
    assert(activeMockWorker.posted.some((m) => m.type === "ws_send"));

    activeMockWorker.onmessage({
      data: {
        type: "ws_message_snapshot",
        payload: { type: "calib_snapshot", width: 4, height: 3, anchorMode: "window", rois: {}, templates: {}, snapW: 4, snapH: 3 },
        snapRgba: new Uint8Array(4 * 3 * 4),
      },
    });
    const wSnap = wMsgs.find((m) => m.type === "calib_snapshot");
    assert(wSnap && wSnap.imageDataUrl === "data:image/jpeg;base64,MOCKSNAP", "Main thread must decode ws_message_snapshot into imageDataUrl");

    // Trigger worker_fallback ("xhr_blocked") and verify seamless replay to fallback runtime
    const fbFromWorkerRt = new Engine.IpadCompanionRuntime({ initialPack: pack, isNativeHost: false });
    wSock.options.fallbackRuntime = fbFromWorkerRt;
    globalThis.__ipadCompanionSetExtConnected(true);
    activeMockWorker.onmessage({ data: { type: "worker_fallback", reason: "xhr_blocked" } });
    await new Promise((r) => setTimeout(r, 35));
    assert.strictEqual(wSock.fallbackReason, "xhr_blocked");
    assert.strictEqual(fbFromWorkerRt.extConnected, true, "Fallback promotion must replay extConnected");
    assert.strictEqual(fbFromWorkerRt.sm.sync_play_mode, "bet", "Fallback promotion must replay sync_state");

    wSock.close();
    global.document = prevDoc;
  }

  console.log("12. Testing 1-frame final dealer card & hole-card wipe rescue, DEALER_INCOMPLETE & shuffle_active...");
  {
    const mkDet = (rank, val, conf = 0.92, suit = "S", x = 10) => ({
      rank, val, conf, suit, x, y: 10, w: 14, h: 16, hand_index: 0,
    });

    // 12a. 1-frame final dealer draw card ("2D") before PLACE_BETS wipe is rescued
    const sm12a = new Engine.CompanionStateMachine(cfg);
    sm12a.step({ ts: 1.0, banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
    const p98 = [mkDet("9", 9, 0.94, "S", 10), mkDet("8", 8, 0.94, "H", 30)];
    sm12a.step({ ts: 1.1, banner_state: "WAIT", banner_conf: 0.95, player_cards: p98, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("BACK", null, 0.92, null, 30)], player_hands: { 0: p98 } });
    sm12a.step({ ts: 1.2, banner_state: "WAIT", banner_conf: 0.95, player_cards: p98, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("BACK", null, 0.92, null, 30)], player_hands: { 0: p98 } });
    sm12a.step({ ts: 1.3, banner_state: "WAIT", banner_conf: 0.95, player_cards: p98, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("6", 6, 0.93, "H", 30)], player_hands: { 0: p98 } });
    sm12a.step({ ts: 1.4, banner_state: "WAIT", banner_conf: 0.95, player_cards: p98, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("6", 6, 0.93, "H", 30)], player_hands: { 0: p98 } });
    // Final "2D" visible for ONLY 1 frame, then brief 1-frame partial drop of "2D" before wipe
    const s15 = sm12a.step({ ts: 1.5, banner_state: "WAIT", banner_conf: 0.95, player_cards: p98, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("6", 6, 0.93, "H", 30), mkDet("2", 2, 0.91, "D", 50)], player_hands: { 0: p98 } });
    sm12a.step({ ts: 1.6, banner_state: "WAIT", banner_conf: 0.95, player_cards: p98, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("6", 6, 0.93, "H", 30)], player_hands: { 0: p98 } });
    const pb12a = sm12a.step({ ts: 1.7, banner_state: "PLACE_BETS", banner_conf: 0.94, player_cards: [], dealer_cards: [], player_hands: {} });
    const rescued2 = [...s15, ...pb12a].find((e) => e.type === "card" && e.target === "D" && e.rankLabel === "2");
    assert(rescued2, "Final 1-frame dealer card '2' must be recorded (instant or rescued on wipe)");
    const nmb12a = sm12a.step({ ts: 1.8, banner_state: "NO_MORE_BETS", banner_conf: 0.94, player_cards: [], dealer_cards: [], player_hands: {} });
    assert(nmb12a.some((e) => e.type === "auto_watch"), "Rescued 18 dealer total should auto_watch cleanly");

    // 12b. 1-frame hole-card reveal ("7C") over BACK right before wipe is rescued
    const sm12b = new Engine.CompanionStateMachine(cfg);
    const p19 = [mkDet("10", 0, 0.95, "S", 10), mkDet("9", 9, 0.95, "H", 30)];
    sm12b.step({ ts: 2.0, banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
    sm12b.step({ ts: 2.1, banner_state: "WAIT", banner_conf: 0.95, player_cards: p19, dealer_cards: [mkDet("10", 0, 0.94, "D", 10), mkDet("BACK", null, 0.92, null, 30)], player_hands: { 0: p19 } });
    sm12b.step({ ts: 2.2, banner_state: "WAIT", banner_conf: 0.95, player_cards: p19, dealer_cards: [mkDet("10", 0, 0.94, "D", 10), mkDet("BACK", null, 0.92, null, 30)], player_hands: { 0: p19 } });
    const s23 = sm12b.step({ ts: 2.3, banner_state: "", banner_conf: 0, player_cards: p19, dealer_cards: [mkDet("10", 0, 0.94, "D", 10), mkDet("7", 7, 0.90, "C", 30)], player_hands: { 0: p19 } });
    const pb12b = sm12b.step({ ts: 2.4, banner_state: "PLACE_BETS", banner_conf: 0.94, player_cards: [], dealer_cards: [], player_hands: {} });
    assert([...s23, ...pb12b].some((e) => e.type === "card" && e.target === "D" && e.rankLabel === "7" && e.slot === 1), "1-frame hole card '7' replacing BACK must be recorded (instant or rescued on wipe)");
    const nmb12b = sm12b.step({ ts: 2.5, banner_state: "NO_MORE_BETS", banner_conf: 0.94, player_cards: [], dealer_cards: [], player_hands: {} });
    assert(nmb12b.some((e) => e.type === "auto_watch"), "Rescued hole card reaching 17 should emit auto_watch");

    // 12c. Incomplete dealer hand (10+9 player vs 10+6 dealer) must flag DEALER_INCOMPLETE
    const sm12c = new Engine.CompanionStateMachine(cfg);
    sm12c.step({ ts: 3.0, banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
    sm12c.step({ ts: 3.1, banner_state: "WAIT", banner_conf: 0.95, player_cards: p19, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("6", 6, 0.93, "H", 30)], player_hands: { 0: p19 } });
    sm12c.step({ ts: 3.2, banner_state: "WAIT", banner_conf: 0.95, player_cards: p19, dealer_cards: [mkDet("10", 0, 0.94, "S", 10), mkDet("6", 6, 0.93, "H", 30)], player_hands: { 0: p19 } });
    const pb12c = sm12c.step({ ts: 3.3, banner_state: "PLACE_BETS", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
    const incPrompt = pb12c.find((e) => e.type === "prompt_confirm");
    assert(incPrompt && incPrompt.reason === "DEALER_INCOMPLETE", `Expected DEALER_INCOMPLETE prompt, got ${JSON.stringify(incPrompt)}`);
    assert.strictEqual(incPrompt.suggestedFocus, "D");

    // 12d. Non-natural player with single dealer card (["10"] only) also flags DEALER_INCOMPLETE (unlike natural BJ in Test 4)
    const sm12d = new Engine.CompanionStateMachine(cfg);
    sm12d.step({ ts: 4.0, banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
    sm12d.step({ ts: 4.1, banner_state: "WAIT", banner_conf: 0.95, player_cards: p19, dealer_cards: [mkDet("10", 0, 0.94, "S", 10)], player_hands: { 0: p19 } });
    sm12d.step({ ts: 4.2, banner_state: "WAIT", banner_conf: 0.95, player_cards: p19, dealer_cards: [mkDet("10", 0, 0.94, "S", 10)], player_hands: { 0: p19 } });
    const pb12d = sm12d.step({ ts: 4.3, banner_state: "PLACE_BETS", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
    const incPrompt1 = pb12d.find((e) => e.type === "prompt_confirm");
    assert(incPrompt1 && incPrompt1.reason === "DEALER_INCOMPLETE", "Single dealer card with non-natural player must flag DEALER_INCOMPLETE");

    // 12e. shuffle_active hold duration + new_shoe emission + exit back to IDLE
    const smShuf = new Engine.CompanionStateMachine(cfg);
    const evShuf0 = smShuf.step({ ts: 100.0, shuffle_active: true, player_cards: [], dealer_cards: [] });
    assert.strictEqual(evShuf0.length, 0);
    const evShuf1 = smShuf.step({ ts: 100.5, shuffle_active: true, player_cards: [], dealer_cards: [] });
    assert.strictEqual(evShuf1.length, 0);
    const evShuf2 = smShuf.step({ ts: 100.0 + cfg.shuffle_hold_sec + 0.1, shuffle_active: true, player_cards: [], dealer_cards: [] });
    assert(evShuf2.some((e) => e.type === "new_shoe" && e.reason === "cut"), "Held shuffle_active must emit new_shoe");
    assert.strictEqual(smShuf.state, "SHUFFLING");
    const evShuf3 = smShuf.step({ ts: 103.0, shuffle_active: true, player_cards: [], dealer_cards: [] });
    assert.strictEqual(evShuf3.length, 0, "new_shoe must not re-emit during same shuffle");
    smShuf.step({ ts: 104.0, shuffle_active: false, player_cards: [], dealer_cards: [] });
    assert.strictEqual(smShuf.state, "IDLE");
  }

  console.log("13. Testing thin single-digit dealer rank HUD signature invalidation & immediate rescan...");
  {
    const tpl7 = detector.banks.hud.find((t) => t.rank === "7") || detector.banks.hud.find((t) => t.rank === "A");
    const tpl10 = detector.banks.hud.find((t) => t.rank === "10") || tplK;
    assert(tpl7 && tpl10, "Must have '7' (or 'A') and '10' HUD templates");

    // Large upscaled 480x140 dealer ROI with card 0 + a thin blank white card 1 frame
    const dealerBefore = Engine.createImage(480, 140, 3, 25);
    fillRect(dealerBefore, 40, 24, 64, 90, 248, 248, 248);
    fillRect(dealerBefore, 112, 24, 64, 90, 248, 248, 248);
    // Draw dark ink for 10 in card 0
    for (let y = 0; y < tpl10.bgr.h; y++) {
      for (let x = 0; x < tpl10.bgr.w; x++) {
        if (tpl10.min_gray.data[y * tpl10.bgr.w + x] < 180) {
          fillRect(dealerBefore, 46 + x * 2, 28 + y * 2, 2, 2, 15, 15, 15);
        }
      }
    }
    // Clone and draw thin single-digit '7' inside card 1 ONLY
    const dealerAfter = Engine.createImage(480, 140, 3, 25);
    dealerAfter.data.set(dealerBefore.data);
    for (let y = 0; y < tpl7.bgr.h; y++) {
      for (let x = 0; x < tpl7.bgr.w; x++) {
        if (tpl7.min_gray.data[y * tpl7.bgr.w + x] < 180) {
          const px = 118 + x;
          const py = 28 + y;
          const idx = (py * dealerAfter.w + px) * 3;
          dealerAfter.data[idx] = 15;
          dealerAfter.data[idx + 1] = 15;
          dealerAfter.data[idx + 2] = 15;
        }
      }
    }

    const sigBefore = Engine.computeRoiSignature(dealerBefore, 80, 36, true);
    const sigAfter = Engine.computeRoiSignature(dealerAfter, 80, 36, true);
    assert.strictEqual(Engine.hudRoiSigChanged(sigBefore, sigBefore, 1), false, "Identical dealer sig must not flag changed");
    assert.strictEqual(Engine.hudRoiSigChanged(sigBefore, sigAfter, 1), true, "Adding thin single-digit glyph must flag hudRoiSigChanged === true");

    // Also test full processCrops cache miss on real-sized 240x70 dealer_strip with new card + slim digit
    const dStrip1 = Engine.createImage(240, 70, 3, 25);
    drawHudCard(dStrip1, 20, 12, tpl10, suitS, false);
    const dStrip2 = Engine.createImage(240, 70, 3, 25);
    dStrip2.data.set(dStrip1.data);
    drawHudCard(dStrip2, 56, 12, tpl7, suitH, true);

    const det13 = new Engine.PlaytechDetector(cfg, pack);
    const dummyRoi = Engine.createImage(160, 40, 3, 20);
    const res13a = det13.processCrops({ playerImg: dummyRoi, dealerImg: dStrip1, shuffleImg: dummyRoi, bannerImg: dummyRoi, placeBetsImg: dummyRoi, ts: 50.0 });
    assert.strictEqual(res13a.dealer_cards.length, 1);

    let dScans = 0;
    const origDetect13 = det13.detectHudStrip.bind(det13);
    det13.detectHudStrip = (img, isDealer) => { if (isDealer) dScans++; return origDetect13(img, isDealer); };

    const res13b = det13.processCrops({ playerImg: dummyRoi, dealerImg: dStrip1, shuffleImg: dummyRoi, bannerImg: dummyRoi, placeBetsImg: dummyRoi, ts: 50.1 });
    assert.strictEqual(dScans, 0, "Identical dealer frame must hit signature cache with 0 detectHudStrip calls");
    assert.strictEqual(res13b.dealer_cards.length, 1);

    const res13c = det13.processCrops({ playerImg: dummyRoi, dealerImg: dStrip2, shuffleImg: dummyRoi, bannerImg: dummyRoi, placeBetsImg: dummyRoi, ts: 50.2 });
    assert.strictEqual(dScans, 1, "Adding thin single-digit dealer card must invalidate cache and rescan immediately");
    assert.strictEqual(res13c.dealer_cards.length, 2);
    assert.strictEqual(res13c.dealer_cards[1].rank, tpl7.rank);
  }

  console.log("14. Testing cached template-spectrum matchFFTPacked parity across cold & warm hits (< 1e-5)...");
  {
    const mkRandGray = (w, h, s0) => {
      const im = Engine.createImage(w, h, 1, 0);
      let s = s0 >>> 0;
      for (let i = 0; i < w * h; i++) {
        s = (s * 1664525 + 1013904223) >>> 0;
        im.data[i] = (s >>> 16) & 255;
      }
      return im;
    };
    const srcA = mkRandGray(131, 89, 424242);
    const srcB = mkRandGray(131, 89, 987654); // Same Fx,Fy -> tests warm spectrum cache hit on different source
    const srcC = mkRandGray(195, 73, 135791); // Different Fx,Fy -> ensures separate cache key works cleanly
    const tpl14 = Engine.cropImage(srcA, 53, 29, 21, 17);
    // Stamp same patch inside srcC at (88, 31)
    for (let y = 0; y < tpl14.h; y++) {
      for (let x = 0; x < tpl14.w; x++) {
        srcC.data[(31 + y) * srcC.w + (88 + x)] = tpl14.data[y * tpl14.w + x];
      }
    }

    const n = tpl14.w * tpl14.h;
    let sumT = 0;
    for (let i = 0; i < n; i++) sumT += tpl14.data[i];
    const meanT = sumT / n;
    const tZero = new Float32Array(n);
    let sqT = 0;
    for (let i = 0; i < n; i++) {
      const d = tpl14.data[i] - meanT;
      tZero[i] = d;
      sqT += d * d;
    }
    const tNorm = Math.sqrt(sqT);

    // Run 1 (cold spectrum build) -> Run 2 (warm cache same src) -> Run 3 (warm cache new srcB, same Fx/Fy) -> Run 4 (srcC diff Fx/Fy) -> Run 5 (warm srcC)
    const prepA = Engine.prepareSourceForMatch(srcA, false);
    const rCold = Engine.matchPreparedTemplate(prepA, tpl14, tZero, tNorm);
    const rWarmSame = Engine.matchPreparedTemplate(prepA, tpl14, tZero, tNorm);
    const prepB = Engine.prepareSourceForMatch(srcB, false);
    const rWarmNewSrc = Engine.matchPreparedTemplate(prepB, tpl14, tZero, tNorm);
    const prepC = Engine.prepareSourceForMatch(srcC, false);
    const rDiffKey1 = Engine.matchPreparedTemplate(prepC, tpl14, tZero, tNorm);
    const rDiffKey2 = Engine.matchPreparedTemplate(prepC, tpl14, tZero, tNorm);

    const spatA = Engine.matchTemplateCCoeffNormed(srcA, tpl14);
    const spatB = Engine.matchTemplateCCoeffNormed(srcB, tpl14);
    const spatC = Engine.matchTemplateCCoeffNormed(srcC, tpl14);

    assert.strictEqual(rCold.maxX, 53);
    assert.strictEqual(rCold.maxY, 29);
    assert.strictEqual(rWarmSame.maxX, 53);
    assert.strictEqual(rDiffKey1.maxX, 88);
    assert.strictEqual(rDiffKey1.maxY, 31);

    let maxCacheErr = 0;
    for (let i = 0; i < rCold.data.length; i++) {
      maxCacheErr = Math.max(maxCacheErr, Math.abs(rCold.data[i] - spatA.data[i]), Math.abs(rWarmSame.data[i] - spatA.data[i]));
    }
    for (let i = 0; i < rWarmNewSrc.data.length; i++) {
      maxCacheErr = Math.max(maxCacheErr, Math.abs(rWarmNewSrc.data[i] - spatB.data[i]));
    }
    for (let i = 0; i < rDiffKey1.data.length; i++) {
      maxCacheErr = Math.max(maxCacheErr, Math.abs(rDiffKey1.data[i] - spatC.data[i]), Math.abs(rDiffKey2.data[i] - spatC.data[i]));
    }
    console.log(`   Cached FFT vs spatial max error: ${maxCacheErr.toExponential(2)}`);
    assert(maxCacheErr < 1e-5, `Cached FFT parity error ${maxCacheErr} exceeded 1e-5`);
  }

  console.log("15. Testing extDiag, test_reload, request_ext_status, triggerSafariReload & WorkerCompanionSocket set_ext_status...");
  {
    const nativeActions = [];
    const extRt = new Engine.IpadCompanionRuntime({
      initialPack: pack,
      isNativeHost: false,
      installGlobalHooks: true,
      onNativeAction: (p) => nativeActions.push(p),
    });
    const clientEvts = [];
    const client = {
      readyState: 1,
      onRawMessage: (m) => clientEvts.push(m),
      onmessage: ({ data }) => clientEvts.push(JSON.parse(data)),
    };
    extRt.clients.add(client);

    // safariExtPresent === false -> blocked with safari_ext_missing
    globalThis.__ipadCompanionSetHostInfo({ safariExtPresent: false, nativeRelayAvailable: true });
    assert.deepStrictEqual(extRt.triggerSafariReload(), [false, "safari_ext_missing"]);

    // safariExtPresent === true, permissionOk === false -> missing_tab_permissions
    globalThis.__ipadCompanionSetHostInfo({ safariExtPresent: true, nativeRelayAvailable: true });
    globalThis.__ipadCompanionSetExtStatus({ connected: true, permissionOk: false, tabCount: 3, matchedCount: 0 });
    assert.deepStrictEqual(extRt.triggerSafariReload(), [false, "missing_tab_permissions"]);
    assert.strictEqual(extRt.buildStatusPayload().extStatus.tabCount, 3);

    // Queued delivery when relay available & extension currently sleeping (connected: false)
    globalThis.__ipadCompanionSetExtStatus({ connected: false, permissionOk: true, tabCount: 2, matchedCount: 1 });
    assert.strictEqual(extRt.extConnected, false);
    const [qOk, qMethod] = extRt.triggerSafariReload({ allowActiveFallback: true, testImmediate: true });
    assert.strictEqual(qOk, true);
    assert.strictEqual(qMethod, "safari_ext_queued");

    // Live connected extension + test_reload + request_ext_status via handleClientMessage
    globalThis.__ipadCompanionSetExtStatus({ connected: true, permissionOk: true, tabCount: 4, matchedCount: 2 });
    assert.strictEqual(extRt.extConnected, true);
    extRt.handleClientMessage(client, JSON.stringify({ type: "keepalive_action", action: "test_reload" }));
    const reloadAction = nativeActions.filter((a) => a.action === "reloadLeftSafari").pop();
    assert(reloadAction && reloadAction.testImmediate === true, "test_reload must post reloadLeftSafari with testImmediate: true");
    assert(clientEvts.some((e) => e.type === "keepalive_status" && e.event === "test_reload_sent" && e.method === "safari_ext_reload"), "Must broadcast test_reload_sent event");

    extRt.handleClientMessage(client, JSON.stringify({ type: "keepalive_action", action: "request_ext_status", keywords: ["newport"] }));
    const diagReq = nativeActions.find((a) => a.action === "requestExtStatus");
    assert(diagReq && Array.isArray(diagReq.keywords) && diagReq.keywords[0] === "newport", "request_ext_status must post requestExtStatus");

    // WorkerCompanionSocket set_ext_status replay on worker_ready + post after ready + fallback replay
    let mockW2 = null;
    class MockWorker2 {
      constructor() {
        this.posted = [];
        this.terminated = false;
        mockW2 = this;
      }
      postMessage(msg) {
        this.posted.push(msg);
        if (msg.type === "init") {
          setTimeout(() => {
            if (!this.terminated && this.onmessage) {
              this.onmessage({
                data: { type: "worker_ready", status: { type: "status", connected: true, ipadMode: true } },
              });
            }
          }, 10);
        }
      }
      terminate() { this.terminated = true; }
    }
    const wSock2 = new Engine.WorkerCompanionSocket({ WorkerClass: MockWorker2, workerUrl: "companion_worker.js", isNativeHost: true });
    // Buffered BEFORE worker_ready
    globalThis.__ipadCompanionSetExtStatus({ connected: true, permissionOk: true, tabCount: 4, matchedCount: 1 });
    await new Promise((r) => setTimeout(r, 30));
    const readyExtReplay = mockW2.posted.find((m) => m.type === "set_ext_status");
    assert(readyExtReplay && readyExtReplay.status.matchedCount === 1, "WorkerCompanionSocket must replay buffered set_ext_status on worker_ready");

    // Forwarded live AFTER worker_ready
    globalThis.__ipadCompanionSetExtStatus({ tabCount: 5, lastAck: { ok: true, matchedBy: "keyword", ts: 1234 } });
    const liveExtPush = mockW2.posted.filter((m) => m.type === "set_ext_status").pop();
    assert.strictEqual(liveExtPush.status.tabCount, 5);
    assert.strictEqual(liveExtPush.status.lastAck.ok, true);

    // Replayed on fallback promotion
    const fbRt2 = new Engine.IpadCompanionRuntime({ initialPack: pack, isNativeHost: false });
    wSock2.options.fallbackRuntime = fbRt2;
    mockW2.onmessage({ data: { type: "worker_fallback", reason: "xhr_blocked" } });
    await new Promise((r) => setTimeout(r, 35));
    assert.strictEqual(fbRt2.extConnected, true);
    assert.strictEqual(fbRt2.extDiag.tabCount, 5);
    assert.strictEqual(fbRt2.extDiag.lastAck.ok, true);
    wSock2.close();
  }

  console.log("   All iPad JS CV, Rotation, Cache, Worker & Loopback Polling checks PASSED!");

// Test 16: red face-up dealer cards (hearts/diamonds) abutting the upcard must not be mistaken for a face-down BACK
{
  console.log("16. Testing dealer red face-up 2nd/3rd card is not misread as BACK...");
  const det16 = new Engine.PlaytechDetector(cfg, pack);
  const hudT = (r) => det16.banks.hud.find((t) => t.rank === r);
  const suitT = (r) => det16.banks.suit.find((t) => t.rank === r);
  const glyph16 = (strip, tpl, ox, oy, red) => {
    for (let y = 0; y < tpl.bgr.h; y++) for (let x = 0; x < tpl.bgr.w; x++) {
      if (tpl.min_gray.data[y * tpl.bgr.w + x] < 180 && ox + x < strip.w) {
        const i = ((oy + y) * strip.w + ox + x) * 3;
        strip.data[i] = red ? 25 : 15; strip.data[i + 1] = red ? 25 : 15; strip.data[i + 2] = red ? 215 : 15;
      }
    }
  };
  const card16 = (strip, x0, rank, suit, red) => {
    fillRect(strip, x0, 12, 32, 45, 248, 248, 248);
    glyph16(strip, hudT(rank), x0 + 3, 14, red);
    glyph16(strip, suitT(suit), x0 + 3, 40, red);
  };
  for (const step of [36, 34, 30, 26, 22]) {
    const strip = Engine.createImage(240, 70, 3, 25);
    card16(strip, 12, "A", "S", false);
    card16(strip, 12 + step, "K", "H", true);
    card16(strip, 12 + 2 * step, "5", "D" in {} ? "D" : "H", true);
    det16.invalidateCaches();
    const [d16] = det16.detectHudStrip(strip, true);
    assert.deepStrictEqual(d16.map((c) => c.rank), ["A", "K", "5"], `step ${step}: got ${d16.map((c) => c.rank)}`);
  }
  console.log("   Test 16 PASSED");
}

// Test 17: Split-hand reliability (debounce, overlap guard, re-anchor, seed lock, auto-watch)
{
  console.log("17. Testing split-hand debounce, overlap guard, re-anchor, seed lock & auto-watch...");
  const sm17 = new Engine.CompanionStateMachine(cfg);
  const mkC = (rank, val, conf = 0.93, suit = "S", x = 10) => ({ rank, val, conf, suit, x, y: 10, w: 12, h: 16, hand_index: 0 });
  const c8a = mkC("8", 8, 0.94, "S", 20);
  const c8b = mkC("8", 8, 0.94, "H", 40);
  const c5 = mkC("5", 5, 0.93, "C", 55);
  const c3 = mkC("3", 3, 0.92, "D", 140);
  const d10 = mkC("10", 0, 0.95, "S", 10);
  const d7 = mkC("7", 7, 0.94, "H", 35);

  let t = 10.0;
  sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
  // Commit initial pair [8, 8] + dealer [10]
  for (let i = 0; i < 3; i++) {
    sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [c8a, c8b], dealer_cards: [d10], player_hands: { 0: [c8a, c8b] } });
  }
  assert.deepStrictEqual(sm17.pDebouncers[0].committedRanks, ["8", "8"]);

  // Single-frame noisy split must NOT emit split
  const evNoise = sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [c8a, c8b], dealer_cards: [d10], player_hands: { 0: [c8a], 1: [c8b] }, split_detected: true });
  assert(!evNoise.some((e) => e.type === "split"), "Single frame must not emit split");

  // Unseparated overlap frame (H0 still [8,8] while H1 emerging) must NOT emit split
  const evOverlap = sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [c8a, c8b, c8b], dealer_cards: [d10], player_hands: { 0: [c8a, c8b], 1: [c8b] }, split_detected: true });
  assert(!evOverlap.some((e) => e.type === "split"), "Unseparated overlap must hold off split");

  // Separated split across 3 frames confirms split and re-anchors H0=[8], H1=[8]
  let splitEvts = [];
  for (let i = 0; i < 3; i++) {
    splitEvts.push(...sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [c8a, c8b], dealer_cards: [d10], player_hands: { 0: [c8a], 1: [c8b] }, split_detected: true }));
  }
  const sp = splitEvts.filter((e) => e.type === "split");
  assert.strictEqual(sp.length, 1, "Confirmed split must emit exactly once");
  assert.strictEqual(splitEvts.filter((e) => e.type === "card").length, 0, "Split re-anchor must not duplicate Card 0");

  // H0 draws 5, H1 draws 3 (with H1 Card 0 occluded by score badge -> only [3] visible!)
  const drawEvts = [];
  for (let i = 0; i < 3; i++) {
    drawEvts.push(...sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [c8a, c5, c3], dealer_cards: [d10], player_hands: { 0: [c8a, c5], 1: [c3] }, split_detected: true }));
  }
  const pCardEvts = drawEvts.filter((e) => e.type === "card" && e.target === "P");
  assert(pCardEvts.some((e) => e.handIndex === 0 && e.rankLabel === "5"), "H0 must emit drawn 5 after re-anchor");
  assert(pCardEvts.some((e) => e.handIndex === 1 && e.rankLabel === "3"), "H1 must emit drawn 3 with seed-card lock");

  // Dealer flips 7
  for (let i = 0; i < 2; i++) {
    sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [c8a, c5, c3], dealer_cards: [d10, d7], player_hands: { 0: [c8a, c5], 1: [c3] }, split_detected: true });
  }
  // Table clears -> clean multi-hand split in Watch mode stages auto_watch (2H)
  let finEvts = [];
  for (let i = 0; i < 4; i++) {
    finEvts.push(...sm17.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} }));
  }
  const nmbEvts = sm17.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
  const aw = [...finEvts, ...nmbEvts].find((e) => e.type === "auto_watch");
  assert(aw && aw.handCount === 2 && aw.summary.includes("(2H)"), `Clean 2H split should auto_watch, got ${JSON.stringify([...finEvts, ...nmbEvts])}`);
  console.log("   Test 17 PASSED");
}

// Test 18: Digital dealer card reliability (slow turn missing WAIT, 2-frame suitless draw, blurred-suit high-conf pip)
{
  console.log("18. Testing dealer slow-turn WAIT-fade survival, 2-frame suitless draw & blurred-suit card...");
  const sm18 = new Engine.CompanionStateMachine(cfg);
  const mkC = (rank, val, conf = 0.90, suit = null, x = 10) => ({ rank, val, conf, suit, x, y: 10, w: 12, h: 16, hand_index: 0 });
  const pA = mkC("A", 1, 0.94, "S", 20);
  const p9 = mkC("9", 9, 0.94, "H", 40);
  const dK = mkC("K", 0, 0.95, "S", 10);
  const dBack = { rank: "BACK", val: null, conf: 0.92, suit: null, x: 35, y: 10, w: 12, h: 16, hand_index: 0 };
  const d6 = mkC("6", 6, 0.83, null, 35);
  const d4 = mkC("4", 4, 0.82, null, 60);

  let t = 20.0;
  sm18.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [], dealer_cards: [], player_hands: {} });
  for (let i = 0; i < 3; i++) {
    sm18.step({ ts: (t += 0.1), banner_state: "WAIT", banner_conf: 0.95, player_cards: [pA, p9], dealer_cards: [dK, dBack], player_hands: { 0: [pA, p9] } });
  }
  // WAIT fades for 4.5s while player/table thinks and dealer still has [K, BACK] -> MUST NOT finalize early
  for (let i = 0; i < 9; i++) {
    const evWaitGone = sm18.step({ ts: (t += 0.5), banner_state: "", banner_conf: 0, player_cards: [pA, p9], dealer_cards: [dK, dBack], player_hands: { 0: [pA, p9] } });
    assert(!evWaitGone.some((e) => e.type === "prompt_confirm" || e.type === "auto_watch"), "Must not finalize while dealer still has BACK");
  }
  // Dealer flips 6 (0.83, suit=null) across 2 frames, then draws 4 (0.82, suit=null) across 2 frames
  const dEvts = [];
  for (let i = 0; i < 2; i++) {
    dEvts.push(...sm18.step({ ts: (t += 0.08), banner_state: "", banner_conf: 0, player_cards: [pA, p9], dealer_cards: [dK, d6], player_hands: { 0: [pA, p9] } }));
  }
  for (let i = 0; i < 2; i++) {
    dEvts.push(...sm18.step({ ts: (t += 0.08), banner_state: "", banner_conf: 0, player_cards: [pA, p9], dealer_cards: [dK, d6, d4], player_hands: { 0: [pA, p9] } }));
  }
  const dCardsOut = dEvts.filter((e) => e.type === "card" && e.target === "D").map((e) => e.rankLabel);
  assert.deepStrictEqual(dCardsOut, ["6", "4"], `Expected 2-frame suitless dealer flip+draw [6, 4], got ${JSON.stringify(dCardsOut)}`);
  console.log("   Test 18 PASSED");
}

// Test 19: iPad center-banner-only round lifecycle (no top WAIT banner; NO_MORE_BETS starts, PLACE_BETS ends)
{
  console.log("19. Testing iPad center-banner-only lifecycle (NO_MORE_BETS starts, PLACE_BETS ends)...");
  const ipadCfg = Engine.defaultConfig({ ...pack.config, anchor_mode: "ipad_split_left" });
  assert.strictEqual(ipadCfg.require_wait_banner, false, "ipad_split_left must set require_wait_banner=false");
  const sm19 = new Engine.CompanionStateMachine(ipadCfg);
  const mkC = (rank, val, conf = 0.94, suit = "S", x = 10) => ({ rank, val, conf, suit, x, y: 10, w: 12, h: 16, hand_index: 0 });
  const p1 = mkC("A", 1, 0.94, "S", 20);
  const p2 = mkC("10", 0, 0.94, "H", 40);
  const d1 = mkC("9", 9, 0.95, "C", 10);
  const d2 = mkC("8", 8, 0.93, "D", 35);

  let t = 50.0;
  // 1. Center "Place Your Bets" -> BETTING
  let ev = sm19.step({ ts: (t += 0.1), banner_state: "PLACE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  assert(ev.some((e) => e.type === "phase" && e.phase === "PLACE_BETS"));
  assert.strictEqual(sm19.state, "BETTING");

  // 2. Center "No More Bets" -> fades -> round starts (DEALING) with NO WAIT banner
  sm19.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  ev = sm19.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [], dealer_cards: [], player_hands: {} });
  assert(ev.some((e) => e.type === "phase" && e.phase === "DEALING"), "Round must start after NO_MORE_BETS without WAIT");

  // 3. Cards dealt (no WAIT banner)
  const dealEv = [];
  for (let i = 0; i < 3; i++) {
    dealEv.push(...sm19.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d1], player_hands: { 0: [p1, p2] } }));
  }
  for (let i = 0; i < 2; i++) {
    dealEv.push(...sm19.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d1, d2], player_hands: { 0: [p1, p2] } }));
  }
  assert.strictEqual(dealEv.filter((e) => e.type === "card" && e.target === "P").length, 2);
  assert.strictEqual(dealEv.filter((e) => e.type === "card" && e.target === "D").length, 2);

  // 4. Center "Place Your Bets" appears even while old cards are still fading -> finalizes round immediately!
  const endEv = sm19.step({ ts: (t += 0.1), banner_state: "PLACE_BETS", banner_conf: 0.92, player_cards: [p1, p2], dealer_cards: [d1, d2], player_hands: { 0: [p1, p2] } });
  assert.strictEqual(sm19.state, "BETTING");
  assert(endEv.some((e) => e.type === "phase" && e.phase === "PLACE_BETS"));
  // Next frame with old fading cards still visible must NOT re-start a round prematurely
  const fadeEv = sm19.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d1, d2], player_hands: { 0: [p1, p2] } });
  assert.strictEqual(sm19.state, "BETTING", "Old fading cards after PLACE_BETS must not start a new round");
  assert.strictEqual(fadeEv.filter((e) => e.type === "card").length, 0);

  // 5. Next NO_MORE_BETS flushes staged auto_watch from previous round
  const nmb2 = sm19.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  assert(nmb2.some((e) => e.type === "auto_watch"), "NO_MORE_BETS must flush auto_watch from completed round");
  console.log("   Test 19 PASSED");
}

// Test 20: 1-frame crisp final dealer card commit & immediate keepalive reload on PLACE_BETS or wiped strips
{
  console.log("20. Testing 1-frame crisp final dealer card commit & immediate keepalive reload...");
  const sm20 = new Engine.CompanionStateMachine(cfg);
  const mkC = (rank, val, conf = 0.92, suit = "S", x = 10) => ({ rank, val, conf, suit, x, y: 10, w: 12, h: 16, hand_index: 0 });
  const p1 = mkC("10", 0, 0.94, "S", 20);
  const p2 = mkC("9", 9, 0.94, "H", 40);
  const d1 = mkC("7", 7, 0.94, "C", 10);
  const d2 = mkC("10", 0, 0.89, "D", 35);
  const d3 = mkC("5", 5, 0.88, null, 55);

  let t = 80.0;
  sm20.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  for (let i = 0; i < 3; i++) {
    sm20.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d1], player_hands: { 0: [p1, p2] } });
  }
  const evD2 = sm20.step({ ts: (t += 0.08), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d1, d2], player_hands: { 0: [p1, p2] } });
  assert(evD2.some((e) => e.type === "card" && e.target === "D" && e.rankLabel === "10"), "2nd dealer card must record on 1st crisp frame");
  const evD3 = sm20.step({ ts: (t += 0.08), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d1, d2, d3], player_hands: { 0: [p1, p2] } });
  assert(evD3.some((e) => e.type === "card" && e.target === "D" && e.rankLabel === "5"), "Last dealer card must record on 1st crisp frame before wipe");

  // Keepalive reload triggers immediately on PLACE_BETS even with draftCount > 0
  const ka20a = new Engine.WatchKeepaliveManager({ ...cfg, keepalive_interval_sec: 60 });
  ka20a._watchElapsedSec = 62;
  const kaEvPb = ka20a.step({ now: 162, windowFound: true, obs: { banner_state: "PLACE_BETS", place_bets_conf: 0.9, player_cards: [], dealer_cards: [] }, smState: "BETTING", playMode: "watch", draftCount: 4, hasPrompt: false, betRoundsCount: 0, triggerRefreshCb: () => [true, "safari_ext"] });
  assert(kaEvPb.some((e) => e.event === "refreshing"), "Keepalive must trigger immediately on PLACE_BETS even with draftCount > 0");

  // Keepalive reload must ONLY trigger during PLACE_BETS (never during ROUND_OVER without PLACE_BETS, SHUFFLING, or LAST_BETS)
  const ka20b = new Engine.WatchKeepaliveManager({ ...cfg, keepalive_interval_sec: 60 });
  ka20b._watchElapsedSec = 62;
  const kaEvWipe = ka20b.step({ now: 262, windowFound: true, obs: { banner_state: "", place_bets_conf: 0, player_cards: [], dealer_cards: [] }, smState: "ROUND_OVER", playMode: "watch", draftCount: 4, hasPrompt: false, betRoundsCount: 0, triggerRefreshCb: () => [true, "safari_ext"] });
  assert(!kaEvWipe.some((e) => e.event === "refreshing"), "Keepalive must NOT trigger in ROUND_OVER without PLACE_BETS");
  const kaEvShuf = ka20b.step({ now: 263, windowFound: true, obs: { banner_state: "SHUFFLING", place_bets_conf: 0, player_cards: [], dealer_cards: [] }, smState: "SHUFFLING", playMode: "watch", draftCount: 0, hasPrompt: false, betRoundsCount: 0, triggerRefreshCb: () => [true, "safari_ext"] });
  assert(!kaEvShuf.some((e) => e.event === "refreshing"), "Keepalive must NOT trigger during SHUFFLING");
  const kaEvLb = ka20b.step({ now: 264, windowFound: true, obs: { banner_state: "LAST_BETS", place_bets_conf: 0.85, player_cards: [], dealer_cards: [] }, smState: "BETTING", playMode: "watch", draftCount: 0, hasPrompt: false, betRoundsCount: 0, triggerRefreshCb: () => [true, "safari_ext"] });
  assert(!kaEvLb.some((e) => e.event === "refreshing"), "Keepalive must NOT trigger during LAST_BETS");
  const kaEvPb2 = ka20b.step({ now: 265, windowFound: true, obs: { banner_state: "PLACE_BETS", place_bets_conf: 0.9, player_cards: [], dealer_cards: [] }, smState: "BETTING", playMode: "watch", draftCount: 0, hasPrompt: false, betRoundsCount: 0, triggerRefreshCb: () => [true, "safari_ext"] });
  assert(kaEvPb2.some((e) => e.event === "refreshing"), "Keepalive must trigger once PLACE_BETS appears");
  console.log("   Test 20 PASSED");
}

// Test 21: Multi-fallback final dealer card rescue (same-frame PLACE_BETS, transient tail buffer, badge OCR & rank deduction)
{
  console.log("21. Testing multi-fallback final dealer card rescue & badge deduction...");
  const mkC = (rank, val, conf = 0.93, suit = "S", x = 10) => ({ rank, val, conf, suit, x, y: 10, w: 12, h: 16, hand_index: 0 });
  const p1 = mkC("10", 0, 0.94, "S", 20);
  const p2 = mkC("8", 8, 0.94, "H", 40);
  const d10 = mkC("10", 0, 0.94, "C", 10);
  const d6 = mkC("6", 6, 0.93, "D", 30);
  const dA = mkC("A", 1, 0.93, "S", 10);
  const d5 = mkC("5", 5, 0.93, "H", 30);

  // 21a. Final dealer card appearing ONLY on the PLACE_BETS frame is still captured before round finalizes
  const sm21a = new Engine.CompanionStateMachine(cfg);
  let t = 100.0;
  sm21a.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  for (let i = 0; i < 3; i++) {
    sm21a.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d10, d6], player_hands: { 0: [p1, p2] } });
  }
  const d3SameFrame = mkC("4", 4, 0.80, null, 50);
  const evPb = sm21a.step({ ts: (t += 0.1), banner_state: "PLACE_BETS", banner_conf: 0.92, player_cards: [p1, p2], dealer_cards: [d10, d6, d3SameFrame], player_hands: { 0: [p1, p2] } });
  assert(evPb.some((e) => e.type === "card" && e.target === "D" && e.rankLabel === "4"), "Final dealer card on PLACE_BETS frame must be rescued");
  const evNmb = sm21a.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  assert(evNmb.some((e) => e.type === "auto_watch"), "Rescued 20 dealer total should auto_watch cleanly");

  // 21b. Transient 0.74 blurred final dealer card that vanished 2 frames before PLACE_BETS is rescued via _bestDealerTail
  const sm21b = new Engine.CompanionStateMachine(cfg);
  sm21b.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  for (let i = 0; i < 3; i++) {
    sm21b.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d10, d6], player_hands: { 0: [p1, p2] } });
  }
  sm21b.step({ ts: (t += 0.08), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d10, d6, mkC("3", 3, 0.74, null, 50)], player_hands: { 0: [p1, p2] } });
  sm21b.step({ ts: (t += 0.08), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d10, d6], player_hands: { 0: [p1, p2] } });
  sm21b.step({ ts: (t += 0.08), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [d10, d6], player_hands: { 0: [p1, p2] } });
  const evPbB = sm21b.step({ ts: (t += 0.1), banner_state: "PLACE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  assert(evPbB.some((e) => e.type === "card" && e.target === "D" && e.rankLabel === "3"), "Transient 0.74 dealer tail card must be rescued via _bestDealerTail");

  // 21c. deduceMissingDealerCard math & CompanionStateMachine badge deduction fallback
  assert.deepStrictEqual(Engine.deduceMissingDealerCard(["10", "6"], 21), { rank: "5", val: 5 });
  assert.deepStrictEqual(Engine.deduceMissingDealerCard(["10", "6"], 26), { rank: "10", val: 0 });
  assert.deepStrictEqual(Engine.deduceMissingDealerCard(["A", "5"], 18), { rank: "2", val: 2 });
  assert.deepStrictEqual(Engine.deduceMissingDealerCard(["10"], 20), { rank: "10", val: 0 });
  assert.deepStrictEqual(Engine.deduceMissingDealerCard(["9"], 20), { rank: "A", val: 1 });

  // Synthetic dark badge OCR test ("21" and "17" to the left of a dealer card cluster)
  const glyphMap = {
    "1": ["....###...", "...####...", "..##.##...", ".....##...", ".....##...", ".....##...", ".....##...", ".....##...", ".....##...", ".....##...", ".....##...", "..#######."],
    "2": ["..######..", ".##....##.", ".##....##.", ".......##.", "......##..", ".....##...", "....##....", "...##.....", "..##......", ".##.......", ".##.......", ".########."],
    "7": [".########.", ".##....##.", ".......##.", "......##..", ".....##...", ".....##...", "....##....", "....##....", "...##.....", "...##.....", "...##.....", "...##....."],
  };
  const mkBadgeImg = (digits) => {
    const img = Engine.createImage(140, 80, 3);
    // Green felt background
    for (let i = 0; i < img.data.length; i += 3) { img.data[i] = 20; img.data[i + 1] = 85; img.data[i + 2] = 35; }
    // Dark circular badge at x=8..44, y=38..72
    for (let y = 38; y < 72; y++) {
      for (let x = 8; x < 44; x++) {
        const idx = (y * 140 + x) * 3;
        img.data[idx] = 22; img.data[idx + 1] = 22; img.data[idx + 2] = 22;
      }
    }
    // White digits inside badge
    digits.forEach((d, di) => {
      const rows = glyphMap[d];
      const ox = 12 + di * 12;
      const oy = 48;
      for (let r = 0; r < rows.length; r++) {
        for (let c = 0; c < rows[r].length; c++) {
          if (rows[r][c] === "#") {
            const idx = ((oy + r) * 140 + (ox + c)) * 3;
            img.data[idx] = 245; img.data[idx + 1] = 245; img.data[idx + 2] = 245;
          }
        }
      }
    });
    return img;
  };
  const [b21, c21] = Engine.detectDealerBadgeTotal(mkBadgeImg(["2", "1"]), [[50, 14, 48, 44]]);
  assert.strictEqual(b21, 21, `Expected badge total 21, got ${b21} (conf=${c21})`);
  const [b17, c17] = Engine.detectDealerBadgeTotal(mkBadgeImg(["1", "7"]), [[50, 14, 48, 44]]);
  assert.strictEqual(b17, 17, `Expected badge total 17, got ${b17} (conf=${c17})`);

  // End-to-end CompanionStateMachine badge deduction when final card is completely unseen
  const sm21c = new Engine.CompanionStateMachine(cfg);
  sm21c.step({ ts: (t += 0.1), banner_state: "NO_MORE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {} });
  for (let i = 0; i < 3; i++) {
    sm21c.step({ ts: (t += 0.1), banner_state: "", banner_conf: 0, player_cards: [p1, p2], dealer_cards: [dA, d5], player_hands: { 0: [p1, p2] }, dealer_badge_total: 16, dealer_badge_conf: 0.90 });
  }
  // Badge updates to 18 on final frame while 3rd card was too fast/missed
  const evBadge = sm21c.step({ ts: (t += 0.1), banner_state: "PLACE_BETS", banner_conf: 0.92, player_cards: [], dealer_cards: [], player_hands: {}, dealer_badge_total: 18, dealer_badge_conf: 0.91 });
  const deducedCard = evBadge.find((e) => e.type === "card" && e.target === "D" && e.source === "badge_deduced");
  assert(deducedCard && deducedCard.rankLabel === "2" && deducedCard.val === 2, `Expected deduced '2', got ${JSON.stringify(evBadge)}`);
  console.log("   Test 21 PASSED");
}

// Test 22: Reload timer resets from WARN/alert mode when player & dealer strips become visible
{
  console.log("22. Testing reload timer reset from WARN when player & dealer strips become visible...");
  const mkC = (rank, val, conf = 0.93, suit = "S", x = 10) => ({ rank, val, conf, suit, x, y: 10, w: 12, h: 16, hand_index: 0 });
  const ka22 = new Engine.WatchKeepaliveManager({ ...cfg, keepalive_interval_sec: 60, keepalive_reload_grace_sec: 14 });
  ka22._watchElapsedSec = 62;
  // 22a. Trigger refresh at t=100, then let it time out past 14s grace -> WARN
  const evRef = ka22.step({
    now: 100,
    windowFound: true,
    obs: { banner_state: "PLACE_BETS", place_bets_conf: 0.9, player_cards: [], dealer_cards: [] },
    smState: "BETTING",
    playMode: "watch",
    draftCount: 0,
    hasPrompt: false,
    betRoundsCount: 0,
    triggerRefreshCb: () => [true, "safari_ext"],
  });
  assert(evRef.some((e) => e.event === "refreshing"));
  assert.strictEqual(ka22.state, "REFRESHING");

  const evTimeout = ka22.step({
    now: 116,
    windowFound: true,
    obs: { banner_state: "", place_bets_conf: 0, player_cards: [], dealer_cards: [] },
    smState: "IDLE",
    playMode: "watch",
    draftCount: 0,
    hasPrompt: false,
    betRoundsCount: 0,
  });
  assert(evTimeout.some((e) => e.event === "timeout_warn"), "Slow reload should emit timeout_warn");
  assert.strictEqual(ka22.state, "WARN");

  // While still in WARN after timeout_warn, keepalive must NOT trigger a second reload
  let secondReloadCalled = false;
  ka22.step({
    now: 116.5,
    windowFound: true,
    obs: { banner_state: "PLACE_BETS", place_bets_conf: 0.9, player_cards: [], dealer_cards: [] },
    smState: "BETTING",
    playMode: "watch",
    draftCount: 0,
    hasPrompt: false,
    betRoundsCount: 0,
    triggerRefreshCb: () => { secondReloadCalled = true; return [true, "ext"]; },
  });
  assert.strictEqual(secondReloadCalled, false, "Keepalive must NOT reload a second time while waiting in WARN");

  // Only player strip visible -> stays WARN; once BOTH player & dealer strips are visible -> resets to WATCH_COUNTDOWN
  ka22.step({
    now: 117,
    windowFound: true,
    obs: { banner_state: "", player_cards: [mkC("10", 0)], dealer_cards: [] },
    smState: "DEALING",
    playMode: "watch",
    draftCount: 1,
    hasPrompt: false,
    betRoundsCount: 0,
  });
  assert.strictEqual(ka22.state, "WARN", "Must wait until both player & dealer strips are visible");

  const evRec = ka22.step({
    now: 118,
    windowFound: true,
    obs: { banner_state: "", player_cards: [mkC("10", 0), mkC("8", 8)], dealer_cards: [mkC("7", 7)] },
    smState: "PLAYER_TURN",
    playMode: "watch",
    draftCount: 3,
    hasPrompt: false,
    betRoundsCount: 0,
  });
  assert(evRec.some((e) => e.event === "recovered" && e.reason === "strips_visible_fallback"), "Must emit recovered when both strips become visible");
  assert.strictEqual(ka22.state, "WATCH_COUNTDOWN");
  assert.strictEqual(ka22._watchElapsedSec, 0);

  // 22b. Alert WARN (triggerRefreshCb returns false) also resets once strips reappear after being absent
  const ka22b = new Engine.WatchKeepaliveManager({ ...cfg, keepalive_interval_sec: 60 });
  ka22b._watchElapsedSec = 65;
  const evAlert = ka22b.step({
    now: 200,
    windowFound: true,
    obs: { banner_state: "PLACE_BETS", place_bets_conf: 0.9, player_cards: [], dealer_cards: [] },
    smState: "BETTING",
    playMode: "watch",
    draftCount: 0,
    hasPrompt: false,
    betRoundsCount: 0,
    triggerRefreshCb: () => [false, "none"],
  });
  assert(evAlert.some((e) => e.event === "alert_place_bets"));
  assert.strictEqual(ka22b.state, "WARN");
  ka22b.step({
    now: 201,
    windowFound: true,
    obs: { banner_state: "", player_cards: [], dealer_cards: [] },
    smState: "IDLE",
    playMode: "watch",
    draftCount: 0,
    hasPrompt: false,
    betRoundsCount: 0,
  });
  const evRecB = ka22b.step({
    now: 202,
    windowFound: true,
    obs: { banner_state: "", player_cards: [mkC("9", 9)], dealer_cards: [mkC("K", 0)] },
    smState: "PLAYER_TURN",
    playMode: "watch",
    draftCount: 2,
    hasPrompt: false,
    betRoundsCount: 0,
  });
  assert(evRecB.some((e) => e.event === "recovered"), "Alert WARN must also reset once player & dealer strips reappear");
  assert.strictEqual(ka22b.state, "WATCH_COUNTDOWN");
  assert.strictEqual(ka22b._watchElapsedSec, 0);
  console.log("   Test 22 PASSED");
}

// 23. 1/3-2/3 Split View #111111 divider detection & small 5x7 dealer badge digits
{
  console.log("23. Testing 1/3-2/3 Split View divider (#111111) & small 5x7 dealer badge digits...");
  const fw = 1024, fh = 711;
  const f = Engine.createImage(fw, fh, 3, 20);
  // Left pane: 16:9 game area at y=99..335 on dark browser chrome
  for (let y = 99; y < 434; y++) {
    for (let x = 0; x < 334; x++) {
      const idx = (y * fw + x) * 3;
      f.data[idx] = 45; f.data[idx + 1] = 95; f.data[idx + 2] = 40;
    }
  }
  // iPadOS #111111 (17,17,17) Split View divider at x=334..339
  for (let y = 0; y < fh; y++) {
    for (let x = 334; x < 339; x++) {
      const idx = (y * fw + x) * 3;
      f.data[idx] = 17; f.data[idx + 1] = 17; f.data[idx + 2] = 17;
    }
    // Secondary internal dark gap at x=714..719 (must NOT override leftmost divider)
    for (let x = 714; x < 719; x++) {
      const idx = (y * fw + x) * 3;
      f.data[idx] = 10; f.data[idx + 1] = 10; f.data[idx + 2] = 10;
    }
  }
  const [vx, vy, vw, vh] = Engine.detectSplitViewLeftPane(f);
  assert.strictEqual(vx, 0);
  assert.strictEqual(vw, 334, `Expected 1/3 Split View width 334, got ${vw}`);
  assert(vy >= 90 && vy <= 105 && vh >= 325 && vh <= 345, `Expected 16:9 vertical bounds, got y=${vy}, h=${vh}`);

  // Small 5x7 '8' inside dark circular badge to left of dealer cluster
  const dImg = Engine.createImage(127, 74, 3, 35);
  // White card cluster at x=39..85, y=12..54
  for (let y = 12; y < 54; y++) {
    for (let x = 39; x < 85; x++) {
      const idx = (y * 127 + x) * 3;
      dImg.data[idx] = 240; dImg.data[idx + 1] = 240; dImg.data[idx + 2] = 240;
    }
  }
  // Dark badge circle around x=30..40, y=28..39
  for (let y = 26; y < 41; y++) {
    for (let x = 27; x < 42; x++) {
      const idx = (y * 127 + x) * 3;
      dImg.data[idx] = 20; dImg.data[idx + 1] = 20; dImg.data[idx + 2] = 20;
    }
  }
  const g8 = [
    ".###.",
    "#...#",
    "#...#",
    ".###.",
    "#...#",
    "#...#",
    ".###.",
  ];
  for (let gy = 0; gy < 7; gy++) {
    for (let gx = 0; gx < 5; gx++) {
      if (g8[gy][gx] === "#") {
        const idx = ((30 + gy) * 127 + (32 + gx)) * 3;
        dImg.data[idx] = 235; dImg.data[idx + 1] = 235; dImg.data[idx + 2] = 235;
      }
    }
  }
  const [badge8] = Engine.detectDealerBadgeTotal(dImg, [[39, 12, 46, 42]]);
  assert.strictEqual(badge8, 8, `Expected 5x7 badge digit 8, got ${badge8}`);
  console.log("   Test 23 PASSED");
}

// 24. Educated guess fallback for incomplete dealer hands
{
  console.log("24. Testing educated guess fallback for incomplete dealer hands...");
  assert.deepStrictEqual(Engine.guessMissingDealerCard(["7", "5"]), { rank: "10", val: 0 });
  assert.deepStrictEqual(Engine.guessMissingDealerCard(["6"]), { rank: "A", val: 1 });

  const cfg24 = Engine.defaultConfig({
    anchor_mode: "ipad_split_left",
    confirm_frames: 1,
    clear_frames: 2,
    require_wait_banner: false,
  });
  const sm24 = new Engine.CompanionStateMachine(cfg24);
  const mk = (rank, val) => ({ rank, val, conf: 0.94, suit: "S", x: 10, y: 10, w: 12, h: 16, hand_index: 0 });

  sm24.step({
    ts: 1,
    banner_state: "NO_MORE_BETS",
    player_cards: [],
    dealer_cards: [],
    player_hands: {},
    hud_refreshed: true,
  });
  sm24.step({
    ts: 2,
    banner_state: "",
    player_cards: [mk("10", 0), mk("9", 9)],
    dealer_cards: [mk("7", 7), { rank: "BACK", val: null, conf: 0.92, suit: null, x: 30, y: 10, w: 20, h: 30, hand_index: 0 }],
    player_hands: { 0: [mk("10", 0), mk("9", 9)] },
    hud_refreshed: true,
  });
  // Wipe to PLACE_BETS before dealer's hole/final card is ever seen or badge read
  const endEvs = sm24.step({
    ts: 3,
    banner_state: "PLACE_BETS",
    player_cards: [],
    dealer_cards: [],
    player_hands: {},
    hud_refreshed: true,
  });
  const guessEv = endEvs.find((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess");
  assert(guessEv, "Must emit educated_guess dealer card when dealer hand is incomplete");
  assert.strictEqual(guessEv.rankLabel, "10");
  assert.strictEqual(guessEv.slot, 1, "Educated guess must replace BACK at slot 1");
  const promptEv = endEvs.find((e) => e.type === "prompt_confirm" && e.reason === "DEALER_INCOMPLETE");
  assert(promptEv, "Must emit DEALER_INCOMPLETE prompt so user can remove guessed card if desired");
  assert.strictEqual(promptEv.educatedGuessRank, "10");
  assert(sm24._pendingAutoWatch, "Must queue _pendingAutoWatch for NO_MORE_BETS after educated guess");
  const nmb24 = sm24.step({ ts: 4, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  assert(nmb24.some((e) => e.type === "auto_watch" && e.fromWatch === true), "Must emit auto_watch at NO_MORE_BETS after educated guess");

  // 24b. Dealer [2, 3] (total 5 < 17) must guess enough cards to reach >= 17
  sm24.step({ ts: 5, banner_state: "", player_cards: [mk("10", 0), mk("9", 9)], dealer_cards: [mk("2", 2), mk("3", 3)], player_hands: { 0: [mk("10", 0), mk("9", 9)] }, hud_refreshed: true });
  const end24b = sm24.step({ ts: 6, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  const g24b = end24b.filter((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess");
  assert.strictEqual(g24b.length, 2, "Dealer [2, 3] must guess 2 cards to reach >= 17");
  const p24b = end24b.find((e) => e.type === "prompt_confirm");
  assert(p24b.dealerTotal >= 17, `Expected dealerTotal >= 17, got ${p24b.dealerTotal}`);
  console.log("   Test 24 PASSED");
}

// 25. Split Guard allows auto_watch logging at NO_MORE_BETS
{
  console.log("25. Testing Split Guard auto_watch logging at NO_MORE_BETS...");
  const cfg25 = Engine.defaultConfig({
    anchor_mode: "ipad_split_left",
    confirm_frames: 1,
    clear_frames: 2,
    require_wait_banner: false,
  });
  const sm25 = new Engine.CompanionStateMachine(cfg25);
  const mk = (rank, val) => ({ rank, val, conf: 0.94, suit: "S", x: 10, y: 10, w: 12, h: 16, hand_index: 0 });

  sm25.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm25.step({
    ts: 2,
    banner_state: "",
    player_cards: [mk("8", 8), mk("8", 8)],
    dealer_cards: [mk("10", 0), mk("7", 7)],
    player_hands: { 0: [mk("8", 8), mk("8", 8)] },
    split_detected: true,
    hud_refreshed: true,
  });
  // Round ends at PLACE_BETS while split_detected=true & only 1 hand committed (splitBlocksAuto=true)
  const pbEvs = sm25.step({ ts: 3, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  assert(pbEvs.some((e) => e.type === "prompt_confirm" && e.reason === "SPLIT"), "Should still show SPLIT prompt during betting");
  assert(sm25._pendingAutoWatch, "Must queue _pendingAutoWatch for NO_MORE_BETS");

  const nmbEvs = sm25.step({ ts: 4, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  assert(nmbEvs.some((e) => e.type === "auto_watch" && e.fromWatch === true), "Must emit auto_watch at NO_MORE_BETS even after Split Guard");
  console.log("   Test 25 PASSED");
}

// 26. Improved non-destructive auto-sync (reconcile) never shrinks committed cards & rescues UI desync
{
  console.log("26. Testing non-destructive monotonic auto-sync (reconcile)...");
  const cfg26 = Engine.defaultConfig({
    anchor_mode: "ipad_split_left",
    confirm_frames: 2,
    clear_frames: 4,
    require_wait_banner: false,
  });
  const sm26 = new Engine.CompanionStateMachine(cfg26);
  const mk = (rank, val, conf = 0.93) => ({ rank, val, conf, suit: "S", x: 10, y: 10, w: 12, h: 16, hand_index: 0 });

  let t = 1.0;
  sm26.step({ ts: t, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  for (let i = 0; i < 3; i++) {
    t += 0.1;
    sm26.step({
      ts: t,
      banner_state: "",
      player_cards: [mk("5", 5), mk("9", 9), mk("10", 0)],
      dealer_cards: [mk("3", 3)],
      player_hands: { 0: [mk("5", 5), mk("9", 9), mk("10", 0)] },
      hud_refreshed: true,
    });
  }
  assert.deepStrictEqual(sm26.pDebouncers[0].committedRanks, ["5", "9", "10"]);

  // 26a. Occluded HUD (missing first card '5' -> only [9, 10]) must NEVER shrink committed cards
  let shrinkEvs = [];
  for (let i = 0; i < 12; i++) {
    t += 0.12;
    shrinkEvs.push(...sm26.step({
      ts: t,
      banner_state: "",
      player_cards: [mk("9", 9), mk("10", 0)],
      dealer_cards: [mk("3", 3)],
      player_hands: { 0: [mk("9", 9), mk("10", 0)] },
      hud_refreshed: true,
    }));
  }
  assert(!shrinkEvs.some((e) => e.type === "reconcile"), "Auto-sync must NEVER shrink committed cards");
  assert.deepStrictEqual(sm26.pDebouncers[0].committedRanks, ["5", "9", "10"]);

  // 26b. UI desync (UI only has [5, 9] while HUD stably shows [5, 9, 10]) must emit reconcile
  sm26.sync_round_id = sm26.round_id;
  sm26.sync_draft_hands = [[5, 9]];
  sm26.sync_draft_hands_manual = [false];
  sm26.sync_draft_dealer = [3];
  sm26.sync_draft_dealer_manual = false;
  let recEvs = [];
  for (let i = 0; i < 10; i++) {
    t += 0.12;
    recEvs.push(...sm26.step({
      ts: t,
      banner_state: "",
      player_cards: [mk("5", 5), mk("9", 9), mk("10", 0)],
      dealer_cards: [mk("3", 3)],
      player_hands: { 0: [mk("5", 5), mk("9", 9), mk("10", 0)] },
      hud_refreshed: true,
    }));
  }
  const rec = recEvs.find((e) => e.type === "reconcile");
  assert(rec, "Auto-sync must emit reconcile when UI is missing committed cards");
  assert.deepStrictEqual(rec.hands[0].map((c) => c.rankLabel), ["5", "9", "10"]);
  console.log("   Test 26 PASSED");
}

// 27. Single-frame dealer card flash capture, 30-frame _pendingTail retention, multi-step badge history & 2-card badge deduction
{
  console.log("27. Testing single-frame dealer card flash capture, 30-frame _pendingTail retention & multi-step/pair badge deduction...");
  // 27a. 1-frame dealer flash with conf=0.79 and hasPipBody=true commits immediately in 1 frame
  const db27 = new Engine.SequenceDebouncer(2, 4, true, 0.84);
  const mkD = (rank, val, conf, suit = null, hasPipBody = false) => ({
    rank,
    val,
    conf,
    suit,
    hasPipBody,
    x: 10,
    y: 10,
    w: 12,
    h: 16,
    hand_index: 0,
  });
  db27.update([mkD("7", 7, 0.92, "S", true), { rank: "BACK", val: null, conf: 0.92, suit: null, x: 25, y: 10, w: 12, h: 16, hand_index: 0 }]);
  db27.update([mkD("7", 7, 0.92, "S", true), { rank: "BACK", val: null, conf: 0.92, suit: null, x: 25, y: 10, w: 12, h: 16, hand_index: 0 }]);
  assert.deepStrictEqual(db27.committedRanks, ["7", "BACK"]);
  // 1-frame hole flip at conf=0.79 (below 0.84, no suit, but hasPipBody=true) -> must commit on that single frame
  const [em27a] = db27.update([mkD("7", 7, 0.92, "S", true), mkD("8", 8, 0.79, null, true)]);
  assert.strictEqual(em27a.length, 1, "1-frame dealer flash with conf=0.79 & hasPipBody=true must commit immediately");
  assert.strictEqual(em27a[0][1].rank, "8");
  assert.deepStrictEqual(db27.committedRanks, ["7", "8"]);

  // 27b. Sub-0.78 1-frame flash (conf=0.785, no suit/pip) followed by 15 frames where the 3rd card faded early must be retained in _pendingTail and rescued on wipe
  db27.update([mkD("7", 7, 0.92, "S", true), mkD("8", 8, 0.91, "H", true), mkD("10", 0, 0.785, null, false)]);
  assert.strictEqual(db27.committedRanks.length, 2, "Sub-crisp 1-frame detection waits in _pendingTail");
  for (let i = 0; i < 15; i++) {
    db27.update([mkD("7", 7, 0.92, "S", true), mkD("8", 8, 0.91, "H", true)]);
  }
  const rescued27b = db27.flushPendingOnWipe(0.78);
  assert.strictEqual(rescued27b.length, 1, "_pendingTail must survive 15 pause frames (up to 30) and rescue on wipe");
  assert.strictEqual(rescued27b[0][1].rank, "10");

  // 27c. Multi-step badge history ([7] -> badge 15 -> badge 25) deduces both missed dealer cards ["8", "10"]
  const cfg27 = Engine.defaultConfig({
    anchor_mode: "ipad_split_left",
    confirm_frames: 1,
    clear_frames: 2,
    require_wait_banner: false,
  });
  const sm27c = new Engine.CompanionStateMachine(cfg27);
  sm27c.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm27c.step({
    ts: 2,
    banner_state: "",
    player_cards: [mkD("10", 0, 0.94, "S", true), mkD("9", 9, 0.94, "H", true)],
    dealer_cards: [mkD("7", 7, 0.94, "C", true)],
    player_hands: { 0: [mkD("10", 0, 0.94, "S", true), mkD("9", 9, 0.94, "H", true)] },
    dealer_badge_total: 7,
    dealer_badge_conf: 0.88,
    hud_refreshed: true,
  });
  // Intermediate frame: badge reads 15 (7 + 8), card flash missed
  sm27c.step({
    ts: 3,
    banner_state: "",
    player_cards: [mkD("10", 0, 0.94, "S", true), mkD("9", 9, 0.94, "H", true)],
    dealer_cards: [mkD("7", 7, 0.94, "C", true)],
    player_hands: { 0: [mkD("10", 0, 0.94, "S", true), mkD("9", 9, 0.94, "H", true)] },
    dealer_badge_total: 15,
    dealer_badge_conf: 0.86,
    hud_refreshed: true,
  });
  // Final frame before wipe: badge reads 25 (15 + 10), card flash missed
  sm27c.step({
    ts: 4,
    banner_state: "",
    player_cards: [mkD("10", 0, 0.94, "S", true), mkD("9", 9, 0.94, "H", true)],
    dealer_cards: [mkD("7", 7, 0.94, "C", true)],
    player_hands: { 0: [mkD("10", 0, 0.94, "S", true), mkD("9", 9, 0.94, "H", true)] },
    dealer_badge_total: 25,
    dealer_badge_conf: 0.87,
    hud_refreshed: true,
  });
  const end27c = sm27c.step({ ts: 5, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  const ded27c = end27c.filter((e) => e.type === "card" && e.target === "D" && e.source === "badge_deduced");
  assert.deepStrictEqual(ded27c.map((e) => e.rankLabel), ["8", "10"], "Multi-step badge history [7, 15, 25] must deduce both '8' and '10'");

  // 27d. Direct 2-card badge pair deduction when only upcard ['7'] and final bust badge 22 are seen
  const pair27d = Engine.deduceMissingDealerPair(["7"], 22);
  assert(pair27d && pair27d.length === 2, "deduceMissingDealerPair(['7'], 22) must return a 2-card pair");
  assert.deepStrictEqual(pair27d.map((c) => c.rank), ["5", "10"], "Most probable 2-card sequence from 7 to 22 is ['5', '10']");
  console.log("   Test 27 PASSED");
}

// 28. Multi-signal All-Rank Educated Guess Fallback (Sub-threshold peaks, Width prior, Soft Badge digits, Felt Sightings & Live Shoe Stratified Sampling)
{
  console.log("28. Testing multi-signal all-rank educated guess fallback across all 10 ranks...");

  // 28a. Signal 1: Sub-threshold template runner-up peak + narrow card width (remRight=13) guesses single-digit rank ('8', '4', '3', etc.)
  const g28a = Engine.guessMissingDealerCard(["10"], {
    subCandidates: [
      { rank: "10", val: 0, conf: 0.56, slotIdx: 1, remRight: 13, hasPipBody: true },
      { rank: "8", val: 8, conf: 0.55, slotIdx: 1, remRight: 13, hasPipBody: true },
    ],
  });
  assert(g28a && g28a.rank === "8" && g28a.val === 8, "Narrow width (remRight=13) must penalize '10' and select sub-threshold '8'");

  // 28b. Signal 2: Partial/soft dealer score badge hints (tensDigit / onesDigit) narrow and pick exact single-digit rank
  const g28b = Engine.guessMissingDealerCard(["10", "2"], {
    badgeHint: { total: null, conf: 0.62, tensDigit: 1, tensConf: 0.72, onesDigit: 8, onesConf: 0.65 },
  });
  assert(g28b && g28b.rank === "6" && g28b.val === 6, "Soft badge hint (tens=1, ones=8) on dealer 12 must guess '6' (12 + 6 = 18)");

  // 28c. Signal 3: Physical felt sightings pick a spotted non-10 card that satisfies stopping rules
  const g28c = Engine.guessMissingDealerCard(["10", "3"], {
    feltSightings: [{ rank: "5", val: 5, conf: 0.74 }],
  });
  assert(g28c && g28c.rank === "5" && g28c.val === 5, "Felt sighting of '5' on dealer 13 must guess '5' (13 + 5 = 18)");

  // 28d. Signal 4 & 5: Live shoe depletion + stratified roundSeed samples across all valid ranks (2..9, A, 10)
  // On dealer ['10', '6'] (hard 16), any rank A, 2..9, 10 reaches >= 17 (17..26).
  // Across 40 roundSeeds with a balanced 8-deck shoe, verify that multiple distinct single-digit ranks AND 'A' AND '10' are guessed.
  const fullShoe = [128, 32, 32, 32, 32, 32, 32, 32, 32, 32];
  const seenRanks = new Set();
  for (let seed = 1; seed <= 60; seed++) {
    const g = Engine.guessMissingDealerCard(["10", "6"], {
      shoeRem: fullShoe,
      roundSeed: seed,
    });
    assert(g, "Must return a valid guess for every roundSeed");
    seenRanks.add(g.rank);
  }
  for (const r of ["10", "A", "2", "3", "4", "5", "6", "7", "8", "9"]) {
    assert(seenRanks.has(r), `Stratified shoe sampling on hard 16 must be able to guess rank '${r}' (seen: ${Array.from(seenRanks).join(",")})`);
  }

  // Also verify shoe depletion: if all 10-value cards are depleted (shoeRem[0] = 0) and only '4' remains in shoe, it deterministically guesses '4'
  const onlyFoursShoe = [0, 0, 0, 0, 12, 0, 0, 0, 0, 0];
  const gDepleted = Engine.guessMissingDealerCard(["10", "3"], {
    shoeRem: onlyFoursShoe,
    roundSeed: 1,
  });
  assert(gDepleted && gDepleted.rank === "4" && gDepleted.val === 4, "Depleted shoe with only 4s left must guess '4'");

  // 28e. End-to-end CompanionStateMachine Fallback E with sub-threshold '7' on dealer ['10', 'BACK']
  const cfg28 = Engine.defaultConfig({
    anchor_mode: "ipad_split_left",
    confirm_frames: 1,
    clear_frames: 2,
    require_wait_banner: false,
  });
  const sm28 = new Engine.CompanionStateMachine(cfg28);
  const mkC = (rank, val, conf = 0.92) => ({ rank, val, conf, suit: "S", hasPipBody: true, x: 10, y: 10, w: 12, h: 16, hand_index: 0 });
  sm28.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm28.step({
    ts: 2,
    banner_state: "",
    player_cards: [mkC("10", 0), mkC("9", 9)],
    dealer_cards: [mkC("10", 0), { rank: "BACK", val: null, conf: 0.92, suit: null, x: 25, y: 10, w: 12, h: 16, hand_index: 0 }],
    dealer_sub_cards: [{ rank: "7", val: 7, conf: 0.59, suit: "H", hasPipBody: true, slotIdx: 1, remRight: 13 }],
    player_hands: { 0: [mkC("10", 0), mkC("9", 9)] },
    hud_refreshed: true,
  });
  const end28 = sm28.step({ ts: 3, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  const guess28 = end28.find((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess");
  assert(guess28, "CompanionStateMachine must emit educated_guess on incomplete dealer hand");
  assert.strictEqual(guess28.rankLabel, "7", "CompanionStateMachine must guess '7' from sub-threshold dealer_sub_cards");
  assert.strictEqual(guess28.val, 7);
  console.log("   Test 28 PASSED");
}

console.log("29. Testing strict dealer completeness & PLACE_BETS 0s/+3s rechecks...");
{
  const cfg29 = Engine.defaultConfig({
    anchor_mode: "ipad_split_left",
    confirm_frames: 1,
    clear_frames: 2,
    require_wait_banner: false,
  });
  const mkC = (rank, val, conf = 0.92) => ({ rank, val, conf, suit: "S", hasPipBody: true, x: 10, y: 10, w: 12, h: 16, hand_index: 0 });
  const _bjTotal = (ranks) => {
    let tot = 0, aces = 0;
    for (const r of ranks) {
      if (r === "A") { tot += 11; aces++; }
      else if (["10", "J", "Q", "K"].includes(r)) tot += 10;
      else tot += Number(r);
    }
    while (tot > 21 && aces > 0) { tot -= 10; aces--; }
    return tot;
  };

  // 1. 0s PLACE_BETS completion
  const sm1 = new Engine.CompanionStateMachine(cfg29);
  sm1.setSyncState({ playMode: "watch" });
  sm1.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm1.step({
    ts: 2,
    banner_state: "",
    player_cards: [mkC("10", 0), mkC("9", 9)],
    dealer_cards: [mkC("10", 0), { rank: "BACK", val: null, conf: 0.92, suit: null, x: 25, y: 10, w: 12, h: 16, hand_index: 0 }],
    player_hands: { 0: [mkC("10", 0), mkC("9", 9)] },
    hud_refreshed: true,
  });
  const evs0s = sm1.step({ ts: 3, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  assert(evs0s.some((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess"), "0s PLACE_BETS must complete [10, BACK]");

  // 2. >2 guesses for low dealer total & +3s recheck on sync_draft_dealer drift
  const sm2 = new Engine.CompanionStateMachine(cfg29);
  sm2.setSyncState({ playMode: "watch" });
  sm2.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm2.step({
    ts: 2,
    banner_state: "",
    player_cards: [mkC("10", 0), mkC("9", 9)],
    dealer_cards: [mkC("2", 2), mkC("3", 3)],
    player_hands: { 0: [mkC("10", 0), mkC("9", 9)] },
    hud_refreshed: true,
  });
  const evsLow = sm2.step({ ts: 3, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  const lowGuesses = evsLow.filter((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess");
  assert(lowGuesses.length >= 2, "Dealer [2, 3] must receive >= 2 guesses to reach >= 17");
  const lowTot = _bjTotal(["2", "3", ...lowGuesses.map((g) => g.rankLabel)]);
  assert(lowTot >= 17, `Completed dealer total must be >= 17, got ${lowTot}`);

  // Simulate UI drift where client only has [2] during PLACE_BETS
  sm2.setSyncState({
    playMode: "watch",
    draftCount: 3,
    draftHands: [[0, 9]],
    draftDealer: [2],
    dealerGuessRemoved: false,
  });
  const evs15 = sm2.step({ ts: 4.5, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  assert(!evs15.some((e) => e.type === "card" && e.source === "educated_guess"), "Should not run 3s recheck before +3.0s");
  const evs31 = sm2.step({ ts: 6.1, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  const g3s = evs31.filter((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess");
  assert(g3s.length >= 2, "+3s PLACE_BETS recheck must complete drifted dealer [2] to >= 17");
  const tot3s = _bjTotal(["2", ...g3s.map((g) => g.rankLabel)]);
  assert(tot3s >= 17, `+3s completed dealer total must be >= 17, got ${tot3s}`);

  // 3. dealerGuessRemoved guard
  const sm3 = new Engine.CompanionStateMachine(cfg29);
  sm3.setSyncState({ playMode: "watch" });
  sm3.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm3.step({
    ts: 2,
    banner_state: "",
    player_cards: [mkC("10", 0), mkC("9", 9)],
    dealer_cards: [mkC("10", 0), mkC("6", 6)],
    player_hands: { 0: [mkC("10", 0), mkC("9", 9)] },
    hud_refreshed: true,
  });
  sm3.step({ ts: 3, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm3.setSyncState({
    playMode: "watch",
    draftCount: 4,
    draftHands: [[0, 9]],
    draftDealer: [0, 6],
    dealerGuessRemoved: true,
  });
  const evsRemoved = sm3.step({ ts: 6.2, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  assert(!evsRemoved.some((e) => e.type === "card" && e.source === "educated_guess"), "dealerGuessRemoved=true must block +3s re-insertion");

  // 4. BET mode preserves _review* snapshot AND fires +3s recheck even when banner flickers blank ("")
  const sm4 = new Engine.CompanionStateMachine(cfg29);
  sm4.setSyncState({ playMode: "bet" });
  sm4.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm4.step({
    ts: 2,
    banner_state: "",
    player_cards: [mkC("10", 0), mkC("8", 8)],
    dealer_cards: [mkC("6", 6), mkC("10", 0)],
    player_hands: { 0: [mkC("10", 0), mkC("8", 8)] },
    hud_refreshed: true,
  });
  sm4.step({ ts: 3, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm4.setSyncState({
    playMode: "bet",
    draftCount: 4,
    draftHands: [[0, 8]],
    draftDealer: [6, 0],
    dealerGuessRemoved: false,
  });
  // Banner flickers to "" at +3.1s while state === BETTING
  const evsBet3s = sm4.step({ ts: 6.1, banner_state: "", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  const betGuesses = evsBet3s.filter((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess");
  assert(betGuesses.length >= 1, "BET mode +3s recheck must still fire on blank frame and complete dealer [6, 10]");
  assert(_bjTotal(["6", "10", ...betGuesses.map((g) => g.rankLabel)]) >= 17, "BET mode +3s completed dealer total must be >= 17");

  // 5. Busted community player hand [10, 6, 8] still completes dealer [6, 10] in Playtech All Bets BJ
  const sm5 = new Engine.CompanionStateMachine(cfg29);
  sm5.setSyncState({ playMode: "watch" });
  sm5.step({ ts: 1, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  sm5.step({
    ts: 2,
    banner_state: "",
    player_cards: [mkC("10", 0), mkC("6", 6), mkC("8", 8)],
    dealer_cards: [mkC("6", 6), mkC("10", 0)],
    player_hands: { 0: [mkC("10", 0), mkC("6", 6), mkC("8", 8)] },
    hud_refreshed: true,
  });
  const evsBust = sm5.step({ ts: 3, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {}, hud_refreshed: true });
  assert(evsBust.some((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess"), "Busted community player must still complete dealer [6, 10]");

  console.log("   Test 29 PASSED");
}

// 30. Dimmed split hands, 13-rank badge occlusion & top-right suit fallback
{
  console.log("30. Testing dimmed split hands, 13-rank badge occlusion & top-right suit fallback...");
  const rankTpls30 = detector.banks.hud;
  const suitTpls30 = detector.banks.suit;
  const findTpl = (bank, rank) => {
    const t = bank.find((x) => x.rank === rank);
    assert(t, `Missing template for ${rank}`);
    return t;
  };
  const pasteCard45 = (dst, x0, y0, rank, suit, withBadge, topRightSuit) => {
    const cw = topRightSuit ? 37 : 31;
    fillRect(dst, x0, y0, cw, 45, 252, 252, 252);
    const rt = findTpl(rankTpls30, rank).bgr;
    for (let y = 0; y < Math.min(rt.h, 20); y++) {
      for (let x = 0; x < Math.min(rt.w, 17); x++) {
        const si = (y * rt.w + x) * 3;
        const di = ((y0 + 2 + y) * dst.w + (x0 + 2 + x)) * 3;
        dst.data[di] = rt.data[si];
        dst.data[di + 1] = rt.data[si + 1];
        dst.data[di + 2] = rt.data[si + 2];
      }
    }
    const st = findTpl(suitTpls30, suit).bgr;
    const isRed = suit === "H" || suit === "D";
    for (let y = 0; y < Math.min(st.h, 18); y++) {
      for (let x = 0; x < Math.min(st.w, 15); x++) {
        const si = (y * st.w + x) * 3;
        const mn = Math.min(st.data[si], st.data[si + 1], st.data[si + 2]);
        const b = isRed && mn < 160 ? 40 : st.data[si];
        const g = isRed && mn < 160 ? 40 : st.data[si + 1];
        const r = isRed && mn < 160 ? 210 : st.data[si + 2];
        const di = ((y0 + 26 + y) * dst.w + (x0 + 3 + x)) * 3;
        dst.data[di] = b;
        dst.data[di + 1] = g;
        dst.data[di + 2] = r;
      }
    }
    if (topRightSuit) {
      const tw = Math.min(st.w, 15), th = Math.min(st.h, 17);
      for (let y = 0; y < th; y++) {
        for (let x = 0; x < tw; x++) {
          const si = (y * st.w + x) * 3;
          const mn = Math.min(st.data[si], st.data[si + 1], st.data[si + 2]);
          const di = ((y0 + 2 + (th - 1 - y)) * dst.w + (x0 + cw - tw - 2 + (tw - 1 - x))) * 3;
          dst.data[di] = isRed && mn < 160 ? 40 : st.data[si];
          dst.data[di + 1] = isRed && mn < 160 ? 40 : st.data[si + 1];
          dst.data[di + 2] = isRed && mn < 160 ? 210 : st.data[si + 2];
        }
      }
    }
    if (withBadge) {
      for (let y = 0; y < 45; y++) {
        for (let x = 0; x < cw; x++) {
          if ((x + 1) * (x + 1) + (y - 33) * (y - 33) <= 121) {
            const di = ((y0 + y) * dst.w + (x0 + x)) * 3;
            dst.data[di] = 18;
            dst.data[di + 1] = 22;
            dst.data[di + 2] = 18;
          }
        }
      }
    }
  };

  const roi45 = Engine.createImage(220, 60, 3, 22);
  fillRect(roi45, 0, 0, 220, 60, 18, 52, 22);
  pasteCard45(roi45, 16, 7, "Q", "C", true, true);
  pasteCard45(roi45, 100, 7, "10", "C", true, false);
  pasteCard45(roi45, 131, 7, "3", "D", false, true);
  for (let y = 0; y < roi45.h; y++) {
    for (let x = 90; x < roi45.w; x++) {
      const idx = (y * roi45.w + x) * 3;
      roi45.data[idx] = Math.floor(roi45.data[idx] * 0.5);
      roi45.data[idx + 1] = Math.floor(roi45.data[idx + 1] * 0.5);
      roi45.data[idx + 2] = Math.floor(roi45.data[idx + 2] * 0.5);
    }
  }
  const roi55 = Engine.resizeImage(roi45, Math.round(220 * 55 / 45), Math.round(60 * 55 / 45));
  const dealerCls = Engine.findWhiteCardClusters(roi55, true);
  assert.strictEqual(dealerCls.length, 1, "Dealer pass must ignore dimmed split hand");
  const playerCls = Engine.findWhiteCardClusters(roi55, false);
  assert.strictEqual(playerCls.length, 2, "Player pass must detect both bright and dimmed split hands");
  const [h0] = Engine.detectClusterCards(roi55, playerCls[0], rankTpls30, suitTpls30, 0.78, false, 0.84);
  const [h1] = Engine.detectClusterCards(roi55, playerCls[1], rankTpls30, suitTpls30, 0.78, false, 0.84);
  assert.deepStrictEqual(h0.map((c) => [c.rank, c.suit]), [["Q", "C"]]);
  assert.deepStrictEqual(h1.map((c) => [c.rank, c.suit]), [["10", "C"], ["3", "D"]]);

  for (const rk of ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]) {
    const r45 = Engine.createImage(100, 60, 3, 22);
    fillRect(r45, 0, 0, 100, 60, 18, 52, 22);
    pasteCard45(r45, 14, 7, rk, "S", true, false);
    pasteCard45(r45, 45, 7, "3", "D", false, false);
    const r55 = Engine.resizeImage(r45, Math.round(100 * 55 / 45), Math.round(60 * 55 / 45));
    const cls = Engine.findWhiteCardClusters(r55, false);
    assert.strictEqual(cls.length, 1, `Rank ${rk}: expected 1 cluster`);
    const [cards] = Engine.detectClusterCards(r55, cls[0], rankTpls30, suitTpls30, 0.78, false, 0.84);
    assert(cards.length >= 2 && cards[0].rank === rk, `Rank ${rk} failed under badge occlusion`);
  }
  console.log("   Test 30 PASSED");
}

// Test 31: Light Mode iPadOS Split View divider (#E5E5EA / #D1D1D6) detection
{
  console.log("31. Testing Light Mode iPadOS Split View divider detection...");
  const sw = 1200, sh = 900;
  const lightFrame = Engine.createImage(sw, sh, 3, 25);
  // Left pane (0..594): dark browser/game area; Divider (595..604): iPadOS Light Mode #E5E5EA (BGR: 234, 229, 229); Right pane (605..1199): tracker UI
  fillRect(lightFrame, 0, 0, 595, sh, 30, 55, 25);
  fillRect(lightFrame, 595, 0, 10, sh, 234, 229, 229);
  fillRect(lightFrame, 605, 0, sw - 605, sh, 28, 30, 36);
  const splitRect = Engine.detectSplitViewLeftPane(lightFrame);
  assert(Array.isArray(splitRect) && splitRect[2] >= 580 && splitRect[2] <= 605, `Expected Light Mode left pane width ~595, got ${JSON.stringify(splitRect)}`);
  console.log("   Test 31 PASSED");
}

// Test 32: Saturated colored dealer badge (green win / red bust) & 3-digit soft X/Y badge parsing
{
  console.log("32. Testing colored dealer badge & 3-digit soft X/Y badge parsing...");
  const D5X7 = {
    "1": ["..1..", ".11..", "..1..", "..1..", "..1..", "..1..", ".111."],
    "7": ["11111", "....1", "...1.", "..1..", ".1...", ".1...", ".1..."],
    "2": [".111.", "1...1", "....1", "..11.", ".1...", "1....", "11111"],
  };
  const drawDigit = (img, ch, ox, oy, scale = 2) => {
    const rows = D5X7[ch];
    for (let r = 0; r < rows.length; r++) {
      for (let c = 0; c < rows[r].length; c++) {
        if (rows[r][c] === "1") {
          fillRect(img, ox + c * scale, oy + r * scale, scale, scale, 250, 250, 250);
        }
      }
    }
  };
  // Bright green win pill (BGR: 45, 210, 50 -> gray ~143 >= 135, so dark-only check would fail without chroma support)
  const greenBadge = Engine.createImage(160, 60, 3, 25);
  fillRect(greenBadge, 10, 14, 58, 30, 45, 210, 50);
  drawDigit(greenBadge, "2", 18, 22, 2);
  drawDigit(greenBadge, "1", 32, 22, 2);
  const [gTot, gConf, gSoft] = Engine.detectDealerBadgeTotal(greenBadge, []);
  assert.strictEqual(gTot, 21, `Expected colored win badge total 21, got ${gTot}`);
  assert(gConf >= 0.56, `Expected badge confidence >= 0.56, got ${gConf}`);
  assert(gSoft && gSoft.total === 21 && gSoft.tensDigit === 2 && gSoft.onesDigit === 1, `Expected softHint metadata for 21, got ${JSON.stringify(gSoft)}`);

  // Soft 7/17 pill where '/' is dropped leaving 3 adjacent digits [7, 1, 7]
  const softBadge = Engine.createImage(160, 60, 3, 25);
  fillRect(softBadge, 6, 14, 72, 30, 20, 20, 20);
  drawDigit(softBadge, "7", 12, 22, 2);
  drawDigit(softBadge, "1", 26, 22, 2);
  drawDigit(softBadge, "7", 40, 22, 2);
  const [sTot, sConf, sSoft] = Engine.detectDealerBadgeTotal(softBadge, []);
  assert.strictEqual(sTot, 17, `Expected soft 7/17 badge total 17, got ${sTot}`);
  assert(sConf >= 0.56);
  assert(sSoft && sSoft.total === 17, `Expected softHint metadata for 17, got ${JSON.stringify(sSoft)}`);
  console.log("   Test 32 PASSED");
}

// Test 33: 10-value pair split (K, Q -> H0 draws 10, H1 has Q+5) and 3-hand re-split seeding
{
  console.log("33. Testing 10-value pair split & 3-hand re-split seeding...");
  const sm33 = new Engine.CompanionStateMachine(Object.assign({}, cfg, { confirm_frames: 2, clear_frames: 2 }));
  const mk33 = (rank, val, x, h = 0) => ({ rank, val, conf: 0.94, suit: "S", x, y: 10, w: 12, h: 16, hand_index: h });
  sm33.step({ ts: 1.0, banner_state: "WAIT", player_cards: [], dealer_cards: [], player_hands: {} });
  // Initial 10-value pair: K + Q
  const initPair = [mk33("K", 0, 20), mk33("Q", 0, 42)];
  const dUp = [mk33("6", 6, 10)];
  for (let f = 0; f < 2; f++) {
    sm33.step({ ts: 1.2 + f * 0.2, banner_state: "WAIT", player_cards: initPair, dealer_cards: dUp, player_hands: { 0: initPair } });
  }
  // Split into H0=[K, 10] and H1=[Q, 5] (H0 drew a 10-value card, must NOT be rejected by unseparatedOverlap!)
  const h0Split = [mk33("K", 0, 15, 0), mk33("10", 0, 35, 0)];
  const h1Split = [mk33("Q", 0, 120, 1), mk33("5", 5, 140, 1)];
  let splitEvts33 = [];
  for (let f = 0; f < 2; f++) {
    splitEvts33.push(
      ...sm33.step({
        ts: 2.0 + f * 0.2,
        banner_state: "WAIT",
        player_cards: [...h0Split, ...h1Split],
        dealer_cards: dUp,
        player_hands: { 0: h0Split, 1: h1Split },
        split_detected: true,
      })
    );
  }
  assert(splitEvts33.some((e) => e.type === "split" && e.handCount === 2), "10-value pair split (K+10, Q+5) must emit split");
  // Re-split into 3 hands: H0=[K, 10], H1=[Q, 5], H2=[10, 4]
  const h2Split = [mk33("10", 0, 210, 2), mk33("4", 4, 230, 2)];
  let resplitEvts = [];
  for (let f = 0; f < 2; f++) {
    resplitEvts.push(
      ...sm33.step({
        ts: 3.0 + f * 0.2,
        banner_state: "WAIT",
        player_cards: [...h0Split, ...h1Split, ...h2Split],
        dealer_cards: dUp,
        player_hands: { 0: h0Split, 1: h1Split, 2: h2Split },
        split_detected: true,
      })
    );
  }
  assert(resplitEvts.some((e) => e.type === "split" && e.handCount === 3), "3-hand re-split must emit split with handCount=3");
  assert(sm33._splitSeedCards.has(2), "3-hand re-split must seed hand 2 in _splitSeedCards");
  console.log("   Test 33 PASSED");
}

// Test 34: guessMissingDealerCard soft-hand intermediate draws, local shoe decrement & deterministic seed
{
  console.log("34. Testing guessMissingDealerCard soft-hand draw, local shoe decrement & deterministic seed...");
  // Soft 15 (A+4) with only '10's remaining: isValidStep must allow intermediate draw '10' (soft 15 -> hard 15)
  const onlyTens = [16, 0, 0, 0, 0, 0, 0, 0, 0, 0];
  const softStep10 = Engine.guessMissingDealerCard(["A", "4"], { shoeRem: onlyTens, roundSeed: 101 });
  assert(softStep10 && softStep10.rank === "10", `Expected soft A+4 with only 10s to allow intermediate "10", got ${JSON.stringify(softStep10)}`);
  assert.strictEqual(onlyTens[0], 16, "guessMissingDealerCard must not mutate caller shoeRem");

  // Soft 15 (A+4) with only '2's remaining -> draws '2' (soft 17)
  const onlyTwos = [0, 0, 4, 0, 0, 0, 0, 0, 0, 0];
  const softStep2 = Engine.guessMissingDealerCard(["A", "4"], { shoeRem: onlyTwos, roundSeed: 102 });
  assert(softStep2 && softStep2.rank === "2", `Expected soft A+4 with only 2s to draw "2", got ${JSON.stringify(softStep2)}`);

  // Deterministic reproducibility with identical roundSeed
  const gA = Engine.guessMissingDealerCard(["6"], { roundSeed: 777 });
  const gB = Engine.guessMissingDealerCard(["6"], { roundSeed: 777 });
  assert.deepStrictEqual(gA, gB, "Same roundSeed must produce deterministic guess");

  // State machine multi-card educated guess decrements local shoeRemCopy without mutating sync_shoe_rem
  const sm34 = new Engine.CompanionStateMachine(Object.assign({}, cfg, { confirm_frames: 2, clear_frames: 2 }));
  const scarceRem = [10, 0, 0, 0, 0, 1, 0, 0, 0, 0]; // 10 tens, only 1 five
  sm34.setSyncState({ play_mode: "watch", shoe_rem: scarceRem });
  const mk34 = (rank, val, x) => ({ rank, val, conf: 0.94, suit: "S", x, y: 10, w: 12, h: 16, hand_index: 0 });
  const p34 = [mk34("10", 0, 20), mk34("8", 8, 42)];
  const d34 = [mk34("2", 2, 10)];
  sm34.step({ ts: 1.0, banner_state: "NO_MORE_BETS", player_cards: [], dealer_cards: [], player_hands: {} });
  for (let f = 0; f < 2; f++) {
    sm34.step({ ts: 1.2 + f * 0.2, banner_state: "WAIT", player_cards: p34, dealer_cards: d34, player_hands: { 0: p34 } });
  }
  const pb34 = sm34.step({ ts: 2.0, banner_state: "PLACE_BETS", player_cards: [], dealer_cards: [], player_hands: {} });
  const guessed34 = pb34.filter((e) => e.type === "card" && e.target === "D" && e.source === "educated_guess").map((e) => e.rankLabel);
  assert(guessed34.length >= 2, `Expected >= 2 guessed cards from dealer '2', got ${JSON.stringify(guessed34)}`);
  assert(guessed34.filter((r) => r === "5").length <= 1, `Scarce rank '5' (rem=1) must not be guessed twice: ${JSON.stringify(guessed34)}`);
  assert.strictEqual(scarceRem[5], 1, "Caller shoe_rem must remain unmutated");
  console.log("   Test 34 PASSED");
}

// Test 35: pCleared && dCleared transitions state machine to ROUND_OVER so WatchKeepaliveManager can trigger
{
  console.log("35. Testing pCleared && dCleared transitions state to ROUND_OVER...");
  const sm35 = new Engine.CompanionStateMachine(Object.assign({}, cfg, { confirm_frames: 2, clear_frames: 2 }));
  const mk35 = (rank, val, x) => ({ rank, val, conf: 0.94, suit: "S", x, y: 10, w: 12, h: 16, hand_index: 0 });
  const p35 = [mk35("10", 0, 20), mk35("9", 9, 42)];
  const d35 = [mk35("10", 0, 10), mk35("8", 8, 34)];
  sm35.step({ ts: 1.0, banner_state: "WAIT", player_cards: [], dealer_cards: [], player_hands: {} });
  for (let f = 0; f < 2; f++) {
    sm35.step({ ts: 1.2 + f * 0.2, banner_state: "WAIT", player_cards: p35, dealer_cards: d35, player_hands: { 0: p35 } });
  }
  assert(sm35.state === "PLAYER_TURN" || sm35.state === "DEALER_TURN");
  // Wipe both strips for clear_frames without PLACE_BETS banner
  for (let f = 0; f < 2; f++) {
    sm35.step({ ts: 2.0 + f * 0.2, banner_state: "WAIT", player_cards: [], dealer_cards: [], player_hands: {} });
  }
  assert.strictEqual(sm35.state, "ROUND_OVER", `Expected state === "ROUND_OVER" after pCleared && dCleared, got ${sm35.state}`);
  console.log("   Test 35 PASSED");
}

// Test 36: companion_worker.js message round-trip (init -> worker_ready, ws_send -> ws_message_str, shutdown)
{
  console.log("36. Testing companion_worker.js message round-trip...");
  const fs = require("fs");
  const vm = require("vm");
  const workerSrc = fs.readFileSync(path.join(__dirname, "companion_worker.js"), "utf8");
  const workerPosted = [];
  let workerClosed = false;
  const workerSelf = {
    postMessage(msg) { workerPosted.push(msg); },
    close() { workerClosed = true; },
    onmessage: null,
  };
  const workerCtx = vm.createContext({
    self: workerSelf,
    globalThis: workerSelf,
    IpadCompanionEngine: Engine,
    DEFAULT_COMPANION_PACK: pack,
    importScripts: () => {},
    console,
    setTimeout,
    clearTimeout,
    Date,
    Math,
    Uint8Array,
    Uint32Array,
    Float32Array,
    Int32Array,
    ArrayBuffer,
    JSON,
    Object,
    Array,
    Map,
    Set,
    Number,
    String,
    Boolean,
    Promise,
  });
  vm.runInContext(workerSrc, workerCtx);
  assert.strictEqual(typeof workerSelf.onmessage, "function", "companion_worker.js must install self.onmessage");

  workerSelf.onmessage({ data: { type: "init", isNativeHost: false } });
  const readyEvt = workerPosted.find((m) => m.type === "worker_ready");
  assert(readyEvt && readyEvt.status && readyEvt.status.ipadMode === true, "Worker init must post worker_ready with status");

  workerSelf.onmessage({ data: { type: "ws_send", data: JSON.stringify({ type: "control", action: "pause" }) } });
  const statusStr = workerPosted.filter((m) => m.type === "ws_message_str").map((m) => JSON.parse(m.data)).pop();
  assert(statusStr && statusStr.type === "status" && statusStr.paused === true, "Worker ws_send pause must broadcast paused status");

  workerSelf.onmessage({ data: { type: "shutdown" } });
  assert.strictEqual(workerClosed, true, "Worker shutdown must call self.close()");
  console.log("   Test 36 PASSED");
}

// 37. Playtech loading screen detection, phantom card/split suppression & keepalive RECOVERING gate
{
  console.log("37. Testing Playtech loading screen detection & reload phantom-split suppression...");
  const full37 = Engine.createImage(1024, 711, 3, 18);
  fillRect(full37, 0, 0, 334, 76, 30, 30, 32);
  fillRect(full37, 0, 632, 334, 79, 28, 28, 30);
  fillRect(full37, 0, 76, 334, 556, 105, 70, 38);
  fillRect(full37, 105, 390, 120, 5, 12, 12, 12);
  fillRect(full37, 105, 390, 38, 5, 195, 215, 235);
  fillRect(full37, 96, 434, 86, 10, 240, 240, 240);
  for (let y = 426; y <= 452; y++) {
    for (let x = 192; x <= 218; x++) {
      if ((x - 205) * (x - 205) + (y - 439) * (y - 439) <= 169) {
        const di = (y * 1024 + x) * 3;
        full37.data[di] = 248; full37.data[di + 1] = 248; full37.data[di + 2] = 248;
      }
    }
  }
  fillRect(full37, 334, 0, 10, 711, 4, 4, 4);
  fillRect(full37, 344, 0, 680, 711, 24, 28, 36);

  const [vx37, vy37, vw37, vh37] = Engine.detectSplitViewLeftPane(full37);
  const vp37 = Engine.cropImage(full37, vx37, vy37, vw37, vh37);
  assert.strictEqual(Engine.detectLoadingScreen(vp37), true, "Must detect Playtech blurred-blue loading splash");
  assert.strictEqual(Engine.detectLoadingScreen(Engine.createImage(300, 400, 3, 245)), true, "Must detect uniform blank white flash");

  const live37 = Engine.createImage(600, 400, 3, 24);
  fillRect(live37, 0, 0, 600, 400, 24, 82, 30);
  fillRect(live37, 220, 260, 140, 60, 250, 250, 250);
  assert.strictEqual(Engine.detectLoadingScreen(live37), false, "Must NOT trigger on live green felt table");

  const det37 = new Engine.PlaytechDetector(cfg, pack);
  det37._ensureBankDecoded("ui");
  const loadingTpls37 = (det37.banks.ui || []).filter((t) => t && (t.rank === "LOADING" || (t.name && t.name.startsWith("LOADING_ALLBETS"))));
  assert.strictEqual(loadingTpls37.length, 1, "LOADING_ALLBETS_1.png must be loaded in detector.banks.ui");
  const nonBlue37 = Engine.createImage(325, 556, 3, 45);
  pasteImage(nonBlue37, loadingTpls37[0].bgr, 98, 206);
  assert.strictEqual(Engine.detectLoadingScreen(nonBlue37, loadingTpls37), true, "Must detect LOADING_ALLBETS_1.png template even on non-blue background");

  const prevHostHook = globalThis.__ipadCompanionSetHostInfo;
  const prevExtHook = globalThis.__ipadCompanionSetExtConnected;
  const rt37 = new Engine.IpadCompanionRuntime();
  globalThis.__ipadCompanionSetHostInfo = prevHostHook;
  globalThis.__ipadCompanionSetExtConnected = prevExtHook;
  rt37.keepalive.state = "REFRESHING";
  rt37.keepalive._refreshStartedTs = 100.0;
  const evs37 = rt37.ingestFrameBgr(full37, 104.0);
  assert(!evs37.some((e) => e.type === "card" || e.type === "split"), "Loading screen must not emit card or split events");
  assert.strictEqual(rt37.keepalive.state, "RECOVERING", "Keepalive must transition to RECOVERING and not falsely recover on loading screen");
  console.log("   Test 37 PASSED");
}

// Test 38: Dual-ROI physical shoe & movable discard-tray auto-new-shoe detection + undo cooldown
{
  console.log("38. Testing dual-ROI shoe_roi & movable discard_roi auto-new-shoe parity...");
  const mkTeal = (w, h) => {
    const im = Engine.createImage(w, h, 3, 0);
    // Playtech teal felt BGR ~ (125, 110, 42) -> HSV H~96, S~170, V~125
    fillRect(im, 0, 0, w, h, 125, 110, 42);
    return im;
  };

  // 1. Shoe PRESENT vs ABSENT vs OCCLUDED
  const shoePres = mkTeal(80, 60);
  fillRect(shoePres, 10, 8, 60, 46, 18, 18, 22);
  const rPres = Engine.detectShoeState(shoePres);
  assert.strictEqual(rPres.state, "PRESENT", `Expected PRESENT, got ${JSON.stringify(rPres)}`);

  const shoeAbs = mkTeal(80, 60);
  const rAbs = Engine.detectShoeState(shoeAbs);
  assert.strictEqual(rAbs.state, "ABSENT", `Expected ABSENT, got ${JSON.stringify(rAbs)}`);

  const armOcc = Engine.createImage(80, 60, 3, 0);
  fillRect(armOcc, 0, 0, 80, 60, 120, 150, 195);
  const rOcc = Engine.detectShoeState(armOcc);
  assert.strictEqual(rOcc.state, "OCCLUDED", `Expected OCCLUDED, got ${JSON.stringify(rOcc)}`);

  // 2. Position-invariant movable discard tray (left, center, right)
  const mkDisc = (trayX, withStack, housing = true) => {
    const im = mkTeal(120, 80);
    if (housing) {
      fillRect(im, trayX, 14, 34, 52, 20, 22, 26);
      if (withStack) fillRect(im, trayX + 5, 24, 24, 36, 232, 232, 235);
    }
    return im;
  };
  const dEmpty = Engine.detectDiscardStack(mkDisc(15, false, true));
  assert(dEmpty.whiteFrac <= 0.005 && dEmpty.darkFrac >= 0.12 && dEmpty.tealFrac >= 0.40);
  const stackFracs = [8, 42, 76].map((tx) => Engine.detectDiscardStack(mkDisc(tx, true, true)).whiteFrac);
  assert(stackFracs.every((v) => v >= 0.05), `Stack fractions too low: ${stackFracs}`);
  assert(Math.max(...stackFracs) - Math.min(...stackFracs) < 0.015, "Discard stack must be position-invariant");

  // 3. Primary shoe_swap cycle + undo cooldown
  const cfg38 = Engine.defaultConfig();
  const sm38 = new Engine.CompanionStateMachine(cfg38);
  sm38.setSyncState({ playMode: "bet", shoeRem: [30, 30, 30, 30, 30, 30, 30, 30, 30, 86] }); // 60 cards seen
  const obs38 = (ts, shoe_state, shoe_dark_frac = 0.65, shoe_teal_frac = 0.25, discW = 0.0, discD = 0.30, discT = 0.45) => ({
    ts, player_cards: [], dealer_cards: [], player_hands: {}, banner_state: "",
    shoe_state, shoe_dark_frac, shoe_teal_frac,
    discard_white_frac: discW, discard_dark_frac: discD, discard_teal_frac: discT,
  });
  sm38.step(obs38(0.0, "PRESENT"));
  sm38.step(obs38(2.5, "PRESENT"));
  sm38.step(obs38(10.0, "ABSENT", 0.05, 0.85));
  sm38.step(obs38(11.5, "OCCLUDED", 0.10, 0.10));
  sm38.step(obs38(11.9, "ABSENT", 0.05, 0.85));
  sm38.step(obs38(13.8, "ABSENT", 0.05, 0.85));
  sm38.step(obs38(15.0, "PRESENT"));
  const ev38 = sm38.step(obs38(16.7, "PRESENT"));
  const ns38 = ev38.find((e) => e.type === "new_shoe");
  assert(ns38 && ns38.reason === "shoe_swap" && ns38.cardsSeen === 60 && ns38.hasPendingBet === false);

  // Undo cooldown blocks immediate re-trigger
  sm38.onUndoNewShoe(17.0);
  sm38.step(obs38(20.0, "ABSENT", 0.05, 0.85));
  sm38.step(obs38(25.0, "ABSENT", 0.05, 0.85));
  assert.strictEqual(sm38.shoeTracker._shoeSwapArmed, false);

  // 4. Discard fallback + off-zone tray movement guard
  const tr38 = new Engine.ShoeSwapTracker(cfg38.shoe_detect);
  for (const t of [1, 2, 3, 4, 5]) {
    tr38.step(obs38(t, "OCCLUDED", 0.2, 0.2, 0.045, 0.30, 0.45), { cardsSeen: 90 });
  }
  // Tray moved off-zone (dark drops to 0.02) -> MUST NOT trigger
  for (const t of [10, 12, 14, 16, 18, 20]) {
    assert.strictEqual(tr38.step(obs38(t, "OCCLUDED", 0.2, 0.2, 0.001, 0.02, 0.85), { cardsSeen: 90 }), null);
  }
  // Emptied in-place (housing dark=0.25) -> triggers discard_cleared
  let fbReason = null;
  for (const t of [25, 26.5, 28, 29.5, 31, 32.5]) {
    const r = tr38.step(obs38(t, "OCCLUDED", 0.2, 0.2, 0.002, 0.25, 0.55), { cardsSeen: 90 });
    if (r) { fbReason = r; break; }
  }
  assert.strictEqual(fbReason, "discard_cleared");
  console.log("   Test 38 PASSED");
}

console.log("\nALL 38 ENGINE TESTS PASSED SUCCESSFULLY!");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});








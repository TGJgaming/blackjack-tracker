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
})().catch((err) => {
  console.error(err);
  process.exit(1);
});



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

  // Keepalive reload also triggers immediately when player & dealer strips are wiped in ROUND_OVER
  const ka20b = new Engine.WatchKeepaliveManager({ ...cfg, keepalive_interval_sec: 60 });
  ka20b._watchElapsedSec = 62;
  const kaEvWipe = ka20b.step({ now: 262, windowFound: true, obs: { banner_state: "", place_bets_conf: 0, player_cards: [], dealer_cards: [] }, smState: "ROUND_OVER", playMode: "watch", draftCount: 4, hasPrompt: false, betRoundsCount: 0, triggerRefreshCb: () => [true, "safari_ext"] });
  assert(kaEvWipe.some((e) => e.event === "refreshing"), "Keepalive must trigger immediately when both strips are wiped in ROUND_OVER");
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
  console.log("   Test 24 PASSED");
}




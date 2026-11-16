/**
 * Standalone iPadOS / Browser Companion Engine for Playtech All Bets Blackjack.
 *
 * Ports the Windows read-only pixel observation pipeline (win_anchor.py, detector.py,
 * state_machine.py, calibrate.py) to run 100% locally inside the iPad Split View
 * Tracker app (or WebWorker) with zero DOM/network injection into the Safari game tab.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    const defPack = (() => {
      try {
        return require("./default_pack.js").DEFAULT_COMPANION_PACK;
      } catch (_) {
        return null;
      }
    })();
    module.exports = factory(defPack);
  } else {
    root.IpadCompanionEngine = factory(root.DEFAULT_COMPANION_PACK || null);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (PRELOADED_PACK) {
  "use strict";

  const _ENGINE_SCRIPT_URL =
    typeof document !== "undefined" && document.currentScript && document.currentScript.src
      ? String(document.currentScript.src)
      : "";

  const RANK_TO_VAL = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 0,
    T: 0,
    J: 0,
    Q: 0,
    K: 0,
    A: 1,
    BACK: null,
    SHUFFLE: null,
  };

  const BANNER_TOKENS = ["NO_MORE_BETS", "PLACE_BETS", "LAST_BETS", "SHUFFLE", "WAIT"];
  const SUIT_TOKENS = new Set(["S", "H", "C", "D"]);
  const TOP_BANNER_SCALES = [
    0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15,
    1.21, 1.27, 1.33, 1.40, 1.48, 1.56, 1.65, 1.75, 1.85, 1.96, 2.08, 2.22, 2.38, 2.55,
  ];
  const PLACE_BETS_SCALES = [
    0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.12, 1.20,
    1.28, 1.36, 1.45, 1.56, 1.68, 1.80, 1.85, 1.95, 2.08, 2.25, 2.45,
  ];

  function normalizeRotationDeg(deg) {
    const n = Number(deg);
    if (!Number.isFinite(n)) return 0;
    const rounded = Math.round(n / 90) * 90;
    const mod = ((rounded % 360) + 360) % 360;
    return (mod === 90 || mod === 180 || mod === 270) ? mod : 0;
  }

  function normalizeRoi(coords) {
    if (!Array.isArray(coords) || coords.length !== 4) {
      throw new Error("Expected 4 ROI coordinates");
    }
    const rx = Math.max(0.0, Math.min(0.99, Number(coords[0]) || 0));
    const ry = Math.max(0.0, Math.min(0.99, Number(coords[1]) || 0));
    const rw = Math.max(0.01, Math.min(1.0 - rx, Number(coords[2]) || 0.01));
    const rh = Math.max(0.01, Math.min(1.0 - ry, Number(coords[3]) || 0.01));
    return [rx, ry, rw, rh];
  }

  function defaultConfig(override = null) {
    const base = Object.assign({}, (PRELOADED_PACK && PRELOADED_PACK.config) || {}, override || {});
    const roisRaw = Object.assign({}, (PRELOADED_PACK && PRELOADED_PACK.config && PRELOADED_PACK.config.rois) || {}, base.rois || {});
    const defRois = {
      player_strip: [0.42, 0.66, 0.32, 0.22],
      dealer_strip: [0.20, 0.34, 0.22, 0.27],
      community_roi: [0.44, 0.50, 0.21, 0.15],
      dealer_felt_roi: [0.38, 0.28, 0.24, 0.18],
      banner_roi: [0.35, 0.00, 0.30, 0.06],
      place_bets_roi: [0.38, 0.30, 0.24, 0.11],
      shuffle_roi: [0.30, 0.24, 0.40, 0.38],
    };
    const rois = {};
    for (const k of Object.keys(defRois)) {
      rois[k] = normalizeRoi(base[k] || roisRaw[k] || defRois[k]);
    }
    for (const k of Object.keys(roisRaw)) {
      if (!rois[k]) rois[k] = normalizeRoi(roisRaw[k]);
    }
    return {
      window_titles: base.window_titles || ["Live Games", "All Bets Blackjack", "epicworld", "Playtech"],
      anchor_mode: base.anchor_mode || "ipad_split_left",
      frame_rotation: normalizeRotationDeg(base.frame_rotation || 0),
      fps: Number(base.fps || 10.0),
      burst_fps: Number(base.burst_fps || 20.0),
      confirm_frames: Math.max(1, Number(base.confirm_frames ?? 3)),
      clear_frames: Math.max(2, Number(base.clear_frames ?? 4)),
      shuffle_hold_sec: Number(base.shuffle_hold_sec || 2.0),
      min_hud_confidence: Number(base.min_hud_confidence || 0.84),
      min_felt_confidence: Number(base.min_felt_confidence || 0.78),
      min_banner_confidence: Number(base.min_banner_confidence || 0.70),
      hud_nms_min_dx_ratio: Number(base.hud_nms_min_dx_ratio || 0.018),
      hud_dedup_window_sec: Number(base.hud_dedup_window_sec || 2.8),
      enable_felt_community: base.enable_felt_community !== false,
      enable_dealer_felt_fallback: base.enable_dealer_felt_fallback !== false,
      require_wait_banner: base.require_wait_banner !== false,
      pause_on_unmatched_felt: !!base.pause_on_unmatched_felt,
      min_auto_confidence: Number(base.min_auto_confidence || 0.70),
      player_strip: rois.player_strip,
      dealer_strip: rois.dealer_strip,
      community_roi: rois.community_roi,
      dealer_felt_roi: rois.dealer_felt_roi,
      banner_roi: rois.banner_roi,
      place_bets_roi: rois.place_bets_roi,
      shuffle_roi: rois.shuffle_roi,
      rois,
      felt_motion: Object.assign(
        {
          min_contour_area_ratio: 0.015,
          min_brightness: 145,
          pulse_cooldown_sec: 0.35,
          settle_timeout_sec: 0.50,
          angles_deg: [-15, -10, -5, 0, 5, 10, 15],
          scales: [0.75, 0.85, 0.92, 1.0, 1.08, 1.18, 1.30],
        },
        base.felt_motion || {}
      ),
      keepalive_enabled: base.keepalive_enabled !== undefined ? !!base.keepalive_enabled : true,
      keepalive_mode: base.keepalive_mode || "auto",
      keepalive_method: base.keepalive_method || "ext_then_os",
      keepalive_interval_sec: Number(base.keepalive_interval_sec || 600),
      keepalive_hard_warn_sec: Number(base.keepalive_hard_warn_sec || 750),
      keepalive_reload_grace_sec: Number(base.keepalive_reload_grace_sec || 12),
      keepalive_reset_on_bet_round: base.keepalive_reset_on_bet_round !== false,
      reload_btn_client_offset: base.reload_btn_client_offset || null,
      keepalive: Object.assign(
        {
          enabled: true,
          mode: "auto",
          method: "ext_then_os",
          prefer_left_window: true,
          min_watch_sec: 570.0,
          max_watch_sec: 630.0,
          hard_warn_sec: 750.0,
          round_over_delay_sec: 1.2,
          place_bets_min_delay_sec: 0.35,
          place_bets_max_delay_sec: 0.85,
          place_bets_latest_sec: 5.0,
          reload_grace_sec: 12.0,
          reload_timeout_sec: 20.0,
          appcommand_verify_sec: 1.2,
          reset_on_bet_round: true,
          reload_btn_offset: null,
        },
        base.keepalive || {}
      ),
    };
  }

  /* ==========================================================================
     1. Lightweight Typed-Array Image & Computer Vision Primitives
     ========================================================================== */

  function createImage(w, h, c = 3, data = null) {
    const width = Math.max(0, w | 0);
    const height = Math.max(0, h | 0);
    const channels = c | 0;
    let buf;
    if (typeof data === "number") {
      buf = new Uint8Array(width * height * channels);
      buf.fill(data);
    } else {
      buf = data || new Uint8Array(width * height * channels);
    }
    return { w: width, h: height, c: channels, data: buf };
  }

  function b64ToUint8(b64) {
    if (!b64) return new Uint8Array(0);
    const clean = b64.includes(",") ? b64.split(",")[1] : b64;
    if (typeof Buffer !== "undefined") {
      return new Uint8Array(Buffer.from(clean, "base64"));
    }
    const bin = atob(clean);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function uint8ToB64(u8) {
    if (typeof Buffer !== "undefined") {
      return Buffer.from(u8.buffer, u8.byteOffset, u8.byteLength).toString("base64");
    }
    let bin = "";
    const chunk = 0x8000;
    for (let i = 0; i < u8.length; i += chunk) {
      bin += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  function fromRGBA(rgba, w, h) {
    const width = Math.max(0, w | 0);
    const height = Math.max(0, h | 0);
    const expected = width * height * 4;
    if (!rgba || width === 0 || height === 0 || rgba.length < expected) {
      return createImage(0, 0, 3);
    }
    const out = new Uint8Array(width * height * 3);
    for (let i = 0, j = 0, n = width * height; i < n; i++, j += 3) {
      const p = i * 4;
      out[j] = rgba[p + 2];     // B
      out[j + 1] = rgba[p + 1]; // G
      out[j + 2] = rgba[p];     // R
    }
    return { w: width, h: height, c: 3, data: out };
  }

  function toGray(img) {
    if (!img || img.w === 0 || img.h === 0) return createImage(0, 0, 1);
    if (img.c === 1) return img;
    const n = img.w * img.h;
    const src = img.data;
    const dst = new Uint8Array(n);
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      // OpenCV BGR2GRAY: 0.114*B + 0.587*G + 0.299*R
      dst[i] = (29 * src[j] + 150 * src[j + 1] + 77 * src[j + 2] + 128) >> 8;
    }
    return { w: img.w, h: img.h, c: 1, data: dst };
  }

  function toMinGray(img) {
    if (!img || img.w === 0 || img.h === 0) return createImage(0, 0, 1);
    if (img.c === 1) return img;
    const n = img.w * img.h;
    const src = img.data;
    const dst = new Uint8Array(n);
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      const b = src[j], g = src[j + 1], r = src[j + 2];
      dst[i] = b < g ? (b < r ? b : r) : (g < r ? g : r);
    }
    return { w: img.w, h: img.h, c: 1, data: dst };
  }

  function grayToBGR(grayImg) {
    if (grayImg.c === 3) return grayImg;
    const n = grayImg.w * grayImg.h;
    const src = grayImg.data;
    const dst = new Uint8Array(n * 3);
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      const v = src[i];
      dst[j] = v;
      dst[j + 1] = v;
      dst[j + 2] = v;
    }
    return { w: grayImg.w, h: grayImg.h, c: 3, data: dst };
  }

  function cropImage(img, x0, y0, w, h) {
    if (!img || img.w === 0 || img.h === 0) return createImage(0, 0, img ? img.c : 3);
    const cx0 = Math.max(0, Math.min(img.w, x0 | 0));
    const cy0 = Math.max(0, Math.min(img.h, y0 | 0));
    const cx1 = Math.max(cx0, Math.min(img.w, (x0 + w) | 0));
    const cy1 = Math.max(cy0, Math.min(img.h, (y0 + h) | 0));
    const cw = cx1 - cx0;
    const ch = cy1 - cy0;
    if (cw <= 0 || ch <= 0) return createImage(0, 0, img.c);
    const c = img.c;
    const dst = new Uint8Array(cw * ch * c);
    const rowBytes = cw * c;
    const srcStride = img.w * c;
    for (let y = 0; y < ch; y++) {
      const sOff = (cy0 + y) * srcStride + cx0 * c;
      dst.set(img.data.subarray(sOff, sOff + rowBytes), y * rowBytes);
    }
    return { w: cw, h: ch, c, data: dst };
  }

  function cropNorm(img, normRoi) {
    if (!img || img.w === 0 || img.h === 0) return createImage(0, 0, img ? img.c : 3);
    const [rx, ry, rw, rh] = normRoi;
    const x0 = Math.max(0, Math.min(img.w - 2, Math.round(rx * img.w)));
    const y0 = Math.max(0, Math.min(img.h - 2, Math.round(ry * img.h)));
    const x1 = Math.max(x0 + 2, Math.min(img.w, Math.round((rx + rw) * img.w)));
    const y1 = Math.max(y0 + 2, Math.min(img.h, Math.round((ry + rh) * img.h)));
    return cropImage(img, x0, y0, x1 - x0, y1 - y0);
  }

  function resizeImage(img, dstW, dstH) {
    if (!img || img.w === 0 || img.h === 0 || dstW <= 0 || dstH <= 0) {
      return createImage(0, 0, img ? img.c : 1);
    }
    if (img.w === dstW && img.h === dstH) return img;
    const srcW = img.w, srcH = img.h, c = img.c;
    const src = img.data;
    const dst = new Uint8Array(dstW * dstH * c);
    const scaleX = srcW / dstW;
    const scaleY = srcH / dstH;

    for (let dy = 0; dy < dstH; dy++) {
      const fy = (dy + 0.5) * scaleY - 0.5;
      const sy0 = Math.max(0, Math.min(srcH - 1, Math.floor(fy)));
      const sy1 = Math.min(srcH - 1, sy0 + 1);
      const wy = Math.max(0, Math.min(1, fy - sy0));
      const wy0 = 1 - wy;
      for (let dx = 0; dx < dstW; dx++) {
        const fx = (dx + 0.5) * scaleX - 0.5;
        const sx0 = Math.max(0, Math.min(srcW - 1, Math.floor(fx)));
        const sx1 = Math.min(srcW - 1, sx0 + 1);
        const wx = Math.max(0, Math.min(1, fx - sx0));
        const wx0 = 1 - wx;
        const dIdx = (dy * dstW + dx) * c;
        const p00 = (sy0 * srcW + sx0) * c;
        const p01 = (sy0 * srcW + sx1) * c;
        const p10 = (sy1 * srcW + sx0) * c;
        const p11 = (sy1 * srcW + sx1) * c;
        for (let k = 0; k < c; k++) {
          const top = src[p00 + k] * wx0 + src[p01 + k] * wx;
          const bot = src[p10 + k] * wx0 + src[p11 + k] * wx;
          dst[dIdx + k] = Math.round(top * wy0 + bot * wy);
        }
      }
    }
    return { w: dstW, h: dstH, c, data: dst };
  }

  function rotate180(img) {
    const w = img.w, h = img.h, c = img.c;
    const dst = new Uint8Array(w * h * c);
    const n = w * h;
    for (let i = 0; i < n; i++) {
      const rev = n - 1 - i;
      for (let k = 0; k < c; k++) {
        dst[rev * c + k] = img.data[i * c + k];
      }
    }
    return { w, h, c, data: dst };
  }

  function rotateImage(img, deg) {
    if (!img || img.w === 0 || img.h === 0) return img;
    const d = normalizeRotationDeg(deg);
    if (d === 0) return img;
    if (d === 180) return rotate180(img);
    const w = img.w, h = img.h, c = img.c;
    const src = img.data;
    const dstW = h, dstH = w;
    const dst = new Uint8Array(dstW * dstH * c);
    if (d === 90) {
      if (c === 3) {
        for (let y = 0; y < h; y++) {
          const dx = h - 1 - y;
          let sOff = y * w * 3;
          for (let x = 0; x < w; x++, sOff += 3) {
            const dOff = (x * dstW + dx) * 3;
            dst[dOff] = src[sOff];
            dst[dOff + 1] = src[sOff + 1];
            dst[dOff + 2] = src[sOff + 2];
          }
        }
      } else if (c === 1) {
        for (let y = 0; y < h; y++) {
          const dx = h - 1 - y;
          const sRow = y * w;
          for (let x = 0; x < w; x++) {
            dst[x * dstW + dx] = src[sRow + x];
          }
        }
      } else {
        for (let y = 0; y < h; y++) {
          const dx = h - 1 - y;
          for (let x = 0; x < w; x++) {
            const sOff = (y * w + x) * c;
            const dOff = (x * dstW + dx) * c;
            for (let k = 0; k < c; k++) dst[dOff + k] = src[sOff + k];
          }
        }
      }
    } else {
      // d === 270 CW (90 CCW)
      if (c === 3) {
        for (let y = 0; y < h; y++) {
          const dx = y;
          let sOff = y * w * 3;
          for (let x = 0; x < w; x++, sOff += 3) {
            const dy = w - 1 - x;
            const dOff = (dy * dstW + dx) * 3;
            dst[dOff] = src[sOff];
            dst[dOff + 1] = src[sOff + 1];
            dst[dOff + 2] = src[sOff + 2];
          }
        }
      } else if (c === 1) {
        for (let y = 0; y < h; y++) {
          const dx = y;
          const sRow = y * w;
          for (let x = 0; x < w; x++) {
            dst[(w - 1 - x) * dstW + dx] = src[sRow + x];
          }
        }
      } else {
        for (let y = 0; y < h; y++) {
          const dx = y;
          for (let x = 0; x < w; x++) {
            const sOff = (y * w + x) * c;
            const dOff = ((w - 1 - x) * dstW + dx) * c;
            for (let k = 0; k < c; k++) dst[dOff + k] = src[sOff + k];
          }
        }
      }
    }
    return { w: dstW, h: dstH, c, data: dst };
  }

  function copyMakeBorder(img, pad, value = 255) {
    const nw = img.w + pad * 2;
    const nh = img.h + pad * 2;
    const c = img.c;
    const dst = new Uint8Array(nw * nh * c);
    dst.fill(value);
    for (let y = 0; y < img.h; y++) {
      const sOff = y * img.w * c;
      const dOff = ((y + pad) * nw + pad) * c;
      dst.set(img.data.subarray(sOff, sOff + img.w * c), dOff);
    }
    return { w: nw, h: nh, c, data: dst };
  }

  function arrayStats(u8) {
    const n = u8.length;
    if (n === 0) return { mean: 0, std: 0, min: 0, max: 0 };
    let sum = 0, sumSq = 0, min = 255, max = 0;
    for (let i = 0; i < n; i++) {
      const v = u8[i];
      sum += v;
      sumSq += v * v;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    const mean = sum / n;
    const variance = Math.max(0, sumSq / n - mean * mean);
    return { mean, std: Math.sqrt(variance), min, max };
  }

  function percentileU8(u8, q) {
    const n = u8.length;
    if (n === 0) return 0;
    const hist = new Int32Array(256);
    for (let i = 0; i < n; i++) hist[u8[i]]++;
    const target = (q / 100) * (n - 1);
    const loIdx = Math.floor(target);
    const hiIdx = Math.ceil(target);
    let cum = 0, vLo = 0, vHi = 0, foundLo = false;
    for (let v = 0; v < 256; v++) {
      cum += hist[v];
      if (!foundLo && cum > loIdx) {
        vLo = v;
        foundLo = true;
      }
      if (cum > hiIdx) {
        vHi = v;
        break;
      }
    }
    return vLo + (vHi - vLo) * (target - loIdx);
  }

  function computeTplZeroMean(tplGray) {
    const nTpl = tplGray.w * tplGray.h;
    const tData = tplGray.data;
    if (nTpl === 0) return { tZero: new Float32Array(0), tNorm: 0 };
    let tSum = 0;
    for (let i = 0; i < nTpl; i++) tSum += tData[i];
    const tMean = tSum / nTpl;
    const tZero = new Float32Array(nTpl);
    let tNormSq = 0;
    for (let i = 0; i < nTpl; i++) {
      const d = tData[i] - tMean;
      tZero[i] = d;
      tNormSq += d * d;
    }
    return { tZero, tNorm: Math.sqrt(tNormSq) };
  }

  function nextPow2(n) {
    let p = 1;
    while (p < n) p <<= 1;
    return p;
  }

  const _fftPlans = new Map();
  function getFftPlan(N) {
    let plan = _fftPlans.get(N);
    if (plan) return plan;
    const bits = Math.round(Math.log2(N));
    const rev = new Int32Array(N);
    for (let i = 0; i < N; i++) {
      let x = i, r = 0;
      for (let b = 0; b < bits; b++) {
        r = (r << 1) | (x & 1);
        x >>= 1;
      }
      rev[i] = r;
    }
    const cosT = new Float64Array(N >> 1);
    const sinT = new Float64Array(N >> 1);
    for (let i = 0; i < (N >> 1); i++) {
      const a = (-2 * Math.PI * i) / N;
      cosT[i] = Math.cos(a);
      sinT[i] = Math.sin(a);
    }
    plan = { N, rev, cosT, sinT };
    _fftPlans.set(N, plan);
    return plan;
  }

  function fft1d(re, im, plan, inverse) {
    const N = plan.N, rev = plan.rev, cosT = plan.cosT, sinT = plan.sinT;
    for (let i = 0; i < N; i++) {
      const j = rev[i];
      if (i < j) {
        let t = re[i]; re[i] = re[j]; re[j] = t;
        t = im[i]; im[i] = im[j]; im[j] = t;
      }
    }
    const sign = inverse ? -1 : 1;
    for (let len = 2; len <= N; len <<= 1) {
      const half = len >> 1;
      const step = N / len;
      if (len === 2) {
        for (let i = 0; i < N; i += 2) {
          const tr = re[i + 1], ti = im[i + 1];
          re[i + 1] = re[i] - tr; im[i + 1] = im[i] - ti;
          re[i] += tr; im[i] += ti;
        }
      } else if (len === 4) {
        for (let i = 0; i < N; i += 4) {
          const r0 = re[i], i0 = im[i], r2 = re[i + 2], i2 = im[i + 2];
          re[i + 2] = r0 - r2; im[i + 2] = i0 - i2;
          re[i] = r0 + r2; im[i] = i0 + i2;
          const r1 = re[i + 1], i1 = im[i + 1], r3 = re[i + 3], i3 = im[i + 3];
          const tr = sign * i3, ti = -sign * r3;
          re[i + 3] = r1 - tr; im[i + 3] = i1 - ti;
          re[i + 1] = r1 + tr; im[i + 1] = i1 + ti;
        }
      } else {
        for (let i = 0; i < N; i += len) {
          for (let j = 0, k = 0; j < half; j++, k += step) {
            const wr = cosT[k], wi = sign * sinT[k];
            const idx1 = i + j, idx2 = idx1 + half;
            const r2 = re[idx2], i2 = im[idx2];
            const tr = wr * r2 - wi * i2;
            const ti = wr * i2 + wi * r2;
            re[idx2] = re[idx1] - tr; im[idx2] = im[idx1] - ti;
            re[idx1] += tr; im[idx1] += ti;
          }
        }
      }
    }
    if (inverse) {
      const inv = 1 / N;
      for (let i = 0; i < N; i++) {
        re[i] *= inv;
        im[i] *= inv;
      }
    }
  }

  function rfft2RowsForward(re, im, planX, outRe, outIm, off0, off1, halfX) {
    fft1d(re, im, planX, false);
    const N = planX.N;
    outRe[off0] = re[0]; outIm[off0] = 0;
    outRe[off1] = im[0]; outIm[off1] = 0;
    for (let k = 1; k < halfX - 1; k++) {
      const rk = re[k], ik = im[k], rm = re[N - k], imk = im[N - k];
      outRe[off0 + k] = 0.5 * (rk + rm);
      outIm[off0 + k] = 0.5 * (ik - imk);
      outRe[off1 + k] = 0.5 * (ik + imk);
      outIm[off1 + k] = 0.5 * (rm - rk);
    }
    const mid = halfX - 1;
    outRe[off0 + mid] = re[mid]; outIm[off0 + mid] = 0;
    outRe[off1 + mid] = im[mid]; outIm[off1 + mid] = 0;
  }

  function rfft2RowsInverse(re, im, planX, inRe, inIm, off0, off1, halfX) {
    const N = planX.N;
    re[0] = inRe[off0];
    im[0] = inRe[off1];
    for (let k = 1; k < halfX - 1; k++) {
      const ar = inRe[off0 + k], ai = inIm[off0 + k];
      const br = inRe[off1 + k], bi = inIm[off1 + k];
      re[k] = ar - bi;
      im[k] = ai + br;
      re[N - k] = ar + bi;
      im[N - k] = br - ai;
    }
    const mid = halfX - 1;
    re[mid] = inRe[off0 + mid];
    im[mid] = inRe[off1 + mid];
    fft1d(re, im, planX, true);
  }

  function prepareSourceForMatch(srcGray, buildFft = false) {
    const sw = srcGray.w, sh = srcGray.h, d = srcGray.data;
    const iw = sw + 1;
    const sumS = new Float64Array(iw * (sh + 1));
    const sumSqS = new Float64Array(iw * (sh + 1));
    for (let y = 0; y < sh; y++) {
      let rowS = 0, rowSq = 0;
      const srcRow = y * sw, prevOff = y * iw, curOff = (y + 1) * iw;
      for (let x = 0; x < sw; x++) {
        const v = d[srcRow + x];
        rowS += v;
        rowSq += v * v;
        sumS[curOff + x + 1] = sumS[prevOff + x + 1] + rowS;
        sumSqS[curOff + x + 1] = sumSqS[prevOff + x + 1] + rowSq;
      }
    }
    const prep = { srcGray, sw, sh, iw, d, sumS, sumSqS, fftReady: false };
    if (buildFft) ensureSourceFft(prep);
    return prep;
  }

  function ensureSourceFft(prep) {
    if (prep.fftReady) return prep;
    const sw = prep.sw, sh = prep.sh, d = prep.d;
    const Fx = nextPow2(Math.max(2, sw));
    const Fy = nextPow2(Math.max(2, sh));
    const planX = getFftPlan(Fx);
    const planY = getFftPlan(Fy);
    const halfX = (Fx >> 1) + 1;
    const specStride = halfX;
    const specSize = specStride * Fy;
    const srcRe = new Float64Array(specSize);
    const srcIm = new Float64Array(specSize);
    const rowRe = new Float64Array(Fx), rowIm = new Float64Array(Fx);
    const colRe = new Float64Array(Fy), colIm = new Float64Array(Fy);

    for (let y = 0; y < sh; y += 2) {
      rowRe.fill(0);
      rowIm.fill(0);
      const sOff0 = y * sw;
      for (let x = 0; x < sw; x++) rowRe[x] = d[sOff0 + x];
      if (y + 1 < sh) {
        const sOff1 = (y + 1) * sw;
        for (let x = 0; x < sw; x++) rowIm[x] = d[sOff1 + x];
        rfft2RowsForward(rowRe, rowIm, planX, srcRe, srcIm, y * specStride, (y + 1) * specStride, halfX);
      } else {
        fft1d(rowRe, rowIm, planX, false);
        srcRe.set(rowRe.subarray(0, halfX), y * specStride);
        srcIm.set(rowIm.subarray(0, halfX), y * specStride);
      }
    }
    for (let x = 0; x < halfX; x++) {
      for (let y = 0; y < Fy; y++) {
        colRe[y] = srcRe[y * specStride + x];
        colIm[y] = srcIm[y * specStride + x];
      }
      fft1d(colRe, colIm, planY, false);
      for (let y = 0; y < Fy; y++) {
        srcRe[y * specStride + x] = colRe[y];
        srcIm[y * specStride + x] = colIm[y];
      }
    }

    prep.Fx = Fx;
    prep.Fy = Fy;
    prep.halfX = halfX;
    prep.specStride = specStride;
    prep.planX = planX;
    prep.planY = planY;
    prep.srcRe = srcRe;
    prep.srcIm = srcIm;
    prep.tplRe = new Float64Array(specSize);
    prep.tplIm = new Float64Array(specSize);
    prep.rowRe = rowRe;
    prep.rowIm = rowIm;
    prep.colRe = colRe;
    prep.colIm = colIm;
    prep.fftReady = true;
    return prep;
  }

  function matchSpatialIntegral(prep, tplGray, tZero, tNorm, maxXLimit = null, maxYLimit = null) {
    const { sw, sh, iw, d, sumS, sumSqS } = prep;
    const tw = tplGray.w, th = tplGray.h;
    const rw = sw - tw + 1;
    const rh = sh - th + 1;
    if (rw <= 0 || rh <= 0) {
      return { w: 0, h: 0, data: new Float32Array(0), maxVal: -1, maxX: 0, maxY: 0 };
    }
    const out = new Float32Array(rw * rh);
    if (tNorm < 1e-5) {
      return { w: rw, h: rh, data: out, maxVal: 0, maxX: 0, maxY: 0 };
    }
    const nTpl = tw * th;
    const yLim = maxYLimit !== null ? Math.min(rh, Math.max(0, maxYLimit | 0)) : rh;
    const xLim = maxXLimit !== null ? Math.min(rw, Math.max(0, maxXLimit | 0)) : rw;
    let maxVal = -Infinity;
    let maxX = 0, maxY = 0;
    for (let y = 0; y < yLim; y++) {
      const iTop = y * iw;
      const iBot = (y + th) * iw;
      const outOff = y * rw;
      for (let x = 0; x < xLim; x++) {
        let dot = 0;
        let tIdx = 0;
        for (let ty = 0; ty < th; ty++) {
          let sIdx = (y + ty) * sw + x;
          for (let tx = 0; tx < tw; tx++, sIdx++, tIdx++) {
            dot += d[sIdx] * tZero[tIdx];
          }
        }
        const x1 = x + tw;
        const pSum = sumS[iBot + x1] - sumS[iTop + x1] - sumS[iBot + x] + sumS[iTop + x];
        const pSumSq = sumSqS[iBot + x1] - sumSqS[iTop + x1] - sumSqS[iBot + x] + sumSqS[iTop + x];
        const pVar = pSumSq - (pSum * pSum) / nTpl;
        let corr = 0;
        if (pVar > 1e-5) {
          corr = dot / (Math.sqrt(pVar) * tNorm);
          if (corr > 1) corr = 1;
          else if (corr < -1) corr = -1;
        }
        out[outOff + x] = corr;
        if (corr > maxVal) {
          maxVal = corr;
          maxX = x;
          maxY = y;
        }
      }
    }
    if (maxVal === -Infinity) { maxVal = 0; }
    return { w: rw, h: rh, data: out, maxVal, maxX, maxY };
  }

  function matchFFTPacked(prep, tplGray, tZero, tNorm) {
    ensureSourceFft(prep);
    const { sw, sh, iw, sumS, sumSqS, Fx, Fy, halfX, specStride, planX, planY, srcRe, srcIm, tplRe, tplIm, rowRe, rowIm, colRe, colIm } = prep;
    const tw = tplGray.w, th = tplGray.h;
    const rw = sw - tw + 1;
    const rh = sh - th + 1;
    if (rw <= 0 || rh <= 0) {
      return { w: 0, h: 0, data: new Float32Array(0), maxVal: -1, maxX: 0, maxY: 0 };
    }
    const nTpl = tw * th;
    const out = new Float32Array(rw * rh);
    if (tNorm < 1e-5) {
      return { w: rw, h: rh, data: out, maxVal: 0, maxX: 0, maxY: 0 };
    }

    tplRe.fill(0);
    tplIm.fill(0);
    for (let ty = 0; ty < th; ty += 2) {
      rowRe.fill(0);
      rowIm.fill(0);
      const tOff0 = ty * tw;
      for (let tx = 0; tx < tw; tx++) rowRe[tx] = tZero[tOff0 + tx];
      if (ty + 1 < th) {
        const tOff1 = (ty + 1) * tw;
        for (let tx = 0; tx < tw; tx++) rowIm[tx] = tZero[tOff1 + tx];
        rfft2RowsForward(rowRe, rowIm, planX, tplRe, tplIm, ty * specStride, (ty + 1) * specStride, halfX);
      } else {
        fft1d(rowRe, rowIm, planX, false);
        tplRe.set(rowRe.subarray(0, halfX), ty * specStride);
        tplIm.set(rowIm.subarray(0, halfX), ty * specStride);
      }
    }
    for (let x = 0; x < halfX; x++) {
      for (let y = 0; y < Fy; y++) {
        colRe[y] = tplRe[y * specStride + x];
        colIm[y] = tplIm[y * specStride + x];
      }
      fft1d(colRe, colIm, planY, false);
      for (let y = 0; y < Fy; y++) {
        const idx = y * specStride + x;
        const sr = srcRe[idx], si = srcIm[idx], tr = colRe[y], ti = colIm[y];
        colRe[y] = sr * tr + si * ti;
        colIm[y] = si * tr - sr * ti;
      }
      fft1d(colRe, colIm, planY, true);
      for (let y = 0; y < rh; y++) {
        const idx = y * specStride + x;
        tplRe[idx] = colRe[y];
        tplIm[idx] = colIm[y];
      }
    }

    let maxVal = -Infinity;
    let maxX = 0, maxY = 0;
    for (let y = 0; y < rh; y += 2) {
      if (y + 1 < rh) {
        rfft2RowsInverse(rowRe, rowIm, planX, tplRe, tplIm, y * specStride, (y + 1) * specStride, halfX);
        for (let pair = 0; pair < 2; pair++) {
          const yy = y + pair;
          const corrArr = pair === 0 ? rowRe : rowIm;
          const iTop = yy * iw, iBot = (yy + th) * iw, outOff = yy * rw;
          for (let x = 0; x < rw; x++) {
            const x1 = x + tw;
            const pSum = sumS[iBot + x1] - sumS[iTop + x1] - sumS[iBot + x] + sumS[iTop + x];
            const pSumSq = sumSqS[iBot + x1] - sumSqS[iTop + x1] - sumSqS[iBot + x] + sumSqS[iTop + x];
            const pVar = pSumSq - (pSum * pSum) / nTpl;
            let corr = 0;
            if (pVar > 1e-5) {
              corr = corrArr[x] / (Math.sqrt(pVar) * tNorm);
              if (corr > 1) corr = 1;
              else if (corr < -1) corr = -1;
            }
            out[outOff + x] = corr;
            if (corr > maxVal) {
              maxVal = corr;
              maxX = x;
              maxY = yy;
            }
          }
        }
      } else {
        const rOff = y * specStride;
        rowRe[0] = tplRe[rOff];
        rowIm[0] = tplIm[rOff];
        for (let x = 1; x < halfX; x++) {
          const re = tplRe[rOff + x], im = tplIm[rOff + x];
          rowRe[x] = re;
          rowIm[x] = im;
          rowRe[Fx - x] = re;
          rowIm[Fx - x] = -im;
        }
        fft1d(rowRe, rowIm, planX, true);
        const iTop = y * iw, iBot = (y + th) * iw, outOff = y * rw;
        for (let x = 0; x < rw; x++) {
          const x1 = x + tw;
          const pSum = sumS[iBot + x1] - sumS[iTop + x1] - sumS[iBot + x] + sumS[iTop + x];
          const pSumSq = sumSqS[iBot + x1] - sumSqS[iTop + x1] - sumSqS[iBot + x] + sumSqS[iTop + x];
          const pVar = pSumSq - (pSum * pSum) / nTpl;
          let corr = 0;
          if (pVar > 1e-5) {
            corr = rowRe[x] / (Math.sqrt(pVar) * tNorm);
            if (corr > 1) corr = 1;
            else if (corr < -1) corr = -1;
          }
          out[outOff + x] = corr;
          if (corr > maxVal) {
            maxVal = corr;
            maxX = x;
            maxY = y;
          }
        }
      }
    }
    return { w: rw, h: rh, data: out, maxVal, maxX, maxY };
  }

  function matchPreparedTemplate(prep, tplGray, tZero, tNorm, maxXLimit = null, maxYLimit = null) {
    const tw = tplGray.w, th = tplGray.h;
    const rw = prep.sw - tw + 1;
    const rh = prep.sh - th + 1;
    if (rw <= 0 || rh <= 0) {
      return { w: 0, h: 0, data: new Float32Array(0), maxVal: -1, maxX: 0, maxY: 0 };
    }
    const effX = maxXLimit !== null ? Math.min(rw, Math.max(0, maxXLimit | 0)) : rw;
    const effY = maxYLimit !== null ? Math.min(rh, Math.max(0, maxYLimit | 0)) : rh;
    const work = effX * effY * tw * th;
    if (work > 12000 && prep.sw >= 16 && prep.sh >= 16 && tw >= 4 && th >= 4 && (maxXLimit === null && maxYLimit === null ? true : work > 90000)) {
      return matchFFTPacked(prep, tplGray, tZero, tNorm);
    }
    return matchSpatialIntegral(prep, tplGray, tZero, tNorm, maxXLimit, maxYLimit);
  }

  /**
   * Exact TM_CCOEFF_NORMED template matcher over 1-channel grayscale images.
   * Returns { w, h, data: Float32Array, maxVal, maxX, maxY }.
   */
  function matchTemplateCCoeffNormed(srcGray, tplGray) {
    const sw = srcGray.w, sh = srcGray.h;
    const tw = tplGray.w, th = tplGray.h;
    const rw = sw - tw + 1;
    const rh = sh - th + 1;
    if (rw <= 0 || rh <= 0) {
      return { w: 0, h: 0, data: new Float32Array(0), maxVal: -1, maxX: 0, maxY: 0 };
    }
    const { tZero, tNorm } = computeTplZeroMean(tplGray);
    if (tNorm < 1e-5) {
      return { w: rw, h: rh, data: new Float32Array(rw * rh), maxVal: 0, maxX: 0, maxY: 0 };
    }
    const work = rw * rh * tw * th;
    const useFft = work > 25000 && sw >= 16 && sh >= 16;
    const prep = prepareSourceForMatch(srcGray, useFft);
    return useFft
      ? matchFFTPacked(prep, tplGray, tZero, tNorm)
      : matchSpatialIntegral(prep, tplGray, tZero, tNorm);
  }

  /**
   * 8-connected components with bounding box + area stats (matches cv2.connectedComponentsWithStats).
   */
  function connectedComponentsWithStats(maskU8, w, h) {
    const labels = new Int32Array(w * h);
    const stats = [[0, 0, w, h, 0]]; // index 0 = background
    const q = new Int32Array(w * h);
    let curLabel = 0;

    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const idx = y * w + x;
        if (!maskU8[idx] || labels[idx] !== 0) continue;
        curLabel++;
        labels[idx] = curLabel;
        let head = 0, tail = 0;
        q[tail++] = idx;
        let minX = x, maxX = x, minY = y, maxY = y, area = 0;

        while (head < tail) {
          const cur = q[head++];
          const cy = (cur / w) | 0;
          const cx = cur - cy * w;
          area++;
          if (cx < minX) minX = cx;
          if (cx > maxX) maxX = cx;
          if (cy < minY) minY = cy;
          if (cy > maxY) maxY = cy;

          const y0 = cy > 0 ? cy - 1 : 0;
          const y1 = cy + 1 < h ? cy + 1 : h - 1;
          const x0 = cx > 0 ? cx - 1 : 0;
          const x1 = cx + 1 < w ? cx + 1 : w - 1;
          for (let ny = y0; ny <= y1; ny++) {
            const rowOff = ny * w;
            for (let nx = x0; nx <= x1; nx++) {
              const nIdx = rowOff + nx;
              if (maskU8[nIdx] && labels[nIdx] === 0) {
                labels[nIdx] = curLabel;
                q[tail++] = nIdx;
              }
            }
          }
        }
        stats.push([minX, minY, maxX - minX + 1, maxY - minY + 1, area]);
      }
    }
    return { count: curLabel + 1, labels, stats };
  }

  /* ==========================================================================
     2. Viewport & iPad Split-View Left-Pane Anchoring
     ========================================================================== */

  function detectGameViewportLocal(winBgr) {
    const w = winBgr.w, h = winBgr.h;
    if (h < 120 || w < 160) return [0, 0, w, h];

    const gray = toGray(winBgr);
    const cx0 = Math.floor(w * 0.14);
    const cx1 = Math.floor(w * 0.92);
    const bandW = Math.max(1, cx1 - cx0);

    const rowActive = new Uint8Array(h);
    for (let y = 0; y < h; y++) {
      let cnt = 0;
      const rOff = y * w;
      for (let x = cx0; x < cx1; x++) {
        if (gray.data[rOff + x] > 28) cnt++;
      }
      if (cnt / bandW > 0.14) rowActive[y] = 1;
    }

    const yMin = Math.floor(h * 0.14);
    const runs = [];
    let inRun = false;
    let startY = yMin;
    for (let y = yMin; y < h; y++) {
      if (rowActive[y] && !inRun) {
        inRun = true;
        startY = y;
      } else if (!rowActive[y] && inRun) {
        inRun = false;
        runs.push([startY, y]);
      }
    }
    if (inRun) runs.push([startY, h]);

    const merged = [];
    for (const r of runs) {
      if (merged.length && r[0] - merged[merged.length - 1][1] <= 18) {
        merged[merged.length - 1][1] = r[1];
      } else {
        merged.push([r[0], r[1]]);
      }
    }
    if (!merged.length) return [0, 0, w, h];

    let best = merged[0];
    for (const r of merged) {
      if (r[1] - r[0] > best[1] - best[0]) best = r;
    }
    let [y0, y1] = best;
    if (y1 - y0 < Math.floor(h * 0.25)) return [0, 0, w, h];

    const padLimit = Math.max(6, Math.min(14, Math.round(h * 0.015)));
    let steps = 0;
    while (steps < padLimit && y0 > yMin) {
      const rOff = (y0 - 1) * w;
      let gt90 = 0, gt22 = 0, rowMax = 0;
      for (let x = cx0; x < cx1; x++) {
        const v = gray.data[rOff + x];
        if (v > 90) gt90++;
        if (v > 22) gt22++;
        if (v > rowMax) rowMax = v;
      }
      if (gt90 / bandW >= 0.30) break;
      if (gt22 / bandW <= 0.004 && rowMax <= 32) {
        const lookTop = Math.max(yMin, y0 - Math.max(2, padLimit - steps));
        let anyBright = false;
        for (let ly = lookTop; ly < y0 - 1 && !anyBright; ly++) {
          const lOff = ly * w;
          for (let x = cx0; x < cx1; x++) {
            if (gray.data[lOff + x] > 80) {
              anyBright = true;
              break;
            }
          }
        }
        if (!anyBright) break;
      }
      y0--;
      steps++;
    }
    y0 = Math.max(yMin, y0 - 2);

    let x0 = 0;
    if (y0 - yMin >= 20) {
      const gapTop = Math.max(yMin, y0 - 45);
      const gapBot = Math.max(gapTop + 4, y0 - 6);
      const maxSideX = Math.floor(w * 0.16);
      let lastSideCol = -1;
      for (let x = 0; x < maxSideX; x++) {
        let activeCnt = 0;
        for (let y = gapTop; y < gapBot; y++) {
          if (gray.data[y * w + x] > 22) activeCnt++;
        }
        if (activeCnt / Math.max(1, gapBot - gapTop) > 0.5) lastSideCol = x;
      }
      if (lastSideCol >= 0 && lastSideCol + 1 < Math.floor(w * 0.14)) {
        x0 = lastSideCol + 1;
      }
    }

    return [x0, y0, Math.max(100, w - x0), Math.max(100, y1 - y0)];
  }

  function detectSplitViewLeftPane(winBgr, defaultSplitRatio = 0.65) {
    const w = winBgr.w, h = winBgr.h;
    if (h < 120 || w < 160) return [0, 0, w, h];

    const gray = toGray(winBgr);
    const searchX0 = Math.floor(w * 0.42);
    const searchX1 = Math.floor(w * 0.78);
    const yBand0 = Math.floor(h * 0.12);
    const yBand1 = Math.max(yBand0 + 16, Math.floor(h * 0.88));
    const bandH = yBand1 - yBand0;

    let splitW = w;
    if (searchX1 > searchX0 + 8) {
      const nCols = searchX1 - searchX0;
      const darkCols = new Uint8Array(nCols);
      const colBuf = new Uint8Array(bandH);
      for (let i = 0; i < nCols; i++) {
        const x = searchX0 + i;
        let sum = 0;
        for (let y = yBand0; y < yBand1; y++) {
          const v = gray.data[y * w + x];
          colBuf[y - yBand0] = v;
          sum += v;
        }
        const mean = sum / bandH;
        const p90 = percentileU8(colBuf, 90);
        if (p90 <= 22.0 && mean <= 14.0) darkCols[i] = 1;
      }
      let bestRun = null;
      let i = 0;
      while (i < nCols) {
        if (darkCols[i]) {
          let j = i;
          while (j < nCols && darkCols[j]) j++;
          if (j - i >= 4 && (!bestRun || j - i > bestRun[1] - bestRun[0])) {
            bestRun = [i, j];
          }
          i = j;
        } else {
          i++;
        }
      }
      if (bestRun) {
        splitW = Math.max(160, searchX0 + bestRun[0]);
      } else if (w / Math.max(1, h) >= 1.22) {
        splitW = Math.max(160, Math.round(w * defaultSplitRatio));
      }
    }

    const leftBgr = splitW < w ? cropImage(winBgr, 0, 0, splitW, h) : winBgr;
    const [vx, vy, vw, vh] = detectGameViewportLocal(leftBgr);
    return [vx, vy, Math.min(vw, splitW - vx), vh];
  }

  /* ==========================================================================
     3. Template Bank & Card / Suit / Banner / Felt Detection
     ========================================================================== */

  function longestRun(arr, minVal, bridge1px = false) {
    const n = arr.length;
    const runs = [];
    let i = 0;
    while (i < n) {
      if (arr[i] >= minVal) {
        let j = i;
        while (j < n) {
          if (arr[j] >= minVal) {
            j++;
          } else if (bridge1px && j + 1 < n && arr[j + 1] >= minVal && arr[j] >= minVal - 20.0) {
            j += 2;
          } else {
            break;
          }
        }
        runs.push([i, j]);
        i = j;
      } else {
        i++;
      }
    }
    if (!runs.length) return [0, n];
    runs.sort((a, b) => (b[1] - b[0]) - (a[1] - a[0]));
    return runs[0];
  }

  function extractFeltCardRect(bgr) {
    if (!bgr || bgr.w < 14 || bgr.h < 10) return bgr;
    const gray = toGray(bgr);
    const w = gray.w, h = gray.h;
    const thr = Math.max(165.0, percentileU8(gray.data, 85) - 35.0);

    const colP80 = new Float32Array(w);
    const colBuf = new Uint8Array(h);
    for (let x = 0; x < w; x++) {
      for (let y = 0; y < h; y++) colBuf[y] = gray.data[y * w + x];
      colP80[x] = percentileU8(colBuf, 80);
    }
    let [x0, x1] = longestRun(colP80, thr, false);
    if (x1 - x0 < 18) { x0 = 0; x1 = w; }

    const subW = x1 - x0;
    const rowP80 = new Float32Array(h);
    const rowBuf = new Uint8Array(subW);
    for (let y = 0; y < h; y++) {
      for (let x = x0; x < x1; x++) rowBuf[x - x0] = gray.data[y * w + x];
      rowP80[y] = percentileU8(rowBuf, 80);
    }
    let [y0, y1] = longestRun(rowP80, thr, true);
    if (y1 - y0 < 12) { y0 = 0; y1 = h; }

    return cropImage(bgr, x0, y0, x1 - x0, y1 - y0);
  }

  function extractFeltRankViews(bgr) {
    const card = extractFeltCardRect(bgr);
    if (!card || card.w < 10 || card.h < 8) return [null, null];
    const mg = toMinGray(card);
    const inner = cropImage(mg, 2, 2, Math.max(1, mg.w - 4), Math.max(1, mg.h - 4));
    if (inner.w === 0 || inner.h === 0) return [null, null];
    const p85 = percentileU8(inner.data, 85);
    const p10 = percentileU8(inner.data, 10);
    if (p85 < 185.0 || p85 - p10 < 55.0) return [null, null];
    const lo = percentileU8(mg.data, 5);
    const hi = percentileU8(mg.data, 85);
    if (hi - lo < 25.0) return [null, null];

    const norm = new Uint8Array(mg.data.length);
    const scale = 255.0 / Math.max(15.0, hi - lo);
    for (let i = 0; i < mg.data.length; i++) {
      norm[i] = Math.max(0, Math.min(255, Math.round((mg.data[i] - lo) * scale)));
    }
    const canon = resizeImage({ w: mg.w, h: mg.h, c: 1, data: norm }, 32, 20);
    const tl = cropImage(canon, 0, 0, 16, 10);
    const br = rotate180(cropImage(canon, 16, 10, 16, 10));
    return [tl, br];
  }

  function trimCardGlyph(cropBgr, bankName) {
    if (!cropBgr || cropBgr.w === 0 || cropBgr.h === 0) return cropBgr;
    const img = cropBgr.c === 1 ? grayToBGR(cropBgr) : cropBgr;
    const mb = toMinGray(img);
    const maskW = new Uint8Array(mb.data.length);
    for (let i = 0; i < mb.data.length; i++) maskW[i] = mb.data[i] > 155 ? 1 : 0;
    const ccW = connectedComponentsWithStats(maskW, img.w, img.h);
    let bestCard = null, bestArea = 0;
    for (let i = 1; i < ccW.count; i++) {
      const [x, y, w, h, area] = ccW.stats[i];
      if (h >= 14 && area > bestArea) {
        bestArea = area;
        bestCard = [x, y, w, h];
      }
    }
    let work = img;
    if (bestCard) {
      const [cx, cy, cw, ch] = bestCard;
      let card = cropImage(img, cx, cy, cw, ch);
      if (ch !== 45) {
        const sc = 45.0 / ch;
        card = resizeImage(card, Math.max(1, Math.round(cw * sc)), 45);
      }
      if (bankName === "hud") {
        work = cropImage(card, 0, 1, Math.min(card.w, 22), 18);
      } else if (bankName === "suit") {
        work = cropImage(card, 0, 28, Math.min(card.w, 22), 17);
      }
    }
    const wMb = toMinGray(work);
    const ink = new Uint8Array(wMb.data.length);
    for (let i = 0; i < wMb.data.length; i++) ink[i] = wMb.data[i] < 180 ? 1 : 0;
    const ccK = connectedComponentsWithStats(ink, work.w, work.h);
    if (ccK.count <= 1) return work;
    let maxA = 0;
    for (let i = 1; i < ccK.count; i++) {
      if (ccK.stats[i][4] > maxA) maxA = ccK.stats[i][4];
    }
    const keepLabels = new Uint8Array(ccK.count);
    for (let i = 1; i < ccK.count; i++) {
      const [, , bw, bh, area] = ccK.stats[i];
      if (bw <= 2 && bh >= Math.max(4, work.h - 4)) continue;
      if (area < Math.max(10, Math.floor(maxA * 0.28))) continue;
      keepLabels[i] = 1;
    }
    let minX = work.w, maxX = -1, minY = work.h, maxY = -1;
    const cleaned = new Uint8Array(work.w * work.h * 3);
    cleaned.fill(255);
    for (let y = 0; y < work.h; y++) {
      for (let x = 0; x < work.w; x++) {
        const idx = y * work.w + x;
        if (keepLabels[ccK.labels[idx]]) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
          const p = idx * 3;
          cleaned[p] = work.data[p];
          cleaned[p + 1] = work.data[p + 1];
          cleaned[p + 2] = work.data[p + 2];
        }
      }
    }
    if (maxX < minX || maxY < minY) return work;
    const sub = cropImage({ w: work.w, h: work.h, c: 3, data: cleaned }, minX, minY, maxX - minX + 1, maxY - minY + 1);
    return copyMakeBorder(sub, 1, 255);
  }

  function createTemplateEntry(bankName, name, bgrImg, dataB64 = "") {
    const stemUpper = name.replace(/\.png$/i, "").toUpperCase();
    const gray = toGray(bgrImg);
    const minGray = toMinGray(bgrImg);
    if (arrayStats(minGray.data).std < 1.0 && arrayStats(gray.data).std < 1.0) {
      return null;
    }

    let matchedBanner = null;
    for (const btok of BANNER_TOKENS) {
      if (stemUpper === btok || stemUpper.startsWith(btok + "_")) {
        matchedBanner = btok;
        break;
      }
    }

    const token = stemUpper.split("_")[0];
    let feltTl = null, feltBr = null;
    if (bankName === "felt" && bgrImg.w >= bgrImg.h && token !== "BACK") {
      const views = extractFeltRankViews(bgrImg);
      feltTl = views[0];
      feltBr = views[1];
    }

    const rank = bankName === "banner" && matchedBanner ? matchedBanner : token;
    const val = Object.prototype.hasOwnProperty.call(RANK_TO_VAL, rank) ? RANK_TO_VAL[rank] : null;
    return {
      bank: bankName,
      name,
      rank,
      val,
      matchedBanner,
      bgr: bgrImg,
      gray: bankName === "suit" ? minGray : gray,
      min_gray: minGray,
      felt_tl: feltTl,
      felt_br: feltBr,
      dataB64,
      _scaleCache: new Map(),
    };
  }

  function getScaledTplGray(tpl, sc, useMinGray = false) {
    const key = `g|${useMinGray ? 1 : 0}|${sc.toFixed(3)}`;
    if (tpl._scaleCache.has(key)) return tpl._scaleCache.get(key);
    const src = useMinGray && tpl.min_gray ? tpl.min_gray : tpl.gray;
    const sw = Math.max(4, Math.round(src.w * sc));
    const sh = Math.max(4, Math.round(src.h * sc));
    const scaled = sc === 1.0 ? src : resizeImage(src, sw, sh);
    if (arrayStats(scaled.data).std < 1.0) {
      tpl._scaleCache.set(key, null);
      return null;
    }
    const { tZero, tNorm } = computeTplZeroMean(scaled);
    if (tNorm < 1e-5) {
      tpl._scaleCache.set(key, null);
      return null;
    }
    const res = { scaled, sw, sh, tZero, tNorm };
    tpl._scaleCache.set(key, res);
    return res;
  }

  function hasLocalContrast(patchGray, darkOnWhite = false) {
    const st = arrayStats(patchGray.data);
    if (st.max - st.min < 65) return false;
    const n = patchGray.data.length;
    let dark = 0, bright = 0;
    const darkThr = darkOnWhite ? 115 : 95;
    for (let i = 0; i < n; i++) {
      const v = patchGray.data[i];
      if (v < darkThr) dark++;
      if (v > 150) bright++;
    }
    if (darkOnWhite) {
      return bright / n >= 0.22 && dark / n >= 0.020;
    }
    return dark / n >= 0.22 && bright / n >= 0.025;
  }

  function detectBestSingleWithScale(roiImg, templates, minConf, scales = [1.0], requireContrast = false, useMinGray = false) {
    if (!roiImg || roiImg.w === 0 || roiImg.h === 0 || !templates || !templates.length) {
      return [null, 0.0, null];
    }
    const gray = useMinGray ? toMinGray(roiImg) : toGray(roiImg);
    if (requireContrast) {
      const st = arrayStats(gray.data);
      const darkLim = useMinGray ? 115 : 95;
      if (st.max - st.min < 65 || st.max <= 150 || st.min >= darkLim) {
        return [null, 0.0, null];
      }
    }
    const prep = prepareSourceForMatch(gray, false);
    let bestTpl = null, bestConf = 0.0, bestScale = null;
    for (const tpl of templates) {
      for (const sc of scales) {
        const entry = getScaledTplGray(tpl, sc, useMinGray);
        if (!entry || entry.sw > gray.w || entry.sh > gray.h) continue;
        const m = matchPreparedTemplate(prep, entry.scaled, entry.tZero, entry.tNorm);
        if (!Number.isFinite(m.maxVal)) continue;
        if (requireContrast) {
          if (m.maxVal < minConf || m.maxVal <= bestConf) continue;
          const patch = cropImage(gray, m.maxX, m.maxY, entry.sw, entry.sh);
          if (!hasLocalContrast(patch, useMinGray)) continue;
        } else if (m.maxVal <= bestConf) {
          continue;
        }
        bestConf = m.maxVal;
        bestTpl = tpl;
        bestScale = sc;
      }
    }
    if (bestTpl && bestConf >= minConf) return [bestTpl, bestConf, bestScale];
    return [null, bestConf, null];
  }

  function findWhiteCardClusters(roiBgr) {
    if (!roiBgr || roiBgr.w === 0 || roiBgr.h === 0) return [];
    const mb = toMinGray(roiBgr);
    const mask = new Uint8Array(mb.data.length);
    for (let i = 0; i < mb.data.length; i++) mask[i] = mb.data[i] > 155 ? 1 : 0;
    const cc = connectedComponentsWithStats(mask, mb.w, mb.h);
    const raw = [];
    const caps = [];

    for (let i = 1; i < cc.count; i++) {
      const [x, y, w, h, area] = cc.stats[i];
      const fill = area / Math.max(1, w * h);
      const patch = cropImage(mb, x, y, w, h);
      let gt160 = 0, gt165 = 0;
      for (let k = 0; k < patch.data.length; k++) {
        if (patch.data[k] > 160) gt160++;
        if (patch.data[k] > 165) gt165++;
      }
      if (h < 13 || area < 105) {
        if (h >= 8 && h <= 24 && w >= 5 && area >= 36 && fill >= 0.25 && gt160 / patch.data.length >= 0.25) {
          caps.push([x, y, w, h]);
        }
        continue;
      }
      if ((w / Math.max(1, h) > 4.0 && fill < 0.45) || fill < 0.19) continue;
      if (gt165 / patch.data.length < 0.25) continue;
      raw.push([x, y, w, h]);
    }
    if (!raw.length) return [];
    raw.sort((a, b) => a[0] - b[0]);
    const merged = [raw[0]];
    for (let i = 1; i < raw.length; i++) {
      const [x, y, w, h] = raw[i];
      const [px, py, pw, ph] = merged[merged.length - 1];
      const gap = x - (px + pw);
      const vOv = Math.min(py + ph, y + h) - Math.max(py, y);
      if (gap <= 2 && vOv >= Math.round(0.55 * Math.min(ph, h))) {
        const nx = Math.min(px, x);
        const ny = Math.min(py, y);
        const nr = Math.max(px + pw, x + w);
        const nb = Math.max(py + ph, y + h);
        merged[merged.length - 1] = [nx, ny, nr - nx, nb - ny];
      } else {
        merged.push([x, y, w, h]);
      }
    }
    for (const [cx, cy, cw, ch] of caps) {
      for (let idx = 0; idx < merged.length; idx++) {
        const [px, py, pw, ph] = merged[idx];
        const gap = px - (cx + cw);
        if (gap >= -2 && gap <= 4 && Math.abs(cy - py) <= 5 && ch < ph) {
          const nx = Math.min(cx, px);
          const ny = Math.min(cy, py);
          const nr = Math.max(cx + cw, px + pw);
          const nb = Math.max(cy + ch, py + ph);
          merged[idx] = [nx, ny, nr - nx, nb - ny];
          break;
        }
      }
    }
    return merged;
  }

  function hasRedBackSignature(bgrPatch) {
    if (!bgrPatch || bgrPatch.c !== 3 || bgrPatch.w === 0 || bgrPatch.h === 0) return false;
    const n = bgrPatch.w * bgrPatch.h;
    const d = bgrPatch.data;
    let redCnt = 0, dotCnt = 0;
    for (let i = 0, j = 0; i < n; i++, j += 3) {
      const b = d[j], g = d[j + 1], r = d[j + 2];
      if (r - Math.max(b, g) > 32) redCnt++;
      if (Math.min(b, g, r) > 150) dotCnt++;
    }
    return redCnt / n >= 0.24 && dotCnt / n >= 0.10;
  }

  function classifySuit(normBgr, rankX, rankW, cwNorm, suitTemplates) {
    if (!normBgr || normBgr.c !== 3 || !suitTemplates || !suitTemplates.length) return [null, 0.0];
    const ix0 = Math.max(0, rankX);
    const ix1 = Math.min(cwNorm, rankX + Math.max(rankW, 12));
    const x0 = Math.max(0, rankX - 3);
    const x1 = Math.min(cwNorm, rankX + Math.max(rankW, 16) + 3);
    const inkBot = cropImage(normBgr, ix0, 27, ix1 - ix0, 18);
    const inkTop = cropImage(normBgr, ix0, 1, ix1 - ix0, 18);
    const suitZone = cropImage(normBgr, x0, 26, x1 - x0, 19);
    if (suitZone.w === 0 || inkBot.w === 0) return [null, 0.0];

    function inkCounts(patch) {
      const n = patch.w * patch.h;
      let redPx = 0, inkPx = 0;
      for (let i = 0, j = 0; i < n; i++, j += 3) {
        const b = patch.data[j], g = patch.data[j + 1], r = patch.data[j + 2];
        if (r - Math.max(b, g) > 32) redPx++;
        if (Math.min(b, g, r) < 175) inkPx++;
      }
      return [redPx, inkPx];
    }

    const [rb, ib] = inkCounts(inkBot);
    const [rt, it] = inkCounts(inkTop);
    const isRed = (rb >= 8 && rb / Math.max(1, ib) >= 0.42) || (rb + rt >= 12 && (rb + rt) / Math.max(1, ib + it) >= 0.42);
    const allowed = isRed ? new Set(["H", "D"]) : new Set(["S", "C"]);
    const filtered = suitTemplates.filter((t) => allowed.has(t.rank));
    const activeTpls = filtered.length ? filtered : suitTemplates;

    const suitMin = toMinGray(suitZone);
    const suitPrep = prepareSourceForMatch(suitMin, false);
    let bestSuit = null, bestConf = 0.0;
    for (const tpl of activeTpls) {
      for (const sc of [0.88, 0.96, 1.0, 1.06, 1.12]) {
        const entry = getScaledTplGray(tpl, sc, true);
        if (!entry || entry.sw > suitMin.w || entry.sh > suitMin.h) continue;
        const m = matchPreparedTemplate(suitPrep, entry.scaled, entry.tZero, entry.tNorm);
        if (Number.isFinite(m.maxVal) && m.maxVal > bestConf) {
          bestConf = m.maxVal;
          bestSuit = tpl.rank;
        }
      }
    }
    return [bestConf >= 0.56 ? bestSuit : null, bestConf];
  }

  function detectClusterCards(roiBgr, cluster, rankTemplates, suitTemplates, floorConf, isDealer, minHudConf = 0.84, refCh = null) {
    let [cx, cy, cw, ch] = cluster;
    if (refCh !== null && ch < 0.85 * refCh) {
      ch = Math.max(ch, Math.min(roiBgr.h - cy, Math.round(refCh)));
    }
    const edgeClipped = cx + cw >= roiBgr.w - 1;
    let clusterBgr = cropImage(roiBgr, cx, cy, cw, ch);
    if (clusterBgr.w === 0 || clusterBgr.h === 0 || !rankTemplates.length) return [[], false];

    const clMb = toMinGray(clusterBgr);
    const whiteRows = [];
    for (let y = 0; y < clMb.h; y++) {
      let wCnt = 0;
      for (let x = 0; x < clMb.w; x++) {
        if (clMb.data[y * clMb.w + x] > 155) wCnt++;
      }
      if (wCnt / clMb.w >= 0.12) whiteRows.push(y);
    }
    if (whiteRows.length >= 13 && whiteRows[whiteRows.length - 1] + 1 - whiteRows[0] >= Math.round(ch * 0.82)) {
      const yOff = whiteRows[0];
      const yEnd = whiteRows[whiteRows.length - 1] + 1;
      clusterBgr = cropImage(clusterBgr, 0, yOff, cw, yEnd - yOff);
      cy += yOff;
      ch = yEnd - yOff;
    }

    const sc = 45.0 / ch;
    const cwNorm = Math.max(1, Math.round(cw * sc));
    const normBgr = ch === 45 ? clusterBgr : resizeImage(clusterBgr, cwNorm, 45);
    const normTop = toMinGray(cropImage(normBgr, 0, 0, cwNorm, 23));

    let effRight = cwNorm;
    let clusterHasBack = false;
    if (isDealer && cwNorm >= 28) {
      for (let col = 22; col < cwNorm - 5; col++) {
        const sub = cropImage(normBgr, col, 10, 6, 26);
        if (hasRedBackSignature(sub)) {
          effRight = col;
          clusterHasBack = true;
          break;
        }
      }
    }
    if (effRight < 14) return [[], clusterHasBack];

    const topPrep = prepareSourceForMatch(normTop, false);
    const minTail = !clusterHasBack && !edgeClipped ? 14 : 12;
    const peaks = [];
    for (const tpl of rankTemplates) {
      for (const tsc of [0.90, 0.96, 1.0, 1.05, 1.10]) {
        const entry = getScaledTplGray(tpl, tsc, true);
        if (!entry) continue;
        const { scaled, sw, sh, tZero, tNorm } = entry;
        if (sw > normTop.w || sh > normTop.h) continue;
        const maxXForTpl = effRight - Math.max(minTail, sw);
        if (maxXForTpl < 0) continue;
        const m = matchPreparedTemplate(topPrep, scaled, tZero, tNorm, maxXForTpl + 1, 9);
        const yLim = Math.min(9, m.h);
        const xLim = Math.min(maxXForTpl, m.w - 1);
        for (let yPos = 0; yPos < yLim; yPos++) {
          for (let xPos = 0; xPos <= xLim; xPos++) {
            const conf = m.data[yPos * m.w + xPos];
            if (conf < floorConf || !Number.isFinite(conf)) continue;
            let pMin = 255, pMax = 0, gt158 = 0, lt120 = 0;
            const pData = normTop.data;
            const ntW = normTop.w;
            for (let py = 0; py < sh; py++) {
              let pOff = (yPos + py) * ntW + xPos;
              for (let px = 0; px < sw; px++, pOff++) {
                const v = pData[pOff];
                if (v < pMin) pMin = v;
                if (v > pMax) pMax = v;
                if (v > 158) gt158++;
                if (v < 120) lt120++;
              }
            }
            if (pMax - pMin < 70) continue;
            const nPatch = sw * sh;
            if (gt158 / nPatch < 0.26 || lt120 / nPatch < 0.07) continue;
            const prio = conf + (tpl.rank === "10" ? 0.015 : 0.0);
            peaks.push([
              prio,
              { rank: tpl.rank, val: tpl.val, conf, x: xPos, y: yPos, w: sw, h: sh, suit: null, hand_index: 0 },
            ]);
          }
        }
      }
    }

    peaks.sort((a, b) => b[0] - a[0]);
    const kept = [];
    for (const [, cand] of peaks) {
      if (kept.some((k) => Math.abs(cand.x - k.x) < 11)) continue;
      kept.push(cand);
    }
    kept.sort((a, b) => a.x - b.x);
    if (!kept.length) return [[], clusterHasBack];

    const out = [];
    for (const d of kept) {
      const [suit, suitConf] = classifySuit(normBgr, d.x, d.w, cwNorm, suitTemplates);
      if (suitTemplates && suitTemplates.length) {
        const sx0 = Math.max(0, d.x - 2);
        const sx1 = Math.min(cwNorm, d.x + Math.max(d.w, 14) + 2);
        const sz = toMinGray(cropImage(normBgr, sx0, 26, sx1 - sx0, 19));
        let gt155 = 0, lt145 = 0, lt140 = 0;
        for (let k = 0; k < sz.data.length; k++) {
          if (sz.data[k] > 155) gt155++;
          if (sz.data[k] < 145) lt145++;
          if (sz.data[k] < 140) lt140++;
        }
        const hasPipBody = sz.data.length > 0 && gt155 / sz.data.length >= 0.30 && lt145 >= 5;
        const hasBadgeOcclusion = !isDealer && d.x <= 9 && sz.data.length > 0 && lt140 / sz.data.length >= 0.25 && d.conf >= Math.max(0.75, floorConf);
        if (!clusterHasBack && !edgeClipped && !hasBadgeOcclusion) {
          const remRight = effRight - d.x;
          if (remRight < 15 && (d.conf < Math.max(0.84, minHudConf) || (!suit && !hasPipBody))) continue;
          if (remRight < 22 && d.conf < Math.max(0.78, minHudConf - 0.06)) continue;
        }
        if (!suit && !hasBadgeOcclusion && (!hasPipBody || suitConf < 0.42 || d.conf < Math.max(0.82, minHudConf - 0.02))) {
          continue;
        }
      }
      out.push({
        rank: d.rank,
        val: d.val,
        conf: d.conf,
        x: cx + Math.round(d.x / sc),
        y: cy + Math.round(d.y / sc),
        w: Math.max(4, Math.round(d.w / sc)),
        h: Math.max(4, Math.round(d.h / sc)),
        suit,
        hand_index: 0,
      });
    }
    return [out, clusterHasBack];
  }

  function computeRoiSignature(img, gridW, gridH, useMinGray = false) {
    if (!img || img.w === 0 || img.h === 0) {
      return { w: 0, h: 0, gw: 0, gh: 0, bins: new Uint8Array(0) };
    }
    const w = img.w, h = img.h, c = img.c, src = img.data;
    const gw = Math.max(1, Math.min(gridW, w));
    const gh = Math.max(1, Math.min(gridH, h));
    const nBins = gw * gh;
    const sums = new Int32Array(nBins);
    const cnts = new Int32Array(nBins);
    const step = (w * h > 65000) ? 2 : 1;
    for (let y = 0; y < h; y += step) {
      const gy = Math.min(gh - 1, ((y * gh) / h) | 0);
      const binRow = gy * gw;
      const pxRow = y * w;
      if (c === 1) {
        for (let x = 0; x < w; x += step) {
          const gx = Math.min(gw - 1, ((x * gw) / w) | 0);
          const bIdx = binRow + gx;
          sums[bIdx] += src[pxRow + x];
          cnts[bIdx]++;
        }
      } else if (useMinGray) {
        for (let x = 0; x < w; x += step) {
          const p = (pxRow + x) * 3;
          const b = src[p], g = src[p + 1], r = src[p + 2];
          const v = b < g ? (b < r ? b : r) : (g < r ? g : r);
          const gx = Math.min(gw - 1, ((x * gw) / w) | 0);
          const bIdx = binRow + gx;
          sums[bIdx] += v;
          cnts[bIdx]++;
        }
      } else {
        for (let x = 0; x < w; x += step) {
          const p = (pxRow + x) * 3;
          const v = (29 * src[p] + 150 * src[p + 1] + 77 * src[p + 2] + 128) >> 8;
          const gx = Math.min(gw - 1, ((x * gw) / w) | 0);
          const bIdx = binRow + gx;
          sums[bIdx] += v;
          cnts[bIdx]++;
        }
      }
    }
    const bins = new Uint8Array(nBins);
    for (let i = 0; i < nBins; i++) {
      bins[i] = cnts[i] > 0 ? Math.round(sums[i] / cnts[i]) : 0;
    }
    return { w, h, gw, gh, bins };
  }

  function roiSigDiff(sigA, sigB) {
    if (!sigA || !sigB) return 255;
    if (sigA.w !== sigB.w || sigA.h !== sigB.h || sigA.gw !== sigB.gw || sigA.gh !== sigB.gh) {
      return 255;
    }
    const a = sigA.bins, b = sigB.bins, n = a.length;
    let maxD = 0;
    for (let i = 0; i < n; i++) {
      const d = a[i] - b[i];
      const ad = d < 0 ? -d : d;
      if (ad > maxD) maxD = ad;
    }
    return maxD;
  }

  class PlaytechDetector {
    constructor(cfg, initialPack = null) {
      this.cfg = cfg;
      this.banks = { hud: [], felt: [], banner: [], suit: [], ui: [] };
      this._bannerTplHints = new Map();
      this.invalidateCaches();
      if (initialPack) this.loadPack(initialPack);
    }

    invalidateCaches() {
      this._bannerTplHints.clear();
      this._lastPSig = null;
      this._cachedP = [[], {}, false];
      this._pSigAge = 0;
      this._lastDSig = null;
      this._cachedD = [];
      this._dSigAge = 0;
      this._lastBannerSig = null;
      this._cachedTopBanner = ["", 0.0];
      this._bannerSigAge = 0;
      this._lastShuffleSig = null;
      this._lastShufMode = "";
      this._cachedShuf = ["", 0.0];
      this._shufSigAge = 0;
      this._lastShuffleScanTs = null;
      this._lastPbSig = null;
      this._cachedPb = 0.0;
      this._pbSigAge = 0;
    }

    loadPack(pack) {
      this.banks = { hud: [], felt: [], banner: [], suit: [], ui: [] };
      this.invalidateCaches();
      if (!pack || !pack.templates) return;
      for (const bank of ["hud", "felt", "banner", "suit", "ui"]) {
        const items = pack.templates[bank] || [];
        for (const item of items) {
          if (item.bgrB64 && item.w && item.h) {
            const bgr = { w: item.w, h: item.h, c: 3, data: b64ToUint8(item.bgrB64) };
            const entry = createTemplateEntry(bank, item.name, bgr, item.dataB64 || "");
            if (entry) this.banks[bank].push(entry);
          } else if (item.bgr && item.bgr.data) {
            const entry = createTemplateEntry(bank, item.name, item.bgr, item.dataB64 || "");
            if (entry) this.banks[bank].push(entry);
          }
        }
      }
    }

    listTemplateCounts() {
      const summary = { hud: {}, felt: {}, banner: {}, suit: {}, ui: {} };
      for (const bank of Object.keys(summary)) {
        for (const t of this.banks[bank] || []) {
          summary[bank][t.rank] = (summary[bank][t.rank] || 0) + 1;
        }
      }
      return summary;
    }

    saveTemplateCrop(bankName, rank, cropBgr, autoTrim = false) {
      const rankUp = String(rank || "").trim().toUpperCase();
      if (!["hud", "felt", "banner", "suit", "ui"].includes(bankName)) {
        throw new Error(`Unsupported template bank: ${bankName}`);
      }
      if (!cropBgr || cropBgr.w === 0 || cropBgr.h === 0 || arrayStats(cropBgr.data).std < 1.0) {
        throw new Error("Selected crop has no contrast (blank region)");
      }
      let outImg = autoTrim && (bankName === "hud" || bankName === "suit") ? trimCardGlyph(cropBgr, bankName) : cropBgr;
      if (bankName === "hud") {
        outImg = grayToBGR(toMinGray(outImg));
      } else if (bankName === "banner" || bankName === "ui") {
        outImg = grayToBGR(toGray(outImg));
      }
      const existing = new Set((this.banks[bankName] || []).map((t) => t.name));
      let idx = 1;
      while (existing.has(`${rankUp}_${idx}.png`)) idx++;
      const filename = `${rankUp}_${idx}.png`;
      const entry = createTemplateEntry(bankName, filename, outImg, "");
      if (!entry) throw new Error("Selected crop has insufficient contrast");
      this.banks[bankName].push(entry);
      this.invalidateCaches();
      return { filename, w: outImg.w, h: outImg.h, entry };
    }

    deleteRankTemplates(bankName, rank) {
      const tok = String(rank || "").trim().toUpperCase();
      const list = this.banks[bankName] || [];
      const kept = list.filter((t) => t.rank !== tok);
      const removed = list.length - kept.length;
      this.banks[bankName] = kept;
      this.invalidateCaches();
      return removed;
    }

    clearBankTemplates(bankName) {
      const list = this.banks[bankName] || [];
      const removed = list.length;
      this.banks[bankName] = [];
      this.invalidateCaches();
      return removed;
    }

    exportPack() {
      const templates = {};
      for (const bank of ["hud", "felt", "banner", "suit", "ui"]) {
        templates[bank] = (this.banks[bank] || []).map((t) => ({
          name: t.name,
          rank: t.rank,
          w: t.bgr.w,
          h: t.bgr.h,
          bgrB64: uint8ToB64(t.bgr.data),
          dataB64: t.dataB64 || "",
        }));
      }
      return {
        version: 1,
        exportedAt: Math.floor(Date.now() / 1000),
        config: JSON.parse(JSON.stringify(this.cfg)),
        templates,
      };
    }

    _detectBannerWithHint(probeImg, templates, minConf, scales, hintKey = "banner") {
      if (!templates || !templates.length || !probeImg || probeImg.w === 0 || probeImg.h === 0) {
        return [null, 0.0];
      }
      const cached = this._bannerTplHints.get(hintKey);
      if (cached && scales.length > 6) {
        const [hintName, hintScale] = cached;
        const hintTpls = templates.filter((t) => t.name === hintName);
        const fastScales = scales.filter((s) => Math.abs(s - hintScale) <= 0.10);
        if (hintTpls.length && fastScales.length) {
          const [ftpl, fconf, fscale] = detectBestSingleWithScale(probeImg, hintTpls, minConf, fastScales, true, false);
          if (ftpl && fconf >= Math.max(minConf, 0.85)) {
            if (fscale !== null) this._bannerTplHints.set(hintKey, [ftpl.name, fscale]);
            return [ftpl, fconf];
          }
        }
      }
      const [btpl, bconf, bscale] = detectBestSingleWithScale(probeImg, templates, minConf, scales, true, false);
      if (btpl && bscale !== null) {
        this._bannerTplHints.set(hintKey, [btpl.name, bscale]);
      }
      return [btpl, bconf];
    }

    detectHudStrip(roiImg, isDealer) {
      const hudTpls = (this.banks.hud || []).filter((t) => t.rank !== "BACK" && t.rank !== "SHUFFLE");
      if (!roiImg || roiImg.w === 0 || roiImg.h === 0 || !hudTpls.length) {
        return [[], {}, false];
      }
      const floorConf = Math.max(0.68, this.cfg.min_hud_confidence - 0.10);
      const clusters = findWhiteCardClusters(roiImg);
      if (!clusters.length) return [[], {}, false];

      const clusterHits = [];
      let backSeen = false;
      const refCh = clusters.length >= 2 ? Math.max(...clusters.map((c) => c[3])) : null;
      for (const cl of clusters) {
        const [dets, clBack] = detectClusterCards(
          roiImg,
          cl,
          hudTpls,
          this.banks.suit || [],
          floorConf,
          isDealer,
          this.cfg.min_hud_confidence,
          refCh
        );
        backSeen = backSeen || clBack;
        if (dets.length) clusterHits.push([cl, dets]);
      }
      if (!clusterHits.length) return [[], {}, false];

      if (isDealer) {
        const flatD = clusterHits.flatMap(([, ds]) => ds).sort((a, b) => a.x - b.x);
        if (flatD.length === 1) {
          const [lastCl] = clusterHits[clusterHits.length - 1];
          const [cx, cy, cw, ch] = lastCl;
          if (!backSeen && roiImg.c === 3) {
            const rx0 = Math.min(roiImg.w, cx + cw);
            const rx1 = Math.min(roiImg.w, rx0 + Math.max(14, Math.round(ch * 0.72)));
            if (rx1 - rx0 >= 8) {
              backSeen = hasRedBackSignature(cropImage(roiImg, rx0, cy, rx1 - rx0, Math.min(roiImg.h - cy, ch)));
            }
          }
          if (backSeen) {
            flatD.push({
              rank: "BACK",
              val: null,
              conf: 0.92,
              x: cx + cw,
              y: cy,
              w: Math.max(10, Math.round(ch * 0.65)),
              h: ch,
              suit: null,
              hand_index: 0,
            });
          }
        }
        return [flatD, { 0: flatD }, false];
      }

      const avgCh = clusterHits.reduce((acc, [cl]) => acc + cl[3], 0) / clusterHits.length;
      const clSepThr = Math.max(4, Math.round(avgCh * 0.30));
      const cardSepThr = Math.max(10, Math.round(avgCh * 0.60));
      const hands = {};
      const allP = [];
      let curHand = 0;
      let prevCl = null;

      for (const [cl, ds] of clusterHits) {
        if (prevCl && cl[0] - (prevCl[0] + prevCl[2]) >= clSepThr) {
          curHand++;
        }
        const dsSorted = ds.slice().sort((a, b) => a.x - b.x);
        for (let i = 0; i < dsSorted.length; i++) {
          const det = dsSorted[i];
          if (i > 0 && det.x - (dsSorted[i - 1].x + dsSorted[i - 1].w) >= cardSepThr) {
            curHand++;
          }
          const tagged = Object.assign({}, det, { hand_index: curHand });
          if (!hands[curHand]) hands[curHand] = [];
          hands[curHand].push(tagged);
          allP.push(tagged);
        }
        prevCl = cl;
      }
      return [allP, hands, Object.keys(hands).length >= 2];
    }

    processCrops({ playerImg, dealerImg, shuffleImg, bannerImg, placeBetsImg, ts = null }) {
      const now = ts !== null ? ts : performance.now() / 1000;
      const REFRESH_PERIOD = 8;
      const SIG_TOL = 4;

      const pSig = computeRoiSignature(playerImg, 48, 24, true);
      const pDiff = roiSigDiff(pSig, this._lastPSig);
      let pCards, pHands, splitDetected;
      if (this._lastPSig && (pDiff === 0 || (pDiff <= SIG_TOL && this._pSigAge < REFRESH_PERIOD))) {
        [pCards, pHands, splitDetected] = this._cachedP;
        if (pDiff > 0) this._pSigAge++;
      } else {
        this._lastPSig = pSig;
        this._pSigAge = 0;
        [pCards, pHands, splitDetected] = this.detectHudStrip(playerImg, false);
        this._cachedP = [pCards, pHands, splitDetected];
      }

      const dSig = computeRoiSignature(dealerImg, 40, 24, true);
      const dDiff = roiSigDiff(dSig, this._lastDSig);
      let dCards;
      if (this._lastDSig && (dDiff === 0 || (dDiff <= SIG_TOL && this._dSigAge < REFRESH_PERIOD))) {
        dCards = this._cachedD;
        if (dDiff > 0) this._dSigAge++;
      } else {
        this._lastDSig = dSig;
        this._dSigAge = 0;
        [dCards] = this.detectHudStrip(dealerImg, true);
        this._cachedD = dCards;
      }

      const cardsPresent = pCards.length > 0 || dCards.length > 0;
      const bannerTpls = this.banks.banner || [];
      let bannerState = "";
      let bannerConf = 0.0;
      let bannerSource = "";

      if (bannerImg && bannerImg.w > 0 && bannerTpls.length) {
        const banSig = computeRoiSignature(bannerImg, 48, 12, false);
        const banDiff = roiSigDiff(banSig, this._lastBannerSig);
        if (this._lastBannerSig && (banDiff === 0 || (banDiff <= SIG_TOL && this._bannerSigAge < REFRESH_PERIOD))) {
          [bannerState, bannerConf] = this._cachedTopBanner;
          if (banDiff > 0) this._bannerSigAge++;
        } else {
          this._lastBannerSig = banSig;
          this._bannerSigAge = 0;
          const [btpl, bconf] = this._detectBannerWithHint(
            bannerImg,
            bannerTpls,
            this.cfg.min_banner_confidence,
            TOP_BANNER_SCALES,
            "banner"
          );
          if (btpl) {
            bannerState = btpl.rank;
            bannerConf = bconf;
          }
          this._cachedTopBanner = [bannerState, bannerConf];
        }
        if (bannerState) bannerSource = "banner_roi";
      }

      let shufOn = false;
      let shufConf = 0.0;
      const shufDue = !cardsPresent || this._lastShuffleScanTs === null || (now - this._lastShuffleScanTs) >= 0.25;
      if (shuffleImg && shuffleImg.w > 0 && bannerTpls.length && !bannerState) {
        const shufMode = cardsPresent ? "cards" : "empty";
        if (shufDue) {
          const shufSig = computeRoiSignature(shuffleImg, 32, 16, false);
          const shufDiff = roiSigDiff(shufSig, this._lastShuffleSig);
          const modeSame = this._lastShufMode === shufMode;
          this._lastShuffleScanTs = now;
          if (this._lastShuffleSig && modeSame && (shufDiff === 0 || (shufDiff <= SIG_TOL && this._shufSigAge < REFRESH_PERIOD))) {
            if (shufDiff > 0) this._shufSigAge++;
          } else {
            this._lastShuffleSig = shufSig;
            this._lastShufMode = shufMode;
            this._shufSigAge = 0;
            const shufTpls = cardsPresent ? bannerTpls.filter((t) => t.rank === "SHUFFLE" || t.rank === "WAIT") : bannerTpls;
            const [btpl, bconf] = shufTpls.length
              ? this._detectBannerWithHint(shuffleImg, shufTpls, this.cfg.min_banner_confidence, TOP_BANNER_SCALES, "shuffle")
              : [null, 0.0];
            this._cachedShuf = btpl ? [btpl.rank, bconf] : ["", 0.0];
          }
        }
        const [cs, cc] = this._cachedShuf;
        if (cs && (!cardsPresent || cs === "SHUFFLE" || cs === "WAIT")) {
          bannerState = cs;
          bannerConf = cc;
          bannerSource = "shuffle_roi";
        }
      }

      let pbConf = 0.0;
      if (placeBetsImg && placeBetsImg.w > 0 && !cardsPresent && bannerTpls.length) {
        const pbTpls = bannerTpls.filter((t) => t.rank === "PLACE_BETS");
        if (pbTpls.length) {
          const pbSig = computeRoiSignature(placeBetsImg, 36, 12, false);
          const pbDiff = roiSigDiff(pbSig, this._lastPbSig);
          if (this._lastPbSig && (pbDiff === 0 || (pbDiff <= SIG_TOL && this._pbSigAge < REFRESH_PERIOD))) {
            pbConf = this._cachedPb;
            if (pbDiff > 0) this._pbSigAge++;
          } else {
            this._lastPbSig = pbSig;
            this._pbSigAge = 0;
            const [ptpl, pconf] = this._detectBannerWithHint(
              placeBetsImg,
              pbTpls,
              Math.max(0.66, this.cfg.min_banner_confidence - 0.02),
              PLACE_BETS_SCALES,
              "place_bets"
            );
            pbConf = ptpl ? pconf : 0.0;
            this._cachedPb = pbConf;
          }
          if (pbConf > 0.0 && !bannerState) {
            bannerState = "PLACE_BETS";
            bannerConf = pbConf;
            bannerSource = "place_bets_roi";
          }
        }
      } else {
        this._lastPbSig = null;
        this._cachedPb = 0.0;
      }

      if (bannerState === "SHUFFLE") {
        shufOn = true;
        shufConf = Math.max(shufConf, bannerConf);
      }

      return {
        ts: now,
        player_cards: pCards,
        dealer_cards: dCards,
        player_hands: pHands,
        felt_events: [],
        banner_state: bannerState,
        banner_conf: bannerConf,
        shuffle_active: shufOn,
        shuffle_conf: shufConf,
        split_detected: splitDetected,
        hud_refreshed: true,
        dealer_only: false,
        place_bets_conf: pbConf,
        banner_source: bannerSource,
      };
    }
  }

  /* ==========================================================================
     4. Sequence Debouncer, Keep-Alive Manager & Companion State Machine
     ========================================================================== */

  function sameBjValue(r1, r2) {
    const v1 = RANK_TO_VAL[r1];
    const v2 = RANK_TO_VAL[r2];
    return v1 !== undefined && v1 !== null && v1 === v2;
  }

  function evalBjRanks(ranks) {
    let total = 0, aces = 0;
    for (const rk of ranks) {
      const val = RANK_TO_VAL[rk];
      if (val === undefined || val === null) continue;
      if (val === 1) { aces++; total += 11; }
      else if (val === 0) total += 10;
      else total += val;
    }
    while (total > 21 && aces > 0) {
      total -= 10;
      aces--;
    }
    return [total, aces > 0, total > 21];
  }

  class SequenceDebouncer {
    constructor(confirmFrames = 3, clearFrames = 4, fastTailConfirm = false, fastTailMinConf = 0.84) {
      this.confirmFrames = confirmFrames;
      this.clearFrames = clearFrames;
      this.fastTailConfirm = fastTailConfirm;
      this.fastTailMinConf = fastTailMinConf;
      this.fastInitialDeal = true;
      this.reset();
    }

    reset() {
      this._candRanks = "";
      this._candDets = [];
      this._candCount = 0;
      this._emptyCount = 0;
      this._slotCands = new Map();
      this.committedRanks = [];
    }

    reanchor(ranks) {
      this.committedRanks = ranks.slice();
      this._candRanks = ranks.join(",");
      this._candDets = [];
      this._candCount = this.confirmFrames;
      this._emptyCount = 0;
      this._slotCands.clear();
    }

    update(detections) {
      if (!detections || !detections.length) {
        this._emptyCount++;
        this._candRanks = "";
        this._candDets = [];
        this._candCount = 0;
        this._slotCands.clear();
        return [[], this._emptyCount >= this.clearFrames];
      }
      this._emptyCount = 0;
      const rKey = detections.map((d) => d.rank).join(",");
      if (rKey === this._candRanks) {
        this._candCount++;
        this._candDets = detections.slice();
      } else {
        this._candRanks = rKey;
        this._candDets = detections.slice();
        this._candCount = 1;
      }

      const committedWipe = detections.length < this.committedRanks.length;
      let prefixOk = !committedWipe;
      for (let k = 0; k < this.committedRanks.length && prefixOk; k++) {
        if (this.committedRanks[k] !== "BACK" && detections[k].rank !== this.committedRanks[k]) {
          prefixOk = false;
        }
      }
      if (this.committedRanks.length && !prefixOk && !committedWipe) {
        this._slotCands.clear();
        return [[], false];
      }

      const seenSlots = new Set();
      let prefixStable = true;
      for (let idx = 0; idx < detections.length; idx++) {
        const det = detections[idx];
        const isUncommitted = idx >= this.committedRanks.length || this.committedRanks[idx] === "BACK";
        if (!isUncommitted) continue;
        if (det.rank === "BACK") {
          this._slotCands.delete(idx);
          continue;
        }
        seenSlots.add(idx);
        const prev = this._slotCands.get(idx);
        let cnt = 1;
        if (prefixStable && prev && prev.rank === det.rank) {
          cnt = prev.cnt + 1;
        } else if (prev && prev.rank !== det.rank) {
          prefixStable = false;
        }
        if (rKey === this._candRanks && this._candCount > cnt) cnt = this._candCount;
        this._slotCands.set(idx, { rank: det.rank, cnt, det });
      }
      for (const idx of Array.from(this._slotCands.keys())) {
        if (!seenSlots.has(idx)) this._slotCands.delete(idx);
      }
      if (committedWipe) return [[], false];

      const emitted = [];
      for (let idx = 0; idx < Math.min(this.committedRanks.length, detections.length); idx++) {
        if (this.committedRanks[idx] === "BACK" && this._slotCands.has(idx)) {
          const { rank, cnt, det } = this._slotCands.get(idx);
          const fastOk = this.fastTailConfirm && (det.conf >= this.fastTailMinConf || (det.suit && det.conf >= 0.78));
          const req = fastOk ? Math.min(this.confirmFrames, 2) : this.confirmFrames;
          if (cnt >= req) {
            this.committedRanks[idx] = rank;
            emitted.push([idx, det]);
            this._slotCands.delete(idx);
          }
        }
      }
      while (!this.committedRanks.includes("BACK") && this.committedRanks.length < detections.length) {
        const idx = this.committedRanks.length;
        if (detections[idx].rank === "BACK") {
          if (idx > 0) this.committedRanks.push("BACK");
          break;
        }
        if (!this._slotCands.has(idx)) break;
        const { rank, cnt, det } = this._slotCands.get(idx);
        const useFast = (this.fastTailConfirm && idx >= 1) || (this.fastInitialDeal && idx < 2);
        const fastOk = useFast && (det.conf >= this.fastTailMinConf || (det.suit && det.conf >= 0.80));
        const req = fastOk ? Math.min(this.confirmFrames, 2) : this.confirmFrames;
        if (cnt < req) break;
        this.committedRanks.push(rank);
        emitted.push([idx, det]);
        this._slotCands.delete(idx);
      }
      return [emitted, false];
    }
  }

  class CompanionStateMachine {
    constructor(cfg) {
      this.cfg = cfg;
      this.paused = false;
      this.sync_play_mode = "watch";
      this.sync_is_split = false;
      this.sync_draft_count = 0;
      this.sync_track_felt = false;
      this.sync_round_id = "";
      this.sync_has_prompt = false;
      this.sync_bet_rounds_count = 0;
      this.state = "IDLE";
      this._roundSeq = 0;
      this.round_id = "r_0";
      this._pendingAutoWatch = null;
      this._lastBannerEmitted = "";
      this._sawClearSinceRoundOver = true;
      this._resetRoundAccumulators();
    }

    _resetRoundAccumulators() {
      this.pDebouncers = {
        0: new SequenceDebouncer(this.cfg.confirm_frames, this.cfg.clear_frames, false, Math.max(0.84, this.cfg.min_hud_confidence)),
      };
      this.dDebouncer = new SequenceDebouncer(this.cfg.confirm_frames, this.cfg.clear_frames, true, Math.max(0.84, this.cfg.min_hud_confidence));
      this._emittedP = 0;
      this._emittedD = 0;
      this._emittedB = 0;
      this._emittedSplitHands = 1;
      this._splitDetected = false;
      this._uncertaintyFlags = [];
      this._lastBetBanner = "";
      this._seenWaitInRound = false;
      this._waitAbsentFrames = 0;
      this._blankCardFrames = 0;
      this._sawEmptyHudInBetting = false;
    }

    resyncRound(clearPending = true) {
      if (clearPending) this._pendingAutoWatch = null;
      this._resetRoundAccumulators();
      this._lastBannerEmitted = "";
      this._sawClearSinceRoundOver = true;
      this.state = "IDLE";
    }

    setSyncState(msg) {
      if (msg.playMode === "watch" || msg.playMode === "bet") {
        this.sync_play_mode = msg.playMode;
      }
      if (msg.isSplit !== undefined) this.sync_is_split = !!msg.isSplit;
      if (msg.draftCount !== undefined) {
        const nc = Math.max(0, Number(msg.draftCount) || 0);
        if (nc === 0 && this._pendingAutoWatch) this._pendingAutoWatch = null;
        this.sync_draft_count = nc;
      }
      if (msg.trackFelt !== undefined) this.sync_track_felt = !!msg.trackFelt;
      if (msg.compRoundId !== undefined) this.sync_round_id = String(msg.compRoundId || "");
      if (msg.hasPrompt !== undefined) this.sync_has_prompt = !!msg.hasPrompt;
      if (msg.betRoundsCount !== undefined) this.sync_bet_rounds_count = Math.max(0, Number(msg.betRoundsCount) || 0);
    }

    _flushPendingAutoWatch(events) {
      if (this._pendingAutoWatch) {
        events.push(this._pendingAutoWatch);
        this._pendingAutoWatch = null;
      }
    }

    _startNewRound() {
      this._roundSeq++;
      this.round_id = `r_${this._roundSeq}`;
      this._resetRoundAccumulators();
      this.state = "DEALING";
    }

    _beginRoundIfIdle(events) {
      if (this.state === "IDLE" || this.state === "ROUND_OVER") {
        if (events) this._flushPendingAutoWatch(events);
        this._roundSeq++;
        this.round_id = `r_${this._roundSeq}`;
        this.state = "DEALING";
      }
    }

    _emitBannerPhaseIfChanged(banner, conf, events) {
      if (banner && banner !== this._lastBannerEmitted) {
        this._lastBannerEmitted = banner;
        events.push({
          type: "phase",
          phase: banner,
          conf: Number(conf.toFixed(3)),
          roundId: this.round_id,
        });
      }
    }

    _finalizeClearedRound() {
      const events = [];
      const totalEmitted = this._emittedP + this._emittedD + this._emittedB;
      if (totalEmitted === 0) {
        this.resyncRound(false);
        return events;
      }
      const dCommitted = this.dDebouncer.committedRanks.filter((r) => r !== "BACK");
      const [dTotal] = evalBjRanks(dCommitted);
      const flags = this._uncertaintyFlags.slice();
      if (this._emittedP < 2 || this._emittedD < 1) flags.push("HUD_GLITCH");

      const canAutoWatch =
        this.sync_play_mode === "watch" &&
        !this._splitDetected &&
        !this.sync_is_split &&
        flags.length === 0 &&
        this._emittedD >= 1;

      if (canAutoWatch) {
        this._flushPendingAutoWatch(events);
        this._pendingAutoWatch = {
          type: "auto_watch",
          roundId: this.round_id,
          handCount: this._emittedSplitHands,
          summary: `${this._emittedP}P+${this._emittedD}D+${this._emittedB}B`,
          fromWatch: true,
        };
      } else {
        const reason = this._splitDetected || this.sync_is_split
          ? "SPLIT"
          : this.sync_play_mode === "bet"
          ? "BET_MODE"
          : flags.includes("HUD_GLITCH")
          ? "HUD_GLITCH"
          : "UNCERTAIN_READ";
        events.push({
          type: "prompt_confirm",
          roundId: this.round_id,
          reason,
          unreadCommunity: 0,
          uncertainSlots: flags,
          suggestedFocus: "P",
          dealerTotal: dTotal,
          feltSightings: [],
        });
      }
      this.resyncRound(!canAutoWatch);
      return events;
    }

    step(obs) {
      if (this.paused) return [];
      const events = [];
      const banner = obs.banner_state || "";
      const hasCards = this._emittedP > 0 || this._emittedD > 0;
      const hasObsCards = (obs.player_cards && obs.player_cards.length > 0) || (obs.dealer_cards && obs.dealer_cards.length > 0);
      const activeRound = ["DEALING", "PLAYER_TURN", "DEALER_TURN"].includes(this.state);

      if (["PLACE_BETS", "LAST_BETS", "NO_MORE_BETS"].includes(banner)) {
        if (activeRound && hasCards) {
          events.push(...this._finalizeClearedRound());
        }
        if (banner === "NO_MORE_BETS") {
          this._flushPendingAutoWatch(events);
        }
        this.state = "BETTING";
        this._sawClearSinceRoundOver = true;
        this._emitBannerPhaseIfChanged(banner, obs.banner_conf || 0.9, events);
        this._resetRoundAccumulators();
        this._lastBetBanner = banner;
        return events;
      }

      if (banner === "WAIT") {
        this._waitAbsentFrames = 0;
        this._blankCardFrames = 0;
        this._sawClearSinceRoundOver = true;
        if (["IDLE", "BETTING", "ROUND_OVER"].includes(this.state)) {
          this._flushPendingAutoWatch(events);
          this._startNewRound();
        }
        this._seenWaitInRound = true;
        this._emitBannerPhaseIfChanged("WAIT", obs.banner_conf || 0.9, events);
      } else {
        if (!hasObsCards) {
          this._sawClearSinceRoundOver = true;
          this._blankCardFrames = 0;
        }
        if (activeRound && this._seenWaitInRound && !hasObsCards) {
          this._waitAbsentFrames++;
          if (hasCards && this._waitAbsentFrames >= Math.max(2, this.cfg.clear_frames)) {
            events.push(...this._finalizeClearedRound());
            this.state = "ROUND_OVER";
            this._sawClearSinceRoundOver = false;
            return events;
          }
        }
        const waitRequired = this.cfg.require_wait_banner && this.sync_play_mode === "watch";
        if (waitRequired && !activeRound) {
          if (hasObsCards && this._sawClearSinceRoundOver) {
            this._blankCardFrames++;
            if (this._blankCardFrames >= 2) {
              this._flushPendingAutoWatch(events);
              this._startNewRound();
              this._emitBannerPhaseIfChanged("DEALING", 1.0, events);
            } else {
              return events;
            }
          } else {
            return events;
          }
        } else if (!waitRequired && this.state === "BETTING") {
          if ((!banner && this._lastBetBanner === "NO_MORE_BETS") || hasObsCards) {
            this._flushPendingAutoWatch(events);
            this._startNewRound();
            this._emitBannerPhaseIfChanged("DEALING", 1.0, events);
          } else {
            return events;
          }
        }
      }

      if (obs.split_detected && !this._splitDetected) {
        this._splitDetected = true;
        const handKeys = Object.keys(obs.player_hands || {});
        if (handKeys.length >= 2) {
          this._beginRoundIfIdle(events);
          this._emittedSplitHands = handKeys.length;
          events.push({
            type: "split",
            handCount: handKeys.length,
            roundId: this.round_id,
            splitCards: handKeys.slice(0, 2).map((hk) => {
              const c0 = obs.player_hands[hk][0];
              return { val: c0.val, rankLabel: c0.rank, suit: c0.suit };
            }),
          });
        }
      }

      const handsInput = obs.player_hands && Object.keys(obs.player_hands).length
        ? obs.player_hands
        : obs.player_cards && obs.player_cards.length
        ? { 0: obs.player_cards }
        : {};

      let pCleared = true;
      const handIndices = Array.from(new Set([...Object.keys(this.pDebouncers), ...Object.keys(handsInput)])).map(Number).sort((a, b) => a - b);
      for (const hIdx of handIndices) {
        if (!this.pDebouncers[hIdx]) {
          this.pDebouncers[hIdx] = new SequenceDebouncer(this.cfg.confirm_frames, this.cfg.clear_frames);
        }
        const [newCards, hCleared] = this.pDebouncers[hIdx].update(handsInput[hIdx] || []);
        if (!hCleared) pCleared = false;
        for (const [slotIdx, det] of newCards) {
          if (det.val === null || det.val === undefined) continue;
          this._beginRoundIfIdle(events);
          this._emittedP++;
          const evt = {
            type: "card",
            target: "P",
            handIndex: hIdx,
            val: det.val,
            slot: slotIdx,
            rankLabel: det.rank,
            conf: Number(det.conf.toFixed(3)),
            source: "hud",
            roundId: this.round_id,
          };
          if (det.suit) evt.suit = det.suit;
          events.push(evt);
        }
      }

      const [newD, dCleared] = this.dDebouncer.update(obs.dealer_cards || []);
      for (const [slotIdx, det] of newD) {
        if (det.val === null || det.val === undefined) continue;
        this._beginRoundIfIdle(events);
        this._emittedD++;
        if (slotIdx >= 1) this.state = "DEALER_TURN";
        const evtD = {
          type: "card",
          target: "D",
          val: det.val,
          slot: slotIdx,
          rankLabel: det.rank,
          conf: Number(det.conf.toFixed(3)),
          source: "hud",
          roundId: this.round_id,
        };
        if (det.suit) evtD.suit = det.suit;
        events.push(evtD);
      }

      if (this.state === "DEALING" && this.dDebouncer.committedRanks.length >= 1 && this._emittedP >= 1) {
        this.state = "PLAYER_TURN";
      }
      if (!["IDLE", "BETTING", "ROUND_OVER"].includes(this.state) && pCleared && dCleared) {
        events.push(...this._finalizeClearedRound());
      }
      return events;
    }
  }

  class WatchKeepaliveManager {
    constructor(cfg) {
      this.cfg = cfg;
      this.state = "WATCH_COUNTDOWN";
      this._watchElapsedSec = 0.0;
      this._lastTickTs = null;
      this._refreshStartedTs = 0.0;
      this._graceUntilTs = 0.0;
      this._lastCooldownEndTs = 0.0;
      this._lastAlertPlaceBetsTs = 0.0;
      this._lastHardWarnTs = 0.0;
      this._lastBetRoundsCount = 0;
      this._manualArmed = false;
      this._refreshCount = 0;
    }

    inGrace(now = performance.now() / 1000) {
      return now < this._graceUntilTs;
    }

    getStatus(now = performance.now() / 1000) {
      const enabled = this.cfg.keepalive_enabled !== false && this.cfg.keepalive_mode !== "off";
      const target = Math.max(30, Number(this.cfg.keepalive_interval_sec) || 600);
      const elapsed = Math.round(this._watchElapsedSec * 10) / 10;
      const rem = Math.max(0, Math.round(target - this._watchElapsedSec));
      return {
        enabled,
        mode: enabled ? this.cfg.keepalive_mode || "auto" : "off",
        method: this.cfg.keepalive_method || "ext_then_os",
        state: enabled ? this.state : "OFF",
        elapsedSec: elapsed,
        targetSec: target,
        remainingSec: rem,
        inGrace: this.inGrace(now),
        refreshCount: this._refreshCount,
        resetOnBetRound: this.cfg.keepalive_reset_on_bet_round !== false,
      };
    }

    resetTimer(now = performance.now() / 1000, reason = "manual") {
      this._watchElapsedSec = 0.0;
      this._lastTickTs = now;
      this._manualArmed = false;
      this._graceUntilTs = 0.0;
      if (this.cfg.keepalive_enabled === false || this.cfg.keepalive_mode === "off") {
        this.state = "OFF";
      } else {
        this.state = "WATCH_COUNTDOWN";
      }
      return {
        type: "keepalive_status",
        event: "reset",
        reason,
        keepalive: this.getStatus(now),
      };
    }

    armNow(now = performance.now() / 1000) {
      this._manualArmed = true;
      this.state = "ARMED_NEXT_BETS";
      return {
        type: "keepalive_status",
        event: "armed",
        reason: "manual_arm",
        keepalive: this.getStatus(now),
      };
    }

    updateConfig(rawPatch, now = performance.now() / 1000) {
      const patch = rawPatch && rawPatch.keepalive && typeof rawPatch.keepalive === "object"
        ? rawPatch.keepalive
        : (rawPatch || {});
      if (patch.enabled !== undefined) this.cfg.keepalive_enabled = !!patch.enabled;
      if (patch.mode && ["auto", "alert_only", "off"].includes(patch.mode)) {
        this.cfg.keepalive_mode = patch.mode;
      }
      if (patch.method && typeof patch.method === "string") {
        this.cfg.keepalive_method = patch.method;
      }
      const nextInterval =
        patch.intervalSec !== undefined
          ? Number(patch.intervalSec)
          : patch.max_watch_sec !== undefined
          ? Number(patch.max_watch_sec)
          : patch.min_watch_sec !== undefined
          ? Number(patch.min_watch_sec)
          : undefined;
      if (nextInterval !== undefined && Number.isFinite(nextInterval)) {
        this.cfg.keepalive_interval_sec = Math.max(30, Math.min(888, nextInterval));
      }
      if (this.cfg.keepalive && typeof this.cfg.keepalive === "object") {
        if (patch.min_watch_sec !== undefined && Number.isFinite(Number(patch.min_watch_sec))) {
          this.cfg.keepalive.min_watch_sec = Math.max(30, Math.min(888, Number(patch.min_watch_sec)));
        }
        if (patch.max_watch_sec !== undefined && Number.isFinite(Number(patch.max_watch_sec))) {
          this.cfg.keepalive.max_watch_sec = Math.max(30, Math.min(888, Number(patch.max_watch_sec)));
        }
      }
      if (patch.resetOnBetRound !== undefined) {
        this.cfg.keepalive_reset_on_bet_round = !!patch.resetOnBetRound;
      } else if (patch.reset_on_bet_round !== undefined) {
        this.cfg.keepalive_reset_on_bet_round = !!patch.reset_on_bet_round;
      }
      if (this.cfg.keepalive_enabled === false || this.cfg.keepalive_mode === "off") {
        this.state = "OFF";
      } else if (this.state === "OFF") {
        this.state = "WATCH_COUNTDOWN";
      }
      return {
        type: "keepalive_status",
        event: "config_saved",
        keepalive: this.getStatus(now),
      };
    }

    step({ now, windowFound, obs, smState, playMode, draftCount, hasPrompt, betRoundsCount, triggerRefreshCb }) {
      const events = [];
      const dt = this._lastTickTs === null ? 0.0 : Math.max(0.0, Math.min(2.0, now - this._lastTickTs));
      this._lastTickTs = now;

      if (betRoundsCount > this._lastBetRoundsCount) {
        this._lastBetRoundsCount = betRoundsCount;
        if (this.cfg.keepalive_reset_on_bet_round !== false) {
          this._watchElapsedSec = 0.0;
          this._manualArmed = false;
          if (!["REFRESHING", "RECOVERING"].includes(this.state)) {
            this.state = "WATCH_COUNTDOWN";
          }
        }
      } else if (betRoundsCount < this._lastBetRoundsCount) {
        this._lastBetRoundsCount = betRoundsCount;
      }

      const enabled = this.cfg.keepalive_enabled !== false && this.cfg.keepalive_mode !== "off";
      if (!enabled) {
        this.state = "OFF";
        return events;
      }

      if (["REFRESHING", "RECOVERING"].includes(this.state)) {
        const reloadDur = now - this._refreshStartedTs;
        const recovered =
          windowFound &&
          obs !== null &&
          reloadDur >= 2.0 &&
          (Boolean(obs.banner_state) || (obs.player_cards && obs.player_cards.length > 0) || (obs.dealer_cards && obs.dealer_cards.length > 0));
        if (recovered) {
          this._watchElapsedSec = 0.0;
          this._manualArmed = false;
          this._graceUntilTs = 0.0;
          this._lastCooldownEndTs = now + 90.0;
          this.state = "WATCH_COUNTDOWN";
          events.push({
            type: "keepalive_status",
            event: "recovered",
            reloadSec: Math.round(reloadDur * 10) / 10,
            keepalive: this.getStatus(now),
          });
          return events;
        }
        if (reloadDur >= (this.cfg.keepalive_reload_grace_sec || 14.0)) {
          this.state = "WARN";
          this._graceUntilTs = 0.0;
          events.push({
            type: "keepalive_status",
            event: "timeout_warn",
            reloadSec: Math.round(reloadDur * 10) / 10,
            keepalive: this.getStatus(now),
          });
        }
        return events;
      }

      if (playMode === "bet") {
        this.state = "PAUSED_BET";
        return events;
      }

      if (windowFound) {
        this._watchElapsedSec += dt;
      }

      const targetSec = Math.max(60, Number(this.cfg.keepalive_interval_sec) || 600);
      const hardWarnSec = Math.max(targetSec + 60, Number(this.cfg.keepalive_hard_warn_sec) || 780);

      if (this._watchElapsedSec >= hardWarnSec && now - this._lastHardWarnTs >= 30.0) {
        this._lastHardWarnTs = now;
        this.state = "WARN";
        events.push({
          type: "keepalive_status",
          event: "hard_warn",
          elapsedSec: Math.round(this._watchElapsedSec),
          keepalive: this.getStatus(now),
        });
      }

      const isDue = this._manualArmed || (this._watchElapsedSec >= targetSec && now >= this._lastCooldownEndTs);
      if (!isDue) {
        if (this.state !== "WARN") this.state = "WATCH_COUNTDOWN";
        return events;
      }

      if (this.state !== "WARN") this.state = "ARMED_NEXT_BETS";

      const banner = obs && obs.banner_state ? obs.banner_state : "";
      const pbConf = obs && obs.place_bets_conf ? obs.place_bets_conf : 0.0;
      const hasLiveCards = Boolean(obs && ((obs.player_cards && obs.player_cards.length) || (obs.dealer_cards && obs.dealer_cards.length)));
      const placeBetsReady = banner === "PLACE_BETS" || pbConf >= 0.66;
      const roundOverSafe =
        smState === "ROUND_OVER" ||
        (smState === "IDLE" && this._manualArmed) ||
        (smState === "BETTING" && banner === "PLACE_BETS");
      const safeWindow =
        windowFound &&
        playMode === "watch" &&
        !hasLiveCards &&
        draftCount === 0 &&
        !hasPrompt &&
        !["DEALING", "PLAYER_TURN", "DEALER_TURN"].includes(smState) &&
        (placeBetsReady || roundOverSafe);

      if (!safeWindow) return events;

      if (this.cfg.keepalive_mode === "alert_only") {
        if (now - this._lastAlertPlaceBetsTs >= 15.0) {
          this._lastAlertPlaceBetsTs = now;
          this._manualArmed = false;
          events.push({
            type: "keepalive_status",
            event: "alert_place_bets",
            elapsedSec: Math.round(this._watchElapsedSec),
            keepalive: this.getStatus(now),
          });
        }
        return events;
      }

      const [ok, methodUsed] = triggerRefreshCb ? triggerRefreshCb() : [false, "none"];
      if (ok) {
        this._refreshCount++;
        this._refreshStartedTs = now;
        this._graceUntilTs = now + (this.cfg.keepalive_reload_grace_sec || 14.0);
        this._manualArmed = false;
        this.state = "REFRESHING";
        events.push({
          type: "keepalive_status",
          event: "refreshing",
          method: methodUsed,
          graceSec: this.cfg.keepalive_reload_grace_sec || 14.0,
          keepalive: this.getStatus(now),
        });
      } else if (now - this._lastAlertPlaceBetsTs >= 15.0) {
        this._lastAlertPlaceBetsTs = now;
        this._manualArmed = false;
        this.state = "WARN";
        events.push({
          type: "keepalive_status",
          event: "alert_place_bets",
          elapsedSec: Math.round(this._watchElapsedSec),
          message: "🔄 Place Bets window — tap Reload (⟳) in left Safari pane to prevent 15-min idle kick.",
          keepalive: this.getStatus(now),
        });
      }
      return events;
    }
  }

  class ViewportSmoother {
    constructor(alpha = 0.35, cacheInterval = 18) {
      this.alpha = alpha;
      this.cacheInterval = Math.max(1, Number(cacheInterval) || 18);
      this._rect = null;
      this._cachedArgs = null;
      this._cachedRect = null;
      this._framesSinceDetect = 0;
    }

    reset() {
      this._rect = null;
      this._cachedArgs = null;
      this._cachedRect = null;
      this._framesSinceDetect = 0;
    }

    resolveAnchor(frameBgr, anchorMode = "ipad_split_left") {
      if (!frameBgr || frameBgr.w === 0 || frameBgr.h === 0) return [0, 0, 0, 0];
      if (anchorMode === "window") {
        this.reset();
        return [0, 0, frameBgr.w, frameBgr.h];
      }
      if (
        this._cachedRect &&
        this._cachedArgs &&
        this._cachedArgs[0] === frameBgr.w &&
        this._cachedArgs[1] === frameBgr.h &&
        this._cachedArgs[2] === anchorMode &&
        this._framesSinceDetect < this.cacheInterval
      ) {
        this._framesSinceDetect++;
        return this._cachedRect;
      }
      const detected =
        anchorMode === "ipad_split_left"
          ? detectSplitViewLeftPane(frameBgr)
          : detectGameViewportLocal(frameBgr);
      if (!this._rect) {
        this._rect = detected.map(Number);
      } else {
        const jump = Math.max(...detected.map((d, i) => Math.abs(d - this._rect[i])));
        if (jump > 80) {
          this._rect = detected.map(Number);
        } else {
          this._rect = this._rect.map((o, i) => (1.0 - this.alpha) * o + this.alpha * detected[i]);
        }
      }
      const [rx, ry, rw, rh] = this._rect.map((v) => Math.round(v));
      const x = Math.max(0, Math.min(frameBgr.w - 1, rx));
      const y = Math.max(0, Math.min(frameBgr.h - 1, ry));
      const w = Math.max(1, Math.min(frameBgr.w - x, rw));
      const h = Math.max(1, Math.min(frameBgr.h - y, rh));
      this._cachedArgs = [frameBgr.w, frameBgr.h, anchorMode];
      this._cachedRect = [x, y, w, h];
      this._framesSinceDetect = 0;
      return this._cachedRect;
    }
  }

  /* ==========================================================================
     5. Standalone iPad Companion Runtime & Virtual WebSocket Bridge
     ========================================================================== */

  const STORAGE_PACK_KEY = "bjPlaytechTracker.ipadCompanionPack.v1";

  function bgrToRGBA(bgrImg) {
    if (!bgrImg || bgrImg.w === 0 || bgrImg.h === 0) return new Uint8Array(0);
    const n = bgrImg.w * bgrImg.h;
    const out = new Uint8Array(n * 4);
    const src = bgrImg.data;
    if (bgrImg.c === 1) {
      for (let i = 0, p = 0; i < n; i++, p += 4) {
        const v = src[i];
        out[p] = v;
        out[p + 1] = v;
        out[p + 2] = v;
        out[p + 3] = 255;
      }
    } else {
      for (let i = 0, j = 0, p = 0; i < n; i++, j += 3, p += 4) {
        out[p] = src[j + 2];
        out[p + 1] = src[j + 1];
        out[p + 2] = src[j];
        out[p + 3] = 255;
      }
    }
    return out;
  }

  function rgbaToDataUrl(rgba, w, h) {
    if (typeof document === "undefined" || !rgba || !w || !h) return "";
    const cv = document.createElement("canvas");
    cv.width = w;
    cv.height = h;
    const ctx = cv.getContext("2d");
    const imgData = ctx.createImageData(w, h);
    imgData.data.set(rgba.subarray(0, w * h * 4));
    ctx.putImageData(imgData, 0, 0);
    return cv.toDataURL("image/jpeg", 0.88);
  }

  function bgrToDataUrl(bgrImg) {
    if (typeof document === "undefined" || !bgrImg || bgrImg.w === 0 || bgrImg.h === 0) {
      return "";
    }
    return rgbaToDataUrl(bgrToRGBA(bgrImg), bgrImg.w, bgrImg.h);
  }

  class IpadCompanionRuntime {
    constructor(options = {}) {
      this.options = options || {};
      this.onSavePack = typeof this.options.onSavePack === "function" ? this.options.onSavePack : null;
      this.onNativeAction = typeof this.options.onNativeAction === "function" ? this.options.onNativeAction : null;
      this.onXhrBlocked = typeof this.options.onXhrBlocked === "function" ? this.options.onXhrBlocked : null;

      const initialPack =
        this.options.initialPack ||
        this._parsePack(this.options.savedPack) ||
        this._loadSavedPack() ||
        PRELOADED_PACK ||
        (typeof globalThis.DEFAULT_COMPANION_PACK === "object" ? globalThis.DEFAULT_COMPANION_PACK : null);

      this.cfg = defaultConfig(initialPack && initialPack.config ? initialPack.config : {});
      if (!this.options.preserveDesktopAnchor && (!initialPack || !initialPack.config || initialPack.config.anchor_mode === "game_viewport")) {
        this.cfg.anchor_mode = "ipad_split_left";
      }
      this.detector = new PlaytechDetector(this.cfg, initialPack);
      this.sm = new CompanionStateMachine(this.cfg);
      this.keepalive = new WatchKeepaliveManager(this.cfg);
      this.smoother = new ViewportSmoother(0.35, 18);

      this.clients = new Set();
      this._unrotatedRawFrame = null;
      this.latestRawFrame = null;
      this.latestFrameTs = 0;
      this._lastFrameSeq = 0;
      this.broadcastActive = false;
      this.frameTransport = "idle";
      this.frameInfo = "";
      this.hostInfo = null;
      this._sawNativeBroadcast = false;
      this._didNotifyNativeReady = false;
      this._consecutiveXhrErrors = 0;
      this.extConnected = false;
      this._loopTimer = null;
      this._busy = false;

      if (this.options.installGlobalHooks !== false && typeof globalThis !== "undefined") {
        globalThis.__ipadCompanionReceiveRGBA = (rgbaBytes, width, height) => {
          this.ingestRGBA(rgbaBytes, width, height);
        };
        globalThis.__ipadCompanionSetExtConnected = (connected) => {
          this.extConnected = !!connected;
          this.broadcast(this.buildStatusPayload());
        };
        globalThis.__ipadCompanionSetHostInfo = (info) => {
          if (info && typeof info === "object") {
            this.hostInfo = Object.assign({}, this.hostInfo || {}, info);
            this.broadcast(this.buildStatusPayload());
          }
        };
      }
    }

    _parsePack(raw) {
      if (!raw) return null;
      try {
        const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (!parsed || typeof parsed !== "object" || !parsed.templates || typeof parsed.templates !== "object" || Object.keys(parsed.templates).length === 0) {
          return null;
        }
        return parsed;
      } catch (_) {
        return null;
      }
    }

    _postNativeAction(payload) {
      if (this.onNativeAction) {
        try {
          this.onNativeAction(payload);
          return true;
        } catch (_) {}
      }
      if (
        typeof window !== "undefined" &&
        window.webkit &&
        window.webkit.messageHandlers &&
        window.webkit.messageHandlers.ipadCompanion
      ) {
        try {
          window.webkit.messageHandlers.ipadCompanion.postMessage(payload);
          return true;
        } catch (_) {}
      }
      return false;
    }

    startNativePolling() {
      if (this._loopTimer || typeof XMLHttpRequest === "undefined") return;
      const hasWkHandler =
        typeof window !== "undefined" &&
        !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ipadCompanion);
      const isNativeHost =
        this.options.isNativeHost !== undefined ? !!this.options.isNativeHost : hasWkHandler;
      if (!isNativeHost) return;

      if (!this._didNotifyNativeReady) {
        this._didNotifyNativeReady = true;
        this._postNativeAction({ action: "companionReady" });
      }

      const proto =
        (typeof globalThis !== "undefined" && globalThis.location && globalThis.location.protocol) ||
        (typeof window !== "undefined" && window.location && window.location.protocol) ||
        "";
      const frameUrlBase =
        this.options.frameUrlBase ||
        (proto === "bjtracker:" ? "/companion-frame/latest" : "companion-frame://latest");

      const noteXhrError = () => {
        this._consecutiveXhrErrors++;
        if (this._consecutiveXhrErrors >= 3 && this.onXhrBlocked) {
          const cb = this.onXhrBlocked;
          this.onXhrBlocked = null;
          try {
            cb();
          } catch (_) {}
        }
      };

      const intervalMs = Math.max(50, Math.round(1000 / (this.cfg.fps || 10)));
      this._loopTimer = setInterval(() => {
        if (this._busy || this.clients.size === 0) return;
        this._busy = true;
        const xhr = new XMLHttpRequest();
        const pollUrl = this._lastFrameSeq ? `${frameUrlBase}?since=${this._lastFrameSeq}` : frameUrlBase;
        xhr.open("GET", pollUrl, true);
        xhr.responseType = "arraybuffer";
        xhr.timeout = 1000;
        const done = () => {
          this._busy = false;
        };
        xhr.onload = () => {
          try {
            if (xhr.status === 0) {
              noteXhrError();
              return;
            }
            this._consecutiveXhrErrors = 0;

            const now = performance.now() / 1000;
            const hdrTransport = (xhr.getResponseHeader && xhr.getResponseHeader("X-BJ-Transport")) || "";
            const hdrActive = (xhr.getResponseHeader && xhr.getResponseHeader("X-BJ-Broadcast-Active")) || "";
            const hdrAppGroupOk = (xhr.getResponseHeader && xhr.getResponseHeader("X-BJ-AppGroup-Ok")) || "";
            const hdrExtPresent = (xhr.getResponseHeader && xhr.getResponseHeader("X-BJ-BroadcastExt-Present")) || "";
            const hdrFrameInfo = (xhr.getResponseHeader && xhr.getResponseHeader("X-BJ-Frame-Info")) || "";

            let statusDirty = false;
            if (hdrAppGroupOk || hdrExtPresent) {
              const prevInfo = this.hostInfo || {};
              const nextAppGroup = hdrAppGroupOk ? hdrAppGroupOk === "1" : !!prevInfo.appGroupAvailable;
              const nextExtPres = hdrExtPresent ? hdrExtPresent === "1" : !!prevInfo.broadcastExtPresent;
              if (prevInfo.appGroupAvailable !== nextAppGroup || prevInfo.broadcastExtPresent !== nextExtPres) {
                this.hostInfo = Object.assign({}, prevInfo, {
                  appGroupAvailable: nextAppGroup,
                  broadcastExtPresent: nextExtPres,
                });
                statusDirty = true;
              }
            }
            if (hdrTransport && hdrTransport !== this.frameTransport) {
              this.frameTransport = hdrTransport;
              statusDirty = true;
            }
            if (hdrFrameInfo && hdrFrameInfo !== this.frameInfo) {
              this.frameInfo = hdrFrameInfo;
              statusDirty = true;
            } else if (!hdrFrameInfo && hdrActive === "0" && hdrTransport === "idle" && this.frameInfo) {
              this.frameInfo = "";
              statusDirty = true;
            }

            let decodedNewFrame = false;
            if (xhr.status === 200 && xhr.response instanceof ArrayBuffer) {
              const buf = xhr.response;
              if (buf.byteLength > 16) {
                const hdr = new Uint32Array(buf, 0, 4);
                const seq = hdr[0];
                const w = hdr[1];
                const h = hdr[2];
                const channels = hdr[3] || 4;
                if (seq !== 0 && seq !== this._lastFrameSeq && w > 0 && h > 0 && w <= 4096 && h <= 4096) {
                  const neededBytes = w * h * channels;
                  if (buf.byteLength >= 16 + neededBytes) {
                    this._lastFrameSeq = seq;
                    this._sawNativeBroadcast = true;
                    decodedNewFrame = true;
                    const pixels = new Uint8Array(buf, 16, neededBytes);
                    if (channels === 4) {
                      this.ingestRGBA(pixels, w, h, now);
                    } else if (channels === 3) {
                      this.ingestFrameBgr({ w, h, c: 3, data: pixels }, now);
                    }
                  }
                }
              }
            }

            if (!decodedNewFrame) {
              const frameFresh = this.latestRawFrame !== null && (now - this.latestFrameTs) < 4.5;
              if (hdrTransport === "loopback_waiting" || (hdrTransport === "loopback" && hdrActive === "1")) {
                this._sawNativeBroadcast = true;
                if (!this.broadcastActive) {
                  this.broadcastActive = true;
                  statusDirty = true;
                }
              } else if (hdrActive === "0" && hdrTransport === "idle") {
                if (this._sawNativeBroadcast) {
                  this._sawNativeBroadcast = false;
                  this._lastFrameSeq = 0;
                  this.latestFrameTs = 0;
                  statusDirty = true;
                }
                if (this.broadcastActive) {
                  this.broadcastActive = false;
                  statusDirty = true;
                }
              } else if (this.broadcastActive && !frameFresh && hdrTransport === "appgroup") {
                this.broadcastActive = false;
                statusDirty = true;
              }

              if (statusDirty) {
                this.broadcast(this.buildStatusPayload(now));
              }
            }
          } catch (_) {
            // Silently ignore malformed or stale packet
          } finally {
            done();
          }
        };
        xhr.onerror = () => {
          noteXhrError();
          done();
        };
        xhr.ontimeout = done;
        xhr.onabort = done;
        try {
          xhr.send();
        } catch (_) {
          noteXhrError();
          done();
        }
      }, intervalMs);
    }

    stopNativePolling() {
      if (this._loopTimer) {
        clearInterval(this._loopTimer);
        this._loopTimer = null;
      }
    }

    _loadSavedPack() {
      try {
        if (typeof localStorage === "undefined") return null;
        return this._parsePack(localStorage.getItem(STORAGE_PACK_KEY));
      } catch (e) {
        return null;
      }
    }

    persistPack() {
      try {
        const pack = this.detector.exportPack();
        const raw = JSON.stringify(pack);
        if (this.onSavePack) {
          this.onSavePack(raw);
          return;
        }
        if (typeof localStorage === "undefined") return;
        localStorage.setItem(STORAGE_PACK_KEY, raw);
      } catch (e) {}
    }

    broadcast(payload) {
      let dataStr = null;
      for (const ws of this.clients) {
        if (ws.readyState !== 1) continue;
        try {
          if (typeof ws.onRawMessage === "function") {
            ws.onRawMessage(payload);
          } else if (typeof ws.onmessage === "function") {
            if (dataStr === null) dataStr = JSON.stringify(payload);
            ws.onmessage({ data: dataStr });
          }
        } catch (e) {}
      }
    }

    buildStatusPayload(now = performance.now() / 1000) {
      const frameFresh = this.latestRawFrame !== null && (now - this.latestFrameTs) < 4.5;
      return {
        type: "status",
        connected: true,
        windowFound: frameFresh || this.broadcastActive,
        paused: this.sm.paused,
        state: this.sm.state,
        extConnected: this.extConnected,
        ipadMode: true,
        broadcastActive: this.broadcastActive,
        transport: this.frameTransport,
        frameRotation: normalizeRotationDeg(this.cfg.frame_rotation || 0),
        frameInfo: this.frameInfo || "",
        hostInfo: this.hostInfo,
        keepalive: this.keepalive.getStatus(now),
      };
    }

    triggerSafariReload() {
      if (this._postNativeAction({ action: "reloadLeftSafari" }) && this.extConnected) {
        return [true, "safari_ext_reload"];
      }
      return [false, "alert_fallback"];
    }

    ingestFrameBgr(frameBgr, now = performance.now() / 1000) {
      if (!frameBgr || frameBgr.w === 0 || frameBgr.h === 0) return [];
      this._unrotatedRawFrame = frameBgr;
      const rot = normalizeRotationDeg(this.cfg.frame_rotation || 0);
      const procFrame = rot !== 0 ? rotateImage(frameBgr, rot) : frameBgr;
      this.latestRawFrame = procFrame;
      this.latestFrameTs = now;
      this.broadcastActive = true;

      const [ax, ay, aw, ah] = this.smoother.resolveAnchor(procFrame, this.cfg.anchor_mode);
      const anchorImg = cropImage(procFrame, ax, ay, aw, ah);
      if (anchorImg.w === 0 || anchorImg.h === 0) return [];

      const playerImg = cropNorm(anchorImg, this.cfg.player_strip);
      const dealerImg = cropNorm(anchorImg, this.cfg.dealer_strip);
      const bannerImg = cropNorm(anchorImg, this.cfg.banner_roi);
      const placeBetsImg = cropNorm(anchorImg, this.cfg.place_bets_roi);
      const shuffleImg = cropNorm(anchorImg, this.cfg.shuffle_roi);

      const obs = this.detector.processCrops({
        playerImg,
        dealerImg,
        shuffleImg,
        bannerImg,
        placeBetsImg,
        ts: now,
      });

      const events = this.sm.step(obs);
      const kaEvents = this.keepalive.step({
        now,
        windowFound: true,
        obs,
        smState: this.sm.state,
        playMode: this.sm.sync_play_mode,
        draftCount: this.sm.sync_draft_count,
        hasPrompt: this.sm.sync_has_prompt,
        betRoundsCount: this.sm.sync_bet_rounds_count,
        triggerRefreshCb: () => this.triggerSafariReload(),
      });

      const allEvents = [...events, ...kaEvents];
      for (const ev of allEvents) {
        this.broadcast(ev);
      }
      this.broadcast(this.buildStatusPayload(now));
      return allEvents;
    }

    ingestRGBA(rgbaBytes, width, height, now = performance.now() / 1000) {
      const bgr = fromRGBA(rgbaBytes, width, height);
      return this.ingestFrameBgr(bgr, now);
    }

    _buildCalibSnapshot(reqAnchor) {
      const rot = normalizeRotationDeg(this.cfg.frame_rotation || 0);
      if (this._unrotatedRawFrame) {
        this.latestRawFrame = rot !== 0 ? rotateImage(this._unrotatedRawFrame, rot) : this._unrotatedRawFrame;
      }
      if (!this.latestRawFrame) return null;
      const [ax, ay, aw, ah] =
        reqAnchor === "toolbar"
          ? [0, 0, this.latestRawFrame.w, Math.min(this.latestRawFrame.h, 110)]
          : this.smoother.resolveAnchor(this.latestRawFrame, reqAnchor);
      const snap = cropImage(this.latestRawFrame, ax, ay, aw, ah);
      this._calibSnapBgr = snap;
      this._calibSnapMode = reqAnchor;
      const hasDoc = typeof document !== "undefined";
      const payload = {
        type: "calib_snapshot",
        anchorMode: reqAnchor,
        width: snap.w,
        height: snap.h,
        frameRotation: rot,
        frameInfo: this.frameInfo || "",
        imageDataUrl: hasDoc ? bgrToDataUrl(snap) : "",
        rois: {
          player_strip: this.cfg.player_strip,
          dealer_strip: this.cfg.dealer_strip,
          community_roi: this.cfg.community_roi,
          dealer_felt_roi: this.cfg.dealer_felt_roi,
          banner_roi: this.cfg.banner_roi,
          place_bets_roi: this.cfg.place_bets_roi,
          shuffle_roi: this.cfg.shuffle_roi,
        },
        templates: this.detector.listTemplateCounts(),
        reloadBtnOffset: this.cfg.reload_btn_client_offset,
      };
      if (!hasDoc) {
        payload.snapRgba = bgrToRGBA(snap);
        payload.snapW = snap.w;
        payload.snapH = snap.h;
      }
      return payload;
    }

    handleClientMessage(ws, rawData) {
      let msg;
      try {
        msg = typeof rawData === "string" ? JSON.parse(rawData) : rawData;
      } catch (e) {
        return;
      }
      if (!msg || !msg.type) return;

      const reply = (obj) => {
        if (!ws || ws.readyState !== 1) return;
        if (typeof ws.onRawMessage === "function") {
          ws.onRawMessage(obj);
        } else if (typeof ws.onmessage === "function") {
          ws.onmessage({ data: JSON.stringify(obj) });
        }
      };

      const mtype = msg.type;
      if (mtype === "sync_state") {
        this.sm.setSyncState(msg);
        return;
      }
      if (mtype === "control") {
        const action = msg.action || "";
        if (action === "pause") this.sm.paused = true;
        else if (action === "resume") this.sm.paused = false;
        else if (action === "resync") this.sm.resyncRound();
        this.broadcast(this.buildStatusPayload());
        return;
      }
      if (mtype === "keepalive_action") {
        const kaAction = msg.action || "";
        let kaEvt = null;
        if (kaAction === "reset") kaEvt = this.keepalive.resetTimer(performance.now() / 1000, "ui_button");
        else if (kaAction === "refresh_now" || kaAction === "arm") kaEvt = this.keepalive.armNow();
        else if (kaAction === "configure" || kaAction === "set_config") kaEvt = this.keepalive.updateConfig(msg);
        if (kaEvt) this.broadcast(kaEvt);
        this.broadcast(this.buildStatusPayload());
        return;
      }
      if (mtype === "calib_set_rotation") {
        this.cfg.frame_rotation = normalizeRotationDeg(msg.rotation);
        this.smoother.reset();
        this.detector.invalidateCaches();
        if (this._unrotatedRawFrame) {
          this.latestRawFrame =
            this.cfg.frame_rotation !== 0
              ? rotateImage(this._unrotatedRawFrame, this.cfg.frame_rotation)
              : this._unrotatedRawFrame;
        }
        this.persistPack();
        const reqAnchor = ["ipad_split_left", "game_viewport", "window", "toolbar"].includes(msg.anchorMode)
          ? msg.anchorMode
          : this.cfg.anchor_mode;
        this.broadcast(this.buildStatusPayload());
        const snapPayload = this._buildCalibSnapshot(reqAnchor);
        if (snapPayload) {
          reply(snapPayload);
        } else {
          reply({
            type: "calib_saved",
            kind: "rotation",
            frameRotation: this.cfg.frame_rotation,
          });
        }
        return;
      }
      if (mtype === "calib_grab") {
        const reqAnchor = ["ipad_split_left", "game_viewport", "window", "toolbar"].includes(msg.anchorMode)
          ? msg.anchorMode
          : this.cfg.anchor_mode;
        this.smoother.reset();
        const snapPayload = this._buildCalibSnapshot(reqAnchor);
        if (!snapPayload) {
          reply({
            type: "calib_error",
            error: "No iPad Broadcast frame received yet — tap '📺 Broadcast Screen' in the top bar (or upload a test screenshot).",
          });
          return;
        }
        reply(snapPayload);
        return;
      }
      if (mtype === "calib_save_rois") {
        const rois = msg.rois || {};
        for (const k of ["player_strip", "dealer_strip", "community_roi", "dealer_felt_roi", "banner_roi", "place_bets_roi", "shuffle_roi"]) {
          if (Array.isArray(rois[k]) && rois[k].length === 4) {
            this.cfg[k] = normalizeRoi(rois[k]);
          }
        }
        if (["ipad_split_left", "game_viewport", "window"].includes(msg.anchorMode)) {
          this.cfg.anchor_mode = msg.anchorMode;
        }
        this.smoother.reset();
        this.detector.invalidateCaches();
        this.persistPack();
        reply({
          type: "calib_saved",
          kind: "rois",
          rois: {
            player_strip: this.cfg.player_strip,
            dealer_strip: this.cfg.dealer_strip,
            community_roi: this.cfg.community_roi,
            dealer_felt_roi: this.cfg.dealer_felt_roi,
            banner_roi: this.cfg.banner_roi,
            place_bets_roi: this.cfg.place_bets_roi,
            shuffle_roi: this.cfg.shuffle_roi,
          },
          anchorMode: this.cfg.anchor_mode,
          frameRotation: normalizeRotationDeg(this.cfg.frame_rotation || 0),
        });
        return;
      }
      if (mtype === "calib_save_template") {
        const snap = this._calibSnapBgr;
        if (!snap) {
          reply({ type: "calib_error", error: "Take a snapshot first before cropping a template." });
          return;
        }
        try {
          const crop = cropNorm(snap, msg.normCrop || [0, 0, 0.1, 0.1]);
          const saved = this.detector.saveTemplateCrop(msg.bank || "hud", msg.rank || "A", crop, !!msg.autoTrim);
          this.persistPack();
          reply({
            type: "calib_saved",
            kind: "template",
            bank: msg.bank || "hud",
            rank: String(msg.rank || "").toUpperCase(),
            filename: saved.filename,
            cropSize: [saved.w, saved.h],
            templates: this.detector.listTemplateCounts(),
            reloadBtnOffset: this.cfg.reload_btn_client_offset,
          });
        } catch (err) {
          reply({ type: "calib_error", error: err.message || String(err) });
        }
        return;
      }
      if (mtype === "calib_delete_rank") {
        const removed = this.detector.deleteRankTemplates(msg.bank || "hud", msg.rank || "");
        this.persistPack();
        reply({
          type: "calib_saved",
          kind: "delete_rank",
          bank: msg.bank || "hud",
          rank: String(msg.rank || "").toUpperCase(),
          removed,
          templates: this.detector.listTemplateCounts(),
        });
        return;
      }
      if (mtype === "calib_clear_bank") {
        const removed = this.detector.clearBankTemplates(msg.bank || "hud");
        this.persistPack();
        reply({
          type: "calib_saved",
          kind: "clear_bank",
          bank: msg.bank || "hud",
          removed,
          templates: this.detector.listTemplateCounts(),
        });
        return;
      }
      if (mtype === "calib_export_pack") {
        reply({
          type: "calib_pack_exported",
          pack: this.detector.exportPack(),
        });
        return;
      }
      if (mtype === "calib_import_pack") {
        try {
          const pack = msg.pack;
          if (!pack || typeof pack !== "object") throw new Error("Invalid companion pack JSON");
          if (pack.config) {
            Object.assign(this.cfg, defaultConfig(pack.config));
          }
          this.cfg.frame_rotation = normalizeRotationDeg(this.cfg.frame_rotation || 0);
          this.smoother.reset();
          this.detector.loadPack(pack);
          if (this._unrotatedRawFrame) {
            this.latestRawFrame =
              this.cfg.frame_rotation !== 0
                ? rotateImage(this._unrotatedRawFrame, this.cfg.frame_rotation)
                : this._unrotatedRawFrame;
          }
          this.persistPack();
          this.broadcast(this.buildStatusPayload());
          reply({
            type: "calib_pack_imported",
            summary: { templates: this.detector.listTemplateCounts() },
            rois: {
              player_strip: this.cfg.player_strip,
              dealer_strip: this.cfg.dealer_strip,
              community_roi: this.cfg.community_roi,
              dealer_felt_roi: this.cfg.dealer_felt_roi,
              banner_roi: this.cfg.banner_roi,
              place_bets_roi: this.cfg.place_bets_roi,
              shuffle_roi: this.cfg.shuffle_roi,
            },
            anchorMode: this.cfg.anchor_mode,
            frameRotation: this.cfg.frame_rotation,
            templates: this.detector.listTemplateCounts(),
          });
        } catch (err) {
          reply({ type: "calib_error", error: err.message || String(err) });
        }
        return;
      }
      if (mtype === "calib_test_detect") {
        const snap = this._calibSnapBgr;
        if (!snap) {
          reply({ type: "calib_error", error: "Take a snapshot first." });
          return;
        }
        const rois = msg.rois || {};
        const pRoi = normalizeRoi(rois.player_strip || this.cfg.player_strip);
        const dRoi = normalizeRoi(rois.dealer_strip || this.cfg.dealer_strip);
        const bRoi = normalizeRoi(rois.banner_roi || this.cfg.banner_roi);
        const pbRoi = normalizeRoi(rois.place_bets_roi || this.cfg.place_bets_roi);
        const shRoi = normalizeRoi(rois.shuffle_roi || this.cfg.shuffle_roi);

        this.detector.invalidateCaches();
        const obs = this.detector.processCrops({
          playerImg: cropNorm(snap, pRoi),
          dealerImg: cropNorm(snap, dRoi),
          bannerImg: cropNorm(snap, bRoi),
          placeBetsImg: cropNorm(snap, pbRoi),
          shuffleImg: cropNorm(snap, shRoi),
        });

        const toNormDets = (roi, dets) => {
          const [rx, ry, rw, rh] = roi;
          const pw = Math.max(1, Math.round(rw * snap.w));
          const ph = Math.max(1, Math.round(rh * snap.h));
          return (dets || []).map((d) => ({
            rank: d.rank,
            val: d.val,
            suit: d.suit,
            handIndex: d.hand_index || 0,
            conf: Number(d.conf.toFixed(3)),
            box: [
              Number((rx + (d.x / pw) * rw).toFixed(4)),
              Number((ry + (d.y / ph) * rh).toFixed(4)),
              Number(((d.w / pw) * rw).toFixed(4)),
              Number(((d.h / ph) * rh).toFixed(4)),
            ],
          }));
        };

        reply({
          type: "calib_test_result",
          player: toNormDets(pRoi, obs.player_cards),
          dealer: toNormDets(dRoi, obs.dealer_cards),
          communityFelt: [],
          dealerFelt: [],
          split: obs.split_detected,
          playerHands: obs.player_hands,
          banner: {
            state: obs.banner_state,
            conf: Number((obs.banner_conf || 0).toFixed(3)),
            source: obs.banner_source,
          },
          placeBets: {
            conf: Number((obs.place_bets_conf || 0).toFixed(3)),
          },
        });
      }
    }
  }

  let _sharedRuntime = null;
  function getSharedRuntime() {
    if (!_sharedRuntime) {
      _sharedRuntime = new IpadCompanionRuntime();
    }
    return _sharedRuntime;
  }

  class IpadCompanionSocket {
    constructor(runtime = null) {
      this.runtime = runtime || getSharedRuntime();
      this.readyState = 0; // CONNECTING
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;

      setTimeout(() => {
        if (this.readyState !== 0) return;
        this.readyState = 1; // OPEN
        this.runtime.clients.add(this);
        this.runtime.startNativePolling();
        if (typeof this.onopen === "function") {
          this.onopen({});
        }
        if (typeof this.onmessage === "function") {
          this.onmessage({ data: JSON.stringify(this.runtime.buildStatusPayload()) });
        }
      }, 15);
    }

    send(data) {
      if (this.readyState !== 1) return;
      this.runtime.handleClientMessage(this, data);
    }

    close() {
      if (this.readyState === 3) return;
      this.readyState = 3; // CLOSED
      this.runtime.clients.delete(this);
      if (this.runtime.clients.size === 0) {
        this.runtime.stopNativePolling();
      }
      if (typeof this.onclose === "function") {
        this.onclose({});
      }
    }
  }

  class WorkerCompanionSocket {
    constructor(options = {}) {
      this.options = options || {};
      this.readyState = 0; // CONNECTING
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;

      this._closed = false;
      this._worker = null;
      this._workerReady = false;
      this._fallback = null;
      this._fallbackPromoted = false;
      this.fallbackReason = "";
      this._bootTimer = null;

      this._lastHostInfo = null;
      this._lastExtConnected = null;
      this._lastSyncMsg = null;
      this._lastKaConfigMsg = null;

      this._installGlobalHooks();
      this._startBoot();
    }

    _installGlobalHooks() {
      if (typeof globalThis === "undefined") return;
      globalThis.__ipadCompanionReceiveRGBA = (rgbaBytes, width, height) => {
        if (this._closed) return;
        if (this._fallbackPromoted && this._fallback) {
          this._fallback.runtime.ingestRGBA(rgbaBytes, width, height);
          return;
        }
        if (this._worker && rgbaBytes) {
          let copy;
          if (rgbaBytes instanceof Uint8Array) {
            copy = new Uint8Array(rgbaBytes);
          } else if (ArrayBuffer.isView(rgbaBytes)) {
            copy = new Uint8Array(rgbaBytes.buffer.slice(rgbaBytes.byteOffset, rgbaBytes.byteOffset + rgbaBytes.byteLength));
          } else {
            copy = new Uint8Array(rgbaBytes);
          }
          try {
            this._worker.postMessage({ type: "ingest_rgba", rgba: copy, w: width, h: height }, [copy.buffer]);
          } catch (_) {}
        }
      };
      globalThis.__ipadCompanionSetExtConnected = (connected) => {
        this._lastExtConnected = !!connected;
        if (this._closed) return;
        if (this._fallbackPromoted && this._fallback) {
          this._fallback.runtime.extConnected = !!connected;
          this._fallback.runtime.broadcast(this._fallback.runtime.buildStatusPayload());
        } else if (this._workerReady && this._worker) {
          try {
            this._worker.postMessage({ type: "set_ext_connected", connected: !!connected });
          } catch (_) {}
        }
      };
      globalThis.__ipadCompanionSetHostInfo = (info) => {
        if (info && typeof info === "object") {
          this._lastHostInfo = Object.assign({}, this._lastHostInfo || {}, info);
        }
        if (this._closed || !this._lastHostInfo) return;
        if (this._fallbackPromoted && this._fallback) {
          this._fallback.runtime.hostInfo = Object.assign({}, this._fallback.runtime.hostInfo || {}, this._lastHostInfo);
          this._fallback.runtime.broadcast(this._fallback.runtime.buildStatusPayload());
        } else if (this._workerReady && this._worker) {
          try {
            this._worker.postMessage({ type: "set_host_info", info: this._lastHostInfo });
          } catch (_) {}
        }
      };
    }

    _resolveWorkerUrl() {
      if (this.options.workerUrl) return String(this.options.workerUrl);
      if (_ENGINE_SCRIPT_URL) {
        try {
          return new URL("companion_worker.js", _ENGINE_SCRIPT_URL).toString();
        } catch (_) {}
      }
      const proto = (typeof location !== "undefined" && location && location.protocol) || "";
      return proto === "bjtracker:" ? "/companion/ipad/companion_worker.js" : "companion/ipad/companion_worker.js";
    }

    _startBoot() {
      const WorkerCtor = this.options.WorkerClass || (typeof Worker === "function" ? Worker : null);
      const isFileProto = typeof location !== "undefined" && location && location.protocol === "file:";
      if (!WorkerCtor || (isFileProto && !this.options.WorkerClass)) {
        setTimeout(() => this._promoteFallback("worker_unavailable"), 0);
        return;
      }

      let workerUrl;
      try {
        workerUrl = this._resolveWorkerUrl();
      } catch (_) {
        setTimeout(() => this._promoteFallback("worker_url_error"), 0);
        return;
      }

      const timeoutMs = Math.max(50, Number(this.options.bootTimeoutMs) || 600);
      this._bootTimer = setTimeout(() => {
        if (!this._workerReady && !this._closed && !this._fallbackPromoted) {
          this._promoteFallback("boot_timeout");
        }
      }, timeoutMs);

      let worker;
      try {
        worker = new WorkerCtor(workerUrl);
      } catch (_) {
        clearTimeout(this._bootTimer);
        this._bootTimer = null;
        setTimeout(() => this._promoteFallback("worker_construct_failed"), 0);
        return;
      }
      this._worker = worker;

      worker.onmessage = (ev) => {
        if (this._closed || this._fallbackPromoted) return;
        const msg = ev && ev.data;
        if (!msg || !msg.type) return;

        if (msg.type === "worker_ready") {
          if (this._bootTimer) {
            clearTimeout(this._bootTimer);
            this._bootTimer = null;
          }
          this._workerReady = true;
          if (this._lastHostInfo !== null) {
            try { worker.postMessage({ type: "set_host_info", info: this._lastHostInfo }); } catch (_) {}
          }
          if (this._lastExtConnected !== null) {
            try { worker.postMessage({ type: "set_ext_connected", connected: this._lastExtConnected }); } catch (_) {}
          }
          if (this.readyState === 0) {
            this.readyState = 1;
            if (typeof this.onopen === "function") {
              this.onopen({});
            }
          }
          if (msg.status && typeof this.onmessage === "function" && this.readyState === 1) {
            this.onmessage({ data: JSON.stringify(msg.status) });
          }
          return;
        }

        if (msg.type === "worker_save_pack") {
          try {
            if (typeof localStorage !== "undefined" && typeof msg.packJson === "string") {
              localStorage.setItem(STORAGE_PACK_KEY, msg.packJson);
            }
          } catch (_) {}
          return;
        }

        if (msg.type === "worker_native_action") {
          if (this.options.onNativeAction) {
            try { this.options.onNativeAction(msg.payload); } catch (_) {}
          } else if (
            typeof window !== "undefined" &&
            window.webkit &&
            window.webkit.messageHandlers &&
            window.webkit.messageHandlers.ipadCompanion
          ) {
            try {
              window.webkit.messageHandlers.ipadCompanion.postMessage(msg.payload);
            } catch (_) {}
          }
          return;
        }

        if (msg.type === "worker_fallback") {
          this._promoteFallback(msg.reason || "worker_fallback");
          return;
        }

        if (msg.type === "ws_message_str") {
          if (this.readyState === 1 && typeof this.onmessage === "function") {
            this.onmessage({ data: msg.data });
          }
          return;
        }

        if (msg.type === "ws_message_snapshot") {
          if (this.readyState === 1 && typeof this.onmessage === "function") {
            const payload = Object.assign({}, msg.payload || {});
            if (!payload.imageDataUrl && msg.snapRgba && payload.snapW && payload.snapH) {
              payload.imageDataUrl = rgbaToDataUrl(msg.snapRgba, payload.snapW, payload.snapH);
            }
            this.onmessage({ data: JSON.stringify(payload) });
          }
        }
      };

      worker.onerror = () => {
        if (!this._closed && !this._fallbackPromoted) {
          this._promoteFallback("worker_error");
        }
      };

      const hasWkHandler =
        typeof window !== "undefined" &&
        !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ipadCompanion);
      const proto = (typeof location !== "undefined" && location && location.protocol) || "";
      const frameUrlBase =
        this.options.frameUrlBase ||
        (proto === "bjtracker:" ? "/companion-frame/latest" : "companion-frame://latest");
      let savedPack = "";
      try {
        if (typeof localStorage !== "undefined") {
          savedPack = localStorage.getItem(STORAGE_PACK_KEY) || "";
        }
      } catch (_) {}

      try {
        worker.postMessage({
          type: "init",
          isNativeHost: this.options.isNativeHost !== undefined ? !!this.options.isNativeHost : hasWkHandler,
          frameUrlBase,
          savedPack,
          preserveDesktopAnchor: !!this.options.preserveDesktopAnchor,
        });
      } catch (_) {
        this._promoteFallback("worker_init_post_failed");
      }
    }

    _replayToFallback(fb) {
      if (!fb || !fb.runtime) return;
      if (this._lastExtConnected !== null) {
        fb.runtime.extConnected = !!this._lastExtConnected;
      }
      if (this._lastHostInfo) {
        fb.runtime.hostInfo = Object.assign({}, fb.runtime.hostInfo || {}, this._lastHostInfo);
      }
      if (this._lastSyncMsg !== null) {
        fb.runtime.handleClientMessage(fb, this._lastSyncMsg);
      }
      if (this._lastKaConfigMsg !== null) {
        fb.runtime.handleClientMessage(fb, this._lastKaConfigMsg);
      }
    }

    _promoteFallback(reason = "fallback") {
      if (this._closed || this._fallbackPromoted) return;
      this._fallbackPromoted = true;
      this.fallbackReason = reason;
      if (this._bootTimer) {
        clearTimeout(this._bootTimer);
        this._bootTimer = null;
      }
      if (this._worker) {
        try {
          if (typeof this._worker.terminate === "function") this._worker.terminate();
        } catch (_) {}
        this._worker = null;
      }

      const wasOpen = this.readyState === 1;
      const fb = new IpadCompanionSocket(this.options.fallbackRuntime || null);
      this._fallback = fb;
      fb.onopen = () => {
        if (this._closed) return;
        this._replayToFallback(fb);
        if (!wasOpen) {
          this.readyState = 1;
          if (typeof this.onopen === "function") {
            this.onopen({});
          }
        }
      };
      fb.onmessage = (ev) => {
        if (this._closed) return;
        if (typeof this.onmessage === "function") {
          this.onmessage(ev);
        }
      };
      fb.onerror = (ev) => {
        if (!this._closed && typeof this.onerror === "function") {
          this.onerror(ev);
        }
      };
      fb.onclose = (ev) => {
        if (this._closed) return;
        this.readyState = 3;
        if (typeof this.onclose === "function") {
          this.onclose(ev);
        }
      };
    }

    send(data) {
      if (this.readyState !== 1 || this._closed) return;
      try {
        const parsed = typeof data === "string" ? JSON.parse(data) : data;
        if (parsed && parsed.type === "sync_state") {
          this._lastSyncMsg = data;
        } else if (parsed && parsed.type === "keepalive_action" && (parsed.action === "set_config" || parsed.action === "configure")) {
          this._lastKaConfigMsg = data;
        }
      } catch (_) {}

      if (this._fallbackPromoted && this._fallback) {
        this._fallback.send(data);
        return;
      }
      if (this._worker) {
        try {
          this._worker.postMessage({ type: "ws_send", data });
        } catch (_) {}
      }
    }

    close() {
      if (this.readyState === 3) return;
      this.readyState = 3; // CLOSED
      this._closed = true;
      if (this._bootTimer) {
        clearTimeout(this._bootTimer);
        this._bootTimer = null;
      }
      if (this._worker) {
        try { this._worker.postMessage({ type: "shutdown" }); } catch (_) {}
        try {
          if (typeof this._worker.terminate === "function") this._worker.terminate();
        } catch (_) {}
        this._worker = null;
      }
      if (this._fallback) {
        this._fallback.onclose = null;
        this._fallback.close();
        this._fallback = null;
      }
      if (typeof this.onclose === "function") {
        this.onclose({});
      }
    }
  }

  return {
    RANK_TO_VAL,
    normalizeRoi,
    normalizeRotationDeg,
    defaultConfig,
    createImage,
    fromRGBA,
    bgrToRGBA,
    toGray,
    toMinGray,
    grayToBGR,
    cropImage,
    cropNorm,
    resizeImage,
    rotate180,
    rotateImage,
    copyMakeBorder,
    prepareSourceForMatch,
    matchPreparedTemplate,
    matchTemplateCCoeffNormed,
    connectedComponentsWithStats,
    detectGameViewportLocal,
    detectSplitViewLeftPane,
    extractFeltCardRect,
    extractFeltRankViews,
    trimCardGlyph,
    findWhiteCardClusters,
    detectClusterCards,
    PlaytechDetector,
    SequenceDebouncer,
    CompanionStateMachine,
    WatchKeepaliveManager,
    ViewportSmoother,
    IpadCompanionRuntime,
    IpadCompanionSocket,
    WorkerCompanionSocket,
    getSharedRuntime,
  };
});


#!/usr/bin/env python3
"""Deterministic generator and validator for companion/ipad/default_pack.js.

Reads PNG templates from companion/templates/{hud,felt,banner,suit,ui} and builds
companion/ipad/default_pack.js with iPadOS default ROIs.

Usage:
  python3 companion/ipad/build_default_pack.py          # regenerate default_pack.js
  python3 companion/ipad/build_default_pack.py --check  # verify templates & pack sync
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
COMPANION_DIR = ROOT_DIR / "companion"
TEMPLATES_DIR = COMPANION_DIR / "templates"
OUTPUT_JS = COMPANION_DIR / "ipad" / "default_pack.js"
BANKS = ("hud", "felt", "banner", "suit", "ui")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# iPadOS Split-View default configuration embedded in default_pack.js
IPAD_DEFAULT_CONFIG = {
    "window_titles": ["Live Games", "All Bets Blackjack", "epicworld", "Playtech"],
    "anchor_mode": "game_viewport",
    "ws_host": "127.0.0.1",
    "ws_port": 8765,
    "fps": 15,
    "burst_fps": 30,
    "confirm_frames": 3,
    "clear_frames": 4,
    "shuffle_hold_sec": 2,
    "min_hud_confidence": 0.84,
    "min_felt_confidence": 0.78,
    "min_banner_confidence": 0.74,
    "hud_nms_min_dx_ratio": 0.018,
    "hud_dedup_window_sec": 2.8,
    "enable_felt_community": True,
    "enable_dealer_felt_fallback": True,
    "require_wait_banner": True,
    "pause_on_unmatched_felt": False,
    "min_auto_confidence": 0.7,
    "rois": {
        "player_strip": [0.22, 0.66, 0.56, 0.22],
        "dealer_strip": [0.02, 0.27, 0.38, 0.22],
        "community_roi": [0.4086, 0.6179, 0.2417, 0.0782],
        "dealer_felt_roi": [0.4384, 0.5453, 0.1681, 0.0838],
        "banner_roi": [0.35, 0, 0.3, 0.06],
        "place_bets_roi": [0.2, 0.36, 0.6, 0.18],
        "shuffle_roi": [0.2582, 0, 0.5095, 0.1264],
        "shoe_roi": [0.77, 0.52, 0.14, 0.11],
        "discard_roi": [0.11, 0.45, 0.22, 0.22],
    },
    "shoe_detect": {
        "enabled": True,
        "enable_shoe_roi": True,
        "enable_discard_fallback": True,
        "shoe_present_hold_sec": 2.0,
        "shoe_absent_hold_sec": 3.5,
        "shoe_return_hold_sec": 1.5,
        "shoe_corroborated_absent_sec": 1.8,
        "shoe_max_flicker_sec": 0.7,
        "shoe_absent_safety_sec": 12.0,
        "shoe_min_cards": 15,
        "discard_peak_white_min": 0.022,
        "discard_empty_white_max": 0.007,
        "discard_empty_hold_sec": 4.5,
        "discard_housing_dark_min": 0.10,
        "discard_min_cards": 45,
        "cooldown_sec": 30.0,
    },
    "felt_motion": {
        "min_contour_area_ratio": 0.015,
        "min_brightness": 145,
        "pulse_cooldown_sec": 0.35,
        "settle_timeout_sec": 0.5,
        "angles_deg": [-15, -10, -5, 0, 5, 10, 15],
        "scales": [0.75, 0.85, 0.92, 1, 1.08, 1.18, 1.3],
    },
    "keepalive": {
        "enabled": True,
        "mode": "auto",
        "method": "ext_then_os",
        "prefer_left_window": True,
        "min_watch_sec": 570,
        "max_watch_sec": 630,
        "hard_warn_sec": 750,
        "round_over_delay_sec": 1.2,
        "place_bets_min_delay_sec": 0.35,
        "place_bets_max_delay_sec": 0.85,
        "place_bets_latest_sec": 5,
        "reload_grace_sec": 12,
        "reload_timeout_sec": 20,
        "appcommand_verify_sec": 1.2,
        "reset_on_bet_round": True,
        "reload_btn_offset": None,
    },
}


def decode_png_to_bgr(raw: bytes) -> tuple[int, int, bytes]:
    """Decode an 8-bit non-interlaced PNG (color types 0, 2, 4, 6) into (w, h, bgr_bytes)."""
    if len(raw) < 24 or not raw.startswith(PNG_MAGIC):
        raise ValueError("Invalid PNG header")
    pos = 8
    idat = bytearray()
    w = h = bd = ct = interlace = 0
    while pos + 8 <= len(raw):
        ln = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        chunk = raw[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
        if ctype == b"IHDR":
            w, h, bd, ct, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk[:13])
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
    if w <= 0 or h <= 0 or bd != 8 or interlace != 0:
        raise ValueError(f"Unsupported PNG format: {w}x{h} bd={bd} ct={ct} interlace={interlace}")
    bpp_map = {0: 1, 2: 3, 4: 2, 6: 4}
    if ct not in bpp_map:
        raise ValueError(f"Unsupported PNG color type: {ct}")
    bpp = bpp_map[ct]
    decompressed = zlib.decompress(bytes(idat))
    stride = w * bpp
    expected_len = h * (stride + 1)
    if len(decompressed) < expected_len:
        raise ValueError("Truncated PNG IDAT payload")

    prev = bytearray(stride)
    bgr = bytearray(w * h * 3)
    out_i = 0
    for y in range(h):
        row_start = y * (stride + 1)
        ft = decompressed[row_start]
        row = bytearray(decompressed[row_start + 1 : row_start + 1 + stride])
        if ft == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 255
        elif ft != 0:
            raise ValueError(f"Unknown PNG filter type: {ft}")
        prev = row

        if ct == 0:  # Grayscale
            for x in range(w):
                v = row[x]
                bgr[out_i] = v
                bgr[out_i + 1] = v
                bgr[out_i + 2] = v
                out_i += 3
        elif ct == 4:  # Grayscale + Alpha
            for x in range(0, stride, 2):
                v = row[x]
                bgr[out_i] = v
                bgr[out_i + 1] = v
                bgr[out_i + 2] = v
                out_i += 3
        elif ct == 2:  # RGB -> BGR
            for x in range(0, stride, 3):
                bgr[out_i] = row[x + 2]
                bgr[out_i + 1] = row[x + 1]
                bgr[out_i + 2] = row[x]
                out_i += 3
        elif ct == 6:  # RGBA -> BGR
            for x in range(0, stride, 4):
                bgr[out_i] = row[x + 2]
                bgr[out_i + 1] = row[x + 1]
                bgr[out_i + 2] = row[x]
                out_i += 3
    return w, h, bytes(bgr)


def collect_templates() -> tuple[dict[str, list[dict[str, object]]], list[str], list[str]]:
    templates: dict[str, list[dict[str, object]]] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for bank in BANKS:
        bank_dir = TEMPLATES_DIR / bank
        entries: list[dict[str, object]] = []
        seen_hashes: dict[str, str] = {}
        if bank_dir.is_dir():
            for png_path in sorted(bank_dir.glob("*.png"), key=lambda p: p.name):
                raw = png_path.read_bytes()
                try:
                    w, h, bgr_bytes = decode_png_to_bgr(raw)
                except Exception as exc:
                    errors.append(f"Invalid PNG file {bank}/{png_path.name}: {exc}")
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                if digest in seen_hashes:
                    errors.append(
                        f"Duplicate PNG in bank '{bank}': {png_path.name} is byte-identical to {seen_hashes[digest]}"
                    )
                    continue
                seen_hashes[digest] = png_path.name
                entries.append(
                    {
                        "name": png_path.name,
                        "dataB64": base64.b64encode(raw).decode("ascii"),
                        "w": w,
                        "h": h,
                        "bgrB64": base64.b64encode(bgr_bytes).decode("ascii"),
                    }
                )
        templates[bank] = entries

    banner_names = [str(e["name"]).upper() for e in templates.get("banner", [])]
    if not any(n.startswith("SHUFFLE") for n in banner_names):
        warnings.append("No SHUFFLE_*.png template in companion/templates/banner/ (shuffle_active requires manual calibration)")

    return templates, errors, warnings


def build_js_content(templates: dict[str, list[dict[str, str]]]) -> str:
    total = sum(len(v) for v in templates.values())
    pack = {
        "version": 1,
        "exportedAt": 1794743004,
        "config": IPAD_DEFAULT_CONFIG,
        "templates": templates,
    }
    payload = json.dumps(pack, separators=(",", ":"), ensure_ascii=True)
    return (
        f"// Auto-generated default Playtech All Bets companion pack (config + {total} templates)\n"
        f"// Regenerate with: python3 companion/ipad/build_default_pack.py\n"
        f"const DEFAULT_COMPANION_PACK = {payload};\n"
        f'if (typeof window !== "undefined") {{ window.DEFAULT_COMPANION_PACK = DEFAULT_COMPANION_PACK; }}\n'
        f'if (typeof self !== "undefined") {{ self.DEFAULT_COMPANION_PACK = DEFAULT_COMPANION_PACK; }}\n'
        f'if (typeof module !== "undefined" && module.exports) {{ module.exports = {{ DEFAULT_COMPANION_PACK }}; }}\n'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or validate companion/ipad/default_pack.js")
    parser.add_argument("--check", action="store_true", help="Validate templates and check default_pack.js is up to date")
    args = parser.parse_args()

    templates, errors, warnings = collect_templates()
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    expected = build_js_content(templates)
    total = sum(len(v) for v in templates.values())

    if args.check:
        if not OUTPUT_JS.exists():
            print(f"ERROR: {OUTPUT_JS} does not exist.", file=sys.stderr)
            return 1
        actual = OUTPUT_JS.read_text(encoding="utf-8")
        if actual != expected:
            print(
                f"ERROR: {OUTPUT_JS} is out of sync with companion/templates/. "
                f"Run: python3 companion/ipad/build_default_pack.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {OUTPUT_JS.name} is valid and in sync ({total} templates, 0 duplicates).")
        return 0

    OUTPUT_JS.write_text(expected, encoding="utf-8")
    print(f"Generated {OUTPUT_JS} ({total} templates, {len(expected)} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

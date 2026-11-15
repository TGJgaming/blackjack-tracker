"""Interactive ROI calibrator, template store helpers, and CLI fallback."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from config import COMPANION_DIR, CompanionConfig, RANK_TO_VAL, load_config, normalize_roi, save_config
from win_anchor import WindowCapturer, find_target_window, get_primary_work_area, set_dpi_aware

ROI_ORDER = [
    ("player_strip", "Drag over Player Digital HUD Strip (bottom-center cards)"),
    ("dealer_strip", "Drag over Dealer Digital HUD Strip (left floating cards)"),
    ("community_roi", "Drag over Physical Felt Community Zone (below BLACKJACK)"),
    ("dealer_felt_roi", "Drag over Physical Felt Dealer Zone (top-center felt)"),
    ("shuffle_roi", "Drag over Center Shuffle / New Shoe Banner area"),
    ("banner_roi", "Drag over Top Status Banner (Wait / Last Bets / No More Bets)"),
    ("place_bets_roi", "Drag over Center PLACE YOUR BETS banner"),
]

BANNER_TOKENS = {"WAIT", "LAST_BETS", "NO_MORE_BETS", "PLACE_BETS", "SHUFFLE"}
SUIT_TOKENS = {"S", "H", "C", "D"}
UI_TOKENS = {"RELOAD_BTN"}

HARVEST_KEYMAP = {
    ord("a"): "A",
    ord("2"): "2",
    ord("3"): "3",
    ord("4"): "4",
    ord("5"): "5",
    ord("6"): "6",
    ord("7"): "7",
    ord("8"): "8",
    ord("9"): "9",
    ord("0"): "10",
    ord("t"): "10",
    ord("j"): "J",
    ord("q"): "Q",
    ord("k"): "K",
    ord("b"): "BACK",
    ord("s"): "SHUFFLE",
}


def _trim_card_glyph(crop: np.ndarray, bank_name: str) -> np.ndarray:
    """Cleanly isolate and scale a rank or suit glyph to canonical H=45 proportions."""
    if crop is None or crop.size == 0:
        return crop
    img = crop.copy()
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    mb = img.min(axis=2)
    mask_w = (mb > 155).astype(np.uint8)
    num_w, _, stats_w, _ = cv2.connectedComponentsWithStats(mask_w)
    best_card = None
    best_area = 0
    for i in range(1, num_w):
        x, y, w, h, area = [int(v) for v in stats_w[i]]
        if h >= 14 and area > best_area:
            best_area = area
            best_card = (x, y, w, h)

    work = img
    if best_card is not None:
        cx, cy, cw, ch = best_card
        card = img[cy : cy + ch, cx : cx + cw]
        if ch != 45:
            sc = 45.0 / ch
            interp = cv2.INTER_AREA if sc < 1.0 else cv2.INTER_CUBIC
            card = cv2.resize(card, (max(1, int(round(cw * sc))), 45), interpolation=interp)
        if bank_name == "hud":
            work = card[1:19, : min(card.shape[1], 22)]
        elif bank_name == "suit":
            work = card[28:45, : min(card.shape[1], 22)]

    w_mb = work.min(axis=2)
    h_w = w_mb.shape[0]
    ink = (w_mb < 180).astype(np.uint8)
    n_k, labels, st_k, _ = cv2.connectedComponentsWithStats(ink)
    if n_k <= 1:
        return work
    max_a = max(int(st_k[i, cv2.CC_STAT_AREA]) for i in range(1, n_k))
    keep = np.zeros_like(ink, dtype=bool)
    for i in range(1, n_k):
        _x, _y, bw, bh, area = [int(v) for v in st_k[i]]
        if bw <= 2 and bh >= max(4, h_w - 4):
            continue
        if area < max(10, int(max_a * 0.28)):
            continue
        keep |= (labels == i)
    ys, xs = np.where(keep)
    if len(ys) == 0:
        return work
    cleaned = np.full_like(work, 255)
    cleaned[keep] = work[keep]
    sub = cleaned[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]
    pad = 1
    out = np.full((sub.shape[0] + 2 * pad, sub.shape[1] + 2 * pad, 3), 255, dtype=np.uint8)
    out[pad:-pad, pad:-pad] = sub
    return out


def save_template_crop(
    bank_name: str,
    rank: str,
    crop: np.ndarray,
    templates_root: Path | None = None,
    auto_trim: bool = False,
) -> Path:
    """Save a crisp card/suit/banner crop into templates/<bank>/<RANK>_<N>.png."""
    rank_up = rank.strip().upper()
    if bank_name == "banner":
        if rank_up not in BANNER_TOKENS:
            raise ValueError(f"Unsupported banner token: {rank}")
    elif bank_name == "ui":
        if rank_up not in UI_TOKENS:
            raise ValueError(f"Unsupported UI token: {rank}")
    elif bank_name == "suit":
        if rank_up not in SUIT_TOKENS:
            raise ValueError(f"Unsupported suit token: {rank}")
    elif rank_up not in RANK_TO_VAL:
        raise ValueError(f"Unsupported rank label: {rank}")
    if bank_name not in ("hud", "felt", "banner", "suit", "ui"):
        raise ValueError(f"Unsupported template bank: {bank_name}")
    root = templates_root or (COMPANION_DIR / "templates")
    folder = root / bank_name
    folder.mkdir(parents=True, exist_ok=True)
    if crop is None or crop.size == 0 or float(np.std(crop)) < 1.0:
        raise ValueError("Selected crop has no contrast (blank region)")
    out_img = _trim_card_glyph(crop, bank_name) if (auto_trim and bank_name in ("hud", "suit")) else crop
    if bank_name == "hud":
        # Preserve red-ink contrast on white background by saving min(B,G,R) monochrome
        mono = out_img.min(axis=2) if out_img.ndim == 3 else out_img
        out_img = cv2.cvtColor(mono, cv2.COLOR_GRAY2BGR)
    elif bank_name in ("banner", "ui") and out_img.ndim == 3:
        out_img = cv2.cvtColor(out_img, cv2.COLOR_BGR2GRAY)
    idx = 1
    while (folder / f"{rank_up}_{idx}.png").exists():
        idx += 1
    out = folder / f"{rank_up}_{idx}.png"
    cv2.imwrite(str(out), out_img)
    return out


def list_template_counts(templates_root: Path | None = None) -> dict[str, dict[str, int]]:
    """Return {bank: {rank: count}} summary of saved templates."""
    root = templates_root or (COMPANION_DIR / "templates")
    summary: dict[str, dict[str, int]] = {"hud": {}, "felt": {}, "banner": {}, "suit": {}, "ui": {}}
    for bank in ("hud", "felt", "banner", "suit", "ui"):
        folder = root / bank
        if not folder.exists():
            continue
        counts: dict[str, int] = {}
        for png in sorted(folder.glob("*.png")):
            stem_upper = png.stem.upper()
            if bank == "banner":
                matched = None
                for btok in sorted(BANNER_TOKENS, key=len, reverse=True):
                    if stem_upper == btok or stem_upper.startswith(btok + "_"):
                        matched = btok
                        break
                if matched:
                    counts[matched] = counts.get(matched, 0) + 1
            elif bank == "ui":
                matched_ui = None
                for utok in UI_TOKENS:
                    if stem_upper == utok or stem_upper.startswith(utok + "_"):
                        matched_ui = utok
                        break
                if matched_ui:
                    counts[matched_ui] = counts.get(matched_ui, 0) + 1
            elif bank == "suit":
                tok = stem_upper.split("_")[0]
                if tok in SUIT_TOKENS:
                    counts[tok] = counts.get(tok, 0) + 1
            else:
                tok = stem_upper.split("_")[0]
                if tok in RANK_TO_VAL:
                    counts[tok] = counts.get(tok, 0) + 1
        summary[bank] = counts
    return summary


def delete_rank_templates(bank_name: str, rank: str, templates_root: Path | None = None) -> int:
    """Remove all saved templates for one rank/token in one bank; returns deleted count."""
    root = templates_root or (COMPANION_DIR / "templates")
    folder = root / bank_name
    if not folder.exists():
        return 0
    tok = rank.strip().upper()
    removed = 0
    for png in list(folder.glob(f"{tok}_*.png")) + list(folder.glob(f"{tok}.png")):
        png.unlink(missing_ok=True)
        removed += 1
    return removed


def clear_bank_templates(bank_name: str, templates_root: Path | None = None) -> int:
    """Delete all .png templates inside one bank folder; returns removed count."""
    if bank_name not in ("hud", "felt", "banner", "suit", "ui"):
        raise ValueError(f"Unsupported template bank: {bank_name}")
    root = templates_root or (COMPANION_DIR / "templates")
    folder = root / bank_name
    if not folder.exists():
        return 0
    removed = 0
    for png in list(folder.glob("*.png")):
        png.unlink(missing_ok=True)
        removed += 1
    return removed


def _fit_for_display(frame: np.ndarray, max_frac: float = 0.78) -> tuple[np.ndarray, float]:
    """Scale a frame down symmetrically only when larger than comfortable work area."""
    sw, sh = get_primary_work_area()
    h, w = frame.shape[:2]
    scale = min(1.0, (sw * max_frac) / max(1, w), (sh * max_frac) / max(1, h))
    if scale >= 0.99:
        return frame, 1.0
    dw, dh = max(64, int(round(w * scale))), max(64, int(round(h * scale)))
    return cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA), scale


def calibrate_rois(cfg: CompanionConfig, frame: np.ndarray) -> CompanionConfig:
    """Fit-to-screen interactive OpenCV ROI selector updating normalized ROIs on config."""
    set_dpi_aware()
    h, w = frame.shape[:2]
    disp_base, scale = _fit_for_display(frame)
    print("\n=== Interactive ROI Calibration (Scaled Fit) ===")
    print("Tip: You can also use the built-in browser Calibrator inside index.html!")
    print("Draw each box with mouse and press ENTER/SPACE. Press C or ESC to keep existing box.\n")

    win_name = "Playtech ROI Calibrator"
    for key, hint in ROI_ORDER:
        rx, ry, rw, rh = cfg.rois[key]
        x0, y0 = int(round(rx * w * scale)), int(round(ry * h * scale))
        bw, bh = int(round(rw * w * scale)), int(round(rh * h * scale))
        preview = disp_base.copy()
        cv2.rectangle(preview, (x0, y0), (x0 + bw, y0 + bh), (0, 220, 255), 2)
        cv2.putText(preview, f"{key}: {hint}", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 180), 2)
        cv2.putText(preview, "ENTER=confirm | C/ESC=keep yellow box", (10, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (220, 220, 220), 1)

        cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(win_name, preview.shape[1], preview.shape[0])
        box = cv2.selectROI(win_name, preview, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(win_name)
        bx, by, bwidth, bheight = [int(v) for v in box]
        if bwidth >= 8 and bheight >= 8:
            cfg.rois[key] = normalize_roi((bx / (w * scale), by / (h * scale), bwidth / (w * scale), bheight / (h * scale)))
            print(f"  [OK] {key} -> {[round(v, 4) for v in cfg.rois[key]]}")
        else:
            print(f"  [KEPT] {key} -> {[round(v, 4) for v in cfg.rois[key]]}")

    saved = save_config(cfg)
    print(f"\nSaved config to {saved}")
    return cfg


def harvest_templates(cfg: CompanionConfig, frame: np.ndarray, bank_name: str = "hud") -> None:
    """Let user crop card corner / shuffle templates from frame and tag with a keystroke."""
    set_dpi_aware()
    disp, scale = _fit_for_display(frame)
    print(f"\n=== Template Harvester ({bank_name}) ===")
    print("Drag a tight box around a card's TOP-LEFT rank+suit corner (or SHUFFLE banner), press ENTER.")
    print("Then press rank key: A, 2-9, 0/T (10), J, Q, K, B (BACK), S (SHUFFLE). Press ESC in ROI box when done.\n")

    win_name = f"Harvest [{bank_name}] - Drag tight rank corner (ESC=done)"
    while True:
        cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(win_name, disp.shape[1], disp.shape[0])
        box = cv2.selectROI(win_name, disp, showCrosshair=True, fromCenter=False)
        cv2.destroyAllWindows()
        bx, by, bw, bh = [int(v) for v in box]
        if bw < 6 or bh < 6:
            break
        # Map display coords back to crisp lossless 1:1 pixels
        ox0 = max(0, int(round(bx / scale)))
        oy0 = max(0, int(round(by / scale)))
        ox1 = min(frame.shape[1], int(round((bx + bw) / scale)))
        oy1 = min(frame.shape[0], int(round((by + bh) / scale)))
        crop = frame[oy0:oy1, ox0:ox1]
        zoom = cv2.resize(crop, (max(160, (ox1 - ox0) * 4), max(160, (oy1 - oy0) * 4)), interpolation=cv2.INTER_NEAREST)
        cv2.putText(zoom, "Key: A,2-9,0/T,J,Q,K,B=BACK,S=SHUF | ESC=skip", (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1)
        cv2.imshow("Label Crop", zoom)
        key = cv2.waitKey(0) & 0xFF
        cv2.destroyWindow("Label Crop")
        if key == 27:
            continue
        ch = HARVEST_KEYMAP.get(key)
        if ch and ch in RANK_TO_VAL:
            try:
                out = save_template_crop(bank_name, ch, crop)
                print(f"  Saved template: {out.name} ({ox1 - ox0}x{oy1 - oy0})")
            except ValueError as exc:
                print(f"  Skipped: {exc}")
        else:
            print("  Unknown key; skipped.")


def _grab_source_frame(cfg: CompanionConfig, image_path: Path | None) -> np.ndarray:
    set_dpi_aware()
    if image_path is not None:
        img = cv2.imread(str(image_path))
        if img is None:
            raise FileNotFoundError(f"Could not read image: {image_path}")
        return img

    win = find_target_window(cfg.window_titles)
    if win is None:
        raise RuntimeError(
            f"No visible browser window matched {cfg.window_titles}. Open Playtech All Bets in Brave/Chrome or pass --image."
        )
    cap = WindowCapturer()
    try:
        return cap.grab_window(win)
    finally:
        cap.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate Playtech ROIs and harvest HUD/Felt templates.")
    parser.add_argument("--image", type=Path, help="Optional PNG/JPG screenshot instead of live browser window")
    parser.add_argument("--rois", action="store_true", help="Run ROI box calibrator")
    parser.add_argument("--harvest", choices=["hud", "felt", "banner"], help="Harvest card/shuffle/banner templates into templates/<bank>")
    parser.add_argument("--crop-roi", choices=list(CompanionConfig().rois.keys()), help="Zoom into one saved ROI while harvesting")
    args = parser.parse_args()

    cfg = load_config()
    frame = _grab_source_frame(cfg, args.image)

    run_rois = args.rois or (args.harvest is None)
    if run_rois:
        cfg = calibrate_rois(cfg, frame)

    if args.harvest:
        h, w = frame.shape[:2]
        if args.crop_roi:
            rx, ry, rw, rh = cfg.rois[args.crop_roi]
            x0, y0 = int(rx * w), int(ry * h)
            x1, y1 = min(w, x0 + int(rw * w)), min(h, y0 + int(rh * h))
            sub = frame[y0:y1, x0:x1]
        else:
            sub = frame
        harvest_templates(cfg, sub, bank_name=args.harvest)


if __name__ == "__main__":
    main()

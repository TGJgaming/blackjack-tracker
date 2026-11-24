"""OpenCV template matching for Playtech digital HUD strips and physical felt burst capture."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

# Disable OpenCV OpenCL GPU dispatch to prevent GPU queue stalls and contention with Brave video decoding.
try:
    cv2.ocl.setUseOpenCL(False)
except Exception:
    pass

from config import COMPANION_DIR, CompanionConfig, RANK_TO_VAL


BANNER_TOKENS: set[str] = {"WAIT", "LAST_BETS", "NO_MORE_BETS", "PLACE_BETS", "SHUFFLE"}
SUIT_TOKENS: set[str] = {"S", "H", "C", "D"}


@dataclass(frozen=True)
class CardDetection:
    rank: str
    val: int | None
    conf: float
    x: int
    y: int = 0
    w: int = 0
    h: int = 0
    suit: str | None = None
    hand_index: int = 0
    has_pip_body: bool = False


@dataclass(frozen=True)
class FeltObservation:
    ts: float
    matched: bool
    rank: str | None
    val: int | None
    conf: float
    sharpness: float
    zone: str = "community"


@dataclass(frozen=True)
class FrameObservation:
    ts: float
    player_cards: list[CardDetection] = field(default_factory=list)
    dealer_cards: list[CardDetection] = field(default_factory=list)
    player_hands: dict[int, list[CardDetection]] = field(default_factory=dict)
    felt_events: list[FeltObservation] = field(default_factory=list)
    banner_state: str = ""
    banner_conf: float = 0.0
    shuffle_active: bool = False
    shuffle_conf: float = 0.0
    split_detected: bool = False
    hud_refreshed: bool = True
    dealer_only: bool = False
    place_bets_conf: float = 0.0
    banner_source: str = ""
    dealer_badge_total: int | None = None
    dealer_badge_conf: float = 0.0
    dealer_sub_cards: list[dict[str, Any]] = field(default_factory=list)
    dealer_badge_hint: dict[str, Any] | None = None
    loading_screen: bool = False
    shoe_state: str = ""  # "PRESENT" | "ABSENT" | "OCCLUDED" | ""
    shoe_dark_frac: float = 0.0
    shoe_teal_frac: float = 0.0
    discard_white_frac: float = 0.0
    discard_dark_frac: float = 0.0
    discard_teal_frac: float = 0.0


_UI_TPLS_CACHE: dict[str, list[np.ndarray]] = {}


def _get_ui_templates(prefix: str) -> list[np.ndarray]:
    if prefix in _UI_TPLS_CACHE:
        return _UI_TPLS_CACHE[prefix]
    ui_dir = Path(__file__).resolve().parent / "templates" / "ui"
    tpls: list[np.ndarray] = []
    if ui_dir.exists():
        for p in sorted(ui_dir.glob(f"{prefix}*.png")):
            im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if im is not None and im.size > 0:
                tpls.append(im)
    _UI_TPLS_CACHE[prefix] = tpls
    return tpls


def _match_ui_templates(roi_bgr: np.ndarray, tpls: list[np.ndarray], target_h: int = 36) -> float:
    if roi_bgr is None or roi_bgr.size == 0 or roi_bgr.ndim != 3 or not tpls:
        return 0.0
    h, w = roi_bgr.shape[:2]
    if h < 12 or w < 12:
        return 0.0
    nw = max(16, int(round(w * (target_h / float(h)))))
    rg = cv2.resize(cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY), (nw, target_h), interpolation=cv2.INTER_AREA)
    best = 0.0
    scale_ratio = target_h / 72.0
    for tg in tpls:
        if tg.shape[0] < 6 or tg.shape[1] < 6:
            continue
        for sc in (0.82, 1.00, 1.18):
            eff_sc = sc * scale_ratio
            sw = max(6, int(round(tg.shape[1] * eff_sc)))
            sh = max(6, int(round(tg.shape[0] * eff_sc)))
            if sw > nw or sh > target_h:
                continue
            st = cv2.resize(tg, (sw, sh), interpolation=cv2.INTER_AREA if eff_sc < 1.0 else cv2.INTER_CUBIC)
            res = cv2.matchTemplate(rg, st, cv2.TM_CCOEFF_NORMED)
            _, mv, _, _ = cv2.minMaxLoc(res)
            if np.isfinite(mv) and float(mv) > best:
                best = float(mv)
                if best >= 0.92:
                    return best
    return best


def _felt_mask(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Match both Playtech teal felt and Soho deep burgundy/red felt (excluding pale skin tones)."""
    teal = (h >= 78) & (h <= 108) & (s >= 55) & (v >= 55)
    burgundy = ((h <= 7) | (h >= 168)) & (s >= 120) & (v >= 45) & (v <= 155)
    return teal | burgundy


def _shoe_gray_48x36(shoe_img: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(shoe_img, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (48, 36), interpolation=cv2.INTER_AREA).astype(np.float32)


def _ncc_flat(a: np.ndarray, b: np.ndarray) -> float:
    am = a - float(a.mean())
    bm = b - float(b.mean())
    denom = float(np.sqrt(np.sum(am * am) * np.sum(bm * bm)))
    if denom < 1e-6:
        return 0.0
    return float(np.sum(am * bm) / denom)


def detect_shoe_state(
    shoe_img: np.ndarray | None,
    present_tpls: list[np.ndarray] | None = None,
    absent_tpls: list[np.ndarray] | None = None,
) -> tuple[str, float, float]:
    """Classify the right-side physical dealing shoe ROI into PRESENT | ABSENT | OCCLUDED.

    Fuses optional SHOE_PRESENT / SHOE_ABSENT training templates with full-ROI & bottom-half
    HSV dark/felt classification (supporting Playtech teal felt, Soho burgundy felt, and ROIs
    whose upper half includes a dark table rim or backup shoe).
    """
    if shoe_img is None or shoe_img.size == 0 or shoe_img.ndim != 3:
        return "", 0.0, 0.0
    hsv = cv2.cvtColor(shoe_img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    dark_mask = v < 58
    felt_mask = _felt_mask(h, s, v)
    dark_frac = float(np.mean(dark_mask))
    felt_frac = float(np.mean(felt_mask))
    h_px = shoe_img.shape[0]
    bot = slice(int(h_px * 0.48), h_px)
    bot_dark = float(np.mean(dark_mask[bot])) if h_px >= 4 else dark_frac
    bot_felt = float(np.mean(felt_mask[bot])) if h_px >= 4 else felt_frac

    p_tpls = _get_ui_templates("SHOE_PRESENT_") if present_tpls is None else present_tpls
    a_tpls = _get_ui_templates("SHOE_ABSENT_") if absent_tpls is None else absent_tpls
    p_conf = _match_ui_templates(shoe_img, p_tpls) if p_tpls else 0.0
    a_conf = _match_ui_templates(shoe_img, a_tpls) if a_tpls else 0.0

    if a_conf >= 0.86 and a_conf >= p_conf + 0.16 and felt_frac >= 0.38 and bot_dark <= 0.22:
        state = "ABSENT"
    elif p_conf >= 0.72 and p_conf >= a_conf + 0.08 and dark_frac >= 0.22:
        state = "PRESENT"
    elif dark_frac <= 0.22 and felt_frac >= 0.48:
        state = "ABSENT"
    elif dark_frac >= 0.28 and felt_frac <= 0.62:
        state = "PRESENT"
    elif bot_felt >= 0.70 and bot_dark <= 0.20 and felt_frac >= 0.40 and dark_frac < 0.32:
        state = "ABSENT"
    else:
        state = "OCCLUDED"
    return state, round(dark_frac, 4), round(felt_frac, 4)


def detect_discard_stack(
    discard_img: np.ndarray | None,
    empty_tpls: list[np.ndarray] | None = None,
    full_tpls: list[np.ndarray] | None = None,
) -> tuple[float, float, float]:
    """Position-invariant movable discard-tray stack detector inside a wide left-side ROI.

    Combines:
      1. Direct-lit white card edges (S<=50, V>=170) AND shadowed card edges inside/adjacent
         to the dark acrylic tray housing (S<=85, V>=110..169 inside dilated V<65 housing).
      2. Optional DISCARD_EMPTY / DISCARD_FULL training template matching to prevent shadowed
         or thin stacks (e.g. 10-card stacks) from falsely reading empty.
      3. Teal + burgundy felt detection so both All Bets BJ and Soho BJ tables are supported.
    """
    if discard_img is None or discard_img.size == 0 or discard_img.ndim != 3:
        return 0.0, 0.0, 0.0
    hsv = cv2.cvtColor(discard_img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    dark_frac = float(np.mean(v < 58))
    felt_frac = float(np.mean(_felt_mask(h, s, v)))

    housing_zone = cv2.dilate((v < 65).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    bright_mask = (s <= 50) & (v >= 170)
    shaded_mask = (s <= 85) & (v >= 110) & (v < 170)
    white_mask = (bright_mask | shaded_mask).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    white_mask = cv2.morphologyEx(white_mask, cv2.MORPH_OPEN, kernel)

    total_px = float(discard_img.shape[0] * discard_img.shape[1])
    min_blob_px = max(6, int(round(total_px * 0.002)))
    n_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(white_mask, connectivity=8)
    stack_px = 0
    for i in range(1, n_lbl):
        bx = int(stats[i, cv2.CC_STAT_LEFT])
        by = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_blob_px or bw < 2 or bh < 2:
            continue
        solidity = area / float(max(1, bw * bh))
        if solidity < 0.45:
            continue
        blob_roi = labels[by : by + bh, bx : bx + bw] == i
        if np.any(bright_mask[by : by + bh, bx : bx + bw] & blob_roi) or np.any(housing_zone[by : by + bh, bx : bx + bw] & blob_roi):
            stack_px += area

    white_stack_frac = stack_px / max(1.0, total_px)

    e_tpls = _get_ui_templates("DISCARD_EMPTY_") if empty_tpls is None else empty_tpls
    f_tpls = _get_ui_templates("DISCARD_FULL_") if full_tpls is None else full_tpls
    if e_tpls or f_tpls:
        e_conf = _match_ui_templates(discard_img, e_tpls) if e_tpls else 0.0
        f_conf = _match_ui_templates(discard_img, f_tpls) if f_tpls else 0.0
        if e_conf >= 0.80 and (f_conf < 0.76 or e_conf >= f_conf + 0.08):
            white_stack_frac = min(white_stack_frac, 0.004)
        elif f_conf >= 0.76 and f_conf >= e_conf + 0.04:
            white_stack_frac = max(white_stack_frac, 0.030)

    return round(white_stack_frac, 4), round(dark_frac, 4), round(felt_frac, 4)


@dataclass
class TemplateEntry:
    rank: str
    val: int | None
    gray: np.ndarray
    name: str
    min_gray: np.ndarray | None = None
    bgr: np.ndarray | None = None
    felt_tl: np.ndarray | None = None
    felt_br: np.ndarray | None = None
    felt_tl_pad: np.ndarray | None = None
    felt_br_pad: np.ndarray | None = None
    _scale_cache: dict[tuple[Any, ...], Any] = field(default_factory=dict, repr=False, compare=False)


def _to_min_gray(bgr_or_gray: np.ndarray) -> np.ndarray:
    """Convert BGR image to min(B,G,R) so red card ranks remain as dark as black ranks."""
    if bgr_or_gray.ndim == 2:
        return bgr_or_gray
    return bgr_or_gray.min(axis=2)


def extract_felt_card_rect(bgr: np.ndarray) -> np.ndarray:
    """Isolate the contiguous bright white playing-card rectangle from a felt crop, trimming outer felt/table text."""
    if bgr.size == 0:
        return bgr
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
    h, w = gray.shape[:2]
    if h < 10 or w < 14:
        return bgr
    thr = max(165.0, float(np.percentile(gray, 85)) - 35.0)

    def _longest_run(arr: np.ndarray, min_val: float, bridge_1px: bool = False) -> tuple[int, int]:
        n = len(arr)
        runs: list[tuple[int, int]] = []
        i = 0
        while i < n:
            if arr[i] >= min_val:
                j = i
                while j < n:
                    if arr[j] >= min_val:
                        j += 1
                    elif bridge_1px and (j + 1 < n) and arr[j + 1] >= min_val and arr[j] >= (min_val - 20.0):
                        j += 2
                    else:
                        break
                runs.append((i, j))
                i = j
            else:
                i += 1
        if not runs:
            return 0, n
        runs.sort(key=lambda r: (r[1] - r[0]), reverse=True)
        return runs[0]

    col_p80 = np.percentile(gray, 80, axis=0)
    x0, x1 = _longest_run(col_p80, thr, bridge_1px=False)
    if (x1 - x0) < 18:
        x0, x1 = 0, w
    row_p80 = np.percentile(gray[:, x0:x1], 80, axis=1)
    y0, y1 = _longest_run(row_p80, thr, bridge_1px=True)
    if (y1 - y0) < 14:
        row_p85 = np.percentile(gray[:, x0:x1], 85, axis=1)
        y0, y1 = _longest_run(row_p85, thr - 10.0, bridge_1px=True)
    if (y1 - y0) < 12:
        y0, y1 = 0, h
    col_p80_2 = np.percentile(gray[y0:y1, :], 80, axis=0)
    x0, x1 = _longest_run(col_p80_2, thr, bridge_1px=False)
    if (x1 - x0) < 18:
        x0, x1 = 0, w
    return bgr[y0:y1, x0:x1]


def extract_felt_rank_views(bgr: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Extract contrast-normalized (tl, 180°-rotated br) 16x10 rank corner views from a landscape felt card."""
    card = extract_felt_card_rect(bgr)
    if card.size == 0:
        return None, None
    mg = _to_min_gray(card).astype(np.float32)
    mh, mw = mg.shape[:2]
    if mh < 8 or mw < 10:
        return None, None
    inner = mg[2 : max(3, mh - 2), 2 : max(3, mw - 2)]
    if inner.size == 0:
        return None, None
    p85 = float(np.percentile(inner, 85))
    p10 = float(np.percentile(inner, 10))
    if p85 < 185.0 or (p85 - p10) < 55.0:
        return None, None
    lo = float(np.percentile(mg, 5))
    hi = float(np.percentile(mg, 85))
    if (hi - lo) < 25.0:
        return None, None
    norm = np.clip((mg - lo) * (255.0 / max(15.0, hi - lo)), 0, 255).astype(np.uint8)
    canon = cv2.resize(norm, (32, 20), interpolation=cv2.INTER_LINEAR)
    tl = canon[0:10, 0:16]
    br = cv2.rotate(canon[10:20, 16:32], cv2.ROTATE_180)
    return tl, br


class TemplateBank:
    """Loads PNG templates organized by prefix (<RANK>_*.png, <SUIT>_*.png, or <BANNER_TOKEN>_*.png)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.card_templates: list[TemplateEntry] = []
        self.shuffle_templates: list[TemplateEntry] = []
        self.banner_templates: list[TemplateEntry] = []
        self.suit_templates: list[TemplateEntry] = []
        self.reload()

    def reload(self) -> None:
        _RANK_SCALE_CACHE.clear()
        self.card_templates.clear()
        self.shuffle_templates.clear()
        self.banner_templates.clear()
        self.suit_templates.clear()
        if not self.directory.exists():
            return
        is_suit_dir = self.directory.name.lower() == "suit"
        is_felt_dir = self.directory.name.lower() == "felt"
        for png in sorted(self.directory.glob("*.png")):
            stem_upper = png.stem.upper()
            raw = cv2.imread(str(png), cv2.IMREAD_UNCHANGED)
            if raw is None or raw.size == 0:
                continue
            if raw.ndim == 3 and raw.shape[2] == 4:
                bgr = raw[:, :, :3]
            elif raw.ndim == 3:
                bgr = raw
            else:
                bgr = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
            gray = _to_gray(bgr)
            min_g = _to_min_gray(bgr)
            if float(np.std(min_g)) < 1.0 and float(np.std(gray)) < 1.0:
                continue

            matched_banner = None
            for btok in sorted(BANNER_TOKENS, key=len, reverse=True):
                if stem_upper == btok or stem_upper.startswith(btok + "_"):
                    matched_banner = btok
                    break

            if matched_banner:
                self.banner_templates.append(
                    TemplateEntry(
                        rank=matched_banner,
                        val=None,
                        gray=gray,
                        min_gray=min_g,
                        bgr=bgr,
                        name=png.name,
                    )
                )

            token = stem_upper.split("_")[0]
            if is_suit_dir and token in SUIT_TOKENS:
                self.suit_templates.append(
                    TemplateEntry(
                        rank=token,
                        val=None,
                        gray=min_g,
                        min_gray=min_g,
                        bgr=bgr,
                        name=png.name,
                    )
                )
            elif token in RANK_TO_VAL:
                if is_felt_dir and token == "BACK":
                    continue
                felt_tl: np.ndarray | None = None
                felt_br: np.ndarray | None = None
                felt_tl_pad: np.ndarray | None = None
                felt_br_pad: np.ndarray | None = None
                if is_felt_dir and bgr.shape[1] >= bgr.shape[0]:
                    felt_tl, felt_br = extract_felt_rank_views(bgr)
                    if felt_tl is not None and felt_br is not None:
                        felt_tl_pad = cv2.copyMakeBorder(felt_tl, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=255)
                        felt_br_pad = cv2.copyMakeBorder(felt_br, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=255)
                entry = TemplateEntry(
                    rank=token,
                    val=RANK_TO_VAL[token],
                    gray=gray,
                    min_gray=min_g,
                    bgr=bgr,
                    felt_tl=felt_tl,
                    felt_br=felt_br,
                    felt_tl_pad=felt_tl_pad,
                    felt_br_pad=felt_br_pad,
                    name=png.name,
                )
                if token == "SHUFFLE":
                    self.shuffle_templates.append(entry)
                else:
                    self.card_templates.append(entry)


def _rotate_image(img: np.ndarray, angle_deg: int) -> np.ndarray:
    if angle_deg == 0:
        return img
    h, w = img.shape[:2]
    mat = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), float(angle_deg), 1.0)
    return cv2.warpAffine(img, mat, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _to_gray(bgr_or_gray: np.ndarray) -> np.ndarray:
    if bgr_or_gray.ndim == 2:
        return bgr_or_gray
    return cv2.cvtColor(bgr_or_gray, cv2.COLOR_BGR2GRAY)


def match_templates_nms(
    roi_img: np.ndarray,
    templates: list[TemplateEntry],
    threshold: float,
    min_dx: int,
    scales: tuple[float, ...] = (1.0,),
) -> list[CardDetection]:
    """Run TM_CCOEFF_NORMED across templates and return left-to-right NMS detections."""
    if roi_img.size == 0 or not templates:
        return []
    gray = _to_gray(roi_img)
    roi_h, roi_w = gray.shape[:2]
    peaks: list[CardDetection] = []

    for tpl in templates:
        th, tw = tpl.gray.shape[:2]
        for sc in scales:
            sw = max(4, int(round(tw * sc)))
            sh = max(4, int(round(th * sc)))
            if sw > roi_w or sh > roi_h:
                continue
            scaled = tpl.gray if sc == 1.0 else cv2.resize(tpl.gray, (sw, sh), interpolation=cv2.INTER_AREA)
            if float(np.std(scaled)) < 1.0:
                continue
            res = cv2.matchTemplate(gray, scaled, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= threshold)
            for y_pos, x_pos in zip(ys.tolist(), xs.tolist()):
                conf = float(res[y_pos, x_pos])
                if not np.isfinite(conf):
                    continue
                patch = gray[y_pos : y_pos + sh, x_pos : x_pos + sw]
                if int(patch.max()) - int(patch.min()) < 65 or not np.any(patch > 150) or not np.any(patch < 110):
                    continue
                peaks.append(
                    CardDetection(
                        rank=tpl.rank,
                        val=tpl.val,
                        conf=conf,
                        x=int(x_pos),
                        y=int(y_pos),
                        w=sw,
                        h=sh,
                    )
                )

    if not peaks:
        return []

    peaks.sort(key=lambda d: d.conf, reverse=True)
    kept: list[CardDetection] = []
    min_dy = max(8, int(roi_h * 0.35))
    for cand in peaks:
        if any(abs(cand.x - k.x) < min_dx and abs(cand.y - k.y) < min_dy for k in kept):
            continue
        kept.append(cand)

    # Sort left-to-right cleanly (grouping vertical rows if split UI stacks slightly)
    kept.sort(key=lambda d: (d.y // max(12, min_dy), d.x))
    return kept


def _has_local_contrast(patch: np.ndarray, dark_on_white: bool = False) -> bool:
    """Reject uniform dark/flat patches that match text templates via tiny sensor noise."""
    if patch.size == 0:
        return False
    ptp = int(patch.max()) - int(patch.min())
    if ptp < 65:
        return False
    if dark_on_white:
        dark_frac = float(np.mean(patch < 115))
        bright_frac = float(np.mean(patch > 150))
        return bright_frac >= 0.22 and dark_frac >= 0.020
    dark_frac = float(np.mean(patch < 95))
    bright_frac = float(np.mean(patch > 150))
    return dark_frac >= 0.22 and bright_frac >= 0.025


def _get_scaled_tpl_gray(tpl: TemplateEntry, sc: float, use_min_gray: bool = False) -> tuple[np.ndarray, int, int] | None:
    """Return per-template cached resized grayscale image (avoids re-resizing every frame)."""
    key = ("g", use_min_gray, sc)
    if key in tpl._scale_cache:
        return tpl._scale_cache[key]
    src = (tpl.min_gray if (use_min_gray and tpl.min_gray is not None) else tpl.gray)
    th, tw = src.shape[:2]
    sw = max(4, int(round(tw * sc)))
    sh = max(4, int(round(th * sc)))
    interp = cv2.INTER_AREA if sc <= 1.0 else cv2.INTER_CUBIC
    scaled = src if sc == 1.0 else cv2.resize(src, (sw, sh), interpolation=interp)
    if float(np.std(scaled)) < 1.0:
        tpl._scale_cache[key] = None
        return None
    val = (scaled, sw, sh)
    tpl._scale_cache[key] = val
    return val


def detect_best_single_with_scale(
    roi_img: np.ndarray,
    templates: list[TemplateEntry],
    min_conf: float,
    angles: tuple[int, ...] = (0,),
    scales: tuple[float, ...] = (1.0,),
    require_contrast: bool = False,
    use_min_gray: bool = False,
) -> tuple[TemplateEntry | None, float, float | None]:
    """Return (best_tpl, best_conf, best_scale) over candidate angles and scales."""
    if roi_img.size == 0 or not templates:
        return None, 0.0, None
    gray = _to_min_gray(roi_img) if use_min_gray else _to_gray(roi_img)
    if require_contrast:
        dark_lim = 115 if use_min_gray else 95
        if int(gray.max()) - int(gray.min()) < 65 or not np.any(gray > 150) or not np.any(gray < dark_lim):
            return None, 0.0, None
    roi_h, roi_w = gray.shape[:2]
    best_tpl: TemplateEntry | None = None
    best_conf = 0.0
    best_scale: float | None = None

    for angle in angles:
        rotated = _rotate_image(gray, angle)
        for tpl in templates:
            for sc in scales:
                entry = _get_scaled_tpl_gray(tpl, sc, use_min_gray=use_min_gray)
                if entry is None:
                    continue
                scaled, sw, sh = entry
                if sw > roi_w or sh > roi_h:
                    continue
                res = cv2.matchTemplate(rotated, scaled, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if not np.isfinite(max_val):
                    continue
                if require_contrast:
                    mx, my = int(max_loc[0]), int(max_loc[1])
                    matched_box = rotated[my : my + sh, mx : mx + sw]
                    if not _has_local_contrast(matched_box, dark_on_white=use_min_gray):
                        continue
                if float(max_val) > best_conf:
                    best_conf = float(max_val)
                    best_tpl = tpl
                    best_scale = sc
                    if best_conf >= 0.92:
                        return best_tpl, best_conf, best_scale

    if best_tpl is not None and best_conf >= min_conf:
        return best_tpl, best_conf, best_scale
    return None, best_conf, None


def detect_best_single(
    roi_img: np.ndarray,
    templates: list[TemplateEntry],
    min_conf: float,
    angles: tuple[int, ...] = (0,),
    scales: tuple[float, ...] = (1.0,),
    require_contrast: bool = False,
    use_min_gray: bool = False,
) -> tuple[TemplateEntry | None, float]:
    """Return best single template match over candidate angles and scales."""
    tpl, conf, _ = detect_best_single_with_scale(
        roi_img,
        templates,
        min_conf=min_conf,
        angles=angles,
        scales=scales,
        require_contrast=require_contrast,
        use_min_gray=use_min_gray,
    )
    return tpl, conf


class FeltBurstMonitor:
    """Watches physical felt zone (community or dealer) and captures the sharpest frame per card arrival."""

    def __init__(
        self,
        cfg: CompanionConfig,
        felt_bank: TemplateBank,
        zone: str = "community",
        fallback_bank: TemplateBank | None = None,
    ) -> None:
        self.cfg = cfg
        self.felt_bank = felt_bank
        self.zone = zone
        self.fallback_bank = fallback_bank
        self._active = False
        self._start_ts = 0.0
        self._last_emit_ts = -999.0
        self._best_frame: np.ndarray | None = None
        self._best_sharpness = -1.0
        self._had_contour = False
        self._active_box: tuple[int, int, int, int] | None = None
        self._settled_boxes: list[tuple[int, int, int, int]] = []
        self._empty_frames: int = 0

    def reset(self, clear_settled: bool = True) -> None:
        self._active = False
        self._best_frame = None
        self._best_sharpness = -1.0
        self._had_contour = False
        self._active_box = None
        if clear_settled:
            self._settled_boxes.clear()
            self._empty_frames = 0

    @staticmethod
    def _is_face_up_felt_box(bgr: np.ndarray, x: int, y: int, w: int, h: int) -> bool:
        """Reject face-down grey/blue hole-card backs (e.g. BACK_1.png) while keeping face-up white cards."""
        patch_mb = _to_min_gray(bgr[y : y + h, x : x + w])
        if patch_mb.size == 0 or float(np.mean(patch_mb > 135)) < 0.25:
            return False
        ph, pw = patch_mb.shape[:2]
        inner = patch_mb[2 : max(3, ph - 2), 2 : max(3, pw - 2)]
        if inner.size == 0:
            return False
        return float(np.percentile(inner, 85)) >= 192.0

    @classmethod
    def _split_wide_felt_box(
        cls, bgr: np.ndarray, x: int, y: int, w: int, h: int
    ) -> list[tuple[int, int, int, int]]:
        """Split a wide horizontal contour containing 2-4 side-by-side landscape felt cards at vertical seams."""
        if (w / max(1.0, float(h))) < 2.15:
            return [(x, y, w, h)] if cls._is_face_up_felt_box(bgr, x, y, w, h) else []
        unit_w = max(22.0, min(33.0, h * 1.62))
        if w < max(40, int(round(unit_w * 1.55))):
            return [(x, y, w, h)] if cls._is_face_up_felt_box(bgr, x, y, w, h) else []
        n_cards = max(2, min(4, int(round(w / unit_w))))
        mg = _to_min_gray(bgr[y : y + h, x : x + w]).astype(np.float32)
        col_p75 = np.percentile(mg, 75, axis=0)
        cuts = [0]
        for k in range(1, n_cards):
            expected = int(round(k * w / float(n_cards)))
            lo = max(cuts[-1] + 16, expected - 7)
            hi = min(w - 16, expected + 7)
            cut = (lo + int(np.argmin(col_p75[lo : hi + 1]))) if hi >= lo else expected
            cuts.append(cut)
        cuts.append(w)
        out: list[tuple[int, int, int, int]] = []
        for idx in range(len(cuts) - 1):
            sx0, sx1 = cuts[idx], cuts[idx + 1]
            sw = sx1 - sx0
            if sw < 14:
                continue
            if cls._is_face_up_felt_box(bgr, x + sx0, y, sw, h):
                out.append((x + sx0, y, sw, h))
        return out

    def _find_card_contours(self, bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
        """Return valid bright white playing-card bounding boxes inside the felt ROI."""
        if bgr.size == 0:
            return []
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        v_min = max(150, self.cfg.felt_motion.min_brightness)
        mask = cv2.inRange(hsv, (0, 0, v_min), (180, 80, 255))
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return []
        roi_area = float(bgr.shape[0] * bgr.shape[1])
        rects: list[tuple[int, int, int, int]] = []
        for cnt in contours:
            area = float(cv2.contourArea(cnt))
            if area / max(1.0, roi_area) < self.cfg.felt_motion.min_contour_area_ratio:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            if h < 12 or (w / max(1.0, float(h))) > 8.5:
                continue
            fill = area / float(max(1, w * h))
            if fill < 0.25:
                continue
            for sub_box in self._split_wide_felt_box(bgr, x, y, w, h):
                rects.append(sub_box)
        rects.sort(key=lambda r: (r[1] // 10, r[0]))
        return rects

    def _select_novel_contour(
        self, bgr: np.ndarray, rects: list[tuple[int, int, int, int]]
    ) -> tuple[bool, np.ndarray | None, tuple[int, int, int, int] | None]:
        """Select a newly arrived or expanded card contour that has not already settled."""
        if not rects:
            return False, None, None

        chosen_crop_rect: tuple[int, int, int, int] | None = None
        chosen_full_rect: tuple[int, int, int, int] | None = None

        for rx, ry, rw, rh in rects:
            rcx = rx + rw * 0.5
            rcy = ry + rh * 0.5
            matched_settled = False
            for sx, sy, sw, sh in self._settled_boxes:
                scx = sx + sw * 0.5
                scy = sy + sh * 0.5
                if (
                    abs(rcx - scx) <= 11.0
                    and abs(rcy - scy) <= 9.0
                    and abs(rw - sw) <= 12
                    and abs(rh - sh) <= 10
                ):
                    matched_settled = True
                    break
                # Check if a new card landed immediately adjacent/overlapping the right side of a settled card
                if abs(ry - sy) <= 10 and abs(rx - sx) <= 10 and (rw - sw) >= 10:
                    nx = sx + max(4, sw - 6)
                    nw = max(12, (rx + rw) - nx)
                    chosen_crop_rect = (nx, ry, nw, rh)
                    chosen_full_rect = (rx, ry, rw, rh)
                    break
            if chosen_crop_rect is not None:
                break
            if not matched_settled:
                chosen_crop_rect = (rx, ry, rw, rh)
                chosen_full_rect = (rx, ry, rw, rh)
                break

        if chosen_crop_rect is None or chosen_full_rect is None:
            return False, None, None

        x, y, w, h = chosen_crop_rect
        pad = 3
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(bgr.shape[1], x + w + pad)
        y1 = min(bgr.shape[0], y + h + pad)
        return True, bgr[y0:y1, x0:x1], chosen_full_rect

    def _has_card_contour(self, bgr: np.ndarray) -> tuple[bool, np.ndarray | None]:
        """Backward-compatible helper returning (present, patch) for the primary novel card contour."""
        rects = self._find_card_contours(bgr)
        present, patch, _ = self._select_novel_contour(bgr, rects)
        return present, patch

    def _match_felt_dual_corner(
        self, frame: np.ndarray, templates: list[TemplateEntry]
    ) -> tuple[TemplateEntry | None, float]:
        """Match canonical 32x20 landscape felt card using top-left and 180°-rotated bottom-right rank corners."""
        dual_tpls = [t for t in templates if t.felt_tl is not None and t.felt_br is not None]
        if not dual_tpls:
            return None, 0.0
        q_tl, q_br = extract_felt_rank_views(frame)
        if q_tl is None or q_br is None:
            return None, 0.0
        pad_tl = cv2.copyMakeBorder(q_tl, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=255)
        pad_br = cv2.copyMakeBorder(q_br, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=255)
        best_tpl: TemplateEntry | None = None
        best_conf = 0.0
        for tpl in dual_tpls:
            assert tpl.felt_tl is not None and tpl.felt_br is not None
            t_tl = tpl.felt_tl[1:-1, 1:-1]
            t_br = tpl.felt_br[1:-1, 1:-1]
            m_tl_tl = float(cv2.matchTemplate(pad_tl, t_tl, cv2.TM_CCOEFF_NORMED).max())
            m_tl_br = float(cv2.matchTemplate(pad_tl, t_br, cv2.TM_CCOEFF_NORMED).max())
            m_br_tl = float(cv2.matchTemplate(pad_br, t_tl, cv2.TM_CCOEFF_NORMED).max())
            m_br_br = float(cv2.matchTemplate(pad_br, t_br, cv2.TM_CCOEFF_NORMED).max())
            best_single = max(m_tl_tl, m_tl_br, m_br_tl, m_br_br)
            dual = 0.55 * max(m_tl_tl, m_br_br) + 0.45 * min(m_tl_tl, m_br_br)
            bonus = 0.04 if min(m_tl_tl, m_br_br) >= 0.58 else 0.0
            score = min(0.99, max(best_single * 0.92, dual) + bonus)
            if np.isfinite(score) and score > best_conf:
                best_conf = score
                best_tpl = tpl
        if best_tpl is not None and best_conf >= self.cfg.min_felt_confidence:
            return best_tpl, best_conf
        return None, best_conf

    def _match_patch(
        self, frame: np.ndarray, templates: list[TemplateEntry], scales: tuple[float, ...]
    ) -> tuple[TemplateEntry | None, float]:
        if not templates or frame.size == 0:
            return None, 0.0
        fh, fw = frame.shape[:2]
        best_tpl: TemplateEntry | None = None
        best_conf = 0.0
        if fw >= fh and fh >= 12 and fw >= 18:
            dtpl, dconf = self._match_felt_dual_corner(frame, templates)
            if dtpl is not None:
                return dtpl, dconf
            best_conf = max(best_conf, dconf)

        angles = tuple(self.cfg.felt_motion.angles_deg)
        tpl, conf = detect_best_single(
            frame,
            templates,
            min_conf=self.cfg.min_felt_confidence,
            angles=angles,
            scales=scales,
            require_contrast=True,
            use_min_gray=True,
        )
        if tpl is not None and conf > best_conf:
            best_tpl, best_conf = tpl, conf
        elif conf > best_conf:
            best_conf = conf
        # Also probe top-left corner sub-patch in case full card pip/border dilutes small corner template
        if fh >= 28 and fw >= 24:
            corner = frame[: max(20, int(round(fh * 0.58))), : max(18, int(round(fw * 0.58)))]
            ctpl, cconf = detect_best_single(
                corner,
                templates,
                min_conf=self.cfg.min_felt_confidence,
                angles=angles,
                scales=scales,
                require_contrast=True,
                use_min_gray=True,
            )
            if ctpl is not None and cconf > best_conf:
                best_tpl, best_conf = ctpl, cconf
            elif cconf > best_conf:
                best_conf = cconf
        return best_tpl, best_conf

    def detect_static_cards(self, bgr: np.ndarray) -> list[CardDetection]:
        """Detect all static face-up cards currently visible inside a felt ROI snapshot."""
        if bgr.size == 0:
            return []
        rects = self._find_card_contours(bgr)
        if not rects:
            return []
        scales = tuple(self.cfg.felt_motion.scales) if self.cfg.felt_motion.scales else (1.0,)
        out: list[CardDetection] = []
        pad = 2
        for x, y, w, h in rects:
            x0 = max(0, x - pad)
            y0 = max(0, y - pad)
            x1 = min(bgr.shape[1], x + w + pad)
            y1 = min(bgr.shape[0], y + h + pad)
            patch = bgr[y0:y1, x0:x1]
            tpl, conf = self._match_patch(patch, self.felt_bank.card_templates, scales)
            if tpl is None and self.fallback_bank is not None:
                fb_tpls = [t for t in self.fallback_bank.card_templates if t.rank != "BACK"]
                fb_scales = tuple(sorted(set(scales) | {1.0, 1.15, 1.30, 1.45, 1.65}))
                tpl, conf = self._match_patch(patch, fb_tpls, fb_scales)
            if tpl is not None and tpl.rank != "BACK":
                out.append(
                    CardDetection(
                        rank=tpl.rank,
                        val=tpl.val,
                        conf=conf,
                        x=int(x),
                        y=int(y),
                        w=int(w),
                        h=int(h),
                    )
                )
        return out

    def _finalize(self, ts: float, settled_in_place: bool = False) -> FeltObservation:
        frame = self._best_frame
        sharp = max(0.0, self._best_sharpness)
        active_box = self._active_box
        self.reset(clear_settled=False)
        if settled_in_place and active_box is not None:
            self._settled_boxes.append(active_box)
        elif not settled_in_place and active_box is not None and self._settled_boxes:
            ax, ay, aw, ah = active_box
            kept = []
            for bx, by, bw, bh in self._settled_boxes:
                ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
                iy = max(0, min(ay + ah, by + bh) - max(ay, by))
                if (ix * iy) / float(max(1, bw * bh)) < 0.35:
                    kept.append((bx, by, bw, bh))
            self._settled_boxes = kept
        self._last_emit_ts = ts
        if frame is None:
            return FeltObservation(ts=ts, matched=False, rank=None, val=None, conf=0.0, sharpness=sharp, zone=self.zone)

        scales = tuple(self.cfg.felt_motion.scales) if self.cfg.felt_motion.scales else (1.0,)
        tpl, conf = self._match_patch(frame, self.felt_bank.card_templates, scales)
        if tpl is None and self.fallback_bank is not None:
            fb_tpls = [t for t in self.fallback_bank.card_templates if t.rank != "BACK"]
            fb_scales = tuple(sorted(set(scales) | {1.0, 1.15, 1.30, 1.45, 1.65}))
            tpl2, conf2 = self._match_patch(frame, fb_tpls, fb_scales)
            if tpl2 is not None and conf2 >= self.cfg.min_felt_confidence:
                tpl, conf = tpl2, conf2
            elif conf2 > conf:
                conf = conf2

        if tpl is None:
            return FeltObservation(ts=ts, matched=False, rank=None, val=None, conf=conf, sharpness=sharp, zone=self.zone)
        return FeltObservation(
            ts=ts,
            matched=True,
            rank=tpl.rank,
            val=tpl.val,
            conf=conf,
            sharpness=sharp,
            zone=self.zone,
        )

    def step(self, bgr: np.ndarray, ts: float | None = None) -> FeltObservation | None:
        """Process one burst frame of felt ROI; emits FeltObservation when a card leaves or settles."""
        now = ts if ts is not None else time.monotonic()
        rects = self._find_card_contours(bgr)
        if not rects:
            self._empty_frames += 1
            if self._active:
                return self._finalize(now, settled_in_place=False)
            if self._empty_frames >= 2:
                self._settled_boxes.clear()
            return None

        self._empty_frames = 0
        present, patch, full_rect = self._select_novel_contour(bgr, rects)

        if present and patch is not None:
            if not self._active:
                if (now - self._last_emit_ts) < self.cfg.felt_motion.pulse_cooldown_sec:
                    return None
                self._active = True
                self._start_ts = now
                self._best_sharpness = -1.0
                self._best_frame = None
            self._active_box = full_rect

            gray = _to_min_gray(patch)
            sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            if sharp > self._best_sharpness:
                self._best_sharpness = sharp
                self._best_frame = patch.copy()

            if (now - self._start_ts) >= self.cfg.felt_motion.settle_timeout_sec:
                return self._finalize(now, settled_in_place=True)
            return None

        if self._active:
            return self._finalize(now, settled_in_place=True)
        return None


_LOADING_TPLS_CACHE: list[np.ndarray] | None = None


def _get_loading_templates() -> list[np.ndarray]:
    global _LOADING_TPLS_CACHE
    if _LOADING_TPLS_CACHE is not None:
        return _LOADING_TPLS_CACHE
    ui_dir = Path(__file__).resolve().parent / "templates" / "ui"
    tpls: list[np.ndarray] = []
    if ui_dir.exists():
        for p in sorted(ui_dir.glob("LOADING_ALLBETS*.png")):
            im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if im is not None and im.size > 0:
                tpls.append(im)
    _LOADING_TPLS_CACHE = tpls
    return tpls


def _match_loading_logo(band_bgr: np.ndarray, loading_tpls: list[np.ndarray] | None = None) -> bool:
    if band_bgr is None or band_bgr.size == 0 or band_bgr.ndim != 3:
        return False
    tpls = _get_loading_templates() if loading_tpls is None else loading_tpls
    if not tpls:
        return False
    bh, bw = band_bgr.shape[:2]
    if bh < 16 or bw < 20:
        return False
    nh = 100
    nw = max(40, int(round(bw * (nh / float(bh)))))
    band_g = cv2.resize(cv2.cvtColor(band_bgr, cv2.COLOR_BGR2GRAY), (nw, nh), interpolation=cv2.INTER_AREA)
    for tpl_g in tpls:
        if tpl_g.shape[0] < 4 or tpl_g.shape[1] < 8:
            continue
        for sc in (0.24, 0.28, 0.32, 0.36, 0.42, 0.48, 0.56, 0.66, 0.78, 0.92, 1.08):
            sw = max(12, int(round(tpl_g.shape[1] * sc)))
            sh = max(6, int(round(tpl_g.shape[0] * sc)))
            if sw > nw or sh > nh:
                continue
            st = cv2.resize(tpl_g, (sw, sh), interpolation=cv2.INTER_AREA if sc < 1.0 else cv2.INTER_CUBIC)
            res = cv2.matchTemplate(band_g, st, cv2.TM_CCOEFF_NORMED)
            _, mv, _, _ = cv2.minMaxLoc(res)
            if np.isfinite(mv) and float(mv) >= 0.72:
                return True
    return False


def detect_loading_screen(anchor_bgr: np.ndarray, loading_tpls: list[np.ndarray] | None = None) -> bool:
    """Detect Playtech reload/loading splash screens (uniform blank flash, ALL BETS logo template, or blurred-blue splash)."""
    if anchor_bgr is None or anchor_bgr.size == 0 or anchor_bgr.ndim != 3:
        return False
    h, w = anchor_bgr.shape[:2]
    if h < 40 or w < 40:
        return False
    th = 140
    tw = max(48, min(260, int(round(w * (th / float(h))))))
    sm = cv2.resize(anchor_bgr, (tw, th), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY)

    # 1. Uniform blank/dark/white reload flash (ignore bottom 14% in case a browser bottom toolbar is inside anchor)
    core_h = int(th * 0.86)
    if float(np.std(gray[:core_h, :])) < 8.5:
        return True

    # 2. Reliable template match for the Playtech "ALL BETS" loading logo in center band [x:0.08..0.92, y:0.20..0.72]
    band = anchor_bgr[int(h * 0.20) : int(h * 0.72), int(w * 0.08) : int(w * 0.92)]
    if _match_loading_logo(band, loading_tpls=loading_tpls):
        return True

    # 3. Playtech blurred-blue loading splash fallback: cool blue wash (B > G, B - R > 14, B >= 45) + blurred top + splash sig
    core = sm[:core_h, :]
    cb = core[:, :, 0].astype(np.int16)
    cg = core[:, :, 1].astype(np.int16)
    cr = core[:, :, 2].astype(np.int16)
    blue_mask = (cb > cg) & ((cb - cr) > 14) & (cb >= 45)
    if float(np.mean(blue_mask)) < 0.60:
        return False

    top_gray = gray[: int(th * 0.24), :].astype(np.float32)
    top_grad = float(np.mean(np.abs(np.diff(top_gray, axis=1))) + np.mean(np.abs(np.diff(top_gray, axis=0))))
    if top_grad > 8.5:
        return False

    m_w = max(4, int(tw * 0.16))
    margin_blue = 0.5 * (float(np.mean(blue_mask[:, :m_w])) + float(np.mean(blue_mask[:, tw - m_w :])))
    if margin_blue < 0.65:
        return False

    # (a) Playtech LIVE circular white badge in lower-center [y=0.50..0.82, x=0.48..0.76] with white text to its left
    y0, y1 = int(th * 0.50), int(th * 0.82)
    x0, x1 = int(tw * 0.48), int(tw * 0.76)
    sub_mb = sm[y0:y1, x0:x1].min(axis=2)
    num, _, stats, _ = cv2.connectedComponentsWithStats((sub_mb > 165).astype(np.uint8))
    for i in range(1, num):
        cx, cy, cw, ch, area = [int(v) for v in stats[i]]
        if 4 <= cw <= 20 and 4 <= ch <= 20 and 0.72 <= (cw / max(1.0, float(ch))) <= 1.38:
            fill = area / float(cw * ch)
            if 0.48 <= fill <= 0.94:
                lx0 = max(0, x0 + cx - int(cw * 4.2))
                lx1 = max(lx0 + 1, x0 + cx - 1)
                ly0 = max(0, y0 + cy)
                ly1 = min(th, y0 + cy + ch)
                left_mb = sm[ly0:ly1, lx0:lx1].min(axis=2)
                if left_mb.size > 0 and float(np.mean(left_mb > 155)) >= 0.06:
                    return True

    # (b) Thin horizontal loading progress bar in [y=0.44..0.68, x=0.25..0.75]
    bx0, bx1 = int(tw * 0.25), int(tw * 0.75)
    for y in range(int(th * 0.44), int(th * 0.68)):
        row = gray[y, bx0:bx1]
        above = gray[max(0, y - 4) : max(1, y - 1), bx0:bx1]
        below = gray[min(th - 1, y + 2) : min(th, y + 5), bx0:bx1]
        if above.size and below.size:
            bar_px = float(np.mean((row < 35) | (row > 190)))
            if bar_px >= 0.22 and float(np.mean(row < 35)) >= 0.08 and float(np.mean(above)) > 45 and float(np.mean(below)) > 45:
                return True
    return False


def detect_loading_screen_crops(
    shuffle_img: np.ndarray,
    player_img: np.ndarray,
    dealer_img: np.ndarray | None = None,
    center_img: np.ndarray | None = None,
    place_bets_img: np.ndarray | None = None,
    loading_tpls: list[np.ndarray] | None = None,
) -> bool:
    """Loading-screen check from tight ROI crops (template match on center/place_bets + uniform/blue-blur fallback)."""
    for probe in (center_img, place_bets_img):
        if probe is not None and probe.size > 0 and _match_loading_logo(probe, loading_tpls=loading_tpls):
            return True
    if shuffle_img is None or shuffle_img.size == 0 or player_img is None or player_img.size == 0:
        return False
    if float(np.std(shuffle_img)) < 8.5 and float(np.std(player_img)) < 8.5:
        return True
    if shuffle_img.ndim != 3 or player_img.ndim != 3:
        return False

    def _blue_blur_ok(patch: np.ndarray) -> bool:
        b = patch[:, :, 0].astype(np.int16)
        g = patch[:, :, 1].astype(np.int16)
        r = patch[:, :, 2].astype(np.int16)
        blue_frac = float(np.mean((b > g) & ((b - r) > 14) & (b >= 45)))
        if blue_frac < 0.72:
            return False
        sm = cv2.resize(patch, (min(80, max(16, patch.shape[1])), min(32, max(12, patch.shape[0]))), interpolation=cv2.INTER_AREA)
        gr = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY).astype(np.float32)
        grad = float(np.mean(np.abs(np.diff(gr, axis=1))) + np.mean(np.abs(np.diff(gr, axis=0))))
        return grad <= 7.5

    if not (_blue_blur_ok(shuffle_img) and _blue_blur_ok(player_img)):
        return False
    if dealer_img is not None and dealer_img.size and dealer_img.ndim == 3:
        db = dealer_img[:, :, 0].astype(np.int16)
        dg = dealer_img[:, :, 1].astype(np.int16)
        dr = dealer_img[:, :, 2].astype(np.int16)
        if float(np.mean((db > dg) & ((db - dr) > 14) & (db >= 45))) < 0.50:
            return False
    return True


def _find_white_card_clusters(
    roi_bgr: np.ndarray, is_dealer: bool = False, allow_dimmed: bool | None = None
) -> list[tuple[int, int, int, int]]:
    """Locate connected Playtech card clusters inside an ROI, including dimmed split hands when allowed."""
    if roi_bgr.size == 0:
        return []
    if allow_dimmed is None:
        allow_dimmed = not is_dealer
    mb = _to_min_gray(roi_bgr)
    chroma = (
        (roi_bgr.max(axis=2).astype(np.int16) - mb.astype(np.int16))
        if roi_bgr.ndim == 3
        else np.zeros_like(mb, dtype=np.int16)
    )

    def _run_pass(
        mask_thr: int, cap_thr: int, mean_thr: int, max_chroma: int | None = None
    ) -> list[tuple[int, int, int, int]]:
        mask = (mb > mask_thr) & (chroma <= max_chroma) if max_chroma is not None else (mb > mask_thr)
        num, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        raw: list[tuple[int, int, int, int]] = []
        caps: list[tuple[int, int, int, int]] = []
        for i in range(1, num):
            x, y, w, h, area = [int(v) for v in stats[i]]
            fill = area / float(max(1, w * h))
            patch_mb = mb[y : y + h, x : x + w]
            if h < 13 or area < 105:
                if 8 <= h <= 24 and w >= 5 and area >= 36 and fill >= 0.25 and float(np.mean(patch_mb > cap_thr)) >= 0.25:
                    caps.append((x, y, w, h))
                continue
            if (w / max(1.0, float(h)) > 4.0 and fill < 0.45) or fill < 0.19:
                continue
            if float(np.mean(patch_mb > mean_thr)) < 0.25:
                continue
            if max_chroma is not None:
                # Dimmed inactive cards still have crisp paper-vs-ink contrast (unlike flat gray UI/felt)
                p95 = float(np.percentile(patch_mb, 95))
                p5 = float(np.percentile(patch_mb, 5))
                if (p95 - p5) < 55.0 or int(patch_mb.min()) >= 55:
                    continue
            raw.append((x, y, w, h))
        if not raw:
            return []
        raw.sort(key=lambda b: b[0])
        merged: list[tuple[int, int, int, int]] = [raw[0]]
        for x, y, w, h in raw[1:]:
            px, py, pw, ph = merged[-1]
            gap = x - (px + pw)
            v_ov = min(py + ph, y + h) - max(py, y)
            if gap <= 2 and v_ov >= int(round(0.55 * min(ph, h))):
                nx = min(px, x)
                ny = min(py, y)
                nr = max(px + pw, x + w)
                nb = max(py + ph, y + h)
                merged[-1] = (nx, ny, nr - nx, nb - ny)
            else:
                merged.append((x, y, w, h))

        # Merge any top-left white corner cap (where score badge severed the bottom of Card 0) into its right neighbor
        if caps:
            for cx, cy, cw, ch in caps:
                for idx, (px, py, pw, ph) in enumerate(merged):
                    gap = px - (cx + cw)
                    if -2 <= gap <= 4 and abs(cy - py) <= 5 and ch < ph:
                        nx = min(cx, px)
                        ny = min(cy, py)
                        nr = max(cx + cw, px + pw)
                        nb = max(cy + ch, py + ph)
                        merged[idx] = (nx, ny, nr - nx, nb - ny)
                        break
        return merged

    primary = _run_pass(155, 160, 165, max_chroma=None)
    if allow_dimmed:
        dimmed = _run_pass(80, 88, 92, max_chroma=28)
        if not dimmed:
            return primary
        if not primary:
            return dimmed
        combined = list(primary)
        for dx, dy, dw, dh in dimmed:
            d_area = max(1, dw * dh)
            overlaps = False
            for px, py, pw, ph in primary:
                ix = max(0, min(dx + dw, px + pw) - max(dx, px))
                iy = max(0, min(dy + dh, py + ph) - max(dy, py))
                if (ix * iy) / float(d_area) >= 0.25:
                    overlaps = True
                    break
            if not overlaps:
                combined.append((dx, dy, dw, dh))
        combined.sort(key=lambda b: b[0])
        return combined
    if not is_dealer:
        return primary
    total_w = sum(c[2] for c in primary)
    if not primary or total_w < roi_bgr.shape[1] * 0.28:
        relaxed = _run_pass(142, 145, 148, max_chroma=None)
        if sum(c[2] for c in relaxed) > total_w:
            return relaxed
    return primary


def _has_red_back_signature(bgr_patch: np.ndarray) -> bool:
    """Return True when a BGR patch exhibits Playtech's red-checkered hole card pattern."""
    if bgr_patch.size == 0 or bgr_patch.ndim != 3:
        return False
    b = bgr_patch[:, :, 0].astype(np.int16)
    g = bgr_patch[:, :, 1].astype(np.int16)
    r = bgr_patch[:, :, 2].astype(np.int16)
    red_mask = (r - np.maximum(b, g)) > 32
    red_ratio = float(np.mean(red_mask))
    mn = bgr_patch.min(axis=2)
    dot_ratio = float(np.mean(mn > 150))
    white_ratio = float(np.mean(mn >= 225))
    if not (red_ratio >= 0.24 and dot_ratio >= 0.10 and white_ratio <= 0.42):
        return False
    # Red face-up Hearts/Diamonds cards only have red ink concentrated in the rank/pip corner,
    # whereas a checkered card back spans both top and bottom halves of the patch.
    h = bgr_patch.shape[0]
    if h >= 6:
        mid = h // 2
        if float(np.mean(red_mask[:mid, :])) < 0.14 or float(np.mean(red_mask[mid:, :])) < 0.14:
            return False
    return True


def _classify_suit_zone(
    suit_zone: np.ndarray,
    ink_zones: list[np.ndarray],
    suit_templates: list[TemplateEntry],
    paper_hi: float = 250.0,
) -> tuple[str | None, float]:
    """Classify card suit (S, H, C, D) on a canonical H=45 pip zone with adaptive luminance thresholds."""
    if suit_zone.size == 0 or suit_zone.ndim != 3 or not suit_templates:
        return None, 0.0
    paper_scale = min(1.0, max(0.45, paper_hi / 220.0))
    red_diff_thr = max(18, min(32, int(round(32.0 * paper_scale))))
    suit_ink_thr = max(72, min(175, int(round(paper_hi * 0.72))))

    def _ink_counts(patch: np.ndarray) -> tuple[int, int]:
        if patch.size == 0 or patch.ndim != 3:
            return 0, 0
        b = patch[:, :, 0].astype(np.int16)
        g = patch[:, :, 1].astype(np.int16)
        r = patch[:, :, 2].astype(np.int16)
        red_px = int(np.sum((r - np.maximum(b, g)) > red_diff_thr))
        ink_px = int(np.sum(patch.min(axis=2) < suit_ink_thr))
        return red_px, ink_px

    rb, ib = _ink_counts(ink_zones[0]) if ink_zones else (0, 0)
    rt, it = _ink_counts(ink_zones[1]) if len(ink_zones) > 1 else (0, 0)
    red_frac_bot = rb / max(1, ib)
    red_frac_all = (rb + rt) / max(1, ib + it)
    is_red = (rb >= 8 and red_frac_bot >= 0.42) or ((rb + rt) >= 12 and red_frac_all >= 0.42)
    allowed = {"H", "D"} if is_red else {"S", "C"}
    filtered = [t for t in suit_templates if t.rank in allowed] or suit_templates

    suit_min = _to_min_gray(suit_zone)
    zh, zw = suit_min.shape[:2]
    best_suit: str | None = None
    best_conf = 0.0
    per_suit_best: dict[str, tuple[float, int, int, int, int]] = {}
    for tpl in filtered:
        for sc in (0.88, 0.96, 1.0, 1.06, 1.12):
            entry = _get_scaled_tpl_gray(tpl, sc, use_min_gray=True)
            if entry is None:
                continue
            scaled, sw, sh = entry
            if sw > zw or sh > zh:
                continue
            res = cv2.matchTemplate(suit_min, scaled, cv2.TM_CCOEFF_NORMED)
            _, mval, _, mloc = cv2.minMaxLoc(res)
            if np.isfinite(mval):
                fval = float(mval)
                prev = per_suit_best.get(tpl.rank)
                if prev is None or fval > prev[0]:
                    per_suit_best[tpl.rank] = (fval, int(mloc[0]), int(mloc[1]), sw, sh)
                if fval > best_conf:
                    best_conf = fval
                    best_suit = tpl.rank

    # Clubs vs Spades apex-fill tie-breaker when black-suit NCC scores are close (within 0.04)
    if (
        not is_red
        and best_suit in ("C", "S")
        and "C" in per_suit_best
        and "S" in per_suit_best
        and abs(per_suit_best["C"][0] - per_suit_best["S"][0]) <= 0.04
    ):
        _, mx, my, mw, mh = per_suit_best[best_suit]
        if mh >= 7 and mw >= 6:
            sub = suit_min[my : my + mh, mx : mx + mw]
            ink = sub < suit_ink_thr
            top_h = max(2, int(round(mh * 0.28)))
            mid_y0 = max(top_h, int(round(mh * 0.35)))
            mid_y1 = max(mid_y0 + 1, int(round(mh * 0.65)))
            top_band = ink[:top_h, :]
            mid_band = ink[mid_y0:mid_y1, :]
            top_fill = float(np.mean(top_band)) if top_band.size else 0.0
            mid_fill = float(np.mean(mid_band)) if mid_band.size else 0.0
            # Clubs have a wide rounded top lobe (higher top fill relative to middle pinch);
            # Spades taper to a sharp point at the apex (lower top fill relative to wide lower-mid belly).
            if top_fill >= 0.44 and top_fill >= mid_fill * 0.72:
                best_suit = "C"
                best_conf = per_suit_best["C"][0]
            elif top_fill <= 0.30 and mid_fill >= 0.50:
                best_suit = "S"
                best_conf = per_suit_best["S"][0]

    if best_suit is not None and best_conf >= 0.56:
        return best_suit, best_conf
    return None, best_conf


def _classify_suit(
    norm_bgr: np.ndarray,
    rank_x: int,
    rank_w: int,
    cw_norm: int,
    suit_templates: list[TemplateEntry],
    paper_hi: float = 250.0,
) -> tuple[str | None, float]:
    """Classify card suit (S, H, C, D) from canonical H=45 bottom pip slice directly below rank."""
    if norm_bgr.ndim != 3 or not suit_templates:
        return None, 0.0
    ix0 = max(0, rank_x)
    ix1 = min(cw_norm, rank_x + max(rank_w, 12))
    x0 = max(0, rank_x - 3)
    x1 = min(cw_norm, rank_x + max(rank_w, 16) + 3)
    ink_zone_bot = norm_bgr[27:45, ix0:ix1]
    ink_zone_top = norm_bgr[1:19, ix0:ix1]
    suit_zone = norm_bgr[26:45, x0:x1]
    if suit_zone.size == 0 or ink_zone_bot.size == 0:
        return None, 0.0
    return _classify_suit_zone(suit_zone, [ink_zone_bot, ink_zone_top], suit_templates, paper_hi=paper_hi)


def _classify_top_right_suit(
    norm_bgr: np.ndarray,
    cw_norm: int,
    suit_templates: list[TemplateEntry],
    paper_hi: float = 250.0,
) -> tuple[str | None, float]:
    """Fallback suit check on the top-right upright pip (rotated 180 deg) for single badge-occluded cards."""
    if norm_bgr.ndim != 3 or not suit_templates or cw_norm < 24:
        return None, 0.0
    x0 = max(0, cw_norm - 19)
    tr_zone = cv2.rotate(norm_bgr[1:20, x0:cw_norm], cv2.ROTATE_180)
    return _classify_suit_zone(tr_zone, [tr_zone], suit_templates, paper_hi=paper_hi)


def _detect_cluster_cards(
    roi_bgr: np.ndarray,
    cluster: tuple[int, int, int, int],
    rank_templates: list[TemplateEntry],
    suit_templates: list[TemplateEntry],
    floor_conf: float,
    is_dealer: bool,
    min_hud_conf: float = 0.84,
    ref_ch: float | None = None,
    sub_out: list[dict[str, Any]] | None = None,
) -> tuple[list[CardDetection], bool]:
    """Detect cards and suits inside one normalized H=45 white card cluster."""
    cx, cy, cw, ch = cluster
    if ref_ch is not None and ch < 0.85 * ref_ch:
        ch = max(ch, min(roi_bgr.shape[0] - cy, int(round(ref_ch))))
    edge_clipped = (cx + cw) >= (roi_bgr.shape[1] - 1)
    cluster_bgr = roi_bgr[cy : cy + ch, cx : cx + cw]
    if cluster_bgr.size == 0 or not rank_templates:
        return [], False

    # Trim outer dark border rows before vertical normalization without cropping badge-occluded lower rows
    cl_mb = _to_min_gray(cluster_bgr)
    paper_hi = float(np.percentile(cl_mb, 92))
    paper_scale = min(1.0, max(0.45, paper_hi / 220.0))
    row_wh_thr = 142.0 if is_dealer else max(92.0, min(155.0, paper_hi * 0.72))
    paper_thr = max(92, min(158, int(round(paper_hi * 0.74))))
    ink_thr = max(62, min(120, int(round(paper_hi * 0.55))))
    min_contrast = max(48, int(round(70.0 * paper_scale)))
    pip_paper_thr = max(92, min(155, int(round(paper_hi * 0.72))))
    pip_ink_thr = max(68, min(145, int(round(paper_hi * 0.62))))
    badge_dark_thr = max(55, min(140, int(round(paper_hi * 0.56))))
    is_dimmed_cl = (not is_dealer) and paper_hi < 175.0
    dim_max_ink = max(55, int(round(paper_hi * 0.42)))
    white_rows = np.where(np.mean(cl_mb > row_wh_thr, axis=1) >= 0.12)[0]
    if len(white_rows) >= 13 and (int(white_rows[-1]) + 1 - int(white_rows[0])) >= int(round(ch * 0.82)):
        y_off = int(white_rows[0])
        y_end = int(white_rows[-1]) + 1
        cluster_bgr = cluster_bgr[y_off:y_end, :]
        cy += y_off
        ch = y_end - y_off

    sc = 45.0 / float(ch)
    cw_norm = max(1, int(round(cw * sc)))
    if not is_dealer and not edge_clipped and cw_norm < 22:
        return [], False
    interp = cv2.INTER_CUBIC if sc > 1.0 else cv2.INTER_AREA
    norm_bgr = cluster_bgr if ch == 45 else cv2.resize(cluster_bgr, (cw_norm, 45), interpolation=interp)
    norm_top = _to_min_gray(norm_bgr[:23, :])

    eff_right = cw_norm
    cluster_has_back = False
    if is_dealer and cw_norm >= 28 and norm_bgr.ndim == 3:
        # Playtech red-checkered BACK spans rank (10:19), divider gap (19:27), and lower pip (27:36) without white paper
        band_full = norm_bgr[10:36, :]
        bf_b = band_full[:, :, 0].astype(np.int16)
        bf_g = band_full[:, :, 1].astype(np.int16)
        bf_r = band_full[:, :, 2].astype(np.int16)
        red_mask = (bf_r - np.maximum(bf_b, bf_g)) > 32
        red_up = np.mean(red_mask[:9, :], axis=0)
        red_gap = np.mean(red_mask[9:17, :], axis=0)
        red_lo = np.mean(red_mask[17:, :], axis=0)
        white_mid = np.mean(band_full.min(axis=2) > 165, axis=0)
        for col in range(22, cw_norm - 5):
            if (
                float(np.mean(red_gap[col : col + 6])) >= 0.24
                and float(np.mean(red_up[col : col + 6])) >= 0.20
                and float(np.mean(red_lo[col : col + 6])) >= 0.20
                and float(np.mean(white_mid[col : col + 6])) < 0.55
            ):
                eff_right = col
                cluster_has_back = True
                break

    if eff_right < 14:
        return [], cluster_has_back

    def _is_back_column(x0: int, sw_check: int) -> bool:
        if not is_dealer or norm_bgr.ndim != 3:
            return False
        mid_chk = norm_bgr[17:31, x0 : min(cw_norm, x0 + max(sw_check, 12))]
        if mid_chk.size == 0:
            return False
        mb = mid_chk[:, :, 0].astype(np.int16)
        mg = mid_chk[:, :, 1].astype(np.int16)
        mr = mid_chk[:, :, 2].astype(np.int16)
        red_r = float(np.mean((mr - np.maximum(mb, mg)) > 32))
        white_r = float(np.mean(mid_chk.min(axis=2) > 165))
        return red_r > 0.22 and white_r < 0.55

    min_tail = 11 if is_dealer else (14 if (not cluster_has_back and not edge_clipped) else 12)
    peaks: list[tuple[float, CardDetection]] = []
    sub_peaks: list[tuple[float, CardDetection]] = []
    sub_floor = 0.42 if (is_dealer and sub_out is not None) else floor_conf
    roi_h, roi_w = norm_top.shape[:2]
    _loc_kernel = np.ones((3, 3), np.uint8)
    for tpl in rank_templates:
        for tsc in (0.93, 1.0, 1.07):
            entry = _get_scaled_rank_tpl_entry(tpl, tsc, masked=False)
            if entry is None:
                continue
            scaled, sw, sh, _, _, _ = entry
            if sw > roi_w or sh > roi_h:
                continue
            max_x_for_tpl = eff_right - max(min_tail, sw)
            if max_x_for_tpl < 0:
                continue
            # Match only over the valid top-y & x window so OpenCV skips wasted rows/cols
            sub_h = min(roi_h, sh + 8)
            sub_w = min(roi_w, max_x_for_tpl + sw)
            res = cv2.matchTemplate(norm_top[:sub_h, :sub_w], scaled, cv2.TM_CCOEFF_NORMED)
            if res.size == 0 or float(res.max()) < sub_floor:
                continue
            # Evaluate ONLY 2D local maxima instead of every pixel on the correlation hill
            if res.shape[0] >= 3 and res.shape[1] >= 3:
                loc_max = cv2.dilate(res, _loc_kernel)
                ys, xs = np.where((res >= sub_floor) & (res >= loc_max - 1e-6))
            else:
                ys, xs = np.where(res >= sub_floor)
            for y_pos, x_pos in zip(ys.tolist(), xs.tolist()):
                if y_pos > 8 or x_pos > max_x_for_tpl:
                    continue
                conf = float(res[y_pos, x_pos])
                if not np.isfinite(conf):
                    continue
                # Reject dark/red score badges, fade frames, and dark borders lacking crisp paper+ink contrast
                patch = norm_top[y_pos : y_pos + sh, x_pos : x_pos + sw]
                is_dealer_tail = is_dealer and x_pos >= (cw_norm - 24)
                min_bright_frac = 0.20 if is_dealer_tail else 0.26
                min_ptp = 58 if is_dealer_tail else min_contrast
                min_dark_frac = 0.06 if is_dealer_tail else 0.07
                if (
                    float(np.mean(patch > paper_thr)) < min_bright_frac
                    or (int(patch.max()) - int(patch.min())) < min_ptp
                    or float(np.mean(patch < ink_thr)) < min_dark_frac
                    or (is_dimmed_cl and int(patch.min()) >= dim_max_ink)
                ):
                    continue
                if _is_back_column(x_pos, sw):
                    continue
                cand_det = CardDetection(
                    rank=tpl.rank,
                    val=tpl.val,
                    conf=conf,
                    x=int(x_pos),
                    y=int(y_pos),
                    w=sw,
                    h=sh,
                )
                if conf >= floor_conf:
                    priority = conf + (0.015 if tpl.rank == "10" else 0.0)
                    peaks.append((priority, cand_det))
                elif is_dealer and sub_out is not None:
                    sub_peaks.append((conf, cand_det))

    if not peaks:
        kept: list[CardDetection] = []
    else:
        peaks.sort(key=lambda item: item[0], reverse=True)
        kept = []
        min_dx = 11
        for _, cand in peaks:
            if any(abs(cand.x - k.x) < min_dx for k in kept):
                continue
            kept.append(cand)
        kept.sort(key=lambda d: d.x)

    # Locate Card 0's true left paper edge above the score pill/badge (elongated pills like 1/11 can shift cluster left by 12-20px)
    top_white_cols = np.where(np.mean(norm_top[1:8, :] > paper_thr, axis=0) >= 0.45)[0]
    first_card_x = min(max(0, cw_norm - 12), min(24, int(top_white_cols[0]))) if len(top_white_cols) else 0
    badge_probe = norm_top[14:23, max(0, first_card_x - 2) : min(cw_norm, first_card_x + 10)]
    if norm_bgr.shape[0] >= 38:
        badge_probe = _to_min_gray(norm_bgr[18:38, max(0, first_card_x - 4) : min(cw_norm, first_card_x + 10)])
    top_paper = float(np.mean(norm_top[1:8, first_card_x : min(cw_norm, first_card_x + 14)] > paper_thr))
    left_badge_intrusion = (
        top_paper >= 0.25
        and badge_probe.size > 0
        and float(np.mean(badge_probe < badge_dark_thr)) >= 0.22
    )

    # Masked left-badge recovery when card 0's bottom-left corner is clipped or weakened by a score pill/badge
    card0_missing = (kept and kept[0].x >= first_card_x + 10) or (not kept and left_badge_intrusion and cw_norm >= 12)
    card0_weak_under_badge = left_badge_intrusion and kept and kept[0].x <= first_card_x + 6 and kept[0].conf < 0.88
    if card0_missing or card0_weak_under_badge:
        gap_w = kept[0].x if card0_missing and kept else min(first_card_x + 22, cw_norm)
        top_white = float(np.mean(norm_top[1:11, first_card_x : min(cw_norm, max(first_card_x + 12, gap_w))] > paper_thr))
        if top_white >= 0.20:
            best_left: tuple[float, CardDetection] | None = None
            mask_thr = max(0.75, floor_conf)
            mask_paper_thr = max(88, int(round(paper_thr * 0.96)))
            mask_ink_thr = min(135, ink_thr + 5)
            mask_min_contrast = max(44, min_contrast - 5)
            for tpl in rank_templates:
                for tsc in (0.95, 1.03):
                    entry = _get_scaled_rank_tpl_entry(tpl, tsc, masked=True)
                    if entry is None:
                        continue
                    scaled_u8, sw, sh, tm_base, t_norm, mask = entry
                    if sw > norm_top.shape[1] or sh > norm_top.shape[0] or tm_base is None or mask is None:
                        continue
                    min_lx = max(0, first_card_x - 1)
                    max_lx = min(first_card_x + 6, max(min_lx + 1, gap_w - 10), norm_top.shape[1] - sw + 1)
                    max_ly = min(6, norm_top.shape[0] - sh + 1)
                    if max_lx <= min_lx or max_ly <= 0:
                        continue
                    # Fast C++ masked template match over all (y0, x0) in one SIMD call
                    sub_y1 = min(norm_top.shape[0], max_ly - 1 + sh)
                    sub_x1 = min(norm_top.shape[1], max_lx - 1 + sw)
                    sub_top = norm_top[0:sub_y1, min_lx:sub_x1]
                    if sub_top.shape[0] < sh or sub_top.shape[1] < sw:
                        continue
                    mask_u8 = mask.astype(np.uint8) * 255
                    res_m = cv2.matchTemplate(sub_top, scaled_u8, cv2.TM_CCOEFF_NORMED, mask=mask_u8)
                    _, sc_masked, _, max_loc = cv2.minMaxLoc(res_m)
                    if not np.isfinite(sc_masked) or float(sc_masked) < mask_thr:
                        continue
                    x0 = min_lx + int(max_loc[0])
                    y0 = int(max_loc[1])
                    if card0_missing and kept and abs(x0 - kept[0].x) < 11:
                        continue
                    p = norm_top[y0 : y0 + sh, x0 : x0 + sw]
                    pm_raw = p[mask]
                    if (
                        pm_raw.size == 0
                        or float(np.mean(pm_raw > mask_paper_thr)) < 0.24
                        or (int(pm_raw.max()) - int(pm_raw.min())) < mask_min_contrast
                        or float(np.mean(pm_raw < mask_ink_thr)) < 0.07
                        or (is_dimmed_cl and int(pm_raw.min()) >= dim_max_ink)
                        or _is_back_column(x0, sw)
                    ):
                        continue
                    prio = float(sc_masked) + (0.015 if tpl.rank == "10" else 0.0)
                    if best_left is None or prio > best_left[0]:
                        best_left = (
                            prio,
                            CardDetection(
                                rank=tpl.rank,
                                val=tpl.val,
                                conf=float(sc_masked),
                                x=int(x0),
                                y=int(y0),
                                w=sw,
                                h=sh,
                            ),
                        )
            if best_left is not None:
                if card0_missing:
                    kept.insert(0, best_left[1])
                elif best_left[1].conf >= max(0.78, kept[0].conf + 0.03):
                    kept[0] = best_left[1]

    def _record_sub(cand_d: CardDetection, suit_v: str | None, pip_ok: bool) -> None:
        if not is_dealer or sub_out is None:
            return
        sub_out.append(
            {
                "rank": cand_d.rank,
                "val": cand_d.val,
                "conf": float(cand_d.conf),
                "x": cx + int(round(cand_d.x / sc)),
                "y": cy + int(round(cand_d.y / sc)),
                "w": max(4, int(round(cand_d.w / sc))),
                "h": max(4, int(round(cand_d.h / sc))),
                "suit": suit_v,
                "has_pip_body": bool(pip_ok),
                "rem_right": int(eff_right - cand_d.x),
                "cw_norm": int(cw_norm),
            }
        )

    if is_dealer and sub_out is not None and sub_peaks:
        sub_peaks.sort(key=lambda item: item[0], reverse=True)
        seen_sub: set[tuple[str, int]] = set()
        for _, sp in sub_peaks[:12]:
            key_s = (sp.rank, sp.x // 6)
            if key_s in seen_sub:
                continue
            seen_sub.add(key_s)
            _record_sub(sp, None, False)

    if not kept:
        return [], cluster_has_back

    out: list[CardDetection] = []
    for d in kept:
        suit, suit_conf = _classify_suit(norm_bgr, d.x, d.w, cw_norm, suit_templates, paper_hi=paper_hi)
        has_pip_body = False
        if suit_templates:
            sx0 = max(0, d.x - 2)
            sx1 = min(cw_norm, d.x + max(d.w, 14) + 2)
            sz = _to_min_gray(norm_bgr[26:45, sx0:sx1]) if norm_bgr.shape[0] >= 40 else np.empty((0, 0), dtype=np.uint8)
            has_pip_body = (
                sz.size > 0
                and float(np.mean(sz > pip_paper_thr)) >= 0.30
                and int(np.sum(sz < pip_ink_thr)) >= 5
            )
            # Score badge/pill on Card 0 covers lower-left (rows 26:45) with dark badge pixels
            has_badge_occlusion = (
                d.x <= first_card_x + 6
                and sz.size > 0
                and float(np.mean(sz < badge_dark_thr)) >= 0.25
                and d.conf >= max(0.75, floor_conf)
            )
            if suit is None and has_badge_occlusion and cw_norm <= 44:
                tr_suit, tr_conf = _classify_top_right_suit(norm_bgr, cw_norm, suit_templates, paper_hi=paper_hi)
                if tr_suit is not None:
                    suit, suit_conf = tr_suit, tr_conf
            if (not cluster_has_back and not edge_clipped) and not has_badge_occlusion:
                rem_right = eff_right - d.x
                if rem_right < 15:
                    min_r15 = max(0.74 if is_dealer else 0.84, (min_hud_conf - 0.10) if is_dealer else min_hud_conf)
                    if d.conf < min_r15 or (suit is None and not has_pip_body):
                        _record_sub(d, suit, has_pip_body)
                        continue
                elif rem_right < 22:
                    min_r22 = max(0.72 if is_dealer else 0.82, min_hud_conf - (0.12 if is_dealer else 0.02))
                    if d.conf < min_r22:
                        _record_sub(d, suit, has_pip_body)
                        continue
                    if suit is None and (not has_pip_body or d.conf < max(0.82, min_hud_conf - 0.02)):
                        _record_sub(d, suit, has_pip_body)
                        continue
            high_conf_pip_ok = has_pip_body and suit_conf >= 0.35 and d.conf >= max(0.75 if is_dealer else 0.86, 0.75 if is_dealer else min_hud_conf)
            if suit is None and not has_badge_occlusion and not high_conf_pip_ok:
                if not has_pip_body or suit_conf < 0.42 or d.conf < max(0.82, min_hud_conf - 0.02):
                    _record_sub(d, suit, has_pip_body)
                    continue
            elif suit is not None and d.conf < max(0.78, min_hud_conf - 0.06) and suit_conf < 0.60:
                _record_sub(d, suit, has_pip_body)
                continue
            if d.rank == "4" and not has_badge_occlusion and d.conf < max(0.82, min_hud_conf - 0.02):
                _record_sub(d, suit, has_pip_body)
                continue
            if d.rank == "Q" and suit is None and not has_badge_occlusion and (not has_pip_body or suit_conf < 0.45 or d.conf < 0.88):
                _record_sub(d, suit, has_pip_body)
                continue
        out.append(
            CardDetection(
                rank=d.rank,
                val=d.val,
                conf=d.conf,
                x=cx + int(round(d.x / sc)),
                y=cy + int(round(d.y / sc)),
                w=max(4, int(round(d.w / sc))),
                h=max(4, int(round(d.h / sc))),
                suit=suit,
                hand_index=0,
                has_pip_body=bool(has_pip_body),
            )
        )
    return out, cluster_has_back


_RANK_SCALE_CACHE: dict[tuple[tuple[int, ...], str, int, float, bool], tuple[np.ndarray, int, int, np.ndarray | None, float] | None] = {}
_RANK_SCALE_CACHE_MAX = 512


def _put_rank_scale_cache(
    key: tuple[tuple[int, ...], str, int, float, bool],
    val: tuple[np.ndarray, int, int, np.ndarray | None, float] | None,
) -> None:
    if len(_RANK_SCALE_CACHE) >= _RANK_SCALE_CACHE_MAX and key not in _RANK_SCALE_CACHE:
        try:
            _RANK_SCALE_CACHE.pop(next(iter(_RANK_SCALE_CACHE)))
        except StopIteration:
            pass
    _RANK_SCALE_CACHE[key] = val


def _get_scaled_rank_tpl_entry(
    tpl: TemplateEntry, tsc: float, masked: bool = False
) -> tuple[np.ndarray, int, int, np.ndarray | None, float, np.ndarray | None] | None:
    """Return cached resized rank template (and precomputed corner mask/vector when masked=True)."""
    key = ("r", tsc, masked)
    if key in tpl._scale_cache:
        return tpl._scale_cache[key]
    t_img = tpl.min_gray if tpl.min_gray is not None else tpl.gray
    th, tw = t_img.shape[:2]
    sw = max(4, int(round(tw * tsc)))
    sh = max(4, int(round(th * tsc)))
    t_interp = cv2.INTER_AREA if tsc < 1.0 else cv2.INTER_CUBIC
    scaled = t_img if tsc == 1.0 else cv2.resize(t_img, (sw, sh), interpolation=t_interp)
    if float(np.std(scaled)) < 1.0:
        tpl._scale_cache[key] = None
        return None
    if not masked:
        val = (scaled, sw, sh, None, 0.0, None)
        tpl._scale_cache[key] = val
        return val
    mask = np.ones((sh, sw), dtype=bool)
    if sh > 8 and sw > 7:
        mask[8:, : max(7, int(round(sw * 0.48)))] = False
    tm_base = scaled[mask].astype(np.float32)
    tm_base -= float(tm_base.mean())
    t_norm = float(np.linalg.norm(tm_base))
    if t_norm < 1e-5:
        tpl._scale_cache[key] = None
        return None
    val = (scaled, sw, sh, tm_base, t_norm, mask)
    tpl._scale_cache[key] = val
    return val


def _get_scaled_rank_tpl(
    t_img: np.ndarray, tsc: float, masked: bool = False
) -> tuple[np.ndarray, int, int, np.ndarray | None, float] | None:
    """Backward-compatible wrapper for cached scaled rank templates."""
    key = (t_img.shape, t_img.dtype.str, hash(t_img.tobytes()), tsc, masked)
    cached = _RANK_SCALE_CACHE.get(key)
    if cached is not None or key in _RANK_SCALE_CACHE:
        return cached
    th, tw = t_img.shape[:2]
    sw = max(4, int(round(tw * tsc)))
    sh = max(4, int(round(th * tsc)))
    t_interp = cv2.INTER_AREA if tsc < 1.0 else cv2.INTER_CUBIC
    scaled = t_img if tsc == 1.0 else cv2.resize(t_img, (sw, sh), interpolation=t_interp)
    if float(np.std(scaled)) < 1.0:
        _put_rank_scale_cache(key, None)
        return None
    if not masked:
        val = (scaled, sw, sh, None, 0.0)
        _put_rank_scale_cache(key, val)
        return val
    mask = np.ones((sh, sw), dtype=bool)
    if sh > 8 and sw > 7:
        mask[8:, :7] = False
    tm_base = scaled[mask].astype(np.float32)
    tm_base -= float(tm_base.mean())
    t_norm = float(np.linalg.norm(tm_base))
    if t_norm < 1e-5:
        _put_rank_scale_cache(key, None)
        return None
    val = (scaled, sw, sh, tm_base, t_norm)
    _put_rank_scale_cache(key, val)
    return val


_TOP_BANNER_SCALES = (
    0.55, 0.66, 0.78, 0.85, 0.92, 1.00, 1.08, 1.16, 1.28, 1.45, 1.70, 2.00, 2.35,
)
_CENTER_BANNER_SCALES = (0.75, 0.90, 1.05, 1.25, 1.50, 1.85, 2.25)
_PLACE_BETS_SCALES = (
    0.50, 0.62, 0.76, 0.92, 1.10, 1.32, 1.58, 1.90, 2.30,
)


_BADGE_DIGIT_GLYPHS: dict[str, list[str]] = {
    "0": [
        "..........",
        "..######..",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        "..######..",
        "..........",
    ],
    "1": [
        "..........",
        "....###...",
        "...####...",
        "..##.##...",
        ".....##...",
        ".....##...",
        ".....##...",
        ".....##...",
        ".....##...",
        ".....##...",
        ".....##...",
        ".....##...",
        "..#######.",
        "..........",
    ],
    "2": [
        "..........",
        "..######..",
        ".##....##.",
        ".##....##.",
        ".......##.",
        "......##..",
        ".....##...",
        "....##....",
        "...##.....",
        "..##......",
        ".##.......",
        ".##.......",
        ".########.",
        "..........",
    ],
    "3": [
        "..........",
        "..######..",
        ".##....##.",
        ".......##.",
        ".......##.",
        "......##..",
        "...#####..",
        "......##..",
        ".......##.",
        ".......##.",
        ".##....##.",
        ".##....##.",
        "..######..",
        "..........",
    ],
    "4": [
        "..........",
        ".....###..",
        "....####..",
        "...##.##..",
        "..##..##..",
        ".##...##..",
        ".##...##..",
        ".########.",
        "......##..",
        "......##..",
        "......##..",
        "......##..",
        "......##..",
        "..........",
    ],
    "5": [
        "..........",
        ".########.",
        ".##.......",
        ".##.......",
        ".##.......",
        ".#######..",
        ".......##.",
        ".......##.",
        ".......##.",
        ".......##.",
        ".##....##.",
        ".##....##.",
        "..######..",
        "..........",
    ],
    "6": [
        "..........",
        "...#####..",
        "..##...##.",
        ".##.......",
        ".##.......",
        ".#######..",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        "..######..",
        "..........",
    ],
    "7": [
        "..........",
        ".########.",
        ".##....##.",
        ".......##.",
        "......##..",
        ".....##...",
        ".....##...",
        "....##....",
        "....##....",
        "...##.....",
        "...##.....",
        "...##.....",
        "...##.....",
        "..........",
    ],
    "8": [
        "..........",
        "..######..",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        "..######..",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        "..######..",
        "..........",
    ],
    "9": [
        "..........",
        "..######..",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        ".##....##.",
        "..#######.",
        ".......##.",
        ".......##.",
        "......##..",
        ".##...##..",
        "..#####...",
        "..........",
    ],
}
_BADGE_DIGIT_TPLS: list[tuple[str, np.ndarray, float]] | None = None


def _get_badge_digit_templates() -> list[tuple[str, np.ndarray, float]]:
    """Build cached 10x14 zero-mean digit vectors for Dealer Score Badge OCR."""
    global _BADGE_DIGIT_TPLS
    if _BADGE_DIGIT_TPLS is not None:
        return _BADGE_DIGIT_TPLS
    out: list[tuple[str, np.ndarray, float]] = []
    for digit, rows in _BADGE_DIGIT_GLYPHS.items():
        vec = np.array([255.0 if ch == "#" else 0.0 for row in rows for ch in row], dtype=np.float32)
        vec -= float(vec.mean())
        out.append((digit, vec, float(np.linalg.norm(vec))))
    _BADGE_DIGIT_TPLS = out
    return out


def _match_badge_digit_patch(gray_img: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[str | None, float]:
    """Match a white digit glyph inside a dark dealer score badge against 10x14 digit vectors."""
    if gray_img.size == 0 or w < 2 or h < 5:
        return None, 0.0
    target_h = 12
    target_w = max(2, min(8, int(round(w * (target_h / float(h))))))
    cropped = gray_img[y : y + h, x : x + w]
    if cropped.size == 0:
        return None, 0.0
    resized = cv2.resize(cropped, (target_w, target_h), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    base_ox = (10 - target_w) // 2
    oy = 1
    tpls = _get_badge_digit_templates()
    best_d: str | None = None
    best_conf = -1.0
    tried_ox = sorted({max(0, min(10 - target_w, base_ox + d)) for d in (-1, 0, 1)})
    for ox in tried_ox:
        canvas = np.zeros((14, 10), dtype=np.float32)
        canvas[oy : oy + target_h, ox : ox + target_w] = resized
        vec = canvas.reshape(-1)
        vec = vec - float(vec.mean())
        c_norm = float(np.linalg.norm(vec))
        if c_norm < 1e-4:
            continue
        for digit, tpl_vec, tpl_norm in tpls:
            conf = float(np.dot(vec, tpl_vec) / (c_norm * tpl_norm))
            if conf > best_conf:
                best_conf = conf
                best_d = digit
    return best_d, best_conf


def detect_dealer_badge_total(
    dealer_img: np.ndarray,
    clusters: list[tuple[int, int, int, int]] | None = None,
    return_hint: bool = False,
) -> tuple[int | None, float] | tuple[int | None, float, dict[str, Any] | None]:
    """Read the white-on-dark circular Dealer Score Badge total ([4..26]) beside/below dealer cards."""
    if dealer_img.size == 0 or dealer_img.ndim < 2 or dealer_img.shape[0] < 16 or dealer_img.shape[1] < 16:
        return (None, 0.0, None) if return_hint else (None, 0.0)
    dh, dw = dealer_img.shape[:2]
    if clusters:
        cx, cy, _, ch = clusters[0]
        bx0 = max(0, int(round(cx - ch * 1.40)))
        bx1 = min(dw, int(round(cx + ch * 0.35)))
        by0 = max(0, int(round(cy - ch * 0.08)))
        by1 = min(dh, int(round(cy + ch * 1.35)))
        min_digit_h = max(6, int(round(ch * 0.16)))
        max_digit_h = max(min_digit_h + 4, int(round(ch * 0.65)))
    else:
        bx0 = 0
        by0 = int(round(dh * 0.20))
        bx1 = int(round(dw * 0.55))
        by1 = dh
        min_digit_h = 7
        max_digit_h = int(round(dh * 0.65))

    bw = bx1 - bx0
    bh = by1 - by0
    if bw < 10 or bh < 10:
        return (None, 0.0, None) if return_hint else (None, 0.0)

    sub_bgr = dealer_img[by0:by1, bx0:bx1]
    sub_gray = _to_gray(sub_bgr)
    is_color = sub_bgr.ndim == 3 and sub_bgr.shape[2] >= 3
    if is_color:
        b_ch = sub_bgr[:, :, 0].astype(np.int16)
        g_ch = sub_bgr[:, :, 1].astype(np.int16)
        r_ch = sub_bgr[:, :, 2].astype(np.int16)
        mn_ch = np.minimum(np.minimum(b_ch, g_ch), r_ch)
        mx_ch = np.maximum(np.maximum(b_ch, g_ch), r_ch)
        chroma = mx_ch - mn_ch
        mask = ((mn_ch >= 150) | ((sub_gray >= 165) & (chroma < 42))).astype(np.uint8)
        pill_bg_mask = (sub_gray < 135) | ((chroma >= 45) & (mn_ch < 155))
    else:
        mask = (sub_gray >= 165).astype(np.uint8)
        pill_bg_mask = sub_gray < 135

    num, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    digit_cands: list[tuple[int, int, int, int, str, float]] = []
    soft_cands: list[tuple[int, int, int, int, str, float]] = []
    for lbl in range(1, num):
        x, y, w, h, area = [int(v) for v in stats[lbl]]
        if h < min_digit_h or h > max_digit_h:
            continue
        if w < 2 or w > int(round(h * 1.05)) or area < 6:
            continue
        if (x + w) >= (bw - 1) and h >= max_digit_h * 0.9:
            continue

        mx0 = max(0, x - 3)
        my0 = max(0, y - 3)
        mx1 = min(bw, x + w + 3)
        my1 = min(bh, y + h + 3)
        ring_bg = pill_bg_mask[my0:my1, mx0:mx1]
        ring_mask = np.ones(ring_bg.shape, dtype=bool)
        ring_mask[y - my0 : y + h - my0, x - mx0 : x + w - mx0] = False
        ring_px = ring_bg[ring_mask]
        dark_ratio = float(np.mean(ring_px)) if ring_px.size > 0 else 0.0
        if ring_px.size > 0 and dark_ratio < 0.45:
            continue

        digit, conf = _match_badge_digit_patch(sub_gray, x, y, w, h)
        min_single_conf = 0.55 if dark_ratio >= 0.80 else 0.68
        if digit is not None and conf >= 0.54:
            soft_cands.append((x, y, w, h, digit, conf))
            if conf >= min_single_conf:
                digit_cands.append((x, y, w, h, digit, conf))

    if not soft_cands:
        return (None, 0.0, None) if return_hint else (None, 0.0)

    def _group_digits(cands: list[tuple[int, int, int, int, str, float]]) -> list[list[tuple[int, int, int, int, str, float]]]:
        cands.sort(key=lambda c: c[0])
        raw_runs: list[list[tuple[int, int, int, int, str, float]]] = []
        for cand in cands:
            x, y, w, h, _, _ = cand
            if raw_runs:
                last_g = raw_runs[-1]
                px, py, pw, ph, _, _ = last_g[-1]
                same_row = abs(y - py) <= max(3, int(round(ph * 0.25)))
                same_h = (ph * 0.70) <= h <= (ph * 1.40)
                gap = x - (px + pw)
                if same_row and same_h and -2 <= gap <= max(4, int(round(ph * 0.55))):
                    last_g.append(cand)
                    continue
            raw_runs.append([cand])

        # Split any 3+ digit soft X/Y run (e.g. "7/17" with '/' dropped -> [7, 1, 7]) from the right
        grps: list[list[tuple[int, int, int, int, str, float]]] = []
        for run in raw_runs:
            if len(run) <= 2:
                grps.append(run)
            else:
                tail2 = run[-2:]
                tail_val = int("".join(c[4] for c in tail2))
                if 12 <= tail_val <= 21:
                    grps.append(run[:-2])
                    grps.append(tail2)
                else:
                    for i in range(0, len(run), 2):
                        grps.append(run[i : i + 2])
        return grps

    best_total: int | None = None
    best_conf = 0.0
    if digit_cands:
        for g in reversed(_group_digits(digit_cands)):
            total = int("".join(c[4] for c in g))
            min_conf = min(c[5] for c in g)
            if 4 <= total <= 26 and min_conf >= 0.56:
                best_total = total
                best_conf = float(min_conf)
                break

    if not return_hint:
        return best_total, best_conf

    soft_hint: dict[str, Any] | None = None
    for g in reversed(_group_digits(soft_cands)):
        if len(g) == 2:
            t_dig, o_dig = int(g[0][4]), int(g[1][4])
            t_conf, o_conf = float(g[0][5]), float(g[1][5])
            tot = t_dig * 10 + o_dig
            if 10 <= tot <= 26:
                soft_hint = {
                    "total": tot,
                    "conf": min(t_conf, o_conf),
                    "tensDigit": t_dig,
                    "tensConf": t_conf,
                    "onesDigit": o_dig,
                    "onesConf": o_conf,
                }
                break
        elif len(g) == 1:
            dig = int(g[0][4])
            d_conf = float(g[0][5])
            soft_hint = {
                "total": dig if 4 <= dig <= 9 else None,
                "conf": d_conf,
                "tensDigit": dig if dig in (1, 2) else None,
                "tensConf": d_conf if dig in (1, 2) else 0.0,
                "onesDigit": dig,
                "onesConf": d_conf,
            }
            break
    return best_total, best_conf, soft_hint


class PlaytechDetector:
    """Combines normalized HUD rank/suit detection with felt burst tracking and banner detection."""

    def __init__(self, cfg: CompanionConfig, templates_root: Path | None = None) -> None:
        self.cfg = cfg
        root = templates_root or (COMPANION_DIR / "templates")
        self.hud_bank = TemplateBank(root / "hud")
        self.felt_bank = TemplateBank(root / "felt")
        self.banner_bank = TemplateBank(root / "banner")
        self.suit_bank = TemplateBank(root / "suit")
        self.felt_monitor = FeltBurstMonitor(cfg, self.felt_bank, zone="community", fallback_bank=self.hud_bank)
        self.dealer_felt_monitor = FeltBurstMonitor(cfg, self.felt_bank, zone="dealer", fallback_bank=self.hud_bank)
        self._last_p_sig: bytes | None = None
        self._last_d_sig: bytes | None = None
        self._last_banner_sig: bytes | None = None
        self._last_pb_sig: bytes | None = None
        self._last_shuffle_sig: bytes | None = None
        self._last_center_sig: bytes | None = None
        self._last_shuffle_scan_ts: float = -999.0
        self._last_center_scan_ts: float = -999.0
        self._banner_scale_hint: float | None = None
        self._banner_tpl_hints: dict[str, tuple[str, float]] = {}
        self._cached_p: tuple[list[CardDetection], dict[int, list[CardDetection]], bool] = ([], {}, False)
        self._cached_d: list[CardDetection] = []
        self._cached_d_sub: list[dict[str, Any]] = []
        self._cached_d_badge: tuple[int | None, float, dict[str, Any] | None] = (None, 0.0, None)
        self._last_d_clusters: list[tuple[int, int, int, int]] = []
        self._cached_top_banner: tuple[str, float] = ("", 0.0)
        self._cached_pb: tuple[float, bool] = (0.0, False)
        self._cached_shuf: tuple[bool, float, str, float] = (False, 0.0, "", 0.0)
        self._last_shuf_mode: tuple[bool, bool] = (False, False)
        self._cached_center_banner: tuple[str, float, bool] = ("", 0.0, False)
        self._cached_b: tuple[bool, float, str, float, float, str] = (False, 0.0, "", 0.0, 0.0, "")

    @property
    def _last_hud_hash(self) -> tuple[bytes | None, bytes | None, bytes | None, bytes | None] | None:
        if (
            self._last_p_sig is None
            and self._last_d_sig is None
            and self._last_banner_sig is None
            and self._last_shuffle_sig is None
        ):
            return None
        return (self._last_p_sig, self._last_d_sig, self._last_banner_sig, self._last_shuffle_sig)

    @_last_hud_hash.setter
    def _last_hud_hash(self, val: Any) -> None:
        if val is None:
            self.invalidate_cache()
        elif isinstance(val, tuple) and len(val) == 4:
            self._last_p_sig, self._last_d_sig, self._last_banner_sig, self._last_shuffle_sig = val
        elif isinstance(val, tuple) and len(val) == 3:
            self._last_p_sig, self._last_d_sig, self._last_banner_sig = val

    def invalidate_cache(self, reset_scale_hint: bool = False) -> None:
        self._last_p_sig = None
        self._last_d_sig = None
        self._last_banner_sig = None
        self._last_pb_sig = None
        self._last_shuffle_sig = None
        self._last_center_sig = None
        self._last_shuffle_scan_ts = -999.0
        self._last_center_scan_ts = -999.0
        self._cached_p = ([], {}, False)
        self._cached_d = []
        self._cached_d_badge = (None, 0.0)
        self._last_d_clusters = []
        self._cached_top_banner = ("", 0.0)
        self._cached_pb = (0.0, False)
        self._cached_shuf = (False, 0.0, "", 0.0)
        self._last_shuf_mode = (False, False)
        self._cached_center_banner = ("", 0.0, False)
        self._cached_b = (False, 0.0, "", 0.0, 0.0, "")
        if reset_scale_hint:
            self._banner_scale_hint = None
            self._banner_tpl_hints.clear()

    def is_center_scan_due(self, ts: float, cards_present: bool) -> bool:
        """Return True when center betting-banner probe should run."""
        return (not cards_present) or ((ts - self._last_center_scan_ts) >= 0.22)

    def _detect_banner_with_hint(
        self,
        probe_img: np.ndarray,
        templates: list[TemplateEntry],
        min_conf: float,
        scales: tuple[float, ...],
        hint_key: str = "banner",
    ) -> tuple[TemplateEntry | None, float]:
        """Probe last matched (template, scale) first for speed, then scan all templates/scales cleanly."""
        if not templates or probe_img.size == 0:
            return None, 0.0
        cached_hint = self._banner_tpl_hints.get(hint_key)
        if cached_hint is not None and len(scales) > 6:
            hint_name, hint_scale = cached_hint
            hint_tpls = [t for t in templates if t.name == hint_name]
            fast_scales = tuple(s for s in scales if abs(s - hint_scale) <= 0.10)
            if hint_tpls and fast_scales:
                ftpl, fconf, fscale = detect_best_single_with_scale(
                    probe_img,
                    hint_tpls,
                    min_conf=min_conf,
                    scales=fast_scales,
                    require_contrast=True,
                )
                if ftpl is not None and fconf >= max(min_conf, 0.85):
                    if fscale is not None:
                        self._banner_tpl_hints[hint_key] = (ftpl.name, fscale)
                        self._banner_scale_hint = fscale
                    return ftpl, fconf

        best_tpl, best_conf, best_scale = detect_best_single_with_scale(
            probe_img,
            templates,
            min_conf=min_conf,
            scales=scales,
            require_contrast=True,
        )
        if best_tpl is not None and best_scale is not None:
            self._banner_tpl_hints[hint_key] = (best_tpl.name, best_scale)
            if len(scales) > 6:
                self._banner_scale_hint = best_scale
        return best_tpl, best_conf

    def reload_templates(self) -> None:
        self.hud_bank.reload()
        self.felt_bank.reload()
        self.banner_bank.reload()
        self.suit_bank.reload()
        self.invalidate_cache()

    def _detect_hud_strip(
        self,
        roi_img: np.ndarray,
        is_dealer: bool,
        sub_out: list[dict[str, Any]] | None = None,
    ) -> tuple[list[CardDetection], dict[int, list[CardDetection]], bool]:
        """Detect cards, suits, and optional horizontal splits inside player or dealer HUD ROI."""
        if roi_img.size == 0 or not self.hud_bank.card_templates:
            return [], {}, False

        floor_conf = max(0.68, self.cfg.min_hud_confidence - 0.10)
        rank_tpls = [t for t in self.hud_bank.card_templates if t.rank != "BACK"]
        med_tpl_h = float(np.median([t.gray.shape[0] for t in rank_tpls])) if rank_tpls else 20.0
        clusters = _find_white_card_clusters(roi_img, is_dealer=is_dealer, allow_dimmed=(not is_dealer)) if med_tpl_h <= 23.0 else []
        if is_dealer:
            self._last_d_clusters = clusters

        if clusters:
            cluster_hits: list[tuple[tuple[int, int, int, int], list[CardDetection]]] = []
            back_seen = False
            ref_ch = float(max(cl[3] for cl in clusters)) if len(clusters) >= 2 else None
            for cl in clusters:
                dets, cl_back = _detect_cluster_cards(
                    roi_img,
                    cl,
                    rank_tpls,
                    self.suit_bank.suit_templates,
                    floor_conf,
                    is_dealer=is_dealer,
                    min_hud_conf=self.cfg.min_hud_confidence,
                    ref_ch=ref_ch,
                    sub_out=sub_out if is_dealer else None,
                )
                back_seen = back_seen or cl_back
                if dets:
                    cluster_hits.append((cl, dets))

            if cluster_hits:
                if is_dealer:
                    flat_d = [d for _, ds in cluster_hits for d in ds]
                    flat_d.sort(key=lambda d: d.x)
                    if len(flat_d) == 1:
                        last_cl, _ = cluster_hits[-1]
                        cx, cy, cw, ch = last_cl
                        # Also probe immediately right of white upcard cluster for detached red-checkered BACK card
                        if not back_seen and roi_img.ndim == 3:
                            rx0 = min(roi_img.shape[1], cx + cw)
                            rx1 = min(roi_img.shape[1], rx0 + max(14, int(round(ch * 0.72))))
                            if rx1 - rx0 >= 8:
                                back_seen = _has_red_back_signature(roi_img[cy : min(roi_img.shape[0], cy + ch), rx0:rx1])
                        if back_seen:
                            flat_d.append(
                                CardDetection(
                                    rank="BACK",
                                    val=None,
                                    conf=0.92,
                                    x=cx + cw,
                                    y=cy,
                                    w=max(10, int(round(ch * 0.65))),
                                    h=ch,
                                )
                            )
                    return flat_d, {0: flat_d}, False

                # Player horizontal multi-cluster split grouping
                avg_ch = float(np.mean([cl[3] for cl, _ in cluster_hits]))
                cl_sep_thr = max(4, int(round(avg_ch * 0.30)))
                card_sep_thr = max(12, int(round(avg_ch * 0.78)))
                hands: dict[int, list[CardDetection]] = {}
                all_p: list[CardDetection] = []
                cur_hand = 0
                prev_cl: tuple[int, int, int, int] | None = None

                for cl, ds in cluster_hits:
                    if prev_cl is not None:
                        cl_gap = cl[0] - (prev_cl[0] + prev_cl[2])
                        if cl_gap >= cl_sep_thr:
                            cur_hand += 1
                    ds_sorted = sorted(ds, key=lambda d: d.x)
                    for idx, det in enumerate(ds_sorted):
                        if idx > 0 and (det.x - (ds_sorted[idx - 1].x + ds_sorted[idx - 1].w)) >= card_sep_thr:
                            cur_hand += 1
                        tagged = CardDetection(
                            rank=det.rank,
                            val=det.val,
                            conf=det.conf,
                            x=det.x,
                            y=det.y,
                            w=det.w,
                            h=det.h,
                            suit=det.suit,
                            hand_index=cur_hand,
                            has_pip_body=det.has_pip_body,
                        )
                        hands.setdefault(cur_hand, []).append(tagged)
                        all_p.append(tagged)
                    prev_cl = cl

                return all_p, hands, len(hands) >= 2

        # Fallback to direct unscaled NMS matching ONLY for legacy un-trimmed synthetic full-card templates (>23px tall)
        if med_tpl_h <= 23.0:
            return [], {}, False
        min_dx_ratio = 4.2 if is_dealer else 3.8
        dx = max(8, int(round(roi_img.shape[1] * (self.cfg.hud_nms_min_dx_ratio * min_dx_ratio))))
        fallback = match_templates_nms(
            roi_img, self.hud_bank.card_templates, floor_conf, min_dx=dx, scales=(0.94, 1.0, 1.06)
        )
        hands_fb: dict[int, list[CardDetection]] = {0: fallback} if fallback else {}
        split_fb = False
        if not is_dealer and len(fallback) >= 2 and roi_img.size:
            max_gap = max(fallback[i + 1].x - fallback[i].x for i in range(len(fallback) - 1))
            if max_gap > roi_img.shape[1] * 0.36:
                split_fb = True
        return fallback, hands_fb, split_fb

    def process_crops(
        self,
        player_img: np.ndarray,
        dealer_img: np.ndarray,
        community_img: np.ndarray | None = None,
        shuffle_img: np.ndarray | None = None,
        banner_img: np.ndarray | None = None,
        ts: float | None = None,
        hud_refreshed: bool = True,
        center_img: np.ndarray | None = None,
        player_refreshed: bool = True,
        dealer_refreshed: bool = True,
        banner_refreshed: bool = True,
        dealer_only: bool = False,
        dealer_felt_img: np.ndarray | None = None,
        place_bets_img: np.ndarray | None = None,
        force_dealer_scan: bool = False,
        shoe_img: np.ndarray | None = None,
        discard_img: np.ndarray | None = None,
        use_shoe_baseline: bool = True,
    ) -> FrameObservation:
        """Analyze cropped ROIs and return detections for a single tick."""
        now = ts if ts is not None else time.monotonic()

        if player_refreshed:
            p_arr = (
                cv2.resize(_to_min_gray(player_img), (96, 32), interpolation=cv2.INTER_AREA)
                if player_img.size
                else None
            )
            prev_p_arr = getattr(self, "_last_p_arr", None)
            p_unchanged = (
                self._last_p_sig is not None
                and p_arr is not None
                and prev_p_arr is not None
                and prev_p_arr.shape == p_arr.shape
                and float(np.mean(np.abs(p_arr.astype(np.int16) - prev_p_arr.astype(np.int16)))) < 1.5
                and int(np.max(np.abs(p_arr.astype(np.int16) - prev_p_arr.astype(np.int16)))) < 14
            ) or (p_arr is None and prev_p_arr is None and self._last_p_sig == b"")
            if p_unchanged:
                p_cards, p_hands, split_detected = self._cached_p
            else:
                self._last_p_arr = p_arr
                self._last_p_sig = p_arr.tobytes() if p_arr is not None else b""
                p_cards, p_hands, split_detected = self._detect_hud_strip(player_img, is_dealer=False)
                self._cached_p = (p_cards, p_hands, split_detected)
        else:
            p_cards, p_hands, split_detected = self._cached_p

        if dealer_refreshed:
            d_arr = (
                cv2.resize(_to_min_gray(dealer_img), (80, 36), interpolation=cv2.INTER_AREA)
                if dealer_img.size
                else None
            )
            prev_d_arr = getattr(self, "_last_d_arr", None)
            d_unchanged = (
                not force_dealer_scan
                and self._last_d_sig is not None
                and d_arr is not None
                and prev_d_arr is not None
                and prev_d_arr.shape == d_arr.shape
                and float(np.mean(np.abs(d_arr.astype(np.int16) - prev_d_arr.astype(np.int16)))) < 1.5
                and int(np.max(np.abs(d_arr.astype(np.int16) - prev_d_arr.astype(np.int16)))) < 14
            ) or (not force_dealer_scan and d_arr is None and prev_d_arr is None and self._last_d_sig == b"")
            if d_unchanged:
                d_cards = self._cached_d
                d_sub_cards = self._cached_d_sub
                d_badge_total, d_badge_conf, d_badge_hint = self._cached_d_badge
            else:
                self._last_d_arr = d_arr
                self._last_d_sig = d_arr.tobytes() if d_arr is not None else b""
                d_sub_cards = []
                d_cards, _, _ = self._detect_hud_strip(dealer_img, is_dealer=True, sub_out=d_sub_cards)
                d_badge_total, d_badge_conf, d_badge_hint = detect_dealer_badge_total(
                    dealer_img, self._last_d_clusters, return_hint=True
                )
                self._cached_d = d_cards
                self._cached_d_sub = d_sub_cards
                self._cached_d_badge = (d_badge_total, d_badge_conf, d_badge_hint)
        else:
            d_cards = self._cached_d
            d_sub_cards = self._cached_d_sub
            d_badge_total, d_badge_conf, d_badge_hint = self._cached_d_badge

        cards_present = bool(p_cards or d_cards)

        if banner_refreshed:
            # 1. Top banner_roi check (WAIT / LAST_BETS / NO_MORE_BETS / SHUFFLE; also supports legacy tall banner_roi)
            has_sep_pb = place_bets_img is not None and place_bets_img.size > 0
            ban_sig_h = 32 if (not has_sep_pb and banner_img is not None and banner_img.size and banner_img.shape[0] >= 80) else 16
            ban_arr = (
                cv2.resize(_to_gray(banner_img), (64, ban_sig_h), interpolation=cv2.INTER_AREA)
                if (banner_img is not None and banner_img.size)
                else None
            )
            prev_ban_arr = getattr(self, "_last_banner_arr", None)
            last_ban_ts = getattr(self, "_last_banner_scan_ts", -999.0)
            ban_unchanged = (
                self._last_banner_sig is not None
                and ban_arr is not None
                and prev_ban_arr is not None
                and prev_ban_arr.shape == ban_arr.shape
                and float(np.mean(np.abs(ban_arr.astype(np.int16) - prev_ban_arr.astype(np.int16)))) < 2.2
            ) or (ban_arr is None and prev_ban_arr is None and self._last_banner_sig == b"")
            if ban_unchanged or (self._last_banner_sig is not None and (now - last_ban_ts) < 0.24 and not self._cached_top_banner[0]):
                top_banner_state, top_banner_conf = self._cached_top_banner
            else:
                self._last_banner_arr = ban_arr
                self._last_banner_sig = ban_arr.tobytes() if ban_arr is not None else b""
                self._last_banner_scan_ts = now
                top_banner_state = ""
                top_banner_conf = 0.0
                if banner_img is not None and banner_img.size and self.banner_bank.banner_templates:
                    bh = banner_img.shape[0]
                    is_tall_banner = (not has_sep_pb) and (bh >= 55) and (self.cfg.rois.get("banner_roi", (0.0, 0.0, 0.0, 0.0))[3] >= 0.18)
                    if is_tall_banner:
                        top_h = max(32, min(bh, int(round(bh * 0.35))))
                        top_strip = banner_img[:top_h, :]
                        btpl, bconf = self._detect_banner_with_hint(
                            top_strip,
                            self.banner_bank.banner_templates,
                            self.cfg.min_banner_confidence,
                            _TOP_BANNER_SCALES,
                            hint_key="top_strip",
                        )
                        if btpl is None:
                            lower_strip = banner_img[max(0, top_h - 12) :, :]
                            center_tpls = [
                                t
                                for t in self.banner_bank.banner_templates
                                if t.rank in ("PLACE_BETS", "LAST_BETS", "NO_MORE_BETS", "SHUFFLE")
                            ]
                            if center_tpls and lower_strip.size:
                                btpl, bconf = self._detect_banner_with_hint(
                                    lower_strip,
                                    center_tpls,
                                    self.cfg.min_banner_confidence,
                                    _TOP_BANNER_SCALES,
                                    hint_key="lower_strip",
                                )
                    else:
                        btpl, bconf = self._detect_banner_with_hint(
                            banner_img,
                            self.banner_bank.banner_templates,
                            self.cfg.min_banner_confidence,
                            _TOP_BANNER_SCALES,
                            hint_key="banner",
                        )
                    if btpl is not None:
                        top_banner_state = btpl.rank
                        top_banner_conf = bconf
                self._cached_top_banner = (top_banner_state, top_banner_conf)

            # 2. Independent shuffle_roi check (throttled to ~1.6 Hz; skip redundant banner scan when overlapping banner_roi)
            shuf_due = (now - self._last_shuffle_scan_ts) >= 0.60
            shuf_mode = (cards_present, bool(top_banner_state))
            shuf_roi_cfg = self.cfg.rois.get("shuffle_roi", (0.30, 0.24, 0.40, 0.38))
            shuf_overlaps_top = shuf_roi_cfg[1] < 0.05 and (banner_img is not None and banner_img.size > 0)
            if shuffle_img is not None and shuffle_img.size and shuf_due:
                shuf_arr = cv2.resize(_to_gray(shuffle_img), (32, 20), interpolation=cv2.INTER_AREA)
                prev_shuf_arr = getattr(self, "_last_shuffle_arr", None)
                shuf_unchanged = (
                    self._last_shuffle_sig is not None
                    and prev_shuf_arr is not None
                    and prev_shuf_arr.shape == shuf_arr.shape
                    and self._last_shuf_mode == shuf_mode
                    and float(np.mean(np.abs(shuf_arr.astype(np.int16) - prev_shuf_arr.astype(np.int16)))) < 2.2
                )
                if shuf_unchanged:
                    shuf_on, shuf_conf, shuf_ban_state, shuf_ban_conf = self._cached_shuf
                else:
                    self._last_shuffle_arr = shuf_arr
                    self._last_shuffle_sig = shuf_arr.tobytes()
                    self._last_shuf_mode = shuf_mode
                    self._last_shuffle_scan_ts = now
                    shuf_on = False
                    shuf_conf = 0.0
                    if self.hud_bank.shuffle_templates:
                        stpl, sconf = detect_best_single(shuffle_img, self.hud_bank.shuffle_templates, min_conf=0.78)
                        shuf_on = stpl is not None
                        shuf_conf = sconf
                    shuf_ban_state = ""
                    shuf_ban_conf = 0.0
                    if not top_banner_state and not shuf_overlaps_top and self.banner_bank.banner_templates:
                        shuf_tpls = (
                            [t for t in self.banner_bank.banner_templates if t.rank in ("SHUFFLE", "WAIT")]
                            if cards_present
                            else self.banner_bank.banner_templates
                        )
                        if shuf_tpls:
                            btpl, bconf = self._detect_banner_with_hint(
                                shuffle_img,
                                shuf_tpls,
                                self.cfg.min_banner_confidence,
                                _TOP_BANNER_SCALES,
                                hint_key="shuffle",
                            )
                            if btpl is not None:
                                shuf_ban_state = btpl.rank
                                shuf_ban_conf = bconf
                    self._cached_shuf = (shuf_on, shuf_conf, shuf_ban_state, shuf_ban_conf)
            else:
                shuf_on, shuf_conf, shuf_ban_state, shuf_ban_conf = self._cached_shuf

            banner_state = top_banner_state
            banner_conf = top_banner_conf
            banner_source = "banner_roi" if top_banner_state else ""
            if not banner_state and shuf_ban_state:
                if not cards_present or shuf_ban_state in ("SHUFFLE", "WAIT"):
                    banner_state = shuf_ban_state
                    banner_conf = shuf_ban_conf
                    banner_source = "shuffle_roi"

            # 3. Dedicated tight center place_bets_roi check (PLACE_BETS, NO_MORE_BETS, LAST_BETS)
            pb_conf = 0.0
            if has_sep_pb and not cards_present and self.banner_bank.banner_templates:
                pb_tpls = [
                    t
                    for t in self.banner_bank.banner_templates
                    if t.rank in ("PLACE_BETS", "NO_MORE_BETS", "LAST_BETS")
                ]
                if pb_tpls:
                    last_pb_ts = getattr(self, "_last_pb_scan_ts", -999.0)
                    pb_arr = cv2.resize(_to_gray(place_bets_img), (48, 16), interpolation=cv2.INTER_AREA)
                    prev_pb_arr = getattr(self, "_last_pb_arr", None)
                    pb_unchanged = (
                        self._last_pb_sig is not None
                        and prev_pb_arr is not None
                        and prev_pb_arr.shape == pb_arr.shape
                        and not self._cached_pb[1]
                        and float(np.mean(np.abs(pb_arr.astype(np.int16) - prev_pb_arr.astype(np.int16)))) < 2.2
                    )
                    if pb_unchanged or (self._last_pb_sig is not None and (now - last_pb_ts) < 0.32 and not self._cached_pb[1]):
                        pb_conf, _ = self._cached_pb
                        center_rank = getattr(self, "_cached_pb_rank", "PLACE_BETS" if pb_conf > 0 else "")
                    else:
                        self._last_pb_arr = pb_arr
                        self._last_pb_sig = pb_arr.tobytes()
                        self._last_pb_scan_ts = now
                        pb_min_conf = max(0.62, self.cfg.min_banner_confidence - 0.06)
                        ptpl, pconf = self._detect_banner_with_hint(
                            place_bets_img,
                            pb_tpls,
                            pb_min_conf,
                            _PLACE_BETS_SCALES,
                            hint_key="place_bets",
                        )
                        center_rank = ptpl.rank if ptpl is not None else ""
                        center_conf = pconf if ptpl is not None else 0.0
                        self._cached_pb_rank = center_rank
                        self._cached_pb_conf = center_conf
                        pb_conf = center_conf if center_rank == "PLACE_BETS" else 0.0
                        self._cached_pb = (pb_conf, False)
                    center_conf_val = getattr(self, "_cached_pb_conf", pb_conf)
                    if center_rank and center_conf_val > 0.0 and not banner_state:
                        banner_state = center_rank
                        banner_conf = center_conf_val
                        banner_source = "place_bets_roi"
            else:
                self._cached_pb = (0.0, cards_present)
                self._cached_pb_rank = ""
                self._cached_pb_conf = 0.0

            # 4. Fallback throttled center probe (only when top, shuffle, and place_bets_roi are empty)
            center_due = self.is_center_scan_due(now, cards_present)
            if not banner_state and not has_sep_pb and self.banner_bank.banner_templates and center_due and center_img is not None and center_img.size:
                c_sig = cv2.resize(_to_gray(center_img), (36, 18), interpolation=cv2.INTER_AREA).tobytes()
                if c_sig == self._last_center_sig and self._cached_center_banner[2] == cards_present:
                    c_st, c_cf, _ = self._cached_center_banner
                    banner_state, banner_conf = c_st, c_cf
                    if c_st:
                        banner_source = "center_probe"
                else:
                    self._last_center_sig = c_sig
                    self._last_center_scan_ts = now
                    allowed_center = ("SHUFFLE",) if cards_present else ("PLACE_BETS", "LAST_BETS", "NO_MORE_BETS", "SHUFFLE")
                    tall_tpls = [t for t in self.banner_bank.banner_templates if t.rank in allowed_center and t.gray.shape[0] >= 10]
                    thin_tpls = [t for t in self.banner_bank.banner_templates if t.rank in allowed_center and t.gray.shape[0] < 10]
                    c_st = ""
                    c_cf = 0.0
                    for group_tpls, min_c in (
                        (tall_tpls, max(self.cfg.min_banner_confidence, 0.78)),
                        (thin_tpls, max(self.cfg.min_banner_confidence, 0.78)),
                    ):
                        if not group_tpls:
                            continue
                        btpl, bconf = detect_best_single(
                            center_img,
                            group_tpls,
                            min_conf=min_c,
                            scales=_CENTER_BANNER_SCALES,
                            require_contrast=True,
                        )
                        if btpl is not None and bconf > c_cf:
                            c_st = btpl.rank
                            c_cf = bconf
                    self._cached_center_banner = (c_st, c_cf, cards_present)
                    if c_st:
                        banner_state = c_st
                        banner_conf = c_cf
                        banner_source = "center_probe"
            elif not banner_state and not has_sep_pb:
                c_st, c_cf, _ = self._cached_center_banner
                if c_st and (not cards_present or c_st == "SHUFFLE"):
                    banner_state, banner_conf = c_st, c_cf
                    banner_source = "center_probe"

            if banner_state == "SHUFFLE":
                shuf_on = True
                shuf_conf = max(shuf_conf, banner_conf)

            self._cached_b = (shuf_on, shuf_conf, banner_state, banner_conf, pb_conf, banner_source)
        else:
            shuf_on, shuf_conf, banner_state, banner_conf, pb_conf, banner_source = self._cached_b

        felt_events: list[FeltObservation] = []
        if self.cfg.enable_felt_community and community_img is not None and community_img.size:
            obs = self.felt_monitor.step(community_img, ts=now)
            if obs is not None:
                felt_events.append(obs)
        if (
            (self.cfg.enable_felt_community or self.cfg.enable_dealer_felt_fallback)
            and dealer_felt_img is not None
            and dealer_felt_img.size
        ):
            d_obs = self.dealer_felt_monitor.step(dealer_felt_img, ts=now)
            if d_obs is not None:
                felt_events.append(d_obs)

        # Dual-ROI physical shoe & movable discard-tray metrics (throttled ~2.5 Hz)
        shoe_state, shoe_dark, shoe_teal = getattr(self, "_cached_shoe", ("", 0.0, 0.0))
        disc_white, disc_dark, disc_teal = getattr(self, "_cached_discard", (0.0, 0.0, 0.0))
        last_shoe_ts = getattr(self, "_last_shoe_scan_ts", -999.0)
        if (shoe_img is not None or discard_img is not None) and (
            force_dealer_scan or (now - last_shoe_ts) >= 0.35
        ):
            self._last_shoe_scan_ts = now
            if shoe_img is not None and shoe_img.size:
                if use_shoe_baseline:
                    shoe_state, shoe_dark, shoe_teal = self._classify_shoe_with_baseline(shoe_img, now)
                else:
                    shoe_state, shoe_dark, shoe_teal = detect_shoe_state(shoe_img)
                self._cached_shoe = (shoe_state, shoe_dark, shoe_teal)
            if discard_img is not None and discard_img.size:
                disc_white, disc_dark, disc_teal = detect_discard_stack(discard_img)
                self._cached_discard = (disc_white, disc_dark, disc_teal)

        return FrameObservation(
            ts=now,
            player_cards=list(p_cards),
            dealer_cards=list(d_cards),
            player_hands={k: list(v) for k, v in p_hands.items()},
            felt_events=felt_events,
            banner_state=banner_state,
            banner_conf=banner_conf,
            shuffle_active=shuf_on,
            shuffle_conf=shuf_conf,
            split_detected=split_detected,
            hud_refreshed=hud_refreshed,
            dealer_only=dealer_only,
            place_bets_conf=pb_conf,
            banner_source=banner_source,
            dealer_badge_total=d_badge_total,
            dealer_badge_conf=d_badge_conf,
            dealer_sub_cards=list(d_sub_cards),
            dealer_badge_hint=d_badge_hint,
            shoe_state=shoe_state,
            shoe_dark_frac=shoe_dark,
            shoe_teal_frac=shoe_teal,
            discard_white_frac=disc_white,
            discard_dark_frac=disc_dark,
            discard_teal_frac=disc_teal,
        )

    def reset_shoe_baseline(self) -> None:
        """Clear the live resting-shoe reference (called on new_shoe or ROI recalibration)."""
        self._shoe_ref_gray: np.ndarray | None = None
        self._shoe_ref_dark: float = 0.0
        self._shoe_ref_felt: float = 0.0
        self._shoe_ref_bot_dark: float = 0.0
        self._shoe_ref_bot_felt: float = 0.0
        self._shoe_prev_gray: np.ndarray | None = None
        self._shoe_still_since: float | None = None
        self._shoe_new_present_since: float | None = None

    def _classify_shoe_with_baseline(
        self, shoe_img: np.ndarray, now: float
    ) -> tuple[str, float, float]:
        """Classify shoe_roi using both stateless template/HSV cues and a live resting-shoe NCC baseline."""
        st, dark_frac, felt_frac = detect_shoe_state(shoe_img)
        cur_gray = _shoe_gray_48x36(shoe_img)
        hsv = cv2.cvtColor(shoe_img, cv2.COLOR_BGR2HSV)
        h_px = shoe_img.shape[0]
        bot = slice(int(h_px * 0.48), h_px)
        bot_dark = float(np.mean(hsv[bot, :, 2] < 58)) if h_px >= 4 else dark_frac
        bot_felt = float(np.mean(_felt_mask(hsv[bot, :, 0], hsv[bot, :, 1], hsv[bot, :, 2]))) if h_px >= 4 else felt_frac

        prev_gray = getattr(self, "_shoe_prev_gray", None)
        frame_ncc = _ncc_flat(cur_gray, prev_gray) if prev_gray is not None else 1.0
        self._shoe_prev_gray = cur_gray

        if frame_ncc >= 0.88:
            if getattr(self, "_shoe_still_since", None) is None:
                self._shoe_still_since = now
        else:
            self._shoe_still_since = None

        ref_gray = getattr(self, "_shoe_ref_gray", None)
        still_dur = (now - self._shoe_still_since) if self._shoe_still_since is not None else 0.0

        # Lock baseline once the shoe sits still in shoe_roi for >= 1.5s
        if ref_gray is None:
            if still_dur >= 1.5 and (st == "PRESENT" or (dark_frac >= 0.22 and st != "ABSENT")):
                self._shoe_ref_gray = cur_gray.copy()
                self._shoe_ref_dark = dark_frac
                self._shoe_ref_felt = felt_frac
                self._shoe_ref_bot_dark = bot_dark
                self._shoe_ref_bot_felt = bot_felt
                st = "PRESENT"
            return st, dark_frac, felt_frac

        ref_ncc = _ncc_flat(cur_gray, ref_gray)
        if ref_ncc >= 0.72:
            self._shoe_new_present_since = None
            if frame_ncc >= 0.92:
                self._shoe_ref_gray = 0.98 * ref_gray + 0.02 * cur_gray
                self._shoe_ref_dark = 0.98 * self._shoe_ref_dark + 0.02 * dark_frac
                self._shoe_ref_felt = 0.98 * self._shoe_ref_felt + 0.02 * felt_frac
                self._shoe_ref_bot_dark = 0.98 * self._shoe_ref_bot_dark + 0.02 * bot_dark
                self._shoe_ref_bot_felt = 0.98 * self._shoe_ref_bot_felt + 0.02 * bot_felt
            return "PRESENT", dark_frac, felt_frac

        if ref_ncc < 0.45:
            # Genuine shoe removal: stateless ABSENT, OR felt rose AND dark dropped significantly
            full_emptied = (felt_frac >= self._shoe_ref_felt + 0.18) and (dark_frac <= self._shoe_ref_dark - 0.10)
            bot_emptied = (bot_felt >= self._shoe_ref_bot_felt + 0.22) and (bot_dark <= self._shoe_ref_bot_dark - 0.18)
            if st == "ABSENT" or full_emptied or bot_emptied:
                self._shoe_new_present_since = None
                return "ABSENT", dark_frac, felt_frac
            # New shoe placed back down (or table switched) at a different angle/position -> PRESENT & auto-relock
            if st == "PRESENT" and frame_ncc >= 0.88:
                if getattr(self, "_shoe_new_present_since", None) is None:
                    self._shoe_new_present_since = now
                elif (now - self._shoe_new_present_since) >= 1.0:
                    if (now - self._shoe_new_present_since) >= 2.0:
                        self._shoe_ref_gray = cur_gray.copy()
                        self._shoe_ref_dark = dark_frac
                        self._shoe_ref_felt = felt_frac
                        self._shoe_ref_bot_dark = bot_dark
                        self._shoe_ref_bot_felt = bot_felt
                        self._shoe_new_present_since = None
                    return "PRESENT", dark_frac, felt_frac
            else:
                self._shoe_new_present_since = None
            return "OCCLUDED", dark_frac, felt_frac

        self._shoe_new_present_since = None
        return st, dark_frac, felt_frac


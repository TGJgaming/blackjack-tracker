"""Unit tests for dual-ROI physical shoe & movable discard-tray auto-new-shoe detection."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

COMP_DIR = Path(__file__).resolve().parents[1]
if str(COMP_DIR) not in sys.path:
    sys.path.insert(0, str(COMP_DIR))

from config import CompanionConfig, ShoeDetectConfig
from detector import FrameObservation, detect_discard_stack, detect_shoe_state
from state_machine import CompanionStateMachine, ShoeSwapTracker


def _make_teal_patch(w: int = 80, h: int = 60) -> np.ndarray:
    """Create a synthetic Playtech teal felt patch (HSV ~ H=96, S=170, V=125)."""
    hsv = np.zeros((h, w, 3), dtype=np.uint8)
    hsv[:, :, 0] = 96
    hsv[:, :, 1] = 170
    hsv[:, :, 2] = 125
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _make_shoe_present_patch(w: int = 80, h: int = 60) -> np.ndarray:
    img = _make_teal_patch(w, h)
    # Large black dealing shoe body occupying ~65% of the ROI
    img[8:54, 10:70] = (18, 18, 22)
    return img


def _make_discard_patch(
    *,
    tray_x: int = 20,
    with_stack: bool = False,
    housing_visible: bool = True,
    w: int = 120,
    h: int = 80,
) -> np.ndarray:
    """Create a synthetic wide discard_roi with movable tray at `tray_x`."""
    img = _make_teal_patch(w, h)
    if housing_visible:
        # Dark black/acrylic discard tray housing
        img[14:66, tray_x : tray_x + 34] = (20, 22, 26)
        if with_stack:
            # Bright neutral-white card-stack side edge inside the tray
            img[24:60, tray_x + 5 : tray_x + 29] = (232, 232, 235)
    return img


def _obs(
    ts: float,
    *,
    shoe_state: str = "PRESENT",
    shoe_dark: float = 0.65,
    shoe_teal: float = 0.25,
    disc_white: float = 0.0,
    disc_dark: float = 0.30,
    disc_teal: float = 0.45,
    banner: str = "",
) -> FrameObservation:
    return FrameObservation(
        ts=ts,
        banner_state=banner,
        shoe_state=shoe_state,
        shoe_dark_frac=shoe_dark,
        shoe_teal_frac=shoe_teal,
        discard_white_frac=disc_white,
        discard_dark_frac=disc_dark,
        discard_teal_frac=disc_teal,
    )


def test_detect_shoe_state_present_absent_and_occluded() -> None:
    st_p, d_p, t_p = detect_shoe_state(_make_shoe_present_patch())
    assert st_p == "PRESENT" and d_p >= 0.45 and t_p <= 0.45

    st_a, d_a, t_a = detect_shoe_state(_make_teal_patch())
    assert st_a == "ABSENT" and d_a <= 0.05 and t_a >= 0.90

    # Dealer skin/arm occlusion (no teal felt) -> OCCLUDED, never ABSENT
    arm = np.full((60, 80, 3), (120, 150, 195), dtype=np.uint8)
    st_o, _, t_o = detect_shoe_state(arm)
    assert st_o == "OCCLUDED" and t_o < 0.05


def test_detect_discard_stack_position_invariant() -> None:
    # Empty tray anywhere -> ~0 white stack, dark housing present
    w0, d0, t0 = detect_discard_stack(_make_discard_patch(tray_x=15, with_stack=False))
    assert w0 <= 0.005 and d0 >= 0.12 and t0 >= 0.40

    # Dealer slides tray left, center, or right -> consistent white card-stack area!
    vals = []
    for tx in (8, 42, 76):
        w, d, t = detect_discard_stack(_make_discard_patch(tray_x=tx, with_stack=True))
        assert w >= 0.05 and d >= 0.05 and t >= 0.40
        vals.append(w)
    assert max(vals) - min(vals) < 0.015


def test_primary_shoe_swap_cycle_and_undo_cooldown() -> None:
    cfg = ShoeDetectConfig(
        shoe_present_hold_sec=2.0,
        shoe_absent_hold_sec=3.5,
        shoe_return_hold_sec=1.5,
        shoe_min_cards=15,
        cooldown_sec=30.0,
    )
    tr = ShoeSwapTracker(cfg)

    # Confirm PRESENT (>= 2.0s)
    assert tr.step(_obs(0.0), cards_seen=25, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr.step(_obs(2.2), cards_seen=25, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None

    # Brief 1.5s absence (too short) -> no trigger
    assert tr.step(_obs(3.0, shoe_state="ABSENT", shoe_dark=0.05, shoe_teal=0.85), cards_seen=25, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr.step(_obs(4.5, shoe_state="PRESENT"), cards_seen=25, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None

    # Real swap: ABSENT >= 3.5s (with a brief 0.4s OCCLUDED arm flicker tolerated!) -> RETURNED >= 1.5s
    assert tr.step(_obs(10.0, shoe_state="ABSENT", shoe_dark=0.05, shoe_teal=0.85), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr.step(_obs(11.5, shoe_state="OCCLUDED", shoe_dark=0.10, shoe_teal=0.10), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr.step(_obs(11.9, shoe_state="ABSENT", shoe_dark=0.05, shoe_teal=0.85), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr.step(_obs(13.8, shoe_state="ABSENT", shoe_dark=0.05, shoe_teal=0.85), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr._shoe_swap_armed is True

    # Shoe returns and holds >= 1.5s -> triggers shoe_swap
    assert tr.step(_obs(15.0, shoe_state="PRESENT"), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    res = tr.step(_obs(16.6, shoe_state="PRESENT"), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False)
    assert res == "shoe_swap"

    # Undo sets 30s cooldown so immediate re-trigger is impossible
    tr.on_undo_new_shoe(17.0)
    assert tr.step(_obs(20.0, shoe_state="ABSENT", shoe_dark=0.05, shoe_teal=0.85), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr.step(_obs(25.0, shoe_state="ABSENT", shoe_dark=0.05, shoe_teal=0.85), cards_seen=40, has_active_cards=False, in_betting_overlay=False, suppressed=False) is None
    assert tr._shoe_swap_armed is False


def test_discard_fallback_and_tray_moved_off_zone_guard() -> None:
    cfg = ShoeDetectConfig(
        discard_peak_white_min=0.022,
        discard_empty_white_max=0.007,
        discard_empty_hold_sec=4.5,
        discard_housing_dark_min=0.10,
        discard_min_cards=45,
    )
    tr = ShoeSwapTracker(cfg)

    # Build up peak white card stack mid-shoe (5 samples for rolling median)
    for t in (1.0, 2.0, 3.0, 4.0, 5.0):
        tr.step(_obs(t, shoe_state="OCCLUDED", disc_white=0.045, disc_dark=0.30, disc_teal=0.45), cards_seen=90, has_active_cards=False, in_betting_overlay=False, suppressed=False)
    assert tr._discard_peak_white >= 0.04

    # Case A: Dealer slides tray COMPLETELY outside discard_roi (both white AND dark housing vanish!) -> MUST NOT trigger!
    for t in (10.0, 12.0, 14.0, 16.0, 18.0, 20.0):
        res = tr.step(_obs(t, shoe_state="OCCLUDED", disc_white=0.001, disc_dark=0.02, disc_teal=0.85), cards_seen=90, has_active_cards=False, in_betting_overlay=False, suppressed=False)
        assert res is None

    # Case B: Tray emptied IN-PLACE (white drops to 0.002 while dark tray housing remains visible >= 0.10) for >= 4.5s -> triggers discard_cleared!
    triggered = None
    for t in (25.0, 26.5, 28.0, 29.5, 31.0, 32.5):
        out = tr.step(_obs(t, shoe_state="OCCLUDED", disc_white=0.002, disc_dark=0.25, disc_teal=0.55), cards_seen=90, has_active_cards=False, in_betting_overlay=False, suppressed=False)
        if out:
            triggered = out
            break
    assert triggered == "discard_cleared"


def test_state_machine_emits_new_shoe_and_suppresses_in_keepalive_grace() -> None:
    cfg = CompanionConfig()
    sm = CompanionStateMachine(cfg)
    # Simulate 60 cards seen (356 remaining out of 416)
    sm.set_sync_state(play_mode="bet", shoe_rem=[30, 30, 30, 30, 30, 30, 30, 30, 30, 86])

    sm.step(_obs(0.0, shoe_state="PRESENT"))
    sm.step(_obs(2.5, shoe_state="PRESENT"))
    # During keepalive grace, ABSENT is completely suppressed
    sm.step(_obs(5.0, shoe_state="ABSENT", shoe_dark=0.04, shoe_teal=0.88), keepalive_grace=True)
    sm.step(_obs(10.0, shoe_state="ABSENT", shoe_dark=0.04, shoe_teal=0.88), keepalive_grace=True)
    assert sm.shoe_tracker._shoe_swap_armed is False

    # Outside grace: full cycle emits new_shoe even in bet mode (with hasPendingBet=False between rounds)
    sm.step(_obs(15.0, shoe_state="ABSENT", shoe_dark=0.04, shoe_teal=0.88))
    sm.step(_obs(19.0, shoe_state="ABSENT", shoe_dark=0.04, shoe_teal=0.88))
    sm.step(_obs(20.0, shoe_state="PRESENT"))
    evs = sm.step(_obs(22.0, shoe_state="PRESENT"))
    ns = [e for e in evs if e.get("type") == "new_shoe"]
    assert len(ns) == 1
    assert ns[0]["reason"] == "shoe_swap"
    assert ns[0]["cardsSeen"] == 60
    assert ns[0]["hasPendingBet"] is False

"""Automated unit and WebSocket bridge tests for Playtech All Bets companion."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from websockets.asyncio.client import connect as ws_connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import CompanionBridge
from calibrate import delete_rank_templates, list_template_counts, save_template_crop
from config import CompanionConfig, load_config, save_config
from detector import (
    CardDetection,
    FeltBurstMonitor,
    FeltObservation,
    FrameObservation,
    PlaytechDetector,
    TemplateBank,
    match_templates_nms,
)
from state_machine import CompanionStateMachine
from win_anchor import ViewportSmoother, WindowRect, detect_game_viewport_local, resolve_roi


def _make_card_glyph(label: str, size: tuple[int, int] = (24, 32)) -> np.ndarray:
    """Render a crisp synthetic grayscale card corner glyph."""
    w, h = size
    img = np.full((h, w), 245, dtype=np.uint8)
    cv2.rectangle(img, (0, 0), (w - 1, h - 1), 160, 1)
    cv2.putText(img, label, (2, h - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.58, 25, 2)
    return img


def test_resolve_roi_and_config_roundtrip(tmp_path: Path) -> None:
    win = WindowRect(hwnd=1, title="Live Games - Brave", left=10, top=40, width=960, height=1000)
    rect = resolve_roi(win, (0.5, 0.25, 0.25, 0.1))
    assert rect == {"left": 490, "top": 290, "width": 240, "height": 100}

    # Clamped inside client rect if oversized
    clamped = resolve_roi(win, (0.95, 0.95, 0.3, 0.3))
    assert clamped["left"] + clamped["width"] <= win.left + win.width
    assert clamped["top"] + clamped["height"] <= win.top + win.height

    cfg_path = tmp_path / "cfg.json"
    cfg = CompanionConfig()
    cfg.fps = 9.0
    save_config(cfg, cfg_path)
    reloaded = load_config(cfg_path)
    assert reloaded.fps == 9.0


def test_hud_template_matching_overlap_and_duplicates(tmp_path: Path) -> None:
    hud_dir = tmp_path / "templates" / "hud"
    hud_dir.mkdir(parents=True)
    for tok in ("3", "7", "8", "K", "BACK"):
        cv2.imwrite(str(hud_dir / f"{tok}_1.png"), _make_card_glyph(tok if tok != "BACK" else "X"))

    bank = TemplateBank(hud_dir)
    # Build overlapping horizontal card strip: 7, 7, 3 with 28px stride
    strip = np.full((44, 180), 30, dtype=np.uint8)
    for idx, tok in enumerate(("7", "7", "3")):
        glyph = _make_card_glyph(tok)
        x0 = 12 + idx * 28
        strip[6:38, x0 : x0 + 24] = glyph

    dets = match_templates_nms(strip, bank.card_templates, threshold=0.80, min_dx=18)
    assert [d.rank for d in dets] == ["7", "7", "3"]
    assert [d.val for d in dets] == [7, 7, 3]


def test_felt_burst_monitor_sharpest_frame_and_missed_pulse(tmp_path: Path) -> None:
    felt_dir = tmp_path / "templates" / "felt"
    felt_dir.mkdir(parents=True)
    glyph_k = _make_card_glyph("K", size=(36, 48))
    cv2.imwrite(str(felt_dir / "K_1.png"), glyph_k)

    cfg = CompanionConfig()
    cfg.min_felt_confidence = 0.72
    cfg.felt_motion.min_contour_area_ratio = 0.04
    cfg.felt_motion.pulse_cooldown_sec = 0.1

    bank = TemplateBank(felt_dir)
    mon = FeltBurstMonitor(cfg, bank)

    bg = np.full((100, 140, 3), (85, 95, 25), dtype=np.uint8)  # dark teal felt
    # Frame 1: empty
    assert mon.step(bg, ts=1.0) is None

    # Frame 2: blurry white card appears (rotated 10 deg + blurred)
    card_frame = bg.copy()
    patch_bgr = cv2.cvtColor(_make_card_glyph("K", size=(36, 48)), cv2.COLOR_GRAY2BGR)
    card_frame[20:68, 45:81] = patch_bgr
    blur_frame = cv2.GaussianBlur(card_frame, (11, 11), 4.0)
    assert mon.step(blur_frame, ts=1.05) is None

    # Frame 3: sharp rotated (+10 deg) frame
    mat = cv2.getRotationMatrix2D((18, 24), 10.0, 1.0)
    rot_patch = cv2.warpAffine(patch_bgr, mat, (36, 48), borderMode=cv2.BORDER_REPLICATE)
    sharp_frame = bg.copy()
    sharp_frame[20:68, 45:81] = rot_patch
    assert mon.step(sharp_frame, ts=1.10) is None

    # Frame 4: card disappears -> should finalize using sharpest frame and match 'K'
    obs = mon.step(bg, ts=1.20)
    assert obs is not None
    assert obs.matched is True
    assert obs.rank == "K"
    assert obs.val == 0

    # Now simulate an unreadable white blob (occlusion/glare -> no matching template)
    blob = bg.copy()
    cv2.rectangle(blob, (40, 20), (88, 72), (240, 240, 240), -1)
    assert mon.step(blob, ts=2.0) is None
    missed = mon.step(bg, ts=2.1)
    assert missed is not None
    assert missed.matched is False


def _det(rank: str, val: int | None, x: int, conf: float = 0.95) -> CardDetection:
    return CardDetection(rank=rank, val=val, conf=conf, x=x)


def _feed_hud(
    sm: CompanionStateMachine,
    p: list[CardDetection],
    d: list[CardDetection],
    ts: float,
    frames: int = 3,
    felt: list[FeltObservation] | None = None,
    shuffle: bool = False,
    banner: str = "",
) -> list[dict]:
    out = []
    for k in range(frames):
        obs = FrameObservation(
            ts=ts + k * 0.14,
            player_cards=p,
            dealer_cards=d,
            felt_events=felt if k == 0 and felt else [],
            shuffle_active=shuffle,
            banner_state=banner,
            hud_refreshed=True,
        )
        out.extend(sm.step(obs))
    return out


def test_state_machine_watch_dedup_and_autolog() -> None:
    cfg = CompanionConfig(confirm_frames=3, clear_frames=4, hud_dedup_window_sec=2.8)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch", is_split=False)

    # Initial deal under WAIT banner: P=[7,8], D=[7, BACK]
    evs = _feed_hud(
        sm,
        [_det("7", 7, 10), _det("8", 8, 40)],
        [_det("7", 7, 10), _det("BACK", None, 40)],
        ts=10.0,
        banner="WAIT",
    )
    assert [(e["target"], e["val"]) for e in evs if e["type"] == "card"] == [("P", 7), ("P", 8), ("D", 7)]

    # Community felt echoes same hit '3' seen cleanly on Player HUD within window -> zero double-count
    evs = _feed_hud(
        sm,
        [_det("7", 7, 10), _det("8", 8, 40), _det("3", 3, 70)],
        [_det("7", 7, 10), _det("BACK", None, 40)],
        ts=11.0,
        felt=[FeltObservation(ts=11.0, matched=True, rank="3", val=3, conf=0.88, sharpness=200.0)],
        banner="WAIT",
    )
    assert [(e["target"], e["val"]) for e in evs if e["type"] == "card"] == [("P", 3)]

    # Dealer flips hole card BACK -> K (10 -> val 0)
    evs = _feed_hud(
        sm,
        [_det("7", 7, 10), _det("8", 8, 40), _det("3", 3, 70)],
        [_det("7", 7, 10), _det("K", 0, 40)],
        ts=13.0,
        banner="WAIT",
    )
    assert [(e["target"], e["val"]) for e in evs if e["type"] == "card"] == [("D", 0)]

    # Table clears -> auto_watch is staged for review, held during PLACE_BETS, emitted at NO_MORE_BETS
    evs = _feed_hud(sm, [], [], ts=15.0, frames=4, banner="WAIT")
    assert evs == []
    evs_pb = sm.step(FrameObservation(ts=16.0, banner_state="PLACE_BETS"))
    assert [e["type"] for e in evs_pb] == ["phase"]
    evs_nmb = sm.step(FrameObservation(ts=17.0, banner_state="NO_MORE_BETS"))
    assert [e["type"] for e in evs_nmb] == ["auto_watch", "phase"]


def test_state_machine_hud_glitch_recovers_into_community_extra() -> None:
    cfg = CompanionConfig(confirm_frames=2, clear_frames=3)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="bet", track_felt=False)

    _feed_hud(sm, [_det("9", 9, 10), _det("2", 2, 40)], [_det("6", 6, 10), _det("BACK", None, 40)], ts=20.0, frames=2)
    # Player stood and toggled Community Extra [B] -> physical felt 'K' is armed and recorded cleanly!
    sm.set_sync_state(track_felt=True)
    sm.step(
        FrameObservation(
            ts=21.0,
            player_cards=[_det("9", 9, 10), _det("2", 2, 40)],
            dealer_cards=[_det("6", 6, 10), _det("BACK", None, 40)],
            felt_events=[FeltObservation(ts=21.0, matched=True, rank="K", val=0, conf=0.85, sharpness=190.0)],
            hud_refreshed=False,
        )
    )
    # Dealer reveals hole card -> armed felt 'K' flushes automatically to Community Extra [B]!
    evs = _feed_hud(sm, [_det("9", 9, 10), _det("2", 2, 40)], [_det("6", 6, 10), _det("A", 1, 40)], ts=23.5, frames=2)
    assert [(e["target"], e["val"]) for e in evs if e["type"] == "card"] == [("D", 1), ("B", 0)]

    evs = _feed_hud(sm, [], [], ts=25.0, frames=3)
    assert [e["type"] for e in evs] == ["prompt_confirm"]
    assert sm.felt_tracking_armed is False


def test_state_machine_post_stand_bet_pause_and_missed_felt_guard() -> None:
    cfg = CompanionConfig(confirm_frames=2, clear_frames=3, pause_on_unmatched_felt=True)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="bet", track_felt=False)

    _feed_hud(sm, [_det("10", 0, 10), _det("8", 8, 40)], [_det("5", 5, 10), _det("BACK", None, 40)], ts=30.0, frames=2)
    # Player stood on 18 and toggled Community Extra [B]; dealer deals physical card cleanly into community_roi -> B
    sm.set_sync_state(track_felt=True)
    sm.step(
        FrameObservation(
            ts=31.0,
            player_cards=[_det("10", 0, 10), _det("8", 8, 40)],
            dealer_cards=[_det("5", 5, 10), _det("BACK", None, 40)],
            felt_events=[FeltObservation(ts=31.0, matched=True, rank="4", val=4, conf=0.84, sharpness=210.0)],
            hud_refreshed=False,
        )
    )
    # And another physical card is blurred/occluded -> unmatched failed felt pulse when pause_on_unmatched_felt=True
    sm.step(
        FrameObservation(
            ts=31.8,
            player_cards=[_det("10", 0, 10), _det("8", 8, 40)],
            dealer_cards=[_det("5", 5, 10), _det("BACK", None, 40)],
            felt_events=[FeltObservation(ts=31.8, matched=False, rank=None, val=None, conf=0.31, sharpness=45.0)],
            hud_refreshed=False,
        )
    )
    evs_flip = _feed_hud(sm, [_det("10", 0, 10), _det("8", 8, 40)], [_det("5", 5, 10), _det("7", 7, 40)], ts=33.0, frames=2)
    assert ("B", 4) in [(e["target"], e["val"]) for e in evs_flip if e["type"] == "card"]

    evs_end = _feed_hud(sm, [], [], ts=35.0, frames=3)
    prompts = [e for e in evs_end if e["type"] == "prompt_confirm"]
    assert len(prompts) == 1
    assert prompts[0]["reason"] == "BET_MODE"
    assert prompts[0]["unreadCommunity"] == 1
    assert "MISSED_COMMUNITY_CARD" in prompts[0]["uncertainSlots"]


def test_state_machine_split_hud_glitch_and_shuffle() -> None:
    cfg = CompanionConfig(confirm_frames=2, clear_frames=2, shuffle_hold_sec=1.5)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch", is_split=True)

    _feed_hud(sm, [_det("8", 8, 10), _det("8", 8, 40)], [_det("6", 6, 10), _det("Q", 0, 40)], ts=40.0, frames=2, banner="WAIT")
    evs_end = _feed_hud(sm, [], [], ts=41.0, frames=2, banner="WAIT")
    prompts_split = [e for e in evs_end if e["type"] == "prompt_confirm"]
    assert len(prompts_split) == 1
    assert prompts_split[0]["reason"] == "SPLIT"

    # Reset split and test incomplete initial HUD (< 2 cards)
    sm.set_sync_state(play_mode="watch", is_split=False)
    _feed_hud(sm, [_det("9", 9, 10)], [_det("4", 4, 10), _det("K", 0, 40)], ts=45.0, frames=2, banner="WAIT")
    evs_glitch = _feed_hud(sm, [], [], ts=46.0, frames=2, banner="WAIT")
    assert evs_glitch[0]["type"] == "prompt_confirm"
    assert evs_glitch[0]["reason"] == "HUD_GLITCH"

    # Shuffle hold gating (< 1.5s ignored, >= 1.5s emits new_shoe once)
    assert sm.step(FrameObservation(ts=50.0, shuffle_active=True)) == []
    assert sm.step(FrameObservation(ts=51.0, shuffle_active=True)) == []
    evs_shuf = sm.step(FrameObservation(ts=51.6, shuffle_active=True))
    assert evs_shuf == [{"type": "new_shoe", "reason": "cut", "hasPendingBet": False}]
    assert sm.step(FrameObservation(ts=52.0, shuffle_active=True)) == []


def test_state_machine_banner_transitions_and_betting_lockout() -> None:
    cfg = CompanionConfig(confirm_frames=2, clear_frames=3)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    # 1. PLACE_BETS, LAST_BETS, NO_MORE_BETS, and blank "" in watch mode MUST NOT record cards
    for idx, b_state in enumerate(["PLACE_BETS", "LAST_BETS", "NO_MORE_BETS"]):
        evs_bet = sm.step(
            FrameObservation(
                ts=60.0 + idx,
                banner_state=b_state,
                player_cards=[_det("10", 0, 10), _det("8", 8, 40)],
                dealer_cards=[_det("A", 1, 10)],
            )
        )
        assert [e for e in evs_bet if e["type"] != "phase"] == []
        assert any(e["type"] == "phase" and e["phase"] == b_state for e in evs_bet)
        assert sm.state == "BETTING"

    # Blank banner between NO_MORE_BETS and WAIT still blocks stray cards in watch mode
    evs_blank = sm.step(
        FrameObservation(
            ts=63.5,
            banner_state="",
            player_cards=[_det("10", 0, 10), _det("8", 8, 40)],
            dealer_cards=[_det("6", 6, 10)],
        )
    )
    assert evs_blank == []
    assert sm.state == "BETTING"

    # 2. WAIT FOR THE NEXT ROUND appears -> dealing/recording enabled cleanly (including 0.78 conf without pausing)
    evs_deal = _feed_hud(
        sm,
        [_det("10", 0, 10, conf=0.78), _det("8", 8, 40, conf=0.80)],
        [_det("7", 7, 10), _det("K", 0, 40)],
        ts=65.0,
        frames=2,
        banner="WAIT",
    )
    card_evs = [e for e in evs_deal if e["type"] == "card"]
    assert len(card_evs) == 4
    assert sm.state in ("PLAYER_TURN", "DEALER_TURN")

    # 3. Next round begins with PLACE_BETS -> holds auto_watch for review, holds through LAST_BETS, emits at NO_MORE_BETS
    evs_next = sm.step(FrameObservation(ts=68.0, banner_state="PLACE_BETS"))
    assert [e["type"] for e in evs_next] == ["phase"]
    assert evs_next[0]["phase"] == "PLACE_BETS"
    assert sm.state == "BETTING"

    evs_lb = sm.step(FrameObservation(ts=69.0, banner_state="LAST_BETS"))
    assert [e["type"] for e in evs_lb] == ["phase"]
    assert evs_lb[0]["phase"] == "LAST_BETS"

    evs_nmb = sm.step(FrameObservation(ts=70.0, banner_state="NO_MORE_BETS"))
    auto_watch_evs = [e for e in evs_nmb if e["type"] == "auto_watch"]
    assert len(auto_watch_evs) == 1
    assert auto_watch_evs[0]["summary"] == "2P+2D+0B"
    assert sm.state == "BETTING"

    # 4. Subsequent NO_MORE_BETS frame does not re-emit
    assert sm.step(FrameObservation(ts=70.5, banner_state="NO_MORE_BETS")) == []


def test_websocket_bridge_roundtrip() -> None:
    async def _run() -> None:
        inbound: list[dict] = []
        bridge = CompanionBridge("127.0.0.1", 0, on_message=inbound.append, on_connect=lambda: [{"type": "status", "state": "IDLE"}])
        await bridge.start()
        try:
            sock = next(iter(bridge._server.sockets))
            port = sock.getsockname()[1]
            async with ws_connect(f"ws://127.0.0.1:{port}", origin="null") as client:
                hello = json.loads(await client.recv())
                assert hello["type"] == "status"

                await client.send(json.dumps({"type": "sync_state", "playMode": "watch", "isSplit": False, "draftCount": 0}))
                await bridge.broadcast({"type": "card", "target": "P", "val": 7, "rankLabel": "7", "conf": 0.95})
                ev = json.loads(await client.recv())
                assert ev["type"] == "card" and ev["val"] == 7

                await asyncio.sleep(0.05)
                assert inbound and inbound[0]["type"] == "sync_state"
        finally:
            await bridge.stop()

    asyncio.run(_run())


def test_viewport_detection_hysteresis_and_template_store(tmp_path: Path) -> None:
    # Build synthetic Brave-like frame: top chrome, dark letterbox, left sidebar, Playtech viewport
    win_img = np.full((600, 500, 3), (10, 10, 12), dtype=np.uint8)
    win_img[:85, :] = (45, 45, 48)  # Brave tabs + URL bar
    win_img[85:, :26] = (40, 40, 44)  # Brave left sidebar
    # Actual Playtech game frame: y=180..480, x=26..500
    win_img[180:480, 26:500] = (90, 110, 45)

    vx, vy, vw, vh = detect_game_viewport_local(win_img)
    assert 22 <= vx <= 30
    assert 176 <= vy <= 184
    assert 465 <= vw <= 480
    assert 295 <= vh <= 305

    smoother = ViewportSmoother(tol_px=8)
    win = WindowRect(hwnd=1, title="Live Games - Brave", left=0, top=0, width=500, height=600)
    a1, l1 = smoother.resolve_anchor(win, win_img, "game_viewport")
    # Small 3px nudge stays locked cleanly via hysteresis
    shifted = win_img.copy()
    shifted[177:180, 26:500] = (90, 110, 45)
    a2, l2 = smoother.resolve_anchor(win, shifted, "game_viewport")
    assert a1 == a2 and l1 == l2

    # Template save/list/delete round-trip
    troot = tmp_path / "templates"
    out = save_template_crop("hud", "k", _make_card_glyph("K"), templates_root=troot)
    assert out.name == "K_1.png"
    assert list_template_counts(troot)["hud"] == {"K": 1}
    assert delete_rank_templates("hud", "K", templates_root=troot) == 1
    assert list_template_counts(troot)["hud"] == {}

    out_b = save_template_crop("banner", "WAIT", _make_card_glyph("WAIT", size=(60, 18)), templates_root=troot)
    assert out_b.name == "WAIT_1.png"
    assert list_template_counts(troot)["banner"] == {"WAIT": 1}
    assert delete_rank_templates("banner", "WAIT", templates_root=troot) == 1
    assert list_template_counts(troot)["banner"] == {}



def test_normalized_hud_red_black_and_suit_classification(tmp_path) -> None:
    """Normalized H=45 HUD detector matches both red & black ranks across scales and classifies suits."""
    import cv2
    import numpy as np
    from calibrate import save_template_crop
    from config import CompanionConfig
    from detector import PlaytechDetector

    tdir = tmp_path / "templates"

    def make_strip(card_specs, h_card=45, w_card=22, gap_after=None, badge_left=False):
        pad_x = 14 if badge_left else 6
        extra_gap = 28 if gap_after is not None else 0
        total_w = pad_x + len(card_specs) * w_card + extra_gap + 8
        img = np.full((h_card + 12, total_w, 3), 24, dtype=np.uint8)
        y0 = 6
        if badge_left:
            cv2.circle(img, (pad_x - 2, y0 + round(h_card * 0.62)), max(4, round(h_card * 0.19)), (42, 42, 42), -1)
        x_cur = pad_x
        for i, (rk, suit, is_red) in enumerate(card_specs):
            if gap_after is not None and i == gap_after:
                x_cur += extra_gap
            cw = w_card - 2
            img[y0 : y0 + h_card, x_cur : x_cur + cw] = (248, 248, 248)
            color = (30, 30, 215) if is_red else (20, 20, 20)
            thick = max(1, round(h_card / 22))
            fscale = (h_card / 45.0) * 0.50
            cv2.putText(img, rk, (x_cur + 2, y0 + round(h_card * 0.36)), cv2.FONT_HERSHEY_SIMPLEX, fscale, color, thick, cv2.LINE_AA)
            cx = x_cur + cw // 2
            cy = y0 + round(h_card * 0.78)
            r = max(3, round(h_card * 0.14))
            if suit == "S":
                pts = np.array([[cx, cy - r], [cx - r, cy + r], [cx + r, cy + r]], np.int32)
                cv2.fillPoly(img, [pts], color)
            elif suit == "H":
                pts = np.array([[cx - r, cy - r // 2], [cx + r, cy - r // 2], [cx, cy + r]], np.int32)
                cv2.fillPoly(img, [pts], color)
            x_cur += w_card
        return img

    canon_7s = make_strip([("7", "S", False)], h_card=45, w_card=22)
    canon_qh = make_strip([("Q", "H", True)], h_card=45, w_card=22)
    save_template_crop("hud", "7", canon_7s, tdir, auto_trim=True)
    save_template_crop("hud", "Q", canon_qh, tdir, auto_trim=True)
    save_template_crop("suit", "S", canon_7s, tdir, auto_trim=True)
    save_template_crop("suit", "H", canon_qh, tdir, auto_trim=True)

    det = PlaytechDetector(CompanionConfig(), templates_root=tdir)
    # Test at ~0.64x scale WITH overlapping left dark score badge
    test_strip = make_strip([("7", "S", False), ("Q", "H", True)], h_card=29, w_card=15, badge_left=True)
    flat, hands, is_split = det._detect_hud_strip(test_strip, is_dealer=False)
    assert is_split is False
    cards = hands.get(0, [])
    assert [c.rank for c in cards] == ["7", "Q"]
    assert [c.suit for c in cards] == ["S", "H"]
    assert len(flat) == 2


def test_banner_contrast_gate_rejects_blank_dark_strip(tmp_path) -> None:
    """Empty dark banner strip does not false-trigger WAIT/PLACE_BETS via float noise."""
    import cv2
    import numpy as np
    from config import CompanionConfig
    from detector import PlaytechDetector

    tdir = tmp_path / "templates"
    bdir = tdir / "banner"
    bdir.mkdir(parents=True)
    tpl = np.full((18, 120), 18, dtype=np.uint8)
    cv2.putText(tpl, "WAIT", (12, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.50, 245, 1, cv2.LINE_AA)
    cv2.imwrite(str(bdir / "WAIT_1.png"), tpl)

    det = PlaytechDetector(CompanionConfig(), templates_root=tdir)
    empty_hud = np.full((60, 120, 3), 24, dtype=np.uint8)
    blank_dark = np.full((34, 240, 3), 18, dtype=np.uint8)
    obs_blank = det.process_crops(empty_hud, empty_hud, banner_img=blank_dark, ts=1.0)
    assert not obs_blank.banner_state

    real_wait = np.full((34, 240, 3), 18, dtype=np.uint8)
    cv2.putText(real_wait, "WAIT", (40, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (245, 245, 245), 1, cv2.LINE_AA)
    obs_wait = det.process_crops(empty_hud, empty_hud, banner_img=real_wait, ts=2.0)
    assert obs_wait.banner_state == "WAIT"


def test_horizontal_split_cluster_and_state_machine_no_double_count(tmp_path) -> None:
    """Horizontal split produces 2 distinct hands and auto-tracks community splits without double-counting."""
    import cv2
    import numpy as np
    from calibrate import save_template_crop
    from config import CompanionConfig
    from detector import CardDetection, FrameObservation, PlaytechDetector
    from state_machine import CompanionStateMachine

    tdir = tmp_path / "templates"

    def mk_card(rk, red=False):
        im = np.full((57, 30, 3), 24, dtype=np.uint8)
        im[6:51, 5:25] = (248, 248, 248)
        col = (30, 30, 215) if red else (20, 20, 20)
        cv2.putText(im, rk, (7, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, col, 2, cv2.LINE_AA)
        return im

    save_template_crop("hud", "8", mk_card("8"), tdir, auto_trim=True)
    save_template_crop("hud", "3", mk_card("3", True), tdir, auto_trim=True)
    save_template_crop("hud", "Q", mk_card("Q"), tdir, auto_trim=True)

    det = PlaytechDetector(CompanionConfig(), templates_root=tdir)
    split_strip = np.full((57, 150, 3), 24, dtype=np.uint8)
    # Cluster 0 (H0): 8, 3 adjacent white cards
    split_strip[6:51, 8:28] = (248, 248, 248)
    cv2.putText(split_strip, "8", (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2, cv2.LINE_AA)
    split_strip[6:51, 29:49] = (248, 248, 248)
    cv2.putText(split_strip, "3", (31, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (30, 30, 215), 2, cv2.LINE_AA)
    # Dark horizontal split gap (~26px) then Cluster 1 (H1): 8, Q
    split_strip[6:51, 75:95] = (248, 248, 248)
    cv2.putText(split_strip, "8", (77, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2, cv2.LINE_AA)
    split_strip[6:51, 96:116] = (248, 248, 248)
    cv2.putText(split_strip, "Q", (98, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2, cv2.LINE_AA)

    flat, hands, is_split = det._detect_hud_strip(split_strip, is_dealer=False)
    assert is_split is True
    assert [c.rank for c in hands[0]] == ["8", "3"]
    assert [c.rank for c in hands[1]] == ["8", "Q"]
    assert len(flat) == 4

    # State machine check: [8, 8] -> horizontal split into H0:[8,3] H1:[8,Q] -> NO double counting!
    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    for ts in (10.0, 10.5):
        sm.step(FrameObservation(
            ts=ts,
            banner_state="WAIT",
            player_cards=[CardDetection("8", 8, 0.95, 0, 0, 10, 20, suit="S", hand_index=0),
                          CardDetection("8", 8, 0.95, 12, 0, 10, 20, suit="C", hand_index=0)],
            player_hands={0: [CardDetection("8", 8, 0.95, 0, 0, 10, 20, suit="S", hand_index=0),
                              CardDetection("8", 8, 0.95, 12, 0, 10, 20, suit="C", hand_index=0)]},
            dealer_cards=[CardDetection("7", 7, 0.95, 0, 0, 10, 20, suit="H")],
        ))
    emitted_cards = []
    for ts in (12.0, 12.5):
        h0 = [CardDetection("8", 8, 0.95, 0, 0, 10, 20, suit="S", hand_index=0),
              CardDetection("3", 3, 0.95, 12, 0, 10, 20, suit="H", hand_index=0)]
        h1 = [CardDetection("8", 8, 0.95, 50, 0, 10, 20, suit="C", hand_index=1),
              CardDetection("Q", 0, 0.95, 62, 0, 10, 20, suit="D", hand_index=1)]
        evs = sm.step(FrameObservation(
            ts=ts,
            banner_state="WAIT",
            split_detected=True,
            player_cards=h0 + h1,
            player_hands={0: h0, 1: h1},
            dealer_cards=[CardDetection("7", 7, 0.95, 0, 0, 10, 20, suit="H"),
                          CardDetection("K", 0, 0.95, 12, 0, 10, 20, suit="S")],
        ))
        emitted_cards.extend(evs)

    split_evs = [e for e in emitted_cards if e["type"] == "split"]
    p_cards_after_split = [e for e in emitted_cards if e["type"] == "card" and e["target"] == "P"]
    assert len(split_evs) == 1 and split_evs[0]["handCount"] == 2
    assert [(e["handIndex"], e["rankLabel"], e["suit"]) for e in p_cards_after_split] == [
        (0, "3", "H"),
        (1, "Q", "D"),
    ]

    evs_done = sm.step(FrameObservation(ts=20.0, banner_state="PLACE_BETS"))
    assert [e["type"] for e in evs_done] == ["phase"]
    evs_nmb = sm.step(FrameObservation(ts=21.0, banner_state="NO_MORE_BETS"))
    aw = [e for e in evs_nmb if e["type"] == "auto_watch"]
    assert len(aw) == 1 and aw[0]["handCount"] == 2 and aw[0]["summary"] == "4P(2H)+2D+0B"


def test_calibrate_suit_bank_autotrim_and_clear_bank(tmp_path) -> None:
    """Calibrator saves canonical auto-trimmed suit pip and clear_bank_templates empties the bank."""
    import cv2
    import numpy as np
    from calibrate import clear_bank_templates, list_template_counts, save_template_crop

    img = np.full((48, 36, 3), 248, dtype=np.uint8)
    cv2.circle(img, (18, 24), 7, (20, 20, 20), -1)

    tdir = tmp_path / "templates"
    out_path = save_template_crop("suit", "S", img, tdir, auto_trim=True)
    assert out_path.name == "S_1.png"
    saved = cv2.imread(str(out_path))
    assert saved is not None and 10 <= saved.shape[0] <= 24 and 10 <= saved.shape[1] <= 24
    assert list_template_counts(tdir)["suit"]["S"] == 1

    removed = clear_bank_templates("suit", tdir)
    assert removed == 1
    assert list_template_counts(tdir)["suit"] == {}


def test_websocket_protocol_phase_split_suit_and_clear_bank(tmp_path, monkeypatch) -> None:
    """Preview rendering handles split hands/suits and companion state events match WS contract."""
    import json
    import cv2
    import numpy as np
    from companion import _draw_preview
    from config import CompanionConfig
    from detector import CardDetection, FrameObservation
    from state_machine import CompanionStateMachine

    monkeypatch.setattr(cv2, "imshow", lambda *a: None)
    monkeypatch.setattr(cv2, "waitKey", lambda *a: -1)

    cfg = CompanionConfig(confirm_frames=1, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    evs_p = sm.step(FrameObservation(ts=1.0, banner_state="PLACE_BETS"))
    assert json.loads(json.dumps(evs_p))[0]["type"] == "phase"

    h0 = [CardDetection("A", 1, 0.96, (10, 10, 12, 20), suit="H", hand_index=0)]
    h1 = [CardDetection("A", 1, 0.96, (60, 10, 12, 20), suit="S", hand_index=1)]
    obs = FrameObservation(
        ts=2.0,
        banner_state="WAIT",
        split_detected=True,
        player_cards=h0 + h1,
        player_hands={0: h0, 1: h1},
        dealer_cards=[CardDetection("K", 0, 0.95, 10, 10, 12, 20, suit="D")],
    )
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    res = _draw_preview(frame, (0, 0, 640, 360), cfg, obs, sm, 7.1)
    assert isinstance(res, bool)

    evs = sm.step(obs)
    assert any(e["type"] == "split" and e["handCount"] == 2 for e in evs)
    assert any(e["type"] == "card" and e.get("suit") == "S" and e.get("handIndex") == 1 for e in evs)


def test_wait_banner_trusts_felt_and_autologs_before_next_round() -> None:
    """Default config trusts matched community felt cards without pausing on unmatched motion noise and logs before next round."""
    from config import CompanionConfig
    from detector import FeltObservation, FrameObservation
    from state_machine import CompanionStateMachine

    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)
    assert cfg.require_wait_banner is True
    assert cfg.pause_on_unmatched_felt is False
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    # NO_MORE_BETS -> cards ignored
    sm.step(FrameObservation(ts=1.0, banner_state="NO_MORE_BETS", player_cards=[_det("4", 4, 10)]))
    assert sm._emitted_p == 0

    # WAIT FOR THE NEXT ROUND -> records 4, 4 + dealer 10 (upcard only, fast round)
    evs_deal = _feed_hud(
        sm,
        [_det("4", 4, 10, conf=0.78), _det("4", 4, 30, conf=0.79)],
        [_det("10", 0, 10, conf=0.84), _det("BACK", None, 35, conf=0.92)],
        ts=2.0,
        frames=2,
        banner="WAIT",
    )
    assert len([e for e in evs_deal if e["type"] == "card"]) == 3

    # In watch mode, felt pulses are suppressed -> zero bogus B cards and zero false pauses
    sm.step(FrameObservation(
        ts=3.0,
        banner_state="WAIT",
        player_cards=[_det("4", 4, 10), _det("4", 4, 30)],
        dealer_cards=[_det("10", 0, 10), _det("BACK", None, 35)],
        felt_events=[
            FeltObservation(ts=3.0, matched=True, rank="9", val=9, conf=0.76, sharpness=180.0),
            FeltObservation(ts=3.1, matched=False, rank=None, val=None, conf=0.25, sharpness=40.0),
        ],
        hud_refreshed=False,
    ))

    # WAIT disappears before next round begins (2 clear frames) -> completes [10, BACK] via educated guess, stages auto_watch, flushes on NO_MORE_BETS!
    assert sm.step(FrameObservation(ts=4.2, banner_state="", hud_refreshed=True)) == []
    evs_end = sm.step(FrameObservation(ts=4.4, banner_state="", hud_refreshed=True))
    assert ("B", 9) not in [(e["target"], e["val"]) for e in evs_end if e["type"] == "card"]
    assert any(e["type"] == "prompt_confirm" and e["reason"] == "DEALER_INCOMPLETE" for e in evs_end)
    assert not any(e["type"] == "auto_watch" for e in evs_end)
    evs_nmb = sm.step(FrameObservation(ts=5.0, banner_state="NO_MORE_BETS"))
    assert any(e["type"] == "auto_watch" and e["summary"] == "2P+2D+0B" for e in evs_nmb)

    # Roundtrip preserves new flags cleanly while keeping defaults backward-compatible
    parsed = CompanionConfig.from_dict({"rois": cfg.rois})
    assert parsed.require_wait_banner is True
    assert parsed.pause_on_unmatched_felt is False
    assert parsed.min_auto_confidence == 0.70


def test_bet_mode_blank_banner_starts_round_after_no_more_bets() -> None:
    """When playing a round (bet mode), blank banner after NO_MORE_BETS starts DEALING and records cards."""
    from config import CompanionConfig
    from detector import FrameObservation
    from state_machine import CompanionStateMachine

    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="bet")

    # Betting banners lock out cards and record _last_bet_banner
    sm.step(FrameObservation(ts=1.0, banner_state="PLACE_BETS", player_cards=[_det("7", 7, 10)]))
    sm.step(FrameObservation(ts=2.0, banner_state="NO_MORE_BETS", player_cards=[_det("7", 7, 10)]))
    assert sm.state == "BETTING"
    assert sm._emitted_p == 0

    # Banner goes blank after NO_MORE_BETS -> starts DEALING immediately and records cards on blank banner
    evs_start = sm.step(FrameObservation(ts=3.0, banner_state=""))
    assert sm.state == "DEALING"
    assert any(e["type"] == "phase" and e["phase"] == "DEALING" for e in evs_start)

    evs_deal = _feed_hud(
        sm,
        [_det("7", 7, 10), _det("8", 8, 30)],
        [_det("5", 5, 10), _det("BACK", None, 35)],
        ts=4.0,
        frames=2,
        banner="",
    )
    assert [(e["target"], e["rankLabel"]) for e in evs_deal if e["type"] == "card"] == [
        ("P", "7"),
        ("P", "8"),
        ("D", "5"),
    ]


def test_watch_mode_does_not_stay_stuck_in_idle_with_persistent_cards() -> None:
    """In watch mode, persistent HUD cards wake out of IDLE cleanly when WAIT is absent (e.g., played round / scaled banner miss)."""
    from config import CompanionConfig
    from detector import FrameObservation
    from state_machine import CompanionStateMachine

    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    p_cards = [_det("7", 7, 10), _det("8", 8, 30)]
    d_cards = [_det("5", 5, 10), _det("BACK", None, 35)]

    # Single blank frame stays guarded against stray 1-frame flickers
    evs1 = sm.step(FrameObservation(ts=1.0, banner_state="", player_cards=p_cards, dealer_cards=d_cards))
    assert evs1 == []
    assert sm.state == "IDLE"

    # Persistent real cards across confirm_frames wake companion from IDLE -> DEALING -> PLAYER_TURN and record cards
    evs_more = []
    for t in (1.2, 1.4):
        evs_more.extend(sm.step(FrameObservation(ts=t, banner_state="", player_cards=p_cards, dealer_cards=d_cards)))
    assert sm.state == "PLAYER_TURN"
    assert [(e["target"], e["rankLabel"]) for e in evs_more if e["type"] == "card"] == [
        ("P", "7"),
        ("P", "8"),
        ("D", "5"),
    ]


def test_dealer_last_card_fast_confirm_and_single_frame_wipe_rescue() -> None:
    """Dealer hole reveals/final hits confirm in <=2 frames and even 1-frame >=0.85 flashes rescue before wipe."""
    from config import CompanionConfig
    from detector import FrameObservation
    from state_machine import CompanionStateMachine

    cfg = CompanionConfig(confirm_frames=3, clear_frames=3)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    p_cards = [_det("10", 0, 10), _det("8", 8, 30)]
    _feed_hud(sm, p_cards, [_det("6", 6, 10), _det("BACK", None, 35)], ts=1.0, frames=3, banner="WAIT")

    # Dealer hole reveal '10' seen for only 2 frames (< confirm_frames=3) confirms via fast tail
    evs_rev = _feed_hud(sm, p_cards, [_det("6", 6, 10), _det("10", 0, 35)], ts=2.0, frames=2, banner="WAIT")
    assert [("D", "10")] == [(e["target"], e["rankLabel"]) for e in evs_rev if e["type"] == "card"]

    # Dealer final hit '5' (conf=0.90) seen on only 1 split-second frame right before HUD clears to []
    evs_flash = sm.step(
        FrameObservation(
            ts=3.0,
            banner_state="WAIT",
            player_cards=p_cards,
            dealer_cards=[_det("6", 6, 10), _det("10", 0, 35), _det("5", 5, 60)],
        )
    )
    assert [e for e in evs_flash if e["type"] == "card"] == []

    evs_wipe = _feed_hud(sm, [], [], ts=3.2, frames=3, banner="WAIT")
    card_evs = [(e["target"], e["rankLabel"]) for e in evs_wipe if e["type"] == "card"]
    assert ("D", "5") in card_evs
    assert not any(e["type"] == "auto_watch" for e in evs_wipe)
    evs_nmb = sm.step(FrameObservation(ts=4.0, banner_state="NO_MORE_BETS"))
    assert any(e["type"] == "auto_watch" and e["summary"] == "2P+3D+0B" for e in evs_nmb)


def test_community_felt_requires_bet_mode_and_toggled_extra_box() -> None:
    """Community felt cards are ignored in watch mode and before toggling [B], and tracked once (bet + track_felt)."""
    from config import CompanionConfig
    from detector import FeltObservation, FrameObservation
    from state_machine import CompanionStateMachine

    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)
    sm = CompanionStateMachine(cfg)

    # 1. Watch mode never arms felt tracking even if track_felt=True is passed
    sm.set_sync_state(play_mode="watch", track_felt=True)
    assert sm.felt_tracking_armed is False
    _feed_hud(sm, [_det("10", 0, 10), _det("9", 9, 30)], [_det("7", 7, 10), _det("BACK", None, 35)], ts=1.0, frames=2, banner="WAIT")
    sm.step(FrameObservation(
        ts=1.5,
        banner_state="WAIT",
        player_cards=[_det("10", 0, 10), _det("9", 9, 30)],
        dealer_cards=[_det("7", 7, 10), _det("BACK", None, 35)],
        felt_events=[FeltObservation(ts=1.5, matched=True, rank="K", val=0, conf=0.90, sharpness=200.0)],
        hud_refreshed=False,
    ))
    evs_watch_flip = _feed_hud(sm, [_det("10", 0, 10), _det("9", 9, 30)], [_det("7", 7, 10), _det("10", 0, 35)], ts=2.0, frames=2, banner="WAIT")
    assert not any(e.get("target") == "B" for e in evs_watch_flip)
    _feed_hud(sm, [], [], ts=2.5, frames=2, banner="WAIT")

    # 2. Bet mode BEFORE user toggles [B] (e.g. still hitting) ignores felt motion/cards
    sm.set_sync_state(play_mode="bet", track_felt=False)
    assert sm.felt_tracking_armed is False
    _feed_hud(sm, [_det("6", 6, 10), _det("5", 5, 30)], [_det("4", 4, 10), _det("BACK", None, 35)], ts=5.0, frames=2)
    sm.step(FrameObservation(
        ts=5.5,
        player_cards=[_det("6", 6, 10), _det("5", 5, 30)],
        dealer_cards=[_det("4", 4, 10), _det("BACK", None, 35)],
        felt_events=[FeltObservation(ts=5.5, matched=True, rank="Q", val=0, conf=0.90, sharpness=200.0)],
        hud_refreshed=False,
    ))

    # 3. User stands and specifically toggles Community Extra [B] -> now armed, ignores pre-toggle 'Q', records '8'
    sm.set_sync_state(play_mode="bet", track_felt=True)
    assert sm.felt_tracking_armed is True
    sm.step(FrameObservation(
        ts=6.0,
        player_cards=[_det("6", 6, 10), _det("5", 5, 30)],
        dealer_cards=[_det("4", 4, 10), _det("BACK", None, 35)],
        felt_events=[FeltObservation(ts=6.0, matched=True, rank="8", val=8, conf=0.88, sharpness=210.0)],
        hud_refreshed=False,
    ))
    evs_bet_flip = _feed_hud(sm, [_det("6", 6, 10), _det("5", 5, 30)], [_det("4", 4, 10), _det("10", 0, 35)], ts=6.5, frames=2)
    b_cards = [(e["target"], e["rankLabel"]) for e in evs_bet_flip if e["type"] == "card" and e["target"] == "B"]
    assert b_cards == [("B", "8")]

    # Round clear automatically disarms felt tracking for the next round
    _feed_hud(sm, [], [], ts=7.0, frames=2)
    assert sm.felt_tracking_armed is False


def test_viewport_preserves_top_banner_and_detector_ignores_center_false_bets() -> None:
    """Viewport top edge never clips thin WAIT header text, and center betting probes never override active HUD cards."""
    import cv2
    import numpy as np
    from config import CompanionConfig
    from detector import PlaytechDetector
    from win_anchor import detect_game_viewport_local

    # Synthetic browser frame: dark webpage void (y<180), thin white top text at y=182..190 inside black bar, bright table below y=191
    frame = np.full((600, 640, 3), 17, dtype=np.uint8)
    frame[180:560, 40:620] = (12, 12, 12)
    frame[191:550, 40:620] = (95, 115, 70)
    # Thin bright WAIT-like pixels at y=182..189 (<14% row density on top rows)
    frame[182:189, 275:335] = (245, 245, 245)

    _, vy, _, vh = detect_game_viewport_local(frame)
    assert vy <= 182
    assert vy + vh >= 540

    # Active HUD card present + noisy center_img: detector must NOT emit PLACE_BETS/LAST_BETS/NO_MORE_BETS
    cfg = CompanionConfig()
    det = PlaytechDetector(cfg)
    p_strip = np.full((60, 220, 3), 24, dtype=np.uint8)
    p_strip[8:52, 40:72] = (248, 248, 248)
    cv2.putText(p_strip, "8", (44, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2, cv2.LINE_AA)
    d_strip = np.zeros((45, 120, 3), dtype=np.uint8)
    # Noisy lace-like center crop without banner
    center_noise = np.zeros((120, 180, 3), dtype=np.uint8)
    center_noise[48:55, 8:58] = (200, 200, 200)

    obs = det.process_crops(
        player_img=p_strip,
        dealer_img=d_strip,
        community_img=None,
        shuffle_img=np.zeros((40, 220, 3), dtype=np.uint8),
        banner_img=np.zeros((30, 180, 3), dtype=np.uint8),
        center_img=center_noise,
        ts=1.0,
        hud_refreshed=True,
    )
    assert obs.banner_state == ""


def test_dealer_consecutive_single_frame_reveals_and_partial_wipe_rescue() -> None:
    """Back-to-back dealer reveals commit per-slot without resetting earlier slots and survive partial wipes."""
    cfg = CompanionConfig(confirm_frames=3, clear_frames=3)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    p_cards = [_det("10", 0, 10), _det("7", 7, 30)]
    _feed_hud(sm, p_cards, [_det("6", 6, 10), _det("BACK", None, 35)], ts=1.0, frames=3, banner="WAIT")

    # Frame 1: hole card '10' reveals
    evs1 = sm.step(FrameObservation(ts=2.00, banner_state="WAIT", player_cards=p_cards, dealer_cards=[_det("6", 6, 10), _det("10", 0, 35)]))
    assert [e for e in evs1 if e["type"] == "card"] == []

    # Frame 2: hit '4' arrives immediately while '10' hits frame 2 -> '10' commits without resetting!
    evs2 = sm.step(FrameObservation(
        ts=2.02,
        banner_state="WAIT",
        player_cards=p_cards,
        dealer_cards=[_det("6", 6, 10), _det("10", 0, 35), _det("4", 4, 58)],
    ))
    assert [(e["target"], e["rankLabel"]) for e in evs2 if e["type"] == "card"] == [("D", "10")]

    # Frame 3: hit '5' arrives immediately while '4' hits frame 2 -> '4' commits!
    evs3 = sm.step(FrameObservation(
        ts=2.04,
        banner_state="WAIT",
        player_cards=p_cards,
        dealer_cards=[_det("6", 6, 10), _det("10", 0, 35), _det("4", 4, 58), _det("5", 5, 80)],
    ))
    assert [(e["target"], e["rankLabel"]) for e in evs3 if e["type"] == "card"] == [("D", "4")]

    # Frame 4: partial wipe shows only ['6'] before PLACE_BETS -> '5' still rescues immediately for review, auto_watch waits for NO_MORE_BETS
    evs4 = sm.step(FrameObservation(ts=2.06, banner_state="WAIT", player_cards=p_cards, dealer_cards=[_det("6", 6, 10)]))
    assert [e for e in evs4 if e["type"] == "card"] == []
    evs_end = sm.step(FrameObservation(ts=2.08, banner_state="PLACE_BETS"))
    assert ("D", "5") in [(e["target"], e["rankLabel"]) for e in evs_end if e["type"] == "card"]
    assert not any(e["type"] == "auto_watch" for e in evs_end)
    evs_nmb = sm.step(FrameObservation(ts=2.50, banner_state="NO_MORE_BETS"))
    assert any(e["type"] == "auto_watch" and e["summary"] == "2P+4D+0B" for e in evs_nmb)


def test_dealer_only_micropoll_and_wait_fade_keeps_dealer_active() -> None:
    """Dealer-only micro-polls commit hole/hit cards fast and early WAIT fade doesn't blindfold active cards."""
    cfg = CompanionConfig(confirm_frames=3, clear_frames=3)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    p_cards = [_det("9", 9, 10), _det("9", 9, 30)]
    _feed_hud(sm, p_cards, [_det("5", 5, 10), _det("BACK", None, 35)], ts=1.0, frames=3, banner="WAIT")
    assert sm.state == "PLAYER_TURN"

    # Two dealer_only micro-polls commit 'K' immediately and transition to DEALER_TURN
    evs_m1 = sm.step(FrameObservation(
        ts=1.50,
        dealer_cards=[_det("5", 5, 10), _det("K", 0, 35)],
        hud_refreshed=False,
        dealer_only=True,
    ))
    assert evs_m1 == []
    evs_m2 = sm.step(FrameObservation(
        ts=1.52,
        dealer_cards=[_det("5", 5, 10), _det("K", 0, 35)],
        hud_refreshed=False,
        dealer_only=True,
    ))
    assert [(e["target"], e["rankLabel"]) for e in evs_m2 if e["type"] == "card"] == [("D", "K")]
    assert sm.state == "DEALER_TURN"

    # WAIT fades out early while cards remain visible on HUD -> dealer hit '7' still records cleanly!
    evs_hit = _feed_hud(
        sm,
        p_cards,
        [_det("5", 5, 10), _det("K", 0, 35), _det("7", 7, 58)],
        ts=1.70,
        frames=2,
        banner="",
    )
    assert [(e["target"], e["rankLabel"]) for e in evs_hit if e["type"] == "card"] == [("D", "7")]


def test_red_dealer_face_card_not_truncated_as_back(tmp_path: Path) -> None:
    """Red heart/diamond dealer cards neither trigger BACK truncation nor lose rightmost slim cards."""
    tdir = tmp_path / "templates"

    def mk_card(rk: str, is_red: bool = False, h_card: int = 45, w_card: int = 22) -> np.ndarray:
        img = np.full((h_card + 12, w_card + 8, 3), 24, dtype=np.uint8)
        y0, x0 = 6, 4
        img[y0 : y0 + h_card, x0 : x0 + w_card] = (248, 248, 248)
        color = (25, 25, 215) if is_red else (20, 20, 20)
        thick = max(1, round(h_card / 22))
        fscale = (h_card / 45.0) * 0.50
        cv2.putText(img, rk, (x0 + 2, y0 + round(h_card * 0.36)), cv2.FONT_HERSHEY_SIMPLEX, fscale, color, thick, cv2.LINE_AA)
        cx = x0 + w_card // 2
        cy = y0 + round(h_card * 0.78)
        r = max(3, round(h_card * 0.14))
        pts = np.array([[cx - r, cy - r // 2], [cx + r, cy - r // 2], [cx, cy + r]], np.int32)
        cv2.fillPoly(img, [pts], color)
        return img

    for rk in ("6", "Q", "5"):
        save_template_crop("hud", rk, mk_card(rk), tdir, auto_trim=True)

    det = PlaytechDetector(CompanionConfig(), templates_root=tdir)
    # Connected cluster: black 6, red Q♥, narrow rightmost black 5 (w=17)
    h_card = 36
    strip = np.full((h_card + 12, 90, 3), 24, dtype=np.uint8)
    y0, x_cur = 6, 8
    for rk, is_red, cw in (("6", False, 18), ("Q", True, 18), ("5", False, 16)):
        strip[y0 : y0 + h_card, x_cur : x_cur + cw] = (248, 248, 248)
        color = (25, 25, 215) if is_red else (20, 20, 20)
        cv2.putText(strip, rk, (x_cur + 2, y0 + round(h_card * 0.36)), cv2.FONT_HERSHEY_SIMPLEX, (h_card / 45.0) * 0.50, color, 2, cv2.LINE_AA)
        cx = x_cur + cw // 2
        cy = y0 + round(h_card * 0.78)
        r = max(3, round(h_card * 0.14))
        pts = np.array([[cx - r, cy - r // 2], [cx + r, cy - r // 2], [cx, cy + r]], np.int32)
        cv2.fillPoly(strip, [pts], color)
        x_cur += cw

    flat, _, _ = det._detect_hud_strip(strip, is_dealer=True)
    assert [c.rank for c in flat] == ["6", "Q", "5"]


def test_no_ghost_no_suit_aces_on_felt_text_or_fade(tmp_path: Path) -> None:
    """Empty felt shadows, white text, faded cards, standalone BACK, and no-suit edge glitches never trigger ghost Aces."""
    tdir = tmp_path / "templates"
    card_a = np.full((57, 30, 3), 24, dtype=np.uint8)
    card_a[6:51, 4:26] = (248, 248, 248)
    cv2.putText(card_a, "A", (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (20, 20, 20), 2, cv2.LINE_AA)
    cv2.fillPoly(card_a, [np.array([[15, 34], [10, 44], [20, 44]], np.int32)], (20, 20, 20))
    save_template_crop("hud", "A", card_a, tdir, auto_trim=True)
    save_template_crop("suit", "S", card_a, tdir, auto_trim=True)

    det = PlaytechDetector(CompanionConfig(), templates_root=tdir)
    base = np.full((75, 180, 3), (38, 52, 42), dtype=np.uint8)

    # 1. Low-contrast felt shadow & white UI text must not invoke unguarded fallback
    shadow = base.copy()
    cv2.fillPoly(shadow, [np.array([[48, 32], [56, 17], [64, 32]])], (28, 40, 32))
    assert det._detect_hud_strip(shadow, is_dealer=True)[0] == []

    txt = base.copy()
    cv2.putText(txt, "WIN", (22, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (235, 235, 235), 2, cv2.LINE_AA)
    assert det._detect_hud_strip(txt, is_dealer=True)[0] == []

    # 2. Standalone red-checkered BACK & 50% faded white card must not produce ghost A
    back_roi = base.copy()
    back = np.full((45, 28, 3), (35, 35, 210), dtype=np.uint8)
    back[2:43:3, 2:26:3] = (240, 240, 240)
    back[:3, :] = (240, 240, 240)
    back[-3:, :] = (240, 240, 240)
    back_roi[12:57, 40:68] = back
    assert det._detect_hud_strip(back_roi, is_dealer=True)[0] == []

    faded = base.copy()
    faded[18:63, 60:82] = (140, 140, 140)
    cv2.putText(faded, "A", (62, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (70, 70, 70), 2, cv2.LINE_AA)
    assert det._detect_hud_strip(faded, is_dealer=True)[0] == []

    # 3. White card cluster WITH rank 'A' but missing any suit pip below must be rejected when suit bank is active
    no_suit = base.copy()
    no_suit[12:57, 40:66] = (248, 248, 248)
    cv2.putText(no_suit, "A", (42, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (20, 20, 20), 2, cv2.LINE_AA)
    assert det._detect_hud_strip(no_suit, is_dealer=True)[0] == []


def test_state_machine_rejects_prefix_shift_and_low_conf_fast_tail(tmp_path: Path) -> None:
    """Misaligned left-prepended ghosts never duplicate committed ranks, and <0.84 conf needs full confirm_frames."""
    cfg = CompanionConfig(confirm_frames=3, clear_frames=3, min_hud_confidence=0.84)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    p_cards = [_det("10", 0, 10), _det("7", 7, 30)]
    _feed_hud(sm, p_cards, [_det("6", 6, 30), _det("BACK", None, 55)], ts=1.0, frames=3, banner="WAIT")

    # Left-prepended ghost 'A' shifts real '6' to index 1 -> must NOT emit duplicate '6'
    evs_shift = _feed_hud(
        sm,
        p_cards,
        [_det("A", 1, 8, conf=0.90), _det("6", 6, 30, conf=0.95)],
        ts=2.0,
        frames=3,
        banner="WAIT",
    )
    assert [e for e in evs_shift if e["type"] == "card"] == []

    # Low-conf (0.79 < 0.84) reveal must NOT commit in 2 frames; waits for frame 3
    obs1 = FrameObservation(ts=2.50, banner_state="WAIT", player_cards=p_cards, dealer_cards=[_det("6", 6, 30), _det("K", 0, 55, conf=0.79)])
    obs2 = FrameObservation(ts=2.52, banner_state="WAIT", player_cards=p_cards, dealer_cards=[_det("6", 6, 30), _det("K", 0, 55, conf=0.79)])
    obs3 = FrameObservation(ts=2.54, banner_state="WAIT", player_cards=p_cards, dealer_cards=[_det("6", 6, 30), _det("K", 0, 55, conf=0.79)])
    assert [e for e in sm.step(obs1) if e["type"] == "card"] == []
    assert [e for e in sm.step(obs2) if e["type"] == "card"] == []
    assert [(e["target"], e["rankLabel"]) for e in sm.step(obs3) if e["type"] == "card"] == [("D", "K")]

    # detector.invalidate_cache clears cached strips and scale hints on resync
    det = PlaytechDetector(cfg, templates_root=tmp_path)
    det._cached_d = [_det("A", 1, 10)]
    det._last_d_sig = b"abc"
    det._banner_scale_hint = 1.25
    det.invalidate_cache(reset_scale_hint=True)
    assert det._cached_d == []
    assert det._last_d_sig is None
    assert det._banner_scale_hint is None


def test_qq_split_overlap_debounce_and_auto_resync(tmp_path: Path) -> None:
    """Q,Q split ignores overlap/1-frame H1 misreads without duplicating Q, and auto-resync fixes drifted UI state."""
    from config import CompanionConfig
    from detector import CardDetection, FrameObservation
    from state_machine import CompanionStateMachine

    cfg = CompanionConfig(confirm_frames=2, clear_frames=3, min_hud_confidence=0.84)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    q1 = CardDetection("Q", 0, 0.94, 10, 0, 12, 22, suit="S", hand_index=0)
    q2 = CardDetection("Q", 0, 0.93, 24, 0, 12, 22, suit="H", hand_index=0)
    d6 = CardDetection("6", 6, 0.95, 10, 0, 12, 22, suit="C")
    for ts in (1.0, 1.2):
        evs_pre = sm.step(FrameObservation(ts=ts, banner_state="WAIT", player_cards=[q1, q2], player_hands={0: [q1, q2]}, dealer_cards=[d6]))

    # Unseparated overlap frame (H0 still [Q, Q] while H1 shows [Q]) must not emit split or duplicate Q
    h1_q = CardDetection("Q", 0, 0.92, 60, 0, 12, 22, suit="H", hand_index=1)
    evs_ov = sm.step(FrameObservation(
        ts=2.0,
        banner_state="WAIT",
        split_detected=True,
        player_cards=[q1, q2, h1_q],
        player_hands={0: [q1, q2], 1: [h1_q]},
        dealer_cards=[d6],
    ))
    assert evs_ov == []

    # Separated frame 1 with temporary H1 misread 'K' (still val 0) holds split pending confirmation
    h1_k = CardDetection("K", 0, 0.88, 60, 0, 12, 22, suit="H", hand_index=1)
    evs_p1 = sm.step(FrameObservation(
        ts=2.2,
        banner_state="WAIT",
        split_detected=True,
        player_cards=[q1, h1_k],
        player_hands={0: [q1], 1: [h1_k]},
        dealer_cards=[d6],
    ))
    assert evs_p1 == []

    # Separated frame 2 confirms split cleanly; neither H0 nor H1 emits a duplicate Q
    evs_p2 = sm.step(FrameObservation(
        ts=2.4,
        banner_state="WAIT",
        split_detected=True,
        player_cards=[q1, h1_q],
        player_hands={0: [q1], 1: [h1_q]},
        dealer_cards=[d6],
    ))
    assert [e for e in evs_p2 if e["type"] == "split"]
    assert [e for e in evs_p2 if e["type"] == "card" and e["target"] == "P"] == []

    # Genuine later 3rd Q dealt to H0 still records normally at slot 1
    q3 = CardDetection("Q", 0, 0.95, 24, 0, 12, 22, suit="D", hand_index=0)
    evs_hit = []
    for ts in (3.0, 3.2):
        evs_hit.extend(sm.step(FrameObservation(
            ts=ts,
            banner_state="WAIT",
            split_detected=True,
            player_cards=[q1, q3, h1_q],
            player_hands={0: [q1, q3], 1: [h1_q]},
            dealer_cards=[d6],
        )))
    assert [(e["handIndex"], e["slot"], e["rankLabel"]) for e in evs_hit if e["type"] == "card" and e["target"] == "P"] == [(0, 1, "Q")]

    # Auto-resync fixes an extra drifted card in UI (e.g. phantom '4') after >=6 frames & >=0.55s
    sm.resync_round()
    sm.set_sync_state(play_mode="watch")
    c4_a = CardDetection("4", 4, 0.93, 10, 0, 12, 22, suit="C", hand_index=0)
    c4_b = CardDetection("4", 4, 0.95, 24, 0, 12, 22, suit="S", hand_index=0)
    for ts in (5.0, 5.15):
        sm.step(FrameObservation(ts=ts, banner_state="WAIT", player_cards=[c4_a, c4_b], player_hands={0: [c4_a, c4_b]}, dealer_cards=[d6]))
    sm.set_sync_state(
        comp_round_id=sm.round_id,
        draft_hands=[[4, 4, 4]],
        draft_dealer=[6],
        draft_hands_manual=[False],
        draft_dealer_manual=False,
    )
    rec_evs = []
    for i in range(6):
        rec_evs.extend(sm.step(FrameObservation(
            ts=5.5 + i * 0.12,
            banner_state="WAIT",
            player_cards=[c4_a, c4_b],
            player_hands={0: [c4_a, c4_b]},
            dealer_cards=[d6],
        )))
    recs = [e for e in rec_evs if e["type"] == "reconcile"]
    assert len(recs) == 1
    assert [[c["val"] for c in h] for h in recs[0]["hands"]] == [[4, 4]]

    # Manual-entry guard: manually typed card must NOT be shrunk away by fewer HUD cards
    sm.set_sync_state(
        comp_round_id=sm.round_id,
        draft_hands=[[4, 4, 5]],
        draft_dealer=[6],
        draft_hands_manual=[True],
        draft_dealer_manual=False,
    )
    man_evs = []
    for i in range(7):
        man_evs.extend(sm.step(FrameObservation(
            ts=8.0 + i * 0.12,
            banner_state="WAIT",
            player_cards=[c4_a, c4_b],
            player_hands={0: [c4_a, c4_b]},
            dealer_cards=[d6],
        )))
    assert [e for e in man_evs if e["type"] == "reconcile"] == []


def test_delayed_auto_watch_review_window_fallback_and_manual_clear() -> None:
    """auto_watch waits through PLACE_BETS/LAST_BETS until NO_MORE_BETS, flushes on fallback, and cancels on manual clear."""
    from config import CompanionConfig
    from detector import FrameObservation
    from state_machine import CompanionStateMachine

    cfg = CompanionConfig(confirm_frames=2, clear_frames=2, shuffle_hold_sec=1.0)

    # 1. Held across clear -> PLACE_BETS -> LAST_BETS -> flushed BEFORE phase on NO_MORE_BETS
    sm1 = CompanionStateMachine(cfg)
    sm1.set_sync_state(play_mode="watch")
    _feed_hud(sm1, [_det("10", 0, 10), _det("9", 9, 30)], [_det("7", 7, 10), _det("K", 0, 35)], ts=1.0, frames=2, banner="WAIT")
    evs_wipe = _feed_hud(sm1, [], [], ts=2.0, frames=2, banner="WAIT")
    assert evs_wipe == []
    evs_pb = sm1.step(FrameObservation(ts=3.0, banner_state="PLACE_BETS"))
    assert [e["type"] for e in evs_pb] == ["phase"]
    evs_lb = sm1.step(FrameObservation(ts=4.0, banner_state="LAST_BETS"))
    assert [e["type"] for e in evs_lb] == ["phase"]
    evs_nmb = sm1.step(FrameObservation(ts=5.0, banner_state="NO_MORE_BETS"))
    assert [e["type"] for e in evs_nmb] == ["auto_watch", "phase"]
    assert evs_nmb[0]["fromWatch"] is True
    assert evs_nmb[0]["summary"] == "2P+2D+0B"

    # 2. Fallback flush on missed NO_MORE_BETS when next round WAIT begins
    sm2 = CompanionStateMachine(cfg)
    sm2.set_sync_state(play_mode="watch")
    _feed_hud(sm2, [_det("8", 8, 10), _det("7", 7, 30)], [_det("9", 9, 10), _det("10", 0, 35)], ts=1.0, frames=2, banner="WAIT")
    r1_id = sm2.round_id
    assert sm2.step(FrameObservation(ts=2.0, banner_state="PLACE_BETS"))[0]["type"] == "phase"
    evs_wait = sm2.step(FrameObservation(ts=3.0, banner_state="WAIT"))
    assert [e["type"] for e in evs_wait] == ["auto_watch", "phase"]
    assert evs_wait[0]["roundId"] == r1_id
    assert evs_wait[1]["phase"] == "WAIT"
    assert evs_wait[1]["roundId"] != r1_id

    # 3. Manual UI log/clear during review (draft_count -> 0) cancels pending auto_watch so NO_MORE_BETS won't double-log
    sm3 = CompanionStateMachine(cfg)
    sm3.set_sync_state(play_mode="watch", draft_count=4)
    _feed_hud(sm3, [_det("6", 6, 10), _det("5", 5, 30)], [_det("8", 8, 10), _det("10", 0, 35)], ts=1.0, frames=2, banner="WAIT")
    sm3.step(FrameObservation(ts=2.0, banner_state="PLACE_BETS"))
    assert sm3._pending_auto_watch is not None
    sm3.set_sync_state(draft_count=0)
    assert sm3._pending_auto_watch is None
    evs_nmb3 = sm3.step(FrameObservation(ts=3.0, banner_state="NO_MORE_BETS"))
    assert [e["type"] for e in evs_nmb3] == ["phase"]

    # 4. Shuffle before NO_MORE_BETS flushes final watched hand BEFORE new_shoe event
    sm4 = CompanionStateMachine(cfg)
    sm4.set_sync_state(play_mode="watch")
    _feed_hud(sm4, [_det("A", 1, 10), _det("9", 9, 30)], [_det("K", 0, 10), _det("7", 7, 35)], ts=1.0, frames=2, banner="WAIT")
    _feed_hud(sm4, [], [], ts=2.0, frames=2, banner="WAIT")
    sm4.step(FrameObservation(ts=3.0, shuffle_active=True))
    evs_shuf = sm4.step(FrameObservation(ts=4.2, shuffle_active=True))
    assert [e["type"] for e in evs_shuf] == ["auto_watch", "new_shoe"]


def test_score_badge_occluded_card0_and_split_seed_lock(tmp_path) -> None:
    """Score badge covering bottom-left of Card 0 still recovers Card 0, and split seed locking prevents Card 0 loss."""
    import cv2
    import numpy as np
    from calibrate import save_template_crop
    from detector import PlaytechDetector

    tdir = tmp_path / "templates"

    def mk_card(txt: str, red: bool = False) -> np.ndarray:
        im = np.full((45, 22, 3), 248, dtype=np.uint8)
        col = (30, 30, 215) if red else (20, 20, 20)
        cv2.putText(im, txt, (2, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.52, col, 2, cv2.LINE_AA)
        return im

    save_template_crop("hud", "8", mk_card("8"), tdir, auto_trim=True)
    save_template_crop("hud", "3", mk_card("3", True), tdir, auto_trim=True)
    save_template_crop("hud", "Q", mk_card("Q"), tdir, auto_trim=True)

    det = PlaytechDetector(CompanionConfig(), templates_root=tdir)
    strip = np.full((57, 150, 3), 24, dtype=np.uint8)
    # H0: Card 0 ("8") has its lower rows (25:51) occluded by a dark score badge, leaving only top corner (6:25)!
    strip[6:51, 12:30] = (248, 248, 248)
    cv2.putText(strip, "8", (14, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2, cv2.LINE_AA)
    strip[25:53, 6:31] = (32, 35, 40)  # Dark circular score badge covering lower half of Card 0
    # Card 1 ("3") beside Card 0
    strip[6:51, 31:51] = (248, 248, 248)
    cv2.putText(strip, "3", (33, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (30, 30, 215), 2, cv2.LINE_AA)

    flat, hands, is_split = det._detect_hud_strip(strip, is_dealer=False)
    assert is_split is False
    assert [c.rank for c in hands[0]] == ["8", "3"]

    # State machine split seed lock: even if H0's Card 0 is completely occluded in a frame after split, seed '8' is locked
    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")
    for ts in (1.0, 1.2):
        sm.step(
            FrameObservation(
                ts=ts,
                banner_state="WAIT",
                player_cards=[_det("8", 8, 10), _det("8", 8, 25)],
                player_hands={0: [_det("8", 8, 10), _det("8", 8, 25)]},
                dealer_cards=[_det("7", 7, 10)],
            )
        )
    # Split occurs, but H0 only detects the drawn card ["3"] while H1 detects ["8", "Q"]
    emitted: list[dict] = []
    for ts in (2.0, 2.2):
        h0_partial = [CardDetection("3", 3, 0.95, 22, 0, 14, 42, hand_index=0)]
        h1_full = [
            CardDetection("8", 8, 0.95, 70, 0, 14, 42, hand_index=1),
            CardDetection("Q", 0, 0.95, 84, 0, 14, 42, hand_index=1),
        ]
        emitted.extend(
            sm.step(
                FrameObservation(
                    ts=ts,
                    banner_state="WAIT",
                    split_detected=True,
                    player_cards=h0_partial + h1_full,
                    player_hands={0: h0_partial, 1: h1_full},
                    dealer_cards=[_det("7", 7, 10), _det("K", 0, 28)],
                )
            )
        )
    assert sm.p_debouncers[0].committed_ranks == ["8", "3"]
    assert sm.p_debouncers[1].committed_ranks == ["8", "Q"]
    p_cards = [(e.get("handIndex"), e["rankLabel"]) for e in emitted if e["type"] == "card" and e["target"] == "P"]
    assert p_cards == [(0, "3"), (1, "Q")]


def test_dealer_partial_wipe_rescue_and_dealer_felt_fallback() -> None:
    """1-frame final dealer draw survives a 2-frame partial wipe; if HUD never shows it, dealer_felt_roi rescues it."""
    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)

    # Case A: 1-frame final dealer card ('5') followed by 2 frames of partial wipe ([6, 10]) then full wipe
    sm_a = CompanionStateMachine(cfg)
    sm_a.set_sync_state(play_mode="watch")
    _feed_hud(sm_a, [_det("10", 0, 10), _det("9", 9, 30)], [_det("6", 6, 10), _det("10", 0, 28)], ts=1.0, frames=2, banner="WAIT")
    # Single frame of 3rd dealer card '5' (brings dealer 16 -> 21)
    sm_a.step(
        FrameObservation(
            ts=2.0,
            banner_state="WAIT",
            player_cards=[_det("10", 0, 10), _det("9", 9, 30)],
            dealer_cards=[_det("6", 6, 10), _det("10", 0, 28), _det("5", 5, 46, conf=0.81)],
            hud_refreshed=True,
        )
    )
    # Rightmost card '5' fades 2 frames early while [6, 10] are still visible
    for k in range(2):
        sm_a.step(
            FrameObservation(
                ts=2.15 + k * 0.12,
                banner_state="WAIT",
                player_cards=[_det("10", 0, 10), _det("9", 9, 30)],
                dealer_cards=[_det("6", 6, 10), _det("10", 0, 28)],
                hud_refreshed=True,
            )
        )
    # Full wipe rescues '5' -> dealer reaches 21 -> stages auto_watch cleanly
    wipe_evs = _feed_hud(sm_a, [], [], ts=3.0, frames=2, banner="WAIT")
    d_rescued = [e for e in wipe_evs if e["type"] == "card" and e["target"] == "D"]
    assert len(d_rescued) == 1 and d_rescued[0]["rankLabel"] == "5"
    assert sm_a._pending_auto_watch is not None

    # Case B: Digital HUD never renders the 3rd dealer card (wipes at [6, 10] = 16), but dealer_felt_roi caught '5'
    sm_b = CompanionStateMachine(cfg)
    sm_b.set_sync_state(play_mode="watch")
    _feed_hud(sm_b, [_det("10", 0, 10), _det("8", 8, 30)], [_det("6", 6, 10), _det("10", 0, 28)], ts=10.0, frames=2, banner="WAIT")
    felt_evs = sm_b.step(
        FrameObservation(
            ts=11.0,
            banner_state="WAIT",
            player_cards=[_det("10", 0, 10), _det("8", 8, 30)],
            dealer_cards=[_det("6", 6, 10), _det("10", 0, 28)],
            felt_events=[FeltObservation(ts=11.0, matched=True, rank="5", val=5, conf=0.84, sharpness=210.0, zone="dealer")],
            hud_refreshed=False,
        )
    )
    assert any(e["type"] == "felt_sighting" and e["zone"] == "dealer" and e["rankLabel"] == "5" for e in felt_evs)
    # Digital HUD wipes at 16 -> dealer_felt_fallback rescues '5' into D!
    evs_pb = sm_b.step(FrameObservation(ts=12.0, banner_state="PLACE_BETS"))
    fb_cards = [e for e in evs_pb if e["type"] == "card" and e["target"] == "D" and e.get("source") == "dealer_felt_fallback"]
    assert len(fb_cards) == 1 and fb_cards[0]["rankLabel"] == "5"
    assert sm_b._pending_auto_watch is not None

    # Case C: Digital HUD wipes at [6, 10] = 16 with NO dealer_felt_roi fallback -> inserts educated guess & queues auto_watch for NO_MORE_BETS
    sm_c = CompanionStateMachine(cfg)
    sm_c.set_sync_state(play_mode="watch")
    _feed_hud(sm_c, [_det("10", 0, 10), _det("8", 8, 30)], [_det("6", 6, 10), _det("10", 0, 28)], ts=20.0, frames=2, banner="WAIT")
    evs_inc = sm_c.step(FrameObservation(ts=21.0, banner_state="PLACE_BETS"))
    guess_cards = [e for e in evs_inc if e["type"] == "card" and e["target"] == "D" and e.get("source") == "educated_guess"]
    assert len(guess_cards) == 1 and guess_cards[0]["rankLabel"] == "10"
    prompts = [e for e in evs_inc if e["type"] == "prompt_confirm"]
    assert len(prompts) == 1
    assert prompts[0]["reason"] == "DEALER_INCOMPLETE"
    assert prompts[0]["suggestedFocus"] == "D"
    assert prompts[0]["educatedGuessRank"] == "10"
    assert prompts[0]["dealerTotal"] == 26
    assert sm_c._pending_auto_watch is not None
    evs_nmb_c = sm_c.step(FrameObservation(ts=22.0, banner_state="NO_MORE_BETS"))
    assert any(e["type"] == "auto_watch" and e.get("fromWatch") is True for e in evs_nmb_c)


def test_felt_burst_monitor_multicard_and_hud_fallback_bank(tmp_path) -> None:
    """FeltBurstMonitor matches ranks via hud_bank fallback and detects a 2nd card placed beside a stationary 1st card."""
    import cv2
    import numpy as np
    from calibrate import save_template_crop
    from detector import FeltBurstMonitor, TemplateBank

    tdir = tmp_path / "templates"
    im_k = np.full((38, 22, 3), 248, dtype=np.uint8)
    cv2.putText(im_k, "K", (2, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (25, 25, 215), 2, cv2.LINE_AA)
    im_5 = np.full((38, 22, 3), 248, dtype=np.uint8)
    cv2.putText(im_5, "5", (2, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 2, cv2.LINE_AA)
    save_template_crop("hud", "K", im_k, tdir, auto_trim=True)
    save_template_crop("hud", "5", im_5, tdir, auto_trim=True)

    empty_felt_bank = TemplateBank(tdir / "felt")
    hud_bank = TemplateBank(tdir / "hud")
    cfg = CompanionConfig()
    mon = FeltBurstMonitor(cfg, empty_felt_bank, zone="dealer", fallback_bank=hud_bank)

    bg = np.full((90, 160, 3), (35, 95, 45), dtype=np.uint8)
    assert mon.step(bg, 1.0) is None

    # Card 1 ('K') slides in on the left and settles
    f1_move = bg.copy()
    f1_move[18:68, 16:52] = (245, 245, 245)
    cv2.putText(f1_move, "K", (19, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (25, 25, 215), 2, cv2.LINE_AA)
    f1_settle = bg.copy()
    f1_settle[20:70, 18:54] = (248, 248, 248)
    cv2.putText(f1_settle, "K", (21, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (25, 25, 215), 2, cv2.LINE_AA)

    mon.step(f1_move, 1.10)
    obs1 = None
    for i in range(5):
        obs1 = mon.step(f1_settle, 1.20 + i * 0.15) or obs1
    assert obs1 is not None and obs1.matched is True and obs1.rank == "K" and obs1.zone == "dealer"

    # Card 1 ('K') stays stationary on the felt while Card 2 ('5') is dealt beside it at x=80..116
    f2_move = f1_settle.copy()
    f2_move[18:68, 78:114] = (245, 245, 245)
    cv2.putText(f2_move, "5", (81, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 2, cv2.LINE_AA)
    f2_settle = f1_settle.copy()
    f2_settle[20:70, 80:116] = (248, 248, 248)
    cv2.putText(f2_settle, "5", (83, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 2, cv2.LINE_AA)

    mon.step(f2_move, 2.50)
    obs2 = None
    for i in range(5):
        obs2 = mon.step(f2_settle, 2.60 + i * 0.15) or obs2
    assert obs2 is not None and obs2.matched is True and obs2.rank == "5" and obs2.zone == "dealer"


def test_real_felt_templates_leave_one_out_seam_split_and_back_rejection() -> None:
    """Uploaded physical felt samples achieve >=95% leave-one-out accuracy, split side-by-side cards, and ignore BACK_1."""
    from config import COMPANION_DIR, CompanionConfig
    from detector import FeltBurstMonitor, TemplateBank, extract_felt_rank_views

    felt_dir = COMPANION_DIR / "templates" / "felt"
    bank = TemplateBank(felt_dir)
    # BACK_1.png is automatically excluded from card_templates
    assert all(t.rank != "BACK" for t in bank.card_templates)
    assert len(bank.card_templates) >= 40

    cfg = CompanionConfig()
    mon = FeltBurstMonitor(cfg, bank, zone="community")

    # 1. Leave-one-out benchmark across all multi-sample uploaded felt templates
    by_rank: dict[str, int] = {}
    for t in bank.card_templates:
        by_rank[t.rank] = by_rank.get(t.rank, 0) + 1

    loo_total = 0
    loo_hits = 0
    for idx, query_tpl in enumerate(bank.card_templates):
        assert query_tpl.bgr is not None
        if by_rank[query_tpl.rank] < 2:
            # Singleton rank (e.g. 7_1.png): verify top-left vs 180°-rotated bottom-right self-consistency
            tl, br = extract_felt_rank_views(query_tpl.bgr)
            assert tl is not None and br is not None
            pad_br = cv2.copyMakeBorder(br, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=255)
            self_conf = float(cv2.matchTemplate(pad_br, tl, cv2.TM_CCOEFF_NORMED).max())
            assert self_conf >= 0.80
            continue
        loo_total += 1
        others = [t for j, t in enumerate(bank.card_templates) if j != idx]
        matched_tpl, conf = mon._match_patch(query_tpl.bgr, others, tuple(cfg.felt_motion.scales))
        if matched_tpl is not None and matched_tpl.rank == query_tpl.rank and conf >= cfg.min_felt_confidence:
            loo_hits += 1

    assert loo_total >= 35
    assert (loo_hits / loo_total) >= 0.95

    # 2. BACK_1.png (face-down card) is rejected and never locks _settled_boxes before flipping face-up
    back_img = cv2.imread(str(felt_dir / "BACK_1.png"))
    c4_img = cv2.imread(str(felt_dir / "4_4.png"))
    q3_img = cv2.imread(str(felt_dir / "Q_3.png"))
    c10_img = cv2.imread(str(felt_dir / "10_1.png"))
    assert back_img is not None and c4_img is not None and q3_img is not None and c10_img is not None
    assert extract_felt_rank_views(back_img) == (None, None)

    roi = np.full((34, 130, 3), (45, 70, 35), dtype=np.uint8)
    # Place face-down BACK_1 at x=10 and face-up 4_4 at x=42
    roi[7 : 7 + back_img.shape[0], 10 : 10 + back_img.shape[1]] = back_img
    roi[7 : 7 + c4_img.shape[0], 42 : 42 + c4_img.shape[1]] = c4_img

    # Static detector finds 4_4 and ignores BACK_1
    static_1 = mon.detect_static_cards(roi)
    assert [d.rank for d in static_1] == ["4"]

    # Step FeltBurstMonitor until 4_4 settles
    obs_4 = None
    for i in range(6):
        obs_4 = mon.step(roi, 1.0 + i * 0.15) or obs_4
    assert obs_4 is not None and obs_4.matched is True and obs_4.rank == "4"

    # Now place Q_3 immediately adjacent to 4_4 at x=71 (touching, forming a wide fused 2-card contour)
    roi[7 : 7 + q3_img.shape[0], 71 : 71 + q3_img.shape[1]] = q3_img
    static_2 = mon.detect_static_cards(roi)
    assert [d.rank for d in static_2] == ["4", "Q"]

    obs_q = None
    for i in range(6):
        obs_q = mon.step(roi, 2.5 + i * 0.15) or obs_q
    assert obs_q is not None and obs_q.matched is True and obs_q.rank == "Q"

    # Now flip BACK_1 in-place at x=10 into face-up 10_1 -> because BACK_1 never locked _settled_boxes, 10_1 is detected!
    roi[7 : 7 + back_img.shape[0], 10 : 10 + back_img.shape[1]] = (45, 70, 35)
    roi[7 : 7 + c10_img.shape[0], 10 : 10 + c10_img.shape[1]] = c10_img
    obs_10 = None
    for i in range(6):
        obs_10 = mon.step(roi, 4.0 + i * 0.15) or obs_10
    assert obs_10 is not None and obs_10.matched is True and obs_10.rank == "10"


def test_staggered_initial_player_deal_and_predeal_seed_no_delay() -> None:
    """Player Card 1 commits on Frame 2 even when Card 2 arrives on Frame 2, and pre-deal cards during NO_MORE_BETS seed deal."""
    cfg = CompanionConfig(confirm_frames=3, clear_frames=3)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")

    # 1. Staggered deal under WAIT: Card 1 ('10') on Frame 1, then Card 2 ('A') joins on Frame 2
    c1 = CardDetection("10", 0, 0.94, 10, 0, 14, 22, suit="S")
    c2 = CardDetection("A", 1, 0.95, 26, 0, 14, 22, suit="H")
    d_up = CardDetection("6", 6, 0.93, 10, 0, 14, 22, suit="D")
    d_back = CardDetection("BACK", None, 0.92, 26, 0, 14, 22)

    # Frame 1: only Player Card 1 ('10') is on HUD
    evs_f1 = sm.step(FrameObservation(ts=1.00, banner_state="WAIT", player_cards=[c1], dealer_cards=[]))
    assert [e for e in evs_f1 if e["type"] == "card"] == []

    # Frame 2 (40ms later): Player Card 2 ('A') and Dealer Upcard ('6') arrive!
    # Card 1 ('10') MUST commit immediately on Frame 2 instead of having its counter reset by Card 2!
    evs_f2 = sm.step(
        FrameObservation(ts=1.04, banner_state="WAIT", player_cards=[c1, c2], dealer_cards=[d_up, d_back])
    )
    assert [(e["target"], e["rankLabel"]) for e in evs_f2 if e["type"] == "card"] == [("P", "10")]

    # Frame 3 (40ms later): Player Card 2 ('A') and Dealer Upcard ('6') commit on their 2nd frame!
    evs_f3 = sm.step(
        FrameObservation(ts=1.08, banner_state="WAIT", player_cards=[c1, c2], dealer_cards=[d_up, d_back])
    )
    assert [(e["target"], e["rankLabel"]) for e in evs_f3 if e["type"] == "card"] == [("P", "A"), ("D", "6")]

    # 2. Pre-deal card appearing during tail end of NO_MORE_BETS (after empty betting HUD) emits on 1st WAIT frame
    sm2 = CompanionStateMachine(cfg)
    sm2.set_sync_state(play_mode="watch")
    # Empty HUD during PLACE_BETS
    sm2.step(FrameObservation(ts=5.00, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[]))
    # First cards appear on HUD while NO_MORE_BETS is still fading out -> locked out during NO_MORE_BETS, but pre-seeded!
    evs_nmb = sm2.step(
        FrameObservation(ts=6.00, banner_state="NO_MORE_BETS", player_cards=[c1, c2], dealer_cards=[d_up, d_back])
    )
    assert [e for e in evs_nmb if e["type"] == "card"] == []
    assert sm2._emitted_p == 0

    # Very first frame of WAIT -> pre-seeded cards reach 2 frames and emit immediately!
    evs_wait1 = sm2.step(
        FrameObservation(ts=6.04, banner_state="WAIT", player_cards=[c1, c2], dealer_cards=[d_up, d_back])
    )
    assert [(e["target"], e["rankLabel"]) for e in evs_wait1 if e["type"] == "card"] == [
        ("P", "10"),
        ("P", "A"),
        ("D", "6"),
    ]


def test_keepalive_config_roundtrip() -> None:
    cfg = CompanionConfig.from_dict(
        {
            "keepalive": {
                "enabled": True,
                "mode": "alert_only",
                "method": "appcommand",
                "prefer_left_window": False,
                "reload_btn_offset": [138, 74],
                "min_watch_sec": 300.0,
                "max_watch_sec": 420.0,
                "hard_warn_sec": 540.0,
                "place_bets_min_delay_sec": 0.4,
                "place_bets_max_delay_sec": 1.2,
                "place_bets_latest_sec": 3.0,
                "reload_grace_sec": 10.0,
                "reload_timeout_sec": 18.0,
                "appcommand_verify_sec": 0.8,
                "reset_on_bet_round": False,
            }
        }
    )
    assert cfg.keepalive.mode == "alert_only"
    assert cfg.keepalive.method == "appcommand"
    assert cfg.keepalive.prefer_left_window is False
    assert cfg.keepalive.reload_btn_offset == [138, 74]
    assert cfg.keepalive.min_watch_sec == 300.0
    assert cfg.keepalive.max_watch_sec == 420.0
    assert cfg.keepalive.reset_on_bet_round is False

    out = cfg.to_dict()["keepalive"]
    assert out["mode"] == "alert_only"
    assert out["reload_btn_offset"] == [138, 74]
    assert out["min_watch_sec"] == 300.0
    assert out["max_watch_sec"] == 420.0
    assert out["reset_on_bet_round"] is False

    # Default config uses Brave Extension -> OS Reload click -> F5 -> Ctrl+R fallback + ROUND_OVER 1.2s delay
    def_cfg = CompanionConfig()
    assert def_cfg.keepalive.enabled is True
    assert def_cfg.keepalive.mode == "auto"
    assert def_cfg.keepalive.method == "ext_then_os"
    assert def_cfg.keepalive.round_over_delay_sec == 1.2
    assert def_cfg.keepalive.min_watch_sec == 570.0
    assert def_cfg.keepalive.max_watch_sec == 630.0
    assert def_cfg.keepalive.hard_warn_sec == 750.0
    assert def_cfg.keepalive.prefer_left_window is True
    assert def_cfg.keepalive.reload_btn_offset is None
    assert "place_bets_roi" in def_cfg.rois
    assert def_cfg.rois["place_bets_roi"] == (0.38, 0.30, 0.24, 0.11)


def test_watch_keepalive_watch_only_pause_and_bet_round_reset() -> None:
    import random
    from config import KeepaliveConfig
    from state_machine import WatchKeepaliveManager

    ka_cfg = KeepaliveConfig(min_watch_sec=100.0, max_watch_sec=100.0, hard_warn_sec=140.0, reset_on_bet_round=True)
    mgr = WatchKeepaliveManager(ka_cfg, rng=random.Random(7))

    # First tick establishes baseline without jump
    act0 = mgr.tick(
        10.0,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="IDLE",
        banner_state="WAIT",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        bet_rounds_count=0,
    )
    assert not act0.trigger_refresh
    assert mgr.to_status(10.0)["state"] == "WATCH_COUNTDOWN"

    # Advance 40s in watch mode (dt capped at 5s per tick -> 8 steps of 5s)
    for i in range(1, 9):
        mgr.tick(
            10.0 + i * 5.0,
            play_mode="watch",
            paused=False,
            window_found=True,
            sm_state="IDLE",
            banner_state="WAIT",
            obs_has_cards=False,
            has_unresolved_prompt=False,
            bet_rounds_count=0,
        )
    st_watch = mgr.to_status(50.0)
    assert 39.0 <= st_watch["elapsedSec"] <= 41.0

    # Switch to BET mode -> pauses timer, never advances elapsed, never fires even if time passes
    for i in range(1, 20):
        act_bet = mgr.tick(
            50.0 + i * 5.0,
            play_mode="bet",
            paused=False,
            window_found=True,
            sm_state="BETTING",
            banner_state="PLACE_BETS",
            obs_has_cards=False,
            has_unresolved_prompt=False,
            bet_rounds_count=0,
        )
        assert not act_bet.trigger_refresh
        assert act_bet.emit_alert is None

    st_bet = mgr.to_status(145.0)
    assert st_bet["state"] == "PAUSED_BET"
    assert 39.0 <= st_bet["elapsedSec"] <= 41.0

    # Settle a real bet (bet_rounds_count 0 -> 1) -> resets Newport clock back to 0:00
    mgr.tick(
        150.0,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="IDLE",
        banner_state="WAIT",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        bet_rounds_count=1,
    )
    st_reset = mgr.to_status(150.0)
    assert st_reset["state"] == "WATCH_COUNTDOWN"
    assert st_reset["elapsedSec"] < 1.0


def test_watch_keepalive_place_bets_trigger_appcommand_fallback_and_recovery() -> None:
    import random
    from config import KeepaliveConfig
    from state_machine import WatchKeepaliveManager

    ka_cfg = KeepaliveConfig(
        mode="auto",
        method="appcommand_then_f5",
        min_watch_sec=20.0,
        max_watch_sec=20.0,
        hard_warn_sec=35.0,
        place_bets_min_delay_sec=0.6,
        place_bets_max_delay_sec=0.6,
        place_bets_latest_sec=3.6,
        appcommand_verify_sec=0.9,
        reload_grace_sec=12.0,
        reload_timeout_sec=20.0,
    )
    mgr = WatchKeepaliveManager(ka_cfg, rng=random.Random(123))

    # Step up to 22s in watch mode under WAIT (5s ticks) -> enters ARMED_NEXT_BETS
    for t in [0.0, 5.0, 10.0, 15.0, 20.0, 22.0]:
        act = mgr.tick(
            t,
            play_mode="watch",
            paused=False,
            window_found=True,
            sm_state="IDLE",
            banner_state="WAIT",
            obs_has_cards=True,
            has_unresolved_prompt=False,
        )
        assert not act.trigger_refresh
    assert mgr.to_status(22.0)["state"] == "ARMED_NEXT_BETS"

    # Unresolved watch-mode prompt MUST NOT block Watch Keep-Alive refresh (user is likely AFK)
    mgr_prompt = WatchKeepaliveManager(ka_cfg, rng=random.Random(123))
    for t in [0.0, 5.0, 10.0, 15.0, 20.0, 22.0]:
        mgr_prompt.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="WAIT", obs_has_cards=True, has_unresolved_prompt=True)
    mgr_prompt.tick(23.0, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=True)
    act_prompt = mgr_prompt.tick(
        23.7,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=True,
    )
    assert act_prompt.trigger_refresh is True
    assert act_prompt.flush_auto_watch is True

    # Active mouse movement AND arming mid-PLACE_BETS still fire whenever PLACE_BETS is visible (no LAST_BETS)
    mgr_mid = WatchKeepaliveManager(ka_cfg, rng=random.Random(9))
    for t in [0.0, 5.0, 10.0, 15.0]:
        mgr_mid.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="WAIT", obs_has_cards=False, has_unresolved_prompt=False)
    # PLACE_BETS already open 4s before timer finishes, plus mouse active (user_idle_ms=50)
    mgr_mid.tick(18.0, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False, user_idle_ms=50)
    mgr_mid.tick(20.0, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False, user_idle_ms=50)
    # 1-frame transient "" flicker should NOT wipe scheduled trigger
    mgr_mid.tick(20.1, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="", obs_has_cards=False, has_unresolved_prompt=False, user_idle_ms=50)
    act_mid = mgr_mid.tick(
        20.4,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        user_idle_ms=50,
    )
    assert act_mid.trigger_refresh is True

    # Default sendinput_f5 escalates to sendinput_ctrl_r if page never blanks
    ka_f5 = KeepaliveConfig(
        mode="auto",
        method="sendinput_f5",
        min_watch_sec=10.0,
        max_watch_sec=10.0,
        place_bets_min_delay_sec=0.35,
        place_bets_max_delay_sec=0.35,
        appcommand_verify_sec=1.2,
    )
    mgr_f5 = WatchKeepaliveManager(ka_f5, rng=random.Random(3))
    for t in [0.0, 5.0, 10.0]:
        mgr_f5.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="WAIT", obs_has_cards=False, has_unresolved_prompt=False)
    # LAST_BETS must NOT trigger refresh even when armed
    act_lb = mgr_f5.tick(10.5, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="LAST_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    assert not act_lb.trigger_refresh
    mgr_f5.tick(11.0, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    act_f5 = mgr_f5.tick(11.4, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_f5.trigger_refresh is True
    assert act_f5.preferred_method == "sendinput_f5"
    mgr_f5.mark_refresh_dispatched(11.4, True, "sendinput_f5")
    # After 1.2s with no reload interruption, escalates to sendinput_ctrl_r
    act_ctrl_r = mgr_f5.tick(12.7, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_ctrl_r.trigger_refresh is True
    assert act_ctrl_r.preferred_method == "sendinput_ctrl_r"
    mgr_f5.mark_refresh_dispatched(12.7, True, "sendinput_ctrl_r")
    # Subsequent check during RECOVERING with LAST_BETS visible (never blanked) must NOT falsely recover
    mgr_f5.tick(16.8, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="LAST_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    act_no_false_rec = mgr_f5.tick(17.0, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="LAST_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_no_false_rec.emit_alert is None
    assert mgr_f5.to_status(17.0)["state"] == "RECOVERING"

    # Default click_reload_then_f5 escalates cleanly: click_reload -> sendinput_f5 -> sendinput_ctrl_r
    ka_click = KeepaliveConfig(
        mode="auto",
        method="click_reload_then_f5",
        min_watch_sec=10.0,
        max_watch_sec=10.0,
        place_bets_min_delay_sec=0.35,
        place_bets_max_delay_sec=0.35,
        appcommand_verify_sec=1.0,
    )
    mgr_clk = WatchKeepaliveManager(ka_click, rng=random.Random(3))
    for t in [0.0, 5.0, 10.0]:
        mgr_clk.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="WAIT", obs_has_cards=False, has_unresolved_prompt=False)
    mgr_clk.tick(11.0, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    act_clk0 = mgr_clk.tick(11.4, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_clk0.trigger_refresh is True
    assert act_clk0.preferred_method == "click_reload"
    mgr_clk.mark_refresh_dispatched(11.4, True, "click_reload")
    act_clk1 = mgr_clk.tick(12.5, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_clk1.trigger_refresh is True
    assert act_clk1.preferred_method == "sendinput_f5"
    mgr_clk.mark_refresh_dispatched(12.5, True, "sendinput_f5")
    act_clk2 = mgr_clk.tick(13.6, play_mode="watch", paused=False, window_found=True, sm_state="BETTING", banner_state="PLACE_BETS", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_clk2.trigger_refresh is True
    assert act_clk2.preferred_method == "sendinput_ctrl_r"

    # Transition away and into a fresh PLACE_BETS edge -> arms 0.6s jitter delay and fires appcommand
    mgr.tick(
        28.0,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="DEALING",
        banner_state="NO_MORE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
    )
    act_pb0 = mgr.tick(
        29.0,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
    )
    assert not act_pb0.trigger_refresh

    act_pb1 = mgr.tick(
        29.7,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
    )
    assert act_pb1.trigger_refresh is True
    assert act_pb1.preferred_method == "appcommand"
    assert act_pb1.flush_auto_watch is True
    disp = mgr.mark_refresh_dispatched(29.7, True, "appcommand", epoch_ts=1000.0)
    assert disp["event"] == "refreshing"
    assert mgr.to_status(29.7)["inGrace"] is True

    # If no reload occurs within appcommand_verify_sec (0.9s), escalates to sendinput_f5 fallback
    act_fb = mgr.tick(
        30.7,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        reload_luma_shift=False,
        uniform_loading_screen=False,
    )
    assert act_fb.trigger_refresh is True
    assert act_fb.preferred_method == "sendinput_f5"
    mgr.mark_refresh_dispatched(30.7, True, "sendinput_f5", epoch_ts=1001.0)

    # Needs >= 2.0s blackout/reload elapsed before accepting recovery (test loading interruption first)
    act_early_rec = mgr.tick(
        31.5,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="IDLE",
        banner_state="",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        reload_luma_shift=True,
        uniform_loading_screen=True,
    )
    assert act_early_rec.emit_alert is None
    assert mgr.to_status(31.5)["state"] == "RECOVERING"

    # After >= 2.0s elapsed and interruption observed, 2 PLACE_BETS banner frames recover cleanly
    rec_alert = None
    for t_rec in [33.8, 34.0]:
        res = mgr.tick(
            t_rec,
            play_mode="watch",
            paused=False,
            window_found=True,
            sm_state="IDLE",
            banner_state="PLACE_BETS",
            obs_has_cards=False,
            has_unresolved_prompt=False,
            reload_luma_shift=False,
            uniform_loading_screen=False,
        )
        if res.emit_alert is not None:
            rec_alert = res.emit_alert
    assert rec_alert is not None and rec_alert["event"] == "recovered"
    st_done = mgr.to_status(34.1)
    assert st_done["state"] == "WATCH_COUNTDOWN"
    assert st_done["refreshCount"] == 1
    assert st_done["elapsedSec"] < 1.0

    # Timeout warning emits if reload stalls beyond reload_timeout_sec
    mgr.tick(49.5, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="WAIT", obs_has_cards=False, has_unresolved_prompt=False)
    mgr.arm_immediate_refresh(50.0)
    act_to = mgr.tick(
        50.1,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
    )
    assert act_to.trigger_refresh is True
    mgr.mark_refresh_dispatched(50.1, True, "sendinput_f5")
    act_to_warn = mgr.tick(
        71.0,
        play_mode="watch",
        paused=False,
        window_found=False,
        sm_state="IDLE",
        banner_state="",
        obs_has_cards=False,
        has_unresolved_prompt=False,
    )
    assert act_to_warn.emit_alert is not None and act_to_warn.emit_alert["event"] == "timeout_warn"


def test_watch_keepalive_hard_warn_shuffling_and_window_title_filter() -> None:
    import asyncio
    import json
    import random
    import websockets
    from bridge import CompanionBridge
    from config import KeepaliveConfig
    from state_machine import WatchKeepaliveManager
    from win_anchor import _match_window_title, verify_target_hwnd

    # Window matcher rejects our own tracker title while accepting Playtech casino titles
    titles = ["live games", "all bets blackjack", "epicworld", "playtech"]
    assert _match_window_title("All Bets Blackjack - Live Games - Brave", titles) == 0
    assert _match_window_title("Playtech Live Blackjack · Advantage & Wonging Tracker - Brave", titles) is None
    assert _match_window_title("README.md - Visual Studio Code", titles) is None
    assert verify_target_hwnd(0, titles, prefer_left=True) is False

    # Emergency SHUFFLING fallback when timer reaches hard_warn_sec
    ka_cfg = KeepaliveConfig(
        mode="auto",
        method="sendinput_f5",
        min_watch_sec=10.0,
        max_watch_sec=10.0,
        hard_warn_sec=20.0,
    )
    mgr = WatchKeepaliveManager(ka_cfg, rng=random.Random(5))
    for t in [0.0, 5.0, 10.0, 15.0, 20.0]:
        mgr.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="WAIT", obs_has_cards=False, has_unresolved_prompt=False)
    act_shuf = mgr.tick(21.0, play_mode="watch", paused=False, window_found=True, sm_state="SHUFFLING", banner_state="SHUFFLING", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_shuf.trigger_refresh is True
    assert act_shuf.preferred_method == "sendinput_f5"

    # Separate check: hard_warn alert emits once when crossing hard_warn_sec during active round
    mgr2 = WatchKeepaliveManager(ka_cfg, rng=random.Random(5))
    for t in [0.0, 5.0, 10.0, 15.0, 19.0]:
        mgr2.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="DEALING", banner_state="WAIT", obs_has_cards=True, has_unresolved_prompt=False)
    act_warn = mgr2.tick(21.0, play_mode="watch", paused=False, window_found=True, sm_state="DEALING", banner_state="WAIT", obs_has_cards=True, has_unresolved_prompt=False)
    assert act_warn.emit_alert is not None and act_warn.emit_alert["event"] == "hard_warn"

    # CompanionStateMachine tracks has_prompt and bet_rounds_count cleanly
    sm = CompanionStateMachine(CompanionConfig())
    sm.set_sync_state(play_mode="bet", has_prompt=True, bet_rounds_count=3)
    assert sm.sync_has_prompt is True
    assert sm.sync_bet_rounds_count == 3

    # CompanionBridge roundtrip for keepalive_action RPCs ('reset' and 'set_config')
    received: list[dict] = []

    def _on_msg(msg: dict) -> list[dict]:
        received.append(msg)
        if msg.get("type") == "keepalive_action":
            return [{"type": "keepalive_status", "event": "config_saved" if msg.get("action") == "set_config" else "reset"}]
        return []

    async def _run_bridge_check() -> list[dict]:
        bridge = CompanionBridge("127.0.0.1", 0, _on_msg)
        await bridge.start()
        try:
            sock = next(iter(bridge._server.sockets))
            port = sock.getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"type": "keepalive_action", "action": "reset"}))
                r1 = json.loads(await ws.recv())
                await ws.send(
                    json.dumps(
                        {
                            "type": "keepalive_action",
                            "action": "set_config",
                            "keepalive": {"mode": "alert_only", "min_watch_sec": 300, "max_watch_sec": 420},
                        }
                    )
                )
                r2 = json.loads(await ws.recv())
                return [r1, r2]
        finally:
            await bridge.stop()

    replies = asyncio.run(_run_bridge_check())
    assert [r["event"] for r in replies] == ["reset", "config_saved"]
    assert received[1]["keepalive"]["mode"] == "alert_only"


def test_separate_place_bets_roi_and_reload_button_match(tmp_path) -> None:
    import cv2
    import numpy as np
    from config import CompanionConfig
    from detector import PlaytechDetector
    from win_anchor import match_reload_button_in_toolbar

    tdir = tmp_path / "templates"
    bdir = tdir / "banner"
    bdir.mkdir(parents=True)

    # Create high-contrast synthetic LAST_BETS (top banner) & PLACE_BETS (center banner) templates
    lb_tpl = np.full((20, 76, 3), 20, dtype=np.uint8)
    cv2.putText(lb_tpl, "LAST BETS", (4, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 200, 60), 1, cv2.LINE_AA)
    cv2.imwrite(str(bdir / "LAST_BETS_1.png"), lb_tpl)

    pb_tpl = np.full((22, 118, 3), 22, dtype=np.uint8)
    cv2.putText(pb_tpl, "PLACE YOUR BETS", (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (245, 245, 245), 1, cv2.LINE_AA)
    cv2.imwrite(str(bdir / "PLACE_BETS_1.png"), pb_tpl)

    det = PlaytechDetector(CompanionConfig(), templates_root=tdir)
    empty_hud = np.full((60, 120, 3), 24, dtype=np.uint8)
    empty_shuf = np.full((40, 120, 3), 24, dtype=np.uint8)
    blank_top = np.full((40, 260, 3), 20, dtype=np.uint8)
    blank_pb = np.full((60, 260, 3), 22, dtype=np.uint8)

    pb_scene = blank_pb.copy()
    pb_scene[18:40, 65:183] = pb_tpl

    # 1. Center place_bets_roi matches PLACE_BETS cleanly while top banner_roi is empty
    obs_pb = det.process_crops(
        player_img=empty_hud,
        dealer_img=empty_hud,
        shuffle_img=empty_shuf,
        banner_img=blank_top,
        place_bets_img=pb_scene,
        banner_refreshed=True,
        ts=1.0,
    )
    assert obs_pb.banner_state == "PLACE_BETS"
    assert obs_pb.banner_source == "place_bets_roi"
    assert obs_pb.place_bets_conf >= 0.72

    # 2. Top LAST_BETS overrides simultaneous center PLACE_BETS so closing bets are never interrupted
    top_lb = blank_top.copy()
    top_lb[10:30, 90:166] = lb_tpl
    obs_lb = det.process_crops(
        player_img=empty_hud,
        dealer_img=empty_hud,
        shuffle_img=empty_shuf,
        banner_img=top_lb,
        place_bets_img=pb_scene,
        banner_refreshed=True,
        ts=2.0,
    )
    assert obs_lb.banner_state == "LAST_BETS"
    assert obs_lb.banner_source == "banner_roi"
    assert obs_lb.place_bets_conf >= 0.72

    # 3. Toolbar Reload (⟳) icon locator prefers offset_hint tie-breaker and rejects flat background
    toolbar = np.full((160, 420, 3), 38, dtype=np.uint8)
    btn = np.full((18, 18, 3), 45, dtype=np.uint8)
    cv2.circle(btn, (9, 9), 6, (215, 220, 230), 2, cv2.LINE_AA)
    cv2.line(btn, (12, 3), (15, 6), (215, 220, 230), 2, cv2.LINE_AA)
    # Place target near (138, 74) and identical distractor near (300, 74)
    toolbar[65:83, 129:147] = btn
    toolbar[65:83, 291:309] = btn

    hit = match_reload_button_in_toolbar(toolbar, [btn], offset_hint=[138, 74])
    assert hit is not None
    cx, cy, conf = hit
    assert abs(cx - 138) <= 2 and abs(cy - 74) <= 2
    assert conf > 0.90

    flat_toolbar = np.full((160, 420, 3), 38, dtype=np.uint8)
    assert match_reload_button_in_toolbar(flat_toolbar, [btn], offset_hint=[138, 74]) is None


def test_round_over_trigger_ext_bridge_and_escalation() -> None:
    import asyncio
    import json
    import pathlib
    import random
    import socket
    import websockets
    from bridge import CompanionBridge
    from config import KeepaliveConfig
    from state_machine import WatchKeepaliveManager

    # 1. Verify Brave MV3 extension manifest stays strictly stealthy (tabs + alarms only)
    manifest_path = pathlib.Path(__file__).resolve().parent.parent / "brave_keepalive_ext" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"tabs", "alarms"}
    assert "content_scripts" not in manifest
    assert "host_permissions" not in manifest
    assert "scripting" not in manifest["permissions"]

    # 2. ROUND_OVER trigger (~1.2s delay, works when pb=0.00 and banner=='') + ext_then_os escalation
    ka_cfg = KeepaliveConfig(
        mode="auto",
        method="ext_then_os",
        min_watch_sec=10.0,
        max_watch_sec=10.0,
        hard_warn_sec=30.0,
        round_over_delay_sec=1.2,
        place_bets_min_delay_sec=0.3,
        place_bets_max_delay_sec=0.3,
        place_bets_latest_sec=4.5,
        appcommand_verify_sec=1.0,
    )
    mgr = WatchKeepaliveManager(ka_cfg, rng=random.Random(1))
    for t in (0.0, 5.0, 10.5):
        mgr.tick(
            t,
            play_mode="watch",
            paused=False,
            window_found=True,
            sm_state="DEALER_TURN",
            banner_state="WAIT",
            obs_has_cards=True,
            has_unresolved_prompt=False,
        )
    assert mgr.to_status(10.5)["state"] == "ARMED_NEXT_BETS"

    # Must NEVER fire on LAST_BETS / WAIT / DEALING / PLAYER_TURN
    for sm_s, b_s in (("LAST_BETS", "LAST_BETS"), ("IDLE", "WAIT"), ("PLAYER_TURN", "WAIT")):
        act_bad = mgr.tick(
            10.7,
            play_mode="watch",
            paused=False,
            window_found=True,
            sm_state=sm_s,
            banner_state=b_s,
            obs_has_cards=False,
            has_unresolved_prompt=False,
        )
        assert not act_bad.trigger_refresh

    # Enter ROUND_OVER with banner='' and obs_has_cards=False at t=11.0 (pb=0.00 scenario)
    act_ro0 = mgr.tick(11.0, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert not act_ro0.trigger_refresh
    act_ro1 = mgr.tick(11.9, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert not act_ro1.trigger_refresh
    act_ro2 = mgr.tick(12.4, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_ro2.trigger_refresh is True
    assert act_ro2.preferred_method == "ext_reload"
    assert act_ro2.flush_auto_watch is True

    # Escalation order: ext_reload -> click_reload -> sendinput_f5 -> sendinput_ctrl_r
    mgr.mark_refresh_dispatched(12.4, True, "ext_reload", epoch_ts=2000.0)
    act_e1 = mgr.tick(13.5, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_e1.trigger_refresh is True
    assert act_e1.preferred_method == "click_reload"
    mgr.mark_refresh_dispatched(13.5, True, "click_reload", epoch_ts=2001.0)
    act_e2 = mgr.tick(14.6, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_e2.trigger_refresh is True
    assert act_e2.preferred_method == "sendinput_f5"
    mgr.mark_refresh_dispatched(14.6, True, "sendinput_f5", epoch_ts=2002.0)
    act_e3 = mgr.tick(15.7, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_e3.trigger_refresh is True
    assert act_e3.preferred_method == "sendinput_ctrl_r"

    # 3. Cutoff guard: refuses firing when ROUND_OVER window age exceeds max(place_bets_latest_sec, 6.5) before hard_warn_sec
    mgr_late = WatchKeepaliveManager(ka_cfg, rng=random.Random(2))
    for t in (0.0, 5.0, 10.5):
        mgr_late.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="DEALER_TURN", banner_state="WAIT", obs_has_cards=True, has_unresolved_prompt=False)
    mgr_late.tick(11.0, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=True)
    act_late = mgr_late.tick(18.0, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert not act_late.trigger_refresh

    # 4. Empty-banner ROUND_OVER recovery: no false-positive recovery on empty banner, clean recovery via notify_tab_event
    ka_rec = KeepaliveConfig(
        mode="auto",
        method="ext_then_os",
        min_watch_sec=10.0,
        max_watch_sec=10.0,
        hard_warn_sec=30.0,
        round_over_delay_sec=1.2,
        appcommand_verify_sec=5.0,
    )
    mgr_rec = WatchKeepaliveManager(ka_rec, rng=random.Random(4))
    for t in (0.0, 5.0, 10.5):
        mgr_rec.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="DEALER_TURN", banner_state="WAIT", obs_has_cards=True, has_unresolved_prompt=False)
    mgr_rec.tick(11.0, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    act_go = mgr_rec.tick(12.4, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_go.trigger_refresh is True and act_go.preferred_method == "ext_reload"
    mgr_rec.mark_refresh_dispatched(12.4, True, "ext_reload", epoch_ts=3000.0)
    # Without luma_shift / loading / tab_event, empty banner at 2.4s post-dispatch must NOT falsely recover
    act_still_rec = mgr_rec.tick(14.8, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_still_rec.emit_alert is None
    assert mgr_rec.to_status(14.8)["state"] == "RECOVERING"
    # Tab loading -> complete events settle and mark recovered cleanly
    mgr_rec.notify_tab_event("loading", now=14.9)
    mgr_rec.notify_tab_event("complete", now=15.2)
    act_done = mgr_rec.tick(15.3, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="WAIT", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_done.emit_alert is not None
    assert act_done.emit_alert["event"] == "recovered"
    assert mgr_rec.to_status(15.3)["elapsedSec"] == 0.0
    assert mgr_rec.to_status(15.3)["refreshCount"] == 1

    # 5. CompanionBridge accepts chrome-extension:// origin, skips ext clients on broadcast(), and completes ext_reload_tab RPC
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    ext_msgs: list[dict] = []

    async def _on_msg(data: dict) -> None:
        ext_msgs.append(data)

    async def _run_ext_bridge_check() -> tuple[bool, str, dict | None, dict]:
        bridge = CompanionBridge(host="127.0.0.1", port=free_port, on_message=_on_msg)
        await bridge.start()
        try:
            uri = f"ws://127.0.0.1:{free_port}"
            async with (
                websockets.connect(uri, origin="null") as ui_ws,
                websockets.connect(uri, origin="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") as ext_ws,
            ):
                await ext_ws.send(json.dumps({"type": "browser_ext_hello", "version": "1.0.0"}))
                ack = json.loads(await asyncio.wait_for(ext_ws.recv(), timeout=1.0))
                assert ack["type"] == "browser_ext_ack"
                assert bridge.has_extension_client is True

                await bridge.broadcast({"type": "status", "windowFound": True})
                ui_pkt = json.loads(await ui_ws.recv())
                assert ui_pkt["type"] == "status"

                rpc_task = asyncio.create_task(
                    bridge.request_extension_reload(
                        ["all bets", "live games"],
                        prefer_left=True,
                        bypass_cache=False,
                        timeout_sec=1.5,
                    )
                )
                # Extension client must receive ext_reload_tab next (no 'status' broadcast leaked to it)
                req = json.loads(await asyncio.wait_for(ext_ws.recv(), timeout=1.0))
                assert req["type"] == "ext_reload_tab"
                assert req["preferLeft"] is True
                await ext_ws.send(
                    json.dumps(
                        {
                            "type": "ext_reload_ack",
                            "requestId": req["requestId"],
                            "ok": True,
                            "reason": "reloaded",
                            "tabId": 42,
                            "windowId": 7,
                            "title": "Live Games",
                            "url": "https://example.invalid/game",
                        }
                    )
                )
                ok, reason, meta = await rpc_task
                return ok, reason, meta, ui_pkt
        finally:
            await bridge.stop()

    ok, method, meta, _ = asyncio.run(_run_ext_bridge_check())
    assert ok is True
    assert method == "ext_reload"
    assert meta is not None and meta["tabId"] == 42 and meta["reason"] == "reloaded"
    assert any(m.get("type") == "browser_ext_hello" for m in ext_msgs)


def test_ipados_parity_badge_deduction_educated_guess_split_watch_and_warn_recovery(tmp_path) -> None:
    import cv2
    import numpy as np
    import random
    from calibrate import save_template_crop
    from config import CompanionConfig, KeepaliveConfig
    from detector import PlaytechDetector, _has_red_back_signature, detect_dealer_badge_total
    from state_machine import (
        CompanionStateMachine,
        WatchKeepaliveManager,
        deduce_missing_dealer_card,
        deduce_missing_dealer_pair,
        guess_missing_dealer_card,
    )

    # 1. Pure rank deduction & educated guess helpers
    assert deduce_missing_dealer_card(["7", "BACK"], 17) == {"rank": "10", "val": 0}
    assert deduce_missing_dealer_card(["6", "BACK"], 17) == {"rank": "A", "val": 1}
    assert deduce_missing_dealer_pair(["5"], 21) == [{"rank": "6", "val": 6}, {"rank": "10", "val": 0}]
    assert deduce_missing_dealer_pair(["2"], 17) is not None
    assert guess_missing_dealer_card(["7", "5"]) == {"rank": "10", "val": 0}
    assert guess_missing_dealer_card(["6"]) == {"rank": "A", "val": 1}

    # 2. Multi-step dealer badge history & 2-card pair deduction + badge detector sanity
    cfg = CompanionConfig(confirm_frames=2, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")
    p_hand = [_det("10", 0, 10), _det("8", 8, 30)]
    _feed_hud(sm, p_hand, [_det("5", 5, 10), _det("BACK", None, 35)], ts=1.0, frames=2, banner="WAIT")
    # Badge steps: 11 (flashed '6' before visible) -> 21 (flashed '10' before visible), then table wipes
    sm.step(FrameObservation(ts=2.0, banner_state="WAIT", player_cards=p_hand, dealer_cards=[_det("5", 5, 10)], dealer_badge_total=11, dealer_badge_conf=0.88))
    sm.step(FrameObservation(ts=2.2, banner_state="WAIT", player_cards=p_hand, dealer_cards=[_det("5", 5, 10)], dealer_badge_total=21, dealer_badge_conf=0.86))
    end_badge = sm.step(FrameObservation(ts=3.0, banner_state="PLACE_BETS"))
    ded_cards = [(e["slot"], e["rankLabel"], e.get("source")) for e in end_badge if e["type"] == "card" and e["target"] == "D"]
    assert ded_cards == [(1, "6", "badge_deduced"), (2, "10", "badge_deduced")]
    assert sm._pending_auto_watch is not None

    # Empty dark badge area returns None
    assert detect_dealer_badge_total(np.zeros((45, 100, 3), dtype=np.uint8)) == (None, 0.0)

    # 3. Multi-card educated guess loop reaches >= 17 & auto-logs at NO_MORE_BETS
    sm_g = CompanionStateMachine(CompanionConfig(confirm_frames=1, clear_frames=2))
    sm_g.set_sync_state(play_mode="watch")
    _feed_hud(sm_g, [_det("10", 0, 10), _det("9", 9, 30)], [_det("2", 2, 10), _det("3", 3, 28)], ts=10.0, frames=1, banner="WAIT")
    end_g = sm_g.step(FrameObservation(ts=11.0, banner_state="PLACE_BETS"))
    g_cards = [e for e in end_g if e["type"] == "card" and e["target"] == "D" and e.get("source") == "educated_guess"]
    assert len(g_cards) == 2
    p_g = [e for e in end_g if e["type"] == "prompt_confirm"]
    assert len(p_g) == 1 and p_g[0]["dealerTotal"] >= 17
    nmb_g = sm_g.step(FrameObservation(ts=12.0, banner_state="NO_MORE_BETS"))
    assert any(e["type"] == "auto_watch" and e.get("fromWatch") is True for e in nmb_g)

    # 4. Watch Split Guard auto-logs at NO_MORE_BETS
    sm_sp = CompanionStateMachine(CompanionConfig(confirm_frames=2, clear_frames=2))
    sm_sp.set_sync_state(play_mode="watch", is_split=True)
    _feed_hud(sm_sp, [_det("8", 8, 10), _det("3", 3, 28)], [_det("10", 0, 10), _det("7", 7, 28)], ts=20.0, frames=2, banner="WAIT")
    end_sp = sm_sp.step(FrameObservation(ts=21.0, banner_state="PLACE_BETS"))
    assert any(e["type"] == "prompt_confirm" and e["reason"] == "SPLIT" for e in end_sp)
    assert sm_sp._pending_auto_watch is not None
    nmb_sp = sm_sp.step(FrameObservation(ts=22.0, banner_state="NO_MORE_BETS"))
    assert any(e["type"] == "auto_watch" and e.get("fromWatch") is True for e in nmb_sp)

    # 5. Red face-up card BACK rejection + false-positive '4' rejection
    white_red_card = np.full((30, 24, 3), (245, 245, 245), dtype=np.uint8)
    white_red_card[4:16, 4:14] = (30, 30, 220)  # ~28% red ink on mostly white card face
    assert _has_red_back_signature(white_red_card) is False

    tdir = tmp_path / "tpl_4"
    im4 = np.full((45, 22, 3), 248, dtype=np.uint8)
    cv2.putText(im4, "4", (2, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20), 2, cv2.LINE_AA)
    save_template_crop("hud", "4", im4, tdir, auto_trim=True)
    im_s = np.full((18, 18, 3), 248, dtype=np.uint8)
    cv2.circle(im_s, (9, 9), 5, (20, 20, 20), -1)
    save_template_crop("suit", "S", im_s, tdir, auto_trim=True)
    det4 = PlaytechDetector(CompanionConfig(min_hud_confidence=0.84), templates_root=tdir)
    # Noisy low-contrast patch that vaguely looks like '4' (~0.75 < 0.82) must be rejected
    noisy_strip = np.full((55, 60, 3), 24, dtype=np.uint8)
    noisy_strip[5:50, 12:36] = 248
    cv2.putText(noisy_strip, "4", (14, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (105, 105, 105), 1, cv2.LINE_AA)
    cv2.circle(noisy_strip, (24, 38), 5, (20, 20, 20), -1)
    p4, _, _ = det4._detect_hud_strip(noisy_strip, is_dealer=False)
    assert all(d.rank != "4" or d.conf >= 0.82 for d in p4)

    # 6. WatchKeepaliveManager WARN 90s cooldown + strips_visible_fallback recovery
    ka_cfg = KeepaliveConfig(mode="auto", method="ext_then_os", min_watch_sec=10.0, max_watch_sec=10.0, reload_timeout_sec=12.0, round_over_delay_sec=0.5)
    mgr = WatchKeepaliveManager(ka_cfg, rng=random.Random(9))
    for t in (0.0, 5.0, 10.0):
        mgr.tick(t, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    mgr.tick(10.5, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    act_fire = mgr.tick(11.2, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_fire.trigger_refresh is True
    mgr.mark_refresh_dispatched(11.2, True, "ext_reload")
    mgr.notify_tab_event("loading", now=12.0)
    # Timeout at +12.8s enters WARN with 90s cooldown
    act_to = mgr.tick(24.0, play_mode="watch", paused=False, window_found=True, sm_state="IDLE", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert act_to.emit_alert and act_to.emit_alert["event"] == "timeout_warn"
    assert mgr.state == "WARN"
    # Even when ROUND_OVER occurs 5s later while in WARN cooldown, must NOT re-trigger reload
    act_no_re = mgr.tick(29.0, play_mode="watch", paused=False, window_found=True, sm_state="ROUND_OVER", banner_state="", obs_has_cards=False, has_unresolved_prompt=False)
    assert not act_no_re.trigger_refresh
    # When cards return while in WARN with _pending_reload_recovery, recovers cleanly via strips_visible_fallback
    act_rec_strips = mgr.tick(30.0, play_mode="watch", paused=False, window_found=True, sm_state="DEALING", banner_state="", obs_has_cards=True, has_unresolved_prompt=False)
    assert act_rec_strips.emit_alert and act_rec_strips.emit_alert["event"] == "recovered"
    assert act_rec_strips.emit_alert["reason"] == "strips_visible_fallback"
    assert mgr.state == "WATCH_COUNTDOWN"


def test_educated_guess_all_ranks_and_signals() -> None:
    from state_machine import guess_missing_dealer_card

    # 1. Signal 1: Sub-threshold template runner-up peak + narrow card width (rem_right=13)
    g1 = guess_missing_dealer_card(
        ["10"],
        {
            "sub_candidates": [
                {"rank": "10", "val": 0, "conf": 0.56, "slotIdx": 1, "rem_right": 13, "has_pip_body": True},
                {"rank": "8", "val": 8, "conf": 0.55, "slotIdx": 1, "rem_right": 13, "has_pip_body": True},
            ]
        },
    )
    assert g1 == {"rank": "8", "val": 8}

    # 2. Signal 2: Partial/soft dealer score badge hints (tensDigit=1, onesDigit=8)
    g2 = guess_missing_dealer_card(
        ["10", "2"],
        {"badge_hint": {"total": None, "conf": 0.62, "tensDigit": 1, "tensConf": 0.72, "onesDigit": 8, "onesConf": 0.65}},
    )
    assert g2 == {"rank": "6", "val": 6}

    # 3. Signal 3: Physical felt sightings
    g3 = guess_missing_dealer_card(
        ["10", "3"],
        {"felt_sightings": [{"rank": "5", "val": 5, "conf": 0.74}]},
    )
    assert g3 == {"rank": "5", "val": 5}

    # 4 & 5. Signal 4 & 5: Live shoe depletion + stratified round_seed across all 10 ranks
    full_shoe = [128, 32, 32, 32, 32, 32, 32, 32, 32, 32]
    seen_ranks = set()
    for seed in range(1, 61):
        g = guess_missing_dealer_card(["10", "6"], {"shoe_rem": full_shoe, "round_seed": seed})
        assert g is not None
        seen_ranks.add(g["rank"])
    for r in ("10", "A", "2", "3", "4", "5", "6", "7", "8", "9"):
        assert r in seen_ranks, f"Missing rank {r} from stratified shoe sampling: {seen_ranks}"

    # Shoe depletion: only 4s remaining
    only_fours = [0, 0, 0, 0, 12, 0, 0, 0, 0, 0]
    g_dep = guess_missing_dealer_card(["10", "3"], {"shoe_rem": only_fours, "round_seed": 1})
    assert g_dep == {"rank": "4", "val": 4}

    # End-to-end CompanionStateMachine Fallback E with dealer_sub_cards
    cfg = CompanionConfig(confirm_frames=1, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")
    sm.step(
        FrameObservation(
            ts=1.0,
            banner_state="NO_MORE_BETS",
            player_cards=[],
            dealer_cards=[],
            player_hands={},
        )
    )
    sm.step(
        FrameObservation(
            ts=2.0,
            banner_state="WAIT",
            player_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)],
            dealer_cards=[CardDetection("9", 9, 0.94, 10), CardDetection("BACK", None, 0.92, 30)],
            player_hands={0: [CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)]},
            dealer_sub_cards=[{"rank": "8", "val": 8, "conf": 0.59, "suit": "H", "has_pip_body": True, "slotIdx": 1, "rem_right": 13}],
        )
    )
    end_evs = sm.step(
        FrameObservation(
            ts=3.0,
            banner_state="PLACE_BETS",
            player_cards=[],
            dealer_cards=[],
            player_hands={},
        )
    )
    guess_ev = next((e for e in end_evs if e.get("type") == "card" and e.get("target") == "D" and e.get("source") == "educated_guess"), None)
    assert guess_ev is not None
    assert guess_ev["rankLabel"] == "8"
    assert guess_ev["val"] == 8

def test_dimmed_split_hand_and_13_rank_badge_occlusion() -> None:
    """Dimmed inactive split hands, circular score-badge occlusion across all 13 ranks, and top-right suit fallback work cleanly."""
    from config import load_config
    from detector import PlaytechDetector, _detect_cluster_cards, _find_white_card_clusters

    det = PlaytechDetector(load_config())
    rank_tpls = det.hud_bank.card_templates
    suit_tpls = det.suit_bank.suit_templates
    assert len(rank_tpls) >= 13 and len(suit_tpls) >= 4

    def _find_tpl(bank: list, rank: str) -> np.ndarray:
        for t in bank:
            if t.rank == rank:
                return t.bgr
        raise AssertionError(f"Missing template {rank}")

    def _make_card_h45(rank: str, suit: str, with_badge: bool = False, top_right_suit: bool = False) -> np.ndarray:
        cw = 37 if top_right_suit else 31
        card = np.full((45, cw, 3), 252, dtype=np.uint8)
        rt = _find_tpl(rank_tpls, rank)
        rh, rw = rt.shape[:2]
        card[2 : 2 + min(rh, 20), 2 : 2 + min(rw, 17)] = rt[: min(rh, 20), : min(rw, 17)]
        st = _find_tpl(suit_tpls, suit)
        sh, sw = st.shape[:2]
        if suit in ("H", "D"):
            # Tint dark ink red for red suits
            st_col = st.copy()
            dark = st_col.min(axis=2) < 160
            st_col[dark] = (40, 40, 210)
        else:
            st_col = st
        card[26 : 26 + min(sh, 18), 3 : 3 + min(sw, 15)] = st_col[: min(sh, 18), : min(sw, 15)]
        if top_right_suit:
            tr = cv2.rotate(st_col[: min(sh, 17), : min(sw, 15)], cv2.ROTATE_180)
            th, tw = tr.shape[:2]
            card[2 : 2 + th, cw - tw - 2 : cw - 2] = tr
        if with_badge:
            cv2.circle(card, (-1, 33), 11, (18, 22, 18), -1)
        return card

    # 1. Dimmed split hand + badge occlusion + top-right suit fallback (scaled to H=55)
    roi45 = np.full((60, 220, 3), (18, 52, 22), dtype=np.uint8)
    left_qc = _make_card_h45("Q", "C", with_badge=True, top_right_suit=True)
    right_10c = _make_card_h45("10", "C", with_badge=True)
    right_3d = _make_card_h45("3", "D", with_badge=False, top_right_suit=True)
    roi45[7:52, 16 : 16 + left_qc.shape[1]] = left_qc
    roi45[7:52, 100:131] = right_10c
    roi45[7:52, 131 : 131 + right_3d.shape[1]] = right_3d
    # Dim right split hand to 50%
    roi45[:, 90:] = (roi45[:, 90:].astype(np.float32) * 0.5).astype(np.uint8)
    roi55 = cv2.resize(roi45, (int(round(220 * 55 / 45)), int(round(60 * 55 / 45))), interpolation=cv2.INTER_CUBIC)

    # Dealer pass (allow_dimmed=False) must ignore the dimmed right hand
    dealer_cls = _find_white_card_clusters(roi55, allow_dimmed=False)
    assert len(dealer_cls) == 1

    # Player pass (allow_dimmed=True) must detect BOTH hands accurately and emit no spurious '4' on right edge
    player_cls = _find_white_card_clusters(roi55, allow_dimmed=True)
    assert len(player_cls) == 2
    hands = []
    for cl in player_cls:
        cards, _ = _detect_cluster_cards(roi55, cl, rank_tpls, suit_tpls, 0.78, is_dealer=False, min_hud_conf=0.84)
        hands.append([(c.rank, c.suit) for c in cards])
    assert hands[0] == [("Q", "C")], f"Expected [('Q', 'C')], got {hands[0]}"
    assert hands[1] == [("10", "C"), ("3", "D")], f"Expected [('10', 'C'), ('3', 'D')], got {hands[1]}"

    # 2. 13-rank badge-occlusion matrix (every rank A..K under bottom-left badge occlusion)
    for rank in ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"):
        r45 = np.full((60, 100, 3), (18, 52, 22), dtype=np.uint8)
        r45[7:52, 14:45] = _make_card_h45(rank, "S", with_badge=True)
        r45[7:52, 45:76] = _make_card_h45("3", "D", with_badge=False)
        r55 = cv2.resize(r45, (int(round(100 * 55 / 45)), int(round(60 * 55 / 45))), interpolation=cv2.INTER_CUBIC)
        cls = _find_white_card_clusters(r55, allow_dimmed=True)
        assert len(cls) == 1, f"Rank {rank}: expected 1 cluster, got {cls}"
        cards, _ = _detect_cluster_cards(r55, cls[0], rank_tpls, suit_tpls, 0.78, is_dealer=False, min_hud_conf=0.84)
        assert len(cards) >= 2 and cards[0].rank == rank, f"Rank {rank} failed under badge occlusion: {[(c.rank, c.suit, round(c.conf, 3)) for c in cards]}"




def test_place_bets_strict_dealer_recheck_start_and_3s() -> None:
    """PLACE_BETS rechecks dealer completeness at 0s and +3s, supports >2 guesses, and respects manual removal."""
    from config import CompanionConfig
    from detector import CardDetection, FrameObservation
    from state_machine import CompanionStateMachine

    def _bj_total(ranks: list[str]) -> int:
        tot = 0
        aces = 0
        for r in ranks:
            if r == "A":
                tot += 11
                aces += 1
            elif r in ("10", "J", "Q", "K"):
                tot += 10
            else:
                tot += int(r)
        while tot > 21 and aces > 0:
            tot -= 10
            aces -= 1
        return tot

    # 1. Start of PLACE_BETS (0s) completes [10, BACK] when player has [10, 9]
    cfg = CompanionConfig(confirm_frames=1, clear_frames=2)
    sm = CompanionStateMachine(cfg)
    sm.set_sync_state(play_mode="watch")
    sm.step(FrameObservation(ts=1.0, banner_state="NO_MORE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    sm.step(
        FrameObservation(
            ts=2.0,
            banner_state="WAIT",
            player_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)],
            dealer_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("BACK", None, 0.92, 30)],
            player_hands={0: [CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)]},
        )
    )
    evs_0s = sm.step(FrameObservation(ts=3.0, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    guesses_0s = [e for e in evs_0s if e.get("type") == "card" and e.get("target") == "D" and e.get("source") == "educated_guess"]
    assert len(guesses_0s) >= 1

    # 2. +3s recheck catches sync_state drift (e.g. dealer=["2"] -> completes >= 17 with >2 guesses if needed)
    sm2 = CompanionStateMachine(cfg)
    sm2.set_sync_state(play_mode="watch")
    sm2.step(FrameObservation(ts=1.0, banner_state="NO_MORE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    sm2.step(
        FrameObservation(
            ts=2.0,
            banner_state="WAIT",
            player_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)],
            dealer_cards=[CardDetection("2", 2, 0.94, 10), CardDetection("3", 3, 0.94, 30)],
            player_hands={0: [CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)]},
        )
    )
    evs_pb0 = sm2.step(FrameObservation(ts=3.0, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    # 0s already completes [2, 3] to >= 17
    guesses_pb0 = [e for e in evs_pb0 if e.get("type") == "card" and e.get("target") == "D" and e.get("source") == "educated_guess"]
    assert len(guesses_pb0) >= 2
    assert _bj_total(["2", "3"] + [g["rankLabel"] for g in guesses_pb0]) >= 17

    # Simulate client drift during PLACE_BETS where draft only has ["2"] at +1s, then reaches +3s
    sm2.set_sync_state(
        play_mode="watch",
        draft_count=3,
        draft_hands=[["10", "9"]],
        draft_dealer=["2"],
        dealer_guess_removed=False,
    )
    evs_pb1 = sm2.step(FrameObservation(ts=4.5, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    assert not any(e.get("type") == "card" and e.get("source") == "educated_guess" for e in evs_pb1)
    evs_pb3 = sm2.step(FrameObservation(ts=6.1, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    guesses_pb3 = [e for e in evs_pb3 if e.get("type") == "card" and e.get("target") == "D" and e.get("source") == "educated_guess"]
    assert len(guesses_pb3) >= 2
    assert _bj_total(["2"] + [g["rankLabel"] for g in guesses_pb3]) >= 17

    # 3. dealer_guess_removed=True prevents +3s recheck from re-inserting guessed cards
    sm3 = CompanionStateMachine(cfg)
    sm3.set_sync_state(play_mode="watch")
    sm3.step(FrameObservation(ts=1.0, banner_state="NO_MORE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    sm3.step(
        FrameObservation(
            ts=2.0,
            banner_state="WAIT",
            player_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)],
            dealer_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("6", 6, 0.94, 30)],
            player_hands={0: [CardDetection("10", 0, 0.94, 10), CardDetection("9", 9, 0.94, 30)]},
        )
    )
    sm3.step(FrameObservation(ts=3.0, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    sm3.set_sync_state(
        play_mode="watch",
        draft_count=4,
        draft_hands=[["10", "9"]],
        draft_dealer=["10", "6"],
        dealer_guess_removed=True,
    )
    evs_pb3_removed = sm3.step(FrameObservation(ts=6.2, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    assert not any(e.get("type") == "card" and e.get("source") == "educated_guess" for e in evs_pb3_removed)

    # 4. BET mode preserves _review_* snapshot AND fires +3s recheck even when banner flickers blank ("")
    sm4 = CompanionStateMachine(cfg)
    sm4.set_sync_state(play_mode="bet")
    sm4.step(FrameObservation(ts=1.0, banner_state="NO_MORE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    sm4.step(
        FrameObservation(
            ts=2.0,
            banner_state="WAIT",
            player_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("8", 8, 0.94, 30)],
            dealer_cards=[CardDetection("6", 6, 0.94, 10), CardDetection("10", 0, 0.94, 30)],
            player_hands={0: [CardDetection("10", 0, 0.94, 10), CardDetection("8", 8, 0.94, 30)]},
        )
    )
    sm4.step(FrameObservation(ts=3.0, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    sm4.set_sync_state(
        play_mode="bet",
        draft_count=4,
        draft_hands=[[0, 8]],
        draft_dealer=[6, 0],
        dealer_guess_removed=False,
    )
    evs_bet_3s = sm4.step(FrameObservation(ts=6.1, banner_state="", player_cards=[], dealer_cards=[], player_hands={}))
    bet_guesses = [e for e in evs_bet_3s if e.get("type") == "card" and e.get("target") == "D" and e.get("source") == "educated_guess"]
    assert len(bet_guesses) >= 1
    assert _bj_total(["6", "10"] + [g["rankLabel"] for g in bet_guesses]) >= 17

    # 5. Busted community player hand [10, 6, 8] still completes dealer [6, 10]
    sm5 = CompanionStateMachine(cfg)
    sm5.set_sync_state(play_mode="watch")
    sm5.step(FrameObservation(ts=1.0, banner_state="NO_MORE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    sm5.step(
        FrameObservation(
            ts=2.0,
            banner_state="WAIT",
            player_cards=[CardDetection("10", 0, 0.94, 10), CardDetection("6", 6, 0.94, 30), CardDetection("8", 8, 0.94, 50)],
            dealer_cards=[CardDetection("6", 6, 0.94, 10), CardDetection("10", 0, 0.94, 30)],
            player_hands={0: [CardDetection("10", 0, 0.94, 10), CardDetection("6", 6, 0.94, 30), CardDetection("8", 8, 0.94, 50)]},
        )
    )
    evs_bust = sm5.step(FrameObservation(ts=3.0, banner_state="PLACE_BETS", player_cards=[], dealer_cards=[], player_hands={}))
    assert any(e.get("type") == "card" and e.get("target") == "D" and e.get("source") == "educated_guess" for e in evs_bust)


def test_delete_rank_templates_rejects_path_traversal_and_globbing(tmp_path) -> None:
    """delete_rank_templates must reject invalid banks, glob metacharacters, and path traversal tokens."""
    import pytest
    from calibrate import delete_rank_templates

    tpl_root = tmp_path / "templates"
    hud_dir = tpl_root / "hud"
    hud_dir.mkdir(parents=True)
    sentinel = hud_dir / "A_1.png"
    sentinel.write_bytes(b"\x89PNG\r\n\x1a\n")

    for bad_bank, bad_rank in [
        ("hud", "../hud"),
        ("hud", "*"),
        ("hud", "A/../../etc"),
        ("hud", ".."),
        ("../etc", "A"),
        ("unknown_bank", "A"),
    ]:
        with pytest.raises(ValueError):
            delete_rank_templates(bad_bank, bad_rank, templates_root=tpl_root)

    assert sentinel.exists(), "Sentinel template must not be deleted by rejected traversal attempts"


def test_save_config_atomic_path_preservation_and_validation(tmp_path) -> None:
    """load_config/save_config preserve custom _config_path atomically and clamp invalid ws_port & place_bets delays."""
    from config import CompanionConfig, load_config, save_config

    custom_path = tmp_path / "custom_companion.json"
    cfg = CompanionConfig(ws_port=0)
    cfg.keepalive.place_bets_min_delay_sec = 4.0
    cfg.keepalive.place_bets_max_delay_sec = 1.5
    save_config(cfg, path=custom_path)
    assert custom_path.exists()

    loaded = load_config(path=custom_path)
    assert loaded.ws_port == 8765
    assert loaded.keepalive.place_bets_max_delay_sec >= loaded.keepalive.place_bets_min_delay_sec
    assert getattr(loaded, "_config_path", None) == custom_path

    loaded.ws_port = 99999
    loaded.confirm_frames = 5
    # Saving without explicit path must write back to _config_path rather than default config.json
    save_config(loaded)
    reloaded = load_config(path=custom_path)
    assert reloaded.ws_port == 8765
    assert reloaded.confirm_frames == 5


def test_import_companion_pack_rejects_corrupt_png_without_wiping_bank(tmp_path) -> None:
    """import_companion_pack must pre-validate PNGs before wiping existing templates in the target bank."""
    import base64
    from calibrate import import_companion_pack
    from config import CompanionConfig

    tpl_root = tmp_path / "templates"
    hud_dir = tpl_root / "hud"
    hud_dir.mkdir(parents=True)
    existing = hud_dir / "A_1.png"
    existing.write_bytes(b"\x89PNG\r\n\x1a\nEXISTING")

    bad_pack = {
        "templates": {
            "hud": [
                {"name": "K_1.png", "dataB64": base64.b64encode(b"NOT_A_PNG_FILE").decode("ascii")},
            ]
        }
    }
    _, imported_count = import_companion_pack(bad_pack, CompanionConfig(), templates_root=tpl_root, replace_existing=True)
    assert imported_count == 0
    assert existing.exists(), "Existing bank templates must not be wiped when incoming pack has no valid PNGs"


def test_soft_hand_dealer_guess_and_keepalive_user_idle_guard() -> None:
    """guess_missing_dealer_card allows soft-hand intermediate draws and WatchKeepaliveManager defers OS reload while user is active."""
    from config import CompanionConfig
    from detector import FrameObservation
    from state_machine import WatchKeepaliveManager, guess_missing_dealer_card

    only_tens = [16, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    g10 = guess_missing_dealer_card(["A", "4"], {"shoe_rem": only_tens, "round_seed": 101})
    assert g10 is not None and g10["rank"] == "10"
    assert only_tens[0] == 16

    g_a = guess_missing_dealer_card(["6"], {"round_seed": 777})
    g_b = guess_missing_dealer_card(["6"], {"round_seed": 777})
    assert g_a == g_b

    cfg = CompanionConfig()
    cfg.keepalive.enabled = True
    cfg.keepalive.mode = "auto"
    cfg.keepalive.method = "sendinput_ctrl_r"
    cfg.keepalive.place_bets_min_delay_sec = 0.2
    cfg.keepalive.place_bets_max_delay_sec = 0.25
    ka = WatchKeepaliveManager(cfg.keepalive)
    ka.watch_elapsed_sec = ka.target_sec + 1.0
    # First tick enters PLACE_BETS window and schedules fire_ts
    act0 = ka.tick(
        100.0,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        user_idle_ms=200,
    )
    assert act0.trigger_refresh is False
    # Past scheduled fire_ts, but user_idle_ms=200 < 1200 -> must defer OS-level sendinput_ctrl_r reload
    act_active = ka.tick(
        101.0,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        user_idle_ms=200,
    )
    assert act_active.trigger_refresh is False, "OS-level reload must be deferred while user_idle_ms < 1200"

    # Once user is idle (user_idle_ms=2000 >= 1200), OS-level reload proceeds
    act_idle = ka.tick(
        101.5,
        play_mode="watch",
        paused=False,
        window_found=True,
        sm_state="BETTING",
        banner_state="PLACE_BETS",
        obs_has_cards=False,
        has_unresolved_prompt=False,
        user_idle_ms=2000,
    )
    assert act_idle.trigger_refresh is True
    assert act_idle.preferred_method == "sendinput_ctrl_r"


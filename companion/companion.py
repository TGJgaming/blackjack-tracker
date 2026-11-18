"""Main runner for the Newport / Playtech All Bets desktop companion."""

from __future__ import annotations

import argparse
import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from bridge import CompanionBridge
from calibrate import clear_bank_templates, delete_rank_templates, list_template_counts, save_template_crop
from config import COMPANION_DIR, CompanionConfig, KeepaliveConfig, load_config, normalize_roi, save_config
from detector import FrameObservation, PlaytechDetector
from state_machine import CompanionStateMachine, WatchKeepaliveManager
from win_anchor import (
    ViewportSmoother,
    WindowCapturer,
    WindowRect,
    detect_game_viewport_local,
    disable_high_res_timer,
    enable_high_res_timer,
    find_target_window,
    get_primary_work_area,
    get_user_idle_ms,
    match_reload_button_in_toolbar,
    trigger_browser_refresh,
)


def _crop_norm(img: np.ndarray, norm_roi: tuple[float, float, float, float]) -> np.ndarray:
    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return np.zeros((0, 0, 3), dtype=np.uint8)
    rx, ry, rw, rh = norm_roi
    x0 = max(0, min(w - 2, int(round(rx * w))))
    y0 = max(0, min(h - 2, int(round(ry * h))))
    x1 = max(x0 + 2, min(w, int(round((rx + rw) * w))))
    y1 = max(y0 + 2, min(h, int(round((ry + rh) * h))))
    return img[y0:y1, x0:x1]


def _draw_preview(
    win_img: np.ndarray,
    vp_local: tuple[int, int, int, int],
    cfg: CompanionConfig,
    obs: FrameObservation,
    sm: CompanionStateMachine,
    fps_actual: float,
) -> bool:
    """Render annotated debug preview fitted to screen; returns False on Q/ESC."""
    canvas = win_img.copy()
    fh, fw = canvas.shape[:2]
    vx, vy, vw, vh = vp_local

    # Highlight detected Playtech 16:9 game viewport inside Brave
    if cfg.anchor_mode == "game_viewport":
        cv2.rectangle(canvas, (vx, vy), (vx + vw - 1, vy + vh - 1), (255, 255, 255), 1)
        ax, ay, aw, ah = vx, vy, vw, vh
    else:
        ax, ay, aw, ah = 0, 0, fw, fh

    colors = {
        "player_strip": (60, 220, 90),
        "dealer_strip": (80, 175, 255),
        "community_roi": (255, 210, 40),
        "dealer_felt_roi": (94, 63, 244),
        "shuffle_roi": (210, 110, 255),
        "banner_roi": (235, 120, 210),
    }
    for name, (rx, ry, rw, rh) in cfg.rois.items():
        x0 = ax + int(round(rx * aw))
        y0 = ay + int(round(ry * ah))
        x1 = ax + int(round((rx + rw) * aw))
        y1 = ay + int(round((ry + rh) * ah))
        col = colors.get(name, (200, 200, 200))
        cv2.rectangle(canvas, (x0, y0), (x1, y1), col, 2)
        cv2.putText(canvas, name, (x0 + 4, max(16, y0 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)

    if len(obs.player_hands) >= 2:
        p_str = " | ".join(
            f"H{h + 1}:[" + " ".join(f"{c.rank}{c.suit or ''}({c.conf:.2f})" for c in cs) + "]"
            for h, cs in sorted(obs.player_hands.items())
        )
    else:
        p_str = " ".join(f"{c.rank}{c.suit or ''}({c.conf:.2f})" for c in obs.player_cards) or "-"
    d_str = " ".join(f"{c.rank}{c.suit or ''}({c.conf:.2f})" for c in obs.dealer_cards) or "-"
    banner_str = obs.banner_state or "-"
    felt_str = "ON" if sm.felt_tracking_armed else "OFF"
    hud_line = f"State:{sm.state} Banner:[{banner_str}] Mode:{sm.sync_play_mode.upper()} Felt:{felt_str} FPS:{fps_actual:.1f} | P:[{p_str}] D:[{d_str}]"

    # Stack telemetry bar ABOVE frame so top pixels are never covered
    bar = np.full((28, fw, 3), (18, 22, 28), dtype=np.uint8)
    cv2.putText(bar, hud_line[:125], (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (230, 240, 250), 1)
    combined = np.vstack([bar, canvas])

    sw, sh = get_primary_work_area()
    ch, cw = combined.shape[:2]
    scale = min(1.0, (sw * 0.48) / max(1, cw), (sh * 0.80) / max(1, ch))
    disp_w, disp_h = max(320, int(round(cw * scale))), max(240, int(round(ch * scale)))

    win_name = "Playtech Companion Preview (Q=quit)"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win_name, disp_w, disp_h)
    cv2.imshow(win_name, combined)
    key = cv2.waitKey(1) & 0xFF
    return key not in (ord("q"), 27)


async def run_companion(cfg: CompanionConfig, preview: bool = False) -> None:
    detector = PlaytechDetector(cfg)
    sm = CompanionStateMachine(cfg)
    keepalive = WatchKeepaliveManager(cfg.keepalive)
    capturer = WindowCapturer()
    vp_smoother = ViewportSmoother(tol_px=8)

    win_cache: WindowRect | None = None
    anchor_cache: WindowRect | None = None
    vp_local_cache: tuple[int, int, int, int] = (0, 0, 1, 1)
    last_win_scan = 0.0
    last_vp_scan = 0.0
    last_hud_ts = 0.0
    last_status_ts = 0.0
    last_ka_arm_log_ts = 0.0
    last_ka_arm_sig: tuple[Any, ...] | None = None
    fps_ema = cfg.fps
    prev_tick = time.monotonic()
    reload_probe_baseline: tuple[float, float] | None = None

    calib_snapshot: np.ndarray | None = None
    calib_anchor_mode: str = cfg.anchor_mode
    calib_vp_local: tuple[int, int, int, int] = (0, 0, 1, 1)
    calib_full_size: tuple[int, int] = (0, 0)
    empty = np.zeros((0, 0, 3), dtype=np.uint8)
    last_player_img = empty
    last_dealer_img = empty
    last_shuffle_img = empty
    last_banner_img = empty
    last_pb_img = empty
    last_center_img = empty
    ext_status_dirty = False
    bridge: CompanionBridge | None = None

    def build_status(win: WindowRect | None) -> dict[str, Any]:
        counts = list_template_counts()
        return {
            "type": "status",
            "state": sm.state,
            "fps": round(fps_ema, 1),
            "windowFound": win is not None,
            "windowTitle": win.title if win else "",
            "paused": sm.paused,
            "playMode": sm.sync_play_mode,
            "anchorMode": cfg.anchor_mode,
            "hudTemplates": len(detector.hud_bank.card_templates),
            "suitTemplates": len(detector.suit_bank.suit_templates),
            "feltTemplates": len(detector.felt_bank.card_templates),
            "bannerTemplates": len(detector.banner_bank.banner_templates),
            "reloadBtnTemplates": counts.get("ui", {}).get("RELOAD_BTN", 0),
            "reloadBtnOffset": cfg.keepalive.reload_btn_offset,
            "extConnected": bool(bridge is not None and bridge.has_extension_client),
            "keepalive": keepalive.to_status(time.monotonic()),
        }

    def _refresh_anchor_from_full(win: WindowRect) -> tuple[WindowRect, tuple[int, int, int, int], np.ndarray]:
        win_bgr = capturer.grab_window(win)
        abs_anchor, vp_local = vp_smoother.resolve_anchor(win, win_bgr, cfg.anchor_mode)
        return abs_anchor, vp_local, win_bgr

    def _run_calib_test(rois_override: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal calib_snapshot
        if calib_snapshot is None or calib_snapshot.size == 0:
            return {"type": "calib_error", "error": "No calibration snapshot captured yet."}

        if calib_anchor_mode == "toolbar":
            from win_anchor import _load_reload_templates
            ui_tpls = _load_reload_templates(COMPANION_DIR / "templates")
            hit = match_reload_button_in_toolbar(
                calib_snapshot,
                ui_tpls,
                offset_hint=cfg.keepalive.reload_btn_offset,
            )
            sh, sw = calib_snapshot.shape[:2]
            btn_list = []
            if hit is not None:
                cx, cy, conf = hit
                tw = ui_tpls[0].shape[1] if ui_tpls else 22
                th = ui_tpls[0].shape[0] if ui_tpls else 22
                btn_list.append({
                    "rank": "RELOAD",
                    "conf": round(conf, 3),
                    "cx": cx,
                    "cy": cy,
                    "box": [
                        round(max(0.0, (cx - tw / 2) / max(1, sw)), 4),
                        round(max(0.0, (cy - th / 2) / max(1, sh)), 4),
                        round(tw / max(1, sw), 4),
                        round(th / max(1, sh), 4),
                    ],
                })
            return {"type": "calib_test_result", "toolbarMode": True, "reloadBtns": btn_list}

        test_rois = dict(cfg.rois)
        if rois_override:
            for k, v in rois_override.items():
                if k in test_rois:
                    test_rois[k] = normalize_roi(v)

        p_img = _crop_norm(calib_snapshot, test_rois["player_strip"])
        d_img = _crop_norm(calib_snapshot, test_rois["dealer_strip"])
        c_img = _crop_norm(calib_snapshot, test_rois["community_roi"])
        df_img = _crop_norm(calib_snapshot, test_rois["dealer_felt_roi"]) if "dealer_felt_roi" in test_rois else empty
        s_img = _crop_norm(calib_snapshot, test_rois["shuffle_roi"])
        b_img = _crop_norm(calib_snapshot, test_rois["banner_roi"])
        pb_img = _crop_norm(calib_snapshot, test_rois["place_bets_roi"]) if "place_bets_roi" in test_rois else empty

        center_probe_roi = (0.34, 0.25, 0.32, 0.28)
        needs_center_probe = ("place_bets_roi" not in test_rois) and (
            (test_rois["shuffle_roi"][1] + test_rois["shuffle_roi"][3]) < 0.28
        )
        c_probe_img = _crop_norm(calib_snapshot, center_probe_roi) if needs_center_probe else None

        prev_hash = detector._last_hud_hash
        detector._last_hud_hash = None
        obs = detector.process_crops(
            player_img=p_img,
            dealer_img=d_img,
            community_img=None,
            shuffle_img=s_img,
            banner_img=b_img,
            hud_refreshed=True,
            center_img=c_probe_img,
            place_bets_img=pb_img,
        )
        detector._last_hud_hash = prev_hash

        comm_felt_dets = detector.felt_monitor.detect_static_cards(c_img)
        dealer_felt_dets = detector.dealer_felt_monitor.detect_static_cards(df_img) if df_img.size else []

        # Normalize matched boxes back to full snapshot [0..1]
        crop_dims = {
            "player_strip": (p_img.shape[1], p_img.shape[0]),
            "dealer_strip": (d_img.shape[1], d_img.shape[0]),
            "community_roi": (c_img.shape[1], c_img.shape[0]),
            "dealer_felt_roi": (df_img.shape[1], df_img.shape[0]),
        }

        def _map_dets(roi_key: str, dets: list[Any]) -> list[dict[str, Any]]:
            rx, ry, rw, rh = test_rois[roi_key]
            cw, ch = crop_dims.get(roi_key, (calib_snapshot.shape[1], calib_snapshot.shape[0]))
            out = []
            for det in dets:
                nx = rx + (det.x / max(1.0, float(cw))) * rw
                ny = ry + (det.y / max(1.0, float(ch))) * rh
                nw = max(0.01, (det.w / max(1.0, float(cw))) * rw)
                nh = max(0.01, (det.h / max(1.0, float(ch))) * rh)
                item: dict[str, Any] = {
                    "rank": det.rank,
                    "val": det.val,
                    "conf": round(det.conf, 3),
                    "handIndex": int(getattr(det, "hand_index", 0)),
                    "box": [round(nx, 4), round(ny, 4), round(nw, 4), round(nh, 4)],
                }
                if getattr(det, "suit", None):
                    item["suit"] = det.suit
                out.append(item)
            return out

        mapped_player = _map_dets("player_strip", obs.player_cards)
        return {
            "type": "calib_test_result",
            "split": obs.split_detected,
            "player": mapped_player,
            "playerHands": {str(h): _map_dets("player_strip", cs) for h, cs in obs.player_hands.items()},
            "dealer": _map_dets("dealer_strip", obs.dealer_cards),
            "communityFelt": _map_dets("community_roi", comm_felt_dets),
            "dealerFelt": _map_dets("dealer_felt_roi", dealer_felt_dets) if "dealer_felt_roi" in test_rois else [],
            "shuffle": {"active": obs.shuffle_active, "conf": round(obs.shuffle_conf, 3)},
            "banner": (
                {"state": obs.banner_state, "conf": round(obs.banner_conf, 3), "source": obs.banner_source}
                if obs.banner_state
                else None
            ),
            "placeBets": {"active": obs.place_bets_conf > 0, "conf": round(obs.place_bets_conf, 3)},
        }

    def on_inbound(msg: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal calib_snapshot, calib_anchor_mode, calib_vp_local, calib_full_size
        nonlocal win_cache, anchor_cache, vp_local_cache, last_vp_scan
        nonlocal last_player_img, last_dealer_img, last_shuffle_img, last_banner_img, last_center_img
        nonlocal ext_status_dirty
        mtype = msg.get("type")

        if mtype == "browser_ext_hello":
            print(f"[Keepalive] Brave extension connected ({msg.get('agent', 'brave_ext')} v{msg.get('version', '?')}); background chrome.tabs.reload ready.")
            ext_status_dirty = True
            return None

        if mtype == "ext_tab_status":
            tab_st = str(msg.get("status") or "").lower()
            keepalive.notify_tab_event(tab_st, now=time.monotonic())
            print(f"[Keepalive] Brave tab event: status={tab_st} title={msg.get('title', '')!r}")
            return None

        if mtype == "sync_state":
            sm.set_sync_state(
                play_mode=msg.get("playMode"),
                is_split=msg.get("isSplit"),
                draft_count=msg.get("draftCount"),
                track_felt=msg.get("trackFelt"),
                comp_round_id=msg.get("compRoundId"),
                draft_hands=msg.get("draftHands"),
                draft_dealer=msg.get("draftDealer"),
                draft_hands_manual=msg.get("draftHandsManual"),
                draft_dealer_manual=msg.get("draftDealerManual"),
                has_prompt=msg.get("hasPrompt"),
                bet_rounds_count=msg.get("betRoundsCount"),
                shoe_rem=msg.get("shoeRem"),
                dealer_guess_removed=msg.get("dealerGuessRemoved"),
            )
            return None

        if mtype == "keepalive_action":
            act = str(msg.get("action") or "")
            now_mono = time.monotonic()
            if act == "reset":
                keepalive.reset_timer(reason="manual", now=now_mono)
                return {
                    "type": "keepalive_status",
                    "event": "reset",
                    "keepalive": keepalive.to_status(now_mono),
                }
            if act == "refresh_now":
                keepalive.arm_immediate_refresh(now_mono)
                return {
                    "type": "keepalive_status",
                    "event": "armed",
                    "keepalive": keepalive.to_status(now_mono),
                }
            if act == "set_config":
                ka_in = msg.get("keepalive") or {}
                new_mode = str(ka_in.get("mode", cfg.keepalive.mode))
                if new_mode not in ("auto", "alert_only", "off"):
                    new_mode = cfg.keepalive.mode
                new_method = str(ka_in.get("method", cfg.keepalive.method))
                if new_method not in (
                    "ext_then_os",
                    "ext_reload",
                    "click_reload_then_f5",
                    "click_reload",
                    "sendinput_f5",
                    "sendinput_ctrl_r",
                    "appcommand_then_f5",
                    "appcommand",
                ):
                    new_method = cfg.keepalive.method
                min_w = max(30.0, float(ka_in.get("min_watch_sec", cfg.keepalive.min_watch_sec)))
                max_w = max(min_w, float(ka_in.get("max_watch_sec", cfg.keepalive.max_watch_sec)))
                cfg.keepalive = KeepaliveConfig(
                    enabled=(new_mode != "off") and bool(ka_in.get("enabled", True)),
                    mode=new_mode,
                    method=new_method,
                    prefer_left_window=bool(ka_in.get("prefer_left_window", cfg.keepalive.prefer_left_window)),
                    min_watch_sec=min_w,
                    max_watch_sec=max_w,
                    hard_warn_sec=max(max_w, float(ka_in.get("hard_warn_sec", max(cfg.keepalive.hard_warn_sec, max_w + 90.0)))),
                    round_over_delay_sec=cfg.keepalive.round_over_delay_sec,
                    place_bets_min_delay_sec=cfg.keepalive.place_bets_min_delay_sec,
                    place_bets_max_delay_sec=cfg.keepalive.place_bets_max_delay_sec,
                    place_bets_latest_sec=cfg.keepalive.place_bets_latest_sec,
                    reload_grace_sec=cfg.keepalive.reload_grace_sec,
                    reload_timeout_sec=cfg.keepalive.reload_timeout_sec,
                    appcommand_verify_sec=cfg.keepalive.appcommand_verify_sec,
                    reset_on_bet_round=bool(ka_in.get("reset_on_bet_round", cfg.keepalive.reset_on_bet_round)),
                    reload_btn_offset=cfg.keepalive.reload_btn_offset,
                )
                keepalive.update_config(cfg.keepalive)
                save_config(cfg)
                return {
                    "type": "keepalive_status",
                    "event": "config_saved",
                    "keepalive": keepalive.to_status(now_mono),
                }
            return None

        if mtype == "control":
            act = msg.get("action")
            if act == "pause":
                sm.paused = True
            elif act == "resume":
                sm.paused = False
            elif act == "resync":
                sm.resync_round()
                detector.invalidate_cache(reset_scale_hint=True)
                detector.felt_monitor.reset()
                detector.dealer_felt_monitor.reset()
                vp_smoother.reset()
                anchor_cache = None
                last_vp_scan = 0.0
                last_player_img = empty
                last_dealer_img = empty
                last_shuffle_img = empty
                last_banner_img = empty
                last_center_img = empty
            return build_status(win_cache)

        if mtype == "calib_grab":
            req_mode = msg.get("anchorMode")
            if req_mode in ("game_viewport", "window", "toolbar"):
                calib_anchor_mode = req_mode
            else:
                calib_anchor_mode = cfg.anchor_mode

            win = find_target_window(cfg.window_titles, prefer_left=cfg.keepalive.prefer_left_window)
            if win is None:
                return {"type": "calib_error", "error": "Casino window not found. Keep Newport/Playtech open (not minimized)."}
            win_cache = win
            win_bgr = capturer.grab_window(win)
            vp_local = detect_game_viewport_local(win_bgr)
            calib_vp_local = vp_local
            calib_full_size = (win.width, win.height)

            if calib_anchor_mode == "toolbar":
                tw = min(win_bgr.shape[1], 420)
                th = min(win_bgr.shape[0], 160)
                calib_snapshot = win_bgr[:th, :tw].copy()
            elif calib_anchor_mode == "game_viewport":
                vx, vy, vw, vh = vp_local
                calib_snapshot = win_bgr[vy : vy + vh, vx : vx + vw].copy()
            else:
                calib_snapshot = win_bgr.copy()

            ok, buf = cv2.imencode(".png", calib_snapshot)
            if not ok:
                return {"type": "calib_error", "error": "Failed to encode snapshot PNG."}
            b64 = base64.b64encode(buf.tobytes()).decode("ascii")
            return {
                "type": "calib_snapshot",
                "imageDataUrl": f"data:image/png;base64,{b64}",
                "width": int(calib_snapshot.shape[1]),
                "height": int(calib_snapshot.shape[0]),
                "fullWindowSize": list(calib_full_size),
                "viewportLocal": list(calib_vp_local),
                "anchorMode": calib_anchor_mode,
                "rois": {k: [round(x, 4) for x in v] for k, v in cfg.rois.items()},
                "templates": list_template_counts(),
                "reloadBtnOffset": cfg.keepalive.reload_btn_offset,
            }

        if mtype == "calib_save_rois":
            rois_in = msg.get("rois") or {}
            for k, v in rois_in.items():
                if k in cfg.rois:
                    cfg.rois[k] = normalize_roi(v)
            req_mode = msg.get("anchorMode")
            if req_mode in ("game_viewport", "window"):
                cfg.anchor_mode = req_mode
            save_config(cfg)
            vp_smoother.reset()
            anchor_cache = None
            last_vp_scan = 0.0
            detector.invalidate_cache(reset_scale_hint=True)
            return {
                "type": "calib_saved",
                "kind": "rois",
                "anchorMode": cfg.anchor_mode,
                "rois": {k: [round(x, 4) for x in v] for k, v in cfg.rois.items()},
            }

        if mtype == "calib_save_template":
            if calib_snapshot is None or calib_snapshot.size == 0:
                return {"type": "calib_error", "error": "Capture a snapshot first before cropping a template."}
            bank = str(msg.get("bank", "hud"))
            rank = str(msg.get("rank", "")).upper()
            auto_trim = bool(msg.get("autoTrim", False))
            norm_crop = msg.get("normCrop")
            if not isinstance(norm_crop, (list, tuple)) or len(norm_crop) != 4:
                return {"type": "calib_error", "error": "Invalid normCrop coordinates."}
            norm_r = normalize_roi(norm_crop)
            crop = _crop_norm(calib_snapshot, norm_r)
            try:
                out_path = save_template_crop(bank, rank, crop, auto_trim=auto_trim)
            except ValueError as exc:
                return {"type": "calib_error", "error": str(exc)}
            if bank == "ui" and rank == "RELOAD_BTN":
                sh, sw = calib_snapshot.shape[:2]
                rx, ry, rw, rh = norm_r
                cx_px = int(round((rx + rw * 0.5) * sw))
                cy_px = int(round((ry + rh * 0.5) * sh))
                if calib_anchor_mode == "game_viewport":
                    cx_px += int(calib_vp_local[0])
                    cy_px += int(calib_vp_local[1])
                cfg.keepalive.reload_btn_offset = [cx_px, cy_px]
                keepalive.update_config(cfg.keepalive)
                save_config(cfg)
            detector.reload_templates()
            detector._last_hud_hash = None
            return {
                "type": "calib_saved",
                "kind": "template",
                "bank": bank,
                "rank": rank,
                "filename": out_path.name,
                "cropSize": [int(crop.shape[1]), int(crop.shape[0])],
                "templates": list_template_counts(),
                "reloadBtnOffset": cfg.keepalive.reload_btn_offset,
            }

        if mtype == "calib_delete_rank":
            bank = str(msg.get("bank", "hud"))
            rank = str(msg.get("rank", "")).upper()
            removed = delete_rank_templates(bank, rank)
            if bank == "ui" and rank == "RELOAD_BTN":
                cfg.keepalive.reload_btn_offset = None
                save_config(cfg)
            detector.reload_templates()
            detector._last_hud_hash = None
            return {
                "type": "calib_saved",
                "kind": "delete_rank",
                "bank": bank,
                "rank": rank,
                "removed": removed,
                "templates": list_template_counts(),
                "reloadBtnOffset": cfg.keepalive.reload_btn_offset,
            }

        if mtype == "calib_clear_bank":
            bank = str(msg.get("bank", "hud"))
            try:
                removed = clear_bank_templates(bank)
            except ValueError as exc:
                return {"type": "calib_error", "error": str(exc)}
            if bank == "ui":
                cfg.keepalive.reload_btn_offset = None
                save_config(cfg)
            detector.reload_templates()
            detector._last_hud_hash = None
            return {
                "type": "calib_saved",
                "kind": "clear_bank",
                "bank": bank,
                "removed": removed,
                "templates": list_template_counts(),
                "reloadBtnOffset": cfg.keepalive.reload_btn_offset,
            }

        if mtype == "calib_test_detect":
            return _run_calib_test(msg.get("rois"))

        return None

    def _on_ext_disconnect() -> None:
        nonlocal ext_status_dirty
        print("[Keepalive] Brave extension disconnected; OS fallback remains active.")
        ext_status_dirty = True

    bridge = CompanionBridge(
        cfg.ws_host,
        cfg.ws_port,
        on_message=on_inbound,
        on_connect=lambda: [build_status(win_cache)],
        on_ext_disconnect=_on_ext_disconnect,
    )
    await bridge.start()
    print(f"[Companion] WebSocket bridge listening on ws://{cfg.ws_host}:{cfg.ws_port} (anchor={cfg.anchor_mode})")
    print(
        f"[Companion] Loaded {len(detector.hud_bank.card_templates)} HUD templates, "
        f"{len(detector.felt_bank.card_templates)} Felt templates, "
        f"{len(detector.banner_bank.banner_templates)} Banner templates, "
        f"{len(detector.hud_bank.shuffle_templates)} Shuffle templates."
    )
    print(
        f"[Keepalive] Method={cfg.keepalive.method} | ROUND_OVER delay={cfg.keepalive.round_over_delay_sec:.1f}s "
        f"| Brave ext folder: {COMPANION_DIR / 'brave_keepalive_ext'}"
    )

    center_banner_roi = (0.34, 0.25, 0.32, 0.28)
    was_felt_armed = False
    last_pb_img = np.empty((0, 0, 3), dtype=np.uint8)
    last_armed_diag_ts = 0.0
    last_armed_diag_sig = ""
    grab_failures = 0
    high_res_timer = enable_high_res_timer()
    next_hud_ts = time.monotonic()
    next_dealer_ts = next_hud_ts
    next_felt_ts = next_hud_ts

    try:
        while True:
            now = time.monotonic()
            dt = max(1e-3, now - prev_tick)
            prev_tick = now

            window_title_missing_in_grace = False
            if win_cache is None or (now - last_win_scan) >= 1.5:
                new_win = find_target_window(
                    cfg.window_titles,
                    prefer_left=cfg.keepalive.prefer_left_window,
                )
                if keepalive.in_grace_period(now) and new_win is None and win_cache is not None:
                    # Brave briefly changes tab title while reloading; keep anchor frozen during grace
                    window_title_missing_in_grace = True
                elif new_win != win_cache:
                    win_cache = new_win
                    anchor_cache = None
                    grab_failures = 0
                last_win_scan = now

            if win_cache is None:
                ka_idle = keepalive.tick(
                    now,
                    play_mode=sm.sync_play_mode,
                    paused=sm.paused,
                    window_found=False,
                    sm_state=sm.state,
                    banner_state="",
                    obs_has_cards=False,
                    has_unresolved_prompt=sm.sync_has_prompt,
                    bet_rounds_count=sm.sync_bet_rounds_count,
                )
                if ka_idle.emit_alert:
                    await bridge.broadcast(ka_idle.emit_alert)
                if (now - last_status_ts) >= 0.75:
                    await bridge.broadcast(build_status(None))
                    last_status_ts = now
                await asyncio.sleep(0.35)
                next_hud_ts = time.monotonic()
                next_dealer_ts = next_hud_ts
                next_felt_ts = next_hud_ts
                continue

            active_round = sm.state in ("DEALING", "PLAYER_TURN", "DEALER_TURN")
            deal_imminent = (
                (sm.state == "BETTING" and sm._last_bet_banner in ("NO_MORE_BETS", "LAST_BETS"))
                or bool(detector._cached_p[0] or detector._cached_d)
            )
            felt_armed = (cfg.enable_felt_community or cfg.enable_dealer_felt_fallback) and (
                active_round or sm.felt_tracking_armed
            )
            if felt_armed != was_felt_armed:
                detector.felt_monitor.reset()
                detector.dealer_felt_monitor.reset()
                was_felt_armed = felt_armed

            felt_burst = felt_armed and active_round
            if sm.state == "DEALER_TURN":
                hud_fps = max(cfg.burst_fps, 28.0)
                dealer_fps = min(50.0, max(cfg.burst_fps * 2.5, 45.0))
            elif sm.state == "PLAYER_TURN":
                hud_fps = max(cfg.burst_fps, 24.0)
                dealer_fps = min(45.0, max(cfg.burst_fps * 2.0, 36.0))
            elif sm.state == "DEALING" or deal_imminent:
                hud_fps = max(cfg.burst_fps, 24.0)
                dealer_fps = (
                    min(40.0, max(cfg.burst_fps * 2.0, 32.0))
                    if sm._emitted_d >= 1
                    else hud_fps
                )
            else:
                hud_fps = max(cfg.fps, 10.0)
                dealer_fps = hud_fps

            if preview:
                dealer_fps = hud_fps

            fps_ema = 0.85 * fps_ema + 0.15 * min(120.0, 1.0 / dt)

            hud_interval = 1.0 / hud_fps
            dealer_interval = 1.0 / dealer_fps
            felt_interval = 1.0 / max(cfg.burst_fps, 24.0) if felt_burst else hud_interval

            hud_due = True if preview else (now >= (next_hud_ts - 0.0015))
            dealer_due = hud_due or (active_round and dealer_fps > hud_fps and now >= (next_dealer_ts - 0.0015))
            felt_due = felt_armed and (hud_due or (felt_burst and now >= (next_felt_ts - 0.0015)))

            # Avoid full-window viewport re-scans during active hands, imminent deal, or page reload grace
            in_grace_now = keepalive.in_grace_period(now)
            vp_due = (
                anchor_cache is None
                or ((not active_round) and (not deal_imminent) and (not in_grace_now) and (now - last_vp_scan) >= 1.8)
            )
            need_center_probe = ("place_bets_roi" not in cfg.rois) and ((cfg.rois["shuffle_roi"][1] + cfg.rois["shuffle_roi"][3]) < 0.28)
            dealer_only_tick = False
            comm_img = empty
            dealer_felt_img = empty

            try:
                if preview:
                    anchor_cache, vp_local_cache, full_win = _refresh_anchor_from_full(win_cache)
                    last_vp_scan = now
                    base_img = (
                        full_win[
                            vp_local_cache[1] : vp_local_cache[1] + vp_local_cache[3],
                            vp_local_cache[0] : vp_local_cache[0] + vp_local_cache[2],
                        ]
                        if cfg.anchor_mode == "game_viewport"
                        else full_win
                    )
                    last_player_img = _crop_norm(base_img, cfg.rois["player_strip"])
                    last_dealer_img = _crop_norm(base_img, cfg.rois["dealer_strip"])
                    last_shuffle_img = _crop_norm(base_img, cfg.rois["shuffle_roi"])
                    last_banner_img = _crop_norm(base_img, cfg.rois["banner_roi"])
                    last_pb_img = _crop_norm(base_img, cfg.rois["place_bets_roi"]) if "place_bets_roi" in cfg.rois else empty
                    last_center_img = _crop_norm(base_img, center_banner_roi) if need_center_probe else empty
                    comm_img = _crop_norm(base_img, cfg.rois["community_roi"]) if felt_armed else empty
                    dealer_felt_img = (
                        _crop_norm(base_img, cfg.rois["dealer_felt_roi"])
                        if (felt_armed and "dealer_felt_roi" in cfg.rois)
                        else empty
                    )
                    last_hud_ts = now
                    next_hud_ts = now + hud_interval
                    next_dealer_ts = next_hud_ts
                    if felt_armed:
                        next_felt_ts = now + felt_interval
                else:
                    if vp_due:
                        anchor_cache, vp_local_cache, _ = _refresh_anchor_from_full(win_cache)
                        last_vp_scan = now
                    anchor = anchor_cache or win_cache
                    if hud_due:
                        cards_active = bool(sm._emitted_p or sm._emitted_d or detector._cached_p[0] or detector._cached_d)
                        rois_to_grab: dict[str, tuple[float, float, float, float]] = {
                            "player_strip": cfg.rois["player_strip"],
                            "dealer_strip": cfg.rois["dealer_strip"],
                            "shuffle_roi": cfg.rois["shuffle_roi"],
                            "banner_roi": cfg.rois["banner_roi"],
                        }
                        if "place_bets_roi" in cfg.rois:
                            rois_to_grab["place_bets_roi"] = cfg.rois["place_bets_roi"]
                        if need_center_probe and detector.is_center_scan_due(now, cards_active):
                            rois_to_grab["center_probe"] = center_banner_roi
                        if felt_armed:
                            rois_to_grab["community_roi"] = cfg.rois["community_roi"]
                            if "dealer_felt_roi" in cfg.rois:
                                rois_to_grab["dealer_felt_roi"] = cfg.rois["dealer_felt_roi"]

                        # Single OS capture over tight ROI union; avoids copying unused full-viewport pixels
                        crops = capturer.grab_rois(anchor, rois_to_grab)
                        if not crops or crops["dealer_strip"].size == 0:
                            raise RuntimeError("Empty window capture")
                        last_player_img = crops["player_strip"]
                        last_dealer_img = crops["dealer_strip"]
                        last_shuffle_img = crops["shuffle_roi"]
                        last_banner_img = crops["banner_roi"]
                        last_pb_img = crops.get("place_bets_roi", empty)
                        last_center_img = crops.get("center_probe", empty)
                        comm_img = crops.get("community_roi", empty) if felt_armed else empty
                        dealer_felt_img = crops.get("dealer_felt_roi", empty) if felt_armed else empty
                        last_hud_ts = now
                        next_hud_ts = (now + hud_interval) if (now - next_hud_ts) > hud_interval else (next_hud_ts + hud_interval)
                        next_dealer_ts = now + dealer_interval
                        if felt_armed:
                            next_felt_ts = now + felt_interval
                    elif dealer_due or felt_due:
                        rois_to_grab = {}
                        if dealer_due:
                            rois_to_grab["dealer_strip"] = cfg.rois["dealer_strip"]
                        if felt_due:
                            rois_to_grab["community_roi"] = cfg.rois["community_roi"]
                            if "dealer_felt_roi" in cfg.rois:
                                rois_to_grab["dealer_felt_roi"] = cfg.rois["dealer_felt_roi"]
                        crops = capturer.grab_rois(anchor, rois_to_grab)
                        if dealer_due:
                            d_crop = crops.get("dealer_strip", empty)
                            if d_crop.size == 0:
                                raise RuntimeError("Empty dealer capture")
                            last_dealer_img = d_crop
                            dealer_only_tick = True
                            next_dealer_ts = (
                                (now + dealer_interval)
                                if (now - next_dealer_ts) > dealer_interval
                                else (next_dealer_ts + dealer_interval)
                            )
                        comm_img = crops.get("community_roi", empty) if felt_due else empty
                        dealer_felt_img = crops.get("dealer_felt_roi", empty) if felt_due else empty
                        if felt_due:
                            next_felt_ts = (
                                (now + felt_interval)
                                if (now - next_felt_ts) > felt_interval
                                else (next_felt_ts + felt_interval)
                            )
                grab_failures = 0
            except Exception:
                grab_failures += 1
                anchor_cache = None
                if grab_failures >= 3:
                    win_cache = None
                    grab_failures = 0
                await asyncio.sleep(0.2)
                next_hud_ts = time.monotonic()
                next_dealer_ts = next_hud_ts
                next_felt_ts = next_hud_ts
                continue

            if hud_due or dealer_only_tick or comm_img.size or dealer_felt_img.size:
                force_dealer_scan = sm.state == "DEALER_TURN" or ("BACK" in sm.d_debouncer.committed_ranks)
                obs = detector.process_crops(
                    player_img=last_player_img,
                    dealer_img=last_dealer_img,
                    community_img=comm_img,
                    shuffle_img=last_shuffle_img,
                    banner_img=last_banner_img,
                    ts=now,
                    hud_refreshed=hud_due,
                    center_img=last_center_img,
                    place_bets_img=last_pb_img,
                    player_refreshed=hud_due,
                    dealer_refreshed=(hud_due or dealer_only_tick),
                    banner_refreshed=hud_due,
                    dealer_only=dealer_only_tick,
                    dealer_felt_img=dealer_felt_img,
                    force_dealer_scan=force_dealer_scan,
                )
                in_ka_reload = keepalive.state in ("REFRESHING", "RECOVERING")
                if not in_ka_reload:
                    events = sm.step(obs)
                    if events:
                        await bridge.broadcast_many(events)

                if hud_due:
                    uniform_loading = bool(
                        last_shuffle_img.size
                        and last_player_img.size
                        and float(np.std(last_shuffle_img)) < 8.0
                        and float(np.std(last_player_img)) < 8.0
                    )
                    luma_shift = False
                    if reload_probe_baseline is not None and last_shuffle_img.size:
                        cur_mean = float(np.mean(last_shuffle_img))
                        cur_std = float(np.std(last_shuffle_img))
                        p_mean = float(np.mean(last_player_img)) if last_player_img.size else cur_mean
                        shuf_diff = abs(cur_mean - reload_probe_baseline[0])
                        p_diff = abs(p_mean - reload_probe_baseline[2]) if len(reload_probe_baseline) >= 3 else 0.0
                        luma_shift = shuf_diff >= 14.0 or p_diff >= 14.0 or cur_std < 10.0
                    idle_ms = (
                        get_user_idle_ms()
                        if (not in_ka_reload and keepalive.watch_elapsed_sec >= keepalive.target_sec)
                        else 10000
                    )
                    obs_cards_flag = bool(obs.player_cards or obs.dealer_cards or obs.player_hands)
                    ka_action = keepalive.tick(
                        now,
                        play_mode=sm.sync_play_mode,
                        paused=sm.paused,
                        window_found=not window_title_missing_in_grace,
                        sm_state=sm.state,
                        banner_state=obs.banner_state,
                        obs_has_cards=obs_cards_flag,
                        has_unresolved_prompt=sm.sync_has_prompt,
                        bet_rounds_count=sm.sync_bet_rounds_count,
                        user_idle_ms=idle_ms,
                        reload_luma_shift=luma_shift,
                        uniform_loading_screen=uniform_loading,
                    )
                    if in_ka_reload and keepalive.state not in ("REFRESHING", "RECOVERING"):
                        sm.resync_round(clear_pending=True)
                        detector.invalidate_cache(reset_scale_hint=True)
                        reload_probe_baseline = None
                        if obs.banner_state:
                            rec_events = sm.step(obs)
                            if rec_events:
                                await bridge.broadcast_many(rec_events)
                    if ka_action.flush_auto_watch:
                        flush_evts: list[dict[str, Any]] = []
                        sm._flush_pending_auto_watch(flush_evts)
                        if flush_evts:
                            await bridge.broadcast_many(flush_evts)
                    if ka_action.emit_alert:
                        ev_name = ka_action.emit_alert.get("event", "")
                        if ev_name == "recovered":
                            print(f"[Keepalive] Recovered in {ka_action.emit_alert.get('reloadSec', '?')}s; timer reset.")
                        elif ev_name in ("hard_warn", "timeout_warn"):
                            print(f"[Keepalive] Alert: {ev_name}")
                        await bridge.broadcast(ka_action.emit_alert)
                    if ka_action.trigger_refresh:
                        req_method = ka_action.preferred_method or cfg.keepalive.method
                        print(f"[Keepalive] Triggering refresh ({req_method}) in sm={sm.state} (banner='{obs.banner_state}')...")
                        reload_probe_baseline = (
                            (
                                float(np.mean(last_shuffle_img)),
                                float(np.std(last_shuffle_img)),
                                float(np.mean(last_player_img)) if last_player_img.size else 0.0,
                            )
                            if last_shuffle_img.size
                            else (0.0, 0.0, 0.0)
                        )
                        ok = False
                        used_method = req_method
                        if req_method in ("ext_reload", "ext_then_os"):
                            ok, used_method, ack = await bridge.request_extension_reload(
                                cfg.window_titles,
                                prefer_left=cfg.keepalive.prefer_left_window,
                            )
                            if ok:
                                tab_t = (ack or {}).get("title", "")
                                tab_id = (ack or {}).get("tabId")
                                print(f"[Keepalive] Extension chrome.tabs.reload OK (tabId={tab_id}, title={tab_t!r}).")
                            elif cfg.keepalive.method == "ext_then_os":
                                print(f"[Keepalive] Extension reload unavailable ({used_method}); falling back immediately to OS refresh.")
                                req_method = "click_reload"

                        if not ok and req_method not in ("ext_reload", "ext_then_os"):
                            if win_cache is None:
                                print("[Keepalive] Refresh skipped: game window handle not found.")
                                used_method = "window_not_found"
                            else:
                                ok, used_method = await asyncio.to_thread(
                                    trigger_browser_refresh,
                                    win_cache.hwnd,
                                    cfg.window_titles,
                                    req_method,
                                    cfg.keepalive.prefer_left_window,
                                    templates_root=COMPANION_DIR / "templates",
                                    reload_btn_offset=cfg.keepalive.reload_btn_offset,
                                )
                                if (
                                    not ok
                                    and req_method in ("click_reload", "click_reload_then_f5")
                                    and used_method == "reload_btn_not_found"
                                    and cfg.keepalive.method in ("ext_then_os", "click_reload_then_f5")
                                ):
                                    print("[Keepalive] Reload button not matched; falling back immediately to sendinput_f5.")
                                    ok, used_method = await asyncio.to_thread(
                                        trigger_browser_refresh,
                                        win_cache.hwnd,
                                        cfg.window_titles,
                                        "sendinput_f5",
                                        cfg.keepalive.prefer_left_window,
                                    )
                        print(f"[Keepalive] Dispatch result: ok={ok}, method={used_method}")
                        disp_evt = keepalive.mark_refresh_dispatched(time.monotonic(), ok, used_method)
                        last_armed_diag_sig = ""
                        await bridge.broadcast(disp_evt)
                        if ok:
                            sm.resync_round(clear_pending=True)
                            detector.invalidate_cache(reset_scale_hint=True)
                    elif ka_action.status_changed:
                        if keepalive.state == "ARMED_NEXT_BETS":
                            print(f"[Keepalive] Armed ({keepalive.watch_elapsed_sec:.0f}s/{keepalive.target_sec:.0f}s); waiting for ROUND_OVER or PLACE YOUR BETS.")
                        await bridge.broadcast(build_status(win_cache))
                        last_status_ts = now
                    if not ka_action.trigger_refresh and keepalive.state == "ARMED_NEXT_BETS":
                        diag_sig = f"{obs.banner_state}|{obs.banner_source}|{sm.state}|{len(obs.player_cards)}+{len(obs.dealer_cards)}"
                        if diag_sig != last_armed_diag_sig or (now - last_armed_diag_ts) >= 2.5:
                            print(
                                f"[Keepalive] Armed wait: banner='{obs.banner_state}' "
                                f"(conf={obs.banner_conf:.2f}, pb={obs.place_bets_conf:.2f}, src={obs.banner_source}), "
                                f"sm={sm.state}, cards={len(obs.player_cards)}+{len(obs.dealer_cards)}"
                            )
                            last_armed_diag_sig = diag_sig
                            last_armed_diag_ts = now
                    elif keepalive.state != "ARMED_NEXT_BETS":
                        last_armed_diag_sig = ""

                if preview:
                    keep_open = _draw_preview(full_win, vp_local_cache, cfg, obs, sm, fps_ema)
                    if not keep_open:
                        break

            if ext_status_dirty or (now - last_status_ts) >= 0.6:
                ext_status_dirty = False
                await bridge.broadcast(build_status(win_cache))
                last_status_ts = now

            next_wakeup = next_hud_ts
            if active_round and dealer_fps > hud_fps:
                next_wakeup = min(next_wakeup, next_dealer_ts)
            if felt_burst:
                next_wakeup = min(next_wakeup, next_felt_ts)
            await asyncio.sleep(max(0.001, next_wakeup - time.monotonic()))
    finally:
        if high_res_timer:
            disable_high_res_timer()
        if preview:
            cv2.destroyAllWindows()
        capturer.close()
        await bridge.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Playtech All Bets Blackjack local desktop companion.")
    parser.add_argument("--config", type=Path, help="Optional path to custom config.json")
    parser.add_argument("--preview", action="store_true", help="Show live annotated ROI/detection preview window")
    args = parser.parse_args()

    cfg = load_config(args.config)
    try:
        asyncio.run(run_companion(cfg, preview=args.preview))
    except KeyboardInterrupt:
        print("\n[Companion] Stopped cleanly.")


if __name__ == "__main__":
    main()

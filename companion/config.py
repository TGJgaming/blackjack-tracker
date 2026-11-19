"""Configuration loading and card rank normalization for Playtech companion."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMPANION_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = COMPANION_DIR / "config.json"
EXAMPLE_CONFIG_PATH = COMPANION_DIR / "config.example.json"

RANK_TO_VAL: dict[str, int | None] = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "10": 0,
    "T": 0,
    "J": 0,
    "Q": 0,
    "K": 0,
    "A": 1,
    "BACK": None,
    "SHUFFLE": None,
}


def normalize_roi(coords: list[float] | tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Clamp a normalized [rx, ry, rw, rh] ROI tuple cleanly within [0.0, 1.0]."""
    if len(coords) != 4:
        raise ValueError(f"Expected 4 ROI coordinates, got {len(coords)}")
    rx = max(0.0, min(0.99, float(coords[0])))
    ry = max(0.0, min(0.99, float(coords[1])))
    rw = max(0.01, min(1.0 - rx, float(coords[2])))
    rh = max(0.01, min(1.0 - ry, float(coords[3])))
    return (rx, ry, rw, rh)


@dataclass
class FeltMotionConfig:
    min_contour_area_ratio: float = 0.015
    min_brightness: int = 145
    pulse_cooldown_sec: float = 0.35
    settle_timeout_sec: float = 0.50
    angles_deg: list[int] = field(default_factory=lambda: [-15, -10, -5, 0, 5, 10, 15])
    scales: list[float] = field(default_factory=lambda: [0.75, 0.85, 0.92, 1.0, 1.08, 1.18, 1.30])


@dataclass
class KeepaliveConfig:
    enabled: bool = True
    mode: str = "auto"  # "auto" | "alert_only" | "off"
    method: str = "ext_then_os"  # "ext_then_os" | "ext_reload" | "click_reload_then_f5" | "click_reload" | "sendinput_f5" | "sendinput_ctrl_r" | "appcommand_then_f5" | "appcommand"
    prefer_left_window: bool = True
    min_watch_sec: float = 570.0
    max_watch_sec: float = 630.0
    hard_warn_sec: float = 750.0
    round_over_delay_sec: float = 1.2
    place_bets_min_delay_sec: float = 0.35
    place_bets_max_delay_sec: float = 0.85
    place_bets_latest_sec: float = 5.0
    reload_grace_sec: float = 12.0
    reload_timeout_sec: float = 20.0
    appcommand_verify_sec: float = 1.2
    reset_on_bet_round: bool = True
    reload_btn_offset: list[int] | None = None


@dataclass
class CompanionConfig:
    window_titles: list[str] = field(default_factory=lambda: ["Live Games", "All Bets Blackjack", "epicworld", "Playtech"])
    anchor_mode: str = "game_viewport"  # "game_viewport" | "window"
    ws_host: str = "127.0.0.1"
    ws_port: int = 8765
    fps: float = 7.5
    burst_fps: float = 18.0
    confirm_frames: int = 3
    clear_frames: int = 4
    shuffle_hold_sec: float = 2.0
    min_hud_confidence: float = 0.84
    min_felt_confidence: float = 0.78
    min_banner_confidence: float = 0.70
    hud_nms_min_dx_ratio: float = 0.018
    hud_dedup_window_sec: float = 2.8
    enable_felt_community: bool = True
    enable_dealer_felt_fallback: bool = True
    require_wait_banner: bool = True
    pause_on_unmatched_felt: bool = False
    min_auto_confidence: float = 0.70
    rois: dict[str, tuple[float, float, float, float]] = field(
        default_factory=lambda: {
            "player_strip": (0.42, 0.66, 0.32, 0.22),
            "dealer_strip": (0.20, 0.34, 0.22, 0.27),
            "community_roi": (0.44, 0.50, 0.21, 0.15),
            "dealer_felt_roi": (0.38, 0.28, 0.24, 0.18),
            "banner_roi": (0.35, 0.00, 0.30, 0.06),
            "place_bets_roi": (0.38, 0.30, 0.24, 0.11),
            "shuffle_roi": (0.30, 0.24, 0.40, 0.38),
        }
    )
    felt_motion: FeltMotionConfig = field(default_factory=FeltMotionConfig)
    keepalive: KeepaliveConfig = field(default_factory=KeepaliveConfig)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CompanionConfig":
        fm_raw = raw.get("felt_motion") or {}
        fm = FeltMotionConfig(
            min_contour_area_ratio=float(fm_raw.get("min_contour_area_ratio", 0.015)),
            min_brightness=int(fm_raw.get("min_brightness", 145)),
            pulse_cooldown_sec=float(fm_raw.get("pulse_cooldown_sec", 0.35)),
            settle_timeout_sec=float(fm_raw.get("settle_timeout_sec", 0.50)),
            angles_deg=[int(a) for a in fm_raw.get("angles_deg", [-15, -10, -5, 0, 5, 10, 15])],
            scales=[float(s) for s in fm_raw.get("scales", [0.75, 0.85, 0.92, 1.0, 1.08, 1.18, 1.30])],
        )
        ka_raw = raw.get("keepalive") or {}
        ka_mode = str(ka_raw.get("mode", "auto"))
        if ka_mode not in ("auto", "alert_only", "off"):
            ka_mode = "auto"
        ka_method = str(ka_raw.get("method", "ext_then_os"))
        if ka_method not in (
            "ext_then_os",
            "ext_reload",
            "click_reload_then_f5",
            "click_reload",
            "sendinput_f5",
            "sendinput_ctrl_r",
            "appcommand_then_f5",
            "appcommand",
        ):
            ka_method = "ext_then_os"
        min_w = max(30.0, float(ka_raw.get("min_watch_sec", 570.0)))
        max_w = max(min_w, float(ka_raw.get("max_watch_sec", 630.0)))
        raw_btn_off = ka_raw.get("reload_btn_offset")
        btn_off = (
            [int(raw_btn_off[0]), int(raw_btn_off[1])]
            if isinstance(raw_btn_off, (list, tuple)) and len(raw_btn_off) >= 2
            else None
        )
        pb_min = max(0.2, float(ka_raw.get("place_bets_min_delay_sec", 0.35)))
        pb_max = max(pb_min, float(ka_raw.get("place_bets_max_delay_sec", 0.85)))
        ka = KeepaliveConfig(
            enabled=bool(ka_raw.get("enabled", True)) and ka_mode != "off",
            mode=ka_mode,
            method=ka_method,
            prefer_left_window=bool(ka_raw.get("prefer_left_window", True)),
            min_watch_sec=min_w,
            max_watch_sec=max_w,
            hard_warn_sec=max(max_w, float(ka_raw.get("hard_warn_sec", 750.0))),
            round_over_delay_sec=max(0.2, float(ka_raw.get("round_over_delay_sec", 1.2))),
            place_bets_min_delay_sec=pb_min,
            place_bets_max_delay_sec=pb_max,
            place_bets_latest_sec=max(1.5, float(ka_raw.get("place_bets_latest_sec", 5.0))),
            reload_grace_sec=max(5.0, float(ka_raw.get("reload_grace_sec", 12.0))),
            reload_timeout_sec=max(10.0, float(ka_raw.get("reload_timeout_sec", 20.0))),
            appcommand_verify_sec=max(0.4, float(ka_raw.get("appcommand_verify_sec", 1.2))),
            reset_on_bet_round=bool(ka_raw.get("reset_on_bet_round", True)),
            reload_btn_offset=btn_off,
        )
        default_rois = cls().rois
        rois_raw = raw.get("rois") or {}
        rois = {k: normalize_roi(rois_raw.get(k, v)) for k, v in default_rois.items()}
        for k, v in rois_raw.items():
            if k not in rois:
                rois[k] = normalize_roi(v)

        anchor = str(raw.get("anchor_mode", "game_viewport"))
        if anchor not in ("game_viewport", "ipad_split_left", "window"):
            anchor = "game_viewport"

        try:
            ws_port = int(raw.get("ws_port", 8765))
        except (TypeError, ValueError):
            ws_port = 8765
        if not (1 <= ws_port <= 65535):
            ws_port = 8765

        return cls(
            window_titles=list(raw.get("window_titles", cls().window_titles)),
            anchor_mode=anchor,
            ws_host=str(raw.get("ws_host", "127.0.0.1")),
            ws_port=ws_port,
            fps=max(2.0, float(raw.get("fps", 7.5))),
            burst_fps=max(5.0, float(raw.get("burst_fps", 18.0))),
            confirm_frames=max(1, int(raw.get("confirm_frames", 3))),
            clear_frames=max(2, int(raw.get("clear_frames", 4))),
            shuffle_hold_sec=max(0.5, float(raw.get("shuffle_hold_sec", 2.0))),
            min_hud_confidence=float(raw.get("min_hud_confidence", 0.84)),
            min_felt_confidence=float(raw.get("min_felt_confidence", 0.78)),
            min_banner_confidence=float(raw.get("min_banner_confidence", 0.70)),
            hud_nms_min_dx_ratio=float(raw.get("hud_nms_min_dx_ratio", 0.018)),
            hud_dedup_window_sec=float(raw.get("hud_dedup_window_sec", 2.8)),
            enable_felt_community=bool(raw.get("enable_felt_community", True)),
            enable_dealer_felt_fallback=bool(raw.get("enable_dealer_felt_fallback", True)),
            require_wait_banner=bool(raw.get("require_wait_banner", True)),
            pause_on_unmatched_felt=bool(raw.get("pause_on_unmatched_felt", False)),
            min_auto_confidence=float(raw.get("min_auto_confidence", 0.70)),
            rois=rois,
            felt_motion=fm,
            keepalive=ka,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_titles": self.window_titles,
            "anchor_mode": self.anchor_mode,
            "ws_host": self.ws_host,
            "ws_port": self.ws_port,
            "fps": self.fps,
            "burst_fps": self.burst_fps,
            "confirm_frames": self.confirm_frames,
            "clear_frames": self.clear_frames,
            "shuffle_hold_sec": self.shuffle_hold_sec,
            "min_hud_confidence": self.min_hud_confidence,
            "min_felt_confidence": self.min_felt_confidence,
            "min_banner_confidence": self.min_banner_confidence,
            "hud_nms_min_dx_ratio": self.hud_nms_min_dx_ratio,
            "hud_dedup_window_sec": self.hud_dedup_window_sec,
            "enable_felt_community": self.enable_felt_community,
            "enable_dealer_felt_fallback": self.enable_dealer_felt_fallback,
            "require_wait_banner": self.require_wait_banner,
            "pause_on_unmatched_felt": self.pause_on_unmatched_felt,
            "min_auto_confidence": self.min_auto_confidence,
            "rois": {k: [round(x, 4) for x in v] for k, v in self.rois.items()},
            "felt_motion": {
                "min_contour_area_ratio": self.felt_motion.min_contour_area_ratio,
                "min_brightness": self.felt_motion.min_brightness,
                "pulse_cooldown_sec": self.felt_motion.pulse_cooldown_sec,
                "settle_timeout_sec": self.felt_motion.settle_timeout_sec,
                "angles_deg": self.felt_motion.angles_deg,
                "scales": self.felt_motion.scales,
            },
            "keepalive": {
                "enabled": self.keepalive.enabled,
                "mode": self.keepalive.mode,
                "method": self.keepalive.method,
                "prefer_left_window": self.keepalive.prefer_left_window,
                "min_watch_sec": self.keepalive.min_watch_sec,
                "max_watch_sec": self.keepalive.max_watch_sec,
                "hard_warn_sec": self.keepalive.hard_warn_sec,
                "round_over_delay_sec": self.keepalive.round_over_delay_sec,
                "place_bets_min_delay_sec": self.keepalive.place_bets_min_delay_sec,
                "place_bets_max_delay_sec": self.keepalive.place_bets_max_delay_sec,
                "place_bets_latest_sec": self.keepalive.place_bets_latest_sec,
                "reload_grace_sec": self.keepalive.reload_grace_sec,
                "reload_timeout_sec": self.keepalive.reload_timeout_sec,
                "appcommand_verify_sec": self.keepalive.appcommand_verify_sec,
                "reset_on_bet_round": self.keepalive.reset_on_bet_round,
                "reload_btn_offset": self.keepalive.reload_btn_offset,
            },
        }


def load_config(path: Path | None = None) -> CompanionConfig:
    """Load config.json if present, else fall back to config.example.json or defaults."""
    target = path or DEFAULT_CONFIG_PATH
    save_target = path or DEFAULT_CONFIG_PATH
    if not target.exists() and path is None and EXAMPLE_CONFIG_PATH.exists():
        target = EXAMPLE_CONFIG_PATH
    if not target.exists():
        cfg = CompanionConfig()
        setattr(cfg, "_config_path", save_target)
        return cfg
    with target.open("r", encoding="utf-8") as fh:
        cfg = CompanionConfig.from_dict(json.load(fh))
        setattr(cfg, "_config_path", save_target)
        return cfg


def save_config(cfg: CompanionConfig, path: Path | None = None) -> Path:
    """Write configuration atomically to disk via temp file + os.replace."""
    import os
    import tempfile

    target = path or getattr(cfg, "_config_path", None) or DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cfg.to_dict(), fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    setattr(cfg, "_config_path", target)
    return target

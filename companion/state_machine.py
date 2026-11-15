"""Pure round & shoe state machine with HUD-vs-Felt deduplication and BET/Split/Glitch guards."""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import time
from typing import Any

from config import RANK_TO_VAL, CompanionConfig, KeepaliveConfig
from detector import CardDetection, FeltObservation, FrameObservation


def _same_bj_value(r1: str, r2: str) -> bool:
    """Return True when two card rank labels share the same blackjack pip value (e.g. Q and K -> 10)."""
    v1 = RANK_TO_VAL.get(r1)
    v2 = RANK_TO_VAL.get(r2)
    return v1 is not None and v1 == v2


def _eval_bj_ranks(ranks: list[str]) -> tuple[int, bool, bool]:
    """Return (best_total, is_soft, is_bust) for a sequence of rank labels."""
    total = 0
    aces = 0
    for rk in ranks:
        val = RANK_TO_VAL.get(rk)
        if val is None:
            continue
        if val == 1:
            aces += 1
            total += 11
        elif val == 0:
            total += 10
        else:
            total += val
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total, aces > 0, total > 21


@dataclass
class SequenceDebouncer:
    """Requires N identical non-empty rank sequences before committing, and M empty frames to clear."""

    confirm_frames: int
    clear_frames: int
    fast_tail_confirm: bool = False
    fast_tail_min_conf: float = 0.84
    fast_initial_deal: bool = True
    _cand_ranks: tuple[str, ...] = ()
    _cand_dets: tuple[CardDetection, ...] = ()
    _cand_count: int = 0
    _empty_count: int = 0
    _wipe_dets: tuple[CardDetection, ...] = ()
    _slot_cands: dict[int, tuple[str, int, CardDetection]] = field(default_factory=dict)
    _pending_tail: dict[int, tuple[CardDetection, int]] = field(default_factory=dict)
    committed_ranks: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self._cand_ranks = ()
        self._cand_dets = ()
        self._cand_count = 0
        self._empty_count = 0
        self._wipe_dets = ()
        self._slot_cands.clear()
        self._pending_tail.clear()
        self.committed_ranks = []

    def reanchor(
        self,
        ranks: list[str],
        cand_ranks: tuple[str, ...] | None = None,
        cand_count: int | None = None,
    ) -> None:
        """Re-anchor committed ranks after a split or auto-resync without re-emitting known cards."""
        self.committed_ranks = list(ranks)
        self._cand_ranks = tuple(ranks) if cand_ranks is None else cand_ranks
        self._cand_dets = ()
        self._cand_count = self.confirm_frames if cand_count is None else max(0, int(cand_count))
        self._empty_count = 0
        self._wipe_dets = ()
        self._slot_cands.clear()
        self._pending_tail.clear()

    def update(self, detections: list[CardDetection]) -> tuple[list[tuple[int, CardDetection]], bool]:
        """Feed one frame's detections; returns (newly committed (slot_idx, CardDetection), is_cleared_now)."""
        if not detections:
            if self._empty_count == 0 and self._cand_dets and len(self._cand_dets) >= len(self.committed_ranks) and self.committed_ranks:
                self._wipe_dets = self._cand_dets
            self._empty_count += 1
            self._cand_ranks = ()
            self._cand_dets = ()
            self._cand_count = 0
            self._slot_cands.clear()
            return [], self._empty_count >= self.clear_frames

        self._empty_count = 0
        ranks = tuple(d.rank for d in detections)
        if ranks == self._cand_ranks:
            self._cand_count += 1
            self._cand_dets = tuple(detections)
        else:
            self._cand_ranks = ranks
            self._cand_dets = tuple(detections)
            self._cand_count = 1
        if self.fast_tail_confirm and self.committed_ranks and len(detections) >= len(self.committed_ranks):
            prefix_ok_wipe = all(
                detections[k].rank == self.committed_ranks[k]
                for k in range(len(self.committed_ranks))
                if self.committed_ranks[k] != "BACK"
            )
            if prefix_ok_wipe:
                self._wipe_dets = tuple(detections)

        max_tracked = max(
            len(self.committed_ranks),
            (max(self._pending_tail.keys()) + 1) if self._pending_tail else 0,
        )
        partial_wipe = bool(self.fast_tail_confirm and self.committed_ranks and len(detections) < max_tracked)
        committed_wipe = len(detections) < len(self.committed_ranks)
        prefix_ok = (not committed_wipe) and all(
            detections[k].rank == self.committed_ranks[k]
            for k in range(len(self.committed_ranks))
            if self.committed_ranks[k] != "BACK"
        )
        if self.committed_ranks and not prefix_ok and not committed_wipe:
            # Ignore misaligned / prepended ghost glitch frames so committed slots never duplicate
            self._slot_cands.clear()
            return [], False

        seen_slots: set[int] = set()
        prefix_stable = True
        for idx, det in enumerate(detections):
            is_uncommitted = idx >= len(self.committed_ranks) or self.committed_ranks[idx] == "BACK"
            if not is_uncommitted:
                continue
            if det.rank == "BACK":
                self._slot_cands.pop(idx, None)
                continue
            seen_slots.add(idx)
            prev = self._slot_cands.get(idx)
            if prefix_stable and prev is not None and prev[0] == det.rank:
                cnt = prev[1] + 1
            else:
                cnt = 1
                if prev is not None and prev[0] != det.rank:
                    prefix_stable = False
            if ranks == self._cand_ranks and self._cand_count > cnt:
                cnt = self._cand_count
            self._slot_cands[idx] = (det.rank, cnt, det)
            if self.fast_tail_confirm and self.committed_ranks:
                self._pending_tail[idx] = (det, 0)

        if not partial_wipe:
            for idx in list(self._slot_cands.keys()):
                if idx not in seen_slots:
                    self._slot_cands.pop(idx, None)
            if self.fast_tail_confirm and self.committed_ranks:
                for idx in list(self._pending_tail.keys()):
                    if idx not in seen_slots:
                        det_p, missed = self._pending_tail[idx]
                        if missed + 1 >= 4:
                            self._pending_tail.pop(idx, None)
                        else:
                            self._pending_tail[idx] = (det_p, missed + 1)
        elif not committed_wipe:
            # Trailing uncommitted card faded early before committed prefix: keep in _pending_tail for wipe rescue
            for idx in list(self._pending_tail.keys()):
                if idx not in seen_slots:
                    det_p, missed = self._pending_tail[idx]
                    if missed + 1 >= 5:
                        self._pending_tail.pop(idx, None)
                    else:
                        self._pending_tail[idx] = (det_p, missed + 1)

        if committed_wipe:
            return [], False

        emitted: list[tuple[int, CardDetection]] = []
        for idx in range(min(len(self.committed_ranks), len(detections))):
            if self.committed_ranks[idx] == "BACK" and idx in self._slot_cands:
                rk, cnt, det = self._slot_cands[idx]
                fast_ok = self.fast_tail_confirm and (
                    det.conf >= self.fast_tail_min_conf or (det.suit is not None and det.conf >= 0.78)
                )
                req = min(self.confirm_frames, 2) if fast_ok else self.confirm_frames
                if cnt >= req:
                    self.committed_ranks[idx] = rk
                    emitted.append((idx, det))
                    self._slot_cands.pop(idx, None)
                    self._pending_tail.pop(idx, None)

        while "BACK" not in self.committed_ranks and len(self.committed_ranks) < len(detections):
            idx = len(self.committed_ranks)
            if detections[idx].rank == "BACK":
                if idx > 0:
                    self.committed_ranks.append("BACK")
                break
            if idx not in self._slot_cands:
                break
            rk, cnt, det = self._slot_cands[idx]
            use_fast = (self.fast_tail_confirm and idx >= 1) or (self.fast_initial_deal and idx < 2)
            fast_ok = use_fast and (
                det.conf >= self.fast_tail_min_conf or (det.suit is not None and det.conf >= 0.80)
            )
            req = min(self.confirm_frames, 2) if fast_ok else self.confirm_frames
            if cnt < req:
                break
            self.committed_ranks.append(rk)
            emitted.append((idx, det))
            self._slot_cands.pop(idx, None)
            self._pending_tail.pop(idx, None)

        if emitted:
            self._wipe_dets = ()
        return emitted, False

    def flush_pending_on_wipe(self, min_conf: float = 0.80) -> list[tuple[int, CardDetection]]:
        """Flush high-confidence pending reveal or hit cards caught right before table wipe or partial clear."""
        if not self.committed_ranks:
            return []
        merged: dict[int, CardDetection] = {
            idx: det for idx, (det, missed) in self._pending_tail.items() if missed <= 4
        }
        fallback_seq = self._cand_dets if len(self._cand_dets) >= len(self.committed_ranks) else self._wipe_dets
        for idx, det in enumerate(fallback_seq):
            if idx not in merged:
                merged[idx] = det

        def _can_rescue(idx: int, det: CardDetection) -> bool:
            if det.rank == "BACK":
                return False
            if det.conf >= min_conf:
                return True
            cand = self._slot_cands.get(idx)
            return cand is not None and cand[0] == det.rank and cand[1] >= 2 and det.conf >= 0.78

        emitted: list[tuple[int, CardDetection]] = []
        for idx in range(len(self.committed_ranks)):
            if self.committed_ranks[idx] == "BACK" and idx in merged:
                det = merged[idx]
                if _can_rescue(idx, det):
                    self.committed_ranks[idx] = det.rank
                    emitted.append((idx, det))

        if "BACK" not in self.committed_ranks:
            while True:
                idx = len(self.committed_ranks)
                det = merged.get(idx)
                if det is None or not _can_rescue(idx, det):
                    break
                self.committed_ranks.append(det.rank)
                emitted.append((idx, det))

        self._wipe_dets = ()
        self._pending_tail.clear()
        self._slot_cands.clear()
        return emitted


@dataclass
class KeepaliveAction:
    """Action returned by WatchKeepaliveManager for companion loop & WebSocket bridge."""

    flush_auto_watch: bool = False
    trigger_refresh: bool = False
    preferred_method: str = ""
    emit_alert: dict[str, Any] | None = None
    status_changed: bool = False


class WatchKeepaliveManager:
    """Watch-Mode-only inactivity guard that schedules safe browser refreshes during ROUND_OVER / early PLACE_BETS."""

    def __init__(self, cfg: KeepaliveConfig, rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self._rng = rng or random.Random()
        self.watch_elapsed_sec: float = 0.0
        self.target_sec: float = self._sample_target()
        self.state: str = "OFF" if (not cfg.enabled or cfg.mode == "off") else "WATCH_COUNTDOWN"
        self._last_tick_ts: float | None = None

        self._place_bets_since: float | None = None
        self._place_bets_miss_streak: int = 0
        self._window_kind: str = ""
        self._last_banner_seen: str = ""
        self._dispatch_had_banner: bool = False
        self._scheduled_fire_ts: float | None = None
        self._alert_sent_for_window: bool = False
        self._hard_warn_sent: bool = False

        self._appcommand_verified: bool | None = None
        self._appcommand_sent_ts: float | None = None
        self._refresh_sent_ts: float | None = None
        self._ctrl_r_tried_for_reload: bool = False
        self._warn_retry_after_ts: float | None = None
        self.refresh_started_ts: float | None = None
        self.grace_until_ts: float | None = None
        self.saw_reload_interruption: bool = False
        self.last_tab_event: str = ""
        self.tab_complete_since: float | None = None
        self.recovery_banner_streak: int = 0
        self.refresh_count: int = 0
        self.last_refresh_epoch: float | None = None
        self.last_method_used: str = ""
        self._last_bet_rounds_count: int | None = None
        self._manual_blank_streak: int = 0
        self._manual_reloading: bool = False

    def _sample_target(self) -> float:
        lo = min(self.cfg.min_watch_sec, self.cfg.max_watch_sec)
        hi = max(self.cfg.min_watch_sec, self.cfg.max_watch_sec)
        return float(self._rng.uniform(lo, hi))

    def in_grace_period(self, now: float) -> bool:
        return (
            self.state in ("REFRESHING", "RECOVERING")
            and self.grace_until_ts is not None
            and now < self.grace_until_ts
        )

    def update_config(self, cfg: KeepaliveConfig, reset_method_probe: bool = False) -> None:
        old_method = self.cfg.method
        self.cfg = cfg
        if reset_method_probe or cfg.method != old_method:
            self._appcommand_verified = None
        if not cfg.enabled or cfg.mode == "off":
            self.state = "OFF"
            self._place_bets_since = None
            self._scheduled_fire_ts = None
            self.grace_until_ts = None
        else:
            self.target_sec = max(self.cfg.min_watch_sec, min(self.cfg.max_watch_sec, self.target_sec))
            if self.state == "OFF":
                self.state = "WATCH_COUNTDOWN"

    def reset_timer(self, reason: str = "manual", now: float | None = None) -> None:
        self.watch_elapsed_sec = 0.0
        self.target_sec = self._sample_target()
        self._place_bets_since = None
        self._place_bets_miss_streak = 0
        self._window_kind = ""
        self._dispatch_had_banner = False
        self._scheduled_fire_ts = None
        self._alert_sent_for_window = False
        self._hard_warn_sent = False
        self._appcommand_sent_ts = None
        self._refresh_sent_ts = None
        self._ctrl_r_tried_for_reload = False
        self._warn_retry_after_ts = None
        self.grace_until_ts = None
        self.refresh_started_ts = None
        self.saw_reload_interruption = False
        self.last_tab_event = ""
        self.tab_complete_since = None
        self.recovery_banner_streak = 0
        self._manual_blank_streak = 0
        self._manual_reloading = False
        if now is not None:
            self._last_tick_ts = now
        if not self.cfg.enabled or self.cfg.mode == "off":
            self.state = "OFF"
        elif reason != "pause":
            self.state = "WATCH_COUNTDOWN"

    def choose_method(self) -> str:
        if self.cfg.method in ("ext_then_os", "ext_reload"):
            return "ext_reload"
        if self.cfg.method in ("click_reload", "click_reload_then_f5"):
            return "click_reload"
        if self.cfg.method == "sendinput_ctrl_r":
            return "sendinput_ctrl_r"
        if self.cfg.method == "sendinput_f5" or self._appcommand_verified is False:
            return "sendinput_f5"
        if self.cfg.method in ("appcommand", "appcommand_then_f5"):
            return "appcommand"
        return "ext_reload"

    def notify_tab_event(self, event_status: str, now: float | None = None) -> None:
        """Record browser-reported chrome.tabs.onUpdated status during active reload."""
        if self.state not in ("REFRESHING", "RECOVERING"):
            return
        st = str(event_status or "").lower()
        self.last_tab_event = st
        if st in ("loading", "complete"):
            self.saw_reload_interruption = True
        if st == "complete":
            self.tab_complete_since = time.monotonic() if now is None else now

    def arm_immediate_refresh(self, now: float) -> None:
        """Force the watch timer past threshold so the next or active ROUND_OVER / PLACE_BETS triggers immediately."""
        self.watch_elapsed_sec = max(self.watch_elapsed_sec, self.target_sec)
        self.state = "ARMED_NEXT_BETS"
        self._scheduled_fire_ts = now
        self._warn_retry_after_ts = None

    def mark_refresh_dispatched(self, now: float, ok: bool, used_method: str, epoch_ts: float | None = None) -> dict[str, Any]:
        if not ok:
            self.state = "WARN"
            self._scheduled_fire_ts = None
            self.grace_until_ts = None
            self._appcommand_sent_ts = None
            self._refresh_sent_ts = None
            self._warn_retry_after_ts = now + 4.0
            if used_method == "appcommand_post_failed":
                self._appcommand_verified = False
            return {
                "type": "keepalive_status",
                "event": "refresh_failed",
                "reason": used_method,
                "keepalive": self.to_status(now),
            }
        self.state = "RECOVERING"
        self.refresh_started_ts = now
        self.grace_until_ts = now + self.cfg.reload_grace_sec
        self.saw_reload_interruption = False
        self._dispatch_had_banner = bool(self._last_banner_seen)
        self.last_tab_event = ""
        self.tab_complete_since = None
        self.recovery_banner_streak = 0
        self.last_method_used = used_method
        self._appcommand_sent_ts = now if used_method == "appcommand" else None
        self._refresh_sent_ts = now
        self._warn_retry_after_ts = None
        if used_method == "sendinput_ctrl_r":
            self._ctrl_r_tried_for_reload = True
        else:
            self._ctrl_r_tried_for_reload = False
        return {
            "type": "keepalive_status",
            "event": "refreshing",
            "method": used_method,
            "graceSec": self.cfg.reload_grace_sec,
            "epochTs": epoch_ts or time.time(),
            "keepalive": self.to_status(now),
        }

    def tick(
        self,
        now: float,
        *,
        play_mode: str,
        paused: bool,
        window_found: bool,
        sm_state: str,
        banner_state: str,
        obs_has_cards: bool,
        has_unresolved_prompt: bool,
        bet_rounds_count: int = 0,
        user_idle_ms: int = 10000,
        reload_luma_shift: bool = False,
        uniform_loading_screen: bool = False,
    ) -> KeepaliveAction:
        action = KeepaliveAction()
        prev_state = self.state

        if self._last_tick_ts is None:
            dt = 0.0
        else:
            dt = min(5.0, max(0.0, now - self._last_tick_ts))
        self._last_tick_ts = now

        # Reset Watch timer automatically when a real BET round settles & logs
        if self._last_bet_rounds_count is None:
            self._last_bet_rounds_count = bet_rounds_count
        elif bet_rounds_count > self._last_bet_rounds_count:
            self._last_bet_rounds_count = bet_rounds_count
            if self.cfg.reset_on_bet_round:
                self.reset_timer(reason="bet_logged", now=now)
                action.status_changed = True
                return action
        elif bet_rounds_count < self._last_bet_rounds_count:
            self._last_bet_rounds_count = bet_rounds_count

        if not self.cfg.enabled or self.cfg.mode == "off":
            self.state = "OFF"
            self._place_bets_since = None
            self._scheduled_fire_ts = None
            if self.state != prev_state:
                action.status_changed = True
            return action

        if banner_state:
            self._last_banner_seen = banner_state
        elif self.state not in ("REFRESHING", "RECOVERING"):
            self._last_banner_seen = ""

        # Handle active post-refresh reload & grace period recovery
        if self.state in ("REFRESHING", "RECOVERING"):
            started = self.refresh_started_ts if self.refresh_started_ts is not None else now
            elapsed_reload = now - started
            if (
                (not window_found)
                or reload_luma_shift
                or uniform_loading_screen
                or (self._dispatch_had_banner and banner_state == "")
            ):
                self.saw_reload_interruption = True
                if self._appcommand_sent_ts is not None:
                    self._appcommand_verified = True

            # Verify refresh actually blanked/reloaded the page; escalate (ext_reload -> click_reload -> F5 -> Ctrl+R)
            verify_elapsed = (now - self._refresh_sent_ts) if self._refresh_sent_ts is not None else 0.0
            can_escalate_now = (
                play_mode == "watch"
                and window_found
                and (banner_state in ("", "PLACE_BETS") or sm_state == "ROUND_OVER")
                and banner_state not in ("LAST_BETS", "NO_MORE_BETS", "WAIT")
                and not obs_has_cards
            )
            if (
                self._refresh_sent_ts is not None
                and not self.saw_reload_interruption
                and verify_elapsed >= max(0.6, self.cfg.appcommand_verify_sec)
            ):
                if self.last_method_used == "ext_reload":
                    self._refresh_sent_ts = None
                    if self.cfg.method == "ext_then_os" and can_escalate_now:
                        self.state = "REFRESHING"
                        action.trigger_refresh = True
                        action.preferred_method = "click_reload"
                        action.status_changed = True
                        return action
                elif self.last_method_used == "click_reload":
                    self._refresh_sent_ts = None
                    if self.cfg.method in ("ext_then_os", "click_reload_then_f5") and can_escalate_now:
                        self.state = "REFRESHING"
                        action.trigger_refresh = True
                        action.preferred_method = "sendinput_f5"
                        action.status_changed = True
                        return action
                elif self.last_method_used == "appcommand":
                    self._appcommand_verified = False
                    self._appcommand_sent_ts = None
                    self._refresh_sent_ts = None
                    if self.cfg.method == "appcommand_then_f5" and can_escalate_now:
                        self.state = "REFRESHING"
                        action.trigger_refresh = True
                        action.preferred_method = "sendinput_f5"
                        action.status_changed = True
                        return action
                elif (
                    self.last_method_used == "sendinput_f5"
                    and not self._ctrl_r_tried_for_reload
                    and self.cfg.method in ("ext_then_os", "click_reload_then_f5", "sendinput_f5", "appcommand_then_f5")
                ):
                    self._ctrl_r_tried_for_reload = True
                    self._refresh_sent_ts = None
                    if can_escalate_now:
                        self.state = "REFRESHING"
                        action.trigger_refresh = True
                        action.preferred_method = "sendinput_ctrl_r"
                        action.status_changed = True
                        return action

            banner_recovered = (
                window_found
                and banner_state in ("PLACE_BETS", "LAST_BETS", "NO_MORE_BETS", "WAIT")
                and elapsed_reload >= 2.0
                and self.saw_reload_interruption
            )
            tab_settled_recovered = (
                window_found
                and self.last_tab_event == "complete"
                and self.tab_complete_since is not None
                and (now - self.tab_complete_since) >= 1.5
                and elapsed_reload >= 5.0
                and not uniform_loading_screen
                and self.saw_reload_interruption
            )
            if banner_recovered:
                self.recovery_banner_streak += 1
            elif tab_settled_recovered:
                self.recovery_banner_streak = max(self.recovery_banner_streak + 1, 2)
            else:
                self.recovery_banner_streak = 0

            need_streak = 1 if banner_state in ("NO_MORE_BETS", "WAIT") else 2
            if self.recovery_banner_streak >= need_streak:
                self.refresh_count += 1
                self.last_refresh_epoch = time.time()
                self.reset_timer(reason="reloaded", now=now)
                self.state = "WATCH_COUNTDOWN" if play_mode == "watch" else "PAUSED_BET"
                action.status_changed = True
                action.emit_alert = {
                    "type": "keepalive_status",
                    "event": "recovered",
                    "reloadSec": round(elapsed_reload, 1),
                    "keepalive": self.to_status(now),
                }
                return action

            if elapsed_reload >= self.cfg.reload_timeout_sec:
                self.state = "WARN"
                self.grace_until_ts = None
                self._refresh_sent_ts = None
                self._warn_retry_after_ts = now + 4.0
                action.status_changed = True
                action.emit_alert = {
                    "type": "keepalive_status",
                    "event": "timeout_warn",
                    "reloadSec": round(elapsed_reload, 1),
                    "keepalive": self.to_status(now),
                }
                return action

            return action

        # Never arm or refresh when paused or actively in BET mode
        if paused:
            self._scheduled_fire_ts = None
            return action

        if play_mode != "watch":
            self.state = "PAUSED_BET"
            self._place_bets_since = None
            self._scheduled_fire_ts = None
            self._alert_sent_for_window = False
            if self.state != prev_state:
                action.status_changed = True
            return action

        # Watch mode active: count idle watch time cleanly
        if window_found:
            self.watch_elapsed_sec += dt

        # Detect manual F5 reload in Watch Mode so timer resets cleanly
        if banner_state == "" and not obs_has_cards and (uniform_loading_screen or not window_found):
            self._manual_blank_streak += 1
            if self._manual_blank_streak >= 4:
                self._manual_reloading = True
        elif self._manual_reloading and window_found and banner_state in ("PLACE_BETS", "LAST_BETS", "NO_MORE_BETS", "WAIT"):
            self.reset_timer(reason="manual_reload_detected", now=now)
            action.status_changed = True
            return action
        else:
            self._manual_blank_streak = 0

        # Track safe early betting window: ROUND_OVER (cards wiped before LAST_BETS) OR explicit PLACE_BETS banner
        in_fresh_place_bets = (
            window_found
            and sm_state == "BETTING"
            and banner_state == "PLACE_BETS"
            and not obs_has_cards
        )
        in_fresh_round_over = (
            window_found
            and sm_state == "ROUND_OVER"
            and banner_state not in ("LAST_BETS", "NO_MORE_BETS", "WAIT")
            and not obs_has_cards
        )
        in_safe_bet_window = in_fresh_place_bets or in_fresh_round_over
        hard_cancel_bet_window = (
            (not window_found)
            or obs_has_cards
            or banner_state in ("LAST_BETS", "NO_MORE_BETS", "WAIT")
            or sm_state not in ("BETTING", "IDLE", "ROUND_OVER")
        )
        if in_safe_bet_window:
            self._place_bets_miss_streak = 0
            if self._place_bets_since is None:
                self._place_bets_since = now
                self._window_kind = "ROUND_OVER" if in_fresh_round_over else "PLACE_BETS"
                self._alert_sent_for_window = False
            elif in_fresh_place_bets and self._window_kind != "ROUND_OVER":
                self._window_kind = "PLACE_BETS"
        elif hard_cancel_bet_window:
            self._place_bets_miss_streak = 0
            self._place_bets_since = None
            self._window_kind = ""
            self._scheduled_fire_ts = None
            self._alert_sent_for_window = False
        else:
            # Allow 1-2 frame empty-banner flickers without losing active schedule
            self._place_bets_miss_streak += 1
            if self._place_bets_miss_streak >= 3:
                self._place_bets_since = None
                self._window_kind = ""
                self._scheduled_fire_ts = None
                self._alert_sent_for_window = False

        if self.watch_elapsed_sec < self.target_sec:
            if self.state != "WARN":
                self.state = "WATCH_COUNTDOWN"
            if self.state != prev_state:
                action.status_changed = True
            return action

        # Hold WARN briefly after dispatch failure / timeout so retries don't tight-loop
        if self.state == "WARN" and self._warn_retry_after_ts is not None and now < self._warn_retry_after_ts:
            if not self._hard_warn_sent and self.watch_elapsed_sec >= self.cfg.hard_warn_sec:
                self._hard_warn_sent = True
                action.emit_alert = {
                    "type": "keepalive_status",
                    "event": "hard_warn",
                    "elapsedSec": round(self.watch_elapsed_sec, 1),
                    "keepalive": self.to_status(now),
                }
            return action

        # Timer elapsed threshold reached: arm for any clean ROUND_OVER / PLACE_BETS window (no LAST_BETS)
        self._warn_retry_after_ts = None
        self.state = "ARMED_NEXT_BETS"
        if self.state != prev_state:
            action.status_changed = True

        if not self._hard_warn_sent and self.watch_elapsed_sec >= self.cfg.hard_warn_sec:
            self._hard_warn_sent = True
            action.emit_alert = {
                "type": "keepalive_status",
                "event": "hard_warn",
                "elapsedSec": round(self.watch_elapsed_sec, 1),
                "keepalive": self.to_status(now),
            }

        if in_safe_bet_window and self._place_bets_since is not None:
            win_age = now - self._place_bets_since
            win_kind = self._window_kind or ("ROUND_OVER" if in_fresh_round_over else "PLACE_BETS")
            safe_max_age = max(self.cfg.place_bets_latest_sec, 6.5) if win_kind == "ROUND_OVER" else max(self.cfg.place_bets_latest_sec, 6.0)
            too_late_for_window = (
                win_kind == "ROUND_OVER"
                and win_age > safe_max_age
                and self.watch_elapsed_sec < self.cfg.hard_warn_sec
            )
            if self.cfg.mode == "auto" and not too_late_for_window:
                if self._scheduled_fire_ts is None:
                    if win_kind == "ROUND_OVER":
                        base_d = self.cfg.round_over_delay_sec
                        min_d = max(0.25, base_d - 0.15)
                        max_d = base_d + 0.15
                    else:
                        min_d = self.cfg.place_bets_min_delay_sec
                        max_d = max(min_d, self.cfg.place_bets_max_delay_sec)
                    if win_age <= min_d:
                        jitter = self._rng.uniform(min_d, max_d)
                        self._scheduled_fire_ts = max(now + 0.05, self._place_bets_since + jitter)
                    else:
                        # Timer finished mid-window within safe runway: trigger promptly
                        self._scheduled_fire_ts = now + self._rng.uniform(0.05, 0.25)
                if self._scheduled_fire_ts is not None and now >= self._scheduled_fire_ts:
                    self.state = "REFRESHING"
                    self._scheduled_fire_ts = None
                    self._ctrl_r_tried_for_reload = False
                    action.flush_auto_watch = True
                    action.trigger_refresh = True
                    action.preferred_method = self.choose_method()
                    action.status_changed = True
                    return action
            elif self.cfg.mode == "alert_only" and not self._alert_sent_for_window:
                self._alert_sent_for_window = True
                action.emit_alert = {
                    "type": "keepalive_status",
                    "event": "alert_place_bets",
                    "elapsedSec": round(self.watch_elapsed_sec, 1),
                    "keepalive": self.to_status(now),
                }
        elif (
            self.cfg.mode == "auto"
            and self.watch_elapsed_sec >= self.cfg.hard_warn_sec
            and window_found
            and sm_state == "SHUFFLING"
            and not obs_has_cards
        ):
            self.state = "REFRESHING"
            self._scheduled_fire_ts = None
            self._ctrl_r_tried_for_reload = False
            action.flush_auto_watch = True
            action.trigger_refresh = True
            action.preferred_method = self.choose_method()
            action.status_changed = True

        return action

    def to_status(self, now: float | None = None) -> dict[str, Any]:
        ts_now = time.monotonic() if now is None else now
        return {
            "enabled": bool(self.cfg.enabled and self.cfg.mode != "off"),
            "mode": self.cfg.mode,
            "method": self.cfg.method,
            "state": self.state,
            "elapsedSec": round(self.watch_elapsed_sec, 1),
            "targetSec": round(self.target_sec, 1),
            "minWatchSec": round(self.cfg.min_watch_sec, 1),
            "maxWatchSec": round(self.cfg.max_watch_sec, 1),
            "remainingSec": round(max(0.0, self.target_sec - self.watch_elapsed_sec), 1),
            "inGrace": self.in_grace_period(ts_now),
            "refreshCount": self.refresh_count,
            "lastMethod": self.last_method_used,
            "lastRefreshTs": self.last_refresh_epoch,
        }


class CompanionStateMachine:
    """Tracks live Playtech All Bets round progression and outputs tracker WebSocket events."""

    def __init__(self, cfg: CompanionConfig) -> None:
        self.cfg = cfg
        self.p_debouncers: dict[int, SequenceDebouncer] = {
            0: SequenceDebouncer(
                cfg.confirm_frames,
                cfg.clear_frames,
                fast_tail_min_conf=max(0.84, cfg.min_hud_confidence),
            )
        }
        self.d_debouncer = SequenceDebouncer(
            cfg.confirm_frames,
            cfg.clear_frames,
            fast_tail_confirm=True,
            fast_tail_min_conf=max(0.84, cfg.min_hud_confidence),
        )

        self.paused: bool = False
        self.sync_play_mode: str = "watch"
        self.sync_is_split: bool = False
        self.sync_draft_count: int = 0
        self.sync_track_felt: bool = False
        self.sync_round_id: str = ""
        self.sync_draft_hands: list[list[int]] | None = None
        self.sync_draft_hands_manual: list[bool] = []
        self.sync_draft_dealer: list[int] | None = None
        self.sync_draft_dealer_manual: bool = False
        self.sync_has_prompt: bool = False
        self.sync_bet_rounds_count: int = 0

        self.state: str = "IDLE"  # IDLE | BETTING | DEALING | PLAYER_TURN | DEALER_TURN | ROUND_OVER | SHUFFLING
        self._round_seq: int = 0
        self.round_id: str = "r_0"

        self._emitted_p: int = 0
        self._emitted_d: int = 0
        self._emitted_b: int = 0
        self._emitted_split_hands: int = 1
        self._split_detected: bool = False
        self._split_cand_count: int = 0
        self._split_cand_target: int = 1
        self._split_pending_seqs: dict[int, tuple[tuple[str, ...], int]] = {}
        self._split_seed_cards: dict[int, CardDetection] = {}
        self._uncertainty_flags: list[str] = []
        self._unread_community_pulses: int = 0

        self._recent_hud_p: list[tuple[float, int | None]] = []
        self._recent_hud_d: list[tuple[float, int | None]] = []
        self._pending_felt: list[FeltObservation] = []
        self._pending_dealer_felt: list[FeltObservation] = []
        self._round_felt_sightings: list[dict[str, Any]] = []
        self._last_sighting_by_zone: dict[tuple[str, str], float] = {}

        self._reconcile_sig: tuple[Any, ...] | None = None
        self._reconcile_frames: int = 0
        self._reconcile_since: float = 0.0
        self._last_reconcile_ts: float = -999.0

        self._last_banner_emitted: str = ""
        self._last_bet_banner: str = ""
        self._seen_wait_in_round: bool = False
        self._wait_absent_frames: int = 0
        self._wait_missing_with_cards_since: float | None = None
        self._blank_card_frames: int = 0
        self._saw_clear_since_round_over: bool = True
        self._saw_empty_hud_in_betting: bool = False
        self._predeal_obs: FrameObservation | None = None
        self._shuffle_since: float | None = None
        self._shuffle_emitted: bool = False
        self._pending_auto_watch: dict[str, Any] | None = None

    @property
    def p_debouncer(self) -> SequenceDebouncer:
        return self.p_debouncers[0]

    @property
    def felt_tracking_armed(self) -> bool:
        """True only while betting AND the user explicitly toggled Community Extra [B]."""
        return self.sync_play_mode == "bet" and self.sync_track_felt

    def _reset_round_accumulators(self) -> None:
        self.p_debouncers = {
            0: SequenceDebouncer(
                self.cfg.confirm_frames,
                self.cfg.clear_frames,
                fast_tail_min_conf=max(0.84, self.cfg.min_hud_confidence),
            )
        }
        self.d_debouncer.reset()
        self._emitted_p = 0
        self._emitted_d = 0
        self._emitted_b = 0
        self._emitted_split_hands = 1
        self._split_detected = False
        self._split_cand_count = 0
        self._split_cand_target = 1
        self._split_pending_seqs.clear()
        self._split_seed_cards.clear()
        self._reconcile_sig = None
        self._reconcile_frames = 0
        self._reconcile_since = 0.0
        self._uncertainty_flags.clear()
        self._unread_community_pulses = 0
        self._recent_hud_p.clear()
        self._recent_hud_d.clear()
        self._pending_felt.clear()
        self._pending_dealer_felt.clear()
        self._round_felt_sightings.clear()
        self._last_sighting_by_zone.clear()
        self._last_bet_banner = ""
        self._seen_wait_in_round = False
        self._wait_absent_frames = 0
        self._wait_missing_with_cards_since = None
        self._blank_card_frames = 0
        self._saw_empty_hud_in_betting = False
        self._predeal_obs = None

    def _start_new_round(self) -> None:
        self._round_seq += 1
        self.round_id = f"r_{self._round_seq}"
        self._reset_round_accumulators()
        self.state = "DEALING"

    def _seed_predeal_obs(self, predeal: FrameObservation) -> None:
        """Pre-seed debouncers with a clean pre-deal frame caught right before WAIT/DEALING woke."""
        if self.cfg.confirm_frames < 2:
            return
        p_hands = (
            {h: list(cs) for h, cs in predeal.player_hands.items() if cs}
            if predeal.player_hands
            else ({0: list(predeal.player_cards)} if predeal.player_cards else {})
        )
        if len(p_hands) == 1 and 0 in p_hands:
            self.p_debouncers[0].update(p_hands[0])
        if predeal.dealer_cards:
            self.d_debouncer.update(list(predeal.dealer_cards))

    def set_sync_state(
        self,
        play_mode: str | None = None,
        is_split: bool | None = None,
        draft_count: int | None = None,
        track_felt: bool | None = None,
        comp_round_id: str | None = None,
        draft_hands: list[list[int]] | None = None,
        draft_dealer: list[int] | None = None,
        draft_hands_manual: list[bool] | None = None,
        draft_dealer_manual: bool | None = None,
        has_prompt: bool | None = None,
        bet_rounds_count: int | None = None,
    ) -> None:
        was_armed = self.felt_tracking_armed
        if play_mode in ("watch", "bet"):
            self.sync_play_mode = play_mode
            if play_mode == "watch":
                self.sync_track_felt = False
        if track_felt is not None:
            self.sync_track_felt = bool(track_felt) and self.sync_play_mode == "bet"
        if not self.felt_tracking_armed:
            self._pending_felt.clear()
        elif not was_armed:
            # User just armed [B] after standing: clear pre-Stand HUD dedup so matching ranks still count as [B]
            self._recent_hud_p.clear()
        if is_split is not None:
            self.sync_is_split = bool(is_split)
            if self.sync_is_split:
                self._split_detected = True
        if draft_count is not None:
            new_count = max(0, int(draft_count))
            if new_count == 0 and self._pending_auto_watch is not None:
                # User manually logged or cleared the review draft in UI.
                self._pending_auto_watch = None
            self.sync_draft_count = new_count
        if comp_round_id is not None:
            self.sync_round_id = str(comp_round_id)
        if draft_hands is not None:
            self.sync_draft_hands = [[int(v) for v in h] for h in draft_hands]
        if draft_dealer is not None:
            self.sync_draft_dealer = [int(v) for v in draft_dealer]
        if draft_hands_manual is not None:
            self.sync_draft_hands_manual = [bool(x) for x in draft_hands_manual]
        if draft_dealer_manual is not None:
            self.sync_draft_dealer_manual = bool(draft_dealer_manual)
        if has_prompt is not None:
            self.sync_has_prompt = bool(has_prompt)
        if bet_rounds_count is not None:
            self.sync_bet_rounds_count = max(0, int(bet_rounds_count))

    def resync_round(self, clear_pending: bool = True) -> None:
        """Reset transient round accumulators without changing shoe/sync settings."""
        if clear_pending:
            self._pending_auto_watch = None
        self._reset_round_accumulators()
        self._last_banner_emitted = ""
        self._saw_clear_since_round_over = True
        self.state = "IDLE"

    def _flush_pending_auto_watch(self, events: list[dict[str, Any]]) -> None:
        if self._pending_auto_watch is not None:
            events.append(self._pending_auto_watch)
            self._pending_auto_watch = None

    def _begin_round_if_idle(self, events: list[dict[str, Any]] | None = None) -> None:
        if self.state in ("IDLE", "ROUND_OVER"):
            if events is not None:
                self._flush_pending_auto_watch(events)
            self._round_seq += 1
            self.round_id = f"r_{self._round_seq}"
            self.state = "DEALING"

    def _record_hud_p_for_dedup(self, ts: float, val: int | None) -> None:
        self._recent_hud_p.append((ts, val))
        win = self.cfg.hud_dedup_window_sec
        # Consume matching pending felt arrival OR blurred felt pulse caused by quick card motion
        for i, f in enumerate(self._pending_felt):
            if abs(ts - f.ts) <= win and ((f.matched and f.val == val) or not f.matched):
                self._pending_felt.pop(i)
                return

    def _record_hud_d_for_dedup(self, ts: float, val: int | None) -> None:
        self._recent_hud_d.append((ts, val))
        win = self.cfg.hud_dedup_window_sec
        for i, f in enumerate(self._pending_dealer_felt):
            if abs(ts - f.ts) <= win and ((f.matched and f.val == val) or not f.matched):
                self._pending_dealer_felt.pop(i)
                return

    def _handle_dealer_felt_event(self, obs: FeltObservation) -> None:
        win = self.cfg.hud_dedup_window_sec
        if obs.matched:
            for i, (hts, hval) in enumerate(self._recent_hud_d):
                if 0.0 <= (obs.ts - hts) <= win and hval == obs.val:
                    self._recent_hud_d.pop(i)
                    return
            self._pending_dealer_felt.append(obs)

    def _maybe_emit_felt_sighting(self, obs: FeltObservation, events: list[dict[str, Any]]) -> None:
        """Emit a passive 'felt_sighting' advisory event when a physical felt card is recognized."""
        min_trusted = min(self.cfg.min_felt_confidence, max(0.70, self.cfg.min_felt_confidence - 0.04))
        if not obs.matched or obs.val is None or not obs.rank or obs.conf < min_trusted:
            return
        win = self.cfg.hud_dedup_window_sec
        if obs.zone == "dealer":
            if any(0.0 <= (obs.ts - hts) <= win and hval == obs.val for hts, hval in self._recent_hud_d):
                return
        else:
            if any(0.0 <= (obs.ts - hts) <= win and hval == obs.val for hts, hval in self._recent_hud_p):
                return
        key = (obs.zone, obs.rank)
        last_ts = self._last_sighting_by_zone.get(key, -999.0)
        if (obs.ts - last_ts) < 1.5:
            return
        self._last_sighting_by_zone[key] = obs.ts
        sighting = {
            "type": "felt_sighting",
            "zone": obs.zone,
            "val": obs.val,
            "rankLabel": obs.rank,
            "conf": round(obs.conf, 3),
            "roundId": self.round_id,
        }
        self._round_felt_sightings.append(sighting)
        events.append(sighting)

    def _handle_felt_event(self, obs: FeltObservation) -> None:
        win = self.cfg.hud_dedup_window_sec
        # Check if HUD already emitted this exact card within recent dedup window
        if obs.matched:
            for i, (hts, hval) in enumerate(self._recent_hud_p):
                if 0.0 <= (obs.ts - hts) <= win and hval == obs.val:
                    self._recent_hud_p.pop(i)
                    return
        self._pending_felt.append(obs)

    def _flush_unmatched_felt(self, now: float, force_all: bool = False) -> list[dict[str, Any]]:
        """Flush physical felt hits that were NOT echoed by Player HUD into Community Extra [B]."""
        events: list[dict[str, Any]] = []
        keep: list[FeltObservation] = []
        win = self.cfg.hud_dedup_window_sec

        min_trusted_felt = min(self.cfg.min_felt_confidence, max(0.70, self.cfg.min_felt_confidence - 0.04))
        for obs in self._pending_felt:
            if not force_all and (now - obs.ts) < min(0.8, win * 0.45):
                keep.append(obs)
                continue
            if obs.matched and obs.val is not None and obs.conf >= min_trusted_felt:
                self._emitted_b += 1
                events.append(
                    {
                        "type": "card",
                        "target": "B",
                        "val": obs.val,
                        "slot": self._emitted_b - 1,
                        "rankLabel": obs.rank,
                        "conf": round(obs.conf, 3),
                        "source": "felt",
                        "roundId": self.round_id,
                    }
                )
            elif self.cfg.pause_on_unmatched_felt:
                self._unread_community_pulses += 1
                if "MISSED_COMMUNITY_CARD" not in self._uncertainty_flags:
                    self._uncertainty_flags.append("MISSED_COMMUNITY_CARD")

        self._pending_felt = keep
        return events

    def _emit_dealer_cards(
        self,
        pairs: list[tuple[int, CardDetection]],
        events: list[dict[str, Any]],
        ts: float = 0.0,
        source: str = "hud",
    ) -> bool:
        dealer_flipped_or_drew = False
        for slot_idx, det in pairs:
            if det.val is None:
                continue
            self._begin_round_if_idle(events)
            self._emitted_d += 1
            if source == "hud":
                self._record_hud_d_for_dedup(ts, det.val)
            if slot_idx >= 1:
                dealer_flipped_or_drew = True
            if det.conf < self.cfg.min_auto_confidence:
                self._uncertainty_flags.append(f"D{slot_idx + 1}:{det.rank}@{det.conf:.2f}")
            evt_d: dict[str, Any] = {
                "type": "card",
                "target": "D",
                "val": det.val,
                "slot": slot_idx,
                "rankLabel": det.rank,
                "conf": round(det.conf, 3),
                "source": source,
                "roundId": self.round_id,
            }
            if det.suit:
                evt_d["suit"] = det.suit
            events.append(evt_d)
        return dealer_flipped_or_drew

    def _finalize_cleared_round(self, now: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        rescued_d = self.d_debouncer.flush_pending_on_wipe(min_conf=max(0.78, self.cfg.min_hud_confidence - 0.06))
        if rescued_d:
            self._emit_dealer_cards(rescued_d, events, ts=now)
        if self.felt_tracking_armed:
            events.extend(self._flush_unmatched_felt(now, force_all=True))
        else:
            self._pending_felt.clear()
        total_emitted = self._emitted_p + self._emitted_d + self._emitted_b
        if total_emitted == 0 and self._unread_community_pulses == 0:
            self.sync_track_felt = False
            self.resync_round(clear_pending=False)
            return events

        p_hands_ranks = [
            [r for r in db.committed_ranks if r != "BACK"]
            for db in self.p_debouncers.values()
            if db.committed_ranks
        ]
        any_player_alive = any(not _eval_bj_ranks(hr)[2] for hr in p_hands_ranks) if p_hands_ranks else True
        d_committed = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
        d_total, _, _ = _eval_bj_ranks(d_committed)

        # Automatic dealer_felt_roi fallback if the digital dealer HUD cleared incomplete (< 17 or < 2 cards)
        min_trusted_felt = min(self.cfg.min_felt_confidence, max(0.70, self.cfg.min_felt_confidence - 0.04))
        if (
            self.cfg.enable_dealer_felt_fallback
            and self._pending_dealer_felt
            and self._emitted_p >= 2
            and any_player_alive
            and (len(d_committed) < 2 or d_total < 17)
        ):
            for f_obs in list(self._pending_dealer_felt):
                if not (f_obs.matched and f_obs.val is not None and f_obs.rank and f_obs.conf >= min_trusted_felt):
                    continue
                if len(d_committed) >= 2 and d_total >= 17:
                    break
                if "BACK" in self.d_debouncer.committed_ranks:
                    slot_idx = self.d_debouncer.committed_ranks.index("BACK")
                    self.d_debouncer.committed_ranks[slot_idx] = f_obs.rank
                else:
                    slot_idx = len(self.d_debouncer.committed_ranks)
                    self.d_debouncer.committed_ranks.append(f_obs.rank)
                det_fb = CardDetection(rank=f_obs.rank, val=f_obs.val, conf=f_obs.conf, x=0)
                self._emit_dealer_cards([(slot_idx, det_fb)], events, ts=now, source="dealer_felt_fallback")
                d_committed = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
                d_total, _, _ = _eval_bj_ranks(d_committed)
        self._pending_dealer_felt.clear()

        flags = list(self._uncertainty_flags)
        split_clean_multi = (
            self._emitted_split_hands >= 2
            and len(self.p_debouncers) >= 2
            and all(len(db.committed_ranks) >= 2 for db in self.p_debouncers.values())
        )
        if self._emitted_p < 2 or self._emitted_d < 1:
            flags.append("HUD_GLITCH")

        dealer_incomplete = (
            self._emitted_p >= 2
            and any_player_alive
            and (
                (len(d_committed) >= 2 and d_total < 17)
                or (len(d_committed) == 1 and "BACK" not in self.d_debouncer.committed_ranks)
            )
        )
        if dealer_incomplete and "HUD_GLITCH" not in flags and "DEALER_INCOMPLETE" not in flags:
            flags.append("DEALER_INCOMPLETE")

        split_blocks_auto = (self._split_detected or self.sync_is_split) and not split_clean_multi
        can_auto_watch = (
            self.sync_play_mode == "watch"
            and not split_blocks_auto
            and self._unread_community_pulses == 0
            and not flags
            and self._emitted_d >= 1
        )

        if can_auto_watch:
            sum_p = (
                f"{self._emitted_p}P({self._emitted_split_hands}H)"
                if self._emitted_split_hands >= 2
                else f"{self._emitted_p}P"
            )
            # Stage auto_watch until NO_MORE_BETS so the user can review/edit recorded cards during PLACE_BETS/LAST_BETS
            self._flush_pending_auto_watch(events)
            self._pending_auto_watch = {
                "type": "auto_watch",
                "roundId": self.round_id,
                "handCount": self._emitted_split_hands,
                "summary": f"{sum_p}+{self._emitted_d}D+{self._emitted_b}B",
                "fromWatch": True,
            }
        else:
            if split_blocks_auto or (self.sync_play_mode == "bet" and (self._split_detected or self.sync_is_split)):
                reason = "SPLIT"
            elif self.sync_play_mode == "bet":
                reason = "BET_MODE"
            elif self._unread_community_pulses > 0:
                reason = "MISSED_COMMUNITY_CARD"
            elif "HUD_GLITCH" in flags:
                reason = "HUD_GLITCH"
            elif "DEALER_INCOMPLETE" in flags:
                reason = "DEALER_INCOMPLETE"
            else:
                reason = "UNCERTAIN_READ"

            focus = "D" if "DEALER_INCOMPLETE" in flags else ("B" if reason == "MISSED_COMMUNITY_CARD" else "P")
            events.append(
                {
                    "type": "prompt_confirm",
                    "roundId": self.round_id,
                    "reason": reason,
                    "unreadCommunity": self._unread_community_pulses,
                    "uncertainSlots": flags,
                    "suggestedFocus": focus,
                    "dealerTotal": d_total,
                    "feltSightings": list(self._round_felt_sightings),
                }
            )

        self.sync_track_felt = False
        self.resync_round(clear_pending=not can_auto_watch)
        return events

    def _emit_banner_phase_if_changed(self, banner: str, conf: float, events: list[dict[str, Any]]) -> None:
        if banner and banner != self._last_banner_emitted:
            self._last_banner_emitted = banner
            events.append(
                {
                    "type": "phase",
                    "phase": banner,
                    "conf": round(conf, 3),
                    "roundId": self.round_id,
                }
            )

    def step(self, obs: FrameObservation) -> list[dict[str, Any]]:
        """Consume one FrameObservation and return zero or more WebSocket protocol events."""
        if self.paused:
            return []

        events: list[dict[str, Any]] = []

        if obs.dealer_only:
            if self.state not in ("DEALING", "PLAYER_TURN", "DEALER_TURN"):
                return []
            if obs.felt_events:
                for fe in obs.felt_events:
                    if fe.zone == "dealer":
                        self._handle_dealer_felt_event(fe)
                    elif self.felt_tracking_armed:
                        self._handle_felt_event(fe)
                    self._maybe_emit_felt_sighting(fe, events)
            new_d, _ = self.d_debouncer.update(obs.dealer_cards)
            dealer_flipped_or_drew = self._emit_dealer_cards(new_d, events, ts=obs.ts)
            if self.state == "DEALING" and len(self.d_debouncer.committed_ranks) >= 1 and self._emitted_p >= 1:
                self.state = "PLAYER_TURN"
            if dealer_flipped_or_drew:
                self.state = "DEALER_TURN"
                if self.felt_tracking_armed:
                    events.extend(self._flush_unmatched_felt(obs.ts, force_all=True))
                elif self._pending_felt:
                    self._pending_felt.clear()
            return events

        # 1. Check Shuffle Banner hold
        if obs.shuffle_active:
            if self._shuffle_since is None:
                self._shuffle_since = obs.ts
            elif (obs.ts - self._shuffle_since) >= self.cfg.shuffle_hold_sec and not self._shuffle_emitted:
                self._flush_pending_auto_watch(events)
                has_pending_bet = (self.state not in ("IDLE", "BETTING")) and (self.sync_play_mode == "bet" or self._split_detected)
                self.sync_track_felt = False
                self.resync_round()
                self.state = "SHUFFLING"
                self._shuffle_emitted = True
                events.append({"type": "new_shoe", "reason": "cut", "hasPendingBet": has_pending_bet})
            return events

        self._shuffle_since = None
        if self.state == "SHUFFLING":
            self.state = "IDLE"
        self._shuffle_emitted = False

        # 2. Process Round Status Banner Transitions
        # Playtech banner flow: PLACE_BETS -> LAST_BETS -> NO_MORE_BETS (no cards dealt).
        # Sitting out (watch): "WAIT FOR THE NEXT ROUND" (WAIT) stays on banner during dealing/play;
        # persistent real HUD cards while blank can also wake from IDLE if WAIT was missed or user placed a bet.
        # Active round (bet): banner stays blank ("") after NO_MORE_BETS, so the round starts once NO_MORE_BETS ends.
        banner = obs.banner_state
        has_cards = self._emitted_p > 0 or self._emitted_d > 0
        has_obs_cards = bool(obs.player_cards or obs.dealer_cards or obs.player_hands)
        active_round = self.state in ("DEALING", "PLAYER_TURN", "DEALER_TURN")

        if banner in ("PLACE_BETS", "LAST_BETS", "NO_MORE_BETS"):
            if active_round and has_cards:
                events.extend(self._finalize_cleared_round(obs.ts))
            if banner == "NO_MORE_BETS":
                self._flush_pending_auto_watch(events)
            was_empty_in_betting = self._saw_empty_hud_in_betting or (not has_obs_cards)
            self.state = "BETTING"
            self._saw_clear_since_round_over = True
            self._emit_banner_phase_if_changed(banner, obs.banner_conf, events)
            self._reset_round_accumulators()
            self.sync_track_felt = False
            self._last_bet_banner = banner
            self._saw_empty_hud_in_betting = was_empty_in_betting
            if banner == "NO_MORE_BETS" and was_empty_in_betting and has_obs_cards:
                self._predeal_obs = obs
            return events

        if banner == "WAIT":
            self._wait_absent_frames = 0
            self._wait_missing_with_cards_since = None
            self._blank_card_frames = 0
            self._saw_clear_since_round_over = True
            if self.state in ("IDLE", "BETTING", "ROUND_OVER"):
                predeal = self._predeal_obs if self._saw_empty_hud_in_betting else None
                self._flush_pending_auto_watch(events)
                self._start_new_round()
                if predeal is not None:
                    self._seed_predeal_obs(predeal)
            self._seen_wait_in_round = True
            self._emit_banner_phase_if_changed(banner, obs.banner_conf, events)
        else:
            if not has_obs_cards:
                self._saw_clear_since_round_over = True
                self._blank_card_frames = 0
                if self.state == "BETTING":
                    self._saw_empty_hud_in_betting = True
                    self._predeal_obs = None
            if not banner and active_round:
                self._last_banner_emitted = ""
            if active_round and self._seen_wait_in_round:
                if has_obs_cards:
                    # Cards still visible on HUD (WAIT faded early before final dealer cards): keep debouncers active!
                    self._wait_absent_frames = 0
                    if self._wait_missing_with_cards_since is None:
                        self._wait_missing_with_cards_since = obs.ts
                    elif has_cards and (obs.ts - self._wait_missing_with_cards_since) >= 3.0:
                        events.extend(self._finalize_cleared_round(obs.ts))
                        self.state = "ROUND_OVER"
                        self._saw_clear_since_round_over = False
                        return events
                else:
                    self._wait_missing_with_cards_since = None
                    if obs.hud_refreshed:
                        self._wait_absent_frames += 1
                    if has_cards and self._wait_absent_frames >= max(2, self.cfg.clear_frames):
                        events.extend(self._finalize_cleared_round(obs.ts))
                        self.state = "ROUND_OVER"
                        self._saw_clear_since_round_over = False
                        return events

            wait_required = self.cfg.require_wait_banner and self.sync_play_mode == "watch"
            if wait_required:
                if active_round:
                    if self._seen_wait_in_round and not has_obs_cards:
                        if has_cards:
                            # Speed up end-of-round auto-log if both WAIT banner and cards are gone
                            self._wait_absent_frames = max(self._wait_absent_frames, self.cfg.clear_frames - 1)
                        return events
                else:
                    if has_obs_cards and self._saw_clear_since_round_over:
                        if obs.hud_refreshed:
                            self._blank_card_frames += 1
                            if self._blank_card_frames == 1:
                                self._predeal_obs = obs
                        if self._blank_card_frames >= 2:
                            predeal = self._predeal_obs
                            self._flush_pending_auto_watch(events)
                            self._start_new_round()
                            self._emit_banner_phase_if_changed("DEALING", 1.0, events)
                            if predeal is not None:
                                self._seed_predeal_obs(predeal)
                        else:
                            return events
                    else:
                        if self.state == "BETTING":
                            last_bet = self._last_bet_banner
                            self._reset_round_accumulators()
                            self._last_bet_banner = last_bet
                            self._saw_empty_hud_in_betting = True
                        return events

            elif self.state == "BETTING":
                if (not banner and self._last_bet_banner == "NO_MORE_BETS") or has_obs_cards:
                    predeal = self._predeal_obs if self._saw_empty_hud_in_betting else None
                    self._flush_pending_auto_watch(events)
                    self._start_new_round()
                    self._emit_banner_phase_if_changed("DEALING", 1.0, events)
                    if predeal is not None:
                        self._seed_predeal_obs(predeal)
                else:
                    return events

        # 3. Process felt sightings (passive 'Saw X Card' advisory + dealer_felt_roi fallback + optional armed [B])
        if obs.felt_events:
            for fe in obs.felt_events:
                if fe.zone == "dealer":
                    self._handle_dealer_felt_event(fe)
                elif self.felt_tracking_armed:
                    self._begin_round_if_idle(events)
                    self._handle_felt_event(fe)
                self._maybe_emit_felt_sighting(fe, events)
        if not self.felt_tracking_armed and self._pending_felt:
            self._pending_felt.clear()

        if obs.split_detected:
            self._split_detected = True

        # 4. Update HUD debouncers only on refreshed HUD frames
        if not obs.hud_refreshed:
            return events

        hands_input: dict[int, list[CardDetection]] = (
            {h: list(cs) for h, cs in obs.player_hands.items() if cs}
            if obs.player_hands
            else ({0: list(obs.player_cards)} if obs.player_cards else {})
        )

        # Apply locked split seed cards (Card 0) when score badge occludes Card 0 on a split hand
        if self._split_seed_cards:
            for h_k, seed in self._split_seed_cards.items():
                cur_list = hands_input.get(h_k)
                if cur_list and not _same_bj_value(cur_list[0].rank, seed.rank):
                    hands_input[h_k] = [seed] + cur_list

        # Detect live horizontal split transition (e.g. [Q, Q] -> H0:[Q,...] + H1:[Q,...]) across >=2 clean frames
        split_pending = False
        hands_for_debouncers: dict[int, list[CardDetection]] = hands_input
        if len(hands_input) >= 2 and len(hands_input) > self._emitted_split_hands:
            prev_h0 = list(self.p_debouncers[0].committed_ranks)
            h0_list = hands_input.get(0, [])
            h1_list = hands_input.get(1, [])
            unseparated_overlap = (
                self._emitted_split_hands == 1
                and len(prev_h0) == 2
                and _same_bj_value(prev_h0[0], prev_h0[1])
                and len(h0_list) >= 2
                and _same_bj_value(h0_list[0].rank, prev_h0[0])
                and _same_bj_value(h0_list[1].rank, prev_h0[1])
            )
            if unseparated_overlap:
                # Hold off split while H0 still renders the unsplit pair alongside emerging H1
                self._split_cand_count = 0
                self._split_cand_target = 1
                self._split_pending_seqs.clear()
                split_pending = True
                hands_for_debouncers = {0: h0_list}
            else:
                prior_pending_seqs = dict(self._split_pending_seqs)
                target_cnt = len(hands_input)
                if self._split_cand_target != target_cnt:
                    self._split_cand_target = target_cnt
                    self._split_cand_count = 1
                    prior_pending_seqs = {}
                else:
                    self._split_cand_count += 1

                for h_k, ds_k in hands_input.items():
                    seq_k = tuple(d.rank for d in ds_k)
                    p_seq, p_cnt = prior_pending_seqs.get(h_k, ((), 0))
                    self._split_pending_seqs[h_k] = (seq_k, p_cnt + 1 if seq_k == p_seq else 1)

                split_confirm = 1 if self.cfg.confirm_frames <= 1 else max(2, min(3, self.cfg.confirm_frames))
                if self._split_cand_count < split_confirm:
                    split_pending = True
                    hands_for_debouncers = {}
                else:
                    self._begin_round_if_idle(events)
                    self._emitted_split_hands = target_cnt
                    self._split_detected = True
                    self._split_cand_count = 0
                    self._split_cand_target = target_cnt
                    self._split_pending_seqs.clear()

                    pair_can_reanchor = len(prev_h0) == 2 and _same_bj_value(prev_h0[0], prev_h0[1])
                    # Infer split seed rank even if prev_h0 was not a full pair (e.g., joined on split or badge hid Card 0)
                    inferred_seed_rk: str | None = None
                    if pair_can_reanchor:
                        inferred_seed_rk = prev_h0[0]
                    elif len(prev_h0) == 1 and prev_h0[0] != "BACK":
                        inferred_seed_rk = prev_h0[0]
                    elif h0_list and h1_list:
                        if _same_bj_value(h0_list[0].rank, h1_list[0].rank):
                            inferred_seed_rk = h0_list[0].rank
                        elif len(h1_list) >= 2 and len(h0_list) == 1:
                            inferred_seed_rk = h1_list[0].rank
                        elif len(h0_list) >= 2 and len(h1_list) == 1:
                            inferred_seed_rk = h0_list[0].rank

                    split_cards: list[dict[str, Any]] = []
                    for h_k in sorted(hands_input.keys())[:2]:
                        c0 = hands_input[h_k][0]
                        rk0 = (
                            prev_h0[h_k]
                            if (pair_can_reanchor and h_k < 2)
                            else (inferred_seed_rk if inferred_seed_rk is not None else c0.rank)
                        )
                        val0 = RANK_TO_VAL.get(rk0)
                        if val0 is None:
                            val0 = c0.val
                        suit0 = c0.suit if _same_bj_value(c0.rank, rk0) else None
                        seed_det = CardDetection(
                            rank=rk0,
                            val=val0,
                            conf=max(0.92, c0.conf),
                            x=max(0, int(c0.x) - 12) if isinstance(c0.x, (int, float)) else 0,
                            y=c0.y,
                            w=c0.w,
                            h=c0.h,
                            suit=suit0,
                            hand_index=h_k,
                        )
                        self._split_seed_cards[h_k] = seed_det
                        if not _same_bj_value(c0.rank, rk0):
                            hands_input[h_k] = [seed_det] + hands_input[h_k]
                        split_cards.append({"val": val0, "rankLabel": rk0, "suit": suit0})

                    h0_list = hands_input.get(0, [])
                    h1_list = hands_input.get(1, [])

                    events.append(
                        {
                            "type": "split",
                            "handCount": target_cnt,
                            "roundId": self.round_id,
                            "splitCards": split_cards,
                        }
                    )

                    if pair_can_reanchor:
                        seq0_cur = tuple(d.rank for d in h0_list)
                        p0_seq, p0_cnt = prior_pending_seqs.get(0, ((), 0))
                        p0_seq_norm = (
                            p0_seq
                            if (p0_seq and _same_bj_value(p0_seq[0], prev_h0[0]))
                            else ((prev_h0[0],) + p0_seq if p0_seq else ())
                        )
                        if p0_seq_norm == seq0_cur and p0_cnt > 0 and len(seq0_cur) > 1:
                            self.p_debouncers[0].reanchor([prev_h0[0]], cand_ranks=seq0_cur, cand_count=p0_cnt)
                        else:
                            self.p_debouncers[0].reanchor([prev_h0[0]])

                        # Always anchor H1 slot 0 to prev_h0[1] so temporary occlusion/misread cannot duplicate card 0
                        d1 = SequenceDebouncer(self.cfg.confirm_frames, self.cfg.clear_frames)
                        if h1_list:
                            seq1_cur = tuple(d.rank for d in h1_list)
                            p1_seq, p1_cnt = prior_pending_seqs.get(1, ((), 0))
                            p1_seq_norm = (
                                p1_seq
                                if (p1_seq and _same_bj_value(p1_seq[0], prev_h0[1]))
                                else ((prev_h0[1],) + p1_seq if p1_seq else ())
                            )
                            if (
                                _same_bj_value(h1_list[0].rank, prev_h0[1])
                                and p1_seq_norm == seq1_cur
                                and p1_cnt > 0
                                and len(seq1_cur) > 1
                            ):
                                d1.reanchor([prev_h0[1]], cand_ranks=seq1_cur, cand_count=p1_cnt)
                            else:
                                d1.reanchor([prev_h0[1]])
                        else:
                            d1.reanchor([prev_h0[1]])
                        self.p_debouncers[1] = d1
        else:
            self._split_cand_count = 0
            self._split_cand_target = max(1, len(hands_input))
            self._split_pending_seqs.clear()

        p_cleared = True
        if split_pending and not hands_for_debouncers:
            p_cleared = False
        else:
            iter_hands = (
                sorted(hands_for_debouncers.keys())
                if split_pending
                else sorted(set(self.p_debouncers.keys()) | set(hands_for_debouncers.keys()))
            )
            for h_idx in iter_hands:
                if h_idx not in self.p_debouncers:
                    self.p_debouncers[h_idx] = SequenceDebouncer(self.cfg.confirm_frames, self.cfg.clear_frames)
                new_cards, h_cleared = self.p_debouncers[h_idx].update(hands_for_debouncers.get(h_idx, []))
                if not h_cleared:
                    p_cleared = False
                for slot_idx, det in new_cards:
                    if det.val is None:
                        continue
                    self._begin_round_if_idle(events)
                    self._emitted_p += 1
                    self._record_hud_p_for_dedup(obs.ts, det.val)
                    if det.conf < self.cfg.min_auto_confidence:
                        self._uncertainty_flags.append(f"P{h_idx}:{slot_idx + 1}:{det.rank}@{det.conf:.2f}")
                    evt: dict[str, Any] = {
                        "type": "card",
                        "target": "P",
                        "handIndex": h_idx,
                        "val": det.val,
                        "slot": slot_idx,
                        "rankLabel": det.rank,
                        "conf": round(det.conf, 3),
                        "source": "hud",
                        "roundId": self.round_id,
                    }
                    if det.suit:
                        evt["suit"] = det.suit
                    events.append(evt)

            if not self.p_debouncers:
                p_cleared = True

        new_d, d_cleared = self.d_debouncer.update(obs.dealer_cards)
        dealer_flipped_or_drew = self._emit_dealer_cards(new_d, events, ts=obs.ts)

        # Transition phase & flush unmatched post-Stand / glitched felt cards once dealer acts
        if self.state == "DEALING" and len(self.d_debouncer.committed_ranks) >= 1 and self._emitted_p >= 1:
            self.state = "PLAYER_TURN"

        if dealer_flipped_or_drew:
            self.state = "DEALER_TURN"
            if self.felt_tracking_armed:
                events.extend(self._flush_unmatched_felt(obs.ts, force_all=True))
            elif self._pending_felt:
                self._pending_felt.clear()

        # 5. Fallback end-of-round clear check (cards wiped from HUD)
        if self.state not in ("IDLE", "BETTING", "ROUND_OVER") and p_cleared and d_cleared:
            events.extend(self._finalize_cleared_round(obs.ts))
            return events

        # 6. Auto-Resync fallback: reconcile drifted debouncer / UI cards against stably held crisp HUD snapshot
        if not split_pending and not any(e.get("type") in ("card", "split") for e in events):
            self._maybe_reconcile_hud(obs, hands_input, events)

        return events

    def _maybe_reconcile_hud(
        self,
        obs: FrameObservation,
        hands_input: dict[int, list[CardDetection]],
        events: list[dict[str, Any]],
    ) -> None:
        if (
            self.state not in ("DEALING", "PLAYER_TURN", "DEALER_TURN")
            or obs.shuffle_active
            or obs.banner_state in ("PLACE_BETS", "LAST_BETS", "NO_MORE_BETS")
            or not hands_input
        ):
            self._reconcile_sig = None
            self._reconcile_frames = 0
            return

        min_rec_conf = max(0.80, self.cfg.min_hud_confidence - 0.04)
        for cs in hands_input.values():
            if not cs or any(d.val is None or d.conf < min_rec_conf for d in cs):
                self._reconcile_sig = None
                self._reconcile_frames = 0
                return
        d_real = [d for d in obs.dealer_cards if d.val is not None]
        if any(d.conf < min_rec_conf for d in d_real):
            self._reconcile_sig = None
            self._reconcile_frames = 0
            return

        sorted_keys = sorted(hands_input.keys())
        p_sig = tuple((h, tuple((d.rank, d.val) for d in hands_input[h])) for h in sorted_keys)
        d_sig = tuple((d.rank, d.val) for d in d_real)
        sig = (p_sig, d_sig)

        if sig == self._reconcile_sig:
            self._reconcile_frames += 1
        else:
            self._reconcile_sig = sig
            self._reconcile_frames = 1
            self._reconcile_since = obs.ts

        req_frames = max(6, self.cfg.confirm_frames * 2)
        if (
            self._reconcile_frames < req_frames
            or (obs.ts - self._reconcile_since) < 0.55
            or (obs.ts - self._last_reconcile_ts) < 1.5
        ):
            return

        deb_hands = {
            h: [r for r in db.committed_ranks if r != "BACK"]
            for h, db in sorted(self.p_debouncers.items())
            if db.committed_ranks
        }
        hud_hands_ranks = {h: [d.rank for d in hands_input[h]] for h in sorted_keys}
        deb_dealer = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
        hud_dealer_real_ranks = [d.rank for d in d_real]
        deb_mismatch = (deb_hands != hud_hands_ranks) or (deb_dealer != hud_dealer_real_ranks)

        hud_hands_vals = [[int(d.val) for d in hands_input[h] if d.val is not None] for h in sorted_keys]
        hud_dealer_vals = [int(d.val) for d in d_real if d.val is not None]

        p_ui_mismatch = False
        d_ui_mismatch = False
        if self.sync_round_id == self.round_id and self.sync_draft_hands is not None:
            if len(self.sync_draft_hands) != len(hud_hands_vals):
                total_ui = sum(len(x) for x in self.sync_draft_hands)
                total_hud = sum(len(x) for x in hud_hands_vals)
                if not (any(self.sync_draft_hands_manual) and total_hud < total_ui):
                    p_ui_mismatch = True
            else:
                for idx, (ui_h, hud_h) in enumerate(zip(self.sync_draft_hands, hud_hands_vals)):
                    if ui_h != hud_h:
                        is_man = idx < len(self.sync_draft_hands_manual) and self.sync_draft_hands_manual[idx]
                        if not (is_man and len(hud_h) < len(ui_h)):
                            p_ui_mismatch = True
                            break
            if self.sync_draft_dealer is not None and self.sync_draft_dealer != hud_dealer_vals:
                if not (self.sync_draft_dealer_manual and len(hud_dealer_vals) < len(self.sync_draft_dealer)):
                    d_ui_mismatch = True

        if not (deb_mismatch or p_ui_mismatch or d_ui_mismatch):
            return

        new_debouncers: dict[int, SequenceDebouncer] = {}
        for pos_idx, h_key in enumerate(sorted_keys):
            db = SequenceDebouncer(self.cfg.confirm_frames, self.cfg.clear_frames)
            db.reanchor(hud_hands_ranks[h_key])
            new_debouncers[pos_idx] = db
        self.p_debouncers = new_debouncers

        self.d_debouncer.reanchor([d.rank for d in obs.dealer_cards])
        self._emitted_split_hands = max(1, len(sorted_keys))
        self._split_detected = len(sorted_keys) >= 2
        self._emitted_p = sum(len(v) for v in hud_hands_vals)
        self._emitted_d = len(hud_dealer_vals)
        self.sync_is_split = self._split_detected
        self.sync_draft_hands = [list(v) for v in hud_hands_vals]
        self.sync_draft_dealer = list(hud_dealer_vals)
        self._uncertainty_flags = [f for f in self._uncertainty_flags if f == "MISSED_COMMUNITY_CARD"]
        self._last_reconcile_ts = obs.ts

        events.append(
            {
                "type": "reconcile",
                "roundId": self.round_id,
                "handCount": self._emitted_split_hands,
                "hands": [
                    [
                        {
                            "val": d.val,
                            "rankLabel": d.rank,
                            "suit": d.suit,
                            "conf": round(d.conf, 3),
                        }
                        for d in hands_input[h]
                    ]
                    for h in sorted_keys
                ],
                "dealer": [
                    {
                        "val": d.val,
                        "rankLabel": d.rank,
                        "suit": d.suit,
                        "conf": round(d.conf, 3),
                    }
                    for d in d_real
                ],
            }
        )

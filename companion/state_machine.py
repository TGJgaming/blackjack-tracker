"""Pure round & shoe state machine with HUD-vs-Felt deduplication and BET/Split/Glitch guards."""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import time
from typing import Any

from config import RANK_TO_VAL, CompanionConfig, KeepaliveConfig, ShoeDetectConfig
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


_DEDUCE_RANK_CANDIDATES: list[dict[str, Any]] = [
    {"rank": "A", "val": 1},
    {"rank": "2", "val": 2},
    {"rank": "3", "val": 3},
    {"rank": "4", "val": 4},
    {"rank": "5", "val": 5},
    {"rank": "6", "val": 6},
    {"rank": "7", "val": 7},
    {"rank": "8", "val": 8},
    {"rank": "9", "val": 9},
    {"rank": "10", "val": 0},
]


def deduce_missing_dealer_card(d_committed_ranks: list[str], badge_total: int | None) -> dict[str, Any] | None:
    """Infer a single missing dealer card rank when score badge total uniquely determines it."""
    if not d_committed_ranks or badge_total is None or not isinstance(badge_total, int) or badge_total < 4 or badge_total > 26:
        return None
    cur_total, _, _ = _eval_bj_ranks(d_committed_ranks)
    if cur_total == badge_total:
        return None
    matches: list[dict[str, Any]] = []
    for cand in _DEDUCE_RANK_CANDIDATES:
        tot, _, _ = _eval_bj_ranks([*d_committed_ranks, cand["rank"]])
        if tot == badge_total:
            matches.append(dict(cand))
    return matches[0] if len(matches) == 1 else None


def deduce_missing_dealer_pair(d_committed_ranks: list[str], badge_total: int | None) -> list[dict[str, Any]] | None:
    """Infer two missing dealer cards from score badge total when a 2-card draw/flip was missed."""
    if not d_committed_ranks or badge_total is None or not isinstance(badge_total, int) or badge_total < 17 or badge_total > 26:
        return None
    cur_total, _, _ = _eval_bj_ranks(d_committed_ranks)
    if cur_total >= 17 or cur_total == badge_total:
        return None
    if deduce_missing_dealer_card(d_committed_ranks, badge_total) is not None:
        return None
    best_pair: list[dict[str, Any]] | None = None
    best_score = -1
    for c1 in _DEDUCE_RANK_CANDIDATES:
        mid_tot, _, _ = _eval_bj_ranks([*d_committed_ranks, c1["rank"]])
        if mid_tot >= 17:
            continue
        for c2 in _DEDUCE_RANK_CANDIDATES:
            end_tot, _, _ = _eval_bj_ranks([*d_committed_ranks, c1["rank"], c2["rank"]])
            if end_tot != badge_total:
                continue
            score = (4 if c2["rank"] == "10" else 1) * (4 if c1["rank"] == "10" else 1)
            if score > best_score:
                best_score = score
                best_pair = [dict(c1), dict(c2)]
    return best_pair


_ALL_GUESS_RANKS: list[dict[str, Any]] = [
    {"rank": "10", "val": 0},
    {"rank": "A", "val": 1},
    {"rank": "2", "val": 2},
    {"rank": "3", "val": 3},
    {"rank": "4", "val": 4},
    {"rank": "5", "val": 5},
    {"rank": "6", "val": 6},
    {"rank": "7", "val": 7},
    {"rank": "8", "val": 8},
    {"rank": "9", "val": 9},
]

_RANK_TO_VAL: dict[str, int] = {
    "10": 0, "J": 0, "Q": 0, "K": 0, "0": 0,
    "A": 1, "1": 1,
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
}


def guess_missing_dealer_card(
    d_committed_ranks: list[str],
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Multi-signal educated guess across all 10 blackjack ranks (A, 2..9, 10) for incomplete dealer hands."""
    if not d_committed_ranks:
        return None
    d_tot, d_soft, _ = _eval_bj_ranks(d_committed_ranks)
    if not context:
        if d_tot == 6 and not d_soft:
            return {"rank": "A", "val": 1}
        return {"rank": "10", "val": 0}

    must_reach_17 = context.get("must_reach_17", True)
    slot_idx = int(context.get("slot_idx", len(d_committed_ranks)))

    def _is_valid_step(canon_rk: str) -> bool:
        if not must_reach_17:
            return True
        new_tot, _, _ = _eval_bj_ranks([*d_committed_ranks, canon_rk])
        if new_tot >= 17:
            return True
        if d_tot < 7 or (d_soft and d_tot < 17):
            return True
        return False

    # 1. Sub-threshold template runner-up peaks + visual width prior (rem_right)
    sub_cands_raw = context.get("sub_candidates")
    if isinstance(sub_cands_raw, dict):
        sub_cands = sub_cands_raw.get(slot_idx) or []
    elif isinstance(sub_cands_raw, list):
        sub_cands = sub_cands_raw
    else:
        sub_cands = []

    if sub_cands:
        best_sub: dict[str, Any] | None = None
        best_score = -1.0
        for sc in sub_cands:
            if not isinstance(sc, dict):
                continue
            rk_raw = sc.get("rank")
            if not rk_raw or rk_raw == "BACK":
                continue
            val = _RANK_TO_VAL.get(rk_raw) if rk_raw in _RANK_TO_VAL else sc.get("val")
            if val is None:
                continue
            canon_rank = "10" if val == 0 else ("A" if val == 1 else str(val))
            if not _is_valid_step(canon_rank):
                continue
            score = float(sc.get("conf") or 0.0)
            rem_r = sc.get("rem_right")
            if rem_r is None:
                rem_r = sc.get("remRight")
            if isinstance(rem_r, (int, float)) and 0 < rem_r < 16:
                if canon_rank == "10":
                    score -= 0.14
                else:
                    score += 0.06
            if sc.get("has_pip_body") or sc.get("hasPipBody"):
                score += 0.04
            if sc.get("suit"):
                score += 0.03
            if score >= 0.48 and score > best_score:
                best_score = score
                best_sub = {"rank": canon_rank, "val": int(val)}
        if best_sub is not None:
            return best_sub

    # 2. Partial / soft dealer score badge hints (tensDigit / onesDigit)
    badge_hint = context.get("badge_hint") or context.get("badgeHint")
    if isinstance(badge_hint, dict):
        hint_tot = badge_hint.get("total")
        hint_conf = float(badge_hint.get("conf") or 0.0)
        if isinstance(hint_tot, int) and 17 <= hint_tot <= 26 and hint_conf >= 0.56:
            exact = deduce_missing_dealer_card(d_committed_ranks, hint_tot)
            if exact is not None:
                return exact
        tens_dig = badge_hint.get("tensDigit")
        tens_conf = float(badge_hint.get("tensConf") or 0.0)
        ones_dig = badge_hint.get("onesDigit")
        ones_conf = float(badge_hint.get("onesConf") or 0.0)
        if tens_dig in (1, 2) and tens_conf >= 0.58:
            lo = 17 if tens_dig == 1 else 20
            hi = 19 if tens_dig == 1 else 26
            tens_matches = [
                c for c in _ALL_GUESS_RANKS
                if lo <= _eval_bj_ranks([*d_committed_ranks, c["rank"]])[0] <= hi
            ]
            if tens_matches:
                if isinstance(ones_dig, int) and ones_conf >= 0.54:
                    for c in tens_matches:
                        if (_eval_bj_ranks([*d_committed_ranks, c["rank"]])[0] % 10) == ones_dig:
                            return c
                if tens_dig == 2:
                    for c in tens_matches:
                        if c["rank"] == "10":
                            return c
                return tens_matches[len(tens_matches) // 2]

    # 3. Physical felt sightings recorded during the round
    felt_sightings = context.get("felt_sightings") or context.get("feltSightings")
    if isinstance(felt_sightings, list):
        for fs in felt_sightings:
            if not isinstance(fs, dict):
                continue
            rk_raw = fs.get("rank") or fs.get("rankLabel")
            val = _RANK_TO_VAL.get(rk_raw) if rk_raw in _RANK_TO_VAL else fs.get("val")
            if val is not None:
                canon_rank = "10" if val == 0 else ("A" if val == 1 else str(val))
                if _is_valid_step(canon_rank):
                    return {"rank": canon_rank, "val": int(val)}

    # 4. Live shoe depletion & stopping-rule Bayesian stratified selection across all 10 ranks
    default_rem = [128, 32, 32, 32, 32, 32, 32, 32, 32, 32]
    raw_rem = context.get("shoe_rem") if context.get("shoe_rem") is not None else context.get("shoeRem")
    if isinstance(raw_rem, list) and len(raw_rem) == 10:
        shoe_rem = [max(0, int(n)) if isinstance(n, (int, float)) else default_rem[i] for i, n in enumerate(raw_rem)]
    else:
        shoe_rem = default_rem

    valid_pool = [c for c in _ALL_GUESS_RANKS if _is_valid_step(c["rank"])]
    if not valid_pool:
        valid_pool = list(_ALL_GUESS_RANKS)

    tail_rem_right = context.get("tail_rem_right")
    if isinstance(tail_rem_right, (int, float)) and 0 < tail_rem_right < 15:
        non_ten = [c for c in valid_pool if c["rank"] != "10"]
        if non_ten:
            valid_pool = non_ten

    non_depleted = [c for c in valid_pool if shoe_rem[c["val"]] > 0]
    if non_depleted:
        valid_pool = non_depleted

    weights = [shoe_rem[c["val"]] if shoe_rem[c["val"]] > 0 else 1 for c in valid_pool]
    total_weight = sum(weights)

    round_seed = context.get("round_seed")
    if isinstance(round_seed, (int, float)) and total_weight > 0:
        u = (float(round_seed) * 0.618033988749895) % 1.0
        target = u * total_weight
        acc = 0.0
        for idx, c in enumerate(valid_pool):
            acc += weights[idx]
            if target <= acc:
                return c

    if d_tot == 6 and not d_soft:
        for c in valid_pool:
            if c["rank"] == "A":
                return c
    best_cand = valid_pool[0]
    best_w = -1
    for idx, c in enumerate(valid_pool):
        if weights[idx] > best_w:
            best_w = weights[idx]
            best_cand = c
    return best_cand


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
                prev_pend = self._pending_tail.get(idx)
                best_det = det if (prev_pend is None or det.conf >= prev_pend[0].conf) else prev_pend[0]
                self._pending_tail[idx] = (best_det, 0)

        if not partial_wipe:
            for idx in list(self._slot_cands.keys()):
                if idx not in seen_slots:
                    self._slot_cands.pop(idx, None)
            if self.fast_tail_confirm and self.committed_ranks:
                for idx in list(self._pending_tail.keys()):
                    if idx not in seen_slots:
                        det_p, missed = self._pending_tail[idx]
                        if missed + 1 >= 30:
                            self._pending_tail.pop(idx, None)
                        else:
                            self._pending_tail[idx] = (det_p, missed + 1)
        elif not committed_wipe:
            # Trailing uncommitted card faded early before committed prefix: keep in _pending_tail for wipe rescue
            for idx in list(self._pending_tail.keys()):
                if idx not in seen_slots:
                    det_p, missed = self._pending_tail[idx]
                    if missed + 1 >= 30:
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
                    det.conf >= min(0.82, self.fast_tail_min_conf)
                    or (det.suit is not None and det.conf >= 0.78)
                    or (det.has_pip_body and det.conf >= 0.78)
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
                det.conf >= (min(0.82, self.fast_tail_min_conf) if self.fast_tail_confirm else self.fast_tail_min_conf)
                or (det.suit is not None and det.conf >= 0.80)
                or (self.fast_tail_confirm and det.has_pip_body and det.conf >= 0.78)
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
            idx: det for idx, (det, missed) in self._pending_tail.items() if missed <= 30
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
        self._pending_reload_recovery: bool = False
        self._saw_strips_absent_in_warn: bool = False
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
        self._pending_reload_recovery = False
        self._saw_strips_absent_in_warn = False
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
        self._pending_reload_recovery = False
        self._saw_strips_absent_in_warn = False

    def mark_refresh_dispatched(self, now: float, ok: bool, used_method: str, epoch_ts: float | None = None) -> dict[str, Any]:
        if not ok:
            self.state = "WARN"
            self._scheduled_fire_ts = None
            self.grace_until_ts = None
            self._appcommand_sent_ts = None
            self._refresh_sent_ts = None
            self._warn_retry_after_ts = now + 4.0
            self._pending_reload_recovery = True
            self._saw_strips_absent_in_warn = False
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
                self._warn_retry_after_ts = now + 90.0
                self._pending_reload_recovery = True
                self._saw_strips_absent_in_warn = False
                action.status_changed = True
                action.emit_alert = {
                    "type": "keepalive_status",
                    "event": "timeout_warn",
                    "reloadSec": round(elapsed_reload, 1),
                    "keepalive": self.to_status(now),
                }
                return action

            return action

        if self.state == "WARN" and window_found:
            if not obs_has_cards:
                self._saw_strips_absent_in_warn = True
            elif obs_has_cards and (self._pending_reload_recovery or self._saw_strips_absent_in_warn):
                started = self.refresh_started_ts if self.refresh_started_ts is not None else now
                reload_dur = max(0.0, now - started)
                self.refresh_count += 1
                self.last_refresh_epoch = time.time()
                self.reset_timer(reason="strips_visible_fallback", now=now)
                self.state = "WATCH_COUNTDOWN" if play_mode == "watch" else "PAUSED_BET"
                action.status_changed = True
                action.emit_alert = {
                    "type": "keepalive_status",
                    "event": "recovered",
                    "reason": "strips_visible_fallback",
                    "reloadSec": round(reload_dur, 1),
                    "keepalive": self.to_status(now),
                }
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

        # Track safe early betting window: strictly explicit PLACE_BETS phase only
        in_safe_bet_window = (
            window_found
            and banner_state == "PLACE_BETS"
            and sm_state in ("BETTING", "IDLE", "ROUND_OVER")
            and not obs_has_cards
        )
        hard_cancel_bet_window = (
            (not window_found)
            or obs_has_cards
            or banner_state in ("LAST_BETS", "NO_MORE_BETS", "WAIT", "SHUFFLING")
            or sm_state not in ("BETTING", "IDLE", "ROUND_OVER")
        )
        if in_safe_bet_window:
            self._place_bets_miss_streak = 0
            if self._place_bets_since is None:
                self._place_bets_since = now
                self._window_kind = "PLACE_BETS"
                self._alert_sent_for_window = False
            else:
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

        # Timer elapsed threshold reached: arm strictly for PLACE_BETS phase only
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
            if self.cfg.mode == "auto":
                if self._scheduled_fire_ts is None:
                    min_d = self.cfg.place_bets_min_delay_sec
                    max_d = max(min_d, self.cfg.place_bets_max_delay_sec)
                    if win_age <= min_d:
                        jitter = self._rng.uniform(min_d, max_d)
                        self._scheduled_fire_ts = max(now + 0.05, self._place_bets_since + jitter)
                    else:
                        # Timer finished mid-window within safe runway: trigger promptly
                        self._scheduled_fire_ts = now + self._rng.uniform(0.05, 0.25)
                if self._scheduled_fire_ts is not None and now >= self._scheduled_fire_ts:
                    preferred = self.choose_method()
                    if (
                        preferred != "ext_reload"
                        and user_idle_ms < 1200
                        and self.watch_elapsed_sec < self.cfg.hard_warn_sec
                    ):
                        return action
                    self.state = "REFRESHING"
                    self._scheduled_fire_ts = None
                    self._ctrl_r_tried_for_reload = False
                    action.flush_auto_watch = True
                    action.trigger_refresh = True
                    action.preferred_method = preferred
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


class ShoeSwapTracker:
    """Dual-ROI physical table auto-new-shoe tracker (dealing shoe + movable discard tray).

    Triggers:
      1. Primary (`shoe_swap`): shoe_roi transitions PRESENT (>=2.0s) -> ABSENT (>=3.5s,
         tolerating <=0.7s OCCLUDED flicker) -> RETURNED (>=1.5s) [or ABSENT >=12s + PLACE_BETS].
      2. Fast Corroborated (`shoe_and_discard`): shoe_roi ABSENT >=1.8s AND discard_roi
         drops from confirmed white stack peak to empty.
      3. Fallback (`discard_cleared`): discard_roi drops from confirmed peak (>=0.022) to
         empty (<=0.007) for >=4.5s while dark tray housing remains visible (>=0.10) so
         sliding the tray completely off-zone never false-triggers.
    """

    def __init__(self, cfg: ShoeDetectConfig) -> None:
        self.cfg = cfg
        self.shoe_state: str = ""
        self.shoe_dark_frac: float = 0.0
        self.shoe_teal_frac: float = 0.0
        self.discard_white_frac: float = 0.0
        self.discard_dark_frac: float = 0.0
        self.discard_teal_frac: float = 0.0

        self._shoe_present_since: float | None = None
        self._shoe_confirmed_present: bool = False
        self._shoe_absent_since: float | None = None
        self._shoe_occluded_since: float | None = None
        self._shoe_return_since: float | None = None
        self._shoe_swap_armed: bool = False
        self._shoe_swap_corroborated: bool = False

        self._discard_window: list[float] = []
        self._discard_peak_white: float = 0.0
        self._discard_peak_above_since: float | None = None
        self._discard_peak_confirmed: bool = False
        self._discard_empty_since: float | None = None
        self._last_active_cards_ts: float = -1e9
        self._shoe_disturb_since: float | None = None
        self._last_shoe_disturbance_ts: float = -1e9
        self.discard_veto: str = ""

        self._cooldown_until: float = -1e9
        self.last_trigger_reason: str = ""

    def reset_for_new_shoe(self, now: float) -> None:
        self._shoe_present_since = None
        self._shoe_confirmed_present = False
        self._shoe_absent_since = None
        self._shoe_occluded_since = None
        self._shoe_return_since = None
        self._shoe_swap_armed = False
        self._shoe_swap_corroborated = False
        self._discard_window.clear()
        self._discard_peak_white = 0.0
        self._discard_peak_above_since = None
        self._discard_peak_confirmed = False
        self._discard_empty_since = None
        self._shoe_disturb_since = None
        self._last_shoe_disturbance_ts = -1e9
        self.discard_veto = ""
        self._cooldown_until = now + self.cfg.cooldown_sec

    def on_undo_new_shoe(self, now: float) -> None:
        """Called when the user clicks Undo Auto-Reset: suppress re-triggering for cooldown_sec."""
        self._shoe_absent_since = None
        self._shoe_occluded_since = None
        self._shoe_return_since = None
        self._shoe_swap_armed = False
        self._shoe_swap_corroborated = False
        self._shoe_confirmed_present = True
        self._discard_empty_since = None
        self._shoe_disturb_since = None
        self._last_shoe_disturbance_ts = -1e9
        self.discard_veto = "cooldown"
        self._cooldown_until = now + self.cfg.cooldown_sec

    def status_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "shoeState": self.shoe_state or "UNKNOWN",
            "shoeDark": round(self.shoe_dark_frac, 3),
            "shoeTeal": round(self.shoe_teal_frac, 3),
            "swapArmed": self._shoe_swap_armed,
            "discardWhite": round(self.discard_white_frac, 4),
            "discardPeak": round(self._discard_peak_white, 4),
            "discardPeakConfirmed": self._discard_peak_confirmed,
            "discardDark": round(self.discard_dark_frac, 3),
            "discardTeal": round(self.discard_teal_frac, 3),
            "discardVeto": self.discard_veto,
            "lastReason": self.last_trigger_reason,
        }

    def step(
        self,
        obs: FrameObservation,
        *,
        cards_seen: int,
        has_active_cards: bool,
        in_betting_overlay: bool,
        suppressed: bool,
    ) -> str | None:
        """Advance physical shoe & discard-tray state. Returns trigger reason or None."""
        now = obs.ts
        if has_active_cards:
            self._last_active_cards_ts = now
        if obs.shoe_state:
            self.shoe_state = obs.shoe_state
            self.shoe_dark_frac = obs.shoe_dark_frac
            self.shoe_teal_frac = obs.shoe_teal_frac
        if obs.discard_teal_frac > 0.0 or obs.discard_dark_frac > 0.0 or obs.discard_white_frac > 0.0:
            self.discard_white_frac = obs.discard_white_frac
            self.discard_dark_frac = obs.discard_dark_frac
            self.discard_teal_frac = obs.discard_teal_frac

        if not self.cfg.enabled or suppressed or now < self._cooldown_until:
            return None

        # --- 1. Update movable discard-tray stack peak (15-frame median + sustained peak hold) ---
        discard_just_emptied = False
        if (
            self.cfg.enable_discard_fallback
            and not in_betting_overlay
            and self.discard_teal_frac >= 0.12
        ):
            self._discard_window.append(self.discard_white_frac)
            if len(self._discard_window) > 15:
                self._discard_window.pop(0)
            med_white = sorted(self._discard_window)[len(self._discard_window) // 2]

            if med_white >= self.cfg.discard_peak_white_min:
                if self._discard_peak_above_since is None:
                    self._discard_peak_above_since = now
                if (now - self._discard_peak_above_since) >= self.cfg.discard_peak_hold_sec:
                    self._discard_peak_confirmed = True
                    if med_white > self._discard_peak_white:
                        self._discard_peak_white = med_white
            else:
                self._discard_peak_above_since = None

            housing_visible = self.discard_dark_frac >= self.cfg.discard_housing_dark_min
            drop_ok = self._discard_peak_white > 0.0 and (med_white <= self._discard_peak_white * self.cfg.discard_drop_ratio_max)
            if (
                self._discard_peak_confirmed
                and self._discard_peak_white >= self.cfg.discard_peak_white_min
                and med_white <= self.cfg.discard_empty_white_max
                and drop_ok
                and housing_visible
                and not has_active_cards
            ):
                if self._discard_empty_since is None:
                    self._discard_empty_since = now
                discard_just_emptied = True
            else:
                self._discard_empty_since = None

        # --- 2. Primary: shoe_roi PRESENT -> ABSENT -> RETURNED ---
        if self.cfg.enable_shoe_roi and self.shoe_state:
            if self.shoe_state in ("ABSENT", "OCCLUDED"):
                if self._shoe_disturb_since is None:
                    self._shoe_disturb_since = now
                elif (now - self._shoe_disturb_since) >= 1.2:
                    self._last_shoe_disturbance_ts = now
            else:
                self._shoe_disturb_since = None

            if self.shoe_state == "PRESENT":
                self._shoe_absent_since = None
                self._shoe_occluded_since = None
                if self._shoe_present_since is None:
                    self._shoe_present_since = now
                elif (now - self._shoe_present_since) >= self.cfg.shoe_present_hold_sec:
                    self._shoe_confirmed_present = True

                if self._shoe_swap_armed:
                    if self._shoe_return_since is None:
                        self._shoe_return_since = now
                    elif (now - self._shoe_return_since) >= self.cfg.shoe_return_hold_sec and not has_active_cards:
                        reason = "shoe_and_discard" if self._shoe_swap_corroborated else "shoe_swap"
                        self.last_trigger_reason = reason
                        self.reset_for_new_shoe(now)
                        return reason
                else:
                    self._shoe_return_since = None

            elif self.shoe_state == "ABSENT":
                self._shoe_present_since = None
                self._shoe_return_since = None
                self._shoe_occluded_since = None
                if self._shoe_confirmed_present and cards_seen >= self.cfg.shoe_min_cards:
                    if self._shoe_absent_since is None:
                        self._shoe_absent_since = now
                    absent_dur = now - self._shoe_absent_since
                    if absent_dur >= self.cfg.shoe_absent_hold_sec:
                        self._shoe_swap_armed = True
                    elif discard_just_emptied and absent_dur >= self.cfg.shoe_corroborated_absent_sec:
                        self._shoe_swap_armed = True
                        self._shoe_swap_corroborated = True
                    # Safety valve: shoe absent >= 12s and PLACE_BETS appeared (e.g. new shoe shifted off ROI)
                    if (
                        self._shoe_swap_armed
                        and absent_dur >= self.cfg.shoe_absent_safety_sec
                        and obs.banner_state == "PLACE_BETS"
                        and not has_active_cards
                    ):
                        reason = "shoe_and_discard" if self._shoe_swap_corroborated else "shoe_swap"
                        self.last_trigger_reason = reason
                        self.reset_for_new_shoe(now)
                        return reason

            elif self.shoe_state == "OCCLUDED":
                # Tolerate brief dealer arm flicker while ABSENT; otherwise ignore occlusions
                self._shoe_present_since = None
                self._shoe_return_since = None
                if self._shoe_absent_since is not None and not self._shoe_swap_armed:
                    if self._shoe_occluded_since is None:
                        self._shoe_occluded_since = now
                    elif (now - self._shoe_occluded_since) > self.cfg.shoe_max_flicker_sec:
                        self._shoe_absent_since = None
                        self._shoe_occluded_since = None

        # --- 3. Standalone Fallback: discard_roi peak -> empty (with cross-sensor shoe_roi veto & idle gap) ---
        self.discard_veto = ""
        if (
            self.cfg.enable_discard_fallback
            and self._discard_empty_since is not None
            and cards_seen >= self.cfg.discard_min_cards
            and (now - self._discard_empty_since) >= self.cfg.discard_empty_hold_sec
            and not has_active_cards
        ):
            idle_ok = (self._last_active_cards_ts < -1e8) or ((now - self._last_active_cards_ts) >= self.cfg.discard_idle_gap_sec)
            shoe_vetoed = bool(
                self.cfg.enable_shoe_roi
                and self.cfg.discard_require_shoe_disturbance
                and self._shoe_confirmed_present
                and (now - self._last_shoe_disturbance_ts) > 30.0
            )
            if not idle_ok:
                self.discard_veto = "idle_gap"
            elif shoe_vetoed:
                self.discard_veto = "shoe_present"
            else:
                self.last_trigger_reason = "discard_cleared"
                self.reset_for_new_shoe(now)
                return "discard_cleared"

        return None


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
        self.sync_shoe_rem: list[int] | None = None
        self.sync_dealer_guess_removed: bool = False

        self.state: str = "IDLE"  # IDLE | BETTING | DEALING | PLAYER_TURN | DEALER_TURN | ROUND_OVER | SHUFFLING
        self._round_seq: int = 0
        self.round_id: str = "r_0"

        self._review_round_id: str = ""
        self._review_player_ranks: list[list[str]] = []
        self._review_dealer_ranks: list[str] = []
        self._review_sub_dealer_tail: dict[int, list[dict[str, Any]]] = {}
        self._review_badge_hint: dict[str, Any] | None = None
        self._review_felt_sightings: list[dict[str, Any]] = []
        self._review_emitted_p: int = 0
        self._review_emitted_d: int = 0
        self._review_emitted_b: int = 0
        self._review_emitted_split_hands: int = 1
        self._place_bets_entered_ts: float | None = None
        self._place_bets_recheck_0_done: bool = False
        self._place_bets_recheck_3_done: bool = False

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
        self._best_dealer_tail: dict[int, CardDetection] = {}
        self._best_sub_dealer_tail: dict[int, list[dict[str, Any]]] = {}
        self._last_dealer_badge_total: int | None = None
        self._last_dealer_badge_conf: float = 0.0
        self._last_dealer_badge_hint: dict[str, Any] | None = None
        self._dealer_badge_history: list[dict[str, Any]] = []

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
        self.shoe_tracker = ShoeSwapTracker(cfg.shoe_detect)

    def on_undo_new_shoe(self, now: float | None = None) -> None:
        self.shoe_tracker.on_undo_new_shoe(time.monotonic() if now is None else now)

    def reset_for_new_shoe(self, now: float | None = None) -> None:
        self.shoe_tracker.reset_for_new_shoe(time.monotonic() if now is None else now)

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
        self._best_dealer_tail.clear()
        self._best_sub_dealer_tail.clear()
        self._last_dealer_badge_total = None
        self._last_dealer_badge_conf = 0.0
        self._last_dealer_badge_hint = None
        self._dealer_badge_history.clear()
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
        self._clear_review_snapshot()
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
        shoe_rem: list[int] | None = None,
        dealer_guess_removed: bool | None = None,
    ) -> None:
        if isinstance(shoe_rem, list) and len(shoe_rem) == 10:
            self.sync_shoe_rem = [max(0, int(n)) for n in shoe_rem]
        elif shoe_rem is None:
            pass
        if dealer_guess_removed is not None:
            self.sync_dealer_guess_removed = bool(dealer_guess_removed)
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
            if new_count == 0:
                self._review_round_id = ""
                self._review_player_ranks.clear()
                self._review_dealer_ranks.clear()
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

    def _clear_review_snapshot(self) -> None:
        self._review_round_id = ""
        self._review_player_ranks.clear()
        self._review_dealer_ranks.clear()
        self._review_sub_dealer_tail.clear()
        self._review_badge_hint = None
        self._review_felt_sightings.clear()
        self._review_emitted_p = 0
        self._review_emitted_d = 0
        self._review_emitted_b = 0
        self._review_emitted_split_hands = 1
        self._place_bets_entered_ts = None
        self._place_bets_recheck_0_done = False
        self._place_bets_recheck_3_done = False
        self.sync_dealer_guess_removed = False

    def resync_round(self, clear_pending: bool = True, clear_review: bool | None = None) -> None:
        """Reset transient round accumulators without changing shoe/sync settings."""
        if clear_review is None:
            clear_review = clear_pending
        if clear_pending:
            self._pending_auto_watch = None
        if clear_review:
            self._clear_review_snapshot()
        self._reset_round_accumulators()
        self._last_banner_emitted = ""
        self._saw_clear_since_round_over = True
        self._place_bets_entered_ts = None
        self._place_bets_recheck_0_done = False
        self._place_bets_recheck_3_done = False
        self.state = "IDLE"

    def _flush_pending_auto_watch(self, events: list[dict[str, Any]]) -> None:
        if self._pending_auto_watch is not None:
            events.append(self._pending_auto_watch)
            self._pending_auto_watch = None

    def _begin_round_if_idle(self, events: list[dict[str, Any]] | None = None) -> None:
        if self.state in ("IDLE", "ROUND_OVER"):
            if events is not None:
                self._flush_pending_auto_watch(events)
            self._clear_review_snapshot()
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

    def _record_dealer_obs(self, obs: FrameObservation | None) -> None:
        if obs is None:
            return
        b_tot = obs.dealer_badge_total
        b_conf = float(obs.dealer_badge_conf if obs.dealer_badge_conf > 0.0 else 0.85)
        if b_tot is not None and 4 <= b_tot <= 26 and b_conf >= 0.56:
            self._last_dealer_badge_total = int(b_tot)
            self._last_dealer_badge_conf = b_conf
            if not self._dealer_badge_history or self._dealer_badge_history[-1]["total"] != int(b_tot):
                self._dealer_badge_history.append({"total": int(b_tot), "conf": b_conf})
            elif b_conf > self._dealer_badge_history[-1]["conf"]:
                self._dealer_badge_history[-1]["conf"] = b_conf
        b_hint = getattr(obs, "dealer_badge_hint", None)
        if isinstance(b_hint, dict):
            if self._last_dealer_badge_hint is None or float(b_hint.get("conf") or 0.0) >= float(self._last_dealer_badge_hint.get("conf") or 0.0):
                self._last_dealer_badge_hint = b_hint
        for idx, det in enumerate(obs.dealer_cards):
            if det.rank == "BACK" or det.val is None:
                continue
            already_real = (
                idx < len(self.d_debouncer.committed_ranks)
                and self.d_debouncer.committed_ranks[idx] != "BACK"
            )
            if already_real:
                continue
            if det.conf >= 0.70:
                prev = self._best_dealer_tail.get(idx)
                if prev is None or det.conf > prev.conf:
                    self._best_dealer_tail[idx] = det
            elif det.conf >= 0.42:
                self._best_sub_dealer_tail.setdefault(idx, []).append(
                    {
                        "rank": det.rank,
                        "val": det.val,
                        "conf": det.conf,
                        "suit": det.suit,
                        "has_pip_body": getattr(det, "has_pip_body", False),
                    }
                )
        d_sub_list = getattr(obs, "dealer_sub_cards", None) or []
        if d_sub_list:
            base_slot = max(
                sum(1 for d in obs.dealer_cards if d.rank != "BACK"),
                sum(1 for r in self.d_debouncer.committed_ranks if r != "BACK"),
            )
            for i, sdet in enumerate(d_sub_list):
                if not isinstance(sdet, dict) or not sdet.get("rank") or sdet.get("rank") == "BACK":
                    continue
                slot_idx = int(sdet["slotIdx"]) if isinstance(sdet.get("slotIdx"), int) else (base_slot + i)
                self._best_sub_dealer_tail.setdefault(slot_idx, []).append(sdet)

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
            if source != "educated_guess" and det.conf < self.cfg.min_auto_confidence:
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
        if not p_hands_ranks and self.sync_round_id == self.round_id and self.sync_draft_hands:
            p_hands_ranks = [
                ["10" if v == 0 else ("A" if v == 1 else str(v)) for v in h]
                for h in self.sync_draft_hands
                if h
            ]
        total_effective_p = max(self._emitted_p, sum(len(hr) for hr in p_hands_ranks))
        any_player_alive = any(not _eval_bj_ranks(hr)[2] for hr in p_hands_ranks) if p_hands_ranks else True
        all_players_natural_bj = bool(p_hands_ranks) and all(
            len(hr) == 2 and _eval_bj_ranks(hr)[0] == 21 for hr in p_hands_ranks
        )
        d_committed = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
        d_total, _, _ = _eval_bj_ranks(d_committed)

        # Automatic dealer_felt_roi fallback if the digital dealer HUD cleared incomplete (< 17 or < 2 cards)
        min_trusted_felt = min(self.cfg.min_felt_confidence, max(0.70, self.cfg.min_felt_confidence - 0.04))
        if (
            self.cfg.enable_dealer_felt_fallback
            and self._pending_dealer_felt
            and (total_effective_p >= 2 or len(d_committed) >= 2)
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

        def _is_dealer_still_incomplete(include_ten_ace_back: bool = True) -> bool:
            d_comm = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
            d_tot, _, _ = _eval_bj_ranks(d_comm)
            has_back = "BACK" in self.d_debouncer.committed_ranks
            back_incomplete = has_back and len(d_comm) >= 1 and (
                include_ten_ace_back or not all_players_natural_bj
            )
            return (
                total_effective_p >= 2
                and (
                    (len(d_comm) >= 2 and d_tot < 17)
                    or back_incomplete
                    or (len(d_comm) == 1 and not has_back and not all_players_natural_bj)
                )
            )

        # Fallback B2: lower-confidence wipe rescue if dealer hand is still incomplete
        if _is_dealer_still_incomplete(include_ten_ace_back=True):
            rescued_low = self.d_debouncer.flush_pending_on_wipe(min_conf=0.72)
            if rescued_low:
                self._emit_dealer_cards(rescued_low, events, ts=now)

        # Fallback C: transient best-seen dealer card buffer across the round
        if _is_dealer_still_incomplete(include_ten_ace_back=True) and self._best_dealer_tail:
            tail_recovered: list[tuple[int, CardDetection]] = []
            for idx in range(len(self.d_debouncer.committed_ranks)):
                if self.d_debouncer.committed_ranks[idx] == "BACK" and idx in self._best_dealer_tail:
                    det = self._best_dealer_tail[idx]
                    if det.conf >= 0.72:
                        self.d_debouncer.committed_ranks[idx] = det.rank
                        tail_recovered.append((idx, det))
            if "BACK" not in self.d_debouncer.committed_ranks:
                while True:
                    idx = len(self.d_debouncer.committed_ranks)
                    det = self._best_dealer_tail.get(idx)
                    if det is None or det.conf < 0.72:
                        break
                    self.d_debouncer.committed_ranks.append(det.rank)
                    tail_recovered.append((idx, det))
            if tail_recovered:
                self._emit_dealer_cards(tail_recovered, events, ts=now)

        # Fallback D: deduce missing dealer card(s) from Dealer Score Badge history or 2-card pair sum
        if _is_dealer_still_incomplete(include_ten_ace_back=True):
            def _commit_deduced_card(card: dict[str, Any], conf_val: float) -> None:
                if "BACK" in self.d_debouncer.committed_ranks:
                    slot_idx = self.d_debouncer.committed_ranks.index("BACK")
                    self.d_debouncer.committed_ranks[slot_idx] = card["rank"]
                else:
                    slot_idx = len(self.d_debouncer.committed_ranks)
                    self.d_debouncer.committed_ranks.append(card["rank"])
                deduced_det = CardDetection(
                    rank=card["rank"],
                    val=card["val"],
                    conf=max(0.78, conf_val or 0.85),
                    x=0,
                )
                self._emit_dealer_cards([(slot_idx, deduced_det)], events, ts=now, source="badge_deduced")

            badge_steps = (
                list(self._dealer_badge_history)
                if self._dealer_badge_history
                else (
                    [{"total": self._last_dealer_badge_total, "conf": self._last_dealer_badge_conf}]
                    if self._last_dealer_badge_total is not None
                    else []
                )
            )
            for step in badge_steps:
                if not _is_dealer_still_incomplete(include_ten_ace_back=True):
                    break
                d_comm_before = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
                deduced = deduce_missing_dealer_card(d_comm_before, step["total"])
                if deduced:
                    _commit_deduced_card(deduced, float(step.get("conf", 0.85)))

            if _is_dealer_still_incomplete(include_ten_ace_back=True) and self._last_dealer_badge_total is not None:
                d_comm_before = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
                pair = deduce_missing_dealer_pair(d_comm_before, self._last_dealer_badge_total)
                if pair and len(pair) == 2:
                    _commit_deduced_card(pair[0], self._last_dealer_badge_conf)
                    _commit_deduced_card(pair[1], self._last_dealer_badge_conf)

        # Fallback E: multi-signal educated guess across all 10 ranks (A, 2..9, 10) so dealer reaches >= 17
        educated_guess_rank: str | None = None
        guess_count = 0
        shoe_rem_copy = list(self.sync_shoe_rem) if isinstance(self.sync_shoe_rem, list) else None
        active_badge_hint = self._last_dealer_badge_hint
        while _is_dealer_still_incomplete(include_ten_ace_back=True) and guess_count < 6:
            d_comm_before = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
            if "BACK" in self.d_debouncer.committed_ranks:
                slot_idx = self.d_debouncer.committed_ranks.index("BACK")
            else:
                slot_idx = len(self.d_debouncer.committed_ranks)
            guess = guess_missing_dealer_card(
                d_comm_before,
                {
                    "slot_idx": slot_idx,
                    "sub_candidates": self._best_sub_dealer_tail,
                    "badge_hint": active_badge_hint,
                    "felt_sightings": self._round_felt_sightings,
                    "shoe_rem": shoe_rem_copy,
                    "round_seed": self._round_seq + guess_count * 7,
                    "must_reach_17": True,
                },
            )
            if not guess:
                break
            if "BACK" in self.d_debouncer.committed_ranks:
                self.d_debouncer.committed_ranks[slot_idx] = guess["rank"]
            else:
                self.d_debouncer.committed_ranks.append(guess["rank"])
            if shoe_rem_copy is not None and 0 <= int(guess["val"]) < len(shoe_rem_copy) and shoe_rem_copy[int(guess["val"])] > 0:
                shoe_rem_copy[int(guess["val"])] -= 1
            active_badge_hint = None
            educated_guess_rank = f"{educated_guess_rank},{guess['rank']}" if educated_guess_rank else guess["rank"]
            guess_det = CardDetection(rank=guess["rank"], val=guess["val"], conf=0.50, x=0)
            self._emit_dealer_cards([(slot_idx, guess_det)], events, ts=now, source="educated_guess")
            guess_count += 1

        d_committed = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
        d_total, _, _ = _eval_bj_ranks(d_committed)
        flags = list(self._uncertainty_flags)
        split_clean_multi = (
            self._emitted_split_hands >= 2
            and len(self.p_debouncers) >= 2
            and all(len(db.committed_ranks) >= 2 for db in self.p_debouncers.values())
        )
        if self._emitted_p < 2 or self._emitted_d < 1:
            flags.append("HUD_GLITCH")

        dealer_incomplete = _is_dealer_still_incomplete(include_ten_ace_back=True) or (educated_guess_rank is not None)
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
        non_dealer_inc_flags = [f for f in flags if f != "DEALER_INCOMPLETE"]
        auto_on_no_more_bets = (
            not can_auto_watch
            and self.sync_play_mode == "watch"
            and self._unread_community_pulses == 0
            and not non_dealer_inc_flags
            and (split_blocks_auto or educated_guess_rank is not None)
            and self._emitted_p >= 2
            and self._emitted_d >= 1
        )

        # Save persistent review snapshot before resync_round() wipes transient debouncers
        self._review_round_id = self.round_id
        self._review_player_ranks = [list(hr) for hr in p_hands_ranks]
        self._review_dealer_ranks = list(self.d_debouncer.committed_ranks)
        self._review_sub_dealer_tail = {k: list(v) for k, v in self._best_sub_dealer_tail.items()}
        self._review_badge_hint = dict(self._last_dealer_badge_hint) if self._last_dealer_badge_hint else None
        self._review_felt_sightings = list(self._round_felt_sightings)
        self._review_emitted_p = self._emitted_p
        self._review_emitted_d = self._emitted_d
        self._review_emitted_b = self._emitted_b
        self._review_emitted_split_hands = self._emitted_split_hands

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
            if auto_on_no_more_bets:
                sum_p = (
                    f"{self._emitted_p}P({self._emitted_split_hands}H)"
                    if self._emitted_split_hands >= 2
                    else f"{self._emitted_p}P"
                )
                self._flush_pending_auto_watch(events)
                self._pending_auto_watch = {
                    "type": "auto_watch",
                    "roundId": self.round_id,
                    "handCount": self._emitted_split_hands,
                    "summary": f"{sum_p}+{self._emitted_d}D+{self._emitted_b}B",
                    "fromWatch": True,
                }
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
                    "educatedGuessRank": educated_guess_rank,
                    "feltSightings": list(self._round_felt_sightings),
                }
            )

        self.sync_track_felt = False
        self.resync_round(clear_pending=not (can_auto_watch or auto_on_no_more_bets), clear_review=False)
        return events

    def _recheck_dealer_in_place_bets(
        self,
        obs: FrameObservation,
        events: list[dict[str, Any]],
        now: float,
        stage: str,
    ) -> None:
        """Verify dealer hand completeness at PLACE_BETS start (0s) and +3s; complete via educated guess if needed."""
        if self.sync_dealer_guess_removed:
            return
        target_round_id = self._review_round_id or ((self.sync_round_id or self.round_id) if self.sync_draft_count > 0 else "")
        if not target_round_id:
            return
        if self.sync_round_id == target_round_id and self.sync_draft_count == 0 and self._pending_auto_watch is None:
            return

        # Reconcile player & dealer ranks with UI sync state if available
        p_hands_ranks = list(self._review_player_ranks)
        if self.sync_draft_hands and (self.sync_draft_count > 0 or not p_hands_ranks):
            ui_hands = [
                ["10" if v == 0 else ("A" if v == 1 else str(v)) for v in h]
                for h in self.sync_draft_hands
                if h
            ]
            if ui_hands:
                p_hands_ranks = ui_hands
        d_ranks = list(self._review_dealer_ranks)
        review_real_cnt = len([r for r in d_ranks if r != "BACK"])
        if self.sync_draft_dealer is not None and (self.sync_draft_count > 0 or not d_ranks):
            ui_d = ["10" if v == 0 else ("A" if v == 1 else str(v)) for v in self.sync_draft_dealer]
            if self.sync_draft_dealer_manual and len(ui_d) < review_real_cnt:
                return
            if stage == "3s" or len(ui_d) >= review_real_cnt:
                d_ranks = ui_d

        if not d_ranks and not p_hands_ranks:
            return

        all_players_natural_bj = bool(p_hands_ranks) and all(
            len(hr) == 2 and _eval_bj_ranks(hr)[0] == 21 for hr in p_hands_ranks
        )
        total_p = sum(len(hr) for hr in p_hands_ranks)

        def _rev_incomplete() -> bool:
            d_comm = [r for r in d_ranks if r != "BACK"]
            d_tot, _, _ = _eval_bj_ranks(d_comm)
            has_back = "BACK" in d_ranks
            return (
                total_p >= 2
                and (
                    (len(d_comm) >= 2 and d_tot < 17)
                    or (has_back and len(d_comm) >= 1)
                    or (len(d_comm) == 1 and not has_back and not all_players_natural_bj)
                    or (stage == "3s" and len(d_comm) == 0)
                )
            )

        if not _rev_incomplete():
            return

        # Check if late dealer cards appeared on the current frame during PLACE_BETS (verify prefix matches)
        if obs.dealer_cards:
            real_obs = [d for d in obs.dealer_cards if d.rank != "BACK" and d.val is not None and d.conf >= 0.72]
            existing_real = [r for r in d_ranks if r != "BACK"]
            prefix_matches = len(real_obs) > len(existing_real) and all(
                _same_bj_value(real_obs[k].rank, existing_real[k]) for k in range(len(existing_real))
            )
            while prefix_matches and len(real_obs) > len([r for r in d_ranks if r != "BACK"]):
                nxt_idx = len([r for r in d_ranks if r != "BACK"])
                det = real_obs[nxt_idx]
                if "BACK" in d_ranks:
                    slot_idx = d_ranks.index("BACK")
                    d_ranks[slot_idx] = det.rank
                else:
                    slot_idx = len(d_ranks)
                    d_ranks.append(det.rank)
                self._review_emitted_d += 1
                evt_d: dict[str, Any] = {
                    "type": "card",
                    "target": "D",
                    "val": det.val,
                    "slot": slot_idx,
                    "rankLabel": det.rank,
                    "conf": round(det.conf, 3),
                    "source": "hud",
                    "roundId": target_round_id,
                }
                if det.suit:
                    evt_d["suit"] = det.suit
                events.append(evt_d)

        guessed_ranks: list[str] = []
        guess_count = 0
        shoe_rem_copy = list(self.sync_shoe_rem) if isinstance(self.sync_shoe_rem, list) else None
        active_badge_hint = self._review_badge_hint
        while _rev_incomplete() and guess_count < 6:
            d_comm_before = [r for r in d_ranks if r != "BACK"]
            slot_idx = d_ranks.index("BACK") if "BACK" in d_ranks else len(d_ranks)
            guess = guess_missing_dealer_card(
                d_comm_before,
                {
                    "slot_idx": slot_idx,
                    "sub_candidates": self._review_sub_dealer_tail,
                    "badge_hint": active_badge_hint,
                    "felt_sightings": self._review_felt_sightings,
                    "shoe_rem": shoe_rem_copy,
                    "round_seed": self._round_seq + guess_count * 7 + (13 if stage == "3s" else 0),
                    "must_reach_17": True,
                },
            )
            if not guess:
                break
            if "BACK" in d_ranks:
                d_ranks[slot_idx] = guess["rank"]
            else:
                d_ranks.append(guess["rank"])
            if shoe_rem_copy is not None and 0 <= int(guess["val"]) < len(shoe_rem_copy) and shoe_rem_copy[int(guess["val"])] > 0:
                shoe_rem_copy[int(guess["val"])] -= 1
            active_badge_hint = None
            guessed_ranks.append(guess["rank"])
            self._review_emitted_d += 1
            events.append(
                {
                    "type": "card",
                    "target": "D",
                    "val": guess["val"],
                    "slot": slot_idx,
                    "rankLabel": guess["rank"],
                    "conf": 0.5,
                    "source": "educated_guess",
                    "roundId": target_round_id,
                }
            )
            guess_count += 1

        self._review_dealer_ranks = list(d_ranks)
        if not guessed_ranks:
            return

        d_comm_after = [r for r in d_ranks if r != "BACK"]
        d_total_after, _, _ = _eval_bj_ranks(d_comm_after)
        if self._pending_auto_watch is not None and self._pending_auto_watch.get("roundId") == target_round_id:
            sum_p = (
                f"{self._review_emitted_p}P({self._review_emitted_split_hands}H)"
                if self._review_emitted_split_hands >= 2
                else f"{self._review_emitted_p}P"
            )
            self._pending_auto_watch["summary"] = f"{sum_p}+{len(d_comm_after)}D+{self._review_emitted_b}B"
        events.append(
            {
                "type": "prompt_confirm",
                "roundId": target_round_id,
                "reason": "DEALER_INCOMPLETE",
                "unreadCommunity": 0,
                "uncertainSlots": ["DEALER_INCOMPLETE"],
                "suggestedFocus": "D",
                "dealerTotal": d_total_after,
                "educatedGuessRank": ",".join(guessed_ranks),
                "feltSightings": list(self._review_felt_sightings),
            }
        )

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

    def step(self, obs: FrameObservation, *, keepalive_grace: bool = False) -> list[dict[str, Any]]:
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
            self._record_dealer_obs(obs)
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
                self._clear_review_snapshot()
                self.resync_round()
                self.state = "SHUFFLING"
                self._shuffle_emitted = True
                self.shoe_tracker.reset_for_new_shoe(obs.ts)
                events.append({"type": "new_shoe", "reason": "cut", "hasPendingBet": has_pending_bet})
            return events

        self._shuffle_since = None
        if self.state == "SHUFFLING":
            self.state = "IDLE"
        self._shuffle_emitted = False

        # 1b. Dual-ROI physical shoe / movable discard-tray auto-new-shoe detection
        cards_seen_in_shoe = (
            max(0, 416 - sum(self.sync_shoe_rem))
            if self.sync_shoe_rem and len(self.sync_shoe_rem) == 10
            else 0
        )
        has_active_cards_now = bool(
            obs.player_cards or obs.dealer_cards or obs.player_hands or self._emitted_p > 0 or self._emitted_d > 0
        )
        in_betting_ui = obs.banner_state in ("PLACE_BETS", "LAST_BETS")
        swap_reason = self.shoe_tracker.step(
            obs,
            cards_seen=cards_seen_in_shoe,
            has_active_cards=has_active_cards_now,
            in_betting_overlay=in_betting_ui,
            suppressed=bool(keepalive_grace or obs.loading_screen or self.paused),
        )
        if swap_reason:
            self._flush_pending_auto_watch(events)
            has_pending_bet = (self.state not in ("IDLE", "BETTING")) and (
                self.sync_play_mode == "bet" or self._split_detected
            )
            self.sync_track_felt = False
            self._clear_review_snapshot()
            self.resync_round()
            self.state = "IDLE"
            events.append(
                {
                    "type": "new_shoe",
                    "reason": swap_reason,
                    "cardsSeen": cards_seen_in_shoe,
                    "hasPendingBet": has_pending_bet,
                }
            )
            return events

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
                self._record_dealer_obs(obs)
                if obs.dealer_cards:
                    new_d_pre, _ = self.d_debouncer.update(obs.dealer_cards)
                    if new_d_pre:
                        self._emit_dealer_cards(new_d_pre, events, ts=obs.ts)
                events.extend(self._finalize_cleared_round(obs.ts))
            if banner == "NO_MORE_BETS":
                self._flush_pending_auto_watch(events)
                self._clear_review_snapshot()
            was_empty_in_betting = self._saw_empty_hud_in_betting or (not has_obs_cards)
            self.state = "BETTING"
            self._saw_clear_since_round_over = True
            self._emit_banner_phase_if_changed(banner, obs.banner_conf, events)
            self._reset_round_accumulators()
            self.sync_track_felt = False
            self._last_bet_banner = banner
            self._saw_empty_hud_in_betting = was_empty_in_betting
            if banner in ("PLACE_BETS", "LAST_BETS"):
                if self._place_bets_entered_ts is None:
                    self._place_bets_entered_ts = obs.ts
                    self._place_bets_recheck_0_done = False
                    self._place_bets_recheck_3_done = False
                if not self._place_bets_recheck_0_done:
                    self._place_bets_recheck_0_done = True
                    self._recheck_dealer_in_place_bets(obs, events, obs.ts, "0s")
                if (
                    not self._place_bets_recheck_3_done
                    and (obs.ts - self._place_bets_entered_ts) >= 3.0
                ):
                    self._place_bets_recheck_3_done = True
                    self._recheck_dealer_in_place_bets(obs, events, obs.ts, "3s")
            if banner == "NO_MORE_BETS" and was_empty_in_betting and has_obs_cards:
                self._predeal_obs = obs
            return events

        if (
            self.state == "BETTING"
            and self._place_bets_entered_ts is not None
            and not self._place_bets_recheck_3_done
            and (obs.ts - self._place_bets_entered_ts) >= 3.0
            and self._last_bet_banner in ("PLACE_BETS", "LAST_BETS")
        ):
            self._place_bets_recheck_3_done = True
            self._recheck_dealer_in_place_bets(obs, events, obs.ts, "3s")

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
            h1_has_split_seed = bool(h1_list) and len(prev_h0) >= 2 and _same_bj_value(h1_list[0].rank, prev_h0[1])
            unseparated_overlap = (
                self._emitted_split_hands == 1
                and len(prev_h0) == 2
                and _same_bj_value(prev_h0[0], prev_h0[1])
                and len(h0_list) >= 2
                and _same_bj_value(h0_list[0].rank, prev_h0[0])
                and _same_bj_value(h0_list[1].rank, prev_h0[1])
                and not h1_has_split_seed
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
                    elif 0 in self._split_seed_cards:
                        inferred_seed_rk = self._split_seed_cards[0].rank
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
                    for h_k in sorted(hands_input.keys())[:4]:
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

                        # Always anchor H1..H3 slot 0 to split seed rank so temporary occlusion/misread cannot duplicate card 0
                        for h_k in sorted(hands_input.keys())[1:4]:
                            seed_rk = prev_h0[1] if h_k < 2 else (inferred_seed_rk or prev_h0[0])
                            h_list = hands_input.get(h_k, [])
                            dk = SequenceDebouncer(self.cfg.confirm_frames, self.cfg.clear_frames)
                            if h_list:
                                seq_cur = tuple(d.rank for d in h_list)
                                p_seq, p_cnt = prior_pending_seqs.get(h_k, ((), 0))
                                p_seq_norm = (
                                    p_seq
                                    if (p_seq and _same_bj_value(p_seq[0], seed_rk))
                                    else ((seed_rk,) + p_seq if p_seq else ())
                                )
                                if (
                                    _same_bj_value(h_list[0].rank, seed_rk)
                                    and p_seq_norm == seq_cur
                                    and p_cnt > 0
                                    and len(seq_cur) > 1
                                ):
                                    dk.reanchor([seed_rk], cand_ranks=seq_cur, cand_count=p_cnt)
                                else:
                                    dk.reanchor([seed_rk])
                            else:
                                dk.reanchor([seed_rk])
                            self.p_debouncers[h_k] = dk
                    else:
                        for h_k in sorted(hands_input.keys())[:4]:
                            if h_k in self.p_debouncers or h_k not in self._split_seed_cards:
                                continue
                            dk = SequenceDebouncer(self.cfg.confirm_frames, self.cfg.clear_frames)
                            dk.reanchor([self._split_seed_cards[h_k].rank])
                            self.p_debouncers[h_k] = dk
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

        self._record_dealer_obs(obs)
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
            self.state = "ROUND_OVER"
            self._saw_clear_since_round_over = False
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

        # Monotonic guard: never shrink or delete already-committed cards in player or dealer debouncers
        if len(sorted_keys) < sum(1 for db in self.p_debouncers.values() if db.committed_ranks):
            return
        for h in sorted_keys:
            comm_p = [r for r in self.p_debouncers[h].committed_ranks if r != "BACK"] if h in self.p_debouncers else []
            hud_p = hands_input[h]
            if len(hud_p) < len(comm_p):
                return
            if len(hud_p) == len(comm_p):
                for j in range(len(comm_p)):
                    if not _same_bj_value(hud_p[j].rank, comm_p[j]) and hud_p[j].conf < 0.90:
                        return
        comm_d = [r for r in self.d_debouncer.committed_ranks if r != "BACK"]
        if len(d_real) < len(comm_d):
            return
        if len(d_real) == len(comm_d):
            for j in range(len(comm_d)):
                if not _same_bj_value(d_real[j].rank, comm_d[j]) and d_real[j].conf < 0.90:
                    return

        deb_hands = {
            h: [r for r in db.committed_ranks if r != "BACK"]
            for h, db in sorted(self.p_debouncers.items())
            if db.committed_ranks
        }
        hud_hands_ranks = {h: [d.rank for d in hands_input[h]] for h in sorted_keys}
        deb_dealer = comm_d
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
                if not (any(self.sync_draft_hands_manual) and total_hud <= total_ui):
                    p_ui_mismatch = True
            else:
                for idx, (ui_h, hud_h) in enumerate(zip(self.sync_draft_hands, hud_hands_vals)):
                    if ui_h != hud_h:
                        is_man = idx < len(self.sync_draft_hands_manual) and self.sync_draft_hands_manual[idx]
                        if not (is_man and len(hud_h) <= len(ui_h)):
                            p_ui_mismatch = True
                            break
            if self.sync_draft_dealer is not None and self.sync_draft_dealer != hud_dealer_vals:
                if not (self.sync_draft_dealer_manual and len(hud_dealer_vals) <= len(self.sync_draft_dealer)):
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

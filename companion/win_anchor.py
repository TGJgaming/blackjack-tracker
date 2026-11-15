"""DPI-aware Windows client-area window discovery, 16:9 game viewport anchoring, and MSS ROI capture."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import mss
import numpy as np


@dataclass(frozen=True)
class WindowRect:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int

    @property
    def is_valid(self) -> bool:
        return self.width >= 100 and self.height >= 100


_DPI_DONE = False


def set_dpi_aware() -> None:
    """Enable Per-Monitor V2 DPI awareness before any Win32 coordinate call."""
    global _DPI_DONE
    if _DPI_DONE or sys.platform != "win32":
        return
    _DPI_DONE = True
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# Ensure DPI awareness is active at module import time
set_dpi_aware()


def enable_high_res_timer() -> bool:
    """Request 1ms Windows multimedia timer resolution so asyncio.sleep hits 50-60 FPS cleanly."""
    if sys.platform != "win32":
        return False
    try:
        return ctypes.windll.winmm.timeBeginPeriod(1) == 0
    except Exception:
        return False


def disable_high_res_timer() -> None:
    """Release the 1ms Windows multimedia timer request."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def get_primary_work_area() -> tuple[int, int]:
    """Return usable screen width & height in physical pixels (excluding taskbar when possible)."""
    set_dpi_aware()
    if sys.platform == "win32":
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return max(640, int(rect.right - rect.left)), max(480, int(rect.bottom - rect.top))
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        if w > 0 and h > 0:
            return int(w), int(h)
    return 1920, 1080


def resolve_roi(anchor: WindowRect, norm_roi: tuple[float, float, float, float]) -> dict[str, int]:
    """Convert normalized [rx, ry, rw, rh] coordinates inside anchor rect to integer MSS screen bounds."""
    rx, ry, rw, rh = norm_roi
    left = max(anchor.left, min(anchor.left + max(0, anchor.width - 4), anchor.left + int(round(rx * anchor.width))))
    top = max(anchor.top, min(anchor.top + max(0, anchor.height - 4), anchor.top + int(round(ry * anchor.height))))
    width = max(4, int(round(rw * anchor.width)))
    height = max(4, int(round(rh * anchor.height)))
    right = min(anchor.left + anchor.width, left + width)
    bottom = min(anchor.top + anchor.height, top + height)
    width = max(4, right - left)
    height = max(4, bottom - top)
    return {"left": left, "top": top, "width": width, "height": height}


def detect_game_viewport_local(win_bgr: np.ndarray) -> tuple[int, int, int, int]:
    """Detect Playtech game canvas (x, y, w, h) inside a captured browser client BGR image."""
    h, w = win_bgr.shape[:2]
    if h < 120 or w < 160:
        return (0, 0, w, h)

    gray = cv2.cvtColor(win_bgr, cv2.COLOR_BGR2GRAY)
    # 1. Inspect central horizontal band to ignore Brave's left sidebar & scrollbar
    cx0, cx1 = int(w * 0.14), int(w * 0.92)
    sub = gray[:, cx0:cx1]
    row_active = np.mean(sub > 28, axis=1) > 0.14

    # Search below browser top tabs/URL/bookmarks strip (~15% min, or after first dark gap)
    y_min = int(h * 0.14)
    runs: list[tuple[int, int]] = []
    in_run = False
    start_y = y_min
    for y in range(y_min, h):
        if row_active[y] and not in_run:
            in_run = True
            start_y = y
        elif not row_active[y] and in_run:
            in_run = False
            runs.append((start_y, y))
    if in_run:
        runs.append((start_y, h))

    # Bridge dark sub-bars (<= 18px) inside the Playtech header/footer
    merged: list[tuple[int, int]] = []
    for r in runs:
        if merged and (r[0] - merged[-1][1]) <= 18:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)

    if not merged:
        return (0, 0, w, h)

    y0, y1 = max(merged, key=lambda r: r[1] - r[0])
    if (y1 - y0) < int(h * 0.25):
        return (0, 0, w, h)

    # Expand y0 slightly upward into dark Playtech top header bar so thin top text ("WAIT FOR THE NEXT ROUND") isn't clipped
    pad_limit = max(6, min(14, int(round(h * 0.015))))
    steps = 0
    while steps < pad_limit and y0 > y_min:
        row_above = sub[y0 - 1]
        if float(np.mean(row_above > 90)) >= 0.30:
            break
        if float(np.mean(row_above > 22)) <= 0.004 and int(row_above.max()) <= 32:
            # Look ahead a few pixels for thin bright header text (e.g. WAIT) separated by a 1-3px dark spacer row
            look_top = max(y_min, y0 - max(2, pad_limit - steps))
            if not np.any(sub[look_top : y0 - 1] > 80):
                break
        y0 -= 1
        steps += 1
    y0 = max(y_min, y0 - 2)

    # 2. Detect vertical browser sidebar on left (e.g. Brave sidebar) via dark gap strip above video
    x0 = 0
    if (y0 - y_min) >= 20:
        gap_top = max(y_min, y0 - 45)
        gap_bot = max(gap_top + 4, y0 - 6)
        gap = gray[gap_top:gap_bot, : int(w * 0.16)]
        side_cols = np.where(np.mean(gap > 22, axis=0) > 0.5)[0]
        if len(side_cols):
            cand_x0 = int(side_cols[-1]) + 1
            if cand_x0 < int(w * 0.14):
                x0 = cand_x0

    vw = max(100, w - x0)
    vh = max(100, y1 - y0)
    return (x0, y0, vw, vh)


class ViewportSmoother:
    """Stabilizes detected game viewport bounds across frames with small jitter hysteresis."""

    def __init__(self, tol_px: int = 8) -> None:
        self.tol_px = tol_px
        self._last_win_size: tuple[int, int] | None = None
        self._cached_local: tuple[int, int, int, int] | None = None

    def reset(self) -> None:
        self._last_win_size = None
        self._cached_local = None

    def resolve_anchor(self, win: WindowRect, win_bgr: np.ndarray, anchor_mode: str) -> tuple[WindowRect, tuple[int, int, int, int]]:
        """Return (absolute_screen_anchor_rect, local_viewport_rect_inside_win)."""
        local = detect_game_viewport_local(win_bgr)
        if anchor_mode != "game_viewport":
            full_local = (0, 0, win.width, win.height)
            return win, local

        win_size = (win.width, win.height)
        if (
            self._cached_local is None
            or self._last_win_size != win_size
            or any(abs(a - b) > self.tol_px for a, b in zip(local, self._cached_local))
        ):
            self._cached_local = local
            self._last_win_size = win_size

        vx, vy, vw, vh = self._cached_local
        abs_anchor = WindowRect(
            hwnd=win.hwnd,
            title=win.title,
            left=win.left + vx,
            top=win.top + vy,
            width=vw,
            height=vh,
        )
        return abs_anchor, (vx, vy, vw, vh)


def _match_window_title(title: str, title_substrings: list[str]) -> int | None:
    """Return index of first matching configured title substring, excluding tracker/helper UIs."""
    low = title.lower()
    if (
        ("advantage" in low and "wonging" in low)
        or "companion preview" in low
        or "roi calibrator" in low
    ):
        return None
    for idx, sub in enumerate(title_substrings):
        needle = sub.strip().lower()
        if needle and needle in low:
            return idx
    return None


def _read_window_client_rect(user32: Any, hwnd: int, title: str) -> WindowRect | None:
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    crect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(crect)):
        return None
    pt = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    w = int(crect.right - crect.left)
    h = int(crect.bottom - crect.top)
    rect = WindowRect(hwnd=int(hwnd), title=title, left=int(pt.x), top=int(pt.y), width=w, height=h)
    return rect if rect.is_valid else None


def find_target_window(title_substrings: list[str], prefer_left: bool = True) -> WindowRect | None:
    """Locate the best visible, non-minimized window matching any configured title substring."""
    set_dpi_aware()
    if sys.platform != "win32":
        return None

    user32 = ctypes.windll.user32
    work_w, _ = get_primary_work_area()
    candidates: list[tuple[int, WindowRect]] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        matched_rank = _match_window_title(title, title_substrings)
        if matched_rank is None:
            return True
        rect = _read_window_client_rect(user32, hwnd, title)
        if rect is not None:
            candidates.append((matched_rank, rect))
        return True

    user32.EnumWindows(EnumWindowsProc(_callback), 0)
    if not candidates:
        return None
    if prefer_left:
        left_cutoff = int(work_w * 0.45)
        candidates.sort(
            key=lambda item: (
                item[0],
                0 if (item[1].left < left_cutoff or item[1].width >= int(work_w * 0.65)) else 1,
                item[1].left,
                -(item[1].width * item[1].height),
            )
        )
    else:
        candidates.sort(key=lambda item: (item[0], -(item[1].width * item[1].height)))
    return candidates[0][1]


def verify_target_hwnd(hwnd: int, title_substrings: list[str], prefer_left: bool = True) -> bool:
    """Verify HWND still points to a visible matching casino window (and on left side when split-screen)."""
    if sys.platform != "win32" or hwnd <= 0:
        return False
    set_dpi_aware()
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return False
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value
    if _match_window_title(title, title_substrings) is None:
        return False
    rect = _read_window_client_rect(user32, hwnd, title)
    if rect is None:
        return False
    if prefer_left:
        work_w, _ = get_primary_work_area()
        # Protect split-screen right-hand windows from ever receiving an automated refresh
        if rect.width < int(work_w * 0.65) and rect.left >= int(work_w * 0.45):
            return False
    return True


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_user_idle_ms() -> int:
    """Return milliseconds since the user's last mouse or keyboard input on Windows."""
    if sys.platform != "win32":
        return 10000
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 10000
        tick = ctypes.windll.kernel32.GetTickCount()
        return int((tick - lii.dwTime) & 0xFFFFFFFF)
    except Exception:
        return 10000


_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _build_key_input(vk: int, key_up: bool = False) -> _INPUT:
    user32 = ctypes.windll.user32
    scan = int(user32.MapVirtualKeyW(vk, 0))
    flags = 0x0002 if key_up else 0  # KEYEVENTF_KEYUP on release; keep wVk + wScan for Chromium
    inp = _INPUT()
    inp.type = 1  # INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


_build_vk_scan_input = _build_key_input


def _send_vk(user32: Any, vk: int, key_up: bool = False) -> bool:
    inp = _build_key_input(vk, key_up=key_up)
    return bool(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) == 1)


def _send_mouse_left_click(user32: Any, sx: int, sy: int) -> bool:
    """Move cursor to (sx, sy) with tiny jitter, left-click once, and restore prior cursor position."""
    prev_pt = wintypes.POINT()
    has_prev = bool(user32.GetCursorPos(ctypes.byref(prev_pt)))
    try:
        tx = int(sx + random.randint(-1, 1))
        ty = int(sy + random.randint(-1, 1))
        user32.SetCursorPos(tx, ty)
        time.sleep(0.025)
        down = _INPUT()
        down.type = 0  # INPUT_MOUSE
        down.u.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=0x0002, time=0, dwExtraInfo=0)
        up = _INPUT()
        up.type = 0  # INPUT_MOUSE
        up.u.mi = _MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=0x0004, time=0, dwExtraInfo=0)
        s1 = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(down))
        time.sleep(random.uniform(0.045, 0.085))
        s2 = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(up))
        time.sleep(0.045)
        return bool(s1 == 1 and s2 == 1)
    finally:
        if has_prev:
            user32.SetCursorPos(int(prev_pt.x), int(prev_pt.y))


def _load_reload_templates(templates_root: Path | None = None) -> list[np.ndarray]:
    root = templates_root or (Path(__file__).resolve().parent / "templates")
    ui_dir = root / "ui"
    if not ui_dir.exists():
        return []
    out: list[np.ndarray] = []
    for png in sorted(ui_dir.glob("RELOAD_BTN*.png")):
        img = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
        if img is not None and img.size > 0 and float(np.std(img)) >= 2.0:
            out.append(img)
    return out


def match_reload_button_in_toolbar(
    toolbar_bgr: np.ndarray,
    templates: list[np.ndarray],
    offset_hint: list[int] | tuple[int, int] | None = None,
    min_conf: float = 0.76,
) -> tuple[int, int, float] | None:
    """Locate Brave's reload button center (cx, cy, conf) inside a top-left toolbar BGR crop."""
    if toolbar_bgr.size == 0 or not templates:
        return None
    gray = cv2.cvtColor(toolbar_bgr, cv2.COLOR_BGR2GRAY) if toolbar_bgr.ndim == 3 else toolbar_bgr
    th, tw = gray.shape[:2]
    candidates: list[tuple[float, float, int, int]] = []  # (conf, dist, cx, cy)
    for tpl in templates:
        tpl_g = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY) if tpl.ndim == 3 else tpl
        rh, rw = tpl_g.shape[:2]
        for sc in (0.85, 0.92, 1.0, 1.08, 1.16):
            sw, sh = max(6, int(round(rw * sc))), max(6, int(round(rh * sc)))
            if sw >= tw or sh >= th:
                continue
            resized = tpl_g if sc == 1.0 else cv2.resize(tpl_g, (sw, sh), interpolation=cv2.INTER_AREA if sc < 1.0 else cv2.INTER_CUBIC)
            if float(np.std(resized)) < 2.0:
                continue
            res = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if not np.isfinite(mx) or float(mx) < min_conf:
                continue
            px, py = int(loc[0]), int(loc[1])
            patch = gray[py : py + sh, px : px + sw]
            if int(patch.max()) - int(patch.min()) < 45:
                continue
            cx = px + sw // 2
            cy = py + sh // 2
            dist = 0.0
            if offset_hint and len(offset_hint) >= 2:
                dist = float(((cx - offset_hint[0]) ** 2 + (cy - offset_hint[1]) ** 2) ** 0.5)
            candidates.append((float(mx), dist, cx, cy))
    if not candidates:
        return None
    top_conf = max(c[0] for c in candidates)
    near_best = [c for c in candidates if c[0] >= top_conf - 0.05]
    if offset_hint and len(offset_hint) >= 2:
        chosen = min(near_best, key=lambda c: (c[1], -c[0]))
    else:
        chosen = max(near_best, key=lambda c: c[0])
    return chosen[2], chosen[3], chosen[0]


def locate_reload_button_screen_xy(
    hwnd: int,
    templates_root: Path | None = None,
    offset_hint: list[int] | tuple[int, int] | None = None,
    min_conf: float = 0.76,
) -> tuple[int, int, float] | None:
    """Capture Brave's top-left toolbar strip and return verified screen (sx, sy, conf) for the Reload button."""
    if sys.platform != "win32" or hwnd <= 0:
        return None
    templates = _load_reload_templates(templates_root)
    if not templates:
        return None
    user32 = ctypes.windll.user32
    rect = _read_window_client_rect(user32, hwnd, "")
    if rect is None:
        return None
    max_x = min(rect.width, max(320, int(offset_hint[0]) + 140) if offset_hint and len(offset_hint) >= 2 else 340)
    max_y = min(rect.height, max(150, int(offset_hint[1]) + 80) if offset_hint and len(offset_hint) >= 2 else 155)
    cap = WindowCapturer()
    try:
        strip = cap.grab_rect({"left": rect.left, "top": rect.top, "width": max(32, max_x), "height": max(32, max_y)})
    finally:
        cap.close()
    hit = match_reload_button_in_toolbar(strip, templates, offset_hint=offset_hint, min_conf=min_conf)
    if hit is None:
        return None
    cx, cy, conf = hit
    return rect.left + cx, rect.top + cy, conf


def _focus_window_temporarily(user32: Any, target_hwnd: int) -> tuple[bool, int]:
    """Safely focus target_hwnd and return (focused_ok, prev_foreground_hwnd)."""
    prev_hwnd = int(user32.GetForegroundWindow() or 0)
    if prev_hwnd == target_hwnd:
        return True, prev_hwnd

    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(prev_hwnd, None) if prev_hwnd else 0
    tgt_tid = user32.GetWindowThreadProcessId(target_hwnd, None)
    attached_fg = False
    attached_tgt = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        if tgt_tid and tgt_tid != cur_tid:
            attached_tgt = bool(user32.AttachThreadInput(cur_tid, tgt_tid, True))
        user32.BringWindowToTop(target_hwnd)
        user32.SetForegroundWindow(target_hwnd)
        time.sleep(0.035)
        focused = int(user32.GetForegroundWindow() or 0) == target_hwnd
        if not focused:
            user32.ShowWindow(target_hwnd, 5)  # SW_SHOW
            user32.SwitchToThisWindow(target_hwnd, True)
            time.sleep(0.045)
            focused = int(user32.GetForegroundWindow() or 0) == target_hwnd
        return focused, prev_hwnd
    finally:
        if attached_tgt and tgt_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, tgt_tid, False)
        if attached_fg and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def _restore_foreground_window(user32: Any, prev_hwnd: int, target_hwnd: int) -> None:
    if not prev_hwnd or prev_hwnd == target_hwnd or not user32.IsWindow(prev_hwnd):
        return
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_hwnd = int(user32.GetForegroundWindow() or 0)
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    prev_tid = user32.GetWindowThreadProcessId(prev_hwnd, None)
    attached_fg = False
    attached_prev = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        if prev_tid and prev_tid != cur_tid:
            attached_prev = bool(user32.AttachThreadInput(cur_tid, prev_tid, True))
        user32.SetForegroundWindow(prev_hwnd)
    except Exception:
        pass
    finally:
        if attached_prev and prev_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, prev_tid, False)
        if attached_fg and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def trigger_browser_refresh(
    hwnd: int,
    title_substrings: list[str],
    method: str = "click_reload",
    prefer_left: bool = True,
    templates_root: Path | None = None,
    reload_btn_offset: list[int] | tuple[int, int] | None = None,
) -> tuple[bool, str]:
    """Refresh verified left-hand game window via Reload button click, F5, Ctrl+R, or WM_APPCOMMAND."""
    if sys.platform != "win32":
        return False, "unsupported_os"
    if not verify_target_hwnd(hwnd, title_substrings, prefer_left=prefer_left):
        return False, "target_verification_failed"

    user32 = ctypes.windll.user32
    if method == "appcommand":
        WM_APPCOMMAND = 0x0319
        APPCOMMAND_BROWSER_REFRESH = 13
        posted = bool(user32.PostMessageW(hwnd, WM_APPCOMMAND, hwnd, APPCOMMAND_BROWSER_REFRESH << 16))
        return posted, ("appcommand" if posted else "appcommand_post_failed")

    if method == "click_reload":
        focused, prev_hwnd = _focus_window_temporarily(user32, hwnd)
        if not focused:
            return False, "focus_blocked"
        try:
            if not verify_target_hwnd(hwnd, title_substrings, prefer_left=prefer_left):
                return False, "target_changed"
            hit = locate_reload_button_screen_xy(
                hwnd,
                templates_root=templates_root,
                offset_hint=reload_btn_offset,
            )
            if hit is None:
                return False, "reload_btn_not_found"
            sx, sy, _conf = hit
            ok = _send_mouse_left_click(user32, sx, sy)
            return ok, ("click_reload" if ok else "click_failed")
        finally:
            _restore_foreground_window(user32, prev_hwnd, hwnd)

    if method in ("sendinput_f5", "sendinput_ctrl_r"):
        focused, prev_hwnd = _focus_window_temporarily(user32, hwnd)
        if not focused:
            return False, "focus_blocked"
        ctrl_held = False
        try:
            if not verify_target_hwnd(hwnd, title_substrings, prefer_left=prefer_left):
                return False, "target_changed"
            if method == "sendinput_f5":
                vk_f5 = 0x74
                sent_down = _send_vk(user32, vk_f5, key_up=False)
                time.sleep(random.uniform(0.055, 0.095))
                sent_up = _send_vk(user32, vk_f5, key_up=True)
                time.sleep(0.055)
                ok = bool(sent_down and sent_up)
                return ok, ("sendinput_f5" if ok else "sendinput_failed")

            vk_ctrl = 0x11
            vk_r = 0x52
            ctrl_held = _send_vk(user32, vk_ctrl, key_up=False)
            time.sleep(0.025)
            r_down = _send_vk(user32, vk_r, key_up=False)
            time.sleep(random.uniform(0.045, 0.085))
            r_up = _send_vk(user32, vk_r, key_up=True)
            time.sleep(0.025)
            ctrl_up = _send_vk(user32, vk_ctrl, key_up=True)
            ctrl_held = ctrl_held and not ctrl_up
            time.sleep(0.055)
            ok = bool(r_down and r_up and ctrl_up)
            return ok, ("sendinput_ctrl_r" if ok else "sendinput_failed")
        finally:
            if ctrl_held:
                _send_vk(user32, 0x11, key_up=True)
            _restore_foreground_window(user32, prev_hwnd, hwnd)

    return False, f"unknown_method:{method}"


class WindowCapturer:
    """Fast MSS-backed screen region capturer returning BGR numpy arrays."""

    def __init__(self) -> None:
        set_dpi_aware()
        self._sct = mss.mss()

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass

    def grab_rect(self, rect: dict[str, int]) -> np.ndarray:
        """Capture a screen bounding dict as BGR uint8 ndarray."""
        raw = self._sct.grab(rect)
        arr = np.asarray(raw, dtype=np.uint8)
        return arr[:, :, :3].copy()

    def grab_rois(
        self,
        anchor: WindowRect,
        rois: dict[str, tuple[float, float, float, float]],
    ) -> dict[str, np.ndarray]:
        """Single atomic OS capture over tight union of ROIs; copies only each cropped ROI."""
        if not rois:
            return {}
        resolved = {name: resolve_roi(anchor, norm_roi) for name, norm_roi in rois.items()}
        u_left = min(r["left"] for r in resolved.values())
        u_top = min(r["top"] for r in resolved.values())
        u_right = max(r["left"] + r["width"] for r in resolved.values())
        u_bottom = max(r["top"] + r["height"] for r in resolved.values())
        raw = self._sct.grab(
            {
                "left": u_left,
                "top": u_top,
                "width": max(4, u_right - u_left),
                "height": max(4, u_bottom - u_top),
            }
        )
        arr = np.asarray(raw, dtype=np.uint8)
        if arr.size == 0:
            return {}
        ah, aw = arr.shape[:2]
        out: dict[str, np.ndarray] = {}
        for name, r in resolved.items():
            x0 = max(0, min(aw - 1, r["left"] - u_left))
            y0 = max(0, min(ah - 1, r["top"] - u_top))
            x1 = max(x0 + 1, min(aw, x0 + r["width"]))
            y1 = max(y0 + 1, min(ah, y0 + r["height"]))
            out[name] = arr[y0:y1, x0:x1, :3].copy()
        return out

    def grab_window(self, win: WindowRect) -> np.ndarray:
        """Capture the full target window client area."""
        return self.grab_rect({"left": win.left, "top": win.top, "width": win.width, "height": win.height})

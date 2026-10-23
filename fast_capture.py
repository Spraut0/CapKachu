# -*- coding: utf-8 -*-
"""
CapKachu — Fast region capture for Windows
- Alt+Shift+S: instant region capture -> clipboard + toast
- Click toast -> Editor (pen / highlighter / text / save / copy / open in Paint)
- System tray resident, Run-on-Startup option

Design:
- pystray: main thread (Win32 systray)
- HotkeyListener: dedicated thread (Win32 message loop)
- GUI: one dedicated thread + single hidden Tk root, all windows are Toplevel
  -> snappy hotkey response, no PhotoImage cross-Tk conflicts, safe multi-window

"""

from __future__ import annotations

import sys
import os
import io
import time
import ctypes
import ctypes.wintypes as wintypes
import threading
import winreg
import subprocess
import tempfile
import tkinter as tk
from tkinter import filedialog
from dataclasses import dataclass
from typing import Optional, Callable

from PIL import Image, ImageDraw, ImageTk, ImageEnhance, ImageGrab, ImageFont
import pystray
from pystray import MenuItem as item


# ============================================================
# 설정
# ============================================================
APP_NAME = "CapKachu"
APP_TITLE = "CapKachu"
APP_TOOLTIP = "CapKachu — Alt+Shift+S"
HOTKEY_ID = 1
MOD_ALT = 0x0001
MOD_SHIFT = 0x0004
VK_S = 0x53
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

PEN_WIDTH = 3
HIGHLIGHTER_WIDTH = 18
HIGHLIGHTER_ALPHA = 95
TEXT_SIZE = 16
TEXT_FONT_FAMILY = "맑은 고딕"
TEXT_FONT_WEIGHT = "bold"
TEXT_FONT_PIL_CANDIDATES = ("malgunbd.ttf", "malgun.ttf", "segoeuib.ttf", "segoeui.ttf", "arial.ttf")
TEXT_INPUT_BG = "#FFFFE0"  # 입력 중 살짝 노란 배경

COLORS = [
    "#E53935",  # 빨강
    "#FDD835",  # 노랑
    "#43A047",  # 초록
    "#1E88E5",  # 파랑
    "#212121",  # 검정
]
DEFAULT_COLOR = "#E53935"

# 액션 버튼 색상
COLOR_COPY_BG = "#0078D4"   # 파랑 (메인 액션)
COLOR_COPY_HOVER = "#106EBE"
COLOR_SAVE_BG = "#107C10"   # 초록
COLOR_SAVE_HOVER = "#0B5A0B"
COLOR_EXPORT_BG = "#5C2D91" # 보라
COLOR_EXPORT_HOVER = "#4A2375"
COLOR_NEUTRAL_BG = "#F3F2F1"
COLOR_NEUTRAL_HOVER = "#E1DFDD"

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

CF_DIB = 8
GMEM_MOVEABLE = 0x0002

GA_ROOT = 2

# Single-instance mutex
MUTEX_NAME = "CapKachu_SingleInstance_Mutex_v1"
ERROR_ALREADY_EXISTS = 183

# DPI 스케일링 보정 — 125%/150% 배율에서 좌표 틀어지지 않게
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ============================================================
# Win32 ctypes 시그니처
# ============================================================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
kernel32.GlobalFree.restype = wintypes.HANDLE
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
user32.GetMonitorInfoW.restype = wintypes.BOOL

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND

user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


MONITOR_DEFAULTTONEAREST = 0x00000002

# WM_SETICON / LoadImage 상수
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040


# ============================================================
# 리소스 경로
# ============================================================
def resource_path(name: str) -> str:
    """frozen exe든 .py든 동작하게 리소스 경로 해석"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p = os.path.join(meipass, name)
            if os.path.exists(p):
                return p
        return os.path.join(os.path.dirname(sys.executable), name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _get_hwnd(win) -> int:
    """tkinter 창의 진짜 root win32 HWND 얻기 — GA_ROOT가 가장 신뢰 가능"""
    try:
        win.update_idletasks()
        wid = win.winfo_id()
        # GetAncestor with GA_ROOT — 진짜 toplevel 반환
        root = user32.GetAncestor(wid, GA_ROOT)
        if root:
            return root
        parent = user32.GetParent(wid)
        return parent if parent else wid
    except Exception:
        return 0


def apply_window_icon(win) -> None:
    """창에 icon.ico 적용 — iconbitmap + WM_SETICON으로 작업표시줄까지 강제"""
    icon_path = resource_path("icon.ico")
    if not os.path.exists(icon_path):
        return
    try:
        win.iconbitmap(icon_path)
    except tk.TclError:
        pass
    # 작업표시줄/Alt-Tab 아이콘 강제 (WM_SETICON)
    try:
        win.update_idletasks()
        hwnd = _get_hwnd(win)
        if not hwnd:
            return
        hicon_small = user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
        )
        hicon_big = user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
        )
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
    except Exception:
        pass


# ============================================================
# 클립보드 (ctypes 전용)
# ============================================================
def copy_image_to_clipboard(pil_image: Image.Image) -> bool:
    output = io.BytesIO()
    pil_image.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]

    size = len(data)
    h_data = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not h_data:
        return False
    p_data = kernel32.GlobalLock(h_data)
    if not p_data:
        kernel32.GlobalFree(h_data)
        return False
    ctypes.memmove(p_data, data, size)
    kernel32.GlobalUnlock(h_data)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(h_data)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, h_data):
            kernel32.GlobalFree(h_data)
            return False
        return True
    finally:
        user32.CloseClipboard()


# ============================================================
# 자동시작
# ============================================================
def get_exe_path() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(__file__)


def is_auto_start_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_auto_start(enable: bool) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{get_exe_path()}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass
    except OSError:
        pass


# ============================================================
# 가상 화면
# ============================================================
def get_virtual_screen_bounds() -> tuple[int, int, int, int]:
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, w, h


def grab_all_screens() -> Image.Image:
    try:
        return ImageGrab.grab(all_screens=True)
    except TypeError:
        return ImageGrab.grab()


def get_monitor_at_cursor() -> tuple[int, int, int, int]:
    """마우스 커서가 있는 모니터의 (x, y, w, h) — 화면 좌표계"""
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return get_virtual_screen_bounds()
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return get_virtual_screen_bounds()
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return get_virtual_screen_bounds()
    r = mi.rcMonitor
    return r.left, r.top, r.right - r.left, r.bottom - r.top


# ============================================================
# GUI 스레드 — 단일 Tk root, 모든 창은 Toplevel
# ============================================================
class GuiThread:
    """전용 스레드에서 Tk root mainloop 실행. schedule()로 GUI 작업 dispatch."""

    def __init__(self) -> None:
        self.root: Optional[tk.Tk] = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="GuiThread").start()
        self._ready.wait(timeout=10)

    def _run(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        apply_window_icon(self.root)
        self._ready.set()
        self.root.mainloop()

    def schedule(self, fn: Callable, *args, **kwargs) -> None:
        if self.root is None:
            return
        self.root.after(0, lambda: fn(*args, **kwargs))

    def stop(self) -> None:
        if self.root is not None:
            try:
                self.root.after(0, self.root.quit)
            except Exception:
                pass


GUI: Optional[GuiThread] = None


# ============================================================
# 캡쳐 오버레이 — Toplevel
# ============================================================
class CaptureOverlay:
    """Win+Shift+S 클론. on_complete(Image|None)로 결과 반환."""

    def __init__(self, parent: tk.Tk, on_complete: Callable[[Optional[Image.Image]], None]):
        self.on_complete = on_complete
        self._called = False
        self.mode = "rect"

        self.vx, self.vy, self.vw, self.vh = get_virtual_screen_bounds()
        self.screenshot = grab_all_screens()
        self.dimmed = ImageEnhance.Brightness(self.screenshot).enhance(0.4)

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.geometry(f"{self.vw}x{self.vh}+{self.vx}+{self.vy}")
        self.win.attributes("-topmost", True)
        self.win.configure(bg="black", cursor="crosshair")

        self.dim_photo = ImageTk.PhotoImage(self.dimmed, master=self.win)
        self.canvas = tk.Canvas(
            self.win, width=self.vw, height=self.vh,
            highlightthickness=0, bg="black", borderwidth=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.dim_photo)

        self.start_x = 0
        self.start_y = 0
        self.selection_id: Optional[int] = None
        self.bright_id: Optional[int] = None
        self.bright_photo: Optional[ImageTk.PhotoImage] = None
        self.coord_id: Optional[int] = None
        self.free_points: list[tuple[int, int]] = []
        self.window_rect: Optional[tuple[int, int, int, int]] = None
        self._overlay_hwnd: Optional[int] = None
        self._dragging_active = False
        self.tb_top: Optional[tk.Toplevel] = None
        # 모드 툴바 제거됨 — 사각형 드래그 캡쳐만 사용 (ESC로 취소)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.win.bind("<Escape>", lambda e: self._finish(None))

        self.win.focus_force()
        self.win.grab_set()

    # ---------- 공통 종료 ----------
    def _finish(self, img: Optional[Image.Image]) -> None:
        if self._called:
            return
        self._called = True
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        if self.tb_top is not None:
            try:
                self.tb_top.destroy()
            except tk.TclError:
                pass
            self.tb_top = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        try:
            self.on_complete(img)
        except Exception:
            pass

    # ---------- 툴바 (별도 Toplevel — 캔버스 redraw에 영향 없음) ----------
    def _build_toolbar(self) -> None:
        self.tb_top = tk.Toplevel(self.win)
        self.tb_top.overrideredirect(True)
        self.tb_top.attributes("-topmost", True)
        self.tb_top.configure(bg="#2A2A2A")

        tb = tk.Frame(self.tb_top, bg="#2A2A2A", padx=4, pady=4, bd=0)
        tb.pack(fill=tk.BOTH, expand=True)

        modes = [
            ("□", "rect"),
            ("◇", "free"),
            ("◰", "window"),
            ("■", "full"),
            ("✕", "close"),
        ]
        self.mode_btns: dict[str, tk.Button] = {}
        for sym, m in modes:
            btn = tk.Button(
                tb, text=sym, width=3,
                bg="#0078D4" if m == self.mode else "#2A2A2A",
                fg="white", relief=tk.FLAT,
                font=("Segoe UI", 12, "bold"),
                activebackground="#106EBE", activeforeground="white",
                bd=0, highlightthickness=0,
                command=lambda mode=m: self._set_mode(mode),
            )
            btn.pack(side=tk.LEFT, padx=2)
            self.mode_btns[m] = btn

        # 첫 위치 = 마우스 있는 모니터 상단
        self._update_toolbar_position()

    def _update_toolbar_position(self) -> None:
        """마우스 있는 모니터로 툴바 위치 이동. 모니터 안 바뀌면 no-op (깜빡임 방지)."""
        if self.tb_top is None:
            return
        mx, my, mw, mh = get_monitor_at_cursor()
        key = (mx, my, mw, mh)
        if key == self._current_toolbar_monitor:
            return
        self._current_toolbar_monitor = key
        x = mx + (mw - self._toolbar_w) // 2
        y = my + 12
        try:
            self.tb_top.geometry(f"{self._toolbar_w}x{self._toolbar_h}+{x}+{y}")
        except tk.TclError:
            pass

    def _set_mode(self, mode: str) -> None:
        if mode == "close":
            self._finish(None)
            return
        if mode == "full":
            self._finish(self.screenshot)
            return
        self.mode = mode
        self.window_rect = None
        self._clear_selection()
        for m, btn in self.mode_btns.items():
            if m in ("close",):
                continue
            btn.configure(bg="#0078D4" if m == mode else "#2A2A2A")

    # ---------- 마우스 ----------
    def _on_motion(self, event) -> None:
        # 드래그 중에는 툴바 위치 변경 안 함 (안정감)
        if not self._dragging_active:
            self._update_toolbar_position()
        if self.mode == "window":
            self._update_window_hover(event.x, event.y)

    def _on_press(self, event) -> None:
        if self.mode == "window":
            return
        self._dragging_active = True
        self.start_x = event.x
        self.start_y = event.y
        self.free_points = [(event.x, event.y)] if self.mode == "free" else []
        self._clear_selection()

    def _on_drag(self, event) -> None:
        if self.mode == "rect":
            self._draw_rect_selection(self.start_x, self.start_y, event.x, event.y)
        elif self.mode == "free":
            self.free_points.append((event.x, event.y))
            self._draw_free_selection()

    def _on_release(self, event) -> None:
        self._dragging_active = False
        if self.mode == "rect":
            x1, y1 = min(self.start_x, event.x), min(self.start_y, event.y)
            x2, y2 = max(self.start_x, event.x), max(self.start_y, event.y)
            if (x2 - x1) > 5 and (y2 - y1) > 5:
                self._finish(self.screenshot.crop((x1, y1, x2, y2)))
        elif self.mode == "free":
            if len(self.free_points) > 3:
                img = self._crop_freeform(self.free_points)
                if img is not None:
                    self._finish(img)
        elif self.mode == "window":
            if self.window_rect is not None:
                x1, y1, x2, y2 = self.window_rect
                x1 = max(0, x1); y1 = max(0, y1)
                x2 = min(self.vw, x2); y2 = min(self.vh, y2)
                if (x2 - x1) > 5 and (y2 - y1) > 5:
                    self._finish(self.screenshot.crop((x1, y1, x2, y2)))

    # ---------- 선택 표시 ----------
    def _clear_selection(self) -> None:
        for attr in ("selection_id", "bright_id", "coord_id"):
            cid = getattr(self, attr)
            if cid is not None:
                try:
                    self.canvas.delete(cid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        self.bright_photo = None

    def _draw_rect_selection(self, sx: int, sy: int, ex: int, ey: int) -> None:
        x1, y1 = min(sx, ex), min(sy, ey)
        x2, y2 = max(sx, ex), max(sy, ey)
        self._clear_selection()

        if (x2 - x1) > 2 and (y2 - y1) > 2:
            cropped = self.screenshot.crop((x1, y1, x2, y2))
            self.bright_photo = ImageTk.PhotoImage(cropped, master=self.win)
            self.bright_id = self.canvas.create_image(
                x1, y1, anchor=tk.NW, image=self.bright_photo
            )
        self.selection_id = self.canvas.create_rectangle(
            x1, y1, x2, y2, outline="white", width=2
        )
        cw, ch = x2 - x1, y2 - y1
        label_x = min(x2 + 8, self.vw - 80)
        label_y = min(y2 + 8, self.vh - 24)
        self.coord_id = self.canvas.create_text(
            label_x, label_y, text=f"{cw} × {ch}", fill="white",
            font=("Segoe UI", 10, "bold"), anchor=tk.NW,
        )

    def _draw_free_selection(self) -> None:
        if self.selection_id is not None:
            try:
                self.canvas.delete(self.selection_id)
            except tk.TclError:
                pass
            self.selection_id = None
        if len(self.free_points) >= 2:
            flat = [c for p in self.free_points for c in p]
            self.selection_id = self.canvas.create_line(
                flat, fill="white", width=2, smooth=True
            )

    def _get_overlay_hwnd(self) -> int:
        if self._overlay_hwnd is not None:
            return self._overlay_hwnd
        self._overlay_hwnd = _get_hwnd(self.win)
        return self._overlay_hwnd

    def _find_window_at(self, sx: int, sy: int) -> Optional[tuple[int, int, int, int]]:
        """우리 오버레이 제외하고 (sx, sy)를 포함하는 가장 위 가시 창의 rect"""
        overlay_hwnd = self._get_overlay_hwnd()
        found: list[Optional[tuple[int, int, int, int]]] = [None]

        def cb(hwnd, _lp):
            if hwnd == overlay_hwnd:
                return True
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.IsIconic(hwnd):
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 5 or h < 5:
                return True
            # 가상 화면 전체 덮는 창(데스크탑/시작화면 등)은 제외
            if w >= self.vw and h >= self.vh:
                return True
            if rect.left <= sx <= rect.right and rect.top <= sy <= rect.bottom:
                found[0] = (rect.left, rect.top, rect.right, rect.bottom)
                return False  # z-order 위에서부터 첫 발견 → 중단
            return True

        try:
            user32.EnumWindows(EnumWindowsProc(cb), 0)
        except Exception:
            return None
        return found[0]

    def _update_window_hover(self, mx: int, my: int) -> None:
        sx = mx + self.vx
        sy = my + self.vy
        rect = self._find_window_at(sx, sy)
        if rect is None:
            return
        sl, st, sr, sb = rect
        x1 = sl - self.vx
        y1 = st - self.vy
        x2 = sr - self.vx
        y2 = sb - self.vy
        self.window_rect = (x1, y1, x2, y2)
        self._draw_rect_selection(x1, y1, x2, y2)

    def _crop_freeform(self, points: list[tuple[int, int]]) -> Optional[Image.Image]:
        if len(points) < 3:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        if (x2 - x1) < 5 or (y2 - y1) < 5:
            return None
        mask = Image.new("L", self.screenshot.size, 0)
        ImageDraw.Draw(mask).polygon(points, fill=255)
        result = Image.new("RGBA", self.screenshot.size, (0, 0, 0, 0))
        result.paste(self.screenshot, (0, 0), mask)
        return result.crop((x1, y1, x2, y2))


# ============================================================
# 토스트
# ============================================================
def show_toast(parent: tk.Tk, image: Image.Image, on_click: Callable[[], None]) -> None:
    toast = tk.Toplevel(parent)
    toast.overrideredirect(True)
    toast.attributes("-topmost", True)
    toast.attributes("-alpha", 0.96)
    toast.configure(bg="#252525")

    sw = toast.winfo_screenwidth()
    sh = toast.winfo_screenheight()
    tw, th = 320, 84
    x = sw - tw - 24
    y = sh - th - 80
    toast.geometry(f"{tw}x{th}+{x}+{y}")

    thumb = image.copy()
    thumb.thumbnail((64, 64), Image.LANCZOS)
    photo = ImageTk.PhotoImage(thumb, master=toast)

    frame = tk.Frame(toast, bg="#252525")
    frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    lbl_img = tk.Label(frame, image=photo, bg="#252525", cursor="hand2", bd=0)
    lbl_img.image = photo
    lbl_img.pack(side=tk.LEFT)

    lbl_txt = tk.Label(
        frame, text="Capture ready\nClick to edit", bg="#252525", fg="white",
        font=("Segoe UI", 10), cursor="hand2", justify=tk.LEFT,
    )
    lbl_txt.pack(side=tk.LEFT, padx=12)

    clicked = {"v": False}

    def handle_click(_e=None):
        if clicked["v"]:
            return
        clicked["v"] = True
        try:
            toast.destroy()
        except tk.TclError:
            pass
        try:
            on_click()
        except Exception:
            pass

    for w in (toast, frame, lbl_img, lbl_txt):
        w.bind("<Button-1>", handle_click)

    def auto_close():
        if clicked["v"]:
            return
        try:
            if toast.winfo_exists():
                toast.destroy()
        except tk.TclError:
            pass

    toast.after(4500, auto_close)


# ============================================================
# 에디터
# ============================================================
@dataclass
class Stroke:
    points: list[tuple[int, int]]
    color: str
    width: int
    is_highlighter: bool = False


@dataclass
class TextNote:
    x: int
    y: int
    text: str
    color: str
    size: int = TEXT_SIZE


class ThinSlider(tk.Canvas):
    """가는 트랙 + 검정 작대기 thumb. 배경 투명. ttk.Scale 호환 인터페이스."""

    def __init__(self, master, *, from_=1, to=20, length=150,
                 height=18, bg="#FFFFFF",
                 track_color="#9A9A9A", thumb_color="#222222",
                 command=None):
        super().__init__(
            master, width=length, height=height, bg=bg,
            highlightthickness=0, bd=0,
        )
        self._from = float(from_)
        self._to = float(to)
        self._length = length
        self._height = height
        self._track_color = track_color
        self._thumb_color = thumb_color
        self._command = command
        self._value = float(from_)
        self._enabled = True
        self._padding = 6
        self._track_y = height // 2

        self._track_id = self.create_line(
            self._padding, self._track_y,
            length - self._padding, self._track_y,
            fill=track_color, width=1,
        )
        self._thumb_id = self.create_rectangle(0, 0, 0, 0, fill=thumb_color, outline="")
        self._update_thumb()

        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_press)

    def _value_to_x(self, value):
        span = self._to - self._from
        if span == 0:
            return self._padding
        ratio = (value - self._from) / span
        ratio = max(0.0, min(1.0, ratio))
        return self._padding + ratio * (self._length - 2 * self._padding)

    def _x_to_value(self, x):
        span = self._length - 2 * self._padding
        if span <= 0:
            return self._from
        ratio = (x - self._padding) / span
        ratio = max(0.0, min(1.0, ratio))
        return self._from + ratio * (self._to - self._from)

    def _update_thumb(self):
        x = self._value_to_x(self._value)
        w, h = 3, self._height - 4  # 가는 검정 작대기
        self.coords(
            self._thumb_id,
            x - w / 2, self._track_y - h / 2,
            x + w / 2, self._track_y + h / 2,
        )

    def _on_press(self, event):
        if not self._enabled:
            return
        self._value = self._x_to_value(event.x)
        self._update_thumb()
        if self._command:
            self._command(self._value)

    # ttk.Scale 호환 API
    def set(self, value):
        self._value = max(self._from, min(self._to, float(value)))
        self._update_thumb()

    def get(self):
        return self._value

    def configure(self, **kw):
        if "from_" in kw:
            self._from = float(kw.pop("from_"))
        if "to" in kw:
            self._to = float(kw.pop("to"))
        if "state" in kw:
            state = kw.pop("state")
            self._enabled = (state == "normal")
            self.itemconfig(
                self._thumb_id,
                fill=self._thumb_color if self._enabled else "#C0C0C0",
            )
            self.itemconfig(
                self._track_id,
                fill=self._track_color if self._enabled else "#D8D8D8",
            )
        if kw:
            super().configure(**kw)
        self._update_thumb()

    config = configure


class HoverButton(tk.Button):
    """배경/호버 색상 분리된 평면 버튼. outline=True면 1px 검정 테두리"""

    def __init__(self, master, *, bg, hover_bg, fg="white", outline=False, **kw):
        kw.setdefault("relief", tk.FLAT)
        kw.setdefault("bd", 0)
        if outline:
            kw["highlightthickness"] = 1
            kw["highlightbackground"] = "#1A1A1A"
            kw["highlightcolor"] = "#1A1A1A"
        else:
            kw.setdefault("highlightthickness", 0)
        kw.setdefault("activebackground", hover_bg)
        kw.setdefault("activeforeground", fg)
        super().__init__(master, bg=bg, fg=fg, **kw)
        self._bg = bg
        self._hover_bg = hover_bg
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover_bg))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))


class EditorWindow:
    """미리보기 + 펜/형광펜/텍스트/저장/복사/내보내기 (Toplevel)"""

    def __init__(self, parent: tk.Tk, image: Image.Image):
        self.image = image.copy().convert("RGBA")
        self.tool = "pen"
        self.color = DEFAULT_COLOR
        self.actions: list = []
        self.redo_stack: list = []
        self.current_stroke: Optional[Stroke] = None
        self.saved_path: Optional[str] = None
        # 도구별 굵기 (슬라이더로 조절)
        self.pen_width = PEN_WIDTH
        self.highlighter_width = HIGHLIGHTER_WIDTH
        # 텍스트 노트 개체화
        self._active_text_input: Optional[int] = None
        self._text_note_items: dict = {}  # canvas item_id -> TextNote
        self._dragging_note: Optional[TextNote] = None
        self._note_drag_offset: tuple[int, int] = (0, 0)

        self.win = tk.Toplevel(parent)
        self.win.title(f"{APP_TITLE} — Editor")
        self.win.configure(bg="#FAFAFA")
        apply_window_icon(self.win)

        self._build_toolbar()
        self._build_canvas()

        self.win.bind_all("<Control-z>", lambda e: self.undo())
        self.win.bind_all("<Control-Z>", lambda e: self.undo())
        self.win.bind_all("<Control-y>", lambda e: self.redo())
        self.win.bind_all("<Control-Y>", lambda e: self.redo())
        self.win.bind("<Control-c>", lambda e: self.copy_to_clipboard())
        self.win.bind("<Control-C>", lambda e: self.copy_to_clipboard())
        self.win.bind("<Control-s>", lambda e: self.save())
        self.win.bind("<Control-S>", lambda e: self.save())

        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        # toolbar가 캔버스보다 넓으면 창 width를 toolbar 기준으로 보장
        toolbar_w = self.toolbar_frame.winfo_reqwidth() + 24
        canvas_w = self.disp_image.width + 40
        min_w = max(toolbar_w, canvas_w)
        ww = max(self.win.winfo_reqwidth(), min_w)
        wh = self.win.winfo_reqheight()
        self.win.minsize(min_w, wh)
        self.win.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 2}")
        self.win.focus_force()
        self.win.lift()

    # ---------- 툴바 ----------
    def _build_toolbar(self) -> None:
        tb = tk.Frame(self.win, bg="#FFFFFF", pady=10, padx=12)
        tb.pack(fill=tk.X)
        self.toolbar_frame = tb

        # 공통 버튼 옵션 — 모든 버튼 동일 높이/패딩
        BTN_FONT = ("Segoe UI", 10)
        BTN_PADX = 14
        BTN_PADY = 7

        # === Tools ===
        self.tool_btns: dict[str, HoverButton] = {}
        for tool, label in [("pen", "✎ Pen"), ("highlighter", "▮ Highlighter"), ("text", "T Text")]:
            b = HoverButton(
                tb, text=label,
                bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER,
                fg="#222222", font=BTN_FONT,
                padx=BTN_PADX, pady=BTN_PADY,
                command=lambda t=tool: self._set_tool(t),
            )
            b.pack(side=tk.LEFT, padx=2)
            self.tool_btns[tool] = b

        tk.Frame(tb, width=1, bg="#D0D0D0").pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=4)

        # === 색상 + 굵기 슬라이더 (세로 스택, 좌우 정렬) ===
        # 색상 5개 Canvas — 선택 표시는 안쪽에 그려서 옆 친구랑 안 씹힘
        SWATCH = 26
        SWATCH_PADX = 2
        SLIDER_LEN = 5 * (SWATCH + 2 * SWATCH_PADX)  # 5 × 30 = 150

        color_block = tk.Frame(tb, bg="#FFFFFF")
        color_block.pack(side=tk.LEFT, padx=2)

        color_row = tk.Frame(color_block, bg="#FFFFFF")
        color_row.pack(side=tk.TOP)
        self.color_btns: dict[str, dict] = {}
        for hexv in COLORS:
            wrap = tk.Frame(color_row, bg="#FFFFFF", padx=SWATCH_PADX, pady=0, bd=0)
            wrap.pack(side=tk.LEFT)
            sw = tk.Canvas(
                wrap, width=SWATCH, height=SWATCH,
                highlightthickness=0, bd=0, bg="#FFFFFF", cursor="hand2",
            )
            sw.pack()
            # 색상 채우기 (전체)
            sw.create_rectangle(0, 0, SWATCH, SWATCH, fill=hexv, outline="")
            # 기본 테두리 (얇은 회색 — 항상 보임)
            sw.create_rectangle(0, 0, SWATCH - 1, SWATCH - 1, outline="#999999", width=1)
            # 선택 표시: 안쪽 흰색 분리선 + 어두운 외곽 (안쪽으로만 그려져서 폭 안 늘어남)
            sel_inner = sw.create_rectangle(
                2, 2, SWATCH - 3, SWATCH - 3,
                outline="#FFFFFF", width=1, state="hidden",
            )
            sel_outer = sw.create_rectangle(
                0, 0, SWATCH - 1, SWATCH - 1,
                outline="#1A1A1A", width=2, state="hidden",
            )
            sw.bind("<Button-1>", lambda e, c=hexv: self._set_color(c))
            self.color_btns[hexv] = {"sw": sw, "sel_inner": sel_inner, "sel_outer": sel_outer}

        # 얇은 커스텀 슬라이더 — 트랙 + 검정 작대기만
        self.thickness_slider = ThinSlider(
            color_block, from_=1, to=20, length=SLIDER_LEN,
            bg="#FFFFFF",
            command=self._on_thickness_change,
        )
        self.thickness_slider.pack(side=tk.TOP, pady=(4, 2))

        tk.Frame(tb, width=1, bg="#D0D0D0").pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=4)

        # === Undo / Redo ===
        for sym, fn in [("↶", self.undo), ("↷", self.redo)]:
            b = HoverButton(
                tb, text=sym,
                bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER,
                fg="#222222", font=("Segoe UI", 12, "bold"),
                padx=BTN_PADX - 4, pady=BTN_PADY - 1,
                command=fn,
            )
            b.pack(side=tk.LEFT, padx=2)

        # === Right-side actions ===
        # Copy — primary action (blue, stands out)
        copy_btn = HoverButton(
            tb, text="📋  Copy",
            bg=COLOR_COPY_BG, hover_bg=COLOR_COPY_HOVER, fg="white",
            font=("Segoe UI", 10, "bold"),
            padx=BTN_PADX, pady=BTN_PADY,
            command=self.copy_to_clipboard,
        )
        copy_btn.pack(side=tk.RIGHT, padx=2)

        # Open in Paint — neutral grey (matches Pen/Highlighter)
        paint_btn = HoverButton(
            tb, text="🎨  Open in Paint",
            bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER, fg="#222222",
            font=BTN_FONT,
            padx=BTN_PADX, pady=BTN_PADY,
            command=self.export,
        )
        paint_btn.pack(side=tk.RIGHT, padx=2)

        # Save — neutral grey
        save_btn = HoverButton(
            tb, text="💾  Save",
            bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER, fg="#222222",
            font=BTN_FONT,
            padx=BTN_PADX, pady=BTN_PADY,
            command=self.save,
        )
        save_btn.pack(side=tk.RIGHT, padx=2)

        self._set_tool("pen")
        self._set_color(DEFAULT_COLOR)

    def _set_tool(self, t: str) -> None:
        self.tool = t
        for tt, btn in self.tool_btns.items():
            active = (tt == t)
            btn._bg = "#D0E7FF" if active else COLOR_NEUTRAL_BG
            btn._hover_bg = "#B8D8F8" if active else COLOR_NEUTRAL_HOVER
            btn.configure(bg=btn._bg)
        if hasattr(self, "canvas"):
            self.canvas.configure(cursor="xterm" if t == "text" else "pencil")
        # 굵기 슬라이더 = 도구별 범위/값 동기화 (ttk.Scale은 string state)
        if hasattr(self, "thickness_slider"):
            if t == "pen":
                self.thickness_slider.configure(state="normal", from_=1, to=20)
                self.thickness_slider.set(self.pen_width)
            elif t == "highlighter":
                self.thickness_slider.configure(state="normal", from_=5, to=50)
                self.thickness_slider.set(self.highlighter_width)
            else:  # text
                self.thickness_slider.configure(state="disabled")

    def _on_thickness_change(self, value) -> None:
        try:
            val = int(float(value))
        except (TypeError, ValueError):
            return
        if self.tool == "pen":
            self.pen_width = val
        elif self.tool == "highlighter":
            self.highlighter_width = val

    def _set_color(self, c: str) -> None:
        self.color = c
        for cc, info in self.color_btns.items():
            active = (cc == c)
            sw = info["sw"]
            sw.itemconfig(info["sel_inner"], state="normal" if active else "hidden")
            sw.itemconfig(info["sel_outer"], state="normal" if active else "hidden")

    # ---------- 캔버스 ----------
    def _build_canvas(self) -> None:
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        max_w = sw - 100
        max_h = sh - 220

        if self.image.width > max_w or self.image.height > max_h:
            ratio = min(max_w / self.image.width, max_h / self.image.height)
            new_w = int(self.image.width * ratio)
            new_h = int(self.image.height * ratio)
            self.disp_image = self.image.resize((new_w, new_h), Image.LANCZOS)
            self.scale = ratio
        else:
            self.disp_image = self.image
            self.scale = 1.0

        # 디스플레이용 — 회색 배경 위에 알파 합성 (자유형 모양 보존)
        display_bg = Image.new("RGB", self.disp_image.size, (220, 220, 220))
        if self.disp_image.mode == "RGBA":
            display_bg.paste(self.disp_image, mask=self.disp_image.split()[3])
        else:
            display_bg.paste(self.disp_image)
        self.canvas_photo = ImageTk.PhotoImage(display_bg, master=self.win)
        self.canvas = tk.Canvas(
            self.win, width=self.disp_image.width, height=self.disp_image.height,
            highlightthickness=0, bg="#DCDCDC", cursor="pencil",
        )
        self.canvas.pack(padx=20, pady=14)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.canvas_photo)

        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

    def _on_canvas_press(self, event) -> None:
        # 텍스트 박스가 떠있으면 캔버스 다른 동작 막음 (Enter/Esc로만 빠져나감)
        if self._active_text_input is not None:
            return
        # 클릭 위치가 텍스트 노트 위면 노트 핸들러가 처리 → 캔버스 무시
        closest = self.canvas.find_closest(event.x, event.y)
        if closest:
            cid = closest[0]
            if cid in self._text_note_items:
                bbox = self.canvas.bbox(cid)
                if bbox and bbox[0] - 2 <= event.x <= bbox[2] + 2 \
                        and bbox[1] - 2 <= event.y <= bbox[3] + 2:
                    return
        if self.tool == "text":
            self._open_text_input(event.x, event.y)
            return
        is_hl = self.tool == "highlighter"
        width = self.highlighter_width if is_hl else self.pen_width
        self.current_stroke = Stroke(
            points=[(event.x, event.y)],
            color=self.color,
            width=width,
            is_highlighter=is_hl,
        )

    def _on_canvas_drag(self, event) -> None:
        if self.current_stroke is None:
            return
        prev = self.current_stroke.points[-1]
        self.current_stroke.points.append((event.x, event.y))
        kwargs: dict = {
            "fill": self.current_stroke.color,
            "width": self.current_stroke.width,
            "capstyle": tk.ROUND, "smooth": True,
        }
        if self.current_stroke.is_highlighter:
            kwargs["stipple"] = "gray50"
        self.canvas.create_line(prev[0], prev[1], event.x, event.y, **kwargs)

    def _on_canvas_release(self, _event) -> None:
        if self.current_stroke is None:
            return
        if len(self.current_stroke.points) >= 2:
            self.actions.append(self.current_stroke)
            self.redo_stack.clear()
        self.current_stroke = None

    def _open_text_input(self, x: int, y: int, initial_text: str = "") -> None:
        if self._active_text_input is not None:
            return  # 한 번에 하나만

        # 컨테이너 = 검정 외곽 + 헤더(드래그 핸들) + Text 입력
        holder = tk.Frame(self.canvas, bg="#1A1A1A", bd=0)
        header = tk.Frame(holder, bg="#1A1A1A", cursor="fleur")
        header.pack(side=tk.TOP, fill=tk.X)
        hint = tk.Label(
            header,
            text=" ⠿  Enter=Apply   Shift+Enter=Newline   Esc=Cancel ",
            bg="#1A1A1A", fg="#BBBBBB",
            font=("Segoe UI", 7), cursor="fleur",
        )
        hint.pack(fill=tk.X, padx=2)

        txt = tk.Text(
            holder,
            font=(TEXT_FONT_FAMILY, TEXT_SIZE, TEXT_FONT_WEIGHT),
            bd=0, relief=tk.FLAT,
            fg=self.color, bg=TEXT_INPUT_BG,
            height=1, width=18, wrap="word",
            padx=4, pady=2, highlightthickness=0,
            insertbackground=self.color,
        )
        txt.pack(fill=tk.BOTH, expand=True)
        if initial_text:
            txt.insert("1.0", initial_text)

        holder_id = self.canvas.create_window(x, y, anchor=tk.NW, window=holder)
        txt.focus_set()
        self._active_text_input = holder_id

        finalized = {"v": False}

        def adjust_height(_e=None):
            try:
                line_count = int(txt.index("end-1c").split(".")[0])
                txt.configure(height=max(1, line_count))
            except tk.TclError:
                pass

        if initial_text:
            adjust_height()

        def commit(_e=None):
            if finalized["v"]:
                return "break"
            finalized["v"] = True
            self._active_text_input = None
            try:
                cx, cy = self.canvas.coords(holder_id)
            except tk.TclError:
                cx, cy = x, y
            text = txt.get("1.0", "end-1c").rstrip()
            try:
                self.canvas.delete(holder_id)
            except tk.TclError:
                pass
            if text:
                # holder는 헤더 높이를 포함하므로 텍스트 위치는 헤더 높이만큼 아래
                header_h = header.winfo_height() if header.winfo_height() > 0 else 14
                note = TextNote(
                    x=int(cx), y=int(cy) + header_h,
                    text=text, color=self.color, size=TEXT_SIZE,
                )
                self.actions.append(note)
                self.redo_stack.clear()
                self._draw_text_note(note)
            return "break"

        def cancel(_e=None):
            if finalized["v"]:
                return "break"
            finalized["v"] = True
            self._active_text_input = None
            try:
                self.canvas.delete(holder_id)
            except tk.TclError:
                pass
            return "break"

        def newline(_e=None):
            txt.insert(tk.INSERT, "\n")
            adjust_height()
            return "break"

        # Enter=확정, Shift/Alt/Ctrl+Enter=줄바꿈, Esc=취소
        # FocusOut 바인딩 없음 → 다른 데 클릭해도 안 사라짐
        txt.bind("<Return>", commit)
        txt.bind("<Shift-Return>", newline)
        txt.bind("<Alt-Return>", newline)
        txt.bind("<Control-Return>", newline)
        txt.bind("<Escape>", cancel)
        txt.bind("<KeyRelease>", adjust_height)

        # 헤더 드래그 = 박스 이동
        drag = {"sx": 0, "sy": 0}

        def header_press(e):
            drag["sx"] = e.x_root
            drag["sy"] = e.y_root

        def header_drag(e):
            try:
                cx, cy = self.canvas.coords(holder_id)
            except tk.TclError:
                return
            dx = e.x_root - drag["sx"]
            dy = e.y_root - drag["sy"]
            drag["sx"] = e.x_root
            drag["sy"] = e.y_root
            self.canvas.coords(holder_id, cx + dx, cy + dy)

        def bind_drag(w):
            w.bind("<Button-1>", header_press)
            w.bind("<B1-Motion>", header_drag)

        bind_drag(header)
        bind_drag(hint)

    # ---------- 그리기 ----------
    def _draw_stroke(self, s: Stroke) -> None:
        if len(s.points) < 2:
            return
        flat = [c for p in s.points for c in p]
        kwargs: dict = {
            "fill": s.color, "width": s.width,
            "capstyle": tk.ROUND, "smooth": True,
        }
        if s.is_highlighter:
            kwargs["stipple"] = "gray50"
        self.canvas.create_line(flat, **kwargs)

    def _draw_text_note(self, note: TextNote) -> None:
        item_id = self.canvas.create_text(
            note.x, note.y, anchor=tk.NW, text=note.text,
            fill=note.color,
            font=(TEXT_FONT_FAMILY, note.size, TEXT_FONT_WEIGHT),
        )
        self._text_note_items[item_id] = note
        # 클릭/드래그/더블클릭 바인딩
        self.canvas.tag_bind(
            item_id, "<Button-1>",
            lambda e, n=note, i=item_id: self._on_note_press(n, i, e),
        )
        self.canvas.tag_bind(
            item_id, "<B1-Motion>",
            lambda e, n=note, i=item_id: self._on_note_drag(n, i, e),
        )
        self.canvas.tag_bind(
            item_id, "<ButtonRelease-1>",
            lambda e, n=note, i=item_id: self._on_note_release(n, i, e),
        )
        self.canvas.tag_bind(
            item_id, "<Double-Button-1>",
            lambda e, n=note, i=item_id: self._edit_text_note(n, i),
        )
        self.canvas.tag_bind(item_id, "<Enter>", lambda e: self.canvas.configure(cursor="fleur"))
        self.canvas.tag_bind(item_id, "<Leave>", lambda e: self._reset_cursor())

    def _reset_cursor(self) -> None:
        self.canvas.configure(cursor="xterm" if self.tool == "text" else "pencil")

    def _on_note_press(self, note: TextNote, item_id: int, event) -> None:
        if self._active_text_input is not None:
            return
        self._dragging_note = note
        self._note_drag_offset = (event.x - note.x, event.y - note.y)

    def _on_note_drag(self, note: TextNote, item_id: int, event) -> None:
        if self._dragging_note is not note:
            return
        nx = event.x - self._note_drag_offset[0]
        ny = event.y - self._note_drag_offset[1]
        note.x = nx
        note.y = ny
        try:
            self.canvas.coords(item_id, nx, ny)
        except tk.TclError:
            pass

    def _on_note_release(self, note: TextNote, item_id: int, event) -> None:
        self._dragging_note = None

    def _edit_text_note(self, note: TextNote, item_id: int) -> None:
        if self._active_text_input is not None:
            return
        # 기존 노트 액션 목록에서 제거 → 다시 그림 → 입력 박스 띄우기
        if note in self.actions:
            self.actions.remove(note)
        self._redraw_all()
        self._open_text_input(note.x, note.y, initial_text=note.text)

    def _redraw_all(self) -> None:
        self.canvas.delete("all")
        self._text_note_items.clear()
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.canvas_photo)
        for a in self.actions:
            if isinstance(a, Stroke):
                self._draw_stroke(a)
            elif isinstance(a, TextNote):
                self._draw_text_note(a)

    def undo(self) -> None:
        if not self.actions:
            return
        self.redo_stack.append(self.actions.pop())
        self._redraw_all()

    def redo(self) -> None:
        if not self.redo_stack:
            return
        self.actions.append(self.redo_stack.pop())
        self._redraw_all()

    # ---------- 출력 ----------
    @staticmethod
    def _hex_to_rgb(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    @staticmethod
    def _flatten(img: Image.Image, bg=(255, 255, 255)) -> Image.Image:
        """RGBA → RGB (자유형 외부 = 흰 배경 합성)"""
        if img.mode != "RGBA":
            return img.convert("RGB")
        flat = Image.new("RGB", img.size, bg)
        flat.paste(img, mask=img.split()[3])
        return flat

    def _render_to_image(self, alpha: bool = False) -> Image.Image:
        """alpha=True면 RGBA 그대로 (PNG 저장용 — 자유형 모양 유지)"""
        out = self.image.copy()
        if out.mode != "RGBA":
            out = out.convert("RGBA")
        inv = 1.0 / self.scale
        for a in self.actions:
            if isinstance(a, Stroke) and a.is_highlighter:
                self._render_stroke(out, a, inv, HIGHLIGHTER_ALPHA)
        for a in self.actions:
            if isinstance(a, Stroke) and not a.is_highlighter:
                self._render_stroke(out, a, inv, 255)
        for a in self.actions:
            if isinstance(a, TextNote):
                self._render_text(out, a, inv)
        if alpha:
            return out
        return self._flatten(out)

    def _render_stroke(self, img: Image.Image, s: Stroke, inv: float, alpha: int) -> None:
        if len(s.points) < 2:
            return
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        rgb = self._hex_to_rgb(s.color)
        scaled_pts = [(int(p[0] * inv), int(p[1] * inv)) for p in s.points]
        scaled_w = max(1, int(s.width * inv))
        odraw.line(scaled_pts, fill=rgb + (alpha,), width=scaled_w, joint="curve")
        r = scaled_w // 2
        for px, py in scaled_pts:
            odraw.ellipse([px - r, py - r, px + r, py + r], fill=rgb + (alpha,))
        img.alpha_composite(overlay)

    def _render_text(self, img: Image.Image, note: TextNote, inv: float) -> None:
        font_size = max(8, int(note.size * inv))
        font = None
        for fname in TEXT_FONT_PIL_CANDIDATES:
            try:
                font = ImageFont.truetype(fname, font_size)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
        rgb = self._hex_to_rgb(note.color)
        draw = ImageDraw.Draw(img)
        pos = (int(note.x * inv), int(note.y * inv))
        if "\n" in note.text:
            draw.multiline_text(pos, note.text, fill=rgb + (255,), font=font, spacing=4)
        else:
            draw.text(pos, note.text, fill=rgb + (255,), font=font)

    # ---------- 액션 ----------
    def copy_to_clipboard(self) -> None:
        img = self._render_to_image()
        if copy_image_to_clipboard(img):
            self._flash_status("📋 Copied to clipboard")
        else:
            self._flash_status("⚠ Copy failed")

    def save(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.win,
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("BMP", "*.bmp")],
            initialdir=os.path.expanduser("~/Pictures"),
            initialfile=f"capture_{int(time.time())}.png",
        )
        if not path:
            return
        is_png = path.lower().endswith(".png")
        img = self._render_to_image(alpha=is_png)
        try:
            img.save(path)
            self.saved_path = path
            self._flash_status(f"💾 Saved: {os.path.basename(path)}")
        except Exception as e:
            self._flash_status(f"⚠ Save failed: {e}")

    def export(self) -> None:
        """그림판(mspaint)으로 이미지 보내기 — 임시 PNG (자유형 모양 보존)"""
        img = self._render_to_image(alpha=True)
        tmp_path = os.path.join(
            tempfile.gettempdir(), f"fastcap_{int(time.time() * 1000)}.png"
        )
        try:
            img.save(tmp_path)
            subprocess.Popen(["mspaint", tmp_path])
            self._flash_status("🎨 Opened in Paint")
        except Exception as e:
            self._flash_status(f"⚠ Paint failed: {e}")

    def _flash_status(self, msg: str) -> None:
        original = f"{APP_TITLE} — Editor"
        self.win.title(f"{original}   ·   {msg}")
        self.win.after(2500, lambda: self.win.title(original))


# ============================================================
# 글로벌 핫키 — 별도 스레드, 콜백은 즉시 GUI 스레드로 dispatch
# ============================================================
class HotkeyListener:
    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self._running = True
        self._thread_id: Optional[int] = None
        self._registered = False

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT | MOD_SHIFT, VK_S):
            return
        self._registered = True

        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                # 콜백이 무거우면 메시지 루프 막힘 → 즉시 비차단 호출
                try:
                    self.callback()
                except Exception:
                    pass

        if self._registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        self._running = False
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# ============================================================
# 트레이 아이콘
# ============================================================
def create_tray_icon() -> Image.Image:
    """Prefer icon.ico from resources; fall back to code-drawn CapKachu icon."""
    icon_path = resource_path("icon.ico")
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path)
        except Exception:
            pass

    # Fallback: 64x64 simplified CapKachu (single seamless Pikachu-tail polygon)
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=10, fill=(18, 18, 18, 255))
    # Single 8-point polygon scaled to 64×64
    tail = [
        (28, 7), (44, 7), (34, 28), (50, 28),
        (37, 58), (21, 58), (31, 32), (15, 32),
    ]
    draw.polygon(tail, fill=(255, 203, 5, 255))
    return img


# ============================================================
# 캡쳐 트리거 — 핫키/트레이 공용
# ============================================================
_overlay_lock = threading.Lock()
_overlay_active = False


def trigger_capture() -> None:
    """오버레이 1개만 동시 허용. 에디터/토스트는 별개로 떠 있어도 새 캡쳐 OK."""
    global _overlay_active
    with _overlay_lock:
        if _overlay_active:
            return
        _overlay_active = True

    if GUI is None or GUI.root is None:
        with _overlay_lock:
            _overlay_active = False
        return

    def _make_overlay():
        def on_complete(img: Optional[Image.Image]) -> None:
            global _overlay_active
            with _overlay_lock:
                _overlay_active = False
            if img is None:
                return
            copy_image_to_clipboard(img)
            show_toast(GUI.root, img, lambda: GUI.schedule(_open_editor, img))

        try:
            CaptureOverlay(GUI.root, on_complete=on_complete)
        except Exception:
            global _overlay_active
            with _overlay_lock:
                _overlay_active = False

    GUI.schedule(_make_overlay)


def _open_editor(img: Image.Image) -> None:
    try:
        EditorWindow(GUI.root, img)
    except Exception:
        pass


# ============================================================
# 단일 인스턴스 락 (Named Mutex)
# ============================================================
_instance_lock_handle: Optional[int] = None


def acquire_single_instance_lock() -> bool:
    """Named mutex로 single-instance 보장. 이미 다른 인스턴스가 있으면 False 반환."""
    global _instance_lock_handle
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        # mutex 생성 실패 — fail open (실행 허용)
        return True
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _instance_lock_handle = handle  # 프로세스 살아있는 동안 유지 (GC 방지)
    return True


# ============================================================
# 메인
# ============================================================
def main() -> None:
    # 단일 인스턴스 보장 — 이미 트레이에 떠있으면 silent exit
    if not acquire_single_instance_lock():
        return

    # 자동시작 시 작업폴더가 System32로 잡히는 문제 방지
    try:
        if getattr(sys, "frozen", False):
            os.chdir(os.path.dirname(sys.executable))
    except Exception:
        pass

    # Set explicit AppUserModelID so taskbar groups under our own icon
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "CapKachu"
        )
    except Exception:
        pass

    global GUI
    GUI = GuiThread()

    hotkey = HotkeyListener(trigger_capture)
    hotkey_thread = threading.Thread(target=hotkey.run, daemon=True, name="HotkeyThread")
    hotkey_thread.start()

    def toggle_autostart(_icon, _mi) -> None:
        set_auto_start(not is_auto_start_enabled())

    def autostart_checked(_mi) -> bool:
        return is_auto_start_enabled()

    def on_capture_now(_icon, _mi) -> None:
        trigger_capture()

    def on_quit(icon, _mi) -> None:
        hotkey.stop()
        if GUI is not None:
            GUI.stop()
        icon.stop()

    icon = pystray.Icon(
        APP_NAME,
        create_tray_icon(),
        APP_TOOLTIP,
        menu=pystray.Menu(
            # Hidden default item: left-click (double-click) still triggers capture
            item("Capture", on_capture_now, default=True, visible=False),
            item("Run on Startup", toggle_autostart, checked=autostart_checked),
            pystray.Menu.SEPARATOR,
            item("Exit", on_quit),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()

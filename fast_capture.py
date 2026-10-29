# -*- coding: utf-8 -*-
"""
CapKachu v2 — Fast region capture + region screen recording for Windows
- Alt+Shift+S: instant region capture -> clipboard + toast
- Alt+Shift+R: region screen recording -> MP4 (H.264 via Windows Media Foundation)
- Alt+Shift+G: region screen recording -> GIF
- Click toast -> Editor (pen / highlighter / text / save / copy / open in Paint)
- System tray resident, Run-on-Startup option, Settings (capkachu.json next to exe)

Design:
- pystray: main thread (Win32 systray)
- HotkeyListener: dedicated thread (Win32 message loop)
- GUI: one dedicated thread + single hidden Tk root, all windows are Toplevel
  -> snappy hotkey response, no PhotoImage cross-Tk conflicts, safe multi-window

"""

from __future__ import annotations

import os
# numpy(OpenBLAS)가 CPU 스레드 수만큼 버퍼를 커밋함 (20스레드 PC에서 +600MB) → 녹화엔 BLAS 불필요
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
import sys
import io
import time
import ctypes
import ctypes.wintypes as wintypes
import threading
import winreg
import subprocess
import tempfile
import json
import shutil
import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
from dataclasses import dataclass
from typing import Optional, Callable

from PIL import Image, ImageDraw, ImageTk, ImageGrab, ImageFont
import pystray
from pystray import MenuItem as item


# ============================================================
# 설정
# ============================================================
APP_NAME = "CapKachu2"
APP_TITLE = "CapKachu"
APP_VERSION = "2.0.0"
APP_TOOLTIP = "CapKachu"
HOTKEY_ID = 1           # capture
HOTKEY_ID_RECORD = 2    # mp4
HOTKEY_ID_GIF = 3       # gif
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_S = 0x53
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# 녹화 기본값 (capkachu.json 으로 덮어씀)
SETTINGS_FILE = "capkachu2.json"
DEFAULT_SETTINGS = {
    "save_dir": "",            # "" → <Videos>\CapKachu
    "autostart_init": False,   # 첫 실행 때 Run on Startup 한 번만 켬 (끄면 다시 안 켬)
    "fps": 20,                 # mp4
    "gif_fps": 10,
    "max_minutes": 10,         # 자동 저장 종료
    "min_seconds": 1.0,        # 이보다 짧으면 버림
    "show_cursor": True,
    "hotkey_capture": "alt+shift+s",
    "hotkey_record": "alt+shift+r",
    "hotkey_gif": "alt+shift+g",
}
REC_BORDER_COLOR = "#E53935"
REC_BORDER_WIDTH = 3
REC_BORDER_GAP = 2         # 영역과 테두리 사이 (테두리가 영상에 안 찍히게)

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
MUTEX_NAME = "CapKachu2_SingleInstance_Mutex_v1"
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


user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.DrawIconEx.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
    ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT,
]
user32.DrawIconEx.restype = wintypes.BOOL

gdi32 = ctypes.windll.gdi32
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL

kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetShortPathNameW.restype = wintypes.DWORD


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP


class _CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP),
    ]


user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
user32.GetCursorInfo.restype = wintypes.BOOL
user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(_ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
CURSOR_SHOWING = 0x00000001
DI_NORMAL = 0x0003
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000


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


def pen_cursor_spec() -> str:
    """에디터 펜 커서. 자체 pencil.cur(끝=좌하단) 없으면 Tk 기본 pencil."""
    p = resource_path("pencil.cur")
    if os.path.exists(p):
        return "@" + p.replace("\\", "/")
    return "pencil"


def set_cursor_safe(widget, spec: str, fallback: str = "pencil") -> None:
    try:
        widget.configure(cursor=spec)
    except tk.TclError:
        try:
            widget.configure(cursor=fallback)
        except tk.TclError:
            pass


def get_videos_dir() -> str:
    """사용자 '동영상' 폴더 (KNOWNFOLDERID Videos). 실패 시 %USERPROFILE%\\Videos."""
    try:
        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]
        guid = _GUID(0x18989B1D, 0x99B5, 0x455B, (ctypes.c_ubyte * 8)(0x84, 0x1C, 0xAB, 0x7C, 0x74, 0xE4, 0xDD, 0xFC))
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0 and out.value:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Videos")


def short_path(path: str) -> str:
    """8.3 짧은 경로 (OpenCV MSMF가 non-ASCII 경로를 못 여는 경우 대비)."""
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = kernel32.GetShortPathNameW(path, buf, 512)
        if 0 < n < 512:
            return buf.value
    except Exception:
        pass
    return path


_VK_NAMES = {
    "space": 0x20, "tab": 0x09, "pause": 0x13, "printscreen": 0x2C, "prtsc": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "scrolllock": 0x91, "numlock": 0x90,
}


def parse_hotkey(text: str) -> Optional[tuple[int, int]]:
    """'alt+shift+r' → (mods, vk). 잘못되면 None."""
    if not text:
        return None
    parts = [p.strip().lower() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        if p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p in ("win", "windows"):
            mods |= MOD_WIN
        else:
            if key is not None:
                return None
            key = p
    if key is None:
        return None
    vk: Optional[int] = None
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _VK_NAMES:
        vk = _VK_NAMES[key]
    if vk is None:
        return None
    if mods == 0 and not (0x70 <= vk <= 0x87):
        return None  # 수식키 없는 일반 키는 전역 핫키로 쓰면 모든 앱 입력 먹힘
    return mods, vk


def hotkey_display(text: str) -> str:
    parts = [p.strip() for p in text.split("+") if p.strip()]
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    return "+".join(names.get(p.lower(), p.upper() if len(p) == 1 else p.capitalize()) for p in parts)


def split_hotkey(text: str) -> tuple[str, str, str]:
    """'alt+shift+r' → ('alt+shift+', 'Alt+Shift+', 'R')  (수식키 소문자, 표시용, 키)"""
    parts = [p.strip().lower() for p in (text or "").split("+") if p.strip()]
    modnames = ("ctrl", "control", "alt", "shift", "win")
    mods = [p for p in parts if p in modnames]
    keys = [p for p in parts if p not in modnames]
    if not mods:
        mods = ["alt", "shift"]
    key = keys[0] if keys else ""
    names = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    disp_key = key.upper() if len(key) == 1 else key.capitalize()
    return "+".join(mods) + "+", "+".join(names[m] for m in mods) + "+", disp_key


class Settings:
    """capkachu.json — exe 옆. 못 쓰면 %APPDATA%\\CapKachu."""

    def __init__(self) -> None:
        self.data: dict = dict(DEFAULT_SETTINGS)
        self.path = self._pick_path()
        self.load()

    @staticmethod
    def _base_dir() -> str:
        return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
            else os.path.dirname(os.path.abspath(__file__))

    @staticmethod
    def _pick_path() -> str:
        """%APPDATA%\\CapKachu2\\capkachu2.json — exe 옆에 json이 보이면 사용자가 거슬려함."""
        appdata = os.path.join(os.environ.get("APPDATA", Settings._base_dir()), "CapKachu2")
        try:
            os.makedirs(appdata, exist_ok=True)
            return os.path.join(appdata, SETTINGS_FILE)
        except OSError:
            return os.path.join(Settings._base_dir(), SETTINGS_FILE)

    def _read_into(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    for k in DEFAULT_SETTINGS:
                        if k in loaded:
                            self.data[k] = loaded[k]
            return True
        except Exception:
            return False

    def load(self) -> None:
        # exe 옆 파일은 읽지도 지우지도 않음 (EDR이 "실행 직후 옆 파일 삭제"를 의심)
        self._read_into(self.path)

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key: str):
        return self.data.get(key, DEFAULT_SETTINGS.get(key))

    def save_dir(self) -> str:
        d = (self.get("save_dir") or "").strip()
        if not d:
            d = os.path.join(get_videos_dir(), "CapKachu")
        return d

    def fps(self) -> int:
        try:
            return max(5, min(30, int(self.get("fps"))))
        except Exception:
            return 20

    def gif_fps(self) -> int:
        try:
            return max(2, min(15, int(self.get("gif_fps"))))
        except Exception:
            return 10

    def max_seconds(self) -> float:
        try:
            return max(0.5, float(self.get("max_minutes"))) * 60.0
        except Exception:
            return 600.0

    def min_seconds(self) -> float:
        try:
            return max(0.0, float(self.get("min_seconds")))
        except Exception:
            return 1.0

    def show_cursor(self) -> bool:
        return bool(self.get("show_cursor"))


SETTINGS: Optional[Settings] = None


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


def get_autostart_command() -> str:
    """exe면 exe 경로, .py면 같은 venv의 pythonw + 스크립트"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.exists(pyw):
        py = pyw
    return f'"{py}" "{os.path.abspath(__file__)}"'


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
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, get_autostart_command())
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
_DIM_LUT = [int(v * 0.4) for v in range(256)] * 3


class CaptureOverlay:
    """Win+Shift+S 클론. on_complete(Image|None)로 결과 반환."""

    def __init__(self, parent: tk.Tk, on_complete: Callable, rect_mode: bool = False,
                 hint: str = ""):
        """rect_mode=True면 이미지 대신 화면 좌표 (x1, y1, x2, y2)를 on_complete로 넘김 (녹화 영역 선택용)."""
        self.on_complete = on_complete
        self._called = False
        self.mode = "rect"
        self.rect_mode = rect_mode
        self.hint = hint

        self.vx, self.vy, self.vw, self.vh = get_virtual_screen_bounds()
        self.screenshot = grab_all_screens()
        # LUT point()가 ImageEnhance보다 3배 빠름 (3840x1080 기준 5ms vs 17ms) — 핫키 체감 지연 최소화
        self.dimmed = self.screenshot.convert("RGB").point(_DIM_LUT)

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
        if self.hint:
            mx, my, mw, mh = get_monitor_at_cursor()
            hx = mx - self.vx + mw // 2
            hy = my - self.vy + 40
            self.canvas.create_rectangle(hx - 230, hy - 18, hx + 230, hy + 18, fill="#1F1F1F", outline="")
            self.canvas.create_text(hx, hy, text=self.hint, fill="white", font=("Segoe UI", 11, "bold"))

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
                if self.rect_mode:
                    self._finish((x1 + self.vx, y1 + self.vy, x2 + self.vx, y2 + self.vy))
                else:
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
def show_toast(parent: tk.Tk, image: Optional[Image.Image], on_click: Callable[[], None],
               text: str = "Capture ready\nClick to edit", duration_ms: int = 4500) -> None:
    if image is None:
        image = Image.new("RGB", (64, 64), (60, 60, 60))
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
        frame, text=text, bg="#252525", fg="white",
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

    toast.after(duration_ms, auto_close)


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
        self.win.title(APP_TITLE)
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
        """한 줄 툴바 — 모든 요소 같은 높이(세로 중앙), 그룹 사이만 구분선."""
        tb = tk.Frame(self.win, bg="#FFFFFF", pady=6, padx=10)
        tb.pack(fill=tk.X)
        self.toolbar_frame = tb

        BTN_FONT = ("Segoe UI", 10)
        BTN_PADX = 10
        BTN_PADY = 6
        GAP = 3           # 버튼 사이
        GROUP_GAP = 10    # 구분선 좌우

        def vsep():
            tk.Frame(tb, width=1, bg="#D8D8D8").pack(side=tk.LEFT, fill=tk.Y, padx=GROUP_GAP, pady=6)

        # === Tools ===
        self.tool_btns: dict[str, HoverButton] = {}
        for tool, label in [("pen", "✎ Pen"), ("highlighter", "▮ Highlighter"), ("text", "T Text")]:
            b = HoverButton(
                tb, text=label,
                bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER,
                fg="#222222", font=BTN_FONT, padx=BTN_PADX, pady=BTN_PADY,
                command=lambda t=tool: self._set_tool(t),
            )
            b.pack(side=tk.LEFT, padx=GAP)
            self.tool_btns[tool] = b

        vsep()

        # === 색상 5개 + 굵기 슬라이더 (같은 줄, 세로 중앙) ===
        SWATCH = 22
        SWATCH_GAP = 5
        self.color_btns: dict[str, dict] = {}
        for i, hexv in enumerate(COLORS):
            sw = tk.Canvas(
                tb, width=SWATCH, height=SWATCH,
                highlightthickness=0, bd=0, bg="#FFFFFF", cursor="hand2",
            )
            sw.pack(side=tk.LEFT, padx=(0 if i == 0 else SWATCH_GAP, 0))
            sw.create_rectangle(1, 1, SWATCH - 2, SWATCH - 2, fill=hexv, outline="")
            sw.create_rectangle(0, 0, SWATCH - 1, SWATCH - 1, outline="#B0B0B0", width=1)
            sel_inner = sw.create_rectangle(2, 2, SWATCH - 3, SWATCH - 3, outline="#FFFFFF", width=1, state="hidden")
            sel_outer = sw.create_rectangle(0, 0, SWATCH - 1, SWATCH - 1, outline="#1A1A1A", width=2, state="hidden")
            sw.bind("<Button-1>", lambda e, c=hexv: self._set_color(c))
            self.color_btns[hexv] = {"sw": sw, "sel_inner": sel_inner, "sel_outer": sel_outer}

        self.thickness_slider = ThinSlider(
            tb, from_=1, to=20, length=110, height=SWATCH,
            bg="#FFFFFF", command=self._on_thickness_change,
        )
        self.thickness_slider.pack(side=tk.LEFT, padx=(12, 0))

        vsep()

        # === Undo / Redo — 글자 버튼과 같은 높이가 되게 같은 폰트 크기/패딩 ===
        for sym, fn in [("↶", self.undo), ("↷", self.redo)]:
            b = HoverButton(
                tb, text=sym,
                bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER,
                fg="#222222", font=("Segoe UI Symbol", 12), padx=BTN_PADX, pady=BTN_PADY - 4,
                command=fn,
            )
            b.pack(side=tk.LEFT, padx=GAP)

        # === Right-side actions (오른쪽 정렬, 같은 높이) ===
        copy_btn = HoverButton(
            tb, text="📋 Copy",
            bg=COLOR_COPY_BG, hover_bg=COLOR_COPY_HOVER, fg="white",
            font=("Segoe UI", 10, "bold"), padx=BTN_PADX, pady=BTN_PADY,
            command=self.copy_to_clipboard,
        )
        copy_btn.pack(side=tk.RIGHT, padx=GAP)
        paint_btn = HoverButton(
            tb, text="🎨 Open in Paint",
            bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER, fg="#222222",
            font=BTN_FONT, padx=BTN_PADX, pady=BTN_PADY,
            command=self.export,
        )
        paint_btn.pack(side=tk.RIGHT, padx=GAP)
        save_btn = HoverButton(
            tb, text="💾 Save",
            bg=COLOR_NEUTRAL_BG, hover_bg=COLOR_NEUTRAL_HOVER, fg="#222222",
            font=BTN_FONT, padx=BTN_PADX, pady=BTN_PADY,
            command=self.save,
        )
        save_btn.pack(side=tk.RIGHT, padx=GAP)

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
            set_cursor_safe(self.canvas, "xterm" if t == "text" else pen_cursor_spec())
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
            highlightthickness=0, bg="#DCDCDC",
        )
        set_cursor_safe(self.canvas, pen_cursor_spec())
        self.canvas.pack(padx=10, pady=(0, 10))
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
        set_cursor_safe(self.canvas, "xterm" if self.tool == "text" else pen_cursor_spec())

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
    """여러 전역 핫키. bindings = [(id, "alt+shift+s", callback), ...]
    RegisterHotKey는 등록한 스레드의 메시지 큐로 오므로 전용 스레드에서 run()."""

    def __init__(self, bindings: list[tuple[int, str, Callable[[], None]]]):
        self.bindings = bindings
        self._running = True
        self._thread_id: Optional[int] = None
        self._registered: list[int] = []
        self.failed: list[str] = []       # 등록 실패한 핫키 문자열 (다른 앱이 선점 등)
        self.ready = threading.Event()

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        callbacks: dict[int, Callable[[], None]] = {}
        for hid, text, cb in self.bindings:
            parsed = parse_hotkey(text)
            if parsed is None:
                self.failed.append(text or "(empty)")
                continue
            mods, vk = parsed
            if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                self._registered.append(hid)
                callbacks[hid] = cb
            else:
                self.failed.append(text)
        self.ready.set()

        msg = wintypes.MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            if msg.message == WM_HOTKEY and msg.wParam in callbacks:
                # 콜백이 무거우면 메시지 루프 막힘 → 즉시 비차단 호출
                try:
                    callbacks[msg.wParam]()
                except Exception:
                    pass

        for hid in self._registered:
            user32.UnregisterHotKey(None, hid)
        self._registered.clear()

    def stop(self) -> None:
        self._running = False
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


class HotkeyManager:
    """설정 변경 시 리스너 스레드를 통째로 재시작."""

    def __init__(self, make_bindings: Callable[[], list[tuple[int, str, Callable[[], None]]]]):
        self._make = make_bindings
        self._listener: Optional[HotkeyListener] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> list[str]:
        self.stop()
        self._listener = HotkeyListener(self._make())
        self._thread = threading.Thread(target=self._listener.run, daemon=True, name="HotkeyThread")
        self._thread.start()
        self._listener.ready.wait(timeout=3)
        return list(self._listener.failed)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            if self._thread is not None:
                self._thread.join(timeout=2)
        self._listener = None
        self._thread = None


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
# 화면 녹화 — 영역 BitBlt (ctypes) → MP4(H.264, Media Foundation) / GIF(Pillow 스트리밍)
# ============================================================
_cv2 = None
_np = None


def _load_cv2():
    """cv2/numpy 지연 로드 — 시작 속도 유지. 실패하면 (None, None)."""
    global _cv2, _np
    if _cv2 is None:
        try:
            import numpy as np  # noqa
            import cv2  # noqa
            _np, _cv2 = np, cv2
        except Exception:
            return None, None
    return _cv2, _np


class RegionGrabber:
    """화면 영역을 32bpp DIB로 BitBlt. 커서(핫스팟 보정)까지 그려 넣음. numpy BGRA 배열 반환."""

    def __init__(self, x: int, y: int, w: int, h: int, show_cursor: bool = True):
        _, np = _load_cv2()
        if np is None:
            raise RuntimeError("numpy not available")
        self.np = np
        self.x, self.y, self.w, self.h = x, y, w, h
        self.show_cursor = show_cursor
        self._hotspots: dict[int, tuple[int, int]] = {}

        self.hdc_screen = user32.GetDC(None)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_screen)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        self.bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                           ctypes.byref(self.bits), None, 0)
        if not self.hbmp or not self.bits.value:
            self.close()
            raise RuntimeError("CreateDIBSection failed")
        self.old_obj = gdi32.SelectObject(self.hdc_mem, self.hbmp)
        buf_t = ctypes.c_ubyte * (w * h * 4)
        self._buf = buf_t.from_address(self.bits.value)
        self._arr = np.ctypeslib.as_array(self._buf).reshape(h, w, 4)

    def _draw_cursor(self) -> None:
        ci = _CURSORINFO()
        ci.cbSize = ctypes.sizeof(_CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(ci)) or not (ci.flags & CURSOR_SHOWING) or not ci.hCursor:
            return
        hcur = int(ci.hCursor)
        hs = self._hotspots.get(hcur)
        if hs is None:
            ii = _ICONINFO()
            if user32.GetIconInfo(hcur, ctypes.byref(ii)):
                hs = (int(ii.xHotspot), int(ii.yHotspot))
                if ii.hbmMask:
                    gdi32.DeleteObject(ii.hbmMask)
                if ii.hbmColor:
                    gdi32.DeleteObject(ii.hbmColor)
            else:
                hs = (0, 0)
            if len(self._hotspots) > 64:
                self._hotspots.clear()
            self._hotspots[hcur] = hs
        cx = ci.ptScreenPos.x - self.x - hs[0]
        cy = ci.ptScreenPos.y - self.y - hs[1]
        if -64 < cx < self.w and -64 < cy < self.h:
            user32.DrawIconEx(self.hdc_mem, cx, cy, hcur, 0, 0, 0, None, DI_NORMAL)

    def grab(self):
        """BGRA (h, w, 4) — 내부 버퍼라서 호출자가 복사/변환해서 써야 함."""
        gdi32.BitBlt(self.hdc_mem, 0, 0, self.w, self.h, self.hdc_screen, self.x, self.y, SRCCOPY | CAPTUREBLT)
        if self.show_cursor:
            self._draw_cursor()
        return self._arr

    def close(self) -> None:
        try:
            if getattr(self, "old_obj", None):
                gdi32.SelectObject(self.hdc_mem, self.old_obj)
            if getattr(self, "hbmp", None):
                gdi32.DeleteObject(self.hbmp)
            if getattr(self, "hdc_mem", None):
                gdi32.DeleteDC(self.hdc_mem)
            if getattr(self, "hdc_screen", None):
                user32.ReleaseDC(None, self.hdc_screen)
        except Exception:
            pass
        self.hbmp = None
        self.hdc_mem = None
        self.hdc_screen = None


class Mp4Writer:
    """OpenCV MSMF 백엔드 → Windows 내장 H.264 인코더. 외부 ffmpeg.exe 없음."""

    def __init__(self, path: str, fps: int, w: int, h: int):
        cv2, np = _load_cv2()
        if cv2 is None:
            raise RuntimeError("OpenCV not available")
        self.cv2 = cv2
        self.writer = cv2.VideoWriter(path, cv2.CAP_MSMF, cv2.VideoWriter_fourcc(*"H264"), float(fps), (w, h))
        if not self.writer.isOpened():
            # 폴백: ffmpeg 백엔드 mp4v (MPEG-4 Part 2)
            self.writer = cv2.VideoWriter(path, cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
        if not self.writer.isOpened():
            raise RuntimeError("VideoWriter open failed")

    def write(self, bgra) -> None:
        self.writer.write(self.cv2.cvtColor(bgra, self.cv2.COLOR_BGRA2BGR))

    def release(self) -> None:
        try:
            self.writer.release()
        except Exception:
            pass


class GifWriter:
    """Pillow 스트리밍 GIF — 프레임마다 FASTOCTREE 팔레트(local color table). 메모리에 안 쌓음."""

    def __init__(self, path: str, fps: int, w: int, h: int):
        from PIL import GifImagePlugin
        self._gif = GifImagePlugin
        _, np = _load_cv2()
        self.np = np
        self.fp = open(path, "wb")
        self.duration = max(20, int(round(1000.0 / fps)))
        self.w, self.h = w, h
        self._first = True

    def _to_p(self, bgra) -> Image.Image:
        rgb = self.np.ascontiguousarray(bgra[:, :, 2::-1])
        im = Image.frombuffer("RGB", (self.w, self.h), rgb, "raw", "RGB", 0, 1)
        return im.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)

    def write(self, bgra) -> None:
        p = self._to_p(bgra)
        if self._first:
            self._first = False
            for chunk in self._gif.getheader(p, info={"loop": 0})[0]:
                self.fp.write(chunk)
        for chunk in self._gif.getdata(p, duration=self.duration, include_color_table=True):
            self.fp.write(chunk)

    def release(self) -> None:
        try:
            if not self._first:
                self.fp.write(b";")
            self.fp.close()
        except Exception:
            pass


@dataclass
class RecordResult:
    path: Optional[str]
    fmt: str
    duration: float
    size_bytes: int
    discarded: bool
    error: Optional[str]
    last_frame: Optional[Image.Image]


class ScreenRecorder:
    """녹화 스레드. stop(save=True/False)로 종료. 끝나면 on_done(RecordResult) (녹화 스레드에서 호출)."""

    def __init__(self, rect: tuple[int, int, int, int], fmt: str, final_path: str, tmp_path: str,
                 fps: int, max_seconds: float, min_seconds: float, show_cursor: bool,
                 on_done: Callable[[RecordResult], None]):
        self.x, self.y, self.w, self.h = rect
        self.fmt = fmt
        self.final_path = final_path
        self.tmp_path = tmp_path
        self.fps = fps
        self.max_seconds = max_seconds
        self.min_seconds = min_seconds
        self.show_cursor = show_cursor
        self.on_done = on_done
        self._stop = threading.Event()
        self._save = True
        self.started_at = time.perf_counter()
        self.frames_written = 0
        self.stats: dict = {"pending": 0, "grabs": 0, "repeats": 0, "max_n": 0}
        self.thread = threading.Thread(target=self._run, daemon=True, name="RecorderThread")

    def start(self) -> None:
        self.thread.start()

    def stop(self, save: bool = True) -> None:
        self._save = save and self._save
        self._stop.set()

    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    def _run(self) -> None:
        grabber = None
        writer = None
        error: Optional[str] = None
        last = None
        try:
            grabber = RegionGrabber(self.x, self.y, self.w, self.h, self.show_cursor)
            try:
                ctypes.windll.dwmapi.DwmFlush()
            except Exception:
                pass
            period = 1.0 / self.fps
            self.started_at = time.perf_counter()
            start = self.started_at

            # writer(MSMF 인코더) 초기화는 0.2~0.8초 걸림 → 그동안 프레임을 복사해 뒀다가 flush
            holder: dict = {}

            def _make_writer():
                try:
                    if self.fmt == "gif":
                        holder["w"] = GifWriter(self.tmp_path, self.fps, self.w, self.h)
                    else:
                        holder["w"] = Mp4Writer(self.tmp_path, self.fps, self.w, self.h)
                except Exception as e:  # noqa
                    holder["err"] = e

            wt = threading.Thread(target=_make_writer, daemon=True, name="WriterInit")
            wt.start()
            pending: list = []
            while wt.is_alive() and not self._stop.is_set():
                now = time.perf_counter()
                due = start + len(pending) * period
                if now < due:
                    self._stop.wait(min(due - now, 0.05))
                    continue
                if len(pending) < 100:
                    pending.append(grabber.grab().copy())
                else:
                    self._stop.wait(0.02)
            wt.join()
            if "err" in holder:
                raise holder["err"]
            writer = holder.get("w")
            if writer is None:
                raise RuntimeError("writer init failed")
            if not self._save:
                pending = pending[-1:]  # 버림이면 인코딩 생략 (마지막 프레임만 썸네일용)
            for fr in pending:
                writer.write(fr)
                last = fr
            self.frames_written = len(pending)
            self.stats["pending"] = len(pending)
            pending.clear()

            while not self._stop.is_set():
                now = time.perf_counter()
                due = start + self.frames_written * period
                if now < due:
                    self._stop.wait(min(due - now, 0.05))
                    continue
                frame = grabber.grab()
                # 밀린 슬롯만큼 같은 프레임 반복 → 실시간 길이 유지 (최대 5)
                slots = int((time.perf_counter() - start) / period) + 1
                n = max(1, min(slots - self.frames_written, 5))
                for _ in range(n):
                    writer.write(frame)
                self.frames_written += n
                self.stats["grabs"] += 1
                self.stats["repeats"] += n - 1
                self.stats["max_n"] = max(self.stats["max_n"], n)
                last = frame
                if time.perf_counter() - start >= self.max_seconds:
                    break
        except Exception as e:  # noqa
            error = f"{type(e).__name__}: {e}"
        finally:
            if writer is not None:
                writer.release()
            if grabber is not None:
                if last is not None:
                    try:
                        _, np = _load_cv2()
                        rgb = np.ascontiguousarray(last[:, :, 2::-1])
                        last_img = Image.frombuffer("RGB", (self.w, self.h), rgb, "raw", "RGB", 0, 1).copy()
                    except Exception:
                        last_img = None
                else:
                    last_img = None
                grabber.close()
            else:
                last_img = None

        duration = self.frames_written / float(self.fps)
        discarded = (not self._save) or duration < self.min_seconds or error is not None
        path: Optional[str] = None
        size = 0
        if discarded:
            try:
                if os.path.exists(self.tmp_path):
                    os.remove(self.tmp_path)
            except Exception:
                pass
        else:
            try:
                os.makedirs(os.path.dirname(self.final_path), exist_ok=True)
                shutil.move(self.tmp_path, self.final_path)
                path = self.final_path
                size = os.path.getsize(path)
            except Exception as e:  # noqa
                error = f"save failed: {e}"
                path = self.tmp_path if os.path.exists(self.tmp_path) else None
        try:
            self.on_done(RecordResult(path, self.fmt, duration, size, discarded, error, last_img))
        except Exception:
            pass


def _apply_exstyle(win, flags: int) -> None:
    try:
        hwnd = _get_hwnd(win)
        if hwnd:
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | flags)
    except Exception:
        pass


class RecordingHUD:
    """녹화 중 표시: 영역 바깥 빨간 테두리(클릭 통과) + 작은 컨트롤 바 (● 00:12 ■ ✕)."""

    TRANS = "#010203"

    def __init__(self, parent: tk.Tk, rect: tuple[int, int, int, int], fmt: str,
                 elapsed_fn: Callable[[], float], on_stop: Callable[[bool], None]):
        self.parent = parent
        self.x, self.y, self.w, self.h = rect
        self.fmt = fmt
        self.elapsed_fn = elapsed_fn
        self.on_stop = on_stop
        self._alive = True
        self._blink = True

        # ---- 테두리 ----
        gap = REC_BORDER_GAP + REC_BORDER_WIDTH
        bx, by = self.x - gap, self.y - gap
        bw, bh = self.w + 2 * gap, self.h + 2 * gap
        self.border = tk.Toplevel(parent)
        self.border.overrideredirect(True)
        self.border.attributes("-topmost", True)
        self.border.geometry(f"{bw}x{bh}+{bx}+{by}")
        self.border.configure(bg=self.TRANS)
        try:
            self.border.attributes("-transparentcolor", self.TRANS)
        except tk.TclError:
            pass
        c = tk.Canvas(self.border, width=bw, height=bh, bg=self.TRANS, highlightthickness=0, bd=0)
        c.pack()
        half = REC_BORDER_WIDTH // 2
        c.create_rectangle(half, half, bw - half - 1, bh - half - 1,
                           outline=REC_BORDER_COLOR, width=REC_BORDER_WIDTH)
        self.border.update_idletasks()
        _apply_exstyle(self.border, WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

        # ---- 컨트롤 바 ----
        self.bar = tk.Toplevel(parent)
        self.bar.overrideredirect(True)
        self.bar.attributes("-topmost", True)
        self.bar.configure(bg="#1F1F1F")
        f = tk.Frame(self.bar, bg="#1F1F1F", padx=10, pady=6)
        f.pack()
        self.dot = tk.Label(f, text="●", fg=REC_BORDER_COLOR, bg="#1F1F1F", font=("Segoe UI", 11, "bold"))
        self.dot.pack(side=tk.LEFT)
        self.timer = tk.Label(f, text="00:00", fg="white", bg="#1F1F1F", font=("Consolas", 14, "bold"))
        self.timer.pack(side=tk.LEFT, padx=(6, 4))
        self.fmt_lbl = tk.Label(f, text=fmt.upper(), fg="#9A9A9A", bg="#1F1F1F", font=("Segoe UI", 8))
        self.fmt_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_stop = HoverButton(f, text="■ Stop", bg=REC_BORDER_COLOR, hover_bg="#C62828",
                                    font=("Segoe UI", 12, "bold"), padx=16, pady=5,
                                    command=lambda: self._click(True))
        self.btn_stop.pack(side=tk.LEFT)
        self.btn_discard = HoverButton(f, text="✕", bg="#3A3A3A", hover_bg="#555555",
                                       font=("Segoe UI", 12, "bold"), padx=14, pady=5,
                                       command=lambda: self._click(False))
        self.btn_discard.pack(side=tk.LEFT, padx=(10, 0))
        for w_ in (f, self.dot, self.timer, self.fmt_lbl):  # Toplevel에 걸면 버튼까지 상속됨
            w_.bind("<ButtonPress-1>", self._drag_start)
            w_.bind("<B1-Motion>", self._drag_move)
        self.bar.update_idletasks()
        self._place_bar()
        _apply_exstyle(self.bar, WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self._tick()

    def _place_bar(self) -> None:
        bw = self.bar.winfo_reqwidth()
        bh = self.bar.winfo_reqheight()
        vx, vy, vw, vh = get_virtual_screen_bounds()
        gap = REC_BORDER_GAP + REC_BORDER_WIDTH + 6
        x = self.x + self.w - bw
        x = max(vx, min(x, vx + vw - bw))
        y = self.y + self.h + gap
        if y + bh > vy + vh:
            y = self.y - gap - bh
            if y < vy:
                y = self.y + gap  # 공간 없으면 영역 안쪽 우상단
        self.bar.geometry(f"{bw}x{bh}+{x}+{y}")

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root - self.bar.winfo_x(), e.y_root - self.bar.winfo_y())

    def _drag_move(self, e) -> None:
        d = getattr(self, "_drag", None)
        if d:
            self.bar.geometry(f"+{e.x_root - d[0]}+{e.y_root - d[1]}")

    def _click(self, save: bool) -> None:
        if not self._alive:
            return
        self.btn_stop.configure(state="disabled", text="Saving…" if save else "■ Stop")
        self.btn_discard.configure(state="disabled")
        try:
            self.on_stop(save)
        except Exception:
            pass

    def _tick(self) -> None:
        if not self._alive:
            return
        try:
            s = int(self.elapsed_fn())
            self.timer.configure(text=f"{s // 60:02d}:{s % 60:02d}")
            self._blink = not self._blink
            self.dot.configure(fg=REC_BORDER_COLOR if self._blink else "#6A2A2A")
            self.bar.after(500, self._tick)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._alive = False
        for w_ in (self.bar, self.border):
            try:
                w_.destroy()
            except tk.TclError:
                pass


# ============================================================
# 녹화 오케스트레이션 — 핫키/트레이 공용
# ============================================================
_rec_lock = threading.Lock()
_recorder: Optional[ScreenRecorder] = None
_rec_hud: Optional[RecordingHUD] = None


def warm_up_encoder() -> None:
    """백그라운드에서 H.264 인코더를 한 번 열었다 닫음 — 첫 녹화 시작 지연 단축."""
    try:
        cv2, _ = _load_cv2()
        if cv2 is None:
            return
        p = os.path.join(short_path(tempfile.gettempdir()), f"capkachu_warm_{os.getpid()}.mp4")
        w = cv2.VideoWriter(p, cv2.CAP_MSMF, cv2.VideoWriter_fourcc(*"H264"), 20.0, (64, 64))
        w.release()
        try:
            os.remove(p)
        except OSError:
            pass
    except Exception:
        pass


def is_recording() -> bool:
    with _rec_lock:
        return _recorder is not None


def open_recordings_folder(select_path: Optional[str] = None) -> None:
    try:
        if select_path and os.path.exists(select_path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(select_path)])
            return
        d = SETTINGS.save_dir() if SETTINGS else os.path.join(get_videos_dir(), "CapKachu")
        os.makedirs(d, exist_ok=True)
        os.startfile(d)
    except Exception:
        pass


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _fmt_dur(sec: float) -> str:
    s = int(round(sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def stop_recording(save: bool = True) -> None:
    """어느 스레드에서든 호출 가능. 실제 정리는 _on_record_done(GUI 스레드)."""
    with _rec_lock:
        rec = _recorder
    if rec is not None:
        rec.stop(save)


def _on_record_done(result: RecordResult) -> None:
    global _recorder, _rec_hud
    with _rec_lock:
        _recorder = None
        hud, _rec_hud = _rec_hud, None
    if hud is not None:
        hud.destroy()
    if GUI is None or GUI.root is None:
        return
    if result.error and result.path is None:
        show_toast(GUI.root, result.last_frame, lambda: None,
                   text=f"Recording failed\n{result.error[:60]}", duration_ms=7000)
        return
    if result.discarded:
        if result.duration > 0 and result.duration < (SETTINGS.min_seconds() if SETTINGS else 1.0):
            show_toast(GUI.root, result.last_frame, lambda: None,
                       text="Too short — discarded", duration_ms=2500)
        return
    path = result.path
    text = f"Saved {result.fmt.upper()} {_fmt_dur(result.duration)} · {_fmt_size(result.size_bytes)}\nClick to open folder"
    show_toast(GUI.root, result.last_frame, lambda: open_recordings_folder(path), text=text, duration_ms=7000)


def _start_recording(rect: tuple[int, int, int, int], fmt: str) -> None:
    """GUI 스레드. rect = 화면 좌표 (x1, y1, x2, y2)."""
    global _recorder, _rec_hud
    x1, y1, x2, y2 = rect
    w = (x2 - x1) & ~1  # H.264는 짝수 크기
    h = (y2 - y1) & ~1
    if w < 16 or h < 16:
        return
    settings = SETTINGS or Settings()
    fps = settings.gif_fps() if fmt == "gif" else settings.fps()
    ext = "gif" if fmt == "gif" else "mp4"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    final_path = os.path.join(settings.save_dir(), f"CapKachu_{stamp}.{ext}")
    tmp_dir = short_path(tempfile.gettempdir())
    tmp_path = os.path.join(tmp_dir, f"capkachu_rec_{os.getpid()}_{int(time.time())}.{ext}")

    def on_done(result: RecordResult) -> None:
        if GUI is not None:
            GUI.schedule(_on_record_done, result)

    with _rec_lock:
        if _recorder is not None:
            return
        rec = ScreenRecorder((x1, y1, w, h), fmt, final_path, tmp_path, fps,
                             settings.max_seconds(), settings.min_seconds(), settings.show_cursor(), on_done)
        _recorder = rec
    try:
        _rec_hud = RecordingHUD(GUI.root, (x1, y1, w, h), fmt, rec.elapsed, stop_recording)
    except Exception:
        _rec_hud = None
    rec.start()


def trigger_record(fmt: str = "mp4") -> None:
    """핫키/트레이 공용. 녹화 중이면 어느 녹화 키든 '저장하고 종료'로 동작."""
    global _overlay_active
    if is_recording():
        stop_recording(save=True)
        return
    with _overlay_lock:
        if _overlay_active:
            return
        _overlay_active = True
    if GUI is None or GUI.root is None:
        with _overlay_lock:
            _overlay_active = False
        return

    def _make_overlay():
        def on_complete(rect) -> None:
            global _overlay_active
            if rect is None:
                with _overlay_lock:
                    _overlay_active = False
                return

            def _go():
                global _overlay_active
                try:
                    _start_recording(rect, fmt)
                finally:
                    with _overlay_lock:
                        _overlay_active = False

            # 오버레이가 완전히 내려간 뒤 녹화 시작 (첫 프레임에 회색 화면 안 들어가게). 그 사이 핫키는 무시
            GUI.root.after(120, _go)

        try:
            label = "GIF" if fmt == "gif" else "MP4"
            CaptureOverlay(GUI.root, on_complete=on_complete, rect_mode=True,
                           hint=f"Drag the area to record ({label})")
        except Exception:
            global _overlay_active
            with _overlay_lock:
                _overlay_active = False

    GUI.schedule(_make_overlay)


# ============================================================
# 설정 창
# ============================================================
_settings_win: Optional[tk.Toplevel] = None


def open_settings(on_saved: Callable[[], None]) -> None:
    """GUI 스레드에서 호출."""
    global _settings_win
    if GUI is None or GUI.root is None or SETTINGS is None:
        return
    if _settings_win is not None:
        try:
            if _settings_win.winfo_exists():
                _settings_win.lift()
                _settings_win.focus_force()
                return
        except tk.TclError:
            pass
    s = SETTINGS
    win = tk.Toplevel(GUI.root)
    _settings_win = win
    win.title(APP_TITLE)
    win.configure(bg="#FAFAFA")
    win.resizable(False, False)
    apply_window_icon(win)

    body = tk.Frame(win, bg="#FAFAFA", padx=22, pady=18)
    body.pack(fill=tk.BOTH, expand=True)
    FONT = ("Segoe UI", 11)
    KEYFONT = ("Consolas", 12, "bold")

    # ---- 단축키: "Alt+Shift+" 고정 표시 + 키 한 칸 입력 ----
    rows = [("Screen capture", "hotkey_capture"), ("Record (MP4)", "hotkey_record"), ("Record (GIF)", "hotkey_gif")]
    key_vars: dict[str, tuple[str, tk.StringVar]] = {}
    grid = tk.Frame(body, bg="#FAFAFA")
    grid.pack(fill=tk.X)
    for i, (name, skey) in enumerate(rows):
        mods_l, mods_disp, key = split_hotkey(s.get(skey))
        tk.Label(grid, text=name, bg="#FAFAFA", fg="#222222", font=FONT, anchor="w", width=15).grid(row=i, column=0, sticky="w", pady=5)
        tk.Label(grid, text=mods_disp, bg="#FAFAFA", fg="#0078D4", font=KEYFONT, anchor="e").grid(row=i, column=1, sticky="e", padx=(16, 0))
        var = tk.StringVar(value=key)
        e = tk.Entry(grid, textvariable=var, width=4, font=KEYFONT, fg="#0078D4", justify="center", relief="solid", bd=1)
        e.grid(row=i, column=2, sticky="w", padx=(2, 0))
        key_vars[skey] = (mods_l, var)

    # ---- 저장 폴더 (실제 경로가 칸에 바로 보임) ----
    v_dir = tk.StringVar(value=s.save_dir())
    sec = tk.Frame(body, bg="#FAFAFA")
    sec.pack(fill=tk.X, pady=(18, 0))
    tk.Label(sec, text="Save folder", bg="#FAFAFA", font=FONT, anchor="w").pack(side=tk.LEFT, padx=(0, 10))

    def browse():
        d = filedialog.askdirectory(parent=win, initialdir=v_dir.get() or s.save_dir(), title="Recordings folder")
        if d:
            v_dir.set(os.path.normpath(d))

    tk.Button(sec, text="Browse…", font=FONT, command=browse).pack(side=tk.RIGHT)
    tk.Entry(sec, textvariable=v_dir, font=FONT, width=36).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

    btns = tk.Frame(body, bg="#FAFAFA")
    btns.pack(fill=tk.X, pady=(20, 0))

    def close():
        global _settings_win
        _settings_win = None
        try:
            win.destroy()
        except tk.TclError:
            pass

    def save():
        hk_vals = {}
        for skey, (mods_l, var) in key_vars.items():
            k = var.get().strip().lower()
            hk_vals[skey] = mods_l + k if k else ""
        bad = [name for (name, skey) in rows if parse_hotkey(hk_vals[skey]) is None]
        if bad:
            messagebox.showerror(APP_TITLE, "Check the key: " + ", ".join(bad), parent=win)
            return
        if len(set(hk_vals.values())) < 3:
            messagebox.showerror(APP_TITLE, "The three hotkeys must be different.", parent=win)
            return
        d = v_dir.get().strip()
        default_dir = os.path.join(get_videos_dir(), "CapKachu")
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                messagebox.showerror(APP_TITLE, "Cannot create folder:\n" + d, parent=win)
                return
        if os.path.normcase(os.path.abspath(d or default_dir)) == os.path.normcase(os.path.abspath(default_dir)):
            d = ""
        s.data.update({"save_dir": d, **hk_vals})
        s.save()
        close()
        try:
            on_saved()
        except Exception:
            pass

    HoverButton(btns, text="Save", bg=COLOR_COPY_BG, hover_bg=COLOR_COPY_HOVER, font=("Segoe UI", 11, "bold"),
                padx=22, pady=6, command=save).pack(side=tk.RIGHT)
    win.protocol("WM_DELETE_WINDOW", close)
    win.bind("<Escape>", lambda e: close())

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"+{(sw - ww) // 2}+{(sh - wh) // 2}")
    win.focus_force()
    win.lift()


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
            "CapKachu2"
        )
    except Exception:
        pass

    global GUI, SETTINGS
    SETTINGS = Settings()
    if not SETTINGS.get("autostart_init"):
        set_auto_start(True)
        SETTINGS.data["autostart_init"] = True
        SETTINGS.save()
    GUI = GuiThread()

    def make_bindings():
        return [
            (HOTKEY_ID, SETTINGS.get("hotkey_capture"), trigger_capture),
            (HOTKEY_ID_RECORD, SETTINGS.get("hotkey_record"), lambda: trigger_record("mp4")),
            (HOTKEY_ID_GIF, SETTINGS.get("hotkey_gif"), lambda: trigger_record("gif")),
        ]

    hotkeys = HotkeyManager(make_bindings)
    failed = hotkeys.start()
    # (워밍업은 안 함 — cv2/numpy를 미리 올리면 대기 메모리 +60MB. 녹화 시작 시 선버퍼링으로 프레임 손실 없음)
    icon: Optional[pystray.Icon] = None

    def tooltip() -> str:
        return APP_TOOLTIP  # 트레이 툴팁은 이름만 (단축키 나열 = 조잡)

    def notify_failed(failed_list: list[str]) -> None:
        if failed_list and icon is not None:
            try:
                icon.notify("Hotkey not available (used by another app): " + ", ".join(failed_list),
                            APP_TITLE)
            except Exception:
                pass

    def on_settings_saved() -> None:
        # GUI 스레드에서 호출됨 → 핫키 재등록은 별도 스레드에서 (join 대기로 GUI 안 막게)
        def _re():
            f = hotkeys.start()
            if icon is not None:
                try:
                    icon.title = tooltip()
                    icon.update_menu()
                except Exception:
                    pass
            notify_failed(f)
        threading.Thread(target=_re, daemon=True).start()

    def toggle_autostart(_icon, _mi) -> None:
        set_auto_start(not is_auto_start_enabled())

    def autostart_checked(_mi) -> bool:
        return is_auto_start_enabled()

    def on_capture_now(_icon, _mi) -> None:
        trigger_capture()

    def on_stop_rec(_icon, _mi) -> None:
        stop_recording(save=True)

    def on_open_folder(_icon, _mi) -> None:
        open_recordings_folder()

    def on_settings(_icon, _mi) -> None:
        if GUI is not None:
            GUI.schedule(open_settings, on_settings_saved)

    def on_quit(icon_, _mi) -> None:
        if is_recording():
            stop_recording(save=True)
            time.sleep(1.0)
        hotkeys.stop()
        if GUI is not None:
            GUI.stop()
        icon_.stop()

    icon = pystray.Icon(
        APP_NAME,
        create_tray_icon(),
        tooltip(),
        menu=pystray.Menu(
            # Hidden default item: left-click (double-click) still triggers capture
            item("Capture", on_capture_now, default=True, visible=False),
            item("■ Stop recording (save)", on_stop_rec, visible=lambda _mi: is_recording()),
            item("Open recordings folder", on_open_folder),
            item("Settings…", on_settings),
            pystray.Menu.SEPARATOR,
            item("Run on Startup", toggle_autostart, checked=autostart_checked),
            pystray.Menu.SEPARATOR,
            item("Exit", on_quit),
        ),
    )
    if failed:
        threading.Timer(2.0, notify_failed, args=(failed,)).start()
    icon.run()


if __name__ == "__main__":
    main()

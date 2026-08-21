"""A notification-area icon that reopens the console after you close the tab.

The service runs under ``pythonw`` and owns no window, so once the operator
closes the browser tab there is nothing on screen to click.  This puts a small
icon next to the clock: left-click (or double-click) reopens
``http://127.0.0.1:8765/``, right-click offers the same plus a clean shutdown.

Written directly against ``Shell_NotifyIconW`` through ``ctypes`` rather than
pulling in ``pystray`` (which needs Pillow) or ``pywin32``.  This project
already reaches for ``ctypes`` for the Credential Manager and for process
identity, so it is the established pattern here, and the dependency list stays
short enough to audit.

The tray is strictly optional decoration: it runs on a daemon thread and every
failure is swallowed.  A machine where the shell is unavailable must still get
a working service — losing the icon is an inconvenience, losing the payout
scheduler is not.
"""

from __future__ import annotations

import logging
import threading
import webbrowser

logger = logging.getLogger(__name__)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_APP_TRAY = 0x0400 + 1  # WM_APP + 1: our private notification message
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x0
NIM_DELETE = 0x2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4

IDM_OPEN = 1001
IDM_QUIT = 1002

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512


def _build(url: str, icon_path: str | None, on_quit) -> tuple[object, object]:
    """Create the message-only window and register the icon.

    Returns ``(hwnd, notify_data)`` so the caller can tear it down.  Kept
    separate from the loop so a failure during setup never leaves a stray icon
    behind in the notification area.
    """

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    # ctypes defaults every undeclared argument and return value to C int.  On
    # 64-bit Windows that silently truncates handles: an undeclared
    # GetModuleHandleW hands back the low 32 bits of HINSTANCE, RegisterClassW
    # then reads a struct containing that garbage, and the process dies with an
    # access violation — inside the service, that would take the payout
    # scheduler down with it.  Declare everything that carries a handle.
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_void_p, wintypes.LPCWSTR
    ]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    # ``c_void_p`` rather than a POINTER(WNDCLASS): the struct is defined below,
    # and byref() satisfies a void* parameter either way.
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.GetCursorPos.argtypes = [ctypes.c_void_p]

    class NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        ]

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    icon = None
    if icon_path:
        icon = user32.LoadImageW(
            None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
    if not icon:
        icon = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))

    notify = NOTIFYICONDATA()
    notify.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    notify.uID = 1
    notify.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    notify.uCallbackMessage = WM_APP_TRAY
    notify.hIcon = icon
    notify.szTip = "紫鸟提现自动化 — 点击打开管理后台"

    def show_menu(hwnd) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, IDM_OPEN, "打开管理后台")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_QUIT, "退出并停止后台服务")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # Required by TrackPopupMenu, otherwise the menu will not dismiss when
        # the operator clicks elsewhere.
        user32.SetForegroundWindow(hwnd)
        chosen = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None
        )
        user32.DestroyMenu(menu)
        if chosen == IDM_OPEN:
            webbrowser.open(url)
        elif chosen == IDM_QUIT:
            user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)

    def window_proc(hwnd, message, wparam, lparam):
        if message == WM_APP_TRAY:
            action = lparam & 0xFFFF
            if action in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                webbrowser.open(url)
            elif action == WM_RBUTTONUP:
                show_menu(hwnd)
            return 0
        if message == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(notify))
            user32.PostQuitMessage(0)
            on_quit()
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    proc = WNDPROC(window_proc)
    window_class = WNDCLASS()
    window_class.lpfnWndProc = proc
    window_class.hInstance = kernel32.GetModuleHandleW(None)
    window_class.lpszClassName = "ZiniaoAutomationTray"
    # A window class name is per-process and outlives the window.  Without this
    # a second start() — a restart inside one process, or the second test in a
    # run — fails with ERROR_CLASS_ALREADY_EXISTS and loses the icon.
    user32.UnregisterClassW("ZiniaoAutomationTray", window_class.hInstance)
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise OSError(ctypes.get_last_error(), "RegisterClassW failed")

    hwnd = user32.CreateWindowExW(
        0, "ZiniaoAutomationTray", "紫鸟提现自动化", 0, 0, 0, 0, 0,
        None, None, window_class.hInstance, None,
    )
    if not hwnd:
        raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")

    notify.hWnd = hwnd
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(notify)):
        user32.DestroyWindow(hwnd)
        raise OSError(ctypes.get_last_error(), "Shell_NotifyIconW failed")

    # Keep the callable and the class alive; if Python collects ``proc`` while
    # Windows still holds the pointer, the next message crashes the process.
    hwnd_keepalive.extend([proc, window_class, notify])
    return hwnd, notify


hwnd_keepalive: list[object] = []
_thread: threading.Thread | None = None


def _run(url: str, icon_path: str | None, on_quit) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        _build(url, icon_path, on_quit)
    except Exception:
        logger.info(
            "Tray icon unavailable; the service continues without it",
            exc_info=True,
            extra={"event": "tray_unavailable"},
        )
        return

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))


def stop() -> None:
    """Remove the icon and end the message loop.  Safe to call more than once.

    A daemon thread blocked in ``GetMessageW`` is still inside a Win32 call
    while CPython tears the interpreter down, which prints an alarming (and
    harmless) stack trace on exit.  Asking the window to close first avoids it.
    """

    for item in list(hwnd_keepalive):
        hwnd = getattr(item, "hWnd", None)
        if not hwnd:
            continue
        try:
            import ctypes

            ctypes.WinDLL("user32", use_last_error=True).PostMessageW(
                hwnd, WM_DESTROY, 0, 0
            )
        except Exception:
            pass
    # Wait for the loop to act on WM_DESTROY *before* dropping the references.
    # Clearing first frees the window procedure while Windows is still
    # dispatching into it — an access violation, which is precisely what this
    # keepalive list exists to prevent.
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=2.0)
    hwnd_keepalive.clear()


def start(url: str, *, icon_path: str | None = None, on_quit=lambda: None) -> None:
    """Show the tray icon on a background thread.  Never raises.

    A daemon thread so it cannot keep the process alive after uvicorn stops.
    """

    import atexit
    import os

    if os.name != "nt":
        return
    global _thread
    _thread = threading.Thread(
        target=_run, args=(url, icon_path, on_quit), name="ziniao-tray", daemon=True
    )
    _thread.start()
    atexit.register(stop)

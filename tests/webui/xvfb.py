"""Xvfb display and X11 helpers for the desktop-window tests."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import time

import httpx
import pytest


@pytest.fixture
def display():
    """A virtual X display; the test skips where Xvfb and xdotool are not installed."""
    missing = [tool for tool in ("Xvfb", "xdotool") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"needs {' and '.join(missing)} for a virtual X display")
    r, w = os.pipe()
    xvfb = subprocess.Popen(
        ["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        pass_fds=(w,),
        stderr=subprocess.DEVNULL,
    )
    os.close(w)
    with os.fdopen(r) as f:
        number = f.readline().strip()
    yield f":{number}"
    xvfb.terminate()
    xvfb.wait(10)


def _wait(cond, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def _close_like_a_user(display: str, win: int) -> None:
    """Ask the window to close as a window manager does for the close button: a WM_DELETE_WINDOW client message.

    Not `xdotool windowclose`: that destroys the X window under the client, and a Flet (GTK) client still painting
    then gets BadDrawable and exits 1 (seen under load). This xdotool has no `windowquit`, and Xvfb has
    no window manager, so the message is sent with libX11 directly.
    """

    class ClientMessage(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_int),
            ("serial", ctypes.c_ulong),
            ("send_event", ctypes.c_int),
            ("display", ctypes.c_void_p),
            ("window", ctypes.c_ulong),
            ("message_type", ctypes.c_ulong),
            ("format", ctypes.c_int),
            ("data", ctypes.c_long * 5),
        ]

    class XEvent(ctypes.Union):
        _fields_ = [("xclient", ClientMessage), ("pad", ctypes.c_long * 24)]

    x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XSendEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_long,
        ctypes.POINTER(XEvent),
    ]
    x11.XFlush.argtypes = x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x11.XOpenDisplay(display.encode())
    assert dpy, f"cannot open display {display}"
    try:
        ev = XEvent()
        ev.xclient.type = 33  # ClientMessage
        ev.xclient.window = win
        ev.xclient.message_type = x11.XInternAtom(dpy, b"WM_PROTOCOLS", False)
        ev.xclient.format = 32
        ev.xclient.data[0] = x11.XInternAtom(dpy, b"WM_DELETE_WINDOW", False)
        ev.xclient.data[1] = 0  # CurrentTime
        assert x11.XSendEvent(dpy, win, False, 0, ctypes.byref(ev))
        x11.XFlush(dpy)
    finally:
        x11.XCloseDisplay(dpy)


def _health(url: str) -> bool:
    try:
        return httpx.get(f"{url}/api/v1/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False

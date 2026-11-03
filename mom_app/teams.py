"""Guess the current Teams meeting name from Teams' window titles, e.g. "Sprint Planning | Microsoft Teams"."""

import logging
import re

log = logging.getLogger(__name__)

TEAMS_PROCESSES = {"ms-teams.exe", "teams.exe", "msteams.exe"}
# Titles of Teams' own views, not meetings
NOT_MEETINGS = {
    "microsoft teams", "chat", "activity", "calendar", "teams", "calls", "files", "apps", "onedrive",
    "notifications", "search", "settings", "assignments", "communities", "meet", "copilot", "people",
    "sharing control bar", "meeting compact view", "compact meeting view", "screen sharing toolbar",
}


def _process_name(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def teams_windows() -> list[tuple[int, str]]:
    """(hwnd, title) of every visible Teams window."""
    import win32gui
    import win32process

    found = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        name = _process_name(pid)
        if name in TEAMS_PROCESSES:
            found.append((hwnd, title))
        return True

    win32gui.EnumWindows(visit, None)
    return found


def meeting_name_from_title(title: str) -> str:
    """"Sprint Planning | Microsoft Teams" -> "Sprint Planning"; "" for Teams' own views and chats."""
    parts = [p.strip() for p in title.split("|")]
    parts = [p for p in parts if p and p.lower() not in NOT_MEETINGS and "microsoft teams" not in p.lower()]
    # chat windows look like "Chat | Person | Microsoft Teams" - skip those
    if parts and not title.lower().startswith("chat"):
        return parts[0]
    return ""


def detect_meeting_name() -> str:
    """Return the meeting name, or "" if no Teams meeting window is open."""
    try:
        titles = [title for _, title in teams_windows()]
    except Exception:
        log.exception("Could not read Teams windows")
        return ""
    log.info("Teams windows: %s", titles)
    for title in titles:
        name = meeting_name_from_title(title)
        if name:
            return name
    return ""


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    name = re.sub(r"\s+", "_", name)
    return name[:60] or "Meeting"

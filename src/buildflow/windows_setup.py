"""Persist checkout locations in the native Windows user's environment."""
import json
from pathlib import Path
import uuid


def register_environment(tools: dict[str, str], directory: Path) -> Path | None:
    """Back up replaced registry values, write user values and notify desktops."""
    import ctypes
    from ctypes import wintypes
    import winreg

    changed = {}
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                           winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        for name, value in tools.items():
            try:
                old, kind = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                old, kind = None, None
            if old != value or kind != winreg.REG_SZ:
                changed[name] = {"value": old, "type": kind}
        if not changed:
            return None
        backup = directory / ("windows-environment.buildflow-backup-" + uuid.uuid4().hex + ".json")
        with backup.open("x", encoding="utf-8") as file:
            json.dump(changed, file, ensure_ascii=False, indent=2)
            file.write("\n")
        try:
            for name in changed:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, tools[name])
        except OSError:
            for name, previous in changed.items():
                if previous["type"] is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(key, name, 0, previous["type"], previous["value"])
            raise
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                          wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT,
                                          ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
    return backup

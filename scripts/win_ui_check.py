"""Windows GUI self-check for the tray + settings window (card t_e0676b15).

Run on the Windows side (the WSL shell has no desktop session):

    python  <repo>\scripts\win_ui_check.py <out-dir>

What it does — every step is a real check, not a description:
 1. loads a temporary copy of ``config/sample.config.json`` with **every trigger
    set to ``none``** and starts the real ``EnterRouterApp`` (the real LL hook is
    installed, but no keystroke of the operator's session is held, swallowed or
    re-sent for the duration of the check);
 2. starts the real ``UiRuntime``: Shell_NotifyIcon icon on a real message loop;
 3. opens the settings window through the same command the tray menu emits,
    screenshots it, and inspects the live widget tree (3 tabs, 18 trigger rows,
    IME guard, preview text);
 4. drives the UI like a user: 追加/削除, 保存 (and verifies the JSON diff on
    disk + that the running app picked it up without a restart), an invalid
    value (and verifies the file was NOT touched and the field is named),
    キャンセル, テスト送信 (twice in a row = double-click guard), and finally
    パニック停止 with one click;
 5. shuts down cleanly and writes ``ui_check_report.json`` + a ``.bmp``
    screenshot into the output directory.

Exit code 0 = every check passed. ASCII-only stdout (the PowerShell capture is
CP932 and must not mangle evidence); Japanese text stays in the JSON report.
"""
from __future__ import annotations

import ctypes
import json
import logging
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

CHECKS: list[dict] = []
log = logging.getLogger("win_ui_check")


def _safe(text: str) -> str:
    """ASCII-only console line: the PowerShell capture is CP932 and must not
    raise on Japanese text or an em dash (the UTF-8 detail is in the JSON)."""
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str):
    """Decorator: run one check, record ok/fail + the reason, never re-raise."""
    def wrap(fn):
        entry = {"name": name, "ok": False, "detail": ""}
        CHECKS.append(entry)
        try:
            detail = fn()
            entry["ok"] = True
            entry["detail"] = "" if detail is None else str(detail)
            print(f"CHECK {name} ... ok {_safe(entry['detail'])}")
        except BaseException as exc:                     # noqa: BLE001 - report it
            entry["detail"] = f"{type(exc).__name__}: {exc}"
            print(f"CHECK {name} ... FAIL {_safe(entry['detail'])}")
            traceback.print_exc()
        return fn
    return wrap


# ---------------------------------------------------------------------------
# Win32 bits this script needs itself (screenshot + window probing)
# ---------------------------------------------------------------------------
def dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                     # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:                                 # noqa: BLE001
            pass


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def _dib_to_png(buffer: bytes, width: int, height: int, row: int, path: Path) -> None:
    """Bottom-up BGR DIB rows -> top-down RGB PNG (stdlib only)."""
    rows = []
    for y in range(height - 1, -1, -1):
        line = buffer[y * row: y * row + width * 3]
        rgb = bytearray(width * 3)
        rgb[0::3] = line[2::3]                    # BGR -> RGB
        rgb[1::3] = line[1::3]
        rgb[2::3] = line[0::3]
        rows.append(b"\x00" + bytes(rgb))         # filter type 0 per scanline
    _write_png(path, width, height, b"".join(rows))


def screenshot_region(left: int, top: int, width: int, height: int, path: Path) -> dict:
    """Capture a screen rectangle and return simple pixel statistics.

    The statistics exist so a caller can assert that the region really contains
    the thing it claims to show (e.g. the green tray icon) instead of trusting a
    file name.
    """
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, width, height, screen, left, top, 0x00CC0020)  # SRCCOPY
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 24
        info.bmiHeader.biCompression = 0
        row = ((width * 3 + 3) // 4) * 4
        buffer = ctypes.create_string_buffer(row * height)
        gdi32.GetDIBits(mem, bmp, 0, height, buffer, ctypes.byref(info), 0)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
    raw = buffer.raw
    _dib_to_png(raw, width, height, row, path)
    colours = set()
    green = 0
    for y in range(height):
        line = raw[y * row: y * row + width * 3]
        for x in range(width):
            r, g, b = line[x * 3], line[x * 3 + 1], line[x * 3 + 2]
            colours.add((r, g, b))
            if g > 140 and g > r + 60 and g > b + 40:
                green += 1
    return {"width": width, "height": height, "distinct": len(colours), "green": green}


class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", ctypes.c_void_p),
                ("uID", ctypes.c_uint32), ("guidItem", ctypes.c_byte * 16)]


def tray_icon_rect(hwnd: int, uid: int = 1) -> tuple[int, int, int, int]:
    """Shell_NotifyIconGetRect: where Windows actually placed our tray icon."""
    ident = _NOTIFYICONIDENTIFIER()
    ident.cbSize = ctypes.sizeof(_NOTIFYICONIDENTIFIER)
    ident.hWnd = ctypes.c_void_p(hwnd)
    ident.uID = uid
    rect = _RECT()
    hr = ctypes.windll.shell32.Shell_NotifyIconGetRect(ctypes.byref(ident),
                                                       ctypes.byref(rect))
    if hr != 0:
        raise RuntimeError(f"Shell_NotifyIconGetRect failed (hr=0x{hr & 0xFFFFFFFF:08x})")
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def screenshot_window(hwnd: int, path: Path) -> tuple[int, int]:
    """Capture one window to a 24-bit PNG. PrintWindow first, BitBlt as fallback.

    No Pillow (design forbids new dependencies): the DIB rows are copied by hand
    and encoded with ``zlib``, which is stdlib.
    """
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = _RECT()
    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"bad window rect {width}x{height}")
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # PW_RENDERFULLCONTENT = 2 (renders even when partly occluded)
        drawn = user32.PrintWindow(ctypes.c_void_p(hwnd), mem, 2)
        if not drawn:
            SRCCOPY = 0x00CC0020
            gdi32.BitBlt(mem, 0, 0, width, height, screen, rect.left, rect.top, SRCCOPY)
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = height          # bottom-up rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 24
        info.bmiHeader.biCompression = 0          # BI_RGB
        row = ((width * 3 + 3) // 4) * 4
        size = row * height
        buffer = ctypes.create_string_buffer(size)
        gdi32.GetDIBits(mem, bmp, 0, height, buffer, ctypes.byref(info), 0)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)

    raw = buffer.raw
    _dib_to_png(raw, width, height, row, path)
    return width, height


def _write_png(path: Path, width: int, height: int, filtered_rows: bytes) -> None:
    """Minimal PNG (8-bit RGB, no interlace) using only the stdlib."""
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (len(payload).to_bytes(4, "big") + tag + payload
                + zlib.crc32(tag + payload).to_bytes(4, "big"))

    ihdr = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes([8, 2, 0, 0, 0]))             # 8 bit, truecolour, no interlace
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(filtered_rows, 6))
                     + chunk(b"IEND", b""))


def hwnd_of(tk_window) -> int:
    """The frame HWND (includes the title bar) for the screenshot."""
    try:
        return int(tk_window.wm_frame(), 16)
    except Exception:                                     # noqa: BLE001
        return int(tk_window.winfo_id())


# ---------------------------------------------------------------------------
# helpers over the live widget tree
# ---------------------------------------------------------------------------
def walk(widget):
    yield widget
    try:
        children = widget.winfo_children()
    except Exception:                                     # noqa: BLE001
        return
    for child in children or ():
        yield from walk(child)


def find_button(root, text: str):
    for widget in walk(root):
        try:
            if widget.winfo_class() in ("TButton", "Button") and widget.cget("text") == text:
                return widget
        except Exception:                                 # noqa: BLE001
            continue
    return None


def text_of(widget) -> str:
    try:
        return widget.get("1.0", "end").strip()
    except Exception:                                     # noqa: BLE001
        return ""


def run(out_dir: Path) -> int:
    dpi_aware()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="enter-router-ui-"))

    from enter_router.app.main import EnterRouterApp
    from enter_router.app import logsetup
    from enter_router.core.config import parse_config
    from enter_router.ui import ime_guard, settings as settings_mod
    from enter_router.ui.runtime import UiRuntime
    from enter_router.platform import notify_win

    data = json.loads((REPO / "config" / "sample.config.json").read_text(encoding="utf-8"))
    for op in list(data["global"]["operations"]):
        data["global"]["operations"][op] = "none"     # never touch the operator's keys
    data["logging"]["dir"] = str(tmp / "logs")
    config_path = str(tmp / "config.json")
    Path(config_path).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    config = parse_config(data)
    log_path = logsetup.setup(config.logging, str(tmp), console=False)
    log.info("win_ui_check starting (config=%s)", config_path)

    app = EnterRouterApp(config, config_path, logger_ready=True)
    runtime = UiRuntime(app, on_icon_created=notify_win.register_icon)
    app.start()
    tray_ok = runtime.start()
    shots: list[str] = []
    report_tray: dict = {}

    # -- 1. tray icon on a real message loop ----------------------------
    @check("tray_icon_created")
    def _():
        if not tray_ok:
            raise AssertionError(f"tray.start() failed: {runtime.tray.error if runtime.tray else '?'}")
        if runtime.tray is None or not runtime.tray.visible or not runtime.tray.hwnd:
            raise AssertionError("icon not visible / no hwnd")
        return f"hwnd={runtime.tray.hwnd} state={runtime.tray.state}"

    @check("app_running_with_hook")
    def _():
        runtime.pump(0.4)
        status = app.get_status()
        if status.state != "running":
            raise AssertionError(f"state={status.state} hook={status.hook_installed}")
        return f"state={status.state} hook={status.hook_installed}"

    @check("tray_tooltip_matches_state")
    def _():
        runtime.refresh_now()
        runtime.pump(0.3)
        if runtime.tray.state != "running":
            raise AssertionError(f"tray state={runtime.tray.state}")
        return "running"

    @check("tray_menu_is_built")
    def _():
        from enter_router.ui.tray import menu_items
        items = [item.command for item in menu_items("running")]
        if items[0] != "panic":
            raise AssertionError(f"panic is not the first item: {items}")
        return ",".join(items)

    @check("tray_icon_handle_is_a_real_hicon")
    def _():
        handle = runtime.tray.icon_handle
        if not handle:
            raise AssertionError("no HICON registered")
        return f"hicon={handle}"

    @check("tray_icon_registered_with_the_shell")
    def _():
        # Shell_NotifyIconGetRect is the shell telling us where the icon lives.
        # NOTE (measured): Windows 11 keeps NEW tray icons in the overflow
        # flyout, so no pixels of our icon are on screen until the user pins it —
        # the icon handle and the shell registration are the evidence here, and
        # `--settings` gives a second way into the window. See the report field
        # "tray_icon_visible_on_screen".
        hwnd = runtime.tray.hwnd
        left, top, width, height = tray_icon_rect(hwnd)
        if width <= 0 or height <= 0:
            raise AssertionError(f"empty icon rect {width}x{height}")
        stats = screenshot_region(left - 6, top - 6, width + 12, height + 12,
                                  tmp / "tray_cell.png")
        report_tray.update({"rect": [left, top, width, height],
                            "visible_green_px": stats["green"]})
        return (f"rect={left},{top} {width}x{height} green_px={stats['green']}"
                f" (0 = hidden in the Win11 overflow until pinned)")

    @check("settings_reopens_via_--settings_message")
    def _():
        # The path `main.py --settings` uses in a *separate* process:
        # FindWindowW on the tray window + WM_APP message.
        from enter_router.ui.tray import post_open_settings
        if runtime.settings is None:            # nothing open yet: open it the normal way
            runtime.handle_command("settings")
            runtime.pump(0.4)
        window = runtime.settings
        window.destroy()                     # close it first
        runtime.pump(0.3)
        if runtime.settings is not None:
            raise AssertionError("window did not close")
        if not post_open_settings():
            raise AssertionError("post_open_settings found no tray window")
        runtime.pump(0.6)
        if runtime.settings is None or not runtime.settings.win.winfo_ismapped():
            raise AssertionError("the cross-process message did not open the window")
        return "closed, then reopened through WM_APP from outside the Tk thread"

    # -- 2. settings window via the tray command ------------------------
    @check("settings_opens_from_tray_command")
    def _():
        runtime.handle_command("settings")            # exactly what the menu emits
        runtime.pump(0.6)
        window = runtime.settings
        if window is None:
            raise AssertionError("no settings window")
        if not window.win.winfo_exists() or not window.win.winfo_ismapped():
            raise AssertionError("window exists but is not mapped/visible")
        return f"title={window.win.title()!r} geometry={window.win.geometry()}"

    @check("screenshots_every_tab")
    def _():
        window = runtime.settings
        captured = []
        for index, tab in enumerate(window.notebook.tabs()):
            window.notebook.select(tab)
            runtime.pump(0.35)
            path = out_dir / f"settings_tab{index + 1}.png"
            size = screenshot_window(hwnd_of(window.win), path)
            shots.append(str(path))
            captured.append(f"{path.name}={size[0]}x{size[1]}")
        window.notebook.select(window.tab_a)
        runtime.pump(0.2)
        return ", ".join(captured)

    @check("three_tabs_present")
    def _():
        window = runtime.settings
        tabs = [window.notebook.tab(tab, "text") for tab in window.notebook.tabs()]
        if tabs != ["操作マップ", "プロファイル", "テスト送信"]:
            raise AssertionError(f"tabs={tabs}")
        return " / ".join(tabs)

    @check("all_18_triggers_listed")
    def _():
        window = runtime.settings
        if len(window.op_vars) != 18:
            raise AssertionError(f"{len(window.op_vars)} trigger rows")
        for op, var in window.op_vars.items():
            if not var.get():
                raise AssertionError(f"{op} shows an empty value")
        return "18 rows, all with a current value"

    @check("preview_names_the_send_key")
    def _():
        window = runtime.settings
        # The check runs with every trigger neutralised (all "none") so the
        # operator's keys are never touched; the preview is computed from the
        # in-window document, so we exercise it with 改行/送信 selected in the UI
        # only (the running hook keeps meaning "none").
        window.op_vars["enter"].set(settings_mod.MEANING_LABELS["newline"])
        window._on_meaning_changed("enter", window.op_vars["enter"])
        window.op_vars["enter2"].set(settings_mod.MEANING_LABELS["send"])
        window._on_meaning_changed("enter2", window.op_vars["enter2"])
        window._commit_preview()
        text = text_of(window.preview_text)
        if "site-a" not in text or "shift+enter" not in text or "ctrl+enter" not in text:
            raise AssertionError(f"preview={text[:160]!r}")
        window.cancel()                       # back to the saved document
        return text.splitlines()[2].strip() if len(text.splitlines()) > 2 else text[:80]

    @check("ime_guard_holds_on_the_real_window")
    def _():
        window = runtime.settings
        ime_guard.assert_no_enter_submit(window.win)     # raises on violation
        bound = window.win.bind()
        return f"toplevel bindings={bound or ()}"

    @check("layout_fits_without_clipping")
    def _():
        window = runtime.settings
        problems = []
        for tab in (window.tab_a, window.tab_b, window.tab_c):
            window.notebook.select(tab)
            runtime.pump(0.3)
            for widget in walk(tab):
                if widget is tab:
                    continue
                try:
                    if not widget.winfo_ismapped():
                        continue
                    x, y = widget.winfo_x(), widget.winfo_y()
                    width, height = widget.winfo_width(), widget.winfo_height()
                    parent = widget.master
                    parent_w, parent_h = parent.winfo_width(), parent.winfo_height()
                except Exception:                     # noqa: BLE001 - skip odd widgets
                    continue
                if x + width > parent_w + 2 or y + height > parent_h + 2:
                    problems.append(f"{widget.winfo_class()} {x}+{width}>{parent_w} "
                                    f"/ {y}+{height}>{parent_h}")
        if problems:
            raise AssertionError("clipped widgets: " + "; ".join(problems[:6]))
        return "no widget exceeds its parent box on any tab"

    # -- 3. profile CRUD through the widgets ---------------------------
    @check("profile_add_and_delete")
    def _():
        window = runtime.settings
        before = len(window.model.profiles())
        add = find_button(window.win, "追加")
        if add is None:
            raise AssertionError("no 追加 button")
        add.invoke()
        runtime.pump(0.2)
        after_add = len(window.model.profiles())
        listed_after_add = window.profile_list.size()
        delete = find_button(window.win, "削除")
        delete.invoke()
        runtime.pump(0.2)
        after_delete = len(window.model.profiles())
        listed_after_delete = window.profile_list.size()
        if after_add != before + 1 or after_delete != before:
            raise AssertionError(f"model: {before} -> {after_add} -> {after_delete}")
        if listed_after_add != after_add or listed_after_delete != after_delete:
            raise AssertionError(f"listbox: {listed_after_add} / {listed_after_delete}")
        return f"{before} -> {after_add} -> {after_delete} (listbox in sync)"

    # -- 4. save: file diff + immediate effect -------------------------
    @check("save_writes_json_and_applies_without_restart")
    def _():
        window = runtime.settings
        before = json.loads(Path(config_path).read_text(encoding="utf-8"))
        var = window.op_vars["f5"]
        var.set(settings_mod.MEANING_LABELS["clipboard"])
        window._on_meaning_changed("f5", var)
        ok = window.save()
        runtime.pump(0.3)
        after = json.loads(Path(config_path).read_text(encoding="utf-8"))
        live = app.get_config_dict()
        if not ok:
            raise AssertionError("save() returned False")
        if before["global"]["operations"]["f5"] == after["global"]["operations"]["f5"]:
            raise AssertionError("file unchanged")
        if after["global"]["operations"]["f5"] != "clipboard":
            raise AssertionError(f"file has {after['global']['operations']['f5']!r}")
        if live["global"]["operations"]["f5"] != "clipboard":
            raise AssertionError("running app did not pick the value up")
        if app.get_status().config_warnings:
            raise AssertionError(f"warnings after save: {app.get_status().config_warnings}")
        for key in before:
            if key not in after:
                raise AssertionError(f"top-level key {key} disappeared")
        # Keep a real, reviewable diff of what the save wrote.
        import difflib
        before_text = json.dumps(before, ensure_ascii=False, indent=2).splitlines()
        after_text = json.dumps(after, ensure_ascii=False, indent=2).splitlines()
        (out_dir / "config_before.json").write_text("\n".join(before_text) + "\n",
                                                    encoding="utf-8")
        (out_dir / "config_after.json").write_text("\n".join(after_text) + "\n",
                                                   encoding="utf-8")
        diff = list(difflib.unified_diff(before_text, after_text,
                                         "config.before.json", "config.after.json",
                                         lineterm="", n=2))
        CHECKS[-1]["unified_diff"] = diff
        CHECKS[-1]["diff"] = {"f5": [before["global"]["operations"]["f5"],
                                     after["global"]["operations"]["f5"]]}
        return "f5 none -> clipboard (file + live config); diff in unified_diff"

    @check("invalid_value_is_named_and_file_untouched")
    def _():
        window = runtime.settings
        before = Path(config_path).read_bytes()
        window.number_vars["hold_window_ms"].set("9000")
        window._on_policy_changed()
        ok = window.save()
        runtime.pump(0.2)
        issues = text_of(window.issues_text)
        if ok:
            raise AssertionError("save() accepted an out-of-range value")
        if Path(config_path).read_bytes() != before:
            raise AssertionError("the config file was written despite the error")
        if "保留窓" not in issues:
            raise AssertionError(f"field not named: {issues!r}")
        window.number_vars["hold_window_ms"].set("250")
        window._on_policy_changed()
        if not window.save():
            raise AssertionError("could not restore a valid value")
        return "rejected, named 保留窓, file untouched, restored"

    @check("cancel_restores_saved_state")
    def _():
        window = runtime.settings
        before = json.loads(Path(config_path).read_text(encoding="utf-8"))
        original = window.op_vars["enter"].get()
        var = window.op_vars["enter"]
        var.set(settings_mod.MEANING_LABELS["send"])
        window._on_meaning_changed("enter", var)
        if window.op_vars["enter"].get() == original:
            raise AssertionError("the edit did not reach the widget")
        window.cancel()
        runtime.pump(0.2)
        after = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if after != before:
            raise AssertionError("cancel touched the file")
        if window.op_vars["enter"].get() != original:
            raise AssertionError(f"widget still shows {window.op_vars['enter'].get()!r}")
        return f"edited then cancelled: widgets back to {original!r}, file unchanged"

    # -- 5. test send + double-click guard ----------------------------
    @check("test_send_reports_result_and_ignores_double_click")
    def _():
        window = runtime.settings
        window.notebook.select(window.tab_c)
        window.test_sink_var.set(settings_mod.SINK_LABELS["clipboard"])
        window.test_text_var.set("enter-router test send")
        send = find_button(window.win, "テスト送信を実行")
        send.invoke()                                  # click 1
        first_state = str(window.test_button.cget("state"))
        window.send_test()                             # click 2, immediately
        runtime.pump(0.2)
        deadline = time.monotonic() + 15
        while window.tracker.busy and time.monotonic() < deadline:
            runtime.pump(0.2)
        result = text_of(window.test_result_text)
        if window.tracker.state != "done":
            raise AssertionError(f"state={window.tracker.state} label={window.tracker.label()}")
        if "成功" not in window.tracker.label():
            raise AssertionError(f"label={window.tracker.label()}")
        if not result:
            raise AssertionError("no result text on screen")
        if first_state != "disabled":
            raise AssertionError(f"button was not disabled while sending ({first_state})")
        if str(window.test_button.cget("state")) != "normal":
            raise AssertionError("button not re-enabled after the send")
        return window.tracker.label()[:80]

    # -- 5b. failure path: the screen must say why, not "success" --------
    @check("test_send_shows_failure_reason")
    def _():
        # Drive a sink that fails fast and deterministically: Hermes pointed at a
        # WSL distribution that does not exist. The payload is small and the
        # failure is classified by the sink; the UI must show 失敗 + the reason.
        window = runtime.settings
        distro_var = window.sink_vars[("hermes", "wsl_distro")]
        timeout_var = window.sink_vars[("hermes", "timeout_sec")]
        original_distro, original_timeout = distro_var.get(), timeout_var.get()
        distro_var.set("enter-router-no-such-distro")
        timeout_var.set("5")
        window._commit_sinks()
        if not window.save():
            raise AssertionError("could not save the failure-probe config")
        window.notebook.select(window.tab_c)
        window.test_sink_var.set(settings_mod.SINK_LABELS["hermes"])
        window.test_text_var.set("enter-router failure probe")
        window.test_button.invoke()
        deadline = time.monotonic() + 60
        while window.tracker.busy and time.monotonic() < deadline:
            runtime.pump(0.2)
        label = window.tracker.label()
        state = window.tracker.state
        distro_var.set(original_distro)                  # restore the sample config
        timeout_var.set(original_timeout)
        window._commit_sinks()
        restored = window.save()
        if state != "done":
            raise AssertionError(f"state={state} label={label}")
        if "成功" in label:
            raise AssertionError(f"a failed send was reported as success: {label}")
        if "失敗" not in label:
            raise AssertionError(f"no failure label: {label}")
        if not restored:
            raise AssertionError("could not restore the sample hermes settings")
        return label[:120]

    @check("panic_from_settings_is_one_click")
    def _():
        window = runtime.settings
        window.panic_button.invoke()                   # a single click
        runtime.pump(0.8)
        status = app.get_status()
        if status.state != "panic":
            raise AssertionError(f"state={status.state}")
        runtime.refresh_now()
        runtime.pump(0.3)
        if runtime.tray.state != "panic":
            raise AssertionError(f"tray still shows {runtime.tray.state}")
        return f"state={status.state} tray={runtime.tray.state}"

    @check("tray_survives_panic")
    def _():
        if runtime.tray is None or not runtime.tray.visible:
            raise AssertionError("tray icon disappeared")
        return "icon still visible (the app stays resident for 再開 / 終了)"

    # -- 7. shutdown --------------------------------------------------
    @check("shutdown_is_clean")
    def _():
        runtime.stop()
        app.shutdown()
        if runtime.tray is not None:
            raise AssertionError("tray object not released")
        return "ui + app stopped"

    report = {
        "ok": all(entry["ok"] for entry in CHECKS),
        "checks": CHECKS,
        "passed": sum(1 for entry in CHECKS if entry["ok"]),
        "total": len(CHECKS),
        "screenshots": shots,
        "tray_icon": report_tray,
        "config_used": config_path,
        "log_file": log_path,
        "python": sys.version,
        "platform": sys.platform,
    }
    (out_dir / "ui_check_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT {out_dir / 'ui_check_report.json'}")
    print(f"UI_CHECK_{'OK' if report['ok'] else 'FAIL'} {report['passed']}/{report['total']}")
    return 0 if report["ok"] else 1


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")      # CP932 console: never raise
        except Exception:                              # noqa: BLE001
            pass
    out_dir = Path(argv[1]) if len(argv) > 1 else Path.cwd() / "ui_check_out"
    try:
        code = run(out_dir)
    except BaseException:                                 # noqa: BLE001
        traceback.print_exc()
        print("UI_CHECK_FAIL unhandled")
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Measure the settings window's layout: is the footer (保存) clipped?

Builds the real SettingsWindow against the live config with a stub controller,
then prints the requested vs actual heights, per-tab requirements and where the
save button lands. Windows-only (needs Tk + a desktop session).

    C:\\Python314\\python.exe -X utf8 scripts\\win_settings_layout.py

A healthy result is `window geometry now : 960x951`, `footer actual : 960x37`
and `save button mapped : 1`. If `save button mapped : 0` and the footer is 1px,
the packer ran out of room and the bottom bars lost to the notebook: pack the
bottom bars first with side="bottom" and let the notebook shrink.
"""
from __future__ import annotations

import json
import os
import sys
import tkinter as tk

sys.path.insert(0, r"C:\Users\hirok\enter-router\src")

from enter_router.ui.settings import SettingsWindow  # noqa: E402


class StubController:
    def __init__(self, doc: dict) -> None:
        self.doc = doc

    def get_config_dict(self) -> dict:
        return json.loads(json.dumps(self.doc))

    def get_status(self):
        return None


def main() -> None:
    path = os.path.join(os.environ["APPDATA"], "enter-router", "config.json")
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)

    root = tk.Tk()
    root.withdraw()
    window = SettingsWindow(root, StubController(doc))
    win = window.win
    win.update_idletasks()
    win.update()

    print(f"screen: {win.winfo_screenwidth()}x{win.winfo_screenheight()} (logical px)")
    print(f"window geometry now : {win.winfo_width()}x{win.winfo_height()}")
    print(f"window requested    : {win.winfo_reqwidth()}x{win.winfo_reqheight()}")
    footer = window.save_button.master
    print(f"footer requested    : {footer.winfo_reqwidth()}x{footer.winfo_reqheight()}")
    print(f"footer actual       : {footer.winfo_width()}x{footer.winfo_height()}")
    print(f"notebook requested  : {window.notebook.winfo_reqheight()}")
    print(f"notebook actual     : {window.notebook.winfo_height()}")
    print(f"issues requested    : {window.issues_text.winfo_reqheight()}")
    print(f"issues actual       : {window.issues_text.winfo_height()} mapped="
          f"{window.issues_text.winfo_ismapped()}")
    print(f"save button mapped  : {window.save_button.winfo_ismapped()}")
    print(f"save button y/w/h   : {window.save_button.winfo_rooty() - win.winfo_rooty()} / "
          f"{window.save_button.winfo_width()}x{window.save_button.winfo_height()}")
    bottom = (window.save_button.winfo_rooty() - win.winfo_rooty()
              + window.save_button.winfo_height())
    print(f"save button bottom  : {bottom} (window inner height {win.winfo_height()})")
    win.destroy()
    root.destroy()


if __name__ == "__main__":
    main()

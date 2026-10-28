"""Tray + settings UI (frontend card t_e0676b15).

Constraints (design.md §7):
- Tray icon: pywin32 Shell_NotifyIcon (no pystray/Pillow — not installed).
- Settings window: stdlib tkinter only, ONE window, 3 tabs.
- Talks to the app exclusively through app.controller.AppController.
"""

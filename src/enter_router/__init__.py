"""enter-router: Win11 resident Enter-policy router.

Layout:
    core/      pure logic, stdlib only, unit-tested on Linux (pytest in WSL)
    platform/  Windows adapters (pywin32/ctypes); never imported on Linux tests
    app/       composition root (main entry, controller, watchdog, config watcher)
    ui/        tray + settings window (tkinter); frontend card scope
"""

__version__ = "0.1.0"

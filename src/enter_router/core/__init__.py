"""Pure logic layer.

Hard rule: this package imports ONLY the Python standard library.
No win32/ctypes/globals anything OS-specific. This is what makes
`python3 -m pytest tests/` pass on Linux (WSL).
"""

"""Browser-URL selection from an accessibility-node snapshot (pure, stdlib only).

Why this module exists (measured on hardware, 2026-10-28): in Firefox the URL is
NOT on the root MSAA object. ``AccessibleObjectFromWindow(hwnd, OBJID_CLIENT)``
returns the APPLICATION node whose ``accValue`` is empty; the live URL sits on a
child (the address bar, measured as role PROGRESSBAR with
``accValue='claude.ai/chat/<id>'``) and, for inactive tabs, on each tab's
DOCUMENT node. Reading only the root therefore always yields ``None`` and every
URL-scoped profile silently never matches.

The selection is deliberately evidence-only (design §4.3 “never guess”):

1. a DOCUMENT node whose ``accName`` equals the active page title,
2. else a focused DOCUMENT node,
3. else the address bar (a scheme-less ``host/path`` value) -> ``https://`` +
   value. Firefox trims ``https://`` from the address bar display; that is the
   only normalization applied, and only to a *displayed address*, never to a
   tab title.
4. otherwise ``None``.

An “any URL in the tree” fallback is intentionally absent: other tabs' DOCUMENT
nodes are in the same tree, and returning one of those would scope a profile by
the wrong tab — the single failure mode this module must not have.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# MSAA role / state constants (IAccessible::get_accRole / get_accState)
ROLE_DOCUMENT = 0x0000000F
STATE_FOCUSED = 0x00000004

#: Window-title suffixes browsers append to the page title. Longest first so
#: " — LibreWolf" cannot be half-eaten by a shorter entry.
BROWSER_TITLE_SUFFIXES = (
    " — Mozilla Firefox",
    " - Mozilla Firefox",
    " — LibreWolf",
    " - LibreWolf",
    " - Google Chrome",
    " - Chromium",
    " - Microsoft Edge",
    " - Brave",
    " - Vivaldi",
    " - Opera",
)
PRIVATE_SUFFIXES = (" (プライベートブラウジング)", " — プライベートブラウジング",
                    " (Private Browsing)", " - Private Browsing")

MAX_URL_LEN = 2048

#: ``host.tld[:port][/path]`` with no scheme — the address bar's displayed form.
_TRIMMED_URL = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._~%\[\]-]*)\.[A-Za-z]{2,}(?::\d{1,5})?(?:[/?#][^\s]*)?$")


@dataclass(frozen=True)
class AccNode:
    """One accessibility node, flattened (no COM pointers leave the adapter)."""
    role: int = 0
    state: int = 0
    name: str = ""
    value: str = ""


def is_absolute_url(value: str | None) -> bool:
    """``scheme://...`` — the form a DOCUMENT node and Chrome's root report."""
    if not value:
        return False
    v = value.strip()
    return "://" in v and len(v) < MAX_URL_LEN and " " not in v


def is_trimmed_url(value: str | None) -> bool:
    """The address bar's displayed form (Firefox hides ``https://``)."""
    if not value:
        return False
    v = value.strip()
    if not v or len(v) >= MAX_URL_LEN or " " in v or "://" in v:
        return False
    return bool(_TRIMMED_URL.match(v))


def page_title_of(window_title: str | None) -> str:
    """``'Greeting - Claude — Mozilla Firefox'`` -> ``'Greeting - Claude'``."""
    title = (window_title or "").strip()
    for suffix in PRIVATE_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    for suffix in BROWSER_TITLE_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
            break
    return title


def select_document_url_node(nodes: list[AccNode] | tuple[AccNode, ...],
                             page_title: str | None = None) -> AccNode | None:
    """The ACTIVE tab's URL-bearing node out of a flattened snapshot (or None)."""
    title = (page_title or "").strip().lower()
    absolute = [(n, (n.value or "").strip()) for n in nodes if is_absolute_url(n.value)]

    if title:
        for node, _url in absolute:
            if (node.name or "").strip().lower() == title:
                return node
    for node, _url in absolute:
        if node.role == ROLE_DOCUMENT and node.state & STATE_FOCUSED:
            return node
    for node in nodes:
        if is_trimmed_url(node.value):
            return node
    return None


def url_of_node(node: AccNode | None) -> str | None:
    """The URL string a selected node stands for (scheme restored when trimmed)."""
    if node is None:
        return None
    value = (node.value or "").strip()
    if is_absolute_url(value):
        return value
    if is_trimmed_url(value):
        return "https://" + value
    return None


def select_document_url(nodes: list[AccNode] | tuple[AccNode, ...],
                        page_title: str | None = None) -> str | None:
    """Pick the ACTIVE tab's URL out of a flattened accessibility snapshot."""
    return url_of_node(select_document_url_node(nodes, page_title))


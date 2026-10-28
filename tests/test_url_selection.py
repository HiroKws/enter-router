"""Tests for the pure browser-URL selection (core/browser_url.py).

These exist because of a measured defect: the Windows adapter used to read only
the ROOT accessibility object's accValue. In Firefox the root is the APPLICATION
node (empty accValue) and the URL lives on children, so every URL-scoped profile
silently never matched. The policy that decides *which* child is the ACTIVE tab
lives here so it can be tested without Windows.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from enter_router.core.browser_url import (  # noqa: E402
    ROLE_DOCUMENT, STATE_FOCUSED, AccNode, is_absolute_url, is_trimmed_url,
    page_title_of, select_document_url)

ROLE_APPLICATION = 0x0E
ROLE_PROGRESSBAR = 0x2E


def doc(name, value, state=0):
    return AccNode(role=ROLE_DOCUMENT, state=state, name=name, value=value)


def address_bar(value):
    return AccNode(role=ROLE_PROGRESSBAR, state=0x40110400, name="Search or enter address",
                   value=value)


class TestUrlPredicates(unittest.TestCase):
    def test_absolute(self):
        self.assertTrue(is_absolute_url("https://claude.ai/chat/1"))
        self.assertFalse(is_absolute_url("about:newtab"))   # scheme:opaque is not a URL we scope on
        self.assertFalse(is_absolute_url("claude.ai/chat/1"))
        self.assertFalse(is_absolute_url(""))
        self.assertFalse(is_absolute_url(None))
        self.assertFalse(is_absolute_url("https://two words/x"))

    def test_trimmed(self):
        self.assertTrue(is_trimmed_url("claude.ai/chat/af1d59e2-83cc"))
        self.assertTrue(is_trimmed_url("x.com/home"))
        self.assertTrue(is_trimmed_url("localhost.example.jp:8443/a?b=c"))
        self.assertFalse(is_trimmed_url("about:newtab"))        # no TLD
        self.assertFalse(is_trimmed_url("Google で検索"))        # placeholder text
        self.assertFalse(is_trimmed_url("https://x.com/"))       # absolute, other rule
        self.assertFalse(is_trimmed_url(""))


class TestPageTitle(unittest.TestCase):
    def test_firefox_suffix(self):
        self.assertEqual(page_title_of("Greeting - Claude — Mozilla Firefox"),
                         "Greeting - Claude")

    def test_chrome_suffix(self):
        self.assertEqual(page_title_of("Greeting - Claude - Google Chrome"),
                         "Greeting - Claude")

    def test_private_window_suffix(self):
        self.assertEqual(
            page_title_of("Greeting - Claude — Mozilla Firefox (プライベートブラウジング)"),
            "Greeting - Claude")

    def test_no_suffix(self):
        self.assertEqual(page_title_of("Greeting - Claude"), "Greeting - Claude")
        self.assertEqual(page_title_of(None), "")


class TestSelectDocumentUrl(unittest.TestCase):
    """The measured Firefox tree: root APPLICATION, address bar, per-tab DOCUMENTs."""

    def snapshot(self):
        return [
            AccNode(role=ROLE_APPLICATION, state=0x00160040,
                    name="Greeting - Claude — Mozilla Firefox", value=""),
            address_bar("claude.ai/chat/af1d59e2-83cc"),
            doc("AI Just BROKE Computer Science", "https://mail.google.com/mail/u/0/"),
            doc("ホーム / X", "https://x.com/home"),
            doc("Greeting - Claude", ""),           # active tab: NO accValue
        ]

    def test_address_bar_is_used_for_the_active_tab(self):
        # The decisive regression: never one of the other tabs' URLs.
        url = select_document_url(self.snapshot(), "Greeting - Claude")
        self.assertEqual(url, "https://claude.ai/chat/af1d59e2-83cc")

    def test_active_document_wins_when_it_carries_a_url(self):
        nodes = self.snapshot() + [
            doc("Greeting - Claude", "https://claude.ai/chat/active")]
        nodes[1] = address_bar("claude.ai/chat/stale")
        self.assertEqual(select_document_url(nodes, "Greeting - Claude"),
                         "https://claude.ai/chat/active")

    def test_focused_document_when_the_title_is_unknown(self):
        nodes = [doc("Other", "https://other.example/a"),
                 AccNode(role=ROLE_DOCUMENT, state=STATE_FOCUSED, name="Live",
                         value="https://live.example/b")]
        self.assertEqual(select_document_url(nodes, ""), "https://live.example/b")

    def test_other_tabs_are_never_used_as_a_fallback(self):
        nodes = [doc("AI Just BROKE Computer Science", "https://mail.google.com/mail/u/0/"),
                 doc("ホーム / X", "https://x.com/home")]
        self.assertIsNone(select_document_url(nodes, "Greeting - Claude"))

    def test_empty_snapshot(self):
        self.assertIsNone(select_document_url([], "Greeting - Claude"))


if __name__ == "__main__":
    unittest.main()

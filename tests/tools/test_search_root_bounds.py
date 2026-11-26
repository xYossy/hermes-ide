"""Root bounds for content and filename search.

On 2026-11-26 a content search rooted at ``/`` ran for two hours after the
session that started it had ended: 88% of one core, four million reads, 6 TB
through the page cache, output going into a pipe nobody owned. Nothing in the
search tier stopped a root with no edge.

Two contracts follow, and these tests are those contracts, not snapshots:

1. The filesystem anchor is refused on every engine. Even ripgrep scans the
   whole device when matches are rare, so the ignore semantics do not save it.
2. The shell grep fallback gets the same broad-root bound the filename walk
   already had. grep has no ignore semantics, so a root at or above ``$HOME``
   has nothing to shorten it.

These drive the real methods through the real local terminal backend.
"""

import os

import pytest

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


def _ops(root):
    return ShellFileOperations(LocalEnvironment(cwd=str(root)), cwd=str(root))


def _grep_only(ops):
    """Pretend ripgrep is absent so the shell fallback is the only engine left."""
    ops._has_command = lambda cmd: cmd == "grep"


def _content(ops, pattern, path):
    return ops._search_content(pattern, str(path), None, 10, 0, "content", 0)


def test_content_root_search_refused_with_ripgrep(tmp_path):
    res = _content(_ops(tmp_path), "needle", "/")
    assert res.error is not None
    assert "filesystem root" in res.error


def test_content_root_search_refused_without_rg(tmp_path):
    ops = _ops(tmp_path)
    _grep_only(ops)
    res = _content(ops, "needle", "/")
    assert res.error is not None
    # The anchor refusal wins, so no shell-out happens at all.
    assert "filesystem root" in res.error


@pytest.mark.skipif(os.path.expanduser("~") == os.sep, reason="home is the filesystem root")
def test_grep_fallback_refuses_broad_root(tmp_path):
    ops = _ops(tmp_path)
    _grep_only(ops)
    res = _content(ops, "needle", os.path.expanduser("~"))
    assert res.error is not None
    assert "without ripgrep" in res.error


def test_grep_fallback_allows_narrow_root(tmp_path):
    (tmp_path / "a.txt").write_text("needle one\n")
    (tmp_path / "b.txt").write_text("needle two\n")
    ops = _ops(tmp_path)
    _grep_only(ops)
    res = _content(ops, "needle", tmp_path)
    assert res.error is None
    assert len(res.matches) == 2


def test_relative_dot_root_still_allowed(tmp_path):
    (tmp_path / "a.txt").write_text("needle here\n")
    ops = _ops(tmp_path)
    _grep_only(ops)
    res = _content(ops, "needle", ".")
    assert res.error is None
    assert len(res.matches) == 1


def test_file_search_refuses_filesystem_root(tmp_path):
    res = _ops(tmp_path)._search_files("*.txt", "/", 10, 0)
    assert res.error is not None
    assert "filesystem root" in res.error

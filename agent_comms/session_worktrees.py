"""A child session's own git worktree, on its node (DESIGN_NOTES "Session host: parent and child sessions").

A child session with `isolation: worktree` runs in a new git worktree made from the HEAD of its parent's checkout, so
what the parent has not committed stays out of it. The dispatcher's helpers do the work, with their checks
(dispatch.run_worktree_path, ensure_run_worktree, removable_run_worktree): the directory lies under `[dispatch]
worktree_dir` (default ~/agent-comms-worktrees), beside the dispatcher's run worktrees of the same checkout, and is
named `session-<session id>`, which the dispatcher's own pruning never takes for one of its `<agent>-t<thread>`
directories.

- Created when the session is first opened on the node, reused when it is opened again (a node restart). A directory
  there that is not a worktree of this checkout is refused, never reused or overwritten.
- Removed only when the session has ended AND the dispatcher's safe-removal rule holds: nothing uncommitted or
  untracked, and its HEAD is on some branch or remote-tracking ref. Otherwise it stays where it is, for the human.
- Nothing here takes a path from the hub: the hub says `worktree`, and the path follows from this machine's settings
  and the session id.
"""
from __future__ import annotations

import os
import subprocess

from .core import Conflict
from .dispatch import (DispatchConfig, _git_common_dir, _within, ensure_run_worktree, protected_paths,
                       removable_run_worktree, run_worktree_path)
from .providers import SESSION_ID_RE, validate_session_id

PREFIX = "session-"


def _check_root(config: DispatchConfig) -> str:
    """The worktree root, held to what [dispatch] parallel_worktrees demands of it at load: not the home directory,
    nothing that contains it, nothing that overlaps a protected location."""
    root, user = config.worktree_root(), os.path.realpath(os.path.expanduser("~"))
    protected = list(protected_paths()[0]) + list(config.protected_dirs)
    if (not os.path.isabs(root) or os.path.normpath(root) != root or root == "/" or root == user
            or _within(user, root) or any(_within(root, d) or _within(d, root) for d in protected)):
        raise Conflict("[dispatch] worktree_dir may not be the home directory, contain it or overlap a protected "
                       "location, so no session worktree is made there")
    return root


def path_for(config: DispatchConfig, checkout: str, session_id: str) -> str:
    """Where this session's worktree of `checkout` is: beside the dispatcher's run worktrees of that checkout."""
    repo = os.path.dirname(run_worktree_path(_check_root(config), checkout, "session", 0))
    return os.path.join(repo, PREFIX + validate_session_id(session_id))


def ensure(config: DispatchConfig, checkout: str, session_id: str) -> str:
    """Create the session's worktree, detached at the checkout's HEAD, or reuse the one it has. Conflict: the
    checkout is not a Git repository, the path is taken by something else, or git refused."""
    return ensure_run_worktree(checkout, path_for(config, checkout, session_id))


def remove_if_safe(checkout: str, path: str) -> str:
    """After the session ended: "removed", "gone" (there is nothing there any more), or "kept". Only a directory
    named as `path_for` names them, that is a worktree of this checkout and holds nothing unique
    (removable_run_worktree), is removed, and by git itself (`git worktree remove`, which refuses a dirty tree)."""
    name = os.path.basename(path)
    if not name.startswith(PREFIX) or not SESSION_ID_RE.fullmatch(name[len(PREFIX):]) or not os.path.isabs(path):
        return "kept"
    if not os.path.lexists(path):
        return "gone"
    if os.path.islink(path) or not os.path.isdir(path):
        return "kept"
    common = _git_common_dir(checkout)
    if common is None or _git_common_dir(path) != common or not removable_run_worktree(path):
        return "kept"
    try:
        r = subprocess.run(["git", "-C", checkout, "worktree", "remove", path], capture_output=True, text=True,
                           timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return "kept"
    return "removed" if r.returncode == 0 and not os.path.lexists(path) else "kept"

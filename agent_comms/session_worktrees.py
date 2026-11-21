"""A child session's own git worktree, on its node (DESIGN_NOTES "Session host: parent and child sessions").

A child session with `isolation: worktree` runs in a new git worktree made from the HEAD of its parent's checkout, so
what the parent has not committed stays out of it. The dispatcher's helpers do the work, with their checks
(dispatch.run_worktree_path, ensure_run_worktree, removable_run_worktree): the directory lies under `[dispatch]
worktree_dir` (default ~/agent-comms-worktrees), beside the dispatcher's run worktrees of the same checkout, and is
named `session-<session id>`, which the dispatcher's own pruning never takes for one of its `<agent>-t<thread>`
directories.

- Created when the session is first opened on the node, reused when it is opened again (a node restart). Only a
  directory that git itself lists as a linked worktree of this checkout, at exactly this path with no symlink in
  it, is reused: anything else there is refused, never reused or overwritten. (That a directory is inside the
  repository is not enough: git finds the checkout's own `.git` from any plain folder under it.)
- Removed only when the session has ended AND nothing unique is in it: the dispatcher's rule (nothing uncommitted
  or untracked, HEAD on some branch or remote-tracking ref) and, beyond it, no IGNORED file either. A build output
  or a local `.env` is ignored and is still somebody's work; neither `git status` nor an unforced `git worktree
  remove` looks at ignored files. Otherwise it stays where it is, for the human.
- Nothing here takes a path from the hub: the hub says `worktree`, and the path follows from this machine's settings
  and the session id.
"""
from __future__ import annotations

import os
import subprocess

from .core import Conflict
from .dispatch import (DispatchConfig, _within, ensure_run_worktree, protected_paths, removable_run_worktree,
                       run_worktree_path)
from .providers import SESSION_ID_RE, validate_session_id

PREFIX = "session-"
NOT_OURS = "the session's worktree path exists but is not a linked worktree of this checkout registered at that path"


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


def _git(cwd: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def registered(checkout: str, path: str) -> bool:
    """`path` IS a linked worktree of this checkout, now:
    - the path is canonical: a real directory, itself and every ancestor free of symlinks;
    - git lists it (`git worktree list`, never the main one) and does not call the entry `prunable`. A stale entry
      whose directory was deleted stays listed until it is pruned, and any folder later made at that path would
      otherwise pass;
    - the directory itself answers as that worktree: its git top level is the path, its git directory is one of the
      checkout's `worktrees/` entries and not the checkout's own. (From a plain folder git would find some
      repository above it and answer with that one's top level.)"""
    if os.path.islink(path) or not os.path.isdir(path) or os.path.realpath(path) != path:
        return False
    r = _git(checkout, "worktree", "list", "--porcelain", "-z")
    if r is None or r.returncode != 0:
        return False
    entries = [[field for field in block.split("\0") if field] for block in r.stdout.split("\0\0")]
    listed = False
    for entry in [e for e in entries if e][1:]:         # the first entry is the main worktree
        where = next((f[len("worktree "):] for f in entry if f.startswith("worktree ")), None)
        stale = any(f == "prunable" or f.startswith("prunable ") for f in entry)
        if where is not None and os.path.realpath(where) == path and not stale:
            listed = True
    if not listed:
        return False
    top = _git(path, "rev-parse", "--show-toplevel")
    own = _git(path, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    main = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if any(x is None or x.returncode != 0 for x in (top, own, main)):
        return False
    dirs = [os.path.realpath(d) for d in own.stdout.splitlines() if d.strip()]
    common = os.path.realpath(main.stdout.strip())
    return (os.path.realpath(top.stdout.strip()) == path and len(dirs) == 2 and dirs[1] == common
            and dirs[0] != common and os.path.dirname(dirs[0]) == os.path.join(common, "worktrees"))


def ensure(config: DispatchConfig, checkout: str, session_id: str) -> str:
    """Create the session's worktree, detached at the checkout's HEAD, or reuse the one it has. Conflict: the
    checkout is not a Git repository, the path is taken by anything that is not this checkout's registered linked
    worktree at exactly that path, a symlink is on the way, or git refused."""
    path = path_for(config, checkout, session_id)
    if os.path.lexists(path):
        if not registered(checkout, path):
            raise Conflict(NOT_OURS)
        return path
    made = ensure_run_worktree(checkout, path)      # refuses a parent directory reached through a symlink
    if not registered(checkout, made):
        raise Conflict(NOT_OURS)
    return made


def holds_nothing(path: str) -> bool:
    """Nothing of anybody's is in this worktree: nothing modified, staged, untracked or IGNORED, and its HEAD is on a
    branch or remote-tracking ref (dispatch.removable_run_worktree). Every untracked and ignored file is listed, one
    by one: a directory git would otherwise sum up as one line, or skip, counts."""
    r = _git(path, "status", "--porcelain", "--ignored", "--untracked-files=all")
    return r is not None and r.returncode == 0 and not r.stdout.strip() and removable_run_worktree(path)


def remove_if_safe(checkout: str, path: str) -> str:
    """After the session ended: "removed", "gone" (there is nothing there any more), or "kept". Only a directory
    named as `path_for` names them, that git lists as a linked worktree of this checkout at exactly that path and
    that holds nothing (`holds_nothing`, ignored files included), is removed, and by git itself."""
    name = os.path.basename(path)
    if not name.startswith(PREFIX) or not SESSION_ID_RE.fullmatch(name[len(PREFIX):]) or not os.path.isabs(path):
        return "kept"
    if not os.path.lexists(path):
        return "gone"
    if not registered(checkout, path) or not holds_nothing(path):
        return "kept"
    r = _git(checkout, "worktree", "remove", path, timeout=60)
    return "removed" if r is not None and r.returncode == 0 and not os.path.lexists(path) else "kept"

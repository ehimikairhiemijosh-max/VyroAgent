"""
Galaxy Gamez - Git Sync Layer
Both the Render command server AND the GitHub Actions posting job read/write
the same JSON files. Since there's no separate database, git itself is the
source of truth. This module keeps the local working copy in sync before
every read and pushes after every write, with a retry-on-conflict pattern.
"""
import os
import subprocess
import time

from config import GITHUB_TOKEN, GITHUB_REPOSITORY

REPO_DIR = "/tmp/repo" if os.environ.get("RENDER") else "."


def _run(cmd, cwd=None, timeout=20):
    try:
        result = subprocess.run(
            cmd, cwd=cwd or REPO_DIR, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        print(f"git_sync: command timed out: {' '.join(cmd)}")
        return 1, "", "timeout"


def ensure_repo():
    """On Render, clone the repo into /tmp on first boot. Locally (GitHub
    Actions), the repo is already checked out, so this is a no-op."""
    if not os.environ.get("RENDER"):
        return
    if os.path.exists(os.path.join(REPO_DIR, ".git")):
        return
    print(f"git_sync: cloning {GITHUB_REPOSITORY} into {REPO_DIR}...")
    url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPOSITORY}.git"
    result = subprocess.run(["git", "clone", url, REPO_DIR], capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(os.path.join(REPO_DIR, ".git")):
        print(
            f"git_sync: CLONE FAILED - check that GITHUB_REPOSITORY "
            f"('{GITHUB_REPOSITORY}') exactly matches your repo's "
            f"username/reponame and that GITHUB_TOKEN is valid.\n"
            f"Git said: {result.stderr.strip()}"
        )
        return
    _run(["git", "config", "user.name", "render-bot"])
    _run(["git", "config", "user.email", "render-bot@galaxygamez.local"])


def _unpushed_commit_count():
    """How many local commits on HEAD haven't been pushed to origin/main."""
    code, out, err = _run(["git", "rev-list", "origin/main..HEAD", "--count"])
    if code != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def pull_latest():
    ensure_repo()

    # Guard 1: uncommitted working-directory changes (e.g. a flag just set
    # but not yet committed) - a hard reset would wipe them before they get
    # a chance to be committed.
    code, out, err = _run(["git", "status", "--porcelain"])
    if out.strip():
        return

    # Refresh what origin/main actually has before comparing against it.
    _run(["git", "fetch", "origin", "main"])

    # Guard 2 (the fix): a LOCAL COMMIT that was made but failed to push
    # (e.g. it collided with a GitHub Actions commit landing around the
    # same time). A hard reset here would silently destroy that commit -
    # this is what was causing the Telegram update offset to get stuck and
    # the nudge/"Feed refreshed" messages to keep re-firing every cycle.
    # Instead of resetting, try to get the unpushed commit onto origin first.
    if _unpushed_commit_count() > 0:
        print("git_sync: found unpushed local commit(s), pushing instead of resetting.")
        code, out, err = _run(["git", "push", "origin", "main"])
        if code == 0:
            return

        print("git_sync: push rejected, rebasing onto origin/main.")
        code, out, err = _run(["git", "rebase", "origin/main"])
        if code == 0:
            code, out, err = _run(["git", "push", "origin", "main"])
            if code == 0:
                return

        # Rebase/push still failing - abort any half-finished rebase and
        # skip the reset this cycle (so the commit isn't destroyed). Will
        # retry again automatically next cycle.
        _run(["git", "rebase", "--abort"])
        print("git_sync: could not push or rebase unpushed commit, will retry next cycle.")
        return

    _run(["git", "reset", "--hard", "origin/main"])


def push_changes(message, files):
    """files: list of filenames (relative to repo root) to commit. Retries
    once on conflict by fetching + rebasing onto origin/main and re-applying
    (git handles this fine since each JSON write is a full-file overwrite
    from load->modify->save). Rebasing here (not resetting) is what protects
    the commit this function just made instead of it getting thrown away."""
    for attempt in range(2):
        _run(["git", "add"] + files)
        code, out, err = _run(["git", "commit", "-m", message])
        if "nothing to commit" in (out + err):
            return True

        code, out, err = _run(["git", "push", "origin", "main"])
        if code == 0:
            return True

        # push rejected - fetch + rebase onto the latest remote, then retry.
        _run(["git", "fetch", "origin", "main"])
        code, out, err = _run(["git", "rebase", "origin/main"])
        if code != 0:
            _run(["git", "rebase", "--abort"])
            print(f"git_sync: rebase conflict pushing {files}, aborting this attempt.")
            return False
        time.sleep(1)

    return False

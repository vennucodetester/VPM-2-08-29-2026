import datetime
import os
import re
import shutil
import subprocess
import sys

from utils.app_support import append_support_event, recovery_dir
from utils.version_info import FALLBACK_VERSION, install_dir


def run_git(*args, check=True):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        ["git", "-C", install_dir(), *args],
        text=True,
        capture_output=True,
        creationflags=flags,
    )
    if check and result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(output or f"git {' '.join(args)} failed")
    return result


def current_branch():
    result = run_git("branch", "--show-current")
    return result.stdout.strip()


def existing_versions():
    result = run_git("tag", "--list", "rev-*", "--sort=v:refname", check=False)
    versions = []
    for line in result.stdout.splitlines():
        name = line.strip()
        match = re.fullmatch(r"rev-(\d{4}\.\d{2}\.\d{2})-r(\d+)", name)
        if match:
            versions.append((match.group(1), int(match.group(2)), name))
    return versions


def next_version():
    today = datetime.date.today().strftime("%Y.%m.%d")
    today_revs = [rev for day, rev, _ in existing_versions() if day == today]
    return f"{today}-r{max(today_revs, default=0) + 1}"


def copy_recovery_snapshot(version):
    snapshot = os.path.join(recovery_dir(), f"{datetime.datetime.now():%Y%m%d-%H%M%S}-before-{version}")
    ignored = {".git", ".git-helper", "__pycache__", "usage_logs"}
    os.makedirs(snapshot, exist_ok=True)
    for name in os.listdir(install_dir()):
        if name in ignored or name.endswith(".pyc"):
            continue
        source = os.path.join(install_dir(), name)
        target = os.path.join(snapshot, name)
        if os.path.isdir(source):
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target)
    return snapshot


def ensure_repo():
    if not os.path.isdir(os.path.join(install_dir(), ".git")):
        run_git("init", "-b", "main")
        append_support_event("repo_initialized")


def rev_up():
    ensure_repo()
    branch = current_branch()
    if branch != "main":
        raise RuntimeError(f"Rev Up only runs on main. Current branch: {branch or '(detached)'}")

    status = run_git("status", "--porcelain").stdout.strip()
    if not status:
        print(f"No changes to save. Current fallback revision is {FALLBACK_VERSION}.")
        append_support_event("rev_up_no_changes")
        return 0

    version = next_version()
    snapshot = copy_recovery_snapshot(version)
    run_git("add", ".")
    run_git("commit", "-m", f"Rev {version}")
    run_git("tag", f"rev-{version}")
    append_support_event("rev_up_complete", version=version, recovery=snapshot)
    print(f"Saved Rev {version}")
    print(f"Recovery snapshot: {snapshot}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(rev_up())
    except Exception as exc:
        append_support_event("rev_up_failed", error=str(exc))
        print(f"Rev Up failed: {exc}")
        raise SystemExit(1)

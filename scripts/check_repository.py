"""Reject runtime files and common credential formats in Git's index.

Only file names and rule names are printed, never matched credential values.
This focused check complements review; it is not a universal secret detector.
"""

import re
import subprocess
from pathlib import PurePosixPath


PATTERNS = {
    "Telegram token": re.compile(rb"\b\d{6,12}:[A-Za-z0-9_-]{35}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{35,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "private key": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
}
FORBIDDEN_DIRS = {".venv", "__pycache__", "data", "backups", "dist"}


def check_repository() -> list[str]:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode("utf-8").split("\0")
    issues = []
    for name in filter(None, paths):
        path = PurePosixPath(name)
        if (FORBIDDEN_DIRS.intersection(path.parts) or path.name == ".bot.lock"
                or (path.name.startswith(".env") and path.name != ".env.example")
                or path.suffix in {".sqlite3", ".sqlite", ".db", ".pyc", ".log"}
                or ".log." in path.name):
            issues.append(f"{name}: runtime or private file must not be tracked")
        content = subprocess.check_output(["git", "show", f":{name}"])
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                issues.append(f"{name}: possible {label}")
    return issues


if __name__ == "__main__":
    found = check_repository()
    for issue in found:
        print(issue)
    if found:
        raise SystemExit(1)
    print("Repository check passed: no runtime files or recognized credentials in tracked files.")

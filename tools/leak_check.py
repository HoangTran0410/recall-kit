#!/usr/bin/env python3
"""Fail if a tracked file contains something that looks private.

    python tools/leak_check.py            # scan tracked files
    LEAK_EXTRA='acme|internal-host' python tools/leak_check.py   # plus your own regex

Patterns are assembled from pieces so this file does not match itself.
"""
import os
import re
import subprocess
import sys

SLASH = "/"
PATTERNS = {
    "home directory path": SLASH + "Users" + SLASH + "|" + SLASH + "home" + SLASH + "|Desk" + "top",
    "email address": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}",
    "IPv4 address": r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])",
    "secret-looking assignment": r"(?i)(?:api[_-]?key|secret|passw(?:or)?d|tok" + r"en|bearer)\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{16,}",
}


def main() -> int:
    extra = os.environ.get("LEAK_EXTRA", "")
    if extra:
        PATTERNS["LEAK_EXTRA"] = extra
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
    bad = 0
    for path in filter(None, files):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for no, line in enumerate(lines, 1):
            for name, pattern in PATTERNS.items():
                if re.search(pattern, line):
                    bad += 1
                    print("%s:%d: %s: %s" % (path, no, name, line.strip()[:120]))
    print("leak check: %d problem(s) in %d files" % (bad, len(list(filter(None, files)))))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

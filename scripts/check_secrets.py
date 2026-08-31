"""Offline pre-commit check. Prints filenames/categories, never matched secret values.

Checks the current working tree, not Git history. This is a lightweight guard,
not a replacement for reviewing the staged diff or rotating exposed credentials.
"""

import os
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

from fortross.settings import Settings

PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "LinkedIn session": re.compile(rb"\bAQED[A-Za-z0-9_-]{70,}"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}


def main() -> int:
    try:
        settings = Settings()
    except Exception:
        print(
            "Cannot load local settings for the configured-secret scan. Check configuration first."
        )
        return 1
    secrets = {
        value.strip('"').encode()
        for value in (
            # Still scan an obsolete local key, even though the API no longer loads it.
            os.environ.get("API_KEY") or dotenv_values(".env").get("API_KEY") or "",
            settings.linkedin_li_at,
            settings.linkedin_jsessionid,
            settings.turso_auth_token,
        )
        if len(value.strip('"')) >= 8
    }
    files = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        )
        .decode()
        .split("\0")
    )
    failures = []
    scanned = 0
    for filename in sorted(set(files) - {""}):
        path = Path(filename)
        if path.is_symlink():
            failures.append((filename, "symlink requires manual review"))
            continue
        if not path.is_file():
            continue
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", filename], check=False
        ).returncode
        if ignored == 0:
            failures.append((filename, "tracked despite ignore rule"))
        data = path.read_bytes()
        scanned += 1
        if any(secret in data for secret in secrets):
            failures.append((filename, "configured secret"))
        for category, pattern in PATTERNS.items():
            if pattern.search(data):
                failures.append((filename, category))
    for filename, category in failures:
        print(f"FAIL: {filename}: {category}")
    print(f"Checked {scanned} Git-visible files; {len(failures)} findings.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read only the two required session cookies, without logging the supplied header."""

import re
from pathlib import Path

MAX_COOKIE_BYTES = 65_536


def parse_cookie_header(header: str) -> tuple[str, str]:
    header = header.strip()
    if header.lower().startswith("cookie:"):
        header = header[7:].strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in header):
        raise ValueError("Cookie input must be one Cookie header value, without control characters")
    found: dict[str, str] = {}
    for item in header.split(";"):
        name, separator, value = item.strip().partition("=")
        if name not in {"li_at", "JSESSIONID"}:
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if not separator or not re.fullmatch(r"[A-Za-z0-9_:=./+%-]+", value):
            raise ValueError("A required session cookie is empty or malformed")
        if name in found and found[name] != value:
            raise ValueError("Cookie input contains conflicting session cookie values")
        found[name] = value
    if not all(name in found for name in ("li_at", "JSESSIONID")):
        raise ValueError("Cookie input must contain both li_at and JSESSIONID")
    return found["li_at"], found["JSESSIONID"]


def read_cookie_file(filename: str) -> tuple[str, str]:
    try:
        with Path(filename).open("rb") as handle:
            raw = handle.read(MAX_COOKIE_BYTES + 1)
        if len(raw) > MAX_COOKIE_BYTES:
            raise ValueError("Cookie file exceeds the 64 KiB size limit")
        header = raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise ValueError("Cookie file could not be read as UTF-8") from None
    return parse_cookie_header(header)

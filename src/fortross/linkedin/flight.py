"""Bounded, data-only decoding of LinkedIn hydration. Never executes page actions."""

import json
import re
from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup, Tag

_BOOTSTRAP = re.compile(r"window\.__como_rehydration__\s*=\s*")
_ROW = re.compile(rb"([0-9a-f]+):")
_REF = re.compile(r"^\$(?:L)?([0-9a-f]+)((?::[A-Za-z0-9_]+)*)$")
_TAG = re.compile(r"^[a-z][a-z0-9-]*$")
_SKIP = {
    "actions",
    "triggers",
    "requestMetadata",
    "viewTrackingSpecs",
    "tracking",
    "breadcrumbs",
    "buttonProps",
    "onClientRequestFailureAction",
}


def extract_rsc_payload(document: str) -> str | None:
    match = _BOOTSTRAP.search(document)
    if not match:
        return None
    end = document.find("</script>", match.end())
    if end < 0:
        return None
    try:
        chunks, _ = json.JSONDecoder().raw_decode(document[match.end() : end].lstrip())
    except (ValueError, RecursionError):
        return None
    if isinstance(chunks, list) and all(isinstance(chunk, str) for chunk in chunks):
        return "".join(chunks)
    return None


def flight_rows(payload: str) -> dict[str, Any]:
    data = payload.encode("utf-8")
    rows: dict[str, Any] = {}
    offset = 0
    while offset < len(data) and len(rows) < 30_000:
        match = _ROW.match(data, offset)
        if not match:
            end = data.find(b"\n", offset)
            if end < 0:
                break
            offset = end + 1
            continue
        key = match[1].decode()
        offset = match.end()
        if data[offset : offset + 1] == b"T":
            # Flight text rows use a hexadecimal UTF-8 byte count, not newline framing.
            length = re.match(rb"T([0-9a-f]+),", data[offset : offset + 24])
            if not length:
                break
            start = offset + length.end()
            end = start + int(length[1], 16)
            if end > len(data):
                break
            rows[key] = data[start:end].decode("utf-8", errors="replace")
            offset = end
            if data[offset : offset + 1] == b"\n":
                offset += 1
            continue
        end = data.find(b"\n", offset)
        end = len(data) if end < 0 else end
        raw = data[offset:end]
        # Import, debug, error and binary records are not JSON model rows.
        if raw[:1] in (b"[", b"{", b'"'):
            try:
                rows[key] = json.loads(raw)
            except (ValueError, RecursionError):
                pass
        offset = end + 1
    return rows


def is_element(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= 4
        and value[0] == "$"
        and isinstance(value[3], dict)
    )


class FlightDocument:
    def __init__(self, document: str) -> None:
        self.html = BeautifulSoup(document, "html.parser")
        self.payload = extract_rsc_payload(document)
        self.rows = flight_rows(self.payload or "")
        self.truncated = False

    def resolve(self, value: Any, seen: frozenset[str] = frozenset(), depth: int = 0) -> Any:
        if depth > 80:
            self.truncated = True
            return None
        for _ in range(80):
            if not isinstance(value, str):
                return value
            if value.startswith("$$"):
                return value[1:]
            match = _REF.fullmatch(value)
            if not match:
                return None if value.startswith("$") else value
            if value in seen:
                return None
            seen = seen | {value}
            value = self.rows.get(match[1])
            for part in match[2].split(":")[1:]:
                value = self.resolve(value, seen, depth + 1)
                if is_element(value) and part == "props":
                    value = value[3]
                elif isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                    value = value[int(part)]
                else:
                    return None
        return None

    def objects(self) -> Iterator[dict[str, Any]]:
        roots = list(self.rows.values())
        for script in self.html.find_all(
            "script", type=["application/json", "application/ld+json"]
        ):
            try:
                roots.append(json.loads(script.string or script.get_text()))
            except (ValueError, RecursionError):
                pass
        if self.payload and not self.rows:
            try:
                roots.append(json.loads(self.payload))
            except (ValueError, RecursionError):
                pass
        stack = [(root, 0) for root in reversed(roots)]
        remaining = 100_000
        while stack and remaining:
            remaining -= 1
            value, depth = stack.pop()
            if depth > 80:
                continue
            if isinstance(value, dict):
                if str(value.get("$type", "")).startswith("proto.sdui.actions."):
                    continue
                yield value
                stack.extend(
                    (child, depth + 1)
                    for key, child in reversed(list(value.items()))
                    if key not in _SKIP and not key.startswith("on")
                )
            elif isinstance(value, list):
                stack.extend((child, depth + 1) for child in reversed(value))

    def native_soup(self) -> BeautifulSoup:
        soup = BeautifulSoup("<main></main>", "html.parser")
        remaining = 150_000
        rendered: set[str] = set()

        def append(value: Any, parent: Tag, seen: frozenset[str], depth: int = 0) -> None:
            nonlocal remaining
            remaining -= 1
            if remaining < 0 or depth > 100:
                self.truncated = True
                return
            if isinstance(value, str):
                match = _REF.fullmatch(value)
                if match:
                    if value in seen:
                        return
                    rendered.add(match[1])
                    resolved = self.resolve(value)
                    append(resolved, parent, seen | {value}, depth + 1)
                elif value.startswith("$$"):
                    parent.append(value[1:])
                elif not value.startswith("$"):
                    parent.append(value)
                return
            if is_element(value):
                name, props = value[1], value[3]
                if name in ("script", "style", "noscript"):
                    return
                native = isinstance(name, str) and _TAG.fullmatch(name)
                tag = soup.new_tag(name if native else "div")
                for key in (
                    "id",
                    "role",
                    "aria-label",
                    "aria-hidden",
                    "href",
                    "src",
                    "datetime",
                    "data-urn",
                    "data-id",
                    "data-view-name",
                    "componentkey",
                    "componentKey",
                    "className",
                ):
                    attr = self.resolve(props.get(key))
                    if isinstance(attr, str):
                        mapped = {
                            "className": "class",
                            "componentKey": "data-component-key",
                            "componentkey": "data-component-key",
                        }.get(key, key)
                        tag[mapped] = attr
                parent.append(tag)
                append(props.get("children"), tag, seen, depth + 1)
                return
            if isinstance(value, list):
                for child in value:
                    append(child, parent, seen, depth + 1)
            elif isinstance(value, dict):
                # Only render explicitly provided children, never action/toast metadata.
                append(value.get("children"), parent, seen, depth + 1)

        # Root-first traversal preserves header/section/record ownership across references.
        for key in sorted(self.rows, key=lambda key: (key != "0", int(key, 16))):
            if key not in rendered:
                append(self.rows[key], soup.main, frozenset({"$" + key}))
                rendered.add(key)
        return soup

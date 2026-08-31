"""Explicit, local-only, one-attempt capture for offline posts debugging.

Run from the project root. Authenticated bodies are private diagnostic artifacts.
They are never loaded into tests or committed. No request headers are saved.
"""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx

from fortross.linkedin.client import LinkedInClient, UpstreamGovernor
from fortross.linkedin.errors import LinkedInError
from fortross.linkedin.feed import parse_posts_feed
from fortross.linkedin.flight import FlightDocument
from fortross.linkedin.posts import parse_posts_document
from fortross.linkedin.urls import parse_profile_url
from fortross.safety import build_counter_store
from fortross.service import ProfileService
from fortross.settings import Settings


def private_write(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)


def capture_directory() -> Path:
    root = Path("responses")
    if root.is_symlink():
        raise ValueError("Capture directory must not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="posts-", dir=root)).resolve()


async def capture(url: str, feed_from: Path | None = None, count: int = 5) -> None:
    settings = Settings()
    target = parse_profile_url(url)
    if settings.linkedin_max_concurrency != 1 or settings.linkedin_max_retries != 0:
        raise ValueError("Capture requires concurrency 1 and retries 0")
    directory = await asyncio.to_thread(capture_directory)
    store = build_counter_store(settings)

    class CaptureClient(LinkedInClient):
        async def _read_response(self, response: httpx.Response) -> str:
            body = await super()._read_response(response)
            redacted = body
            for secret in (
                settings.linkedin_li_at,
                settings.linkedin_jsessionid,
                settings.linkedin_jsessionid.strip('"'),
                settings.api_key,
            ):
                if len(secret) >= 8:
                    redacted = redacted.replace(secret, "[REDACTED]")
            private_write(directory / ("body.json" if feed_from else "body.html"), redacted)
            private_write(
                directory / "metadata.json",
                json.dumps(
                    {
                        "status": response.status_code,
                        "content_type": response.headers.get("content-type"),
                        "content_encoding": response.headers.get("content-encoding"),
                        "decoded_bytes": len(body.encode("utf-8")),
                        "request_headers_saved": False,
                        "configured_secrets_redacted": True,
                    },
                    indent=2,
                ),
            )
            return body

    client = CaptureClient(settings, UpstreamGovernor(settings, store))
    try:
        service = ProfileService(settings, client)
        service.authorize(target)
        if feed_from is not None:
            bootstrap = await asyncio.to_thread(feed_from.read_text, encoding="utf-8")
            document = await client.fetch_posts_feed_from_bootstrap(bootstrap, target.slug, count)
            try:
                payload = json.loads(document)
                summary = {"valid_json": True, "json_type": type(payload).__name__}
            except ValueError:
                summary = {"valid_json": False}
            try:
                posts, truncated, warnings = parse_posts_feed(document, count)
                summary.update(post_count=len(posts), truncated=truncated, warnings=warnings)
            except LinkedInError as exc:
                summary.update(parser_error=exc.code, message=str(exc))
            print(json.dumps({"capture_directory": str(directory), **summary}, indent=2))
            return
        document = await client.fetch_posts_document(target.slug)
        decoded = FlightDocument(document)
        result = {
            "capture_directory": str(directory),
            "hydration": decoded.payload is not None,
            "flight_rows": len(decoded.rows),
        }
        try:
            posts, _, warnings = parse_posts_document(document)
            result.update(post_count=len(posts), warnings=warnings)
        except LinkedInError as exc:
            result.update(parser_error=exc.code, message=str(exc))
        print(json.dumps(result, indent=2))
    finally:
        await client.close()
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument(
        "--feed-from",
        type=Path,
        help="Explicitly make ONE feed GET using this saved bootstrap instead of fetching HTML",
    )
    parser.add_argument("--count", type=int, choices=range(1, 6), default=5)
    args = parser.parse_args()
    try:
        asyncio.run(capture(args.url, args.feed_from, args.count))
    except LinkedInError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}))
        raise SystemExit(1) from None
    except Exception as exc:
        # Never print a Settings/HTTP object or traceback containing credentials.
        print(json.dumps({"error": "capture_failed", "type": type(exc).__name__}))
        raise SystemExit(1) from None

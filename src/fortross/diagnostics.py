"""Explicit loopback-only capture server. Never enabled by the production entry point."""

import argparse
import asyncio
import json
import os
import re
import tempfile
from functools import partial
from pathlib import Path
from urllib.parse import quote

import uvicorn

from fortross.linkedin.client import SECTION_PATHS, LinkedInClient
from fortross.linkedin.errors import LinkedInSafetyError
from fortross.linkedin.urls import parse_profile_url
from fortross.sessions import SessionManager
from fortross.settings import get_settings


def private_write(path: Path, body: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(body)


def capture_client(
    slug: str, directory: Path, *, sections: tuple[str, ...] = (), saved_profile: str | None = None
) -> type[LinkedInClient]:
    if any(section not in SECTION_PATHS for section in sections):
        raise ValueError("Unknown diagnostic section")
    sections = tuple(dict.fromkeys(sections))
    section_labels = {
        f"/in/{slug}/details/{SECTION_PATHS[section]}/": f"section-{section}"
        for section in sections
    }
    # Shared across sessions. A fresh login cannot reset this diagnostic budget.
    attempted: set[str] = set()
    stopped = False

    class CaptureClient(LinkedInClient):
        async def fetch_profile_documents(self, requested_slug, include_sections=True):
            if requested_slug != slug or (include_sections and not sections):
                raise LinkedInSafetyError(
                    "Capture permits only the configured profile; use include_sections=false "
                    "unless a section was explicitly enabled by the diagnostic launcher"
                )
            if saved_profile is not None:
                documents, warnings = (
                    {"profile": saved_profile},
                    ["Diagnostic replay: main profile came from a saved capture, not a new GET"],
                )
            else:
                documents, warnings = await super().fetch_profile_documents(requested_slug, False)
            if include_sections and sections:
                for section in sections:
                    documents[section] = await self._get_once(
                        f"/in/{slug}/details/{SECTION_PATHS[section]}/"
                    )
                warnings = [w for w in warnings if w != "Detail sections were not requested"]
                warnings.append(
                    "Diagnostic capture requested only these detail sections: "
                    + ", ".join(sections)
                )
            return documents, warnings

        async def fetch_posts_document(self, requested_slug):
            if requested_slug != slug or sections:
                raise LinkedInSafetyError("Capture permits only the configured profile")
            return await super().fetch_posts_document(requested_slug)

        async def fetch_posts_feed_from_bootstrap(self, document, requested_slug, count=5):
            if requested_slug != slug or sections or not 1 <= count <= 5:
                raise LinkedInSafetyError("Capture permits at most five posts")
            return await super().fetch_posts_feed_from_bootstrap(document, requested_slug, count)

        async def _get_once(self, path, *, accept=None):
            nonlocal stopped
            if path == f"/in/{slug}/" and saved_profile is None:
                label = "profile"
            elif path == f"/in/{slug}/recent-activity/shares/" and not sections:
                label = "posts-bootstrap"
            elif path in section_labels:
                label = section_labels[path]
            elif (
                path.startswith("/voyager/api/graphql?")
                and not sections
                and re.search(r"variables=\(count:[1-5],start:0,profileUrn:", path)
                and "posts-bootstrap" in attempted
            ):
                label = "posts-feed"
            else:
                raise LinkedInSafetyError("Request outside the approved capture scope")
            if stopped or label in attempted:
                raise LinkedInSafetyError("Capture attempt already used or capture stopped")
            attempted.add(label)
            try:
                body = await super()._get_once(path, accept=accept)
                redacted = body
                for secret in (self.settings.linkedin_li_at, self.settings.linkedin_jsessionid):
                    if secret:
                        for variant in {secret, quote(secret, safe=""), json.dumps(secret)[1:-1]}:
                            redacted = redacted.replace(variant, "[REDACTED]")
                suffix = "json" if label == "posts-feed" else "html"
                await asyncio.to_thread(private_write, directory / f"{label}.{suffix}", redacted)
                return body
            except BaseException:
                stopped = True
                raise

    return CaptureClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-url", required=True)
    parser.add_argument(
        "--section",
        choices=SECTION_PATHS,
        action="append",
        default=[],
        help="Allow one GET for this detail section; repeat for other approved sections",
    )
    parser.add_argument(
        "--profile-from",
        type=Path,
        help="Reuse saved profile HTML instead of another main-page GET",
    )
    args = parser.parse_args()
    settings = get_settings()
    if settings.app_env != "development":
        parser.error("Capture server is restricted to APP_ENV=development")
    if settings.linkedin_max_retries != 0 or settings.linkedin_max_concurrency != 1:
        parser.error("Capture requires retries=0 and concurrency=1")
    settings.validate_server_configuration()
    target = parse_profile_url(args.profile_url)
    saved_profile = None
    if args.profile_from is not None:
        if args.profile_from.stat().st_size > settings.linkedin_max_response_bytes:
            parser.error("Saved profile exceeds the response size limit")
        saved_profile = args.profile_from.read_text(encoding="utf-8")
        if f"/in/{target.slug}/" not in saved_profile:
            parser.error("Saved profile must reference the configured target URL")
    root = Path("responses")
    if root.is_symlink():
        parser.error("Capture directory cannot be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="session-capture-", dir=root)).resolve()
    print(f"Private capture directory: {directory}")
    print("Bounded capture only; no retries. Log in via /playground.")
    if args.section:
        print("One GET per selected section: " + ", ".join(dict.fromkeys(args.section)))
        print("Unselected sections and posts are blocked in this section-capture run.")
    if saved_profile is not None:
        print("Main profile will be replayed from the saved capture, with no main-page GET.")
    # This factory override exists only in this explicitly launched diagnostic process.
    # The ordinary production entry point has no body-capture hook.
    from fortross import main as api_main

    api_main.SessionManager = partial(
        SessionManager,
        client_factory=capture_client(
            target.slug, directory, sections=tuple(args.section), saved_profile=saved_profile
        ),
    )
    # Match the playground examples to this launcher's narrower scope. The client
    # guards still enforce these limits even if a caller edits the request manually.
    schema = api_main.app.openapi()
    models = schema["components"]["schemas"]
    models["ProfileRequest"]["properties"]["include_sections"]["default"] = bool(args.section)
    models["PostsRequest"]["properties"]["limit"]["default"] = 5
    models["PostsRequest"]["properties"]["limit"]["maximum"] = 5
    uvicorn.run(api_main.app, host="127.0.0.1", port=8000, workers=1, access_log=False)


if __name__ == "__main__":
    main()

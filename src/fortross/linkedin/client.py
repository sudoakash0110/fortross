import asyncio
import hashlib
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx

from fortross.linkedin.errors import (
    CircuitOpenError,
    LinkedInAuthError,
    LinkedInChallengeError,
    LinkedInRateLimitError,
    LinkedInSafetyError,
    LinkedInUpstreamError,
    ProfileNotFoundError,
)
from fortross.linkedin.voyager import first_feed_path, profile_urn_from_bootstrap
from fortross.safety import CounterStore, MemoryCounterStore
from fortross.settings import Settings

SECTION_PATHS = {
    "experience": "experience",
    "education": "education",
    "skills": "skills",
    "certifications": "certifications",
    "languages": "languages",
}


class UpstreamGate:
    """One process-wide queue shared by all API sessions."""

    def __init__(self, concurrency: int = 1):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.last_request = 0.0


class UpstreamGovernor:
    def __init__(
        self,
        settings: Settings,
        store: CounterStore | None = None,
        gate: UpstreamGate | None = None,
    ) -> None:
        self._gate = gate or UpstreamGate(settings.linkedin_max_concurrency)
        self._semaphore = self._gate.semaphore
        self._interval = settings.linkedin_min_request_interval_seconds
        self._cooldown = settings.linkedin_circuit_breaker_cooldown_seconds
        self._open_until = 0.0
        self._lock = self._gate.lock
        self._store = store or MemoryCounterStore()
        self._identity = hashlib.sha256(settings.linkedin_li_at.encode()).hexdigest()
        self._limits = (
            (3600, settings.linkedin_max_requests_per_hour),
            (86400, settings.linkedin_max_requests_per_day),
        )
        self._global_limits = (
            (3600, settings.linkedin_global_max_requests_per_hour),
            (86400, settings.linkedin_global_max_requests_per_day),
        )

    def _check_circuit(self) -> None:
        if time.monotonic() < self._open_until:
            raise CircuitOpenError("LinkedIn access is paused by the circuit breaker")

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self._check_circuit()
        async with self._semaphore:
            async with self._lock:
                self._check_circuit()
                wait = self._interval - (time.monotonic() - self._gate.last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._check_circuit()
                try:
                    blocked_until = await self._store.blocked_until(self._identity)
                except Exception:
                    raise LinkedInSafetyError("Circuit-breaker storage is unavailable") from None
                if blocked_until > time.time():
                    raise CircuitOpenError("LinkedIn access is paused by the circuit breaker")
                now = int(time.time())
                for seconds, limit in self._limits:
                    try:
                        count = await self._store.increment(
                            f"upstream:{self._identity}:{seconds}", now - now % seconds
                        )
                    except Exception as exc:
                        raise LinkedInSafetyError("Upstream budget storage is unavailable") from exc
                    if count > limit:
                        raise LinkedInSafetyError("Session-wide upstream request budget exhausted")
                for seconds, limit in self._global_limits:
                    try:
                        count = await self._store.increment(
                            f"upstream:global:{seconds}", now - now % seconds
                        )
                    except Exception:
                        raise LinkedInSafetyError(
                            "Global upstream budget storage unavailable"
                        ) from None
                    if count > limit:
                        raise LinkedInSafetyError("Global upstream request budget exhausted")
                self._gate.last_request = time.monotonic()
            yield

    async def trip(self) -> None:
        self._open_until = time.monotonic() + self._cooldown
        try:
            await self._store.block_until(self._identity, math.ceil(time.time() + self._cooldown))
        except Exception:
            # The local circuit stays open even when persistence fails.
            raise LinkedInSafetyError(
                "Could not persist the circuit breaker; access paused"
            ) from None


class LinkedInClient:
    def __init__(self, settings: Settings, governor: UpstreamGovernor | None = None) -> None:
        self.settings = settings
        self.governor = governor or UpstreamGovernor(settings)
        self.active_check: Callable[[], None] | None = None
        self.authentication_rejected = False
        jsessionid = settings.linkedin_jsessionid.strip().strip('"')
        self._client = httpx.AsyncClient(
            base_url="https://www.linkedin.com",
            timeout=settings.linkedin_request_timeout_seconds,
            follow_redirects=False,
            cookies={"li_at": settings.linkedin_li_at, "JSESSIONID": f'"{jsessionid}"'},
            headers={
                "accept": "text/html,application/xhtml+xml",
                "accept-language": "en-US,en;q=0.9",
                "cache-control": "no-cache",
                "csrf-token": jsessionid,
                "user-agent": settings.linkedin_user_agent,
                "x-restli-protocol-version": "2.0.0",
            },
        )

    def clear_credentials(self) -> None:
        self._client.cookies.clear()
        self._client.headers.pop("csrf-token", None)
        self.settings.linkedin_li_at = ""
        self.settings.linkedin_jsessionid = ""

    async def close(self) -> None:
        self.clear_credentials()
        await self._client.aclose()

    async def _get(self, path: str) -> str:
        attempts = self.settings.linkedin_max_retries + 1
        for attempt in range(attempts):
            try:
                return await self._get_once(path)
            except (httpx.TransportError, LinkedInUpstreamError):
                if attempt + 1 >= attempts:
                    raise
        raise LinkedInUpstreamError("LinkedIn request failed")

    async def _get_once(self, path: str, *, accept: str | None = None) -> str:
        try:
            if self.active_check:
                self.active_check()
            headers = {"accept": accept} if accept else None
            async with self.governor.slot():
                if self.active_check:
                    self.active_check()
                async with self._client.stream("GET", path, headers=headers) as response:
                    return await self._read_response(response)
        except httpx.TransportError as exc:
            raise LinkedInUpstreamError("Could not reach LinkedIn") from exc

    async def _read_response(self, response: httpx.Response) -> str:
        location = response.headers.get("location", "")
        if response.status_code in {401, 403}:
            self.authentication_rejected = True
            await self.governor.trip()
            raise LinkedInAuthError("LinkedIn rejected the configured session")
        if response.status_code in {429, 999}:
            await self.governor.trip()
            raise LinkedInRateLimitError("LinkedIn rate-limited the configured session")
        if any(marker in location.lower() for marker in ("checkpoint", "challenge", "login")):
            self.authentication_rejected = True
            await self.governor.trip()
            raise LinkedInChallengeError("LinkedIn requested a checkpoint or login")
        if response.status_code == 404:
            raise ProfileNotFoundError("LinkedIn profile or section was not found")
        if response.status_code >= 500:
            raise LinkedInUpstreamError(f"LinkedIn returned HTTP {response.status_code}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LinkedInUpstreamError(f"LinkedIn returned HTTP {response.status_code}") from exc
        if any(
            marker in str(response.url).lower() for marker in ("checkpoint", "challenge", "login")
        ):
            self.authentication_rejected = True
            await self.governor.trip()
            raise LinkedInChallengeError("LinkedIn requested a checkpoint or login")
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                response_size = int(content_length)
            except ValueError:
                response_size = 0
            if response_size > self.settings.linkedin_max_response_bytes:
                raise LinkedInUpstreamError("LinkedIn response exceeded the configured size limit")
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > self.settings.linkedin_max_response_bytes:
                raise LinkedInUpstreamError("LinkedIn response exceeded the configured size limit")
        body = bytes(chunks).decode(response.encoding or "utf-8", errors="replace")
        sample = body[:100_000].casefold()
        json_response = "json" in response.headers.get("content-type", "").lower()
        if (not json_response or sample.lstrip().startswith("<")) and any(
            marker in sample for marker in ("/checkpoint/", "authwall", "sign in to linkedin")
        ):
            self.authentication_rejected = True
            await self.governor.trip()
            raise LinkedInChallengeError("LinkedIn returned a checkpoint or login page")
        return body

    async def fetch_profile_documents(
        self, slug: str, include_sections: bool = True
    ) -> tuple[dict[str, str], list[str]]:
        documents = {"profile": await self._get(f"/in/{slug}/")}
        warnings: list[str] = []
        if not include_sections:
            return documents, ["Detail sections were not requested"]
        for name, path in SECTION_PATHS.items():
            try:
                documents[name] = await self._get(f"/in/{slug}/details/{path}/")
            except ProfileNotFoundError:
                warnings.append(f"LinkedIn did not expose a {name} section")
        return documents, warnings

    async def fetch_posts_document(self, slug: str) -> str:
        # One activity-page attempt. The service may then request one initial feed page.
        return await self._get_once(f"/in/{slug}/recent-activity/shares/")

    async def fetch_posts_feed_from_bootstrap(
        self, document: str, slug: str, count: int = 5
    ) -> str:
        """Reuses the activity bootstrap. One feed attempt, no pagination/retries.
        Callers must authorize the target before calling, as with other fetch methods.
        """
        urn = profile_urn_from_bootstrap(document, slug)
        return await self._get_once(
            first_feed_path(urn, count, self.settings.linkedin_posts_query_id),
            accept="application/vnd.linkedin.normalized+json+2.1",
        )

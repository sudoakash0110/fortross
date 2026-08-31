"""Isolated, temporary API sessions. Cookies and tokens are never persisted."""

import asyncio
import hashlib
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

from fortross.cookies import parse_cookie_header
from fortross.linkedin.client import LinkedInClient, UpstreamGate, UpstreamGovernor
from fortross.linkedin.errors import (
    LinkedInAuthError,
    LinkedInChallengeError,
    LinkedInSafetyError,
    SessionAuthError,
    SessionCapacityError,
)
from fortross.safety import CounterStore, RateLimiter
from fortross.service import ProfileService
from fortross.settings import Settings

audit = logging.getLogger("fortross.audit")
T = TypeVar("T")


@dataclass(repr=False)
class APISession:
    id: str
    token_hash: str
    account_key: str
    expires_at: datetime
    deadline: float
    service: ProfileService
    revoked: bool = False
    tasks: set[asyncio.Task] = field(default_factory=set)

    def ensure_active(self) -> None:
        if self.revoked or time.monotonic() >= self.deadline:
            raise SessionAuthError("Session is invalid or expired; log in with your own cookie")


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        store: CounterStore,
        *,
        client_factory: Callable[[Settings, UpstreamGovernor], LinkedInClient] = LinkedInClient,
    ):
        self.settings = settings
        self._client_factory = client_factory
        self.store = store
        self.gate = UpstreamGate(1)
        self._sessions: dict[str, APISession] = {}
        self._governors: dict[str, UpstreamGovernor] = {}
        self._lock = asyncio.Lock()
        login_settings = settings.model_copy(
            update={
                "rate_limit_per_minute": settings.login_limit_per_minute,
                "rate_limit_per_hour": 30,
                "rate_limit_per_day": 100,
            }
        )
        self.login_limiter = RateLimiter(login_settings, store, namespace="login")

    async def create(self, cookie: str) -> tuple[str, APISession]:
        li_at, jsessionid = parse_cookie_header(cookie)
        await self.reap()
        async with self._lock:
            if len(self._sessions) >= self.settings.max_active_sessions:
                raise SessionCapacityError("Active session limit reached; try again later")
            # model_copy cannot reload .env or a legacy operator cookie file.
            settings = self.settings.model_copy(
                update={
                    "linkedin_li_at": li_at,
                    "linkedin_jsessionid": jsessionid,
                    "linkedin_cookie_file": "",
                }
            )
            account_key = hashlib.sha256(li_at.encode()).hexdigest()
            governor = self._governors.get(account_key)
            if governor is None:
                governor = UpstreamGovernor(settings, self.store, self.gate)
                self._governors[account_key] = governor
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            ttl = settings.session_ttl_seconds
            session = APISession(
                id=str(uuid.uuid4()),
                token_hash=digest,
                account_key=account_key,
                expires_at=datetime.fromtimestamp(time.time() + ttl, UTC),
                deadline=time.monotonic() + ttl,
                service=ProfileService(settings, self._client_factory(settings, governor)),
            )
            session.service.client.active_check = session.ensure_active
            self._sessions[digest] = session
            audit.info("session_created session_id=%s", session.id)
            return token, session

    async def resolve(self, token: str) -> APISession:
        if not token or len(token) > 256 or not token.isascii():
            raise SessionAuthError("A valid bearer session token is required")
        session = self._sessions.get(hashlib.sha256(token.encode()).hexdigest())
        if session is None:
            raise SessionAuthError("A valid bearer session token is required")
        if time.monotonic() >= session.deadline:
            await self.revoke(session, "expired")
        session.ensure_active()
        return session

    async def run(self, session: APISession, operation: Callable[[], Awaitable[T]]) -> T:
        session.ensure_active()
        task = asyncio.create_task(operation())
        session.tasks.add(task)
        try:
            result = await task
            session.ensure_active()
            return result
        except (LinkedInAuthError, LinkedInChallengeError, LinkedInSafetyError) as exc:
            if (
                isinstance(exc, LinkedInSafetyError)
                and not session.service.client.authentication_rejected
            ):
                raise
            # All tokens for the same LinkedIn credential must stop, but never other accounts.
            await self.revoke_account(session.account_key)
            raise SessionAuthError("LinkedIn rejected this session; log in again") from None
        except asyncio.CancelledError:
            task.cancel()
            if session.revoked or time.monotonic() >= session.deadline:
                raise SessionAuthError("Session was revoked or expired") from None
            raise
        finally:
            session.tasks.discard(task)

    def _detach(self, session: APISession, reason: str) -> None:
        self._sessions.pop(session.token_hash, None)
        session.revoked = True
        session.service.client.clear_credentials()
        for task in list(session.tasks):
            task.cancel()
        audit.info("session_revoked session_id=%s reason=%s", session.id, reason)

    async def _dispose(self, session: APISession) -> None:
        tasks = list(session.tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await session.service.client.close()
        session.tasks.clear()

    async def revoke(self, session: APISession, reason: str = "logout") -> None:
        self._detach(session, reason)
        await self._dispose(session)

    async def revoke_account(self, account_key: str) -> None:
        sessions = [s for s in self._sessions.values() if s.account_key == account_key]
        for session in sessions:
            self._detach(session, "linkedin_auth_rejected")
        for session in sessions:
            await self._dispose(session)

    async def reap(self) -> None:
        expired = [s for s in self._sessions.values() if time.monotonic() >= s.deadline]
        for session in expired:
            self._detach(session, "expired")
        for session in expired:
            await self._dispose(session)
        active_accounts = {s.account_key for s in self._sessions.values()}
        self._governors = {
            key: governor
            for key, governor in self._governors.items()
            if key in active_accounts or governor._open_until > time.monotonic()
        }

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(min(30, self.settings.session_ttl_seconds))
            await self.reap()

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            self._detach(session, "shutdown")
        for session in sessions:
            await self._dispose(session)
        self._governors.clear()

"""In-memory OTP challenge store for the 2-FA login flow."""

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Literal

from backend.config import settings

logger = logging.getLogger(__name__)


@dataclass
class OTPChallenge:
    challenge_id: str
    username: str
    code_hash: str          # sha256 of the plain code
    attempts_left: int
    expires_at: datetime
    consumed: bool = False


class OTPStore:
    def __init__(self) -> None:
        self._store: Dict[str, OTPChallenge] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, username: str) -> tuple[str, str]:
        """Create a new OTP challenge.

        Returns (challenge_id, plain_code).
        """
        self.cleanup_expired()

        plain_code = str(secrets.randbelow(10 ** settings.OTP_LENGTH)).zfill(settings.OTP_LENGTH)
        code_hash = hashlib.sha256(plain_code.encode()).hexdigest()
        challenge_id = secrets.token_urlsafe(32)

        challenge = OTPChallenge(
            challenge_id=challenge_id,
            username=username,
            code_hash=code_hash,
            attempts_left=settings.OTP_MAX_ATTEMPTS,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(seconds=settings.OTP_TTL_SECONDS),
        )
        self._store[challenge_id] = challenge
        return challenge_id, plain_code

    def verify(
        self, challenge_id: str, code: str
    ) -> Literal["ok", "expired", "wrong", "exhausted", "consumed", "not_found"]:
        """Verify an OTP code against a challenge."""
        challenge = self._store.get(challenge_id)
        if challenge is None:
            return "not_found"

        if challenge.consumed:
            return "consumed"

        if datetime.now(tz=timezone.utc) > challenge.expires_at:
            del self._store[challenge_id]
            return "expired"

        if challenge.attempts_left <= 0:
            del self._store[challenge_id]
            return "exhausted"

        # Constant-time compare
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        if not hmac.compare_digest(code_hash, challenge.code_hash):
            challenge.attempts_left -= 1
            if challenge.attempts_left <= 0:
                del self._store[challenge_id]
                return "exhausted"
            return "wrong"

        # Success — mark consumed
        challenge.consumed = True
        username = challenge.username
        del self._store[challenge_id]
        return "ok"

    def get_attempts_left(self, challenge_id: str) -> int:
        challenge = self._store.get(challenge_id)
        if challenge is None:
            return 0
        return challenge.attempts_left

    def cleanup_expired(self) -> None:
        """Remove expired challenges from memory."""
        now = datetime.now(tz=timezone.utc)
        expired = [cid for cid, ch in self._store.items() if now > ch.expires_at]
        for cid in expired:
            del self._store[cid]
        if expired:
            logger.debug("Cleaned up %d expired OTP challenges", len(expired))


# Module-level singleton
otp_store = OTPStore()

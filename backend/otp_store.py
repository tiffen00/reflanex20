"""In-memory OTP challenge store for the 2-FA login flow."""

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Literal, Optional, Tuple

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


@dataclass
class VerifyResult:
    status: Literal["ok", "expired", "wrong", "exhausted", "consumed", "not_found"]
    username: Optional[str] = None   # set on "ok"
    attempts_left: Optional[int] = None  # set on "wrong"


class OTPStore:
    def __init__(self) -> None:
        self._store: Dict[str, OTPChallenge] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, username: str) -> Tuple[str, str]:
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

    def verify(self, challenge_id: str, code: str) -> VerifyResult:
        """Verify an OTP code against a challenge.

        Returns a VerifyResult with status and optional fields.
        On "ok" the challenge is consumed and removed from the store.
        """
        challenge = self._store.get(challenge_id)
        if challenge is None:
            return VerifyResult(status="not_found")

        if challenge.consumed:
            return VerifyResult(status="consumed")

        if datetime.now(tz=timezone.utc) > challenge.expires_at:
            self._remove(challenge_id)
            return VerifyResult(status="expired")

        if challenge.attempts_left <= 0:
            self._remove(challenge_id)
            return VerifyResult(status="exhausted")

        # Constant-time compare
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        if not hmac.compare_digest(code_hash, challenge.code_hash):
            challenge.attempts_left -= 1
            if challenge.attempts_left <= 0:
                self._remove(challenge_id)
                return VerifyResult(status="exhausted")
            return VerifyResult(status="wrong", attempts_left=challenge.attempts_left)

        # Success — consume and remove
        username = challenge.username
        challenge.consumed = True
        self._remove(challenge_id)
        return VerifyResult(status="ok", username=username)

    def cleanup_expired(self) -> None:
        """Remove expired challenges from memory."""
        now = datetime.now(tz=timezone.utc)
        expired = [cid for cid, ch in self._store.items() if now > ch.expires_at]
        for cid in expired:
            self._remove(cid)
        if expired:
            logger.debug("Cleaned up %d expired OTP challenges", len(expired))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _remove(self, challenge_id: str) -> None:
        self._store.pop(challenge_id, None)


# Module-level singleton
otp_store = OTPStore()

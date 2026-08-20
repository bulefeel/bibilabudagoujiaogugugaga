"""Single-administrator bootstrap and opaque server-side sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import pbkdf2_hmac, sha256
import hmac
import secrets

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .db import utc_now
from .models import AdminCredential, AdminSession


PBKDF2_ITERATIONS = 600_000


def _token_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    derived = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class AuthenticatedAdmin:
    id: int
    username: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class NewSession:
    session_token: str
    csrf_token: str
    username: str


class AuthService:
    def __init__(self, session: Session, *, session_hours: int = 12) -> None:
        self.session = session
        self.session_hours = session_hours

    def is_initialized(self) -> bool:
        return bool(self.session.scalar(select(func.count(AdminCredential.id))))

    def bootstrap(self, username: str, password: str) -> NewSession:
        """Atomically create the only administrator, exactly once."""

        if self.is_initialized():
            raise RuntimeError("管理员已经初始化")
        # Fixed primary key turns concurrent first-run attempts into a database
        # uniqueness conflict: this application can never gain a second admin.
        admin = AdminCredential(id=1, username=username, password_hash=hash_password(password))
        self.session.add(admin)
        self.session.flush()
        return self._new_session(admin)

    def authenticate(self, username: str, password: str) -> NewSession | None:
        admin = self.session.scalar(
            select(AdminCredential).where(AdminCredential.username == username)
        )
        now = utc_now()
        if admin is None:
            # Do comparable work so unknown users are not a cheap timing oracle.
            verify_password(password, hash_password("timing-equalizer-phrase"))
            return None
        locked_until = admin.locked_until
        if locked_until is not None:
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=now.tzinfo)
            if locked_until > now:
                return None
        if not verify_password(password, admin.password_hash):
            admin.failed_attempts += 1
            if admin.failed_attempts >= 5:
                admin.locked_until = now + timedelta(minutes=15)
                admin.failed_attempts = 0
            self.session.flush()
            return None
        admin.failed_attempts = 0
        admin.locked_until = None
        return self._new_session(admin)

    def _new_session(self, admin: AdminCredential) -> NewSession:
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        now = utc_now()
        self.session.add(
            AdminSession(
                token_hash=_token_hash(token), admin_id=admin.id,
                session_epoch=admin.session_epoch, csrf_hash=_token_hash(csrf),
                expires_at=now + timedelta(hours=self.session_hours),
            )
        )
        self.session.flush()
        return NewSession(token, csrf, admin.username)

    def validate(self, token: str | None) -> AuthenticatedAdmin | None:
        if not token:
            return None
        now = utc_now()
        # Do not flush an earlier last_seen update before a slow external API
        # call; otherwise this request can hold SQLite's write lock for the
        # whole Ziniao round-trip.
        with self.session.no_autoflush:
            pair = self.session.execute(
                select(AdminSession, AdminCredential)
                .join(AdminCredential, AdminCredential.id == AdminSession.admin_id)
                .where(
                    AdminSession.token_hash == _token_hash(token),
                    AdminSession.revoked_at.is_(None),
                    AdminSession.expires_at > now,
                    AdminSession.session_epoch == AdminCredential.session_epoch,
                )
            ).first()
        if not pair:
            return None
        auth_session, admin = pair
        auth_session.last_seen_at = now
        # Commit the touch on its own.  ``no_autoflush`` above only protects the
        # SELECT in this method: the endpoint's very next query autoflushes this
        # UPDATE, and the request session then holds SQLite's write lock for the
        # rest of the request.  When that request goes on to call the automation
        # service — which writes through its own connection — the second writer
        # blocks on the first, the blocking sqlite call stalls the event loop so
        # the first can never reach its commit, and both fail after
        # ``busy_timeout`` with "database is locked".  A session touch does not
        # belong to the request's outcome, so committing it here is also the
        # semantically correct scope.
        self.session.commit()
        # The raw CSRF token exists only in a separate client cookie.  An empty
        # marker here forces callers to validate it with validate_csrf().
        return AuthenticatedAdmin(admin.id, admin.username, "")

    def validate_csrf(self, token: str | None, csrf: str | None) -> bool:
        if not token or not csrf:
            return False
        with self.session.no_autoflush:
            row = self.session.scalar(
                select(AdminSession).where(
                    AdminSession.token_hash == _token_hash(token),
                    AdminSession.revoked_at.is_(None),
                    AdminSession.expires_at > utc_now(),
                )
            )
        return bool(row and hmac.compare_digest(row.csrf_hash, _token_hash(csrf)))

    def logout(self, token: str | None) -> None:
        if token:
            self.session.execute(
                update(AdminSession)
                .where(AdminSession.token_hash == _token_hash(token), AdminSession.revoked_at.is_(None))
                .values(revoked_at=utc_now())
            )

    def purge_expired(self) -> int:
        result = self.session.execute(
            update(AdminSession)
            .where(AdminSession.expires_at <= utc_now(), AdminSession.revoked_at.is_(None))
            .values(revoked_at=utc_now())
        )
        return result.rowcount

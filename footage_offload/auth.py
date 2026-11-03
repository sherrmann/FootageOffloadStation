"""Password hashing, CSRF tokens and login rate limiting.

Passwords are hashed with scrypt from the standard library and stored as

    scrypt$<n>$<r>$<p>$<salt base64>$<hash base64>

With n=2**14 and r=8 scrypt needs 16 MiB and takes a noticeable fraction of
a second on a Pi Zero; that is acceptable for a login and slows down
guessing. The parameters are stored with the hash, so they can be raised
later without breaking existing passwords.
"""
import base64
import hashlib
import hmac
import secrets
import threading
import time

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 64
PREFIX = "scrypt"


class PasswordError(ValueError):
    """Raised with a message id when a new password is not acceptable."""

    def __init__(self, message_id):
        super().__init__(message_id)
        self.message_id = message_id


def password_problem(password):
    """Message id describing why `password` cannot be used, or None."""
    if not isinstance(password, str):
        return "error.type_string"
    if len(password) < MIN_PASSWORD_LENGTH:
        return "error.password_too_short"
    if len(password) > MAX_PASSWORD_LENGTH or "\x00" in password:
        return "error.invalid"
    return None


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _scrypt(password, salt, n, r, p):
    # maxmem must cover 128 * n * r * p bytes plus some overhead.
    maxmem = 128 * n * r * (p + 2) + (1 << 20)
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=KEY_BYTES, maxmem=maxmem)


def hash_password(password):
    """Hash a new password. Raises PasswordError if it is not acceptable."""
    problem = password_problem(password)
    if problem:
        raise PasswordError(problem)
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"{PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password, stored):
    """True if `password` matches the stored hash. Never raises."""
    if not isinstance(password, str) or not isinstance(stored, str) or not stored:
        return False
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        prefix, n, r, p, salt_b64, key_b64 = stored.split("$")
        if prefix != PREFIX:
            return False
        n, r, p = int(n), int(r), int(p)
        # Refuse absurd parameters (a tampered file must not exhaust memory).
        if not (2 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(key_b64, validate=True)
        if not expected:
            return False
        key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                             dklen=len(expected), maxmem=128 * n * r * (p + 2) + (1 << 20))
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(key, expected)


def password_tag(stored_hash):
    """Short fingerprint of the stored hash, kept in the session cookie so
    that changing the password ends every other session."""
    return hashlib.sha256(("fos-session:" + (stored_hash or "")).encode()).hexdigest()[:24]


def new_csrf_token():
    return secrets.token_urlsafe(32)


def csrf_ok(expected, given):
    """Constant-time comparison of CSRF tokens; empty tokens never match."""
    if not expected or not given or not isinstance(given, str):
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


class LoginLimiter:
    """Simple per-client brute-force protection.

    After `free_attempts` failures a client is locked out; every further
    failure doubles the lock-out, up to `max_lockout_s`. Failures older than
    `forget_after_s` are forgotten. A successful login clears the record.
    """

    def __init__(self, free_attempts=5, base_lockout_s=30.0, max_lockout_s=900.0,
                 forget_after_s=3600.0, clock=time.monotonic, max_clients=1000):
        self.free_attempts = free_attempts
        self.base_lockout_s = base_lockout_s
        self.max_lockout_s = max_lockout_s
        self.forget_after_s = forget_after_s
        self.clock = clock
        self.max_clients = max_clients
        self._lock = threading.Lock()
        self._clients = {}          # ip -> [failures, last_failure, locked_until]

    def _expire(self, now):
        stale = [ip for ip, rec in self._clients.items()
                 if now - rec[1] > self.forget_after_s and now >= rec[2]]
        for ip in stale:
            del self._clients[ip]

    def retry_after(self, client):
        """Seconds the client still has to wait; 0 when it may try."""
        with self._lock:
            now = self.clock()
            self._expire(now)
            rec = self._clients.get(client)
            if not rec:
                return 0
            return max(0, int(rec[2] - now + 0.999))

    def failure(self, client):
        with self._lock:
            now = self.clock()
            self._expire(now)
            if client not in self._clients and len(self._clients) >= self.max_clients:
                # Many distinct clients: drop the oldest record.
                oldest = min(self._clients, key=lambda ip: self._clients[ip][1])
                del self._clients[oldest]
            rec = self._clients.setdefault(client, [0, now, 0.0])
            rec[0] += 1
            rec[1] = now
            excess = rec[0] - self.free_attempts
            if excess >= 0:
                lock = min(self.max_lockout_s, self.base_lockout_s * (2 ** min(excess, 16)))
                rec[2] = now + lock

    def success(self, client):
        with self._lock:
            self._clients.pop(client, None)

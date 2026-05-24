import os
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get('ENCRYPTION_KEY', '')
        if not key:
            # Auto-generate a key for dev if not set — warn loudly
            logger.warning(
                'ENCRYPTION_KEY not set in .env — generating ephemeral key. '
                'Data encrypted now will be UNREADABLE after restart. '
                'Set ENCRYPTION_KEY in your .env file immediately.'
            )
            key = Fernet.generate_key().decode()
            os.environ['ENCRYPTION_KEY'] = key
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(value: str | None) -> str | None:
    """Encrypt a string. Returns None if value is None or empty."""
    if value is None or value == '':
        return value
    try:
        return _get_fernet().encrypt(str(value).encode()).decode()
    except Exception as e:
        logger.error('Encryption error: %s', e)
        return value


def decrypt(value: str | None) -> str | None:
    """Decrypt a string. Returns the original value if decryption fails (e.g. plaintext legacy data)."""
    if value is None or value == '':
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        # Value is likely plaintext (pre-encryption legacy data) — return as-is
        return value


def encrypt_dict(d: dict, fields: list[str]) -> dict:
    """Return a copy of dict with specified fields encrypted."""
    out = dict(d)
    for f in fields:
        if f in out:
            out[f] = encrypt(out[f])
    return out


def decrypt_dict(d: dict, fields: list[str]) -> dict:
    """Return a copy of dict with specified fields decrypted."""
    out = dict(d)
    for f in fields:
        if f in out:
            out[f] = decrypt(out[f])
    return out


# ── Field maps: which fields to encrypt per table ────────────────────────────
USER_FIELDS       = ['email', 'name', 'student_id']
SESSION_FIELDS    = ['student_name']
VIOLATION_FIELDS  = ['details']
SUBMISSION_FIELDS = ['answers']
AUDIT_FIELDS      = ['ip', 'user_agent']

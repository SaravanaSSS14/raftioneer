import hashlib
import hmac
import secrets

# Stored format: pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>
# A per-user random salt means identical passwords never share a hash, and
# the iteration count makes brute-forcing a leaked database slow. Stdlib
# only - no extra dependency for the container images.
_PBKDF2_ITERATIONS = 100_000


def hash_password(password: str) -> str:
    """Hash a password with a fresh random salt (PBKDF2-HMAC-SHA256)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def validate_password_stored(password_provided: str, password_stored_hash: str) -> bool:
    """Check a provided password against a stored salted hash."""
    try:
        scheme, iterations, salt_hex, digest_hex = password_stored_hash.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password_provided.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def generate_session_token() -> str:
    """Generate a secure random session token."""
    # Generate 64 character hex string (256 bits)
    return secrets.token_hex(32)

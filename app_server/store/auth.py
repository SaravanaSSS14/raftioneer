import hashlib
import secrets
from typing import Optional
from datetime import datetime


def hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


def validate_password_stored(password_provided: str, password_stored_hash: str) -> bool:
    """Validate provided password against stored hash."""
    password_hash = hash_password(password_provided)
    return password_hash == password_stored_hash


def generate_session_token() -> str:
    """Generate a secure random session token."""
    # Generate 64 character hex string (256 bits)
    return secrets.token_hex(32)


def create_auth_credentials(username: str, password: str) -> Optional[dict]:
    """
    Create authentication credentials.
    
    Args:
        username: User's chosen username
        password: User's password (will be hashed)
    
    Returns:
        dict with session_token, user_id, stored_password_hash if successful
        None if session token already exists (another session active)
    """
    from store.database import get_connection, validate_session
    
    # Check if session token already exists
    existing_token = validate_session(generate_session_token())
    
    user_id = f"user_{username}"
    password_hash = hash_password(password)
    
    # Generate new unique token
    session_token = generate_session_token()
    
    # Try to create session (will fail if token collision, retry)
    max_retries = 3
    for _ in range(max_retries):
        token_result = get_connection().execute(
            """INSERT INTO sessions 
               (session_token, user_id, username, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                session_token, 
                user_id, 
                username, 
                datetime.now().timestamp() * 1000,
                datetime.now().timestamp() * 1000 + (60 * 60 * 1000)  # 1 hour expiry
            )
        )
        
        # Commit and check for errors
        conn = get_connection()
        conn.commit()
        
        if token_result.rowcount == 1:
            break
        
    return {
        "session_token": session_token,
        "user_id": user_id,
        "username": username,
        "password_hash": password_hash
    }
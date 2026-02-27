"""
portal/agent_auth.py — HMAC-SHA256 request authentication for AD Agent API.

Every agent request must include headers:
    X-Agent-Id:        agent identifier
    X-Agent-Timestamp: ISO 8601 UTC timestamp
    X-Agent-Nonce:     unique 32-char hex nonce
    X-Agent-Signature: HMAC-SHA256 hex digest

Signature is computed over: agent_id + "\n" + timestamp + "\n" + nonce + "\n" + body
using the per-agent shared secret.

Replay protection: timestamp must be within TIMESTAMP_WINDOW seconds of server time,
and nonces are tracked in memory with automatic expiry.
"""

import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from flask import request, jsonify, g, current_app

from database import get_ad_agent, decrypt_value

log = logging.getLogger("portal")

TIMESTAMP_WINDOW = 300  # 5 minutes

# In-memory nonce cache with expiry (keyed by nonce -> expiry timestamp)
_nonce_lock = threading.Lock()
_seen_nonces: dict[str, float] = {}
_NONCE_CLEANUP_INTERVAL = 600  # Clean up every 10 minutes
_last_cleanup = 0.0


def _cleanup_nonces():
    """Remove expired nonces from the cache."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _NONCE_CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    cutoff = now - TIMESTAMP_WINDOW - 60  # Keep nonces a bit longer than window
    expired = [n for n, ts in _seen_nonces.items() if ts < cutoff]
    for n in expired:
        _seen_nonces.pop(n, None)


def _check_nonce(nonce: str) -> bool:
    """Return True if nonce is fresh (not seen before)."""
    with _nonce_lock:
        _cleanup_nonces()
        if nonce in _seen_nonces:
            return False
        _seen_nonces[nonce] = time.time()
        return True


def _verify_signature(agent_id: str, timestamp: str, nonce: str,
                      body: bytes, secret: str) -> bool:
    """Compute and verify HMAC-SHA256 signature."""
    message = f"{agent_id}\n{timestamp}\n{nonce}\n".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    provided = request.headers.get("X-Agent-Signature", "")
    return hmac.compare_digest(expected, provided)


def agent_auth_required(f):
    """Decorator that validates HMAC-signed agent requests.

    Sets g.agent (agent dict) and g.agent_secret (decrypted) on success.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        agent_id = request.headers.get("X-Agent-Id", "").strip()
        timestamp = request.headers.get("X-Agent-Timestamp", "").strip()
        nonce = request.headers.get("X-Agent-Nonce", "").strip()
        signature = request.headers.get("X-Agent-Signature", "").strip()

        # Check all required headers present
        if not all([agent_id, timestamp, nonce, signature]):
            return jsonify({"ok": False, "error": "Missing authentication headers"}), 401

        # Validate timestamp format and window
        try:
            req_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if req_time.tzinfo is None:
                req_time = req_time.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = abs((now - req_time).total_seconds())
            if delta > TIMESTAMP_WINDOW:
                log.warning("Agent %s: timestamp drift %.0fs", agent_id, delta)
                return jsonify({"ok": False, "error": "Request timestamp expired"}), 401
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Invalid timestamp format"}), 401

        # Validate nonce uniqueness (replay protection)
        if len(nonce) < 16 or len(nonce) > 64:
            return jsonify({"ok": False, "error": "Invalid nonce length"}), 401
        if not _check_nonce(nonce):
            log.warning("Agent %s: replayed nonce %s", agent_id, nonce[:8])
            return jsonify({"ok": False, "error": "Nonce already used"}), 401

        # Look up agent
        agent = get_ad_agent(agent_id)
        if not agent:
            log.warning("Agent auth: unknown agent_id %s", agent_id)
            return jsonify({"ok": False, "error": "Unknown agent"}), 401
        if agent["status"] != "active":
            log.warning("Agent auth: revoked agent %s", agent_id)
            return jsonify({"ok": False, "error": "Agent revoked"}), 403

        # Decrypt agent secret
        try:
            secret = decrypt_value(agent["agent_secret_enc"],
                                   current_app.secret_key)
        except Exception:
            log.error("Agent %s: failed to decrypt secret", agent_id)
            return jsonify({"ok": False, "error": "Server error"}), 500

        # Verify HMAC signature
        body = request.get_data()
        if not _verify_signature(agent_id, timestamp, nonce, body, secret):
            log.warning("Agent %s: invalid signature", agent_id)
            return jsonify({"ok": False, "error": "Invalid signature"}), 401

        g.agent = agent
        g.agent_secret = secret
        return f(*args, **kwargs)
    return decorated

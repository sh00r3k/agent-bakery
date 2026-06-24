"""Personal Access Token crypto (Pattern 3 — Personal Access Tokens).

Pure, dependency-light token mint/hash/verify primitives. Kept dashboard-local
(next to ``store``) rather than in agentkit so we don't convert agentkit's
single-file ``auth.py`` into a package for a dashboard-only concern; promote it
to ``agentkit`` only if a sibling agent needs to accept PATs.

Token shape: ``ab_<43-char url-safe base64 of 32 random bytes>``. Only the
``ab_<first 8 of the random part>`` PREFIX is stored in clear (for lookup +
display); the FULL token is hashed with SHA-256 and only the hash is persisted.
The plaintext secret exists only in the mint response — shown to the operator
exactly once, never recoverable.

Future wire-acceptance (not yet enforced on inbound requests): an agent that
accepts PATs would read the ``ab_``-prefixed bearer, ``split_prefix`` it, look
the row up by ``prefix``, ``verify_pat`` against the stored hash, check
``expires_at``/``revoked_at``, then build a ``Principal(sub, tenant, role)``
from the row — encoding tenant+role at mint so the verifier scopes correctly
(BR-002). Wiring that into ``require_session`` is a follow-up.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

PAT_PREFIX = "ab_"  # a token namespace marker, not a secret
_PREFIX_LEN = 8  # chars of the random part kept in clear for lookup/display


def new_pat_secret() -> str:
    """Return a fresh full token: ``ab_`` + 43 url-safe base64 chars (32 bytes)."""
    return PAT_PREFIX + secrets.token_urlsafe(32)


def hash_pat(secret: str) -> str:
    """SHA-256 hex digest of the FULL token (what we persist)."""
    return sha256(secret.encode()).hexdigest()


def split_prefix(secret: str) -> str:
    """The stored lookup prefix = ``ab_`` + first 8 chars of the random part.

    Tolerant of a token that is missing the namespace marker (treats the whole
    string as the random part) so verification can't crash on malformed input.
    """
    body = secret[len(PAT_PREFIX) :] if secret.startswith(PAT_PREFIX) else secret
    return PAT_PREFIX + body[:_PREFIX_LEN]


def mint_pat() -> tuple[str, str, str]:
    """Mint a token: returns ``(full_secret, prefix, token_hash)``.

    Only ``prefix`` + ``token_hash`` are stored; ``full_secret`` is returned to
    the caller for the one-time reveal and then discarded.
    """
    secret = new_pat_secret()
    return secret, split_prefix(secret), hash_pat(secret)


def verify_pat(secret: str, *, stored_hash: str) -> bool:
    """Constant-time check that ``secret`` hashes to ``stored_hash``."""
    return hmac.compare_digest(hash_pat(secret), stored_hash)

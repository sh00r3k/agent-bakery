"""``platform token mint`` — operator JWT via the existing mint script.

Re-uses ``apps/dashboard/scripts/mint-admin-token.py`` (HS256, shared
``JWT_SECRET``) rather than re-implementing JWT, so the signing path stays in one
place. Roles are exactly the script's choices (``admin`` / ``manager``);
``ops`` / ``end-user`` are deliberately not offered (BR-002 tenant stays a JWT
claim minted by the host).

``JWT_SECRET`` is read from the environment by the shelled script — never an
arg, never written anywhere (BR-010). With no ``JWT_SECRET`` the script exits
non-zero and prints nothing to stdout (NS-002).
"""

from __future__ import annotations

import subprocess
import sys

from .config import REPO_ROOT

MINT_SCRIPT = REPO_ROOT / "apps" / "dashboard" / "scripts" / "mint-admin-token.py"

# Mirror the script's own --role choices; do NOT offer ops/end-user.
ROLE_CHOICES: tuple[str, ...] = ("admin", "manager")


def token_mint(
    *,
    sub: str = "operator",
    tenant: str = "platform",
    role: str = "admin",
    ttl: int = 3600,
    audience: str | None = None,
) -> int:
    """Shell the mint script with the given flags; return its exit code.

    The script's stdout (the token) and stderr flow straight through, so on
    success the bare token lands on this process's stdout, and on a missing
    secret nothing is printed to stdout (only the script's stderr message).
    """
    argv = [
        sys.executable,
        str(MINT_SCRIPT),
        "--sub",
        sub,
        "--tenant",
        tenant,
        "--role",
        role,
        "--ttl",
        str(ttl),
    ]
    if audience:
        argv += ["--audience", audience]
    # Fixed interpreter + repo-internal script + argparse-constrained flags; no
    # shell, no untrusted executable. The script reads JWT_SECRET from env itself.
    return subprocess.run(argv, check=False).returncode  # noqa: S603

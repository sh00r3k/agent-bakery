#!/usr/bin/env python3
"""Mint a short-lived admin token for the dashboard login (Plan 4 §4).

For a solo operator: produces a token to paste once into the dashboard's /login
form. Signed with the shared JWT_SECRET (HS256) so the dashboard — and every
agent — verifies it. NOT a login DB; just the host signing path.

Usage:
    JWT_SECRET=... python scripts/mint-admin-token.py [--sub op] [--ttl 3600]

Reads JWT_SECRET from the environment (never hardcode it).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import jwt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="operator", help="token subject (operator id)")
    ap.add_argument("--tenant", default="platform")
    ap.add_argument("--role", default="admin", choices=["admin", "manager"])
    ap.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    ap.add_argument("--audience", default=os.environ.get("JWT_AUDIENCE") or None)
    args = ap.parse_args()

    secret = os.environ.get("JWT_SECRET")
    if not secret:
        print("error: JWT_SECRET not set in environment", file=sys.stderr)
        return 2

    claims = {
        "sub": args.sub,
        "tenant": args.tenant,
        "role": args.role,
        "exp": int(time.time()) + args.ttl,
    }
    if args.audience:
        claims["aud"] = args.audience
    print(jwt.encode(claims, secret, algorithm="HS256"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

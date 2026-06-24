"""``platform`` console-script entry point — argparse subcommand dispatch.

Mirrors the ``main()`` pattern of ``monitoring_agent/__main__.py``: a single
``main()`` returns a process exit code. Subcommands map onto the per-area
modules (``compose`` / ``registry_cmds`` / ``token_cmds`` / ``doctor``).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import compose, doctor, registry_cmds, token_cmds


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform",
        description="Operator CLI for the agent platform.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- up / down -------------------------------------------------------
    for action in ("up", "down"):
        p = sub.add_parser(action, help=f"docker compose {action} (repo-root default compose)")
        p.add_argument("services", nargs="*", help="optional service name(s)")
        p.add_argument(
            "--profile",
            action="append",
            default=[],
            choices=list(compose.KNOWN_PROFILES),
            help="opt-in compose profile (repeatable)",
        )

    # --- agent add | list | remove --------------------------------------
    agent = sub.add_parser("agent", help="manage the dashboard's agent registry")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)

    p_add = agent_sub.add_parser("add", help="append/replace an agent in DASHBOARD_AGENTS")
    p_add.add_argument("slug")
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--kind", default="server", choices=["server", "batch", "self"])
    p_add.add_argument("--port", type=int, default=0)
    p_add.add_argument("--title", default="")
    p_add.add_argument("--feature", action="append", default=[], dest="features")

    p_list = agent_sub.add_parser("list", help="print the registry as the dashboard sees it")
    p_list.add_argument("--json", action="store_true", dest="as_json")

    p_remove = agent_sub.add_parser("remove", help="drop the agent with that slug")
    p_remove.add_argument("slug")

    # --- token mint ------------------------------------------------------
    token = sub.add_parser("token", help="mint operator tokens")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    p_mint = token_sub.add_parser("mint", help="mint an operator login JWT")
    p_mint.add_argument("--sub", default="operator")
    p_mint.add_argument("--tenant", default="platform")
    p_mint.add_argument("--role", default="admin", choices=list(token_cmds.ROLE_CHOICES))
    p_mint.add_argument("--ttl", type=int, default=3600)
    p_mint.add_argument("--audience", default=None)

    # --- doctor ----------------------------------------------------------
    p_doc = sub.add_parser("doctor", help="probe /healthz + /readyz per registered agent")
    p_doc.add_argument("--slug", default=None)
    p_doc.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command in ("up", "down"):
            return compose.compose(args.command, args.services, args.profile)

        if args.command == "agent":
            if args.agent_command == "add":
                return registry_cmds.agent_add(
                    slug=args.slug,
                    url=args.url,
                    kind=args.kind,
                    port=args.port,
                    title=args.title,
                    features=args.features,
                )
            if args.agent_command == "list":
                return registry_cmds.agent_list(as_json=args.as_json)
            if args.agent_command == "remove":
                return registry_cmds.agent_remove(slug=args.slug)

        if args.command == "token" and args.token_command == "mint":  # noqa: S105
            return token_cmds.token_mint(
                sub=args.sub,
                tenant=args.tenant,
                role=args.role,
                ttl=args.ttl,
                audience=args.audience,
            )

        if args.command == "doctor":
            return doctor.doctor(slug=args.slug, as_json=args.as_json)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error("unknown command")  # pragma: no cover - argparse guards this
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

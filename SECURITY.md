# Security policy

## Supported versions

| Version                        | Status        |
| ------------------------------ | ------------- |
| `main` (latest)                | Supported     |
| Tagged releases ≤ latest minor | Supported     |
| Anything older                 | Not supported |

The project is pre-1.0. We support the **current minor** on `main` and
the latest tagged release. We do not maintain security branches for
older versions.

## Reporting a vulnerability

Please **do not open a public issue** for suspected vulnerabilities.

Report privately to the maintainer via the contact listed in
[`README.md`](README.md). The maintainer is the only committer with
merge rights in v0.x; private reports go directly to them.

When reporting, include:

- A short description and impact assessment.
- Steps to reproduce, or a working PoC.
- The affected commit SHA or release tag.
- The component (`packages/agentkit`, `agents/monitoring`,
  `apps/dashboard`, `examples/*`, `infra/*`).
- Any known workarounds.

**Do not include** real secrets, real tenant names, real customer data,
or production URLs in the report. Use placeholders (`acme`, `demo`,
`your-gateway.example.com`).

## Response SLA

| Stage                                  | Target             |
| -------------------------------------- | ------------------ |
| Initial acknowledgement                | within 72 hours    |
| Status update with a plan              | within 7 days      |
| Fix for `Severity: High` or `Critical` | within 30 days     |
| Fix for `Severity: Medium`             | within 90 days     |
| Fix for `Severity: Low`                | next minor release |

Severity is assessed using CVSS 3.1 as a guideline. The maintainer's
call is final; if you disagree, the discussion happens in a private
thread, not in the issue tracker.

## Out of scope

The following are explicitly **out of scope** for the security policy:

- **Prompt-injection and LLM-output manipulation.** The project is a
  runtime; the LLM is a third-party model whose output is not under the
  project's control. See `docs/business-rules.md` (BR-001: AI never
  auto-sends to a user) for the safety model.
- **Denial of service against the bundled examples** (`examples/hello-agent`).
  The example is for local development; do not expose it to the public
  internet.
- **Theoretical vulnerabilities without a working PoC.** Describe the
  impact, not the theory.
- **Dependency CVEs already tracked by the security workflow.** The
  CI runs `pip-audit` against the locked dependency set and `gitleaks`
  against the full git history; if a CVE is already failing CI, open
  a normal issue, not a security report.
- **Issues in third-party services** (LangGraph, FastAPI, Postgres,
  Redis, RabbitMQ, Ollama, the LLM gateway). Report those upstream.

## Coordinated disclosure

We follow **coordinated disclosure**: the maintainer will work with the
reporter on a fix and a release, and agree on a public disclosure date
that is at least 90 days after the report. Credit is given in the
release notes on request.

## Recognition

Security researchers who report valid issues are credited in the
release notes that ship the fix, unless they prefer to remain
anonymous. We do not run a paid bug-bounty program; this is a
maintainer-funded project.

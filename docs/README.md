# Documentation Map — agent-bakery

> Spec-Driven Development. **Read-first** before writing code; spec **ahead of**
> code — change behavior → change the doc first, then the code.

**Date:** 2026-06-15

Public, open-source repo. No business identifiers, real secrets, host IPs, or
real tenant names in any doc — examples use generic tenants `acme` / `demo` and
the gateway `https://your-gateway.example.com/v1` (env-driven).

---

## Reading order

### 1. WHY
| File | About | Read when |
| --- | --- | --- |
| [`vision.md`](vision.md) | Why agent-bakery exists, who it serves, non-goals | First contact |
| [`adr/decisions.md`](adr/decisions.md) | All architecture decisions + rationale (ADR-0001…0011) | "Why was X decided?" |

### 2. WHAT — product spec
| File | About | Read when |
| --- | --- | --- |
| [`user-stories.md`](user-stories.md) | Behavior as roles + Given/When/Then. IDs **US-NNN** / out-of-scope **NS-NNN** | Before adding a feature |
| [`functional-decomposition.md`](functional-decomposition.md) | Capability tree (A toolkit / C monitoring / D dashboard) → US/BR leaves, MVP, deps | "Where does this go?" |
| [`domain-model.md`](domain-model.md) | Entities, fields, invariants, state machines, audit SQL | Before changing the data model |
| [`business-rules.md`](business-rules.md) | Cross-cutting rules + audit queries. IDs **BR-NNN** | "Why is it built/validated this way?" |

### 3. HOW — engineering spec
| File | About | Read when |
| --- | --- | --- |
| [`architecture.md`](architecture.md) | Layers, members, data flow, isolation, conventions | Before working in a member |
| [`constitution.md`](constitution.md) | Iron rules (CR/AR/SAFE/SR/WR) | Before a design call |
| [`principles.md`](principles.md) | Engineering principles + pre-PR checklist | Day one |
| [`api.md`](api.md) | Public API: agentkit Python + monitoring/dashboard HTTP | Before touching a contract |
| [`adr/decisions.md`](adr/decisions.md) | Decision records (append-only) | "Why was X decided?" |
| [`conventions/llm.md`](conventions/llm.md) · [`conventions/tests.md`](conventions/tests.md) | LLM + testing conventions | While coding |

Member-level context lives with the member:
[`../packages/agentkit/README.md`](../packages/agentkit/README.md).

### 4. OPERATE / EXTEND
| File | About | Read when |
| --- | --- | --- |
| [`deploy-your-own.md`](deploy-your-own.md) | Bring up infra, configure & run each agent, edge/TLS | When deploying |
| [`add-your-own-agent.md`](add-your-own-agent.md) | Scaffold a new agent on `agentkit` | When extending (US-012) |
| [`agent-standard.md`](agent-standard.md) | The one agent format — how a member plugs into the platform | When building/porting an agent |

Design notes for shipped features (read on demand):
[`design-private-mode.md`](design-private-mode.md).

Additional-interface designs (ADR-0009/0011):
[`design-distribution.md`](design-distribution.md) — one-command root-compose distribution,
[`design-platform-cli.md`](design-platform-cli.md) — operator `platform` CLI over the registry.

---

## The spec chain

```
vision.md  (+ adr/decisions.md)
  ↓ defines
user-stories.md  ←─ functional-decomposition.md
  ↓ uses               ↓ groups
domain-model.md ──── business-rules.md
  ↓ realized by
architecture.md  →  api.md  →  code
```

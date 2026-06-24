# Ollama RAM tuning on the agent host

Host: `127.0.0.1` — 2 vCPU / 7.8 GB RAM / ~42 GB disk, 4 GB swap.

## Why this matters

Ollama serves **embeddings only** (`nomic-embed-text`) for your agents — no chat
model runs locally (chat goes to the `gateway.example.com` gateway). Per the RAM verdict
in `docs/plan/0-production-plan.md §3`, the agents themselves are cheap
(~110 MiB RSS each). **The single real OOM/thrash risk on this box is Ollama
embedding bursts (+1.2–1.6 GB)**, not the agents:

> a web-ext batch + an agent RAG embed + openviking busy could transiently hit
> ~5–5.5 GB. That does not OOM with 4 GB swap but it thrashes.

By default Ollama will:
- run **multiple requests in parallel** (`OLLAMA_NUM_PARALLEL`, auto: often 4),
  each allocating its own KV/compute working set, and
- keep **multiple models resident** at once (`OLLAMA_MAX_LOADED_MODELS`, auto),

On a 2-core / 7.8 GB box that auto-scaling is exactly the thrash trigger. We cap
both to 1: embeddings are throughput-tolerant (batch + RAG ingest can serialize),
and serializing them trades a little latency for a hard ceiling on Ollama's
resident + working RAM.

## What to set

```
OLLAMA_NUM_PARALLEL=1        # serialize concurrent requests -> one working set
OLLAMA_MAX_LOADED_MODELS=1   # only one model in memory at a time
OLLAMA_KEEP_ALIVE=30s        # evict the model 30s after idle (don't pin RAM)
# Optional: confine to the one model your agents use.
# OLLAMA_MAX_QUEUE=64        # bound the wait queue instead of unbounded growth
```

`OLLAMA_KEEP_ALIVE` is included because with `MAX_LOADED_MODELS=1` an idle but
still-resident model keeps ~0.5–1 GB pinned for no reason between bursts; a short
keep-alive returns that RAM to the host (the next embed pays a ~1 s reload —
acceptable for batch/RAG, not user-facing).

## How to apply (host-side — operator runs these)

Ollama on this host runs as a **systemd service** (not in the agent_backend
compose; agents reach it via `host.docker.internal:11434`). Override via a
systemd drop-in so it survives package upgrades. Hand the operator one combined
command (no passwordless sudo in this environment):

```sh
sudo sh -c 'install -d /etc/systemd/system/ollama.service.d && \
cat > /etc/systemd/system/ollama.service.d/ram-cap.conf <<EOF
[Service]
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_KEEP_ALIVE=30s"
EOF
systemctl daemon-reload && systemctl restart ollama'
```

Verify after restart:

```sh
systemctl show ollama -p Environment        # shows the three vars
curl -s http://127.0.0.1:11434/api/ps       # at most ONE model resident
```

> If Ollama instead runs as a docker container on this host, set the same three
> as `environment:` entries on its service and `docker compose up -d` it — do
> NOT add it to the agent compose files; it stays its own unit.

## Companion mitigations (not Ollama config, same goal)

These are tracked elsewhere but are the other half of the burst defense:
- **Serialize heavy crons** so embed-heavy jobs never overlap: security scan
  03:00, pm digest 04:00, web-ext batch manual/off-peak.
- **Per-container `mem_limit`** stays as the OOM backstop (compose `deploy`).
- **Monitor host vitals** (the monitoring agent watches `available` RAM / swap);
  host-upgrade trigger is sustained `available < 1.5 GB` or swap-in-use
  > 512 MiB for > 1h (plan §3). Capping Ollama is what makes a bigger host
  unnecessary now.

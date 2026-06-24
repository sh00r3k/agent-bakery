"""Dashboard panel routes (v0.2).

Two HTMX-friendly endpoints that back the partials added in 4b1765c:
- ``GET /panels/workspaces`` — workspace list (JSON) backing
  ``_workspace_switcher.html``.
- ``GET /panels/node-health`` — agents node-health HTML partial backing
  ``_node_health.html`` (htmx auto-polls every 5s on the client).

Auth: both routes use ``page_principal`` (session cookie, redirects to
``/login`` if missing). The dashboard's existing ``Aggregator.agents_health``
provides the data for the node-health panel — this module does not
introduce a new data path.

References:
- design-ui-inspiration.md §"Pattern 1 — Workspace as top-level container"
- design-ui-inspiration.md §"Pattern 2 — Live cluster node health"
"""

from __future__ import annotations

from agentkit.auth import Principal
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .api import _session_dep, page_principal, templates

router = APIRouter(prefix="/panels", tags=["panels"])


@router.get("/workspaces", response_class=JSONResponse)
async def list_workspaces(
    request: Request,
    principal: Principal = Depends(page_principal),
) -> JSONResponse:
    """Return the list of workspaces available to the principal.

    v0.2 source: only the current tenant (``principal.tenant``). The
    template renders a switcher partial that is functional for the
    single-tenant case; cross-tenant switching lands in v0.3 alongside
    the tenant-registry DB table (design-ui-inspiration.md §"Pattern 1").

    Return shape (kept stable for the template):

        {
          "workspaces": [
            {"tenant_id": "acme", "display_name": "acme", "is_current": true},
            ...
          ]
        }
    """
    workspaces = [
        {
            "tenant_id": principal.tenant,
            "display_name": principal.tenant,
            "is_current": True,
        }
    ]
    # TODO v0.3: source from a tenant-registry table or settings.WORKSPACES.
    return JSONResponse({"workspaces": workspaces, "principal_tenant": principal.tenant})


@router.get("/node-health", response_class=HTMLResponse)
async def node_health_partial(
    request: Request,
    principal: Principal = Depends(_session_dep),
) -> HTMLResponse:
    """Render ``_node_health.html`` from ``Aggregator.agents_health``.

    The aggregator runs a live HTTP fan-out to every agent in the registry on
    each call (htmx polls this every 5s), so the result IS the freshness signal.
    We shape each ``HealthDTO`` into the row the template expects::

        {"name", "status": green|amber|red|grey, "uptime_s", "error"}

    If the fan-out itself fails we degrade to an ``unavailable`` panel instead
    of 500-ing (which would freeze htmx on the loading placeholder).
    """
    aggregator = request.app.state.aggregator
    registry = request.app.state.registry
    try:
        agents = await aggregator.agents_health(registry)
    except Exception as exc:  # pragma: no cover - defensive degrade path
        return templates.TemplateResponse(
            request, "_node_health.html", {"nodes": [], "unavailable": str(exc)}
        )

    nodes = [
        {
            "name": h.slug,
            "status": h.status,  # green | amber | red | grey (HealthDTO.status)
            "uptime_s": h.uptime_s,
            "error": h.error,
        }
        for h in agents
        if h.kind != "self"
    ]
    return templates.TemplateResponse(
        request, "_node_health.html", {"nodes": nodes, "unavailable": None}
    )

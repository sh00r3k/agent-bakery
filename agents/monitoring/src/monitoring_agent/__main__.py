"""Entry point: `python -m monitoring_agent` serves the API."""

from __future__ import annotations

import uvicorn

from .settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("monitoring_agent.api:app", host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()

"""@spec BR-008 — Sentry + Alertmanager parsing, probe->signal thresholds.

Parsing tests for the collectors module — Sentry + Alertmanager payloads
and the probe->signal threshold logic. No network involved."""

from __future__ import annotations

import asyncio

import monitoring_agent.collectors as collectors_mod
import pytest
from monitoring_agent.collectors import (
    ProbeResult,
    is_probeable_url,
    parse_alert,
    parse_sentry,
    probe_target,
    probe_to_signal,
    sweep,
)


def test_parse_sentry_basic() -> None:
    payload = {
        "data": {
            "event": {
                "title": "TypeError: cannot read property of undefined",
                "culprit": "vpn.service in createSubscription",
                "level": "error",
                "project": "backend",
                "web_url": "https://sentry.io/issues/123/",
                "count": 7,
                "fingerprint": ["abc", "def"],
            }
        }
    }
    sig = parse_sentry(payload)
    assert sig.source == "sentry"
    assert sig.title.startswith("TypeError")
    assert sig.suggested_severity == "warning"  # error -> warning
    assert sig.url == "https://sentry.io/issues/123/"
    assert sig.fingerprint == "abc:def"
    assert sig.meta["count"] == 7
    assert "backend" in sig.body


def test_parse_sentry_fatal_is_critical_and_derives_fingerprint() -> None:
    payload = {
        "data": {
            "event": {
                "title": "OOMKilled",
                "culprit": "worker",
                "level": "fatal",
                "project": "worker",
            }
        }
    }
    sig = parse_sentry(payload)
    assert sig.suggested_severity == "critical"
    # No fingerprint list -> derived from project:culprit
    assert sig.fingerprint == "worker:worker"


def test_parse_sentry_flat_payload_defaults() -> None:
    sig = parse_sentry({"message": "weird thing"})
    assert sig.title == "weird thing"
    assert sig.suggested_severity == "warning"  # default error->warning
    assert sig.source == "sentry"


def test_parse_alert_alertmanager_v4() -> None:
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "instance": "app.example.com",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "Error rate above 5%",
                    "description": "5xx spiking",
                },
                "fingerprint": "deadbeef",
                "generatorURL": "https://prom/graph",
            },
            {
                "status": "firing",
                "labels": {"alertname": "DiskWarn", "severity": "warning"},
                "annotations": {"summary": "Disk 80%"},
            },
        ]
    }
    sigs = parse_alert(payload)
    assert len(sigs) == 2
    assert sigs[0].suggested_severity == "critical"
    assert sigs[0].fingerprint == "deadbeef"
    assert sigs[0].url == "https://prom/graph"
    assert sigs[1].suggested_severity == "warning"
    assert sigs[1].fingerprint == "DiskWarn:"


def test_parse_alert_single_flat_object() -> None:
    sigs = parse_alert({"name": "Lonely", "severity": "info"})
    assert len(sigs) == 1
    assert sigs[0].suggested_severity == "info"
    assert sigs[0].source == "alert"


def test_probe_to_signal_down() -> None:
    res = ProbeResult(
        target="https://app.example.com",
        ok=False,
        status_code=503,
        latency_seconds=0.2,
        tls_expiry_days=90,
        error=None,
    )
    sig = probe_to_signal(res, slow_threshold_seconds=2.0, cert_warn_days=14)
    assert sig is not None
    assert sig.suggested_severity == "critical"
    assert sig.fingerprint == "down:https://app.example.com"
    assert "DOWN" in sig.title


def test_probe_to_signal_slow() -> None:
    res = ProbeResult(
        target="https://example.com",
        ok=True,
        status_code=200,
        latency_seconds=3.5,
        tls_expiry_days=90,
    )
    sig = probe_to_signal(res, slow_threshold_seconds=2.0, cert_warn_days=14)
    assert sig is not None
    assert sig.suggested_severity == "warning"
    assert sig.fingerprint.startswith("slow:")


def test_probe_to_signal_cert_expiring_critical_under_3d() -> None:
    res = ProbeResult(
        target="https://example.com",
        ok=True,
        status_code=200,
        latency_seconds=0.1,
        tls_expiry_days=2,
    )
    sig = probe_to_signal(res, slow_threshold_seconds=2.0, cert_warn_days=14)
    assert sig is not None
    assert sig.suggested_severity == "critical"
    assert sig.fingerprint.startswith("cert:")


def test_probe_to_signal_healthy_returns_none() -> None:
    res = ProbeResult(
        target="https://example.com",
        ok=True,
        status_code=200,
        latency_seconds=0.1,
        tls_expiry_days=90,
    )
    assert probe_to_signal(res, slow_threshold_seconds=2.0, cert_warn_days=14) is None


# --------------------------------------------------------------------------- #
# SSRF hygiene (AF-07): scheme allowlist + no redirect following
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example.com",
        "https://app.example.com:8443/health",
        "http://127.0.0.1:8000",
    ],
)
def test_is_probeable_url_allows_http_schemes(url: str) -> None:
    assert is_probeable_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://internal:70/_data",
        "ftp://files.example.com/x",
        "http://",  # no host
        "not-a-url",
        "",
    ],
)
def test_is_probeable_url_rejects_non_http_or_hostless(url: str) -> None:
    assert is_probeable_url(url) is False


async def test_probe_target_rejects_non_http_scheme_without_network() -> None:
    # A config-influenced file:// target must never reach httpx.
    res = await probe_target("file:///etc/passwd")
    assert res.ok is False
    assert res.status_code is None
    assert res.error is not None and "http" in res.error


# --------------------------------------------------------------------------- #
# sweep concurrency cap
# --------------------------------------------------------------------------- #


async def test_sweep_caps_concurrent_probes(monkeypatch) -> None:
    """With concurrency=N, no more than N probes are ever in flight at once."""
    in_flight = 0
    peak = 0

    async def fake_probe(target, *, timeout=10.0, client=None) -> ProbeResult:  # noqa: ASYNC109 - matches probe_target's signature
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield so other tasks can pile up if unbounded
        in_flight -= 1
        return ProbeResult(
            target=target, ok=True, status_code=200, latency_seconds=0.0, tls_expiry_days=90
        )

    monkeypatch.setattr(collectors_mod, "probe_target", fake_probe)
    targets = [f"https://t{i}.example.com" for i in range(20)]

    signals = await sweep(targets, slow_threshold_seconds=2.0, cert_warn_days=14, concurrency=4)
    assert peak <= 4
    assert signals == []  # all healthy


async def test_sweep_unbounded_when_concurrency_nonpositive(monkeypatch) -> None:
    """concurrency<=0 keeps the historical unbounded gather (no semaphore)."""
    in_flight = 0
    peak = 0

    async def fake_probe(target, *, timeout=10.0, client=None) -> ProbeResult:  # noqa: ASYNC109 - matches probe_target's signature
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return ProbeResult(
            target=target, ok=True, status_code=200, latency_seconds=0.0, tls_expiry_days=90
        )

    monkeypatch.setattr(collectors_mod, "probe_target", fake_probe)
    targets = [f"https://t{i}.example.com" for i in range(10)]

    await sweep(targets, slow_threshold_seconds=2.0, cert_warn_days=14, concurrency=0)
    assert peak == 10  # all started together

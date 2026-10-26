"""Attempt-aware fetching: timeouts, exponential backoff, retry limits, outcome classification.
temporary_error (timeout, 5xx, 429, network) -> retried later with backoff up to RESEARCH_MAX_RETRIES;
not_found (404/410) and blocked (401/403: needs key/login) are NOT retried without a reason;
invalid = response received but unusable (e.g. not an image). Every attempt is stored."""
from __future__ import annotations
import os, time
from datetime import timedelta
import httpx
from sqlalchemy import select
from ..db import utcnow
from .models import ResearchAttempt

MAX_RETRIES = int(os.getenv("RESEARCH_MAX_RETRIES", "3"))
BACKOFF_S = float(os.getenv("RESEARCH_BACKOFF_SECONDS", "60"))
TIMEOUT_S = float(os.getenv("RESEARCH_TIMEOUT_SECONDS", "15"))


def classify(status: int | None, exc: Exception | None = None) -> str:
    if exc is not None:
        return "temporary_error"
    if status is None:
        return "temporary_error"
    if 200 <= status < 300:
        return "success"
    if status in (404, 410):
        return "not_found"
    if status in (400, 422):
        return "invalid"
    if status in (401, 402, 403):
        return "blocked"
    return "temporary_error"   # 408, 425, 429, 5xx


def history(s, job_id, target):
    return list(s.scalars(select(ResearchAttempt).where(ResearchAttempt.job_id == job_id, ResearchAttempt.target == target)
                          .order_by(ResearchAttempt.id)).all())


def should_try(s, job_id, target, force=False) -> tuple[bool, str]:
    h = history(s, job_id, target)
    if not h:
        return True, "first attempt"
    last = h[-1]
    if last.outcome == "success":
        return False, "already succeeded"
    if last.outcome in ("not_found", "blocked", "invalid") and not force:
        return False, f"last outcome {last.outcome}: not repeated without a reason"
    if len(h) > MAX_RETRIES:
        return False, f"retry limit {MAX_RETRIES} reached"
    nra = last.next_retry_at
    if nra is not None and (nra if nra.tzinfo else nra.replace(tzinfo=utcnow().tzinfo)) > utcnow():
        return False, "waiting for backoff"
    return True, f"retry #{len(h)} after {last.outcome}"


def record(s, job_id, check, source, target, outcome, http_status=None, error=None, by="bot",
           alternative_for=None, duration_ms=None):
    n = len(history(s, job_id, target)) + 1
    nra = None
    if outcome == "temporary_error" and n <= MAX_RETRIES:
        nra = utcnow() + timedelta(seconds=BACKOFF_S * (2 ** (n - 1)))
    a = ResearchAttempt(job_id=job_id, check=check, source=source, target=target[:1000], attempt_no=n, by=by,
                        outcome=outcome, http_status=http_status, error=(error or "")[:500] or None,
                        alternative_for=alternative_for, next_retry_at=nra, duration_ms=duration_ms)
    s.add(a)
    return a


async def fetch(client: httpx.AsyncClient, url: str, **kw) -> tuple[str, httpx.Response | None, str | None, int]:
    t0 = time.monotonic()
    try:
        r = await client.get(url, timeout=TIMEOUT_S, **kw)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        return "temporary_error", None, f"{type(e).__name__}: {e}", int((time.monotonic() - t0) * 1000)
    return classify(r.status_code), r, None if r.status_code < 300 else f"HTTP {r.status_code}", int((time.monotonic() - t0) * 1000)


def pending_retries(s, job_id) -> list:
    """Targets whose latest attempt was temporary and still has retries left."""
    out = {}
    for a in s.scalars(select(ResearchAttempt).where(ResearchAttempt.job_id == job_id).order_by(ResearchAttempt.id)).all():
        out[a.target] = a
    return [a for a in out.values() if a.outcome == "temporary_error" and a.attempt_no <= MAX_RETRIES]


def summary(s, job_id) -> list[dict]:
    rows = s.scalars(select(ResearchAttempt).where(ResearchAttempt.job_id == job_id).order_by(ResearchAttempt.id)).all()
    return [{"check": a.check, "source": a.source, "target": a.target, "attempt": a.attempt_no, "by": a.by,
             "outcome": a.outcome, "http_status": a.http_status, "error": a.error,
             "at": a.created_at.isoformat() if a.created_at else None} for a in rows]

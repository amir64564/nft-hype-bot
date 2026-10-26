"""Research tables. All additive; results and reviews are append-only."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base, utcnow

JOB_STEPS = [
    ("identity", "Identify project & official accounts"),
    ("x_data", "Collect X data (official posts, mentions, counts)"),
    ("x_interest", "Analyze X interest & discussion quality"),
    ("team", "Research artist / team"),
    ("art", "Review artwork & originality"),
    ("market", "Collect market / mint data"),
    ("category", "Classify category & stage"),
    ("history", "Compare with history & peers"),
    ("social", "Sorsa social graph & smart followers (separate signal)"),
    ("score", "Score hype & quality with category profile"),
    ("report", "Build evidence report & verdict"),
]


class ResearchJob(Base):
    __tablename__ = "research_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    # RESEARCHING | RETRYING | READY_FOR_SCORING | INCOMPLETE | FINALIZED  (legacy: queued/researching/done)
    status: Mapped[str] = mapped_column(String(20), default="RESEARCHING", index=True)
    steps: Mapped[dict] = mapped_column(JSON, default=dict)          # step -> {status, note, at}
    x_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hints: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # user-given links / slugs
    channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    guild_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_rendered: Mapped[str | None] = mapped_column(Text, nullable=True)
    feeder_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    unavailable_sources: Mapped[list] = mapped_column(JSON, default=list)  # [{source, reason}]
    result_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)
    report_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # Discord report msg to edit on updates
    posted_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True)  # skip re-post when unchanged
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Evidence(Base):
    """One sourced observation. kind: observation (verifiable fact with source) | assumption |
    judgment (qualitative, e.g. art review by an agent). verified=False when no source URL."""
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    section: Mapped[str] = mapped_column(String(20), index=True)   # identity|x_interest|team|art|market|category
    kind: Mapped[str] = mapped_column(String(12))
    claim: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    posted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    engagement: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal: Mapped[str | None] = mapped_column(String(40), nullable=True)   # machine key, e.g. team_prior_collection
    value: Mapped[float | None] = mapped_column(Float, nullable=True)        # -1..1 polarity/rating when given
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    collected_by: Mapped[str] = mapped_column(String(40))                    # bot:<collector> | feeder
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResearchResult(Base):
    __tablename__ = "research_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(30))
    stage: Mapped[str] = mapped_column(String(20))
    verdict: Mapped[str] = mapped_column(String(40))   # Tier | Needs More Research | Manual Verification Required
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)   # before hysteresis
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    hype_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_label: Mapped[str] = mapped_column(String(20))
    report: Mapped[dict] = mapped_column(JSON)
    finalization_status: Mapped[str] = mapped_column(String(24), default="LEGACY", server_default="LEGACY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ResearchAttempt(Base):
    """Every fetch attempt (bot or feeder-reported) with its outcome. Append-only."""
    __tablename__ = "research_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("research_jobs.id"), index=True)
    check: Mapped[str] = mapped_column(String(30))          # checklist key, e.g. marketplace, artwork
    source: Mapped[str] = mapped_column(String(80))         # e.g. OpenSea stats, image
    target: Mapped[str] = mapped_column(String(1000))       # URL or identifier
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    by: Mapped[str] = mapped_column(String(20), default="bot")   # bot | feeder
    outcome: Mapped[str] = mapped_column(String(20))        # success|temporary_error|not_found|blocked|invalid
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    alternative_for: Mapped[str | None] = mapped_column(String(80), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ManualReview(Base):
    __tablename__ = "manual_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("research_results.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(20))    # confirm|change_tier|reject|note
    bot_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bot_verdict: Mapped[str] = mapped_column(String(40))
    reviewer_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


@event.listens_for(ResearchAttempt, "before_update")
@event.listens_for(ResearchAttempt, "before_delete")
@event.listens_for(ResearchResult, "before_update")
@event.listens_for(ManualReview, "before_update")
@event.listens_for(ResearchResult, "before_delete")
@event.listens_for(ManualReview, "before_delete")
@event.listens_for(Evidence, "before_delete")
def _append_only(mapper, conn, target):
    raise RuntimeError(f"{target.__tablename__} rows are append-only")


# ---------------------------------------------------------------------------------------------
# Social graph / account tracking (Sorsa integration, r2.1). Extends the same DB; no new database.
# ---------------------------------------------------------------------------------------------
RELIABILITY_LABELS = ("provisional", "low", "medium", "high")


class TrackedAccount(Base):
    """An X account whose follows/mentions may be an early-discovery signal. Being tracked (e.g. from
    Rudra's seed list) does NOT make an account 'smart': reliability comes only from recorded history."""
    __tablename__ = "tracked_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    x_handle: Mapped[str] = mapped_column(String(32), unique=True, index=True)      # lowercase, no @
    x_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    followers_count: Mapped[int | None] = mapped_column(Integer, nullable=True)     # last observed (X or Sorsa)
    sorsa_score: Mapped[float | None] = mapped_column(Float, nullable=True)         # None = never fetched / unavailable
    sorsa_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    category_hints: Mapped[list | None] = mapped_column(JSON, nullable=True)        # e.g. ["nft_collector","alpha_caller"]
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)               # seed_list | history_mining | sorsa_top_followers ...
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    history: Mapped[dict | None] = mapped_column(JSON, nullable=True)               # computed discovery stats (accounts.py)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)                   # 0..1 confidence in the history record
    reliability_label: Mapped[str] = mapped_column(String(16), default="provisional")
    flags: Mapped[dict | None] = mapped_column(JSON, nullable=True)                 # spam_promoter, coordinated_suspect, ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AccountProjectLink(Base):
    """One observed relationship between a tracked account and a project, with its source. A follow
    date is only set when a source (Sorsa follower date / 7d new followers) states it."""
    __tablename__ = "account_project_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("tracked_accounts.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    relationship: Mapped[str] = mapped_column(String(16))      # followed | mentioned | engaged
    source: Mapped[str] = mapped_column(String(40))            # sorsa_top_followers | sorsa_new_followers | independent_research | seed_list | history_mining
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    follow_date: Mapped[str | None] = mapped_column(String(40), nullable=True)   # verified follow time (ISO) or None
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SorsaProjectSnapshot(Base):
    """Append-only pack of Sorsa data for a project handle. Sorsa = ONE source family."""
    __tablename__ = "sorsa_project_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True, nullable=True)
    username: Mapped[str] = mapped_column(String(32), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    week_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    month_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    followers_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    top_followers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    new_followers_7d: Mapped[list | None] = mapped_column(JSON, nullable=True)
    unavailable: Mapped[list | None] = mapped_column(JSON, nullable=True)        # [{endpoint, reason}]
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    api_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    remaining_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SorsaCache(Base):
    """Response cache keyed by (endpoint, username). Errors that are permanent (404/not indexed) are
    cached too so the same miss is not paid for twice within the TTL."""
    __tablename__ = "sorsa_cache"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(40), index=True)
    username: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SorsaApiLog(Base):
    """Every Sorsa request decision: live call, cache hit, or quota/no-key skip. Append-only."""
    __tablename__ = "sorsa_api_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    endpoint: Mapped[str] = mapped_column(String(40))
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24))        # http status or cached|skipped_quota|no_key|error
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    remaining_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


@event.listens_for(SorsaProjectSnapshot, "before_update")
@event.listens_for(SorsaProjectSnapshot, "before_delete")
@event.listens_for(SorsaApiLog, "before_update")
@event.listens_for(SorsaApiLog, "before_delete")
@event.listens_for(AccountProjectLink, "before_delete")
def _append_only_social(mapper, conn, target):
    raise RuntimeError(f"{target.__tablename__} rows are append-only")

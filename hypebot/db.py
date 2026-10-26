"""Persistence (SQLAlchemy; PostgreSQL or SQLite). Snapshots are append-only."""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine, event, func, inspect, select, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    x_username: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # lowercase
    x_display_handle: Mapped[str] = mapped_column(String(32))
    x_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    discord_invite: Mapped[str | None] = mapped_column(String(100), nullable=True)
    identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="project", order_by="Snapshot.created_at")


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    raw_data: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON)
    components: Mapped[dict] = mapped_column(JSON)
    missing: Mapped[list] = mapped_column(JSON)
    flags: Mapped[list] = mapped_column(JSON)
    weights: Mapped[dict] = mapped_column(JSON)
    hype_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_label: Mapped[str] = mapped_column(String(20))
    provisional: Mapped[bool] = mapped_column(Boolean)
    trend: Mapped[str] = mapped_column(String(20))
    score_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # algorithm v2+: rows created before versioning are tagged 'v1-legacy' by the migration default
    algorithm_version: Mapped[str] = mapped_column(String(20), default="v1-legacy", server_default="v1-legacy")
    evaluation: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # coverage, ranked, confidence parts...
    project: Mapped[Project] = relationship(back_populates="snapshots")
    is_recalculation = False


class SnapshotRecalc(Base):
    """A historical snapshot re-scored with a newer algorithm. The original row is never touched."""
    __tablename__ = "snapshot_recalcs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    algorithm_version: Mapped[str] = mapped_column(String(20))
    recalculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # = source snapshot time
    metrics: Mapped[dict] = mapped_column(JSON)
    components: Mapped[dict] = mapped_column(JSON)
    missing: Mapped[list] = mapped_column(JSON)
    flags: Mapped[list] = mapped_column(JSON)
    weights: Mapped[dict] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON)
    hype_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    confidence_label: Mapped[str] = mapped_column(String(20))
    provisional: Mapped[bool] = mapped_column(Boolean)
    evaluation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    original_hype_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_algorithm_version: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    trend = "recalculated"
    score_delta = None
    previous_tier = None
    is_recalculation = True


@event.listens_for(SnapshotRecalc, "before_update")
@event.listens_for(Snapshot, "before_update")
def _no_update(mapper, conn, target):
    raise RuntimeError("Snapshots are append-only and cannot be modified")


@event.listens_for(SnapshotRecalc, "before_delete")
@event.listens_for(Snapshot, "before_delete")
def _no_delete(mapper, conn, target):
    raise RuntimeError("Snapshots are append-only and cannot be deleted")


class Database:
    def __init__(self, url: str):
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        self.engine = create_engine(url, future=True, **kw)
        from .research import models as _research_models  # noqa: F401  (register research tables)
        Base.metadata.create_all(self.engine)  # all models are defined at import time
        self._migrate()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def _migrate(self):
        """Additive, non-destructive migration: add version/evaluation columns to old DBs."""
        cols = {c["name"] for c in inspect(self.engine).get_columns("snapshots")}
        with self.engine.begin() as c:
            if "algorithm_version" not in cols:
                c.execute(text("ALTER TABLE snapshots ADD COLUMN algorithm_version VARCHAR(20) NOT NULL DEFAULT 'v1-legacy'"))
            if "evaluation" not in cols:
                c.execute(text("ALTER TABLE snapshots ADD COLUMN evaluation JSON"))
            insp = inspect(self.engine)
            if "research_results" in insp.get_table_names():
                rc = {x["name"] for x in insp.get_columns("research_results")}
                if "finalization_status" not in rc:
                    c.execute(text("ALTER TABLE research_results ADD COLUMN finalization_status VARCHAR(24) NOT NULL DEFAULT 'LEGACY'"))
            if "research_jobs" in insp.get_table_names():
                jc = {x["name"] for x in insp.get_columns("research_jobs")}
                if "report_message_id" not in jc:
                    c.execute(text("ALTER TABLE research_jobs ADD COLUMN report_message_id VARCHAR(32)"))
                if "posted_fingerprint" not in jc:
                    c.execute(text("ALTER TABLE research_jobs ADD COLUMN posted_fingerprint VARCHAR(32)"))
                # map r1.0 job statuses to the r2.0 state machine (history kept in steps._legacy_status)
                c.execute(text("""UPDATE research_jobs SET status = CASE
                    WHEN status IN ('queued','researching','scoring') THEN 'RESEARCHING'
                    WHEN status = 'failed' THEN 'INCOMPLETE'
                    WHEN status = 'done' AND result_id IN (SELECT id FROM research_results WHERE verdict='Tier') THEN 'FINALIZED'
                    WHEN status = 'done' THEN 'INCOMPLETE' ELSE status END
                    WHERE status IN ('queued','researching','scoring','done','failed')"""))

    def get_project(self, s: Session, key: str) -> Project | None:
        k = key.strip().lstrip("@").lower()
        p = s.scalar(select(Project).where(Project.x_username == k))
        if p:
            return p
        return s.scalar(select(Project).where(func.lower(Project.name) == k))

    def upsert_project(self, s: Session, name: str, handle: str, **fields) -> Project:
        p = s.scalar(select(Project).where(Project.x_username == handle.lower()))
        if not p:
            p = Project(name=name, x_username=handle.lower(), x_display_handle=handle)
            s.add(p)
        p.name = name or p.name
        for k, v in fields.items():
            if v is not None:
                setattr(p, k, v)
        s.flush()
        return p

    def latest_snapshot(self, s: Session, project_id: int, version: str | None = None) -> Snapshot | None:
        q = select(Snapshot).where(Snapshot.project_id == project_id)
        if version:
            q = q.where(Snapshot.algorithm_version == version)
        return s.scalar(q.order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1))

    def latest_current(self, s: Session, project_id: int, version: str):
        """Latest result computed by `version`: a live snapshot, else a recalculation of an old one."""
        snap = self.latest_snapshot(s, project_id, version)
        rc = s.scalar(select(SnapshotRecalc).where(SnapshotRecalc.project_id == project_id,
                                                   SnapshotRecalc.algorithm_version == version)
                      .order_by(SnapshotRecalc.created_at.desc(), SnapshotRecalc.id.desc()).limit(1))
        if snap and rc:
            a = snap.created_at.replace(tzinfo=None); b = rc.created_at.replace(tzinfo=None)
            return snap if a >= b else rc
        return snap or rc

    def history(self, s: Session, project_id: int, limit: int = 20) -> list[Snapshot]:
        rows = s.scalars(select(Snapshot).where(Snapshot.project_id == project_id)
                         .order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(limit)).all()
        return list(reversed(rows))

    def top(self, s: Session, limit: int = 10, include_provisional: bool = True,
            version: str | None = None) -> list[tuple[Project, Snapshot]]:
        res = []
        for p in s.scalars(select(Project)).all():
            snap = self.latest_current(s, p.id, version) if version else self.latest_snapshot(s, p.id)
            if snap and snap.tier is not None and (include_provisional or not snap.provisional):
                res.append((p, snap))
        res.sort(key=lambda ps: (ps[1].hype_score, ps[1].confidence), reverse=True)
        return res[:limit]


class XRequest(Base):
    """Pending request for externally collected X data (used when no X API token)."""
    __tablename__ = "x_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    x_username: Mapped[str] = mapped_column(String(32))
    project_name: Mapped[str] = mapped_column(String(200))
    channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    guild_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    interaction_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provisional_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending|done|failed
    result_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

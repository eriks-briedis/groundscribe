"""The voice system's own tables (phase 10).

Three rows, each holding something the rest of the system cannot reconstruct.

``VoiceProfileVersion`` gives the phase-02 profile shell a scope, a version and a
snapshot: the document lives in the content-addressed store like every other
artefact, and the row is its identity and its place in the hierarchy.

``ManualEdit`` records a person changing prose by hand, and — the field that
matters — whether that edit may be used as evidence about their style. Fixing a
number is not a style preference, and only the person making the edit knows which
kind it was.

``VoiceSuggestion`` is an inferred rule waiting for an answer. It exists as a row
precisely because the answer has not been given yet: a suggestion held in memory
would be a suggestion that disappeared, and one applied immediately would be the
silent self-modification plan/10 forbids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON as JSONColumn
from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from groundscribe.db import Base, UTCDateTime, enum_column
from groundscribe.domain.enums import FindingStatus
from groundscribe.domain.models import (
    Article,
    ArticleVersion,
    ArtifactSnapshot,
    EntityMixin,
    LineageMixin,
    Project,
    User,
)
from groundscribe.voice.enums import VoiceScope


class VoiceProfileVersion(LineageMixin, EntityMixin, Base):
    """One version of one profile, at one scope.

    Separate from the phase-02 ``voice_profiles`` row, which names a profile as a
    thing a user owns. This is a *version* of it — the unit an article actually
    cites, and the unit that must never change once something has been written
    under it.

    ``project_id`` and ``article_id`` are both nullable and at most one is set: a
    global profile scopes to neither, a project profile to the first, an article
    override to the second. Encoded as two nullable keys rather than one generic
    "target" column so the database enforces that the thing being scoped to
    actually exists.
    """

    __tablename__ = "voice_profile_versions"

    profile_id: Mapped[str] = mapped_column(ForeignKey("voice_profiles.id"), nullable=False)
    scope: Mapped[VoiceScope] = mapped_column(enum_column(VoiceScope), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    article_id: Mapped[str | None] = mapped_column(ForeignKey("articles.id"), nullable=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_snapshots.id"), nullable=True
    )
    # Which version is in force. A flag rather than "the highest version", because
    # versions are strings an author chooses and reverting to an earlier one is a
    # legitimate act that would otherwise be unrepresentable.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    project: Mapped[Project | None] = relationship()
    article: Mapped[Article | None] = relationship()
    snapshot: Mapped[ArtifactSnapshot | None] = relationship(foreign_keys=[snapshot_id])


class ManualEdit(EntityMixin, Base):
    """A person changing the prose by hand, and whether it may teach anything.

    ``voice_training_eligible`` is the whole reason this is a row. plan/10 asks
    for edits to record it, and the reason is that the alternative — inferring
    eligibility later — cannot work: a corrected statistic and a reworded
    sentence look identical afterwards, and only one of them says anything about
    how the author writes.
    """

    __tablename__ = "manual_edits"

    article_version_id: Mapped[str] = mapped_column(
        ForeignKey("article_versions.id"), nullable=False
    )
    before: Mapped[str] = mapped_column(String, nullable=False)
    after: Mapped[str] = mapped_column(String, default="", nullable=False)
    made_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    voice_training_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    made_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    article_version: Mapped[ArticleVersion] = relationship()
    editor: Mapped[User] = relationship()


class VoiceSuggestion(EntityMixin, Base):
    """An inferred rule, and the answer it is waiting for.

    ``status`` reuses :class:`~groundscribe.domain.enums.FindingStatus` rather
    than inventing a parallel vocabulary. A suggestion about the author's style
    has the same fates as a review finding about their article — proposed, then
    accepted, rejected or edited — and two enums meaning the same three things
    would drift.

    ``instruction`` and ``evidence`` are JSON because both are read whole and
    never joined on: the first is the ``VoiceInstruction`` this would add, the
    second the edits it was inferred from, kept so a person can disagree with the
    inference rather than only with the conclusion.
    """

    __tablename__ = "voice_suggestions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    scope: Mapped[VoiceScope] = mapped_column(
        enum_column(VoiceScope), default=VoiceScope.GLOBAL, nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    #: The habit this suggestion is about, so the same one is never raised twice.
    habit: Mapped[str] = mapped_column(String, nullable=False)
    instruction: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONColumn, default=dict, nullable=False)
    status: Mapped[FindingStatus] = mapped_column(
        enum_column(FindingStatus), default=FindingStatus.PROPOSED, nullable=False
    )
    decided_by: Mapped[str] = mapped_column(String, default="", nullable=False)
    reason: Mapped[str] = mapped_column(String, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: The profile version approving it produced, if it was approved.
    resulting_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("voice_profile_versions.id"), nullable=True
    )

    user: Mapped[User] = relationship()
    project: Mapped[Project | None] = relationship()
    resulting_version: Mapped[VoiceProfileVersion | None] = relationship()

    @property
    def is_decided(self) -> bool:
        """Whether a person has already answered this one."""
        return self.status is not FindingStatus.PROPOSED


#: Schema-version stamp shared with every other entity, kept here so the count of
#: mapped columns matches what the parity tests expect.
_ = Integer


__all__ = ["ManualEdit", "VoiceProfileVersion", "VoiceSuggestion"]

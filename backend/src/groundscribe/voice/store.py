"""Where profiles live, and which one reaches the prose (phase 10).

plan/10 → implementation tasks 2 and 7. The resolver decides precedence between
documents; this module decides *which documents*, which is the join a voice
system usually gets wrong in the way nobody notices — the profile is saved, the
article is written, and the two are never connected.

A profile version is stored the way every other readable artefact is: the
document goes into the content-addressed snapshot store, and the row carries its
identity, its scope and whether it is the version in force. That gives a profile
the same integrity guarantee as the article written under it, which matters
because "written under ada@3" is a claim about a document that has to still be
checkable years later.

**One active version per scope**, enforced on save. Two would leave the resolver
choosing between them, and an article's record of which voice produced it would
stop meaning anything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundscribe.domain.enums import ArtifactType, BranchStatus
from groundscribe.domain.models import VoiceProfile
from groundscribe.provenance.recorder import ProvenanceRecorder
from groundscribe.storage.snapshot_store import SnapshotStore
from groundscribe.voice.enums import VoiceScope
from groundscribe.voice.models import VoiceProfileVersion
from groundscribe.voice.precedence import ResolvedVoice, resolve_voice
from groundscribe.voice.schemas import VoiceProfileDocument


class VoiceStore:
    """Saves profile versions and answers which ones apply."""

    def __init__(
        self,
        session: Session,
        *,
        snapshots: SnapshotStore,
        recorder: ProvenanceRecorder,
    ) -> None:
        self._session = session
        self._snapshots = snapshots
        self._recorder = recorder

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def save(
        self,
        document: VoiceProfileDocument,
        *,
        user_id: str,
        project_id: str | None = None,
        article_id: str | None = None,
        execution_id: str | None = None,
    ) -> VoiceProfileVersion:
        """Store a new version and put it in force at its scope.

        The previous version at that scope is retired rather than deleted:
        superseded is not the same as gone, and an article written last month
        still needs the document it names to be readable.
        """
        owner = self._owner(document, user_id)
        previous = self._active(
            document.scope, user_id=user_id, project_id=project_id, article_id=article_id
        )
        snapshot = self._snapshots.write(
            artifact_type=ArtifactType.VOICE_PROFILE,
            content=document.model_dump_json().encode("utf-8"),
            created_by_execution_id=execution_id,
            parent=previous.snapshot if previous is not None else None,
        )

        if previous is not None:
            previous.active = False
            previous.branch_status = BranchStatus.SUPERSEDED
            self._session.flush()

        version = VoiceProfileVersion(
            id=uuid.uuid4().hex,
            profile_id=owner.id,
            scope=document.scope,
            project_id=project_id if document.scope is not VoiceScope.GLOBAL else None,
            article_id=article_id if document.scope is VoiceScope.ARTICLE else None,
            version=document.version,
            snapshot_id=snapshot.id,
            active=True,
            parent_id=previous.id if previous is not None else None,
            created_by_execution_id=execution_id,
        )
        self._session.add(version)
        self._session.flush()
        return version

    def document(self, version: VoiceProfileVersion) -> VoiceProfileDocument:
        """The document a stored version holds."""
        if version.snapshot is None:
            raise ValueError(f"voice profile version {version.id} has no stored document")
        return VoiceProfileDocument.model_validate_json(self._snapshots.read(version.snapshot))

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def resolve(
        self,
        *,
        user_id: str,
        project_id: str | None = None,
        article_id: str | None = None,
    ) -> ResolvedVoice:
        """The effective voice for one article, project or author.

        An author with nothing saved resolves to an empty voice rather than an
        error. plan/10's calibration produces the first profile, and requiring
        one before anything could run would make onboarding a precondition
        instead of a first result.
        """
        return resolve_voice(
            global_profile=self._document_at(VoiceScope.GLOBAL, user_id=user_id),
            project_profile=(
                self._document_at(VoiceScope.PROJECT, user_id=user_id, project_id=project_id)
                if project_id
                else None
            ),
            article_profile=(
                self._document_at(
                    VoiceScope.ARTICLE,
                    user_id=user_id,
                    project_id=project_id,
                    article_id=article_id,
                )
                if article_id
                else None
            ),
        )

    def versions(self, *, user_id: str) -> tuple[VoiceProfileVersion, ...]:
        """Every version this author has saved, newest scope-wise last."""
        return tuple(
            self._session.scalars(
                select(VoiceProfileVersion)
                .join(VoiceProfile, VoiceProfile.id == VoiceProfileVersion.profile_id)
                .where(VoiceProfile.user_id == user_id)
                .order_by(VoiceProfileVersion.scope, VoiceProfileVersion.id)
            )
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _document_at(
        self,
        scope: VoiceScope,
        *,
        user_id: str,
        project_id: str | None = None,
        article_id: str | None = None,
    ) -> VoiceProfileDocument | None:
        version = self._active(scope, user_id=user_id, project_id=project_id, article_id=article_id)
        return self.document(version) if version is not None else None

    def _active(
        self,
        scope: VoiceScope,
        *,
        user_id: str,
        project_id: str | None,
        article_id: str | None,
    ) -> VoiceProfileVersion | None:
        """The version in force at one exact scope.

        Matched on the scope's own key, not on "the narrowest thing that
        matches": an article override belongs to its article, and one that
        answered for a second article would be the invisible bug this module
        exists to prevent.
        """
        stmt = (
            select(VoiceProfileVersion)
            .join(VoiceProfile, VoiceProfile.id == VoiceProfileVersion.profile_id)
            .where(
                VoiceProfile.user_id == user_id,
                VoiceProfileVersion.scope == scope,
                VoiceProfileVersion.active.is_(True),
            )
        )
        if scope is VoiceScope.GLOBAL:
            stmt = stmt.where(VoiceProfileVersion.project_id.is_(None))
        else:
            stmt = stmt.where(VoiceProfileVersion.project_id == project_id)
        if scope is VoiceScope.ARTICLE:
            stmt = stmt.where(VoiceProfileVersion.article_id == article_id)
        else:
            stmt = stmt.where(VoiceProfileVersion.article_id.is_(None))
        return self._session.scalars(stmt).first()

    def _owner(self, document: VoiceProfileDocument, user_id: str) -> VoiceProfile:
        """The phase-02 profile row a version belongs to, created on first use.

        Named rather than generated, so two versions of "ada" are two versions of
        one thing. The phase-02 row is the *name*; this module's row is a version
        of it.
        """
        existing = self._session.scalars(
            select(VoiceProfile).where(
                VoiceProfile.user_id == user_id, VoiceProfile.name == document.name
            )
        ).first()
        if existing is not None:
            return existing

        profile = VoiceProfile(
            id=uuid.uuid4().hex,
            user_id=user_id,
            name=document.name,
            description=document.description,
        )
        self._session.add(profile)
        self._session.flush()
        return profile


__all__ = ["VoiceStore"]

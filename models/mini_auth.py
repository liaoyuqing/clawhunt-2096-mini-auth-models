"""SQLAlchemy 2 models for the mini-program member space.

Three tables, every one of them org scoped and soft deletable:

* mini_users               - one WeChat mini-program identity per org
* member_auth_applications - a member enterprise-authentication request
* mini_login_events        - append-only login trail

The load-bearing rules are expressed in the database, not in a service layer:

* uq_mini_users_org_openid   - one live openid per org
* uq_mini_auth_one_submitted - at most one submitted application per user
* phone_hash                 - an HMAC-SHA256 digest, never a phone number

Two of those are partial (WHERE deleted_at IS NULL / WHERE status = submitted)
so that a soft-deleted row does not block a returning user, and so that the
review history of a user keeps growing after the first decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Allowed values of mini_users.auth_status.
AUTH_STATUS_VALUES: tuple[str, ...] = ("unverified", "pending", "verified", "rejected")
#: Allowed values of mini_users.enterprise_role.
ENTERPRISE_ROLE_VALUES: tuple[str, ...] = ("admin", "member")
#: Allowed values of member_auth_applications.status.
APPLICATION_STATUS_VALUES: tuple[str, ...] = ("submitted", "approved", "rejected", "withdrawn")
#: Allowed values of mini_login_events.result (a check constraint, not an enum:
#: the ticket only asks for enumerations on the two tables above).
LOGIN_RESULT_VALUES: tuple[str, ...] = ("success", "failure", "locked")

# create_type=False everywhere: the Alembic migration owns CREATE TYPE / DROP TYPE,
# so importing these models never emits DDL.
AUTH_STATUS_ENUM = postgresql.ENUM(*AUTH_STATUS_VALUES, name="auth_status", create_type=False)
ENTERPRISE_ROLE_ENUM = postgresql.ENUM(
    *ENTERPRISE_ROLE_VALUES, name="enterprise_role", create_type=False
)
APPLICATION_STATUS_ENUM = postgresql.ENUM(
    *APPLICATION_STATUS_VALUES, name="auth_application_status", create_type=False
)

#: Postgres-native UUID. Kept as the explicit dialect type so that alembic check
#: compares this metadata against the reflected catalogue without a type false alarm.
UUID = postgresql.UUID(as_uuid=True)

#: Only the primary-key name is conventionalised; every other constraint and index is
#: named explicitly below so the migration and the models agree character for character.
METADATA = MetaData(naming_convention={"pk": "pk_%(table_name)s"})


class Base(DeclarativeBase):
    """Declarative base shared by the three tables."""

    metadata = METADATA


class OrgScopedMixin:
    """The columns every business table in this platform carries.

    Present on all three tables: id, org_id, site_id, created_at, created_by,
    updated_at and deleted_at (soft delete).
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        doc="Surrogate key; generated inside PostgreSQL (gen_random_uuid, no extension needed).",
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID, nullable=False, doc="Tenant (member-space operator) this row belongs to."
    )
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, nullable=True, doc="Optional sub-site; null when the row is org wide."
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID, nullable=True, doc="Actor id; null for rows created by the mini-program itself."
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="Soft delete marker. Every uniqueness rule below is partial on this column.",
    )


class MiniUser(OrgScopedMixin, Base):
    """A WeChat mini-program user of one member space."""

    __tablename__ = "mini_users"

    openid: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="WeChat openid; unique per org among live rows."
    )
    unionid: Mapped[str | None] = mapped_column(
        String(128), nullable=True, doc="WeChat unionid when the account is bound to an open platform."
    )
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    position: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="Job title as typed by the member."
    )
    bio: Mapped[str | None] = mapped_column(
        String(200), nullable=True, doc="Short biography; the 200 character cap is a check constraint.",
    )
    auth_status: Mapped[str] = mapped_column(
        AUTH_STATUS_ENUM,
        nullable=False,
        server_default=text("'unverified'::auth_status"),
        doc="unverified / pending / verified / rejected.",
    )
    member_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID,
        nullable=True,
        doc="Nullable link into S2-CRM contacts. Deliberately no foreign key: CRM is a stub here.",
    )
    enterprise_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, nullable=True, doc="Enterprise the member belongs to, once authentication succeeds.",
    )
    enterprise_role: Mapped[str | None] = mapped_column(
        ENTERPRISE_ROLE_ENUM, nullable=True, doc="admin / member; null before authentication.",
    )
    phone_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="HMAC-SHA256(phone, PHONE_HASH_KEY), 64 lowercase hex chars. Null when no phone is bound.",
    )
    phone_bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("char_length(bio) <= 200", name="ck_mini_users_bio_len"),
        CheckConstraint(
            "phone_hash IS NULL OR phone_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mini_users_phone_hash_format",
        ),
        Index(
            "uq_mini_users_org_openid",
            "org_id",
            "openid",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_mini_users_org_phone_hash", "org_id", "phone_hash"),
        Index("ix_mini_users_org_auth_status", "org_id", "auth_status"),
    )


class MemberAuthApplication(OrgScopedMixin, Base):
    """A member enterprise-authentication application and its review trail."""

    __tablename__ = "member_auth_applications"

    mini_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey(
            "mini_users.id",
            name="fk_member_auth_applications_mini_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    real_name: Mapped[str] = mapped_column(String(64), nullable=False)
    enterprise_name: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proof_file_ids: Mapped[list[uuid.UUID]] = mapped_column(
        postgresql.ARRAY(UUID),
        nullable=False,
        server_default=text("'{}'::uuid[]"),
        doc="Uploaded proof file ids; at most three, enforced by a check constraint.",
    )
    remark: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(
        APPLICATION_STATUS_ENUM,
        nullable=False,
        server_default=text("'submitted'::auth_application_status"),
        doc="submitted / approved / rejected / withdrawn.",
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    matched_enterprise_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, nullable=True, doc="Enterprise matched by the reviewer; null until approved.",
    )

    __table_args__ = (
        CheckConstraint(
            "cardinality(proof_file_ids) <= 3",
            name="ck_member_auth_applications_proof_files_max",
        ),
        Index(
            "uq_mini_auth_one_submitted",
            "mini_user_id",
            unique=True,
            postgresql_where=text("status = 'submitted' AND deleted_at IS NULL"),
        ),
        Index(
            "ix_member_auth_applications_org_status_created_at",
            "org_id",
            "status",
            "created_at",
        ),
        Index("ix_member_auth_applications_mini_user_id", "mini_user_id"),
    )


class MiniLoginEvent(OrgScopedMixin, Base):
    """Append-only login trail for the mini-program."""

    __tablename__ = "mini_login_events"

    mini_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey(
            "mini_users.id",
            name="fk_mini_login_events_mini_user_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    client_ip: Mapped[str | None] = mapped_column(postgresql.INET(), nullable=True)
    result: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'success'"),
        doc="success / failure / locked.",
    )

    __table_args__ = (
        CheckConstraint(
            "result IN ('success', 'failure', 'locked')",
            name="ck_mini_login_events_result",
        ),
        Index("ix_mini_login_events_mini_user_id_login_at", "mini_user_id", "login_at"),
        Index("ix_mini_login_events_org_login_at", "org_id", "login_at"),
    )

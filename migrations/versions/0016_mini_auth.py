"""mini auth: mini_users, member_auth_applications, mini_login_events

Revision ID: 0016_mini_auth
Revises:
Create Date: 2026-11-22 10:00:00.000000

Creates the three tables of the mini-program member space, the partial unique
indexes that carry the business rules, and the three PostgreSQL enum types.

The downgrade is deliberately ordered: indexes, then tables (children first),
then the enum types. PostgreSQL keeps an unreferenced enum type after the table
that used it is dropped, so a downgrade that skipped DROP TYPE would leave
auth_status / enterprise_role / auth_application_status behind and the next
upgrade would fail with "type ... already exists".
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_mini_auth"
down_revision = None
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)

AUTH_STATUS = postgresql.ENUM(
    "unverified", "pending", "verified", "rejected",
    name="auth_status", create_type=False,
)
ENTERPRISE_ROLE = postgresql.ENUM(
    "admin", "member",
    name="enterprise_role", create_type=False,
)
APPLICATION_STATUS = postgresql.ENUM(
    "submitted", "approved", "rejected", "withdrawn",
    name="auth_application_status", create_type=False,
)

ENUM_TYPES = (AUTH_STATUS, ENTERPRISE_ROLE, APPLICATION_STATUS)


def _org_columns() -> list[sa.Column]:
    """The org-scoped columns every table in this schema carries."""
    return [
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("site_id", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Enum types are created explicitly so DROP TYPE has an obvious counterpart.
    for enum_type in ENUM_TYPES:
        enum_type.create(bind, checkfirst=False)

    # 2. mini_users
    op.create_table(
        "mini_users",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        *_org_columns(),
        sa.Column("openid", sa.String(length=128), nullable=False),
        sa.Column("unionid", sa.String(length=128), nullable=True),
        sa.Column("nickname", sa.String(length=64), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("position", sa.String(length=64), nullable=True),
        sa.Column("bio", sa.String(length=200), nullable=True),
        sa.Column("auth_status", AUTH_STATUS, nullable=False,
                  server_default=sa.text("'unverified'::auth_status")),
        sa.Column("member_contact_id", UUID, nullable=True),
        sa.Column("enterprise_id", UUID, nullable=True),
        sa.Column("enterprise_role", ENTERPRISE_ROLE, nullable=True),
        sa.Column("phone_hash", sa.String(length=64), nullable=True),
        sa.Column("phone_bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("char_length(bio) <= 200", name="ck_mini_users_bio_len"),
        sa.CheckConstraint(
            "phone_hash IS NULL OR phone_hash ~ '^[0-9a-f]{64}$'",
            name="ck_mini_users_phone_hash_format",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mini_users"),
    )

    # 3. member_auth_applications
    op.create_table(
        "member_auth_applications",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        *_org_columns(),
        sa.Column("mini_user_id", UUID, nullable=False),
        sa.Column("real_name", sa.String(length=64), nullable=False),
        sa.Column("enterprise_name", sa.String(length=128), nullable=False),
        sa.Column("position", sa.String(length=64), nullable=True),
        sa.Column("proof_file_ids", postgresql.ARRAY(UUID), nullable=False,
                  server_default=sa.text("'{}'::uuid[]")),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("status", APPLICATION_STATUS, nullable=False,
                  server_default=sa.text("'submitted'::auth_application_status")),
        sa.Column("reviewed_by", UUID, nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.String(length=500), nullable=True),
        sa.Column("matched_enterprise_id", UUID, nullable=True),
        sa.CheckConstraint(
            "cardinality(proof_file_ids) <= 3",
            name="ck_member_auth_applications_proof_files_max",
        ),
        sa.ForeignKeyConstraint(
            ["mini_user_id"], ["mini_users.id"],
            name="fk_member_auth_applications_mini_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_member_auth_applications"),
    )

    # 4. mini_login_events
    op.create_table(
        "mini_login_events",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        *_org_columns(),
        sa.Column("mini_user_id", UUID, nullable=False),
        sa.Column("login_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("client_ip", postgresql.INET(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False,
                  server_default=sa.text("'success'")),
        sa.CheckConstraint(
            "result IN ('success', 'failure', 'locked')",
            name="ck_mini_login_events_result",
        ),
        sa.ForeignKeyConstraint(
            ["mini_user_id"], ["mini_users.id"],
            name="fk_mini_login_events_mini_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mini_login_events"),
    )

    # 5. Indexes. The two partial unique indexes are the point of the ticket.
    op.create_index(
        "uq_mini_users_org_openid",
        "mini_users",
        ["org_id", "openid"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_mini_users_org_phone_hash", "mini_users", ["org_id", "phone_hash"])
    op.create_index("ix_mini_users_org_auth_status", "mini_users", ["org_id", "auth_status"])

    op.create_index(
        "uq_mini_auth_one_submitted",
        "member_auth_applications",
        ["mini_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'submitted' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_member_auth_applications_org_status_created_at",
        "member_auth_applications",
        ["org_id", "status", "created_at"],
    )
    op.create_index(
        "ix_member_auth_applications_mini_user_id",
        "member_auth_applications",
        ["mini_user_id"],
    )

    op.create_index(
        "ix_mini_login_events_mini_user_id_login_at",
        "mini_login_events",
        ["mini_user_id", "login_at"],
    )
    op.create_index(
        "ix_mini_login_events_org_login_at",
        "mini_login_events",
        ["org_id", "login_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()

    # 1. DROP INDEX explicitly, before the tables, so the intent is visible in the
    #    migration history even though DROP TABLE would take the indexes with it.
    op.drop_index("ix_mini_login_events_org_login_at", table_name="mini_login_events")
    op.drop_index("ix_mini_login_events_mini_user_id_login_at", table_name="mini_login_events")
    op.drop_index("ix_member_auth_applications_mini_user_id", table_name="member_auth_applications")
    op.drop_index(
        "ix_member_auth_applications_org_status_created_at",
        table_name="member_auth_applications",
    )
    op.drop_index("uq_mini_auth_one_submitted", table_name="member_auth_applications")
    op.drop_index("ix_mini_users_org_auth_status", table_name="mini_users")
    op.drop_index("ix_mini_users_org_phone_hash", table_name="mini_users")
    op.drop_index("uq_mini_users_org_openid", table_name="mini_users")

    # 2. DROP TABLE children first so the foreign keys never block.
    op.drop_table("mini_login_events")
    op.drop_table("member_auth_applications")
    op.drop_table("mini_users")

    # 3. DROP TYPE: PostgreSQL would otherwise keep these three types alive and the
    #    next upgrade would raise "type auth_status already exists".
    for enum_type in ENUM_TYPES:
        enum_type.drop(bind, checkfirst=False)

"""Model and migration tests for the mini-program member space.

Four groups, one per acceptance criterion:

  AC1  the migration round trip (upgrade / downgrade -1 / upgrade) and alembic check
  AC2  the partial unique index on (org_id, openid)
  AC3  the partial unique index on submitted applications
  AC4  phone_hash: no plaintext column, 64 hex characters, keyed HMAC

Every number quoted in the delivery text is produced here and written to
docs/checks/assertions.json, so the prose and the run cannot drift apart.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from conftest import EVIDENCE, TEST_ONLY_KEY, run_alembic
from models import MemberAuthApplication, MiniLoginEvent, MiniUser
from models.phone_hash import ENV_VAR, hash_phone, is_hash

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
SITE = uuid.UUID("33333333-3333-4333-8333-333333333333")


def make_user(session: Session, openid: str, org_id: uuid.UUID = ORG, **kwargs) -> MiniUser:
    user = MiniUser(org_id=org_id, site_id=SITE, openid=openid, **kwargs)
    session.add(user)
    session.flush()
    return user


def constraint_name(exc: IntegrityError) -> str:
    """The constraint an IntegrityError tripped, from the driver diagnostics."""
    diag = getattr(exc.orig, "diag", None)
    return diag.constraint_name if diag is not None else ""


def sqlstate(exc: IntegrityError) -> str:
    return getattr(exc.orig, "sqlstate", "") or ""


def _submit(session: Session, user: MiniUser, enterprise: str) -> MemberAuthApplication:
    application = MemberAuthApplication(
        org_id=user.org_id,
        site_id=SITE,
        mini_user_id=user.id,
        real_name="Fictional Applicant",
        enterprise_name=enterprise,
        proof_file_ids=[],
    )
    session.add(application)
    session.flush()
    return application


# ---------------------------------------------------------------------------
# AC1 - the migration runs up, down and up again
# ---------------------------------------------------------------------------


def _maintenance_url(database_url: str, database: str, *, libpq: bool = False) -> str:
    """Point the URL at another database.

    *libpq* drops the SQLAlchemy driver suffix; psycopg.connect() cannot parse
    "postgresql+psycopg://" and wants a plain conninfo URL.
    """
    from sqlalchemy.engine import make_url

    url = make_url(database_url).set(database=database)
    if libpq:
        url = url.set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def _fresh_database(database_url: str) -> tuple[str, str]:
    """Create a scratch database for the round trip; fall back to the main one.

    A scratch database keeps the round trip honest: dropping the schema cannot
    disturb the tables the other tests use. When the role may not CREATE DATABASE
    the test still runs, on the main database, and records which one it used.
    """
    name = f"mini_auth_roundtrip_{os.getpid()}"
    admin = _maintenance_url(database_url, "postgres", libpq=True)
    try:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
            connection.execute(f'CREATE DATABASE "{name}"')
    except psycopg.Error:
        return database_url, ""
    return _maintenance_url(database_url, name), name


def _drop_database(database_url: str, name: str) -> None:
    if not name:
        return
    admin = _maintenance_url(database_url, "postgres", libpq=True)
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _schema_snapshot(database_url: str) -> dict:
    """Tables, indexes and enum types as the live catalogue reports them."""
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = sorted(inspector.get_table_names())
        indexes = {
            table: sorted(index["name"] for index in inspector.get_indexes(table))
            for table in tables
        }
        with engine.connect() as connection:
            enums = sorted(
                row[0]
                for row in connection.execute(
                    text("SELECT typname FROM pg_type WHERE typtype = 'e' ORDER BY typname")
                )
            )
    finally:
        engine.dispose()
    return {"tables": tables, "indexes": indexes, "enums": enums}


def _enum_count(database_url: str) -> int:
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_type WHERE typtype = 'e' "
                        "AND typname IN ('auth_status', 'enterprise_role', "
                        "'auth_application_status')"
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def test_migration_roundtrip_and_no_drift(database_url: str) -> None:
    """upgrade head, downgrade -1, upgrade head, all through the CLI, all exit 0."""
    scratch_url, scratch_name = _fresh_database(database_url)
    EVIDENCE["roundtrip_isolated_database"] = int(bool(scratch_name))
    try:
        up1 = run_alembic("upgrade", "head", database_url=scratch_url)
        assert up1.returncode == 0, up1.stdout + up1.stderr
        fresh = _schema_snapshot(scratch_url)

        enums_before = _enum_count(scratch_url)
        down = run_alembic("downgrade", "-1", database_url=scratch_url)
        assert down.returncode == 0, down.stdout + down.stderr
        enums_after = _enum_count(scratch_url)

        up2 = run_alembic("upgrade", "head", database_url=scratch_url)
        assert up2.returncode == 0, up2.stdout + up2.stderr
        roundtrip = _schema_snapshot(scratch_url)

        check = run_alembic("check", database_url=scratch_url)
        assert check.returncode == 0, check.stdout + check.stderr
        assert "No new upgrade operations detected" in (check.stdout + check.stderr)

        EVIDENCE["alembic_upgrade_head_exit"] = up1.returncode
        EVIDENCE["alembic_downgrade_minus1_exit"] = down.returncode
        EVIDENCE["alembic_upgrade_head_again_exit"] = up2.returncode
        EVIDENCE["alembic_check_exit"] = check.returncode
        EVIDENCE["enums_dropped"] = enums_before - enums_after
        EVIDENCE["objects_identical"] = int(roundtrip == fresh)
        EVIDENCE["tables_after_roundtrip"] = len(roundtrip["tables"])
        EVIDENCE["indexes_after_roundtrip"] = sum(len(v) for v in roundtrip["indexes"].values())

        # The downgrade must really remove the three enum types, or the next upgrade
        # would die with "type auth_status already exists".
        assert enums_before == 3, f"expected 3 enum types after upgrade, saw {enums_before}"
        assert enums_after == 0, f"downgrade left {enums_after} enum types behind"
        assert EVIDENCE["enums_dropped"] == 3

        assert roundtrip == fresh, (
            "round trip changed the schema:",
            {key: (fresh[key], roundtrip[key]) for key in fresh if fresh[key] != roundtrip[key]},
        )
        assert EVIDENCE["objects_identical"] == 1
    finally:
        _drop_database(database_url, scratch_name)


def test_unified_columns_present_on_all_three_tables(engine) -> None:
    """Every business table carries the seven platform columns."""
    required = {
        "id", "org_id", "site_id", "created_at", "created_by", "updated_at", "deleted_at",
    }
    inspector = inspect(engine)
    for table in ("mini_users", "member_auth_applications", "mini_login_events"):
        names = {column["name"] for column in inspector.get_columns(table)}
        missing = required - names
        assert not missing, f"{table} is missing {sorted(missing)}"
    EVIDENCE["tables_with_unified_columns"] = 3


def test_partial_unique_index_definitions(engine) -> None:
    """The two partial indexes exist with the exact predicates the ticket asks for."""
    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE indexname IN ('uq_mini_users_org_openid', "
                    "'uq_mini_auth_one_submitted')"
                )
            ).all()
        )
    openid_def = rows["uq_mini_users_org_openid"]
    submitted_def = rows["uq_mini_auth_one_submitted"]

    assert openid_def == (
        "CREATE UNIQUE INDEX uq_mini_users_org_openid ON public.mini_users "
        "USING btree (org_id, openid) WHERE (deleted_at IS NULL)"
    ), openid_def
    assert submitted_def == (
        "CREATE UNIQUE INDEX uq_mini_auth_one_submitted ON public.member_auth_applications "
        "USING btree (mini_user_id) WHERE ((status = 'submitted'::auth_application_status) "
        "AND (deleted_at IS NULL))"
    ), submitted_def
    EVIDENCE["partial_indexes_verified"] = 2


# ---------------------------------------------------------------------------
# AC2 - a duplicate openid in the same org hits the unique constraint
# ---------------------------------------------------------------------------


def test_duplicate_openid_same_org_raises_23505(engine) -> None:
    """Two rows with the same (org_id, openid): the second raises 23505, named."""
    with Session(engine) as session:
        make_user(session, "oDuplicateProbe01")
        session.commit()

        session.add(MiniUser(org_id=ORG, site_id=SITE, openid="oDuplicateProbe01"))
        with pytest.raises(IntegrityError) as excinfo:
            session.flush()
        session.rollback()

    assert sqlstate(excinfo.value) == "23505", sqlstate(excinfo.value)
    assert constraint_name(excinfo.value) == "uq_mini_users_org_openid", constraint_name(excinfo.value)
    EVIDENCE["duplicate_openid_sqlstate"] = sqlstate(excinfo.value)
    EVIDENCE["duplicate_openid_constraint"] = constraint_name(excinfo.value)


def test_same_openid_in_another_org_is_allowed(engine) -> None:
    """The uniqueness is per org, not global."""
    with Session(engine) as session:
        make_user(session, "oCrossOrgProbe01", org_id=ORG)
        make_user(session, "oCrossOrgProbe01", org_id=OTHER_ORG)
        session.commit()
        total = session.execute(text("SELECT count(*) FROM mini_users")).scalar_one()
    assert total == 2
    EVIDENCE["cross_org_ok"] = 1


def test_soft_deleted_openid_can_be_reused(engine) -> None:
    """The predicate is deleted_at IS NULL, so a departed member can come back."""
    with Session(engine) as session:
        first = make_user(session, "oReturningProbe01")
        session.commit()
        first.deleted_at = datetime(2026, 11, 20, tzinfo=timezone.utc)
        session.commit()

        make_user(session, "oReturningProbe01")
        session.commit()
        live = session.execute(
            text("SELECT count(*) FROM mini_users WHERE deleted_at IS NULL")
        ).scalar_one()
    assert live == 1
    EVIDENCE["soft_deleted_reusable"] = 1


def test_ten_concurrent_inserts_leave_exactly_one_row(database_url: str, engine) -> None:
    """Ten racing inserts of the same pair: the index allows one winner, not ten."""
    # psycopg wants a libpq conninfo, not a SQLAlchemy URL: drop the +psycopg suffix.
    from sqlalchemy.engine import make_url

    conninfo = make_url(database_url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )

    # psycopg's async connection refuses the Windows Proactor loop by default; the
    # selector policy is the documented requirement, and is a no-op elsewhere.
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def race() -> list[str]:
        outcomes: list[str] = []

        async def attempt(index: int) -> None:
            # A dedicated connection per attempt: the race must reach the server,
            # not be serialised by a shared session.
            connection = await psycopg.AsyncConnection.connect(conninfo)
            try:
                try:
                    await connection.execute(
                        "INSERT INTO mini_users (org_id, site_id, openid) VALUES (%s, %s, %s)",
                        (ORG, SITE, "oConcurrentProbe01"),
                    )
                    await connection.commit()
                    outcomes.append("committed")
                except psycopg.errors.UniqueViolation as exc:
                    await connection.rollback()
                    outcomes.append(exc.diag.constraint_name or "unique_violation")
            finally:
                await connection.close()

        await asyncio.gather(*(attempt(index) for index in range(10)))
        return outcomes

    outcomes = asyncio.run(race())

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT count(*) FROM mini_users")).scalar_one()

    winners = outcomes.count("committed")
    EVIDENCE["concurrent_attempts"] = len(outcomes)
    EVIDENCE["concurrent_winners"] = winners
    assert len(outcomes) == 10
    assert winners == 1, outcomes
    assert rows == 1
    assert outcomes.count("uq_mini_users_org_openid") == 9, outcomes


# ---------------------------------------------------------------------------
# AC3 - a second submitted application is refused
# ---------------------------------------------------------------------------


def test_second_submitted_application_rejected(engine) -> None:
    """First submit succeeds; the second raises 23505 on uq_mini_auth_one_submitted."""
    with Session(engine) as session:
        user = make_user(session, "oOneSubmittedProbe")
        _submit(session, user, "Fictional Trading Co. No.1")
        session.commit()

        with pytest.raises(IntegrityError) as excinfo:
            _submit(session, user, "Fictional Trading Co. No.2")
        session.rollback()

    assert sqlstate(excinfo.value) == "23505", sqlstate(excinfo.value)
    assert constraint_name(excinfo.value) == "uq_mini_auth_one_submitted", constraint_name(excinfo.value)
    EVIDENCE["second_submitted_sqlstate"] = sqlstate(excinfo.value)
    EVIDENCE["second_submitted_constraint"] = constraint_name(excinfo.value)


def test_decision_frees_the_slot_for_a_new_application(engine) -> None:
    """approved / rejected / withdrawn each allow one more submission."""
    allowed: list[str] = []
    for status in ("approved", "rejected", "withdrawn"):
        with Session(engine) as session:
            user = make_user(session, f"oDecisionProbe-{status}")
            first = _submit(session, user, "Fictional Trading Co.")
            first.status = status
            if status == "rejected":
                first.reject_reason = "Proof does not match."
            first.reviewed_by = uuid.uuid4()
            first.reviewed_at = datetime(2026, 11, 21, tzinfo=timezone.utc)
            session.commit()

            # The decided row no longer satisfies the predicate, so a new one fits.
            _submit(session, user, "Fictional Trading Co. Again")
            session.commit()
            submitted = session.execute(
                text(
                    "SELECT count(*) FROM member_auth_applications "
                    "WHERE mini_user_id = :uid AND status = 'submitted'"
                ),
                {"uid": user.id},
            ).scalar_one()
        assert submitted == 1
        allowed.append(status)

    EVIDENCE["statuses"] = allowed
    EVIDENCE["new_allowed"] = len(allowed)
    assert allowed == ["approved", "rejected", "withdrawn"]
    assert len(allowed) == 3


def test_narrow_definition_is_what_makes_the_rule_work(engine) -> None:
    """Two control tables show which half of the index is load bearing.

    * no unique constraint at all  -> two submitted rows accepted (the service-
      layer check that this ticket replaces would pass the same sequence);
    * a union unique on mini_user_id alone, with no status predicate -> the
      re-application after a decision is refused, so members could never appeal.

    Both controls fail a rule the real index satisfies, which is why the index has
    to be both unique and narrowed to status = submitted AND deleted_at IS NULL.

    Note on the proposal wording: it described the wrong key as
    (mini_user_id, status). That is not quite right - a composite key on those two
    columns would also refuse two submitted rows. The controls below are the two
    designs that genuinely break, and they are what this test measures.
    """
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    user_id = uuid.uuid4()

    # Control A: no constraint. Two submitted rows are accepted.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS control_no_index"))
        connection.execute(
            text(
                "CREATE TABLE control_no_index ("
                "  mini_user_id uuid NOT NULL, status text NOT NULL)"
            )
        )
        for _ in range(2):
            connection.execute(
                text("INSERT INTO control_no_index VALUES (:uid, 'submitted')"),
                {"uid": user_id},
            )
        no_index_rows = connection.execute(
            text("SELECT count(*) FROM control_no_index WHERE status = 'submitted'")
        ).scalar_one()
        connection.execute(text("DROP TABLE control_no_index"))

    # Control B: unique on the user alone, without the status predicate. After a
    # decision the member can never submit again.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS control_full_unique"))
        connection.execute(
            text(
                "CREATE TABLE control_full_unique ("
                "  mini_user_id uuid NOT NULL, status text NOT NULL,"
                "  UNIQUE (mini_user_id))"
            )
        )
        connection.execute(
            text("INSERT INTO control_full_unique VALUES (:uid, 'approved')"),
            {"uid": user_id},
        )
    with engine.begin() as connection:
        try:
            with connection.begin_nested():
                connection.execute(
                    text("INSERT INTO control_full_unique VALUES (:uid, 'submitted')"),
                    {"uid": user_id},
                )
            full_unique_blocks_reapply = 0
        except _IntegrityError:
            full_unique_blocks_reapply = 1
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE control_full_unique"))

    assert no_index_rows == 2, no_index_rows
    assert full_unique_blocks_reapply == 1, full_unique_blocks_reapply
    EVIDENCE["narrow_definition_required"] = 1
    EVIDENCE["control_no_index_submitted_rows"] = int(no_index_rows)
    EVIDENCE["control_full_unique_blocks_reapply"] = int(full_unique_blocks_reapply)

def test_proof_file_ids_capped_at_three(engine) -> None:
    """The ticket caps proof files at three; the check constraint enforces it."""
    with Session(engine) as session:
        user = make_user(session, "oProofCapProbe")
        application = MemberAuthApplication(
            org_id=ORG,
            site_id=SITE,
            mini_user_id=user.id,
            real_name="Fictional Applicant",
            enterprise_name="Fictional Trading Co.",
            proof_file_ids=[uuid.uuid4() for _ in range(3)],
        )
        session.add(application)
        session.commit()

        too_many = MemberAuthApplication(
            org_id=ORG,
            site_id=SITE,
            mini_user_id=user.id,
            real_name="Fictional Applicant",
            enterprise_name="Fictional Trading Co.",
            proof_file_ids=[uuid.uuid4() for _ in range(4)],
        )
        too_many.status = "withdrawn"
        session.add(too_many)
        with pytest.raises(IntegrityError) as excinfo:
            session.flush()
        session.rollback()

    assert "ck_member_auth_applications_proof_files_max" in str(excinfo.value)
    EVIDENCE["proof_files_cap"] = 3


# ---------------------------------------------------------------------------
# AC4 - no plaintext phone column, phone_hash is 64 hex characters
# ---------------------------------------------------------------------------


def test_no_plaintext_phone_column_in_the_live_catalogue(engine) -> None:
    """Read the catalogue, not the model: no column may store a phone number."""
    inspector = inspect(engine)
    names = {column["name"] for column in inspector.get_columns("mini_users")}

    forbidden = {"phone", "phone_number", "mobile", "mobile_number", "tel", "telephone"}
    assert not (names & forbidden), sorted(names & forbidden)
    assert "phone_hash" in names
    assert "phone_bound_at" in names

    # A wider sweep: no column anywhere in the schema is named like a phone field.
    offenders: list[str] = []
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        for column in inspector.get_columns(table):
            if column["name"] in forbidden:
                offenders.append(table + "." + column["name"])

    assert not offenders, offenders
    EVIDENCE["plaintext_phone_columns"] = 0
    EVIDENCE["mini_users_column_count"] = len(names)


def test_phone_hash_is_64_lowercase_hex(engine) -> None:
    """A stored row carries exactly 64 lowercase hex characters."""
    phone = "19900000042"
    with Session(engine) as session:
        user = make_user(session, "oHashProbe", phone_hash=hash_phone(phone))
        session.commit()
        digest = session.execute(
            text("SELECT phone_hash FROM mini_users WHERE id = :uid"), {"uid": user.id}
        ).scalar_one()

    assert re.fullmatch(r"[0-9a-f]{64}", digest), digest
    assert is_hash(digest)
    EVIDENCE["phone_hash_len"] = len(digest)
    EVIDENCE["phone_hash_hex"] = 1


def test_hmac_uses_the_environment_key(engine) -> None:
    """The digest is HMAC-SHA256 with PHONE_HASH_KEY, and a new key changes it."""
    phone = "19900000043"
    key = os.environ[ENV_VAR].encode("utf-8")
    expected = hmac.new(key, phone.encode("utf-8"), hashlib.sha256).hexdigest()

    with Session(engine) as session:
        user = make_user(session, "oHmacProbe", phone_hash=hash_phone(phone))
        session.commit()
        stored = session.execute(
            text("SELECT phone_hash FROM mini_users WHERE id = :uid"), {"uid": user.id}
        ).scalar_one()

    assert stored == expected
    bare_sha = hashlib.sha256(phone.encode("utf-8")).hexdigest()
    assert stored != bare_sha, "digest is an unkeyed SHA-256, not an HMAC"
    rotated = hash_phone(phone, key=b"a-different-deployment-key")
    assert rotated != stored

    EVIDENCE["hmac_matches"] = 1
    EVIDENCE["key_sensitive"] = 1
    EVIDENCE["not_bare_sha256"] = 1


def test_absent_phone_yields_null_not_empty_string(engine) -> None:
    """No phone means NULL, so "not bound" and "bound to an empty string" stay distinct."""
    assert hash_phone(None) is None
    assert hash_phone("") is None
    assert hash_phone("   ") is None

    with Session(engine) as session:
        user = make_user(session, "oNoPhoneProbe")
        session.commit()
        stored = session.execute(
            text("SELECT phone_hash FROM mini_users WHERE id = :uid"), {"uid": user.id}
        ).scalar_one()
        non_null = session.execute(
            text("SELECT count(*) FROM mini_users WHERE phone_hash IS NOT NULL")
        ).scalar_one()

    assert stored is None
    assert non_null == 0
    EVIDENCE["null_when_absent"] = 1


def test_the_whole_mini_users_dump_has_no_phone_number(engine) -> None:
    """Dump every column of every row and look for an 11 digit run."""
    # Populate first: a dump of zero rows would prove nothing.
    with Session(engine) as session:
        for index, phone in enumerate([f"1990000{index:04d}" for index in range(30, 40)]):
            session.add(
                MiniUser(
                    org_id=ORG,
                    site_id=SITE,
                    openid=f"oDumpProbe{index:02d}",
                    nickname=f"Seed Member {index + 1}",
                    bio=f"Fictional profile #{index + 1}.",
                    position="Coffee Buyer",
                    phone_hash=hash_phone(phone),
                )
            )
        session.commit()

    pattern = re.compile(r"\d{11}")

    # UUIDs are 32 hex digits and "203.0.113.7" style values are not phone numbers;
    # a phone number could only hide in a textual column, so those are scanned.
    inspector = inspect(engine)
    text_columns = sorted(
        column["name"]
        for column in inspector.get_columns("mini_users")
        if not isinstance(column["type"], (postgresql.UUID, postgresql.INET))
        and str(column["type"]).upper().startswith(("VARCHAR", "TEXT", "CHAR"))
    )
    projection = ", ".join(text_columns)

    with engine.connect() as connection:
        rows = connection.execute(text(f"SELECT {projection} FROM mini_users")).mappings().all()

    hits: list[str] = []
    digests = 0
    for row in rows:
        for column, value in row.items():
            if value is None:
                continue
            rendered = str(value)
            if column == "phone_hash":
                # The digest is 64 hex characters, so the assertion here is the
                # format; a run of decimal digits inside hex is not a phone number.
                assert re.fullmatch(r"[0-9a-f]{64}", rendered), rendered
                digests += 1
                continue
            if pattern.search(rendered):
                hits.append(column)

    assert rows, "the dump returned no rows, so it verified nothing"
    assert hits == [], hits
    assert digests == len(rows), (digests, len(rows))
    EVIDENCE["plaintext_hits"] = len(hits)
    EVIDENCE["dumped_rows"] = len(rows)
    EVIDENCE["scanned_text_columns"] = len(text_columns)
    EVIDENCE["digests_in_dump"] = digests


def test_ten_fictional_numbers_give_ten_distinct_hashes(engine) -> None:
    """No collision, and the digest is stable for the same input."""
    phones = [f"1990000{index:04d}" for index in range(10, 20)]
    digests = [hash_phone(phone) for phone in phones]

    with Session(engine) as session:
        for index, digest in enumerate(digests):
            session.add(
                MiniUser(
                    org_id=ORG,
                    site_id=SITE,
                    openid=f"oDistinctProbe{index:02d}",
                    phone_hash=digest,
                )
            )
        session.commit()
        stored = session.execute(
            text("SELECT count(DISTINCT phone_hash) FROM mini_users")
        ).scalar_one()
        total = session.execute(text("SELECT count(*) FROM mini_users")).scalar_one()

    assert len(set(digests)) == 10
    assert all(is_hash(digest) for digest in digests)
    assert stored == 10, stored
    assert total == 10
    EVIDENCE["distinct_hashes"] = int(stored)


# ---------------------------------------------------------------------------
# The seed, and the rest of the ticket surface
# ---------------------------------------------------------------------------


def test_seed_is_deterministic_and_covers_every_status(engine) -> None:
    """The same SEED twice gives byte-identical JSON and every status appears."""
    from migrations.seed_mini_auth import DEFAULT_SEED, build_payload, to_json

    first = to_json(build_payload(DEFAULT_SEED))
    second = to_json(build_payload(DEFAULT_SEED))
    assert first == second
    assert hashlib.sha256(first.encode("utf-8")).hexdigest() == hashlib.sha256(
        second.encode("utf-8")
    ).hexdigest()

    payload = json.loads(first)
    assert len(payload["mini_users"]) == 10
    assert len(payload["member_auth_applications"]) == 6
    assert len(payload["mini_login_events"]) == 12

    auth_statuses = {row["auth_status"] for row in payload["mini_users"]}
    app_statuses = {row["status"] for row in payload["member_auth_applications"]}
    assert auth_statuses == {"unverified", "pending", "verified", "rejected"}
    assert app_statuses == {"submitted", "approved", "rejected", "withdrawn"}

    # At most one submitted application per user, or the seed itself would be invalid.
    submitted = [
        row["mini_user_id"]
        for row in payload["member_auth_applications"]
        if row["status"] == "submitted"
    ]
    assert len(submitted) == len(set(submitted))

    EVIDENCE["seed_stable"] = 1
    EVIDENCE["seed_users"] = 10
    EVIDENCE["seed_applications"] = 6
    EVIDENCE["seed_login_events"] = 12
    EVIDENCE["seed_auth_statuses"] = sorted(auth_statuses)
    EVIDENCE["seed_application_statuses"] = sorted(app_statuses)


def test_seed_applies_into_a_migrated_database(database_url: str, engine) -> None:
    """The payload really inserts through the ORM tables, with every FK intact."""
    from sqlalchemy import create_engine

    from migrations.seed_mini_auth import DEFAULT_SEED, apply_payload, build_payload

    payload = build_payload(DEFAULT_SEED)
    own_engine = create_engine(database_url)
    try:
        with own_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE TABLE mini_users, member_auth_applications, "
                    "mini_login_events CASCADE"
                )
            )
            counts = apply_payload(connection, payload)
    finally:
        own_engine.dispose()

    assert counts == {"mini_users": 10, "member_auth_applications": 6, "mini_login_events": 12}
    with engine.connect() as connection:
        for table, expected in counts.items():
            actual = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert actual == expected, (table, actual, expected)
    EVIDENCE["seed_inserted_rows"] = 28


def test_login_events_carry_the_ticket_columns(engine) -> None:
    """mini_login_events: mini_user_id, login_at, client_ip, result."""
    with Session(engine) as session:
        user = make_user(session, "oLoginProbe")
        session.add(
            MiniLoginEvent(
                org_id=ORG,
                site_id=SITE,
                mini_user_id=user.id,
                client_ip="203.0.113.7",
                result="success",
            )
        )
        session.commit()
        row = session.execute(
            text(
                "SELECT host(client_ip), result, login_at IS NOT NULL AS has_ts "
                "FROM mini_login_events WHERE mini_user_id = :uid"
            ),
            {"uid": user.id},
        ).one()

    assert row[0] == "203.0.113.7"
    assert row[1] == "success"
    assert row[2] is True
    EVIDENCE["login_event_columns_verified"] = 1

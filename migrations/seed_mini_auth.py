"""Fictional seed data for the mini-program member space.

10 users, 6 authentication applications and 12 login events, all invented. No real
person, phone number or company appears: names are placeholders, phones come from
the Chinese fictional range, addresses come from TEST-NET-3 (203.0.113.0/24), and
enterprise names are plainly synthetic.

Determinism: every value is derived from a single integer seed, and the canonical
serialisation uses sort_keys + a fixed separator, so two runs with SEED=20261122
produce byte-identical JSON. tests/test_mini_auth_models.py asserts that.

Usage:

    # print the payload as canonical JSON, touching no database
    python -m migrations.seed_mini_auth --json

    # insert it (DATABASE_URL must point at a migrated database)
    python -m migrations.seed_mini_auth --apply

Both accept --seed to override the default 20261122.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_SEED = 20261122

#: Fixed namespace so generated UUIDs are stable across runs and machines.
UUID_NAMESPACE = uuid.UUID("6f0e2c1a-1f4b-4a51-9f0f-2d4f5b0c7a11")

#: A fictional operator org, referenced by every seeded row.
ORG_ID = uuid.uuid5(UUID_NAMESPACE, "org")
SITE_ID = uuid.uuid5(UUID_NAMESPACE, "site")

AUTH_STATUS_PLAN = [
    "verified", "verified", "verified", "pending", "pending",
    "unverified", "unverified", "unverified", "unverified", "rejected",
]

#: (user index, status, has a rejection reason, has a reviewer).
APPLICATION_PLAN = [
    (0, "approved", False, True),
    (1, "submitted", False, False),
    (2, "rejected", True, True),
    (3, "withdrawn", False, False),
    (0, "submitted", False, False),
    (4, "approved", False, True),
]

LOGIN_PLAN = [
    (0, "success", 0), (1, "success", 1), (2, "failure", 2), (3, "success", 3),
    (4, "success", 4), (5, "locked", 5), (0, "success", 6), (6, "failure", 7),
    (7, "success", 8), (8, "success", 9), (9, "locked", 10), (1, "success", 11),
]

EPOCH = datetime(2026, 11, 1, 9, 0, 0, tzinfo=timezone.utc)


def _stable_id(kind: str, index: int) -> uuid.UUID:
    return uuid.uuid5(UUID_NAMESPACE, f"{kind}:{index}")


def _hash_phone(phone: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), phone.encode("utf-8"), hashlib.sha256).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(seed: int = DEFAULT_SEED, key: str | None = None, org_id: uuid.UUID = ORG_ID) -> dict[str, Any]:
    """Build the whole seed as a plain dict, with no database access.

    *key* defaults to PHONE_HASH_KEY; passing it explicitly keeps this function
    usable from a test that sets the variable itself.
    """
    rng = random.Random(seed)
    key = key if key is not None else os.environ.get("PHONE_HASH_KEY", "")
    if not key:
        raise RuntimeError("PHONE_HASH_KEY must be set to build the seed payload.")

    users: list[dict[str, Any]] = []
    for index, auth_status in enumerate(AUTH_STATUS_PLAN):
        base = EPOCH + timedelta(days=index * 3, hours=rng.randint(0, 8))
        has_phone = index % 5 != 3  # four of the ten leave phone_hash null
        phone = f"1990000{index:04d}" if has_phone else None
        enterprise_role = None
        if auth_status == "verified":
            enterprise_role = "admin" if index == 0 else "member"
        users.append(
            {
                "id": str(_stable_id("user", index)),
                "org_id": str(org_id),
                "site_id": str(SITE_ID),
                "openid": f"oFictionSeed{index:04d}",
                "unionid": f"uFictionSeed{index:04d}" if index % 2 == 0 else None,
                "nickname": f"Seed Member {index + 1}",
                "avatar_url": f"https://cdn.example.invalid/avatar/{index}.png",
                "position": ["Coffee Buyer", "Office Manager", "Designer", None][index % 4],
                "bio": None if index % 4 == 3 else f"Fictional member profile #{index + 1}.",
                "auth_status": auth_status,
                "member_contact_id": None,
                "enterprise_id": str(_stable_id("enterprise", index)) if enterprise_role else None,
                "enterprise_role": enterprise_role,
                "phone_hash": _hash_phone(phone, key) if phone else None,
                "phone_bound_at": _iso(base) if phone else None,
                "last_login_at": None,
                "created_at": _iso(base),
                "created_by": None,
                "updated_at": _iso(base),
                "deleted_at": None,
            }
        )

    applications: list[dict[str, Any]] = []
    for index, (user_index, status, rejected, reviewed) in enumerate(APPLICATION_PLAN):
        submitted_at = EPOCH + timedelta(days=10 + index * 2, hours=rng.randint(0, 6))
        reviewed_at = submitted_at + timedelta(days=2) if reviewed else None
        applications.append(
            {
                "id": str(_stable_id("application", index)),
                "org_id": str(org_id),
                "site_id": str(SITE_ID),
                "mini_user_id": str(_stable_id("user", user_index)),
                "real_name": f"Fictional Applicant {index + 1}",
                "enterprise_name": f"Fictional Trading Co. No.{index + 1}",
                "position": ["Procurement Lead", "Founder", "Sales Director"][index % 3],
                "proof_file_ids": [
                    str(_stable_id("file", index * 3 + n)) for n in range(index % 4)
                ],
                "remark": f"Seed application {index + 1}, submitted through the mini-program.",
                "status": status,
                "reviewed_by": str(_stable_id("reviewer", 0)) if reviewed else None,
                "reviewed_at": _iso(reviewed_at) if reviewed_at else None,
                "reject_reason": (
                    "The uploaded proof does not match the enterprise name." if rejected else None
                ),
                "matched_enterprise_id": (
                    str(_stable_id("enterprise", 0)) if status == "approved" else None
                ),
                "created_at": _iso(submitted_at),
                "created_by": str(_stable_id("user", user_index)),
                "updated_at": _iso(reviewed_at or submitted_at),
                "deleted_at": None,
            }
        )

    login_events: list[dict[str, Any]] = []
    for index, (user_index, result, day_offset) in enumerate(LOGIN_PLAN):
        login_at = EPOCH + timedelta(days=20 + day_offset, minutes=rng.randint(0, 59))
        login_events.append(
            {
                "id": str(_stable_id("login", index)),
                "org_id": str(org_id),
                "site_id": str(SITE_ID),
                "mini_user_id": str(_stable_id("user", user_index)),
                "login_at": _iso(login_at),
                "client_ip": f"203.0.113.{10 + user_index}",
                "result": result,
                "created_at": _iso(login_at),
                "created_by": None,
                "updated_at": _iso(login_at),
                "deleted_at": None,
            }
        )

    return {"seed": seed, "mini_users": users, "member_auth_applications": applications,
            "mini_login_events": login_events}


def to_json(payload: dict[str, Any]) -> str:
    """Canonical serialisation: sorted keys, fixed separators, trailing newline."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _coerce(row: dict[str, Any], table) -> dict[str, Any]:
    """Turn the JSON-friendly strings back into uuid.UUID where the column wants one.

    The payload is plain JSON so it can be diffed and hashed; the driver, however,
    wants real UUID objects for uuid and uuid[] parameters.
    """
    from sqlalchemy.dialects import postgresql as _pg

    out: dict[str, Any] = {}
    for column in table.columns:
        if column.key not in row:
            continue
        value = row[column.key]
        column_type = column.type
        if isinstance(column_type, _pg.UUID) and isinstance(value, str):
            value = uuid.UUID(value)
        elif isinstance(column_type, _pg.ARRAY) and isinstance(column_type.item_type, _pg.UUID):
            value = [uuid.UUID(item) if isinstance(item, str) else item for item in value]
        out[column.key] = value
    return out


def apply_payload(connection, payload: dict[str, Any]) -> dict[str, int]:
    """Insert the payload through SQLAlchemy Core; returns row counts per table."""
    from models import MemberAuthApplication, MiniLoginEvent, MiniUser

    counts: dict[str, int] = {}
    for model, key in (
        (MiniUser, "mini_users"),
        (MemberAuthApplication, "member_auth_applications"),
        (MiniLoginEvent, "mini_login_events"),
    ):
        rows = [_coerce(row, model.__table__) for row in payload[key]]
        connection.execute(model.__table__.insert(), rows)
        counts[key] = len(rows)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the mini-program member schema.")
    parser.add_argument("--json", action="store_true", help="print canonical JSON and exit")
    parser.add_argument("--apply", action="store_true", help="insert into DATABASE_URL")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    payload = build_payload(args.seed)

    if args.json or not args.apply:
        sys.stdout.write(to_json(payload))
        return 0

    from sqlalchemy import create_engine
    from models import Base

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    engine = create_engine(url)
    with engine.begin() as connection:
        counts = apply_payload(connection, payload)
    print(json.dumps({"seeded": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

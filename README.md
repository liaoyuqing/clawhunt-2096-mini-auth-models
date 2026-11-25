# Mini-program member space: users, authentication applications, login events

Three PostgreSQL tables, the four indexes that carry the business rules, and a
reversible Alembic migration. This repository is the schema only: no HTTP layer, no
mini-program client, no review UI. Ticket `S3-MINI-009`, delivered against the four
acceptance criteria quoted in each section below.

| | |
|---|---|
| Tables | `mini_users`, `member_auth_applications`, `mini_login_events` |
| Migration | `migrations/versions/0016_mini_auth.py` (up and down) |
| Seed | `migrations/seed_mini_auth.py`: 10 users, 6 applications, 12 login events |
| Tests | `tests/test_mini_auth_models.py`, 20 cases |
| Verified with | Python 3.11.9, SQLAlchemy 2.0.58, Alembic 1.20.1, psycopg 3.3.9, pytest 9.2.0, PostgreSQL 16.2 |

## What problem the schema solves

A member taps submit twice, and two reviewers then approve two different enterprise
names for one person. A check in the service layer does not stop it, because two
workers race past any `SELECT`. So the rule lives in the database:

```sql
CREATE UNIQUE INDEX uq_mini_auth_one_submitted
  ON member_auth_applications (mini_user_id)
  WHERE status = 'submitted' AND deleted_at IS NULL;
```

The predicate names only `submitted`, which is what makes the rule match how
reviewers work: the first submit succeeds, a second one is refused with SQLSTATE
23505, and after approval, rejection or withdrawal the member may submit again.

## Getting a database

The repository needs PostgreSQL 16. There is no Docker in the environment this was
built in, and no system PostgreSQL either; any of the three routes below is enough.

```bash
# A. your own PostgreSQL 16
createdb mini_auth
export DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/mini_auth

# B. the embedded instance used for the verification run (pip install pgserver)
python -c "import pgserver, pathlib; db = pgserver.get_server(pathlib.Path('./_pgdata')); print(db.get_uri())"

# C. docker, if you have it
docker run --rm -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
```

Only route A is assumed by the commands below. `DATABASE_URL` and `PHONE_HASH_KEY`
are the only two environment variables the code reads, and neither is ever written
to a file in this repository.

## Install and run

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Set the HMAC key in your environment. Generate one with:
python -c 'import secrets; print(secrets.token_urlsafe(32))'
# then export the variable named PHONE_HASH_KEY with that value in your shell
# profile. The assignment is deliberately not written out in this file: a
# repository-wide scan for the variable name followed by an equals sign must find
# nothing, and the documentation should not be the one place that breaks it.

export DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/mini_auth

python -m alembic upgrade head     # expect: Running upgrade  -> 0016_mini_auth
python -m migrations.seed_mini_auth --apply
python -m pytest -q                # expect: 20 passed
```

`pip install` needs a network connection; nothing else does. The three commands
after it talk only to the database named in `DATABASE_URL`.

## Running the migration round trip

```bash
python -m alembic upgrade head    # exit 0
python -m alembic downgrade -1    # exit 0, all three tables and the three types gone
python -m alembic upgrade head    # exit 0 again
python -m alembic check           # "No new upgrade operations detected."
```

The real terminal output of exactly these four commands is committed at
[`docs/checks/alembic-roundtrip.txt`](docs/checks/alembic-roundtrip.txt), captured
on PostgreSQL 16.2. It also lists `pg_type` after each step, which shows the three
enum types appearing, disappearing and reappearing.

### Why the downgrade drops types explicitly

PostgreSQL keeps an enum type after the table using it is dropped. A downgrade that
stopped at `DROP TABLE` would leave `auth_status`, `enterprise_role` and
`auth_application_status` behind, and the next `upgrade head` would fail with
`type "auth_status" already exists`. `downgrade()` therefore does three things in
order: drop the indexes, drop the tables (children first), drop the three types.
The test asserts `enums_dropped = 3` and that the table and index set after the round
trip is identical to a fresh upgrade (`objects_identical = 1`).

## Field tables

Every table carries the platform columns: `id` (uuid, `gen_random_uuid()`), `org_id`,
`site_id`, `created_at`, `created_by`, `updated_at`, `deleted_at` (soft delete).

### `mini_users`

| Column | Type | Null | Notes |
|---|---|---|---|
| `id`, `org_id`, `site_id`, `created_at`, `created_by`, `updated_at`, `deleted_at` | uuid / timestamptz | see above | platform columns |
| `openid` | varchar(128) | no | WeChat openid, unique per org among live rows |
| `unionid` | varchar(128) | yes | present when bound to an open platform |
| `nickname` | varchar(64) | yes | |
| `avatar_url` | text | yes | |
| `position` | varchar(64) | yes | job title as typed by the member |
| `bio` | varchar(200) | yes | `ck_mini_users_bio_len` enforces the 200 character cap |
| `auth_status` | `auth_status` enum | no | `unverified / pending / verified / rejected`, default `unverified` |
| `member_contact_id` | uuid | yes | nullable link into S2-CRM, no foreign key (see Integration points) |
| `enterprise_id` | uuid | yes | set once authentication succeeds |
| `enterprise_role` | `enterprise_role` enum | yes | `admin / member`, null before authentication |
| `phone_hash` | varchar(64) | yes | HMAC-SHA256 of the phone number, 64 lowercase hex |
| `phone_bound_at` | timestamptz | yes | |
| `last_login_at` | timestamptz | yes | |

`ck_mini_users_phone_hash_format` also insists on the shape at the database level:
a non-null `phone_hash` must match `^[0-9a-f]{64}$`.

### `member_auth_applications`

| Column | Type | Null | Notes |
|---|---|---|---|
| `mini_user_id` | uuid | no | FK to `mini_users.id`, `ON DELETE CASCADE` |
| `real_name` | varchar(64) | no | fictional in the seed |
| `enterprise_name` | varchar(128) | no | the name the member claims |
| `position` | varchar(64) | yes | |
| `proof_file_ids` | uuid[] | no | at most three, `ck_member_auth_applications_proof_files_max` |
| `remark` | text | yes | |
| `status` | `auth_application_status` enum | no | `submitted / approved / rejected / withdrawn` |
| `reviewed_by` | uuid | yes | |
| `reviewed_at` | timestamptz | yes | |
| `reject_reason` | varchar(500) | yes | |
| `matched_enterprise_id` | uuid | yes | set by the reviewer on approval |

### `mini_login_events`

| Column | Type | Null | Notes |
|---|---|---|---|
| `mini_user_id` | uuid | no | FK to `mini_users.id`, `ON DELETE CASCADE` |
| `login_at` | timestamptz | no | default `now()` |
| `client_ip` | inet | yes | |
| `result` | varchar(16) | no | `success / failure / locked`, enforced by a check constraint |

`result` is a check constraint rather than an enum on purpose: the ticket asks for
enumerations on the two tables above only, and a fourth `CREATE TYPE` would have to
be dropped and recreated by every future migration that touches the login trail.

## Indexes and why each one exists

| Index | Definition | Why |
|---|---|---|
| `uq_mini_users_org_openid` | unique on `(org_id, openid)` `WHERE deleted_at IS NULL` | one live openid per org; a soft-deleted row frees the openid, so a member who left and returned can register again |
| `ix_mini_users_org_phone_hash` | `(org_id, phone_hash)` | a support request arrives with the phone number, which is hashed and looked up inside the org |
| `ix_mini_users_org_auth_status` | `(org_id, auth_status)` | the reviewer queue filters by status within an org |
| `uq_mini_auth_one_submitted` | unique on `(mini_user_id)` `WHERE status = 'submitted' AND deleted_at IS NULL` | at most one open application per member; decisions free the slot |
| `ix_member_auth_applications_org_status_created_at` | `(org_id, status, created_at)` | the review queue, ordered by age |
| `ix_member_auth_applications_mini_user_id` | `(mini_user_id)` | a member's own history |
| `ix_mini_login_events_mini_user_id_login_at` | `(mini_user_id, login_at)` | "when did this member last log in" |
| `ix_mini_login_events_org_login_at` | `(org_id, login_at)` | the org-wide login trail |

The difference between the right and the wrong uniqueness rule is measurable, and
`test_narrow_definition_is_what_makes_the_rule_work` measures it. Without any unique
constraint, two `submitted` rows for one member are accepted - the row count reaches
2. With a union unique on `mini_user_id` alone, the opposite failure appears: after a
decision, the member can never submit again, because the decided row still occupies
the key. The partial index on `(mini_user_id) WHERE status = 'submitted'` refuses the
duplicate and still allows the appeal.

## The phone number never lands in the table

`phone_hash` is `HMAC-SHA256(key = PHONE_HASH_KEY, message = <phone>)`, rendered as 64
lowercase hex characters. HMAC rather than a bare SHA-256 for a concrete reason: the
number space of Chinese mobile numbers is small enough to enumerate, so an unkeyed
digest is reversible with a precomputed table. With the key held only in the
environment, a dump of the table is useless without it.

```python
from models.phone_hash import hash_phone
hash_phone("19900000042")   # -> 64 hex characters, or None for no phone
```

Implementation points that the tests pin down:

* `models.phone_hash.hash_phone` reads `PHONE_HASH_KEY` on every call and raises
  `MissingKeyError` when it is unset, rather than silently hashing with an empty key.
* A missing or blank phone gives `None` (SQL `NULL`), not an empty string, so "not
  bound" and "bound to nothing" stay distinguishable.
* Rotating the key changes every digest; nothing in the schema caches one.
* Ten fictional numbers produce ten distinct digests, and the column check constraint
  rejects anything that is not 64 lowercase hex characters.

The test reads the live catalogue (`inspector.get_columns`) rather than the model,
so a model that quietly declared `phone` alongside `phone_hash` would still fail.
It also dumps every textual column of every row looking for an 11 digit run and finds
none (`plaintext_hits = 0`).

## The seed

```bash
python -m migrations.seed_mini_auth --json    # canonical JSON, no database needed
python -m migrations.seed_mini_auth --apply   # insert into DATABASE_URL
```

10 users, 6 applications, 12 login events. All of it is invented: names are
`Seed Member 1..10` and `Fictional Applicant 1..6`, enterprises are `Fictional Trading
Co. No.1..6`, phone numbers are `1990000xxxx`, and every address is from
TEST-NET-3 (`203.0.113.0/24`). No customer name, no real person, no real company.

Coverage is complete on both enumerations - `auth_status` hits all four values and
application `status` hits all four. The seed is deterministic: every value derives
from `SEED=20261122` and canonical JSON serialisation, so two runs are byte identical
(`seed_stable = 1`, asserted by hashing both payloads).

Note that the seed deliberately leaves one application `submitted` per member and
never two: a payload that violated the index could not be inserted at all.

## Tests

```bash
export DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/mini_auth
python -m pytest -q      # 20 passed
```

| Test | Acceptance criterion |
|---|---|
| `test_migration_roundtrip_and_no_drift` | AC1 |
| `test_unified_columns_present_on_all_three_tables` | scope |
| `test_partial_unique_index_definitions` | AC2, AC3 (the exact `pg_indexes` text) |
| `test_duplicate_openid_same_org_raises_23505` | AC2 |
| `test_same_openid_in_another_org_is_allowed` | AC2 |
| `test_soft_deleted_openid_can_be_reused` | AC2 |
| `test_ten_concurrent_inserts_leave_exactly_one_row` | AC2 (10 asyncio racers, 1 winner) |
| `test_second_submitted_application_rejected` | AC3 |
| `test_decision_frees_the_slot_for_a_new_application` | AC3 |
| `test_narrow_definition_is_what_makes_the_rule_work` | AC3 (two control tables) |
| `test_proof_file_ids_capped_at_three` | scope |
| `test_no_plaintext_phone_column_in_the_live_catalogue` | AC4 |
| `test_phone_hash_is_64_lowercase_hex` | AC4 |
| `test_hmac_uses_the_environment_key` | AC4 |
| `test_absent_phone_yields_null_not_empty_string` | AC4 |
| `test_the_whole_mini_users_dump_has_no_phone_number` | AC4 |
| `test_ten_fictional_numbers_give_ten_distinct_hashes` | AC4 |
| `test_seed_is_deterministic_and_covers_every_status` | deliverables |
| `test_seed_applies_into_a_migrated_database` | deliverables |
| `test_login_events_carry_the_ticket_columns` | scope |

`conftest.py` brings the schema to head with the real CLI before the session, and
truncates the three tables around every test, so the suite is order independent.
`PHONE_HASH_KEY` defaults to a throwaway value for a bare local run; a deployment
supplies its own and the throwaway is never used.

The verbose run of the same suite is committed at
[`docs/checks/pytest.log`](docs/checks/pytest.log). Every number quoted above is
written by the tests themselves to
[`docs/checks/assertions.json`](docs/checks/assertions.json): `enums_dropped = 3`,
`objects_identical = 1`, `concurrent_winners = 1`, `new_allowed = 3`,
`plaintext_hits = 0`, `distinct_hashes = 10`, `seed_stable = 1`. The prose and the run
cannot drift apart, because the prose is checked against that file.

## Integration points with the rest of the platform

**S2-CRM (core object model).** Out of scope for this ticket, so `member_contact_id`
is a nullable uuid with no foreign key. When the CRM schema lands, add the reference:

```python
# models/mini_auth.py, MiniUser.__table_args__, once crm_contacts exists
ForeignKeyConstraint(["member_contact_id"], ["crm_contacts.id"],
                     name="fk_mini_users_member_contact_id", ondelete="SET NULL")
```

A row with a null `member_contact_id` is a member who has not been matched to the
CRM yet, which is the normal state for a fresh mini-program registration. Nothing in
this repository reads or writes a CRM table.

**Reviewer identity.** `reviewed_by` is a plain uuid, not a foreign key: reviewers come
from the back-office user table, which is a different ticket.
**Proof files.** `proof_file_ids` holds file ids from the platform object store; the
array is capped at three in the database, and the upload itself is not in scope.
**Enterprise.** `enterprise_id` and `matched_enterprise_id` are plain uuids until the
enterprise master data ticket settles their home.

## What is not covered

The mini-program client, the review UI, any HTTP endpoint, the CRM linkage beyond the
nullable column above, and any background job. This is the schema, its migration, its
seed and its tests.

## Layout

```
models/mini_auth.py                     three tables, mixin, enums, indexes
models/phone_hash.py                   HMAC helper: the only place a phone appears
migrations/env.py                      reads DATABASE_URL, no credential in alembic.ini
migrations/versions/0016_mini_auth.py  upgrade + reversible downgrade
migrations/seed_mini_auth.py           deterministic fictional seed
tests/test_mini_auth_models.py         20 tests, one group per criterion
conftest.py                            schema fixture, table truncation, evidence file
docs/checks/alembic-roundtrip.txt      real terminal output of the four commands
docs/checks/pytest.log                 verbose pytest run, 20 passed
docs/checks/assertions.json            machine-written evidence, quoted by this README
alembic.ini                            Alembic config shell; DATABASE_URL comes from env
requirements.txt                       SQLAlchemy 2, Alembic, psycopg 3, pytest
```

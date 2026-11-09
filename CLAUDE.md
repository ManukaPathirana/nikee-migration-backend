# Nikee Migrations — Backend

FastAPI API for the Nikee Migrations mobile platform (customers, staff, admin).

## Stack
- Python 3.11, FastAPI, Uvicorn
- SQLAlchemy 2.x (async) + asyncpg, Alembic migrations, PostgreSQL 15+ (local dev: PostgreSQL 18)
- Pydantic v2 + pydantic-settings (config from `.env`)
- Auth: JWT access token (15 min) + rotating refresh tokens stored hashed; argon2 password hashing
- Background jobs: arq + Redis (all emails sent via jobs, never inline).
  Jobs are enqueued through one `services/job_queue` interface: if `REDIS_URL` is set it uses arq,
  otherwise it runs them as in-process asyncio background tasks plus an in-process reminder scheduler.
  This machine has no Redis (Memurai's installer fails here), so local dev runs with `REDIS_URL` empty.
- Email: `EMAIL_BACKEND` = console | file (writes `.eml` files to `dev_mail/`) | memory (tests) | smtp | resend.
  Local dev uses `file`; open the newest `.eml` in `dev_mail/` to read OTP codes and invite links.
  Templates: `app/templates/email/*.html` (extend `base.html`). Send only via `job_queue.send_email(...)`.
- Storage: local filesystem adapter in dev, S3-compatible (boto3) in prod; private files, short-lived signed URLs
- Rate limiting: slowapi. Calendar invites: icalendar.
- Tests: pytest + pytest-asyncio + httpx. Lint/format: ruff + black.

## Layout
```
app/
  main.py          app factory, routers, middleware, error handlers
  core/            config.py, security.py, deps.py (get_current_user, require_role, CurrentUser,
                   AdminUser, StaffOrAdmin, CustomerUser), errors.py (AppError + subclasses), rate_limit.py
  db/              session.py, base.py (Base, UUIDPkMixin, TimestampMixin, pg_enum)
  models/          SQLAlchemy models: user, visa, inquiry, meeting, misc (reviews, notifications,
                   company_info, audit_logs); import new models in models/__init__.py
  scripts/         seed.py (idempotent)
  schemas/         Pydantic request/response schemas
  api/v1/          routers: auth, users, staff, customers, company, visa, documents,
                   inquiries, meetings, reviews, notifications, admin
  services/        business logic (inquiry_service, meeting_service, email_service, storage_service)
  workers/         arq tasks (send_email, meeting_reminders)
  templates/       Jinja2 email templates
alembic/           migrations
tests/
```

## Commands (Windows PowerShell, from backend/)
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head
python -m app.scripts.seed
uvicorn app.main:app --reload            # API at http://localhost:8000, docs at /docs
arq app.workers.WorkerSettings           # background worker (only when REDIS_URL is set)
pytest                                   # uses TEST_DATABASE_URL (nikee_migrations_test), recreated each run
ruff check . ; black --check .
alembic revision --autogenerate -m "..." # then review: shared PG enums need create_type=False
```
PostgreSQL binaries live at `D:\PostgreSQL-H7\pgsql\bin` on this machine (not on PATH).
Databases: `nikee_migrations` (dev) and `nikee_migrations_test` (pytest), owned by role `nikee`.

Seeded accounts (dev): admin from `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD`;
`demo.staff@example.com` and `demo.customer@example.com` with `SEED_DEMO_PASSWORD`.

## Auth API (`/api/v1/auth`)
register → email OTP → verify-email (returns tokens) · resend-verification · login · refresh (rotates;
reusing an old refresh token revokes all sessions) · logout · forgot-password → reset-password ·
accept-invite (staff, token from email) · change-password. `GET/PATCH /api/v1/me`.
OTP codes: 6 digits, 10 min, max 5 wrong attempts; stored as HMAC hashes.

## Rules
- All routes under `/api/v1`. Every list endpoint is paginated (`page`, `page_size`, returns `items`, `total`).
- Consistent error format: `{"error": {"code": str, "message": str, "details": any}}`.
- Role checks only via `require_role(...)` dependencies. Customers can only read/write their own data.
- Business rules live in `services/`, not in routers. Routers stay thin.
- Inquiry status transitions: scheduled → processing|cancelled; processing → completed|cancelled.
  Completed/cancelled are final (admin may reopen). Every change writes `inquiry_status_history`; cancel needs a reason.
- Every admin/staff mutation writes an `audit_logs` row.
- UUID primary keys, `timestamptz` in UTC, `created_at`/`updated_at` on every table.
- Schema changes only through Alembic migrations — never `create_all` outside tests.
- Uploads: max 10 MB, PDF/JPG/PNG only.
- Never commit secrets. New settings go into both `core/config.py` and `.env.example`.
- Conventional commits on `main` (`feat:`, `fix:`, `chore:`, `test:`, `docs:`).

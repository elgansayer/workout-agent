# Role-based administrative permissions

Ownership of tenant data and administrative authority are separate controls.

- **Tenant ownership** is decided from the authenticated account id. A user
  always acts on their own records, and a client-supplied owner id is never
  trusted as authorisation.
- **Administrative authority** is decided by the account's `users.role` and the
  central matrix in `backend/permissions.py`. It only grants actions that cross
  the tenant boundary.

Implementation: `backend/permissions.py` (matrix and `authorize`),
`backend/accounts.py` (`AccountRole`, `set_account_role`),
`backend/admin_service.py` (permission-enforced services),
`backend/webapp/app.py` (`/api/admin/*` routes), and
`backend/scripts/manage_accounts.py` (`set-role`).

## Roles and the matrix

Roles are ordered `owner` > `admin` > `support` > `user`. The vocabulary is
defined once in `database.ACCOUNT_ROLES`; the `users.role` `CHECK` constraint,
`accounts.AccountRole` and `permissions.ROLE_PERMISSIONS` are tested for drift.

| Permission | owner | admin | support | user |
| --- | --- | --- | --- | --- |
| `account.management` | yes | yes | no | no |
| `support.access` | yes | yes | yes | no |
| `connector.diagnostics` | yes | yes | yes | no |
| `data.export` | yes | yes | no | no |
| `data.deletion` | yes | yes | no | no |
| `configuration.management` | yes | no | no | no |

`user` is the default for every new account and has **no** administrative
permission. It retains full ownership of its own tenant data: nothing in this
model restricts a user's own settings, exports, deletion requests or connector
configuration.

## Enforcement

Every administrative route resolves the actor from the signed session, re-reads
the role from the database, and calls `permissions.authorize` before reading or
changing another tenant. Hiding a control in the SPA is not a control; these
routes deny server-side with `403`.

| Route | Permission | Effect |
| --- | --- | --- |
| `GET /api/admin/accounts` | `account.management` | List accounts with status and role. |
| `POST /api/admin/accounts/{id}/status` | `account.management` | Suspend or reactivate a lower role. |
| `POST /api/admin/accounts/{id}/role` | `configuration.management` | Assign a role (owner only). |
| `GET /api/admin/accounts/{id}/support` | `support.access` | Non-secret account and row-count context. |
| `GET /api/admin/accounts/{id}/diagnostics` | `connector.diagnostics` | Aggregate database/AI/connector diagnostics. |
| `GET /api/admin/accounts/{id}/export` | `data.export` | Account, preferences, active programme and row counts; never credentials, provider subjects, sessions or audit trails. |
| `DELETE /api/admin/accounts/{id}` | `data.deletion` | Access tombstone and session revocation. |
| `GET /api/admin/config` | `configuration.management` | Non-secret deployment configuration. |

`GET /api/me/permissions` returns the current role and permissions for
presentation. It is a convenience only; every action re-checks the matrix.

Precedence is enforced in the service layer as well as the route:

- Status changes and deletion require the actor to **strictly outrank** the
  target (`owner` cannot suspend or delete another `owner`; `admin` cannot touch
  an `owner`).
- Role changes require `configuration.management` and may target an equal role,
  but demoting the **last active owner** is refused so a deployment cannot lock
  itself out.
- A role change bumps the target's `session_epoch`, so sessions issued under the
  old role fail closed on their next request.

## Bootstrap and operation

New accounts are `user`. Promote the deployment owner from the operator CLI,
which runs with the application's own database configuration:

```bash
python scripts/manage_accounts.py set-role --email owner@example.com --role owner
python scripts/manage_accounts.py set-role --email support@example.com --role support
python scripts/manage_accounts.py list
```

The legacy `legacy@local` holding account cannot be promoted, and terminal
(`retired`/`deleted`) accounts cannot change role. Every role change ends the
target's earlier sessions.

## Audit

Authorised and denied administrative calls emit `admin.access`; role changes
emit `permission.changed`; export emits `data.exported`; an administrative
access tombstone emits `data.deleted`. Metadata contains the closed-vocabulary
permission/role and opaque account ids only — never addresses, secrets or health
payloads. See `docs/SECURITY_AUDIT_LOGGING.md`.

## Background actions

Scheduled coaching, insight, notification and connector jobs are tenant-owned
per-user work, not administrative actions; they continue to act only for
`active` accounts and never gain cross-tenant authority. Any future background
action that crosses accounts must call `permissions.authorize` (or the relevant
`admin_service` function) and must not rely on the role captured when the job was
queued.

## Verification

```bash
.venv/bin/python -m pytest -q \
  backend/tests/test_permissions.py backend/tests/test_admin_api.py \
  backend/tests/test_user_model.py backend/tests/test_manage_accounts.py
```

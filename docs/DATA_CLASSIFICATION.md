# Data classification policy

This policy is the canonical handling contract for data processed by Workout Agent. The executable source of truth for field classification and reusable sanitizers is `backend/data_classification.py`.

## Classification matrix

| Class | Examples | Storage | Encryption | Logging | Retention | User export | Access |
|---|---|---|---|---|---|---|---|
| Public | static assets, public product copy | normal application storage | TLS in transit; platform controls at rest | allowed | product/documentation lifecycle | yes | public |
| Identifier | user ID, email, display name, device or endpoint identifiers, IP address | tenant-scoped only | TLS plus protected at-rest storage | redact or pseudonymise | account lifetime plus documented operational retention | yes, owning user only | owner and explicitly authorised operators |
| Health | weight, body fat, resting heart rate, HRV, sleep, recovery | tenant-scoped only | TLS plus protected at-rest storage | never log raw values | user-controlled health-data policy | yes, owning user only | owner and purpose-authorised processing |
| Workout | Hevy payloads, routines, exercises, sets, reps, training plans | tenant-scoped only | TLS plus protected at-rest storage | metadata only; never raw payloads | user-controlled workout-data policy | yes, owning user only | owner and purpose-authorised processing |
| Prompt | user prompts, chat content, coaching reasoning | tenant-scoped when persisted | TLS plus protected at-rest storage | never log raw prompt/response payloads | shortest period needed for enabled coaching features | yes for user-authored/user-visible content | owner and selected AI processor |
| Analytics | correlations, scores, aggregate/derived insights | aggregate or pseudonymise where possible | TLS plus protected at-rest storage | aggregate values only | bounded analytics window | no by default; explicit reviewed export required | owner plus authorised product analytics |
| Credential | API keys, OAuth codes/tokens, client secrets, cookies, push auth keys, encryption keys | encrypted secret store only | authenticated application-layer encryption plus TLS | never | until rotation, revocation, disconnect, or account deletion | never | narrow credential service only |
| Internal | system prompts, security metadata, operator-only diagnostics, unclassified new fields | protected application storage | TLS plus platform at-rest controls | safe metadata only | documented operational retention | no | authorised application/operator paths only |

Unknown fields intentionally classify as **Internal**. A new field must not silently become public or exportable merely because it was not added to the registry.

## Code contract

Use `classify_field()` and `policy_for()` instead of adding feature-local sensitivity rules. Add stable schema/API field names to `FIELD_CLASSES`; name-pattern classification is a fail-safe for new variants, not a substitute for explicit review.

Before logging a value whose class is not Public, pass it through `safe_log_value()` or pass the whole structure through `redact_for_log()`. Credentials, identifiers, health values, workout payloads, prompts, and derived analytics are replaced with class-labelled redaction markers.

Before building a user export, pass untrusted/general mappings through `sanitize_for_export()` and then apply the feature's authenticated tenant filter. The sanitizer always removes Credential, Internal, and Analytics fields. A future export that deliberately includes a derived analytics field must explicitly transform it into a reviewed export DTO rather than weakening the global policy.

The classification helper does **not** replace encryption, ownership checks, consent checks, or retention jobs. It provides one shared decision point that those systems can consume.

## Schema requirements

Sensitive database fields must have an explicit or pattern-derived non-Internal classification. In particular, `user_api_keys` credentials, push-subscription auth material, user identifiers, Hevy payloads, body metrics, chat content, reasoning, and derived insights must remain classified.

Any migration that adds a credential, identifier, health, workout, prompt, or analytics column should update `FIELD_CLASSES` in the same pull request. The focused tests assert the current high-value database boundary so accidental reclassification fails review.

## Enforcement

`backend/scripts/check_data_policy.py` performs two high-confidence repository checks:

1. it parses Python logging calls and fails when credential-like variables are passed directly rather than through a central redaction helper;
2. it scans SQLite schema declarations and fails when sensitive-looking columns resolve to the fail-closed Internal class.

`backend/tests/test_data_classification.py` verifies the complete matrix, classification fallbacks, recursive log redaction, and export filtering. `backend/tests/test_data_policy_check.py` exercises the static checker against synthetic safe and unsafe code.

The `Data classification policy` GitHub Actions workflow runs the focused tests and repository scanner whenever classification code, database code, backend Python, or the policy workflow changes.

## Review checklist

- Classify new fields before persistence or transport.
- Keep every tenant-owned value scoped to the authenticated owner.
- Never log raw credentials, health payloads, workout payloads, or prompts.
- Never return stored credentials after submission.
- Treat exports as a new disclosure boundary; filter by owner first and classification second.
- Do not downgrade a class to make a test pass. Add a purpose-specific reviewed DTO when a legitimate disclosure is required.
- Keep retention and deletion behavior aligned with the class matrix and connector-specific policy.

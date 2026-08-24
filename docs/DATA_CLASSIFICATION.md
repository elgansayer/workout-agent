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

## Public source and synthetic-data rule

The public repository is not an approved storage location for real-user Identifier, Health, Workout, Prompt, Analytics, or Credential data. This applies to documentation, examples, fixtures, sample payloads, screenshots, generated reports, issue reproductions, and copied provider exports.

When an example needs user-shaped data:

- create a fictional profile rather than adapting a real user's profile;
- mark sensitive fixture/profile examples explicitly with `synthetic-profile: true` or `"synthetic_profile": true`;
- use reserved example email domains such as `example.com`;
- use generic, non-identifying workout and health values;
- reduce bugs from Hevy, Garmin, Google Health/Health Connect, Fitbit, Apple Health, or other providers to the smallest synthetic payload that reproduces the behavior;
- never paste a production export, screenshot, prompt transcript, API response, credential, or personal coaching profile into source control.

A synthetic marker is an assertion by the contributor that the data is fictional. It is not permission to copy a real record and relabel it. Reviewers must reject examples that appear derived from a real person even when a marker is present.

If real personal data has already been committed, removing it from the current tree is only the first step. Follow `docs/PUBLIC_DATA_HISTORY_REVIEW.md` to decide whether a coordinated history rewrite is required. Any credential discovered in current or historical content must be revoked or rotated independently of history cleanup.

## Enforcement

`backend/scripts/check_data_policy.py` performs high-confidence repository checks:

1. it parses Python logging calls and fails when credential-like variables are passed directly rather than through a central redaction helper;
2. it scans SQLite schema declarations and fails when sensitive-looking columns resolve to the fail-closed Internal class;
3. it scans public documentation and fixture/sample sources for likely named personal profiles, nearby first-person health/training details, non-example email addresses in sensitive example data, and sensitive fixtures that lack an explicit synthetic marker.

The public-source heuristic is deliberately conservative. It is a prevention layer, not a complete DLP or historical-secret scanner, and does not replace review of screenshots, binary attachments, generated artifacts, or Git history.

`backend/tests/test_data_classification.py` verifies the complete matrix, classification fallbacks, recursive log redaction, and export filtering. `backend/tests/test_data_policy_check.py` exercises the static checker against synthetic safe and unsafe code.

The `Data classification policy` GitHub Actions workflow runs the focused tests and repository scanner when backend policy code, public documentation, fixture paths, or the workflow itself changes.

## Review checklist

- Classify new fields before persistence or transport.
- Keep every tenant-owned value scoped to the authenticated owner.
- Never log raw credentials, health payloads, workout payloads, or prompts.
- Never return stored credentials after submission.
- Treat exports as a new disclosure boundary; filter by owner first and classification second.
- Do not downgrade a class to make a test pass. Add a purpose-specific reviewed DTO when a legitimate disclosure is required.
- Keep retention and deletion behavior aligned with the class matrix and connector-specific policy.
- Keep public docs, examples, fixtures, and samples synthetic and non-identifying.
- Treat removal from `main` and removal from Git history as separate remediation steps.

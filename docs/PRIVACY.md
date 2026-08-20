# Workout Agent privacy notice

**Notice version:** 1.0.0  
**Effective date:** 2026-08-20  
**Machine-readable inventory:** `docs/data-flow-inventory.json`

Workout Agent is a self-hosted training and coaching application. This notice explains what information the application can process, why it is processed, which external services may receive it, and the controls available to a user or deployment operator. A deployment operator may need to supplement this notice with its own legal identity, contact details, hosting location, and jurisdiction-specific terms.

## What we collect

Depending on the features a user enables, Workout Agent can process:

- account and identity data such as email address, display name, account identifiers, session metadata, locale and preferences;
- workout and programme data such as exercises, sets, repetitions, loads, routines, programme blocks, check-ins and training history;
- health and wellness data such as body metrics, sleep, recovery, readiness, activity and other records imported from an enabled health connector;
- coaching data such as goals, constraints, chat messages, prompts, generated recommendations, reasoning records and feedback;
- connector metadata such as provider account identifiers, sync cursors, connection state and timestamps;
- credentials supplied by a user, including API keys and OAuth tokens, which are used to connect enabled providers and must not be returned to the browser after submission;
- notification delivery data such as Telegram chat identifiers or web-push subscriptions when those channels are enabled;
- operational security data such as timestamps, request identifiers, error details and audit records required to operate and protect the service.

The application should not require unrelated personal information. Do not put secrets or health information into public GitHub issues, logs, fixtures, screenshots or documentation.

## Why we use the data

Workout Agent uses these data to authenticate users, import and normalise training or health records, build and adapt programmes, calculate progress and recovery signals, provide AI-assisted coaching, deliver user-requested notifications, support exports/deletion workflows, diagnose connector failures, and secure the service.

AI processing is optional and provider-dependent. When a user selects an AI provider, the prompt and the minimum context needed for the requested coaching feature can be sent to that provider. Users should avoid putting unnecessary identifying information in free-text coaching fields.

## Who receives data

The application itself processes data in the browser, FastAPI backend and configured database. Data is sent to an external service only when the corresponding feature is configured or invoked. Current integration families are recorded in `docs/data-flow-inventory.json` and include:

- Hevy for workout import;
- Google identity and Google Health/Fitbit where configured;
- Garmin, Oura, Polar and Withings health connectors;
- Google Gemini, Anthropic Claude, OpenAI and DeepSeek for optional AI coaching;
- Telegram and browser push infrastructure for optional notifications.

Each external provider operates under its own terms and privacy policy. The deployment operator is responsible for configuring only providers it intends to use and for meeting any contractual or legal requirements that apply to those providers.

## Retention and deletion

The machine-readable inventory records the intended retention rule for each system and flow. In the current self-hosted design, primary account, workout, coaching and health records are stored in the configured database until they are deleted by the application or deployment operator. Connector credentials remain only while the relevant connection is enabled or until revoked/deleted. Browser/session data is bounded by session and browser storage behaviour. External providers may retain data under their own policies after receiving a request.

Backups, logs and operational evidence are controlled by the deployment environment and should use the shortest period consistent with recovery, security and legal obligations. They must not become an indefinite secondary copy of user data.

A deployment must not claim that account deletion, backup purging or provider-side deletion is automatic unless those paths are actually configured and verified. Where a complete automated deletion flow is not yet available, the operator must handle a verified deletion request using the documented operational process.

## Your choices and rights

Users can choose which optional connectors, AI providers and notification channels to enable. They can disconnect providers and revoke their provider credentials. Where supported by the deployed product, users can request an export, correction or deletion of their account data and can withdraw consent for optional processing.

Applicable legal rights vary by jurisdiction. The deployment operator is responsible for authenticating a requester before disclosing, changing or deleting account data and for responding within any legally required period.

## Security

Credentials and health data are sensitive. Production deployments should require authentication, use encrypted transport, restrict tenant access, prevent personalised responses from being cached, protect secrets at rest, redact logs, rotate compromised credentials and keep tested backups. Security controls are defence in depth and do not replace careful provider configuration.

## Contact

For a privacy request about a deployed Workout Agent instance, contact the operator or organisation that gave you access to that instance. The operator must provide users with a private contact route before production use. Do **not** publish account, credential or health information in a public GitHub issue.

For a software defect in the public project that does not require sharing personal data, use the repository's normal GitHub issue or security-reporting process.

## Inventory change control

`docs/data-flow-inventory.json` is the canonical machine-readable map of data movement. CI runs `tools/validate_data_flow_inventory.py` to compare the inventory with registered AI providers, health connectors, Hevy, Google Health and notification integrations. Adding a provider without updating the inventory therefore fails the architecture check rather than silently creating an undocumented data flow.

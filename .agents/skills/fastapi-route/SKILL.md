---
name: fastapi-route
description: 'Add a new /api/* endpoint or page route to webapp/app.py following modern async FastAPI, Pydantic v2 validation, and session auth conventions.'
---

# FastAPI Route & API Endpoints

## When to Use

- Adding a new `/api/*` JSON endpoint (data retrieval, settings mutation, SSE streaming).
- Adding a new wearable or authentication OAuth flow.
- Extending WebSocket / SSE endpoints for streaming real-time AI responses.

## Reference Layout

- The FastAPI server in `backend/webapp/app.py` serves the compiled **Angular SPA** at `/` and handles all API traffic under `/api/*`.
- Request and response shapes are strictly typed with **Pydantic v2** models.
- Authentication is enforced via session middleware / OAuth2 tokens.

## Procedure

1. **Auth Enforcement:**
   - The session middleware verifies user sessions. Retrieve the acting user with `request.session.get("user")` or dependency injection (`get_current_user`).

2. **Define Typed Pydantic Request / Response Models:**
   - Never accept untyped dictionaries. Define clear Pydantic schemas for request bodies and API response models.

3. **Async Database Operations:**
   - Use asynchronous database queries (`asyncpg` / `SQLAlchemy 2.0 Async`) to keep all I/O non-blocking.

4. **Streaming Endpoints:**
   - For real-time chat, AI logs, and reasoning generation, return a `StreamingResponse` yielding text chunks from `AIProvider.generate(prompt, stream=True)` or Server-Sent Events.

5. **Testing:**
   - Add integration tests in `backend/tests/test_webapp.py` covering authenticated happy paths, 401 unauthorized responses, and validation error scenarios.


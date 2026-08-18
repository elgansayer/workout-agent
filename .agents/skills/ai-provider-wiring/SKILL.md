---
name: ai-provider-wiring
description: 'Resolve a user’s chosen AI provider (Gemini/Claude/OpenAI/DeepSeek/Ollama) and API key, then call it through ai_provider.py instead of hardcoding any specific SDK. Use when touching gemini_engine.py, insight_cron.py, or any backend endpoint generating AI text (chat, RAG search, XAI reasoning, insights).'
---

# AI Provider Wiring

## Why This Exists

`ai_provider.py` defines a clean `AIProvider` ABC + `get_provider()` factory supporting Gemini, Claude, OpenAI, DeepSeek, and local LLMs (Ollama / vLLM). The Settings UI lets each user securely configure their preferred provider and model with encrypted keys. This skill is the reference for adding new AI generation call sites or providers with zero vendor lock-in.

## When to Use

- Adding a new AI-generated feature (new prompt, new analysis, or streaming endpoint).
- Touching an existing generation call site in `gemini_engine.py`, `insight_cron.py`, or `webapp/app.py` (`/api/xai_reasoning`, `/api/project_peak`, `/api/rag_search`, `/api/chat/*`).
- Adding support for new providers or models (e.g. Claude 3.7/3.5, Gemini 2.5 Flash, DeepSeek-V3/R1, Ollama local instances).

## Procedure

1. **Resolve the provider for the acting user dynamically:**

   ```python
   from ai_provider import AIProvider, get_provider
   from ai_resolver import resolve_provider

   async def run_ai_task(user_id: str, prompt: str) -> str:
       provider: AIProvider = resolve_provider(user_id)
       return await provider.generate_async(prompt)
   ```

2. **Handle Streaming Responses for Real-Time UI (Chat & RAG Search):**
   - For interactive chat and log investigator endpoints, use `provider.generate(prompt, stream=True)` with FastAPI's `StreamingResponse` or Server-Sent Events (SSE).

3. **Handle provider-specific failure modes gracefully:**
   - Catch authentication or rate-limit exceptions at the route level and return clean, user-facing error messages ("Your OpenAI/Claude key is invalid or rate-limited; please check Settings").

4. **Support Local & Offline Models (Ollama / vLLM):**
   - Allow users to supply custom OpenAI-compatible base URLs (e.g. `http://localhost:11434/v1` for Ollama) to run 100% private, local inference without external API keys.

## Verification

Run the `verification-gate` suite. Add or update tests in `tests/test_ai_provider.py` verifying that provider resolution accurately picks up user preferences and securely loads encrypted keys.


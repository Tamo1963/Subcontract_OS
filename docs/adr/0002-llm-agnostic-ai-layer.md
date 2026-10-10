# ADR-0002 — LLM-agnostic AI layer

Status: **Accepted** · 2026-10-10

## Decision
The OS never depends on a particular model vendor. Administrators register any provider by
**endpoint, model and key**, chosen from a dropdown catalogue, and assign one provider per purpose.
With nothing assigned, the deterministic rules proposer and extractive retrieval are used.

## Design
| Element | Implementation |
|---|---|
| Provider catalogue (dropdown) | `app/llm/catalogue.py` — OpenAI, Anthropic, Azure OpenAI, Google Gemini, Mistral, Groq, DeepSeek, OpenRouter, Together, LiteLLM/enterprise gateway (Bedrock, Vertex…), Ollama, vLLM/TGI, LM Studio, custom OpenAI-compatible. Adding a provider that speaks an existing protocol is one catalogue line. |
| Wire protocols | `app/llm/gateway.py` — `openai` (chat/completions), `anthropic` (messages), `azure_openai` (deployments + api-version), `gemini` (generateContent), `ollama` (api/chat). Plain HTTPS via httpx; no vendor SDKs. |
| One call signature | `complete(db, provider, purpose, system, user)`; business code never sees a vendor format. |
| Purposes | `flowdown` (disposition proposals) and `knowledge` (answer drafting). New purposes are added to `PURPOSES`. |
| Keys | Write-only. Encrypted at rest with Fernet (`SCOS_SECRET_KEY` or an owner-only key file), or referenced by environment-variable name (`api_key_env`, preferred in production). The API returns only a hint (`…1234`). |
| Hybrid-hosting policy | Each provider has a data zone (PRIVATE / CLOUD). Content classified above `SCOS_LLM_CLOUD_MAX_CLASSIFICATION` (default CONFIDENTIAL) is never sent to a CLOUD provider; the rules proposer is used and the reason is recorded on the proposal. |
| Untrusted input | Clause and source text is delimited and the system prompt forbids following embedded instructions. Tool permissions never depend on model output. |
| Output validation | Flow-down: JSON must name one of the six dispositions with a rationale, otherwise rules fallback. Knowledge: every cited `[S#]` must be a retrieved, authorised source, otherwise the extractive answer stands with a warning. The server, not the model, owns source IDs. |
| Proposal, never decision | Model output fills `proposed_disposition` / draft answer only; humans record decisions. `proposal_method` records `llm:<provider>/<model>` or the fallback reason. |
| Observability | `audit_llm_calls` stores provider, model, purpose, latency, character counts and outcome — never prompt or response text. Provider changes and assignments are in the audit ledger. |
| Resilience | Timeouts, HTTP errors, malformed output and disabled providers all fall back to rules; the OS keeps working with no model available (blueprint principle). |

## Testing without keys
`seed/mock_llm.py` is a deterministic OpenAI-compatible stub (`uvicorn seed.mock_llm:app --port 8099`).
`tests/test_llm_agnostic.py` mocks all five protocols with `httpx.MockTransport`.

## Model upgrades
Per blueprint §14.6, promote a new provider or model only after it passes the labelled evaluation set and the
critical-clause, permission and financial regression suites. Switching is a configuration change, recorded in the audit trail.

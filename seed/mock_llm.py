"""Deterministic stand-in for an OpenAI-compatible model server, for demos and offline testing.

    uvicorn seed.mock_llm:app --port 8099
Register it in AI providers as "vLLM / TGI (self-hosted, OpenAI-compatible)" with base URL
http://localhost:8099/v1 and any model name. It is NOT a language model: it pattern-matches the
prompt so the end-to-end path (gateway, validation, citations, audit) can be exercised without keys.
"""
import json
import re

from fastapi import FastAPI, Request

app = FastAPI(title="Mock LLM (demo only)")
RULES = {"DELAY_LD": ("COMMERCIAL_DEVIATION", "LD exposure must be priced for this package; propose a reduced cap."),
         "NOTICE": ("ADAPTED", "Back-to-back notice with a shorter period so the Contractor can comply upstream."),
         "TERMINATION": ("ADAPTED", "Flow down with a subcontract cure period; legal to confirm."),
         "INSURANCE": ("PROJECT_SPECIFIC", "Insurance values must be sized to this subcontract."),
         "INDEMNITY_LIABILITY": ("PROJECT_SPECIFIC", "Cap liability at the subcontract value."),
         "DISPUTE": ("ADAPTED", "Arbitration flows down with joinder to main-contract disputes.")}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    system, user = body["messages"][0]["content"], body["messages"][-1]["content"]
    if "connectivity check" in system:
        out = {"status": "ok"}
    elif "disposition" in system:
        kind = re.search(r"\(classified (\w+)\)", user)
        disc = re.search(r"Discipline: (\w+)", user)
        kind = kind.group(1) if kind else "GENERAL"
        clause = user.split("<clause>")[-1].lower()
        if disc and disc.group(1) == "CIVIL" and kind == "TESTING" and "piping" in clause:
            out = {"disposition": "NOT_APPLICABLE", "rationale": "Piping test requirements fall outside the civil scope."}
        else:
            d, r = RULES.get(kind, ("DIRECT", "Applies to the subcontract scope with party references adjusted."))
            out = {"disposition": d, "rationale": r}
    else:
        first = re.search(r"\[S1\][^\n]*:\n(.+?)(?:\n\n\[S2\]|$)", user, re.S)
        text = (first.group(1).strip().split(". ")[0] if first else "").rstrip(".")
        out = {"answer": f"{text} [S1]." if text else "INSUFFICIENT", "citations": ["S1"] if text else []}
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(out)}}]}

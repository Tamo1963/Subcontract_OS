"""Contract-aware, permission-filtered evidence retrieval (blueprint Section 6.6).

Order: permissions -> applicable (governing) revisions -> exact clause/document match -> lexical
relevance -> rerank by authority. Answers are extractive and every source is derived from records
the caller is authorised to read; nothing is generated that cannot be traced to a passage.
A semantic (pgvector) stage plugs in after the lexical stage without changing this contract.
"""
import math
import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentRevision, ExtractionAssertion, Passage, ProcessingJob, User
from app.security import can_see_classification

STOP = set("a an the of to in on for and or is are be by with what which who when how does do any shall "
           "under this that from as at it its apply applies there their must can may".split())
AUTHORITY = {"SUBCONTRACT": 1.3, "SPECIAL_CONDITIONS": 1.25, "GCC": 1.2, "MAIN_CONTRACT": 1.2,
             "SPECIFICATION": 1.1}


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+)*", text.lower()) if t not in STOP and len(t) > 1]


def stem(t: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(t) > 4 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def query(db: Session, user: User, project_id: str, question: str, subcontract_id: str | None = None,
          include_superseded: bool = False, top_k: int = 5) -> dict:
    docs = list(db.scalars(select(Document).where(Document.project_id == project_id)))
    docs = [d for d in docs if can_see_classification(user, d.security_classification)
            and (d.subcontract_id is None or subcontract_id is None or d.subcontract_id == subcontract_id)]
    candidates = []
    for d in docs:
        usable = [r for r in d.revisions if r.ingestion_state not in ("CONFLICT", "PROCESSING_FAILED")]
        latest_unapproved = usable[-1].id if usable and d.governing_revision_id is None else None
        for r in usable:
            governing = r.id == d.governing_revision_id
            # Correspondence and records that nobody has approved are still evidence: use the latest revision,
            # clearly labelled NOT_APPROVED, and never rank it as governing.
            current = governing or r.id == latest_unapproved
            if not (current or include_superseded):
                continue
            job = db.scalar(select(ProcessingJob).where(ProcessingJob.revision_id == r.id,
                                                        ProcessingJob.status == "COMPLETED")
                            .order_by(ProcessingJob.completed_at.desc()))
            if job:
                for p in db.scalars(select(Passage).where(Passage.job_id == job.id)):
                    candidates.append((d, r, p, governing, current))

    q_tokens = [stem(t) for t in tokens(question)]
    clause_refs = set(re.findall(r"\b\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\b", question))
    if not candidates or not q_tokens:
        return _insufficient(question, "No authorised, governing evidence available for this project.")

    df = Counter()
    p_tokens = {}
    for _, _, p, _, _ in candidates:
        ts = [stem(t) for t in tokens(p.text)]
        p_tokens[p.id] = Counter(ts)
        df.update(set(ts))
    n = len(candidates)
    scored = []
    for d, r, p, governing, current in candidates:
        tf = p_tokens[p.id]
        matched = [t for t in set(q_tokens) if tf[t]]
        score = sum((1 + math.log(tf[t])) * math.log(1 + n / df[t]) for t in matched)
        if p.section_reference and p.section_reference in clause_refs:
            score += 25          # exact clause reference beats similarity
        if d.document_number.lower() in question.lower():
            score += 10
        score *= AUTHORITY.get(d.document_type, 1.0) * (1.0 if governing else 0.8 if current else 0.6)
        coverage = len(matched) / len(set(q_tokens))
        if score > 0:
            scored.append((score, coverage, d, r, p, governing, current))
    scored.sort(key=lambda x: -x[0])
    top = scored[:top_k]
    if not top or (top[0][1] < 0.5 and not clause_refs & {t[4].section_reference for t in top}):
        return _insufficient(question, "Retrieved passages do not cover the question; no answer is asserted.")

    sources, warnings, notice_values = [], [], {}
    for score, coverage, d, r, p, governing, current in top:
        if "EMBEDDED_INSTRUCTION" in (p.flags or []):
            warnings.append(f"Passage in {d.document_number} contains embedded instructions; treated as data only.")
        for a in db.scalars(select(ExtractionAssertion).where(ExtractionAssertion.passage_id == p.id,
                                                              ExtractionAssertion.field_name == "NOTICE_PERIOD_DAYS")):
            if p.section_reference:   # compare like with like: same clause reference across documents
                notice_values.setdefault(p.section_reference, {}).setdefault(a.normalised_value, []).append(
                    f"{d.document_number} rev {r.revision_label} cl. {p.section_reference}")
        sources.append({"documentId": d.id, "documentNumber": d.document_number, "documentType": d.document_type,
                        "revisionId": r.id, "revision": r.revision_label, "governing": governing,
                        "approval": "GOVERNING" if governing else "NOT_APPROVED" if current else "SUPERSEDED",
                        "passageId": p.id, "page": p.page_number, "clauseReference": p.section_reference,
                        "cell": p.cell_address, "excerpt": p.text[:600], "score": round(score, 2)})
    contradictions = [{"field": "NOTICE_PERIOD_DAYS", "clause": ref,
                       "values": {k.replace("|", " "): v for k, v in vals.items()},
                       "note": "Sources disagree; apply the contract's order of precedence and confirm."}
                      for ref, vals in notice_values.items() if len(vals) > 1]
    lead = sources[0]
    answer = (f"Most relevant governing provision: {lead['documentNumber']} rev {lead['revision']}"
              f"{', clause ' + lead['clauseReference'] if lead['clauseReference'] else ''}"
              f"{', page ' + str(lead['page']) if lead['page'] else ''}. Quoted evidence: \"{lead['excerpt'][:350]}\"")
    return {"question": question, "answer": answer,
            "status": "SUPPORTED_WITH_QUALIFICATIONS" if (contradictions or warnings) else "SUPPORTED",
            "sources": sources, "missingEvidence": [], "contradictions": contradictions, "warnings": warnings,
            "requiresProfessionalReview": True, "method": "lexical-v1 + exact-reference boost"}


def _insufficient(question: str, why: str) -> dict:
    return {"question": question, "answer": None, "status": "INSUFFICIENT_EVIDENCE", "sources": [],
            "missingEvidence": [why], "contradictions": [], "warnings": [], "requiresProfessionalReview": True,
            "method": "lexical-v1 + exact-reference boost"}

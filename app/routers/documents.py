"""Document ingestion, revision control, review queue (roadmap Step 5)."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, config
from app.db import get_db
from app.models import (ContractClause, Document, DocumentRevision, ExtractionAssertion, MainContract, Passage,
                        ProcessingJob, Project, Subcontract, User)
from app.routers.common import get_or_404
from app.security import can_see_classification, current_user, require
from app.services import ingestion

router = APIRouter(prefix="/api/v1", tags=["documents"])


def rev_out(r: DocumentRevision, governing_id: str | None) -> dict:
    return {"id": r.id, "revision": r.revision_label, "sha256": r.content_hash, "filename": r.filename,
            "size_bytes": r.size_bytes, "received_at": r.received_at.isoformat(), "issued_at": r.issued_at,
            "ingestion_state": r.ingestion_state, "business_status": r.business_status,
            "governing": r.id == governing_id, "conflict_note": r.conflict_note,
            "supersedes_revision_id": r.supersedes_revision_id}


def doc_out(d: Document) -> dict:
    return {"id": d.id, "project_id": d.project_id, "document_number": d.document_number, "title": d.title,
            "document_type": d.document_type, "source_party": d.source_party,
            "security_classification": d.security_classification, "main_contract_id": d.main_contract_id,
            "subcontract_id": d.subcontract_id, "governing_revision_id": d.governing_revision_id,
            "revisions": [rev_out(r, d.governing_revision_id) for r in d.revisions]}


def _load_doc(db, user, doc: Document, action="project.read"):
    require(db, user, action, doc.project_id)
    if not can_see_classification(user, doc.security_classification):
        raise HTTPException(403, {"code": "CLASSIFICATION", "message": "Document classification exceeds clearance"})
    return doc


@router.post("/documents", status_code=201)
async def upload(file: UploadFile = File(...), project_id: str = Form(...), document_number: str = Form(...),
                 revision: str = Form(...), document_type: str = Form(...), title: str = Form(""),
                 main_contract_id: str | None = Form(None), subcontract_id: str | None = Form(None),
                 source_party: str = Form("EMPLOYER"), security_classification: str = Form("CONFIDENTIAL"),
                 issued_at: str | None = Form(None), user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    get_or_404(db, Project, project_id, "Project")
    require(db, user, "document.upload", project_id)
    # Resolve declared context; never guess across projects.
    if main_contract_id:
        mc = db.get(MainContract, main_contract_id)
        if not mc or mc.project_id != project_id:
            raise HTTPException(422, {"code": "CONTEXT_MISMATCH", "message": "Main contract is not in this project"})
    if subcontract_id:
        sc = db.get(Subcontract, subcontract_id)
        if not sc or sc.project_id != project_id:
            raise HTTPException(422, {"code": "CONTEXT_MISMATCH", "message": "Subcontract is not in this project"})
    data = await file.read()
    res = ingestion.register_revision(
        db, actor_id=user.id, project_id=project_id, document_number=document_number.strip(),
        revision_label=revision.strip(), document_type=document_type.upper(), title=title or file.filename,
        filename=file.filename, data=data, main_contract_id=main_contract_id, subcontract_id=subcontract_id,
        source_party=source_party, security_classification=security_classification.upper(), issued_at=issued_at)
    db.commit()
    job = None
    if res.outcome in ("NEW_DOCUMENT", "NEW_REVISION"):
        job = ingestion.process_revision(db, res.revision)
    db.refresh(res.document)
    return {"outcome": res.outcome, "message": res.message, "document": doc_out(res.document),
            "revision_id": res.revision.id,
            "job": {"id": job.id, "status": job.status, "stats": job.stats, "error": job.error_code} if job else None}


@router.get("/projects/{pid}/documents")
def list_documents(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.read", pid)
    docs = db.scalars(select(Document).where(Document.project_id == pid).order_by(Document.document_number))
    return [doc_out(d) for d in docs if can_see_classification(user, d.security_classification)]


@router.get("/documents/{did}")
def get_document(did: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = _load_doc(db, user, get_or_404(db, Document, did, "Document"))
    out = doc_out(d)
    out["jobs"] = [{"id": j.id, "revision_id": j.revision_id, "pipeline_version": j.pipeline_version,
                    "status": j.status, "stats": j.stats, "error": j.error_code}
                   for r in d.revisions for j in db.scalars(select(ProcessingJob).where(ProcessingJob.revision_id == r.id))]
    return out


def _load_rev(db, user, rid, action="project.read") -> DocumentRevision:
    r = get_or_404(db, DocumentRevision, rid, "Revision")
    _load_doc(db, user, r.document, action)
    return r


@router.get("/revisions/{rid}/passages")
def passages(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _load_rev(db, user, rid)
    out = []
    for p in ingestion.latest_job_passages(db, rid):
        a = db.scalars(select(ExtractionAssertion).where(ExtractionAssertion.passage_id == p.id))
        out.append({"id": p.id, "seq": p.seq, "page": p.page_number, "section": p.section_reference,
                    "cell": p.cell_address, "type": p.content_type, "text": p.text, "flags": p.flags,
                    "assertions": [{"id": x.id, "field": x.field_name, "value": x.normalised_value,
                                    "confidence": float(x.confidence), "status": x.validation_status} for x in a]})
    return out


@router.get("/revisions/{rid}/download")
def download(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = _load_rev(db, user, rid)
    data = ingestion.read_object(r.object_key)
    import hashlib
    if hashlib.sha256(data).hexdigest() != r.content_hash:
        raise HTTPException(500, {"code": "INTEGRITY_FAILURE", "message": "Stored object hash mismatch"})
    audit.record(db, user.id, "document.downloaded", "DocumentRevision", r.id, project_id=r.document.project_id)
    db.commit()
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{r.filename}"', "X-Content-SHA256": r.content_hash})


class ApproveIn(BaseModel):
    reason: str = "Verified against source and approved as governing revision"


@router.post("/revisions/{rid}/approve")
def approve_revision(rid: str, body: ApproveIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Explicitly designate the governing revision. Uploading never does this implicitly."""
    r = _load_rev(db, user, rid, "document.review")
    if r.ingestion_state in ("CONFLICT", "PROCESSING_FAILED", "PROCESSING", "RECEIVED"):
        raise HTTPException(409, {"code": "NOT_APPROVABLE", "message": f"Revision is {r.ingestion_state}"})
    d = r.document
    prev = d.governing_revision_id
    if prev and prev != r.id:
        old = db.get(DocumentRevision, prev)
        old.business_status, old.ingestion_state = "SUPERSEDED", "SUPERSEDED"
        r.supersedes_revision_id = prev
    r.business_status, r.ingestion_state = "APPROVED", "PUBLISHED"
    d.governing_revision_id = r.id
    audit.record(db, user.id, "document.revision_approved", "DocumentRevision", r.id, project_id=d.project_id,
                 before={"governing_revision_id": prev}, after={"governing_revision_id": r.id}, reason=body.reason)
    db.commit()
    db.refresh(d)
    return doc_out(d)


class ReprocessIn(BaseModel):
    pipeline_version: str = config.PIPELINE_VERSION


@router.post("/revisions/{rid}/reprocess")
def reprocess(rid: str, body: ReprocessIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = _load_rev(db, user, rid, "document.review")
    job = ingestion.process_revision(db, r, body.pipeline_version)
    return {"id": job.id, "status": job.status, "pipeline_version": job.pipeline_version, "stats": job.stats}


@router.get("/projects/{pid}/review-queue")
def review_queue(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.read", pid)
    rows = db.execute(select(ExtractionAssertion, Passage, DocumentRevision, Document)
                      .join(Passage, ExtractionAssertion.passage_id == Passage.id)
                      .join(DocumentRevision, Passage.revision_id == DocumentRevision.id)
                      .join(Document, DocumentRevision.document_id == Document.id)
                      .where(Document.project_id == pid, ExtractionAssertion.validation_status == "PROPOSED"))
    out = []
    for a, p, r, d in rows:
        if can_see_classification(user, d.security_classification):
            out.append({"id": a.id, "field": a.field_name, "proposed_value": a.normalised_value,
                        "raw_value": a.raw_value, "confidence": float(a.confidence), "method": a.method,
                        "document_number": d.document_number, "revision": r.revision_label,
                        "section": p.section_reference, "page": p.page_number, "passage_text": p.text})
    conflicts = db.execute(select(DocumentRevision, Document).join(Document).where(
        Document.project_id == pid, DocumentRevision.ingestion_state == "CONFLICT"))
    flagged = db.execute(select(Passage, Document).join(DocumentRevision, Passage.revision_id == DocumentRevision.id)
                         .join(Document).where(Document.project_id == pid))
    return {"assertions": out,
            "revision_conflicts": [{"revision_id": r.id, "document_number": d.document_number, "note": r.conflict_note}
                                   for r, d in conflicts],
            "flagged_passages": [{"passage_id": p.id, "document_number": d.document_number, "flags": p.flags,
                                  "text": p.text[:300]} for p, d in flagged if p.flags]}


class AssertionReviewIn(BaseModel):
    decision: str           # ACCEPT | REJECT | EDIT
    value: str | None = None


@router.post("/assertions/{aid}/review")
def review_assertion(aid: str, body: AssertionReviewIn, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    a = get_or_404(db, ExtractionAssertion, aid, "Assertion")
    p = db.get(Passage, a.passage_id)
    _load_rev(db, user, p.revision_id, "document.review")
    before = {"value": a.normalised_value, "status": a.validation_status}
    if body.decision == "ACCEPT":
        a.validation_status = "ACCEPTED"
    elif body.decision == "REJECT":
        a.validation_status = "REJECTED"
    elif body.decision == "EDIT" and body.value:
        a.normalised_value, a.validation_status = body.value, "EDITED"
    else:
        raise HTTPException(422, {"code": "INVALID_DECISION", "message": "ACCEPT, REJECT or EDIT with value"})
    a.reviewer_id = user.id
    audit.record(db, user.id, "assertion.reviewed", "ExtractionAssertion", a.id,
                 project_id=db.get(DocumentRevision, p.revision_id).document.project_id,
                 before=before, after={"value": a.normalised_value, "status": a.validation_status})
    db.commit()
    return {"id": a.id, "status": a.validation_status, "value": a.normalised_value}


@router.get("/revisions/{rid}/clauses")
def clauses(rid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _load_rev(db, user, rid)
    return [{"id": c.id, "clause_reference": c.clause_reference, "heading": c.heading, "clause_type": c.clause_type,
             "text": c.text} for c in db.scalars(select(ContractClause).where(ContractClause.document_revision_id == rid))]

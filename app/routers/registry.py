"""Registry, projects, memberships and main contracts (roadmap Step 4)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audit
from app.db import get_db
from app.models import (MainContract, Project, ProjectMembership, ProjectSubcontractor, Subcontract,
                        Subcontractor, User, now)
from app.routers.common import get_or_404, sub_out
from app.security import accessible_project_ids, current_user, require

router = APIRouter(prefix="/api/v1", tags=["registry & projects"])


class SubcontractorIn(BaseModel):
    legal_name: str = Field(min_length=2)
    registration_number: str = Field(min_length=3)
    country_code: str = Field(min_length=2, max_length=2)
    disciplines: list[str] = []


def sc_out(s: Subcontractor) -> dict:
    return {"id": s.id, "legal_name": s.legal_name, "registration_number": s.registration_number,
            "country_code": s.country_code, "disciplines": s.disciplines, "entity_status": s.entity_status}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ms = db.scalars(select(ProjectMembership).where(ProjectMembership.user_id == user.id))
    return {"id": user.id, "username": user.username, "full_name": user.full_name, "roles": user.roles,
            "clearance": user.clearance, "projects": {m.project_id: m.roles for m in ms},
            "must_change_password": bool(user.must_change_password and user.password_hash)}


@router.post("/subcontractors", status_code=201)
def create_subcontractor(body: SubcontractorIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "registry.write")
    cc, reg = body.country_code.upper(), body.registration_number.strip().upper()
    existing = db.scalar(select(Subcontractor).where(Subcontractor.country_code == cc,
                                                     Subcontractor.registration_number == reg))
    if existing:   # REG-01
        raise HTTPException(409, {"code": "DUPLICATE_LEGAL_ENTITY", "message": "Legal entity already registered",
                                  "existing_id": existing.id})
    s = Subcontractor(legal_name=body.legal_name, registration_number=reg, country_code=cc,
                      disciplines=body.disciplines)
    db.add(s)
    db.flush()
    audit.record(db, user.id, "subcontractor.created", "Subcontractor", s.id, after=sc_out(s))
    db.commit()
    return sc_out(s)


@router.get("/subcontractors")
def list_subcontractors(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [sc_out(s) for s in db.scalars(select(Subcontractor).order_by(Subcontractor.legal_name))]


@router.get("/subcontractors/{sid}")
def get_subcontractor(sid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = get_or_404(db, Subcontractor, sid, "Subcontractor")
    visible = set(accessible_project_ids(db, user))
    links = [{"project_id": l.project_id, "eligibility_status": l.eligibility_status, "package": l.package}
             for l in db.scalars(select(ProjectSubcontractor).where(ProjectSubcontractor.subcontractor_id == sid))
             if l.project_id in visible]   # project relationships only where the caller has access
    return {**sc_out(s), "project_relationships": links}


class ProjectIn(BaseModel):
    project_code: str
    name: str
    sector_code: str = "PROCESS"
    location: str = ""


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.admin")
    p = Project(**body.model_dump())
    db.add(p)
    db.flush()
    db.add(ProjectMembership(user_id=user.id, project_id=p.id, roles=["project_coordinator"]))
    audit.record(db, user.id, "project.created", "Project", p.id, project_id=p.id, after=body.model_dump())
    db.commit()
    return {"id": p.id, **body.model_dump()}


@router.get("/projects")
def list_projects(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = accessible_project_ids(db, user)
    return [{"id": p.id, "project_code": p.project_code, "name": p.name, "sector_code": p.sector_code,
             "location": p.location} for p in db.scalars(select(Project).where(Project.id.in_(ids)))]


@router.get("/projects/{pid}")
def get_project(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = get_or_404(db, Project, pid, "Project")
    require(db, user, "project.read", pid)
    mcs = db.scalars(select(MainContract).where(MainContract.project_id == pid))
    links = db.scalars(select(ProjectSubcontractor).where(ProjectSubcontractor.project_id == pid))
    subs = db.scalars(select(Subcontract).where(Subcontract.project_id == pid))
    return {"id": p.id, "project_code": p.project_code, "name": p.name, "sector_code": p.sector_code,
            "location": p.location, "timezone": p.timezone,
            "main_contracts": [{"id": m.id, "contract_reference": m.contract_reference,
                                "employer_name": m.employer_name, "effective_date": m.effective_date} for m in mcs],
            "eligible_subcontractors": [{"subcontractor_id": l.subcontractor_id, "eligibility_status":
                                         l.eligibility_status, "package": l.package} for l in links],
            "subcontracts": [sub_out(s) for s in subs]}


class MemberIn(BaseModel):
    username: str
    roles: list[str]


@router.post("/projects/{pid}/members", status_code=201)
def add_member(pid: str, body: MemberIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.admin", pid)
    target = db.scalar(select(User).where(User.username == body.username))
    if not target:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "User not found"})
    existing = db.scalar(select(ProjectMembership).where(ProjectMembership.user_id == target.id,
                                                         ProjectMembership.project_id == pid))
    if existing:   # add roles to an existing membership instead of failing
        body.roles = sorted(set(existing.roles) | set(body.roles))
        existing.roles = body.roles
    else:
        db.add(ProjectMembership(user_id=target.id, project_id=pid, roles=body.roles))
    audit.record(db, user.id, "membership.granted", "Project", pid, project_id=pid,
                 after={"user": body.username, "roles": body.roles})
    db.commit()
    return {"project_id": pid, "username": body.username, "roles": body.roles}


@router.get("/projects/{pid}/members")
def list_members(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.read", pid)
    rows = db.execute(select(ProjectMembership, User).join(User, ProjectMembership.user_id == User.id)
                      .where(ProjectMembership.project_id == pid).order_by(User.username))
    return [{"username": u.username, "full_name": u.full_name, "roles": m.roles} for m, u in rows]


@router.delete("/projects/{pid}/members/{username}")
def remove_member(pid: str, username: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require(db, user, "project.admin", pid)
    target = db.scalar(select(User).where(User.username == username))
    if target:
        db.execute(delete(ProjectMembership).where(ProjectMembership.user_id == target.id,
                                                   ProjectMembership.project_id == pid))
        audit.record(db, user.id, "membership.revoked", "Project", pid, project_id=pid, before={"user": username})
        db.commit()
    return {"revoked": username}


class MainContractIn(BaseModel):
    contract_reference: str
    employer_name: str
    effective_date: str | None = None


@router.post("/projects/{pid}/main-contracts", status_code=201)
def create_main_contract(pid: str, body: MainContractIn, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    require(db, user, "project.admin", pid)
    m = MainContract(project_id=pid, **body.model_dump())
    db.add(m)
    db.flush()
    audit.record(db, user.id, "main_contract.registered", "MainContract", m.id, project_id=pid, after=body.model_dump())
    db.commit()
    return {"id": m.id, "project_id": pid, **body.model_dump()}


class EligibilityIn(BaseModel):
    subcontractor_id: str
    package: str = ""
    eligibility_status: str = "ELIGIBLE"


@router.post("/projects/{pid}/subcontractors", status_code=201)
def link_subcontractor(pid: str, body: EligibilityIn, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    require(db, user, "project.admin", pid)
    get_or_404(db, Subcontractor, body.subcontractor_id, "Subcontractor")
    link = ProjectSubcontractor(project_id=pid, subcontractor_id=body.subcontractor_id, package=body.package,
                                eligibility_status=body.eligibility_status, approved_by=user.id, approved_at=now())
    db.add(link)
    audit.record(db, user.id, "project_subcontractor.linked", "Project", pid, project_id=pid, after=body.model_dump())
    db.commit()
    return {"id": link.id, **body.model_dump()}

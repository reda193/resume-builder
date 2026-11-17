import re
import unicodedata
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from edit import EditError, InvalidChangeError, apply_edits, propose_edits
from extract import (
    AIExtractionError,
    UnreadableFileError,
    UnsupportedFileError,
    extract_from_file,
    validate_resume,
)
from tailor import BuildError, TailoringError, build_pdf, tailor_to_job
from user_profile import resume_to_profile, validate_profile

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

app = FastAPI(title="Resume Builder API")


class ProfileRequest(BaseModel):
    resume: dict


class BuildRequest(BaseModel):
    resume: dict


class EditRequest(BaseModel):
    resume: dict
    profile: dict
    request: str = Field(min_length=3, max_length=2000)
    lines: Literal[1, 2] = 2


class ApplyRequest(BaseModel):
    resume: dict
    changes: list[dict] = Field(max_length=50)


class TailorRequest(BaseModel):
    profile: dict
    job_posting: str = Field(min_length=50, max_length=20000)
    lines: Literal[1, 2] = 2
    bullets: int = Field(default=3, ge=1, le=10)
    projects: int = Field(default=2, ge=0, le=10)
    pages: int = Field(default=1, ge=1, le=2)
    group_roles: bool = False
    build: bool = True


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract")
def extract(file: UploadFile):
    data = file.file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is larger than 5 MB.")

    try:
        result = extract_from_file(data, file.filename)
    except UnsupportedFileError as e:
        raise HTTPException(status_code=415, detail=str(e))
    except UnreadableFileError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except AIExtractionError as e:
        print(f"Extraction failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))

    return result


@app.post("/profile")
def make_profile(request: ProfileRequest):
    problems = validate_resume(request.resume)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    profile = resume_to_profile(request.resume)

    problems = validate_profile(profile)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    return profile


@app.post("/tailor")
def tailor(request: TailorRequest):
    problems = validate_profile(request.profile)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    settings = {
        "lines": request.lines,
        "bullets": request.bullets,
        "projects": request.projects,
        "pages": request.pages,
        "group_roles": request.group_roles,
    }

    try:
        result = tailor_to_job(request.profile, request.job_posting, settings, build=request.build)
    except TailoringError as e:
        print(f"Tailoring failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    except BuildError as e:
        print(f"Build failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))

    return result


@app.post("/build")
def build(request: BuildRequest):
    problems = validate_resume(request.resume)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    try:
        pdf, report = build_pdf(request.resume)
    except BuildError as e:
        print(f"Build failed: {e}")
        raise HTTPException(status_code=503, detail=str(e))

    if pdf is None:
        raise HTTPException(status_code=422, detail=report["problems"])

    name = request.resume.get("basics", {}).get("name", "")
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", plain).strip("_") or "Resume"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}_Resume.pdf"',
            "X-Page-Count": str(report["pages"]),
        },
    )


@app.post("/edit")
def edit(request: EditRequest):
    problems = validate_resume(request.resume) + validate_profile(request.profile)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    try:
        return propose_edits(request.resume, request.profile, request.request, request.lines)
    except EditError as e:
        print(f"Editing failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/edit/apply")
def apply_edit(request: ApplyRequest):
    problems = validate_resume(request.resume)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    try:
        return {"resume": apply_edits(request.resume, request.changes)}
    except InvalidChangeError as e:
        raise HTTPException(status_code=422, detail=str(e))
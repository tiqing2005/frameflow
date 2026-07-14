from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..asr import resolve_asr_source_token


router = APIRouter(prefix="/api/v1/asr", tags=["asr-internal"])


@router.get("/source/{token}", include_in_schema=False)
def get_temporary_asr_source(token: str, request: Request):
    path = resolve_asr_source_token(token, request.app.state.settings)
    if path is None:
        raise HTTPException(status_code=404, detail="ASR source is unavailable")
    return FileResponse(path, filename=path.name)

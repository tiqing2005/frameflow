from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..asr import resolve_asr_source_token


router = APIRouter(prefix="/api/v1/asr", tags=["asr-internal"])


def _asr_source_response(token: str, request: Request) -> FileResponse:
    path = resolve_asr_source_token(token, request.app.state.settings)
    if path is None:
        raise HTTPException(status_code=404, detail="ASR source is unavailable")
    return FileResponse(path, media_type="audio/mpeg", filename="audio.mp3")


@router.get("/source/{token}/audio.mp3", include_in_schema=False)
def get_temporary_asr_mp3_source(token: str, request: Request):
    """Serve the compact ASR input from a URL with an explicit media suffix."""

    return _asr_source_response(token, request)


@router.get("/source/{token}", include_in_schema=False)
def get_temporary_asr_source(token: str, request: Request):
    # Preserve already-issued URLs during rolling deployments. New DashScope
    # submissions use the explicit /audio.mp3 route above.
    return _asr_source_response(token, request)

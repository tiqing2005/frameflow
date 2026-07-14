from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import FileResponse

from ..errors import request_id
from ..schemas import (
    ImageGenerationAccept,
    ImageGenerationCreate,
    SegmentImageGenerationCreate,
)
from ..serializers import image_generation_dict
from ..services.image_generations import (
    accept_image_generation,
    cancel_image_generation,
    create_image_generation,
    discard_image_generation,
    image_generation_content_path,
    image_generation_detail,
    list_image_generations,
    retry_image_generation,
)
from ._deps import SessionDep, SettingsDep


router = APIRouter(prefix="/api/v1", tags=["image-generation"])


@router.post("/image-generations", status_code=status.HTTP_202_ACCEPTED)
def post_image_generation(
    payload: ImageGenerationCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    generation, replay = create_image_generation(
        session,
        settings,
        payload,
        idempotency_key,
        request_id(request),
    )
    response.headers["Idempotent-Replay"] = "true" if replay else "false"
    return {
        "generation": image_generation_dict(generation),
        "idempotent_replay": replay,
    }


@router.post(
    "/segments/{segment_id}/image-generations",
    status_code=status.HTTP_202_ACCEPTED,
)
def post_segment_image_generation(
    segment_id: str,
    payload: SegmentImageGenerationCreate,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    generation, replay = create_image_generation(
        session,
        settings,
        payload,
        idempotency_key,
        request_id(request),
        segment_id=segment_id,
    )
    response.headers["Idempotent-Replay"] = "true" if replay else "false"
    return {
        "generation": image_generation_dict(generation),
        "idempotent_replay": replay,
    }


@router.get("/image-generations")
def get_image_generations(
    session: SessionDep,
    task_status: str | None = Query(
        default=None,
        alias="status",
        pattern="^(queued|running|succeeded|failed|canceled)$",
    ),
    segment_id: str | None = Query(default=None, max_length=36),
    include_discarded: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return list_image_generations(
        session,
        status=task_status,
        segment_id=segment_id,
        include_discarded=include_discarded,
        limit=limit,
        offset=offset,
    )


@router.get("/image-generations/{generation_id}")
def get_image_generation_detail(generation_id: str, session: SessionDep):
    return image_generation_detail(session, generation_id)


@router.get("/image-generations/{generation_id}/content")
def get_image_generation_content(
    generation_id: str, session: SessionDep, settings: SettingsDep
):
    path = image_generation_content_path(session, settings, generation_id)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post(
    "/image-generations/{generation_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
def post_image_generation_retry(
    generation_id: str, request: Request, session: SessionDep
):
    generation = retry_image_generation(
        session, generation_id, request_id(request)
    )
    return {"generation": image_generation_dict(generation)}


@router.post("/image-generations/{generation_id}/cancel")
def post_image_generation_cancel(
    generation_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
):
    generation = cancel_image_generation(
        session, settings, generation_id, request_id(request)
    )
    return {"generation": image_generation_dict(generation)}


@router.post("/image-generations/{generation_id}/accept")
def post_image_generation_accept(
    generation_id: str,
    payload: ImageGenerationAccept,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
):
    result = accept_image_generation(
        session,
        settings,
        generation_id,
        payload,
        request_id(request),
    )
    response.headers["Idempotent-Replay"] = (
        "true" if result["idempotent_replay"] else "false"
    )
    return result


@router.delete(
    "/image-generations/{generation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_image_generation(
    generation_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    discard_image_generation(
        session, settings, generation_id, request_id(request)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

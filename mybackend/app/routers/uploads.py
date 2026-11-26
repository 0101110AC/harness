"""上传与文件服务路由：/api/v1/drawings/upload-sessions、/view-url、/files/{token}"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse

from app.core import storage
from app.core.deps import DB, require
from app.core.errors import AppError, NotFound
from app.core.file_token import verify_file_token
from app.models.drawing import Drawing
from app.models.user import User
from app.schemas.drawing import DrawingOut, ViewUrlResponse
from app.schemas.upload import (
    ChunkUploadResponse,
    UploadCompleteRequest,
    UploadSessionCreateRequest,
    UploadSessionOut,
)
from app.services import upload_service as svc
from sqlalchemy import select

router = APIRouter(tags=["drawings"])

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "dxf": "application/dxf",
}


@router.post(
    "/drawings/upload-sessions",
    response_model=UploadSessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建上传会话（分片上传第 1 步）",
)
async def create_upload_session(
    body: UploadSessionCreateRequest,
    db: DB,
    user: Annotated[User, Depends(require("drawing:upload"))],
) -> UploadSessionOut:
    return await svc.create_session(db, body, user)


@router.put(
    "/drawings/upload-sessions/{upload_id}/chunks/{index}",
    response_model=ChunkUploadResponse,
    summary="上传单个分片（请求体为二进制，支持并发）",
)
async def upload_chunk(
    upload_id: uuid.UUID,
    index: int,
    request: Request,
    db: DB,
    user: Annotated[User, Depends(require("drawing:upload"))],
) -> ChunkUploadResponse:
    return await svc.receive_chunk(db, upload_id, index, request.stream(), user)


@router.post(
    "/drawings/upload-sessions/{upload_id}/complete",
    response_model=DrawingOut,
    status_code=status.HTTP_201_CREATED,
    summary="合并分片并落库（含魔数 / MD5 校验）",
)
async def complete_upload(
    upload_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("drawing:upload"))],
    body: UploadCompleteRequest | None = None,
) -> DrawingOut:
    return await svc.complete(db, upload_id, body.md5 if body else None, user)


@router.get(
    "/drawings/{drawing_id}/view-url",
    response_model=ViewUrlResponse,
    summary="获取短时签名预览地址（10 分钟）",
)
async def get_view_url(
    drawing_id: uuid.UUID,
    db: DB,
    user: Annotated[User, Depends(require("drawing:read"))],
) -> ViewUrlResponse:
    data = await svc.issue_view_url(db, drawing_id, user)
    return ViewUrlResponse(**data)


@router.get(
    "/files/{token}",
    summary="按签名令牌返回文件流（供 <img>/<iframe> 直接引用）",
    response_class=FileResponse,
)
async def serve_file(token: str, db: DB) -> FileResponse:
    """鉴权前置在签发环节（drawing:read + 项目成员）；此处靠 HMAC 签名 + 过期时间。

    浏览器 <img>/<iframe> 无法附加 Authorization 头，所以这里不能要求 Bearer，
    否则预览功能不可用 —— 这是能力型 URL 的标准做法（详见 D3 复盘 2.5）。
    """
    drawing_id = verify_file_token(token)

    drawing = (
        await db.execute(
            select(Drawing).where(Drawing.id == drawing_id, Drawing.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if drawing is None:
        raise NotFound("文件不存在或已删除")

    path: Path = storage.resolve_key(drawing.storage_key)
    if not path.exists():
        raise AppError("FILE_NOT_FOUND", "文件已丢失，请重新上传", 404)

    return FileResponse(
        path=path,
        media_type=MEDIA_TYPES.get(drawing.file_type.value, "application/octet-stream"),
        filename=drawing.file_name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=600"},
    )

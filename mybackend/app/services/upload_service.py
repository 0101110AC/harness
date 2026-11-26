"""分片上传业务：会话创建 → 分片接收 → 合并校验落地。"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import settings
from app.core.deps import ensure_project_member
from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.models.drawing import Drawing, UploadSession
from app.models.enums import DrawingStatus, FileType, UploadSessionStatus
from app.models.project import Project
from app.models.user import User
from app.schemas.drawing import DrawingOut
from app.schemas.upload import (
    ChunkUploadResponse,
    UploadSessionCreateRequest,
    UploadSessionOut,
)

log = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {"pdf", "dxf"}
MAX_SIZES = {"pdf": settings.MAX_PDF_SIZE, "dxf": settings.MAX_DXF_SIZE}


# ------------------------------------------------------------------ 错误码
def _reject(code: str, message: str, detail: dict | None = None) -> AppError:
    return AppError(code, message, 422, detail)


# ------------------------------------------------------------------ 会话
async def create_session(
    db: AsyncSession, payload: UploadSessionCreateRequest, user: User
) -> UploadSessionOut:
    await ensure_project_member(db, payload.project_id, user)

    suffix = storage.sniff_filename_suffix(payload.file_name)

    # 三重校验之一：后缀（DWG 单独拦，给前端专门的引导弹窗用）
    if suffix == "dwg":
        raise _reject(
            "ERR_DWG_UNSUPPORTED",
            "暂不支持 DWG 格式，请另存为 DXF 后上传",
            {"file_name": payload.file_name, "how_to": "CAD → 另存为 → DXF 2018"},
        )
    if suffix not in ALLOWED_SUFFIXES:
        raise _reject(
            "ERR_EXT_NOT_ALLOWED",
            "当前版本仅支持 PDF 与 DXF 文件",
            {"file_name": payload.file_name, "allowed": sorted(ALLOWED_SUFFIXES)},
        )
    if suffix != payload.file_type:
        raise _reject(
            "ERR_MIME_MISMATCH",
            f"文件后缀（{suffix}）与声明类型（{payload.file_type}）不一致",
            {"suffix": suffix, "file_type": payload.file_type},
        )

    limit = MAX_SIZES[payload.file_type]
    if payload.total_size > limit:
        raise _reject(
            "ERR_FILE_TOO_LARGE",
            f"{payload.file_type.upper()} 文件不能超过 {limit // 1024 // 1024}MB",
            {"size": payload.total_size, "limit": limit},
        )

    chunk_size = settings.CHUNK_SIZE
    chunk_total = max(1, -(-payload.total_size // chunk_size))  # ceil
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.UPLOAD_SESSION_TTL_HOURS)

    session = UploadSession(
        project_id=payload.project_id,
        user_id=user.id,
        file_name=payload.file_name,
        file_type=FileType(payload.file_type),
        total_size=payload.total_size,
        chunk_size=chunk_size,
        chunk_total=chunk_total,
        uploaded_chunks=0,
        md5=payload.md5,
        status=UploadSessionStatus.UPLOADING,
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()

    # 每个会话独立临时目录：多用户并发上传天然隔离
    await asyncio.to_thread(storage.temp_dir(session.id).mkdir, parents=True, exist_ok=True)

    await db.commit()
    return UploadSessionOut(
        upload_id=session.id,
        chunk_size=chunk_size,
        chunk_total=chunk_total,
        expires_at=expires_at,
        status=session.status,
    )


async def _get_session_or_404(db: AsyncSession, upload_id: uuid.UUID) -> UploadSession:
    session = (
        await db.execute(select(UploadSession).where(UploadSession.id == upload_id))
    ).scalar_one_or_none()
    if session is None:
        raise NotFound("上传会话不存在")
    return session


def _ensure_session_usable(session: UploadSession) -> None:
    if session.status is not UploadSessionStatus.UPLOADING:
        raise Conflict(
            "上传会话不在可写状态",
            code="INVALID_STATE_TRANSITION",
            detail={"status": session.status.value},
        )
    if session.expires_at < datetime.now(timezone.utc):
        raise _reject("UPLOAD_SESSION_EXPIRED", "上传会话已过期，请重新创建", None)


# ------------------------------------------------------------------ 分片
async def receive_chunk(
    db: AsyncSession,
    upload_id: uuid.UUID,
    index: int,
    stream: AsyncIterator[bytes],
    user: User,
) -> ChunkUploadResponse:
    session = await _get_session_or_404(db, upload_id)
    await ensure_project_member(db, session.project_id, user)
    _ensure_session_usable(session)

    if index < 0 or index >= session.chunk_total:
        raise _reject(
            "ERR_CHUNK_INDEX_OUT_OF_RANGE",
            "分片下标超出范围",
            {"index": index, "chunk_total": session.chunk_total},
        )

    written = await storage.write_chunk(upload_id, index, stream)
    if written == 0:
        raise _reject("ERR_CHUNK_EMPTY", "分片内容为空", {"index": index})

    # 幂等计数：直接数磁盘上的完整分片，而不是 += 1
    # （重传、并发提交都不会把 uploaded_chunks 数成超过 chunk_total）
    received = await asyncio.to_thread(storage.count_chunks, upload_id)

    await db.execute(
        update(UploadSession)
        .where(UploadSession.id == upload_id)
        .values(uploaded_chunks=min(received, session.chunk_total))
    )
    await db.commit()

    return ChunkUploadResponse(
        upload_id=upload_id, received=min(received, session.chunk_total),
        chunk_total=session.chunk_total,
    )


# ------------------------------------------------------------------ 合并
async def complete(
    db: AsyncSession,
    upload_id: uuid.UUID,
    declared_md5: str | None,
    user: User,
) -> DrawingOut:
    session = await _get_session_or_404(db, upload_id)
    await ensure_project_member(db, session.project_id, user)
    if session.status is UploadSessionStatus.COMPLETED:
        raise Conflict("该上传已完成", code="INVALID_STATE_TRANSITION",
                       detail={"status": session.status.value})
    _ensure_session_usable(session)

    # 抢占合并权：条件更新 + 立即提交，保证并发 complete 只有一个能进（复盘 2.3）
    claimed = await db.execute(
        update(UploadSession)
        .where(
            UploadSession.id == upload_id,
            UploadSession.status == UploadSessionStatus.UPLOADING,
        )
        .values(status=UploadSessionStatus.MERGING)
    )
    await db.commit()
    if (claimed.rowcount or 0) == 0:
        raise Conflict("该上传正在合并或已完成", code="INVALID_STATE_TRANSITION")

    dest: Path | None = None
    drawing_id = uuid.uuid4()
    try:
        # 1) 分片完整性（以磁盘为准，不信 DB 计数）
        have = await asyncio.to_thread(storage.list_chunks, upload_id)
        missing = [i for i in range(session.chunk_total) if i not in have]
        if missing:
            raise _reject(
                "ERR_CHUNK_MISSING",
                f"分片不完整，缺少 {len(missing)} 个",
                {"missing_indexes": missing[:10], "chunk_total": session.chunk_total},
            )

        # 2) 流式合并 + 增量哈希
        dest = storage.final_path(session.project_id, drawing_id, session.file_type.value)
        size, sha256_hex, md5_hex = await asyncio.to_thread(
            storage.merge_chunks, upload_id, session.chunk_total, dest
        )

        # 3) 大小核对（防"声明 1MB 实际传 90MB"）
        if size != session.total_size:
            raise _reject(
                "ERR_FILE_CORRUPTED",
                "文件大小与声明不一致",
                {"declared": session.total_size, "actual": size},
            )

        # 4) 魔数校验（防"改名伪造"）
        detected = await asyncio.to_thread(storage.detect_file_type, dest)
        if detected != session.file_type.value:
            raise _reject(
                "ERR_FILE_CORRUPTED",
                "文件内容与格式不符（魔数校验失败）",
                {"declared": session.file_type.value, "detected": detected},
            )

        # 5) MD5 校验（可选，客户端传了才比）
        effective_md5 = declared_md5 or session.md5
        if effective_md5 and effective_md5.lower() != md5_hex:
            raise _reject(
                "ERR_MD5_MISMATCH",
                "文件校验失败，请重新上传",
                {"declared": effective_md5, "actual": md5_hex},
            )

        # 6) 落库：图纸 + 计数 + 会话状态，同一事务
        drawing = Drawing(
            id=drawing_id,
            project_id=session.project_id,
            name=Path(session.file_name).stem,
            file_name=session.file_name,
            file_type=session.file_type,
            file_size=size,
            storage_key=storage.storage_key(session.project_id, drawing_id, session.file_type.value),
            checksum=sha256_hex,
            version=1,
            is_current=True,
            status=DrawingStatus.UPLOADED,
            uploaded_by=user.id,
            is_deleted=False,
        )
        db.add(drawing)
        await db.execute(
            update(Project)
            .where(Project.id == session.project_id)
            .values(drawing_count=Project.drawing_count + 1)
        )
        await db.execute(
            update(UploadSession)
            .where(UploadSession.id == upload_id)
            .values(status=UploadSessionStatus.COMPLETED, uploaded_chunks=session.chunk_total)
        )
        await db.commit()

    except BaseException:
        # 任何失败都要：清掉半成品文件 + 会话置 FAILED（让前端能明确重开一次）
        if dest is not None:
            await asyncio.to_thread(storage._safe_unlink, dest)
        await db.rollback()
        try:
            await db.execute(
                update(UploadSession)
                .where(UploadSession.id == upload_id)
                .values(status=UploadSessionStatus.FAILED)
            )
            await db.commit()
        except Exception:  # pragma: no cover - 兜底
            log.exception("failed to mark upload session %s as FAILED", upload_id)
        raise

    # 成功后才清临时目录；清理失败不影响结果（复盘 2.4）
    await asyncio.to_thread(storage.remove_chunk_dir, upload_id)

    from app.services import drawing_service

    return await drawing_service.get_drawing(db, drawing_id, user)


# ------------------------------------------------------------------ 签名 URL
async def issue_view_url(db: AsyncSession, drawing_id: uuid.UUID, user: User) -> dict:
    drawing = (
        await db.execute(
            select(Drawing).where(Drawing.id == drawing_id, Drawing.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if drawing is None:
        raise NotFound("图纸不存在或已删除")

    await ensure_project_member(db, drawing.project_id, user)

    path = storage.resolve_key(drawing.storage_key)
    if not path.exists():
        raise ValidationFailed("图纸文件缺失，请联系管理员重新上传", detail={"drawing_id": str(drawing_id)})

    from app.core.file_token import sign_file_token

    token, expires_at = sign_file_token(drawing_id)
    return {
        "url": f"{settings.API_V1_PREFIX}/files/{token}",
        "expires_at": expires_at,
        "file_type": drawing.file_type,
    }

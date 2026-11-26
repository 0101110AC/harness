"""上传会话模型（prd-07 §6.5）"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.models.enums import UploadSessionStatus
from app.schemas.common import APIModel

MAX_SIZES = {"pdf": 100 * 1024 * 1024, "dxf": 50 * 1024 * 1024}


class UploadSessionCreateRequest(APIModel):
    project_id: uuid.UUID
    file_name: str = Field(max_length=200)
    file_type: Literal["pdf", "dxf"]
    total_size: int = Field(gt=0)
    md5: str | None = Field(default=None, min_length=32, max_length=32)

    @field_validator("file_name")
    @classmethod
    def no_path_traversal(cls, v: str) -> str:
        # 只取文件名，杜绝 "../../etc/passwd" 这类输入参与路径拼接
        import os

        name = os.path.basename(v.replace("\\", "/"))
        if not name or name in {".", ".."}:
            raise ValueError("文件名不合法")
        return name

    @field_validator("md5")
    @classmethod
    def md5_hex(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.lower()
        if any(c not in "0123456789abcdef" for c in v):
            raise ValueError("md5 必须是 32 位十六进制")
        return v

    @field_validator("total_size")
    @classmethod
    def size_limit(cls, v: int, info) -> int:
        ft = info.data.get("file_type")
        if ft is None:
            return v
        limit = MAX_SIZES[ft]
        if v > limit:
            raise ValueError(f"{ft.upper()} 文件不能超过 {limit // 1024 // 1024}MB")
        return v


class UploadSessionOut(APIModel):
    upload_id: uuid.UUID
    chunk_size: int
    chunk_total: int
    expires_at: datetime
    status: UploadSessionStatus


class ChunkUploadResponse(APIModel):
    upload_id: uuid.UUID
    received: int
    chunk_total: int


class UploadCompleteRequest(APIModel):
    md5: str | None = Field(default=None, min_length=32, max_length=32)

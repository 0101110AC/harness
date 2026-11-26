"""图纸模型（prd-07 §6.5）+ 坐标契约（prd-07 §4）"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import DrawingStatus, FileType
from app.schemas.auth import UserBrief
from app.schemas.common import APIModel


# ---------------- 坐标契约（唯一结构，AI 问题/人工批注/消息跳转共用）----------------
class BBox(BaseModel):
    x: float
    y: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)


class PageSize(BaseModel):
    w: float = Field(gt=0)
    h: float = Field(gt=0)


class DxfMeta(BaseModel):
    layout: str = "Model"
    entities: list[str] = []
    layers: list[str] = []


class Location(BaseModel):
    file_type: Literal["pdf", "dxf"]
    page_no: int = Field(ge=1)
    bbox: BBox
    unit: Literal["pt", "mm"]
    coord_system: Literal["pdf_user_space", "dxf_wcs"]
    origin: Literal["top-left", "bottom-left"]
    page_size: PageSize
    dxf: DxfMeta | None = None

    @model_validator(mode="after")
    def check_consistent(self) -> "Location":
        if self.file_type == "dxf":
            if self.unit != "mm" or self.origin != "bottom-left":
                raise ValueError("DXF 坐标必须 unit=mm、origin=bottom-left")
            if self.dxf is None:
                raise ValueError("DXF 批注必须提供 dxf 元信息")
        return self


def clip_location(loc: Location) -> tuple[Location, bool]:
    """把 bbox 裁剪进 page_size；返回 (裁剪后坐标, 是否原本合法)。

    前端缩放抖动会算出越界坐标，后端一律裁剪而不是拒收（prd-07 §4 校验规则 1）。
    """
    ps, bb = loc.page_size, loc.bbox
    x1 = min(max(bb.x, 0.0), ps.w)
    y1 = min(max(bb.y, 0.0), ps.h)
    x2 = min(max(bb.x + bb.w, 0.0), ps.w)
    y2 = min(max(bb.y + bb.h, 0.0), ps.h)

    clipped = BBox(x=x1, y=y1, w=max(x2 - x1, 0.1), h=max(y2 - y1, 0.1))
    valid = (clipped.x == bb.x and clipped.y == bb.y
             and abs(clipped.w - bb.w) < 1e-6 and abs(clipped.h - bb.h) < 1e-6)
    return loc.model_copy(update={"bbox": clipped}), valid


# ---------------- 图纸 ----------------
class ParseMetaOut(APIModel):
    page_count: int
    pages: list[PageSize] = []
    title_block: dict | None = None
    extents: dict | None = None
    layers: list[str] = []
    units: str | None = None


class DrawingOut(APIModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    file_name: str
    file_type: FileType
    file_size: int
    version: int
    parent_drawing_id: uuid.UUID | None = None
    is_current: bool
    status: DrawingStatus
    status_text: str
    page_count: int | None = None
    issue_stat: dict | None = None
    parse_error: str | None = None
    uploaded_by: UserBrief
    created_at: datetime
    updated_at: datetime


class ViewUrlResponse(APIModel):
    url: str
    expires_at: datetime
    file_type: FileType

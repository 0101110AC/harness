"""公共响应模型（prd-07 §1.2 / §6.1）"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ErrorBody(APIModel):
    code: str
    message: str
    detail: dict | None = None
    trace_id: str


class Page(APIModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


class MessageOut(APIModel):
    message: str

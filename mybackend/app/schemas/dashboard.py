"""工作台（prd-01 §6.1、prd-07 §6.3）"""
from __future__ import annotations

from app.schemas.common import APIModel


class DashboardOverview(APIModel):
    pending_drawings: int = 0
    pending_drawings_in_review: int = 0
    pending_annotations: int = 0
    pending_annotations_to_review: int = 0
    confirmed_issues_this_week: int = 0
    confirmed_issues_last_week: int = 0
    wow_change: float | None = None
    ai_block_rate_this_week: float = 0.0
    pending_hitl: int = 0

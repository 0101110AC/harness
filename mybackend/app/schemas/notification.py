"""站内信模型（prd-07 §6.8）"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.models.enums import BizType, NotificationChannel, NotificationType
from app.schemas.common import APIModel


class NotificationOut(APIModel):
    id: uuid.UUID
    type: NotificationType
    type_text: str
    title: str
    content: str
    biz_type: BizType
    biz_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    deeplink: str | None = None
    is_read: bool
    read_at: datetime | None = None
    created_at: datetime
    # 合并条数（>1 表示这条消息代表 N 条事件，prd-04 §4）
    merge_count: int = 1


class UnreadCountResponse(APIModel):
    count: int
    # 契约允许客户端据此判断"要不要显示 0"：失败/降级时不显示数字
    capped: bool = Field(default=False, description="count ≥ 100 时为 true（前端显示 99+）")


class ReadAllResponse(APIModel):
    updated: int


class ChannelPreference(APIModel):
    channel: NotificationChannel
    enabled: bool
    available: bool = Field(description="V1.0 仅 INAPP 为 true")
    target_masked: str | None = None
    event_types: list[str] = []
    unavailable_reason: str | None = None


class PreferencesOut(APIModel):
    channels: list[ChannelPreference]


class PreferencesUpdateRequest(APIModel):
    event_types: list[str] | None = Field(
        default=None, description="V1.0 仅允许改此项；channel/INAPP 不可关闭"
    )

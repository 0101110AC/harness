"""认证相关模型（prd-07 §6.2）"""
from __future__ import annotations

import uuid

from pydantic import EmailStr, Field, field_validator

from app.schemas.common import APIModel


class RegisterRequest(APIModel):
    name: str = Field(min_length=2, max_length=32)
    email: EmailStr
    password: str = Field(min_length=8, max_length=64)

    @field_validator("password")
    @classmethod
    def need_letter_and_digit(cls, v: str) -> str:
        if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
            raise ValueError("密码必须同时包含字母与数字")
        return v


class LoginRequest(APIModel):
    email: EmailStr
    password: str
    remember_me: bool = True


class RefreshRequest(APIModel):
    refresh_token: str


class TokenResponse(APIModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="access 有效期，秒")


class MenuItem(APIModel):
    key: str
    title: str
    path: str | None = None
    icon: str | None = None
    permission: str | None = None
    children: list["MenuItem"] = []


class UserBrief(APIModel):
    id: uuid.UUID
    name: str
    email: EmailStr
    avatar_url: str | None = None


class UserMeResponse(UserBrief):
    roles: list[str]
    permissions: list[str]
    menus: list[MenuItem]

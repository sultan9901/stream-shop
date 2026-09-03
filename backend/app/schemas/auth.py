"""Request/response schemas for authentication endpoints."""
from __future__ import annotations

from pydantic import BaseModel, Field


class StaffLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    device_id: str | None = Field(default=None, max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=6, max_length=256)


class DevGoogleLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str | None = Field(default=None, max_length=160)


class MessageOut(BaseModel):
    ok: bool = True
    message: str | None = None


class LoginOut(BaseModel):
    ok: bool = True
    role: str
    redirect: str
    must_change_password: bool = False
    csrf_token: str

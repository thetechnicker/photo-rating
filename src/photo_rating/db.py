"""
PhotoRank – SQLite persistence layer

Tables
------
host_users   – named host accounts (admin-managed)
sessions     – one row per game session
images       – one row per image in a session
votes        – one row per vote cast

All write helpers are synchronous and called from FastAPI route handlers.
Heavy queries are not expected at this scale (10s–100s of rows), so no
async SQLite driver is needed.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pwdlib import PasswordHash
from sqlmodel import Field, SQLModel, create_engine

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH: Path = Path(os.environ.get("PHOTORANK_DB", "photorank.db"))

_pwd_ctx = PasswordHash.recommended()
_engine = create_engine(f"sqlite:///{DB_PATH.as_posix()}")


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


class HostUsers(SQLModel):
    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(nullable=False, unique=True)
    pwd_hash: str = Field(nullable=False)
    admin: bool = Field(default=False)
    created_at: datetime


class Sessions(SQLModel):
    code: str = Field(primary_key=True)
    created_by: int = Field(foreign_key="hostusers.id")
    created_at: datetime


HostUsers.metadata.create_all(_engine)

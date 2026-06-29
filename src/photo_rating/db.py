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
from typing import ClassVar

from pwdlib import PasswordHash
from sqlmodel import Field, Relationship
from sqlmodel import Session as SQLSession
from sqlmodel import SQLModel, col, create_engine, select

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH: Path = Path(os.environ.get("PHOTORANK_DB", "photorank.db"))

_pwd_ctx = PasswordHash.recommended()
_engine = create_engine(f"sqlite:///{DB_PATH.as_posix()}", echo=False)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def get_session() -> SQLSession:
    """Return a new SQLModel session (caller must close/commit)."""
    return SQLSession(_engine)


def init_db() -> None:
    """Create all tables if they don't exist."""
    SQLModel.metadata.create_all(_engine)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HostUsers(SQLModel, table=True):
    __tablename__: ClassVar[str] = "host_users"  # type: ignore

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(nullable=False, unique=True)
    pwd_hash: str = Field(nullable=False)
    admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.now)

    sessions: list["Sessions"] = Relationship(back_populates="creator")


class Sessions(SQLModel, table=True):
    __tablename__: ClassVar[str] = "sessions"  # type: ignore

    code: str = Field(primary_key=True)
    created_by: int = Field(foreign_key="host_users.id")
    mode: str = Field(nullable=False)
    threshold: float = Field(nullable=False)
    max_per_user_mb: float = Field(nullable=False)
    max_session_mb: float = Field(nullable=False)
    host_image_dir: str | None = Field(default=None)
    auto_advance_lobby_s: float | None = Field(default=None)
    auto_advance_game_s: float | None = Field(default=None)
    state: str = Field(default="idle")
    created_at: datetime = Field(default_factory=datetime.now)
    finished_at: datetime | None = Field(default=None)

    creator: HostUsers = Relationship(back_populates="sessions")
    images: list["Images"] = Relationship(back_populates="session")
    votes: list["Votes"] = Relationship(back_populates="session")


class Images(SQLModel, table=True):
    __tablename__: ClassVar[str] = "images"  # type: ignore

    id: int | None = Field(default=None, primary_key=True)
    session_code: str = Field(foreign_key="sessions.code", nullable=False)
    filename: str = Field(nullable=False)
    filepath: str = Field(nullable=False)
    uploaded_by: str | None = Field(default=None)
    uploaded_at: datetime = Field(default_factory=datetime.now)
    skipped: bool = Field(default=False)

    session: Sessions = Relationship(back_populates="images")
    votes: list["Votes"] = Relationship(back_populates="image")


class a:
    x: ClassVar[str]


class b:
    x = "test"


class Votes(SQLModel, table=True):
    __tablename__: ClassVar[str] = "votes"  # type: ignore

    id: int | None = Field(default=None, primary_key=True)
    session_code: str = Field(foreign_key="sessions.code", nullable=False)
    image_id: int = Field(foreign_key="images.id", nullable=False)
    player: str = Field(nullable=False)
    payload: str = Field(nullable=False)  # JSON string
    created_at: datetime = Field(default_factory=datetime.now)

    session: Sessions = Relationship(back_populates="votes")
    image: Images = Relationship(back_populates="votes")


# ---------------------------------------------------------------------------
# Host user helpers
# ---------------------------------------------------------------------------


def create_host_user(username: str, password: str, admin: bool = False) -> HostUsers:
    """Create a new host user with hashed password."""
    with get_session() as session:
        existing = session.exec(
            select(HostUsers).where(HostUsers.username == username)
        ).first()
        if existing:
            raise ValueError(f"User '{username}' already exists")
        pwd_hash = _pwd_ctx.hash(password)
        user = HostUsers(username=username, pwd_hash=pwd_hash, admin=admin)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def verify_host_user(username: str, password: str) -> HostUsers | None:
    """Verify credentials and return user if valid."""
    with get_session() as session:
        user = session.exec(
            select(HostUsers).where(HostUsers.username == username)
        ).first()
        if user and _pwd_ctx.verify(password, user.pwd_hash):
            return user
        return None


def get_host_user(user_id: int) -> HostUsers | None:
    with get_session() as session:
        return session.get(HostUsers, user_id)


def list_host_users() -> list[HostUsers]:
    with get_session() as session:
        return list(session.exec(select(HostUsers)))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def create_session_db(
    code: str,
    created_by: int,
    mode: str,
    threshold: float,
    max_per_user_mb: float,
    max_session_mb: float,
    host_image_dir: str | None,
    auto_advance_lobby_s: float | None,
    auto_advance_game_s: float | None,
) -> Sessions:
    """Create a new session in the database."""
    with get_session() as session:
        sess = Sessions(
            code=code,
            created_by=created_by,
            mode=mode,
            threshold=threshold,
            max_per_user_mb=max_per_user_mb,
            max_session_mb=max_session_mb,
            host_image_dir=host_image_dir,
            auto_advance_lobby_s=auto_advance_lobby_s,
            auto_advance_game_s=auto_advance_game_s,
            state="lobby",
        )
        session.add(sess)
        session.commit()
        session.refresh(sess)
        return sess


def get_session_db(code: str) -> Sessions | None:
    with get_session() as session:
        return session.get(Sessions, code.upper())


def update_session_state(code: str, state: str) -> Sessions | None:
    with get_session() as session:
        sess = session.get(Sessions, code.upper())
        if sess:
            sess.state = state
            if state == "end":
                sess.finished_at = datetime.now()
            session.add(sess)
            session.commit()
            session.refresh(sess)
        return sess


def update_session_config(
    code: str,
    auto_advance_lobby_s: float | None = None,
    auto_advance_game_s: float | None = None,
    skipped_images: list[str] | None = None,
) -> Sessions | None:
    with get_session() as session:
        sess = session.get(Sessions, code.upper())
        if not sess:
            return None
        if auto_advance_lobby_s is not None:
            sess.auto_advance_lobby_s = auto_advance_lobby_s
        if auto_advance_game_s is not None:
            sess.auto_advance_game_s = auto_advance_game_s
        if skipped_images is not None:
            # Update skipped flag on images
            for img in sess.images:
                img.skipped = img.filename in skipped_images
                session.add(img)
        session.add(sess)
        session.commit()
        session.refresh(sess)
        return sess


def list_sessions() -> list[Sessions]:
    with get_session() as session:
        return list(
            session.exec(select(Sessions).order_by(col(Sessions.created_at).desc()))
        )


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def add_image(
    session_code: str,
    filename: str,
    filepath: str,
    uploaded_by: str | None = None,
) -> Images:
    with get_session() as session:
        img = Images(
            session_code=session_code.upper(),
            filename=filename,
            filepath=filepath,
            uploaded_by=uploaded_by,
        )
        session.add(img)
        session.commit()
        session.refresh(img)
        return img


def get_images(session_code: str, include_skipped: bool = False) -> list[Images]:
    with get_session() as session:
        stmt = select(Images).where(Images.session_code == session_code.upper())
        if not include_skipped:
            stmt = stmt.where(Images.skipped == False)  # noqa: E712
        return list(session.exec(stmt))


def get_image_by_filename(session_code: str, filename: str) -> Images | None:
    with get_session() as session:
        return session.exec(
            select(Images).where(
                Images.session_code == session_code.upper(),
                Images.filename == filename,
            )
        ).first()


def set_image_skipped(session_code: str, filename: str, skipped: bool) -> Images | None:
    with get_session() as session:
        img = session.exec(
            select(Images).where(
                Images.session_code == session_code.upper(),
                Images.filename == filename,
            )
        ).first()
        if img:
            img.skipped = skipped
            session.add(img)
            session.commit()
            session.refresh(img)
        return img


# ---------------------------------------------------------------------------
# Vote helpers
# ---------------------------------------------------------------------------


def add_vote(
    session_code: str,
    image_id: int,
    player: str,
    payload: str,
) -> Votes:
    with get_session() as session:
        vote = Votes(
            session_code=session_code.upper(),
            image_id=image_id,
            player=player,
            payload=payload,
        )
        session.add(vote)
        session.commit()
        session.refresh(vote)
        return vote


def get_votes_for_image(image_id: int) -> list[Votes]:
    with get_session() as session:
        return list(session.exec(select(Votes).where(Votes.image_id == image_id)))


def get_votes_for_session(session_code: str) -> list[Votes]:
    with get_session() as session:
        return list(
            session.exec(
                select(Votes).where(Votes.session_code == session_code.upper())
            )
        )


def has_voted(session_code: str, image_id: int, player: str) -> bool:
    with get_session() as session:
        vote = session.exec(
            select(Votes).where(
                Votes.session_code == session_code.upper(),
                Votes.image_id == image_id,
                Votes.player == player,
            )
        ).first()
        return vote is not None


# Initialize tables on import
init_db()

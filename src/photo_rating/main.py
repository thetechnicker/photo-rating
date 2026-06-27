"""
PhotoRank – FastAPI backend
State machine: IDLE → LOBBY → GAME → END
"""

from __future__ import annotations

import random
import shutil
import string
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from .plugins import PLUGIN_REGISTRY
from .plugins.base import GamePlugin, PluginState

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

root = Path(__file__).parent
UPLOAD_ROOT = Path("uploads")
STATIC_ROOT = root / "static"
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# ---------------------------------------------------------------------------
# Internal state (dataclasses — not exposed directly to API)
# ---------------------------------------------------------------------------


class SessionState(str, Enum):
    IDLE = "idle"
    LOBBY = "lobby"
    GAME = "game"
    END = "end"


@dataclass
class Player:
    nickname: str
    bytes_uploaded: int = 0


@dataclass
class Session:
    code: str
    state: SessionState
    mode: str
    threshold: float
    max_per_user_mb: float
    max_session_mb: float
    host_image_dir: Path | None  # pre-existing images from host laptop
    upload_dir: Path  # images uploaded during lobby
    players: dict[str, Player] = field(default_factory=dict[str, Player])
    images: list[str] = field(default_factory=list[str])  # absolute paths
    plugin: GamePlugin | None = None

    @property
    def total_bytes_uploaded(self) -> int:
        return sum(p.bytes_uploaded for p in self.players.values())


# In-memory store  {code: Session}
_sessions: dict[str, Session] = {}

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    mode: str = Field(..., description="Plugin key: 'star_rating' or 'head_to_head'")
    threshold: float = Field(
        ..., gt=0, description="Win threshold (avg stars or win-rate)"
    )
    max_per_user_mb: float = Field(50.0, gt=0)
    max_session_mb: float = Field(500.0, gt=0)
    host_image_dir: str | None = Field(None, description="Absolute path on host laptop")

    @field_validator("mode")
    @classmethod
    def mode_must_exist(cls, v: str) -> str:
        if v not in PLUGIN_REGISTRY:
            raise ValueError(f"Unknown mode '{v}'. Available: {list(PLUGIN_REGISTRY)}")
        return v

    @model_validator(mode="after")
    def threshold_range(self) -> "CreateSessionRequest":
        if self.mode == "star_rating" and not (1.0 <= self.threshold <= 5.0):
            raise ValueError("star_rating threshold must be between 1.0 and 5.0")
        if self.mode == "head_to_head" and not (0.0 < self.threshold <= 1.0):
            raise ValueError("head_to_head threshold must be between 0.0 and 1.0")
        return self


class JoinRequest(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=32)


class VoteRequest(BaseModel):
    nickname: str
    payload: dict[str, object]  # interpreted by the active plugin


# --- Responses ---


class CreateSessionResponse(BaseModel):
    code: str
    mode: str
    state: SessionState


class JoinResponse(BaseModel):
    code: str
    nickname: str
    state: SessionState


class SessionStateResponse(BaseModel):
    code: str
    state: SessionState
    players: list[str]
    plugin_state: dict[str, object] | None = None


class FinishResponse(BaseModel):
    code: str
    winners: list[str]
    output_dir: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_code(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def _get_session(code: str) -> Session:
    session = _sessions.get(code.upper())
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _require_state(session: Session, *allowed: SessionState) -> None:
    if session.state not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Action not allowed in state '{session.state}'. Expected: {[s.value for s in allowed]}",
        )


def _collect_images(session: Session) -> list[str]:
    """Gather all image paths: host folder + uploaded lobby images."""
    paths: list[str] = []
    if session.host_image_dir and session.host_image_dir.exists():
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
            paths.extend(str(p) for p in session.host_image_dir.glob(ext))
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
        paths.extend(str(p) for p in session.upload_dir.glob(ext))
    return paths


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Photo-Rating", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_ROOT), html=True), name="static")


# ---------------------------------------------------------------------------
# Session lifecycle endpoints
# ---------------------------------------------------------------------------


@app.post("/session/create", response_model=CreateSessionResponse, status_code=201)
def create_session(body: CreateSessionRequest):
    """Host creates a new session. Server moves IDLE → LOBBY."""
    code = _make_code()
    while code in _sessions:
        code = _make_code()

    upload_dir = UPLOAD_ROOT / code
    upload_dir.mkdir(parents=True, exist_ok=True)

    host_dir = Path(body.host_image_dir) if body.host_image_dir else None
    if host_dir and not host_dir.exists():
        raise HTTPException(
            status_code=400, detail=f"Host image dir not found: {host_dir}"
        )

    session = Session(
        code=code,
        state=SessionState.LOBBY,
        mode=body.mode,
        threshold=body.threshold,
        max_per_user_mb=body.max_per_user_mb,
        max_session_mb=body.max_session_mb,
        host_image_dir=host_dir,
        upload_dir=upload_dir,
    )
    _sessions[code] = session
    return CreateSessionResponse(code=code, mode=session.mode, state=session.state)


@app.post("/session/{code}/join", response_model=JoinResponse)
def join_session(code: str, body: JoinRequest):
    """Participant joins the lobby with a nickname."""
    session = _get_session(code)
    _require_state(session, SessionState.LOBBY)

    nick = body.nickname.strip()
    if nick in session.players:
        raise HTTPException(status_code=409, detail="Nickname already taken")

    session.players[nick] = Player(nickname=nick)
    return JoinResponse(code=session.code, nickname=nick, state=session.state)


@app.post("/session/{code}/upload")
async def upload_image(code: str, nickname: str, file: UploadFile = File(...)):
    """Participant uploads an image during the lobby phase."""
    session = _get_session(code)
    _require_state(session, SessionState.LOBBY)

    if nickname not in session.players:
        raise HTTPException(status_code=403, detail="Join the session before uploading")
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=415, detail=f"Unsupported file type: {file.content_type}"
        )

    data = await file.read()
    size = len(data)

    player = session.players[nickname]
    per_user_limit = int(session.max_per_user_mb * 1024 * 1024)
    session_limit = int(session.max_session_mb * 1024 * 1024)

    if player.bytes_uploaded + size > per_user_limit:
        raise HTTPException(status_code=413, detail="Per-user upload limit exceeded")
    if session.total_bytes_uploaded + size > session_limit:
        raise HTTPException(status_code=413, detail="Session upload limit exceeded")

    dest = session.upload_dir / f"{nickname}_{file.filename}"
    dest.write_bytes(data)
    player.bytes_uploaded += size

    return {"filename": dest.name, "size_bytes": size}


@app.post("/session/{code}/start", response_model=SessionStateResponse)
def start_game(code: str):
    """Host starts the game. LOBBY → GAME. Plugin is initialised here."""
    session = _get_session(code)
    _require_state(session, SessionState.LOBBY)

    images = _collect_images(session)
    if not images:
        raise HTTPException(
            status_code=400, detail="No images found. Upload some first."
        )

    plugin_cls = PLUGIN_REGISTRY[session.mode]
    plugin = plugin_cls()
    session.plugin = plugin
    plugin_state = plugin.start(images)
    session.images = images
    session.state = SessionState.GAME

    return _build_state_response(session, plugin_state)


@app.post("/session/{code}/next", response_model=SessionStateResponse)
def next_image(code: str):
    """Host advances to the next image/pair."""
    session = _get_session(code)
    _require_state(session, SessionState.GAME)
    assert session.plugin is not None, "Plugin not started"

    plugin_state = session.plugin.next_image()
    return _build_state_response(session, plugin_state)


@app.post("/session/{code}/vote")
def vote(code: str, body: VoteRequest):
    """Participant submits a vote for the current image/pair."""
    session = _get_session(code)
    _require_state(session, SessionState.GAME)
    assert session.plugin is not None, "Plugin not started"

    if body.nickname not in session.players:
        raise HTTPException(status_code=403, detail="Not a participant of this session")

    try:
        session.plugin.vote(body.nickname, body.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"ok": True}


@app.post("/session/{code}/finish", response_model=FinishResponse)
def finish_session(code: str):
    """Host ends the game. GAME → END. Winners are copied to output folder."""
    session = _get_session(code)
    _require_state(session, SessionState.GAME)
    assert session.plugin is not None, "Plugin not started"

    winners = session.plugin.get_winners(session.threshold)
    output_dir = session.upload_dir / "output"
    output_dir.mkdir(exist_ok=True)

    for filepath in winners:
        src = Path(filepath)
        if src.exists():
            shutil.copy2(src, output_dir / src.name)

    session.state = SessionState.END
    return FinishResponse(
        code=session.code,
        winners=[Path(w).name for w in winners],
        output_dir=str(output_dir),
    )


# ---------------------------------------------------------------------------
# Polling endpoint (used by all clients)
# ---------------------------------------------------------------------------


@app.get("/session/{code}/state", response_model=SessionStateResponse)
def get_state(code: str):
    session = _get_session(code)
    plugin_state = session.plugin.get_state() if session.plugin else None
    return _build_state_response(session, plugin_state)


# ---------------------------------------------------------------------------
# Image serving
# ---------------------------------------------------------------------------


@app.get("/images/{code}/{filename}")
def serve_image(code: str, filename: str):
    session = _get_session(code)
    candidates = [session.upload_dir / filename]
    if session.host_image_dir:
        candidates.append(session.host_image_dir / filename)

    for path in candidates:
        if path.exists():
            return FileResponse(str(path))

    raise HTTPException(status_code=404, detail="Image not found")


# ---------------------------------------------------------------------------
# Internal builder
# ---------------------------------------------------------------------------


def _build_state_response(session: Session, plugin_state: PluginState | None = None) -> SessionStateResponse:
    ps_dict: dict[str, object] | None = None
    if plugin_state:
        ps_dict = {
            "current_image": plugin_state.current_image,
            "image_index": plugin_state.image_index,
            "total_images": plugin_state.total_images,
            **plugin_state.extra,
        }
    return SessionStateResponse(
        code=session.code,
        state=session.state,
        players=list(session.players.keys()),
        plugin_state=ps_dict,
    )


# ---------------------------------------------------------------------------
# Run:  uvicorn main:app --reload
# ---------------------------------------------------------------------------

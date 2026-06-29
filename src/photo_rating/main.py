"""
PhotoRank – FastAPI backend
State machine: IDLE → LOBBY → GAME → END
"""

from __future__ import annotations

import asyncio
import os
import random
import secrets
import shutil
import string
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from fastapi import Cookie, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from .plugins import PLUGIN_REGISTRY
from .plugins.base import GamePlugin, PluginState

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

root_path = Path(__file__).parent
UPLOAD_ROOT = Path("uploads")
STATIC_ROOT = root_path / "static"
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# ---------------------------------------------------------------------------
# Auth config
# ---------------------------------------------------------------------------

# Set PHOTORANK_HOST_PASSWORD env var to change; defaults to "admin"
_HOST_PASSWORD: str = os.environ.get("PHOTORANK_HOST_PASSWORD", "admin")

# In-memory set of valid auth tokens (cleared on restart)
_auth_tokens: set[str] = set()

AUTH_COOKIE = "pr_host_token"
COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours


def _make_token() -> str:
    return secrets.token_hex(32)


def _is_authenticated(token: str | None) -> bool:
    return bool(token and token in _auth_tokens)


# ---------------------------------------------------------------------------
# Internal state
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
    host_image_dir: Path | None
    upload_dir: Path
    # Timer config (seconds); None = manual/disabled
    auto_advance_lobby_s: float | None = None
    auto_advance_game_s: float | None = None
    # Images to exclude from the game
    skipped_images: set[str] = field(default_factory=set[str])
    players: dict[str, Player] = field(default_factory=dict[str, Player])
    images: list[str] = field(default_factory=list[str])
    plugin: GamePlugin | None = None
    # Background auto-advance task
    advance_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # Timestamp (monotonic) when the current image was shown, for countdown
    image_shown_at: float | None = field(default=None, repr=False)

    @property
    def total_bytes_uploaded(self) -> int:
        return sum(p.bytes_uploaded for p in self.players.values())

    @property
    def time_remaining_s(self) -> float | None:
        """Seconds until auto-advance fires, or None if not applicable."""
        if self.auto_advance_game_s is None or self.image_shown_at is None:
            return None
        import time

        elapsed = time.monotonic() - self.image_shown_at
        remaining = self.auto_advance_game_s - elapsed
        return max(0.0, round(remaining, 1))


_sessions: dict[str, Session] = {}

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    mode: str = Field(..., description="Plugin key: 'star_rating' or 'head_to_head'")
    threshold: float = Field(..., gt=0)
    max_per_user_mb: float = Field(50.0, gt=0)
    max_session_mb: float = Field(500.0, gt=0)
    host_image_dir: str | None = Field(None)
    auto_advance_lobby_s: float | None = Field(
        None, gt=0, description="Seconds before auto-start, or null"
    )
    auto_advance_game_s: float | None = Field(
        None, gt=0, description="Seconds per image, or null"
    )

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


class PatchSessionConfigRequest(BaseModel):
    """Partial update: only supplied fields are changed."""

    auto_advance_lobby_s: float | None = Field(
        default=..., description="Pass null to disable"
    )
    auto_advance_game_s: float | None = Field(
        default=..., description="Pass null to disable"
    )
    skipped_images: list[str] | None = Field(
        None, description="Full replacement list of filenames to skip"
    )


class JoinRequest(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=32)


class VoteRequest(BaseModel):
    nickname: str
    payload: dict[str, object]


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
    auto_advance_game_s: float | None = None
    time_remaining_s: float | None = None
    skipped_images: list[str] = []


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
    paths: list[str] = []
    if session.host_image_dir and session.host_image_dir.exists():
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
            paths.extend(str(p) for p in session.host_image_dir.glob(ext))
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
        paths.extend(str(p) for p in session.upload_dir.glob(ext))
    # Exclude skipped images (match by filename)
    skipped = session.skipped_images
    return [p for p in paths if Path(p).name not in skipped]


def _cancel_advance_task(session: Session) -> None:
    if session.advance_task and not session.advance_task.done():
        session.advance_task.cancel()
    session.advance_task = None


# ---------------------------------------------------------------------------
# Auto-advance background task
# ---------------------------------------------------------------------------


async def _auto_advance_game(session: Session) -> None:
    """
    Background task: waits auto_advance_game_s seconds, then calls next_image
    on the plugin and resets the timer. Loops until plugin is finished.
    """
    import time

    delay = session.auto_advance_game_s
    if delay is None:
        return
    try:
        while True:
            session.image_shown_at = time.monotonic()
            await asyncio.sleep(delay)
            if session.state != SessionState.GAME or session.plugin is None:
                break
            if session.plugin.is_finished():
                break
            session.plugin.next_image()
            session.image_shown_at = time.monotonic()
    except asyncio.CancelledError:
        pass


async def _auto_start_lobby(session: Session) -> None:
    """Background task: waits auto_advance_lobby_s then starts the game."""
    delay = session.auto_advance_lobby_s
    if delay is None:
        return
    try:
        await asyncio.sleep(delay)
        if session.state != SessionState.LOBBY:
            return
        images = _collect_images(session)
        if not images:
            return
        plugin_cls = PLUGIN_REGISTRY[session.mode]
        plugin = plugin_cls()
        session.plugin = plugin
        plugin.start(images)
        session.images = images
        session.state = SessionState.GAME
        # Start game auto-advance if configured
        if session.auto_advance_game_s is not None:
            session.advance_task = asyncio.create_task(_auto_advance_game(session))
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Photo-Rating", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(STATIC_ROOT), html=True), name="static")


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@app.get("/")
def root():
    """Root redirects to the user-facing join page."""
    return RedirectResponse(url="/join", status_code=302)


@app.get("/join")
def join_page():
    return FileResponse(str(STATIC_ROOT / "join.html"))


@app.get("/present")
def present_page():
    return FileResponse(str(STATIC_ROOT / "present.html"))


@app.get("/present/{code}")
def present_page_with_code(code: str):
    return FileResponse(str(STATIC_ROOT / "present.html"))


# ── Host login ──────────────────────────────────────────────────────────────


@app.get("/host-login")
def host_login_page():
    return FileResponse(str(STATIC_ROOT / "host-login.html"))


@app.post("/host-login")
def host_login_submit(
    password: str = Form(...),
    next: str = Form(default="/config"),
):
    """Validate password; set auth cookie and redirect to /config on success."""
    if not secrets.compare_digest(password, _HOST_PASSWORD):
        # Re-serve login page with error flag in query string
        return RedirectResponse(url="/host-login?error=1", status_code=302)

    token = _make_token()
    _auth_tokens.add(token)
    response = RedirectResponse(url=next, status_code=302)
    response.set_cookie(
        key=AUTH_COOKIE,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/host-logout")
def host_logout(pr_host_token: str | None = Cookie(default=None)):
    if pr_host_token and pr_host_token in _auth_tokens:
        _auth_tokens.discard(pr_host_token)
    response = RedirectResponse(url="/host-login", status_code=302)
    response.delete_cookie(AUTH_COOKIE)
    return response


# ── Protected host pages ────────────────────────────────────────────────────


# def _auth_guard(token: str | None) -> None:
#    """Raise 401 if not authenticated (API calls). Page routes redirect instead."""
#    if not _is_authenticated(token):
#        raise HTTPException(status_code=401, detail="Not authenticated")


@app.get("/config")
def config_page(pr_host_token: str | None = Cookie(default=None)):
    if not _is_authenticated(pr_host_token):
        return RedirectResponse(url="/host-login?next=/config", status_code=302)
    return FileResponse(str(STATIC_ROOT / "config.html"))


@app.get("/host")
def host_page(pr_host_token: str | None = Cookie(default=None)):
    if not _is_authenticated(pr_host_token):
        return RedirectResponse(url="/host-login?next=/host", status_code=302)
    return FileResponse(str(STATIC_ROOT / "host.html"))


# ---------------------------------------------------------------------------
# Session lifecycle endpoints
# ---------------------------------------------------------------------------


@app.post("/session/create", response_model=CreateSessionResponse, status_code=201)
async def create_session(body: CreateSessionRequest):
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
        auto_advance_lobby_s=body.auto_advance_lobby_s,
        auto_advance_game_s=body.auto_advance_game_s,
    )
    _sessions[code] = session

    # Schedule lobby auto-start if configured
    if body.auto_advance_lobby_s is not None:
        asyncio.create_task(_auto_start_lobby(session))

    return CreateSessionResponse(code=code, mode=session.mode, state=session.state)


@app.patch("/session/{code}/config")
def patch_session_config(code: str, body: PatchSessionConfigRequest):
    """Update timer and skip settings on a live session (any state except END)."""
    session = _get_session(code)
    _require_state(session, SessionState.LOBBY, SessionState.GAME)

    # Update timers
    session.auto_advance_lobby_s = body.auto_advance_lobby_s
    session.auto_advance_game_s = body.auto_advance_game_s

    # Update skipped images
    if body.skipped_images is not None:
        session.skipped_images = set(body.skipped_images)

    # Restart game advance task with new timing if in GAME
    if session.state == SessionState.GAME:
        _cancel_advance_task(session)
        if session.auto_advance_game_s is not None:
            session.advance_task = asyncio.create_task(_auto_advance_game(session))

    return {"ok": True}


@app.post("/session/{code}/join", response_model=JoinResponse)
def join_session(code: str, body: JoinRequest):
    session = _get_session(code)
    _require_state(session, SessionState.LOBBY)

    nick = body.nickname.strip()
    if nick in session.players:
        raise HTTPException(status_code=409, detail="Nickname already taken")

    session.players[nick] = Player(nickname=nick)
    return JoinResponse(code=session.code, nickname=nick, state=session.state)


@app.post("/session/{code}/upload")
async def upload_image(code: str, nickname: str, file: UploadFile = File(...)):
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
async def start_game(code: str):
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

    # Start auto-advance task if configured
    _cancel_advance_task(session)
    if session.auto_advance_game_s is not None:
        session.advance_task = asyncio.create_task(_auto_advance_game(session))

    return _build_state_response(session, plugin_state)


@app.post("/session/{code}/next", response_model=SessionStateResponse)
def next_image(code: str):
    session = _get_session(code)
    _require_state(session, SessionState.GAME)
    assert session.plugin is not None

    # Cancel and restart the auto-advance timer (manual skip resets it)
    _cancel_advance_task(session)
    plugin_state = session.plugin.next_image()

    if session.auto_advance_game_s is not None:
        session.advance_task = asyncio.create_task(_auto_advance_game(session))

    return _build_state_response(session, plugin_state)


@app.post("/session/{code}/vote")
def vote(code: str, body: VoteRequest):
    session = _get_session(code)
    _require_state(session, SessionState.GAME)
    assert session.plugin is not None

    if body.nickname not in session.players:
        raise HTTPException(status_code=403, detail="Not a participant of this session")

    try:
        session.plugin.vote(body.nickname, body.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"ok": True}


@app.post("/session/{code}/finish", response_model=FinishResponse)
def finish_session(code: str):
    session = _get_session(code)
    _require_state(session, SessionState.GAME)
    assert session.plugin is not None

    _cancel_advance_task(session)

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
# Polling endpoint
# ---------------------------------------------------------------------------


@app.get("/session/{code}/state", response_model=SessionStateResponse)
def get_state(code: str):
    session = _get_session(code)
    plugin_state = session.plugin.get_state() if session.plugin else None
    return _build_state_response(session, plugin_state)


# ---------------------------------------------------------------------------
# Image listing (for config skip UI)
# ---------------------------------------------------------------------------


@app.get("/session/{code}/images")
def list_images(code: str):
    """Returns all images in the session (including skipped ones) for the config UI."""
    session = _get_session(code)
    all_images: list[str] = []
    if session.host_image_dir and session.host_image_dir.exists():
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
            all_images.extend(p.name for p in session.host_image_dir.glob(ext))
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
        all_images.extend(p.name for p in session.upload_dir.glob(ext))
    return {
        "images": all_images,
        "skipped": list(session.skipped_images),
    }


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


def _build_state_response(
    session: Session, plugin_state: PluginState | None = None
) -> SessionStateResponse:
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
        auto_advance_game_s=session.auto_advance_game_s,
        time_remaining_s=session.time_remaining_s,
        skipped_images=list(session.skipped_images),
    )

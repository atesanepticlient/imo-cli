"""Authentication API and deliberately plain administrator panel."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

ADMIN_USERNAME = "admin"
# Set IMO_ENFORCE_IP_MATCH=1 to require the same IP as at login. Default off so
# users can validate from any device or network.
ENFORCE_IP = os.getenv("IMO_ENFORCE_IP_MATCH", "0") == "1"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def database_url() -> str:
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required for the control server. See .env.example.")
    # SQLAlchemy's psycopg v3 dialect makes the expected driver explicit.
    return value.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class AccessKey(Base):
    __tablename__ = "imo_access_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    label: Mapped[str] = mapped_column(String(120), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    sessions: Mapped[list["AuthSession"]] = relationship(back_populates="access_key")


class AuthSession(Base):
    __tablename__ = "imo_auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    access_key_id: Mapped[str] = mapped_column(ForeignKey("imo_access_keys.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hint: Mapped[str] = mapped_column(String(14))
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_key: Mapped[AccessKey] = relationship(back_populates="sessions")


class IPBan(Base):
    __tablename__ = "imo_ip_bans"
    ip_address: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(240), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginEvent(Base):
    __tablename__ = "imo_login_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    access_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outcome: Mapped[str] = mapped_column(String(40), index=True)
    detail: Mapped[str] = mapped_column(String(240), default="")


class AdminAccount(Base):
    __tablename__ = "imo_admin_account"
    username: Mapped[str] = mapped_column(String(40), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Hashing helpers
# --------------------------------------------------------------------------

def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


# --------------------------------------------------------------------------
# Database setup
# --------------------------------------------------------------------------

_engine = None
_sessions = None


def get_session_factory():
    global _engine, _sessions
    if _sessions is None:
        _engine = create_engine(database_url(), pool_pre_ping=True)
        _sessions = sessionmaker(_engine, expire_on_commit=False)
    return _sessions


def initialize_database() -> None:
    get_session_factory()
    assert _engine is not None
    Base.metadata.create_all(_engine)
    with get_session_factory()() as db:
        if db.get(AdminAccount, ADMIN_USERNAME):
            return
        # One-time bootstrap only. After this the database is the source of truth.
        initial = os.getenv("IMO_ADMIN_PASSWORD") or secrets.token_urlsafe(16)
        db.add(AdminAccount(username=ADMIN_USERNAME, password_hash=hash_password(initial)))
        db.commit()
        if not os.getenv("IMO_ADMIN_PASSWORD"):
            print(f"[imo-admin] First login -> user: admin  password: {initial}  (change it in the panel)")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


def get_db():
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------
# Request helpers
# --------------------------------------------------------------------------

def _event(db: Session, ip: str, outcome: str, *, access_key_id: str | None = None, session_id: str | None = None, detail: str = "") -> None:
    db.add(LoginEvent(ip_address=ip, outcome=outcome, access_key_id=access_key_id, session_id=session_id, detail=detail))


def client_ip(request: Request) -> str:
    if os.getenv("IMO_TRUST_PROXY_HEADERS") == "1":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def bearer_token(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authentication token")
    return value[7:]


security = HTTPBasic()


def admin_only(credentials: HTTPBasicCredentials = Depends(security), db: Session = Depends(get_db)) -> str:
    account = db.get(AdminAccount, ADMIN_USERNAME)
    user_ok = hmac.compare_digest(credentials.username.encode(), ADMIN_USERNAME.encode())
    pass_ok = bool(account) and verify_password(credentials.password, account.password_hash)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Administrator access required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return ADMIN_USERNAME


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class LoginRequest(BaseModel):
    key: str = Field(min_length=1, max_length=500)


class KeyRequest(BaseModel):
    label: str = Field(default="", max_length=120)
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    max_uses: int = Field(default=1, ge=1, le=10000)


class UserAction(BaseModel):
    scope: Literal["token", "ip"]
    reason: str = Field(default="", max_length=240)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


# --------------------------------------------------------------------------
# App + public auth API (used by the desktop client)
# --------------------------------------------------------------------------

app = FastAPI(title="IMO Control", docs_url=None, redoc_url=None, lifespan=lifespan)
_static = Path(__file__).parent / "web"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login")
def login_api(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    ip = client_ip(request)
    key = db.scalar(select(AccessKey).where(AccessKey.key_hash == _hash(payload.key)))
    now = utcnow()
    invalid = not key or key.revoked_at or (key.expires_at and key.expires_at <= now) or (key and key.uses >= key.max_uses)
    if invalid:
        _event(db, ip, "invalid_key", access_key_id=key.id if key else None)
        db.commit()
        raise HTTPException(status_code=401, detail="The login key is invalid, expired, revoked, or already used.")
    if db.get(IPBan, ip):
        _event(db, ip, "banned", access_key_id=key.id)
        db.commit()
        raise HTTPException(status_code=403, detail="This IP address is suspended.")
    token = secrets.token_urlsafe(48)
    # Assign the UUID before recording the event so the first successful-login
    # history entry is linked to the session as well.
    session = AuthSession(id=str(uuid4()), access_key_id=key.id, token_hash=_hash(token), token_hint=token[-8:], ip_address=ip)
    key.uses += 1
    db.add(session)
    _event(db, ip, "login_success", access_key_id=key.id, session_id=session.id)
    db.commit()
    return {"token": token}


@app.post("/api/auth/validate")
def validate_api(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    token = bearer_token(request)
    ip = client_ip(request)
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _hash(token)))
    if not session:
        _event(db, ip, "invalid_token")
        db.commit()
        raise HTTPException(status_code=401, detail="The authentication token is not recognized.")
    if db.get(IPBan, ip):
        _event(db, ip, "banned", access_key_id=session.access_key_id, session_id=session.id)
        db.commit()
        raise HTTPException(status_code=403, detail="This IP address is suspended.")
    if session.revoked_at or session.access_key.revoked_at:
        _event(db, ip, "revoked", access_key_id=session.access_key_id, session_id=session.id)
        db.commit()
        raise HTTPException(status_code=403, detail="This authentication token has been suspended.")
    if ENFORCE_IP and not hmac.compare_digest(session.ip_address, ip):
        _event(db, ip, "ip_mismatch", access_key_id=session.access_key_id, session_id=session.id, detail=f"expected {session.ip_address}")
        db.commit()
        raise HTTPException(status_code=403, detail="Your IP address does not match the address used at login.")
    session.last_seen_at = utcnow()
    _event(db, ip, "validated", access_key_id=session.access_key_id, session_id=session.id)
    db.commit()
    return {"status": "authorized", "ip_address": ip, "session_id": session.id}


# --------------------------------------------------------------------------
# Admin panel + admin API (HTTP Basic: user "admin", password stored in DB)
# --------------------------------------------------------------------------

@app.get("/admin", dependencies=[Depends(admin_only)])
def admin_panel() -> FileResponse:
    return FileResponse(_static / "admin.html")


@app.get("/admin.css")
def admin_css() -> FileResponse:
    return FileResponse(_static / "admin.css", media_type="text/css")


@app.get("/admin.js")
def admin_js() -> FileResponse:
    return FileResponse(_static / "admin.js", media_type="application/javascript")


@app.get("/api/admin/users", dependencies=[Depends(admin_only)])
def admin_users(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    sessions = db.scalars(select(AuthSession).order_by(AuthSession.last_seen_at.desc())).all()
    return [{
        "id": item.id, "ip_address": item.ip_address, "token_hint": item.token_hint,
        "created_at": item.created_at.isoformat(), "last_seen_at": item.last_seen_at.isoformat(),
        "suspended": bool(item.revoked_at), "ip_banned": bool(db.get(IPBan, item.ip_address)),
        "key_label": item.access_key.label,
    } for item in sessions]


@app.get("/api/admin/history", dependencies=[Depends(admin_only)])
def admin_history(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    rows = db.scalars(select(LoginEvent).order_by(LoginEvent.created_at.desc()).limit(300)).all()
    return [{"created_at": e.created_at.isoformat(), "ip_address": e.ip_address, "outcome": e.outcome, "detail": e.detail, "session_id": e.session_id} for e in rows]


@app.post("/api/admin/keys", dependencies=[Depends(admin_only)])
def create_key(payload: KeyRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    raw = "IMO-" + secrets.token_urlsafe(24)
    expiry = utcnow() + timedelta(days=payload.expires_days) if payload.expires_days else None
    key = AccessKey(label=payload.label, key_hash=_hash(raw), expires_at=expiry, max_uses=payload.max_uses)
    db.add(key)
    db.commit()
    return {"key": raw, "id": key.id, "expires_at": expiry.isoformat() if expiry else None, "max_uses": key.max_uses}


@app.post("/api/admin/users/{session_id}/suspend", dependencies=[Depends(admin_only)])
def suspend_user(session_id: str, payload: UserAction, db: Session = Depends(get_db)) -> dict[str, str]:
    session = db.get(AuthSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="User session not found")
    if payload.scope == "ip":
        if not db.get(IPBan, session.ip_address):
            db.add(IPBan(ip_address=session.ip_address, reason=payload.reason))
    else:
        session.revoked_at = utcnow()
    db.commit()
    return {"status": "suspended", "scope": payload.scope}


@app.post("/api/admin/users/{session_id}/restore", dependencies=[Depends(admin_only)])
def restore_user(session_id: str, payload: UserAction, db: Session = Depends(get_db)) -> dict[str, str]:
    session = db.get(AuthSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="User session not found")
    if payload.scope == "ip":
        ban = db.get(IPBan, session.ip_address)
        if ban:
            db.delete(ban)
    else:
        session.revoked_at = None
    db.commit()
    return {"status": "restored", "scope": payload.scope}


@app.post("/api/admin/password", dependencies=[Depends(admin_only)])
def change_password(payload: PasswordChange, db: Session = Depends(get_db)) -> dict[str, str]:
    account = db.get(AdminAccount, ADMIN_USERNAME)
    if not account or not verify_password(payload.current_password, account.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    account.password_hash = hash_password(payload.new_password)
    account.updated_at = utcnow()
    db.commit()
    return {"status": "updated"}
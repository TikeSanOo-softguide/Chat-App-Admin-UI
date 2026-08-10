"""
Standalone Matrix Identity Service (msisdn-only) backed by SMSPoh's
Verify (OTP) API — not plain SMS sending.

Implements just enough of the Identity Service API for Synapse's
account_threepid_delegates.msisdn to work:

    POST /_matrix/identity/v2/validate/msisdn/requestToken
    POST /_matrix/identity/v2/validate/msisdn/submitToken

Synapse calls these directly (server-to-server) whenever a user tries to
add/verify/reset with a phone number. No Matrix client ever talks to this
service directly.

Design: SMSPoh's /api/otp/request endpoint generates, sends, AND tracks
the OTP itself (returning a requestId). /api/otp/verify checks the code
against that requestId. So this service does NOT generate or store OTP
codes at all — it just proxies, using SMSPoh's requestId as the Matrix
`sid`, and keeps a small local table only to enforce that submitToken's
client_secret matches whoever created the session (per Matrix spec) and
to dedupe repeated requestToken calls with the same send_attempt.

SECURITY: This service will trigger an OTP send (costing you money) to
whatever phone number is POSTed to requestToken, with no authentication
of its own. Do NOT expose its port publicly — only put it on the internal
Docker network that Synapse can reach.
"""

import os
import secrets
import time
import base64
import logging
from typing import Optional

import httpx
import phonenumbers
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("msisdn-identity")

app = FastAPI(title="msisdn-identity-service")

# ---------------------------------------------------------------------------
# Config (env vars)
# ---------------------------------------------------------------------------

SMSPOH_API_KEY = os.environ["SMSPOH_API_KEY"]
SMSPOH_API_SECRET = os.environ["SMSPOH_API_SECRET"]
SMSPOH_OTP_BASE_URL = os.environ.get("SMSPOH_OTP_BASE_URL", "https://v3.smspoh.com/api/otp")
SMSPOH_SENDER_ID = os.environ.get("SMSPOH_SENDER_ID", "SMSPoh Test")  # your approved "from" sender name
BRAND_NAME = os.environ.get("BRAND_NAME", "Teak Chat")  # required by SMSPoh; shown in the OTP text

OTP_LENGTH = int(os.environ.get("OTP_LENGTH", "6"))  # SMSPoh pinLength, 4-8
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "600"))  # SMSPoh ttl, 60-3600
MAX_SUBMIT_ATTEMPTS = int(os.environ.get("MAX_SUBMIT_ATTEMPTS", "5"))  # SMSPoh maxInvalidAttempts, 1-10
MAX_SEND_ATTEMPTS_PER_NUMBER_PER_HOUR = int(os.environ.get("MAX_SEND_PER_HOUR", "5"))

_access_token = base64.b64encode(f"{SMSPOH_API_KEY}:{SMSPOH_API_SECRET}".encode()).decode()

# ---------------------------------------------------------------------------
# In-memory session store.
# Fine for a single-process container. If you ever run this with more than
# one worker/replica, swap this dict for Redis (session lookups need to be
# shared across processes).
# ---------------------------------------------------------------------------

_sessions: dict[str, dict] = {}
_send_history: dict[str, list[float]] = {}  # msisdn -> timestamps, for basic rate limiting


def _prune_expired() -> None:
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["created_at"] > SESSION_TTL_SECONDS]
    for sid in expired:
        _sessions.pop(sid, None)


def _rate_limited(msisdn: str) -> bool:
    now = time.time()
    hist = [t for t in _send_history.get(msisdn, []) if now - t < 3600]
    _send_history[msisdn] = hist
    return len(hist) >= MAX_SEND_ATTEMPTS_PER_NUMBER_PER_HOUR


def _record_send(msisdn: str) -> None:
    _send_history.setdefault(msisdn, []).append(time.time())


def _matrix_error(status: int, errcode: str, error: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errcode": errcode, "error": error})

def _get_active_session(msisdn: str) -> Optional[str]:
    now = time.time()

    for sid, session in _sessions.items():
        if (
            session["msisdn"] == msisdn
            and not session.get("validated")
            and now - session["created_at"] < SESSION_TTL_SECONDS
        ):
            return sid

    return None

# ---------------------------------------------------------------------------
# SMSPoh Verify (OTP) API calls
# ---------------------------------------------------------------------------

class SMSPohError(Exception):
    pass


async def smspoh_request_otp(msisdn_with_plus: str) -> dict:
    """Ask SMSPoh to generate + send an OTP. Returns their response body,
    which includes requestId — we use that as our Matrix `sid`."""
    params = {
        "from": SMSPOH_SENDER_ID,
        "to": msisdn_with_plus,
        "brand": BRAND_NAME,
        "accessToken": _access_token,
        "ttl": SESSION_TTL_SECONDS,
        "pinLength": OTP_LENGTH,
        "template": f"Your {BRAND_NAME} verification code is {{code}}.",
        "maxInvalidAttempts": MAX_SUBMIT_ATTEMPTS,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{SMSPOH_OTP_BASE_URL}/request", params=params)
        if resp.status_code not in (200, 201):
            log.warning("SMSPoh OTP request failed: %s %s", resp.status_code, resp.text)
            raise SMSPohError(f"SMSPoh request failed: {resp.status_code}")
        return resp.json()


async def smspoh_verify_otp(request_id: str, code: str) -> bool:
    """Ask SMSPoh to check `code` against `request_id`. Returns True if valid."""
    params = {
        "requestId": request_id,
        "code": code,
        "accessToken": _access_token,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{SMSPOH_OTP_BASE_URL}/verify", params=params)
        if resp.status_code in (200, 201):
            return True
        if resp.status_code in (400, 401, 403, 404, 409, 410):
            return False
        log.warning("SMSPoh OTP verify unexpected status: %s %s", resp.status_code, resp.text)
        raise SMSPohError(f"SMSPoh verify failed: {resp.status_code}")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class RequestTokenBody(BaseModel):
    client_secret: str
    country: str
    phone_number: str
    send_attempt: int
    next_link: Optional[str] = None


class SubmitTokenBody(BaseModel):
    sid: str
    client_secret: str
    token: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/_matrix/identity/api/v1/validate/msisdn/requestToken")
@app.post("/_matrix/identity/v2/validate/msisdn/requestToken")
async def request_token(body: RequestTokenBody):
    _prune_expired()

    try:
        parsed = phonenumbers.parse(body.phone_number, body.country)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("invalid number")
    except Exception:
        return _matrix_error(400, "M_INVALID_PARAM", "Invalid phone number/country")

    msisdn_e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)  # +959xxxxxxxxx
    msisdn_digits = msisdn_e164.lstrip("+")  # what we return to Synapse per spec (no leading +)

    # If this exact (client_secret, phone_number, send_attempt) combo already has
    # a session, don't ask SMSPoh to resend — return the existing sid (per spec).
    existing_sid = _get_active_session(msisdn_digits)
    if existing_sid:
        _sessions[existing_sid]["client_secret"] = body.client_secret
        _sessions[existing_sid]["send_attempt"] = body.send_attempt

        return {
            "sid": existing_sid,
            "msisdn": msisdn_digits
        }

    if _rate_limited(msisdn_digits):
        return _matrix_error(429, "M_LIMIT_EXCEEDED", "Too many OTP requests for this number")

    try:
        smspoh_resp = await smspoh_request_otp(msisdn_e164)
    except SMSPohError:
        return _matrix_error(500, "M_UNKNOWN", "Failed to send OTP")

    sid = str(smspoh_resp["requestId"])
    _sessions[sid] = {
        "client_secret": body.client_secret,
        "msisdn": msisdn_digits,
        "send_attempt": body.send_attempt,
        "created_at": time.time(),
    }

    _record_send(msisdn_digits)
    return {"sid": sid, "msisdn": msisdn_digits}


@app.post("/_matrix/identity/api/v1/validate/msisdn/submitToken")
@app.post("/_matrix/identity/v2/validate/msisdn/submitToken")
async def submit_token(body: SubmitTokenBody):
    _prune_expired()
    session = _sessions.get(body.sid)

    if session is None:
        return _matrix_error(400, "M_NO_VALID_SESSION", "Unknown or expired session")

    if session["client_secret"] != body.client_secret:
        return _matrix_error(400, "M_NO_VALID_SESSION", "client_secret does not match session")

    try:
        valid = await smspoh_verify_otp(body.sid, body.token)
    except SMSPohError:
        return _matrix_error(500, "M_UNKNOWN", "Failed to verify OTP")

    if valid:
        session["validated"] = True
        session["validated_at"] = time.time()
        return {"success": True}

    return {"success": False}

@app.get("/_matrix/identity/api/v1/3pid/getValidated3pid")
@app.get("/_matrix/identity/v2/3pid/getValidated3pid")
async def get_validated_3pid(
    sid: str = Query(...),
    client_secret: str = Query(...)
):
    _prune_expired()

    session = _sessions.get(sid)

    if session is None:
        return _matrix_error(400, "M_SESSION_NOT_FOUND", "Unknown or expired session")

    if session["client_secret"] != client_secret:
        return _matrix_error(403, "M_FORBIDDEN", "client_secret does not match")

    if not session.get("validated"):
        return _matrix_error(400, "M_SESSION_NOT_VALIDATED", "Session has not been validated")

    return {
        "medium": "msisdn",
        "address": session["msisdn"],
        "validated_at": int(session["validated_at"] * 1000),
    }

@app.get("/health")
async def health():
    return {"ok": True}
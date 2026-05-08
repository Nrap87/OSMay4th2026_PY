"""
OutSystems Star Delivery API helpers + dashboard routes (no import of tsp_solver top-level).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import route_coaxium_check as rcc  # noqa: E402

DEFAULT_BASE_URL = (
    "https://wecode.outsystems.com/StarDelivery_Ngin/rest/StarDeliveryServices"
)

_session_lock = Lock()
# Optional UI session (overrides env when keys present)
_session: dict[str, str] = {}

router = APIRouter(prefix="/api/outsystems", tags=["outsystems"])


def pick(obj: Any, *keys: str):
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            return obj[key]
    return None


def pick_bool(obj: Any, *keys: str) -> bool:
    for key in keys:
        if isinstance(obj, dict) and key in obj and isinstance(obj[key], bool):
            return obj[key]
    return False


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def mask_guid(g: str | None) -> str | None:
    if not g:
        return None
    g = str(g).strip()
    if len(g) <= 10:
        return g[:2] + "…" + g[-2:]
    return g[:4] + "…" + g[-4:]


def mask_email(em: str | None) -> str | None:
    if not em:
        return None
    em = str(em).strip()
    if "@" not in em:
        return em[:3] + "…" if len(em) > 3 else "…"
    local, _, domain = em.partition("@")
    if len(local) <= 2:
        return "***@" + domain
    return local[:2] + "***@" + domain


def _effective_creds() -> tuple[str | None, str | None, str]:
    with _session_lock:
        sg = _session.get("PLAYER_GUID")
        se = _session.get("PLAYER_EMAIL")
        sb = _session.get("STAR_DELIVERY_BASE_URL")
    g = (sg or os.getenv("PLAYER_GUID") or "").strip() or None
    e = (se or os.getenv("PLAYER_EMAIL") or "").strip() or None
    base = (sb or os.getenv("STAR_DELIVERY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return g, e, base


def _cred_source() -> str:
    with _session_lock:
        has_s = bool(
            _session.get("PLAYER_GUID") and _session.get("PLAYER_EMAIL")
        )
    has_e = bool(os.getenv("PLAYER_GUID") and os.getenv("PLAYER_EMAIL"))
    if has_s and has_e:
        return "session_over_env"
    if has_s:
        return "session"
    if has_e:
        return "environment"
    return "none"


def _api_headers() -> tuple[str, dict[str, str]]:
    g, e, base = _effective_creds()
    if not g or not e:
        raise HTTPException(
            status_code=401,
            detail="Configure PlayerGuid and PlayerEmail (form above or PLAYER_GUID / PLAYER_EMAIL env).",
        )
    headers = {
        "Accept": "application/json",
        "PlayerGuid": g,
        "PlayerEmail": e,
    }
    return base, headers


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    g, e, base = _effective_creds()
    if g:
        env["PLAYER_GUID"] = g
    if e:
        env["PLAYER_EMAIL"] = e
    env["STAR_DELIVERY_BASE_URL"] = base
    return env


def fetch_json(base_url: str, path: str, headers: dict[str, str]) -> Any:
    url = f"{base_url}/{path.lstrip('/')}"
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=90) as resp:
            body = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"{path} HTTP {exc.code}: {detail[:800]}",
        ) from exc
    except URLError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{path} failed: {exc}",
        ) from exc
    if not body.strip():
        raise HTTPException(status_code=502, detail=f"{path} empty body")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{path} is not JSON",
        ) from exc


def _challenge_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    return []


def summarize_challenge(raw: dict) -> dict:
    """Use same field resolution as the solver (IDs vs *Planets / *Stops objects)."""
    norm = rcc.normalize_challenge(raw)
    name = norm.get("challengeName") or pick(
        raw, "ChallengeName", "challengeName", "name", "Name"
    ) or ""
    return {
        "challengeId": norm.get("challengeId"),
        "challengeName": str(name).strip() or "(no name)",
        "isFinished": bool(norm.get("isFinished")),
        "startPlanetId": norm["startPlanetId"],
        "mandatoryCount": len(norm.get("mandatoryPlanetIds") or []),
        "forbiddenCount": len(norm.get("forbiddenPlanetIds") or []),
        "bonusCount": len(norm.get("bonusStops") or []),
    }


def _tail(s: str, n: int = 12000) -> str:
    if len(s) <= n:
        return s
    return "…" + s[-n:]


class SessionBody(BaseModel):
    player_guid: str = Field(..., min_length=1)
    player_email: str = Field(..., min_length=1)
    base_url: str | None = Field(
        default=None,
        description="Optional; empty uses default OutSystems URL",
    )


class SubmitAllBody(BaseModel):
    parallel: int = Field(default=6, ge=1, le=16)


@router.get("/status")
def api_status():
    g, e, base = _effective_creds()
    src = _cred_source()
    return {
        "configured": bool(g and e),
        "source": src,
        "player_guid_masked": mask_guid(g),
        "player_email_masked": mask_email(e),
        "base_url": base,
        "uses_default_host": base.rstrip("/") == DEFAULT_BASE_URL.rstrip("/"),
    }


@router.post("/session")
def api_set_session(body: SessionBody):
    with _session_lock:
        _session["PLAYER_GUID"] = body.player_guid.strip()
        _session["PLAYER_EMAIL"] = body.player_email.strip()
        if body.base_url is not None and str(body.base_url).strip():
            _session["STAR_DELIVERY_BASE_URL"] = str(body.base_url).strip().rstrip("/")
        elif "STAR_DELIVERY_BASE_URL" in _session:
            del _session["STAR_DELIVERY_BASE_URL"]
    return api_status()


@router.delete("/session")
def api_clear_session():
    with _session_lock:
        _session.clear()
    return api_status()


@router.post("/refresh")
def api_refresh():
    base, headers = _api_headers()
    map_payload = fetch_json(base, "GetPlanetsAndRoutes", headers)
    ch_payload = fetch_json(base, "GetDailyChallenge", headers)

    if not isinstance(map_payload, dict):
        raise HTTPException(502, "GetPlanetsAndRoutes: unexpected shape")

    planets = pick(map_payload, "Planets", "planets") or []
    routes = pick(map_payload, "Routes", "routes") or []
    if not isinstance(planets, list):
        planets = []
    if not isinstance(routes, list):
        routes = []

    raw_list = _challenge_list(ch_payload)
    challenges = []
    for raw in raw_list:
        if isinstance(raw, dict):
            challenges.append(summarize_challenge(raw))

    challenges.sort(
        key=lambda c: (c.get("challengeId") is None, c.get("challengeId") or 0)
    )

    return {
        "planet_count": len(planets),
        "route_count": len(routes),
        "challenge_count": len(challenges),
        "challenges": challenges,
    }


def _run_tsp_solver(args: list[str], timeout_sec: int = 7200) -> dict:
    cmd = [sys.executable, str(ROOT / "tsp_solver.py"), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=_child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": -1,
            "error": f"Timeout after {timeout_sec}s",
            "stdout_tail": _tail(exc.stdout or ""),
            "stderr_tail": _tail(exc.stderr or ""),
        }
    out = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout_tail": _tail(proc.stdout or ""),
        "stderr_tail": _tail(proc.stderr or ""),
    }
    return out


@router.post("/challenges/{challenge_id}/run")
def api_run_challenge(challenge_id: int, submit: bool = False):
    _api_headers()  # validate creds early
    fd, dump_path = tempfile.mkstemp(suffix=".json", prefix="tsp_dump_")
    os.close(fd)
    try:
        args = [
            "--from-api",
            f"--challenge-id={challenge_id}",
            "--log-interval",
            "0",
            "--dump-result",
            dump_path,
        ]
        if submit:
            args.append("--submit")
        result = _run_tsp_solver(args)
        dump = None
        try:
            with open(dump_path, encoding="utf-8") as f:
                dump = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        result["dump"] = dump
        return result
    finally:
        try:
            os.unlink(dump_path)
        except OSError:
            pass


@router.post("/submit-all")
def api_submit_all(body: SubmitAllBody):
    _api_headers()
    args = [
        "--from-api",
        "--all-challenges",
        "--parallel",
        str(body.parallel),
        "--submit",
        "--log-interval",
        "0",
    ]
    return _run_tsp_solver(args)

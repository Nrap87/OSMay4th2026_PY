"""
Local web UI + JSON API for route scoring (same model as route_coaxium_check).

Run from the planet-tsp-solver directory:
  uvicorn webapp.main:app --reload --host 127.0.0.1 --port 8000
Then open http://127.0.0.1:8000/ (route score) or http://127.0.0.1:8000/outsystems (OutSystems API dashboard).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import route_coaxium_check as rcc
from webapp.outsystems import router as outsystems_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Planet TSP route tools", version="0.1.0")
app.include_router(outsystems_router)


class ScoreRequest(BaseModel):
    """Map JSON (planets + routes), optional challenge, and a planet-id route."""

    data: dict = Field(..., description='Object with "planets" and "routes" arrays')
    challenge: dict | None = None
    route: list[int] = Field(..., min_length=1)


def _load_map(req: ScoreRequest):
    raw = req.data
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail='"data" must be an object')
    planets_raw = raw.get("planets")
    routes_raw = raw.get("routes")
    if not isinstance(planets_raw, list) or not isinstance(routes_raw, list):
        raise HTTPException(
            status_code=400,
            detail='"data" must contain "planets" and "routes" arrays',
        )
    planets_list = [rcc.normalize_planet(p) for p in planets_raw]
    planets = {p["id"]: p for p in planets_list}
    routes_list = [rcc.normalize_route(r) for r in routes_raw]
    return planets, routes_list


def _validation_json(v: dict) -> dict:
    out = dict(v)
    rev = out.get("revisits")
    if isinstance(rev, dict):
        out["revisits"] = {str(k): val for k, val in rev.items()}
    return out


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/sample")
def api_sample():
    """Load data.json, challenge.json, and route_check.json from the repo root (dev convenience)."""
    data_path = ROOT / "data.json"
    ch_path = ROOT / "challenge.json"
    route_path = ROOT / "route_check.json"
    if not data_path.is_file() or not ch_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Place data.json and challenge.json next to tsp_solver.py for this endpoint.",
        )
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    with open(ch_path, encoding="utf-8") as f:
        raw_ch = json.load(f)
        challenge = raw_ch[0] if isinstance(raw_ch, list) else raw_ch
    route: list[int] = []
    if route_path.is_file():
        with open(route_path, encoding="utf-8") as f:
            rj = json.load(f)
            r = rj.get("route") or rj.get("planetIds") or rj.get("path")
            if isinstance(r, list):
                route = [int(x) for x in r]
    if not route:
        route = [challenge.get("startPlanetId", 0), challenge.get("startPlanetId", 0)]
    return {"data": data, "challenge": challenge, "route": route}


@app.post("/api/score")
def api_score(req: ScoreRequest):
    try:
        planets, routes_list = _load_map(req)
        challenge = rcc.normalize_challenge(req.challenge) if req.challenge else None
        metrics = rcc.local_route_metrics(
            planets, routes_list, req.route, challenge
        )
        ctx = (
            rcc.challenge_context(challenge, set(planets.keys()))
            if challenge
            else None
        )
        validation = _validation_json(rcc.validate_route(req.route, ctx, planets))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}") from e

    legs = [{"from": u, "to": v, "cost": round(c, 6)} for u, v, c in metrics["legs"]]
    return {
        "gross_fuel": metrics["gross_fuel"],
        "bonus_subtotal": metrics["bonus_subtotal"],
        "net_fuel_solver_style": metrics["net_fuel_solver_style"],
        "bonus_taken_ids": list(metrics["bonus_taken_ids"]),
        "legs": legs,
        "validation": validation,
        "challenge_id": challenge.get("challengeId") if challenge else None,
    }


app.mount(
    "/assets",
    StaticFiles(directory=str(STATIC_DIR)),
    name="assets",
)


@app.get("/")
def serve_index():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=500, detail="Missing webapp/static/index.html")
    return FileResponse(index)


@app.get("/outsystems")
def serve_outsystems():
    page = STATIC_DIR / "outsystems.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="Missing webapp/static/outsystems.html")
    return FileResponse(page)

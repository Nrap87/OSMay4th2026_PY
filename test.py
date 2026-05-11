#!/usr/bin/env python3
"""
Fetch GetPlanetsAndRoutes and save JSON for offline use as data.json.

Does not import tsp_solver (that module parses CLI on import).

Credentials: --player-guid / --player-email or PLAYER_GUID / PLAYER_EMAIL.
Base URL: --base-url or STAR_DELIVERY_BASE_URL (see DEFAULT_BASE_URL below).

Usage:
  python test.py -o data.json
  python test.py --raw -o api_map_raw.json   # raw API body; tsp_solver load_from_files accepts Planets/Routes too

Offline solve (both from files):
  python tsp_solver.py data.json challenge.json

Hybrid (local map + API challenges), single challenge from daily list:
  python tsp_solver.py --from-challenge-api data.json challenge.json

Hybrid batch (local map + all daily challenges from API):
  python tsp_solver.py --from-challenge-api --all-challenges --parallel 6 --submit

Route diff helper (old test.py): python scripts/diff_routes.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib import request
from urllib.error import HTTPError, URLError

DEFAULT_BASE_URL = "https://wecode.outsystems.com/StarDelivery_Ngin/rest/StarDeliveryServices"


def pick(obj, *keys):
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_planet(raw):
    return {
        "id": to_int(pick(raw, "id", "Id")),
        "name": str(pick(raw, "name", "Name") or ""),
        "x": to_float(pick(raw, "x", "X", "coordinateX", "CoordinateX", "Coordinate_X")),
        "y": to_float(pick(raw, "y", "Y", "coordinateY", "CoordinateY", "Coordinate_Y")),
    }


def normalize_route(raw):
    return {
        "from_planet": to_int(pick(raw, "from_planet", "fromPlanet", "FromPlanet", "From_Planet")),
        "to_planet_id": to_int(pick(raw, "to_planet_id", "toPlanetId", "ToPlanetId", "To_PlanetId")),
        "route_type": str(pick(raw, "route_type", "routeType", "RouteType") or ""),
    }


def fetch_json(base_url: str, path: str, headers: dict) -> object:
    url = f"{base_url}/{path.lstrip('/')}"
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"{path} request failed: {exc}") from exc

    if not body.strip():
        raise RuntimeError(f"{path} returned empty response body")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} response is not valid JSON") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Download GetPlanetsAndRoutes → JSON file for data.json")
    parser.add_argument(
        "-o",
        "--output",
        default="data.json",
        help="Output file path (default: data.json)",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Star Delivery REST base (default: env STAR_DELIVERY_BASE_URL or built-in default)",
    )
    parser.add_argument("--player-guid", default="", help="PlayerGuid header (default: env PLAYER_GUID)")
    parser.add_argument("--player-email", default="", help="PlayerEmail header (default: env PLAYER_EMAIL)")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Write the raw API JSON (Planets/Routes keys). tsp_solver accepts this shape in data.json too.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Minified JSON (default: pretty-printed)",
    )
    args = parser.parse_args()

    base_url = (args.base_url or os.getenv("STAR_DELIVERY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    guid = args.player_guid or os.getenv("PLAYER_GUID") or ""
    email = args.player_email or os.getenv("PLAYER_EMAIL") or ""
    if not guid or not email:
        print(
            "Missing PlayerGuid / PlayerEmail (--player-guid, --player-email or PLAYER_GUID, PLAYER_EMAIL).",
            file=sys.stderr,
        )
        return 2

    headers = {
        "Accept": "application/json",
        "PlayerGuid": guid,
        "PlayerEmail": email,
    }

    try:
        payload = fetch_json(base_url, "GetPlanetsAndRoutes", headers)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print("GetPlanetsAndRoutes: expected a JSON object", file=sys.stderr)
        return 1

    if args.raw:
        out_obj = payload
    else:
        planets_raw = pick(payload, "Planets", "planets") or []
        routes_raw = pick(payload, "Routes", "routes") or []
        out_obj = {
            "planets": [normalize_planet(p) for p in planets_raw],
            "routes": [normalize_route(r) for r in routes_raw],
        }

    indent = None if args.compact else 2
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out_obj, f, ensure_ascii=False, indent=indent)
            if indent is not None:
                f.write("\n")
    except OSError as exc:
        print(f"Could not write {args.output}: {exc}", file=sys.stderr)
        return 1

    if args.raw:
        n_planets = len(pick(out_obj, "Planets", "planets") or [])
        n_routes = len(pick(out_obj, "Routes", "routes") or [])
    else:
        n_planets = len(out_obj["planets"])
        n_routes = len(out_obj["routes"])
    print(f"Wrote {args.output} ({n_planets} planets, {n_routes} routes, raw={args.raw})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

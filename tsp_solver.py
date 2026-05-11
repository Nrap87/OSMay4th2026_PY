"""
TSP solver - OPTION A STRICT (no revisits on allowed waypoints).

Forbidden planets cannot be start, mandatory, or bonus stops, and must not
appear anywhere in the submitted planet sequence. Routing uses the full planet
graph but Dijkstra never steps on forbidden nodes. Only non-forbidden planets
are tracked as "used" for revisit constraints.

For each candidate ordering of mandatory + chosen-bonus planets,
route segment-by-segment with a global exclusion set on allowed planets.
The cheapest net (gross_fuel - sum_of_bonus_values) wins.
"""
import argparse
import heapq
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

try:
    import numpy as _np
    from scipy.sparse.csgraph import shortest_path as _scipy_shortest_path
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False

DEFAULT_BASE_URL = "https://wecode.outsystems.com/StarDelivery_Ngin/rest/StarDeliveryServices"


def api_connection(args):
    base_url = (args.base_url or os.getenv("STAR_DELIVERY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    player_guid = args.player_guid or os.getenv("PLAYER_GUID")
    player_email = args.player_email or os.getenv("PLAYER_EMAIL")
    if not player_guid or not player_email:
        raise ValueError(
            "PlayerGuid and PlayerEmail are required "
            "(--player-guid/--player-email or PLAYER_GUID/PLAYER_EMAIL)."
        )
    headers = {
        "Accept": "application/json",
        "PlayerGuid": player_guid,
        "PlayerEmail": player_email,
    }
    return base_url, headers


def resolved_challenge_id(challenge, args):
    raw_cid = challenge.get("challengeId")
    if raw_cid is not None and str(raw_cid).strip() != "":
        try:
            return int(raw_cid)
        except (TypeError, ValueError):
            pass
    return args.challenge_id


def build_submission_route(full_path, planets_by_id):
    return [{"PlanetId": pid, "Name": planets_by_id[pid]["name"]} for pid in full_path]


def api_calculate_coaxium(base_url, headers, challenge_id, route, planets_by_id):
    submission = build_submission_route(route, planets_by_id)
    q = urlencode({"ChallengeId": str(challenge_id)})
    return post_star_delivery_json(base_url, f"CalculateCoaxium?{q}", submission, headers)


def api_submit_solution(base_url, headers, challenge_id, route, planets_by_id):
    submission = build_submission_route(route, planets_by_id)
    q = urlencode({"ChallengeId": str(challenge_id)})
    return post_star_delivery_json(base_url, f"SubmitChallengeSolution?{q}", submission, headers)


def pick_bool(obj, *keys):
    for key in keys:
        if key in obj and isinstance(obj[key], bool):
            return obj[key]
    return False


def normalize_submission_result(raw):
    o = raw if isinstance(raw, dict) else {}
    coax_raw = pick(o, "Coaxium", "coaxium")
    try:
        coaxium = int(float(coax_raw)) if coax_raw is not None else 0
    except (TypeError, ValueError):
        coaxium = 0
    te_sec = pick(o, "TimeElapsedInSeconds", "timeElapsedInSeconds")
    te = pick(o, "TimeElapsed", "timeElapsed")
    return {
        "is_success": pick_bool(o, "IsSuccess", "isSuccess"),
        "feedback_message": str(pick(o, "FeedbackMessage", "feedbackMessage") or ""),
        "coaxium": coaxium,
        "time_elapsed_in_seconds": te_sec if isinstance(te_sec, (int, float)) else None,
        "time_elapsed": te if isinstance(te, (int, float)) else None,
    }


def post_star_delivery_json(base_url, relative_path_with_query, body_obj, headers):
    url = f"{base_url}/{relative_path_with_query.lstrip('/')}"
    data = json.dumps(body_obj).encode("utf-8")
    h = dict(headers)
    h["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(url, data=data, headers=h, method="POST")
    try:
        with request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        endpoint = relative_path_with_query.split("?", maxsplit=1)[0]
        raise RuntimeError(f"{endpoint} HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"{relative_path_with_query} request failed: {exc}") from exc

    if not text.strip():
        return {
            "is_success": False,
            "feedback_message": "Empty response body.",
            "coaxium": 0,
            "time_elapsed_in_seconds": None,
            "time_elapsed": None,
        }
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Response is not valid JSON") from exc
    return normalize_submission_result(parsed)


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


def pick(obj, *keys):
    for key in keys:
        if key in obj:
            return obj[key]
    return None


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


def normalize_challenge(raw):
    start = to_int(pick(raw, "startPlanetId", "StartPlanetId"))
    mandatory_ids = pick(raw, "mandatoryPlanetIds", "MandatoryPlanetIds")
    forbidden_ids = pick(raw, "forbiddenPlanetIds", "ForbiddenPlanetIds")
    bonus_stops = pick(raw, "bonusStops", "BonusStops")

    if mandatory_ids is None:
        mandatory_planets = pick(raw, "mandatoryPlanets", "MandatoryPlanets") or []
        mandatory_ids = [to_int(pick(p, "planetId", "PlanetId")) for p in mandatory_planets]
    if forbidden_ids is None:
        forbidden_planets = pick(raw, "forbiddenPlanets", "ForbiddenPlanets") or []
        forbidden_ids = [to_int(pick(p, "planetId", "PlanetId")) for p in forbidden_planets]
    if bonus_stops is None:
        bonus_planets = pick(raw, "bonusPlanets", "BonusPlanets") or []
        bonus_stops = [
            {
                "planetId": to_int(pick(b, "planetId", "PlanetId")),
                "value": to_float(pick(b, "value", "Value", "bonus", "Bonus")),
            }
            for b in bonus_planets
        ]
    else:
        bonus_stops = [
            {
                "planetId": to_int(pick(b, "planetId", "PlanetId")),
                "value": to_float(pick(b, "value", "Value")),
            }
            for b in bonus_stops
        ]

    out = {
        "startPlanetId": start,
        "mandatoryPlanetIds": [to_int(v) for v in (mandatory_ids or [])],
        "forbiddenPlanetIds": [to_int(v) for v in (forbidden_ids or [])],
        "bonusStops": bonus_stops,
    }
    cid_raw = pick(raw, "challengeId", "ChallengeId")
    if cid_raw is not None and str(cid_raw).strip() != "":
        try:
            out["challengeId"] = int(cid_raw)
        except (TypeError, ValueError):
            pass
    cname = pick(raw, "challengeName", "ChallengeName", "name", "Name")
    if cname is not None and str(cname).strip():
        out["challengeName"] = str(cname).strip()
    out["isFinished"] = pick_bool(raw, "IsFinished", "isFinished")
    return out


def fetch_json(base_url, path, headers):
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


def fetch_map_raw_blob(args):
    """Raw planets/routes lists as returned by the API (for batch map.json)."""
    try:
        base_url, headers = api_connection(args)
    except ValueError as exc:
        raise ValueError(f"GetPlanetsAndRoutes: {exc}") from exc
    map_payload = fetch_json(base_url, "GetPlanetsAndRoutes", headers)
    if not isinstance(map_payload, dict):
        raise ValueError("GetPlanetsAndRoutes returned an unexpected payload type")
    planets_raw = pick(map_payload, "Planets", "planets") or []
    routes_raw = pick(map_payload, "Routes", "routes") or []
    return {"planets": planets_raw, "routes": routes_raw}


def load_map_raw_blob_from_file(data_path):
    """Same shape as fetch_map_raw_blob, read from local JSON (Planets/Routes or planets/routes)."""
    with open(data_path, encoding="utf-8") as f:
        data_raw = json.load(f)
    planets_raw = pick(data_raw, "Planets", "planets") or []
    routes_raw = pick(data_raw, "Routes", "routes") or []
    return {"planets": planets_raw, "routes": routes_raw}


def fetch_challenges_raw_list(args):
    """Daily challenge list from GetDailyChallenge."""
    try:
        base_url, headers = api_connection(args)
    except ValueError as exc:
        raise ValueError(f"GetDailyChallenge: {exc}") from exc
    challenges_payload = fetch_json(base_url, "GetDailyChallenge", headers)
    if isinstance(challenges_payload, list):
        challenge_list = challenges_payload
    elif isinstance(challenges_payload, dict) and isinstance(challenges_payload.get("items"), list):
        challenge_list = challenges_payload["items"]
    else:
        challenge_list = []
    if not challenge_list:
        raise ValueError("GetDailyChallenge returned no challenges")
    return challenge_list


def pick_raw_challenge_from_list(challenge_list, args):
    if args.challenge_id is not None:
        for c in challenge_list:
            cid = to_int(pick(c, "ChallengeId", "challengeId"), default=-1)
            if cid == args.challenge_id:
                return c
        raise ValueError(f"Challenge ID {args.challenge_id} not found")
    idx = args.challenge_index
    if idx < 0 or idx >= len(challenge_list):
        raise ValueError(f"Challenge index {idx} out of range (found {len(challenge_list)} challenges)")
    return challenge_list[idx]


def load_map_from_api(args):
    blob = fetch_map_raw_blob(args)
    return {
        "planets": [normalize_planet(p) for p in blob["planets"]],
        "routes": [normalize_route(r) for r in blob["routes"]],
    }


def load_challenge_from_api(args):
    challenge_list = fetch_challenges_raw_list(args)
    raw_challenge = pick_raw_challenge_from_list(challenge_list, args)
    return normalize_challenge(raw_challenge)


def load_from_api(args):
    """Fetch both map and one challenge from API (same as --from-planets-api --from-challenge-api)."""
    return load_map_from_api(args), load_challenge_from_api(args)


def fetch_raw_maps_and_challenges(args):
    """Batch: map from API or data_path; challenges always from GetDailyChallenge."""
    if args.from_planets_api:
        data_blob = fetch_map_raw_blob(args)
    else:
        data_blob = load_map_raw_blob_from_file(args.data_path)
    challenge_list = fetch_challenges_raw_list(args)
    return data_blob, challenge_list


def challenge_sort_key(raw):
    return to_int(pick(raw, "ChallengeId", "challengeId"), default=0)


def forward_flags_for_child(args):
    """CLI flags to pass to child tsp_solver (file mode); no --from-* (child uses map/challenge files)."""
    a = []
    if args.verbose:
        a.append("-v")
    a.append(f"--log-interval={args.log_interval}")
    if args.calculate_coaxium:
        a.append("--calculate-coaxium")
    if args.submit:
        a.append("--submit")
    if args.greedy_seed_coaxium:
        a.append("--greedy-seed-coaxium")
    if args.base_url.strip():
        a.append(f"--base-url={args.base_url}")
    if args.player_guid.strip():
        a.append(f"--player-guid={args.player_guid}")
    if args.player_email.strip():
        a.append(f"--player-email={args.player_email}")
    return a


def forward_flags_solve_only(args):
    """Child batch solve: no OutSystems post (parent pipelines Calculate/Submit in ChallengeId order)."""
    a = []
    if args.verbose:
        a.append("-v")
    a.append(f"--log-interval={args.log_interval}")
    if args.greedy_seed_coaxium:
        a.append("--greedy-seed-coaxium")
    if args.base_url.strip():
        a.append(f"--base-url={args.base_url}")
    if args.player_guid.strip():
        a.append(f"--player-guid={args.player_guid}")
    if args.player_email.strip():
        a.append(f"--player-email={args.player_email}")
    return a


def _batch_solve_worker(script, cwd, data_path, ch_path, result_path, flags, *, quiet_report=False):
    cmd = [
        sys.executable,
        script,
        data_path,
        ch_path,
        "--dump-result",
        result_path,
        *flags,
    ]
    env = os.environ.copy()
    if quiet_report:
        env["TSP_SOLVER_QUIET_REPORT"] = "1"
    return subprocess.run(cmd, cwd=cwd, env=env)


def _batch_solve_task(script, cwd, data_path, result_path, flags, job, *, quiet_report=False):
    r = _batch_solve_worker(
        script, cwd, data_path, job["ch_path"], result_path, flags, quiet_report=quiet_report
    )
    return job["cid"], r.returncode, result_path, job


def _enqueue_batch_solve_completion(future, job, completion_q, *, silent=False):
    """ThreadPool callback: record solve outcome; main thread submits API in ChallengeId order."""
    cid = job["cid"]
    idx = job["index"]
    try:
        completion_q.put((idx, "ok", future.result()))
    except Exception as exc:
        completion_q.put((idx, "err", exc))
    if not silent:
        print(
            f"[batch] ChallengeId={cid} solve finished (API runs in id order; may wait on earlier challenges)",
            file=sys.stderr,
            flush=True,
        )


def _fmt_batch_sec(seconds):
    if seconds is None:
        return "—"
    return f"{float(seconds):.3f}"


def _print_batch_challenge_summary(j, dump, args, *, calc_s, calc_coax, submit_s, submit_coax, submit_note):
    """One block per challenge: route, phase timings, coaxium (stdout)."""
    cid = j["cid"]
    cname = j["cname"]
    route = dump.get("route") or []
    solve_s = dump.get("solveSeconds")
    if solve_s is None:
        lb = dump.get("lowerBoundSeconds")
        bb = dump.get("branchBoundSeconds")
        if lb is not None and bb is not None:
            solve_s = float(lb) + float(bb)
    best_net = dump.get("bestNet")

    parts = [
        "",
        "=" * 70,
        f"ChallengeId={cid}  {cname}",
        f"  route: {json.dumps(route, separators=(',', ':'))}",
        f"  solve_s:            {_fmt_batch_sec(solve_s)}",
    ]
    if args.calculate_coaxium:
        parts.append(f"  calculate_coaxium_s: {_fmt_batch_sec(calc_s)}")
        parts.append(
            f"  coaxium_calculate:   {calc_coax if calc_coax is not None else '—'}",
        )
    if args.submit:
        parts.append(f"  submit_s:            {_fmt_batch_sec(submit_s)}")
        parts.append(
            f"  coaxium_submit:      {submit_coax if submit_coax is not None else '—'}",
        )
        if submit_note:
            parts.append(f"  submit_note:         {submit_note}")
    if best_net is not None:
        parts.append(f"  local_best_net:      {best_net}")

    total = 0.0
    if solve_s is not None:
        total += float(solve_s)
    if calc_s is not None:
        total += float(calc_s)
    if submit_s is not None:
        total += float(submit_s)
    parts.append(f"  total_s (solve+API): {_fmt_batch_sec(total)}")
    parts.append("=" * 70)
    print("\n".join(parts), flush=True)


def _batch_api_pipeline_ordered(jobs, completion_q, base_url, headers, args, planets_parent, *, batch_summary):
    """
    For each challenge index in sorted (ChallengeId) order:
      - Wait for that index's local solve to complete (later ids may finish first and queue up).
      - Then CalculateCoaxium if --calculate-coaxium.
      - Submit only if --submit and not IsFinished (finished challenges are still solved locally).
    Returns worst exit code (0 = all ok).
    """
    worst = 0
    pending = {}
    n = len(jobs)
    next_i = 0

    if not batch_summary:
        print("\n" + "=" * 70, file=sys.stderr)
        print(
            "[batch] API pipeline: ChallengeId ascending; each step after prior index (solve + optional API); "
            "Submit skipped when IsFinished",
            file=sys.stderr,
        )
        print("=" * 70 + "\n", file=sys.stderr)

    while next_i < n:
        while next_i not in pending:
            idx, kind, data = completion_q.get()
            if kind == "ok":
                pending[idx] = ("ok", data)
            else:
                pending[idx] = ("err", data)

        kind, data = pending.pop(next_i)
        j = jobs[next_i]
        cid = j["cid"]
        fin = j["is_finished"]

        if kind == "err":
            print(
                f"[batch] ChallengeId={cid} solve task crashed: {data}",
                file=sys.stderr,
                flush=True,
            )
            worst = 1 if worst == 0 else worst
            next_i += 1
            continue

        _, code, result_path, _job = data
        if not batch_summary:
            print(
                f"[batch] --- ChallengeId={cid}  solve_rc={code}  IsFinished={j['is_finished']} "
                f"(ready for API in order) ---",
                file=sys.stderr,
                flush=True,
            )

        if code != 0:
            print(f"[batch] ChallengeId={cid}: skip API (solve failed)", file=sys.stderr, flush=True)
            worst = code if worst == 0 else worst
            next_i += 1
            continue

        try:
            with open(result_path, encoding="utf-8") as rf:
                dump = json.load(rf)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[batch] ChallengeId={cid}: no result JSON ({exc})", file=sys.stderr, flush=True)
            worst = 1 if worst == 0 else worst
            next_i += 1
            continue

        if not dump.get("success"):
            print(f"[batch] ChallengeId={cid}: dump reports failure", file=sys.stderr, flush=True)
            worst = 1 if worst == 0 else worst
            next_i += 1
            continue

        route = dump.get("route")
        if not isinstance(route, list) or not route:
            print(f"[batch] ChallengeId={cid}: missing route in dump", file=sys.stderr, flush=True)
            worst = 1 if worst == 0 else worst
            next_i += 1
            continue

        calc_s = None
        calc_coax = None
        submit_s = None
        submit_coax = None
        submit_note = ""

        if args.calculate_coaxium:
            if not batch_summary:
                print(f"OUTSYSTEMS: CalculateCoaxium  ChallengeId={cid}", flush=True)
            t0 = time.perf_counter()
            try:
                calc = api_calculate_coaxium(base_url, headers, cid, route, planets_parent)
                calc_s = time.perf_counter() - t0
                calc_coax = calc.get("coaxium")
                if not batch_summary:
                    print(f"  Success:   {calc['is_success']}")
                    print(f"  Coaxium:   {calc['coaxium']}")
                    print(f"  Feedback:  {calc['feedback_message']}")
            except RuntimeError as exc:
                calc_s = time.perf_counter() - t0
                print(f"  Error: {exc}", file=sys.stderr, flush=True)
                worst = 1 if worst == 0 else worst

        if args.submit:
            if fin:
                submit_note = "skipped (IsFinished on API — no SubmitChallengeSolution call)"
                if not batch_summary:
                    print(
                        f"[batch] ChallengeId={cid}: IsFinished on API — skip SubmitChallengeSolution",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                if not batch_summary:
                    print(f"OUTSYSTEMS: SubmitChallengeSolution  ChallengeId={cid}", flush=True)
                t0 = time.perf_counter()
                try:
                    sub = api_submit_solution(base_url, headers, cid, route, planets_parent)
                    submit_s = time.perf_counter() - t0
                    submit_coax = sub.get("coaxium")
                    if not batch_summary:
                        print(f"  Success:   {sub['is_success']}")
                        print(f"  Coaxium:   {sub['coaxium']}")
                        print(f"  Feedback:  {sub['feedback_message']}")
                except RuntimeError as exc:
                    submit_s = time.perf_counter() - t0
                    print(f"  Error: {exc}", file=sys.stderr, flush=True)
                    worst = 1 if worst == 0 else worst

        if batch_summary:
            _print_batch_challenge_summary(
                j,
                dump,
                args,
                calc_s=calc_s,
                calc_coax=calc_coax,
                submit_s=submit_s,
                submit_coax=submit_coax,
                submit_note=submit_note,
            )

        next_i += 1

    return worst


def run_batch_all_challenges(args):
    """
    Map from GetPlanetsAndRoutes (--from-planets-api) or from data_path; challenges from GetDailyChallenge.
    Challenges are ordered by ChallengeId ascending (stable: ties keep API list order).
    Solves run in parallel (--parallel), including IsFinished challenges. With --calculate-coaxium /
    --submit, the parent runs API calls in ChallengeId order as soon as each challenge's solve
    completes and prior indices have finished their pipeline step; Submit is skipped when IsFinished.
    """
    data_blob, raw_challenges = fetch_raw_maps_and_challenges(args)
    ordered = sorted(raw_challenges, key=challenge_sort_key)
    if args.challenge_id is not None:
        ordered = [c for c in ordered if to_int(pick(c, "ChallengeId", "challengeId"), default=-1) == args.challenge_id]
        if not ordered:
            print(f"No challenge with id {args.challenge_id} in daily list.", file=sys.stderr)
            return 1
    if args.skip_finished:
        ordered = [c for c in ordered if not pick_bool(c, "IsFinished", "isFinished")]
        if not ordered:
            print("All challenges filtered out (--skip-finished).", file=sys.stderr)
            return 1

    try:
        pw = int(args.parallel)
    except (TypeError, ValueError):
        pw = 4
    parallel = max(1, min(pw, len(ordered)))
    parent_handles_api = bool(args.calculate_coaxium or args.submit)
    use_summary = args.batch_summary
    if use_summary is None:
        use_summary = parent_handles_api
    solve_flags = forward_flags_solve_only(args) if parent_handles_api else forward_flags_for_child(args)
    if parent_handles_api and use_summary:
        solve_flags = [f for f in solve_flags if f not in ("-v", "--verbose")]

    script = os.path.abspath(__file__)
    cwd = os.path.dirname(script) or "."
    worst = 0
    tmpdir = tempfile.mkdtemp(prefix="tsp_batch_")
    data_path = os.path.join(tmpdir, "map.json")
    planets_parent = {
        normalize_planet(p)["id"]: normalize_planet(p) for p in (data_blob.get("planets") or [])
    }

    try:
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data_blob, f, ensure_ascii=False)

        jobs = []
        for i, raw in enumerate(ordered):
            cid = to_int(pick(raw, "ChallengeId", "challengeId"), default=-1)
            ch_path = os.path.join(tmpdir, f"challenge_{cid}_{i}.json")
            result_path = os.path.join(tmpdir, f"result_{cid}_{i}.json")
            with open(ch_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            jobs.append(
                {
                    "index": i,
                    "cid": cid,
                    "raw": raw,
                    "ch_path": ch_path,
                    "result_path": result_path,
                    "cname": str(pick(raw, "ChallengeName", "challengeName") or "").strip() or "(no name)",
                    "is_finished": pick_bool(raw, "IsFinished", "isFinished"),
                }
            )

        print(
            f"[batch] challenges={len(jobs)} order=ChallengeId_asc parallel_solves={parallel} "
            f"parent_ordered_api={'yes' if parent_handles_api else 'no (child handles coaxium/submit if flags set)'} "
            f"summary={'on' if (parent_handles_api and use_summary) else 'off'}",
            file=sys.stderr,
            flush=True,
        )

        with ThreadPoolExecutor(max_workers=parallel) as ex:
            if parent_handles_api:
                completion_q = queue.Queue()
                quiet_child = bool(use_summary)
                for j in jobs:
                    if not use_summary:
                        banner = (
                            f"\n{'#' * 70}\n"
                            f"# SOLVE START  ChallengeId={j['cid']}  IsFinished={j['is_finished']}\n"
                            f"# {j['cname']}\n"
                            f"{'#' * 70}\n"
                        )
                        print(banner, file=sys.stderr, flush=True)
                    fut = ex.submit(
                        _batch_solve_task,
                        script,
                        cwd,
                        data_path,
                        j["result_path"],
                        solve_flags,
                        j,
                        quiet_report=quiet_child,
                    )
                    fut.add_done_callback(
                        lambda f, job=j, silent=quiet_child: _enqueue_batch_solve_completion(
                            f, job, completion_q, silent=silent
                        )
                    )

                try:
                    base_url, headers = api_connection(args)
                except ValueError as exc:
                    print(
                        f"[batch] API credentials required for --calculate-coaxium / --submit: {exc}",
                        file=sys.stderr,
                    )
                    return 1

                worst = _batch_api_pipeline_ordered(
                    jobs, completion_q, base_url, headers, args, planets_parent, batch_summary=use_summary
                )
            else:
                futures_in_order = []
                for j in jobs:
                    banner = (
                        f"\n{'#' * 70}\n"
                        f"# SOLVE START  ChallengeId={j['cid']}  IsFinished={j['is_finished']}\n"
                        f"# {j['cname']}\n"
                        f"{'#' * 70}\n"
                    )
                    print(banner, file=sys.stderr, flush=True)
                    futures_in_order.append(
                        ex.submit(
                            _batch_solve_task,
                            script,
                            cwd,
                            data_path,
                            j["result_path"],
                            solve_flags,
                            j,
                        )
                    )

                for j, fut in zip(jobs, futures_in_order):
                    try:
                        cid, code, result_path, job = fut.result()
                    except Exception as exc:
                        print(
                            f"[batch] ChallengeId={j['cid']} solve task crashed: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        worst = 1 if worst == 0 else worst
                        continue
                    if code != 0:
                        worst = code if worst == 0 else worst
                    print(
                        f"[batch] ChallengeId={cid} solve done rc={code} (ChallengeId order)",
                        file=sys.stderr,
                        flush=True,
                    )

    finally:
        try:
            for name in os.listdir(tmpdir):
                os.unlink(os.path.join(tmpdir, name))
            os.rmdir(tmpdir)
        except OSError:
            pass
    return worst


def load_data_only_from_files(data_path):
    with open(data_path, encoding="utf-8") as f:
        data_raw = json.load(f)
    planets_raw = pick(data_raw, "Planets", "planets") or []
    routes_raw = pick(data_raw, "Routes", "routes") or []
    return {
        "planets": [normalize_planet(p) for p in planets_raw],
        "routes": [normalize_route(r) for r in routes_raw],
    }


def load_challenge_only_from_files(challenge_path):
    with open(challenge_path, encoding="utf-8") as f:
        challenge_raw = json.load(f)
    challenge_obj = challenge_raw[0] if isinstance(challenge_raw, list) else challenge_raw
    if not isinstance(challenge_obj, dict):
        raise ValueError("Challenge file must contain an object or a list with one object")
    return normalize_challenge(challenge_obj)


def load_from_files(data_path, challenge_path):
    return load_data_only_from_files(data_path), load_challenge_only_from_files(challenge_path)


def parse_args():
    parser = argparse.ArgumentParser(description="Solve TSP for Star Delivery maps/challenges.")
    parser.add_argument("data_path", nargs="?", default="data.json", help="Path to local map data JSON")
    parser.add_argument("challenge_path", nargs="?", default="challenge.json", help="Path to local challenge JSON")
    parser.add_argument(
        "--from-planets-api",
        action="store_true",
        help="Fetch planets/routes from GetPlanetsAndRoutes; otherwise use data_path (default data.json).",
    )
    parser.add_argument(
        "--from-challenge-api",
        action="store_true",
        help="Fetch challenge(s) from GetDailyChallenge; otherwise use challenge_path (default challenge.json).",
    )
    parser.add_argument(
        "--from-api",
        action="store_true",
        help="Shorthand for both --from-planets-api and --from-challenge-api.",
    )
    parser.add_argument("--base-url", default="", help="Star Delivery base URL")
    parser.add_argument("--player-guid", default="", help="PlayerGuid header for API requests")
    parser.add_argument("--player-email", default="", help="PlayerEmail header for API requests")
    parser.add_argument(
        "--challenge-id",
        type=int,
        default=None,
        help="ChallengeId: pick from daily list (--from-challenge-api), --calculate-coaxium/--submit, or --greedy-seed-coaxium",
    )
    parser.add_argument(
        "--challenge-index",
        type=int,
        default=0,
        help="Challenge index in GetDailyChallenge list (only with --from-challenge-api).",
    )
    parser.add_argument(
        "--all-challenges",
        action="store_true",
        help=(
            "Requires --from-challenge-api (or --from-api): solve every challenge in GetDailyChallenge. "
            "Map comes from --from-planets-api or data_path. Challenges are sorted by ChallengeId ascending; "
            "solves run in parallel (--parallel). With --calculate-coaxium / --submit, the parent runs API calls in "
            "ChallengeId order; Submit is skipped for IsFinished challenges."
        ),
    )
    parser.add_argument(
        "--skip-finished",
        action="store_true",
        help="With --all-challenges: only run challenges not marked IsFinished.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        metavar="N",
        help=(
            "With --all-challenges: max concurrent solve subprocesses (default 4). "
            "CalculateCoaxium/Submit always run in the parent in ChallengeId order after solves."
        ),
    )
    parser.add_argument(
        "--dump-result",
        default="",
        metavar="PATH",
        help="Write solve result JSON (route, fuel, challengeId) for tooling/batch.",
    )
    parser.add_argument(
        "--calculate-coaxium",
        action="store_true",
        help="POST solved route to CalculateCoaxium (needs credentials + ChallengeId)",
    )
    parser.add_argument(
        "--submit",
        "--submit-route",
        "--submit-after-solve",
        action="store_true",
        dest="submit",
        help=(
            "After the route is computed, POST it to SubmitChallengeSolution (official score). "
            "Requires PlayerGuid/PlayerEmail and ChallengeId (challenge JSON or --challenge-id). "
            "Use with --all-challenges to submit each solved route. Combine with --calculate-coaxium "
            "to print coaxium before submit."
        ),
    )
    parser.add_argument(
        "--greedy-seed-coaxium",
        action="store_true",
        help=(
            "After greedy mandatory-only seed route, call CalculateCoaxium and use returned "
            "coaxium as initial best_net/best_gross for pruning (needs ChallengeId + credentials). "
            "Falls back to locally summed fuel if the request fails. "
            "Assumes API coaxium matches local net objective for that route."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Progress log to stderr (phases + periodic B&B stats). Or set TSP_SOLVER_VERBOSE=1.",
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        default=50000,
        metavar="N",
        help="With --verbose: log every N DFS nodes (0 = phase logs only). Default 50000.",
    )
    _bs = parser.add_mutually_exclusive_group()
    _bs.add_argument(
        "--batch-summary",
        dest="batch_summary",
        action="store_true",
        help=(
            "With --all-challenges and --calculate-coaxium / --submit: print one compact block per challenge "
            "(route, solve/API seconds, coaxium). On by default for that mode; use --no-batch-summary for full solver output."
        ),
    )
    _bs.add_argument(
        "--no-batch-summary",
        dest="batch_summary",
        action="store_false",
        help="In batch API mode, show full interleaved solver stdout (legacy).",
    )
    parser.set_defaults(batch_summary=None)
    return parser.parse_args()


ARGS = parse_args()

if ARGS.from_api:
    ARGS.from_planets_api = True
    ARGS.from_challenge_api = True

if ARGS.all_challenges and not ARGS.from_challenge_api:
    print(
        "--all-challenges requires daily challenges from the API "
        "(--from-challenge-api or --from-api).",
        file=sys.stderr,
    )
    sys.exit(2)
if ARGS.all_challenges:
    try:
        rc = run_batch_all_challenges(ARGS)
    except (ValueError, RuntimeError) as exc:
        print(f"Batch error: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)

_SOLVER_T0 = time.monotonic()
VERBOSE = ARGS.verbose or (
    os.getenv("TSP_SOLVER_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on")
)
LOG_INTERVAL = max(0, ARGS.log_interval)
QUIET_REPORT = os.getenv("TSP_SOLVER_QUIET_REPORT", "").strip().lower() in ("1", "true", "yes", "on")


def solver_log(msg):
    if VERBOSE:
        elapsed = time.monotonic() - _SOLVER_T0
        print(f"[t={elapsed:9.2f}s] {msg}", file=sys.stderr, flush=True)
if not ARGS.from_planets_api and not ARGS.from_challenge_api:
    DATA, CHALLENGE = load_from_files(ARGS.data_path, ARGS.challenge_path)
else:
    if ARGS.from_planets_api:
        DATA = load_map_from_api(ARGS)
    else:
        DATA = load_data_only_from_files(ARGS.data_path)
    if ARGS.from_challenge_api:
        CHALLENGE = load_challenge_from_api(ARGS)
    else:
        CHALLENGE = load_challenge_only_from_files(ARGS.challenge_path)

planets = {p["id"]: p for p in DATA["planets"]}
routes = DATA["routes"]
START = CHALLENGE["startPlanetId"]
MANDATORY = set(CHALLENGE.get("mandatoryPlanetIds", []))
FORBIDDEN = set(CHALLENGE.get("forbiddenPlanetIds", []))
BONUSES = {b["planetId"]: b["value"] for b in CHALLENGE.get("bonusStops", [])}

allowed = {pid for pid in planets if pid not in FORBIDDEN}
if START not in allowed: raise ValueError("Start forbidden")
for m in MANDATORY:
    if m not in allowed: raise ValueError(f"Mandatory {m} forbidden")

# Shortest-path routing never visits forbidden planets; graph includes every known planet.
ROUTING_NODES = set(planets.keys())

def euclid(a, b):
    pa, pb = planets[a], planets[b]
    return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])

route_mult = {}
for r in routes:
    a, b = r["from_planet"], r["to_planet_id"]
    if a not in planets or b not in planets:
        continue
    m = 0.5 if r["route_type"] == "Main Route" else 2.0/3.0
    k = (min(a,b), max(a,b))
    if k not in route_mult or m < route_mult[k]: route_mult[k] = m

def edge_cost(a, b):
    if a == b: return 0.0
    return euclid(a, b) * route_mult.get((min(a,b), max(a,b)), 1.0)

mandatory_list = sorted(m for m in MANDATORY if m != START)
bonus_list = sorted(b for b in BONUSES if b in allowed and b != START and b not in MANDATORY)
key_nodes = [START] + mandatory_list + bonus_list
key_set = set(key_nodes)
mandatory_set = set(mandatory_list)
bonus_set = set(bonus_list)

# Adjacency list: planet_id -> list of (neighbour_id, cost).
# Built once from the complete planet graph using the same edge_cost logic.
# Replaces the O(N^2) inner loop in both Dijkstra variants.
from collections import defaultdict as _defaultdict
ADJ: dict = _defaultdict(list)
_planet_ids = list(planets.keys())
for _a in _planet_ids:
    for _b in _planet_ids:
        if _a == _b:
            continue
        ADJ[_a].append((_b, edge_cost(_a, _b)))

best_net = float("inf")
best_full = None
best_gross = None
best_bonuses_taken = frozenset()
nodes_explored = 0

# Cache for dijkstra_avoiding: keyed on (src, dst, frozenset(excluded)).
# The exclusion set changes at every B&B node but many sibling branches share
# the same (src, dst, excluded) triple, particularly for the closing leg to START.
_dijkstra_cache: dict = {}


def dijkstra_avoiding(src, dst, excluded):
    """Shortest path src -> dst; `excluded` blocks allowed waypoints (other keys, used).
    Forbidden planets are never used as hops (OutSystems rule).
    Returns (cost, path_list) or (None, None).
    Results are memoised by (src, dst, frozenset(excluded))."""
    key = (src, dst, frozenset(excluded))
    cached = _dijkstra_cache.get(key)
    if cached is not None:
        return cached

    INF = float("inf")
    distv = {}
    prev = {}
    distv[src] = 0.0
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > distv.get(u, INF): continue
        if u == dst: break
        for v, w in ADJ[u]:
            if v in FORBIDDEN: continue
            if v in excluded and v != dst: continue
            nd = d + w
            if nd < distv.get(v, INF):
                distv[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if distv.get(dst, INF) == INF:
        _dijkstra_cache[key] = (None, None)
        return None, None
    path, cur = [dst], dst
    while cur != src:
        cur = prev.get(cur)
        if cur is None:
            _dijkstra_cache[key] = (None, None)
            return None, None
        path.append(cur)
    path.reverse()
    result = (distv[dst], path)
    _dijkstra_cache[key] = result
    return result


def dijkstra_all(src):
    """Dijkstra over the full planet graph, never stepping on forbidden nodes.
    Used for admissible lower bounds under the same routing rule as segments."""
    INF = float("inf")
    distv = {}
    distv[src] = 0.0
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > distv.get(u, INF): continue
        for v, w in ADJ[u]:
            if v in FORBIDDEN: continue
            nd = d + w
            if nd < distv.get(v, INF):
                distv[v] = nd
                heapq.heappush(pq, (nd, v))
    return distv


# Lower-bound pairwise costs between every key node, ignoring revisit
# constraints. Real routed costs are always >= these.
#
# Strategy: scipy.sparse.csgraph.shortest_path computes all N sources in one
# C-speed call (~50ms fixed overhead on this graph). Per-source Python Dijkstra
# costs ~4ms each. Break-even is ~12 key nodes, so use scipy for larger
# problems (more bonus stops) and Dijkstra for smaller ones.
_SCIPY_THRESHOLD = 12
t_lb0 = time.time()
LB = {}

if _SCIPY_AVAILABLE and len(key_nodes) >= _SCIPY_THRESHOLD:
    _pids_sorted = sorted(planets.keys())
    _pid_to_i = {pid: i for i, pid in enumerate(_pids_sorted)}
    _N = len(_pids_sorted)
    _mat = _np.full((_N, _N), _np.inf, dtype=_np.float64)
    _np.fill_diagonal(_mat, 0.0)
    for _a in _pids_sorted:
        if _a in FORBIDDEN:
            continue
        for _b, _w in ADJ[_a]:
            if _b in FORBIDDEN:
                continue
            _mat[_pid_to_i[_a], _pid_to_i[_b]] = _w
    _all_pairs = _scipy_shortest_path(_mat, method="D", directed=False)
    for src in key_nodes:
        si = _pid_to_i[src]
        LB[src] = {dst: float(_all_pairs[si, _pid_to_i[dst]]) for dst in key_nodes}
    if VERBOSE:
        solver_log(
            f"LB matrix: scipy all-pairs ({_N} nodes, {len(key_nodes)} key) "
            f"in {(time.time()-t_lb0)*1000:.1f}ms"
        )
else:
    for ni, src in enumerate(key_nodes):
        d = dijkstra_all(src)
        LB[src] = {dst: d.get(dst, float("inf")) for dst in key_nodes}
        if VERBOSE:
            step = max(1, len(key_nodes) // 10)
            if len(key_nodes) <= 12 or ni + 1 == len(key_nodes) or (ni + 1) % step == 0:
                solver_log(
                    f"LB matrix: Dijkstra all-pairs sources {ni + 1}/{len(key_nodes)} "
                    f"(planet id {src})"
                )

t_lb = time.time() - t_lb0
if VERBOSE:
    _lb_method = "scipy" if (_SCIPY_AVAILABLE and len(key_nodes) >= _SCIPY_THRESHOLD) else "dijkstra"
    solver_log(f"LB matrix done in {t_lb * 1000:.1f} ms ({_lb_method})")


# Cheapest single LB-edge into each key node (used for bonus-detour bound).
min_in = {k: min(LB[other][k] for other in key_nodes if other != k) for k in key_nodes}


# Held-Karp on the LB matrix: exact min-cost cycle through
# (current, all of remaining_mandatory, START) using LB as edge weights.
# This is admissible (LB <= real), so the TSP-on-LB <= real-routed-tour.
# Memoised by (current, frozenset(remaining)).
_hk_cache = {}


def hk_lb(current, remaining):
    """Min cost to visit every node in `remaining` exactly once starting from
    `current`, ending at START. Uses LB matrix. Returns 0 if remaining empty
    (just the LB[current][START] edge). Standard bitmask DP, but `remaining`
    is small (<=10) so we use frozenset memoisation."""
    if not remaining:
        return LB[current][START]
    key = (current, remaining)
    if key in _hk_cache:
        return _hk_cache[key]
    best = float("inf")
    for nxt in remaining:
        c = LB[current][nxt] + hk_lb(nxt, remaining - {nxt})
        if c < best:
            best = c
    _hk_cache[key] = best
    return best


# Bitmask indexing for the tight admissible lower bound below.
_M_INDEX = {m: i for i, m in enumerate(mandatory_list)}
_B_INDEX = {b: i for i, b in enumerate(bonus_list)}
_FULL_M_MASK = (1 << len(mandatory_list)) - 1


# DP on the LB matrix: f(current, vm_mask, vb_mask) = minimum LB-net cost to
# complete a tour from `current` that visits every still-unvisited mandatory
# and any *optimal subset* of still-unvisited bonuses, returning to START.
# "Net" here means sum of LB segment costs minus the values of bonuses picked
# along the way. Because LB[a][b] <= the real `dijkstra_avoiding` cost under
# any exclusion set, and bonus values are exact, this is a sound lower bound
# on the real future net contribution. State space is len(key_nodes) * 2^|M|
# * 2^|B|, which is tiny for typical instances (<= ~11 * 64 * 16).
_f_cache: dict = {}


def _f_lb(current, vm_mask, vb_mask):
    key = (current, vm_mask, vb_mask)
    cached = _f_cache.get(key)
    if cached is not None:
        return cached
    if vm_mask == _FULL_M_MASK:
        best = LB[current][START]
    else:
        best = float("inf")
    for i, m in enumerate(mandatory_list):
        if vm_mask & (1 << i):
            continue
        c = LB[current][m] + _f_lb(m, vm_mask | (1 << i), vb_mask)
        if c < best:
            best = c
    for j, b in enumerate(bonus_list):
        if vb_mask & (1 << j):
            continue
        c = LB[current][b] - BONUSES[b] + _f_lb(b, vm_mask, vb_mask | (1 << j))
        if c < best:
            best = c
    _f_cache[key] = best
    return best


def lower_bound(current, visited_mandatory, visited_bonuses, current_cost, bonus_collected_value):
    """Admissible *and tight* lower bound on final net cost.

    Uses a DP on the LB matrix that already accounts for the optimal subset
    and ordering of remaining bonuses (with their values subtracted at the
    point of insertion). Because:
      * each LB edge <= the real segment cost under any exclusion set, and
      * bonus values are exact and only credited if the bonus is in the path,
    the DP value is <= the real minimum future net contribution. We then add
    `current_cost - bonus_collected_value` (already realised) to get a sound
    lower bound on the *final* net.

    Previous formulations were either inadmissible (`2*min_in[b]` detour
    assumption was not a valid floor on insertion cost) or admissible but
    very loose (subtract sum of all unvisited bonus values, regardless of
    where they fit in the tour). This one is provably admissible *and* much
    tighter, which dramatically reduces B&B node count.
    """
    vm_mask = 0
    for m in visited_mandatory:
        idx = _M_INDEX.get(m)
        if idx is not None:
            vm_mask |= 1 << idx
    vb_mask = 0
    for b in visited_bonuses:
        idx = _B_INDEX.get(b)
        if idx is not None:
            vb_mask |= 1 << idx
    return current_cost - bonus_collected_value + _f_lb(current, vm_mask, vb_mask)


def search(current, used_planets, segments, current_cost,
           visited_mandatory, visited_bonuses, bonus_value):
    """DFS with pruning. `segments` is list of segment paths so far."""
    global best_net, best_full, best_gross, best_bonuses_taken, nodes_explored
    nodes_explored += 1
    if VERBOSE and LOG_INTERVAL and nodes_explored % LOG_INTERVAL == 0:
        solver_log(
            f"B&B nodes={nodes_explored:,} best_net={best_net:.2f} "
            f"cur={current} mand={len(visited_mandatory)}/{len(mandatory_set)} "
            f"bonus_pick={len(visited_bonuses)}/{len(bonus_set)} gross_so_far={current_cost:.2f}"
        )

    # Optimistic prune
    if lower_bound(current, visited_mandatory, visited_bonuses, current_cost, bonus_value) >= best_net:
        return

    remaining_m = mandatory_set - visited_mandatory
    remaining_b = bonus_set - visited_bonuses

    # If all mandatory visited, consider closing the tour by returning to start.
    if not remaining_m:
        # Try returning. Excluded = used + every remaining key (we won't visit them)
        excluded = set(used_planets)
        for kn in remaining_b:
            excluded.add(kn)
        excluded.discard(current)
        excluded.discard(START)
        cost, seg = dijkstra_avoiding(current, START, excluded)
        if cost is not None:
            total_gross = current_cost + cost
            net = total_gross - bonus_value
            if net < best_net:
                best_net = net
                best_gross = total_gross
                best_bonuses_taken = frozenset(visited_bonuses)
                full = []
                for s in segments:
                    if not full:
                        full.extend(s)
                    else:
                        full.extend(s[1:])
                full.extend(seg[1:])
                best_full = full

    # Branch: pick next key node. Order children by LB[current][next] ascending
    # so cheap branches go first - improves pruning.
    candidates = list(remaining_m) + list(remaining_b)
    candidates.sort(key=lambda k: LB[current][k])

    for nxt in candidates:
        # Exclude all OTHER unvisited keys + used planets so segment doesn't accidentally pass through them
        excluded = set(used_planets)
        for kn in remaining_m:
            if kn != nxt: excluded.add(kn)
        for kn in remaining_b:
            if kn != nxt: excluded.add(kn)
        excluded.discard(current)
        excluded.discard(nxt)

        cost, seg = dijkstra_avoiding(current, nxt, excluded)
        if seg is None:
            continue

        new_cost = current_cost + cost
        if new_cost - bonus_value >= best_net:
            continue  # this segment alone already too expensive

        new_used = set(used_planets)
        for p in seg[1:-1]:
            new_used.add(p)
        new_used.add(current)  # current is now consumed too

        new_segments = segments + [seg]

        if nxt in mandatory_set:
            new_vm = visited_mandatory | {nxt}
            new_vb = visited_bonuses
            new_bv = bonus_value
        else:
            new_vm = visited_mandatory
            new_vb = visited_bonuses | {nxt}
            new_bv = bonus_value + BONUSES[nxt]

        search(nxt, new_used, new_segments, new_cost,
               new_vm, new_vb, new_bv)


# Greedy seed: visit all mandatory in nearest-LB order, no bonuses, to get
# a non-trivial initial best_net for pruning.
def greedy_seed():
    global best_net, best_full, best_gross, best_bonuses_taken
    cur = START
    used = set()
    full = [START]
    total = 0.0
    remaining = set(mandatory_list)
    while remaining:
        nxt = min(remaining, key=lambda m: LB[cur][m])
        excluded = set(used)
        for kn in remaining:
            if kn != nxt: excluded.add(kn)
        for kn in bonus_set:
            excluded.add(kn)
        excluded.discard(cur); excluded.discard(nxt)
        cost, seg = dijkstra_avoiding(cur, nxt, excluded)
        if seg is None:
            return  # greedy failed, leave best_net at inf
        total += cost
        for p in seg[1:-1]:
            used.add(p)
        used.add(cur)
        full.extend(seg[1:])
        remaining.discard(nxt)
        cur = nxt
    excluded = set(used)
    for kn in bonus_set:
        excluded.add(kn)
    excluded.discard(cur); excluded.discard(START)
    cost, seg = dijkstra_avoiding(cur, START, excluded)
    if seg is None:
        return
    total += cost
    full.extend(seg[1:])
    best_net = total
    best_gross = total
    best_full = full
    best_bonuses_taken = frozenset()


def calculate_coaxium_for_route_ids(route_ids):
    """POST greedy/final route to CalculateCoaxium. Returns (result_dict, None) or (None, err_msg)."""
    cid = resolved_challenge_id(CHALLENGE, ARGS)
    if cid is None:
        return None, "missing ChallengeId (challenge JSON or --challenge-id)"
    try:
        base_url, headers = api_connection(ARGS)
    except ValueError as exc:
        return None, str(exc)
    submission = build_submission_route(route_ids, planets)
    q = urlencode({"ChallengeId": str(cid)})
    try:
        res = post_star_delivery_json(base_url, f"CalculateCoaxium?{q}", submission, headers)
    except RuntimeError as exc:
        return None, str(exc)
    return res, None


if VERBOSE:
    _v_cid = CHALLENGE.get("challengeId")
    _cid_note = f" ChallengeId={_v_cid}" if _v_cid is not None else " ChallengeId=(n/a)"
    solver_log(
        f"Problem size: routing_nodes={len(ROUTING_NODES)} allowed_waypoints={len(allowed)} "
        f"key_nodes={len(key_nodes)} (mandatory={len(mandatory_list)}, bonus={len(bonus_list)})"
        f"{_cid_note}"
    )

if VERBOSE:
    solver_log("Greedy seed (mandatory only, no bonuses)...")
t_bb0 = time.time()
greedy_seed()
if ARGS.greedy_seed_coaxium and best_full is not None:
    local_greedy_net = best_net
    api_res, api_err = calculate_coaxium_for_route_ids(best_full)
    if api_res is not None:
        api_coax = float(api_res["coaxium"])
        best_net = api_coax
        best_gross = api_coax
        print(
            f"Greedy seed: OutSystems coaxium={api_coax:g} used for pruning "
            f"(local summed net was {local_greedy_net:.2f}; "
            f"API success={api_res['is_success']})",
            file=sys.stderr,
            flush=True,
        )
        if api_res.get("feedback_message"):
            solver_log(f"CalculateCoaxium (greedy seed): {api_res['feedback_message'][:300]}")
    else:
        print(
            f"--greedy-seed-coaxium: {api_err}; keeping local greedy bound {local_greedy_net:.2f}",
            file=sys.stderr,
            flush=True,
        )
elif ARGS.greedy_seed_coaxium and best_full is None:
    print(
        "--greedy-seed-coaxium: greedy seed failed (no route); cannot call API",
        file=sys.stderr,
        flush=True,
    )

if VERBOSE:
    if best_net < float("inf"):
        solver_log(
            f"Greedy seed OK: best_net={best_net:.2f} gross={best_gross:.2f} "
            f"(pruning bound for B&B)"
        )
    else:
        solver_log("Greedy seed failed — starting B&B with best_net=inf (weak pruning)")
if VERBOSE:
    if LOG_INTERVAL:
        solver_log(f"Branch-and-bound search (progress every {LOG_INTERVAL:,} nodes)")
    else:
        solver_log("Branch-and-bound search (--log-interval 0: no periodic node logs)")
search(START, set(), [[START]], 0.0, frozenset(), frozenset(), 0.0)

best_bonuses = tuple(sorted(best_bonuses_taken))
t_bb = time.time() - t_bb0

if best_full is None:
    _dump_fail = (ARGS.dump_result or "").strip()
    if _dump_fail:
        try:
            with open(_dump_fail, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "success": False,
                        "challengeId": resolved_challenge_id(CHALLENGE, ARGS),
                        "error": "No feasible tour",
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError as exc:
            print(f"Warning: could not write --dump-result: {exc}", file=sys.stderr)
    print("No feasible tour.", file=sys.stderr if QUIET_REPORT else sys.stdout)
    sys.exit(1)

vc = {}
for p in best_full: vc[p] = vc.get(p, 0) + 1
revisits = {
    p: c
    for p, c in vc.items()
    if c > 1 and p != START and p not in FORBIDDEN
}
forbidden_transit = sorted({p for p in best_full if p in FORBIDDEN})
mandatory_missing = [m for m in MANDATORY if m not in vc]

def name(pid): return planets[pid]["name"]

_report_cid = CHALLENGE.get("challengeId")
_report_name = CHALLENGE.get("challengeName")

if not QUIET_REPORT:
    print("=" * 70)
    print("TSP SOLUTION - STRICT (no revisits)")
    print("=" * 70)
    if _report_cid is not None:
        print(f"ChallengeId: {_report_cid}")
    else:
        print("ChallengeId: (not set — add to challenge JSON or use --challenge-id for API)")
    if _report_name:
        print(f"ChallengeName: {_report_name}")
    print(f"\nStart/End:  {name(START)} ({START})")
    print(f"Mandatory:  {[(p, name(p)) for p in mandatory_list] or 'none'}")
    print(f"Forbidden:  {[(p, name(p)) for p in sorted(FORBIDDEN)] or 'none'}")
    print(f"\nBonus planets:")
    for bid in sorted(BONUSES):
        mark = "TAKEN" if bid in best_bonuses else "skip "
        print(f"  [{mark}] {name(bid):20s} ({bid:3d})  value={BONUSES[bid]}")
    print(f"\nFull path ({len(best_full)} hops, {len(set(best_full))} unique):")
    for i, p in enumerate(best_full):
        tag = ""
        if p == START and (i == 0 or i == len(best_full) - 1):
            tag = " (START/END)"
        elif p in MANDATORY:
            tag = " (mandatory)"
        elif p in BONUSES:
            tag = f" (bonus +{BONUSES[p]})"
        print(f"  {i:3d} {'*' if p in key_set else ' '} {p:4d}  {name(p)}{tag}")
    print()
    print(f"Full path planet ids ({len(best_full)} stops): {json.dumps(best_full, separators=(',', ':'))}")
    print()
    print(f"Gross fuel:    {best_gross:12.2f}")
    print(f"Bonus value:   {sum(BONUSES[b] for b in best_bonuses):12.2f}")
    print(f"NET fuel:      {best_net:12.2f}")
    print()
    print("VALIDATION:")
    if forbidden_transit:
        print(
            f"  ERROR forbidden in path: {forbidden_transit} "
            "(solver invariant broken — report as bug)"
        )
    else:
        print("  Forbidden in path:    none (required)")
    print(f"  Mandatory missing:    {mandatory_missing if mandatory_missing else 'none'}")
    print(f"  Revisits (non-start): {revisits if revisits else 'none'}")
    print(f"  Start visits:         {vc.get(START, 0)} (expected 2)")
    print()
    _lb_method_label = "scipy" if (_SCIPY_AVAILABLE and len(key_nodes) >= _SCIPY_THRESHOLD) else "dijkstra"
    print(f"Lower-bound matrix: {t_lb*1000:.1f}ms ({_lb_method_label}, {len(key_nodes)} key nodes)")
    _cache_total = len(_dijkstra_cache)
    print(f"Dijkstra cache:     {_cache_total} unique (src,dst,excluded) entries")
    print(f"B&B search (greedy + DFS): {nodes_explored} nodes explored in {t_bb*1000:.1f}ms")
    print(f"Total solve: {(t_lb+t_bb)*1000:.1f}ms")
    if VERBOSE:
        solver_log(
            f"Finished: nodes_explored={nodes_explored:,} best_net={best_net:.2f} "
            f"LB={t_lb*1000:.1f}ms B&B={t_bb*1000:.1f}ms dijkstra_cache={_cache_total}"
        )

_dump_ok = (ARGS.dump_result or "").strip()
if _dump_ok:
    try:
        _cid_dump = resolved_challenge_id(CHALLENGE, ARGS)
        _solve_s = float(t_lb) + float(t_bb)
        with open(_dump_ok, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "success": True,
                    "challengeId": _cid_dump,
                    "route": list(best_full),
                    "bestNet": best_net,
                    "bestGross": best_gross,
                    "bonusTaken": list(best_bonuses),
                    "solveSeconds": round(_solve_s, 6),
                    "lowerBoundSeconds": round(float(t_lb), 6),
                    "branchBoundSeconds": round(float(t_bb), 6),
                    "nodesExplored": nodes_explored,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as exc:
        print(f"Warning: could not write --dump-result: {exc}", file=sys.stderr)

if ARGS.calculate_coaxium or ARGS.submit:
    cid = resolved_challenge_id(CHALLENGE, ARGS)
    if cid is None:
        print(
            "\nOutSystems: missing ChallengeId. Add ChallengeId to challenge JSON "
            "or pass --challenge-id.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        base_url, headers = api_connection(ARGS)
    except ValueError as exc:
        print(f"\nOutSystems: {exc}", file=sys.stderr)
        sys.exit(1)
    if ARGS.calculate_coaxium:
        if not QUIET_REPORT:
            print("\n" + "=" * 70)
            print("OUTSYSTEMS: CalculateCoaxium (final route)")
            print(f"  ChallengeId: {cid}")
            print("=" * 70)
        try:
            calc = api_calculate_coaxium(base_url, headers, cid, best_full, planets)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if not QUIET_REPORT:
            print(f"  Success:         {calc['is_success']}")
            print(f"  Coaxium:         {calc['coaxium']}")
            print(f"  Feedback:        {calc['feedback_message']}")
            if calc["time_elapsed_in_seconds"] is not None:
                print(f"  TimeElapsed(s):  {calc['time_elapsed_in_seconds']}")
    if ARGS.submit:
        if not QUIET_REPORT:
            print("\n" + "=" * 70)
            print("OUTSYSTEMS: SubmitChallengeSolution")
            print(f"  ChallengeId: {cid}")
            print("=" * 70)
        try:
            sub = api_submit_solution(base_url, headers, cid, best_full, planets)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        if not QUIET_REPORT:
            print(f"  Success:         {sub['is_success']}")
            print(f"  Coaxium:         {sub['coaxium']}")
            print(f"  Feedback:        {sub['feedback_message']}")
            if sub["time_elapsed_in_seconds"] is not None:
                print(f"  TimeElapsed(s):  {sub['time_elapsed_in_seconds']}")
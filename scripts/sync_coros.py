import argparse
import hashlib
import hmac
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from sync_scope import activity_scope_from_config, start_after_ts
from utils import ensure_dir, load_config, raw_activity_dir, read_json, utc_now, write_json

BASE_URLS = {
    "eu": "https://teameuapi.coros.com",
    "us": "https://teamapi.coros.com",
    "cn": "https://teamcnapi.coros.com",
}
SPORT_TYPES = {
    100: "Run",
    101: "VirtualRun",
    102: "TrailRun",
    103: "Run",
    104: "Hike",
    200: "Ride",
    201: "VirtualRide",
    202: "Ride",
    203: "Ride",
    204: "MountainBikeRide",
    205: "GravelRide",
    300: "Swim",
    301: "Swim",
    400: "Workout",
    401: "Workout",
    402: "WeightTraining",
    500: "AlpineSki",
    501: "Snowboard",
    502: "NordicSki",
    503: "BackcountrySki",
    800: "RockClimbing",
    801: "RockClimbing",
    802: "RockClimbing",
    900: "Walk",
    901: "Workout",
    903: "Elliptical",
    904: "Yoga",
    1000: "Badminton",
    10000: "Workout",
    10001: "Workout",
}
RAW_DIR = raw_activity_dir("coros")
STATE_PATH = os.path.join("data", "backfill_state_coros.json")
ACCOUNT_PATH = os.path.join("data", "athletes_coros.json")
SUMMARY_JSON = os.path.join("data", "last_sync_summary.json")
SUMMARY_TXT = os.path.join("data", "last_sync_summary.txt")
TOKEN_CACHE = ".coros_token.json"
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_REQUEST_ATTEMPTS = 5


def _base_url(region: str) -> str:
    normalized = str(region or "eu").strip().lower()
    if normalized not in BASE_URLS:
        allowed = ", ".join(sorted(BASE_URLS))
        raise ValueError(f"Unsupported COROS region '{normalized}'. Expected one of: {allowed}.")
    return BASE_URLS[normalized]


def _request_json(session: requests.Session, method: str, url: str, **kwargs) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            response = session.request(method, url, timeout=30, **kwargs)
            if response.status_code in TRANSIENT_STATUS_CODES and attempt < MAX_REQUEST_ATTEMPTS:
                retry_after = response.headers.get("Retry-After", "")
                delay = int(retry_after) if retry_after.isdigit() else min(30, 2 ** (attempt - 1))
                time.sleep(max(1, delay))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("COROS API returned a non-object JSON response.")
            return payload
        except requests.RequestException as exc:
            last_error = exc
            status = exc.response.status_code if getattr(exc, "response", None) is not None else None
            if status is not None and status not in TRANSIENT_STATUS_CODES:
                raise
            if attempt < MAX_REQUEST_ATTEMPTS:
                time.sleep(min(30, 2 ** (attempt - 1)))
    if last_error:
        raise last_error
    raise RuntimeError(f"COROS request failed after {MAX_REQUEST_ATTEMPTS} attempts: {url}")


def _unwrap(payload: Dict[str, Any], operation: str) -> Dict[str, Any]:
    result = str(payload.get("result", ""))
    if result not in {"0000", "0", ""}:
        message = payload.get("message") or payload.get("msg") or result
        raise RuntimeError(f"COROS {operation} failed: {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"COROS {operation} response did not contain a data object.")
    return data


def _login(session: requests.Session, account: str, password: str, region: str) -> Dict[str, str]:
    payload = _request_json(
        session,
        "POST",
        f"{_base_url(region)}/account/login",
        json={
            "account": account,
            "accountType": 2,
            "pwd": hashlib.md5(password.encode("utf-8")).hexdigest(),
        },
    )
    data = _unwrap(payload, "login")
    access_token = str(data.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError("COROS login succeeded but returned no access token.")
    return {
        "access_token": access_token,
        "user_id": str(data.get("userId") or ""),
    }


def _save_token_cache(access_token: str) -> None:
    write_json(
        TOKEN_CACHE,
        {
            "access_token": access_token,
            "updated_utc": utc_now().isoformat(),
        },
    )
    try:
        os.chmod(TOKEN_CACHE, 0o600)
    except OSError:
        pass


def _is_auth_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) in {401, 403}:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("access token", "accesstoken", "auth", "login", "session")
    )


def _fetch_page(
    session: requests.Session,
    access_token: str,
    region: str,
    page: int,
    size: int,
    start_day: Optional[str] = None,
    end_day: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"modeList": "", "pageNumber": page, "size": min(size, 200)}
    if start_day:
        params["startDay"] = start_day.replace("-", "")
    if end_day:
        params["endDay"] = end_day.replace("-", "")
    payload = _request_json(
        session,
        "GET",
        f"{_base_url(region)}/activity/query",
        headers={"accessToken": access_token},
        params=params,
    )
    return _unwrap(payload, "activity query")


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _start_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return _start_datetime(int(raw))
    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _normalize_activity(activity: Dict[str, Any]) -> Dict[str, Any]:
    activity_id = activity.get("labelId") or activity.get("activityId") or activity.get("id")
    start_value = (
        activity.get("startTime")
        or activity.get("startTimestamp")
        or activity.get("date")
        or activity.get("day")
    )
    started_at = _start_datetime(start_value)
    if activity_id in (None, "") or started_at is None:
        return {}
    sport_type = activity.get("sportType")
    try:
        sport_type_id = int(sport_type)
    except (TypeError, ValueError):
        sport_type_id = -1
    activity_type = SPORT_TYPES.get(sport_type_id, str(activity.get("sportName") or "Unknown"))
    name = str(activity.get("name") or activity.get("activityName") or "").strip()
    normalized = {
        "id": str(activity_id),
        "start_date_local": started_at.isoformat(),
        "start_date": started_at.isoformat(),
        "type": activity_type,
        "sport_type": activity_type,
        "provider_sport_type": sport_type_id,
        "distance": _safe_float(activity.get("distance") or activity.get("totalDistance")),
        "moving_time": _safe_float(
            activity.get("duration") or activity.get("totalTime") or activity.get("elapsedTime")
        ),
        "total_elevation_gain": _safe_float(
            activity.get("totalAscent") or activity.get("ascent") or activity.get("elevationGain")
        ),
        "provider": "coros",
    }
    if name:
        normalized["name"] = name
    return normalized


def _activity_timestamp(activity: Dict[str, Any]) -> Optional[int]:
    started_at = _start_datetime(activity.get("start_date_local"))
    return int(started_at.timestamp()) if started_at else None


def _write_activity(activity: Dict[str, Any]) -> bool:
    activity_id = str(activity.get("id") or "")
    if not activity_id or activity_id in {".", ".."} or "/" in activity_id or "\\" in activity_id:
        return False
    path = os.path.join(RAW_DIR, f"{activity_id}.json")
    if os.path.exists(path):
        try:
            if read_json(path) == activity:
                return False
        except Exception:
            pass
    write_json(path, activity)
    return True


def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        payload = read_json(STATE_PATH)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _account_fingerprint(account: str, password: str, region: str) -> str:
    identity = f"{region}:{account.strip().lower()}"
    return hmac.new(password.encode("utf-8"), identity.encode("utf-8"), hashlib.sha256).hexdigest()


def _maybe_reset_for_new_account(fingerprint: str) -> None:
    stored = None
    if os.path.exists(ACCOUNT_PATH):
        try:
            stored = read_json(ACCOUNT_PATH).get("fingerprint")
        except Exception:
            stored = None
    existing_outputs = (
        os.path.join("data", "activities_normalized.json"),
        os.path.join("data", "daily_aggregates.json"),
        SUMMARY_JSON,
        SUMMARY_TXT,
        os.path.join("site", "data.json"),
    )
    should_reset = bool(stored and stored != fingerprint)
    if stored is None and (os.path.exists(STATE_PATH) or os.path.exists(RAW_DIR)):
        should_reset = True
    if should_reset:
        print("Detected different COROS account; resetting persisted COROS data.")
        for path in (STATE_PATH, *existing_outputs):
            if os.path.exists(path):
                os.remove(path)
        if os.path.exists(RAW_DIR):
            shutil.rmtree(RAW_DIR)
    ensure_dir("data")
    write_json(
        ACCOUNT_PATH,
        {"fingerprint": fingerprint, "updated_utc": utc_now().isoformat(), "version": 1},
    )


def _sync_pages(
    session: requests.Session,
    token: str,
    region: str,
    per_page: int,
    after: int,
    dry_run: bool,
    *,
    start_page: int = 1,
    start_day: Optional[str] = None,
    end_day: Optional[str] = None,
) -> Dict[str, Any]:
    page = max(1, start_page)
    fetched = 0
    changed = 0
    fetched_ids = set()
    exhausted = False
    while True:
        data = _fetch_page(session, token, region, page, per_page, start_day, end_day)
        raw_items = data.get("dataList") or data.get("activities") or []
        if not isinstance(raw_items, list) or not raw_items:
            exhausted = True
            break
        reached_boundary = False
        for raw_activity in raw_items:
            if not isinstance(raw_activity, dict):
                continue
            activity = _normalize_activity(raw_activity)
            if not activity:
                continue
            timestamp = _activity_timestamp(activity)
            if timestamp is not None and timestamp < after:
                reached_boundary = True
                continue
            fetched += 1
            fetched_ids.add(activity["id"])
            if not dry_run and _write_activity(activity):
                changed += 1
        total_pages = int(data.get("totalPage") or 0)
        if reached_boundary or len(raw_items) < min(per_page, 200) or (total_pages and page >= total_pages):
            exhausted = True
            break
        page += 1
    return {
        "fetched": fetched,
        "new_or_updated": changed,
        "activity_ids": sorted(fetched_ids),
        "next_page": page + (1 if exhausted is False else 0),
        "exhausted": exhausted,
    }


def sync_coros(dry_run: bool, prune_deleted: bool) -> Dict[str, Any]:
    config = load_config()
    coros_cfg = config.get("coros", {}) or {}
    account = str(coros_cfg.get("email") or coros_cfg.get("account") or "").strip()
    password = str(coros_cfg.get("password") or "")
    configured_token = str(coros_cfg.get("access_token") or "").strip()
    region = str(coros_cfg.get("region") or "eu").strip().lower()
    if not account or not password:
        raise RuntimeError("COROS authentication requires coros.email and coros.password.")
    _base_url(region)

    sync_cfg = config.get("sync", {}) or {}
    per_page = max(1, min(int(sync_cfg.get("per_page", 200)), 200))
    recent_days = max(0, int(sync_cfg.get("recent_days", 7)))
    resume_backfill = bool(sync_cfg.get("resume_backfill", True))
    after = start_after_ts(config)
    scope = activity_scope_from_config(config)

    if not dry_run:
        _maybe_reset_for_new_account(_account_fingerprint(account, password, region))
    ensure_dir(RAW_DIR)

    session = requests.Session()
    token = configured_token
    login_performed = False
    if not token:
        credentials = _login(session, account, password, region)
        token = credentials["access_token"]
        login_performed = True
        _save_token_cache(token)
    today = utc_now().date()
    recent_start = today - timedelta(days=recent_days)
    try:
        recent = _sync_pages(
            session,
            token,
            region,
            per_page,
            after,
            dry_run,
            start_day=recent_start.isoformat() if recent_days else today.isoformat(),
            end_day=today.isoformat(),
        )
    except Exception as exc:
        if not configured_token or not _is_auth_error(exc):
            raise
        print("Cached COROS access token was rejected; logging in again.")
        credentials = _login(session, account, password, region)
        token = credentials["access_token"]
        login_performed = True
        _save_token_cache(token)
        recent = _sync_pages(
            session,
            token,
            region,
            per_page,
            after,
            dry_run,
            start_day=recent_start.isoformat() if recent_days else today.isoformat(),
            end_day=today.isoformat(),
        )

    state = _load_state() if resume_backfill and not dry_run else {}
    if state.get("after") != after or state.get("activity_scope") != scope:
        state = {}
    completed_before = bool(state.get("completed"))
    backfill = {
        "fetched": 0,
        "new_or_updated": 0,
        "activity_ids": [],
        "next_page": int(state.get("next_page") or 1),
        "exhausted": completed_before,
    }
    if not completed_before:
        backfill = _sync_pages(
            session,
            token,
            region,
            per_page,
            after,
            dry_run,
            start_page=int(state.get("next_page") or 1),
        )

    completed = bool(backfill["exhausted"])
    if not dry_run and resume_backfill:
        write_json(
            STATE_PATH,
            {
                "after": after,
                "activity_scope": scope,
                "completed": completed,
                "next_page": backfill["next_page"],
                "updated_utc": utc_now().isoformat(),
            },
        )

    fetched_ids = set(recent["activity_ids"]) | set(backfill["activity_ids"])
    deleted = 0
    can_prune = (
        prune_deleted
        and not dry_run
        and completed
        and not completed_before
        and int(state.get("next_page") or 1) == 1
    )
    if can_prune:
        for filename in os.listdir(RAW_DIR):
            if filename.endswith(".json") and filename[:-5] not in fetched_ids:
                os.remove(os.path.join(RAW_DIR, filename))
                deleted += 1
    elif prune_deleted and not dry_run:
        print("Skipping COROS deletion pruning because this run did not enumerate full history.")

    summary = {
        "source": "coros",
        "region": region,
        "login_performed": login_performed,
        "fetched": recent["fetched"] + backfill["fetched"],
        "new_or_updated": recent["new_or_updated"] + backfill["new_or_updated"],
        "deleted": deleted,
        "lookback_start_ts": after,
        "backfill_completed": completed,
        "backfill_next_page": backfill["next_page"],
        "recent_sync": recent,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync COROS Training Hub activities")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prune-deleted", action="store_true")
    args = parser.parse_args()
    config = load_config()
    prune_deleted = args.prune_deleted or bool(config.get("sync", {}).get("prune_deleted", False))
    summary = sync_coros(args.dry_run, prune_deleted)
    if not args.dry_run:
        ensure_dir("data")
        write_json(SUMMARY_JSON, summary)
        start_label = datetime.fromtimestamp(
            summary["lookback_start_ts"], tz=timezone.utc
        ).date().isoformat()
        message = (
            f"Sync COROS: {summary['new_or_updated']} new/updated, "
            f"{summary['deleted']} deleted (start {start_label})"
        )
        with open(SUMMARY_TXT, "w", encoding="utf-8") as handle:
            handle.write(message + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

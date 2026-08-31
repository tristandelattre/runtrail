#!/usr/bin/env python3
"""Push structured quality sessions from vers_beaufortain.html straight into
Garmin Connect (Entrainement > Mes entrainements) via the unofficial
`garminconnect` API, and schedule each one on its plan date.

This is a personal automation script, not part of the deployed app. It never
sends your Garmin credentials anywhere except connect.garmin.com.

Setup (once):
    pip install garminconnect pydantic
    export GARMIN_EMAIL="you@example.com"
    export GARMIN_PASSWORD="..."          # only needed for the very first run

The first login may ask for an MFA code (typed interactively). After that,
a session token is cached at ~/.garminconnect_runtrail so you won't need
your password or MFA again until the token expires (Garmin tokens last
several months).

Usage:
    # See what would be sent, without touching your Garmin account:
    python3 scripts/garmin_push.py --dates 2026-09-08 --dry-run

    # Push one session for real, and schedule it on that date:
    python3 scripts/garmin_push.py --dates 2026-09-08

    # Push several sessions at once:
    python3 scripts/garmin_push.py --dates 2026-09-08 2026-09-11 2026-09-14

    # Push every remaining quality session that has garminSteps data:
    python3 scripts/garmin_push.py --all-quality

Notes / known simplifications (reverse-engineered API, no official docs):
  - Every step is uploaded as a TIME-based step (Garmin's numeric ID for a
    "distance" end-condition isn't reliably documented publicly, so instead
    of risking a silently wrong workout, distance intervals are converted to
    an equivalent time using the target pace). The pace target itself is
    still enforced, so on the watch this behaves almost identically to a
    true distance interval.
  - "Open" (lap-button / no fixed duration) recovery steps are given a
    3-minute placeholder duration since there's no verified numeric ID for
    that end-condition either. Adjust EF_OPEN_FALLBACK_SEC below if needed.
  - Always try one nearby session with --dry-run, then a real push, and
    check it in Garmin Connect > Entrainement before doing a bulk push.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = REPO_ROOT / "vers_beaufortain.html"
TOKENSTORE = str(Path.home() / ".garminconnect_runtrail")
OPEN_FALLBACK_SEC = 180

# Must stay in sync with PACE_TABLE in vers_beaufortain.html
PACE_TABLE = {
    "EF": "5'15-5'45",
    "Fac": "5'30-6'00",
    "AS": "4'30",
    "MA": "4'44",
    "Seuil": "4'20-4'25",
    "A10": "4'10-4'15",
    "A5": "3'45-4'00",
    "Cotes": "4'00-4'10",
}


def pace_to_mps(s: str) -> float:
    m = re.match(r"(\d+)'(\d+)", s.strip())
    if not m:
        raise ValueError(f"unrecognised pace string: {s!r}")
    minutes, seconds = int(m.group(1)), int(m.group(2))
    return 1000.0 / (minutes * 60 + seconds)


def pace_range_to_mps(range_str: str) -> tuple[float, float]:
    parts = range_str.split("-")
    if len(parts) == 1:
        s = pace_to_mps(parts[0])
        return (s * 0.97, s * 1.03)
    s1, s2 = pace_to_mps(parts[0]), pace_to_mps(parts[1])
    return (min(s1, s2), max(s1, s2))


def load_plan() -> dict:
    text = HTML_PATH.read_text(encoding="utf-8")
    match = re.search(r"const PLAN = (\{.*\});", text)
    if not match:
        raise RuntimeError("could not find `const PLAN = {...};` in vers_beaufortain.html")
    return json.loads(match.group(1))


def find_sessions(plan: dict, dates: set[str] | None) -> list[tuple[str, str, list[dict]]]:
    """Return (date, text, garminSteps) for every matching day."""
    found = []
    for block in plan["blocks"]:
        for week in block["weeks"]:
            for day in week["days"]:
                steps = day.get("garminSteps")
                if not steps:
                    continue
                if dates is not None and day["date"] not in dates:
                    continue
                found.append((day["date"], day["text"], steps))
    found.sort(key=lambda t: t[0])
    return found


def build_running_workout(name: str, date: str, steps: list[dict]):
    from garminconnect.workout import (
        ConditionType,
        RunningWorkout,
        StepType,
        TargetType,
        WorkoutSegment,
    )

    def target_type_dict(step: dict):
        if step.get("target") == "pace":
            return {
                "workoutTargetTypeId": TargetType.SPEED,
                "workoutTargetTypeKey": "speed.zone",
                "displayOrder": TargetType.SPEED,
            }
        return {
            "workoutTargetTypeId": TargetType.NO_TARGET,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": TargetType.NO_TARGET,
        }

    def duration_seconds(step: dict) -> float:
        if step["durationType"] == "time":
            return float(step["durationValue"])
        if step["durationType"] == "distance":
            pace_key = step.get("pace")
            range_str = PACE_TABLE.get(pace_key, pace_key)
            low, high = pace_range_to_mps(range_str)
            mid = (low + high) / 2
            return round(step["durationValue"] / mid, 1)
        return float(OPEN_FALLBACK_SEC)  # open/lap-button steps

    step_type_by_intensity = {
        "warmup": (StepType.WARMUP, "warmup", 1),
        "cooldown": (StepType.COOLDOWN, "cooldown", 2),
        "active": (StepType.INTERVAL, "interval", 3),
        "rest": (StepType.RECOVERY, "recovery", 4),
    }

    def to_executable(step: dict, order: int):
        step_id, step_key, display = step_type_by_intensity.get(
            step.get("intensity"), (StepType.INTERVAL, "interval", 3)
        )
        payload = {
            "stepOrder": order,
            "stepType": {"stepTypeId": step_id, "stepTypeKey": step_key, "displayOrder": display},
            "endCondition": {
                "conditionTypeId": ConditionType.TIME,
                "conditionTypeKey": "time",
                "displayOrder": 2,
                "displayable": True,
            },
            "endConditionValue": duration_seconds(step),
            "targetType": target_type_dict(step),
        }
        if step.get("target") == "pace":
            pace_key = step.get("pace")
            range_str = PACE_TABLE.get(pace_key, pace_key)
            low, high = pace_range_to_mps(range_str)
            payload["targetValueOne"] = round(low, 3)
            payload["targetValueTwo"] = round(high, 3)
        return payload

    # Group the flat step list into top-level items, expanding {"repeat": true, "from": i, "times": n}
    # markers into RepeatGroupDTO wrapping the steps starting at original index i.
    top_level: list[dict] = []
    orig_index_of: list[int] = []
    order = 1
    for idx, step in enumerate(steps):
        if step.get("repeat"):
            start = step["from"]
            pos = orig_index_of.index(start)
            children = top_level[pos:]
            del top_level[pos:]
            del orig_index_of[pos:]
            group = {
                "type": "RepeatGroupDTO",
                "stepOrder": order,
                "stepType": {"stepTypeId": StepType.REPEAT, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": step["times"],
                "workoutSteps": children,
                "endCondition": {
                    "conditionTypeId": ConditionType.ITERATIONS,
                    "conditionTypeKey": "iterations",
                    "displayOrder": 7,
                    "displayable": False,
                },
                "endConditionValue": float(step["times"]),
            }
            top_level.append(group)
            orig_index_of.append(start)
            order += 1
            continue
        top_level.append(to_executable(step, order))
        orig_index_of.append(idx)
        order += 1

    total_secs = 0.0
    for item in steps:
        if item.get("repeat"):
            continue
        total_secs += duration_seconds(item)

    return RunningWorkout(
        workoutName=f"{date} - {name}"[:60],
        estimatedDurationInSecs=int(total_secs),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
                workoutSteps=top_level,
            )
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dates", nargs="+", metavar="YYYY-MM-DD", help="only push these dates")
    group.add_argument("--all-quality", action="store_true", help="push every session that has garminSteps data")
    parser.add_argument("--dry-run", action="store_true", help="print the payloads, don't call the Garmin API")
    args = parser.parse_args()

    plan = load_plan()
    dates = set(args.dates) if args.dates else None
    sessions = find_sessions(plan, dates)

    if not sessions:
        print("No matching session with garminSteps found.")
        sys.exit(1)

    print(f"{len(sessions)} session(s) matched:")
    for date, text, _ in sessions:
        print(f"  {date}  {text}")
    print()

    workouts = [(date, build_running_workout(text, date, steps)) for date, text, steps in sessions]

    if args.dry_run:
        for date, w in workouts:
            print(f"--- {date} ---")
            print(json.dumps(w.to_dict(), indent=2, ensure_ascii=False))
        return

    from garminconnect import Garmin

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    api = Garmin(email=email, password=password)
    api.login(tokenstore=TOKENSTORE)

    for date, workout in workouts:
        try:
            result = api.upload_running_workout(workout)
            workout_id = result.get("workoutId") or result.get("workoutID") or result.get("id")
            if workout_id is None:
                print(f"[{date}] uploaded but no workoutId in response: {result}")
                continue
            api.schedule_workout(workout_id, date)
            print(f"[{date}] uploaded and scheduled (workoutId={workout_id})")
        except Exception as e:
            print(f"[{date}] FAILED: {e}")


if __name__ == "__main__":
    main()

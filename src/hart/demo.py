"""Demo data: a fictional athlete ("Alex") with six months of training.

``hart demo`` builds a separate database so you can try hart — or take
screenshots — without a Garmin account.  Everything is generated from a fixed
random seed; no real person's data is involved.  Derived tables (training
load, recovery, views, phases, health checks) are computed by the same code as
for real data; grades, the suggestion and the chat are scripted examples of
what Ember produces.
"""

from __future__ import annotations

import datetime
import json
import math
import random
import tempfile
from pathlib import Path
from typing import Any

from hart.storage.database import Database
from hart.storage.schema import init_schema

D = datetime.date
DAY = datetime.timedelta(days=1)
DAYS = 180

# Weekly template: (weekday, sport, sub_type, minutes, intensity factor 0..1, name)
WEEK = [
    (0, "swim", "lap_swimming", 45, 0.55, "Technique swim"),
    (1, "strength", "strength_training", 50, 0.5, "Strength A"),
    (1, "run", "running", 40, 0.6, "Easy run"),
    (2, "bike", "indoor_cycling", 70, 0.72, "Sweet spot intervals"),
    (3, "swim", "lap_swimming", 60, 0.65, "Coached swim"),
    (3, "run", "running", 50, 0.75, "Tempo run"),
    (4, "strength", "strength_training", 45, 0.5, "Strength B"),
    (5, "bike", "road_biking", 150, 0.62, "Long ride"),
    (6, "run", "running", 80, 0.62, "Long run"),
]
LIFTS = [("BARBELL_DEADLIFT", "DEADLIFT", 70.0, 5), ("BARBELL_BACK_SQUAT", "SQUAT", 60.0, 6),
         ("BARBELL_BENCH_PRESS", "BENCH_PRESS", 45.0, 8), ("PULL_UP", "PULL_UP", 0.0, 7),
         ("STANDING_BARBELL_CALF_RAISE", "CALF_RAISE", 40.0, 12)]


def _seed_files(folder: Path, today: D) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    a_race = today + datetime.timedelta(days=235)
    (folder / "races.json").write_text(json.dumps([
        {"name": "Example Ironman", "race_date": a_race.isoformat(), "distance": "full", "priority": "A",
         "notes": "First full distance."},
        {"name": "Spring Half", "race_date": (a_race - datetime.timedelta(days=84)).isoformat(),
         "distance": "half", "priority": "B"},
    ]))
    (folder / "annotations.json").write_text(json.dumps([
        {"kind": "event", "label": "Training camp", "start_date": (today - datetime.timedelta(days=60)).isoformat(),
         "end_date": (today - datetime.timedelta(days=54)).isoformat()},
        {"kind": "illness", "label": "Cold", "start_date": (today - datetime.timedelta(days=100)).isoformat(),
         "end_date": (today - datetime.timedelta(days=95)).isoformat()},
    ]))
    notes = json.loads((Path(__file__).resolve().parents[2] / "examples" / "seed" / "athlete_notes.json")
                       .read_text(encoding="utf-8")) if (Path(__file__).resolve().parents[2] / "examples").is_dir() else []
    for n in notes:
        if n["title"].startswith("A-race"):
            n["body"] = "First full-distance race, on " + a_race.isoformat() + ". Priorities: durability, pacing, fueling."
        n.pop("valid_from", None)
        if "rules" in n and "planned_labs" in n["rules"]:
            n["rules"] = {"planned_labs": [{"markers": ["tsh"],
                                            "due": (today + datetime.timedelta(days=20)).isoformat(),
                                            "after": (today - datetime.timedelta(days=40)).isoformat()}]}
    (folder / "athlete_notes.json").write_text(json.dumps(notes))
    labs = []
    for months_ago, hb, ferritin, tsh, vitd in ((13, 15.8, 95, 2.4, 34), (7, 15.2, 71, 3.1, 24), (1, 14.9, 58, 3.9, 27)):
        day = today - datetime.timedelta(days=30 * months_ago + 10)
        labs.append({"date": day.isoformat(), "markers": [
            {"name": "Hemoglobin", "value": str(hb), "unit": "g/dl", "reference_range": "13.5 - 18"},
            {"name": "Hematocrit", "value": str(round(hb * 2.95, 1)), "unit": "%", "reference_range": "40 - 52"},
            {"name": "Ferritin", "value": str(ferritin), "unit": "ng/ml", "reference_range": "30 - 400"},
            {"name": "TSH", "value": str(tsh), "unit": "µIU/ml", "reference_range": "0.27 - 4.2"},
            {"name": "Vitamin D 25(OH)", "value": str(vitd), "unit": "ng/ml", "reference_range": "30 - 100",
             **({"flag": "L"} if vitd < 30 else {})},
            {"name": "CRP", "value": "1.2", "unit": "mg/l", "reference_range": "0 - 5"},
            {"name": "Creatinine", "value": "1.0", "unit": "mg/dl", "reference_range": "0.7 - 1.2"},
            {"name": "Glucose", "value": "88", "unit": "mg/dl", "reference_range": "70 - 99"},
        ]})
    (folder / "lab_results.json").write_text(json.dumps(labs))


def _activities(db: Database, rng: random.Random, today: D) -> list[str]:
    ids = []
    for offset in range(DAYS, -1, -1):
        day = today - offset * DAY
        fitness = 0.55 + 0.45 * (1 - offset / DAYS)  # gets fitter over the six months
        ill = 95 <= offset <= 100
        for weekday, sport, sub, minutes, intensity, name in WEEK:
            if day.weekday() != weekday or ill or (offset == 0 and weekday != day.weekday()):
                continue
            if rng.random() < 0.08:  # the odd missed session
                continue
            if offset == 0 and sport != "strength":  # today: only the morning gym session so far
                continue
            block_week = (offset // 7) % 4
            scale = 0.7 if block_week == 0 else 1.0 + 0.08 * (3 - block_week)  # recovery week every 4th
            secs = int(minutes * 60 * scale * rng.uniform(0.9, 1.1))
            start = datetime.datetime.combine(day, datetime.time(7 if sport != "bike" else 9, rng.randint(0, 50)))
            aid = f"demo_{day:%Y%m%d}_{sport}_{weekday}"
            hr = int(118 + 40 * intensity * rng.uniform(0.9, 1.05) - 6 * fitness)
            power = int((150 + 110 * intensity) * fitness * rng.uniform(0.95, 1.05)) if sport == "bike" else None
            pace = (360 - 70 * intensity - 40 * fitness) * rng.uniform(0.97, 1.03) if sport == "run" else None
            dist = (secs / pace * 1000 if pace else (secs / 3600 * (26 + 8 * intensity) * 1000 if sport == "bike"
                    else secs / 60 * 42 if sport == "swim" else None))
            load = round(secs / 3600 * 100 * intensity ** 1.6 * (0.6 if sport == "strength" else 1.0))
            db.execute(
                "INSERT INTO activities (activity_id, source, external_id, sport_type, sub_type, name, start_time, "
                "elapsed_seconds, moving_seconds, distance_meters, avg_hr, max_hr, avg_power, normalized_power, "
                "avg_cadence, avg_pace_sec_km, avg_speed_kmh, training_effect_aerobic, rpe, feel, garmin_training_load) "
                "VALUES (?, 'demo', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [aid, aid, sport, sub, name, start, secs + 120, secs, dist, hr, hr + rng.randint(15, 30), power,
                 int(power * 1.06) if power else None, 88 if sport == "bike" else 172 if sport == "run" else None,
                 pace, round(dist / 1000 / (secs / 3600), 1) if dist else None, round(2.2 + 1.8 * intensity, 1),
                 round(20 + 60 * intensity), 50 if intensity < 0.7 else 25, load],
            )
            z2 = 0.45 + 0.4 * (1 - intensity)
            zones = {"Z1": secs * 0.1, "Z2": secs * z2, "Z3": secs * (0.8 - z2) * 0.6, "Z4": secs * (0.8 - z2) * 0.35,
                     "Z5": secs * (0.8 - z2) * 0.05}
            ef = round(power / hr, 2) if power else (round((1000 / pace * 60) / hr, 3) if pace else None)
            db.execute(
                "INSERT INTO activity_metrics (activity_id, sport_type, date, tss, tss_method, hr_zone_seconds, "
                "efficiency_factor, aerobic_decoupling_pct) VALUES (?, ?, ?, ?, 'garmin', ?, ?, ?)",
                [aid, sport, day, load, json.dumps({k: round(v) for k, v in zones.items()}), ef,
                 round(rng.uniform(1.5, 7.5) * (1.2 - fitness), 1) if sport in ("bike", "run") and secs > 2700 else None],
            )
            if sport == "strength":
                progress = 1 + 0.18 * (1 - offset / DAYS)
                for i, (ex, cat, kg, reps) in enumerate(LIFTS):
                    for s in range(3):
                        # Garmin sometimes records only the category — hart merges those.
                        name_rec = None if (ex == "BARBELL_DEADLIFT" and rng.random() < 0.3) else ex
                        db.execute(
                            "INSERT INTO strength_sets (activity_id, set_index, set_type, repetitions, weight_kg, "
                            "exercise_category, exercise_name) VALUES (?, ?, 'active', ?, ?, ?, ?)",
                            [aid, i * 3 + s, reps, round(kg * progress / 2.5) * 2.5 if kg else 0.0, cat, name_rec])
            if offset <= 21 and sport in ("bike", "run"):
                _streams(db, rng, aid, secs, hr, power)
            ids.append(aid)
    return ids


def _streams(db: Database, rng: random.Random, aid: str, secs: int, hr: int, power: int | None) -> None:
    rows = []
    for t in range(0, secs, 5):
        drift = 4 * t / secs
        rows.append((aid, t, int(hr - 8 + drift + 5 * math.sin(t / 240) + rng.uniform(-2, 2)),
                     int(power + 25 * math.sin(t / 180) + rng.uniform(-10, 10)) if power else None))
    db.executemany("INSERT INTO activity_streams (activity_id, timestamp_sec, heart_rate, power) VALUES (?, ?, ?, ?)",
                   rows)


def _health(db: Database, rng: random.Random, today: D) -> None:
    for offset in range(DAYS, -1, -1):
        day = today - offset * DAY
        ill = 95 <= offset <= 100
        base_hrv = 58 + 8 * (1 - offset / DAYS)
        hrv = base_hrv * rng.uniform(0.88, 1.1) * (0.8 if ill else 1.0)
        rhr = 49 - 3 * (1 - offset / DAYS) + rng.uniform(-2, 2) + (6 if ill else 0)
        sleep_h = rng.uniform(6.4, 8.3) if not ill else rng.uniform(5.5, 7.0)
        db.execute("INSERT INTO daily_health (date, resting_hr, body_battery_start, body_battery_high, training_readiness, "
                   "vo2max_run, vo2max_cycle, avg_stress, steps) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [day, round(rhr), rng.randint(55, 95), rng.randint(70, 100), rng.randint(35, 90),
                    round(49 + 3 * (1 - offset / DAYS), 1), round(51 + 3 * (1 - offset / DAYS), 1),
                    rng.randint(18, 40), rng.randint(6000, 15000)])
        start = datetime.datetime.combine(day - DAY, datetime.time(22, 45))
        db.execute("INSERT INTO sleep_records (date, sleep_start, sleep_end, total_sleep_sec, deep_sleep_sec, "
                   "rem_sleep_sec, light_sleep_sec, sleep_score, hrv_overnight_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [day, start, start + datetime.timedelta(hours=sleep_h), int(sleep_h * 3600), int(sleep_h * 720),
                    int(sleep_h * 800), int(sleep_h * 2000), int(55 + 5 * sleep_h + rng.uniform(-6, 6)), round(hrv)])
        db.execute("INSERT INTO hrv_daily (date, hrv_last_night_ms, hrv_weekly_avg_ms, baseline_low_ms, baseline_high_ms, "
                   "hrv_status) VALUES (?, ?, ?, ?, ?, 'BALANCED')",
                   [day, round(hrv), round(base_hrv), round(base_hrv * 0.88), round(base_hrv * 1.12)])


def _plan_and_ember(db: Database, today: D, activity_ids: list[str]) -> None:
    # The coach's upcoming sessions (none today: today's suggestion is a free choice).
    coach = [
        (1, "bike", "Sweet spot 3x12'", 70, "tempo", "15'- progression to 190W\n3x(12'-235W >85 + 4'-150W)\n10' easy"),
        (2, "swim", "Technique swim", 45, "endurance", "10' easy\n8x50 drills / 25 easy\n6x100 steady\n5' easy"),
        (3, "run", "Tempo run", 50, "tempo", "15' easy\n20' at tempo (Z3)\n15' easy"),
        (5, "bike", "Long ride", 150, "endurance", "2:30 Z2, fuel 70 g/h"),
        (6, "run", "Long run", 80, "endurance", "80' Z2, last 10' steady"),
        (9, "bike", "VO2 5x4'", 75, "vo2", "20' warm-up\n5x(4'-285W + 4'-140W)\n15' easy"),
    ]
    for day_offset, sport, title, minutes, intensity, text in coach:
        db.execute("INSERT INTO planned_sessions (date, sport_type, title, description, duration_min, intensity, source) "
                   "VALUES (?, ?, ?, ?, ?, ?, 'coach_import')",
                   [today + datetime.timedelta(days=day_offset), sport, title, text, minutes, intensity])

    run_id = db.fetchone("INSERT INTO claude_runs (purpose, model, prompt_version, status, num_turns, duration_ms, "
                         "transcript, started_at, finished_at) VALUES ('grade', 'claude-sonnet-5', 'grade@2', 'ok', 2, "
                         "41000, '{}', current_timestamp, current_timestamp) RETURNING id")[0]
    samples = [
        ("endurance", 4, 4, 5, "Controlled long ride: 78% of the time in Z2 and decoupling well under 5%. Efficiency is "
         "up on similar rides — the aerobic base is building.", ["78% in Z2", "Decoupling 3.1%"], []),
        ("tempo", 5, 4, 4, "Tempo block held steady at the planned effort, heart rate settled after 5 minutes.",
         ["Even pacing through the tempo block"], ["Cadence dropped in the last 5 minutes"]),
        ("threshold", 3, 3, 4, "Intervals started too hard and faded in the last rep; power held but HR crept up.",
         ["Hit target power in reps 1–2"], ["Rep 3 faded by 6%", "HR drift of 7%"]),
    ]
    from hart.server.grading import overall
    from hart.server.grading import Scores

    recent = [a for a in activity_ids if a.split("_")[2] in ("bike", "run")][-8:]
    for i, aid in enumerate(recent):
        stype, ex, rs, cx, summary, hl, cc = samples[i % len(samples)]
        score, letter = overall(Scores(execution=ex, response=rs, context_fit=cx))
        db.execute(
            "INSERT INTO session_grades (activity_id, version, status, intent_source, session_type, score_execution, "
            "score_response, score_context, overall_score, letter, confidence, summary, highlights, concerns, citations, "
            "features, claude_run_id) VALUES (?, 1, 'graded', 'inferred', ?, ?, ?, ?, ?, ?, 'high', ?, ?, ?, '[]', ?, ?)",
            [aid, stype, ex, rs, cx, score, letter, summary, json.dumps(hl), json.dumps(cc),
             json.dumps({"justifications": {"execution": "Matched the intent of the session.",
                                            "response": "Heart rate at output better than similar sessions.",
                                            "context_fit": "Right session for the phase and readiness."}}), run_id],
        )

    sessions = [{"sport_type": "run", "title": "Easy run + strides", "duration_min": 45, "intensity": "endurance",
                 "structure": "10' easy\n25' Z2\n6x20\" strides\n5' easy", "garmin_text": None,
                 "rationale": "Readiness is green and the week still owes one easy run; keeps the load low before "
                              "tomorrow's sweet spot ride."}]
    db.execute(
        "INSERT INTO daily_suggestions (for_date, version, kind, readiness, readiness_detail, recommendation, sessions, "
        "cautions, summary, citations, status, context) VALUES (?, 1, 'final', 'green', '{}', 'free_choice', ?, ?, ?, "
        "'[]', 'ok', ?)",
        [today, json.dumps(sessions), json.dumps(["Stop the strides if the Achilles complains"]),
         "No coach session today and readiness is green. An easy run with strides keeps the run frequency up without "
         "adding fatigue before the weekend's long sessions.",
         json.dumps({"week_review": "The coach's sweet spot ride, tempo run and both long sessions are still "
                                    "ahead; today is open. Strength is on track."})],
    )

    conv = "demo-conversation"
    db.execute("INSERT INTO chat_conversations (id, title, model) VALUES (?, 'Am I on track for the Ironman?', "
               "'claude-opus-5-5')", [conv])
    db.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?, 'user', ?)",
               [conv, "Am I on track for the Ironman? How does my bike fitness compare to three months ago?"])
    db.execute(
        "INSERT INTO chat_messages (conversation_id, role, content, tool_calls) VALUES (?, 'assistant', ?, ?)",
        [conv,
         "**Yes — the trend is right, and you have time.** Your fitness (CTL) has climbed steadily since the cold in "
         "spring, with a recovery week every fourth week as planned.\n\n"
         "**Bike:** best 20-minute power is up about 12% on three months ago, and efficiency on long rides improved "
         "while heart rate at the same power dropped.\n\n"
         "**What to watch:**\n- Long rides are still 2.5 h — build towards 4–5 h in the next block\n"
         "- Ferritin has trended down over the last three panels (still in range) — worth asking your doctor\n\n"
         "*(Demo conversation — numbers are illustrative.)*",
         json.dumps([{"name": "get_athlete_context", "input": {}}, {"name": "get_training_load", "input": {"days": 120}},
                     {"name": "get_power_curve", "input": {}}, {"name": "get_lab_results", "input": {"marker": "ferritin"}}])],
    )


def build(path: Path, today: D | None = None) -> dict[str, Any]:
    """Create (or replace) a demo database at *path*."""
    from hart.analytics.training_load import update_training_load
    from hart.server import settings
    from hart.server.jobs.pipeline import persist_recovery_scores
    from hart.server.seed import seed_all
    from hart.storage.views import refresh_all_views

    today = today or D.today()
    for p in (path, Path(str(path) + ".wal")):
        if p.exists():
            p.unlink()
    rng = random.Random(7)
    db = Database(path).connect()
    try:
        init_schema(db)
        ids = _activities(db, rng, today)
        _health(db, rng, today)
        update_training_load(db)
        persist_recovery_scores(db, None)
        refresh_all_views(db)
        # Seed files go to a private temporary folder: nothing next to it (e.g. a real
        # blood_results.json in the data folder) can be picked up by the seeding fallbacks.
        with tempfile.TemporaryDirectory() as tmp:
            seed_dir = Path(tmp) / "seed"
            _seed_files(seed_dir, today)
            seed_all(db, seed_dir)
        db.execute("UPDATE athlete_notes SET status = 'active'")  # demo: notes already approved
        from hart.server.health import sync_checks
        from hart.server.season_ops import seed_phases

        seed_phases(db, today)
        db.execute("UPDATE training_phases SET confirmed = TRUE")
        sync_checks(db, today)
        from hart.server import state

        state.set_setting(db, state.DEMO_MODE, True)
        for key, value in (("athlete_name", "Alex"), ("has_coach", True),
                           ("power_single_sided", True)):
            settings.set_value(db, key, value)
        _plan_and_ember(db, today, ids)
        return {"path": str(path), "activities": len(ids)}
    finally:
        db.close()

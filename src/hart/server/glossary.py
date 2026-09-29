"""One-line explanations for every metric shown in the UI (the ⓘ tips)."""

from __future__ import annotations

GLOSSARY: dict[str, str] = {
    # Load (PMC)
    "ctl": "Fitness (Chronic Training Load): weighted average of your daily training load over ~6 weeks. Rises slowly with consistent training, fades slowly when you stop.",
    "atl": "Fatigue (Acute Training Load): the same average over ~1 week. Jumps after hard days, drops within a few rest days.",
    "tsb": "Form (Training Stress Balance) = CTL − ATL. Above 0 fresh · 0 to −10 normal training · −10 to −30 productive overload · below −30 heavy fatigue.",
    "form_yesterday": "Yesterday's form (TSB = CTL − ATL). Readiness uses yesterday's value because today's training isn't done yet.",
    "ramp": "How much fitness (CTL) changed in the last 7 days. Small steady gains are safe; big jumps are where overload injuries come from.",
    "load": "Garmin's training load for a session (EPOC-based): how much it disturbed your body. Not power-based TSS; daily load is the sum of sessions.",
    "state": "Training state observed from your load curve: building, maintaining, absorbing (easing while fresh), detraining, or overreaching risk. Thresholds are provisional.",
    "ctl_change_28d": "Fitness (CTL) now compared with 28 days ago. A fall of more than 20% with few sessions counts as detraining.",
    "consistency_14d": "Days in the last 14 with at least one swim, bike, run or strength session (walks don't count).",
    # Readiness and recovery
    "readiness": "Morning readiness from sleep, HRV and resting HR vs your 28-day baselines, the recovery score, form and alerts. Unknown means not enough data — never read it as good.",
    "sleep": "Total sleep last night from Garmin. Score 0–100 combines duration, sleep stages and restlessness. Target 7–9 h.",
    "hrv": "Heart-rate variability: average overnight beat-to-beat variation (ms). Compared with your 28-day baseline: 7% below = amber, 15% below = red. Needs 14 nights of data for a baseline.",
    "rhr": "Resting heart rate from Garmin. Compared with your 28-day baseline: +4 bpm = amber, +7 bpm = red — often fatigue, illness, heat or poor sleep.",
    "body_battery": "Garmin's 0–100 energy estimate (from HRV, stress and activity), taken at wake-up.",
    "training_readiness": "Garmin's 0–100 Training Readiness (sleep, recovery time, HRV, acute load). Below 40 = amber.",
    "recovery_score": "hart's 0–100 recovery score: 30% HRV, 25% sleep, 20% Body Battery, 15% Training Readiness, 5% stress, 5% form. Missing parts count as neutral (50), so read it with care when the watch wasn't worn.",
    # Markers and performance
    "session_note": "One note per session. Until you write here it shows the description from Garmin Connect (and follows edits there); once you save your own text it stays in hart only — Garmin isn't changed. The grade, suggestions and Ember read this note.",
    "grade_confidence": "How sure the grade is. Ember sets it from the data it had: high = heart rate, streams and comparable sessions; medium = something missing or only a few comparisons (e.g. no power, a new kind of session); low = little data. hart also drops it to low when more than 30% of the numbers Ember cited can't be traced back to your data.",
    "steps": "Daily steps from Garmin, with the 7-day average. A big walking or hiking day adds fatigue a training plan doesn't see; a very low day often means travel or illness.",
    "vo2max_run": "Garmin's running VO2max estimate (ml/kg/min), from heart rate and pace on outdoor runs.",
    "vo2max_cycle": "Garmin's cycling VO2max estimate (ml/kg/min), from heart rate and power.",
    "resting_hr": "Latest resting heart rate from Garmin, with the change vs ~90 days earlier. A falling resting HR usually means improving fitness.",
    "power_300": "Best average power held for 5 minutes in the last 90 days (vs the previous 90) — roughly VO2max-level effort.",
    "power_1200": "Best average power held for 20 minutes in the last 90 days (vs the previous 90) — close to threshold (FTP ≈ 95%).",
    "power_3600": "Best average power held for 60 minutes in the last 90 days (vs the previous 90) — endurance, the most Ironman-relevant.",
    "np": "Normalized power: what a ride cost physiologically. Weights surges more than steady riding, so it's higher than average power on variable rides.",
    "ef": "Efficiency factor: bike = normalized power ÷ average HR; run = speed (m/min) ÷ average HR. Higher at the same kind of session = fitter aerobic engine.",
    "decoupling": "Aerobic decoupling: drop in efficiency from the first to the second half of a steady session. Below 5% = your endurance holds for that duration.",
    "training_effect": "Garmin Training Effect 0–5 (aerobic / anaerobic): impact on fitness. 2–3 maintaining, 3–4 improving, 4–5 highly improving, 5 overreaching.",
    "cadence": "Pedalling rpm on the bike; steps per minute when running.",
    "hr_zones": "Time in each heart-rate zone, using the zones set on your Garmin when the session was recorded.",
    "rpe": "Your rating of perceived exertion, 1 (very easy) to 10 (maximal).",
    "feel": "How you felt, 1 (very weak) to 5 (very strong).",
    "duration": "Moving time (elapsed time if moving time isn't recorded).",
    # Health charts
    "hrv_chart": "Nightly HRV (dots), Garmin's 7-day average (line) and Garmin's balanced baseline range (dotted).",
    "sleep_chart": "Hours slept (bars) and Garmin's sleep score (line). Gaps are nights without the watch.",
    "weekly_volume": "Hours per week by sport (bars) and total weekly training load (line). Walks and hikes count as 'other'.",
    "coach_plan": "Your coach's sessions for this day, pasted or added on the Plan page — shown word for "
    "word. The coach's plan always takes priority over the suggestion.",
    "suggestion": "Made by Ember from your readiness, the coach's plan, your season phase, this week's sessions "
    "and your notes. Code checks it afterwards against hard limits (red readiness means no hard "
    "sessions; note rules like 'swim only on Thursday'; comeback caps of 1.25x your longest recent "
    "session) and rejects it if it breaks any. Made each morning once sleep syncs (10:30 at the "
    "latest), and at 20:00 for tomorrow.",
    "health_checks": "Reminders computed from your results, season and notes: a blood panel every 6 months (plus "
    "one before Build and ~10 weeks before the A-race), follow-ups on results outside the lab range "
    "or close to a limit after a big change, re-checks planned in notes, markers not tested for a "
    "year (vitamin D timed for February–March), a pre-race medical exam with ECG, and physio "
    "check-ins for active injuries. Lab checks close themselves when newer results cover them.",
    "lab_trends": "Each dot is one test; amber dots were outside the lab range or flagged. The shaded band is the "
    "lab's reference range from the latest test (ranges can differ between labs). Red/amber areas "
    "mark illness and injury periods.",
}

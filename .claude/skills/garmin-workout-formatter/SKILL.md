---
name: garmin-workout-formatter
description: Converts a cycling training plan (often shorthand Polish notation from a coach, e.g. "30'- progresja do 170W, kadencja powyzej 80" or "3x(5'-200W min. 80 + 2'-150W)") into the plain-text workout format pasted into the "Description" field of Garmin's workout creator. Use this whenever the user pastes a structured bike/trainer workout — with warmups, intervals, sets, repeats, power targets in watts, or cadence targets — and wants it converted, formatted, reformatted, or made Garmin-compatible. Trigger even if the user doesn't say "Garmin" explicitly but the input looks like a coach's interval workout (durations plus power or cadence targets, possibly with loop notation like "10x" or "3x(...)"). Also trigger for requests to fix or regenerate a workout that already follows this format.
---

# Garmin Workout Formatter

Coaches write cycling workouts in compact shorthand — total durations, watt targets, loop counts. Garmin's workout creator can't parse that shorthand directly: it chokes on single-line loops, nested parentheses, open-ended ranges, and bare wattage numbers. This skill expands shorthand into the verbose, flat, range-based format Garmin actually accepts.

Work through the plan one block at a time, in order, and apply these four rules to each.

## 1. Loops become their own labeled block

Garmin can't read a loop on one line, and it can't read nested loops. Every repeated group needs its own header (`Set 1 10x`, `Set 2 3x`, ...) with each step inside on its own line, prefixed with `- `.

If the coach nests a loop (e.g., `3x(5'-200W + 2x(1'-300W + 1'-100W))`), flatten it: the outer repeat becomes one `Set N` block, and the inner repeat's steps are simply listed in sequence inside it the right number of times — Garmin has no concept of a loop-within-a-loop, so the inner repeat must be written out explicitly.

## 2. Single-point power becomes a 10-watt range

Garmin workout steps need a range, not a point target. Take the coach's number and spread it ±5W:

- `250W` → `245-255w`
- `150W` → `145-155w`

If the coach already gave a range (`180-190W`), leave it exactly as given — don't re-center or widen it.

## 3. Warmups get broken into granular steps

A ramp like "progresja do 170W" (progression to 170W) can't be a single ramp block in Garmin — it needs to be chopped into discrete steps that climb steadily.

- Start near 100W (`100-110w`) for the very first step.
- Climb by 10W per step from there.
- Steps are mostly 2-3 minutes each; the first step can be 1 minute if that helps the total add up cleanly.
- The last step's range must land exactly on the coach's target ceiling — if the coach said "progresja do 170W", the final step is `160-170w`, not `170-180w` or `150-160w`.
- The step durations must sum to the coach's total warmup time. Adjust step lengths (within the 1-3 min guideline) as needed to make the math work exactly — don't leave a remainder unaccounted for.

Work backward from the target if it helps: if the ceiling is 170W and you start at 100-110w climbing by 10W per step, count how many steps that takes, then distribute the total warmup minutes across that many steps.

## 4. Open-ended cadence ranges get capped at 110rpm

Garmin devices misread an open range like `>80` or `>85` — always convert it to a closed range with 110rpm as the ceiling:

- `>80` → `80-110rpm`
- `>85` → `85-110rpm`

If the coach already gave a closed range (`75-80`), just append `rpm`: `75-80rpm`. Leave it alone otherwise — don't widen or shift it.

If a step has no cadence given at all, omit the cadence entirely rather than inventing one.

## Output

Always reply with just the converted workout — no commentary before or after — inside a fenced code block tagged `text`. Don't use bold, italics, or other markdown inside the block; Garmin's description field is plain text and any markdown syntax would get pasted in literally.

Use these block headers in order, including only the blocks the plan actually has:
- `Warmup`
- `Set 1 Nx`, `Set 2 Nx`, ... (one per distinct repeated group, in the order they appear)
- `Cooldown`

A step with no repeats around it (a single block of work, like a steady-state segment or a cooldown ramp) doesn't get a `Set` header — just list its steps directly under the relevant section, or under a plain section name if it doesn't fit Warmup/Set/Cooldown (e.g., a long steady block between two interval sets can just be its own unlabeled run of `- ` lines, or you can give it a short descriptive header matching what the coach called it).

## Worked example

**Input (coach's shorthand):**
```
15'- progresja do 170W, kadencja >80
10x1'-250W 75-80 + 1'-150W 75-80
3x(5'-200W >80 + 2'-150W)
8'-ROZJAZD
```

**Output:**
```text
Warmup
- 1m 100-110w 80-110rpm
- 2m 110-120w 80-110rpm
- 2m 120-130w 80-110rpm
- 2m 130-140w 80-110rpm
- 2m 140-150w 80-110rpm
- 3m 150-160w 80-110rpm
- 3m 160-170w 80-110rpm

Set 1 10x
- 1m 245-255w 75-80rpm
- 1m 145-155w 75-80rpm

Set 2 3x
- 5m 195-205w 80-110rpm
- 2m 145-155w

Cooldown
- 4m 130-140w
- 4m 110-120w
```

Notice the warmup's 7 steps sum to 15 minutes (1+2+2+2+2+3+3), and the final step (`160-170w`) lands exactly on the coach's stated ceiling of 170W. The cooldown ("ROZJAZD") had no cadence specified, so none was added, and its 8 minutes were split into two steps descending in power.

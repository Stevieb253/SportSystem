# services/matchup_notes.py
# Generates short matchup analysis notes for batters.
#
# Input : a serialised hit_probability or hr_probability result dict
#          (plain dicts after _serialise_model has run — no dataclasses).
# Output: list[str] — 2-5 short note strings ready for template display.
#
# Rules:
#   - Never invent data. Skip any note where the underlying value is 0 / None.
#   - Cap output at MAX_NOTES bullet points.
#   - Keep each note short and plain-English — no jargon overload.
#   - All thresholds are soft heuristics; adjust freely without touching templates.

from __future__ import annotations

# ── Tunable thresholds ────────────────────────────────────────────────────────

# Batter — contact
XBA_ELITE       = 0.290
XBA_WEAK        = 0.225
WHIFF_HIGH      = 0.30      # fraction (0–1) — danger zone vs strong K pitchers
WHIFF_LOW       = 0.18      # fraction — elite bat-to-ball skill

# Batter — power
BARREL_ELITE    = 0.100
BARREL_STRONG   = 0.070
HARD_HIT_ELITE  = 0.44
EV50_ELITE      = 94.0
EV50_STRONG     = 90.0

# Batter — recent form swing (recent_avg vs season avg, in average points)
FORM_HOT        =  0.040    # +40 pts over 14 days → hot
FORM_COLD       = -0.040    # -40 pts over 14 days → cold

# Platoon (platoon_advantage is 0–1; higher = more favorable for batter)
PLATOON_ADV_THR = 0.58
PLATOON_DIS_THR = 0.42

# Pitcher
ERA_GAP             = 0.55   # |xERA - ERA| worth flagging
PITCHER_HHA_HIGH    = 0.42   # hard-hit% allowed — hittable pitcher
PITCHER_BA_HIGH     = 0.085  # barrel% allowed — power-friendly pitcher
PITCHER_HR9_HIGH    = 1.35   # HR/9 — elevated HR risk
PITCHER_K_HIGH      = 0.27   # K rate — heavy strikeout pitcher

# Weather
WIND_NOTABLE    = 10         # mph — worth a note
TEMP_HOT        = 82         # °F — ball carries

MAX_NOTES = 5


# ── Public API ────────────────────────────────────────────────────────────────

def generate_hit_notes(r: dict) -> list[str]:
    """Return up to MAX_NOTES matchup notes for a hit-probability result dict."""
    notes: list[str] = []
    p  = r.get("player")    or {}
    vp = r.get("vs_pitcher") or {}
    g  = r.get("game")       or {}

    _platoon(p, vp, notes)
    _recent_form(p, notes, power_context=False)
    _contact_quality(p, vp, notes)
    _pitcher_quality(vp, notes, power_context=False)
    _weather(g, notes, power_context=False)

    return notes[:MAX_NOTES]


def generate_hr_notes(r: dict) -> list[str]:
    """Return up to MAX_NOTES matchup notes for an HR-probability result dict."""
    notes: list[str] = []
    p  = r.get("player")    or {}
    vp = r.get("vs_pitcher") or {}
    g  = r.get("game")       or {}

    _platoon(p, vp, notes)
    _power_profile(p, notes)
    _recent_form(p, notes, power_context=True)
    _pitcher_quality(vp, notes, power_context=True)
    _weather(g, notes, power_context=True)

    return notes[:MAX_NOTES]


# ── Private note generators ───────────────────────────────────────────────────

def _platoon(p: dict, vp: dict, notes: list) -> None:
    """Platoon / split advantage note."""
    adv    = float(p.get("platoon_advantage") or 0.5)
    b_hand = (p.get("hand") or "").upper().strip()
    p_hand = (vp.get("hand") or "").upper().strip()
    if not b_hand or not p_hand:
        return

    hand_label = {"L": "Left-handed", "R": "Right-handed", "S": "Switch"}.get(b_hand, b_hand)
    p_label    = "RHP" if p_hand == "R" else "LHP"

    if b_hand == "S":
        notes.append(
            f"Switch-hitter advantage: always bats from the favorable side vs {p_label}."
        )
        return

    if adv >= PLATOON_ADV_THR:
        notes.append(
            f"Platoon edge: {hand_label} batter vs {p_label} — a historically favorable split."
        )
    elif adv <= PLATOON_DIS_THR:
        notes.append(
            f"Platoon disadvantage: {hand_label} batter vs {p_label} "
            f"— same-side matchup tends to suppress production."
        )


def _recent_form(p: dict, notes: list, *, power_context: bool) -> None:
    """14-day recent form note."""
    recent = float(p.get("recent_avg") or 0)
    season = float(p.get("avg") or 0)
    r_hh   = float(p.get("recent_hard_hit_pct") or 0)

    if recent <= 0 or season <= 0:
        return

    diff = recent - season
    if diff >= FORM_HOT:
        msg = (
            f"Hot streak: batting .{round(recent * 1000):03d} over the last 14 days "
            f"(season avg .{round(season * 1000):03d})"
        )
        if power_context and r_hh >= HARD_HIT_ELITE:
            msg += f" with a {r_hh * 100:.0f}% hard-hit rate during that stretch."
        else:
            msg += "."
        notes.append(msg)
    elif diff <= FORM_COLD:
        notes.append(
            f"Cold stretch: batting .{round(recent * 1000):03d} over the last 14 days "
            f"vs season average of .{round(season * 1000):03d}."
        )


def _contact_quality(p: dict, vp: dict, notes: list) -> None:
    """Contact-quality and strikeout-risk notes (hit context)."""
    xba     = float(p.get("xba") or 0)
    whiff   = float(p.get("whiff_pct") or 0)
    k_pct_p = float(vp.get("k_pct") or 0)

    if xba >= XBA_ELITE:
        notes.append(
            f"Elite contact profile: xBA of .{round(xba * 1000):03d} projects "
            f"an above-average hit rate regardless of luck."
        )
    elif 0 < xba <= XBA_WEAK:
        notes.append(
            f"Weak contact indicators: xBA of .{round(xba * 1000):03d} suggests "
            f"below-average expected hit rate."
        )

    if whiff > 0 and whiff >= WHIFF_HIGH and k_pct_p >= PITCHER_K_HIGH:
        notes.append(
            f"Elevated strikeout risk: batter's {whiff * 100:.0f}% whiff rate "
            f"faces a pitcher with a {k_pct_p * 100:.0f}% K rate."
        )
    elif whiff > 0 and whiff <= WHIFF_LOW:
        notes.append(
            f"Strong bat-to-ball skills: {whiff * 100:.0f}% whiff rate "
            f"limits strikeout risk even against quality arms."
        )


def _power_profile(p: dict, notes: list) -> None:
    """Power metrics note (HR context only)."""
    barrel = float(p.get("barrel_pct") or 0)
    hh     = float(p.get("hard_hit_pct") or 0)
    ev50   = float(p.get("ev50") or 0)

    if barrel >= BARREL_ELITE and hh >= HARD_HIT_ELITE:
        notes.append(
            f"Elite power profile: {barrel * 100:.1f}% barrel rate and "
            f"{hh * 100:.0f}% hard-hit rate — one of the highest-impact bats today."
        )
    elif barrel >= BARREL_STRONG:
        notes.append(
            f"Above-average power: {barrel * 100:.1f}% barrel rate with "
            f"{hh * 100:.0f}% hard contact rate."
        )

    if ev50 >= EV50_ELITE:
        notes.append(
            f"Top-tier exit velocity: EV50 of {ev50:.1f} mph puts him among the hardest hitters."
        )
    elif ev50 >= EV50_STRONG:
        notes.append(
            f"Above-average exit velocity: EV50 of {ev50:.1f} mph indicates real power potential."
        )


def _pitcher_quality(vp: dict, notes: list, *, power_context: bool) -> None:
    """Pitcher quality and vulnerability notes."""
    era  = float(vp.get("era")  or 0)
    xera = float(vp.get("xera") or 0)
    hha  = float(vp.get("hard_hit_pct_allowed") or 0)
    ba   = float(vp.get("barrel_pct_allowed")   or 0)
    hr9  = float(vp.get("hr9")  or 0)
    name = vp.get("name") or "the pitcher"

    # ERA vs xERA gap
    if era > 0 and xera > 0:
        gap = xera - era
        if gap >= ERA_GAP:
            notes.append(
                f"Pitcher regression risk: {name}'s ERA ({era:.2f}) looks better than "
                f"his xERA ({xera:.2f}) — batters may be underperforming their true outcomes."
            )
        elif gap <= -ERA_GAP:
            notes.append(
                f"Pitcher getting unlucky: {name}'s xERA ({xera:.2f}) is better than "
                f"his ERA ({era:.2f}) — underlying skill may outperform the stat line today."
            )

    if power_context:
        # HR vulnerability
        if hr9 >= PITCHER_HR9_HIGH:
            notes.append(
                f"HR-prone pitcher: {name} allows {hr9:.2f} HR/9 innings — "
                f"elevated home run upside for this matchup."
            )
        if ba >= PITCHER_BA_HIGH:
            notes.append(
                f"Barrel-prone: {name} allows a {ba * 100:.1f}% barrel rate — "
                f"added power upside for hitters with big exit velo."
            )
    else:
        # General hittability
        if hha >= PITCHER_HHA_HIGH:
            notes.append(
                f"Hittable pitcher: {name} allows hard contact on "
                f"{hha * 100:.0f}% of balls in play — above-average hit floor."
            )


def _weather(g: dict, notes: list, *, power_context: bool) -> None:
    """Game-day weather note."""
    w = g.get("weather") or {}
    if not w or w.get("is_dome"):
        return   # dome = no weather impact

    wind   = float(w.get("wind_speed_mph")    or 0)
    temp   = float(w.get("temp_f")            or 72)
    precip = float(w.get("precipitation_mm")  or 0)

    if precip > 2.0:
        notes.append(
            f"Rain in the forecast ({precip:.1f} mm) — may affect pitch grip and ball flight."
        )
        return

    if power_context:
        if wind >= WIND_NOTABLE:
            notes.append(
                f"Wind at {wind:.0f} mph — ball flight affected, check park orientation."
            )
        if temp >= TEMP_HOT:
            notes.append(
                f"Warm conditions ({temp:.0f}°F) — baseball carries further in the heat."
            )
    else:
        parts: list[str] = []
        if wind >= WIND_NOTABLE:
            parts.append(f"{wind:.0f} mph wind")
        if temp >= TEMP_HOT:
            parts.append(f"{temp:.0f}°F")
        if parts:
            notes.append(f"Game-day conditions: {', '.join(parts)} — may influence ball flight.")

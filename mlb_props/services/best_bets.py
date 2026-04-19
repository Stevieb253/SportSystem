# services/best_bets.py
# Phase 2 — Best Bets service.
#
# Curates the top model picks into a "Best Bets" view with confidence tiers,
# optional edge calculations when odds are available, and curated parlays.
#
# IMPORTANT: this service never calls the Odds API directly.
# Odds data must be pre-fetched (manual refresh) and passed in as a dict.

from __future__ import annotations

import dataclasses
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Confidence tier thresholds ─────────────────────────────────────────────────
# Hit probability (0–1 scale, historical model range ~0.35–0.75)
HIT_ELITE   = 0.68
HIT_STRONG  = 0.58
HIT_SOLID   = 0.48

# HR probability (rarer, 0–1 scale, typical range ~0.05–0.25)
HR_ELITE    = 0.17
HR_STRONG   = 0.12
HR_SOLID    = 0.08

# Edge (model_prob - implied_prob) thresholds
EDGE_VALUE  =  0.05   # +5pp over book implied = VALUE
EDGE_AVOID  = -0.03   # 3pp under book implied = AVOID

# Display limits
MAX_HIT_BETS = 8
MAX_HR_BETS  = 6
MAX_PARLAYS  = 3


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclasses.dataclass
class BestBet:
    player_name:     str
    player_id:       int
    team:            str
    bet_type:        str        # "hit" | "hr"
    probability:     float
    confidence:      str        # "ELITE" | "STRONG" | "SOLID"
    game_matchup:    str        # "NYY @ BOS"
    vs_pitcher:      str
    lineup_slot:     int

    # Odds fields — None when operating in model-only mode
    best_odds:       str | None   = None   # "+350" or "-110"
    best_book:       str | None   = None
    implied_prob:    float | None = None
    edge:            float | None = None   # model_prob - implied_prob
    value_label:     str | None   = None   # "VALUE" | "FAIR" | "AVOID"

    # Key supporting stats (labels + formatted values for display)
    stat1_label:     str = ""
    stat1_value:     str = ""
    stat2_label:     str = ""
    stat2_value:     str = ""


@dataclasses.dataclass
class ParlayLeg:
    player_name:  str
    team:         str
    bet_type:     str
    probability:  float
    best_odds:    str | None = None


@dataclasses.dataclass
class Parlay:
    legs:           list[ParlayLeg]
    combined_prob:  float
    confidence:     str   # "STRONG" | "SOLID"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _conf_hit(prob: float) -> str | None:
    if prob >= HIT_ELITE:   return "ELITE"
    if prob >= HIT_STRONG:  return "STRONG"
    if prob >= HIT_SOLID:   return "SOLID"
    return None


def _conf_hr(prob: float) -> str | None:
    if prob >= HR_ELITE:   return "ELITE"
    if prob >= HR_STRONG:  return "STRONG"
    if prob >= HR_SOLID:   return "SOLID"
    return None


def _value_label(edge: float) -> str:
    if edge >= EDGE_VALUE:  return "VALUE"
    if edge <= EDGE_AVOID:  return "AVOID"
    return "FAIR"


def _matchup(game: dict) -> str:
    away = game.get("away_team", {}).get("abbreviation", "AWY")
    home = game.get("home_team", {}).get("abbreviation", "HME")
    return f"{away} @ {home}"


# ── Public API ────────────────────────────────────────────────────────────────

def build_best_bets(
    hit_probabilities: list[dict],
    hr_probabilities:  list[dict],
    odds_by_player:    dict[str, dict] | None = None,
) -> dict:
    """Build curated best bets from today's model.

    Args:
        hit_probabilities: model["hit_probabilities"] list.
        hr_probabilities:  model["hr_probabilities"] list.
        odds_by_player:    Optional dict mapping player_name (or player_name+"_hr")
                           to {"best_odds", "best_book", "implied_prob"}.
                           Pass None or {} for model-only mode.

    Returns:
        dict with keys: hit_bets, hr_bets, parlays, has_odds, mode.
        All bet lists contain plain dicts (dataclasses.asdict).
    """
    odds_by_player = odds_by_player or {}
    has_odds = bool(odds_by_player)

    # ── Hit bets ──────────────────────────────────────────────────────────────
    hit_bets: list[BestBet] = []
    for r in hit_probabilities:
        pdict  = r.get("player", {})   if isinstance(r, dict) else {}
        gdict  = r.get("game", {})     if isinstance(r, dict) else {}
        vpdict = r.get("vs_pitcher", {}) if isinstance(r, dict) else {}
        prob   = float(r.get("hit_probability", 0))

        conf = _conf_hit(prob)
        if not conf:
            continue

        name = pdict.get("name", "")
        pid  = int(pdict.get("player_id", 0) or 0)

        # Odds enrichment
        odds_info = odds_by_player.get(name, {})
        implied   = odds_info.get("implied_prob") if odds_info else None
        edge      = round(prob - implied, 4) if implied is not None else None

        hit_bets.append(BestBet(
            player_name   = name,
            player_id     = pid,
            team          = pdict.get("team", ""),
            bet_type      = "hit",
            probability   = prob,
            confidence    = conf,
            game_matchup  = _matchup(gdict),
            vs_pitcher    = vpdict.get("name", ""),
            lineup_slot   = int(pdict.get("lineup_position", 0) or 0),
            best_odds     = odds_info.get("best_odds")  if odds_info else None,
            best_book     = odds_info.get("best_book")  if odds_info else None,
            implied_prob  = implied,
            edge          = edge,
            value_label   = _value_label(edge) if edge is not None else None,
            stat1_label   = "xBA",
            stat1_value   = f"{float(pdict.get('xba', 0)):.3f}",
            stat2_label   = "Whiff%",
            stat2_value   = f"{float(pdict.get('whiff_pct', 0)) * 100:.1f}%",
        ))

    # Sort: AVOID last, then by probability desc
    hit_bets.sort(key=lambda b: (b.value_label == "AVOID", -b.probability))
    hit_bets = hit_bets[:MAX_HIT_BETS]

    # ── HR bets ───────────────────────────────────────────────────────────────
    hr_bets: list[BestBet] = []
    for r in hr_probabilities:
        pdict  = r.get("player", {})     if isinstance(r, dict) else {}
        gdict  = r.get("game", {})       if isinstance(r, dict) else {}
        vpdict = r.get("vs_pitcher", {}) if isinstance(r, dict) else {}
        prob   = float(r.get("hr_probability", 0))

        conf = _conf_hr(prob)
        if not conf:
            continue

        name = pdict.get("name", "")
        pid  = int(pdict.get("player_id", 0) or 0)

        # Try "player_hr" key first for HR-specific odds, fall back to player name
        odds_info = odds_by_player.get(name + "_hr") or odds_by_player.get(name, {})
        implied   = odds_info.get("implied_prob") if odds_info else None
        edge      = round(prob - implied, 4) if implied is not None else None

        hr_bets.append(BestBet(
            player_name   = name,
            player_id     = pid,
            team          = pdict.get("team", ""),
            bet_type      = "hr",
            probability   = prob,
            confidence    = conf,
            game_matchup  = _matchup(gdict),
            vs_pitcher    = vpdict.get("name", ""),
            lineup_slot   = int(pdict.get("lineup_position", 0) or 0),
            best_odds     = odds_info.get("best_odds")  if odds_info else None,
            best_book     = odds_info.get("best_book")  if odds_info else None,
            implied_prob  = implied,
            edge          = edge,
            value_label   = _value_label(edge) if edge is not None else None,
            stat1_label   = "Barrel%",
            stat1_value   = f"{float(pdict.get('barrel_pct', 0)) * 100:.1f}%",
            stat2_label   = "EV50",
            stat2_value   = f"{float(pdict.get('ev50', 0)):.1f}",
        ))

    hr_bets.sort(key=lambda b: (b.value_label == "AVOID", -b.probability))
    hr_bets = hr_bets[:MAX_HR_BETS]

    # ── Parlays ───────────────────────────────────────────────────────────────
    parlays = _build_parlays(hit_bets, hr_bets)

    return {
        "hit_bets":  [dataclasses.asdict(b) for b in hit_bets],
        "hr_bets":   [dataclasses.asdict(b) for b in hr_bets],
        "parlays":   [dataclasses.asdict(p) for p in parlays],
        "has_odds":  has_odds,
        "mode":      "odds_enhanced" if has_odds else "model_only",
    }


def _build_parlays(hit_bets: list[BestBet], hr_bets: list[BestBet]) -> list[Parlay]:
    """Build up to MAX_PARLAYS 2-leg parlays from ELITE + STRONG picks.

    Rules:
      - Only ELITE or STRONG confidence bets qualify as parlay legs
      - Any type combo is fine: hit+hit, hr+hr, hit+hr — whatever gives the best combined prob
      - No two legs from the same game (correlated risk)
      - Each player appears in at most ONE parlay (no recycling the same top pick)
      - Sorted by combined probability descending — best parlays surface first
    """
    pool = sorted(
        [b for b in (hit_bets + hr_bets) if b.confidence in ("ELITE", "STRONG")],
        key=lambda b: -b.probability,
    )

    if len(pool) < 2:
        return []

    # Pre-build all valid pairs sorted by combined probability
    candidates: list[tuple[float, BestBet, BestBet]] = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            a, b = pool[i], pool[j]
            # No same-game correlation
            if a.game_matchup == b.game_matchup:
                continue
            combined = round(a.probability * b.probability, 4)
            candidates.append((combined, a, b))

    # Sort best combined prob first
    candidates.sort(key=lambda x: -x[0])

    parlays: list[Parlay] = []
    used_players: set[str] = set()   # player_name+bet_type already in a parlay

    for combined, a, b in candidates:
        key_a = a.player_name + a.bet_type
        key_b = b.player_name + b.bet_type

        # Skip if either player is already in a parlay
        if key_a in used_players or key_b in used_players:
            continue

        used_players.add(key_a)
        used_players.add(key_b)

        conf = "STRONG" if combined >= 0.32 else "SOLID"

        parlays.append(Parlay(
            legs=[
                ParlayLeg(a.player_name, a.team, a.bet_type, a.probability, a.best_odds),
                ParlayLeg(b.player_name, b.team, b.bet_type, b.probability, b.best_odds),
            ],
            combined_prob=combined,
            confidence=conf,
        ))

        if len(parlays) >= MAX_PARLAYS:
            break

    return parlays

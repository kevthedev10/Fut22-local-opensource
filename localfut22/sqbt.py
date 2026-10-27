"""Squad Battles: the event, its opponents, the scoring and the leaderboard.

The HTTP layer owns the wire shapes (every one of them was read out of
CardsDLL - see the SQUAD BATTLES block in fut22_server.py). This module owns
the DECISIONS that fill them, so all of it can be tested with no game and no
server running:

  * which window we are in, and its id,
  * the four opponents on the Opponent Select screen, and the squad each one
    fields,
  * what a finished match is worth,
  * the Top 100 board the hub ranks a player against, and the prize tiers.

Everything is DETERMINISTIC for a given event id: the same week always draws
the same opponents, and the same result always books the same points. That is
what makes it testable, and it is also what lets two players on the same server
see the same board.

The SHAPE of the mode (four slots, a pool per slot, points by difficulty) is
taken from FIFA 19's working local server,
analyze/FIFA-19-Local-FUT-Deba-Beta-1.3/LocalFUT/server/fut_sqbt.py. The DATA
is ours: FIFA 22 teams out of fut22_players.json.
"""
from __future__ import annotations

import random
import time

# ---------------------------------------------------------------- the event --

# Retail FUT rolls Squad Battles on a Thursday. Anchor on one so the window
# lines up with what players expect, and keep the length a constant: a private
# server with a handful of testers may well want a shorter cycle, and that is
# the only thing standing between them and the reward screen.
EVENT_DAYS = 7
EVENT_ANCHOR = 1_640_822_400        # 2021-12-30 00:00 UTC, a Thursday


def event_window(now: float | None = None) -> tuple[int, int, int]:
    """-> (event id, start, end). The id counts windows since the anchor."""
    now = int(now if now is not None else time.time())
    span = EVENT_DAYS * 86_400
    index = (now - EVENT_ANCHOR) // span
    start = EVENT_ANCHOR + index * span
    return int(index), int(start), int(start + span)


# ------------------------------------------------------------------ squads --

# The XI a club is rated on and fields, and which positions each slot accepts.
# One list drives both, so a club's advertised star rating and the squad the
# match actually loads are the same eleven players.
RATING_XI = ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "LM", "ST", "ST")
BENCH = ("GK", "CB", "LB", "CM", "CM", "RM", "ST",
         "CB", "RB", "LM", "CAM", "ST")
FORMATION = "f442"
_ACCEPTS = {
    "GK": {"GK"},
    "RB": {"RB", "RWB"},
    "LB": {"LB", "LWB"},
    "CB": {"CB", "RB", "LB"},
    "CM": {"CM", "CDM", "CAM"},
    "CAM": {"CAM", "CM", "CF"},
    "RM": {"RM", "RW", "CM"},
    "LM": {"LM", "LW", "CM"},
    "ST": {"ST", "CF", "LW", "RW"},
}

PLAYERS: dict = {}                 # assetId -> the fut22_players.json record
_BY_TEAM: dict[int, list[int]] = {}
_RATING: dict[int, int] = {}
KITS: dict[int, dict] = {}
NAMES: dict[int, str] = {}


def load(players: dict, kitted=None) -> int:
    """Index the player database by team. -> how many teams can be an opponent.

    `kitted` is the set of teams we have kits for. A club without one runs out
    in blank white shirts, which is exactly what the first opponent did, so a
    club that cannot be dressed is not offered as an opponent at all.
    """
    global PLAYERS, _BY_TEAM, _RATING
    PLAYERS = players or {}
    _BY_TEAM, _RATING = {}, {}
    allowed = {int(t) for t in kitted} if kitted else None
    for asset_id, rec in PLAYERS.items():
        team = int(rec.get("t") or 0)
        if team > 0 and (allowed is None or team in allowed):
            _BY_TEAM.setdefault(team, []).append(int(asset_id))
    return sum(1 for team in _BY_TEAM if team_rating(team) > 0)


def load_from_catalogue(players, kits=None) -> int:
    """Index the local FIFA 22 catalogue plus kit names.

    Catalogue rows use teamid/rating/preferredPosition; the CardsDLL donor
    module uses t/r/p. Kits come from fut22_kits.json so a club we cannot
    dress is never offered as an opponent.
    """
    global KITS, NAMES
    mapped = {}
    names = {}
    for row in players or []:
        asset = int(row.get('assetId') or 0)
        team = int(row.get('teamid') or 0)
        if asset <= 0 or team <= 0:
            continue
        mapped[asset] = {
            't': team,
            'r': int(row.get('rating') or 0),
            'p': str(row.get('preferredPosition') or 'CM'),
            'l': int(row.get('leagueId') or 0),
            'u': int(row.get('nationId') or 0),
            'a': list(row.get('attributeArray') or []),
            'n': str(row.get('name') or ''),
        }
        club = str(row.get('clubName') or '')
        if team not in names and club:
            names[team] = club
    KITS = {int(k): v for k, v in (kits or {}).items()}
    NAMES = names
    return load(mapped, set(KITS) if KITS else None)


def team_name(team_id: int) -> str:
    rec = KITS.get(int(team_id)) or {}
    return str(rec.get('n') or NAMES.get(int(team_id)) or f'TeamName_{int(team_id)}')


def kit_of(team_id: int) -> dict:
    return KITS.get(int(team_id)) or {}


def _pick(team_id: int, slots) -> list[int]:
    """Best available player per slot, nobody used twice."""
    rows = _BY_TEAM.get(int(team_id)) or []
    used: set[int] = set()
    out = []
    for slot in slots:
        accepts = _ACCEPTS.get(slot, {slot})
        best, best_rating = None, -1
        for asset_id in rows:
            if asset_id in used:
                continue
            rec = PLAYERS[asset_id]
            if str(rec.get("p") or "").upper() not in accepts:
                continue
            rating = int(rec.get("r") or 0)
            if rating > best_rating:
                best, best_rating = asset_id, rating
        if best is None:
            continue
        used.add(best)
        out.append(best)
    return out


def team_rating(team_id: int) -> int:
    """The club's best-XI average, or 0 when it cannot field one."""
    team_id = int(team_id)
    if team_id in _RATING:
        return _RATING[team_id]
    xi = _pick(team_id, RATING_XI)
    value = (round(sum(int(PLAYERS[a].get("r") or 0) for a in xi) / len(xi))
             if len(xi) == len(RATING_XI) else 0)
    _RATING[team_id] = int(value)
    return int(value)


# FIFA grades a team in half stars. The bands below put the five-star slot on
# the clubs FIFA 22 itself calls five stars and leave the first slot genuinely
# easy, which is the whole point of having four of them.
_STARS = ((84, 5.0), (82, 4.5), (79, 4.0), (77, 3.5), (74, 3.0), (71, 2.5),
          (68, 2.0))


def team_stars(team_id: int) -> float:
    rating = team_rating(team_id)
    if rating <= 0:
        return 0.0
    for floor, stars in _STARS:
        if rating >= floor:
            return stars
    return 1.5


SQUAD_SLOTS = 23


TOP_PLAYERS = 3


def top_players(team_id: int) -> list[int]:
    """The faces on an opponent's tile - its best three, by rating."""
    return sorted(_BY_TEAM.get(int(team_id)) or [],
                  key=lambda a: -int(PLAYERS[a].get("r") or 0))[:TOP_PLAYERS]


def squad_of(team_id: int) -> list[int]:
    """The 23 assetIds a Squad Battles opponent fields: the XI, then the bench.

    Exactly 23, padded if need be. `_pick` DROPS a slot it cannot fill by
    position, and real clubs do run out - the first one tried, team 10, could
    only field 22 - while the squad consumer clears exactly 23 slots. A short
    list would leave the last slot holding whatever was there before.
    """
    squad = _pick(team_id, list(RATING_XI) + list(BENCH))
    if len(squad) < SQUAD_SLOTS:
        used = set(squad)
        rest = sorted((a for a in (_BY_TEAM.get(int(team_id)) or [])
                       if a not in used),
                      key=lambda a: -int(PLAYERS[a].get("r") or 0))
        squad.extend(rest[:SQUAD_SLOTS - len(squad)])
    return squad[:SQUAD_SLOTS]


# --------------------------------------------------------------- opponents --

# One entry per slot on Opponent Select: the star band it draws from and how
# deep its pool is. Five clubs per slot means five non-repeating rounds of four
# opponents before the pools run out, which is more than a week's play.
#
# The bands DO NOT overlap, and that was a correction. With slot 3 at 4.0-5.0
# it drew from the same five-star pool as slot 4 - measured on our own data
# there are only ten five-star clubs, so slot 4 took five and slot 3 got the
# other five, and two of the four opponents were equally hard. Our teams grade
# out as 10 at five stars, 5 at 4.5, 19 at 4.0, 23 at 3.5, 59 at 3.0 and 474
# below, so these four bands give a real ramp and every one of them has more
# clubs than it needs.
SLOT_RULES = (
    {"slot": 1, "min": 0.0, "max": 2.5, "pool": 5},
    {"slot": 2, "min": 3.0, "max": 3.5, "pool": 5},
    {"slot": 3, "min": 4.0, "max": 4.5, "pool": 5},
    {"slot": 4, "min": 5.0, "max": 5.0, "pool": 5},
)
MAX_ROTATIONS = max(int(rule["pool"]) for rule in SLOT_RULES)
MAX_MATCHES = len(SLOT_RULES) * MAX_ROTATIONS

# How far down its band a slot may reach. Without it the weakest slot fills
# with clubs nobody recognises; with it every slot still rotates, but between
# clubs near the top of its own band.
_POOL_DEPTH = 16


def _eligible(rule) -> list[tuple[int, int]]:
    out = []
    for team_id in _BY_TEAM:
        stars = team_stars(team_id)
        if stars <= 0 or stars < float(rule["min"]) or stars > float(rule["max"]):
            continue
        out.append((team_rating(team_id), int(team_id)))
    out.sort(key=lambda row: (-row[0], row[1]))
    return out


def event_pools(event_id: int) -> list[list[int]]:
    """Each slot's pool of team ids, with no club used by two slots.

    The bands OVERLAP on purpose, so the pools are filled hardest slot first
    and each one excludes what is already taken - otherwise a five-star club
    eligible for both of the top two slots could turn up as two different
    opponents in the same round.
    """
    taken: set[int] = set()
    pools: list[list[int]] = [[] for _ in SLOT_RULES]
    for index in reversed(range(len(SLOT_RULES))):
        rule = SLOT_RULES[index]
        eligible = [row for row in _eligible(rule) if row[1] not in taken]
        if not eligible:
            continue
        size = int(rule["pool"])
        window = eligible[:max(size, min(_POOL_DEPTH, len(eligible)))]
        rng = random.Random(int(event_id) * 100 + index)
        chosen = window if len(window) <= size else rng.sample(window, size)
        chosen.sort(key=lambda row: (-row[0], row[1]))
        taken.update(team for _, team in chosen)
        pools[index] = [team for _, team in chosen]
    return pools


def opponent_id(event_id: int, rotation: int, index: int) -> int:
    """*** AN OPPONENT'S id IS ITS SLOT NUMBER, 0 TO 3. NOTHING ELSE WORKS. ***

    This is what hung the client on the attract screen, and only the hook
    probes found it: every nested consumer parsed and returned cleanly, and
    then the hub handler itself never came back. The code after the parse
    (0x1803390D6) reads the opponent map's SIZE from [r12+0xe0], then walks
    `for r15d in 0..size-1` looking each slot up in that map by an INT key -
    `cmp dword [rcx+0x20], r15d` - and inserts a default node on every miss.
    The key it compares is offset 0 of a parsed entry, which is where `id`
    lands (`mov [r15], edx` at 0x180383D04). So the map has to be keyed by
    position. Ours were event*1e6 + rotation*100 + slot, so all four lookups
    missed, all four inserted empty nodes, and the loop then worked on entries
    nothing had filled.

    The id therefore carries no history, and the ROTATION is what makes a
    result unique - see result_key().
    """
    return int(index)


def result_key(rotation: int, index: int) -> str:
    """How a played match is remembered.

    The wire id is only a slot number now, so it repeats every rotation. The
    rotation is what tells this week's four opponents apart from the next
    four, and it is ours - the client never sees it.
    """
    return f"{int(rotation)}:{int(index)}"


def opponents(event_id: int, rotation: int = 0) -> list[dict]:
    """The four clubs Opponent Select shows after `rotation` refreshes."""
    out = []
    for index, pool in enumerate(event_pools(event_id)):
        if not pool:
            continue
        team_id = pool[int(rotation) % len(pool)]
        out.append({
            "slot": int(SLOT_RULES[index]["slot"]),
            "index": int(index),
            "teamId": int(team_id),
            "rating": team_rating(team_id),
            "stars": team_stars(team_id),
            "rotation": int(rotation),
            "opponentId": opponent_id(event_id, rotation, index),
            "key": result_key(rotation, index),
        })
    return out


# ----------------------------------------------------------------- scoring --

# FIFA 22 offers seven difficulties, Beginner through Ultimate, and the hub's
# three modifier arrays are capped at exactly seven entries by the parser
# (cmp rdi,7 at 0x1803398D0). The client multiplies the base below by the
# modifier for the difficulty, so these two must stay in step: the points the
# UI advertises and the points a result books are then the same number.
DIFFICULTIES = 7
MATCH_RESULT_WIN = 1000
MATCH_RESULT_LOSS = 200
WIN_POINTS = (300, 400, 500, 650, 800, 1000, 1200)
LOSS_POINTS = (60, 80, 100, 130, 160, 200, 240)
DRAW_FACTOR = 0.45
GOAL_POINTS = 25
GOAL_BONUS_CAP = 5
GOAL_PENALTY_CAP = 3
POINTS_CAP = 2000


def modifiers() -> list[float]:
    """The seven floats the hub sends for all three modifier arrays.

    Win and loss share one list because LOSS_POINTS is WIN_POINTS scaled by the
    same factor at every difficulty - 300/1000 is 60/200 - so one ratio drives
    both bases.
    """
    return [value / MATCH_RESULT_WIN for value in WIN_POINTS]


def difficulty_index(difficulty) -> int:
    """*** THE CLIENT COUNTS DIFFICULTIES FROM ZERO. ***

    Measured, not assumed. The picker lists Poczatkujacy / Amator / Nowicjusz /
    Zawodowiec / Kl. swiatowa / Legendarny / Ultimate, Jakub chose the FOURTH
    (Professional), and the request carried `sqbtMatchDifficulty: 3`. The same
    screen advertised 650 projected points, which is `matchResultWin` times our
    own modifier at index 3 - so the client indexes the modifier arrays with
    this number directly. Subtracting one, as FIFA 19's donor does, would have
    paid every match one difficulty short of what the tile promised.
    """
    try:
        value = int(difficulty)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(DIFFICULTIES - 1, value))


def opponent_multiplier(stars) -> float:
    """Opponent strength. Three stars is the reference; a half star is 4%."""
    try:
        value = float(stars)
    except (TypeError, ValueError):
        value = 3.0
    if value <= 0:
        value = 3.0
    return round(1.0 + (value - 3.0) * 0.08, 4)


def match_points(difficulty, result: str, goals_for: int = 0,
                 goals_against: int = 0, stars: float = 3.0) -> int:
    """What a finished match is worth."""
    index = difficulty_index(difficulty)
    outcome = str(result or "").upper()
    if outcome == "WIN":
        base = WIN_POINTS[index]
    elif outcome == "DRAW":
        base = int(round(WIN_POINTS[index] * DRAW_FACTOR))
    else:
        base = LOSS_POINTS[index]
    swing = int(goals_for or 0) - int(goals_against or 0)
    swing = max(-GOAL_PENALTY_CAP, min(GOAL_BONUS_CAP, swing))
    total = base * opponent_multiplier(stars) + swing * GOAL_POINTS
    # Finishing a match is never worth nothing: the difficulty floor is the
    # reward for playing it out rather than quitting.
    return min(POINTS_CAP, max(int(round(LOSS_POINTS[index] * 0.5)),
                               int(round(total))))


def result_of(goals_for: int, goals_against: int) -> str:
    if int(goals_for) > int(goals_against):
        return "WIN"
    return "DRAW" if int(goals_for) == int(goals_against) else "LOSS"


# ------------------------------------------------------------- leaderboard --

# Deterministic opponents for the Top 100. They exist so rank, tier and the
# prize bands mean something on a server with three people on it. The handles
# are generated and no entry claims to be a real person.
_PREFIX = ("Nord", "Vero", "Alta", "Rapi", "Iron", "Onyx", "Vast", "Kilo",
           "Zeta", "Umbra", "Halo", "Nova", "Ember", "Delta", "Solar",
           "Prima", "Vento", "Argo", "Lyra", "Corvo")
_SUFFIX = ("Wolves", "Rangers", "United", "Athletic", "Legion", "Dynamo",
           "Rovers", "Sporting", "Kickers", "Vipers", "Falcons", "Titans",
           "Comets", "Foxes", "Stallions")
BOARD_SIZE = 100
_BOTS: dict[tuple[int, int], list[dict]] = {}


def _bot_name(index: int) -> str:
    return "%s%s%02d" % (_PREFIX[index % len(_PREFIX)],
                         _SUFFIX[(index // len(_PREFIX)) % len(_SUFFIX)],
                         index + 1)


def bots(event_id: int, size: int = BOARD_SIZE) -> list[dict]:
    """The already-settled bot season for this event.

    Settled, not growing: a board that only advanced when the player did left
    every bot on zero, so one match made them Rank 1 and promised the Top 100
    reward while the score tier still said Bronze. Publishing the finished
    season makes the target visible - and stable - from the first minute.
    """
    key = (int(event_id), int(size))
    if key in _BOTS:
        return _BOTS[key]
    rows = []
    count = max(1, int(size))
    for index in range(count):
        ability = 1.0 - index / max(1, count - 1)
        declared = 14 + ((int(event_id) + index) % 2)
        score = wins = draws = losses = 0
        for round_index in range(MAX_MATCHES):
            rng = random.Random(int(event_id) * 1_000_003 + (index + 1) * 97_409
                                + (round_index + 1) * 65_537)
            difficulty = max(1, min(DIFFICULTIES, int(round(
                2.5 + ability * 4.5 + rng.uniform(-0.65, 0.65)))))
            stars = max(1.0, min(5.0, round((1.5 + rng.randrange(8) * 0.5) * 2) / 2))
            roll = rng.random()
            win_chance = 0.40 + ability * 0.47
            draw_chance = 0.08 + (1.0 - ability) * 0.08
            if roll < win_chance:
                outcome = "WIN"
                goals_for = rng.randint(1, 3 + int(ability * 3))
                goals_against = rng.randint(0, max(0, goals_for - 1))
                if round_index < declared:
                    wins += 1
            elif roll < win_chance + draw_chance:
                outcome = "DRAW"
                goals_for = goals_against = rng.randint(0, 3)
                if round_index < declared:
                    draws += 1
            else:
                outcome = "LOSS"
                goals_for = rng.randint(0, 2)
                goals_against = rng.randint(goals_for + 1, min(6, goals_for + 4))
                if round_index < declared:
                    losses += 1
            score += match_points(difficulty, outcome, goals_for,
                                  goals_against, stars)
        rows.append({"personaId": 8_800_000_000 + index, "name": _bot_name(index),
                     "score": score, "played": declared, "wins": wins,
                     "draws": draws, "losses": losses, "isUser": False})
    rows.sort(key=lambda row: -int(row["score"]))
    _BOTS[key] = rows
    return rows


def leaderboard(event_id: int, name: str, score: int, played: int = 0,
                wins: int = 0, draws: int = 0, losses: int = 0,
                size: int = BOARD_SIZE) -> list[dict]:
    """The board with the player on it, once they are past its floor."""
    rows = [dict(row) for row in bots(int(event_id), size)]
    score = max(0, int(score or 0))
    # The screen is called Top 100. An unqualified player in its last rows made
    # it contradict the reward tier the same hub had just published, so the
    # player joins the list only on reaching its floor.
    if score >= thresholds(int(event_id))[0]:
        rows.append({"personaId": 0, "name": str(name or "You"), "score": score,
                     "played": max(0, int(played or 0)),
                     "wins": max(0, int(wins or 0)),
                     "draws": max(0, int(draws or 0)),
                     "losses": max(0, int(losses or 0)), "isUser": True})
    rows.sort(key=lambda row: (-int(row["score"]),
                               0 if row.get("isUser") else 1, str(row["name"])))
    return rows[:max(1, int(size))]


def user_rank(event_id: int, score: int, size: int = BOARD_SIZE) -> int:
    """A 1-based Top 100 position, or 0 below its floor."""
    score = max(0, int(score or 0))
    if score < thresholds(int(event_id))[0]:
        return 0
    return sum(1 for row in bots(int(event_id), size)
               if int(row["score"]) > score) + 1


# ------------------------------------------------------------------ prizes --

# The tiers in the order the client draws them. Tier 0 is the fixed Top 100
# band (tierType "LB_RANKING_TYPE"); the rest are percentage bands. Every pack
# id below is one of ours out of FUT_PACKS, and test_sqbt checks that - an id
# the store does not have would award a pack that cannot be opened.
PRIZE_TIERS = (
    {"tierLevel": 0,  "name": "TOP 100",  "percent": 0,  "coins": 30000,
     "packs": ((6052, 1), (6045, 1))},
    {"tierLevel": 1,  "name": "ELITE 1",  "percent": 3,  "coins": 25000,
     "packs": ((6052, 1), (6038, 1))},
    {"tierLevel": 2,  "name": "ELITE 2",  "percent": 3,  "coins": 20000,
     "packs": ((6045, 1), (6038, 1))},
    {"tierLevel": 3,  "name": "ELITE 3",  "percent": 4,  "coins": 16000,
     "packs": ((6045, 1),)},
    {"tierLevel": 4,  "name": "GOLD 1",   "percent": 10, "coins": 12000,
     "packs": ((6038, 1),)},
    {"tierLevel": 5,  "name": "GOLD 2",   "percent": 10, "coins": 9000,
     "packs": ((6038, 1),)},
    {"tierLevel": 6,  "name": "GOLD 3",   "percent": 10, "coins": 7000,
     "packs": ((6034, 1),)},
    {"tierLevel": 7,  "name": "SILVER 1", "percent": 10, "coins": 5000,
     "packs": ((6034, 1),)},
    {"tierLevel": 8,  "name": "SILVER 2", "percent": 10, "coins": 3500,
     "packs": ((6027, 1),)},
    {"tierLevel": 9,  "name": "SILVER 3", "percent": 10, "coins": 2500,
     "packs": ((6027, 1),)},
    {"tierLevel": 10, "name": "BRONZE 1", "percent": 10, "coins": 1500,
     "packs": ((6021, 1),)},
    {"tierLevel": 11, "name": "BRONZE 2", "percent": 10, "coins": 1000,
     "packs": ()},
    {"tierLevel": 12, "name": "BRONZE 3", "percent": 10, "coins": 500,
     "packs": ()},
)

_THRESHOLDS: dict[int, tuple[int, ...]] = {}


def thresholds(event_id: int) -> tuple[int, ...]:
    """The point floor of every tier, Top 100 first, Bronze 3 last (zero).

    Read off the settled bot board rather than invented, so the numbers mean
    what the board shows. Elite takes 3/3/4 percent and the rest ten-percent
    bands; Bronze 3 floors at zero so any finished match earns a placement.
    """
    event_id = int(event_id)
    if event_id in _THRESHOLDS:
        return _THRESHOLDS[event_id]
    scores = [int(row["score"]) for row in bots(event_id, BOARD_SIZE)]
    cuts = (3, 6, 10, 20, 30, 40, 50, 60, 70, 80, 90)
    bands = [scores[min(len(scores), cut) - 1] for cut in cuts]
    out = tuple([bands[0]] + [int(round(value * 0.8)) for value in bands] + [0])
    _THRESHOLDS[event_id] = out
    return out


def tier_of(event_id: int, score: int) -> dict:
    """Which tier a score sits in."""
    score = max(0, int(score or 0))
    for spec, floor in zip(PRIZE_TIERS, thresholds(int(event_id))):
        if score >= int(floor):
            return dict(spec)
    return dict(PRIZE_TIERS[-1])


def tier_level(event_id: int, score: int) -> int:
    return int(tier_of(event_id, score)["tierLevel"])


def prize_tier(event_id: int, score: int) -> dict:
    return tier_of(event_id, score)

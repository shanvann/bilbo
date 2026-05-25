"""Temporal state smoothing for the primary Awake/Asleep field.

The raw per-frame eye-state classification (`eyes_open` / `eyes_closed`) is
noisy: a single mis-classified frame or a brief eye-open blink during REM
can flip the primary state. `smooth_state_temporal` only lets the primary
`state` flip to Awake/Asleep when STATE_CONFIRM_RUN consecutive raw
`eyeState` readings agree within the last STATE_CONFIRM_WINDOW baby-present
frames. Between flips, the previous smoothed state is carried forward.

The rule operates on the raw `eyeState` classifier label — not the derived
`Awake`/`Asleep` state — so pre-smoothed history is never fed back into the
smoother. Cloud-API fallback frames don't populate `eyeState`, so they
break any in-progress consecutive run (conservative: ~1% of frames).
"""

from datetime import datetime

from bilbo.config import (
    FALLING_ASLEEP_MAX_MINUTES,
    STATE_CONFIRM_RUN,
    STATE_CONFIRM_WINDOW,
    UNKNOWN_ABSORB_MAX_MINUTES,
)

_AWAKE = "Awake"
_ASLEEP = "Asleep"
_FALLING_ASLEEP = "FallingAsleep"
_UNKNOWN = "Unknown"
_NOT_PRESENT = "not_present"

_EYES_OPEN = "eyes_open"
_EYES_CLOSED = "eyes_closed"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_consecutive_run(seq: list[str | None], value: str, n: int) -> bool:
    run = 0
    for v in seq:
        if v == value:
            run += 1
            if run >= n:
                return True
        else:
            run = 0
    return False


def smooth_state_temporal(current_entry: dict, recent_entries: list[dict]) -> str:
    """Return the smoothed primary state for `current_entry`.

    Rule: within the last STATE_CONFIRM_WINDOW baby-present frames (including
    the current one), a run of STATE_CONFIRM_RUN consecutive `eyes_open`
    readings confirms Awake; same for `eyes_closed` → Asleep. Otherwise the
    previous smoothed state is carried forward (or Unknown if there is no
    usable prior state).

    `recent_entries` is the ordered tail of history (oldest → newest) and
    should contain at least STATE_CONFIRM_WINDOW - 1 entries to allow the
    rule to fire from a cold start. With fewer, the function still works but
    will fall back to carry-forward more often.

    Non-present frames are excluded from the window (they don't count toward
    the run but also don't break it — waking up, being removed, being placed
    back should let the most recent present frames still confirm the state).
    Any other `eyeState` value (`face_not_visible`, `low_confidence`,
    `not_in_bassinet`, missing) or a cloud-API fallback frame with no
    `eyeState` at all breaks the consecutive run.
    """
    # not_present is a crisp signal — bypass smoothing entirely.
    if not current_entry.get("babyPresent"):
        return _NOT_PRESENT

    # Window: the last (WINDOW - 1) baby-present entries from history plus
    # the current frame. Filter non-present out of history so absence of
    # the baby doesn't silently break a just-completed run when the baby is
    # taken out briefly and placed back.
    present_history = [e for e in recent_entries if e.get("babyPresent")]
    tail = present_history[-(STATE_CONFIRM_WINDOW - 1):]
    window = tail + [current_entry]

    eye_seq = [e.get("eyeState") for e in window]

    if _has_consecutive_run(eye_seq, _EYES_OPEN, STATE_CONFIRM_RUN):
        return _AWAKE
    if _has_consecutive_run(eye_seq, _EYES_CLOSED, STATE_CONFIRM_RUN):
        return _ASLEEP

    # No confirmed flip — carry forward the most recent smoothed state from
    # history. Only Awake/Asleep are valid carry-forward targets; a prior
    # not_present means a fresh placement and we restart from Unknown.
    for prev in reversed(recent_entries):
        prev_state = prev.get("state")
        if prev_state in (_AWAKE, _ASLEEP):
            return prev_state
        if prev_state == _NOT_PRESENT:
            break
    return _UNKNOWN


def unknown_prefix_to_absorb(current_entry: dict,
                              recent_entries: list[dict],
                              max_minutes: float = UNKNOWN_ABSORB_MAX_MINUTES
                              ) -> list[dict]:
    """If `current_entry` is a just-confirmed Awake, return the preceding
    contiguous Unknown+baby-present run that should be retroactively flipped
    to Awake.

    Walks `recent_entries` from newest to oldest, collecting entries where
    `state == "Unknown"` and `babyPresent` is true. Stops at:
      - any other state (Asleep, Awake, not_present, None)
      - a frame where the baby is not present
      - the beginning of history

    After collection, the span from the oldest Unknown frame in the run to
    `current_entry` is compared against `max_minutes`. If it's within the
    budget, the full run is returned (oldest → newest) for the caller to
    rewrite. Otherwise an empty list is returned — the run is too long to
    absorb and the Unknown block stands.

    Returns an empty list if `current_entry.state != "Awake"` so callers
    can invoke this unconditionally on every smoothed result.
    """
    if current_entry.get("state") != _AWAKE:
        return []

    run: list[dict] = []
    for prev in reversed(recent_entries):
        if prev.get("state") != _UNKNOWN:
            break
        if not prev.get("babyPresent"):
            break
        run.append(prev)

    if not run:
        return []

    # run is newest → oldest; the oldest is the last element.
    oldest = run[-1]
    oldest_ts = _parse_ts(oldest.get("timestamp"))
    current_ts = _parse_ts(current_entry.get("timestamp"))
    if not oldest_ts or not current_ts:
        return []

    span_minutes = (current_ts - oldest_ts).total_seconds() / 60.0
    if span_minutes > max_minutes:
        return []

    run.reverse()  # return oldest → newest
    return run


def putdown_prefix_to_absorb(current_entry: dict,
                              recent_entries: list[dict],
                              max_minutes: float = FALLING_ASLEEP_MAX_MINUTES
                              ) -> tuple[list[dict], str | None]:
    """If `current_entry` is a just-confirmed Asleep and the preceding
    contiguous Unknown+baby-present run is bookended by an out-of-bassinet
    frame, classify the Unknown run:

      - span ≤ max_minutes → FallingAsleep (the putdown-to-sleep case)
      - span  > max_minutes → Awake (crib-awake, eventually dozed off)

    Returns `(run_oldest_to_newest, new_state)` when the pattern matches,
    or `([], None)` otherwise. The caller is expected to rewrite
    `state` on every entry in the run to `new_state`.

    Pattern match rules (walking newest → oldest from `recent_entries`):
      1. Skip & collect frames where `state == "Unknown"` AND
         `babyPresent == True`.
      2. The first frame that breaks that chain must have
         `babyPresent == False`. Any other state (Awake, Asleep,
         FallingAsleep, another Unknown with `babyPresent == False`
         which we treat as `not_present`) means the pattern doesn't
         match — this rule is specifically for the put-down case.
      3. The run must be non-empty (an Asleep that lands one frame
         after not_present, with no Unknown in between, has nothing
         for us to rewrite).

    If `recent_entries` runs out before we hit a terminator we bail —
    we can't assert the `not_present` precondition from truncated
    history, so we leave the run alone.
    """
    if current_entry.get("state") != _ASLEEP:
        return [], None

    run: list[dict] = []
    terminator: dict | None = None
    for prev in reversed(recent_entries):
        if prev.get("state") == _UNKNOWN and prev.get("babyPresent"):
            run.append(prev)
            continue
        terminator = prev
        break

    if terminator is None:
        return [], None  # history exhausted without a bookend
    if terminator.get("babyPresent"):
        return [], None  # bookend isn't not_present — not a putdown pattern
    if not run:
        return [], None  # Asleep directly after not_present, nothing to rewrite

    oldest = run[-1]
    oldest_ts = _parse_ts(oldest.get("timestamp"))
    current_ts = _parse_ts(current_entry.get("timestamp"))
    if not oldest_ts or not current_ts:
        return [], None

    span_minutes = (current_ts - oldest_ts).total_seconds() / 60.0
    new_state = _FALLING_ASLEEP if span_minutes <= max_minutes else _AWAKE

    run.reverse()  # oldest → newest for caller convenience
    return run, new_state

#!/usr/bin/env python3
"""Backfill phase-level batting-versus-bowling statistics for selected matches."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import date
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


LOGGER = logging.getLogger(__name__)

PHASES = {
    "T20": ((1, 36, "POWERPLAY"), (37, 96, "MIDDLE_OVERS"), (97, 120, "DEATH_OVERS")),
    "ODI": ((1, 60, "POWERPLAY"), (61, 240, "MIDDLE_OVERS"), (241, 300, "DEATH_OVERS")),
}
PHASE_ORDER = {"POWERPLAY": 0, "MIDDLE_OVERS": 1, "DEATH_OVERS": 2, "NA": 3}
BOWLING_STYLES = {
    "right arm fast": ("Right-arm fast", "PACE"),
    "right arm fast medium": ("Right-arm fast-medium", "PACE"),
    "right arm medium": ("Right-arm medium", "PACE"),
    "right arm offbreak": ("Right-arm offbreak", "SPIN"),
    "right arm legbreak": ("Right-arm legbreak", "SPIN"),
    "left arm fast": ("Left-arm fast", "PACE"),
    "left arm fast medium": ("Left-arm fast-medium", "PACE"),
    "left arm medium": ("Left-arm medium", "PACE"),
    "left arm orthodox": ("Left-arm orthodox", "SPIN"),
    "left arm wrist spin": ("Left-arm wrist-spin", "SPIN"),
}
TARGET_COLUMNS = (
    "match_id",
    "innings_id",
    "match_format",
    "batting_team_id",
    "batsman_id",
    "bowler_id",
    "bowling_team_id",
    "phase",
    "phase_from_ball_nbr",
    "phase_to_ball_nbr",
    "runs",
    "balls",
    "fours",
    "sixes",
    "dots",
    "dismissal_type",
    "venue_id",
    "bowling_type",
    "batting_hand",
    "batting_order",
    "bowling_technique",
)


class MatchCannotBeBackfilled(ValueError):
    """Raised when source records cannot safely produce target rows."""

    def __init__(self, message: str, reason: str = "source-data") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DatabaseConfig:
    mariadb_host: str
    mariadb_port: int
    mariadb_user: str
    mariadb_password: str
    mariadb_database: str
    cassandra_contacts: tuple[str, ...]
    cassandra_port: int
    cassandra_tunnel_mode: bool
    cassandra_keyspace: str


def as_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    if hasattr(record, "_asdict"):
        return dict(record._asdict())
    if hasattr(record, "_fields"):
        return {field: getattr(record, field) for field in record._fields}
    raise TypeError(f"Unsupported database record: {type(record)!r}")


def normalise_style(style: str) -> str:
    normalised = " ".join(style.lower().replace("-", " ").split())
    return normalised.replace("off break", "offbreak").replace("leg break", "legbreak")


def bowling_style_details(style: str | None) -> tuple[str | None, str | None]:
    if not style:
        return None, None
    normalised = normalise_style(style)
    try:
        return BOWLING_STYLES[normalised]
    except KeyError as error:
        raise MatchCannotBeBackfilled(
            f"unrecognised bowling style: {style!r}", "unrecognised bowling style"
        ) from error


def bowling_type(style: str | None) -> str | None:
    return bowling_style_details(style)[1]


def batting_hand(style: str | None) -> str:
    if style == "RIGHT":
        return "RIGHT"
    if style == "LEFT":
        return "LEFT"
    raise MatchCannotBeBackfilled(
        f"unrecognised batting style: {style!r}", "unrecognised batting style"
    )


def over_ball_to_ball_number(value: Any, *, allow_rounding_noise: bool = False) -> int:
    try:
        overs = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid over.ball value: {value!r}") from error
    if not overs.is_finite() or overs < 0:
        raise ValueError(f"invalid over.ball value: {value!r}")
    try:
        rounded = overs.quantize(Decimal("0.1"))
        if overs != rounded and (
            not allow_rounding_noise or abs(overs - rounded) > Decimal("0.000001")
        ):
            raise ValueError(f"invalid over.ball value: {value!r}")
        overs = rounded
    except InvalidOperation as error:
        raise ValueError(f"invalid over.ball value: {value!r}") from error
    full_overs = int(overs)
    remaining_balls = int((overs - full_overs) * 10)
    if remaining_balls > 6:
        raise ValueError(f"invalid over.ball value: {value!r}")
    return full_overs * 6 + remaining_balls


def opening_powerplay(scorecard: Mapping[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    innings_id = scorecard["innings_id"]
    pp_data = scorecard.get("pp_data")
    if pp_data is None:
        pp_data = {}
    if not isinstance(pp_data, Mapping):
        raise MatchCannotBeBackfilled(f"innings {innings_id}: pp_data is invalid", "invalid-powerplay")
    opening = []
    for key, source in pp_data.items():
        if not str(key).startswith("pp_"):
            continue
        try:
            powerplay = as_dict(source)
            start = over_ball_to_ball_number(powerplay.get("pp_overs_from"), allow_rounding_noise=True)
        except (TypeError, ValueError) as error:
            raise MatchCannotBeBackfilled(
                f"innings {innings_id}: {key} is invalid: {error}", "invalid-powerplay"
            ) from error
        if start == 1:
            opening.append((key, powerplay))
    if len(opening) > 1:
        raise MatchCannotBeBackfilled(
            f"innings {innings_id}: multiple powerplay entries start at ball 1: "
            f"{', '.join(str(key) for key, _ in opening)}", "invalid-powerplay",
        )
    return opening[0] if opening else (None, None)


def innings_phases(
    scorecard: Mapping[str, Any], match_format: str, allotted_balls: int | None,
    last_ball_nbr: int | None = None, missing_pp_as_na: bool = False,
) -> tuple[tuple[int, int, str], ...]:
    innings_id = scorecard["innings_id"]
    pp_key, powerplay = opening_powerplay(scorecard)
    if powerplay is None:
        if missing_pp_as_na:
            if last_ball_nbr is None:
                raise MatchCannotBeBackfilled(
                    f"innings {innings_id}: opening powerplay is missing and no eligible ball number is available for NA phase",
                    "no-eligible-ball",
                )
            limit = allotted_balls if allotted_balls is not None else PHASES[match_format][-1][1]
            if last_ball_nbr > limit:
                raise MatchCannotBeBackfilled(
                    f"innings {innings_id}: last eligible ball {last_ball_nbr} exceeds innings limit {limit}",
                    "ball-beyond-limit",
                )
            return ((1, last_ball_nbr, "NA"),)
        raise MatchCannotBeBackfilled(
            f"innings {innings_id}: opening powerplay is missing", "missing-powerplay"
        )
    if str(powerplay.get("pp_type", "")).casefold() != "mandatory":
        raise MatchCannotBeBackfilled(
            f"innings {innings_id}: {pp_key} is not mandatory", "invalid-powerplay"
        )
    try:
        pp_start = over_ball_to_ball_number(powerplay.get("pp_overs_from"), allow_rounding_noise=True)
        pp_end = over_ball_to_ball_number(powerplay.get("pp_overs_to"), allow_rounding_noise=True)
    except ValueError as error:
        raise MatchCannotBeBackfilled(f"innings {innings_id}: {error}", "invalid-powerplay") from error
    if pp_start != 1 or pp_end < pp_start:
        raise MatchCannotBeBackfilled(
            f"innings {innings_id}: invalid {pp_key} ball range {pp_start}-{pp_end}",
            "invalid-powerplay",
        )
    if allotted_balls == PHASES[match_format][-1][1] and pp_end == PHASES[match_format][0][1]:
        return PHASES[match_format]
    if allotted_balls is None:
        phases = PHASES[match_format]
        if pp_end != phases[0][1]:
            limit = phases[-1][1]
            if pp_end > limit:
                raise MatchCannotBeBackfilled(
                    f"innings {innings_id}: nonstandard {pp_key} ends at ball {pp_end} beyond format limit {limit}",
                    "ball-beyond-limit",
                )
            if last_ball_nbr is None or pp_end > last_ball_nbr:
                raise MatchCannotBeBackfilled(
                    f"innings {innings_id}: nonstandard {pp_key} ends at ball {pp_end}, "
                    f"last eligible ball is {last_ball_nbr}, format limit is {limit}",
                    "powerplay-beyond-play",
                )
            if last_ball_nbr > limit:
                raise MatchCannotBeBackfilled(
                    f"innings {innings_id}: last eligible ball {last_ball_nbr} exceeds format limit {limit}",
                    "ball-beyond-limit",
                )
            if pp_end == last_ball_nbr:
                return ((1, pp_end, "POWERPLAY"),)
            return ((1, pp_end, "POWERPLAY"), (pp_end + 1, last_ball_nbr, "NA"))
        return phases
    if pp_end >= allotted_balls:
        raise MatchCannotBeBackfilled(
            f"innings {innings_id}: {pp_key} ending at ball {pp_end} is at or beyond "
            f"reduced-overs allotment of {allotted_balls} balls "
            f"(last eligible ball {last_ball_nbr})",
            "invalid-powerplay",
        )
    death_balls = (allotted_balls - pp_end + 4) // 5
    death_start = allotted_balls - death_balls + 1
    phases = [(1, pp_end, "POWERPLAY")]
    if pp_end + 1 < death_start:
        phases.append((pp_end + 1, death_start - 1, "MIDDLE_OVERS"))
    phases.append((death_start, allotted_balls, "DEATH_OVERS"))
    return tuple(phases)


def phase_for(
    phases: tuple[tuple[int, int, str], ...], ball_number: int, innings_id: int
) -> tuple[str, int, int]:
    for start, end, phase in phases:
        if start <= ball_number <= end:
            return phase, start, end
    raise MatchCannotBeBackfilled(
        f"innings {innings_id}: ball number {ball_number} is outside the innings phase definition",
        "ball-beyond-limit",
    )


def phase_behavior(
    phases: tuple[tuple[int, int, str], ...], match_format: str, reduced_overs: bool
) -> str:
    if phases[0][2] == "NA":
        return "missing-powerplay-na"
    if reduced_overs:
        return "reduced-overs"
    if phases[0][1] != PHASES[match_format][0][1]:
        return "regular-powerplay-na"
    return "regular-fixed"


def has_highlight(highlights_id: int | None, code: int) -> bool:
    if not highlights_id or highlights_id <= 0:
        return False
    remaining = highlights_id
    for candidate in (8192, 4096, 2048, 1024, 512, 256, 128, 64, 32, 16, 8, 4, 2, 1):
        if candidate > 1 and remaining // candidate == 1:
            if candidate == code:
                return True
            remaining -= candidate
    return False


def player_details(match_info: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    players: dict[int, dict[str, Any]] = {}
    for standard_name, external_name in (("team1", "external_team1"), ("team2", "external_team2")):
        team = match_info.get(external_name) or match_info.get(standard_name)
        if not team:
            raise MatchCannotBeBackfilled(f"{standard_name} is missing", "missing team metadata")
        for player in as_dict(team).get("player_details") or []:
            player_record = as_dict(player)
            if player_record.get("is_support_staff") is True or player_record.get("is_unavailable") is True:
                continue
            players[player_record["id"]] = player_record
    return players


def batting_orders(scorecards: Mapping[int, Mapping[str, Any]]) -> dict[tuple[int, int], int]:
    orders: dict[tuple[int, int], int] = {}
    for innings_id, scorecard in scorecards.items():
        for key, value in (scorecard.get("batsmen_data") or {}).items():
            if not key.startswith("bat_"):
                continue
            try:
                order = int(key.removeprefix("bat_"))
            except ValueError as error:
                raise MatchCannotBeBackfilled(
                    f"invalid batting order key: {key!r}", "invalid batting order"
                ) from error
            batter = as_dict(value)
            orders[(innings_id, batter["bat_id"])] = order
    return orders


def is_run_out(event: Mapping[str, Any]) -> bool:
    return str(event.get("wkt_code", "")).casefold() == "run out"


BALL_LOG_FIELDS = (
    "innings_id", "time_score", "ball_nbr", "bat_striker_id", "bowl_striker_id",
    "legal_runs", "total_runs", "highlights_id", "wkt_code", "extra_code",
)


def ball_snapshot(event: Mapping[str, Any]) -> dict[str, Any]:
    return {field: event.get(field) for field in BALL_LOG_FIELDS}


def event_failure(event: Mapping[str, Any], message: str, reason: str) -> MatchCannotBeBackfilled:
    return MatchCannotBeBackfilled(f"{message}; ball={ball_snapshot(event)}", reason)


def calculate_matchup_rows(
    match_info_source: Any,
    scorecard_sources: Iterable[Any],
    event_sources: Iterable[Any],
    maria_bowling_styles: Mapping[int, str] | None = None,
    allotted_balls_by_innings: Mapping[int, int] | None = None,
    missing_pp_as_na: bool = False,
    phase_behaviors: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Calculate target-table rows without performing database mutations."""

    match_info = as_dict(match_info_source)
    match_format = match_info.get("match_format")
    if match_format not in PHASES:
        raise MatchCannotBeBackfilled(
            f"unsupported match format: {match_format!r}",
            f"unsupported format {match_format!r}",
        )

    venue = as_dict(match_info.get("venue") or {})
    if venue.get("id") is None:
        raise MatchCannotBeBackfilled("venue ID is missing", "missing venue")
    scorecards = {as_dict(source)["innings_id"]: as_dict(source) for source in scorecard_sources}
    if allotted_balls_by_innings is not None:
        missing = set(scorecards) - set(allotted_balls_by_innings)
        extra = set(allotted_balls_by_innings) - set(scorecards)
        if missing or extra:
            raise MatchCannotBeBackfilled(
                f"match {match_info['match_id']}: overs file/scorecard innings mismatch; "
                f"missing innings {sorted(missing)}, extra innings {sorted(extra)}",
                "csv-scorecard-mismatch",
            )
    if not scorecards:
        raise MatchCannotBeBackfilled("no scorecards found", "missing scorecards")
    eligible_events = []
    for source in event_sources:
        event = as_dict(source)
        if event.get("extra_code") in {"Wd", "WD"} or event.get("ball_nbr") == 0:
            continue
        ball_nbr = event.get("ball_nbr")
        if not isinstance(ball_nbr, int) or isinstance(ball_nbr, bool) or ball_nbr < 1:
            raise event_failure(
                event,
                f"innings {event.get('innings_id')}: invalid ball_nbr {ball_nbr!r}",
                "invalid-event",
            )
        innings_id = event.get("innings_id")
        if not isinstance(innings_id, int) or isinstance(innings_id, bool) or innings_id < 1:
            raise event_failure(
                event,
                f"invalid innings_id {innings_id!r} at ball {ball_nbr}", "invalid-event"
            )
        for field in ("bat_striker_id", "bowl_striker_id"):
            if event.get(field) is None:
                raise event_failure(
                    event,
                    f"innings {innings_id}: missing {field} at ball {ball_nbr}",
                    "invalid-event",
                )
        eligible_events.append(event)
    last_ball_by_innings: dict[int, int] = {}
    for event in eligible_events:
        innings_id = event["innings_id"]
        last_ball_by_innings[innings_id] = max(
            last_ball_by_innings.get(innings_id, 0), event["ball_nbr"]
        )
    phase_definitions = {
        innings_id: innings_phases(
            scorecard, match_format,
            allotted_balls_by_innings[innings_id] if allotted_balls_by_innings is not None else None,
            last_ball_by_innings.get(innings_id), missing_pp_as_na,
        )
        for innings_id, scorecard in scorecards.items()
    }
    for innings_id, phases in sorted(phase_definitions.items()):
        if phases[0][2] == "NA":
            LOGGER.warning(
                "match %s innings %s: opening powerplay is missing; using NA for balls 1-%s",
                match_info["match_id"], innings_id, phases[0][1],
            )
        elif allotted_balls_by_innings is None and phases[0][1] != PHASES[match_format][0][1]:
            LOGGER.info(
                "match %s innings %s: nonstandard powerplay expected_pp_end=%s "
                "actual_pp_end=%s last_eligible_ball=%s phases %s",
                match_info["match_id"], innings_id, PHASES[match_format][0][1],
                phases[0][1], last_ball_by_innings.get(innings_id),
                ", ".join(f"{phase}={start}-{end}" for start, end, phase in phases),
            )
        LOGGER.info(
            "match %s innings %s: %s phases %s",
            match_info["match_id"], innings_id,
            phase_behavior(phases, match_format,
                           allotted_balls_by_innings is not None and phases != PHASES[match_format]),
            ", ".join(f"{phase}={start}-{end}" for start, end, phase in phases),
        )
        if LOGGER.isEnabledFor(logging.DEBUG) and phases[0][2] != "NA":
            pp_key, powerplay = opening_powerplay(scorecards[innings_id])
            LOGGER.debug(
                "phase source match_id=%s innings_id=%s pp_key=%s pp_overs_from=%s pp_overs_to=%s "
                "allotted_balls=%s",
                match_info["match_id"], innings_id,
                pp_key,
                powerplay["pp_overs_from"], powerplay["pp_overs_to"],
                allotted_balls_by_innings[innings_id] if allotted_balls_by_innings is not None
                else (phases[-1][1] if len(phases) == 3 else None),
            )

    players = player_details(match_info)
    maria_bowling_styles = maria_bowling_styles or {}
    bowling_styles = {
        player_id: player.get("bowling_style") or maria_bowling_styles.get(player_id)
        for player_id, player in players.items()
    }
    if LOGGER.isEnabledFor(logging.DEBUG):
        for player_id, player in players.items():
            LOGGER.debug(
                "playersMap match_id=%s player_id=%s name=%r batting_style=%r source_bowling_style=%r",
                match_info["match_id"],
                player_id,
                player.get("name") or player.get("full_name"),
                player.get("batting_style"),
                bowling_styles[player_id],
            )
    orders = batting_orders(scorecards)
    aggregates: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    LOGGER.info(
        "match %s: processing %s eligible ball-by-ball events",
        match_info["match_id"], len(eligible_events),
    )
    event_contexts: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], int, tuple[str, int, int]]] = []

    for event in sorted(eligible_events, key=lambda item: str(item.get("time_score", ""))):
        innings_id = event["innings_id"]
        scorecard = scorecards.get(innings_id)
        if scorecard is None:
            raise event_failure(
                event,
                f"no scorecard found for innings {innings_id}", "missing scorecard"
            )
        batter_id = event["bat_striker_id"]
        bowler_id = event["bowl_striker_id"]
        batter = players.get(batter_id)
        bowler = players.get(bowler_id)
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "processing match_id=%s bowler_id=%s bowler_name=%r event=%s",
                match_info["match_id"],
                bowler_id,
                (bowler.get("name") or bowler.get("full_name")) if bowler else None,
                ball_snapshot(event),
            )
        if batter is None or bowler is None:
            raise event_failure(
                event,
                f"missing player metadata for batter {batter_id} or bowler {bowler_id}",
                "missing player metadata",
            )
        batting_order = orders.get((innings_id, batter_id))
        if batting_order is None:
            raise event_failure(
                event,
                f"missing batting order for batter {batter_id} in innings {innings_id}",
                "missing batting order",
            )
        try:
            phase = phase_for(phase_definitions[innings_id], event["ball_nbr"], innings_id)
        except MatchCannotBeBackfilled as error:
            raise event_failure(event, str(error), error.reason) from error
        event_contexts.append((event, scorecard, batter, bowler, batting_order, phase))

    missing_style_bowlers: set[int] = set()
    for event, scorecard, batter, bowler, batting_order, (phase, phase_start, phase_end) in event_contexts:
        innings_id = event["innings_id"]
        batter_id = event["bat_striker_id"]
        bowler_id = event["bowl_striker_id"]
        key = (innings_id, batter_id, bowler_id, phase)
        style = bowling_styles[bowler_id]
        if not style and bowler_id not in missing_style_bowlers:
            LOGGER.warning(
                "match %s: bowler %s (%s) has no bowling style in Cassandra or MariaDB; "
                "inserting NULL bowling_type and bowling_technique",
                match_info["match_id"], bowler_id,
                bowler.get("name") or bowler.get("full_name") or "name unavailable",
            )
            missing_style_bowlers.add(bowler_id)
        try:
            technique, bowler_type = bowling_style_details(style)
        except MatchCannotBeBackfilled as error:
            raise event_failure(
                event,
                f"bowler {bowler_id} ({bowler.get('name') or bowler.get('full_name')}) "
                f"at innings {innings_id}, ball {event['ball_nbr']}: {error}",
                error.reason,
            ) from error
        try:
            hand = batting_hand(batter.get("batting_style"))
        except MatchCannotBeBackfilled as error:
            raise event_failure(event, str(error), error.reason) from error
        aggregate = aggregates.setdefault(
            key,
            {
                "match_id": match_info["match_id"],
                "innings_id": innings_id,
                "match_format": match_format,
                "batting_team_id": scorecard["bat_team_id"],
                "batsman_id": batter_id,
                "bowler_id": bowler_id,
                "bowling_team_id": scorecard["bowl_team_id"],
                "phase": phase,
                "phase_from_ball_nbr": phase_start,
                "phase_to_ball_nbr": phase_end,
                "runs": 0,
                "balls": 0,
                "fours": 0,
                "sixes": 0,
                "dots": 0,
                "dismissal_type": None,
                "venue_id": venue["id"],
                "bowling_type": bowler_type,
                "batting_hand": hand,
                "batting_order": batting_order,
                "bowling_technique": technique,
            },
        )
        aggregate["runs"] += event.get("legal_runs") or 0
        aggregate["balls"] += 1
        aggregate["fours"] += int(has_highlight(event.get("highlights_id"), 2))
        aggregate["sixes"] += int(has_highlight(event.get("highlights_id"), 4))
        aggregate["dots"] += int(
            (event.get("legal_runs") or 0) == 0 and (event.get("total_runs") or 0) == 0
        )
        if event.get("wkt_code") not in {None, "NONE"} and not is_run_out(event):
            aggregate["dismissal_type"] = event["wkt_code"]

    if phase_behaviors is not None:
        if allotted_balls_by_innings is not None and any(
            limit < PHASES[match_format][-1][1] for limit in allotted_balls_by_innings.values()
        ):
            phase_behaviors.add("reduced-overs")
        for phases in phase_definitions.values():
            phase_behaviors.add(
                phase_behavior(phases, match_format,
                               allotted_balls_by_innings is not None and phases != PHASES[match_format])
            )

    return sorted(
        aggregates.values(),
        key=lambda row: (
            row["innings_id"],
            PHASE_ORDER[row["phase"]],
            row["batsman_id"],
            row["bowler_id"],
        ),
    )


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"environment variable {name} is required")
    return value


def cassandra_contact_configuration(value: str, default_port: int) -> tuple[tuple[str, ...], int]:
    contacts: list[str] = []
    ports: set[int] = set()
    for raw_contact in value.split(","):
        contact = raw_contact.strip()
        if not contact:
            continue
        host, separator, port_text = contact.rpartition(":")
        if separator:
            if not host or not port_text.isdecimal():
                raise ValueError(f"invalid Cassandra contact point: {contact!r}")
            port = int(port_text)
        else:
            host = contact
            port = default_port
        if not 1 <= port <= 65535:
            raise ValueError(f"invalid Cassandra port: {port}")
        contacts.append(host)
        ports.add(port)
    if not contacts:
        raise ValueError("CASSANDRA_CONTACT_POINTS must contain at least one host")
    if len(ports) != 1:
        raise ValueError("all Cassandra contact points must use the same port")
    return tuple(contacts), ports.pop()


def environment_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.casefold() in {"1", "true", "yes"}:
        return True
    if value.casefold() in {"0", "false", "no"}:
        return False
    raise ValueError(f"environment variable {name} must be true or false")


def database_config() -> DatabaseConfig:
    cassandra_contacts, cassandra_port = cassandra_contact_configuration(
        required_environment("CASSANDRA_CONTACT_POINTS"),
        int(os.environ.get("CASSANDRA_PORT", "9042")),
    )
    return DatabaseConfig(
        mariadb_host=required_environment("MARIADB_HOST"),
        mariadb_port=int(os.environ.get("MARIADB_PORT", "3306")),
        mariadb_user=required_environment("MARIADB_USER"),
        mariadb_password=required_environment("MARIADB_PASSWORD"),
        mariadb_database=required_environment("MARIADB_DATABASE"),
        cassandra_contacts=cassandra_contacts,
        cassandra_port=cassandra_port,
        cassandra_tunnel_mode=environment_bool("CASSANDRA_TUNNEL_MODE"),
        cassandra_keyspace=required_environment("CASSANDRA_KEYSPACE"),
    )


def fetch_match_sources(
    cassandra_session: Any, match_id: int
) -> tuple[Any, list[Any], list[Any]]:
    match_rows = list(cassandra_session.execute("SELECT * FROM match_info WHERE match_id = %s", (match_id,)))
    if not match_rows:
        raise MatchCannotBeBackfilled(
            f"match_info is missing for match {match_id}", "missing match info"
        )
    LOGGER.info("match %s: found match_info", match_id)
    scorecards = list(
        cassandra_session.execute(
            "SELECT * FROM match_bbb_full_scorecard WHERE match_id = %s GROUP BY innings_id",
            (match_id,),
        )
    )
    innings_ids = sorted({as_dict(scorecard)["innings_id"] for scorecard in scorecards})
    events: list[Any] = []
    for innings_id in innings_ids:
        LOGGER.info("match %s: found scorecard for innings %s", match_id, innings_id)
        innings_events = list(
            cassandra_session.execute(
                "SELECT * FROM match_bbb_commentary WHERE match_id = %s AND innings_id = %s",
                (match_id, innings_id),
            )
        )
        LOGGER.info(
            "match %s innings %s: fetched %s ball-by-ball commentary events",
            match_id, innings_id, len(innings_events),
        )
        events.extend(innings_events)
    return match_rows[0], scorecards, events


def replace_rows(mariadb_connection: Any, match_id: int, rows: list[Mapping[str, Any]]) -> None:
    cursor = mariadb_connection.cursor()
    cursor.execute("SELECT * FROM batting_vs_bowling_matchups WHERE match_id = %s", (match_id,))
    existing_rows = cursor.fetchall()
    print(f"match {match_id}: existing rows to delete: {len(existing_rows)}", flush=True)
    for existing in existing_rows:
        serialized = json.dumps(existing, default=str, sort_keys=True)
        print(serialized, flush=True)
        LOGGER.debug("planned DELETE match_id=%s row=%s", match_id, serialized)
    if LOGGER.isEnabledFor(logging.DEBUG):
        for row in rows:
            LOGGER.debug("planned INSERT match_id=%s row=%s", match_id, json.dumps(row, default=str, sort_keys=True))
    placeholders = ", ".join("%s" for _ in TARGET_COLUMNS)
    columns = ", ".join(TARGET_COLUMNS)
    insert_sql = f"INSERT INTO batting_vs_bowling_matchups ({columns}) VALUES ({placeholders})"
    try:
        mariadb_connection.begin()
        LOGGER.info("match %s: deleting existing matchup rows: %s", match_id, len(existing_rows))
        cursor.execute("DELETE FROM batting_vs_bowling_matchups WHERE match_id = %s", (match_id,))
        LOGGER.info("match %s: inserting matchup rows: %s", match_id, len(rows))
        cursor.executemany(insert_sql, [tuple(row[column] for column in TARGET_COLUMNS) for row in rows])
        mariadb_connection.commit()
    except Exception:
        mariadb_connection.rollback()
        raise
    finally:
        cursor.close()


def fetch_mariadb_bowling_styles(mariadb_connection: Any, player_ids: Iterable[int]) -> dict[int, str]:
    ids = tuple(sorted(set(player_ids)))
    if not ids:
        return {}
    placeholders = ", ".join("%s" for _ in ids)
    cursor = mariadb_connection.cursor()
    try:
        cursor.execute(
            "SELECT pm.id, ps.StyleDesc FROM stats_import_player_master pm "
            "JOIN stats_import_player_style ps ON pm.bowl_playerstyle_id = ps.id "
            f"WHERE pm.id IN ({placeholders})",
            ids,
        )
        return {row["id"]: row["StyleDesc"] for row in cursor.fetchall()}
    finally:
        cursor.close()


def reduced_overs_by_match(path: str | None) -> dict[int, dict[int, int]]:
    if path is None:
        return {}
    innings_by_match: dict[int, dict[int, int]] = {}
    try:
        overs_file = open(path, newline="", encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read reduced overs file {path}: {error}") from error
    with overs_file:
        reader = csv.reader(overs_file, strict=True)
        try:
            header = next(reader, None)
        except csv.Error as error:
            raise ValueError(f"invalid CSV on line {reader.line_num} of {path}: {error}") from error
        if header != ["matchId", "inningsId", "scheduledOvers", "reducedOvers"]:
            raise ValueError(f"invalid reduced overs file header on line 1 of {path}")
        try:
            records = [(reader.line_num, row) for row in reader]
        except csv.Error as error:
            raise ValueError(f"invalid CSV on line {reader.line_num} of {path}: {error}") from error
        for line_number, row in records:
            if len(row) != 4:
                raise ValueError(f"invalid reduced overs row on line {line_number} of {path}: {row!r}")
            try:
                match_id, innings_id = (int(value.strip()) for value in row[:2])
                scheduled_balls = over_ball_to_ball_number(row[2].strip())
                reduced_balls = over_ball_to_ball_number(row[3].strip())
            except ValueError as error:
                raise ValueError(
                    f"invalid reduced overs row on line {line_number} of {path}: {row!r}: {error}"
                ) from error
            if match_id <= 0 or innings_id <= 0 or reduced_balls <= 0 or scheduled_balls <= 0:
                raise ValueError(f"invalid reduced overs row on line {line_number} of {path}: {row!r}")
            if reduced_balls > scheduled_balls:
                raise ValueError(
                    f"reducedOvers exceeds scheduledOvers on line {line_number} of {path}: {row!r}"
                )
            innings = innings_by_match.setdefault(match_id, {})
            if innings_id in innings:
                raise ValueError(
                    f"duplicate matchId,inningsId on line {line_number} of {path}: {row!r}"
                )
            innings[innings_id] = reduced_balls
            LOGGER.debug(
                "overs file match_id=%s innings_id=%s scheduled_balls=%s allotted_balls=%s",
                match_id, innings_id, scheduled_balls, reduced_balls,
            )
    return innings_by_match


def selected_match_ids(mariadb_connection: Any, arguments: argparse.Namespace) -> list[int]:
    if arguments.match_id is not None:
        match_ids = arguments.match_id
    else:
        cursor = mariadb_connection.cursor()
        try:
            if arguments.series_id is not None:
                cursor.execute(
                    "SELECT id FROM krik_match_archive WHERE seriesid = %s ORDER BY id",
                    (arguments.series_id,),
                )
            else:
                cursor.execute(
                    "SELECT id FROM krik_match_archive WHERE startdt >= %s AND startdt <= %s ORDER BY id",
                    (arguments.from_date.isoformat(), arguments.to_date.isoformat()),
                )
            match_ids = [row["id"] for row in cursor.fetchall()]
        finally:
            cursor.close()
    return match_ids


def parse_match_ids(value: str) -> list[int]:
    match_ids: list[int] = []
    seen: set[int] = set()
    for part in value.split(","):
        candidate = part.strip()
        if not candidate.isascii() or not candidate.isdecimal():
            raise argparse.ArgumentTypeError("provide positive match IDs separated by commas")
        match_id = int(candidate)
        if match_id == 0:
            raise argparse.ArgumentTypeError("provide positive match IDs separated by commas")
        if match_id in seen:
            raise argparse.ArgumentTypeError(f"duplicate match ID: {match_id}")
        match_ids.append(match_id)
        seen.add(match_id)
    return match_ids


def log_match_groups(title: str, groups: Mapping[str, Iterable[int]]) -> None:
    if not groups:
        LOGGER.info("%s: none", title)
        return
    LOGGER.info("%s:", title)
    grouped = sorted(
        ((reason, sorted(set(ids))) for reason, ids in groups.items()),
        key=lambda item: (-len(item[1]), item[0]),
    )
    for reason, ids in grouped:
        count = len(ids)
        label = reason.replace("-na", " NA").replace("-", " ")
        LOGGER.info("  %s (%s %s)", label, count, "match" if count == 1 else "matches")
        for offset in range(0, count, 12):
            LOGGER.info(
                "    match IDs: %s",
                ", ".join(str(match_id) for match_id in ids[offset:offset + 12]),
            )


def run_backfill(arguments: argparse.Namespace, config: DatabaseConfig) -> bool:
    overs_plan = reduced_overs_by_match(arguments.reduced_overs_file)
    LOGGER.info(
        "reduced-overs file: %s; matches=%s innings=%s",
        arguments.reduced_overs_file or "none",
        len(overs_plan), sum(len(innings) for innings in overs_plan.values()),
    )
    try:
        import pymysql
        from cassandra.auth import PlainTextAuthProvider
        from cassandra.cluster import Cluster
        from cassandra.policies import WhiteListRoundRobinPolicy
        from cassandra.query import dict_factory
    except ImportError as error:
        raise RuntimeError(
            "Install the 'PyMySQL' and 'cassandra-driver' Python packages before running this command"
        ) from error

    cluster_options: dict[str, Any] = {
        "port": config.cassandra_port,
        "connect_timeout": 30,
    }
    if config.cassandra_tunnel_mode:
        cluster_options.update(
            load_balancing_policy=WhiteListRoundRobinPolicy(config.cassandra_contacts),
            schema_metadata_enabled=False,
            token_metadata_enabled=False,
            control_connection_timeout=30,
        )
    cassandra_user = os.environ.get("CASSANDRA_USERNAME")
    cassandra_password = os.environ.get("CASSANDRA_PASSWORD")
    if bool(cassandra_user) != bool(cassandra_password):
        raise ValueError("Provide both CASSANDRA_USERNAME and CASSANDRA_PASSWORD")
    if cassandra_user:
        cluster_options["auth_provider"] = PlainTextAuthProvider(
            username=cassandra_user, password=cassandra_password
        )
    cluster = Cluster(config.cassandra_contacts, **cluster_options)
    mariadb_connection = None
    try:
        cassandra_session = cluster.connect(config.cassandra_keyspace)
        cassandra_session.row_factory = dict_factory
        cassandra_session.default_timeout = 120
        mariadb_connection = pymysql.connect(
            host=config.mariadb_host,
            port=config.mariadb_port,
            user=config.mariadb_user,
            password=config.mariadb_password,
            database=config.mariadb_database,
            cursorclass=pymysql.cursors.DictCursor,
        )
        match_ids = selected_match_ids(mariadb_connection, arguments)
        LOGGER.info("selected matches for backfill: %s", len(match_ids))
        if not match_ids:
            LOGGER.warning("no matches were selected; no rows were changed")
        backfilled_count = 0
        skipped_match_ids: list[int] = []
        behavior_match_ids: dict[str, set[int]] = {
            behavior: set() for behavior in (
                "regular-fixed", "regular-powerplay-na", "reduced-overs", "missing-powerplay-na"
            )
        }
        skipped_by_reason: dict[str, list[int]] = {}
        for match_id in match_ids:
            LOGGER.info("match %s: starting backfill", match_id)
            try:
                sources = fetch_match_sources(cassandra_session, match_id)
                player_ids = player_details(as_dict(sources[0]))
                maria_bowling_styles = fetch_mariadb_bowling_styles(mariadb_connection, player_ids)
                phase_behaviors: set[str] = set()
                rows = calculate_matchup_rows(
                    *sources,
                    maria_bowling_styles=maria_bowling_styles,
                    allotted_balls_by_innings=overs_plan.get(match_id),
                    missing_pp_as_na=arguments.missing_pp_as_na,
                    phase_behaviors=phase_behaviors,
                )
                if not rows:
                    LOGGER.warning(
                        "match %s: no ball by ball data present; existing rows were left unchanged", match_id
                    )
                    skipped_match_ids.append(match_id)
                    skipped_by_reason.setdefault("no ball by ball data present", []).append(match_id)
                    continue
                LOGGER.info("match %s: calculated %s matchup rows", match_id, len(rows))
                replace_rows(mariadb_connection, match_id, rows)
                LOGGER.info("backfilled %s matchup rows for match %s", len(rows), match_id)
                backfilled_count += 1
                for behavior in phase_behaviors:
                    behavior_match_ids[behavior].add(match_id)
            except MatchCannotBeBackfilled as error:
                LOGGER.warning("match %s was not backfilled: %s", match_id, error)
                skipped_match_ids.append(match_id)
                skipped_by_reason.setdefault(error.reason, []).append(match_id)
        LOGGER.info(
            "match summary: selected=%s backfilled=%s skipped=%s",
            len(match_ids), backfilled_count, len(skipped_match_ids),
        )
        log_match_groups("backfilled matches by phase behavior", behavior_match_ids)
        log_match_groups("skipped matches by reason", skipped_by_reason)
        return backfilled_count > 0
    finally:
        if mariadb_connection is not None:
            mariadb_connection.close()
        cluster.shutdown()


def parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument(
        "--match-id", type=parse_match_ids, metavar="ID[,ID...]",
        help="One match ID or comma-separated match IDs to backfill",
    )
    selectors.add_argument("--series-id", type=int, help="Backfill every match in this series")
    selectors.add_argument("--from-date", type=date.fromisoformat, help="Inclusive UTC start date")
    parser.add_argument("--to-date", type=date.fromisoformat, help="Inclusive UTC end date")
    parser.add_argument("--verbose", action="store_true", help="Log each ball event as it is processed")
    parser.add_argument(
        "--missing-pp-as-na", action="store_true",
        help="Use NA when the opening powerplay is missing, bounded by the last eligible ball number",
    )
    parser.add_argument(
        "--reduced-overs-file",
        help="CSV containing matchId,inningsId,scheduledOvers,reducedOvers for every innings of listed matches",
    )
    parsed = parser.parse_args(arguments)
    if parsed.from_date is None and parsed.to_date is not None:
        parser.error("--to-date requires --from-date")
    if parsed.from_date is not None and parsed.to_date is None:
        parser.error("--from-date requires --to-date")
    if parsed.from_date is not None and parsed.from_date > parsed.to_date:
        parser.error("--from-date must be on or before --to-date")
    return parsed


def main(arguments: list[str] | None = None) -> int:
    try:
        args = parse_arguments(arguments)
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        LOGGER.setLevel(logging.DEBUG if args.verbose else logging.INFO)
        return 0 if run_backfill(args, database_config()) else 1
    except (RuntimeError, ValueError) as error:
        LOGGER.error("%s", error)
        return 2


if __name__ == "__main__":
    sys.exit(main())

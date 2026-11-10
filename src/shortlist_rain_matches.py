#!/usr/bin/env python3
"""Export rain-reduction evidence, a manual review queue, and likely regular matches."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import logging
import os
from pathlib import Path
import re
import subprocess
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pymysql
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


STANDARD_OVERS = {"T20": 20, "ODI": 50}
LOGGER = logging.getLogger(__name__)


MATCH_FORMATS = {1: "TEST", 2: "ODI", 4: "HUN"}


INNINGS_OVERS = re.compile(
    r"\b(1st|first|2nd|second)\s+innings\s+(?:reduced|shortened)\s+(?:to\s+)?(\d{1,3})\s*(?:overs?|ovs)\b",
    re.I,
)


SCHEDULED_OVERS = re.compile(
    r"\b(?:(\d{1,3})\s*[- ]?(?:overs?|ovs)\s+(?:game|match|(?:per|a)\s+side)|(?:reduced|shortened)\s+(?:to\s+)?(\d{1,3})\s*(?:overs?|ovs))\b",
    re.I,
)


def balls(overs):
    whole, _, fraction = str(overs).partition(".")
    return int(whole) * 6 + (int(fraction[:1]) if fraction else 0)


def commentary_text(row):
    text = row.get("comm_txt") or ""
    extra = []
    for formatting in (row.get("comm_format") or {}).values():
        if hasattr(formatting, "_asdict"):
            formatting = formatting._asdict()
        markers = formatting.get("format_id") or []
        for index, value in enumerate(formatting.get("format_value") or []):
            marker = markers[index] if index < len(markers) else None
            if marker and marker in text:
                text = text.replace(marker, value)
            else:
                extra.append(value)
    return " ".join(filter(None, (text, *extra)))


def find_rain_commentary(session, match_id):
    rows = []
    for innings_id in (1, 2):
        LOGGER.debug("match %s: fetching Cassandra freeform rows for innings %s", match_id, innings_id)
        before = len(rows)
        rows.extend(session.execute(
            "SELECT innings_id, ball_nbr, time_score, comm_style, comm_txt, comm_format "
            "FROM match_bbb_miniscore WHERE match_id = %s AND innings_id = %s AND ball_nbr = 0",
            (match_id, innings_id),
        ))
        LOGGER.debug("match %s: fetched %s freeform rows for innings %s", match_id, len(rows) - before, innings_id)
    LOGGER.debug("match %s: scanning %s freeform rows newest first", match_id, len(rows))
    for row in sorted(rows, key=lambda item: item["time_score"], reverse=True):
        if row.get("ball_nbr") != 0:
            continue
        text = commentary_text(row)
        if CREASE_ENTRY.search(text):
            continue
        if SIGNAL.search(text):
            LOGGER.debug("match %s: found commentary signal in innings %s at %s; stopping scan",
                        match_id, row["innings_id"], row["time_score"])
            return {"text": text, "inningsId": row["innings_id"],
                    "timeScore": row["time_score"].isoformat()}
    LOGGER.debug("match %s: no commentary signal found", match_id)
    return None


def read_payload(match_id):
    url = f"http://apiserver.cricbuzz.com/w/mcenter/v1/{match_id}/livescore"
    for attempt in range(3):
        try:
            with urlopen(url, timeout=15) as response:
                if response.status == 204:
                    return None
                return json.load(response)
        except URLError as error:
            if isinstance(error, HTTPError):
                raise
            if attempt < 2:
                LOGGER.warning("match %s: livescore request failed; retrying, attempt %s/3", match_id, attempt + 2)
                sleep(0.5 * 2 ** attempt)
                continue
            LOGGER.warning("match %s: livescore retries exhausted; trying curl fallback", match_id)
            try:
                response = subprocess.run(
                    ["curl", "--fail", "--silent", "--show-error", "--max-time", "15", url],
                    capture_output=True, text=True, check=True,
                )
            except (OSError, subprocess.CalledProcessError) as curl_error:
                reason = getattr(curl_error, "stderr", None) or str(curl_error)
                raise URLError(f"{error}; curl fallback failed: {reason.strip()}") from curl_error
            return json.loads(response.stdout) if response.stdout else None


def database_match_ids(predicate, values):
    host, _, port = os.environ["MARIADB_HOST"].partition(":")
    connection = pymysql.connect(
        host=host,
        port=int(port) if port else 3306,
        user=os.environ["MARIADB_USER"],
        password=os.environ["MARIADB_PASSWORD"],
        database=os.environ["MARIADB_DATABASE"],
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, m.startdt, m.match_type_id
                FROM krik_match_archive AS m
                WHERE {predicate}
                  AND (m.match_type_id NOT IN (1, 4) OR m.match_type_id IS NULL)
                ORDER BY m.startdt, m.id
                """.format(predicate=predicate),
                values,
            )
            return [
                {
                    "matchId": match_id,
                    "startDate": start_date.isoformat(),
                    "matchFormat": MATCH_FORMATS.get(match_type_id, "T20"),
                }
                for match_id, start_date, match_type_id in cursor
            ]
    finally:
        connection.close()


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


def connect_cassandra():
    from cassandra.auth import PlainTextAuthProvider
    from cassandra.cluster import Cluster
    from cassandra.policies import WhiteListRoundRobinPolicy
    from cassandra.query import dict_factory

    contacts, port = cassandra_contact_configuration(
        os.environ["CASSANDRA_CONTACT_POINTS"], int(os.environ.get("CASSANDRA_PORT", "9042")))
    keyspace = os.environ["CASSANDRA_KEYSPACE"]
    options = {"port": port, "connect_timeout": 30}
    if environment_bool("CASSANDRA_TUNNEL_MODE"):
        options.update(load_balancing_policy=WhiteListRoundRobinPolicy(contacts),
                       schema_metadata_enabled=False, token_metadata_enabled=False,
                       control_connection_timeout=30)
    username, password = os.environ.get("CASSANDRA_USERNAME"), os.environ.get("CASSANDRA_PASSWORD")
    if bool(username) != bool(password):
        raise ValueError("Provide both CASSANDRA_USERNAME and CASSANDRA_PASSWORD")
    if username:
        options["auth_provider"] = PlainTextAuthProvider(username=username, password=password)
    cluster = Cluster(contacts, **options)
    try:
        session = cluster.connect(keyspace)
        session.row_factory = dict_factory
        session.default_timeout = 120
        return cluster, session
    except Exception:
        cluster.shutdown()
        raise


RAIN = re.compile(
    r"\b(?:due to|because of|owing to|following)\s+(?:the\s+)?rain\b|"
    r"\brain[- ]shortened\b|\bwet\s+outfield\b", re.I,
)
SIGNAL = re.compile(
    r"\b(?:rain\w*|dls|duckworth|lewis|revis\w*|reduc\w*|shorten\w*|"
    r"interrupt\w*|abandon\w*|wet|weather)\b|\bno[ -]+result\b", re.I,
)
CREASE_ENTRY = re.compile(r"\b(?:to|onto)\s+(?:the\s+)?crease[.!…\s]*$", re.I)
ABANDONED = re.compile(r"\babandon(?:ed|ment)?\b", re.I)
FIELDS = ["matchId", "startDate", "matchFormat", "classification", "reason", "status",
          "inningsId", "runs", "wickets", "playedOvers", "scheduledOvers", "revisedOvers", "evidence"]


def text_limits(text):
    """Only interpret explicit innings limits or an explicit common match limit."""
    specific = list(INNINGS_OVERS.finditer(text))
    remainder = text
    for mention in reversed(specific):
        remainder = remainder[:mention.start()] + " " + remainder[mention.end():]
    common = []
    for mention in SCHEDULED_OVERS.finditer(remainder):
        # A bare "reduced to 15 overs" may refer only to the chase.
        if mention.group(1) or re.search(r"\b(?:match|game)\s+$", remainder[:mention.start()], re.I):
            common.append(int(next(value for value in mention.groups() if value)))
    if len(set(common)) > 1:
        return {}, True
    limits = {1: common[0], 2: common[0]} if common else {}
    seen = {}
    for mention in specific:
        innings = 1 if mention.group(1).lower() in {"1st", "first"} else 2
        limit = int(mention.group(2))
        if innings in seen and seen[innings] != limit:
            return {}, True
        seen[innings] = limit
        limits[innings] = limit
    return limits, False


def scorecard_exclusion(match, payload, commentary=None):
    if payload is None:
        return None
    standard = STANDARD_OVERS.get(match.get("matchFormat"))
    score = payload.get("miniscore", payload)
    details = score.get("matchScoreDetails", {})
    recorded_innings = details.get("inningsScoreList", [])
    status = " ".join(filter(None, (score.get("status"), details.get("customStatus"), details.get("state"))))
    if ABANDONED.search(status) and (not recorded_innings or all(
            item.get("overs") is not None and balls(item["overs"]) == 0
            and item.get("score", 0) == 0 and item.get("wickets", 0) == 0
            for item in recorded_innings)):
        return "Abandoned match with no play recorded"
    innings = {item["inningsId"]: item for item in details.get("inningsScoreList", [])
               if item.get("inningsId") in (1, 2)}
    if standard is None or details.get("state") != "Complete" or set(innings) != {1, 2}:
        return None
    played = {key: balls(item["overs"]) for key, item in innings.items()}
    if all(value == standard * 6 for value in played.values()):
        return "Both innings used the full standard quota"
    texts = [score.get("status"), details.get("customStatus"), commentary["text"] if commentary else None]
    if any(SIGNAL.search(text or "") for text in texts):
        return None
    first, second = innings[1], innings[2]
    if (played[1] == standard * 6 and 0 < played[2] <= standard * 6
            and isinstance(first.get("score"), (int, float))
            and isinstance(second.get("score"), (int, float))
            and second["score"] > first["score"]):
        return "Likely regular: full first innings and successful chase without status interruption signal"
    return None


def classify(match, payload, commentary=None):
    output = {**match, "classification": "review", "limits": {}, "innings": {},
              "status": "", "evidence": []}
    if payload is None:
        return {**output, "reason": "API returned no payload"}
    score = payload.get("miniscore", payload)
    details = score.get("matchScoreDetails", {})
    innings = {item["inningsId"]: item for item in details.get("inningsScoreList", [])
               if item.get("inningsId") in (1, 2)}
    statuses = list(dict.fromkeys(filter(None, [score.get("status"), details.get("customStatus")])))
    texts = statuses + ([commentary["text"]] if commentary else [])
    evidence = list(dict.fromkeys(text for text in texts if SIGNAL.search(text)))
    if commentary:
        evidence.append(f"Cassandra innings {commentary['inningsId']}, time_score {commentary['timeScore']}")
    output.update(innings=innings, status=" | ".join(statuses), evidence=evidence)
    standard = STANDARD_OVERS.get(match.get("matchFormat"))
    exclusion = scorecard_exclusion(match, payload, commentary)
    if exclusion:
        limits = {key: standard for key, item in innings.items()
                  if standard is not None and item.get("overs") is not None
                  and balls(item["overs"]) == standard * 6}
        return {**output, "classification": "excluded", "limits": limits, "reason": exclusion}
    if standard is None:
        return {**output, "reason": "Unknown standard overs for format"}
    if details.get("state") != "Complete" or set(innings) != {1, 2}:
        return {**output, "reason": "Incomplete match or missing innings"}
    played = {key: balls(item["overs"]) for key, item in innings.items()}
    limits = {}
    LOGGER.debug("match %s: checking rain cause, innings limits and scorecard consistency", match["matchId"])
    conflict = False
    rain_reduction = False
    for text in texts:
        found, ambiguous = text_limits(text)
        conflict |= ambiguous
        for key, value in found.items():
            if key in limits and limits[key] != value:
                conflict = True
            limits[key] = value
        if RAIN.search(text) and any(value < standard for value in found.values()):
            rain_reduction = True
    # All-out before the full quota does not establish the allotted overs.
    if 1 not in limits and played[1] == standard * 6:
        limits[1] = standard
    output["limits"] = limits
    invalid = any(value <= 0 or value > standard or played[key] > value * 6
                  for key, value in limits.items())
    if conflict or invalid:
        return {**output, "reason": "Conflicting limits or scorecard exceeds stated limit"}
    if rain_reduction and set(limits) == {1, 2}:
        return {**output, "classification": "confirmed", "reason": "Explicit rain reduction and both innings limits established"}
    if evidence or any(value < standard for value in limits.values()):
        return {**output, "reason": "Rain/revision signal; final innings limits or rain cause need verification"}
    LOGGER.debug("match %s: checking natural innings endings for likely regular exclusion", match["matchId"])
    first, second = innings[1], innings[2]
    target_reached = (isinstance(first.get("score"), (int, float))
                      and isinstance(second.get("score"), (int, float))
                      and second["score"] > first["score"])
    first_finished = played[1] == standard * 6 or first.get("wickets") == 10
    second_finished = played[2] == standard * 6 or second.get("wickets") == 10 or target_reached
    if first_finished and second_finished:
        return {**output, "classification": "excluded", "reason": "Likely regular: natural innings endings and no available interruption signal"}
    return {**output, "reason": "Scorecard does not establish a regular finish"}


def inspect(match, session):
    started = monotonic()
    match_id = match["matchId"]
    try:
        LOGGER.debug("match %s: fetching livescore", match_id)
        payload = read_payload(match_id)
        LOGGER.debug("match %s: checking full-quota exclusion and successful chase", match_id)
        exclusion = scorecard_exclusion(match, payload)
        if exclusion:
            LOGGER.debug("match %s: %s; skipping Cassandra", match_id, exclusion)
            result = classify(match, payload)
        else:
            commentary = find_rain_commentary(session, match_id) if payload is not None else None
            result = classify(match, payload, commentary)
    except Exception as error:
        LOGGER.exception("match %s: fetch/parse failed; sending to Review", match_id)
        result = {**match, "classification": "review", "reason": f"Fetch/parse error: {error}",
                  "status": "", "innings": {}, "limits": {}, "evidence": []}
    LOGGER.debug("match %s: %s | %s | elapsed=%.1fs", match_id, result["classification"],
                result["reason"], monotonic() - started)
    return result


def scan_matches(matches, session, workers):
    started = monotonic()
    results = [None] * len(matches)
    completed = 0
    last_progress = None
    counts = dict.fromkeys(("review", "confirmed", "excluded"), 0)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {executor.submit(inspect, match, session): index for index, match in enumerate(matches)}
        while pending:
            done, _ = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
            for future in done:
                result = future.result()
                results[pending.pop(future)] = result
                counts[result["classification"]] += 1
                completed += 1
            elapsed = monotonic() - started
            eta = f"{elapsed / completed * len(pending):.0f}s" if completed else "estimating"
            if last_progress is None or elapsed - last_progress >= 15 or not pending:
                LOGGER.info("Progress: %s/%s completed | elapsed=%.0fs | scan ETA=%s | "
                            "review=%s confirmed=%s excluded=%s",
                            completed, len(matches), elapsed, eta,
                            counts["review"], counts["confirmed"], counts["excluded"])
                last_progress = elapsed
    return results


def write_outputs(path, matches):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for classification, name in [("review", "Review"), ("confirmed", "Confirmed rain"),
                                  ("excluded", "Excluded regular"), (None, "Evidence")]:
        sheet = workbook.create_sheet(name)
        sheet.append(FIELDS)
        for match in matches:
            if classification is not None and match["classification"] != classification:
                continue
            for innings_id, innings in sorted(match["innings"].items()) or [(None, {})]:
                row = [
                    *[match.get(key, "") for key in FIELDS[:6]],
                    innings_id, innings.get("score"), innings.get("wickets"), innings.get("overs"),
                    STANDARD_OVERS.get(match.get("matchFormat")), match["limits"].get(innings_id),
                    "\n".join(match["evidence"]),
                ]
                sheet.append(row)
                # API text is data, including any leading equals sign.
                for cell in sheet[sheet.max_row]:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="24476A")
        for index, field in enumerate(FIELDS, 1):
            sheet.column_dimensions[get_column_letter(index)].width = (
                70 if field == "evidence" else 55 if field in {"reason", "status"} else 20
            )
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-date")
    source.add_argument("--series-id", type=int)
    source.add_argument("--match-id", type=int)
    parser.add_argument("--to-date")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output-file", type=Path, required=True, help="Excel workbook path (.xlsx)")
    parser.add_argument("--verbose", action="store_true", help="show detailed DEBUG logs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    LOGGER.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    started = monotonic()
    if bool(args.from_date) != bool(args.to_date):
        parser.error("--from-date and --to-date must be supplied together")
    if args.output_file.suffix.lower() != ".xlsx":
        parser.error("--output-file must end in .xlsx")
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.match_id is not None:
        predicate, values = "m.id = %s", (args.match_id,)
    elif args.series_id is not None:
        predicate, values = "m.seriesid = %s", (args.series_id,)
    else:
        predicate, values = "m.startdt BETWEEN %s AND %s", (args.from_date, args.to_date)
    LOGGER.debug("Fetching match list from MariaDB")
    matches = database_match_ids(predicate, values)
    LOGGER.info("Selected %s matches; workers=%s. Scan ETA will appear after the first completion.",
                len(matches), args.workers)
    LOGGER.debug("Connecting to Cassandra")
    cluster, session = connect_cassandra()
    LOGGER.debug("Cassandra connected; starting match scan")
    try:
        results = scan_matches(matches, session, args.workers)
    finally:
        LOGGER.debug("Closing Cassandra connection")
        cluster.shutdown()
    LOGGER.debug("Writing Excel workbook: %s", args.output_file)
    write_outputs(args.output_file, results)
    counts = {key: sum(item["classification"] == key for item in results)
              for key in ("review", "confirmed", "excluded")}
    LOGGER.info("Saved %s | review=%s confirmed=%s excluded=%s | total elapsed=%.1fs",
                args.output_file, counts["review"], counts["confirmed"], counts["excluded"],
                monotonic() - started)


if __name__ == "__main__":
    main()

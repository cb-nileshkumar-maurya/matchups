#!/usr/bin/env python3
"""Find reduced-overs matches from Cricbuzz livescore responses."""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
import json
import os
import re
import sys
from urllib.request import urlopen

import pymysql


STANDARD_OVERS = {"T20": 20, "ODI": 50}
MATCH_FORMATS = {1: "TEST", 2: "ODI", 4: "HUN"}
REDUCED_WORDS = re.compile(r"\b(?:rain[- ]shortened|shortened|reduced)\b", re.I)
NO_RESULT = re.compile(r"\bno[\s-]+result\b", re.I)
DLS = re.compile(r"\b(?:dls|duckworth)\b", re.I)
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


def first_innings_limit(first, standard):
    if not first:
        return None
    played = balls(first.get("overs", 0))
    if played >= standard * 6 or first.get("wickets") == 10:
        return standard
    return None


def detect(payload, match_format):
    score = payload.get("miniscore", payload)
    details = score.get("matchScoreDetails", {})
    innings = {item.get("inningsId"): item for item in details.get("inningsScoreList", [])}
    first, second = innings.get(1), innings.get(2)
    status = " ".join(dict.fromkeys(filter(None, (score.get("status"), details.get("customStatus")))))
    standard = STANDARD_OVERS.get(match_format)
    match_id = details.get("matchId")
    specific = list(INNINGS_OVERS.finditer(status))
    general_status = status
    for mention in reversed(specific):
        general_status = general_status[:mention.start()] + " " + general_status[mention.end():]
    scheduled = SCHEDULED_OVERS.search(general_status)

    if specific:
        common = int(next(value for value in scheduled.groups() if value)) if scheduled else None
        limits = {1: common, 2: common} if common else {}
        for mention in specific:
            limits[1 if mention.group(1).lower() in {"1st", "first"} else 2] = int(mention.group(2))
        if 2 in limits and 1 not in limits and standard:
            limits[1] = first_innings_limit(first, standard)
        return result(match_id, match_format, standard, True, common, "status", status, first, second, limits)
    if scheduled:
        scheduled_overs = int(next(value for value in scheduled.groups() if value))
        return result(match_id, match_format, standard, scheduled_overs < standard if standard else True, scheduled_overs, "status", status, first, second)
    if REDUCED_WORDS.search(status):
        return result(match_id, match_format, standard, True, None, "status", status, first, second)
    if standard:
        played_balls = balls(first.get("overs", 0)) if first else 0
        if details.get("state") == "Complete" and first and first.get("wickets", 10) < 10 and played_balls < standard * 6:
            return result(match_id, match_format, standard, True, None, "first-innings scorecard", first, first, second)
    return result(match_id, match_format, standard, False, standard, "none", None, first, second)


def result(match_id, match_format, standard_overs, reduced_overs, scheduled_overs, source, evidence, first, second,
           allotted_overs_by_innings=None):
    return {
        "matchId": match_id,
        "matchFormat": match_format or None,
        "standardOvers": standard_overs,
        "reducedOvers": reduced_overs,
        "scheduledOvers": scheduled_overs,
        "allottedOversByInnings": allotted_overs_by_innings or {},
        "firstInningsOver": first.get("overs") if first else None,
        "secondInningsOver": second.get("overs") if second else None,
        "source": source,
        "evidence": evidence,
    }


def read_payload(match_id):
    with urlopen(f"http://apiserver.cricbuzz.com/w/mcenter/v1/{match_id}/livescore", timeout=15) as response:
        if response.status == 204:
            return None
        return json.load(response)


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
                SELECT m.id, m.startdt, m.match_type_id, i.inningsId, i.revisedOvers
                FROM krik_match_archive AS m
                LEFT JOIN stats_import3_dump_innings_tbl AS i ON i.matchId = m.id
                WHERE {predicate}
                  AND (m.match_type_id NOT IN (1, 4) OR m.match_type_id IS NULL)
                ORDER BY m.startdt, m.id, i.inningsId
                """.format(predicate=predicate),
                values,
            )
            matches = {}
            for match_id, start_date, match_type_id, innings_id, revised_overs in cursor:
                match = matches.setdefault(match_id, {
                    "matchId": match_id,
                    "startDate": start_date.isoformat(),
                    "matchFormat": MATCH_FORMATS.get(match_type_id, "T20"),
                    "revisedOversByInnings": {},
                })
                if innings_id is not None and revised_overs:
                    match["revisedOversByInnings"][innings_id] = revised_overs
            return list(matches.values())
    finally:
        connection.close()


def inspect_match(match, regular_match_ids=()):
    match_id = match["matchId"]
    if match_id in regular_match_ids:
        standard = STANDARD_OVERS[match["matchFormat"]]
        return {
            **result(match_id, match["matchFormat"], standard, False, standard,
                     "override", None, None, None),
            "startDate": match["startDate"],
            "inningsIds": [],
            "revisedOversByInnings": {},
        }
    try:
        payload = read_payload(match_id)
        if payload is None:
            return None
        score = payload.get("miniscore", payload)
        details = score.get("matchScoreDetails", {})
        statuses = (score.get("status"), details.get("customStatus"))
        if any(NO_RESULT.search(status or "") for status in statuses):
            return None
        status = " ".join(filter(None, statuses))
        if DLS.search(status) and not (INNINGS_OVERS.search(status) or SCHEDULED_OVERS.search(status)):
            return None
        detection = detect(payload, match["matchFormat"])
        innings = details.get("inningsScoreList", [])
        return {
            **detection, "matchId": match_id, "startDate": match["startDate"],
            "inningsIds": sorted({item["inningsId"] for item in innings}),
            "revisedOversByInnings": match["revisedOversByInnings"],
        }
    except Exception as error:
        return {"matchId": match_id, "startDate": match["startDate"], "error": str(error)}


def write_overs_file(path, matches):
    rows = []
    issues = []
    for match in matches:
        if "error" in match:
            issues.append((match["matchId"], None, match["error"]))
            continue
        if not match["reducedOvers"]:
            continue
        if not match["inningsIds"]:
            issues.append((match["matchId"], None, "no innings found"))
            continue
        for innings_id in match["inningsIds"]:
            limit = (match["revisedOversByInnings"].get(innings_id)
                     or match.get("allottedOversByInnings", {}).get(innings_id)
                     or match["scheduledOvers"])
            if limit is None:
                issues.append((match["matchId"], innings_id,
                               "allotted overs unknown; archive revisedOvers is 0 or missing and status gives no limit for this innings"))
                continue
            if (not re.fullmatch(r"\d+(?:\.[0-6])?", str(limit))
                    or limit <= 0 or limit > match["standardOvers"]):
                issues.append((match["matchId"], innings_id, f"invalid allotted overs {limit}"))
                continue
            rows.append((match["matchId"], innings_id, match["standardOvers"], limit))
    if issues:
        match_ids = ",".join(str(match_id) for match_id in dict.fromkeys(issue[0] for issue in issues))
        details = "\n".join(
            f"match {match_id}" + (f" innings {innings_id}" if innings_id is not None else "")
            + f": {reason}"
            for match_id, innings_id, reason in issues
        )
        raise ValueError(
            f"CSV not written; {len(issues)} issue(s) in match IDs: {match_ids}\n{details}"
        )
    with open(path, "w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("matchId", "inningsId", "scheduledOvers", "reducedOvers"))
        writer.writerows(rows)
    return len(rows)


def parse_match_ids(value):
    ids = [item.strip() for item in value.split(",")]
    if not ids or any(not item.isdecimal() or int(item) <= 0 for item in ids):
        raise argparse.ArgumentTypeError("match IDs must be comma-separated positive integers")
    return frozenset(map(int, ids))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-date", help="inclusive YYYY-MM-DD archive start date")
    source.add_argument("--series-id", type=int, help="scan every non-Test, non-Hundred match in this series")
    source.add_argument("--match-id", type=int, help="inspect this non-Test, non-Hundred match")
    parser.add_argument("--to-date", help="inclusive YYYY-MM-DD archive end date, required with --from-date")
    parser.add_argument("--workers", type=int, default=8, help="parallel livescore requests")
    parser.add_argument("--all", action="store_true", help="include matches that are not reduced")
    parser.add_argument("--regular-match-ids", type=parse_match_ids, default=frozenset(),
                        help="comma-separated match IDs to treat as regular without inspecting livescore")
    parser.add_argument("--output-file", help="write UTF-8 innings overs CSV for backfill_matchups.py")
    args = parser.parse_args()
    if args.from_date and not args.to_date:
        parser.error("--to-date is required with --from-date")
    if args.to_date and not args.from_date:
        parser.error("--to-date can only be used with --from-date")
    if args.match_id is not None:
        predicate, values = "m.id = %s", (args.match_id,)
    elif args.series_id is not None:
        predicate, values = "m.seriesid = %s", (args.series_id,)
    else:
        predicate, values = "m.startdt BETWEEN %s AND %s", (args.from_date, args.to_date)
    matches = database_match_ids(predicate, values)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(inspect_match, matches, repeat(args.regular_match_ids)))
    selected = [
        match for match in results
        if match and (args.all or args.match_id is not None or match.get("reducedOvers") or match.get("error"))
    ]
    print(json.dumps(selected, indent=2), flush=True)
    reduced = sum(bool(match and match.get("reducedOvers")) for match in results)
    errors = sum(bool(match and "error" in match) for match in results)
    excluded = sum(match is None for match in results)
    ordinary = len(matches) - reduced - errors - excluded
    csv_rows = "not_requested" if not args.output_file else "not_written"
    try:
        if args.output_file:
            csv_rows = write_overs_file(args.output_file, [match for match in results if match])
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        print(
            f"Summary: scanned={len(matches)} reduced={reduced} ordinary={ordinary} "
            f"excluded={excluded} errors={errors} json_entries={len(selected)} csv_rows={csv_rows}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

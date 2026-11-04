# Matchup backfill pseudocode

This describes the current behavior of `backfill_matchups.py`.

```text
MAIN
    Parse arguments. Require exactly one match selector:
        --match-id: one ID or a comma-separated list of positive, unique IDs
        --series-id: all matches in a MariaDB series
        --from-date and --to-date: a MariaDB date range
    Accept optional --reduced-overs-file and --verbose.
    Read MariaDB and Cassandra settings from environment variables.
    Set INFO logging, or DEBUG logging when --verbose is present.
    Run the backfill.
    Exit 0 if at least one match succeeds, 1 if none succeeds,
    or 2 for a handled input/configuration error.

RUN BACKFILL
    If a reduced-overs CSV is supplied:
        Validate its exact header and every row before processing matches.
        Convert scheduledOvers and reducedOvers from over.ball notation
        to ball counts (for example, 17.3 -> 105).
        Require positive IDs and ball limits, reduced <= scheduled,
        and no duplicate match/innings pair.
        Build match ID -> innings ID -> allotted balls.

    Connect to Cassandra and MariaDB.
    In Cassandra tunnel mode, use only the configured contact points.
    Select match IDs from the explicit list, series, or date query.

    For each selected match ID, in order:
        Try:
            Read its league type through krik_match_archive.seriesid and
            krik_series_archive.series_type; fail if the series row is missing.
            Read one match_info record from Cassandra.
            Read one full scorecard record per innings.
            Read all match_bbb_commentary rows for each innings.
            Build the eligible player map from match_info.
            Look up MariaDB bowling styles for those player IDs as fallback.
            Calculate all matchup rows in memory (see below).

            If calculation produced no rows:
                Warn that no ball by ball data is present.
                Mark the match skipped; leave existing target rows untouched.
                Continue to the next match.

            Replace only this match's target rows in one MariaDB transaction.
            Count the match as backfilled and record its phase behavior.

        On a known source-data/validation error:
            Warn with the match ID and reason.
            Count the match as skipped; leave existing target rows untouched.

    Log selected, backfilled, and skipped counts.
    Group successful match IDs by phase behavior and skipped IDs by reason.
    Close the database connections.

CALCULATE MATCHUP ROWS
    Require a supported format (T20 or ODI), venue ID, and scorecards.
    If this match is in the reduced-overs CSV, require exactly one CSV
    innings entry for every scorecard innings.

    For each commentary event:
        Ignore wides (extra_code Wd or WD) and ball_nbr = 0.
        Validate ball number, innings ID, batter ID, and bowler ID.
        Keep valid eligible events.
    Find the greatest eligible ball number in each innings.

    For each innings, derive inclusive phase boundaries (see below).
    Build a player map from both teams' player_details:
        Exclude support staff and unavailable players.
        Include playing XI, bench, and substitutes if present.
    Build batting order from each scorecard's batsmen_data bat_* entries.

    First pass over eligible events, sorted by time_score:
        Require the event's scorecard, batter, bowler, and batting order.
        Assign the event to an innings phase by ball_nbr.
        If any value is missing or a ball is outside all phase ranges,
        fail the whole match with a warning containing that ball's fields.

    Second pass over the validated events:
        Resolve bowler style from Cassandra, then MariaDB fallback.
        If both are missing, use NULL bowling_type and bowling_technique;
        warn once for that bowler.
        If a nonempty style is unrecognised, fail the match.
        Require batter hand to be RIGHT or LEFT.

        Group by (innings ID, batter ID, bowler ID, phase).
        In each group:
            runs  += legal_runs, or 0 when absent
            balls += 1
            fours += 1 if the four highlight bit is present
            sixes += 1 if the six highlight bit is present
            dots  += 1 if legal_runs = 0 and total_runs = 0
            Set dismissal_type for a non-NONE wicket code except run out.
            Run-out deliveries still contribute runs and balls.

    Return aggregated rows sorted by innings, phase, batter, and bowler.

DERIVE PHASES FOR ONE INNINGS
    Inspect every pp_* object in the scorecard's pp_data.
    Convert each pp_overs_from to a ball number.
    Select the single object starting at ball 1, regardless of its pp_* key.
    Reject malformed entries or multiple objects starting at ball 1.

    If no opening powerplay exists, fail the match.

    Require the selected opening powerplay to be mandatory and valid.
    Convert its pp_overs_to to an inclusive final ball number.

    Reject a powerplay end beyond ball 36 for T20 or 60 for ODI,
    beyond the allotted limit, or any eligible ball beyond the limit.
    Reject an allotted limit beyond 120 T20 or 300 ODI balls.

    If the innings has a full format allotment:
        Require the scorecard powerplay end to match the expected boundary.
        T20: POWERPLAY 1-36, MIDDLE_OVERS 37-90, DEATH_OVERS 91-120.
        ODI: POWERPLAY 1-60, MIDDLE_OVERS 61-240, DEATH_OVERS 241-300.
        BBL T20, identified by series_type: POWERPLAY 1-24,
            MIDDLE_OVERS 25-90, DEATH_OVERS 91-120.

    Else use the CSV allotted ball count and actual scorecard powerplay:
        BBL uses generic T20 reduced rules; compare powerplay with ball 36.
        If powerplay ends at the allotted final ball, use only POWERPLAY.
        If T20 has at most 60 allotted balls and powerplay ends at ball 36,
        or ODI has at most 90 allotted balls and powerplay ends at ball 60:
            Every remaining ball is DEATH_OVERS; there is no middle phase.
        Otherwise:
            death_balls = ceil(allotted balls * 25%)
            death_start = max(powerplay end + 1,
                              allotted balls - death_balls + 1)
            MIDDLE_OVERS fills any gap after POWERPLAY and before DEATH_OVERS.
            DEATH_OVERS runs from death_start through allotted balls.

REPLACE ONE MATCH'S ROWS
    Select existing batting_vs_bowling_matchups rows for this match.
    Print their count and each old row to stdout.
    Begin a MariaDB transaction.
    Delete rows WHERE match_id = this match ID.
    Insert all calculated rows with executemany.
    Commit; roll back and propagate the error if the write fails.
```

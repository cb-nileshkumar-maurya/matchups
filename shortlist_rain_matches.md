# Rain match shortlist

Set `MARIADB_HOST`, `MARIADB_USER`, `MARIADB_PASSWORD` and
`MARIADB_DATABASE` in the environment. This script is self-contained, including
archive queries, API fetching, Cassandra connections, commentary parsing and classification.
The script reads the archive, livescore API and Cassandra commentary, then writes one local workbook.
It does not update the database.

```sh
.venv/bin/python src/shortlist_rain_matches.py \
  --from-date 2025-01-01 --to-date 2025-12-31 \
  --output-file .scratch/rain-review-2025.xlsx
```

Alternatively use `--series-id 123` or `--match-id 123` instead of the date
arguments. `--workers` defaults to 8. Install dependencies with
`.venv/bin/python -m pip install -r requirements.txt`. The selected Excel file
is overwritten if it already exists. Archive selection excludes Tests and
the Hundred, using the archive format mapping and date semantics.

One Excel workbook contains four sheets, with frozen headers and filters:

| Sheet | Contents |
| --- | --- |
| `Review` | Matches requiring manual checks, including matches abandoned after play, other incomplete/no-result matches, missing payloads and fetch failures |
| `Confirmed rain` | Explicit rain-reduction evidence with established limits for both innings |
| `Excluded regular` | Abandoned matches without recorded play, full-quota matches, or likely natural innings endings without an interruption signal |
| `Evidence` | All classifications, reasons, innings data, extracted limits and relevant available text |

Sheets contain one row per innings, or one blank innings row if data is unavailable.
`scheduledOvers` is the format's standard quota. `revisedOvers` is an established
allotted limit, including the unchanged standard quota where established.
It is not the number of overs actually played. Unknown limits remain blank.
Review rows can contain partial limits; check them before assigning final values.

## Progress logging

Default INFO logging shows the selected match count, periodic scan progress and
the final workbook path, category counts and total duration. Progress is reported
at most once every 15 seconds, plus the first update and scan completion.
Warnings and errors remain visible.

Add `--verbose` to show DEBUG details: per-match livescore fetching, full-quota
checks, Cassandra queries and row counts, commentary scanning, rain/limit checks,
natural-finish checks, classification reasons, per-match duration, connection
lifecycle and Excel export. Third-party libraries remain at INFO.

Scan ETA uses elapsed scan time divided by completed matches, multiplied by
remaining matches. It excludes connection setup and Excel export and is
approximate because query durations vary. Before the first completion it displays
`estimating`. Workbook rows retain archive order regardless of completion order.

## Classification rules

After fetching livescore, an abandoned match with no innings rows is excluded
immediately without querying Cassandra. Zero-over innings placeholders also
qualify if runs and wickets are zero. Missing overs on an existing innings row
remain uncertain and go to Review. Abandonment after any recorded play remains
in Review. No allotted overs are assigned to a match abandoned without play.

Next, a complete match with both innings present and both
having played exactly the standard quota is excluded immediately. Cassandra is
not queried for that match, even if the livescore status mentions rain. Both
allotted limits are recorded as the standard quota. A complete match also skips
Cassandra and is excluded as "Likely regular" when the first innings used the
full quota and the second innings scored more runs within its standard quota,
provided livescore status/custom status has no interruption signal. This is a
heuristic: an unreported shortened chase can be missed. The second innings'
allotted limit remains unknown when it finished early. Other early innings
finishes still require a commentary check.

Confirmation requires explicit rain causation, such as "due to rain", or "wet outfield", alongside
an explicit reduced quota in a status or the selected Cassandra freeform comment. Common
limits must explicitly describe the game, match or overs per side. A bare
"reduced to 15 overs" does not establish which innings it applies to. Both
innings limits must be established, within the standard quota, and at least
as large as the recorded overs played. Conflicting available final statements
send the match to review. A first innings that used the full quota establishes
its standard limit; being all out early does not.

The scorecard comes from `/w/mcenter/v1/{matchId}/livescore`. Commentary comes
from Cassandra `match_bbb_miniscore`, querying innings 1 and 2 with
`ball_nbr = 0`. Both sets of rows are sorted together by `time_score` descending.
Formatting markers in `comm_txt` are expanded with `comm_format`. Comments ending
with "to the crease", "onto the crease" or "onto crease" are skipped, including
trailing spaces, periods or ellipses, so player names do not stop the scan.
The scan stops
at the first rain, DLS, revision or interruption signal; older matching comments
are not used to override that selection. The matched text, innings and timestamp
are preserved in workbook evidence. A first match without an explicit quota goes
to Review, even if an older row contains a quota. DLS alone does not establish
rain causation or allotted overs. A Cassandra fetch/parse failure goes to Review.

Set `CASSANDRA_CONTACT_POINTS` and `CASSANDRA_KEYSPACE`. Optional settings match
`src/backfill_matchups.py`: `CASSANDRA_PORT` defaults to 9042, paired
`CASSANDRA_USERNAME` / `CASSANDRA_PASSWORD` enable authentication, and
`CASSANDRA_TUNNEL_MODE=true` enables the tunnel connection policy. Contact points
may include ports but must all use the same port. The script owns its connection
logic and does not import either backfill or reduced-overs scripts.

After the full-quota exclusion and commentary checks, a likely regular match
without interruption signals requires each innings to have a
natural finish: the first used its quota or lost ten wickets; the second used
its quota, lost ten wickets, or exceeded the first innings score. These rows
are explicitly marked "Likely regular". Silent rain reductions can satisfy
these conditions, particularly when teams were all out, so this exclusion is
a workload-reduction heuristic rather than proof. Keep the exclusion sheet for
audit. Confirmation means supported by the available API text and scorecard,
not independently verified official rain records. Text parsing is deliberately
conservative and unsupported wording stays in review.

No scan runs without a selected date range, series or match. To verify locally:

```sh
.venv/bin/python -m unittest tests.test_shortlist_rain_matches -v
```

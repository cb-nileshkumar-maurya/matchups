# Matchup phase definition

Confirmed 2026-11-04. This is the current phase contract for `backfill_matchups.py`. Ball ranges are inclusive, and over.ball values count completed overs plus balls. For example, 17.3 overs is 105 balls.

## Inputs and precedence

- Each match must have a linked `krik_series_archive` row. Use `krik_match_archive.seriesid` to read `krik_series_archive.series_type`. A missing row highlights and skips the match.
- `--reduced-overs-file` is the only reduced-match input. If it names a match, it must supply one allotted limit for every scorecard innings, including any full-length innings. A match absent from the file follows regular rules. The operator handles omissions from the file.
- Choose regular or reduced rules for each innings from its allotted limit. Full length is 120 balls for T20 and 300 for ODI. An innings that ends early keeps boundaries based on its allotted limit.
- The opening mandatory interval in scorecard `pp_data` sets the actual powerplay end. Its key need not be `pp_1`. Later Power Surge entries never create a matchup powerplay or change a ball's phase.

## Regular innings

The scorecard opening powerplay must end at the expected boundary. Any shorter or longer endpoint highlights and skips the match.

| Format or league | Powerplay | Middle overs | Death overs |
| --- | --- | --- | --- |
| T20 | 1-36 | 37-90 | 91-120 |
| ODI | 1-60 | 61-240 | 241-300 |
| BBL T20, identified by `series_type = BBL` | 1-24 | 25-90 | 91-120 |

These boundaries apply to a full-length innings even when the match appears in the reduced-overs file because another innings was shortened.

## Reduced innings

Use the actual scorecard powerplay, capped at ball 36 for T20 or ball 60 for ODI. A reduced BBL innings uses the generic T20 reduced rules; compare its powerplay with the generic six-over boundary, so a four-over BBL powerplay counts as shortened here.

| Format | Allotted length | Powerplay endpoint | Death interval |
| --- | --- | --- | --- |
| T20 | More than 10 overs | Any valid endpoint | Final `ceil(allotted balls / 4)` balls |
| T20 | At most 10 overs | Standard, ball 36 | Every ball after powerplay |
| T20 | At most 10 overs | Shorter than ball 36 | Final `ceil(allotted balls / 4)` balls |
| ODI | More than 15 overs | Any valid endpoint | Final `ceil(allotted balls / 4)` balls |
| ODI | At most 15 overs | Standard, ball 60 | Every ball after powerplay |
| ODI | At most 15 overs | Shorter than ball 60 | Final `ceil(allotted balls / 4)` balls |

The powerplay owns its full scorecard range. When a calculated death interval overlaps it, death starts on the first ball after powerplay. Middle overs fill the gap, if one exists. If powerplay ends exactly at the allotted final ball, the innings has only a powerplay interval. A phase without an eligible event produces no matchup row.

Examples:

- T20, 17.3 overs allotted, powerplay through ball 30: powerplay 1-30, middle 31-78, death 79-105.
- T20, 10 overs allotted, powerplay through ball 36: powerplay 1-36, death 37-60.
- BBL T20, 10 overs allotted, powerplay through ball 24: powerplay 1-24, middle 25-45, death 46-60.
- T20, 7 overs allotted, powerplay through ball 35: powerplay 1-35, death 36-42.
- ODI, 12 overs allotted, powerplay through ball 48: powerplay 1-48, middle 49-54, death 55-72.

## Highlight and skip

Skip the entire match without replacing its existing rows when any innings has a missing or ambiguous opening powerplay, a non-mandatory or malformed opening interval, a powerplay after the format maximum or allotted final ball, an eligible event after the allotted final ball, or a CSV allotment above the format maximum. An unreadable start in any `pp_data` entry is ambiguous; a clearly later entry with a malformed end does not affect phases. Missing scorecards or a mismatch between CSV and scorecard innings also skip the match.

The `--missing-pp-as-na` option is removed. No `NA` phase or special super-over phase is produced. The ball-by-ball source used by this backfill does not include super-over deliveries.

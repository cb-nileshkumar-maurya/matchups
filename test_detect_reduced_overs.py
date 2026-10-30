import argparse
import csv
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backfill_matchups import reduced_overs_by_match
from detect_reduced_overs import detect, inspect_match, main, parse_match_ids, write_overs_file


class OversFileTests(unittest.TestCase):
    def test_regular_match_ids_skip_livescore_and_csv(self):
        matches = [
            {"matchId": 10, "startDate": "2026-01-01", "matchFormat": "T20", "revisedOversByInnings": {}},
            {"matchId": 11, "startDate": "2026-01-02", "matchFormat": "T20", "revisedOversByInnings": {}},
        ]
        payload = {"miniscore": {
            "status": "Match reduced to 18 overs",
            "matchScoreDetails": {
                "matchId": 11,
                "inningsScoreList": [{"inningsId": 1, "overs": 18}],
            },
        }}
        output, summary = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch("sys.argv", ["detect_reduced_overs.py", "--series-id=9241", "--all",
                                   "--regular-match-ids=10, 12", f"--output-file={directory}/overs.txt"]), \
                patch("detect_reduced_overs.database_match_ids", return_value=matches), \
                patch("detect_reduced_overs.read_payload", return_value=payload) as read_payload, \
                redirect_stdout(output), redirect_stderr(summary):
            main()
            self.assertEqual(reduced_overs_by_match(f"{directory}/overs.txt"), {11: {1: 108}})
        read_payload.assert_called_once_with(11)
        result = json.loads(output.getvalue())
        self.assertEqual([(item["matchId"], item["reducedOvers"]) for item in result],
                         [(10, False), (11, True)])
        self.assertIn("reduced=1 ordinary=1", summary.getvalue())
        self.assertIn("csv_rows=1", summary.getvalue())

    def test_regular_match_ids_must_be_positive_integers(self):
        self.assertEqual(parse_match_ids("10, 11,10"), frozenset({10, 11}))
        for value in ("", "10,", "0,11", "abc,11"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_match_ids(value)

    def test_ovs_status_supplies_allotted_limit_for_both_innings(self):
        payload = {"miniscore": {
            "status": "New Zealand won by 113 runs (Match reduced to 37 ovs due to rain)",
            "matchScoreDetails": {
                "matchId": 102058,
                "state": "Complete",
                "inningsScoreList": [
                    {"inningsId": 2, "overs": 30.2, "wickets": 10},
                    {"inningsId": 1, "overs": 36.6, "wickets": 9},
                ],
            },
        }}
        detection = detect(payload, "ODI")
        self.assertTrue(detection["reducedOvers"])
        self.assertEqual(detection["scheduledOvers"], 37)
        match = {
            **detection,
            "inningsIds": [1, 2],
            "revisedOversByInnings": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overs.txt"
            write_overs_file(path, [match])
            self.assertEqual(reduced_overs_by_match(str(path)), {102058: {1: 222, 2: 222}})

    def test_dls_alone_does_not_mark_match_reduced(self):
        for status in ("Won by DLS method", "Won by Duckworth-Lewis method"):
            with self.subTest(status=status):
                payload = {"miniscore": {
                    "status": status,
                    "matchScoreDetails": {"matchId": 10, "inningsScoreList": []},
                }}
                self.assertFalse(detect(payload, "T20")["reducedOvers"])

    def test_second_innings_dls_limit_keeps_first_innings_standard(self):
        match = {
            "matchId": 10,
            "startDate": "2026-01-01",
            "matchFormat": "ODI",
            "revisedOversByInnings": {},
        }
        payload = {"miniscore": {
            "status": "Auckland won by 33 runs (DLS Method) - 2nd innings reduced to 46 overs due to rain, target 345",
            "matchScoreDetails": {
                "matchId": 10,
                "state": "Complete",
                "inningsScoreList": [
                    {"inningsId": 1, "overs": 49.6, "wickets": 8},
                    {"inningsId": 2, "overs": 45.3, "wickets": 9},
                ],
            },
        }}
        with patch("detect_reduced_overs.read_payload", return_value=payload):
            inspected = inspect_match(match)
        self.assertEqual(inspected["allottedOversByInnings"], {1: 50, 2: 46})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overs.txt"
            write_overs_file(path, [inspected])
            self.assertEqual(reduced_overs_by_match(str(path)), {10: {1: 300, 2: 276}})

    def test_first_innings_only_limit_does_not_guess_second(self):
        payload = {"miniscore": {
            "status": "1st innings reduced to 45 overs due to rain",
            "matchScoreDetails": {"matchId": 10, "inningsScoreList": [
                {"inningsId": 1, "overs": 44.6}, {"inningsId": 2, "overs": 40.2},
            ]},
        }}
        detected = detect(payload, "ODI")
        self.assertEqual(detected["allottedOversByInnings"], {1: 45})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overs.txt"
            with self.assertRaisesRegex(ValueError, "match 10 innings 2: allotted overs unknown"):
                write_overs_file(path, [{**detected, "inningsIds": [1, 2], "revisedOversByInnings": {}}])
            self.assertFalse(path.exists())

    def test_second_innings_reduction_does_not_infer_first_limit_from_overs_played(self):
        payload = {"miniscore": {
            "status": "2nd innings reduced to 46 overs due to rain",
            "matchScoreDetails": {
                "matchId": 10, "state": "Complete",
                "inningsScoreList": [
                    {"inningsId": 1, "overs": 44.6, "wickets": 9},
                    {"inningsId": 2, "overs": 42.3, "wickets": 8},
                ],
            },
        }}
        detected = detect(payload, "ODI")
        self.assertEqual(detected["allottedOversByInnings"], {1: None, 2: 46})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "match 10 innings 1: allotted overs unknown"):
                write_overs_file(Path(directory) / "overs.txt", [
                    {**detected, "inningsIds": [1, 2], "revisedOversByInnings": {}}
                ])

    def test_scorecard_fallback_marks_candidate_without_guessing_allotment(self):
        payload = {"miniscore": {
            "status": "Team A won by 20 runs",
            "matchScoreDetails": {
                "matchId": 10, "state": "Complete",
                "inningsScoreList": [
                    {"inningsId": 1, "overs": 44.6, "wickets": 9},
                    {"inningsId": 2, "overs": 40.2, "wickets": 10},
                ],
            },
        }}
        detected = detect(payload, "ODI")
        self.assertTrue(detected["reducedOvers"])
        self.assertIsNone(detected["scheduledOvers"])
        self.assertEqual(detected["allottedOversByInnings"], {})

    def test_no_result_due_to_rain_is_not_processed(self):
        match = {
            "matchId": 149732,
            "startDate": "2026-01-01",
            "matchFormat": "T20",
            "revisedOversByInnings": {},
        }
        for status_location in ("status", "customStatus"):
            with self.subTest(status_location=status_location):
                payload = {"miniscore": {
                    "status": "Rain reduced the match",
                    "matchScoreDetails": {
                        "matchId": 149732,
                        "inningsScoreList": [{"inningsId": 1, "overs": 8.2}],
                    },
                }}
                if status_location == "status":
                    payload["miniscore"]["status"] = "No result (due to rain)"
                else:
                    payload["miniscore"]["matchScoreDetails"]["customStatus"] = "No result (due to rain)"
                with patch("detect_reduced_overs.read_payload", return_value=payload), \
                        patch("detect_reduced_overs.detect") as detect:
                    self.assertIsNone(inspect_match(match))
                    detect.assert_not_called()

    def test_writes_every_innings_with_its_own_limit(self):
        matches = [{
            "matchId": 10,
            "reducedOvers": True,
            "standardOvers": 20,
            "scheduledOvers": 18,
            "inningsIds": [1, 2],
            "revisedOversByInnings": {2: 17.3},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overs.txt"
            write_overs_file(path, matches)
            with path.open(encoding="utf-8", newline="") as output:
                self.assertEqual(list(csv.reader(output)), [
                    ["matchId", "inningsId", "scheduledOvers", "reducedOvers"],
                    ["10", "1", "20", "18"],
                    ["10", "2", "20", "17.3"],
                ])
            self.assertEqual(reduced_overs_by_match(str(path)), {10: {1: 108, 2: 105}})

    def test_unknown_limit_does_not_create_partial_file(self):
        matches = [{
            "matchId": 10,
            "reducedOvers": True,
            "standardOvers": 20,
            "scheduledOvers": None,
            "inningsIds": [1, 2],
            "revisedOversByInnings": {1: 18},
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overs.txt"
            with self.assertRaisesRegex(ValueError, "innings 2: allotted overs unknown"):
                write_overs_file(path, matches)
            self.assertFalse(path.exists())

    def test_rejects_invalid_over_ball_notation(self):
        matches = [{
            "matchId": 10,
            "reducedOvers": True,
            "standardOvers": 20,
            "scheduledOvers": None,
            "inningsIds": [1],
            "revisedOversByInnings": {1: 17.7},
        }]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "invalid allotted overs 17.7"):
                write_overs_file(Path(directory) / "overs.txt", matches)

    def test_reports_all_csv_blockers_before_writing(self):
        matches = [
            {"matchId": 10, "reducedOvers": True, "standardOvers": 20,
             "scheduledOvers": None, "inningsIds": [1, 2], "revisedOversByInnings": {}},
            {"matchId": 11, "reducedOvers": True, "standardOvers": 20,
             "scheduledOvers": 18, "inningsIds": [], "revisedOversByInnings": {}},
            {"matchId": 12, "error": "request timed out"},
            {"matchId": 13, "reducedOvers": True, "standardOvers": 20,
             "scheduledOvers": None, "inningsIds": [1], "revisedOversByInnings": {1: 17.7}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overs.txt"
            with self.assertRaises(ValueError) as raised:
                write_overs_file(path, matches)
            self.assertFalse(path.exists())
        message = str(raised.exception)
        self.assertIn("match IDs: 10,11,12,13", message)
        for detail in ("5 issue(s)", "match 10 innings 1", "match 10 innings 2",
                       "match 11: no innings", "match 12: request timed out",
                       "match 13 innings 1: invalid allotted overs 17.7"):
            self.assertIn(detail, message)

    def test_json_prints_even_when_file_cannot_be_completed(self):
        match = {
            "matchId": 149732,
            "reducedOvers": True,
            "standardOvers": 20,
            "scheduledOvers": None,
            "inningsIds": [1],
            "revisedOversByInnings": {},
        }
        output = StringIO()
        summary = StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch("sys.argv", ["detect_reduced_overs.py", "--series-id=9241",
                                   f"--output-file={directory}/overs.txt"]), \
                patch("detect_reduced_overs.database_match_ids", return_value=[match]), \
                patch("detect_reduced_overs.inspect_match", return_value=match), \
                redirect_stdout(output), redirect_stderr(summary):
            self.assertEqual(main(), 1)
            self.assertEqual(json.loads(output.getvalue()), [match])
            self.assertFalse((Path(directory) / "overs.txt").exists())
        self.assertIn("match 149732 innings 1", summary.getvalue())
        self.assertIn("match IDs: 149732", summary.getvalue())
        self.assertNotIn("Traceback", summary.getvalue())
        self.assertIn("scanned=1 reduced=1 ordinary=0 excluded=0 errors=0", summary.getvalue())
        self.assertIn("csv_rows=not_written", summary.getvalue())

    def test_summary_counts_match_outcomes_without_changing_json(self):
        reduced = {"matchId": 1, "reducedOvers": True}
        ordinary = {"matchId": 2, "reducedOvers": False}
        failed = {"matchId": 4, "error": "request timed out"}
        output, summary = StringIO(), StringIO()
        with patch("sys.argv", ["detect_reduced_overs.py", "--series-id=9241"]), \
                patch("detect_reduced_overs.database_match_ids", return_value=[1, 2, 3, 4]), \
                patch("detect_reduced_overs.inspect_match", side_effect=[reduced, ordinary, None, failed]), \
                redirect_stdout(output), redirect_stderr(summary):
            main()
        self.assertEqual(json.loads(output.getvalue()), [reduced, failed])
        self.assertIn(
            "scanned=4 reduced=1 ordinary=1 excluded=1 errors=1 json_entries=2 csv_rows=not_requested",
            summary.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()

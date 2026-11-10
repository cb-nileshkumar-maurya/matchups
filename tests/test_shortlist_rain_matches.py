from openpyxl import load_workbook
from datetime import date, datetime, timezone
from concurrent.futures import wait as wait_for_futures
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.shortlist_rain_matches import classify, database_match_ids, inspect, commentary_text, find_rain_commentary, connect_cassandra, read_payload, scan_matches, write_outputs

MATCH = {"matchId": 10, "startDate": "2026-01-01", "matchFormat": "T20"}


def payload(text="", first=20, second=18, wickets=3):
    return {"miniscore": {"status": text, "matchScoreDetails": {
        "state": "Complete", "inningsScoreList": [
            {"inningsId": 1, "overs": first, "score": 150, "wickets": wickets},
            {"inningsId": 2, "overs": second, "score": 151, "wickets": 4},
        ]}}}


class ShortlistTests(unittest.TestCase):
    def test_progress_reports_counts_eta_and_preserves_match_order(self):
        matches = [MATCH, {**MATCH, "matchId": 11}]
        def inspected(match, session):
            return {**match, "classification": "review" if match["matchId"] == 10 else "excluded"}
        with patch("src.shortlist_rain_matches.inspect", side_effect=inspected), \
                self.assertLogs("src.shortlist_rain_matches", level="INFO") as logs:
            results = scan_matches(matches, Mock(), 2)
        self.assertEqual([result["matchId"] for result in results], [10, 11])
        self.assertIn("2/2 completed", logs.output[-1])
        self.assertIn("scan ETA=0s", logs.output[-1])
        self.assertIn("review=1 confirmed=0 excluded=1", logs.output[-1])

    def test_empty_scan_returns_no_results(self):
        self.assertEqual(scan_matches([], Mock(), 2), [])

    def test_progress_heartbeat_before_first_completion(self):
        calls = 0
        def wait_once_without_completion(pending, **options):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertEqual(options["timeout"], 15)
                return set(), set(pending)
            return wait_for_futures(pending, **options)
        with patch("src.shortlist_rain_matches.inspect", return_value={"classification": "review"}), \
                patch("src.shortlist_rain_matches.wait", side_effect=wait_once_without_completion), \
                self.assertLogs("src.shortlist_rain_matches", level="INFO") as logs:
            scan_matches([MATCH], Mock(), 1)
        self.assertIn("0/1 completed", logs.output[0])
        self.assertIn("scan ETA=estimating", logs.output[0])
        self.assertIn("1/1 completed", logs.output[-1])

    def test_full_quota_logs_skip_and_final_result(self):
        with patch("src.shortlist_rain_matches.read_payload", return_value=payload(second=20)), \
                self.assertLogs("src.shortlist_rain_matches", level="DEBUG") as logs:
            inspect(MATCH, Mock())
        text = "\n".join(logs.output)
        self.assertIn("fetching livescore", text)
        self.assertIn("checking full-quota exclusion", text)
        self.assertIn("skipping Cassandra", text)
        self.assertIn("excluded | Both innings used the full standard quota | elapsed=", text)

    def test_match_details_are_silent_at_info(self):
        with patch("src.shortlist_rain_matches.read_payload", return_value=payload(second=20)), \
                self.assertNoLogs("src.shortlist_rain_matches", level="INFO"):
            inspect(MATCH, Mock())

    def test_progress_is_throttled_for_fast_matches(self):
        matches = [{**MATCH, "matchId": index} for index in range(20)]
        with patch("src.shortlist_rain_matches.inspect", return_value={"classification": "excluded"}), \
                patch("src.shortlist_rain_matches.monotonic", return_value=100), \
                self.assertLogs("src.shortlist_rain_matches", level="INFO") as logs:
            scan_matches(matches, Mock(), 2)
        self.assertLessEqual(len(logs.output), 2)
        self.assertIn("20/20 completed", logs.output[-1])

    def test_cassandra_connection_supports_tunnel_and_auth(self):
        settings = {"CASSANDRA_CONTACT_POINTS": "localhost:9043", "CASSANDRA_KEYSPACE": "matches_data",
                    "CASSANDRA_TUNNEL_MODE": "true", "CASSANDRA_USERNAME": "user",
                    "CASSANDRA_PASSWORD": "password"}
        with patch.dict(os.environ, settings, clear=True), patch("cassandra.cluster.Cluster") as cluster:
            owner, session = connect_cassandra()
            self.assertIs(owner, cluster.return_value)
            self.assertIs(session, owner.connect.return_value)
            self.assertEqual(cluster.call_args.args, (("localhost",),))
            self.assertEqual(cluster.call_args.kwargs["port"], 9043)
            self.assertFalse(cluster.call_args.kwargs["schema_metadata_enabled"])
            self.assertIn("auth_provider", cluster.call_args.kwargs)
            owner.connect.assert_called_once_with("matches_data")
            self.assertEqual(session.default_timeout, 120)

    def test_cassandra_connection_failure_shuts_down_cluster(self):
        settings = {"CASSANDRA_CONTACT_POINTS": "localhost", "CASSANDRA_KEYSPACE": "matches_data"}
        with patch.dict(os.environ, settings, clear=True), patch("cassandra.cluster.Cluster") as cluster:
            cluster.return_value.connect.side_effect = RuntimeError("cannot connect")
            with self.assertRaisesRegex(RuntimeError, "cannot connect"):
                connect_cassandra()
            cluster.return_value.shutdown.assert_called_once()

    def test_archive_query_returns_match_metadata(self):
        credentials = {"MARIADB_HOST": "localhost:3307", "MARIADB_USER": "user",
                       "MARIADB_PASSWORD": "password", "MARIADB_DATABASE": "db"}
        with patch.dict(os.environ, credentials), patch("src.shortlist_rain_matches.pymysql.connect") as connect:
            cursor = connect.return_value.cursor.return_value.__enter__.return_value
            cursor.__iter__.return_value = iter([(10, date(2026, 1, 1), 2)])
            self.assertEqual(database_match_ids("m.id = %s", (10,)), [
                {"matchId": 10, "startDate": "2026-01-01", "matchFormat": "ODI"}])
            self.assertEqual(cursor.execute.call_args.args[1], (10,))
            self.assertEqual(connect.call_args.kwargs["port"], 3307)
            connect.return_value.close.assert_called_once()

    def test_fetch_handles_empty_response(self):
        with patch("src.shortlist_rain_matches.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 204
            self.assertIsNone(read_payload(10))
            self.assertIn("/10/livescore", urlopen.call_args.args[0])

    def test_commentary_expands_cassandra_format_markers(self):
        from collections import namedtuple
        Format = namedtuple("Format", "format_id format_value")
        row = {"comm_txt": "B0$", "comm_format": {
            "bold": Format(["B0$"], ["15 overs per side due to rain"])}}
        self.assertEqual(commentary_text(row), "15 overs per side due to rain")

    def test_commentary_scans_both_innings_newest_first_and_stops(self):
        def row(innings, second, text):
            return {"innings_id": innings, "ball_nbr": 0,
                    "time_score": datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc),
                    "comm_txt": text, "comm_format": None}
        session = Mock()
        session.execute.side_effect = [
            [row(1, 5, "15 overs per side due to rain")],
            [row(2, 10, "Match finished"), row(2, 8, "2nd innings reduced to 18 overs due to rain"),
             row(2, 2, "Rain has stopped")],
        ]
        with patch("src.shortlist_rain_matches.commentary_text", wraps=commentary_text) as parse:
            found = find_rain_commentary(session, 10)
        self.assertEqual(found["inningsId"], 2)
        self.assertIn("18 overs", found["text"])
        self.assertEqual(parse.call_count, 2)
        self.assertEqual([call.args[1] for call in session.execute.call_args_list], [(10, 1), (10, 2)])
        self.assertTrue(all("FROM match_bbb_miniscore WHERE" in call.args[0]
                            and "ball_nbr = 0" in call.args[0]
                            for call in session.execute.call_args_list))

    def test_no_matching_commentary(self):
        session = Mock()
        session.execute.return_value = []
        self.assertIsNone(find_rain_commentary(session, 10))

    def test_normal_chase_excluded(self):
        result = classify(MATCH, payload())
        self.assertEqual(result["classification"], "excluded")
        self.assertIn("Likely regular", result["reason"])

    def test_full_quotas_skip_cassandra_even_with_rain_status(self):
        for match_format, overs in (("T20", 20), ("ODI", 50), ("T20", 19.6)):
            with self.subTest(match_format=match_format, overs=overs):
                session = Mock()
                data = payload("Rain interrupted play", first=overs, second=overs)
                with patch("src.shortlist_rain_matches.read_payload", return_value=data):
                    result = inspect({**MATCH, "matchFormat": match_format}, session)
                self.assertEqual(result["classification"], "excluded")
                self.assertEqual(result["reason"], "Both innings used the full standard quota")
                standard = 50 if match_format == "ODI" else 20
                self.assertEqual(result["limits"], {1: standard, 2: standard})
                session.execute.assert_not_called()

    def test_natural_finish_still_checks_cassandra(self):
        session = Mock()
        session.execute.return_value = []
        with patch("src.shortlist_rain_matches.read_payload", return_value=payload(first=15, wickets=10)):
            result = inspect(MATCH, session)
        self.assertEqual(result["classification"], "excluded")
        self.assertIn("Likely regular", result["reason"])
        self.assertEqual(session.execute.call_count, 2)

    def test_full_first_innings_and_successful_chase_skip_cassandra(self):
        session = Mock()
        with patch("src.shortlist_rain_matches.read_payload", return_value=payload()):
            result = inspect(MATCH, session)
        self.assertEqual(result["classification"], "excluded")
        self.assertIn("successful chase", result["reason"])
        self.assertEqual(result["limits"], {1: 20})
        session.execute.assert_not_called()

    def test_successful_chase_with_dls_status_still_checks_cassandra(self):
        session = Mock()
        session.execute.return_value = []
        with patch("src.shortlist_rain_matches.read_payload", return_value=payload("Won (DLS Method)")):
            result = inspect(MATCH, session)
        self.assertEqual(result["classification"], "review")
        self.assertEqual(session.execute.call_count, 2)

    def test_crease_entry_is_skipped_and_scan_continues(self):
        for ending in ("comes to the crease", "walks onto crease....", "comes onto the crease…"):
            with self.subTest(ending=ending):
                session = Mock()
                session.execute.side_effect = [[
                    {"innings_id": 1, "ball_nbr": 0, "time_score": datetime(2026, 1, 2),
                     "comm_txt": f"Lewis Gregory, right handed bat, {ending}"},
                    {"innings_id": 1, "ball_nbr": 0, "time_score": datetime(2026, 1, 1),
                     "comm_txt": "15 overs per side due to rain"},
                ], []]
                found = find_rain_commentary(session, 10)
                self.assertEqual(found["text"], "15 overs per side due to rain")

    def test_incomplete_full_quota_match_is_not_excluded(self):
        data = payload(first=20, second=20)
        data["miniscore"]["matchScoreDetails"]["state"] = "In Progress"
        self.assertEqual(classify(MATCH, data)["classification"], "review")

    def test_abandoned_without_play_is_excluded_without_cassandra(self):
        for innings in ([], [{"inningsId": 1, "overs": 0, "score": 0, "wickets": 0}]):
            with self.subTest(innings=innings):
                data = {"miniscore": {"status": "Match abandoned due to rain (no toss)",
                        "matchScoreDetails": {"state": "Abandon", "inningsScoreList": innings}}}
                session = Mock()
                with patch("src.shortlist_rain_matches.read_payload", return_value=data):
                    result = inspect(MATCH, session)
                self.assertEqual(result["classification"], "excluded")
                self.assertEqual(result["reason"], "Abandoned match with no play recorded")
                self.assertEqual(result["limits"], {})
                session.execute.assert_not_called()

    def test_abandoned_after_play_or_unknown_overs_stays_in_review(self):
        for innings in ([{"inningsId": 1, "overs": 0.1, "score": 0, "wickets": 0}],
                        [{"inningsId": 1, "score": 0}],
                        [{"inningsId": 1, "overs": 0, "score": 1, "wickets": 0}]):
            with self.subTest(innings=innings):
                data = {"miniscore": {"status": "Match abandoned due to rain",
                        "matchScoreDetails": {"state": "Abandon", "inningsScoreList": innings}}}
                self.assertEqual(classify(MATCH, data)["classification"], "review")

    def test_missing_innings_without_abandonment_stays_in_review(self):
        data = {"miniscore": {"status": "Match starts tomorrow", "matchScoreDetails": {}}}
        self.assertEqual(classify(MATCH, data)["classification"], "review")

    def test_dls_without_limit_goes_to_review(self):
        self.assertEqual(classify(MATCH, payload("Won (DLS Method)"))["classification"], "review")

    def test_second_innings_reduction_and_full_first_confirmed(self):
        result = classify(MATCH, payload("2nd innings reduced to 18 overs due to rain"))
        self.assertEqual(result["classification"], "confirmed")
        self.assertEqual(result["limits"], {1: 20, 2: 18})

    def test_all_out_does_not_prove_first_limit(self):
        result = classify(MATCH, payload("2nd innings reduced to 18 overs due to rain", first=15, wickets=10))
        self.assertEqual(result["classification"], "review")
        self.assertNotIn(1, result["limits"])

    def test_common_rain_limit_confirmed(self):
        result = classify(MATCH, payload("15 overs per side due to rain", first=15, second=14))
        self.assertEqual(result["classification"], "confirmed")
        self.assertEqual(result["limits"], {1: 15, 2: 15})

    def test_wet_outfield_confirms_status_or_cassandra_limits(self):
        text = "42 Ovs game due to wet outfield"
        match = {**MATCH, "matchFormat": "ODI"}
        for source in ("status", "commentary"):
            with self.subTest(source=source):
                data = payload(text if source == "status" else "Won by 43 runs", first=41.6, second=39.6)
                commentary = ({"text": text, "inningsId": 2, "timeScore": "2025-04-05T06:53:14"}
                              if source == "commentary" else None)
                result = classify(match, data, commentary)
                self.assertEqual(result["classification"], "confirmed")
                self.assertEqual(result["limits"], {1: 42, 2: 42})

    def test_wet_outfield_without_limits_stays_in_review(self):
        self.assertEqual(classify(MATCH, payload("Play delayed due to wet outfield"))["classification"], "review")

    def test_ambiguous_reduction_is_review(self):
        self.assertEqual(classify(MATCH, payload("Reduced to 18 overs due to rain"))["classification"], "review")

    def test_missing_rain_cause_is_review(self):
        self.assertEqual(classify(MATCH, payload("18 overs per side", first=18))["classification"], "review")

    def test_scorecard_contradiction_is_review(self):
        self.assertEqual(classify(MATCH, payload("15 overs per side due to rain"))["classification"], "review")

    def test_cassandra_comment_can_confirm_rain(self):
        result = classify(MATCH, payload(), {"text": "2nd innings reduced to 18 overs due to rain",
                                            "inningsId": 2, "timeScore": "2026-01-01T00:00:00"})
        self.assertEqual(result["classification"], "confirmed")
        self.assertEqual(result["limits"], {1: 20, 2: 18})
        self.assertIn("time_score", result["evidence"][-1])

    def test_conflicting_sources_are_review(self):
        data = payload("2nd innings reduced to 18 overs due to rain")
        result = classify(MATCH, data, {"text": "2nd innings reduced to 19 overs due to rain",
                                        "inningsId": 2, "timeScore": "2026-01-01T00:00:00"})
        self.assertEqual(result["classification"], "review")

    def test_cassandra_failure_goes_to_review(self):
        session = Mock()
        session.execute.side_effect = RuntimeError("Cassandra unavailable")
        with patch("src.shortlist_rain_matches.read_payload", return_value=payload(first=15, wickets=10)), \
                self.assertLogs("src.shortlist_rain_matches", level="ERROR"):
            result = inspect(MATCH, session)
        self.assertEqual(result["classification"], "review")
        self.assertIn("Cassandra unavailable", result["reason"])

    def test_fetch_error_retained(self):
        with patch("src.shortlist_rain_matches.read_payload", side_effect=ValueError("broken")), \
                self.assertLogs("src.shortlist_rain_matches", level="ERROR"):
            result = inspect(MATCH, Mock())
        self.assertEqual(result["classification"], "review")
        self.assertIn("broken", result["reason"])

    def test_exports_have_innings_limits_and_evidence(self):
        result = classify(MATCH, payload("2nd innings reduced to 18 overs due to rain"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rain.xlsx"
            write_outputs(path, [result])
            workbook = load_workbook(path)
            self.assertEqual(workbook.sheetnames, ["Review", "Confirmed rain", "Excluded regular", "Evidence"])
            sheet = workbook["Confirmed rain"]
            self.assertEqual([sheet.cell(row, 12).value for row in (2, 3)], [20, 18])
            self.assertTrue(all("due to rain" in sheet.cell(row, 13).value for row in (2, 3)))
            self.assertEqual(sheet.freeze_panes, "A2")
            self.assertEqual(sheet.auto_filter.ref, "A1:M3")
            self.assertEqual(workbook["Evidence"].max_row, 3)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            workbook.close()

    def test_api_text_is_not_exported_as_formula(self):
        result = classify(MATCH, payload("=untrusted"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rain.xlsx"
            write_outputs(path, [result])
            workbook = load_workbook(path)
            cell = workbook["Excluded regular"]["F2"]
            self.assertEqual(cell.value, "=untrusted")
            self.assertEqual(cell.data_type, "s")
            workbook.close()



if __name__ == "__main__":
    unittest.main()

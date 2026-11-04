import io
import logging
import os
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from tempfile import NamedTemporaryFile
from unittest.mock import Mock, patch

from backfill_matchups import (
    LOGGER,
    MatchCannotBeBackfilled,
    DatabaseConfig,
    TARGET_COLUMNS,
    bowling_style_details,
    bowling_type,
    calculate_matchup_rows,
    database_config,
    fetch_match_sources,
    fetch_mariadb_bowling_styles,
    fetch_series_type,
    innings_phases,
    log_match_groups,
    main,
    parse_arguments,
    player_details,
    replace_rows,
    run_backfill,
    selected_match_ids,
)


class RecordingCursor:
    def __init__(self, rows=None, fail_inserts=False):
        self.rows = rows or []
        self.fail_inserts = fail_inserts
        self.executed = []
        self.inserted = []
        self.closed = False

    def execute(self, statement, parameters=()):
        self.executed.append((statement, parameters))

    def executemany(self, statement, rows):
        if self.fail_inserts:
            raise RuntimeError("insert failed")
        self.executed.append((statement, ()))
        self.inserted = list(rows)

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class RecordingConnection:
    def __init__(self, cursor):
        self.recording_cursor = cursor
        self.began = False
        self.committed = False
        self.rolled_back = False

    def cursor(self, **_kwargs):
        return self.recording_cursor

    def begin(self):
        self.began = True

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def phase_sources(match_format="T20", pp_to=6, ball_numbers=(1,)):
    match_info = {
        "match_id": 10, "match_format": match_format, "venue": {"id": 55},
        "team1": {"player_details": [
            {"id": 101, "batting_style": "RIGHT", "bowling_style": "Right-arm offbreak"}
        ]},
        "team2": {"player_details": [
            {"id": 202, "batting_style": "LEFT", "bowling_style": "Right-arm fast"}
        ]},
    }
    scorecards = [{
        "innings_id": 1, "bat_team_id": 1, "bowl_team_id": 2,
        "batsmen_data": {"bat_1": {"bat_id": 101}},
        "pp_data": {"pp_1": {
            "pp_overs_from": 0.1, "pp_overs_to": pp_to, "pp_type": "mandatory",
        }},
    }]
    events = [
        {"innings_id": 1, "ball_nbr": ball, "bat_striker_id": 101,
         "bowl_striker_id": 202, "legal_runs": 0, "total_runs": 0,
         "highlights_id": 0, "wkt_code": "NONE", "extra_code": "NONE"}
        for ball in ball_numbers
    ]
    return match_info, scorecards, events


def fake_driver_modules(connection):
    modules = {
        name: types.ModuleType(name)
        for name in (
            "pymysql", "cassandra", "cassandra.auth", "cassandra.cluster",
            "cassandra.policies", "cassandra.query",
        )
    }
    modules["pymysql"].connect = Mock(return_value=connection)
    modules["pymysql"].cursors = types.SimpleNamespace(DictCursor=object())
    modules["cassandra.auth"].PlainTextAuthProvider = Mock()
    modules["cassandra.cluster"].Cluster = Mock(return_value=Mock())
    modules["cassandra.policies"].WhiteListRoundRobinPolicy = Mock()
    modules["cassandra.query"].dict_factory = object()
    return modules


class CalculateMatchupRowsTests(unittest.TestCase):
    def test_standard_odi_innings_in_mixed_overs_file_uses_regular_boundaries(self) -> None:
        scorecard = {"innings_id": 1, "pp_data": {"pp_1": {
            "pp_overs_from": 0.1, "pp_overs_to": 10, "pp_type": "mandatory",
        }}}
        self.assertEqual(innings_phases(scorecard, "ODI", 300), (
            (1, 60, "POWERPLAY"), (61, 240, "MIDDLE_OVERS"), (241, 300, "DEATH_OVERS"),
        ))
        self.assertEqual(innings_phases({**scorecard, "innings_id": 2}, "ODI", 276), (
            (1, 60, "POWERPLAY"), (61, 207, "MIDDLE_OVERS"), (208, 276, "DEATH_OVERS"),
        ))

    def test_regular_t20_in_overs_file_uses_new_fixed_boundaries(self) -> None:
        scorecard = phase_sources()[1][0]
        self.assertEqual(innings_phases(scorecard, "T20", 120), (
            (1, 36, "POWERPLAY"), (37, 90, "MIDDLE_OVERS"), (91, 120, "DEATH_OVERS"),
        ))

    def test_reduced_odi_short_branches(self) -> None:
        scorecard = phase_sources(match_format="ODI", pp_to=10)[1][0]
        self.assertEqual(innings_phases(scorecard, "ODI", 90), (
            (1, 60, "POWERPLAY"), (61, 90, "DEATH_OVERS"),
        ))
        scorecard["pp_data"]["pp_1"]["pp_overs_to"] = 8
        self.assertEqual(innings_phases(scorecard, "ODI", 72), (
            (1, 48, "POWERPLAY"), (49, 54, "MIDDLE_OVERS"), (55, 72, "DEATH_OVERS"),
        ))

    def test_reduced_bbl_uses_generic_t20_powerplay_comparison(self) -> None:
        scorecard = phase_sources(pp_to=4)[1][0]
        self.assertEqual(innings_phases(scorecard, "T20", 60, series_type="BBL"), (
            (1, 24, "POWERPLAY"), (25, 45, "MIDDLE_OVERS"), (46, 60, "DEATH_OVERS"),
        ))

    def test_main_resets_logging_level_between_verbose_and_info_runs(self) -> None:
        previous_level = LOGGER.level
        try:
            with patch("backfill_matchups.logging.basicConfig"), \
                    patch("backfill_matchups.database_config", return_value=Mock()), \
                    patch("backfill_matchups.run_backfill", return_value=True):
                self.assertEqual(main(["--match-id", "10", "--verbose"]), 0)
                self.assertEqual(LOGGER.level, logging.DEBUG)
                self.assertEqual(main(["--match-id", "10"]), 0)
                self.assertEqual(LOGGER.level, logging.INFO)
        finally:
            LOGGER.setLevel(previous_level)

    def test_info_logs_phase_ranges_without_per_ball_details(self) -> None:
        sources = phase_sources(pp_to=5, ball_numbers=(30, 31, 97))
        with self.assertLogs("backfill_matchups", level="INFO") as logs:
            calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 105})
        output = "\n".join(logs.output)
        self.assertIn("match 10 innings 1: reduced-overs phases", output)
        self.assertIn("POWERPLAY=1-30", output)
        self.assertIn("MIDDLE_OVERS=31-78", output)
        self.assertIn("DEATH_OVERS=79-105", output)
        self.assertIn("match 10: processing 3 eligible ball-by-ball events", output)
        self.assertNotIn("processing match_id=10", output)
        self.assertNotIn("playersMap", output)

    def test_verbose_logs_players_events_and_phase_source(self) -> None:
        sources = phase_sources(pp_to=5, ball_numbers=(30,))
        sources[0]["team1"]["player_details"][0]["name"] = "Test Batter"
        with self.assertLogs("backfill_matchups", level="DEBUG") as logs:
            calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 105})
        output = "\n".join(logs.output)
        self.assertIn("match 10 innings 1: reduced-overs phases", output)
        self.assertIn("playersMap match_id=10 player_id=101 name='Test Batter'", output)
        self.assertIn("processing match_id=10", output)
        self.assertIn("'ball_nbr': 30", output)
        self.assertIn("pp_overs_from=0.1 pp_overs_to=5 allotted_balls=105", output)

    def test_maps_all_bowling_styles_to_exact_stored_labels(self) -> None:
        cases = (
            ("Right Arm fast", "Right-arm fast", "PACE"),
            ("Right Arm fast medium", "Right-arm fast-medium", "PACE"),
            ("Right Arm medium", "Right-arm medium", "PACE"),
            ("Right Arm off break", "Right-arm offbreak", "SPIN"),
            ("Right Arm leg break", "Right-arm legbreak", "SPIN"),
            ("Left Arm fast", "Left-arm fast", "PACE"),
            ("Left Arm fast medium", "Left-arm fast-medium", "PACE"),
            ("Left Arm medium", "Left-arm medium", "PACE"),
            ("Left arm orthodox", "Left-arm orthodox", "SPIN"),
            ("Left Arm wrist spin", "Left-arm wrist-spin", "SPIN"),
        )
        for source, technique, bowling_type_value in cases:
            with self.subTest(source=source):
                self.assertEqual(bowling_style_details(source), (technique, bowling_type_value))

    def test_classifies_cassandra_spin_style_spacing(self) -> None:
        self.assertEqual(bowling_type("Right Arm off break"), "SPIN")
        self.assertEqual(bowling_type("Right Arm leg break"), "SPIN")

    def test_player_map_excludes_support_staff_and_unavailable_players(self) -> None:
        match_info = {
            "team1": {"player_details": [
                {"id": 1, "name": "Playing XI", "substitute": False},
                {"id": 2, "name": "Bench", "substitute": True},
                {"id": 3, "name": "Bench with missing flag"},
                {"id": 4, "name": "Substitute", "spl_substitute": True},
                {"id": 5, "name": "Staff", "is_support_staff": True},
                {"id": 6, "name": "Unavailable", "is_unavailable": True},
                {"id": 7, "name": "Staff substitute", "is_support_staff": True,
                 "spl_substitute": True},
            ]},
            "team2": {"player_details": [
                {"id": 8, "name": "Other team's player", "substitute": False},
            ]},
        }

        self.assertEqual(set(player_details(match_info)), {1, 2, 3, 4, 8})

    def test_splits_cassandra_host_and_port_from_contact_points(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MARIADB_HOST": "localhost",
                "MARIADB_USER": "user",
                "MARIADB_PASSWORD": "password",
                "MARIADB_DATABASE": "cricbuzz_sitedb",
                "CASSANDRA_CONTACT_POINTS": "127.0.0.1:29042",
                "CASSANDRA_TUNNEL_MODE": "true",
                "CASSANDRA_KEYSPACE": "matches_data",
            },
            clear=True,
        ):
            config = database_config()

        self.assertEqual(config.cassandra_contacts, ("127.0.0.1",))
        self.assertEqual(config.cassandra_port, 29042)
        self.assertTrue(config.cassandra_tunnel_mode)

    def test_tunnel_connection_uses_only_forwarded_host_and_cleans_up_on_failure(self) -> None:
        cluster = Mock()
        cluster.connect.side_effect = RuntimeError("connection failed")
        cluster_type = Mock(return_value=cluster)
        whitelist_policy = Mock(return_value=object())
        driver_modules = {
            name: types.ModuleType(name)
            for name in (
                "pymysql",
                "cassandra",
                "cassandra.auth",
                "cassandra.cluster",
                "cassandra.policies",
                "cassandra.query",
            )
        }
        driver_modules["cassandra.auth"].PlainTextAuthProvider = Mock()
        driver_modules["cassandra.cluster"].Cluster = cluster_type
        driver_modules["cassandra.policies"].WhiteListRoundRobinPolicy = whitelist_policy
        driver_modules["cassandra.query"].dict_factory = object()
        config = DatabaseConfig(
            mariadb_host="localhost",
            mariadb_port=3306,
            mariadb_user="user",
            mariadb_password="password",
            mariadb_database="cricbuzz_sitedb",
            cassandra_contacts=("127.0.0.1",),
            cassandra_port=29042,
            cassandra_tunnel_mode=True,
            cassandra_keyspace="matches_data",
        )

        with patch.dict(sys.modules, driver_modules), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                run_backfill(parse_arguments(["--match-id", "10"]), config)

        whitelist_policy.assert_called_once_with(("127.0.0.1",))
        cluster_type.assert_called_once()
        self.assertEqual(cluster_type.call_args.args, (("127.0.0.1",),))
        self.assertEqual(cluster_type.call_args.kwargs["port"], 29042)
        self.assertEqual(cluster_type.call_args.kwargs["connect_timeout"], 30)
        self.assertEqual(cluster_type.call_args.kwargs["control_connection_timeout"], 30)
        self.assertFalse(cluster_type.call_args.kwargs["schema_metadata_enabled"])
        self.assertFalse(cluster_type.call_args.kwargs["token_metadata_enabled"])
        cluster.shutdown.assert_called_once_with()

    def test_backfill_summary_counts_successes_and_lists_skipped_matches(self) -> None:
        cluster = Mock()
        driver_modules = {
            name: types.ModuleType(name)
            for name in (
                "pymysql", "cassandra", "cassandra.auth", "cassandra.cluster",
                "cassandra.policies", "cassandra.query",
            )
        }
        driver_modules["pymysql"].connect = Mock(return_value=Mock())
        driver_modules["pymysql"].cursors = types.SimpleNamespace(DictCursor=object())
        driver_modules["cassandra.auth"].PlainTextAuthProvider = Mock()
        driver_modules["cassandra.cluster"].Cluster = Mock(return_value=cluster)
        driver_modules["cassandra.policies"].WhiteListRoundRobinPolicy = Mock()
        driver_modules["cassandra.query"].dict_factory = object()
        config = DatabaseConfig(
            mariadb_host="localhost", mariadb_port=3306, mariadb_user="user",
            mariadb_password="password", mariadb_database="cricbuzz_sitedb",
            cassandra_contacts=("127.0.0.1",), cassandra_port=29042,
            cassandra_tunnel_mode=True, cassandra_keyspace="matches_data",
        )
        match_info = {"team1": {"player_details": []}, "team2": {"player_details": []}}
        sources = (match_info, [], [])

        calculated = iter([[{"matchId": 11}], []])

        def calculate_rows(*_sources, phase_behaviors, **_kwargs):
            rows = next(calculated)
            if rows:
                phase_behaviors.update({"regular-fixed", "regular-bbl"})
            return rows

        with patch.dict(sys.modules, driver_modules), patch.dict(os.environ, {}, clear=True), \
                patch("backfill_matchups.fetch_match_sources", side_effect=[
                    sources, MatchCannotBeBackfilled(
                        "unsupported match format: 'HUN'", "unsupported format 'HUN'"
                    ), sources,
                ]), \
                patch("backfill_matchups.fetch_series_type", return_value=""), \
                patch("backfill_matchups.calculate_matchup_rows", side_effect=calculate_rows) as calculate, \
                patch("backfill_matchups.replace_rows") as replace, \
                self.assertLogs("backfill_matchups", level="INFO") as logs:
            result = run_backfill(parse_arguments(["--match-id", "11,12,13"]), config)

        self.assertTrue(result)
        self.assertEqual(calculate.call_args_list[0].kwargs["series_type"], "")
        replace.assert_called_once()
        self.assertEqual(replace.call_args.args[1], 11)
        self.assertIn("match summary: selected=3 backfilled=1 skipped=2", "\n".join(logs.output))
        self.assertIn("backfilled matches by phase behavior:", "\n".join(logs.output))
        self.assertIn("regular fixed (1 match)", "\n".join(logs.output))
        self.assertIn("regular bbl (1 match)", "\n".join(logs.output))
        self.assertIn("reduced overs (0 matches)", "\n".join(logs.output))
        self.assertIn("skipped matches by reason:", "\n".join(logs.output))
        self.assertIn("unsupported format 'HUN' (1 match)", "\n".join(logs.output))
        self.assertIn("no ball by ball data present (1 match)", "\n".join(logs.output))
        skipped_section = "\n".join(logs.output).split("skipped matches by reason:\n", 1)[1]
        self.assertIn("match IDs: 13", skipped_section)
        self.assertIn("match IDs: 12", skipped_section)
        self.assertNotIn("skipped match: match_id=", "\n".join(logs.output))

    def test_match_id_groups_wrap_long_lists(self) -> None:
        with self.assertLogs("backfill_matchups", level="INFO") as logs:
            log_match_groups("skipped matches by reason", {"missing-powerplay": range(101, 115)})

        id_lines = [line for line in logs.output if "match IDs:" in line]
        self.assertEqual(len(id_lines), 2)
        self.assertIn("missing powerplay (14 matches)", "\n".join(logs.output))
        self.assertIn("match IDs: 113, 114", id_lines[1])

    def test_info_warning_shows_failing_ball_without_verbose(self) -> None:
        sources = phase_sources(ball_numbers=(85,))
        sources[0]["team1"]["player_details"][0]["batting_style"] = None
        config = DatabaseConfig(
            mariadb_host="localhost", mariadb_port=3306, mariadb_user="user",
            mariadb_password="password", mariadb_database="cricbuzz_sitedb",
            cassandra_contacts=("127.0.0.1",), cassandra_port=29042,
            cassandra_tunnel_mode=True, cassandra_keyspace="matches_data",
        )
        with patch.dict(sys.modules, fake_driver_modules(Mock())), \
                patch("backfill_matchups.selected_match_ids", return_value=[10]), \
                patch("backfill_matchups.fetch_series_type", return_value=""), \
                patch("backfill_matchups.fetch_match_sources", return_value=sources), \
                patch("backfill_matchups.fetch_mariadb_bowling_styles", return_value={}), \
                patch("backfill_matchups.replace_rows") as replace, \
                self.assertLogs("backfill_matchups", level="INFO") as logs:
            result = run_backfill(parse_arguments(["--match-id", "10"]), config)

        self.assertFalse(result)
        replace.assert_not_called()
        warning = next(line for line in logs.output if "was not backfilled" in line)
        self.assertIn("match 10", warning)
        self.assertIn("ball={'innings_id': 1", warning)
        self.assertIn("'ball_nbr': 85", warning)
        self.assertIn("'bowl_striker_id': 202", warning)
        self.assertNotIn("processing match_id=10", "\n".join(logs.output))

    def test_fetches_all_ball_events_from_commentary(self) -> None:
        events = [
            {"innings_id": 1, "time_score": "t1", "ball_nbr": 1, "wkt_code": "NONE"},
            {"innings_id": 1, "time_score": "t2", "ball_nbr": 2, "wkt_code": "run out"},
        ]
        session = Mock()

        def execute(statement, parameters):
            if "FROM match_info" in statement:
                return [{"match_id": 10}]
            if "FROM match_bbb_full_scorecard" in statement:
                return [{"innings_id": 1}]
            if "FROM match_bbb_commentary" in statement:
                self.assertEqual(parameters, (10, 1))
                return events
            raise AssertionError(statement)

        session.execute.side_effect = execute
        with self.assertLogs("backfill_matchups", level="INFO") as logs:
            _, _, fetched_events = fetch_match_sources(session, 10)

        self.assertEqual(fetched_events, events)
        self.assertIn("found match_info", logs.output[0])
        self.assertIn("found scorecard for innings 1", logs.output[1])
        self.assertIn("fetched 2 ball-by-ball commentary events", logs.output[2])
        self.assertEqual(
            sum("FROM match_bbb_commentary" in call.args[0] for call in session.execute.call_args_list),
            1,
        )

    def test_accepts_a_series_selector(self) -> None:
        arguments = parse_arguments(["--series-id", "9241", "--verbose"])

        self.assertEqual(arguments.series_id, 9241)
        self.assertIsNone(arguments.match_id)
        self.assertTrue(arguments.verbose)

    def test_match_id_selector_accepts_one_or_comma_separated_ids(self) -> None:
        connection = Mock()
        for text, expected in (("10", [10]), ("10,20,30", [10, 20, 30]), ("10, 20", [10, 20])):
            with self.subTest(text=text):
                arguments = parse_arguments(["--match-id", text])
                self.assertEqual(selected_match_ids(connection, arguments), expected)
        self.assertEqual(
            selected_match_ids(connection, parse_arguments(["--match-id=10,20"])),
            [10, 20],
        )
        connection.cursor.assert_not_called()

    def test_match_id_selector_rejects_invalid_or_duplicate_ids(self) -> None:
        for text in ("", "10,", "10,,20", "10,nope", "0", "-1", "10,10"):
            with self.subTest(text=text), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_arguments(["--match-id", text])

    def test_rejects_removed_missing_powerplay_fallback_flag(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_arguments(["--match-id", "10", "--missing-pp-as-na"])

    def test_rejects_malformed_overs_file_before_connecting_or_replacing_rows(self) -> None:
        with NamedTemporaryFile(mode="w+", encoding="utf-8") as overs_file:
            overs_file.write("matchId,inningsId,scheduledOvers,reducedOvers\n")
            overs_file.write("999,1,20,17.7\n")
            overs_file.flush()
            arguments = parse_arguments([
                "--match-id", "10", "--reduced-overs-file", overs_file.name,
            ])
            with patch("backfill_matchups.replace_rows") as replace:
                with self.assertRaisesRegex(ValueError, "line 2.*17.7"):
                    run_backfill(arguments, Mock())
            replace.assert_not_called()

    def test_rejects_duplicate_overs_rows_before_any_replacement(self) -> None:
        with NamedTemporaryFile(mode="w+", encoding="utf-8") as overs_file:
            overs_file.write("matchId,inningsId,scheduledOvers,reducedOvers\n")
            overs_file.write("10,1,20,17.3\n10,1,20,17.3\n")
            overs_file.flush()
            arguments = parse_arguments([
                "--match-id", "10", "--reduced-overs-file", overs_file.name,
            ])
            with patch("backfill_matchups.replace_rows") as replace:
                with self.assertRaisesRegex(ValueError, "line 3.*10.*1"):
                    run_backfill(arguments, Mock())
            replace.assert_not_called()

    def test_rejects_bad_header_and_overs_that_exceed_schedule(self) -> None:
        cases = (
            ("wrong,inningsId,scheduledOvers,reducedOvers\n", "header on line 1"),
            ("matchId,inningsId,scheduledOvers,reducedOvers\n10,1,17.3,18\n",
             "reducedOvers exceeds scheduledOvers on line 2"),
            ("matchId,inningsId,scheduledOvers,reducedOvers\n10,1,20,\"17.3",
             "invalid CSV on line 2"),
            ("matchId,inningsId,scheduledOvers,reducedOvers\n10,1,20,17.3000001\n",
             "line 2.*17.3000001"),
        )
        for content, warning in cases:
            with self.subTest(content=content):
                with NamedTemporaryFile(mode="w+", encoding="utf-8") as overs_file:
                    overs_file.write(content)
                    overs_file.flush()
                    arguments = parse_arguments([
                        "--match-id", "10", "--reduced-overs-file", overs_file.name,
                    ])
                    with self.assertRaisesRegex(ValueError, warning):
                        run_backfill(arguments, Mock())

    def test_listed_match_with_extra_innings_skips_without_deleting(self) -> None:
        with NamedTemporaryFile(mode="w+", encoding="utf-8") as overs_file:
            overs_file.write("matchId,inningsId,scheduledOvers,reducedOvers\n")
            overs_file.write("10,1,20,20\n10,2,20,17.3\n")
            overs_file.flush()
            arguments = parse_arguments([
                "--match-id", "10", "--reduced-overs-file", overs_file.name,
            ])
            connection = Mock()
            with patch.dict(sys.modules, fake_driver_modules(connection)), \
                    patch("backfill_matchups.fetch_series_type", return_value=""), \
                    patch("backfill_matchups.fetch_match_sources", return_value=phase_sources()), \
                    patch("backfill_matchups.fetch_mariadb_bowling_styles", return_value={}), \
                    patch("backfill_matchups.replace_rows") as replace, \
                    self.assertLogs("backfill_matchups", level="WARNING") as logs:
                result = run_backfill(arguments, Mock(cassandra_contacts=("127.0.0.1",)))
            self.assertFalse(result)
            replace.assert_not_called()
            self.assertIn("extra innings [2]", "\n".join(logs.output))

    def test_complete_two_innings_file_uses_each_scorecard_and_allotted_limit(self) -> None:
        with NamedTemporaryFile(mode="w+", encoding="utf-8") as overs_file:
            overs_file.write("matchId,inningsId,scheduledOvers,reducedOvers\n")
            overs_file.write("10,1,20,20\n10,2,20,17.3\n")
            overs_file.flush()
            arguments = parse_arguments([
                "--match-id", "10", "--reduced-overs-file", overs_file.name,
            ])
            sources = phase_sources(pp_to=6, ball_numbers=(36, 37, 91))
            sources[1].append({
                **sources[1][0], "innings_id": 2,
                "pp_data": {"pp_1": {
                    "pp_overs_from": 0.1, "pp_overs_to": 4, "pp_type": "mandatory",
                }},
            })
            sources[2].extend([
                {**sources[2][0], "innings_id": 2, "ball_nbr": ball}
                for ball in (24, 25, 89)
            ])
            connection = Mock()
            with patch.dict(sys.modules, fake_driver_modules(connection)), \
                    patch("backfill_matchups.fetch_series_type", return_value=""), \
                    patch("backfill_matchups.fetch_match_sources", return_value=sources), \
                    patch("backfill_matchups.fetch_mariadb_bowling_styles", return_value={}), \
                    patch("backfill_matchups.replace_rows") as replace, \
                    self.assertLogs("backfill_matchups", level="DEBUG") as logs:
                result = run_backfill(arguments, Mock(cassandra_contacts=("127.0.0.1",)))
            self.assertTrue(result)
            output = "\n".join(logs.output)
            self.assertIn("matches=1 innings=2", output)
            self.assertIn("overs file match_id=10 innings_id=2 scheduled_balls=120 allotted_balls=105", output)
            self.assertIn("match 10: calculated 6 matchup rows", output)
            rows = replace.call_args.args[2]
            self.assertEqual(
                [(row["innings_id"], row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
                 for row in rows],
                [(1, "POWERPLAY", 1, 36), (1, "MIDDLE_OVERS", 37, 90),
                 (1, "DEATH_OVERS", 91, 120), (2, "POWERPLAY", 1, 24),
                 (2, "MIDDLE_OVERS", 25, 78), (2, "DEATH_OVERS", 79, 105)],
            )

    def test_rejects_an_incomplete_date_selector(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_arguments(["--from-date", "2026-03-27"])

    def test_selects_all_series_matches_without_exclusions(self) -> None:
        cursor = RecordingCursor(rows=[{"id": 11}, {"id": 12}, {"id": 13}])
        connection = RecordingConnection(cursor)
        selected = selected_match_ids(connection, parse_arguments(["--series-id", "9241"]))

        self.assertEqual(selected, [11, 12, 13])
        self.assertIn("seriesid", cursor.executed[0][0])
        self.assertEqual(cursor.executed[0][1], (9241,))

    def test_fetches_league_type_and_rejects_missing_series(self) -> None:
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value = cursor
        cursor.fetchone.return_value = {"series_type": "BBL"}
        self.assertEqual(fetch_series_type(connection, 10), "BBL")
        self.assertIn("JOIN krik_series_archive", cursor.execute.call_args.args[0])
        self.assertEqual(cursor.execute.call_args.args[1], (10,))
        cursor.fetchone.return_value = None
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "linked krik_series_archive row is missing"):
            fetch_series_type(connection, 10)

    def test_selects_matches_in_an_inclusive_date_range(self) -> None:
        cursor = RecordingCursor(rows=[{"id": 11}, {"id": 12}])

        selected = selected_match_ids(
            RecordingConnection(cursor),
            parse_arguments(["--from-date", "2026-03-27", "--to-date", "2026-06-01"]),
        )

        self.assertEqual(selected, [11, 12])
        self.assertIn("startdt >= %s AND startdt <= %s", cursor.executed[0][0])
        self.assertEqual(cursor.executed[0][1], ("2026-03-27", "2026-06-01"))

    def test_fetches_mariadb_bowling_styles_for_eligible_player_ids(self) -> None:
        cursor = RecordingCursor(rows=[{"id": 11543, "StyleDesc": "Right-arm fast-medium"}])

        styles = fetch_mariadb_bowling_styles(RecordingConnection(cursor), [11543, 10276, 11543])

        self.assertEqual(styles, {11543: "Right-arm fast-medium"})
        self.assertIn("JOIN stats_import_player_style", cursor.executed[0][0])
        self.assertIn("WHERE pm.id IN (%s, %s)", cursor.executed[0][0])
        self.assertEqual(cursor.executed[0][1], (10276, 11543))
        self.assertTrue(cursor.closed)

    def test_replaces_rows_transactionally_and_prints_deleted_rows(self) -> None:
        cursor = RecordingCursor(rows=[{"id": 9, "match_id": 10}])
        connection = RecordingConnection(cursor)
        replacement = {column: 1 for column in TARGET_COLUMNS}
        replacement.update({"match_id": 10, "match_format": "ODI", "phase": "POWERPLAY"})
        output = io.StringIO()

        with redirect_stdout(output), self.assertLogs("backfill_matchups", level="DEBUG") as logs:
            replace_rows(connection, 10, [replacement])

        self.assertIn("match 10: existing rows to delete: 1", output.getvalue())
        self.assertIn('"id": 9', output.getvalue())
        self.assertTrue(any("deleting existing matchup rows: 1" in line for line in logs.output))
        self.assertTrue(any("inserting matchup rows: 1" in line for line in logs.output))
        self.assertTrue(any("planned DELETE match_id=10" in line and '"id": 9' in line
                            for line in logs.output))
        self.assertTrue(any("planned INSERT match_id=10" in line and '"match_id": 10' in line
                            for line in logs.output))
        self.assertTrue(all("batting_vs_bowling_matchups" in statement for statement, _ in cursor.executed))
        self.assertTrue(all("batting_vs_bowling_stats" not in statement for statement, _ in cursor.executed))
        self.assertTrue(connection.began)
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertEqual(cursor.inserted, [tuple(replacement[column] for column in TARGET_COLUMNS)])

    def test_rolls_back_failed_replacement(self) -> None:
        cursor = RecordingCursor(fail_inserts=True)
        connection = RecordingConnection(cursor)
        replacement = {column: 1 for column in TARGET_COLUMNS}

        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                replace_rows(connection, 10, [replacement])

        self.assertTrue(connection.began)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.rolled_back)

    def test_rejects_missing_player_metadata_before_returning_rows(self) -> None:
        match_info = {
            "match_id": 10,
            "match_format": "T20",
            "venue": {"id": 55},
            "team1": {"player_details": []},
            "team2": {"player_details": []},
        }
        scorecards = [
            {
                "innings_id": 1,
                "bat_team_id": 1,
                "bowl_team_id": 2,
                "batsmen_data": {"bat_1": {"bat_id": 101}},
                "pp_data": {"pp_1": {"pp_overs_from": 0.1, "pp_overs_to": 6, "pp_type": "mandatory"}},
            }
        ]
        events = [
            {
                "innings_id": 1,
                "ball_nbr": 1,
                "bat_striker_id": 101,
                "bowl_striker_id": 202,
                "legal_runs": 0,
                "total_runs": 0,
                "highlights_id": 0,
                "wkt_code": "NONE",
                "extra_code": "NONE",
            }
        ]

        with self.assertRaisesRegex(MatchCannotBeBackfilled, "missing player metadata") as error:
            calculate_matchup_rows(match_info, scorecards, events)
        self.assertEqual(error.exception.reason, "missing player metadata")
        self.assertIn("ball={'innings_id': 1", str(error.exception))
        self.assertIn("'ball_nbr': 1", str(error.exception))

    def test_missing_bowling_style_inserts_null_type_and_technique(self) -> None:
        match_info = {
            "match_id": 151565,
            "match_format": "T20",
            "venue": {"id": 55},
            "team1": {"player_details": [{"id": 10276, "name": "Test Batter", "batting_style": "RIGHT"}]},
            "team2": {"player_details": [{"id": 11543, "name": "Test Bowler", "bowling_style": None}]},
        }
        scorecards = [{"innings_id": 2, "bat_team_id": 1, "bowl_team_id": 2,
                       "batsmen_data": {"bat_1": {"bat_id": 10276}},
                       "pp_data": {"pp_1": {
                           "pp_overs_from": 0.1, "pp_overs_to": 6, "pp_type": "mandatory",
                       }}}]
        events = [{"innings_id": 2, "time_score": "2026-10-06T16:37:04",
                   "ball_nbr": 85, "bat_striker_id": 10276, "bowl_striker_id": 11543,
                   "legal_runs": 0, "total_runs": 0, "highlights_id": 0,
                   "wkt_code": "NONE", "extra_code": "NONE", "unused_field": "ignore"}]
        events.append({**events[0], "ball_nbr": 100, "time_score": "2026-10-06T16:40:00"})

        with self.assertLogs("backfill_matchups", level="DEBUG") as logs:
            rows = calculate_matchup_rows(match_info, scorecards, events)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["bowling_type"] is None and row["bowling_technique"] is None
                            for row in rows))
        warnings = [line for line in logs.output if "inserting NULL bowling_type" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("bowler 11543 (Test Bowler)", warnings[0])
        cursor = RecordingCursor()
        with redirect_stdout(io.StringIO()):
            replace_rows(RecordingConnection(cursor), 151565, rows)
        self.assertTrue(all(values[TARGET_COLUMNS.index("bowling_type")] is None and
                            values[TARGET_COLUMNS.index("bowling_technique")] is None
                            for values in cursor.inserted))

        self.assertTrue(any("bowler_id=11543" in line and "bowler_name='Test Bowler'" in line
                            for line in logs.output))

        with self.assertLogs("backfill_matchups", level="DEBUG") as fallback_logs:
            rows = calculate_matchup_rows(
                match_info, scorecards, events,
                maria_bowling_styles={11543: "Right Arm fast medium"},
            )

        self.assertEqual(rows[0]["bowling_technique"], "Right-arm fast-medium")
        self.assertEqual(rows[0]["bowling_type"], "PACE")
        self.assertTrue(any("player_id=11543" in line and
                            "source_bowling_style='Right Arm fast medium'" in line
                            for line in fallback_logs.output))

        with self.assertRaisesRegex(MatchCannotBeBackfilled, "unrecognised bowling style"):
            calculate_matchup_rows(
                match_info, scorecards, events,
                maria_bowling_styles={11543: "unknown style"},
            )

    def test_calculates_t20_phase_boundaries(self) -> None:
        match_info = {
            "match_id": 10,
            "match_format": "T20",
            "venue": {"id": 55},
            "team1": {
                "player_details": [
                    {"id": 101, "name": "Test Batter", "batting_style": "RIGHT",
                     "bowling_style": "Right-arm offbreak"}
                ]
            },
            "team2": {
                "player_details": [
                    {"id": 202, "batting_style": "LEFT", "bowling_style": "Right-arm fast"}
                ]
            },
        }
        scorecards = [
            {
                "innings_id": 1,
                "bat_team_id": 1,
                "bowl_team_id": 2,
                "batsmen_data": {"bat_1": {"bat_id": 101}},
                "pp_data": {"pp_1": {"pp_overs_from": 0.1, "pp_overs_to": 6, "pp_type": "mandatory"}},
            }
        ]
        events = [
            {
                "innings_id": 1,
                "ball_nbr": ball_number,
                "bat_striker_id": 101,
                "bowl_striker_id": 202,
                "legal_runs": 0,
                "total_runs": 0,
                "highlights_id": 0,
                "wkt_code": "NONE",
                "extra_code": "NONE",
            }
            for ball_number in (36, 37, 90, 91)
        ]
        events[0]["unused_field"] = "do not log"

        with self.assertLogs("backfill_matchups", level="DEBUG") as logs:
            rows = calculate_matchup_rows(match_info, scorecards, events)

        self.assertTrue(any("playersMap match_id=10 player_id=101" in line
                            and "name='Test Batter'" in line
                            and "batting_style='RIGHT'" in line for line in logs.output))
        self.assertTrue(any("processing match_id=10" in line and "'ball_nbr': 36" in line
                            for line in logs.output))
        self.assertNotIn("unused_field", "\n".join(logs.output))

        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"], row["balls"])
             for row in rows],
            [("POWERPLAY", 1, 36, 1), ("MIDDLE_OVERS", 37, 90, 2),
             ("DEATH_OVERS", 91, 120, 1)],
        )

    def test_uses_allotted_balls_and_scorecard_powerplay_for_listed_innings(self) -> None:
        match_info = {
            "match_id": 10, "match_format": "T20", "venue": {"id": 55},
            "team1": {"player_details": [
                {"id": 101, "batting_style": "RIGHT", "bowling_style": "Right-arm offbreak"}
            ]},
            "team2": {"player_details": [
                {"id": 202, "batting_style": "LEFT", "bowling_style": "Right-arm fast"}
            ]},
        }
        scorecards = [{
            "innings_id": 1, "bat_team_id": 1, "bowl_team_id": 2,
            "batsmen_data": {"bat_1": {"bat_id": 101}},
            "pp_data": {"pp_1": {
                "pp_overs_from": 0.1, "pp_overs_to": 5, "pp_type": "mandatory",
            }},
        }]
        events = [
            {"innings_id": 1, "ball_nbr": ball, "bat_striker_id": 101,
             "bowl_striker_id": 202, "legal_runs": 0, "total_runs": 0,
             "highlights_id": 0, "wkt_code": "NONE", "extra_code": "NONE"}
            for ball in (30, 31, 84, 85, 105)
        ]

        rows = calculate_matchup_rows(
            match_info, scorecards, events, allotted_balls_by_innings={1: 105}
        )

        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"], row["balls"])
             for row in rows],
            [("POWERPLAY", 1, 30, 1), ("MIDDLE_OVERS", 31, 78, 1),
             ("DEATH_OVERS", 79, 105, 3)],
        )

    def test_regular_powerplay_mismatch_skips_t20_and_odi(self) -> None:
        for match_format, pp_to, expected in (("T20", 5, 36), ("ODI", 8, 60)):
            with self.subTest(match_format=match_format):
                with self.assertRaisesRegex(MatchCannotBeBackfilled, f"expected ball {expected}"):
                    calculate_matchup_rows(*phase_sources(match_format, pp_to, (30, 85)))

    def test_regular_bbl_uses_league_phases(self) -> None:
        sources = phase_sources(pp_to=4, ball_numbers=(24, 25, 90, 91))
        behaviors = set()
        rows = calculate_matchup_rows(*sources, series_type="BBL", phase_behaviors=behaviors)
        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
             for row in rows],
            [("POWERPLAY", 1, 24), ("MIDDLE_OVERS", 25, 90), ("DEATH_OVERS", 91, 120)],
        )
        self.assertEqual(behaviors, {"regular-bbl"})

    def test_regular_powerplay_and_ball_limit_are_validated(self) -> None:
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "powerplay maximum 36"):
            calculate_matchup_rows(*phase_sources(pp_to=7, ball_numbers=(1,)))
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "last eligible ball 121"):
            calculate_matchup_rows(*phase_sources(ball_numbers=(121,)))

    def test_rounds_death_up_and_uses_listed_limit_even_when_scheduled_equals_reduced(self) -> None:
        sources = phase_sources(pp_to=4, ball_numbers=(77, 78, 103))

        rows = calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 103})

        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
             for row in rows],
            [("MIDDLE_OVERS", 25, 77), ("DEATH_OVERS", 78, 103)],
        )

    def test_accepts_empty_middle_phase_and_completed_over_notation(self) -> None:
        shortened = phase_sources(pp_to=4, ball_numbers=(24, 25))
        rows = calculate_matchup_rows(*shortened, allotted_balls_by_innings={1: 25})
        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
             for row in rows],
            [("POWERPLAY", 1, 24), ("DEATH_OVERS", 25, 25)],
        )

        normal_odi = phase_sources(match_format="ODI", pp_to=9.6)
        self.assertEqual(calculate_matchup_rows(*normal_odi)[0]["phase_to_ball_nbr"], 60)

    def test_rejects_reduced_limit_shorter_than_powerplay_with_useful_warning(self) -> None:
        sources = phase_sources(pp_to=6, ball_numbers=(1,))

        with self.assertRaisesRegex(
            MatchCannotBeBackfilled,
            r"innings 1: pp_1 ends at ball 36 beyond powerplay maximum 36 or allotted limit 30",
        ):
            calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 30})

    def test_accepts_cassandra_float_noise_in_powerplay_overs(self) -> None:
        sources = phase_sources(pp_to=6, ball_numbers=(1,))
        sources[1][0]["pp_data"]["pp_1"]["pp_overs_from"] = 0.10000000149011612

        rows = calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 105})

        self.assertEqual(
            (rows[0]["phase"], rows[0]["phase_from_ball_nbr"], rows[0]["phase_to_ball_nbr"]),
            ("POWERPLAY", 1, 36),
        )

        sources[1][0]["pp_data"]["pp_1"].update(
            pp_overs_from=0.1, pp_overs_to=5.599999904632568
        )
        rows = calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 105})
        self.assertEqual(rows[0]["phase_to_ball_nbr"], 36)

        sources[1][0]["pp_data"]["pp_1"]["pp_overs_from"] = 0.11
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "invalid over.ball value"):
            calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 105})

    def test_selects_opening_powerplay_by_start_ball_not_map_key(self) -> None:
        sources = phase_sources(pp_to=6, ball_numbers=(1, 37))
        sources[1][0]["pp_data"] = {
            "pp_1": {"pp_overs_from": 6.1, "pp_overs_to": 10, "pp_type": "batting"},
            "pp_2": {"pp_overs_from": 0.1, "pp_overs_to": 6, "pp_type": "mandatory"},
        }
        with self.assertLogs("backfill_matchups", level="DEBUG") as logs:
            rows = calculate_matchup_rows(*sources)
        self.assertEqual([(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
                          for row in rows],
                         [("POWERPLAY", 1, 36), ("MIDDLE_OVERS", 37, 90)])
        self.assertIn("pp_key=pp_2", "\n".join(logs.output))

        sources[1][0]["pp_data"]["pp_3"] = {
            "pp_overs_from": 0.1, "pp_overs_to": 6, "pp_type": "mandatory",
        }
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "multiple powerplay entries start at ball 1"):
            calculate_matchup_rows(*sources)

    def test_ignores_later_power_surge_end_but_rejects_unreadable_start(self) -> None:
        sources = phase_sources(pp_to=4, ball_numbers=(73,))
        sources[1][0]["pp_data"]["pp_2"] = {
            "pp_overs_from": 12.1, "pp_overs_to": "bad", "pp_type": "batting",
        }
        rows = calculate_matchup_rows(*sources, series_type="BBL")
        self.assertEqual((rows[0]["phase"], rows[0]["phase_from_ball_nbr"],
                          rows[0]["phase_to_ball_nbr"]), ("MIDDLE_OVERS", 25, 90))
        sources[1][0]["pp_data"]["pp_2"]["pp_overs_from"] = "bad"
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "pp_2 is invalid"):
            calculate_matchup_rows(*sources, series_type="BBL")

    def test_rejects_allotment_above_format_maximum(self) -> None:
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "allotted limit 121 exceeds T20 maximum 120"):
            calculate_matchup_rows(*phase_sources(), allotted_balls_by_innings={1: 121})

    def test_uses_separate_phase_boundaries_for_each_innings(self) -> None:
        match_info, scorecards, events = phase_sources(pp_to=6, ball_numbers=(31,))
        second_scorecard = {
            **scorecards[0], "innings_id": 2,
            "pp_data": {"pp_1": {
                "pp_overs_from": 0.1, "pp_overs_to": 5, "pp_type": "mandatory",
            }},
        }
        scorecards.append(second_scorecard)
        events.append({**events[0], "innings_id": 2})

        rows = calculate_matchup_rows(
            match_info, scorecards, events, allotted_balls_by_innings={1: 120, 2: 105}
        )

        self.assertEqual(
            [(row["innings_id"], row["phase"], row["phase_from_ball_nbr"],
              row["phase_to_ball_nbr"]) for row in rows],
            [(1, "POWERPLAY", 1, 36), (2, "MIDDLE_OVERS", 31, 78)],
        )

    def test_early_finished_innings_keeps_allotted_phase_boundaries(self) -> None:
        sources = phase_sources(pp_to=5, ball_numbers=(30, 60))

        rows = calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 105})

        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
             for row in rows],
            [("POWERPLAY", 1, 30), ("MIDDLE_OVERS", 31, 78)],
        )

    def test_rejects_missing_extra_or_inconsistent_innings_phase_data(self) -> None:
        sources = phase_sources()
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "missing innings \\[1\\], extra innings \\[2\\]"):
            calculate_matchup_rows(*sources, allotted_balls_by_innings={2: 105})

        sources[1][0]["pp_data"] = {}
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "innings 1.*opening powerplay is missing"):
            calculate_matchup_rows(*sources)

        sources[1][0]["pp_data"] = {"pp_1": "invalid"}
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "innings 1.*pp_1 is invalid"):
            calculate_matchup_rows(*sources)

        sources[1][0]["pp_data"] = {"pp_1": {
            "pp_overs_from": 0.1, "pp_overs_to": 5, "pp_type": "mandatory",
        }}
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "expected ball 36"):
            calculate_matchup_rows(*sources)
        self.assertEqual(
            [row["phase"] for row in calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 30})],
            ["POWERPLAY"],
        )

        sources[1][0]["pp_data"]["pp_1"]["pp_type"] = "fielding"
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "pp_1 is not mandatory"):
            calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 105})

    def test_missing_powerplay_skips_regular_and_reduced_innings(self) -> None:
        for allotted in (None, {1: 105}):
            with self.subTest(allotted=allotted):
                sources = phase_sources(ball_numbers=(85,))
                sources[1][0]["pp_data"] = None
                with self.assertRaisesRegex(MatchCannotBeBackfilled, "opening powerplay is missing"):
                    calculate_matchup_rows(*sources, allotted_balls_by_innings=allotted)

    def test_short_t20_uses_remaining_balls_after_standard_powerplay(self) -> None:
        sources = phase_sources(ball_numbers=(36, 37, 60))
        rows = calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 60})
        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
             for row in rows],
            [("POWERPLAY", 1, 36), ("DEATH_OVERS", 37, 60)],
        )

    def test_shortened_powerplay_takes_precedence_over_death(self) -> None:
        sources = phase_sources(pp_to=5.5, ball_numbers=(35, 36, 42))
        rows = calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 42})
        self.assertEqual(
            [(row["phase"], row["phase_from_ball_nbr"], row["phase_to_ball_nbr"])
             for row in rows],
            [("POWERPLAY", 1, 35), ("DEATH_OVERS", 36, 42)],
        )

    def test_phase_validation_exposes_skip_reason_for_summary(self) -> None:
        unsupported = phase_sources()
        unsupported[0]["match_format"] = "HUN"
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "unsupported match format: 'HUN'") as error:
            calculate_matchup_rows(*unsupported)
        self.assertEqual(error.exception.reason, "unsupported format 'HUN'")

        sources = phase_sources()
        sources[1][0]["pp_data"] = None
        with self.assertRaises(MatchCannotBeBackfilled) as missing:
            calculate_matchup_rows(*sources)
        self.assertEqual(missing.exception.reason, "missing-powerplay")

        sources[1][0]["pp_data"] = {"pp_1": "invalid"}
        with self.assertRaises(MatchCannotBeBackfilled) as invalid:
            calculate_matchup_rows(*sources)
        self.assertEqual(invalid.exception.reason, "invalid-powerplay")

        sources[1][0]["pp_data"] = phase_sources()[1][0]["pp_data"]
        sources[2][0]["ball_nbr"] = 121
        with self.assertRaises(MatchCannotBeBackfilled) as beyond_limit:
            calculate_matchup_rows(*sources)
        self.assertEqual(beyond_limit.exception.reason, "ball-beyond-limit")

        with self.assertRaises(MatchCannotBeBackfilled) as csv_mismatch:
            calculate_matchup_rows(*sources, allotted_balls_by_innings={2: 105})
        self.assertEqual(csv_mismatch.exception.reason, "csv-scorecard-mismatch")

    def test_invalid_commentary_event_skips_match(self) -> None:
        sources = phase_sources()
        sources[2][0]["ball_nbr"] = None
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "invalid ball_nbr") as error:
            calculate_matchup_rows(*sources)
        self.assertEqual(error.exception.reason, "invalid-event")

        sources = phase_sources()
        del sources[2][0]["innings_id"]
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "invalid innings_id") as error:
            calculate_matchup_rows(*sources)
        self.assertEqual(error.exception.reason, "invalid-event")

        for missing_field in ("bat_striker_id", "bowl_striker_id"):
            with self.subTest(missing_field=missing_field):
                sources = phase_sources()
                del sources[2][0][missing_field]
                with self.assertRaisesRegex(MatchCannotBeBackfilled, missing_field) as error:
                    calculate_matchup_rows(*sources)
                self.assertEqual(error.exception.reason, "invalid-event")

    def test_rejects_event_beyond_allotted_innings(self) -> None:
        sources = phase_sources(pp_to=4, ball_numbers=(31,))
        with self.assertRaisesRegex(MatchCannotBeBackfilled, "last eligible ball 31 exceeds innings limit 30") as error:
            calculate_matchup_rows(*sources, allotted_balls_by_innings={1: 30})
        self.assertEqual(error.exception.reason, "ball-beyond-limit")

    def test_calculates_odI_phase_rows_for_an_explicit_match(self) -> None:
        match_info = {
            "match_id": 10,
            "match_format": "ODI",
            "venue": {"id": 55},
            "team1": {
                "id": 1,
                "player_details": [
                    {
                        "id": 101,
                        "substitute": False,
                        "batting_style": "RIGHT",
                        "bowling_style": "Right-arm offbreak",
                    }
                ],
            },
            "team2": {
                "id": 2,
                "player_details": [
                    {
                        "id": 202,
                        "substitute": False,
                        "batting_style": "LEFT",
                        "bowling_style": "Right Arm fast",
                    }
                ],
            },
        }
        scorecards = [
            {
                "innings_id": 1,
                "ball_nbr": 300,
                "time_score": "2026-01-01T12:00:00+00:00",
                "bat_team_id": 1,
                "bowl_team_id": 2,
                "batsmen_data": {"bat_1": {"bat_id": 101}},
                "pp_data": {"pp_1": {"pp_overs_from": 0.1, "pp_overs_to": 10, "pp_type": "mandatory"}},
            }
        ]
        events = [
            {
                "innings_id": 1,
                "ball_nbr": 1,
                "time_score": "2026-01-01T10:00:00+00:00",
                "bat_striker_id": 101,
                "bowl_striker_id": 202,
                "legal_runs": 4,
                "total_runs": 4,
                "highlights_id": 2,
                "wkt_code": "NONE",
                "extra_code": "NONE",
            },
            {
                "innings_id": 1,
                "ball_nbr": 61,
                "time_score": "2026-01-01T10:20:00+00:00",
                "bat_striker_id": 101,
                "bowl_striker_id": 202,
                "legal_runs": 1,
                "total_runs": 1,
                "highlights_id": 0,
                "wkt_code": "NONE",
                "extra_code": "NONE",
            },
        ]

        rows = calculate_matchup_rows(
            match_info, scorecards, events,
            maria_bowling_styles={202: "Left-arm orthodox"},
        )

        self.assertEqual(
            rows,
            [
                {
                    "match_id": 10,
                    "innings_id": 1,
                    "match_format": "ODI",
                    "batting_team_id": 1,
                    "batsman_id": 101,
                    "bowler_id": 202,
                    "bowling_team_id": 2,
                    "phase": "POWERPLAY",
                    "phase_from_ball_nbr": 1,
                    "phase_to_ball_nbr": 60,
                    "runs": 4,
                    "balls": 1,
                    "fours": 1,
                    "sixes": 0,
                    "dots": 0,
                    "dismissal_type": None,
                    "venue_id": 55,
                    "bowling_type": "PACE",
                    "batting_hand": "RIGHT",
                    "batting_order": 1,
                    "bowling_technique": "Right-arm fast",
                },
                {
                    "match_id": 10,
                    "innings_id": 1,
                    "match_format": "ODI",
                    "batting_team_id": 1,
                    "batsman_id": 101,
                    "bowler_id": 202,
                    "bowling_team_id": 2,
                    "phase": "MIDDLE_OVERS",
                    "phase_from_ball_nbr": 61,
                    "phase_to_ball_nbr": 240,
                    "runs": 1,
                    "balls": 1,
                    "fours": 0,
                    "sixes": 0,
                    "dots": 0,
                    "dismissal_type": None,
                    "venue_id": 55,
                    "bowling_type": "PACE",
                    "batting_hand": "RIGHT",
                    "batting_order": 1,
                    "bowling_technique": "Right-arm fast",
                },
            ],
        )

    def test_counts_commentary_run_out_without_recording_a_bowler_dismissal(self) -> None:
        match_info = {
            "match_id": 10,
            "match_format": "ODI",
            "venue": {"id": 55},
            "team1": {
                "id": 1,
                "player_details": [
                    {"id": 101, "batting_style": "RIGHT", "bowling_style": "Right-arm offbreak"}
                ],
            },
            "team2": {
                "id": 2,
                "player_details": [
                    {"id": 202, "batting_style": "LEFT", "bowling_style": "Right-arm fast"}
                ],
            },
        }
        scorecards = [
            {
                "innings_id": 1,
                "bat_team_id": 1,
                "bowl_team_id": 2,
                "batsmen_data": {"bat_1": {"bat_id": 101}},
                "pp_data": {"pp_1": {"pp_overs_from": 0.1, "pp_overs_to": 10, "pp_type": "mandatory"}},
            }
        ]
        commentary_events = [
            {
                "innings_id": 1,
                "ball_nbr": 1,
                "time_score": "2026-01-01T10:00:00+00:00",
                "bat_striker_id": 101,
                "bowl_striker_id": 202,
                "legal_runs": 2,
                "total_runs": 2,
                "highlights_id": 0,
                "wkt_code": "run out",
                "extra_code": "NONE",
            }
        ]

        for code, expected_dismissal in (("run out", None), ("b", "b")):
            with self.subTest(wkt_code=code):
                commentary_events[0]["wkt_code"] = code
                rows = calculate_matchup_rows(match_info, scorecards, commentary_events)

                self.assertEqual(rows[0]["runs"], 2)
                self.assertEqual(rows[0]["balls"], 1)
                self.assertEqual(rows[0]["dismissal_type"], expected_dismissal)

    def test_counts_eligible_extras_boundaries_dots_and_bowler_dismissals(self) -> None:
        match_info = {
            "match_id": 10,
            "match_format": "T20",
            "venue": {"id": 55},
            "team1": {
                "player_details": [
                    {"id": 101, "batting_style": "RIGHT", "bowling_style": "Right-arm offbreak"}
                ]
            },
            "team2": {
                "player_details": [
                    {"id": 202, "batting_style": "LEFT", "bowling_style": "Right-arm fast"}
                ]
            },
        }
        scorecards = [
            {
                "innings_id": 1,
                "bat_team_id": 1,
                "bowl_team_id": 2,
                "batsmen_data": {"bat_1": {"bat_id": 101}},
                "pp_data": {"pp_1": {"pp_overs_from": 0.1, "pp_overs_to": 6, "pp_type": "mandatory"}},
            }
        ]
        events = [
            {"innings_id": 1, "ball_nbr": 0, "bat_striker_id": 101, "bowl_striker_id": 202,
             "legal_runs": 6, "total_runs": 6, "highlights_id": 4, "wkt_code": "NONE", "extra_code": "NONE"},
            {"innings_id": 1, "ball_nbr": 1, "bat_striker_id": 101, "bowl_striker_id": 202,
             "legal_runs": 0, "total_runs": 1, "highlights_id": 0, "wkt_code": "NONE", "extra_code": "Wd"},
            {"innings_id": 1, "ball_nbr": 1, "bat_striker_id": 101, "bowl_striker_id": 202,
             "legal_runs": 1, "total_runs": 2, "highlights_id": 4, "wkt_code": "NONE", "extra_code": "N"},
            {"innings_id": 1, "ball_nbr": 2, "bat_striker_id": 101, "bowl_striker_id": 202,
             "legal_runs": 0, "total_runs": 1, "highlights_id": 0, "wkt_code": "NONE", "extra_code": "L"},
            {"innings_id": 1, "ball_nbr": 3, "bat_striker_id": 101, "bowl_striker_id": 202,
             "legal_runs": 0, "total_runs": 0, "highlights_id": 0, "wkt_code": "bowled", "extra_code": "NONE"},
        ]

        row = calculate_matchup_rows(match_info, scorecards, events)[0]

        self.assertEqual(
            {key: row[key] for key in ("runs", "balls", "fours", "sixes", "dots", "dismissal_type")},
            {"runs": 1, "balls": 3, "fours": 0, "sixes": 1, "dots": 1, "dismissal_type": "bowled"},
        )


if __name__ == "__main__":
    unittest.main()

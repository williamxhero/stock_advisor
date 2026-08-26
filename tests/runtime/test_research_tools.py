import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from ai_trading_companion.research_tools import ResearchToolError, ResearchTools, _resolve_edge_profile


class EdgeProfileResolutionTests(TestCase):
    def test_resolves_second_ui_profile_to_edge_profile_one_directory(self):
        with TemporaryDirectory() as temporary:
            user_data = Path(temporary)
            (user_data / "Profile 1").mkdir()
            (user_data / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "用户配置 2"}}}}), encoding="utf-8")
            self.assertEqual("Profile 1", _resolve_edge_profile(user_data, "Profile 2"))

    def test_resolves_named_profile_from_edge_local_state(self):
        with TemporaryDirectory() as temporary:
            user_data = Path(temporary)
            (user_data / "Profile 1").mkdir()
            (user_data / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "Research"}}}}), encoding="utf-8")
            self.assertEqual("Profile 1", _resolve_edge_profile(user_data, "Research"))

    def test_reports_missing_profile_without_guessing(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ResearchToolError):
                _resolve_edge_profile(Path(temporary), "Profile 2")

    def test_bootstrap_copies_profile_two_into_the_dedicated_profile_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "edge"
            profile = source / "Profile 1"
            (profile / "Network").mkdir(parents=True)
            (profile / "Preferences").write_text("{}", encoding="utf-8")
            (profile / "Secure Preferences").write_text("{}", encoding="utf-8")
            cookies = profile / "Network" / "Cookies"
            connection = sqlite3.connect(cookies)
            try:
                connection.execute("CREATE TABLE cookies (name TEXT)")
                connection.commit()
            finally:
                connection.close()
            (profile / "Login Data").write_bytes(b"must not copy")
            (source / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "用户配置 2"}}}}), encoding="utf-8")
            home = root / "home"
            tools = ResearchTools(home, {"playwright": {"edge_profile": "Profile 2", "profile_directory": "browser-profile"}})
            with patch("ai_trading_companion.research_tools._edge_running", return_value=False):
                result = tools.bootstrap_browser_profile(source)
            target = home / "browser-profile"
            self.assertEqual("Profile 1", result["source_profile"])
            self.assertTrue((target / "Profile 1" / "Preferences").is_file())
            self.assertTrue((target / "Profile 1" / "Network" / "Cookies").is_file())
            self.assertFalse((target / "Profile 1" / "Login Data").exists())

    def test_bootstrap_keeps_public_research_available_when_running_edge_locks_cookies(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "edge"
            profile = source / "Profile 1" / "Network"
            profile.mkdir(parents=True)
            (source / "Local State").write_text(
                json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "用户配置 2"}}}}),
                encoding="utf-8",
            )
            (source / "Profile 1" / "Preferences").write_text("{}", encoding="utf-8")
            (profile / "Cookies").write_bytes(b"locked database placeholder")
            home = root / "home"
            tools = ResearchTools(home, {"playwright": {"edge_profile": "Profile 2", "profile_directory": "browser-profile"}})
            with patch("ai_trading_companion.research_tools._edge_running", return_value=True), patch(
                "ai_trading_companion.research_tools._backup_sqlite_snapshot", side_effect=ResearchToolError("locked")
            ):
                result = tools.bootstrap_browser_profile(source)
            self.assertFalse(result["cookies_captured"])
            self.assertEqual("unavailable_while_edge_running", result["cookies_status"])
            self.assertTrue((home / "browser-profile" / "Profile 1" / "Preferences").is_file())

    def test_bootstrap_uses_online_consistent_snapshot_when_edge_is_running(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "edge"
            profile = source / "Profile 1" / "Network"
            profile.mkdir(parents=True)
            (source / "Local State").write_text(
                json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "用户配置 2"}}}}),
                encoding="utf-8",
            )
            (source / "Profile 1" / "Preferences").write_text("{}", encoding="utf-8")
            (source / "Profile 1" / "Secure Preferences").write_text("{}", encoding="utf-8")
            cookies = profile / "Cookies"
            connection = sqlite3.connect(cookies)
            try:
                connection.execute("CREATE TABLE cookies (name TEXT)")
                connection.execute("INSERT INTO cookies VALUES ('session')")
                connection.commit()
            finally:
                connection.close()
            home = root / "home"
            tools = ResearchTools(home, {"playwright": {"edge_profile": "Profile 2", "profile_directory": "browser-profile"}})
            with patch("ai_trading_companion.research_tools._edge_running", return_value=True):
                result = tools.bootstrap_browser_profile(source)
            target = home / "browser-profile"
            self.assertEqual("online_read_only", result["snapshot_mode"])
            self.assertTrue(result["validated"])
            connection = sqlite3.connect(target / "Profile 1" / "Network" / "Cookies")
            try:
                self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual(("session",), connection.execute("SELECT name FROM cookies").fetchone())
            finally:
                connection.close()

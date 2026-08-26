"""Read-only research tools exposed to the configured Provider.

The model chooses when to call them.  This module owns the non-negotiable
permissions: no uploads, no account actions, no executable downloads, and no
browser access outside the dedicated copy of the configured Edge profile.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .secret_guard import assert_safe


_ALLOWED_SUFFIXES = {".pdf", ".txt", ".html", ".htm", ".json", ".csv", ".tsv", ".xls", ".xlsx", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".webp"}
_ALLOWED_CONTENT_TYPES = ("application/pdf", "text/", "application/json", "application/xml", "text/csv", "application/vnd.", "image/")


class ResearchToolError(RuntimeError):
    pass


class ResearchTools:
    def __init__(self, home: Path, research: dict[str, Any]) -> None:
        self.home = home
        self.research = research
        self.quarantine = home / "runtime" / "downloads" / "quarantine"
        self.evidence = home / "evidence"

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": "search_searxng", "description": "Search current public news and web information through the configured SearXNG instance.", "parameters": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {"query": {"type": "string"}, "categories": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "browse_page", "description": "Read a specific public or already-authenticated page in the dedicated, headless Edge research profile. Read-only: do not submit forms or change any external state.", "parameters": {"type": "object", "additionalProperties": False, "required": ["url"], "properties": {"url": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "download_document", "description": "Download an allowed research document for deterministic local parsing and evidence retention. Executables, archives, scripts and uploads are prohibited.", "parameters": {"type": "object", "additionalProperties": False, "required": ["url"], "properties": {"url": {"type": "string"}}}}},
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_searxng":
            return self.search_searxng(str(arguments.get("query") or ""), str(arguments.get("categories") or "news"))
        if name == "browse_page":
            return self.browse_page(str(arguments.get("url") or ""))
        if name == "download_document":
            return self.download_document(str(arguments.get("url") or ""))
        raise ResearchToolError(f"unsupported research tool: {name}")

    def search_searxng(self, query: str, categories: str = "news") -> dict[str, Any]:
        if not query.strip():
            raise ResearchToolError("search query is empty")
        assert_safe(query, boundary="research search query")
        config = self.research.get("searxng") if isinstance(self.research.get("searxng"), dict) else {}
        base_url = str(config.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ResearchToolError("SearXNG is not configured")
        timeout = max(1, int(config.get("timeout_seconds") or 20))
        url = f"{base_url}/search?{urlencode({'q': query, 'categories': categories, 'format': 'json'})}"
        payload = self._fetch_json(url, timeout)
        rows = payload.get("results") if isinstance(payload.get("results"), list) else []
        results: list[dict[str, Any]] = []
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            item = {"title": str(row.get("title") or ""), "url": str(row.get("url") or ""), "snippet": str(row.get("content") or ""), "published_at": row.get("publishedDate")}
            if item["url"]:
                results.append(item)
        return {"backend": "searxng", "query": query, "results": results, "acquired_at": _now()}

    def bootstrap_browser_profile(self, source_user_data: Path | None = None) -> dict[str, Any]:
        source = source_user_data or Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
        requested_profile = str((self.research.get("playwright") or {}).get("edge_profile") or "Profile 2")
        profile_name = _resolve_edge_profile(source, requested_profile)
        profile = source / profile_name
        if not profile.is_dir():
            raise ResearchToolError(f"Edge profile not found: {profile_name}")
        target = self.home / str((self.research.get("playwright") or {}).get("profile_directory") or "browser-profile")
        if target.exists() and any(target.iterdir()):
            raise ResearchToolError("dedicated browser profile already exists; it is never automatically overwritten")
        edge_was_running = _edge_running()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        committed = False
        cookies_captured = False
        cookies_status = "not_present"
        try:
            self._copy_json_snapshot(source / "Local State", staging / "Local State", required=True)
            self._copy_json_snapshot(profile / "Preferences", staging / profile_name / "Preferences", required=True)
            self._copy_json_snapshot(profile / "Secure Preferences", staging / profile_name / "Secure Preferences", required=False)
            cookies = profile / "Network" / "Cookies"
            if cookies.is_file():
                try:
                    _backup_sqlite_snapshot(cookies, staging / profile_name / "Network" / "Cookies")
                    cookies_captured = True
                    cookies_status = "captured"
                except ResearchToolError:
                    if not edge_was_running:
                        raise
                    cookies_status = "unavailable_while_edge_running"
            (staging / "First Run").write_text("", encoding="utf-8")
            self._validate_browser_snapshot(staging, profile_name)
            if target.exists():
                target.rmdir()
            staging.replace(target)
            committed = True
        finally:
            if not committed and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return {
            "profile_directory": str(target),
            "source_profile": profile_name,
            "requested_profile": requested_profile,
            "copied": True,
            "snapshot_mode": "online_read_only",
            "edge_was_running": edge_was_running,
            "cookies_captured": cookies_captured,
            "cookies_status": cookies_status,
            "validated": True,
        }

    @staticmethod
    def _copy_json_snapshot(source: Path, destination: Path, *, required: bool) -> None:
        if not source.is_file():
            if required:
                raise ResearchToolError(f"required Edge profile file not found: {source.name}")
            return
        data = _read_stable_file(source, json_file=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    @staticmethod
    def _validate_browser_snapshot(profile: Path, profile_name: str) -> None:
        for relative in (Path("Local State"), Path(profile_name) / "Preferences"):
            try:
                json.loads((profile / relative).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ResearchToolError(f"dedicated browser profile validation failed: {relative}") from exc
        cookies = profile / profile_name / "Network" / "Cookies"
        if cookies.exists():
            _validate_sqlite(cookies)

    def browse_page(self, url: str) -> dict[str, Any]:
        self._validate_url(url)
        profile = self.home / str((self.research.get("playwright") or {}).get("profile_directory") or "browser-profile")
        if not profile.exists():
            raise ResearchToolError("dedicated browser profile is not initialized")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ResearchToolError("Playwright is not installed in the companion runtime") from exc
        try:
            with sync_playwright() as playwright:
                profile_name = _resolve_edge_profile(profile, str((self.research.get("playwright") or {}).get("edge_profile") or "Profile 2"))
                context = playwright.chromium.launch_persistent_context(
                    str(profile), channel="msedge", headless=True, accept_downloads=True,
                    args=[f"--profile-directory={profile_name}"],
                )
                context.route("**/*", lambda route: route.continue_() if route.request.method in {"GET", "HEAD"} else route.abort())
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                title = page.title()
                text = page.locator("body").inner_text(timeout=15_000)[:60_000]
                current_url = page.url
                context.close()
        except Exception as exc:
            raise ResearchToolError(f"browser read failed: {type(exc).__name__}") from exc
        assert_safe(text, boundary="browser evidence")
        digest = self._persist_evidence(text.encode("utf-8"), suffix=".txt")
        return {"backend": "playwright", "url": current_url, "title": title, "text": text, "content_sha256": digest, "acquired_at": _now()}

    def download_document(self, url: str) -> dict[str, Any]:
        self._validate_url(url)
        limit = max(1, int((self.research.get("playwright") or {}).get("download_limit_mb") or 50)) * 1024 * 1024
        self.quarantine.mkdir(parents=True, exist_ok=True)
        request = Request(url, headers={"User-Agent": "AITradingCompanion/1.0 research"}, method="GET")
        try:
            with urlopen(request, timeout=45) as response:
                content_type = str(response.headers.get_content_type() or "")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > limit:
                    raise ResearchToolError("download exceeds configured size limit")
                data = response.read(limit + 1)
        except ResearchToolError:
            raise
        except Exception as exc:
            raise ResearchToolError(f"download failed: {type(exc).__name__}") from exc
        if len(data) > limit:
            raise ResearchToolError("download exceeds configured size limit")
        suffix = Path(urlparse(url).path).suffix.lower() or mimetypes.guess_extension(content_type) or ".bin"
        if suffix not in _ALLOWED_SUFFIXES or not content_type.startswith(_ALLOWED_CONTENT_TYPES):
            raise ResearchToolError("download type is not allowed for research")
        digest = hashlib.sha256(data).hexdigest()
        quarantine = self.quarantine / f"{digest}{suffix}"
        if not quarantine.exists():
            quarantine.write_bytes(data)
        evidence_path = self.evidence / digest / f"source{suffix}"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        if not evidence_path.exists():
            shutil.copy2(quarantine, evidence_path)
        return {"backend": "download", "url": url, "content_sha256": digest, "content_type": content_type, "bytes": len(data), "acquired_at": _now()}

    @staticmethod
    def _validate_url(url: str) -> None:
        assert_safe(url, boundary="research URL")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ResearchToolError("only http(s) research URLs are allowed")

    @staticmethod
    def _fetch_json(url: str, timeout: int) -> dict[str, Any]:
        try:
            with urlopen(Request(url, headers={"Accept": "application/json", "User-Agent": "AITradingCompanion/1.0 research"}), timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ResearchToolError(f"SearXNG request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise ResearchToolError("SearXNG returned an invalid response")
        return payload

    def _persist_evidence(self, data: bytes, *, suffix: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self.evidence / digest / f"source{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return digest


def _edge_running() -> bool:
    try:
        result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"], capture_output=True, text=True, check=False)
        return result.returncode == 0 and "msedge.exe" in result.stdout.lower()
    except OSError:
        return False


def _resolve_edge_profile(user_data: Path, requested: str) -> str:
    """Resolve a UI profile label such as 'Profile 2' to Edge's directory name."""
    direct = user_data / requested
    if direct.is_dir():
        return requested
    if requested.startswith("Profile "):
        try:
            ordinal = int(requested.removeprefix("Profile "))
        except ValueError:
            ordinal = 0
        candidate = "Profile" if ordinal == 1 else f"Profile {ordinal - 1}" if ordinal > 1 else ""
        if candidate and (user_data / candidate).is_dir():
            return candidate
    local_state = user_data / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        info_cache = ((data.get("profile") or {}).get("info_cache") or {})
        for directory, details in info_cache.items():
            if directory == requested or str((details or {}).get("name") or "") == requested:
                if (user_data / directory).is_dir():
                    return directory
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    raise ResearchToolError(f"Edge profile not found: {requested}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_stable_file(source: Path, *, json_file: bool) -> bytes:
    last_error: Exception | None = None
    for _ in range(5):
        try:
            first = source.read_bytes()
            second = source.read_bytes()
            if first != second:
                time.sleep(0.05)
                continue
            if json_file:
                json.loads(first.decode("utf-8"))
            return first
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise ResearchToolError(f"could not capture stable Edge profile file: {source.name}") from last_error


def _backup_sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for _ in range(5):
        source_connection: sqlite3.Connection | None = None
        destination_connection: sqlite3.Connection | None = None
        attempt_error: Exception | None = None
        try:
            source_uri = source.resolve().as_uri() + "?mode=ro"
            source_connection = sqlite3.connect(source_uri, uri=True, timeout=5)
            destination_connection = sqlite3.connect(destination, timeout=5)
            source_connection.backup(destination_connection, pages=256, sleep=0.05)
            destination_connection.commit()
        except (OSError, sqlite3.Error) as exc:
            attempt_error = exc
            last_error = exc
        finally:
            if destination_connection is not None:
                destination_connection.close()
            if source_connection is not None:
                source_connection.close()
        if attempt_error is None:
            try:
                _validate_sqlite(destination)
                return
            except ResearchToolError as exc:
                attempt_error = exc
                last_error = exc
        destination.unlink(missing_ok=True)
        time.sleep(0.1)
    # Edge normally permits SQLite's online backup, but some Windows builds hold
    # Cookies with an exclusive handle.  A byte snapshot is only accepted when
    # the source is stable, has no active WAL/journal content, and validates.
    try:
        _copy_stable_sqlite_file(source, destination)
        return
    except ResearchToolError as exc:
        raise ResearchToolError(f"could not capture Edge Cookies database: {source.name}") from (last_error or exc)


def _copy_stable_sqlite_file(source: Path, destination: Path) -> None:
    related = [
        source.with_name(f"{source.name}-journal"),
        source.with_name(f"{source.name}-wal"),
        source.with_name(f"{source.name}-shm"),
    ]
    for _ in range(5):
        try:
            if any(item.is_file() and item.stat().st_size > 0 for item in related):
                time.sleep(0.1)
                continue
            data = _read_stable_file(source, json_file=False)
            if any(item.is_file() and item.stat().st_size > 0 for item in related):
                time.sleep(0.1)
                continue
            destination.write_bytes(data)
            _validate_sqlite(destination)
            return
        except (OSError, ResearchToolError):
            destination.unlink(missing_ok=True)
            time.sleep(0.1)
    raise ResearchToolError(f"could not capture a stable SQLite snapshot: {source.name}")


def _validate_sqlite(database: Path) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database, timeout=5)
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ResearchToolError(f"SQLite integrity check failed: {database.name}")
    except sqlite3.Error as exc:
        raise ResearchToolError(f"SQLite validation failed: {database.name}") from exc
    finally:
        if connection is not None:
            connection.close()

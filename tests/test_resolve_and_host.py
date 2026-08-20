"""Regression tests for the residential yt-dlp retry ladder."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("AUDIO_HOST_USER", "audio-user")
os.environ.setdefault("AUDIO_HOST", "audio-host")
os.environ.setdefault("AUDIO_HOST_DIR", "/srv/audio")
os.environ.setdefault("AUDIO_PUBLIC_PREFIX", "https://audio.example.test/audio")

_MODULE_PATH = Path(__file__).parents[1] / "runpod" / "resolve_and_host.py"
_SPEC = importlib.util.spec_from_file_location("resolve_and_host_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
resolver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(resolver)


def test_adaptive_failure_retries_progressive_format_18(tmp_path: Path) -> None:
    """A media-CDN 403 on best audio must trigger a separate format-18 attempt."""
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> None:
        commands.append(command)
        if len(commands) == 1:
            raise subprocess.CalledProcessError(1, command)
        (tmp_path / "audio.mp3").write_bytes(b"progressive audio")

    with (
        patch.object(resolver, "_ytdlp_base_youtube_args", return_value=[]),
        patch.object(resolver.subprocess, "run", side_effect=fake_run),
        patch.dict(os.environ, {"YTDLP_COOKIES_FROM_BROWSER": ""}),
    ):
        result = resolver.download_with_ytdlp("https://youtube.test/watch?v=failing", tmp_path)

    assert result == tmp_path / "audio.mp3"
    assert len(commands) == 2
    assert "-f" not in commands[0]
    assert commands[1][commands[1].index("-f") + 1] == "18"


def test_cookie_attempts_follow_no_cookie_progressive_fallback(tmp_path: Path) -> None:
    """Configured browser cookies are the final ladder, not a prerequisite."""
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> None:
        commands.append(command)
        if len(commands) < 4:
            raise subprocess.CalledProcessError(1, command)
        (tmp_path / "audio.mp3").write_bytes(b"cookie progressive audio")

    with (
        patch.object(resolver, "_ytdlp_base_youtube_args", return_value=[]),
        patch.object(resolver.subprocess, "run", side_effect=fake_run),
        patch.dict(os.environ, {"YTDLP_COOKIES_FROM_BROWSER": "firefox"}),
    ):
        result = resolver.download_with_ytdlp("https://youtube.test/watch?v=stubborn", tmp_path)

    assert result == tmp_path / "audio.mp3"
    assert len(commands) == 4
    assert "--cookies-from-browser" not in commands[0]
    assert commands[1][commands[1].index("-f") + 1] == "18"
    assert commands[2][commands[2].index("--cookies-from-browser") + 1] == "firefox"
    assert commands[3][commands[3].index("-f") + 1] == "18"
    assert commands[3][commands[3].index("--cookies-from-browser") + 1] == "firefox"

"""Regression test for container-safe ffmpeg initialization."""

from unittest.mock import MagicMock, patch

from transcribe_anything.api import _configure_ffmpeg
from transcribe_anything.audio import _ffmpeg_executable
from transcribe_anything.cli_init_insane import main as init_insane
from transcribe_anything.insanely_fast_whisper import _configure_insane_ffmpeg


def test_configure_ffmpeg_prefers_existing_system_binary() -> None:
    """RunPod images must not download static binaries when apt ffmpeg exists."""
    with patch("transcribe_anything.api.static_ffmpeg.add_paths") as add_paths:
        _configure_ffmpeg()

    add_paths.assert_called_once_with(weak=True)


def test_insane_backend_prefers_existing_system_binary() -> None:
    """The RunPod insane backend must not trigger a second binary download."""
    with patch("transcribe_anything.insanely_fast_whisper.static_ffmpeg.add_paths") as add_paths:
        _configure_insane_ffmpeg()

    add_paths.assert_called_once_with(weak=True)


def test_ffmpeg_executable_prefers_system_binary() -> None:
    """Audio conversion must bypass the downloading console wrapper in containers."""
    with patch("transcribe_anything.audio.shutil.which", side_effect=["/usr/bin/ffmpeg"]):
        assert _ffmpeg_executable() == "/usr/bin/ffmpeg"


def test_ffmpeg_executable_falls_back_to_static_wrapper() -> None:
    """Platforms without system ffmpeg retain the packaged fallback."""
    with patch("transcribe_anything.audio.shutil.which", side_effect=[None, "/venv/bin/static_ffmpeg"]):
        assert _ffmpeg_executable() == "/venv/bin/static_ffmpeg"


def test_insane_image_initializer_uses_system_resolver() -> None:
    """Image prewarm must not call the downloading static_ffmpeg console script."""
    env = MagicMock()
    with (
        patch("transcribe_anything.cli_init_insane.get_environment", return_value=env),
        patch("transcribe_anything.cli_init_insane._ffmpeg_executable", return_value="/usr/bin/ffmpeg"),
        patch("transcribe_anything.cli_init_insane.subprocess.run") as run,
    ):
        assert init_insane() == 0

    run.assert_called_once_with(["/usr/bin/ffmpeg", "-version"], check=True)

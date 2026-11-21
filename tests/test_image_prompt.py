"""Tests for the Gemini image prompt template (SA-49 AC3, AC4 and AC11)."""

from pathlib import Path

import pytest

from humblebrag.image_prompt import (
    DEFAULT_TEMPLATE_PATH,
    REPLY_INSTRUCTION,
    ImagePromptError,
    build_image_prompt,
)

SCENE = "Sir Humblebrag III stands on a rainy platform, checking a huge pocket watch."


def test_the_prompt_holds_the_scene_and_the_reply_line() -> None:
    """The scene is filled in and the reply instruction closes the message."""
    prompt = build_image_prompt(SCENE)
    assert SCENE in prompt
    assert "{scene}" not in prompt
    assert prompt.endswith(f"\n\n{REPLY_INSTRUCTION}")


@pytest.mark.parametrize(
    "needle",
    [
        "Using the attached picture of Sir Humblebrag III",
        "top hat",
        "monocle",
        "moustache",
        "plum velvet coat",
        "classical oil portrait painting",
        "16:9 landscape",
        "No text",
        "logos",
        "watermarks",
        "real people",
    ],
)
def test_the_template_covers_the_required_elements(needle: str) -> None:
    """SA-49 AC3: the fixed text names the character, style, format and bans."""
    assert needle.casefold() in build_image_prompt(SCENE).casefold()


def test_the_template_does_not_name_the_portrait_file() -> None:
    """SA-49 AC4: the portrait file is never referenced."""
    assert "jpeg" not in DEFAULT_TEMPLATE_PATH.read_text().casefold()


def test_a_scene_with_braces_is_inserted_literally() -> None:
    """The scene is data: braces in it are not treated as format fields."""
    assert "{x} {scene}" in build_image_prompt("{x} {scene}")


def test_a_missing_template_raises_a_typed_error(tmp_path: Path) -> None:
    """A missing template is an ImagePromptError, not a bare OSError."""
    with pytest.raises(ImagePromptError):
        build_image_prompt(SCENE, tmp_path / "missing.md")


@pytest.mark.parametrize("text", ["no placeholder", "{scene} and {scene}"])
def test_a_template_without_exactly_one_placeholder_is_rejected(
    tmp_path: Path, text: str
) -> None:
    """The placeholder must appear exactly once."""
    path = tmp_path / "t.md"
    path.write_text(text)
    with pytest.raises(ImagePromptError):
        build_image_prompt(SCENE, path)

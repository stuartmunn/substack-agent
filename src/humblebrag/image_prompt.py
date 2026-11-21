"""Builds the Gemini image prompt Stuart pastes by hand after Approve.

The agent never generates images: this only fills a fixed template
(``prompts/image_prompt.md``, relative to the working directory) with the
post's one-sentence scene.
"""

from pathlib import Path

DEFAULT_TEMPLATE_PATH = Path("prompts/image_prompt.md")
ENCODING = "utf-8"
SCENE_PLACEHOLDER = "{scene}"
REPLY_INSTRUCTION = "Reply to this message with the image."


class ImagePromptError(Exception):
    """Raised when the image prompt template is missing or malformed."""


def build_image_prompt(scene: str, path: Path = DEFAULT_TEMPLATE_PATH) -> str:
    """Return the template with ``scene`` filled in, then the reply instruction."""
    try:
        template = path.read_text(encoding=ENCODING).strip()
    except (OSError, UnicodeDecodeError) as error:
        raise ImagePromptError(
            f"Cannot read image prompt template at {path}"
        ) from error
    if template.count(SCENE_PLACEHOLDER) != 1:
        raise ImagePromptError(
            f"Image prompt template at {path} must contain {SCENE_PLACEHOLDER} once"
        )
    prompt = template.replace(SCENE_PLACEHOLDER, scene.strip())
    return f"{prompt}\n\n{REPLY_INSTRUCTION}"

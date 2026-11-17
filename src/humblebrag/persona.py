"""Loads the versioned Sir Humblebrag persona prompt (``prompts/persona.md``,
relative to the working directory: the repo root, or ``/app`` in the image)."""

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PERSONA_PATH = Path("prompts/persona.md")
ENCODING = "utf-8"
_FRONT_MATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_VERSION_LINE = re.compile(r"^version:\s*(?P<version>\d+)\s*$", re.MULTILINE)


class PersonaError(Exception):
    """Raised when the persona prompt is missing, unreadable or malformed."""


@dataclass(frozen=True)
class Persona:
    """The persona system prompt and its version number."""

    version: int
    text: str


def load_persona(path: Path = DEFAULT_PERSONA_PATH) -> Persona:
    """Read the persona file and return its version and prompt text."""
    try:
        raw = path.read_text(encoding=ENCODING)
    except (OSError, UnicodeDecodeError) as error:
        raise PersonaError(f"Cannot read persona prompt at {path}") from error
    match = _FRONT_MATTER.match(raw)
    version = _VERSION_LINE.search(match["meta"]) if match else None
    if match is None or version is None:
        raise PersonaError(f"Persona prompt at {path} has no version front matter")
    return Persona(version=int(version["version"]), text=match["body"].strip())

"""Tests for the repo scaffold (SA-13 acceptance criteria)."""

import tomllib
from pathlib import Path

import humblebrag

ROOT = Path(__file__).resolve().parent.parent
ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "SUBSTACK_EMAIL",
    "SUBSTACK_PASSWORD",
    "SUBSTACK_PUBLICATION_URL",
    "DATA_DIR",
)


def test_expected_files_and_dirs_exist() -> None:
    """AC1: the layout holds the expected files and directories."""
    assert (ROOT / "src" / "humblebrag" / "__init__.py").is_file()
    assert (ROOT / "tests").is_dir()
    for name in ("pyproject.toml", ".gitignore", ".env.example", "README.md"):
        assert (ROOT / name).is_file(), name


def test_pyproject_targets_python_312() -> None:
    """AC1: pyproject declares Python 3.12 and the src layout."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["project"]["requires-python"] == ">=3.12"
    assert config["tool"]["ruff"]["target-version"] == "py312"
    assert config["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


def test_package_is_importable() -> None:
    """AC2: the package imports, so pytest runs against real code."""
    assert humblebrag.__version__


def test_gitignore_excludes_sensitive_paths() -> None:
    """AC3: .gitignore excludes .env, secrets/ and data/."""
    lines = (ROOT / ".gitignore").read_text().splitlines()
    for entry in (".env", "secrets/", "data/"):
        assert entry in lines, entry


def test_env_example_lists_every_variable_without_values() -> None:
    """AC1: .env.example documents each variable and holds no secret values."""
    entries = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    for name in ENV_VARS:
        assert name in entries, name
    for secret in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "SUBSTACK_PASSWORD"):
        assert entries[secret] == ""


def test_readme_covers_purpose_run_steps_and_env_vars() -> None:
    """AC4: README has purpose, local run steps and every env var."""
    readme = (ROOT / "README.md").read_text()
    assert "## Purpose" in readme
    assert "## Local run steps" in readme
    assert "## Environment variables" in readme
    for name in ENV_VARS:
        assert name in readme, name

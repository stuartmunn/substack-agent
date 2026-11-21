"""Tests that /help lists every registered command (SA-54 acceptance criteria)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update
from telegram.ext import Application, CommandHandler

from humblebrag import bot
from humblebrag.bot import COMMANDS, HELP_COMMANDS, build_application, show_help
from humblebrag.config import load_config
from humblebrag.wiring import Runtime

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
    "TELEGRAM_CHAT_ID": "12345",
    "ANTHROPIC_API_KEY": "key",
    "CLAUDE_MODEL": "m",
    "SUBSTACK_PUBLICATION_URL": "https://example.substack.com",
    "SUBSTACK_COOKIE": "cookie",
}
EXPECTED_ORDER = [
    "start",
    "help",
    "seed",
    "seedtop",
    "seeds",
    "unseed",
    "skip",
    "now",
    "nosteer",
    "reply",
]
HEADER = "Sir Humblebrag III. Commands:"


def registered_commands(application: Application) -> set[str]:
    """Every command name any CommandHandler on the application answers to."""
    return {
        command
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }


def missing_from_help(application: Application) -> set[str]:
    """Registered commands that /help does not list."""
    return registered_commands(application) - set(COMMANDS)


@pytest.fixture
def full_application(tmp_path: Path) -> Application:
    """The application as the agent builds it: bot, commands, redo and reply."""
    config = load_config(ENV)
    application = build_application(config)
    Runtime(config, data_dir=tmp_path).setup(application)
    return application


async def _help_reply() -> str:
    """What /help sends back."""
    update = MagicMock(spec=Update)
    update.effective_message.reply_text = AsyncMock()
    await show_help(update, MagicMock())
    return update.effective_message.reply_text.await_args.args[0]


# AC1 / AC2 / AC3 / AC6: the reply


async def test_help_lists_every_command_in_order_with_a_description() -> None:
    """AC1: all ten commands, each on its own line with a description."""
    lines = (await _help_reply()).splitlines()
    assert lines[0] == HEADER
    listed = [line.split(" - ", 1) for line in lines[1:]]
    assert [name for name, _ in listed] == [f"/{name}" for name in EXPECTED_ORDER]
    assert all(description.strip() for _, description in listed)


def test_every_command_has_a_one_line_description() -> None:
    """AC1/AC6: one line per command, no blanks."""
    assert list(COMMANDS) == EXPECTED_ORDER
    for name, description in COMMANDS.items():
        assert description.strip(), name
        assert "\n" not in description, name


@pytest.mark.parametrize(
    "usage",
    [
        "/seed <text>",
        "/seedtop <text>",
        "/unseed <number>",
        "/reply <link> [| steer]",
    ],
)
def test_commands_with_arguments_show_their_usage(usage: str) -> None:
    """AC2: the usage appears in the help text."""
    assert usage in bot.help_text()


def test_the_nosteer_line_says_it_only_works_while_a_redo_waits() -> None:
    """AC3: /nosteer is explained as only valid while a Redo awaits a steer."""
    line = next(
        line for line in bot.help_text().splitlines() if line.startswith("/nosteer")
    )
    assert "only works while a Redo is waiting on a steer" in line


def test_the_help_format_is_unchanged() -> None:
    """AC6: the same header, then exactly one line per command."""
    lines = bot.help_text().splitlines()
    assert lines[0] == HEADER
    assert len(lines) == len(COMMANDS) + 1
    assert all(line.startswith("/") and " - " in line for line in lines[1:])


async def test_start_and_help_give_the_same_reply() -> None:
    """AC1: /start and /help both show the full list."""
    assert set(HELP_COMMANDS) == {"start", "help"}
    text = await _help_reply()
    for name in EXPECTED_ORDER:
        assert f"/{name} - " in text


@pytest.mark.parametrize("name", ["seedtop", "nosteer", "reply"])
def test_the_commands_added_by_sa_46_the_redo_steer_and_sa_53_are_listed(
    name: str,
) -> None:
    """AC5: the SA-36/SA-45 check now covers /seedtop, /nosteer and /reply."""
    assert f"/{name} " in bot.help_text()


# AC4: the guard test


def test_every_registered_command_is_listed_in_help(
    full_application: Application,
) -> None:
    """AC4: handlers from bot, commands, redo and reply all have a /help entry."""
    assert missing_from_help(full_application) == set()


def test_the_guard_sees_the_commands_of_every_module(
    full_application: Application,
) -> None:
    """AC4: the application really holds commands from all four modules."""
    registered = registered_commands(full_application)
    assert {"start", "help"} <= registered  # bot.py
    assert {"seed", "seedtop", "seeds", "unseed", "skip", "now"} <= registered
    assert "nosteer" in registered  # redo.py
    assert "reply" in registered  # reply.py


def test_the_guard_fails_when_a_command_is_missing_from_help(
    full_application: Application,
) -> None:
    """AC4: a new command registered without a /help entry is caught."""
    full_application.add_handler(CommandHandler("shiny", AsyncMock()))
    assert missing_from_help(full_application) == {"shiny"}


def test_help_has_no_entry_for_a_command_that_does_not_exist(
    full_application: Application,
) -> None:
    """The reverse: /help does not advertise a command nobody answers."""
    assert set(COMMANDS) - registered_commands(full_application) == set()


def test_only_the_four_known_modules_register_commands() -> None:
    """AC4: a new module registering commands must be wired in and listed here."""
    source_dir = Path(bot.__file__).parent
    registering = {
        path.stem
        for path in source_dir.glob("*.py")
        if "CommandHandler(" in path.read_text()
    }
    assert registering == {"bot", "commands", "redo", "reply"}

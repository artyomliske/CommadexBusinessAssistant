"""Целостность набора команд.

Файл однажды был испорчен правкой: часть функций исчезла, а разбор
аргументов о них по-прежнему знал. Такое всплывает только у того, кто
запустит команду на сервере. Тесты закрывают разрыв между тем, что
объявлено, и тем, что существует.
"""

from __future__ import annotations

import argparse
import inspect
import re

from repairbot import cli


def _subcommands() -> list[str]:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert actions, "у разбора аргументов нет подкоманд — тест устарел"
    return sorted(actions[0].choices)


def test_every_command_is_dispatched():
    """Объявленная, но не разбираемая команда молча ничего не делает."""
    source = inspect.getsource(cli._run)

    missing = [name for name in _subcommands() if f'case "{name}"' not in source]

    assert not missing, f"нет обработки: {missing}"


def test_every_dispatched_command_exists():
    """Обработчик, зовущий исчезнувшую функцию, падает только при запуске."""
    source = inspect.getsource(cli._run)
    names = set(re.findall(r"\bcmd_\w+", source))

    missing = sorted(n for n in names if not hasattr(cli, n))

    assert not missing, f"нет таких функций: {missing}"

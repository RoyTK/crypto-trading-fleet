"""Lifecycle tests — keep narrow. Async run() is integration-tested on Hetzner.

These cover the simple invariants we can test without mocking Redis/DB:
- subclass must set bot_id
- heartbeat label is auto-derived
- bot_id matches the convention used by the framework's panic dispatcher
"""
import pytest

from bots.base.bot_lifecycle import BotLifecycle


def test_bot_id_required():
    class _Bad(BotLifecycle):
        bot_id = ""
        async def iterate(self):
            return None
    with pytest.raises(ValueError):
        _Bad()


def test_heartbeat_label_derived():
    class _Good(BotLifecycle):
        bot_id = "structure"
        async def iterate(self):
            return None
    bot = _Good()
    assert bot.heartbeat_label == "bot:structure"


def test_structure_bot_id_matches_framework():
    """The bot_id 'structure' must match what's seeded in framework/main.py
    (BOT_IDS tuple) so its bot_state row exists.
    """
    from framework.main import BOT_IDS
    from bots.structure.main import StructureBot

    bot = StructureBot()
    assert bot.bot_id in BOT_IDS

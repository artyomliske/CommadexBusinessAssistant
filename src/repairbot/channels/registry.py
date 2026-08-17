"""Реестр адаптеров каналов."""

from __future__ import annotations

from repairbot.channels.base import ChannelAdapter
from repairbot.domain.events import Channel

_adapters: dict[Channel, ChannelAdapter] = {}


def register(adapter: ChannelAdapter) -> None:
    _adapters[adapter.channel] = adapter


def get(channel: Channel) -> ChannelAdapter:
    try:
        return _adapters[channel]
    except KeyError:
        raise LookupError(f"Адаптер канала не зарегистрирован: {channel}") from None


def registered() -> list[Channel]:
    return list(_adapters)


def clear() -> None:
    _adapters.clear()

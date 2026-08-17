"""Постановка одобренного сообщения в очередь отправки.

Одобрить и не отправить — состояние, внешне неотличимое от успеха:
в выводе «verdict: allow», в журнале аудита запись, в чате ничего.
Именно так вела себя команда `digest --send`. Тесты закрепляют, что
одобрение и постановка в очередь идут вместе.
"""

from __future__ import annotations

from repairbot.outbound.queue import SEND_TASK, queue_send


class _Redis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, int]] = []

    async def enqueue_job(self, name: str, outbound_id: int) -> None:
        self.jobs.append((name, outbound_id))


async def test_approved_message_is_queued():
    redis = _Redis()

    queued = await queue_send(redis, 17)

    assert queued is True
    assert redis.jobs == [(SEND_TASK, 17)]


async def test_nothing_approved_means_nothing_queued():
    """Заблокированное сообщение не должно попасть в очередь отправки."""
    redis = _Redis()

    queued = await queue_send(redis, None)

    assert queued is False
    assert redis.jobs == []


def test_task_name_matches_the_worker():
    """Опечатка здесь тихо отменила бы отправку: arq молча примет любое имя."""
    from repairbot import worker

    assert SEND_TASK == worker.send_outbound.__name__
    assert worker.send_outbound in worker.WorkerSettings.functions


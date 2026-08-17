"""Контролёр исходящих действий (раздел 3.2 и 6 ТЗ).

Единственная дверь наружу. Ни один агент не обращается к клиенту канала
напрямую — всё проходит здесь.

Слой детерминированный: никаких обращений к модели. Модель уже участвовала
в том, что решила написать; проверять её решение той же моделью незачем.

Порядок проверок — от безусловных запретов к оценке содержания. Первый же
запрет прекращает разбор: если включена централизованная остановка, всё
остальное не имеет значения.

На каждую попытку остаётся запись в журнале аудита — и на отправленную,
и на заблокированную. Без этого нельзя ответить на вопрос «почему бот
это написал», а именно его зададут первым.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy import or_ as sa_or
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.db.models import DialogState, OutboundMessage
from repairbot.observability import get_logger
from repairbot.outbound.policy import Audience, Intent, PolicyDecision, Verdict, evaluate

log = get_logger(__name__)

STOP_WORDS: frozenset[str] = frozenset({
    # Раздел 6 называет два слова: «человек» и «менеджер». Перечисляем их
    # словоформы, а не сравниваем по началу слова: «позовите менеджера»
    # обязано срабатывать, «это менеджерская работа» — нет.
    "человек", "человека", "человеку", "человеком", "человеке",
    "менеджер", "менеджера", "менеджеру", "менеджером", "менеджере",
    "менеджеры", "менеджеров",
})
"""Раздел 6: при получении этих слов от заказчика бот прекращает
автоответы в диалоге на 24 часа с уведомлением менеджера.

Список — точка настройки. Если на пилоте окажется, что человека просят
другими словами («оператор», «живой»), добавлять сюда."""

STOP_PAUSE = timedelta(hours=24)

MAX_MESSAGES_PER_HOUR = 6
"""Ограничение частоты сообщений одному адресату.

Шесть в час — это редко для человека и заведомо мало для сорвавшегося
цикла. Значение подобрано с запасом вниз: лишняя задержка дешевле,
чем заваленный сообщениями заказчик."""

DUPLICATE_WINDOW = timedelta(hours=6)
"""В пределах окна одинаковый текст одному адресату не повторяем."""


@dataclass(slots=True)
class OutboundRequest:
    """Намерение что-то отправить. Создаётся агентом, исполняется контролёром."""

    channel: str
    channel_chat_id: str
    text: str
    intent: Intent
    audience: Audience
    idempotency_key: str
    object_id: int | None = None
    source_event_id: int | None = None
    reply_to_message_id: str | None = None
    recipient_is_bot: bool = False
    discloses_automation: bool = False


@dataclass(slots=True)
class ControlResult:
    verdict: Verdict
    reasons: list[str]
    outbound_id: int | None = None
    duplicate: bool = False

    @property
    def may_send(self) -> bool:
        return self.verdict is Verdict.ALLOW


SWITCH_UNAVAILABLE = "не удалось проверить состояние остановки"
"""Причина, по которой отправка запрещена при недоступном хранилище."""


class KillSwitch:
    """Централизованная остановка всей исходящей активности (раздел 6).

    Хранится в Redis, а не в конфигурации: остановка должна срабатывать
    немедленно и на всех процессах сразу, без перезапуска и выкладки.
    """

    KEY = "outbound:halted"

    def __init__(self, redis) -> None:  # type: ignore[no-untyped-def]
        self._redis = redis

    async def engage(self, reason: str) -> None:
        """Остановить отправку.

        Ошибку не глушим: если кнопка «стоп» не сработала, человек обязан
        об этом узнать, а не считать, что всё остановлено.
        """
        await self._redis.set(self.KEY, reason)
        log.error("outbound.halted", reason=reason)

    async def release(self) -> None:
        await self._redis.delete(self.KEY)
        log.warning("outbound.resumed")

    async def reason(self) -> str | None:
        """Причина остановки или None, если отправка разрешена.

        При недоступном хранилище возвращаем причину, а не None. Это
        сознательный отказ в сторону безопасности: раз состояние
        предохранителя проверить нельзя, считаем, что он сработал.
        Молчаливое «разрешено» здесь означало бы, что при падении Redis
        система начинает писать заказчикам без единой проверки остановки.
        """
        if self._redis is None:
            return None
        try:
            return await self._redis.get(self.KEY)
        except Exception as exc:
            log.error("outbound.kill_switch_unreachable", error=str(exc))
            return SWITCH_UNAVAILABLE


class Controller:
    def __init__(
        self,
        session: AsyncSession,
        *,
        kill_switch: KillSwitch | None = None,
        max_per_hour: int = MAX_MESSAGES_PER_HOUR,
    ) -> None:
        self._session = session
        self._kill_switch = kill_switch
        self._max_per_hour = max_per_hour

    async def review(self, request: OutboundRequest) -> ControlResult:
        """Проверить исходящее действие и записать решение в журнал аудита."""
        now = datetime.now(tz=UTC)

        blocked = await self._hard_blocks(request, now)
        if blocked is not None:
            return await self._record(request, Verdict.BLOCK, blocked, now)

        decision = await self._soft_checks(request, now)
        return await self._record(request, decision.verdict, list(decision.reasons), now)

    # --- безусловные запреты ---

    async def _hard_blocks(self, request: OutboundRequest, now: datetime) -> list[str] | None:
        """Проверки, после которых разбор прекращается."""
        if self._kill_switch is not None:
            halted = await self._kill_switch.reason()
            if halted:
                return [f"включена централизованная остановка: {halted}"]

        if request.recipient_is_bot:
            # Защита от циклов при взаимодействии с другими ботами.
            return ["получатель — бот"]

        if not request.text.strip():
            return ["пустой текст"]

        state = await self._dialog_state(request)
        if state is not None and state.is_paused:
            return [
                f"автоответы приостановлены до {state.autoreply_paused_until:%d.%m %H:%M} "
                f"({state.paused_reason or 'по стоп-слову'})"
            ]

        if await self._is_duplicate(request, now):
            return ["такой же текст уже отправлялся этому адресату недавно"]

        return None

    # --- оценка содержания и частоты ---

    async def _soft_checks(self, request: OutboundRequest, now: datetime) -> PolicyDecision:
        state = await self._dialog_state(request)
        first_contact = state is None or state.automation_disclosed_at is None

        decision = evaluate(
            request.intent,
            audience=request.audience,
            text=request.text,
            is_first_contact=first_contact,
            discloses_automation=request.discloses_automation,
        )

        reasons = list(decision.reasons)
        if await self._rate_limited(request, now):
            reasons.append(
                f"превышена частота сообщений адресату: больше {self._max_per_hour} за час"
            )

        if not reasons:
            return decision
        return PolicyDecision(
            verdict=Verdict.HOLD,
            reasons=tuple(reasons),
            triggered_by=decision.triggered_by,
        )

    async def _rate_limited(self, request: OutboundRequest, now: datetime) -> bool:
        """Не частим ли мы с этим адресатом.

        Правило бережёт **заказчика**: получить от бота десяток
        сообщений за час — повод перестать читать их вовсе.

        К своим оно не относится. Помощник отвечает на прямые вопросы,
        и шесть сообщений в час там — обычный темп разговора; предел
        превращал бы беседу в молчание ровно на седьмой реплике, причём
        без всякого объяснения для собеседника.
        """
        if request.audience is Audience.MANAGER:
            return False

        since = now - timedelta(hours=1)
        sent = (
            await self._session.execute(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.channel == request.channel,
                    OutboundMessage.channel_chat_id == request.channel_chat_id,
                    OutboundMessage.sent_at.isnot(None),
                    OutboundMessage.sent_at >= since,
                )
            )
        ).scalar_one()
        return int(sent) >= self._max_per_hour

    async def _is_duplicate(self, request: OutboundRequest, now: datetime) -> bool:
        """Уходил ли этот же текст этому же адресату недавно.

        Считаем дубликатом то, что **ушло или уйдёт**: одобренное к
        отправке и уже отправленное. Заблокированное и отклонённое —
        не считаем.

        Различение не праздное. Считать по факту отправки нельзя: два
        одобренных сообщения ждут очереди, оба пройдут проверку и оба
        уйдут заказчику. Считать по всем записям тоже нельзя: сообщение,
        не прошедшее из-за аварийной остановки, осталось бы навсегда
        заблокированным как «дубликат» самого себя.

        К своим правило не применяется. Оно защищает заказчика от
        повторного автоответа, которого он не просил; а на дважды
        заданный вопрос дважды дать тот же ответ — правильно, и молчать
        вместо этого нельзя. От настоящих двойных отправок своим
        защищает ключ идемпотентности, а не сравнение текста.
        """
        if request.audience is Audience.MANAGER:
            return False

        since = now - DUPLICATE_WINDOW
        existing = (
            await self._session.execute(
                select(OutboundMessage.id).where(
                    OutboundMessage.channel == request.channel,
                    OutboundMessage.channel_chat_id == request.channel_chat_id,
                    OutboundMessage.text == request.text,
                    OutboundMessage.created_at >= since,
                    # Одобрено к отправке либо уже отправлено (в том числе
                    # черновик, который менеджер подтвердил вручную).
                    sa_or(
                        OutboundMessage.verdict == Verdict.ALLOW.value,
                        OutboundMessage.sent_at.isnot(None),
                    ),
                )
            )
        ).first()
        return existing is not None

    # --- журнал аудита ---

    async def _record(
        self,
        request: OutboundRequest,
        verdict: Verdict,
        reasons: list[str],
        now: datetime,
    ) -> ControlResult:
        stmt = (
            pg_insert(OutboundMessage)
            .values(
                object_id=request.object_id,
                source_event_id=request.source_event_id,
                channel=request.channel,
                channel_chat_id=request.channel_chat_id,
                reply_to_message_id=request.reply_to_message_id,
                intent=request.intent.value,
                audience=request.audience.value,
                text=request.text,
                verdict=verdict.value,
                reasons={"reasons": reasons},
                idempotency_key=request.idempotency_key,
            )
            .on_conflict_do_nothing(constraint="uq_outbound_idempotency")
            .returning(OutboundMessage.id)
        )
        outbound_id = (await self._session.execute(stmt)).scalar_one_or_none()

        if outbound_id is None:
            # Повтор задачи: действие уже разобрано, второй раз не отправляем.
            log.info("outbound.duplicate_request", key=request.idempotency_key)
            return ControlResult(Verdict.BLOCK, ["действие уже было обработано"], duplicate=True)

        log.info(
            "outbound.reviewed",
            outbound_id=outbound_id,
            verdict=verdict.value,
            intent=request.intent.value,
            audience=request.audience.value,
            reasons=reasons,
        )
        return ControlResult(verdict, reasons, outbound_id=outbound_id)

    # --- предохранители диалога ---

    async def _dialog_state(self, request: OutboundRequest) -> DialogState | None:
        return (
            await self._session.execute(
                select(DialogState).where(
                    DialogState.channel == request.channel,
                    DialogState.channel_chat_id == request.channel_chat_id,
                )
            )
        ).scalar_one_or_none()

    async def mark_sent(
        self, outbound_id: int, *, channel_message_id: str | None, now: datetime | None = None
    ) -> None:
        """Отметить фактическую отправку и подвинуть счётчики диалога."""
        now = now or datetime.now(tz=UTC)
        message = (
            await self._session.execute(
                select(OutboundMessage).where(OutboundMessage.id == outbound_id)
            )
        ).scalar_one()

        message.sent_at = now
        message.channel_message_id = channel_message_id

        await self._touch_dialog(
            message.channel, message.channel_chat_id, last_outbound_at=now
        )

    async def _touch_dialog(self, channel: str, chat_id: str, **values: object) -> None:
        stmt = (
            pg_insert(DialogState)
            .values(channel=channel, channel_chat_id=chat_id, **values)
            .on_conflict_do_update(constraint="uq_dialog_state", set_=values)
        )
        await self._session.execute(stmt)


def contains_stop_word(text: str) -> str | None:
    """Найти стоп-слово во **входящем** сообщении заказчика.

    Раздел 6: «при получении от заказчика слов „человек“ или „менеджер“
    бот прекращает автоответы в этом диалоге на 24 часа с уведомлением
    менеджера».

    Сравнение по точным словоформам, а не по началу слова: «позовите
    человека» обязано срабатывать, а «человеческий фактор» и
    «менеджерская работа» — нет.
    """
    if not text:
        return None
    words = {w.strip(".,!?;:()«»\"'—-").casefold() for w in text.split()}
    found = words & STOP_WORDS
    return sorted(found)[0] if found else None


async def pause_autoreplies(
    session: AsyncSession,
    *,
    channel: str,
    channel_chat_id: str,
    reason: str,
    now: datetime | None = None,
) -> datetime:
    """Приостановить автоответы в диалоге на 24 часа."""
    now = now or datetime.now(tz=UTC)
    until = now + STOP_PAUSE
    values = {"autoreply_paused_until": until, "paused_reason": reason[:64]}

    stmt = (
        pg_insert(DialogState)
        .values(channel=channel, channel_chat_id=channel_chat_id, **values)
        .on_conflict_do_update(constraint="uq_dialog_state", set_=values)
    )
    await session.execute(stmt)
    log.warning(
        "outbound.autoreply_paused",
        channel=channel,
        chat_id=channel_chat_id,
        reason=reason,
        until=until.isoformat(),
    )
    return until

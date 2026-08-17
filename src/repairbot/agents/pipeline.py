"""Конвейер обработки события: классификатор → экстрактор → журнал.

Роль оркестратора из раздела 3.2 ТЗ: маршрутизация без формирования текстов
и без обращения к внешним API. Решения принимаются на правилах, к модели
обращаемся только там, где без неё не обойтись — при разборе текста.

Извлечённые факты попадают в тот же журнал событий, что и транспортные
события. Факты ниже порога достоверности помечаются `needs_human` и не
применяются автоматически — их подтверждает менеджер.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repairbot.agents import journal
from repairbot.agents.assistant import Assistant, is_allowed
from repairbot.agents.client_agent import (
    DISCLOSURE,
    ClientAgent,
    ClientDraft,
    should_answer,
)
from repairbot.agents.extractor import Extraction, FactExtractor, MessageContext
from repairbot.agents.object_state import rebuild_object_state
from repairbot.agents.prefilter import Verdict, classify
from repairbot.db.models import (
    ChannelIdentity,
    DialogState,
    Event,
    LlmCall,
    Message,
    Person,
    RepairObject,
)
from repairbot.domain.events import (
    Channel,
    ChatKind,
    EventType,
    InboundEvent,
)
from repairbot.domain.facts import Fact
from repairbot.llm.base import LlmError, LlmRefusal
from repairbot.memory.working import WorkingMemory
from repairbot.observability import get_logger
from repairbot.outbound.controller import (
    Controller,
    KillSwitch,
    OutboundRequest,
    contains_stop_word,
    pause_autoreplies,
)
from repairbot.outbound.policy import Audience, Intent

log = get_logger(__name__)

@dataclass(slots=True)
class PipelineOutcome:
    event_id: int
    verdict: Verdict
    reason: str
    facts_stored: int = 0
    facts_needing_confirmation: int = 0
    degraded: bool = False
    error: str | None = None
    outbound_id: int | None = None
    """Одобренный контролёром ответ заказчику, готовый к отправке.

    Ставит задачу на отправку вызывающий код — после того, как транзакция
    зафиксирована. Конвейер о транспорте не знает и знать не должен."""
    reply_verdict: str | None = None
    """Решение контролёра по ответу: allow, hold, block. None — не отвечали."""


class EventPipeline:
    def __init__(
        self,
        extractor: FactExtractor,
        working_memory: WorkingMemory | None = None,
        *,
        client_agent: ClientAgent | None = None,
        kill_switch: KillSwitch | None = None,
        assistant: Assistant | None = None,
        assistant_user_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._extractor = extractor
        self._wm = working_memory
        self._client_agent = client_agent
        self._kill_switch = kill_switch
        self._assistant = assistant
        self._assistant_users = assistant_user_ids

    async def process(self, session: AsyncSession, event_row: Event) -> PipelineOutcome:
        inbound = _rebuild_inbound(event_row)
        if inbound is None:
            return PipelineOutcome(
                event_row.id, Verdict.SKIP, "событие не содержит разбираемого сообщения"
            )

        # Стоп-слово проверяется ДО классификатора. Голое «человек» короткое
        # и без признаков факта — классификатор отбросил бы его, и
        # предохранитель раздела 6 не сработал бы вовсе.
        await self._check_stop_word(session, event_row, inbound)

        decision = classify(inbound)
        outcome = PipelineOutcome(event_row.id, decision.verdict, decision.reason)

        if decision.verdict is Verdict.EXTRACT:
            outcome = await self._extract_and_store(session, event_row, inbound, outcome)
        else:
            log.info(
                "pipeline.skipped",
                event_id=event_row.id,
                verdict=decision.verdict.value,
                reason=decision.reason,
            )

        # Ответ заказчику — отдельный ход, не зависящий от вердикта
        # префильтра. «Когда закончите с кухней?» фактов не содержит, и
        # префильтр такое сообщение пропускает, — а ответить на него надо
        # именно поэтому. Идёт после разбора: состояние к этому моменту
        # уже пересобрано, и агент отвечает по свежей картине.
        # Личный диалог с тем, кто в списке помощника, — не разговор с
        # заказчиком, а вопрос своего человека. Клиентский агент здесь
        # неуместен: он нарочно не называет суммы и сроки.
        if await self._answer_internally(session, event_row, inbound, outcome):
            return outcome

        await self._answer_client(session, event_row, inbound, outcome)
        return outcome

    async def _answer_internally(
        self,
        session: AsyncSession,
        event_row: Event,
        inbound: InboundEvent,
        outcome: PipelineOutcome,
    ) -> bool:
        """Ответ помощника своим. True — сообщение обработано здесь.

        Отвечаем только в личном диалоге: в групповом чате тот же вопрос
        увидели бы бригады, а картина компании не для них.
        """
        if self._assistant is None:
            return False

        # Каждый отказ называется вслух. Молчаливый выход отсюда
        # неотличим от «помощник подумал и решил не отвечать», и разбирать
        # такое приходится подстановкой логов задним числом.
        if inbound.chat.kind is not ChatKind.DIALOG:
            log.info(
                "pipeline.assistant_skipped",
                event_id=event_row.id,
                reason="не личный диалог",
                chat_kind=inbound.chat.kind.value,
            )
            return False

        message = await self._message_of(session, event_row)
        identity_id = message.author_identity_id if message is not None else None
        if not await is_allowed(session, identity_id, self._assistant_users):
            log.info(
                "pipeline.assistant_skipped",
                event_id=event_row.id,
                reason="автора нет в списке",
                identity_id=identity_id,
                allowed=len(self._assistant_users),
            )
            return False

        state = await self._dialog_state(session, event_row)
        pending = state.pending_action if state is not None else None

        try:
            result = await self._assistant.handle(
                session,
                inbound.text or "",
                pending=pending,
                by=str(inbound.actor.channel_user_id) if inbound.actor else "",
                identity_id=identity_id,
            )
        except LlmError as exc:
            log.error("pipeline.assistant_failed", event_id=event_row.id, error=str(exc))
            return False

        await self._remember_pending(session, event_row, result.pending)
        await self._send_internal(session, event_row, inbound, outcome, result.text)
        log.info("pipeline.assistant", event_id=event_row.id, **result.as_dict())
        return True

    async def _message_of(self, session: AsyncSession, event_row: Event) -> Message | None:
        if event_row.source_message_id is None:
            return None
        return await session.get(Message, event_row.source_message_id)

    async def _remember_pending(
        self, session: AsyncSession, event_row: Event, pending: dict[str, Any] | None
    ) -> None:
        state = await self._dialog_state(session, event_row)
        if state is None:
            state = DialogState(
                channel=event_row.channel, channel_chat_id=event_row.channel_chat_id
            )
            session.add(state)
        state.pending_action = pending
        await session.flush()

    async def _send_internal(
        self,
        session: AsyncSession,
        event_row: Event,
        inbound: InboundEvent,
        outcome: PipelineOutcome,
        text: str,
    ) -> None:
        """Провести ответ помощника через контролёр.

        Через контролёр, хотя адресат свой и запрещать тут нечего: путь
        наружу мимо журнала аудита не должен существовать вовсе — иначе
        однажды им воспользуется что-нибудь ещё.
        """
        controller = Controller(session, kill_switch=self._kill_switch)
        result = await controller.review(
            OutboundRequest(
                channel=event_row.channel,
                channel_chat_id=event_row.channel_chat_id,
                text=text,
                intent=Intent.INTERNAL_NOTICE,
                audience=Audience.MANAGER,
                source_event_id=event_row.id,
                reply_to_message_id=inbound.message_id,
                idempotency_key=f"assistant:{event_row.id}",
            )
        )
        outcome.reply_verdict = result.verdict.value
        if result.may_send:
            outcome.outbound_id = result.outbound_id

    async def _extract_and_store(
        self,
        session: AsyncSession,
        event_row: Event,
        inbound: InboundEvent,
        outcome: PipelineOutcome,
    ) -> PipelineOutcome:
        context = await self._build_context(session, event_row, inbound)
        known_names = await self._known_names(session, event_row.object_id)

        try:
            extraction = await self._extractor.extract(
                inbound.text or "",
                context=context,
                known_names=known_names,
            )
        except LlmRefusal as exc:
            # Отказ модели — повод показать сообщение человеку, а не молча потерять его.
            await self._append_needs_human(
                session, event_row, reason=f"модель отклонила разбор: {exc.category}"
            )
            outcome.error = "refusal"
            return outcome
        except LlmError as exc:
            log.error("pipeline.extraction_failed", event_id=event_row.id, error=str(exc))
            outcome.error = str(exc)
            return outcome

        stored = await self._store_facts(session, event_row, extraction)
        await self._record_llm_call(session, event_row, extraction)

        # Состояние — производная журнала, поэтому пересобирается сразу
        # после появления новых фактов, а не по расписанию: иначе панель
        # и витрина показывали бы вчерашнюю картину.
        if stored and event_row.object_id is not None:
            await rebuild_object_state(session, event_row.object_id)

        outcome.facts_stored = stored
        outcome.facts_needing_confirmation = len(
            extraction.result.facts_requiring_confirmation()
        )
        outcome.degraded = extraction.degraded
        return outcome

    # --- контекст ---

    async def _build_context(
        self, session: AsyncSession, event_row: Event, inbound: InboundEvent
    ) -> MessageContext:
        context = MessageContext(
            message_date=inbound.occurred_at.date(),
            author=f"[NAME_{inbound.actor.channel_user_id}]" if inbound.actor else None,
        )

        if event_row.object_id is not None:
            obj = (
                await session.execute(
                    select(RepairObject).where(RepairObject.id == event_row.object_id)
                )
            ).scalar_one_or_none()
            if obj is not None:
                context.object_code = obj.code
                # Адрес объекта не передаём в модель: это персональные данные.
                context.current_stage = (obj.state or {}).get("current_stage")

        if self._wm is not None and event_row.channel_chat_id:
            recent = await self._wm.recent(
                event_row.channel, event_row.channel_chat_id, limit=10
            )
            # Само разбираемое сообщение исключаем по его идентификатору:
            # оно уже лежит в рабочей памяти и дублировать его в контексте
            # не нужно. Опираться на «последний элемент» нельзя — при
            # параллельной обработке порядок не гарантирован.
            context.recent_messages = [
                text
                for item in recent
                if item.get("message_id") != event_row.channel_message_id
                and (text := (item.get("text") or "").strip())
            ]
        return context

    async def _known_names(
        self, session: AsyncSession, object_id: int | None
    ) -> dict[str, str]:
        """Словарь «реальное имя → условный идентификатор».

        Берётся из карточек людей: так один человек получает стабильный
        идентификатор между сообщениями и вызовами модели.
        """
        rows = await session.execute(
            select(Person.display_name, Person.pseudonym, Person.id).where(
                Person.display_name.isnot(None)
            )
        )
        names: dict[str, str] = {}
        for display_name, pseudonym, person_id in rows:
            if not display_name:
                continue
            names[display_name] = pseudonym or f"[NAME_{person_id}]"

        identities = await session.execute(
            select(ChannelIdentity.display_name, ChannelIdentity.channel_user_id).where(
                ChannelIdentity.display_name.isnot(None)
            )
        )
        for display_name, channel_user_id in identities:
            if display_name and display_name not in names:
                names[display_name] = f"[NAME_{channel_user_id}]"
        return names

    # --- предохранитель: просьба позвать человека ---

    async def _check_stop_word(
        self, session: AsyncSession, event_row: Event, inbound: InboundEvent
    ) -> None:
        """Раздел 6: «человек» или «менеджер» останавливают автоответы.

        Пауза ставится на 24 часа, менеджер получает уведомление через
        журнал. Сообщение при этом разбирается дальше как обычно: заказчик
        мог написать «позовите человека, у нас течёт» — факт о протечке
        терять нельзя.
        """
        if inbound.actor is not None and inbound.actor.is_bot:
            return

        stop_word = contains_stop_word(inbound.text or "")
        if stop_word is None:
            return

        until = await pause_autoreplies(
            session,
            channel=inbound.channel.value,
            channel_chat_id=inbound.chat.channel_chat_id,
            reason=f"стоп-слово: {stop_word}",
        )
        await self._append_needs_human(
            session,
            event_row,
            reason=(
                f"Заказчик просит человека (слово «{stop_word}»). Автоответы в этом "
                f"диалоге отключены до {until:%d.%m %H:%M}."
            ),
        )
        log.warning(
            "pipeline.stop_word",
            event_id=event_row.id,
            word=stop_word,
            chat_id=inbound.chat.channel_chat_id,
        )

    # --- ответ заказчику ---

    async def _answer_client(
        self,
        session: AsyncSession,
        event_row: Event,
        inbound: InboundEvent,
        outcome: PipelineOutcome,
    ) -> None:
        """Составить черновик ответа и провести его через контролёр.

        Конвейер не отправляет сам и не может: в результат кладётся только
        идентификатор одобренной записи. Всё, что контролёр придержал,
        останется в очереди черновиков и ждёт менеджера.
        """
        if self._client_agent is None:
            return

        gate = should_answer(
            text=inbound.text or "",
            chat_kind=inbound.chat.kind.value,
            author_role=await self._author_role(session, event_row),
            author_is_bot=bool(inbound.actor and inbound.actor.is_bot),
        )
        if not gate.answer:
            return

        if event_row.object_id is None:
            # Отвечать не по чему: состояние объекта — единственный
            # источник для агента. Менеджер увидит вопрос в очереди.
            await self._append_needs_human(
                session,
                event_row,
                reason="Вопрос заказчика, но чат не привязан к объекту — ответить нечем.",
            )
            return

        state = await self._dialog_state(session, event_row)
        if state is not None and state.is_paused:
            # Стоп-слово сработало в этом же сообщении либо диалог уже
            # на паузе. Контролёр всё равно заблокировал бы ответ —
            # выходим раньше, чтобы не тратить вызов модели.
            log.info("pipeline.client_reply_skipped", event_id=event_row.id, reason="пауза")
            return

        obj = (
            await session.execute(
                select(RepairObject).where(RepairObject.id == event_row.object_id)
            )
        ).scalar_one_or_none()
        if obj is None:
            return

        try:
            draft = await self._client_agent.draft(
                inbound.text or "",
                object_state=obj.state or {},
                object_code=obj.code,
            )
        except LlmRefusal as exc:
            await self._append_needs_human(
                session, event_row, reason=f"модель отклонила ответ заказчику: {exc.category}"
            )
            return
        except LlmError as exc:
            # Ответ заказчику не критичен для приёма событий: факты уже
            # записаны, а вопрос увидит менеджер.
            log.error("pipeline.client_reply_failed", event_id=event_row.id, error=str(exc))
            await self._append_needs_human(
                session, event_row, reason=f"не удалось составить ответ заказчику: {exc}"
            )
            return

        await self._record_client_call(session, event_row, draft)

        if not draft.answerable:
            await self._append_needs_human(
                session,
                event_row,
                reason=(
                    "Агент не смог ответить по состоянию объекта: "
                    f"{draft.missing or 'данных не хватило'}"
                ),
            )
            return

        # Первый контакт: представляемся автоматической системой прямо в
        # тексте (раздел 6). Без этого политика придержала бы сообщение,
        # и первый же вопрос заказчика ушёл бы в очередь менеджеру.
        first_contact = state is None or state.automation_disclosed_at is None
        text = f"{DISCLOSURE}\n\n{draft.text}" if first_contact else draft.text

        result = await Controller(session, kill_switch=self._kill_switch).review(
            OutboundRequest(
                channel=event_row.channel,
                channel_chat_id=event_row.channel_chat_id or "",
                text=text,
                intent=draft.intent,
                audience=Audience.CLIENT,
                # Ключ по исходному событию: повторный запуск задачи
                # не создаст второй ответ на тот же вопрос.
                idempotency_key=f"client_reply:{event_row.id}",
                object_id=event_row.object_id,
                source_event_id=event_row.id,
                reply_to_message_id=event_row.channel_message_id,
                discloses_automation=first_contact,
            )
        )

        outcome.reply_verdict = result.verdict.value
        if result.may_send:
            outcome.outbound_id = result.outbound_id

        log.info(
            "pipeline.client_reply",
            event_id=event_row.id,
            verdict=result.verdict.value,
            intent=draft.intent.value,
            outbound_id=result.outbound_id,
        )

    async def _author_role(self, session: AsyncSession, event_row: Event) -> str | None:
        """Роль автора сообщения, если она известна.

        Идём через сообщение, а не через `Event.actor_id`: последний
        ссылается на карточку человека, которую приём событий не заводит —
        связать учётную запись мессенджера с человеком может только
        менеджер, и до тех пор роль остаётся неизвестной.

        Неизвестная роль — не препятствие: в личном диалоге с ботом это
        почти наверняка заказчик, а сотрудников исключаем по явной роли.
        """
        if event_row.source_message_id is None:
            return None
        return (
            await session.execute(
                select(Person.role)
                .join(ChannelIdentity, ChannelIdentity.person_id == Person.id)
                .join(Message, Message.author_identity_id == ChannelIdentity.id)
                .where(Message.id == event_row.source_message_id)
            )
        ).scalar_one_or_none()

    async def _dialog_state(
        self, session: AsyncSession, event_row: Event
    ) -> DialogState | None:
        return (
            await session.execute(
                select(DialogState).where(
                    DialogState.channel == event_row.channel,
                    DialogState.channel_chat_id == event_row.channel_chat_id,
                )
            )
        ).scalar_one_or_none()

    # --- учёт расхода ---

    async def _record_client_call(
        self, session: AsyncSession, event_row: Event, draft: ClientDraft
    ) -> None:
        session.add(
            LlmCall(
                object_id=event_row.object_id,
                source_event_id=event_row.id,
                purpose="client_reply",
                provider=draft.provider,
                model=draft.model[:64],
                input_tokens=draft.input_tokens,
                cached_input_tokens=draft.cached_input_tokens,
                output_tokens=draft.output_tokens,
                degraded=draft.degraded,
            )
        )

    async def _record_llm_call(
        self, session: AsyncSession, event_row: Event, extraction: Extraction
    ) -> None:
        """Записать вызов модели.

        Считать стоимость по логам неудобно, а понимать её надо: это
        крупнейшая статья эксплуатационных затрат (раздел 9 ТЗ).
        """
        response = extraction.response
        session.add(
            LlmCall(
                object_id=event_row.object_id,
                source_event_id=event_row.id,
                purpose="extraction",
                provider=response.provider,
                model=response.model[:64],
                input_tokens=response.input_tokens,
                cached_input_tokens=response.cached_input_tokens,
                output_tokens=response.output_tokens,
                degraded=response.degraded,
                facts_extracted=len(extraction.result.facts),
            )
        )

    # --- запись фактов ---

    async def _store_facts(
        self, session: AsyncSession, event_row: Event, extraction: Extraction
    ) -> int:
        stored = 0
        for index, fact in enumerate(extraction.result.facts):
            payload = fact.model_dump(mode="json", exclude_none=True)
            payload["extractor"] = {
                "provider": extraction.response.provider,
                "model": extraction.response.model,
                "schema": self._extractor.schema_fingerprint,
                "degraded": extraction.degraded,
            }
            if extraction.result.note:
                payload["note"] = extraction.result.note

            inserted = await self._append_fact_event(
                session,
                event_row,
                fact=fact,
                payload=payload,
                index=index,
            )
            stored += int(inserted)

        if extraction.result.needs_human:
            await self._append_needs_human(
                session,
                event_row,
                reason=extraction.result.note or "экстрактор отметил необходимость человека",
            )

        log.info(
            "pipeline.facts_stored",
            event_id=event_row.id,
            stored=stored,
            extracted=len(extraction.result.facts),
        )
        return stored

    async def _append_fact_event(
        self,
        session: AsyncSession,
        event_row: Event,
        *,
        fact: Fact,
        payload: dict,
        index: int,
    ) -> bool:
        """Добавить факт в журнал.

        Ключ идемпотентности — (событие, порядковый номер факта): повторный
        запуск задачи не удвоит факты. Обратная сторона: после доработки
        промпта повторный разбор того же сообщения новых фактов не запишет.
        Это согласуется с неизменяемостью журнала — исправления приходят
        отдельными событиями, а не правкой прежних.
        """
        return await journal.append_fact(
            session,
            source=event_row,
            fact=fact,
            payload=payload,
            dedup_key=f"fact:{event_row.id}:{index}",
        )

    async def _append_needs_human(
        self, session: AsyncSession, event_row: Event, *, reason: str
    ) -> None:
        await journal.append_needs_human(
            session,
            source=event_row,
            reason=reason,
            dedup_key=f"needs_human:{event_row.id}",
        )


_EXTRACTABLE_EVENT_TYPES = {
    EventType.MESSAGE_CREATED.value,
    EventType.MESSAGE_EDITED.value,
}


def _rebuild_inbound(event_row: Event) -> InboundEvent | None:
    """Восстановить нормализованное событие из журнала.

    Воркер получает id записи, а не объект: так задача переживает
    перезапуск процесса и остаётся идемпотентной.
    """
    if event_row.event_type not in _EXTRACTABLE_EVENT_TYPES:
        return None

    from repairbot.channels import registry
    from repairbot.domain.events import NormalizationError

    try:
        adapter = registry.get(Channel(event_row.channel))
    except (LookupError, ValueError):
        log.warning("pipeline.no_adapter", channel=event_row.channel, event_id=event_row.id)
        return None

    try:
        events = adapter.normalize(event_row.payload or {})
    except NormalizationError:
        return None
    return events[0] if events else None


async def load_event(session: AsyncSession, event_id: int) -> Event | None:
    return (await session.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()


async def load_message_text(session: AsyncSession, message_id: int) -> str | None:
    return (
        await session.execute(select(Message.text).where(Message.id == message_id))
    ).scalar_one_or_none()

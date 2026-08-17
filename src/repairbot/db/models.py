"""Схема данных (раздел 4 ТЗ).

Ключевое свойство: `events` — неизменяемый журнал, только добавление записей.
Текущее состояние объекта является производной от журнала, поэтому любой
показатель прослеживается до исходного сообщения.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    # ClassVar: это настройка сопоставления типов, а не колонка таблицы.
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RepairObject(TimestampMixin, Base):
    """Объект — квартира в ремонте."""

    __tablename__ = "objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    """Короткий человекочитаемый идентификатор, например obj_17."""
    address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    started_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planned_finish_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    spreadsheet_id: Mapped[str | None] = mapped_column(String(128))
    drive_folder_id: Mapped[str | None] = mapped_column(String(128))
    crm_deal_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    """Состояние объекта — свёртка журнала (этап, бюджет, открытые вопросы)."""

    chats: Mapped[list[ChatRecord]] = relationship(back_populates="repair_object")
    aliases: Mapped[list[ObjectAlias]] = relationship(
        back_populates="repair_object", cascade="all, delete-orphan"
    )


class ObjectAlias(TimestampMixin, Base):
    """Как этот объект называют в переписке.

    Чаты у заказчика функциональные, поэтому объект узнаётся из текста
    сообщения. Один адрес пишут по-разному — «Ленина 5», «ЖК Форум»,
    «пятёрка», — и каждый такой способ нужно завести отдельно.
    """

    __tablename__ = "object_aliases"
    __table_args__ = (UniqueConstraint("normalized", name="uq_object_alias_normalized"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_id: Mapped[int] = mapped_column(
        ForeignKey("objects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    """Как записал человек — показывается ему же при разборе."""
    normalized: Mapped[str] = mapped_column(Text, nullable=False)
    """Приведённый вид. Уникален по всей таблице: одна и та же запись не
    может указывать на два объекта, иначе узнавание стало бы гаданием."""

    repair_object: Mapped[RepairObject] = relationship(back_populates="aliases")


class KnowledgeNote(TimestampMixin, Base):
    """Что человек рассказал системе о компании.

    Обычный текст, написанный руками: «касса — наличные в офисе»,
    «подотчёт выдаём только под чеки», «Ростовка и Ростовское шоссе —
    одно и то же». Уходит помощнику в контекст как есть.

    Отдельно от справочника объектов намеренно. Там — строгие записи,
    по которым идёт машинное сопоставление адресов; здесь — свободные
    пояснения, которые ни на что не сопоставляются и нужны только
    модели. Смешать их значило бы либо ограничить пояснения формой,
    либо пустить свободный текст в сопоставление адресов.
    """

    __tablename__ = "knowledge_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    """Короткий заголовок — чтобы список читался."""
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    """Выключенная запись остаётся в списке, но в контекст не идёт.

    Удалять незачем: правило часто нужно временно отключить и вернуть,
    а удалённое приходится вспоминать и набирать заново."""
    created_by: Mapped[str | None] = mapped_column(String(64))


class Person(TimestampMixin, Base):
    """Человек: сотрудник, заказчик или поставщик."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    """staff | client | supplier | unknown"""
    phone: Mapped[str | None] = mapped_column(String(32))
    """Только верифицированный номер из request_contact."""
    email: Mapped[str | None] = mapped_column(String(255))
    pseudonym: Mapped[str | None] = mapped_column(String(64), unique=True)
    """Условный идентификатор для псевдонимизации перед отправкой в модель."""


class ChannelIdentity(TimestampMixin, Base):
    """Связка «человек» ↔ «учётная запись в мессенджере»."""

    __tablename__ = "channel_identities"
    __table_args__ = (
        UniqueConstraint("channel", "channel_user_id", name="uq_channel_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ChatRecord(TimestampMixin, Base):
    """Собственный реестр чатов.

    Метод `GET /chats` в MAX отключён с июня 2026, поэтому реестр ведётся
    на своей стороне по событиям `bot_added` и `bot_started` (раздел 2 ТЗ).
    """

    __tablename__ = "chats"
    __table_args__ = (UniqueConstraint("channel", "channel_chat_id", name="uq_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="group", nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id", ondelete="SET NULL"))
    bot_is_member: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_read_all_messages: Mapped[bool | None] = mapped_column(Boolean)
    """Право `read_all_messages` в групповом чате. None — ещё не проверено."""
    history_backfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repair_object: Mapped[RepairObject | None] = relationship(back_populates="chats")


class Message(Base):
    """Сырое сообщение как оно пришло из канала."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("channel", "channel_chat_id", "channel_message_id", name="uq_message"),
        Index("ix_messages_chat_sent", "chat_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id", ondelete="SET NULL"))
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id", ondelete="SET NULL"))
    object_source: Mapped[str | None] = mapped_column(String(16))
    """Откуда стал известен объект: chat, text, reply, context, manual.

    Без этой пометки разбор непроверяем: по одной лишь ссылке на объект
    не отличить точное указание адреса от догадки по предыдущему
    сообщению, и незаметная ошибка расходится по сводкам.
    """
    author_identity_id: Mapped[int | None] = mapped_column(
        ForeignKey("channel_identities.id", ondelete="SET NULL")
    )
    reply_to_message_id: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_outbound: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    attachments: Mapped[list[AttachmentRecord]] = relationship(back_populates="message")


class AttachmentRecord(Base):
    """Вложение. Перекладывание на Диск — этап 3."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_file_id: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    doc_class: Mapped[str | None] = mapped_column(String(32))
    """photo | measurement | receipt | contract | act — заполняет агент документов."""
    drive_file_id: Mapped[str | None] = mapped_column(String(128))
    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    message: Mapped[Message] = relationship(back_populates="attachments")


class User(TimestampMixin, Base):
    """Пользователь веб-интерфейса.

    Интерфейс показывает переписку и персональные данные, поэтому вход
    обязателен. Роли соответствуют матрице прав из пункта 11 ТЗ — пока
    она не получена, различаем три уровня.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="viewer", nullable=False)
    """admin — всё; manager — подтверждение фактов; viewer — только чтение."""
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def can_review(self) -> bool:
        return self.role in ("admin", "manager")


class LlmCall(Base):
    """Вызов языковой модели — для панели расхода (раздел 9 ТЗ).

    Считать стоимость по логам неудобно, а понимать её надо: обработка
    сообщений моделью — крупнейшая статья эксплуатационных затрат.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id", ondelete="SET NULL"))
    source_event_id: Mapped[int | None] = mapped_column(BigInteger)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    """extraction | client_reply | report — по чему пришёлся расход."""
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Обслужено резервным провайдером."""
    facts_extracted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboundMessage(TimestampMixin, Base):
    """Исходящее действие и решение контролёра по нему (раздел 6 ТЗ).

    Запись создаётся на **каждую попытку**, независимо от исхода: и когда
    сообщение ушло, и когда его заблокировали. Это и есть журнал аудита,
    без которого нельзя ответить на вопрос «почему бот это написал».

    Отклонённые черновики не удаляются: раздел 6 требует накапливать их
    для последующей доработки промптов.
    """

    __tablename__ = "outbound_messages"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbound_idempotency"),
        Index("ix_outbound_pending", "verdict", "created_at"),
        Index("ix_outbound_recipient", "channel", "channel_chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id", ondelete="SET NULL"))
    source_event_id: Mapped[int | None] = mapped_column(BigInteger)

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reply_to_message_id: Mapped[str | None] = mapped_column(String(128))

    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    audience: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    """allow | hold | block — решение контролёра."""
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    """Почему именно так. Разбирать инциденты по свободному тексту нельзя."""

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel_message_id: Mapped[str | None] = mapped_column(String(128))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(String(16))
    """sent | edited | rejected — что выбрал менеджер по черновику."""
    edited_text: Mapped[str | None] = mapped_column(Text)

    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    """Одно и то же действие не должно уйти дважды при повторе задачи."""


class DialogState(TimestampMixin, Base):
    """Состояние диалога для предохранителей раздела 6.

    Отдельно от `chats`: там реестр подключения, здесь — поведение бота
    в конкретном диалоге, и меняется оно намного чаще.
    """

    __tablename__ = "dialog_states"
    __table_args__ = (
        UniqueConstraint("channel", "channel_chat_id", name="uq_dialog_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)

    autoreply_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Стоп-слово «человек» или «менеджер»: автоответы прекращаются на 24 часа."""
    paused_reason: Mapped[str | None] = mapped_column(String(64))

    automation_disclosed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Когда система представилась автоматической. None — ещё не представилась."""

    pending_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """Действие, предложенное помощником и ждущее согласия.

    Хранится в базе, а не в памяти процесса: подтверждение приходит
    следующим сообщением, а между ними воркер может перезапуститься."""

    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outbound_count_hour: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hour_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_paused(self) -> bool:
        from datetime import UTC

        return bool(
            self.autoreply_paused_until
            and self.autoreply_paused_until > datetime.now(tz=UTC)
        )


class Event(Base):
    """Журнал событий.

    Содержимое события (`payload`, `event_type`, `occurred_at`) не меняется
    никогда; удаление запрещено триггером в БД, а не соглашением. Изменять
    допустимо только флаги обработки — `applied` и `needs_human`.

    Здесь лежат и транспортные события шлюза, и извлечённые бизнес-факты
    (этап 2) — у них разный `event_type`, но одинаковая прослеживаемость.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("channel", "channel_chat_id", "dedup_key", name="uq_event_dedup"),
        Index("ix_events_object_created", "object_id", "created_at"),
        Index("ix_events_payload_gin", "payload", postgresql_using="gin"),
        Index("ix_events_pending", "applied", "needs_human"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id", ondelete="SET NULL"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_chat_id: Mapped[str | None] = mapped_column(String(64))
    channel_message_id: Mapped[str | None] = mapped_column(String(128))
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RecurringPayment(TimestampMixin, Base):
    """Регулярный платёж компании: подписка, связь, аренда, налоги.

    К объектам ремонта отношения не имеет — это собственные расходы
    компании, за которыми иначе никто не следит. Поэтому и суммы здесь
    свои, а не заказчика: их можно писать в напоминание без подтверждения
    человеком (раздел 6 запрещает автоматические денежные сведения
    **внешним** адресатам, а не себе).

    День месяца хранится отдельно от даты платежа намеренно: платёж 31-го
    числа в феврале приходится на 28-е, и если запомнить только дату, он
    останется 28-го навсегда. Якорь позволяет вернуться на 31-е в марте.
    """

    __tablename__ = "recurring_payments"
    __table_args__ = (Index("ix_payments_due", "active", "next_due_on"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(24), default="other", nullable=False)
    """subscription | telecom | rent | tax | salary | utilities | other"""

    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    """Пусто — сумма плавающая (связь по факту, коммунальные по счётчику)."""
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)

    period: Mapped[str] = mapped_column(String(16), default="monthly", nullable=False)
    """monthly | quarterly | yearly | weekly | once"""
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    """Якорный день для месячных и годовых. Пусто — берётся из даты."""

    next_due_on: Mapped[date] = mapped_column(Date, nullable=False)
    notify_days_before: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    """Чем и откуда платится: карта, счёт, кто ответственный."""

    last_paid_on: Mapped[date | None] = mapped_column(Date)
    notified_for: Mapped[date | None] = mapped_column(Date)
    """Дата платежа, о которой уже напомнили.

    Ключ идемпотентности напоминаний: без него ежедневная задача слала бы
    напоминание о том же платеже каждый день до срока."""
    overdue_notified_for: Mapped[date | None] = mapped_column(Date)
    """То же для сообщения о просрочке — оно отдельное и приходит один раз."""

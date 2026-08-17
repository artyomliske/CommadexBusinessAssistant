"""Воркер ARQ.

`process_event` — точка, куда события попадают из шлюза. Здесь работает
конвейер этапа 2: классификатор отсеивает болтовню, экстрактор разбирает
остальное, факты уходят в журнал.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy import func, select

from repairbot.agents.assistant import Assistant, allowed_user_ids
from repairbot.agents.client_agent import ClientAgent
from repairbot.agents.document_pass import DocumentPass
from repairbot.agents.documents import DocumentAgent
from repairbot.agents.extractor import FactExtractor
from repairbot.agents.object_state import rebuild_all
from repairbot.agents.payment_calendar import send_reminder as send_payment_reminder
from repairbot.agents.pipeline import EventPipeline, load_event
from repairbot.agents.report_delivery import ReportDelivery
from repairbot.agents.reports import next_period_for
from repairbot.channels import registry
from repairbot.channels.max.adapter import MaxAdapter
from repairbot.channels.telegram import TelegramAdapter
from repairbot.config import get_settings
from repairbot.db.models import LlmCall, RepairObject
from repairbot.db.session import dispose_engine, init_engine, session_scope
from repairbot.ingest.chat_titles import fill_missing_titles
from repairbot.integrations import google_auth
from repairbot.integrations.drive import DriveClient, FileArchive
from repairbot.integrations.sheets import SheetsClient, SheetsError, SheetsSync
from repairbot.llm import build_router, pricing
from repairbot.memory.working import WorkingMemory
from repairbot.observability import get_logger, setup_observability
from repairbot.outbound.controller import Controller, KillSwitch, OutboundRequest
from repairbot.outbound.policy import Audience, Intent
from repairbot.outbound.queue import queue_send
from repairbot.outbound.sender import SendRefused, send_approved

log = get_logger(__name__)


async def process_event(ctx: dict[str, Any], event_id: int) -> dict[str, Any]:
    """Обработать одно событие журнала.

    Задача принимает id записи, а не объект события: так она переживает
    перезапуск воркера, а повторный запуск безопасен — факты пишутся
    с ключом идемпотентности.
    """
    pipeline: EventPipeline = ctx["pipeline"]

    async with session_scope() as session:
        event_row = await load_event(session, event_id)
        if event_row is None:
            log.warning("worker.event_missing", event_id=event_id)
            return {"event_id": event_id, "status": "missing"}

        outcome = await pipeline.process(session, event_row)

    # Отправка ставится в очередь после фиксации транзакции: иначе задача
    # может начаться раньше, чем запись контролёра доедет до базы.
    await queue_send(ctx["redis"], outcome.outbound_id)

    return {
        "event_id": outcome.event_id,
        "status": "error" if outcome.error else outcome.verdict.value,
        "verdict": outcome.verdict.value,
        "reason": outcome.reason,
        "facts_stored": outcome.facts_stored,
        "facts_needing_confirmation": outcome.facts_needing_confirmation,
        "degraded": outcome.degraded,
        "error": outcome.error,
        "reply_verdict": outcome.reply_verdict,
        "outbound_id": outcome.outbound_id,
    }


async def send_outbound(ctx: dict[str, Any], outbound_id: int) -> dict[str, Any]:
    """Отправить одобренное исходящее сообщение.

    Отправка живёт в воркере, а не в веб-процессе: у интерфейса нет
    клиента канала, и это намеренно — панель не должна уметь ходить
    наружу сама.
    """
    async with session_scope() as session:
        controller = Controller(session, kill_switch=ctx.get("kill_switch"))
        try:
            result = await send_approved(session, outbound_id, controller=controller)
        except SendRefused as exc:
            log.error("outbound.refused", outbound_id=outbound_id, error=str(exc))
            return {"outbound_id": outbound_id, "sent": False, "error": str(exc)}

    return {
        "outbound_id": result.outbound_id,
        "sent": result.sent,
        "channel_message_id": result.channel_message_id,
        "skipped_reason": result.skipped_reason,
    }


async def sync_sheets(ctx: dict[str, Any]) -> dict[str, Any]:
    """Перенести журнал в витрину (раздел 5 ТЗ).

    Запускается по расписанию с интервалом 30 с. Пакетная запись означает,
    что за проход расходуется по одному запросу на лист, а не на строку, —
    иначе квота 60 запросов в минуту кончилась бы на первом же объекте.
    """
    sync: SheetsSync | None = ctx.get("sheets_sync")
    if sync is None:
        return {"status": "disabled"}

    settings = ctx["settings"]
    written = 0
    edits = 0
    failed: list[str] = []

    async with session_scope() as session:
        objects = list(
            (
                await session.execute(
                    select(RepairObject).where(RepairObject.status == "active")
                )
            )
            .scalars()
            .all()
        )

        for obj in objects:
            outcome = await sync.sync_object(session, obj)
            written += outcome.rows_written
            edits += outcome.manual_edits
            if outcome.error:
                failed.append(obj.code)

        if settings.sheets_rollup_spreadsheet_id:
            try:
                await sync.sync_rollup(session, settings.sheets_rollup_spreadsheet_id)
            except SheetsError as exc:
                log.warning("sheets.rollup_failed", error=str(exc))
                failed.append("rollup")

    return {
        "objects": len(objects),
        "rows_written": written,
        "manual_edits": edits,
        "failed": failed,
    }


async def archive_files(ctx: dict[str, Any]) -> dict[str, Any]:
    """Переложить вложения на Диск заказчика (этап 3).

    Раз в пять минут, а не по событию: архив не срочен, а пакетная
    обработка ровнее расходует и квоту Диска, и полосу до CDN. Задержка
    здесь ничего не стоит — вложение уже принято и лежит в базе.
    """
    archive: FileArchive | None = ctx.get("file_archive")
    if archive is None:
        return {"status": "disabled"}

    async with session_scope() as session:
        result = await archive.archive_pending(session)
        # Тем же заходом раскладываем то, что легло в общую папку без
        # объекта: объект появляется позже — разбором в панели или новым
        # названием в справочнике, — и файл иначе остался бы не там.
        refiled = await archive.refile_pending(session)
    return {**result.as_dict(), "refiled": refiled}


async def recognize_documents(ctx: dict[str, Any]) -> dict[str, Any]:
    """Разобрать сохранённые на Диске документы (этап 3).

    Порция мелкая и реже архива: каждый документ — вызов модели со
    зрением, самый дорогой из всех, что мы делаем.
    """
    document_pass: DocumentPass | None = ctx.get("document_pass")
    if document_pass is None:
        return {"status": "disabled"}

    async with session_scope() as session:
        result = await document_pass.run(session)
    return result.as_dict()


async def send_digest(ctx: dict[str, Any]) -> dict[str, Any]:
    """Периодическая сводка руководителю (этап 5).

    По понедельникам недельная, в прочие дни суточная. Ключ
    идемпотентности привязан к периоду и дате, поэтому повторный запуск
    задачи после перезапуска воркера второй сводки не пришлёт.
    """
    delivery: ReportDelivery | None = ctx.get("report_delivery")
    if delivery is None:
        return {"status": "disabled"}

    period = next_period_for(datetime.now(tz=UTC).date())
    async with session_scope() as session:
        controller = Controller(session, kill_switch=ctx.get("kill_switch"))
        result = await delivery.deliver(session, period, controller=controller)

    await queue_send(ctx["redis"], result.outbound_id)
    return result.as_dict()


async def remind_payments(ctx: dict[str, Any]) -> dict[str, Any]:
    """Напомнить о регулярных платежах компании.

    Раз в сутки, следом за сводкой. Собственные расходы — подписки, связь,
    аренда — иначе не отслеживает никто, а просроченная подписка выключается
    без предупреждения.
    """
    settings = ctx["settings"]
    if not settings.manager_chat_id:
        return {"status": "disabled"}

    async with session_scope() as session:
        controller = Controller(session, kill_switch=ctx.get("kill_switch"))
        result = await send_payment_reminder(
            session,
            controller=controller,
            channel="max",
            chat_id=settings.manager_chat_id,
        )

    await queue_send(ctx["redis"], result.outbound_id)
    return result.as_dict()


async def fill_chat_titles(ctx: dict[str, Any]) -> dict[str, Any]:
    """Дозапросить названия чатов, которых платформа не прислала.

    В обычном сообщении названия нет, а `GET /chats` в MAX отключён —
    поэтому чат, впервые замеченный по сообщению, остаётся безымянным,
    и в панели вместо имени стоит прочерк. Точечный запрос по одному
    чату разрешён; берём только безымянные, так что после подключения
    задача не делает ни одного запроса.
    """
    adapter = ctx.get("adapter")
    if adapter is None:
        return {"status": "disabled"}

    async with session_scope() as session:
        filled = await fill_missing_titles(session, adapter.client)
    return {"filled": filled}


async def watch_spend(ctx: dict[str, Any]) -> dict[str, Any]:
    """Предупредить, если за сутки на модель ушло больше порога.

    Не запрет, а сообщение. Молча потратить дневной бюджет система не
    должна: узнавать о расходе по счёту провайдера — то же самое, что
    узнавать о протечке от соседей снизу.

    Ключ идемпотентности привязан к дате: сколько бы раз задача ни
    сработала за сутки, предупреждение придёт один раз.
    """
    settings = ctx["settings"]
    if not settings.manager_chat_id or settings.spend_alert_usd <= 0:
        return {"status": "disabled"}

    now = datetime.now(tz=UTC)
    async with session_scope() as session:
        spent = await pricing.spent_usd(session, since=now - timedelta(days=1))
        if spent < settings.spend_alert_usd:
            return {"spent_usd": round(spent, 2), "alerted": False}

        by_purpose = await session.execute(
            select(LlmCall.purpose, func.count(LlmCall.id))
            .where(LlmCall.created_at >= now - timedelta(days=1))
            .group_by(LlmCall.purpose)
            .order_by(func.count(LlmCall.id).desc())
        )
        breakdown = ", ".join(f"{p}: {c}" for p, c in by_purpose.all()) or "нет разбивки"

        controller = Controller(session, kill_switch=ctx.get("kill_switch"))
        outcome = await controller.review(
            OutboundRequest(
                channel="max",
                channel_chat_id=settings.manager_chat_id,
                text=(
                    f"Расход на модель за сутки: ${spent:.2f} "
                    f"(порог ${settings.spend_alert_usd:.2f}).\n"
                    f"Вызовов — {breakdown}.\n"
                    "Подробности на странице «Расход»."
                ),
                intent=Intent.INTERNAL_NOTICE,
                audience=Audience.MANAGER,
                idempotency_key=f"spend:{now:%Y-%m-%d}",
            )
        )

    await queue_send(ctx["redis"], outcome.outbound_id)
    return {"spent_usd": round(spent, 2), "alerted": True, "verdict": outcome.verdict.value}


async def rebuild_states(ctx: dict[str, Any]) -> dict[str, int]:
    """Периодическая пересборка состояния объектов.

    Конвейер и подтверждение фактов пересобирают состояние сразу, но
    ручные правки в таблицах приходят отдельным путём, а отклонения от
    нормативных сроков зависят от текущей даты и «созревают» сами по себе.
    """
    async with session_scope() as session:
        return await rebuild_all(session)


async def compact_working_memory(ctx: dict[str, Any]) -> None:
    """Еженедельная свёртка устаревших событий (раздел 4 ТЗ).

    Наполняется вместе с агентом объекта: свёртка должна опираться на
    состояние объекта, которого пока нет.
    """
    log.info("worker.compaction_noop")


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_observability(settings)
    init_engine(settings)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    working_memory = WorkingMemory(redis, settings)

    # Адаптер нужен воркеру для восстановления события из сырого payload.
    adapter = MaxAdapter(settings)
    registry.register(adapter)

    # Отправка идёт через реестр каналов: воркер должен уметь ответить
    # в тот мессенджер, из которого пришло сообщение.
    telegram = TelegramAdapter(settings) if settings.telegram_enabled else None
    if telegram is not None:
        registry.register(telegram)

    router = build_router(settings)
    extractor = FactExtractor(router)

    credentials = google_auth.from_settings(settings)

    sheets_client = None
    sheets_sync = None
    if settings.sheets_enabled:
        sheets_client = SheetsClient(
            credentials,
            requests_per_minute=settings.sheets_requests_per_minute,
        )
        sheets_sync = SheetsSync(
            sheets_client, drive_folder_id=settings.google_drive_folder_id
        )

    # Архив требует папки: без неё складывать файлы некуда, и включать
    # его наполовину бессмысленно.
    drive_client = None
    file_archive = None
    if credentials.configured and settings.google_drive_folder_id:
        drive_client = DriveClient(credentials)
        file_archive = FileArchive(
            drive_client,
            adapter.client,
            root_folder_id=settings.google_drive_folder_id,
            max_bytes=settings.archive_max_file_bytes,
        )

    # Разбор документов больше не привязан к Диску. Основной источник —
    # копия на Диске: она постоянна, и по ней документ можно перечитать
    # после доработки промптов. Но пока Google не подключён, копий нет
    # вовсе, и без запасного пути счета и чеки лежали бы неразобранными.
    document_pass = DocumentPass(
        DocumentAgent(
            router,
            max_bytes=settings.recognize_max_file_bytes,
            model=settings.recognize_model or None,
        ),
        drive_client,
        max_bytes=settings.recognize_max_file_bytes,
        batch_size=settings.recognize_batch_size,
        channel=adapter.client,
        fast_model=settings.recognize_fast_model or None,
    )

    ctx["settings"] = settings
    ctx["redis_plain"] = redis
    ctx["working_memory"] = working_memory
    ctx["adapter"] = adapter
    ctx["telegram_adapter"] = telegram
    ctx["llm_router"] = router
    kill_switch = KillSwitch(redis)
    # Помощник включается только вместе со списком допущенных: без него
    # он молчит, и это верное поведение по умолчанию — картину компании
    # получил бы любой, кто написал боту в личку.
    allowed = allowed_user_ids(settings.assistant_user_ids)
    ctx["pipeline"] = EventPipeline(
        extractor,
        working_memory,
        client_agent=ClientAgent(router),
        kill_switch=kill_switch,
        assistant=Assistant(router) if allowed else None,
        assistant_user_ids=allowed,
    )
    log.info("worker.assistant", enabled=bool(allowed), people=len(allowed))
    ctx["kill_switch"] = kill_switch
    ctx["sheets_client"] = sheets_client
    ctx["sheets_sync"] = sheets_sync
    ctx["drive_client"] = drive_client
    ctx["file_archive"] = file_archive
    ctx["document_pass"] = document_pass
    ctx["report_delivery"] = ReportDelivery(
        sheets=sheets_client,
        rollup_spreadsheet_id=settings.sheets_rollup_spreadsheet_id,
        manager_chat_id=settings.manager_chat_id,
        pricing={
            "price_input_usd": settings.llm_price_input_usd,
            "price_cached_usd": settings.llm_price_cached_input_usd,
            "price_output_usd": settings.llm_price_output_usd,
            "usd_rub": settings.usd_rub_rate,
        },
    )

    log.info(
        "worker.started",
        model=settings.llm_model,
        effort=settings.llm_effort,
        schema=extractor.schema_fingerprint,
        sheets=settings.sheets_enabled,
        archive=file_archive is not None,
        documents=document_pass is not None,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    if ctx.get("sheets_client") is not None:
        await ctx["sheets_client"].aclose()
    if ctx.get("drive_client") is not None:
        await ctx["drive_client"].aclose()
    await ctx["llm_router"].aclose()
    await ctx["adapter"].aclose()
    if ctx.get("telegram_adapter") is not None:
        await ctx["telegram_adapter"].aclose()
    await ctx["redis_plain"].aclose()
    await dispose_engine()
    registry.clear()
    log.info("worker.stopped")


class WorkerSettings:
    # ClassVar: это конфигурация ARQ, читаемая с класса, а не поля экземпляра.
    functions: ClassVar[list[Any]] = [process_event, send_outbound]
    cron_jobs: ClassVar[list[Any]] = [
        # Состояние пересобираем до записи в витрину, чтобы в таблицу
        # ушла уже свежая сводка, а не прошлая.
        cron(rebuild_states, second={25, 55}, run_at_startup=True, max_tries=1),
        # Названия чатов — редкая и дешёвая задача: запрос уходит только
        # по безымянным чатам, а после подключения таких нет.
        cron(fill_chat_titles, minute={7, 37}, run_at_startup=True, max_tries=1),
        # Расход проверяем раз в час: чаще незачем, реже — и о крупной
        # трате узнаешь к вечеру следующего дня.
        cron(watch_spend, minute={11}, run_at_startup=False, max_tries=1),
        # Интервал записи в Sheets — 30 с (раздел 5 ТЗ: 30-60 с).
        cron(sync_sheets, second={0, 30}, run_at_startup=False, max_tries=1),
        # Архив идёт реже и в стороне: он упирается не в квоту Google,
        # а в скачивание файлов, и торопить его незачем.
        cron(archive_files, minute=set(range(0, 60, 5)), run_at_startup=False, max_tries=1),
        # Распознавание — следом за архивом и вдвое реже: оно читает
        # уже сохранённые файлы, и торопиться ему некуда.
        cron(
            recognize_documents,
            minute={2, 12, 22, 32, 42, 52},
            run_at_startup=False,
            max_tries=1,
        ),
        # Сводка руководителю — раз в сутки, до начала рабочего дня.
        # Час берётся из настроек: часовой пояс заказчика может отличаться.
        cron(
            send_digest,
            hour={get_settings().digest_hour_utc},
            minute={0},
            run_at_startup=False,
            max_tries=2,
        ),
        # Платежи — следом за сводкой, отдельным сообщением: сводка про
        # объекты, напоминание про деньги, и смешивать их незачем.
        cron(
            remind_payments,
            hour={get_settings().digest_hour_utc},
            minute={5},
            run_at_startup=False,
            max_tries=2,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 180
    keep_result = 3600

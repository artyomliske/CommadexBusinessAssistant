"""Конфигурация из переменных окружения."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "repairbot"
    postgres_user: str = "repairbot"
    postgres_password: SecretStr = SecretStr("change-me")

    redis_url: str = "redis://localhost:6379/0"

    max_access_token: SecretStr = SecretStr("")
    max_api_base: str = "https://platform-api2.max.ru"
    max_webhook_secret: SecretStr = SecretStr("change-me")
    max_rate_limit_rps: int = 30

    # --- Канал Telegram (этап 6) ---
    telegram_bot_token: SecretStr = SecretStr("")
    """Токен из @BotFather. Пусто — канал не подключается."""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_webhook_secret: SecretStr = SecretStr("change-me")
    """Возвращается платформой в заголовке каждого запроса и сверяется на
    входе. В отличие от MAX, секрет в путь URL класть не нужно."""
    telegram_rate_limit_rps: int = 25
    """Платформа ограничивает примерно 30 сообщениями в секунду; берём с
    запасом вниз — превышение приводит к временной блокировке бота."""

    working_memory_max_messages: int = Field(default=50, ge=1, le=500)
    working_memory_ttl_seconds: int = Field(default=86_400, ge=60)

    # --- Языковые модели ---
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    """Каким протоколом говорить с основным провайдером.

    `anthropic` — родной протокол: гарантия соответствия схеме,
    кэширование промпта, серверный fallback при отказе модели.
    `openai` — совместимый `/chat/completions`; через него подключается
    OpenRouter. Схема тогда уходит в промпт и проверяется у нас."""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-opus-5"
    llm_base_url: str = ""
    """Адрес провайдера или прокси. Пусто — обращение напрямую.

    Для OpenRouter: https://openrouter.ai/api/v1"""
    llm_supports_media: bool = True
    """Читает ли выбранная модель изображения.

    Свойство модели, а не протокола. Выключите, если модель текстовая:
    тогда распознавание чеков отложится с понятной ошибкой вместо того,
    чтобы записать «на чеке ничего нет»."""
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    """Извлечение фактов из короткого сообщения не требует глубокого
    рассуждения, а расход токенов — статья затрат раздела 9 ТЗ. Уровень
    подлежит проверке на реальной переписке заказчика.

    Действует только при `llm_provider=anthropic`."""

    llm_reserve_api_key: SecretStr = SecretStr("")
    llm_reserve_model: str = ""
    llm_reserve_base_url: str = ""
    llm_reserve_supports_media: bool = False
    """Читает ли резервная модель изображения. По умолчанию нет: у
    российских провайдеров зрение есть не везде, а ошибка честнее, чем
    пустой разбор чека."""
    llm_cooldown_seconds: float = Field(default=60.0, ge=5.0)
    """Пауза перед повторной попыткой обратиться к основному провайдеру."""

    # --- Веб-интерфейс ---
    web_session_secret: SecretStr = SecretStr("")
    """Ключ подписи cookie сессии. Пустой в prod — отказ при старте."""
    web_session_max_age_seconds: int = Field(default=60 * 60 * 12, ge=300)
    web_session_https_only: bool = True
    """В dev по http выключается автоматически, см. `create_app`."""

    # Ставки основного провайдера — для оценки расхода на панели.
    llm_price_input_usd: float = 5.0
    llm_price_cached_input_usd: float = 0.5
    llm_price_output_usd: float = 25.0
    usd_rub_rate: float = 95.0

    # --- Google ---
    # У заказчика личный аккаунт Google, а не Workspace, поэтому основной
    # путь — OAuth. Настройки сервисного аккаунта оставлены на случай
    # переезда на собственный домен (см. `integrations.google_auth`).
    google_oauth_client_secrets_path: str = ""
    """client_secret из Google Cloud Console, тип Desktop app.

    Нужен только для разовой авторизации (`repairbot google-authorize`);
    в работе не используется."""
    google_oauth_token_path: str = ""
    """Файл с refresh-токеном заказчика. Пусто — синхронизация не идёт.

    Содержит долгоживущий доступ к его Диску: права 0600, в резервные
    копии и репозиторий не попадает."""

    google_credentials_path: str = ""
    """Файл сервисного аккаунта — путь для Workspace."""
    google_drive_folder_id: str = ""
    """Папка, где живут книги объектов.

    Пусто — книги не создаются. Это сознательный отказ, а не недоработка:
    книга «где получится» окажется в корне Диска и потеряется среди
    личных файлов заказчика."""

    google_impersonate_subject: str = ""
    """Ящик в домене заказчика, от имени которого работает сервисный аккаунт.

    Только для Workspace: на личном аккаунте делегирования не существует.
    Сервисный аккаунт сам по себе не имеет ни квоты хранилища, ни почтового
    ящика, поэтому создать файл или отправить письмо от своего имени не
    может — делегирование решает и то, и другое.

    Требует, чтобы администратор домена разрешил делегирование и
    **перечислил ровно те области доступа**, что запрашивает система
    (см. `repairbot.integrations.google_auth`)."""
    archive_max_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    """Предел на одно вложение при перекладывании на Диск.

    Всё, что крупнее, помечается отказом и второй раз не скачивается:
    файл держится в памяти целиком, и без предела одно видео с объекта
    способно уронить воркер."""

    recognize_max_file_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    """Предел на файл для распознавания. Меньше архивного намеренно:
    на Диск кладём всё, а в модель двадцатимегабайтную фотографию слать
    незачем — она всё равно ужимается на их стороне, а платим мы за
    передачу."""
    recognize_model: str = ""
    """Модель для распознавания документов. Пусто — общая.

    Стоит задать модель посильнее: сумма с чека попадает в бюджет
    объекта, а документов на порядки меньше, чем сообщений."""
    recognize_batch_size: int = Field(default=10, ge=1, le=100)
    """Сколько документов разбирать за один заход. Каждый — вызов модели
    со зрением, и порция должна укладываться в отведённое задаче время."""

    sheets_requests_per_minute: int = Field(default=60, ge=1, le=300)
    """Квота Sheets API на пользователя (раздел 5 ТЗ)."""
    sheets_rollup_spreadsheet_id: str = ""
    """Книга «Все объекты». Пусто — сводная книга не ведётся."""

    recognize_fast_model: str = ""
    """Дешёвая модель для первого прохода по документам.

    Фотоотчётов больше половины, а стоят они столько же, сколько чтение
    счёта. Дешёвая читает первой; дорогая перечитывает только
    финансовые документы, в которых первая не уверена. Пусто — читать
    сразу дорогой, как было."""

    # --- Расход на модель ---
    spend_confirm_usd: float = 1.0
    """Дороже этого — спрашивать подтверждение перед запуском.

    Разовые операции вроде «перечитать всю историю» стоят реальных
    денег, и узнавать об этом по счёту провайдера поздно. Ноль —
    спрашивать всегда, очень большое число — не спрашивать никогда."""

    spend_alert_usd: float = 5.0
    """Расход за сутки, после которого приходит предупреждение в MAX.

    Не запрет, а сообщение: система продолжает работать, но молча
    потратить дневной бюджет не должна."""

    # --- Внутренний помощник ---
    assistant_user_ids: str = ""
    """Кому помощник отвечает в личном диалоге. Идентификаторы через запятую.

    Пусто — помощник выключен, и это верное значение по умолчанию.
    Помощник видит деньги, сроки и всю картину компании; проверка идёт по
    идентификатору учётной записи, потому что имя подделывается.
    Идентификатор своей учётной записи покажет команда `listen`."""

    # --- Сводки руководителю (этап 5) ---
    manager_chat_id: str = ""
    """Чат MAX, куда уходят периодические сводки. Пусто — не отправляются.

    Почта не используется: у заказчика личный аккаунт Google, а `gmail.send`
    — область ограниченного доступа, которую без проверки приложения не
    выдадут. Раз основной канал MAX, почта в нём лишнее звено."""
    digest_hour_utc: int = Field(default=6, ge=0, le=23)
    """Час отправки суточной сводки по UTC. По умолчанию 6 — это 9 утра
    в Москве, то есть до начала рабочего дня на объектах."""

    sentry_dsn: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sheets_enabled(self) -> bool:
        return bool(self.google_oauth_token_path or self.google_credentials_path)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alembic_database_url(self) -> str:
        """Alembic ходит синхронным драйвером."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_webhook_path(self) -> str:
        """Секрет в пути URL: платформа MAX не подписывает вебхуки."""
        return f"/webhooks/max/{self.max_webhook_secret.get_secret_value()}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def telegram_webhook_path(self) -> str:
        """Адрес постоянный: Telegram присылает секрет заголовком.

        Класть его в путь, как у MAX, не нужно — и не стоит: адрес
        попадает в журналы прокси, а заголовок нет."""
        return "/webhooks/telegram"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()

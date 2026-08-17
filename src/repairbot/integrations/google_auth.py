"""Учётные данные Google.

Способа два, и выбирается он не нами, а тем, какой аккаунт у заказчика.

**OAuth пользователя** — основной путь. У заказчика личный аккаунт Google
(Google One, 5 ТБ), а не Workspace. Делегирования в домене там не существует
как механизма: некому его разрешать, админки домена нет. Поэтому заказчик
один раз разрешает доступ в браузере, а мы храним refresh-токен и работаем
от его имени. Файлы принадлежат ему, что и требовалось.

**Сервисный аккаунт с делегированием** — путь для Workspace. Оставлен на
случай переезда заказчика на собственный домен: тогда токен, живущий у нас
в файле, лучше заменить на делегирование, которое администратор домена
может отозвать сам.

Про refresh-токен есть неприятность, которую надо знать заранее: пока
приложение в Google Cloud числится в статусе Testing, Google отзывает
refresh-токен через семь дней, и синхронизация встаёт до повторного входа
руками. Приложение нужно перевести в Production. Область `drive.file`
у Google не в числе чувствительных, поэтому тяжёлой проверки это не требует.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from repairbot.observability import get_logger

if TYPE_CHECKING:
    from repairbot.config import Settings

log = get_logger(__name__)

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    # drive.file даёт доступ только к файлам, которые создали мы сами или
    # которыми с нами явно поделились. Полный drive не запрашиваем: на том
    # же аккаунте лежит вся личная почта и документы заказчика.
    "https://www.googleapis.com/auth/drive.file",
)


class GoogleAuthError(RuntimeError):
    pass


class Credentials(Protocol):
    """То общее, что нужно клиенту API от обеих реализаций google-auth."""

    token: str | None
    valid: bool

    def refresh(self, request: Any) -> None: ...


@dataclass(frozen=True)
class CredentialsSource:
    """Откуда берутся учётные данные. Загрузка блокирующая — вызывать в потоке."""

    oauth_token_path: str = ""
    service_account_path: str = ""
    impersonate_subject: str = ""

    @property
    def kind(self) -> str:
        if self.oauth_token_path:
            return "oauth"
        if self.service_account_path:
            return "service_account"
        return "none"

    @property
    def configured(self) -> bool:
        return self.kind != "none"

    @property
    def owns_created_files(self) -> bool:
        """Будет ли созданный файл принадлежать заказчику.

        При OAuth — да, владелец тот, кто выдал доступ. У сервисного
        аккаунта своей квоты хранилища нет, поэтому без делегирования он
        не может создать файл нигде: владение обеспечивает `with_subject`.
        """
        if self.kind == "oauth":
            return True
        return self.kind == "service_account" and bool(self.impersonate_subject)

    def load(self) -> Credentials:
        if self.kind == "oauth":
            return self._load_oauth()
        if self.kind == "service_account":
            return self._load_service_account()
        raise GoogleAuthError(
            "Доступ к Google не настроен: задайте GOOGLE_OAUTH_TOKEN_PATH "
            "(личный аккаунт) либо GOOGLE_CREDENTIALS_PATH (Workspace)."
        )

    def _load_oauth(self) -> Credentials:
        from google.oauth2.credentials import Credentials as UserCredentials

        path = Path(self.oauth_token_path)
        if not path.exists():
            raise GoogleAuthError(
                f"Нет файла токена {path}. Выполните: repairbot google-authorize"
            )
        try:
            credentials = UserCredentials.from_authorized_user_file(str(path), list(SCOPES))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            raise GoogleAuthError(f"Файл токена {path} испорчен: {exc}") from exc

        if not credentials.refresh_token:
            # Google отдаёт refresh_token только при первом согласии либо
            # при prompt=consent. Без него токен проживёт час и всё встанет.
            raise GoogleAuthError(
                f"В {path} нет refresh_token — доступ проживёт час. "
                "Пройдите авторизацию заново: repairbot google-authorize"
            )
        return credentials  # type: ignore[return-value]

    def _load_service_account(self) -> Credentials:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            self.service_account_path, scopes=list(SCOPES)
        )
        if self.impersonate_subject:
            credentials = credentials.with_subject(self.impersonate_subject)
        return credentials  # type: ignore[return-value]


def from_settings(settings: Settings) -> CredentialsSource:
    return CredentialsSource(
        oauth_token_path=settings.google_oauth_token_path,
        service_account_path=settings.google_credentials_path,
        impersonate_subject=settings.google_impersonate_subject,
    )


def authorize(client_secrets_path: str, token_path: str, *, port: int = 0) -> str:
    """Разовая авторизация в браузере. Возвращает путь к сохранённому токену.

    Запускать **на машине заказчика или при нём** — браузер должен открыться
    там, где он залогинен в нужный аккаунт. На безголовом сервере это не
    работает: Google требует redirect на localhost, а «скопируйте код из
    браузера» (OOB) отключён с октября 2022 года. Готовый файл токена потом
    кладётся на сервер.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise GoogleAuthError(
            "Не установлен google-auth-oauthlib. Он нужен только для разовой "
            "авторизации: uv pip install 'google-auth-oauthlib>=1.2'"
        ) from exc

    secrets = Path(client_secrets_path)
    if not secrets.exists():
        raise GoogleAuthError(
            f"Нет файла {secrets}. Это client_secret из Google Cloud Console: "
            "APIs & Services → Credentials → OAuth client ID, тип Desktop app."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), list(SCOPES))
    # prompt=consent — иначе при повторной авторизации Google не пришлёт
    # refresh_token, решив, что он у нас уже есть.
    credentials = flow.run_local_server(port=port, prompt="consent", access_type="offline")

    target = Path(token_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(credentials.to_json(), encoding="utf-8")
    # Токен даёт доступ к Диску заказчика — файл не для чужих глаз.
    target.chmod(0o600)

    log.info("google.authorized", token_path=str(target))
    return str(target)

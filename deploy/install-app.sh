#!/usr/bin/env bash
# Установка приложения на российском сервере.
#
# Код должен быть уже залит — deploy/upload.sh делает это с вашей машины.
#
#   bash deploy/upload.sh 1.2.3.4                       # на своей машине
#   ssh root@1.2.3.4 'bash /opt/repairbot/deploy/install-app.sh'
#
# Скрипт готовит всё, кроме секретов: их вы вписываете сами в .env.
# Идемпотентный — повторный запуск перезапустит контейнеры.

set -euo pipefail

APP_DIR=/opt/repairbot
CERT_URL="https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt"

# Что человек обязан вписать сам. Один список на подсказку и на проверку:
# разойдись они — скрипт просил бы одно, а требовал другое.
REQUIRED=(PUBLIC_BASE_URL HTTPS_PROXY HTTP_PROXY LLM_API_KEY LLM_MODEL)

# shellcheck source=common.sh
source "$APP_DIR/deploy/common.sh"

echo "==> Система"
show_system

echo "==> Пакеты"
ensure_docker
apt-get install -y -qq rsync ufw openssl nano >/dev/null

echo "==> Код"
if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
    echo "Код не залит. Сначала на своей машине:" >&2
    echo "  bash deploy/upload.sh $(hostname -I | awk '{print $1}')" >&2
    exit 1
fi
cd "$APP_DIR"

echo "==> Сертификат Минцифры"
mkdir -p certs
if [[ ! -f certs/russian_trusted_root_ca.crt ]]; then
    curl -fsSL -o certs/russian_trusted_root_ca.crt "$CERT_URL"
fi
# Проверяем, что скачался сертификат, а не страница с ошибкой: молча
# положить HTML в certs/ значит потом искать, почему не работает TLS.
if ! openssl x509 -in certs/russian_trusted_root_ca.crt -noout -subject >/dev/null 2>&1; then
    echo "Скачанный файл не является сертификатом. Возьмите его вручную:" >&2
    echo "  https://www.gosuslugi.ru/crt — раздел «Сертификат для ПК»" >&2
    rm -f certs/russian_trusted_root_ca.crt
    exit 1
fi
openssl x509 -in certs/russian_trusted_root_ca.crt -noout -subject

echo "==> Конфигурация"
mkdir -p secrets && chmod 700 secrets
if [[ ! -f .env ]]; then
    cp deploy/.env.production.example .env
    chmod 600 .env
    # Всё, что можно сгенерировать без человека, генерируем сразу:
    # меньше пустых полей — меньше шансов забыть одно из них.
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" .env
    sed -i "s|^WEB_SESSION_SECRET=.*|WEB_SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')|" .env
    sed -i "s|^MAX_WEBHOOK_SECRET=.*|MAX_WEBHOOK_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" .env
    sed -i "s|^TELEGRAM_WEBHOOK_SECRET=.*|TELEGRAM_WEBHOOK_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" .env

    echo
    echo "  Создан /opt/repairbot/.env"
    echo "  Пароль базы, ключ сессий и секреты вебхуков сгенерированы."
    echo
    echo "  Заполните вручную:"
    for key in "${REQUIRED[@]}"; do
        echo "    $key"
    done
    echo
    echo "    nano /opt/repairbot/.env"
    echo
    echo "  Потом запустите этот скрипт ещё раз."
    exit 0
fi

# Настройки, появившиеся в шаблоне после того, как .env уже создали.
# Существующий файл не перезаписывается — и новая настройка молча
# остаётся отсутствующей. Так однажды выключился помощник: строки в
# .env не было, а `sed` по ней ничего не заменил и промолчал.
NEW_KEYS=""
while IFS= read -r key; do
    grep -q "^${key}=" .env || NEW_KEYS="$NEW_KEYS $key"
done < <(grep -oE '^[A-Z][A-Z0-9_]*=' deploy/.env.production.example | tr -d '=')

if [[ -n "$NEW_KEYS" ]]; then
    echo
    echo "  В шаблоне есть настройки, которых нет в вашем .env:"
    for key in $NEW_KEYS; do
        echo "    $key"
    done
    echo "  Дописываю их пустыми — значения смотрите в"
    echo "  deploy/.env.production.example и заполняйте при необходимости."
    for key in $NEW_KEYS; do
        echo "${key}=" >> .env
    done
    echo
fi

MISSING=""
for key in "${REQUIRED[@]}"; do
    value="$(grep "^${key}=" .env | cut -d= -f2- || true)"
    if [[ -z "$value" || "$value" == *"ЗАПОЛНИТЬ"* || "$value" == *"вашдомен"* || "$value" == *"IP_ГЕРМАНИИ"* ]]; then
        MISSING="$MISSING $key"
    fi
done
if [[ -n "$MISSING" ]]; then
    echo "Не заполнено в .env:$MISSING" >&2
    exit 1
fi

echo "==> Запуск"
docker compose up -d --build

echo "==> Firewall"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo
echo "==> Проверки"
docker compose exec -T api python -m repairbot.cli digest --period day || true
echo
echo "Дальше:"
echo "  docker compose exec api python -m repairbot.cli check-drive"
echo "  docker compose exec api python -m repairbot.cli bot-info      # после модерации"
echo "  docker compose exec api python -m repairbot.cli listen --seconds 60"

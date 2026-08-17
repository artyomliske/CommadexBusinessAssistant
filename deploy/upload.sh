#!/usr/bin/env bash
# Заливка проекта на сервер. Запускается НА ВАШЕЙ МАШИНЕ из корня проекта.
#
#   bash deploy/upload.sh 153.80.184.45
#
# Репозитория у проекта нет, поэтому код едет прямо отсюда — 3 МБ.
# Виртуальное окружение, база и кэши не едут: на сервере они свои,
# а .venv с Мака там всё равно не заработает.

set -euo pipefail

SERVER="${1:-}"
USER="${2:-root}"
if [[ -z "$SERVER" ]]; then
    echo "Укажите адрес сервера: bash deploy/upload.sh 1.2.3.4" >&2
    exit 1
fi

if [[ ! -f pyproject.toml ]]; then
    echo "Запускать из корня проекта, там где pyproject.toml" >&2
    exit 1
fi

# rsync нужен на обоих концах, а на чистом Ubuntu его может не быть.
# Ставим до заливки: install-app.sh, который ставит пакеты, запускается
# уже после неё — и без этой строки первый же запуск упирался бы в
# «rsync: command not found» на той стороне.
echo "==> Проверяю rsync на сервере"
ssh "${USER}@${SERVER}" 'mkdir -p /opt/repairbot; command -v rsync >/dev/null || (apt-get update -qq && apt-get install -y -qq rsync)'

echo "==> Заливаю на ${USER}@${SERVER}:/opt/repairbot"
# Только те ключи, что понимает и GNU rsync, и openrsync с macOS.
# `--info=stats1` знает лишь GNU — на Маке заливка падала с usage.
rsync -az --delete \
    --exclude '.venv' \
    --exclude '.venv-pg' \
    --exclude '.pgdata' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '.ruff_cache' \
    --exclude '.mypy_cache' \
    --exclude 'preview' \
    --exclude '.DS_Store' \
    --exclude '*.egg-info' \
    `# Ничего из того, что живёт на сервере, заливка не трогает. С --delete` \
    `# любой не перечисленный здесь каталог был бы стёрт: так уже пропал` \
    `# скачанный сертификат Минцифры, которого локально нет и быть не может.` \
    --exclude '.env' \
    --exclude 'secrets' \
    --exclude 'certs' \
    ./ "${USER}@${SERVER}:/opt/repairbot/"

echo
echo "==> Залито."
echo "   Правки в коде вступят в силу только после пересборки образа:"
echo "   bash deploy/update.sh ${SERVER}"

#!/usr/bin/env bash
# Обновление боевого сервера одной командой. Запускать НА СВОЕЙ МАШИНЕ
# из корня проекта:
#
#   bash deploy/update.sh 153.80.184.45
#
# Заливает код и пересобирает образ. Пересборка обязательна: исходники
# копируются внутрь образа при сборке, а не монтируются. Без неё
# `docker compose up -d` поднимет контейнер со старым кодом — и правка
# будет выглядеть так, будто её не сделали.

set -euo pipefail

SERVER="${1:-}"
USER="${2:-root}"
if [[ -z "$SERVER" ]]; then
    echo "Укажите адрес сервера: bash deploy/update.sh 1.2.3.4" >&2
    exit 1
fi

bash "$(dirname "$0")/upload.sh" "$SERVER" "$USER"

echo "==> Пересборка и перезапуск"
ssh "${USER}@${SERVER}" \
    'cd /opt/repairbot && docker compose up -d --build && docker compose ps'

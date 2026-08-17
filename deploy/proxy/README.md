# Прокси (зарубежный сервер)

Пропускает наружу только OpenRouter, Anthropic, Google и Telegram —
список закрытый. Если кто-то доберётся до прокси, пользоваться им как
открытым он не сможет.

## Установка

```bash
mkdir -p /opt/proxy && cd /opt/proxy
# скопируйте сюда docker-compose.yml и squid.conf
```

Файл паролей создаётся один раз:

```bash
apt install -y apache2-utils
htpasswd -c /opt/proxy/passwd repairbot
```

Пароль придумайте длинный:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

```bash
docker compose up -d
```

## Закрыть от всех, кроме российского сервера

```bash
ufw allow 22/tcp
ufw allow from <IP российского сервера> to any port 3128 proto tcp
ufw --force enable
```

Пароль и ограничение по адресу — оба сразу, а не на выбор. Открытый
прокси находят сканерами за часы, и платить за чужой трафик будете вы.

## Проверка

С российского сервера:

```bash
curl -x http://repairbot:ПАРОЛЬ@<IP Германии>:3128 https://openrouter.ai/api/v1/models -o /dev/null -w "%{http_code}\n"
```

Должно вернуть `200`. А это — `403`, и так и задумано:

```bash
curl -x http://repairbot:ПАРОЛЬ@<IP Германии>:3128 https://example.com -o /dev/null -w "%{http_code}\n"
```

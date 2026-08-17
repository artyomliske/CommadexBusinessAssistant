"""Соблюдение лимита платформы MAX — 30 запросов в секунду."""

from __future__ import annotations

import asyncio
import time

from repairbot.channels.max.client import RateLimiter


async def test_burst_within_capacity_is_not_delayed():
    limiter = RateLimiter(rps=30)
    started = time.monotonic()

    await asyncio.gather(*(limiter.acquire() for _ in range(30)))

    assert time.monotonic() - started < 0.1


async def test_exceeding_capacity_is_throttled():
    limiter = RateLimiter(rps=10)
    started = time.monotonic()

    for _ in range(15):
        await limiter.acquire()

    # 15 запросов при 10 rps: первые 10 мгновенно, остальные 5 — ≈0.5 с.
    elapsed = time.monotonic() - started
    assert 0.4 < elapsed < 1.0


# --- доверенные сертификаты ---


def _self_signed(path) -> None:
    """Настоящий самоподписанный корень: подделка сюда не годится.

    Проверяем именно загрузку в хранилище TLS, а она принимает только
    разбираемый сертификат.
    """
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-root")])
    now = datetime.now(tz=UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def test_no_extra_certificates_means_default_trust(tmp_path):
    """Пустой каталог — не повод подменять системные центры."""
    from repairbot.channels.max.client import trust_context

    assert trust_context(tmp_path) is None


def test_missing_directory_is_not_an_error(tmp_path):
    from repairbot.channels.max.client import trust_context

    assert trust_context(tmp_path / "нет-такого") is None


def test_extra_certificate_is_added_to_system_ones(tmp_path):
    """Системные центры сохраняются.

    Вложения раздаются с обычного CDN: подменить весь набор одним корнем
    Минцифры значило бы сломать их скачивание, починив API.
    """
    import ssl

    from repairbot.channels.max.client import trust_context

    system_count = len(ssl.create_default_context().get_ca_certs())
    _self_signed(tmp_path / "root.crt")

    context = trust_context(tmp_path)

    assert context is not None
    assert len(context.get_ca_certs()) == system_count + 1


def test_broken_certificate_does_not_crash_startup(tmp_path):
    """Испорченный файл не должен ронять клиент при запуске."""
    from repairbot.channels.max.client import trust_context

    (tmp_path / "broken.crt").write_text("не сертификат", encoding="utf-8")

    assert trust_context(tmp_path) is not None

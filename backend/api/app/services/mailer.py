"""Отправка писем: SMTP, HTTPS API Brevo или только запись в лог.

Бэкенд выбирается переменными окружения (см. Settings.mail_backend):
- smtp  — SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS (Mailpit в docker-compose, Mailtrap, Brevo, Gmail и др.);
- brevo — BREVO_API_KEY: письма уходят по HTTPS (порт 443). Нужен там, где исходящий SMTP закрыт:
          бесплатные веб-сервисы Render блокируют порты 25, 465 и 587;
- log   — ничего не отправляется, письмо печатается в лог (разработка без почтового сервера).

Ошибки делятся на временные (сеть, перегрузка сервиса — задачу Celery стоит повторить)
и постоянные (адрес не существует, неверный ключ — повторять бессмысленно).
"""

import json
import logging
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SEC = 20


class MailError(Exception):
    pass


class TransientMailError(MailError):
    """Временный сбой: повторить позже."""


class PermanentMailError(MailError):
    """Постоянная ошибка: повтор не поможет."""


def split_sender(mail_from: str) -> tuple[str, str]:
    """«Имя <адрес>» -> (имя, адрес). email.utils.parseaddr здесь не годится: скобки в имени
    («СППО РУТ (МИИТ)») он принимает за комментарий и отбрасывает."""
    match = re.match(r'^\s*"?(.*?)"?\s*<([^<>]+)>\s*$', mail_from)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", mail_from.strip()


def mask_address(address: str) -> str:
    """a***@domain.ru — адрес целиком в логи не пишем."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def build_message(to: str, subject: str, text: str, html: str | None, sender: str) -> EmailMessage:
    message = EmailMessage()
    name, address = split_sender(sender)
    message["From"] = formataddr((name, address)) if name else address  # имя с «(»  заключается в кавычки
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=(address.partition("@")[2] or "sppo.local"))
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    return message


def _send_smtp(message: EmailMessage, cfg: Settings) -> None:
    if not cfg.smtp_host:
        raise PermanentMailError("SMTP_HOST не задан")
    try:
        if cfg.smtp_use_ssl:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT_SEC,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT_SEC)
        with server:
            server.ehlo()
            if cfg.smtp_use_starttls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_pass or "")
            server.send_message(message)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
        raise PermanentMailError(f"SMTP отклонил письмо: {type(exc).__name__}") from exc
    except smtplib.SMTPResponseException as exc:
        error = PermanentMailError if 500 <= exc.smtp_code < 600 else TransientMailError
        raise error(f"SMTP ответил {exc.smtp_code}") from exc
    except (OSError, smtplib.SMTPException) as exc:  # сеть, таймаут, обрыв соединения
        raise TransientMailError(f"SMTP недоступен: {type(exc).__name__}") from exc


def _send_brevo(to: str, subject: str, text: str, html: str | None, cfg: Settings) -> None:
    if not cfg.brevo_api_key:
        raise PermanentMailError("BREVO_API_KEY не задан")
    name, address = split_sender(cfg.mail_from)
    payload = {
        "sender": {"name": name, "email": address} if name else {"email": address},
        "to": [{"email": to}],
        "subject": subject,
        "textContent": text,
    }
    if html:
        payload["htmlContent"] = html
    request = urllib.request.Request(
        BREVO_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"api-key": cfg.brevo_api_key, "content-type": "application/json", "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC):
            return
    except urllib.error.HTTPError as exc:
        # Тело ответа Brevo содержит причину (например, неподтверждённый отправитель), но не секреты.
        reason = exc.read(300).decode("utf-8", "replace")
        error = TransientMailError if exc.code == 429 or exc.code >= 500 else PermanentMailError
        raise error(f"Brevo ответил {exc.code}: {reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TransientMailError(f"Brevo недоступен: {type(exc).__name__}") from exc


def send(to: str, subject: str, text: str, html: str | None = None, *, cfg: Settings | None = None) -> str:
    """Отправляет письмо выбранным способом. Возвращает имя использованного бэкенда."""
    cfg = cfg or get_settings()
    backend = cfg.mail_backend
    if backend == "log":
        log.info("[mail:log] to=%s subject=%r\n%s", mask_address(to), subject, text)
    elif backend == "brevo":
        _send_brevo(to, subject, text, html, cfg)
    else:
        _send_smtp(build_message(to, subject, text, html, cfg.mail_from), cfg)
    log.info("Mail sent via %s to %s: %s", backend, mask_address(to), subject)
    return backend

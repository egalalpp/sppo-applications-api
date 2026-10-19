"""Письма студенту при смене статуса заявки: принята (pending), одобрена, отклонена.

Тексты не упоминают распознавание и другие внутренние детали: студент видит только результат.
Отправка — отдельная задача Celery (notifications.send_email), чтобы не задерживать HTTP-ответ
и не терять письмо при сбое почтового сервиса (задача повторяется).
"""

import html
import logging

from starlette.concurrency import run_in_threadpool

from app import db
from app.config import get_settings
from app.domain import NOTIFY_STATUSES, AppStatus
from app.worker.celery_app import SEND_EMAIL, producer_app

log = logging.getLogger(__name__)

SIGNATURE = "Это автоматическое сообщение, отвечать на него не нужно.\nСтуденческий профсоюз РУТ (МИИТ)"

SUBJECTS = {
    AppStatus.pending: "Заявка на материальную поддержку принята",
    AppStatus.approved: "Заявка на материальную поддержку одобрена",
    AppStatus.rejected: "Заявка на материальную поддержку отклонена",
}


def application_number(application_id: str) -> str:
    """Короткий номер для писем и обращений: последние 6 символов id заявки."""
    return str(application_id)[-6:].upper()


def render(status: str, *, number: str, basis_title: str, rejection_reason: str | None = None,
           cabinet_url: str | None = None) -> tuple[str, str, str]:
    """Возвращает (тема, текст, html) письма."""
    status = AppStatus(status)
    if status not in NOTIFY_STATUSES:
        raise ValueError(f"Для статуса {status} письмо не отправляется")

    head = f"Заявка № {number} ({basis_title})"
    if status == AppStatus.pending:
        lines = [f"{head} отправлена на рассмотрение.",
                 "О решении мы сообщим по этому адресу электронной почты."]
    elif status == AppStatus.approved:
        lines = [f"{head} одобрена.",
                 "Дальнейшие сведения о выплате сообщит профком."]
    else:
        lines = [f"{head} отклонена.", f"Причина: {rejection_reason or 'не указана'}"]
    if cabinet_url:
        lines.append(f"Подробности — в личном кабинете: {cabinet_url}")

    text = "\n\n".join(["Здравствуйте!", *lines, SIGNATURE])
    paragraphs = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    body = (
        '<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#16213a">'
        '<div style="background:#1f4fbf;color:#fff;padding:16px 20px;border-radius:12px 12px 0 0;'
        'font-weight:bold">СППО РУТ (МИИТ)</div>'
        '<div style="border:1px solid #dbe3f5;border-top:0;padding:8px 20px 16px;border-radius:0 0 12px 12px">'
        f"<p>Здравствуйте!</p>{paragraphs}"
        f'<p style="color:#6b7690;font-size:13px">{html.escape(SIGNATURE).replace(chr(10), "<br>")}</p>'
        "</div></div>"
    )
    return SUBJECTS[status], text, body


async def recipient_for(application: dict) -> str | None:
    """Адрес из формы заявки; если его нет — почта учётной записи автора."""
    email = (application.get("student") or {}).get("email")
    if email:
        return email
    user = await db.get_db()[db.USERS].find_one({"_id": application["applicant_user_id"]}, {"email": 1})
    return user["email"] if user else None


async def notify_status(application: dict, status: str, basis_title: str) -> bool:
    """Ставит письмо в очередь. Уведомление — дополнительная функция: ошибка здесь
    не должна ломать смену статуса, поэтому исключения только пишутся в лог."""
    try:
        if AppStatus(status) not in NOTIFY_STATUSES:
            return False
        to = await recipient_for(application)
        if not to:
            log.warning("No recipient for application %s", application["_id"])
            return False
        frontend = get_settings().frontend_url
        subject, text, body = render(
            status,
            number=application_number(application["_id"]),
            basis_title=basis_title,
            rejection_reason=application.get("rejection_reason"),
            cabinet_url=f"{frontend.rstrip('/')}/cabinet.html" if frontend else None,
        )
        await run_in_threadpool(producer_app.send_task, SEND_EMAIL, args=[to, subject, text, body])
        return True
    except Exception:  # noqa: BLE001
        log.exception("Failed to enqueue %s notification for %s", status, application.get("_id"))
        return False

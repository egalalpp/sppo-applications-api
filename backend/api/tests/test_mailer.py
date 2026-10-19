import io
import json
import smtplib
import urllib.error

import pytest

from app.config import Settings
from app.services import mailer

BASE = dict(jwt_secret="x" * 32, s3_access_key="a", s3_secret_key="b", mongo_app_user="u",
            mongo_app_password="p", redis_password="r")


def cfg(**over) -> Settings:
    return Settings(**{**BASE, **over})


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls, self.ssl = host, port, [], context is not None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.calls.append(("send", message["To"], message["Subject"]))
        self.message = message


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances.clear()
    FakeSMTP.fail_with = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)


def test_backend_selection():
    assert cfg().mail_backend == "log"
    assert cfg(smtp_host="mailpit").mail_backend == "smtp"
    assert cfg(smtp_host="mailpit", brevo_api_key="k").mail_backend == "brevo"
    assert cfg(smtp_host="mailpit", email_backend="log").mail_backend == "log"


def test_tls_defaults():
    assert not cfg(smtp_host="mailpit", smtp_port=1025).smtp_use_starttls  # локальный Mailpit: без логина и TLS
    assert cfg(smtp_host="h", smtp_user="u").smtp_use_starttls
    assert cfg(smtp_host="h", smtp_port=465).smtp_use_ssl


def test_local_smtp_without_auth():
    c = cfg(smtp_host="mailpit", smtp_port=1025)
    assert mailer.send("student@rut.ru", "Тема", "Текст", "<p>Текст</p>", cfg=c) == "smtp"
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("mailpit", 1025)
    assert "starttls" not in smtp.calls and not any(isinstance(x, tuple) and x[0] == "login" for x in smtp.calls)
    assert smtp.message.is_multipart()  # текст + html


def test_smtp_with_credentials_uses_starttls_and_login():
    c = cfg(smtp_host="smtp-relay.example.com", smtp_user="user", smtp_pass="secret")
    mailer.send("a@b.ru", "S", "T", cfg=c)
    calls = FakeSMTP.instances[0].calls
    assert "starttls" in calls and ("login", "user", "secret") in calls
    assert calls.index("starttls") < calls.index(("login", "user", "secret"))


def test_sender_name_with_parentheses_is_kept():
    """«СППО РУТ (МИИТ)»: скобки в имени не должны пропадать как «комментарий» заголовка."""
    from email.utils import parseaddr

    assert mailer.split_sender("СППО РУТ (МИИТ) <noreply@rut.ru>") == ("СППО РУТ (МИИТ)", "noreply@rut.ru")
    assert mailer.split_sender('"СППО РУТ (МИИТ)" <noreply@rut.ru>') == ("СППО РУТ (МИИТ)", "noreply@rut.ru")
    assert mailer.split_sender("noreply@rut.ru") == ("", "noreply@rut.ru")
    mailer.send("a@b.ru", "S", "T", cfg=cfg(smtp_host="h", mail_from="СППО РУТ (МИИТ) <noreply@rut.ru>"))
    assert parseaddr(str(FakeSMTP.instances[0].message["From"])) == ("СППО РУТ (МИИТ)", "noreply@rut.ru")


def test_smtp_ssl_on_465():
    mailer.send("a@b.ru", "S", "T", cfg=cfg(smtp_host="h", smtp_port=465, smtp_user="u", smtp_pass="p"))
    smtp = FakeSMTP.instances[0]
    assert smtp.ssl and "starttls" not in smtp.calls


@pytest.mark.parametrize("error,expected", [
    (smtplib.SMTPRecipientsRefused({}), mailer.PermanentMailError),
    (smtplib.SMTPAuthenticationError(535, b"bad credentials"), mailer.PermanentMailError),
    (smtplib.SMTPDataError(554, b"rejected"), mailer.PermanentMailError),
    (smtplib.SMTPDataError(451, b"try later"), mailer.TransientMailError),
    (ConnectionRefusedError(), mailer.TransientMailError),
    (TimeoutError(), mailer.TransientMailError),
])
def test_smtp_error_classification(error, expected):
    FakeSMTP.fail_with = error
    with pytest.raises(expected):
        mailer.send("a@b.ru", "S", "T", cfg=cfg(smtp_host="h"))


def test_smtp_without_host_is_permanent():
    with pytest.raises(mailer.PermanentMailError):
        mailer.send("a@b.ru", "S", "T", cfg=cfg(email_backend="smtp"))


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_brevo_request(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    c = cfg(brevo_api_key="KEY", mail_from="СППО РУТ (МИИТ) <noreply@rut.ru>")
    assert mailer.send("student@rut.ru", "Тема", "Текст", "<p>Текст</p>", cfg=c) == "brevo"
    assert seen["url"] == "https://api.brevo.com/v3/smtp/email"
    assert seen["headers"]["Api-key"] == "KEY"
    assert seen["body"] == {"sender": {"name": "СППО РУТ (МИИТ)", "email": "noreply@rut.ru"},
                            "to": [{"email": "student@rut.ru"}], "subject": "Тема",
                            "textContent": "Текст", "htmlContent": "<p>Текст</p>"}


@pytest.mark.parametrize("code,expected", [
    (400, mailer.PermanentMailError), (401, mailer.PermanentMailError),
    (429, mailer.TransientMailError), (503, mailer.TransientMailError),
])
def test_brevo_http_errors(monkeypatch, code, expected):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "err", {}, io.BytesIO(b'{"message":"sender not verified"}'))

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(expected):
        mailer.send("a@b.ru", "S", "T", cfg=cfg(brevo_api_key="KEY"))


def test_brevo_network_error_is_transient(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(mailer.TransientMailError):
        mailer.send("a@b.ru", "S", "T", cfg=cfg(brevo_api_key="KEY"))


def test_log_backend(caplog):
    with caplog.at_level("INFO"):
        assert mailer.send("student@rut.ru", "Тема", "Тело письма", cfg=cfg()) == "log"
    assert "s***@rut.ru" in caplog.text and "student@rut.ru" not in caplog.text  # адрес в логе замаскирован

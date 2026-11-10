"""The email notifier end to end against a minimal local SMTP server (implemented here, since
aiosmtpd is not a dependency): STARTTLS, implicit TLS, login, HTML and text bodies, the summary
attachment, retries, and refusals. Certificates are generated for each test run."""

from __future__ import annotations

import base64
import datetime as dt
import email
import ipaddress
import socket
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from dvf.config.notifications import EmailNotification
from dvf.packs.integrations.dispatch import Channel, Dispatcher
from dvf.packs.integrations.messages import MessageContext, build_message
from dvf.plugins.registry import Registry
from tests.unit.reporting_fixtures import build_run

USER = "dq-bot@example.test"
PASSWORD = "synthetic-password-0001"


@pytest.fixture(autouse=True)
def _fast_fqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    """smtplib looks up the client's name with socket.getfqdn() after connecting and before
    EHLO, and for a name without a dot also with socket.gethostbyname(socket.gethostname()).
    On macOS CI runners those lookups can take longer than the test server waits, which then
    drops the connection; these tests are about dvf, not name resolution. A dotted name skips
    both lookups."""
    monkeypatch.setattr(socket, "getfqdn", lambda name="": "client.example.test")


# ---------------------------------------------------------------------- certificates


@dataclass(frozen=True)
class Certificates:
    ca: Path
    cert: Path
    key: Path


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Certificates:
    """A throwaway CA and a server certificate for 127.0.0.1 and localhost."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder = tmp_path_factory.mktemp("tls")
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dvf test CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    paths = Certificates(folder / "ca.pem", folder / "server.pem", folder / "server.key")
    paths.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    paths.key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


# ---------------------------------------------------------------------- SMTP server


@dataclass
class Received:
    mail_from: str
    recipients: list[str]
    data: bytes
    user: str | None
    tls: bool

    def message(self) -> Message:
        return email.message_from_bytes(self.data)


@dataclass
class SmtpServer:
    """Just enough ESMTP (RFC 5321) for smtplib: EHLO, STARTTLS, AUTH PLAIN/LOGIN, MAIL, RCPT,
    DATA, RSET, NOOP, QUIT."""

    context: ssl.SSLContext | None = None
    implicit_tls: bool = False
    starttls: bool = True
    busy_connections: int = 0
    """Answer this many connections with ``421`` (service not available) and close them."""
    received: list[Received] = field(default_factory=list)
    connections: int = 0

    def __post_init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._accept = threading.Thread(target=self._serve, daemon=True)

    @property
    def port(self) -> int:
        port: int = self._sock.getsockname()[1]
        return port

    def __enter__(self) -> SmtpServer:
        self._accept.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._accept.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._session, args=(conn,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _session(self, conn: socket.socket) -> None:
        self.connections += 1
        conn.settimeout(10)
        tls = False
        try:
            if self.busy_connections:
                self.busy_connections -= 1
                conn.sendall(b"421 4.3.2 Service not available, try again later\r\n")
                return
            if self.implicit_tls:
                assert self.context is not None
                conn = self.context.wrap_socket(conn, server_side=True)
                tls = True
            reader = conn.makefile("rb")
            conn.sendall(b"220 localhost ESMTP dvf-test\r\n")
            user: str | None = None
            mail_from = ""
            recipients: list[str] = []
            while True:
                line = reader.readline()
                if not line:
                    return
                command = line.decode("ascii", "replace").rstrip("\r\n")
                verb = command.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    lines = ["localhost", "AUTH PLAIN LOGIN", "SIZE 10485760"]
                    if self.starttls and not tls and self.context is not None:
                        lines.append("STARTTLS")
                    reply = "".join(f"250-{x}\r\n" for x in lines[:-1]) + f"250 {lines[-1]}\r\n"
                    conn.sendall(reply.encode())
                elif verb == "STARTTLS":
                    assert self.context is not None
                    conn.sendall(b"220 2.0.0 Ready to start TLS\r\n")
                    reader.close()
                    conn = self.context.wrap_socket(conn, server_side=True)
                    reader = conn.makefile("rb")
                    tls = True
                elif verb == "AUTH":
                    parts = command.split(" ")
                    if parts[1].upper() == "PLAIN":
                        token = parts[2] if len(parts) > 2 else ""
                        if not token:
                            conn.sendall(b"334 \r\n")
                            token = reader.readline().decode().strip()
                        _, name, secret = base64.b64decode(token).decode().split("\0")
                    else:
                        conn.sendall(b"334 VXNlcm5hbWU6\r\n")
                        name = base64.b64decode(reader.readline().strip()).decode()
                        conn.sendall(b"334 UGFzc3dvcmQ6\r\n")
                        secret = base64.b64decode(reader.readline().strip()).decode()
                    if (name, secret) == (USER, PASSWORD):
                        user = name
                        conn.sendall(b"235 2.7.0 Authentication successful\r\n")
                    else:
                        conn.sendall(b"535 5.7.8 Authentication credentials invalid\r\n")
                elif verb == "MAIL":
                    mail_from = command.split(":", 1)[1].split()[0].strip("<>")
                    recipients = []
                    conn.sendall(b"250 2.1.0 OK\r\n")
                elif verb == "RCPT":
                    address = command.split(":", 1)[1].strip().strip("<>")
                    if address.startswith("reject"):
                        conn.sendall(b"550 5.1.1 No such user\r\n")
                    else:
                        recipients.append(address)
                        conn.sendall(b"250 2.1.5 OK\r\n")
                elif verb == "DATA":
                    conn.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    chunks: list[bytes] = []
                    while True:
                        data_line = reader.readline()
                        if data_line in (b".\r\n", b".\n", b""):
                            break
                        chunks.append(data_line[1:] if data_line.startswith(b"..") else data_line)
                    self.received.append(
                        Received(mail_from, recipients, b"".join(chunks), user, tls)
                    )
                    conn.sendall(b"250 2.0.0 Queued\r\n")
                elif verb in ("RSET", "NOOP"):
                    conn.sendall(b"250 2.0.0 OK\r\n")
                elif verb == "QUIT":
                    conn.sendall(b"221 2.0.0 Bye\r\n")
                    return
                else:
                    conn.sendall(b"502 5.5.2 Command not recognized\r\n")
        except (OSError, ssl.SSLError):
            return
        finally:
            conn.close()


def _server_context(certificates: Certificates) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates.cert, certificates.key)
    return context


@pytest.fixture
def run() -> Any:
    result = build_run()
    result.summary = result.summarize()
    return result


def _send(config: EmailNotification, run: Any, registry: Registry, **context: Any) -> Any:
    message = build_message(run, config, MessageContext(**context))
    dispatcher = Dispatcher(registry, sleep=lambda _: None)
    return dispatcher.deliver(Channel("email", config), message)


def _config(port: int, **fields: Any) -> EmailNotification:
    return EmailNotification.model_validate(
        {
            "type": "email",
            "recipients": ["qa@example.test", "lead@example.test"],
            "sender": "dvf@example.test",
            "smtp_host": "127.0.0.1",
            "smtp_port": port,
            "timeout_s": 5,
            **fields,
        }
    )


def _bodies(message: Message) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        name = part.get_filename() or part.get_content_type()
        bodies[name] = payload.decode(part.get_content_charset() or "utf-8")
    return bodies


# ---------------------------------------------------------------------- tests


def test_starttls_login_and_both_bodies(
    certificates: Certificates, run: Any, registry: Registry
) -> None:
    with SmtpServer(context=_server_context(certificates)) as server:
        config = _config(
            server.port,
            username=USER,
            password=PASSWORD,
            ca_file=str(certificates.ca),
            report_url="https://ci.example.test/r/report.html",
            include_samples=True,
            max_failures=50,
        )
        delivery = _send(config, run, registry)
    assert delivery.ok, delivery.error
    received = server.received[0]
    assert received.tls and received.user == USER
    assert received.mail_from == "dvf@example.test"
    assert received.recipients == ["qa@example.test", "lead@example.test"]
    message = received.message()
    assert message["Subject"] == "[dvf] synthetic-migration: ERROR"
    assert message["To"] == "qa@example.test, lead@example.test"
    bodies = _bodies(message)
    assert "Top failures:" in bodies["text/plain"]
    html = bodies["text/html"]
    assert "<h2" in html and 'href="https://ci.example.test/r/report.html"' in html
    assert "<script>" not in html and "&lt;script&gt;" in html  # sample values are escaped


def test_implicit_tls(certificates: Certificates, run: Any, registry: Registry) -> None:
    with SmtpServer(context=_server_context(certificates), implicit_tls=True) as server:
        config = _config(
            server.port,
            security="ssl",
            username=USER,
            password=PASSWORD,
            ca_file=str(certificates.ca),
        )
        delivery = _send(config, run, registry)
    assert delivery.ok, delivery.error
    assert server.received[0].tls and server.received[0].user == USER


def test_untrusted_certificate_is_refused(
    certificates: Certificates, run: Any, registry: Registry
) -> None:
    with SmtpServer(context=_server_context(certificates)) as server:
        delivery = _send(_config(server.port, retries=2), run, registry)
    assert not delivery.ok and delivery.attempts == 1  # a TLS error is not retried
    assert "SSL" in (delivery.error or "") or "certificate" in (delivery.error or "").lower()
    assert server.received == []


def test_plain_relay_with_summary_attachment(run: Any, registry: Registry) -> None:
    with SmtpServer(context=None, starttls=False) as server:
        config = _config(server.port, security="none", attach_summary=True)
        message = build_message(run, config, MessageContext())
        message = message.__class__(**{**message.__dict__, "summary_markdown": "# Summary\n"})
        delivery = Dispatcher(registry).deliver(Channel("email", config), message)
    assert delivery.ok, delivery.error
    received = server.received[0]
    assert not received.tls and received.user is None
    assert _bodies(received.message())["summary.md"] == "# Summary\n"


def test_password_is_never_sent_unencrypted(run: Any, registry: Registry) -> None:
    with SmtpServer(starttls=False) as server:
        config = _config(server.port, security="none", username=USER, password=PASSWORD)
        delivery = _send(config, run, registry)
    assert not delivery.ok and "unencrypted" in (delivery.error or "")
    assert server.connections == 0


def test_missing_starttls_is_an_error(run: Any, registry: Registry) -> None:
    with SmtpServer(starttls=False) as server:
        delivery = _send(_config(server.port), run, registry)
    assert not delivery.ok and "does not offer STARTTLS" in (delivery.error or "")


def test_busy_server_is_retried(certificates: Certificates, run: Any, registry: Registry) -> None:
    with SmtpServer(context=_server_context(certificates), busy_connections=1) as server:
        config = _config(server.port, ca_file=str(certificates.ca), retries=2)
        delivery = _send(config, run, registry)
    assert delivery.ok and delivery.attempts == 2, delivery.error
    assert len(server.received) == 1


def test_wrong_password_is_not_retried_or_logged(
    certificates: Certificates,
    run: Any,
    registry: Registry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with SmtpServer(context=_server_context(certificates)) as server:
        config = _config(
            server.port,
            username=USER,
            password="synthetic-wrong-password",
            ca_file=str(certificates.ca),
            retries=3,
        )
        delivery = _send(config, run, registry)
    assert not delivery.ok and delivery.attempts == 1
    assert "SMTPAuthenticationError" in (delivery.error or "")
    assert "synthetic-wrong-password" not in caplog.text + (delivery.error or "")


def test_refused_recipients_are_reported(
    certificates: Certificates, run: Any, registry: Registry
) -> None:
    with SmtpServer(context=_server_context(certificates)) as server:
        config = _config(server.port, ca_file=str(certificates.ca))
        config = config.model_copy(
            update={"recipients": ["qa@example.test", "reject@example.test"]}
        )
        delivery = _send(config, run, registry)
    assert not delivery.ok and "reject@example.test" in (delivery.error or "")
    assert server.received[0].recipients == ["qa@example.test"]


def test_counts_only_email_has_no_messages(
    certificates: Certificates, run: Any, registry: Registry
) -> None:
    with SmtpServer(context=_server_context(certificates)) as server:
        config = _config(server.port, ca_file=str(certificates.ca), counts_only=True)
        delivery = _send(config, run, registry)
    assert delivery.ok, delivery.error
    bodies = _bodies(server.received[0].message())
    assert "38 rows differ" not in bodies["text/plain"] + bodies["text/html"]
    assert "sales_migration.customers.column_compare" in bodies["text/plain"]


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    yield

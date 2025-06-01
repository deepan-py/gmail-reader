from __future__ import annotations
import os
import datetime
from typing import Optional, List
from sqlalchemy import String, DateTime, ForeignKey, ARRAY, Boolean, Index
from sqlalchemy.dialects.postgresql import ARRAY as PostgresArray
from sqlalchemy.orm import Session, DeclarativeBase, Mapped, mapped_column, MappedAsDataclass, relationship
import email.utils as email_utils
import base64


class Base(DeclarativeBase, MappedAsDataclass):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True, kw_only=True)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default_factory=datetime.datetime.utcnow, kw_only=True
    )
    mail_count: Mapped[int] = mapped_column(default=0, nullable=False, kw_only=True)
    gmail_token: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default=None, comment="OAuth token for Gmail API", kw_only=True
    )
    last_synced: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True, default=None, comment="Last time the user was synced", kw_only=True
    )
    latest_fetched_mail_date: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=True, default=None, comment="Last fetched mail date", kw_only=True
    )
    is_first_mail_fetched: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="Flag to check if the first mail is fetched", kw_only=True
    )

    def __repr__(self):
        return f"<User(id={self.id}, email={self.email}, name={self.name})>"

    # Fetch user from mail
    @classmethod
    def from_email(cls, email: str, db: Session) -> Optional[User]:
        return db.query(cls).filter(cls.email == email).first()


class Mail(Base):
    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    labels: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True, default=None)
    sent_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True, default=None)
    received_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True, default=None)
    sent_to: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True, default=None)
    sent_cc: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True, default=None)
    sent_bcc: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True, default=None)
    internal_date: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime,
        nullable=False,
        default=None,
        comment="Internal date of the email for Gmail API",
        index=True,
    )
    from_email: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default=None, comment="Email address of the sender", index=True
    )
    all_sent_to: Mapped[Optional[List[str]]] = mapped_column(
        PostgresArray(String),
        nullable=True,
        default=None,
        comment="All sent to addresses including CC and BCC",
    )

    @classmethod
    def from_message(cls, message: dict, user_id: int) -> Optional[Mail]:
        labels = message.get("labelIds", [])
        if "DRAFT" in labels:
            # Skip draft emails
            return None
        if "TRASH" in labels:
            # Skip emails in trash
            return None
        if "SPAM" in labels:
            # Skip spam emails
            return None
        id = message.get("id")
        thread_id = message.get("threadId")
        headers = {header["name"].lower(): header["value"] for header in message.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        body = ""
        sent_at = None
        received_at = None
        sent_to = []
        sent_cc = []
        sent_bcc = []
        internal_date = datetime.datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000.0)
        from_email = email_utils.parseaddr(headers.get("from", ""))[1].strip() if headers.get("from") else None
        if from_email:
            from_email = from_email.lower()
        if "SENT" in labels:
            sent_at_str = headers.get("date", None)
            if sent_at_str:
                sent_at = datetime.datetime.strptime(sent_at_str, "%a, %d %b %Y %H:%M:%S %z")
            to_addresses = email_utils.getaddresses([headers.get("to", "")])
            cc_addresses = email_utils.getaddresses([headers.get("cc", "")])
            bcc_addresses = email_utils.getaddresses([headers.get("bcc", "")])
            sent_to = [addr[1].strip() for addr in to_addresses if addr[1]]
            sent_cc = [addr[1].strip() for addr in cc_addresses if addr[1]]
            sent_bcc = [addr[1].strip() for addr in bcc_addresses if addr[1]]

        else:
            # Consider it as received mail
            received_at = internal_date
            if "UNREAD" not in labels:
                # If the email is not unread, it is considered read
                labels.append("READ")
        mime_type = message.get("payload", {}).get("mimeType", "")
        if "multipart/alternative" in mime_type.lower():
            parts = message.get("payload", {}).get("parts", [])
            for part in parts:
                if part.get("mimeType", "") == "text/plain":
                    body = part.get("body", {}).get("data", "")
                    if body:
                        body = base64.urlsafe_b64decode(body).decode("utf-8")
                elif part.get("mimeType", "") == "multipart/alternative":
                    # Handle multipart emails
                    for subpart in part.get("parts", []):
                        if subpart.get("mimeType", "") == "text/plain":
                            body = subpart.get("body", {}).get("data", "")
                            if body:
                                body = base64.urlsafe_b64decode(body).decode("utf-8")
        elif "text/plain" in mime_type.lower() or "text/html" in mime_type.lower():
            body_data = message.get("payload", {}).get("body", {}).get("data", "")
            if body_data:
                try:
                    body = base64.urlsafe_b64decode(body_data).decode("utf-8")
                except Exception as e:
                    print(f"Error decoding email body: {e}")
                    body = body_data  # Fallback to raw data if decoding fails

        return Mail(
            id=id,
            thread_id=thread_id,
            user_id=user_id,
            subject=subject,
            body=body,
            labels=labels,
            sent_at=sent_at,
            received_at=received_at,
            sent_to=sent_to,
            sent_cc=sent_cc,
            sent_bcc=sent_bcc,
            internal_date=internal_date,
            from_email=from_email,
            all_sent_to=list(set(sent_to + sent_cc + sent_bcc)) if sent_to or sent_cc or sent_bcc else None,
        )


# Indexes for faster queries
# Label index
Index("idx_emails_labels", Mail.labels, postgresql_using="gin")
# User index
Index("idx_emails_user_id", Mail.user_id)
# to email index
Index("idx_emails_all_sent_to", Mail.all_sent_to, postgresql_using="gin")


def get_database_url():
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "gmail_filter")
    db_user = os.getenv("DB_USER", "postgres")
    db_pass = os.getenv("DB_PASSWORD", "postgres")

    return f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"


def _init_db() -> Session:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(get_database_url())
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    return session()


def init_db() -> Session:
    """Initialize the database and return a session."""
    session = _init_db()
    return session

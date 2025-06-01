from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gmail_reader.database.models import User, Mail

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class GmailClient:
    # OAuth 2.0 scopes for Gmail API
    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

    def __init__(self, email: str, name: str, db: Session, creds_path: Optional[Path] = None) -> None:
        self.email = email.strip().lower()
        self.name = name.strip()
        self.db = db
        self.creds_path = creds_path
        self.service = None
        self.user = None
        if not self.setup_user_client():
            raise ValueError("Failed to set up Gmail client. Please check your credentials or configuration.")

    def setup_user_client(self) -> bool:
        user = User.from_email(self.email, self.db)
        if not user:
            user = User(email=self.email, name=self.name, gmail_token=None)
        creds = None
        try:
            if user.gmail_token:
                gmail_token = json.loads(user.gmail_token)
                creds = Credentials.from_authorized_user_info(gmail_token, self.SCOPES)
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logging.error(f"Error refreshing credentials: {e}")
                    return False
            if not creds or not creds.valid:
                if not self.creds_path or not self.creds_path.exists():
                    return False
                flow = InstalledAppFlow.from_client_secrets_file(str(self.creds_path), self.SCOPES)
                creds = flow.run_local_server(port=0)
        except (FileNotFoundError, HttpError, json.JSONDecodeError) as e:
            logging.error(f"Error setting up Gmail client: {e}")
            return False
        user.gmail_token = creds.to_json()
        self.db.add(user)
        self.db.commit()
        self.service = build("gmail", "v1", credentials=creds)
        self.user = user
        return True

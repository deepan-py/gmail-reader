from __future__ import annotations
from gmail_reader.gmail_service.auth import GmailClient

from enum import Enum
import logging
import datetime
import re
from dataclasses import dataclass
from gmail_reader.database.models import Mail
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from googleapiclient.errors import HttpError

from sqlalchemy import text, or_, func
from sqlalchemy.sql import exists, select


LABEL_REGEX = re.compile(r"^[a-zA-Z0-9\-_]+$")
RESERVED_LABELS = {"READ", "UNREAD", "SPAM", "TRASH", "DRAFTS", "SENT", "INBOX"}


class MailService:
    def __init__(self, gmail_client: GmailClient) -> None:
        self.gmail_client = gmail_client
        self.service = gmail_client.service
        self.user = gmail_client.user
        self.db: Session = gmail_client.db

    @classmethod
    def _fetch_mail_content(cls, message_id: str, service: Any) -> dict[str, Any]:
        return service.users().messages().get(userId="me", id=message_id).execute()

    def save_mail(self, message_id: str) -> None:
        mail_content = self._fetch_mail_content(message_id, self.service)
        if mail_content:
            mail = Mail.from_message(mail_content, self.user.id)
            if not mail:
                logging.error(f"Failed to parse mail content for message ID: {message_id} {mail_content}")
                return
            # insert the record if not exists
            existing_mail = self.db.query(Mail).filter(Mail.id == mail.id).first()
            if not existing_mail:
                self.db.add(mail)
                self.user.mail_count += 1
                self.user.last_synced = mail.received_at or mail.sent_at
                self.db.commit()

    def _fetch_first_mails(self) -> None:
        messages = self.service.users().messages().list(userId="me", maxResults=500).execute()
        latest_message = None
        for message in messages.get("messages", []):
            if latest_message is None:
                latest_message = message
            self.save_mail(message.get("id"))
        while messages.get("nextPageToken"):
            messages = (
                self.service.users()
                .messages()
                .list(userId="me", pageToken=messages["nextPageToken"], maxResults=500)
                .execute()
            )
            for message in messages.get("messages", []):
                self.save_mail(message.get("id"))
        self.user.is_first_mail_fetched = True
        if latest_message:
            latest_message_content = self._fetch_mail_content(latest_message.get("id"), self.service)
            if latest_message_content:
                self.user.latest_fetched_mail_date = datetime.datetime.fromtimestamp(
                    int(latest_message_content["internalDate"]) / 1000
                )
                logging.info(
                    f"Latest fetched mail date set to: {self.user.latest_fetched_mail_date} for user {self.user.email}"
                )
        else:
            # Fetch the latest mail date from the database
            latest_mail = (
                self.db.query(Mail).filter(Mail.user_id == self.user.id).order_by(Mail.internal_date.desc()).first()
            )
            if latest_mail and latest_mail.internal_date:
                self.user.latest_fetched_mail_date = latest_mail.internal_date
                logging.info(
                    f"Latest fetched mail date set to: {self.user.latest_fetched_mail_date} for user {self.user.email}"
                )
        self.db.commit()
        return

    def fetch_and_store_email(self) -> None:
        if not self.service or not self.user:
            raise ValueError("Gmail client is not properly initialized.")
        try:
            if not self.user.is_first_mail_fetched:
                self._fetch_first_mails()
                return
            get_records_after = self.user.latest_fetched_mail_date
            print("Records after:", get_records_after)
            messages = (
                self.service.users()
                .messages()
                .list(userId="me", q=f"after:{get_records_after.strftime('%Y/%m/%d')}", maxResults=500)
                .execute()
            )
            for message in messages.get("messages", []):
                self.save_mail(message.get("id"))
            while messages.get("nextPageToken"):
                messages = (
                    self.service.users()
                    .messages()
                    .list(userId="me", pageToken=messages["nextPageToken"], maxResults=500)
                    .execute()
                )
                for message in messages.get("messages", []):
                    self.save_mail(message.get("id"))
        except HttpError as e:
            logging.error(f"An error occurred while fetching emails: {e.resp.status} - {e}")
        except Exception as e:
            logging.exception(f"An error occurred while fetching emails: {e}")


class EmailRuleMatchType(Enum):
    All = "all"
    Any = "any"


class EmailRuleActionType(Enum):
    MoveToFolder = "move_to_folder"
    MarkAsRead = "mark_as_read"
    MarkAsUnread = "mark_as_unread"


class EmailRuleConditionType(Enum):
    From = "from"
    To = "to"  # this includes all recipients, not just the primary recipient
    Subject = "subject"
    Body = "body"
    ReceivedAt = "received_at"
    ReceivedAtDelta = "received_at_delta"  # Days/Months


class EmailRuleCheckType(Enum):
    Contains = "contains"
    NotContains = "not_contains"
    Equals = "equals"
    NotEquals = "not_equals"
    LessThan = "less_than"
    GreaterThan = "greater_than"


@dataclass
class EmailCondition:
    type: EmailRuleConditionType
    check: EmailRuleCheckType
    value: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EmailCondition:
        return cls(
            type=EmailRuleConditionType(data["type"]), check=EmailRuleCheckType(data["check"]), value=data["value"]
        )


@dataclass
class EmailAction:
    type: EmailRuleActionType
    value: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EmailAction:
        return cls(type=EmailRuleActionType(data["type"]), value=data.get("value"))


@dataclass
class Rule:
    name: str
    match_type: EmailRuleMatchType
    conditions: List[EmailCondition]
    actions: List[EmailAction]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Rule:
        return cls(
            name=data["name"],
            match_type=EmailRuleMatchType(data["match_type"]),
            conditions=[EmailCondition.from_dict(cond) for cond in data["conditions"]],
            actions=[EmailAction.from_dict(act) for act in data["actions"]],
        )


class MailRuleExecution:
    def __init__(self, rules_path: str, db: Session) -> None:
        self.rules_path = rules_path
        self.db = db
        self.rules: list[Rule] = self.load_rules()

    def load_rules(self) -> List[Rule]:
        import json

        with open(self.rules_path, "r") as file:
            rules_data = json.load(file)
        return [Rule.from_dict(rule) for rule in rules_data.get("rules", [])]

    @classmethod
    def date_parser(cls, date_str: str) -> datetime.datetime:
        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                return datetime.datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        raise ValueError(f"Date string '{date_str}' does not match any expected format {formats}.")

    @classmethod
    def validate_label(cls, label: str) -> None:
        if not label or len(label) > 150:
            raise ValueError(f"Label '{label}' is invalid. It must be non-empty and less than 150 characters.")
        if not LABEL_REGEX.match(label):
            raise ValueError(
                f"Label '{label}' is invalid. It can only contain alphanumeric characters, hyphens, and underscores."
            )

    def execute_rules(self) -> None:
        for rule in self.rules:
            # Prepare the query based on the rule conditions
            for action in rule.actions:
                if action.type == EmailRuleActionType.MoveToFolder:
                    self.validate_label(action.value)
                elif action.type in [EmailRuleActionType.MarkAsRead, EmailRuleActionType.MarkAsUnread]:
                    continue
                else:
                    raise ValueError(f"Unsupported action type: {action.type} in rule '{rule.name}'")

            query = self.db.query(Mail)
            if rule.match_type == EmailRuleMatchType.All:
                for cond in rule.conditions:
                    logging.debug(f"Condition: {cond}")
                    if cond.type == EmailRuleConditionType.From:
                        if cond.check == EmailRuleCheckType.Contains:
                            query = query.filter(Mail.from_email.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.NotContains:
                            query = query.filter(~Mail.from_email.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.Equals:
                            query = query.filter(Mail.from_email == cond.value)
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            query = query.filter(Mail.from_email != cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.To:
                        if cond.check == EmailRuleCheckType.Contains:
                            query = query.filter(
                                exists(
                                    select(1)
                                    .select_from(text("unnest(all_sent_to) as temp"))
                                    .where(text("temp LIKE :value"))
                                    .scalar_subquery()
                                ).params(value=f"%{cond.value}%")
                            )
                        elif cond.check == EmailRuleCheckType.NotContains:
                            query = query.filter(
                                ~exists(
                                    select(1)
                                    .select_from(text("unnest(all_sent_to) as temp"))
                                    .where(text("temp LIKE :value"))
                                    .scalar_subquery()
                                ).params(value=f"%{cond.value}%")
                            )
                        elif cond.check == EmailRuleCheckType.Equals:
                            query = query.filter(Mail.all_sent_to.contains([cond.value]))
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            query = query.filter(~Mail.all_sent_to.contains([cond.value]))
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.Subject:
                        if cond.check == EmailRuleCheckType.Contains:
                            query = query.filter(Mail.subject.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.NotContains:
                            query = query.filter(~Mail.subject.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.Equals:
                            query = query.filter(Mail.subject == cond.value)
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            query = query.filter(Mail.subject != cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.Body:
                        if cond.check == EmailRuleCheckType.Contains:
                            query = query.filter(Mail.body.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.NotContains:
                            query = query.filter(~Mail.body.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.Equals:
                            query = query.filter(Mail.body == cond.value)
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            query = query.filter(Mail.body != cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.ReceivedAt:
                        self.date_parser(cond.value)  # Validate date format
                        if cond.check == EmailRuleCheckType.LessThan:
                            query = query.filter(Mail.received_at < cond.value)
                        elif cond.check == EmailRuleCheckType.GreaterThan:
                            query = query.filter(Mail.received_at > cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.ReceivedAtDelta:
                        try:
                            value, type_ = cond.value.split(" ")
                            value = int(value)
                            if type_ not in ["days", "months"]:
                                raise ValueError(f"Unsupported time unit: {type_}. Use 'days' or 'months'.")
                            if cond.check == EmailRuleCheckType.GreaterThan:
                                query = query.filter(
                                    Mail.received_at < func.now() - text(f"INTERVAL '{value} {type_}'")
                                )
                            elif cond.check == EmailRuleCheckType.LessThan:
                                query = query.filter(
                                    Mail.received_at > func.now() - text(f"INTERVAL '{value} {type_}'")
                                )
                            else:
                                raise ValueError(
                                    f"Unsupported check type: {cond.check} for condition type: {cond.type}"
                                )
                        except ValueError as e:
                            logging.error(f"Error parsing ReceivedAtDelta condition value '{cond.value}': {e}")
                            raise e

                    else:
                        raise ValueError(f"Unsupported condition type: {cond.type}")

            elif rule.match_type == EmailRuleMatchType.Any:
                or_conditions = []
                for cond in rule.conditions:
                    if cond.type == EmailRuleConditionType.From:
                        if cond.check == EmailRuleCheckType.Contains:
                            or_conditions.append(Mail.from_email.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.NotContains:
                            or_conditions.append(~Mail.from_email.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.Equals:
                            or_conditions.append(Mail.from_email == cond.value)
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            or_conditions.append(Mail.from_email != cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.To:
                        if cond.check == EmailRuleCheckType.Contains:
                            or_conditions.append(
                                exists(
                                    select(1)
                                    .select_from(text("unnest(all_sent_to) as temp"))
                                    .where(text("temp LIKE :value"))
                                    .scalar_subquery()
                                ).params(value=f"%{cond.value}%")
                            )
                        elif cond.check == EmailRuleCheckType.NotContains:
                            or_conditions.append(
                                ~exists(
                                    select(1)
                                    .select_from(text("unnest(all_sent_to) as temp"))
                                    .where(text("temp LIKE :value"))
                                    .scalar_subquery()
                                ).params(value=f"%{cond.value}%")
                            )
                        elif cond.check == EmailRuleCheckType.Equals:
                            or_conditions.append(Mail.all_sent_to.contains([cond.value]))
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            or_conditions.append(~Mail.all_sent_to.contains([cond.value]))
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.Subject:
                        if cond.check == EmailRuleCheckType.Contains:
                            or_conditions.append(Mail.subject.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.NotContains:
                            or_conditions.append(~Mail.subject.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.Equals:
                            or_conditions.append(Mail.subject == cond.value)
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            or_conditions.append(Mail.subject != cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.Body:
                        if cond.check == EmailRuleCheckType.Contains:
                            or_conditions.append(Mail.body.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.NotContains:
                            or_conditions.append(~Mail.body.ilike(f"%{cond.value}%"))
                        elif cond.check == EmailRuleCheckType.Equals:
                            or_conditions.append(Mail.body == cond.value)
                        elif cond.check == EmailRuleCheckType.NotEquals:
                            or_conditions.append(Mail.body != cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.ReceivedAt:
                        self.date_parser(cond.value)  # Validate date format
                        if cond.check == EmailRuleCheckType.LessThan:
                            or_conditions.append(Mail.received_at < cond.value)
                        elif cond.check == EmailRuleCheckType.GreaterThan:
                            or_conditions.append(Mail.received_at > cond.value)
                        else:
                            raise ValueError(f"Unsupported check type: {cond.check} for condition type: {cond.type}")

                    elif cond.type == EmailRuleConditionType.ReceivedAtDelta:
                        try:
                            value, type_ = cond.value.split(" ")
                            value = int(value)
                            if type_ not in ["days", "months"]:
                                raise ValueError(f"Unsupported time unit: {type_}. Use 'days' or 'months'.")
                            if cond.check == EmailRuleCheckType.GreaterThan:
                                or_conditions.append(
                                    Mail.received_at < func.now() - text(f"INTERVAL '{value} {type_}'")
                                )
                            elif cond.check == EmailRuleCheckType.LessThan:
                                or_conditions.append(
                                    Mail.received_at > func.now() - text(f"INTERVAL '{value} {type_}'")
                                )
                            else:
                                raise ValueError(
                                    f"Unsupported check type: {cond.check} for condition type: {cond.type}"
                                )
                        except ValueError as e:
                            logging.error(f"Error parsing ReceivedAtDelta condition value '{cond.value}': {e}")
                            raise e

                    else:
                        raise ValueError(f"Unsupported condition type: {cond.type}")
                query = query.filter(or_(*or_conditions))

            else:
                raise ValueError(f"Unsupported match type: {rule.match_type}")

            logging.debug(f"Executing query: {query}")
            logging.debug(f"Params: {query.statement.compile().params}")
            mails = query.all()
            logging.info(f"Found {len(mails)} mails matching rule '{rule.name}'")

            for mail in mails:
                for action in rule.actions:
                    if action.type == EmailRuleActionType.MoveToFolder:
                        if not action.value:
                            logging.warning(f"No folder specified for action in rule '{rule.name}'")
                            raise ValueError(f"No folder specified for action in rule '{rule.name}'")

                        if action.value.upper() in RESERVED_LABELS:
                            logging.warning(f"Moving to special folder '{action.value}' is not supported, skipping.")
                            continue
                        self.add_labels_to_mail(mail, action.value)
                    elif action.type == EmailRuleActionType.MarkAsRead:
                        self.add_labels_to_mail(mail, "READ")
                        self.remove_labels_from_mail(mail, "UNREAD")
                    elif action.type == EmailRuleActionType.MarkAsUnread:
                        self.add_labels_to_mail(mail, "UNREAD")
                        self.remove_labels_from_mail(mail, "READ")
                    else:
                        raise ValueError(f"Unsupported action type: {action.type} in rule '{rule.name}'")
                logging.info(f"Applied actions for mail ID {mail.id} in rule '{rule.name}'")
        self.db.commit()

    def add_labels_to_mail(self, mail: Mail, label: str) -> None:
        update_query = (
            f"UPDATE {Mail.__tablename__}"
            " SET labels = CASE"
            " WHEN labels IS NULL THEN ARRAY[:label]"
            " ELSE array_append(labels, :label)"
            " END"
            " WHERE id = :mail_id"
            " AND (labels IS NULL OR array_position(labels, :label) IS NULL)"
        )
        self.db.execute(
            text(update_query).bindparams(
                label=label,
                mail_id=mail.id,
            )
        )
        self.db.commit()
        logging.info(f"Label '{label}' added to mail ID {mail.id}")

    def remove_labels_from_mail(self, mail: Mail, label: str) -> None:
        # Remove a label from the mail if it exists
        update_query = (
            f"UPDATE {Mail.__tablename__}"
            " SET labels = array_remove(labels, :label)"
            " WHERE id = :mail_id"
            " AND labels IS NOT NULL"
            " AND array_position(labels, :label) IS NOT NULL"
        )
        self.db.execute(
            text(update_query).bindparams(
                label=label,
                mail_id=mail.id,
            )
        )
        self.db.commit()
        logging.info(f"Label '{label}' removed from mail ID {mail.id}")

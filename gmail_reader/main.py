import logging

from pathlib import Path
from dotenv import load_dotenv
from argparse import ArgumentParser

from gmail_reader.database.models import init_db
from gmail_reader.gmail_service.auth import GmailClient
from gmail_reader.gmail_service.mail import MailService, MailRuleExecution


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("gmail_reader.log", mode="a", encoding="utf-8")],
)


def main():
    parser = ArgumentParser(description="Gmail Reader CLI")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to the .env file containing environment variables",
        required=False,
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode for more verbose logging",
    )
    parser.add_argument(
        "--refresh-token",
        action="store_true",
        help="Refresh the OAuth token for the Gmail API",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "init-db",
        help="Initialize the database if it does not exist",
    )

    update_mails = subparsers.add_parser(
        "update-mails",
        help="Update email data in the database",
    )
    update_mails.add_argument(
        "--creds-file",
        type=Path,
        default=None,
        required=True,
        help="Path to the Google API credentials JSON file",
    )
    update_mails.add_argument(
        "--email",
        type=str,
        required=True,
        help="Email address to update or create in the database",
    )

    rules_parser = subparsers.add_parser(
        "process-rules",
        help="Process email rules from a specified file",
    )
    rules_parser.add_argument(
        "--email",
        type=str,
        required=True,
        help="Email address to process rules for",
    )
    rules_parser.add_argument(
        "--creds-file",
        type=Path,
        default=None,
        required=True,
        help="Path to the Google API credentials JSON file for authentication",
    )
    rules_parser.add_argument(
        "--rules-file",
        type=Path,
        default=None,
        required=True,
        help="Path to the rules file for processing emails",
    )

    args = parser.parse_args()
    if args.env_file.is_file():
        load_dotenv(dotenv_path=args.env_file)
    else:
        raise FileNotFoundError(f"Environment file {args.env_file} does not exist.")

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.debug("Debug mode is enabled. Verbose logging will be shown.")

    if args.command == "init-db":
        try:
            init_db()
            logging.info("Database initialized successfully.")
        except Exception as e:
            logging.exception(f"Error initializing database: {e}")
        exit(0)
    elif args.command == "update-mails":
        if not args.creds_file or not args.creds_file.is_file():
            raise FileNotFoundError("Credentials file is required to update mails.")
        gmail_client = GmailClient(
            email=args.email,
            name=args.email.split("@")[0],  # Default name from email prefix
            creds_path=args.creds_file,
            db=init_db(),
            refresh_token=args.refresh_token,
        )
        mail_service = MailService(gmail_client=gmail_client)
        mail_service.fetch_and_store_email()
        exit(0)
    elif args.command == "process-rules":
        if not args.rules_file.is_file():
            raise FileNotFoundError(f"Rules file {args.rules_file} does not exist.")
        gmail_client = GmailClient(
            email=args.email,
            name=args.email.split("@")[0],  # Default name from email prefix
            creds_path=args.creds_file,
            db=init_db(),
            refresh_token=args.refresh_token,
        )
        MailRuleExecution(
            rules_path=args.rules_file,
            gmail_client=gmail_client,
        ).execute_rules()
        exit(0)
    else:
        logging.warning("No command provided. Use 'init-db' to initialize the database.")
        exit(1)


if __name__ == "__main__":
    main()

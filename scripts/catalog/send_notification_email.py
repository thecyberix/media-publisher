from __future__ import annotations

import argparse
import os
import smtplib
import sys
from email.message import EmailMessage

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587


def send_email(
    *,
    smtp_user: str,
    smtp_password: str,
    to_address: str,
    subject: str,
    body: str,
    smtp_host: str = DEFAULT_SMTP_HOST,
    smtp_port: int = DEFAULT_SMTP_PORT,
) -> None:
    message = EmailMessage()
    message["From"] = smtp_user
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a notification email via Gmail SMTP.")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument(
        "--to",
        default=os.getenv("NOTIFY_EMAIL", "").strip(),
        help="Recipient address (default: NOTIFY_EMAIL env var).",
    )
    parser.add_argument(
        "--from",
        dest="from_address",
        default=os.getenv("GMAIL_SMTP_USER", "").strip(),
        help="Sender Gmail address (default: GMAIL_SMTP_USER env var).",
    )
    args = parser.parse_args()

    smtp_user = args.from_address
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    to_address = args.to

    missing = []
    if not smtp_user:
        missing.append("GMAIL_SMTP_USER")
    if not smtp_password:
        missing.append("GMAIL_SMTP_APP_PASSWORD")
    if not to_address:
        missing.append("NOTIFY_EMAIL")
    if missing:
        print(f"Missing required configuration: {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        send_email(
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            to_address=to_address,
            subject=args.subject,
            body=args.body,
        )
    except smtplib.SMTPException as exc:
        print(f"Failed to send email: {exc}", file=sys.stderr)
        return 1

    print(f"Sent notification email to {to_address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

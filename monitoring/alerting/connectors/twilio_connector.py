"""Twilio P0 SMS connector.

Used ONLY for P0 alerts (fleet halt, /panic, position drift > threshold,
total DD kill). Twilio costs ~$0.0075 per SMS — designed to be rare.
"""
from twilio.rest import Client

from framework.config import get_settings
from framework.logging_setup import get_logger

log = get_logger(__name__)


class TwilioConnector:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None

    def _get_client(self) -> Client | None:
        if not (self.settings.twilio_account_sid and self.settings.twilio_auth_token):
            return None
        if self._client is None:
            self._client = Client(self.settings.twilio_account_sid, self.settings.twilio_auth_token)
        return self._client

    async def send(self, body: str) -> None:
        client = self._get_client()
        if client is None:
            log.warning("twilio_skipped", reason="creds missing")
            return
        if not (self.settings.twilio_from_number and self.settings.twilio_to_number):
            log.warning("twilio_skipped", reason="numbers missing")
            return
        try:
            client.messages.create(
                body=body[:1500],
                from_=self.settings.twilio_from_number,
                to=self.settings.twilio_to_number,
            )
        except Exception:
            log.exception("twilio_send_failed")

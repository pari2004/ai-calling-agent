from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

from ..config import settings

# NOTE: Exotel's Connect API and ExoML are modelled on this file's best
# understanding of https://developer.exotel.com as of writing. Field/param
# names (especially in the StatusCallback payload) can vary slightly by
# account/region — verify against your account's API docs before relying on
# this in production, the same way you'd sanity-check any new provider.


def _base_url() -> str:
    return settings.public_base_url.rstrip("/") if getattr(settings, "public_base_url", "") else ""


def _signed(url: str) -> str:
    if settings.exotel_webhook_secret and url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}sig={urllib.parse.quote(settings.exotel_webhook_secret)}"
    return url


def answer_exoml(*, opening_line: str, call_id: str) -> str:
    """ExoML returned to Exotel when the callee answers.

    Non-streaming flow: <Gather input="speech"> loop that posts recognised
    speech back to our /gather endpoint (mirrors the Twilio Gather fallback).
    """
    say = f"<Say>{escape(opening_line)}</Say>"
    base = _base_url()
    action = _signed(f"{base}/v1/telephony/exotel/gather/{call_id}") if base else ""
    gather = (f"<Gather input=\"speech\" method=\"POST\""
              f"{f' action=\"{action}\"' if action else ''}>{say}</Gather>")
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>{gather}</Response>"


def say_exoml(text: str, *, hangup: bool = False) -> str:
    body = f"<Say>{escape(text)}</Say>"
    if hangup:
        body += "<Hangup/>"
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>{body}</Response>"


class ExotelVoice:
    """Real outbound dialing via Exotel's Connect API, with local fallback so
    the stack stays runnable without credentials."""

    def __init__(self) -> None:
        self.sid = settings.exotel_sid
        self.api_key = settings.exotel_api_key
        self.api_token = settings.exotel_api_token
        self.caller_id = settings.exotel_caller_id
        self.subdomain = settings.exotel_subdomain

    def configured(self) -> bool:
        return bool(self.sid and self.api_key and self.api_token and self.caller_id)

    def dial(self, to_number: str, call_id: str, *, record: bool | None = None) -> dict:
        if not self.configured():
            return {"provider": "local", "status": "ringing",
                    "provider_call_id": f"sim_{call_id}", "to": to_number}
        base = _base_url()
        answer_url = _signed(f"{base}/v1/telephony/exotel/answer/{call_id}") if base else ""
        if not answer_url:
            return {"provider": "exotel", "status": "failed",
                    "error": "PUBLIC_BASE_URL not configured; Exotel needs a public "
                             "callback URL to fetch the ExoML call flow"}
        params = {
            "From": to_number,
            "CallerId": self.caller_id,
            "Url": answer_url,
            "StatusCallback": _signed(f"{base}/v1/telephony/exotel/status"),
        }
        if record if record is not None else settings.recording_enabled:
            params["Record"] = "true"
        try:
            data = self._post_call(params)
            call = data.get("Call", data)
            return {"provider": "exotel", "status": call.get("Status", "queued"),
                    "provider_call_id": call.get("Sid", ""), "to": to_number}
        except Exception as exc:
            return {"provider": "exotel", "status": "failed", "error": str(exc)}

    def _post_call(self, params: dict) -> dict:
        url = f"https://{self.subdomain}/v1/Accounts/{self.sid}/Calls/connect.json"
        body = urllib.parse.urlencode(params).encode()
        auth = base64.b64encode(f"{self.api_key}:{self.api_token}".encode()).decode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())


def parse_status(form: dict) -> dict:
    """Normalise an Exotel status callback into our internal shape."""
    return {
        "provider_call_id": form.get("CallSid", ""),
        "status": (form.get("Status") or form.get("CallStatus", "")).lower(),
        "duration_s": int(form.get("DialCallDuration") or form.get("CallDuration") or 0),
        "from": form.get("From", ""),
        "to": form.get("To", ""),
    }


voice = ExotelVoice()

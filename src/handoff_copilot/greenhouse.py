"""Greenhouse as one connector: a Harvest v3 client and the reads and one write the copilot uses.

Written from Greenhouse's public Harvest v3 reference (checked Nov 1, 2026) and tested
against `fake_harvest.FakeHarvest`, not against a Greenhouse account.

* Auth: OAuth 2.0 client credentials at `https://auth.greenhouse.io/token`, then
  `Authorization: Bearer` on `https://harvest.greenhouse.io/v3`.
* Paging: filters and `per_page` go on the first request only; after that the client
  follows the `Link: <...>; rel="next"` URL exactly, which carries only a cursor.
* Rate limits: it reads `X-RateLimit-Remaining` and pauses until `X-RateLimit-Reset`
  when ten or fewer remain; on a 429 it waits `Retry-After` plus jitter, never retrying
  at once.
* Data minimization: every list asks for named `fields` and drops anything else.
* Writes: one kind only, a private note. There is no method here that rejects, moves,
  advances, hires or rates a candidate, or that writes a scorecard.
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from .context import ScreenContext
from .drafts import with_footer
from .privacy import keep_fields

BASE = "https://harvest.greenhouse.io/v3"
TOKEN_URL = "https://auth.greenhouse.io/token"  # noqa: S105

FIELDS: dict[str, tuple[str, ...]] = {
    "applications": ("id", "candidate_id", "job_id", "recruiter_id", "coordinator_id", "status"),
    "candidates": (
        "id",
        "first_name",
        "last_name",
        "preferred_name",
        "email_addresses",
        "phone_numbers",
    ),
    "interviews": ("id", "application_id", "starts_at", "ends_at", "status"),
    "interviewers": ("id", "interview_id", "user_id", "scorecard_id"),
    "notes": ("id", "candidate_id", "application_id", "type", "body", "email_to", "created_at"),
    "scorecards": ("id", "application_id", "interviewer_id", "status", "submitted_at"),
    "interview_media": ("id", "interview_id", "application_id", "transcript_url"),
    "ai_opt_out_statuses": ("id", "job_id", "candidate_ids", "opted_out", "feature"),
}


class TransportError(OSError):
    """The request may or may not have reached the server."""


class HarvestError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Harvest {status}: {message}")
        self.status = status


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body or b"null")


Transport = Callable[[str, str, dict, "bytes | None"], Response]


class UrllibTransport:
    """The real network path, standard library only. HTTPS unless loopback is allowed."""

    def __init__(self, timeout: float = 30.0, allow_loopback_http: bool = False) -> None:
        self.timeout = timeout
        self.allow_loopback_http = allow_loopback_http
        self._open = urllib.request.build_opener().open

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
        parts = urlsplit(url)
        loopback = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
        if parts.scheme != "https" and not (loopback and self.allow_loopback_http):
            raise ValueError(f"refusing a non-HTTPS URL: {url}")
        request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
        try:
            with self._open(request, timeout=self.timeout) as r:
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TransportError(str(e)) from e


def next_link(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(r'<([^>]+)>\s*;\s*rel="next"', header)
    return match.group(1) if match else None


class HarvestClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: Transport,
        *,
        sub: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.5),  # noqa: S311
        pause_at: int = 10,
        max_retries: int = 5,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._id, self._secret, self.transport = client_id, client_secret, transport
        self.sub, self.sleep, self.now, self.jitter = sub, sleep, now, jitter
        self.pause_at, self.max_retries = pause_at, max_retries
        self.per_page = 500
        self.log = log or (lambda _line: None)
        self._token: str | None = None
        self._token_expires = 0.0
        self._remaining: int | None = None
        self._reset: float | None = None

    def _fetch_token(self) -> str:
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        form = {"grant_type": "client_credentials"}
        if self.sub is not None:
            form["sub"] = str(self.sub)
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = self._with_backoff("POST", TOKEN_URL, headers, urlencode(form).encode(), gate=False)
        if resp.status != 200:
            raise HarvestError(resp.status, "token request refused")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = self.now() + float(data.get("expires_in", 0)) - 60
        self.log("token: client credentials, bearer issued")
        return self._token

    def _bearer(self, refresh: bool = False) -> str:
        if refresh or self._token is None or self.now() >= self._token_expires:
            return self._fetch_token()
        return self._token

    def _with_backoff(
        self, method: str, url: str, headers: dict, body: bytes | None, gate: bool = True
    ) -> Response:
        """Token requests have their own 60 s window, so they skip the Harvest gate."""
        for _ in range(self.max_retries + 1):
            if gate and self._remaining is not None and self._remaining <= self.pause_at:
                wait = max((self._reset or 0) - self.now(), 1.0)
                self.log(f"rate gate: {self._remaining} left in the window, pausing {wait:.0f}s")
                self.sleep(wait)
                self._remaining = None
            resp = self.transport(method, url, headers, body)
            if gate and "x-ratelimit-remaining" in resp.headers:
                self._remaining = int(resp.headers["x-ratelimit-remaining"])
                self._reset = float(resp.headers.get("x-ratelimit-reset", 0))
            if resp.status != 429:
                return resp
            wait = float(resp.headers.get("retry-after", 1)) + self.jitter()
            self.log(f"429: Retry-After {resp.headers.get('retry-after')}s, waiting {wait:.1f}s")
            self.sleep(wait)
            self._remaining = None
        raise HarvestError(429, f"still rate limited after {self.max_retries} retries")

    def _call(self, method: str, url: str, payload: dict | None = None) -> Response:
        body = json.dumps(payload).encode() if payload is not None else None
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._bearer(refresh=attempt == 1)}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            resp = self._with_backoff(method, url, headers, body)
            if resp.status != 401:
                return resp
            self.log("401: token rejected, fetching a new one once")
        return resp

    def list(self, resource: str, *, per_page: int | None = None, **filters) -> list[dict]:
        allowed = FIELDS[resource]
        params = {
            k: ",".join(map(str, v)) if isinstance(v, list | tuple) else str(v)
            for k, v in filters.items()
        }
        params.update(per_page=str(per_page or self.per_page), fields=",".join(allowed))
        url: str | None = f"{BASE}/{resource}?{urlencode(params)}"
        rows: list[dict] = []
        page = 0
        while url:
            resp = self._call("GET", url)
            if resp.status != 200:
                raise HarvestError(resp.status, f"GET {resource} failed: {resp.body[:200]!r}")
            page += 1
            batch = resp.json()
            rows += [keep_fields(r, allowed) for r in batch]
            url = next_link(resp.headers.get("link"))
            self.log(
                f"GET {resource} page {page}: {len(batch)} rows"
                + (", following the cursor" if url else "")
            )
        return rows

    def create_note(self, payload: dict) -> dict:
        resp = self._call("POST", f"{BASE}/notes", payload)
        if resp.status != 201:
            raise HarvestError(resp.status, f"POST notes failed: {resp.body[:200]!r}")
        note = resp.json()
        self.log(f"POST notes: 201, {note.get('visibility')} note #{note.get('id')}")
        return note

    def download(self, url: str) -> bytes:
        """A signed media URL: fetched without the bearer token, which stays with Greenhouse."""
        resp = self.transport("GET", url, {}, None)
        if resp.status != 200:
            raise HarvestError(resp.status, "transcript download failed")
        self.log("transcript: downloaded from its signed URL, no bearer token sent")
        return resp.body


@dataclass(frozen=True)
class Delivery:
    note_id: int
    status: str


class GreenhouseConnector:
    name = "greenhouse"

    def __init__(self, client: HarvestClient) -> None:
        self.client = client

    def _one(self, resource: str, **filters) -> dict:
        rows = self.client.list(resource, **filters)
        if len(rows) != 1:
            raise HarvestError(404, f"expected one {resource} row for {filters}, got {len(rows)}")
        return rows[0]

    def ai_opt_out(self, candidate_id: int, job_id: int | None) -> bool | None:
        """True or False when Greenhouse records a decision; None when the org can't say."""
        if job_id is None:
            return None
        try:
            rows = self.client.list(
                "ai_opt_out_statuses", candidate_ids=candidate_id, job_ids=job_id
            )
        except HarvestError as e:
            if e.status in (403, 404):
                return None
            raise
        return any(r.get("opted_out") for r in rows)

    def screen_context(self, application_id: int, interview_id: int) -> ScreenContext:
        app = self._one("applications", ids=application_id)
        candidate = self._one("candidates", ids=app["candidate_id"])
        interviews = self.client.list("interviews", application_ids=application_id)
        screen = next((i for i in interviews if i["id"] == interview_id), None)
        if screen is None:
            raise HarvestError(404, f"interview {interview_id} isn't on {application_id}")
        panel = self.client.list("interviewers", interview_ids=interview_id)
        notes = self.client.list("notes", candidate_ids=candidate["id"])
        media = self.client.list("interview_media", interview_ids=interview_id)
        url = next((m["transcript_url"] for m in media if m.get("transcript_url")), None)
        transcript = json.loads(self.client.download(url)) if url else None
        opt_out = self.ai_opt_out(candidate["id"], app.get("job_id"))
        seen = (
            "applications",
            "candidates",
            "interviews",
            "interviewers",
            "notes",
            "interview_media",
            "ai_opt_out_statuses",
        )
        return ScreenContext(
            application=app,
            candidate=candidate,
            screen=screen,
            panel=panel,
            interviews=interviews,
            notes=notes,
            transcript=transcript,
            ai_opt_out=opt_out,
            fields_seen={r: list(FIELDS[r]) for r in seen},
        )

    def find_note(self, candidate_id: int, ref: str) -> dict | None:
        notes = self.client.list("notes", candidate_ids=candidate_id)
        return next((n for n in notes if f"ref: {ref}" in (n.get("body") or "")), None)

    def post_private_note(
        self, *, candidate_id: int, application_id: int, body: str, ref: str, user_id: int
    ) -> Delivery:
        """Post once. A retry or rerun finds the earlier note by its ref instead of posting again.

        Delivery is confirmed by reading the note back, not by the POST returning.
        """
        existing = self.find_note(candidate_id, ref)
        if existing:
            return Delivery(existing["id"], "already_delivered")
        payload = {
            "candidate_id": candidate_id,
            "application_id": application_id,
            "body": with_footer(body, ref),
            "note_type": "NOTE",
            "visibility": "private",
            "user_id": user_id,
        }
        try:
            self.client.create_note(payload)
        except TransportError:
            self.client.log("POST notes: no response; checking whether it landed")
        landed = self.find_note(candidate_id, ref)
        if landed is None:
            raise DeliveryError(f"note {ref} did not land; nothing is marked delivered")
        return Delivery(landed["id"], "delivered")

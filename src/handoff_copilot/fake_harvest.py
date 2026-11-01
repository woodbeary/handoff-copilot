"""An in-process fake of the Harvest v3 surface this copilot touches.

Written from Greenhouse's public reference (checked Nov 1, 2026): OAuth client
credentials, a fixed 30-second rate window with `X-RateLimit-*` headers, 429 with
`Retry-After`, opaque cursors in `Link` headers, 422 when a cursor is mixed with other
parameters, `fields=` selection, and `POST /v3/notes`. It serves synthetic records
only. Anything it doesn't implement answers 404, including every endpoint that would
reject, move, advance or hire a candidate.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .greenhouse import Response, TransportError

RESOURCES = (
    "applications",
    "candidates",
    "interviews",
    "interviewers",
    "notes",
    "scorecards",
    "interview_media",
    "ai_opt_out_statuses",
)
FILTERS = {
    "ids": "id",
    "candidate_ids": "candidate_id",
    "application_ids": "application_id",
    "interview_ids": "interview_id",
    "job_ids": "job_id",
}
MEDIA_HOST = "media.synthetic.invalid"
VISIBILITY = {
    "admin_only": "admin_only_visible",
    "private": "privately_visible",
    "public": "publicly_visible",
}
WINDOW = 30.0


def _json(status: int, data, headers: dict | None = None) -> Response:
    return Response(
        status, {"content-type": "application/json", **(headers or {})}, json.dumps(data).encode()
    )


class FakeHarvest:
    def __init__(
        self,
        org: dict,
        *,
        transcripts: dict[str, dict] | None = None,
        client_id: str = "synthetic-client",
        client_secret: str = "synthetic-secret",  # noqa: S107
        limit: int = 50,
        now: Callable[[], float] = time.time,
        talent_matching: bool = True,
    ) -> None:
        self.tables = {r: [dict(row) for row in org.get(r, [])] for r in RESOURCES}
        self.transcripts = transcripts or {}
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.limit, self.now, self.talent_matching = limit, now, talent_matching
        self.calls: list[tuple[str, str, int]] = []
        self.media_auth_leaks = 0
        self.tokens: set[str] = set()
        self._issued = 0
        self._window_start, self._used = now(), 0
        self._scripted_429: list[float] = []
        self._drop_note_responses = 0

    @classmethod
    def from_dir(cls, path: str | Path, **kwargs) -> FakeHarvest:
        root = Path(path)
        org = json.loads((root / "org.json").read_text())
        transcripts = {
            f"https://{MEDIA_HOST}/transcripts/{p.name}": json.loads(p.read_text())
            for p in sorted((root / "transcripts").glob("*.json"))
        }
        return cls(org, transcripts=transcripts, **kwargs)

    def queue_429(self, retry_after: float = 1, times: int = 1) -> None:
        self._scripted_429 += [retry_after] * times

    def drop_next_note_response(self) -> None:
        """The next POST /v3/notes is stored, then the connection drops before the reply."""
        self._drop_note_responses += 1

    def expire_tokens(self) -> None:
        self.tokens.clear()

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
        parts = urlsplit(url)
        shown = parts.path + (f"?{parts.query}" if parts.query else "")
        if parts.netloc == "auth.greenhouse.io" and parts.path == "/token":
            resp = self._token(headers, body)
        elif parts.netloc == MEDIA_HOST:
            resp = self._media(url, headers)
        elif parts.netloc == "harvest.greenhouse.io" and parts.path.startswith("/v3/"):
            resp = self._harvest(method, parts.path[4:], parts.query, headers, body)
        else:
            resp = _json(404, {"message": "Not Found"})
        self.calls.append((method, shown, resp.status))
        return resp

    def _post_then_drop(self) -> None:
        self.calls.append(("POST", "/v3/notes", 0))
        raise TransportError("connection reset after the write landed")

    def _token(self, headers: dict, body: bytes | None) -> Response:
        form = parse_qs((body or b"").decode())
        if headers.get("Authorization") != f"Basic {self._basic}":
            return _json(401, {"error": "invalid_client"})
        if form.get("grant_type") != ["client_credentials"]:
            return _json(400, {"error": "unsupported_grant_type"})
        self._issued += 1
        token = f"synthetic-token-{self._issued}"
        self.tokens.add(token)
        return _json(200, {"token_type": "Bearer", "access_token": token, "expires_in": 3600})

    def _media(self, url: str, headers: dict) -> Response:
        if "Authorization" in headers:
            self.media_auth_leaks += 1
            return _json(400, {"message": "signed URLs take no Authorization header"})
        key = url.split("?")[0]
        if key not in self.transcripts:
            return _json(404, {"message": "Not Found"})
        return _json(200, self.transcripts[key])

    def _rate_headers(self) -> dict:
        now = self.now()
        if now - self._window_start >= WINDOW:
            self._window_start, self._used = now, 0
        reset = int(self._window_start + WINDOW)
        return {
            "x-ratelimit-limit": str(self.limit),
            "x-ratelimit-remaining": str(max(self.limit - self._used, 0)),
            "x-ratelimit-reset": str(reset),
        }

    def _harvest(self, method, resource, query, headers, body) -> Response:
        rate = self._rate_headers()
        if self._scripted_429 or self._used >= self.limit:
            if self._scripted_429:
                wait = self._scripted_429.pop(0)
            else:
                wait = max(int(rate["x-ratelimit-reset"]) - self.now(), 1)
            return _json(
                429,
                {"message": "Too Many Requests"},
                {**rate, "x-ratelimit-remaining": "0", "retry-after": str(int(wait))},
            )
        self._used += 1
        rate["x-ratelimit-remaining"] = str(self.limit - self._used)
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
            return _json(401, {"message": "Unauthorized"}, rate)
        if method == "GET" and resource in RESOURCES:
            if resource == "ai_opt_out_statuses" and not self.talent_matching:
                return _json(403, {"message": "Talent Matching is not enabled"}, rate)
            return self._list(resource, query, rate)
        if method == "POST" and resource == "notes":
            return self._create_note(body, rate)
        return _json(404, {"message": "Not implemented in the fake"}, rate)

    def _list(self, resource: str, query: str, rate: dict) -> Response:
        params = {k: v[0] for k, v in parse_qs(query).items()}
        if "cursor" in params:
            if len(params) > 1:
                return _json(
                    422,
                    {
                        "message": "Unprocessable Content",
                        "errors": ["When passing a cursor, do not include other query params."],
                    },
                    rate,
                )
            try:
                state = json.loads(base64.urlsafe_b64decode(params["cursor"]))
            except ValueError:
                state = {}
            if not isinstance(state, dict) or state.get("resource") != resource:
                return _json(
                    422, {"message": "Unprocessable Content", "errors": ["Invalid cursor"]}, rate
                )
        else:
            unknown = set(params) - set(FILTERS) - {"per_page", "fields"}
            if unknown:
                return _json(422, {"message": f"unsupported in the fake: {sorted(unknown)}"}, rate)
            per_page = params.get("per_page", "100")
            if not per_page.isdigit() or not 1 <= int(per_page) <= 500:
                return _json(
                    422,
                    {
                        "message": "Unprocessable Content",
                        "errors": [{"per_page": f"`{per_page}` is out of range"}],
                    },
                    rate,
                )
            state = {
                "resource": resource,
                "filters": {k: v for k, v in params.items() if k in FILTERS},
                "fields": params.get("fields"),
                "per_page": int(per_page),
                "after_id": None,
            }
        rows = sorted(self.tables[resource], key=lambda r: -r["id"])
        for name, value in state["filters"].items():
            wanted = {int(x) for x in value.split(",")}
            field = FILTERS[name]
            plural = field + "s"
            rows = [r for r in rows if r.get(field) in wanted or set(r.get(plural) or []) & wanted]
        if state["after_id"] is not None:
            rows = [r for r in rows if r["id"] < state["after_id"]]
        page, more = rows[: state["per_page"]], len(rows) > state["per_page"]
        if state["fields"]:
            keep = set(state["fields"].split(","))
            page = [{k: v for k, v in r.items() if k in keep} for r in page]
        headers = dict(rate)
        if more:
            nxt = {**state, "after_id": rows[state["per_page"] - 1]["id"]}
            cursor = base64.urlsafe_b64encode(json.dumps(nxt).encode()).decode()
            nxt_url = f"https://harvest.greenhouse.io/v3/{resource}?cursor={cursor}"
            headers["link"] = f'<{nxt_url}>; rel="next"'
        return _json(200, page, headers)

    def _create_note(self, body: bytes | None, rate: dict) -> Response:
        try:
            data = json.loads(body or b"")
        except ValueError:
            return _json(400, {"message": "Bad Request"}, rate)
        missing = [k for k in ("candidate_id", "body", "visibility", "note_type") if k not in data]
        if (
            missing
            or data["visibility"] not in VISIBILITY
            or data["note_type"] not in ("NOTE", "ACTIVITY")
        ):
            return _json(422, {"message": "Unprocessable Content", "errors": missing}, rate)
        note = {
            "id": max((n["id"] for n in self.tables["notes"]), default=0) + 1,
            "created_at": datetime.fromtimestamp(self.now(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "candidate_id": data["candidate_id"],
            "application_id": data.get("application_id"),
            "body": data["body"],
            "type": data["note_type"],
            "user_id": data.get("user_id"),
            "visibility": VISIBILITY[data["visibility"]],
            "email_to": None,
        }
        self.tables["notes"].append(note)
        if self._drop_note_responses:
            self._drop_note_responses -= 1
            self._post_then_drop()
        return _json(201, note, rate)

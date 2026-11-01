import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import EXAMPLES, client_for

from handoff_copilot.fake_harvest import FakeHarvest
from handoff_copilot.greenhouse import (
    FIELDS,
    DeliveryError,
    GreenhouseConnector,
    HarvestError,
    TransportError,
    UrllibTransport,
    next_link,
)


class Recorder:
    def __init__(self, inner):
        self.inner, self.sent = inner, []

    def __call__(self, method, url, headers, body):
        self.sent.append((method, url, dict(headers), body))
        return self.inner(method, url, headers, body)


def test_client_credentials_then_one_bearer_for_many_calls(fake, clock):
    rec = Recorder(fake)
    client = client_for(rec, clock, sub=35)
    client.list("applications")
    client.list("candidates")
    method, url, headers, body = rec.sent[0]
    assert (method, url) == ("POST", "https://auth.greenhouse.io/token")
    assert (
        headers["Authorization"]
        == "Basic " + base64.b64encode(b"synthetic-client:synthetic-secret").decode()
    )
    assert parse_qs(body.decode()) == {"grant_type": ["client_credentials"], "sub": ["35"]}
    assert sum(u.endswith("/token") for _, u, _, _ in rec.sent) == 1
    assert all(h["Authorization"] == "Bearer synthetic-token-1" for _, _, h, _ in rec.sent[1:])


def test_token_is_renewed_before_expiry_and_once_on_a_401(fake, clock):
    client = client_for(fake, clock)
    client.list("applications")
    clock.t += 3600 - 59
    client.list("applications")
    fake.expire_tokens()
    client.list("applications")
    statuses = [(m, p.split("?")[0], s) for m, p, s in fake.calls]
    assert statuses.count(("POST", "/token", 200)) == 3
    assert ("GET", "/v3/applications", 401) in statuses


def test_filters_go_on_the_first_request_then_only_the_cursor(fake, clock):
    rec = Recorder(fake)
    client = client_for(rec, clock)
    rows = client.list("interviews", per_page=2, job_ids=801)
    urls = [u for m, u, _, _ in rec.sent if m == "GET"]
    first = parse_qs(urlsplit(urls[0]).query)
    assert set(first) == {"job_ids", "per_page", "fields"}
    assert all(set(parse_qs(urlsplit(u).query)) == {"cursor"} for u in urls[1:])
    assert len(urls) == 4 and len(rows) == 7
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)


def test_the_fake_refuses_a_cursor_mixed_with_filters_and_bad_page_sizes(fake, clock):
    client = client_for(fake, clock)
    client.list("applications")
    token = {"Authorization": "Bearer synthetic-token-1"}
    first = fake("GET", "https://harvest.greenhouse.io/v3/interviews?per_page=2", token, None)
    nxt = next_link(first.headers["link"])
    mixed = fake("GET", nxt + "&per_page=2", token, None)
    assert mixed.status == 422
    assert "do not include other query params" in mixed.body.decode()
    huge = fake("GET", "https://harvest.greenhouse.io/v3/interviews?per_page=600", token, None)
    assert huge.status == 422


def test_429_waits_retry_after_and_never_retries_at_once(fake, clock):
    client = client_for(fake, clock)
    fake.queue_429(retry_after=3)
    assert len(client.list("applications")) == 5
    assert clock.slept == [3.0]
    assert [s for _, _, s in fake.calls].count(429) == 1


def test_429_gives_up_after_max_retries(fake, clock):
    client = client_for(fake, clock, max_retries=2)
    client.list("applications")
    fake.queue_429(retry_after=2, times=5)
    with pytest.raises(HarvestError, match="still rate limited"):
        client.list("applications")
    assert clock.slept == [2.0, 2.0, 2.0]


def test_pauses_before_the_limit_instead_of_hitting_it(clock):
    fake = FakeHarvest.from_dir(EXAMPLES, now=clock.time, limit=12)
    client = client_for(fake, clock)
    for _ in range(3):
        client.list("applications")
    assert clock.slept == [30.0]
    assert 429 not in [s for _, _, s in fake.calls]


def test_lists_ask_for_named_fields_and_drop_the_rest(fake, clock):
    rec = Recorder(fake)
    client = client_for(rec, clock)
    candidates = client.list("candidates")
    cards = client.list("scorecards")
    assert parse_qs(urlsplit(rec.sent[1][1]).query)["fields"] == [",".join(FIELDS["candidates"])]
    assert all(set(c) <= set(FIELDS["candidates"]) for c in candidates)
    assert not any("addresses" in c or "company" in c for c in candidates)
    assert not any("candidate_rating" in c or "notes" in c for c in cards)


def test_fields_the_server_sends_anyway_are_dropped(fake, clock):
    def generous(method, url, headers, body):
        resp = fake(method, url, headers, body)
        if method == "GET" and "/v3/candidates" in url:
            rows = [{**r, "addresses": [{"value": "1 Example Way"}]} for r in resp.json()]
            resp = type(resp)(resp.status, resp.headers, json.dumps(rows).encode())
        return resp

    candidates = client_for(generous, clock).list("candidates")
    assert candidates and not any("addresses" in c for c in candidates)


def test_screen_context_downloads_the_transcript_without_the_bearer(fake, connector):
    ctx = connector.screen_context(5101, 7301)
    assert ctx.transcript["entries"][0]["speaker"] == "Casey Lindqvist"
    assert fake.media_auth_leaks == 0
    leaked = fake(
        "GET",
        "https://media.synthetic.invalid/transcripts/7301.json",
        {"Authorization": "Bearer x"},
        None,
    )
    assert leaked.status == 400 and fake.media_auth_leaks == 1


def test_ai_opt_out_is_read_per_job_and_unknown_when_the_org_cannot_say(clock, connector):
    assert connector.ai_opt_out(4203, 801) is True
    assert connector.ai_opt_out(4202, 801) is False
    assert connector.ai_opt_out(4201, 801) is False
    no_tm = FakeHarvest.from_dir(EXAMPLES, now=clock.time, talent_matching=False)
    assert GreenhouseConnector(client_for(no_tm, clock)).ai_opt_out(4203, 801) is None


def test_a_note_is_posted_private_once_and_confirmed_by_reading_it_back(fake, connector):
    first = connector.post_private_note(
        candidate_id=4201, application_id=5101, body="hello", ref="hc-test", user_id=35
    )
    again = connector.post_private_note(
        candidate_id=4201, application_id=5101, body="hello", ref="hc-test", user_id=35
    )
    assert (first.status, again.status) == ("delivered", "already_delivered")
    mine = [n for n in fake.tables["notes"] if "ref: hc-test" in n["body"]]
    assert len(mine) == 1
    assert mine[0]["visibility"] == "privately_visible" and mine[0]["user_id"] == 35
    assert mine[0]["type"] == "NOTE"


def test_a_lost_reply_does_not_double_post(fake, connector):
    fake.drop_next_note_response()
    delivery = connector.post_private_note(
        candidate_id=4201, application_id=5101, body="hello", ref="hc-drop", user_id=35
    )
    assert delivery.status == "delivered"
    assert sum("ref: hc-drop" in n["body"] for n in fake.tables["notes"]) == 1


def test_a_write_that_never_landed_is_not_called_delivered(fake, clock):
    def lossy(method, url, headers, body):
        if method == "POST" and url.endswith("/v3/notes"):
            raise TransportError("timed out before the server saw it")
        return fake(method, url, headers, body)

    connector = GreenhouseConnector(client_for(lossy, clock))
    with pytest.raises(DeliveryError):
        connector.post_private_note(
            candidate_id=4201, application_id=5101, body="hello", ref="hc-lost", user_id=35
        )


def test_next_link_parses_the_documented_header():
    header = '<https://harvest.greenhouse.io/v3/jobs?cursor=abc>; rel="next"'
    assert next_link(header) == "https://harvest.greenhouse.io/v3/jobs?cursor=abc"
    assert next_link(None) is None and next_link("") is None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200 if self.path == "/ok" else 404
        body = json.dumps({"path": self.path}).encode()
        self.send_response(status)
        self.send_header("X-RateLimit-Remaining", "7")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_urllib_transport_refuses_plain_http_and_maps_errors(monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="non-HTTPS"):
        UrllibTransport()("GET", "http://example.com/", {}, None)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        transport = UrllibTransport(timeout=5, allow_loopback_http=True)
        base = f"http://127.0.0.1:{server.server_port}"
        ok = transport("GET", f"{base}/ok", {}, None)
        missing = transport("GET", f"{base}/nope", {}, None)
        assert (ok.status, ok.headers["x-ratelimit-remaining"]) == (200, "7")
        assert missing.status == 404 and missing.json() == {"path": "/nope"}
    finally:
        server.shutdown()

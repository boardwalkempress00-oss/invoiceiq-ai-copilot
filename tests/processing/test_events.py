"""Event Grid BlobCreated parsing: only events for our container and blob names are accepted."""

from __future__ import annotations

import json

import pytest

from invoiceiq.processing.events import InvalidEventError, blob_created_event, parse_blob_created
from tests.processing.conftest import ACCOUNT_URL, CONTAINER, HOST, T0

SID = "0123456789abcdef0123456789abcdef"
BLOB = f"submissions/{SID}.json"


def event(**changes: object) -> dict:
    body = blob_created_event(
        event_id="evt-1",
        account_url=ACCOUNT_URL,
        container=CONTAINER,
        blob_name=BLOB,
        content_length=2143,
        etag='"0x8DE"',
        client_request_id="abcdef0123456789",
        event_time=T0,
    )
    for key, value in changes.items():
        if key.startswith("data_"):
            body["data"][key[5:]] = value
        else:
            body[key] = value
    return body


def parse(body: object) -> object:
    return parse_blob_created(json.dumps(body), container=CONTAINER, expected_host=HOST)


def test_valid_event_is_parsed():
    parsed = parse(event())
    assert parsed.submission_id == SID and parsed.blob_name == BLOB
    assert parsed.event_id == "evt-1" and parsed.api == "PutBlob"
    assert parsed.content_length == 2143 and parsed.client_request_id == "abcdef0123456789"
    assert parsed.event_time == T0


def test_single_element_array_is_accepted():
    assert parse([event()]).submission_id == SID


def test_azurite_style_url_path_is_accepted():
    body = event(data_url=f"http://127.0.0.1:10000/devstoreaccount1/{CONTAINER}/{BLOB}")
    parsed = parse_blob_created(json.dumps(body), container=CONTAINER, expected_host=None)
    assert parsed.submission_id == SID


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "not JSON"),
        ([], "not a JSON object"),
        ([{}, {}], "not a JSON object"),
        (event(eventType="Microsoft.Storage.BlobDeleted"), "event type"),
        (event(id=None), "event id"),
        (
            event(subject=f"/blobServices/default/containers/other/blobs/{BLOB}"),
            "another container",
        ),
        (
            event(subject=f"/blobServices/default/containers/{CONTAINER}/blobs/../x.json"),
            "blob name",
        ),
        (
            event(subject=f"/blobServices/default/containers/{CONTAINER}/blobs/submissions/A.json"),
            "blob name",
        ),
        (event(data=None), "data missing"),
        (event(data_api="CopyBlob"), "storage operation"),
        (event(data_url=None), "url missing"),
        (event(data_url=f"https://evil.blob.core.windows.net/{CONTAINER}/{BLOB}"), "account"),
        (event(data_url=f"{ACCOUNT_URL}/{CONTAINER}/submissions/other.json"), "does not match"),
    ],
)
def test_unexpected_events_are_rejected(body, reason):
    raw = body if isinstance(body, bytes) else json.dumps(body)
    with pytest.raises(InvalidEventError, match=reason):
        parse_blob_created(raw, container=CONTAINER, expected_host=HOST)


def test_oversized_message_is_rejected_before_parsing():
    with pytest.raises(InvalidEventError, match="too large"):
        parse_blob_created(b"{" + b" " * 70_000 + b"}", container=CONTAINER)


def test_negative_content_length_is_ignored():
    assert parse(event(data_contentLength=-5)).content_length is None

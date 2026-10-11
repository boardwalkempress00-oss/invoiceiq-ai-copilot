"""Compatibility probe for the Event Grid → Service Bus Basic test (see README.md).

    uv run python probe.py <blob_account_url> <servicebus_fqdn>

Uses the Azure CLI login (AzureCliCredential); no keys. Prints evidence, settles every message.
"""

from __future__ import annotations

import json
import sys
import time
import uuid

from azure.identity import AzureCliCredential
from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode
from azure.storage.blob import BlobServiceClient

blob_url, sb_fqdn = sys.argv[1], sys.argv[2]
cred = AzureCliCredential()
container = BlobServiceClient(blob_url, credential=cred).get_container_client("invoice-uploads")
correlation = f"compat-{uuid.uuid4().hex[:16]}"
sid = uuid.uuid4().hex

# 1) One blob the subscription must deliver, one it must ignore (outside submissions/).
t0 = time.time()
container.upload_blob(f"submissions/{sid}.json", b'{"probe": true}', client_request_id=correlation)
container.upload_blob(
    f"other/{sid}.json", b'{"probe": "ignore"}', client_request_id="compat-ignore"
)
print(f"uploaded at {time.strftime('%H:%M:%S')} correlation={correlation}")

with ServiceBusClient(sb_fqdn, cred) as client:
    receiver = client.get_queue_receiver(
        "invoice-events", receive_mode=ServiceBusReceiveMode.PEEK_LOCK, max_wait_time=90
    )
    with receiver:
        first = receiver.receive_messages(max_message_count=1, max_wait_time=90)
        assert first, "NO MESSAGE within 90 s — check Event Grid metrics (DeliveryAttemptFailCount)"
        m = first[0]
        event = json.loads(b"".join(bytes(p) for p in m.body))
        print(f"received after {time.time() - t0:.1f}s")
        print("  eventType   :", event.get("eventType"))
        print("  subject     :", event.get("subject"))
        print("  data.api    :", event["data"].get("api"))
        print("  message_id  :", m.message_id, "(event id:", event.get("id"), ")")
        print(
            "  correlation :",
            m.correlation_id,
            "OK" if m.correlation_id == correlation else "MISMATCH",
        )
        props = {
            k.decode() if isinstance(k, bytes) else k: v
            for k, v in (m.application_properties or {}).items()
        }
        print("  aeg headers :", {k: v for k, v in props.items() if str(k).startswith("aeg-")})
        print("  delivery_count (1st receive, raw SDK value):", m.delivery_count)
        # 2) Delivery-count semantics: abandon once, receive again.
        receiver.abandon_message(m)
        again = receiver.receive_messages(max_message_count=1, max_wait_time=30)
        print("  delivery_count (2nd receive after abandon):", again[0].delivery_count)
        receiver.complete_message(again[0])
        # 3) The non-matching blob must not have produced a message.
        extra = receiver.receive_messages(max_message_count=5, max_wait_time=20)
        print("  extra messages (expected 0):", len(extra))
        for e in extra:
            receiver.complete_message(e)
print("PROBE COMPLETE")

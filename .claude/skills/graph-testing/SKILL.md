---
name: graph-testing
description: How to test rah against the fake Graph -- where fixtures live, how to get a wired GraphClient, how to inject errors, and the patterns steps 4-8 lean on.
---

Everything that talks to the Graph API is tested against `tests/fake_graph.py`,
an in-memory mailbox behind an `httpx.MockTransport`. No test hits the network,
and no test sleeps for real. If you're writing code that lists, moves, or
patches messages, you test it here.

One blind spot to respect: the fake implements *our reading* of the Graph API,
so it can't catch a misread. Step 4 shipped a category check against
`/outlookCategories` with the fake happily serving that same wrong path; the
real endpoint is `/outlook/masterCategories`, and only a run against the real
tenant caught it. When client code grows a new endpoint, check the URL against
the Graph docs and have Nate exercise it for real once before trusting the
green tests.

## Getting a client

Two conftest fixtures do the wiring:

- `fake_graph` -- a fresh `FakeGraph` with only the well-known folders seeded
  (`msgFolderRoot` and `Inbox`). Their ids are on the object as
  `fake_graph.root_id` and `fake_graph.inbox_id`.
- `graph_client` -- a `GraphClient` already pointed at that `fake_graph`, with
  a static token and a recording `sleep`.

```python
def test_lists_what_i_put_there(fake_graph, graph_client):
    fake_graph.add_message(fake_graph.inbox_id, subject="hi")
    messages = graph_client.list_messages(fake_graph.inbox_id)
    assert len(messages) == 1
```

`graph_client` and `fake_graph` share one mailbox, so seed state on `fake_graph`
before you call the client. To assert on waits, take the `sleep_spy` fixture too
and read `sleep_spy.calls`.

## Seeding state

Build the mailbox with methods on `fake_graph`:

- `add_folder(display_name, parent_id=..., well_known=...)` returns the folder
  dict; keep its `["id"]` to hang messages or child folders off it.
- `add_message(folder_id, ...)` clones a canned message and drops it in a
  folder. Overrides: `subject`, `internet_message_id`, `categories`,
  `properties` (a dict of extended-property id to value), and `fixture` to pick
  a different base file.
- `add_category(display_name, color)` seeds the master category list.

Canned message bodies are files under `tests/data/graph/`. `message.json` is a
plain REDCap-style alert; `message_with_state.json` already carries the
`rah-retries-left` / `rah-retry-time` extended properties, which is the one to
reach for when you're testing the property round-trip. House rule: message
payloads are files, not inline strings. A new realistic message shape means a
new file there.

## What the fake actually models

- Extended properties come back only when you `$expand` them, and only the ids
  you asked for -- same as real Graph. `add_message(..., properties={...})`
  stores them; `list_messages(folder, expand_properties=(id, ...))` reads them.
- A move reissues the message id. `move_message` returns the message with its
  new id, and the old id 404s afterward. Anything doing crash recovery has to
  cope with that, so the fake makes you cope with it.
- Paging: set `fake_graph.page_size = 2`, add more than that, and
  `list_messages` will follow `@odata.nextLink` across pages. `fake_graph.requests`
  logs every `(method, url)` if you want to prove it made three calls, not one.

## Injecting failures

Forced responses jump the queue -- the next request (or few) gets them before
normal routing:

```python
fake_graph.enqueue_status(429, retry_after=2)   # client retries, sleeps 2
fake_graph.enqueue_status(500)                  # retryable, default backoff
fake_graph.enqueue_status(403, json_body={"error": {"code": "ErrorAccessDenied"}})
fake_graph.enqueue_status(200, content=b"{ not json")   # malformed body
fake_graph.enqueue_exception(httpx.ConnectError("boom"))  # transport failure
```

`GraphClient` turns all of these into `GraphError`, so catch that one thing.
`GraphError.status` is the HTTP status (None for a transport failure) and
`GraphError.code` is Graph's own error code when the body had one. Retries are
capped, so N enqueued 500s in a row exhaust the attempts and raise.

## Testing doctor (and other CLI code that builds a client)

`install_fake_graph` swaps the `GraphClient` name in doctor's module for a
factory wired to a fake, the same trick `install_fake_msal` uses. Pass it a
seeded `FakeGraph`, or let it make an empty one:

```python
def test_doctor_sees_my_folder(write_config, cache_with_account,
                               fake_app, install_fake_msal, install_fake_graph):
    install_fake_msal(fake_app(accounts=ACCOUNTS, silent_result=REFRESHED))
    fake = install_fake_graph()
    fake.add_folder("rah", parent_id=fake.root_id)
    ...
```

If your command reaches a refreshed token, it will try Graph -- so any doctor
test with working secrets and a live-ish token needs `install_fake_graph`, or it
falls through to the real network.

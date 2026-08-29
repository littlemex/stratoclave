"""A rate-limited route must still parse its request.

The defect this pins is a silent one, and it is invisible in review because the
decorator, the route and the request model are each individually correct.

Route modules use `from __future__ import annotations`, so their annotations are
strings that only the defining module's globals can resolve. `functools.wraps`
cannot copy `__globals__`, so a framework that evaluates those strings against the
wrapper it was handed looks for the route's request model inside
`core.rate_limit_ddb` — and FastAPI's response to a parameter it cannot resolve is
not an error but a **query parameter**. The route then stops parsing its body and
answers every well-formed request with `422 Field required` for `query.body`,
which is what 22 tests of the Layer 5 authorize/capture API were doing.

Two checks, deliberately at different levels:

* the signature check fails on any framework version, because it is about what the
  decorator publishes;
* the end-to-end check is the symptom a caller would actually see, and it fails
  only on versions that resolve against the wrapper — which is exactly why the
  first check exists.
"""
from __future__ import annotations

import inspect
from typing import Annotated

import pytest
from fastapi import Body, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

import core.rate_limit_ddb as rl
from core.rate_limit_ddb import DynamoRateLimiter


class AuthorizeProbe(BaseModel):
    """Defined HERE on purpose: `core.rate_limit_ddb` cannot resolve this name."""

    amount_microusd: int


def _key(request: Request) -> str:
    return request.headers.get("x-test-ip", "1.2.3.4")


_dependency_calls: list[int] = []


def principal() -> str:
    _dependency_calls.append(1)
    return "svc"


@pytest.fixture
def limiter(monkeypatch):
    # The limit itself is covered by test_rate_limit_ddb.py; here it must simply
    # not reach DynamoDB.
    monkeypatch.setattr(rl, "_check", lambda *a, **kw: None)
    return DynamoRateLimiter(client_key_func=_key)


def test_the_decorator_publishes_resolved_annotations(limiter):
    @limiter.limit("10/minute")
    def handler(request: Request, body: AuthorizeProbe) -> dict:
        return {"amount": body.amount_microusd}

    published = inspect.signature(handler)
    annotations = {n: p.annotation for n, p in published.parameters.items()}
    assert annotations["body"] is AuthorizeProbe, (
        "the wrapper published a string annotation; a framework resolving it "
        "against core.rate_limit_ddb will not find the route's own models"
    )
    assert annotations["request"] is Request
    assert published.return_annotation is dict


def test_a_rate_limited_route_still_reads_its_body(limiter):
    app = FastAPI()

    @app.post("/authorize")
    @limiter.limit("10/minute")
    def authorize(request: Request, body: AuthorizeProbe):
        return {"amount": body.amount_microusd}

    resp = TestClient(app).post("/authorize", json={"amount_microusd": 1_000_000})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"amount": 1_000_000}


def test_annotated_metadata_survives(limiter):
    """`Annotated` carries the framework's instructions, not decoration.

    Resolving without `include_extras` turns `Annotated[Model, Body(embed=True)]`
    into a bare `Model` and `Annotated[X, Depends(f)]` into a body parameter — the
    same silent misclassification this decorator exists to prevent, and on an auth
    dependency it is a security defect rather than a 422.
    """
    @limiter.limit("10/minute")
    def handler(
        request: Request,
        body: Annotated[AuthorizeProbe, Body(embed=True)],
        who: Annotated[str, Depends(principal)],
    ):
        return {}

    annotations = {
        n: p.annotation for n, p in inspect.signature(handler).parameters.items()
    }
    # Compared structurally: two `Body(embed=True)` calls are different instances,
    # so equality of the annotations would be testing FieldInfo's __eq__.
    import typing as _t

    from fastapi import params

    body_type, body_meta = _t.get_args(annotations["body"])
    assert body_type is AuthorizeProbe
    assert isinstance(body_meta, params.Body) and body_meta.embed is True

    who_type, who_meta = _t.get_args(annotations["who"])
    assert who_type is str
    assert isinstance(who_meta, params.Depends) and who_meta.dependency is principal


def test_an_embedded_body_is_still_embedded_end_to_end(limiter):
    app = FastAPI()

    @app.post("/authorize")
    @limiter.limit("10/minute")
    def authorize(request: Request, body: Annotated[AuthorizeProbe, Body(embed=True)]):
        return {"amount": body.amount_microusd}

    client = TestClient(app)
    embedded = client.post("/authorize", json={"body": {"amount_microusd": 5}})
    assert embedded.status_code == 200, embedded.text
    assert embedded.json() == {"amount": 5}
    # And the unembedded shape must still be refused, i.e. the metadata did not
    # merely survive as decoration.
    assert client.post("/authorize", json={"amount_microusd": 5}).status_code == 422


def test_a_dependency_still_runs(limiter):
    _dependency_calls.clear()
    app = FastAPI()

    @app.post("/authorize")
    @limiter.limit("10/minute")
    def authorize(request: Request, body: AuthorizeProbe,
                  who: Annotated[str, Depends(principal)]):
        return {"who": who}

    resp = TestClient(app).post("/authorize", json={"amount_microusd": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"who": "svc"}
    assert _dependency_calls, "the dependency was dropped: it became a request field instead"


def test_an_async_handler_is_stamped_too(limiter):
    """Most FastAPI routes are async, and the async wrapper is a different object."""
    app = FastAPI()

    @app.post("/authorize")
    @limiter.limit("10/minute")
    async def authorize(request: Request, body: AuthorizeProbe):
        return {"amount": body.amount_microusd}

    resp = TestClient(app).post("/authorize", json={"amount_microusd": 3})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"amount": 3}


def test_one_unresolvable_annotation_does_not_cost_the_others(limiter, monkeypatch):
    """Whole-signature resolution would let an unrelated forward reference put the
    body back on the 422. The body must still be resolved, and the failure must be
    logged rather than passed over."""

    warnings: list[dict] = []
    monkeypatch.setattr(rl._log, "warning", lambda event, **kw: warnings.append({"event": event, **kw}))

    wrapped = limiter.limit("10/minute")(_handler_with_an_unresolvable_return)
    annotations = {
        n: p.annotation for n, p in inspect.signature(wrapped).parameters.items()
    }
    assert annotations["body"] is AuthorizeProbe, (
        "one unresolvable annotation cost the body its resolution"
    )
    assert warnings and warnings[0]["event"] == "rate_limit_signature_unresolved"
    assert "return" in warnings[0]["parameters"]


def test_the_limit_is_still_applied_after_the_signature_is_stamped(monkeypatch):
    """The whole point of the wrapper is the check; stamping a signature must not
    have replaced the handler with something that skips it."""
    seen: list[tuple] = []
    monkeypatch.setattr(rl, "_check", lambda *a, **kw: seen.append(a))
    limiter = DynamoRateLimiter(client_key_func=_key)

    app = FastAPI()

    @app.post("/authorize")
    @limiter.limit("7/minute")
    def authorize(request: Request, body: AuthorizeProbe):
        return {"ok": True}

    assert TestClient(app).post("/authorize", json={"amount_microusd": 1}).status_code == 200
    assert seen, "the rate-limit check did not run"


def _handler_with_an_unresolvable_return(request: Request, body: AuthorizeProbe) -> NeverDefined:  # noqa: F821
    """Module-level on purpose: a route's annotations are resolved against module
    globals, so a handler defined inside a test would fail for the wrong reason."""
    return {}

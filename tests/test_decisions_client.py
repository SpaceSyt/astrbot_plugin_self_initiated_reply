"""Standalone decision transport, parsing, and side-effect-free replay tests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "standalone_decisions_client", ROOT / "decisions_client.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def messages():
    return [
        {"id": "-101", "text": "game music", "name": "A", "sender_id": "a"},
        {"id": "102", "text": "another question", "name": "B", "sender_id": "b"},
        {"id": "103", "text": "previous bot reply", "eligible": False},
    ]


def response(choice="reply:-101", confidence=0.9):
    options = ["ignore", "wait", "reply:-101", "reply:102"]
    return {
        "answers": [
            {
                "type": "choice",
                "name": "reply_target",
                "choice": choice,
                "confidence": confidence,
                "probabilities": [
                    {"value": item, "probability": 1.0 if item == choice else 0.0}
                    for item in options
                ],
            }
        ],
        "usage": {"input_tokens": 123, "output_tokens": 0, "secret": "must not escape"},
    }


def test_request_separates_policy_and_untrusted_evidence():
    payload = module.build_request(messages(), "game music")
    assert payload["model"] == "gpt-6-luna"
    assert payload["input"][0]["role"] == "developer"
    assert payload["input"][1]["role"] == "user"
    options = payload["questions"][0]["choices"]
    assert [item["value"] for item in options] == ["ignore", "wait", "reply:-101", "reply:102"]
    assert "previous bot reply" in payload["input"][1]["content"]


@pytest.mark.parametrize("choice", ["ignore", "wait", "reply:-101", "reply:102"])
def test_choice_and_negative_message_id_preserved(choice):
    payload = module.build_request(messages(), "music")
    result = module.parse_response(response(choice), payload)
    assert result["action"] == ("reply" if choice.startswith("reply:") else choice)
    assert result["target_id"] == (choice[6:] if choice.startswith("reply:") else None)
    assert result["usage"] == {"input_tokens": 123, "output_tokens": 0}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.update(answers=[]),
        lambda body: body["answers"][0].update(name="wrong"),
        lambda body: body["answers"][0].update(choice="reply:103"),
        lambda body: body["answers"][0].update(choice=True),
        lambda body: body["answers"][0].update(confidence=float("nan")),
        lambda body: body["answers"][0].update(confidence=True),
        lambda body: body["answers"][0].update(probabilities=[]),
        lambda body: body["answers"][0]["probabilities"][0].update(probability=float("inf")),
        lambda body: body["answers"][0]["probabilities"][0].update(value="reply:other_group"),
    ],
)
def test_invalid_response_fails_closed(mutation):
    payload = module.build_request(messages(), "music")
    body = response()
    mutation(body)
    result = module.parse_response(body, payload)
    assert result["action"] == "ignore"
    assert result["target_id"] is None
    assert result["reason_code"] == "invalid_response"


def test_refusal_and_low_confidence():
    payload = module.build_request(messages(), "music")
    refusal = {"answers": [{"name": "reply_target", "type": "refusal"}]}
    assert module.parse_response(refusal, payload)["reason_code"] == "refusal"
    result = module.parse_response(response(confidence=0.3), payload, 0.7)
    assert result["reason_code"] == "low_confidence"
    assert result["target_id"] is None


@pytest.mark.parametrize(
    "bad", [[], [{"id": "x", "text": "a"}] * 2, [{"id": 1, "text": "a"}], messages() * 100]
)
def test_bad_candidate_inputs_rejected(bad):
    with pytest.raises(ValueError):
        module.build_request(bad, "music")


@pytest.mark.asyncio
async def test_http_contract_and_usage():
    captured = []

    def handler(request):
        captured.append(request)
        assert request.url == "https://example.test/v1/decisions"
        assert request.headers["authorization"] == "Bearer private-test-key"
        return httpx.Response(200, json=response())

    client = module.DecisionsClient(
        "private-test-key",
        "https://example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.decide(messages=messages(), interests="music")
        assert result["action"] == "reply"
        assert result["target_id"] == "-101"
        assert result["elapsed_sec"] >= 0
        assert len(captured) == 1
        assert "private-test-key" not in json.dumps(result)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 429, 500])
async def test_http_failures_not_retried_or_exposed(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private-test-key", headers={"location": "https://other.test"}
        )

    client = module.DecisionsClient("private-test-key", transport=httpx.MockTransport(handler))
    try:
        result = await client.decide(messages=messages(), interests="music")
        assert result["reason_code"] == f"http_{status}"
        assert len(calls) == 1
        assert "private-test-key" not in json.dumps(result)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_total_timeout_and_cancellation():
    async def handler(request):
        await asyncio.sleep(10)

    client = module.DecisionsClient("test", timeout=0.01, transport=httpx.MockTransport(handler))
    try:
        result = await client.decide(messages=messages(), interests="music")
        assert result["reason_code"] == "timeout"
        task = asyncio.create_task(client.decide(messages=messages(), interests="music"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_background_only_does_not_call_api():
    def handler(request):
        pytest.fail("Background-only input must not call the API")

    client = module.DecisionsClient("test", transport=httpx.MockTransport(handler))
    try:
        result = await client.decide(
            messages=[{"id": "1", "text": "bot", "eligible": False}],
            interests="music",
        )
        assert result["reason_code"] == "no_candidates"
    finally:
        await client.aclose()


def test_replay_default_is_offline():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "replay_decisions.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(result.stdout)
    assert parsed["action"] == "wait"
    assert parsed["reason_code"] == "selected"


def test_module_does_not_import_astrbot():
    result = subprocess.run(
        [sys.executable, "-c", "import decisions_client, sys; assert 'astrbot' not in sys.modules"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

"""Replay a synthetic decision without importing or connecting to AstrBot."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from decisions_client import DecisionsClient, build_request, parse_response  # noqa: E402


async def run(args: argparse.Namespace) -> dict:
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    messages, interests = fixture["messages"], fixture["interests"]
    payload = build_request(messages, interests)
    if not args.live:
        if args.response:
            response = json.loads(args.response.read_text(encoding="utf-8"))
        else:
            response = fixture["response"]
        return parse_response(response, payload, args.min_confidence)
    client = DecisionsClient(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        timeout=args.timeout,
    )
    try:
        return await client.decide(
            messages=messages,
            interests=interests,
            min_confidence=args.min_confidence,
        )
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fixture",
        nargs="?",
        type=Path,
        default=ROOT / "examples" / "decisions_replay.json",
    )
    parser.add_argument("--response", type=Path, help="Recorded API response for offline replay")
    parser.add_argument("--live", action="store_true", help="Explicitly make one paid API request")
    parser.add_argument("--min-confidence", type=float, default=0.7)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if args.live and args.response:
        parser.error("--live and --response cannot be combined")
    try:
        result = asyncio.run(run(args))
    except (OSError, ValueError, KeyError, TypeError):
        # Configuration and file errors can contain URLs or credentials: no raw exception output.
        print(json.dumps({"error": "invalid_fixture_or_configuration"}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["reason_code"] in {"selected", "low_confidence", "no_candidates"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
Push Cherry's system prompt to Retell.

Usage:
    cd voice_agent
    python push_to_retell.py              # dry run — shows diff only
    python push_to_retell.py --push       # apply to Retell

Source of truth: voice_agent/cherry_system_prompt.md
Target: Retell LLM llm_f9729a7312c053ec73505ba465ae

Requires RETELL_API_KEY in env or ../.env
"""

import argparse
import os
import sys

import requests

LLM_ID = "llm_f9729a7312c053ec73505ba465ae"
PROMPT_FILE = os.path.join(os.path.dirname(__file__), "cherry_system_prompt.md")


def _api_key() -> str:
    key = os.environ.get("RETELL_API_KEY", "").strip()
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("RETELL_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        print("ERROR: RETELL_API_KEY not found in env or .env", file=sys.stderr)
        sys.exit(1)
    return key


def fetch_live_prompt(api_key: str) -> str:
    resp = requests.get(
        f"https://api.retellai.com/get-retell-llm/{LLM_ID}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("general_prompt", "")


def push_prompt(api_key: str, prompt: str) -> None:
    resp = requests.patch(
        f"https://api.retellai.com/update-retell-llm/{LLM_ID}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"general_prompt": prompt},
        timeout=15,
    )
    resp.raise_for_status()
    import datetime
    ts = resp.json().get("last_modification_timestamp", 0)
    updated = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M AEST") if ts else "unknown"
    print(f"Pushed successfully. Retell timestamp: {updated}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Push Cherry system prompt to Retell")
    parser.add_argument("--push", action="store_true", help="Actually push (default is dry run)")
    args = parser.parse_args()

    if not os.path.exists(PROMPT_FILE):
        print(f"ERROR: {PROMPT_FILE} not found", file=sys.stderr)
        sys.exit(1)

    with open(PROMPT_FILE) as f:
        local_prompt = f.read()

    api_key = _api_key()
    live_prompt = fetch_live_prompt(api_key)

    if local_prompt == live_prompt:
        print("Already in sync — no changes needed.")
        return

    # Show a simple diff summary
    local_lines = local_prompt.splitlines()
    live_lines = live_prompt.splitlines()
    added = sum(1 for l in local_lines if l not in live_lines)
    removed = sum(1 for l in live_lines if l not in local_lines)
    print(f"Diff: ~{added} lines added, ~{removed} lines removed")
    print(f"Local:  {len(local_prompt):,} chars | Live: {len(live_prompt):,} chars")

    if not args.push:
        print("\nDry run — use --push to apply.")
        return

    push_prompt(api_key, local_prompt)


if __name__ == "__main__":
    main()

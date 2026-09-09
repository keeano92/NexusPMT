#!/usr/bin/env python3
"""Interactively write gitignored .env + secrets/kalshi_private.pem."""

from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"
SECRETS_DIR = ROOT / "secrets"
PEM_PATH = SECRETS_DIR / "kalshi_private.pem"


def _read_multiline(prompt: str) -> str:
    print(prompt)
    print("(Paste PEM including BEGIN/END lines. End with a blank line.)")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "" and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    if not EXAMPLE.exists():
        print("Missing .env.example", file=sys.stderr)
        return 1

    print("=== NexusPMT credential setup ===")
    print("Values are written only to local .env / secrets/ (gitignored).\n")

    key_id = input("KALSHI_KEY_ID: ").strip()
    pem = _read_multiline("Kalshi RSA private key PEM:")
    if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
        print("Does not look like a PEM private key.", file=sys.stderr)
        return 1

    xai = getpass.getpass("XAI_API_KEY (hidden): ").strip()
    op = input("NEXUSPMT_OPERATOR_TOKEN [auto-generate if blank]: ").strip()
    if not op:
        op = secrets.token_urlsafe(32)
        print(f"Generated operator token: {op}")

    env_default = EXAMPLE.read_text(encoding="utf-8")
    lines = []
    for line in env_default.splitlines():
        if line.startswith("KALSHI_KEY_ID="):
            lines.append(f"KALSHI_KEY_ID={key_id}")
        elif line.startswith("XAI_API_KEY="):
            lines.append(f"XAI_API_KEY={xai}")
        elif line.startswith("NEXUSPMT_OPERATOR_TOKEN="):
            lines.append(f"NEXUSPMT_OPERATOR_TOKEN={op}")
        else:
            lines.append(line)

    SECRETS_DIR.mkdir(mode=0o700, exist_ok=True)
    PEM_PATH.write_text(pem, encoding="utf-8")
    try:
        PEM_PATH.chmod(0o600)
    except OSError:
        pass

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass

    print(f"\nWrote {ENV_PATH}")
    print(f"Wrote {PEM_PATH}")
    print("Done. Do not commit these files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Dhan access token renewal.

Calls Dhan's RenewToken endpoint with the current token + client ID as
authentication, then rewrites the DHAN_ACCESS_TOKEN line of .env in place
(every other line preserved byte-for-byte). Imported by DailySchedulerThread
in main.py and invoked once per trading day (market holidays skipped) after
the EOD archive and metrics-db cleanup jobs.

Any failure raises — DailySchedulerThread catches and logs to the terminal.
There is no rollback: Dhan invalidates the previous token the moment renewal
succeeds, so restoring .env to its prior state would write a dead credential.
A pre-write copy of .env is dropped at data/.env.bak so the non-token lines
can be recovered manually if the rewrite ever corrupts the file.

renew_token() returns the new token. The EOD scheduler hands it straight to
the running engine's quote fetcher. When this script runs standalone (the
renew_token wrapper, a separate container), the running engine picks the new
token up from .env on its next failed request — see
DhanQuoteFetcher._reload_token_if_changed in engine.py.
"""

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
BACKUP_PATH = PROJECT_ROOT / "data" / ".env.bak"

DHAN_RENEW_URL = "https://api.dhan.co/v2/RenewToken"
TOKEN_KEY = "DHAN_ACCESS_TOKEN"
REQUEST_TIMEOUT = 30


def _last4(token: str) -> str:
    return token[-4:] if token and len(token) >= 4 else "????"


def _write_token_to_env(new_token: str) -> None:
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env not found at {ENV_PATH}")

    with ENV_PATH.open("r", encoding="utf-8") as f:
        original = f.read()

    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_PATH.write_text(original, encoding="utf-8")

    lines = original.splitlines(keepends=True)
    new_line = f"{TOKEN_KEY}={new_token}\n"
    replaced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(f"{TOKEN_KEY}="):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(new_line)

    # In-place rewrite — atomic rename fails on Docker single-file bind mounts.
    with ENV_PATH.open("r+", encoding="utf-8") as f:
        f.seek(0)
        f.truncate()
        f.write("".join(lines))
        f.flush()
        os.fsync(f.fileno())

    with ENV_PATH.open("r", encoding="utf-8") as f:
        if new_token not in f.read():
            raise RuntimeError(
                f"writeback verification failed; .env may be corrupted. "
                f"pre-write copy at {BACKUP_PATH}"
            )


def renew_token() -> str:
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env not found at {ENV_PATH}")

    load_dotenv(dotenv_path=ENV_PATH, override=True)

    client_id = os.getenv("DHAN_CLIENT_ID")
    access_token = os.getenv("DHAN_ACCESS_TOKEN")
    if not client_id or not access_token:
        raise RuntimeError("DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN missing from .env")

    headers = {
        "access-token": access_token.strip(),
        "dhanClientId": str(client_id).strip(),
    }
    response = requests.get(DHAN_RENEW_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    data = response.json()
    new_token = data.get("token") or data.get("accessToken")
    if not new_token:
        raise RuntimeError(f"Dhan response missing token field: {data}")

    _write_token_to_env(new_token)

    logger.info(
        f"Dhan token renewed (...{_last4(access_token)} -> ...{_last4(new_token)})"
    )
    return new_token


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

    print("Renewing Dhan access token...")
    print()
    try:
        renew_token()
    except Exception as e:
        print()
        print(f"FAILED: {e}")
        print()
        print("If Dhan returned a new token before failure, the old token may already be invalidated.")
        print("A pre-write copy of .env is at data/.env.bak.")
        sys.exit(1)

    print()
    print("SUCCESS: Token renewed and written to .env.")
    print("Backup of previous .env saved to data/.env.bak.")

"""brain-device-token: mint, list, or revoke this machine's device token.

Signs in through the hosted ``/mcp`` with the normal browser Google OAuth
flow, calls the ``device_token_*`` tools, and writes a new token straight to
``~/.config/brain/device-token`` (``BRAIN_CLIENT_DIR``). The token never
passes through a model conversation or a terminal scrollback unless
``--print`` is given. Asking an agent to call ``device_token_create`` works
too, but then the token sits in that session's transcript.

  brain-device-token create --device work-laptop [--profiles work] [--scopes ingest,hook-read]
  brain-device-token create --device work-laptop --force   # revoke the token on disk, then mint
  brain-device-token list
  brain-device-token revoke ID
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .client import base_url, client_dir
from .device_tokens import hash_token, looks_like_token


async def _call_tool(name: str, args: dict) -> dict:
    from fastmcp import Client
    from fastmcp.client.auth import OAuth

    url = base_url() + "/mcp"
    async with Client(url, auth=OAuth(url, client_name="brain-device-token")) as mcp:
        result = await mcp.call_tool(name, args)
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise RuntimeError("tool returned no data")


def write_token(token: str, path: Path, *, force: bool) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.replace(tmp, path)
    return path


def main(argv: list[str] | None = None, *, call=None) -> int:
    parser = argparse.ArgumentParser(prog="brain-device-token", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create", help="mint a token for this machine")
    create.add_argument("--device", required=True)
    create.add_argument("--profiles", default="")
    create.add_argument("--scopes", default="")
    create.add_argument(
        "--force",
        action="store_true",
        help="replace an existing token file and revoke the token it holds",
    )
    create.add_argument("--print", action="store_true", help="also print the token")
    sub.add_parser("list", help="list this account's device tokens")
    rev = sub.add_parser("revoke", help="revoke a token by id")
    rev.add_argument("id", type=int)
    args = parser.parse_args(argv)
    call = call or (lambda name, a: asyncio.run(_call_tool(name, a)))
    path = client_dir() / "device-token"

    if args.cmd == "create":
        if path.exists() and not args.force:
            print(f"brain-device-token: {path} exists; pass --force to replace it", file=sys.stderr)
            return 2
        if args.force and path.exists():
            old = path.read_text(encoding="utf-8").strip()
            if looks_like_token(old):
                revoked = call("device_token_revoke_hash", {"token_hash": hash_token(old)})
                if "error" in revoked and "no active token" not in revoked["error"]:
                    print(f"brain-device-token: {revoked['error']}", file=sys.stderr)
                    return 1
                if revoked.get("revoked") is not None:
                    print(f"revoked previous token {revoked['revoked']}")
        out = call(
            "device_token_create",
            {"device": args.device, "profiles": args.profiles, "scopes": args.scopes},
        )
        if "error" in out:
            print(f"brain-device-token: {out['error']}", file=sys.stderr)
            return 1
        write_token(out["token"], path, force=args.force)
        print(
            f"token {out['id']} ({out['prefix']}...) for {out['device']}, scopes "
            f"{','.join(out['scopes'])}, profiles {','.join(out['profiles'])}, saved to {path}"
        )
        if args.print:
            print(out["token"])
        return 0
    if args.cmd == "list":
        out = call("device_token_list", {})
        if "error" in out:
            print(f"brain-device-token: {out['error']}", file=sys.stderr)
            return 1
        for t in out["tokens"]:
            state = f"revoked {t['revoked_at']}" if t["revoked_at"] else f"last used {t['last_used_at'] or 'never'}"
            print(f"{t['id']:>4}  {t['prefix']}...  {t['device']:<20} {','.join(t['scopes']):<16} {state}")
        return 0
    out = call("device_token_revoke", {"id": args.id})
    if "error" in out:
        print(f"brain-device-token: {out['error']}", file=sys.stderr)
        return 1
    print(f"revoked {out['revoked']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

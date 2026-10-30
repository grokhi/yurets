"""Link Plex through its browser PIN flow without printing an account/server token."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx


async def link(args):
    output = Path(args.output)
    if output.exists():
        raise RuntimeError("Output file already exists; choose a new --output path")
    pending = output.with_name(output.name + ".pending")
    saved = json.loads(pending.read_text()) if pending.exists() else None
    if saved and saved.get("server_id") != args.server_id:
        raise RuntimeError("Pending authorization belongs to another server; choose a new --output")
    client_id = saved["client_id"] if saved else str(uuid.uuid4())
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": "Yurets",
        "X-Plex-Client-Identifier": client_id,
    }
    async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=False) as client:
        if saved:
            token = saved["token"]
            print("Продолжаю ранее подтверждённое подключение Plex.", flush=True)
        else:
            response = await client.post("https://plex.tv/api/v2/pins", data={"strong": "true"})
            response.raise_for_status()
            pin = response.json()
            auth_url = "https://app.plex.tv/auth#?" + urlencode(
                {
                    "clientID": client_id,
                    "code": pin["code"],
                    "context[device][product]": "Радио Юрец",
                }
            )
            print("Откройте ссылку и подтвердите подключение Радио Юрец:", flush=True)
            print(auth_url, flush=True)
            deadline = time.monotonic() + min(float(pin.get("expiresIn", 300)), 600)
            token = None
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                response = await client.get(
                    f"https://plex.tv/api/v2/pins/{pin['id']}", params={"code": pin["code"]}
                )
                response.raise_for_status()
                token = response.json().get("authToken")
                if token:
                    break
            if not token:
                raise RuntimeError("Plex confirmation expired; run the command again")
            # Retain the approved login if the server domain still needs network
            # access or the user needs to choose between music libraries.
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as file:
                json.dump(
                    {"server_id": args.server_id, "client_id": client_id, "token": token}, file
                )
        response = await client.get(
            "https://plex.tv/api/v2/resources",
            params={"includeHttps": 1},
            headers={"X-Plex-Token": token},
        )
        response.raise_for_status()
        servers = [
            s
            for s in response.json()
            if s.get("clientIdentifier") == args.server_id
            and "server" in s.get("provides", "").split(",")
        ]
        if not servers:
            raise RuntimeError("The selected Plex server is not accessible to this account")
        server = servers[0]
        server_token = server.get("accessToken") or token
        candidates = [
            c["uri"]
            for c in server.get("connections", [])
            if urlsplit(c.get("uri", "")).scheme == "https"
        ]
        for url in candidates:
            host = urlsplit(url).hostname
            try:
                response = await client.get(
                    url.rstrip("/") + "/library/sections", headers={"X-Plex-Token": server_token}
                )
                response.raise_for_status()
                libraries = [
                    s
                    for s in response.json()["MediaContainer"].get("Directory", [])
                    if s.get("type") == "artist"
                ]
            except (httpx.HTTPError, ValueError, KeyError):
                print(
                    f"Сервер недоступен через {host}; проверьте доступ к этому домену.", flush=True
                )
                continue
            if args.library_id:
                libraries = [s for s in libraries if str(s["key"]) == args.library_id]
            if len(libraries) != 1:
                print("Музыкальные библиотеки:", [(s["key"], s.get("title")) for s in libraries])
                raise RuntimeError("Choose exactly one music library with --library-id")
            values = {
                "YURETS_PLEX_URL": url,
                "YURETS_PLEX_TOKEN": server_token,
                "YURETS_PLEX_LIBRARY_ID": str(libraries[0]["key"]),
                "YURETS_PLEX_SERVER_ID": args.server_id,
            }
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as file:
                for key, value in values.items():
                    file.write(f"{key}={json.dumps(value)}\n")
            pending.unlink(missing_ok=True)
            print(f"Подключение сохранено в {output}. Токен не выводился.")
            print(f"Plex: {host}; музыкальная библиотека: {libraries[0]['key']}")
            return
        raise RuntimeError("No verified HTTPS connection to the selected Plex server is available")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--library-id")
    parser.add_argument("--output", default=".env.plex")
    args = parser.parse_args()
    try:
        asyncio.run(link(args))
    except httpx.HTTPError:
        # HTTP exceptions may contain PINs or other URL parameters. Never print them.
        raise SystemExit(
            "Plex API unavailable: check network access to plex.tv and the server"
        ) from None
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()

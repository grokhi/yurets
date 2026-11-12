"""Production diagnostics: output only capacity and sanitized connectivity results."""
import asyncio
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from src.settings import Settings
from src.services.program import DailyProgram


def emit(label, **values):
    print(json.dumps({'check': label, **values}), flush=True)


def failure(exc):
    chain = []
    while exc and len(chain) < 4:
        chain.append({'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None)})
        exc = exc.__cause__ or exc.__context__
    return chain


async def main():
    settings = Settings()
    for path in ('/tmp', str(settings.data_dir)):
        usage, stat = shutil.disk_usage(path), os.statvfs(path)
        emit('storage', path=path, free_mb=usage.free // 1048576, total_mb=usage.total // 1048576,
             free_inodes=stat.f_favail)
    root = Path('/tmp')
    emit('temporary_audio', directories=len(list(root.glob('yurets-plex-*'))),
         files=sum(1 for p in root.glob('yurets-plex-*/*') if p.is_file()),
         size_mb=sum(p.stat().st_size for p in root.glob('yurets-plex-*/*') if p.is_file()) // 1048576)
    headers = {'Accept': 'application/json', 'X-Plex-Token': settings.plex_token.get_secret_value(),
               'X-Plex-Client-Identifier': 'yurets-radio-diagnostics'}
    base = settings.plex_url.rstrip('/')
    emit('configured_endpoint', host=urlsplit(base).hostname, port=urlsplit(base).port)
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        async def probe(url, label, token=None, media=False):
            try:
                h = dict(headers)
                if token: h['X-Plex-Token'] = token
                if media: h['Range'] = 'bytes=0-65535'
                started = time.monotonic()
                async with client.stream('GET', url, headers=h) as response:
                    count = 0
                    if media and response.status_code in (200, 206):
                        async for chunk in response.aiter_bytes(8192):
                            count += len(chunk)
                            if count >= 65536: break
                    emit(label, status=response.status_code, bytes=count, elapsed=round(time.monotonic()-started, 2))
            except Exception as exc:
                emit(label, errors=failure(exc))
        await probe(base + '/identity', 'configured_identity')
        program = DailyProgram(settings, None, None)
        day = datetime.now(program.timezone).date()
        with sqlite3.connect(f'file:{Path(settings.data_dir) / "radio.sqlite3"}?mode=ro', uri=True) as db:
            plans = db.execute('SELECT entries FROM daily_plans WHERE namespace=? AND day IN (?,?)',
                               (program.namespace, str(day), str(day-timedelta(days=1)))).fetchall()
        entries = [entry for row in plans for entry in json.loads(row[0])]
        current = next((e for e in entries if e['start'] <= time.time() < e['end']), None)
        if current:
            track = current['track']
            emit('scheduled_track', track_id=track['id'], album=track['album'], end_at=current['end'])
            await probe(base + track['part_key'], 'configured_media', media=True)
        try:
            response = await client.get('https://plex.tv/api/v2/resources', params={'includeHttps': 1}, headers=headers)
            emit('plex_resources', status=response.status_code)
            if response.status_code == 200:
                matches = [s for s in response.json() if s.get('clientIdentifier') == settings.plex_server_id]
                for server in matches:
                    emit('server_presence', presence=server.get('presence'), connections=len(server.get('connections', [])))
                    for connection in server.get('connections', []):
                        uri = connection.get('uri', '')
                        if urlsplit(uri).scheme != 'https' or connection.get('local'): continue
                        emit('candidate', host=urlsplit(uri).hostname, relay=connection.get('relay'))
                        await probe(uri.rstrip('/') + '/identity', 'candidate_identity', server.get('accessToken'))
        except Exception as exc:
            emit('plex_resources', errors=failure(exc))

asyncio.run(main())

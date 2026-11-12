"""Check the configured public Plex endpoint from an independent network, without credentials."""
import http.client
import json
import socket
import ssl
import time

host = '45-133-91-143.1c0c3d698da24e81bce04c03eaa725fc.plex.direct'
report = []
try:
    report.append({'dns': sorted({row[4][0] for row in socket.getaddrinfo(host, 32400)})})
except OSError as exc:
    report.append({'dns_error': type(exc).__name__})
for secure in (True, False):
    conn = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(host, 32400, timeout=8)
    try:
        started = time.monotonic()
        conn.connect()
        row = {'https': secure, 'connected_seconds': round(time.monotonic() - started, 2)}
        if secure:
            row['certificate_expires'] = conn.sock.getpeercert().get('notAfter')
        report.append(row)
        conn.request('GET', '/identity', headers={'Accept': 'application/json'})
        response = conn.getresponse()
        report.append({'https': secure, 'status': response.status, 'elapsed': round(time.monotonic() - started, 2)})
    except (OSError, http.client.HTTPException) as exc:
        report.append({'https': secure, 'error': type(exc).__name__, 'errno': getattr(exc, 'errno', None)})
    finally:
        conn.close()
text = json.dumps(report)
print('::notice title=Independent Plex connection::' + text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A'))

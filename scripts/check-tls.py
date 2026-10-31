"""Read-only public TLS diagnostics; emit only public certificate details."""
import re
import socket
import subprocess


def notice(title, text):
    text = text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f'::notice title={title}::{text}', flush=True)


for host in ('yurets.you', 'www.yurets.you'):
    try:
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, 443)})
    except OSError as exc:
        notice(host, str(exc))
        continue
    notice(host + ' DNS', str(addresses))
    for address in addresses:
        endpoint = f'[{address}]:443' if ':' in address else f'{address}:443'
        try:
            result = subprocess.run(
                ['openssl', 's_client', '-connect', endpoint, '-servername', host,
                 '-verify_hostname', host, '-verify_return_error', '-showcerts'],
                input='', text=True, capture_output=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            notice(host, f'{endpoint}: connection timeout')
            continue
        report = f'{endpoint}; verification exit={result.returncode}\n' + result.stderr
        for pem in re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', result.stdout, re.S):
            cert = subprocess.run(
                ['openssl', 'x509', '-noout', '-subject', '-issuer', '-dates', '-ext', 'subjectAltName'],
                input=pem, text=True, capture_output=True, check=True,
            )
            report += cert.stdout
        notice(host + ' TLS', report)

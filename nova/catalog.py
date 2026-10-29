"""PaperMC metadata and checksum-verified downloads, without user-supplied URLs."""
import hashlib
from pathlib import Path
import tempfile
import httpx

API = 'https://fill.papermc.io/v3'
HEADERS = {'User-Agent': 'NOVA/0.1.0 (Minecraft network manager; github.com/NikiM2009/NOVA)'}


def versions(software):
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        data = client.get(f'{API}/projects/{software}')
        data.raise_for_status()
        entries = data.json()['versions']
        if isinstance(entries, dict):
            entries = [v for group in entries.values() for v in group]
        return list(dict.fromkeys(entries))[:100]


def download(software, version, target):
    with httpx.Client(headers=HEADERS, timeout=120, follow_redirects=True) as client:
        response = client.get(f'{API}/projects/{software}/versions/{version}/builds')
        response.raise_for_status()
        builds = response.json()
        if not isinstance(builds, list) or not builds:
            raise ValueError('Für diese Version wurde kein Build gefunden.')
        stable = [b for b in builds if str(b.get('channel', '')).upper() == 'STABLE']
        build = max(stable or builds, key=lambda b: b['id'])
        artifact = build['downloads']['server:default']
        url = httpx.URL(artifact['url'])
        if url.scheme != 'https' or url.host not in {'fill-data.papermc.io', 'fill.papermc.io', 'api.papermc.io'}:
            raise ValueError('Unerwartete Download-Domain in PaperMC-Metadaten.')
        expected = artifact['checksums']['sha256']
        target = Path(target)
        # Never follow a pre-existing .download symlink in a server directory.
        with tempfile.NamedTemporaryFile(prefix='.nova-download-',dir=target.parent,delete=False) as handle:
            tmp = Path(handle.name)
        checksum = hashlib.sha256()
        try:
            with client.stream('GET', str(url)) as stream:
                stream.raise_for_status()
                with tmp.open('wb') as output:
                    for chunk in stream.iter_bytes():
                        checksum.update(chunk)
                        output.write(chunk)
            if checksum.hexdigest() != expected:
                raise ValueError('SHA-256-Prüfung fehlgeschlagen; Download verworfen.')
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        return build['id']

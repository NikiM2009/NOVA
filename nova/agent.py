"""Outbound-only agent. No shell execution and no public agent HTTP port."""
import base64
from collections import deque
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import zipfile

import httpx
import psutil
import yaml

from . import catalog


class AgentSetupError(Exception):
    """An actionable setup failure that the CLI can show without a traceback."""


def connection_hint(url):
    host = httpx.URL(url).host
    if host in {'127.0.0.1','localhost','::1'}:
        return ('127.0.0.1/localhost bezeichnet diesen Rechner. Läuft die Zentrale hier, '
                'starte sie in einem separaten Terminal ohne --demo. Läuft sie auf einem '
                'anderen Rechner, verwende mit --url dessen erreichbare Adresse.')
    return ('Prüfe, ob die Zentrale läuft und unter dieser Adresse erreichbar ist. '
            'Eine Bindung der Zentrale nur an 127.0.0.1 erlaubt keine Verbindung von anderen Rechnern. '
            'Prüfe außerdem Port, Firewall und gegebenenfalls den HTTPS-Reverse-Proxy.')


def request_central(client, method, path, url, **kwargs):
    try:
        return client.request(method,path,**kwargs)
    except httpx.HTTPError as exc:
        if 'CERTIFICATE_VERIFY_FAILED' in str(exc):
            reason = 'TLS-Zertifikat der Zentrale konnte nicht verifiziert werden.'
        elif isinstance(exc,httpx.TimeoutException):
            reason = f'Zeitüberschreitung beim Verbinden mit der Zentrale unter {url}.'
        elif isinstance(exc,httpx.ProxyError):
            reason = f'Die Proxy-Verbindung zur Zentrale unter {url} ist fehlgeschlagen.'
        else:
            reason = f'Zentrale unter {url} nicht erreichbar.'
        raise AgentSetupError(reason+' '+connection_hint(url)) from None


def check_central(client, url):
    """Check reachability and real mode before requesting a single-use code."""
    response = request_central(client,'GET','/health',url)
    if not response.is_success:
        raise AgentSetupError(f'Zentrale antwortet beim Verbindungstest mit HTTP {response.status_code}. '
                              'Adresse und Reverse-Proxy prüfen; der Kopplungscode wurde noch nicht abgefragt.')
    try:
        info = response.json()
    except ValueError:
        info = None
    if not isinstance(info,dict) or info.get('status')!='ok' or not isinstance(info.get('demo'),bool):
        raise AgentSetupError('Unter dieser Adresse antwortet keine passende NOVA-Zentrale. '
                              'Bitte die Adresse der Zentrale inklusive Port prüfen.')
    if info['demo']:
        raise AgentSetupError('Diese Zentrale läuft im Demomodus. Starte sie ohne --demo, '
                              'bevor du echte Agenten koppelst.')


def register_agent(client, url, token, name, address):
    response = request_central(client,'POST','/api/agent/register',url,json=dict(
        token=token,name=name,address=address,platform=f'{platform.system()} · {platform.machine()}'))
    if response.status_code==401:
        raise AgentSetupError('Kopplungscode abgelaufen, ungültig oder bereits verwendet. '
                              'Unter „Agenten → Agent verbinden“ einen neuen Code erzeugen.')
    if response.status_code==403:
        raise AgentSetupError('Die Zentrale erlaubt keine Agentenkopplung. Prüfe, ob sie ohne --demo läuft.')
    if response.status_code==422:
        raise AgentSetupError('Agentenname oder Adresse ist ungültig. Prüfe --name und --address.')
    if not response.is_success:
        raise AgentSetupError(f'Kopplung fehlgeschlagen (HTTP {response.status_code}). '
                              'Zentrale und gegebenenfalls Reverse-Proxy prüfen.')
    try:
        config = response.json()
    except ValueError:
        config = None
    if not isinstance(config,dict) or not all(isinstance(config.get(k),str) and config[k] for k in ('id','token')):
        raise AgentSetupError('Die Zentrale hat keine gültigen Agentenzugangsdaten zurückgegeben.')
    return dict(id=config['id'],token=config['token'],url=url.rstrip('/'))


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


class FileManager:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, relative):
        if not isinstance(relative,str) or '\x00' in relative:
            raise ValueError('Ungültiger Pfad')
        relative = relative.replace('\\','/')
        if relative.startswith('/') or ':' in relative or '..' in relative.split('/'):
            raise ValueError('Pfad außerhalb der Instanz')
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('Symlinks außerhalb der Instanz sind gesperrt')
        # Deny all symlink traversal, including links that still point inside the root.
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('Symlinks werden nicht unterstützt')
        return path

    def perform(self, operation, payload):
        path = self.path(payload.get('path',''))
        if operation == 'list':
            if not path.is_dir():
                raise ValueError('Kein Verzeichnis')
            entries = []
            for item in sorted(path.iterdir(), key=lambda x:(not x.is_dir(),x.name.lower())):
                if item.is_symlink() or item.name.startswith('.nova') or item.name=='.seeded':
                    continue
                entries.append(dict(name=item.name,directory=item.is_dir(),size=item.stat().st_size,
                                    modified=item.stat().st_mtime))
                if len(entries) >= 2000:
                    break
            return {'entries':entries,'path':payload.get('path','')}
        if operation == 'read':
            if path.stat().st_size > 1_000_000:
                raise ValueError('Editor-Limit: 1 MB. Große Dateien lokal bearbeiten.')
            try:
                text = path.read_text(encoding='utf-8')
                if '\x00' in text:
                    raise UnicodeError()
            except UnicodeError:
                raise ValueError('Binärdateien können nicht im Texteditor geöffnet werden.')
            return {'content':text,'path':payload['path']}
        if path == self.root:
            raise ValueError('Das Instanzverzeichnis kann nicht verändert werden.')
        if operation == 'mkdir':
            path.mkdir(parents=False, exist_ok=False)
        elif operation == 'write':
            content = payload.get('content','')
            raw = base64.b64decode(content, validate=True) if payload.get('encoding')=='base64' else content.encode('utf-8')
            if len(raw) > 1_000_000:
                raise ValueError('Upload-Limit: 1 MB')
            if not path.parent.is_dir():
                raise ValueError('Zielverzeichnis existiert nicht')
            if not payload.get('overwrite',True):
                with path.open('xb') as output:
                    output.write(raw)
                return {'message':'Datei erstellt'}
            # Atomic replacement: an interrupted write never truncates the original file.
            tmp = path.with_name('.nova-write-'+os.urandom(6).hex())
            try:
                with tmp.open('xb') as output:
                    output.write(raw)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
        elif operation == 'delete':
            if path.is_dir():
                path.rmdir()  # Only empty directories; no recursive deletion.
            else:
                path.unlink()
        else:
            raise ValueError('Unbekannte Dateiaktion')
        return {'message':'Gespeichert' if operation=='write' else 'Dateiaktion abgeschlossen'}


def varint(n):
    result = bytearray()
    while True:
        byte = n & 0x7f
        n >>= 7
        result.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(result)


def read_varint(sock):
    number = 0
    for i in range(5):
        chunk = sock.recv(1)
        if not chunk:
            raise OSError('Verbindung geschlossen')
        number |= (chunk[0] & 0x7f) << (7*i)
        if not chunk[0] & 0x80:
            return number
    raise ValueError('Ungültiges Minecraft-Paket')


def minecraft_status(port):
    with socket.create_connection(('127.0.0.1',port), timeout=.5) as sock:
        hostname = b'localhost'
        handshake = b'\x00'+varint(767)+varint(len(hostname))+hostname+struct.pack('>H',port)+b'\x01'
        sock.sendall(varint(len(handshake))+handshake+b'\x01\x00')
        packet_len = read_varint(sock)
        if packet_len > 1_000_000 or read_varint(sock) != 0:
            raise ValueError('Ungültige Statusantwort')
        length = read_varint(sock)
        if length > 1_000_000:
            raise ValueError('Statusantwort zu groß')
        raw = bytearray()
        while len(raw) < length:
            chunk = sock.recv(length-len(raw))
            if not chunk:
                raise OSError('Unvollständige Statusantwort')
            raw.extend(chunk)
        data = json.loads(raw)
        return dict(players=data.get('players',{}).get('online',0),
                    max_players=data.get('players',{}).get('max',0))


class Engine:
    def __init__(self, root, java='java'):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.java = java
        self.lock = threading.RLock()
        self.manifest = self.root / 'instances.json'
        self.servers = json.loads(self.manifest.read_text()) if self.manifest.exists() else {}
        self.processes, self.logs, self.states, self.monitors = {}, {}, {}, {}
        self.readers = {}
        self.stopping = set()
        # Reconcile only the exact Java process previously created by this agent.
        for id_, spec in self.servers.items():
            if spec.get('pid'):
                try:
                    proc = psutil.Process(spec['pid'])
                    if f'-Dnova.instance={id_}' in proc.cmdline() and proc.create_time()==spec.get('pid_started'):
                        proc.terminate()
                        try:
                            proc.wait(timeout=30)
                        except psutil.TimeoutExpired:
                            proc.kill()
                except psutil.Error:
                    pass
                spec.pop('pid',None)
                spec.pop('pid_started',None)
        self.save()

    def save(self):
        with self.lock:
            atomic_json(self.manifest,self.servers)

    def folder(self, id_):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',id_):
            raise ValueError('Ungültige Instanz-ID')
        root = self.root / 'servers' / id_
        if root.is_symlink() or not root.resolve().is_relative_to(self.root):
            raise ValueError('Ungültiger Instanzpfad')
        return root

    def running(self, id_):
        proc = self.processes.get(id_)
        return proc is not None and proc.poll() is None

    def install(self, id_, spec):
        recover = spec.get('recover_existing',False)
        if self.running(id_):
            raise ValueError('Instanz zuerst stoppen; eine laufende Instanz wird nicht verändert.')
        if id_ in self.servers and not recover:
            raise ValueError('Instanz ist bereits installiert; Dateien werden nicht überschrieben.')
        if spec['software'] not in {'paper','velocity'}:
            raise ValueError('Nicht unterstützte Software')
        if spec['software']=='paper' and not spec.get('accept_eula'):
            raise ValueError('Minecraft-EULA wurde nicht akzeptiert')
        java = shutil.which(self.java)
        if not java:
            raise ValueError('Java fehlt. Java 21+ installieren und Agent neu starten.')
        version = subprocess.run([java,'-version'], capture_output=True, text=True, timeout=10)
        match = re.search(r'version "(\d+)',version.stderr+version.stdout)
        if not match or int(match[1]) < 21:
            raise ValueError('Java 21 oder neuer ist erforderlich. Neueste Minecraft-Versionen können Java 25 benötigen.')
        root = self.folder(id_)
        root.mkdir(parents=True, exist_ok=True)
        fs = FileManager(root)
        jar = fs.path('server.jar')
        configs = ['eula.txt','server.properties'] if spec['software']=='paper' else ['velocity.toml']
        for relative in configs:
            path = fs.path(relative)
            if path.exists() and not path.is_file():
                raise ValueError(f'{relative} ist keine reguläre Datei. Vorhandene Daten bleiben unverändert.')
        if jar.exists():
            if not recover:
                raise ValueError('server.jar ist bereits vorhanden. Zur Wiederherstellung ausdrücklich „Installation wiederholen“ verwenden.')
            if not jar.is_file() or not zipfile.is_zipfile(jar):
                raise ValueError('Vorhandene server.jar ist keine lesbare JAR-Datei. Sie wurde nicht überschrieben; bitte sichern und prüfen.')
            for process in psutil.process_iter(['cmdline']):
                try:
                    if f'-Dnova.instance={id_}' in (process.info.get('cmdline') or []):
                        raise ValueError('Ein Prozess dieser Instanz läuft noch. Vor der Wiederherstellung kontrolliert stoppen.')
                except psutil.Error:
                    continue
        self.states[id_] = 'installing'
        try:
            existing = jar.exists()
            build = self.servers.get(id_,{}).get('build') if existing else catalog.download(spec['software'],spec['version'],jar)
            if spec['software']=='paper':
                defaults = {'eula.txt':'eula=true\n',
                            'server.properties':f'server-port={spec["port"]}\nmotd=NOVA | {spec["name"]}\nmax-players=200\nonline-mode=true\nenable-status=true\n'}
            else:
                defaults = {'velocity.toml':f'config-version = "2.7"\nbind = "0.0.0.0:{spec["port"]}"\nmotd = "NOVA Network"\nshow-max-players = 200\nonline-mode = true\nplayer-info-forwarding-mode = "none"\n[servers]\ntry = []\n'}
            for relative, content in defaults.items():
                if not fs.path(relative).exists():
                    fs.perform('write',dict(path=relative,content=content,overwrite=False))
            with self.lock:
                previous = self.servers.get(id_)
                self.servers[id_] = {k:v for k,v in dict(spec,build=build).items() if k!='recover_existing'}
                try:
                    self.save()
                except Exception:
                    if previous is None:
                        self.servers.pop(id_,None)
                    else:
                        self.servers[id_] = previous
                    raise
            message = ('Vorhandene JAR und Konfiguration beibehalten; lokale Zuordnung wiederhergestellt.' if existing else
                       'Installiert. Instanz kann jetzt gestartet werden.')
            return {'build':build,'message':message}
        finally:
            self.states.pop(id_,None)

    def start(self, id_):
        if self.running(id_):
            return {'message':'Instanz läuft bereits'}
        spec = self.servers[id_]
        root = self.folder(id_)
        jar = root / 'server.jar'
        if not jar.is_file() or jar.is_symlink():
            raise ValueError('server.jar fehlt oder ist ein Symlink')
        args = [self.java,f'-Dnova.instance={id_}',f'-Xms{min(spec["memory"],512)}M',
                f'-Xmx{spec["memory"]}M','-jar','server.jar']
        if spec['software']=='paper':
            args.append('--nogui')
        self.logs[id_] = deque(maxlen=400)
        proc = subprocess.Popen(args,cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1)
        with self.lock:
            self.processes[id_] = proc
            self.monitors[id_] = psutil.Process(proc.pid)
            spec['pid'] = proc.pid
            spec['pid_started'] = self.monitors[id_].create_time()
            self.save()
        def read():
            try:
                for line in proc.stdout:
                    self.logs[id_].append(line.rstrip()[:4000])
                proc.wait()
                self.logs[id_].append(f'[NOVA] Prozess beendet (Code {proc.returncode})')
            finally:
                proc.stdout.close()
                proc.stdin.close()
        self.readers[id_] = threading.Thread(target=read, daemon=True)
        self.readers[id_].start()
        time.sleep(.3)
        if proc.poll() is not None:
            raise ValueError('Java-Prozess wurde beendet. Konsolenausgabe prüfen.')
        return {'message':'Startsignal gesendet; Server fährt hoch.'}

    def command(self, id_, command):
        if '\n' in command or '\r' in command or len(command)>1000:
            raise ValueError('Ungültiger Konsolenbefehl')
        if not self.running(id_):
            raise ValueError('Instanz läuft nicht')
        proc = self.processes[id_]
        proc.stdin.write(command+'\n')
        proc.stdin.flush()

    def stop(self, id_):
        if not self.running(id_):
            return {'message':'Instanz ist bereits gestoppt'}
        self.stopping.add(id_)
        try:
            self.command(id_, 'shutdown' if self.servers[id_]['software']=='velocity' else 'stop')
            proc = self.processes[id_]
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            self.readers[id_].join(timeout=2)
            proc.stdin.close()
            return {'message':'Instanz gestoppt'}
        finally:
            self.stopping.discard(id_)

    def backup(self, id_):
        if self.running(id_):
            raise ValueError('Für ein konsistentes Backup zuerst die Instanz stoppen (Routine: stop → backup → start separat planen).')
        root = self.folder(id_)
        target = self.root / 'backups' / id_
        target.mkdir(parents=True,exist_ok=True)
        archive = target / (time.strftime('%Y%m%d-%H%M%S')+'-'+os.urandom(3).hex()+'.zip')
        try:
            with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
                for path in root.rglob('*'):
                    if path.is_symlink() or not path.is_file() or path.name=='server.jar':
                        continue
                    if any(parent.is_symlink() for parent in path.relative_to(root).parents if str(parent)!='.'):
                        continue
                    if path.resolve().is_relative_to(root.resolve()):
                        z.write(path,path.relative_to(root))
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        return {'message':'Backup erstellt','archive':str(archive),'size':archive.stat().st_size}

    def configure_network(self, id_, payload):
        if self.running(id_):
            raise ValueError('Instanz zuerst stoppen')
        root = self.folder(id_)
        fs = FileManager(root)
        secret = payload['secret']
        paths = (['velocity.toml','velocity.toml.nova-before-network','forwarding.secret']
                 if self.servers[id_]['software']=='velocity' else
                 ['server.properties','server.properties.nova-before-network','config',
                  'config/paper-global.yml','config/paper-global.yml.nova-before-network'])
        # Check the complete operation before modifying even the first file.
        for relative in paths:
            fs.path(relative)
        if self.servers[id_]['software']=='velocity':
            file = fs.path('velocity.toml')
            if file.exists():
                shutil.copy2(file,fs.path('velocity.toml.nova-before-network'))
            routes = payload['routes']
            lines = ['config-version = "2.7"',f'bind = "0.0.0.0:{self.servers[id_]["port"]}"',
                     'motd = "NOVA Network"','show-max-players = 200','online-mode = true',
                     'player-info-forwarding-mode = "modern"','forwarding-secret-file = "forwarding.secret"',
                     '[servers]']
            lines += [f'{json.dumps(name)} = {json.dumps(address)}' for name,address in routes.items()]
            lines += ['try = '+json.dumps(list(routes)[:1])]
            FileManager(root).perform('write',{'path':'velocity.toml','content':'\n'.join(lines)+'\n'})
            FileManager(root).perform('write',{'path':'forwarding.secret','content':secret})
        else:
            file = fs.path('server.properties')
            if file.exists():
                shutil.copy2(file,fs.path('server.properties.nova-before-network'))
            text = file.read_text(encoding='utf-8') if file.exists() else ''
            text = re.sub(r'^online-mode=.*\n?', '', text, flags=re.M)
            FileManager(root).perform('write',{'path':'server.properties','content':text+'\nonline-mode=false\n'})
            fs.path('config').mkdir(exist_ok=True)
            file = fs.path('config/paper-global.yml')
            config = yaml.safe_load(file.read_text(encoding='utf-8')) if file.exists() else {}
            if file.exists():
                shutil.copy2(file,fs.path('config/paper-global.yml.nova-before-network'))
            config = config or {}
            config.setdefault('proxies',{})['velocity'] = dict(enabled=True,**{'online-mode':True},secret=secret)
            FileManager(root).perform('write',{'path':'config/paper-global.yml','content':yaml.safe_dump(config)})
        return {'message':'Modern Forwarding konfiguriert. Backend-Ports ausschließlich für den Proxy freigeben.'}

    def execute(self, job):
        id_, kind, payload = job['server_id'], job['kind'], job['payload']
        if kind=='install':
            return self.install(id_,payload)
        if kind=='forget' and id_ not in self.servers:
            return {'message':'Instanz nicht installiert; vorhandene Dateien bleiben erhalten.'}
        if id_ not in self.servers:
            raise ValueError(f'Instanz auf diesem Agenten nicht installiert oder lokale Zuordnung fehlt. '
                             f'Aktives Datenverzeichnis: {self.root}. Prüfe den ursprünglichen Installationsfehler '
                             'in NOVA und verwende danach „Installation wiederholen“.')
        if kind=='start':
            return self.start(id_)
        if kind=='stop':
            return self.stop(id_)
        if kind=='restart':
            self.stop(id_)
            return self.start(id_)
        if kind=='command':
            self.command(id_,payload['command'])
            return {'message':'Befehl gesendet'}
        if kind=='logs':
            return {'lines':list(self.logs.get(id_,['[NOVA] Noch keine Konsolenausgabe. Instanz starten.']))}
        if kind.startswith('files_'):
            return FileManager(self.folder(id_)).perform(kind[6:],payload)
        if kind=='backup':
            return self.backup(id_)
        if kind=='configure_network':
            return self.configure_network(id_,payload)
        if kind=='forget':
            if self.running(id_):
                raise ValueError('Laufende Instanz kann nicht entfernt werden')
            with self.lock:
                del self.servers[id_]
                self.save()
            return {'message':'Instanz entfernt; Dateien sind unverändert erhalten.'}
        raise ValueError('Unbekannter Auftrag')

    def snapshot(self):
        result = {}
        with self.lock:
            instances = [(id_,dict(spec),self.processes.get(id_),self.monitors.get(id_))
                         for id_,spec in self.servers.items()]
        for id_,spec,proc,monitor in instances:
            running = proc is not None and proc.poll() is None
            status = 'running' if running else 'error' if proc and proc.returncode and id_ not in self.stopping else 'stopped'
            metrics = {}
            if status=='running' and monitor:
                try:
                    metrics = dict(cpu=round(monitor.cpu_percent()/max(psutil.cpu_count(),1),1),ram=round(monitor.memory_info().rss/1024**3,2))
                    try:
                        metrics.update(minecraft_status(spec['port']))
                    except (OSError,ValueError,KeyError):
                        metrics['starting'] = True
                except psutil.Error:
                    pass
            result[id_] = dict(status=self.states.get(id_,status),metrics=metrics)
        return result

    def close(self):
        for id_ in list(self.processes):
            try:
                self.stop(id_)
            except (OSError,ValueError):
                pass


def run_agent(url, root, name=None, address=None, allow_http=False, java='java'):
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise AgentSetupError('Ungültige Zentralenadresse. Verwende --url https://deine-zentrale.example.') from None
    if not parsed.host or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}:
        raise AgentSetupError('Die Zentralenadresse muss eine HTTP(S)-Adresse ohne Zugangsdaten, Pfad oder Abfrageparameter sein.')
    if parsed.scheme!='https' and not (parsed.scheme=='http' and (parsed.host in {'localhost','127.0.0.1','::1'} or allow_http)):
        raise ValueError('HTTPS erforderlich. Für ein vertrauenswürdiges lokales Netz explizit --allow-http verwenden.')
    root = Path(root).resolve()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    config_file = root/'connection.json'
    with httpx.Client(base_url=url.rstrip('/'),timeout=20) as client:
        config = None
        if config_file.exists():
            config = json.loads(config_file.read_text())
            if config['url'] != url.rstrip('/'):
                raise ValueError('Agent bereits mit einer anderen Zentrale gekoppelt. Separates Datenverzeichnis verwenden.')
        check_central(client,url)
        if config is None:
            import getpass
            print('Zentrale erreichbar. Halte sie während der Kopplung in Betrieb.',flush=True)
            token = getpass.getpass('Einmaligen Kopplungscode aus NOVA einfügen: ').strip()
            if not token:
                raise AgentSetupError('Kein Kopplungscode eingegeben. Erzeuge den Code in der laufenden Zentrale.')
            config = register_agent(client,url,token,name or socket.gethostname(),address or socket.gethostname())
            atomic_json(config_file,config)
        client.headers['Authorization'] = 'Bearer '+config['token']
        engine = Engine(root,java)
        stopped = threading.Event()
        pending_file = root/'job-results.json'
        completed = json.loads(pending_file.read_text()) if pending_file.exists() else {}
        def worker():
            while not stopped.is_set():
                try:
                    # Durable outbox: retry result delivery without executing commands twice.
                    for id_, result in list(completed.items()):
                        response = client.post('/api/agent/jobs/'+id_,json=result)
                        if response.is_success or response.status_code==404:
                            del completed[id_]
                            atomic_json(pending_file,completed)
                        else:
                            response.raise_for_status()
                    response = client.post('/api/agent/claim')
                    response.raise_for_status()
                    job = response.json()['job']
                    if job:
                        try:
                            result = dict(ok=True,data=engine.execute(job))
                        except Exception as exc:
                            result = dict(ok=False,error=str(exc)[:2000],data={})
                        completed[job['id']] = result
                        atomic_json(pending_file,completed)
                    else:
                        stopped.wait(1)
                except (httpx.HTTPError,OSError) as exc:
                    print('Agent: Auftragsverbindung unterbrochen ('+type(exc).__name__+'). Neuer Versuch in 5 s.',flush=True)
                    stopped.wait(5)
        thread = threading.Thread(target=worker,daemon=True)
        thread.start()
        def shutdown(*_):
            stopped.set()
        signal.signal(signal.SIGTERM,shutdown)
        signal.signal(signal.SIGINT,shutdown)
        print(f'NOVA Agent verbunden. Datenverzeichnis: {root}',flush=True)
        print('Beenden mit Strg+C. Java-Prozesse werden dabei gestoppt.',flush=True)
        try:
            while not stopped.is_set():
                memory = psutil.virtual_memory()
                metrics = dict(cpu=psutil.cpu_percent(),ram_total=round(memory.total/1024**3,1),
                               ram_used=round(memory.used/1024**3,1),disk=psutil.disk_usage(os.fspath(root)).percent,
                               uptime=int(time.time()-psutil.boot_time()),data_dir=os.fspath(root))
                try:
                    response = client.post('/api/agent/heartbeat',json=dict(metrics=metrics,servers=engine.snapshot()))
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    print('Agent: Heartbeat fehlgeschlagen ('+type(exc).__name__+').',flush=True)
                stopped.wait(5)
        finally:
            stopped.set()
            thread.join(timeout=130)
            engine.close()

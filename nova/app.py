import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import secrets
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog
from .store import Store, digest, password_hash


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class Registration(BaseModel):
    token: str = Field(max_length=128)
    name: str = Field(min_length=1, max_length=40, pattern=r'^[\w .-]+$')
    address: str = Field(min_length=1, max_length=253, pattern=r'^[a-zA-Z0-9.:-]+$')
    platform: str = Field(max_length=100)


class ServerSpec(BaseModel):
    name: str = Field(min_length=1, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$')
    agent_id: str
    software: str = Field(pattern='^(paper|velocity)$')
    version: str = Field(min_length=1, max_length=32, pattern=r'^[a-zA-Z0-9.+_-]+$')
    port: int = Field(default=25565, ge=1024, le=65535)
    memory: int = Field(default=2048, ge=512, le=65536)
    accept_eula: bool = False


class Command(BaseModel):
    command: str = Field(min_length=1, max_length=1000)


class FileOperation(BaseModel):
    path: str = Field(default='', max_length=1024)
    content: str = Field(default='', max_length=1_400_000)
    encoding: str = Field(default='text', pattern='^(text|base64)$')
    overwrite: bool = True


class RoutineSpec(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    server_id: str
    action: str = Field(pattern='^(start|stop|restart|backup)$')
    interval_minutes: int = Field(ge=5, le=525600)


class HostMetrics(BaseModel):
    cpu: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    ram_total: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    ram_used: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    disk: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    uptime: int = Field(default=0, ge=0)


class InstanceMetrics(BaseModel):
    cpu: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    ram: float = Field(default=0, ge=0, le=1_000_000, allow_inf_nan=False)
    players: int | None = Field(default=None, ge=0, le=1_000_000)
    max_players: int | None = Field(default=None, ge=0, le=1_000_000)
    starting: bool = False


class InstanceState(BaseModel):
    status: str = Field(pattern='^(running|stopped|error|installing)$')
    metrics: InstanceMetrics = Field(default_factory=InstanceMetrics)


class Heartbeat(BaseModel):
    metrics: HostMetrics
    servers: dict[str, InstanceState] = Field(default_factory=dict, max_length=1000)


class JobResult(BaseModel):
    ok: bool
    data: dict = Field(default_factory=dict)
    error: str = Field(default='', max_length=2000)


def create_app(data_dir=None, demo=False, admin_password=None):
    data = Path(data_dir or os.environ.get('NOVA_DATA', '.nova')).resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(data / ('demo.sqlite' if demo else 'nova.sqlite'))
    if demo:
        store.seed_demo()
    elif not store.setting('password'):
        password = admin_password or os.environ.get('NOVA_ADMIN_PASSWORD') or secrets.token_urlsafe(18)
        salt = secrets.token_hex(16)
        store.set_setting('salt', salt)
        store.set_setting('password', password_hash(password, salt))
        if not admin_password and not os.environ.get('NOVA_ADMIN_PASSWORD'):
            print(f'\nNOVA – initiales Admin-Passwort (jetzt sicher speichern): {password}\n', flush=True)

    def get_server(id_):
        row = store.one('SELECT * FROM servers WHERE id=?', (id_,))
        if not row:
            raise HTTPException(404, 'Server nicht gefunden')
        return row

    def active_agent(id_):
        row = store.one('SELECT * FROM agents WHERE id=?', (id_,))
        if not row or (not demo and time.time() - row['last_seen'] > 30):
            raise HTTPException(409, 'Agent ist offline. Verbindung zuerst wiederherstellen.')
        return row

    def count_players(servers):
        running = [s for s in servers if s['status']=='running']
        proxies = [s for s in running if s['software']=='velocity']
        return sum(s['metrics'].get('players',0) or 0 for s in (proxies or running))

    def queue(server, kind, payload=None):
        active_agent(server['agent_id'])
        job_id = store.job(server['agent_id'], server['id'], kind, payload or {})
        if demo:
            try:
                result = dict(ok=True,data=demo_job(server, kind, payload or {}))
            except (OSError, ValueError) as exc:
                result = dict(ok=False,error=str(exc)[:2000],data={})
            store.execute('UPDATE jobs SET status=?,result=? WHERE id=?',
                          ('done' if result['ok'] else 'failed',json.dumps(result),job_id))
        return {'job_id': job_id}

    def demo_job(server, kind, payload):
        root = data / 'demo-files' / server['id']
        root.mkdir(parents=True, exist_ok=True)
        if not (root / '.seeded').exists():
            (root / 'server.properties').write_text('motd=Welcome to NOVA\nmax-players=200\nonline-mode=true\nserver-port='+str(server['port'])+'\n', encoding='utf-8')
            (root / 'plugins').mkdir(exist_ok=True)
            (root / '.seeded').touch()
        from .agent import FileManager
        fs = FileManager(root)
        if kind.startswith('files_'):
            return fs.perform(kind[6:], payload)
        if kind == 'logs':
            return {'lines': [f'[{time.strftime("%H:%M:%S")}] [NOVA/DEMO] Beispielausgabe – kein Java-Prozess',
                              '[Server thread/INFO]: Preparing spawn area: 100%',
                              '[Server thread/INFO]: Done (2.841s)! For help, type "help"',
                              '[Server thread/INFO]: Alex joined the game']}
        if kind in {'start','stop','restart','install'}:
            status = 'stopped' if kind in {'stop','install'} else 'running'
            store.execute('UPDATE servers SET status=? WHERE id=?', (status,server['id']))
        return {'message': 'Demoaktion ausgeführt – keine echte Maschine verändert.'}

    async def maintenance():
        while True:
            await asyncio.sleep(10)
            now = time.time()
            # Expired jobs are never silently replayed: a start/install/write may already have run.
            for job in store.all("SELECT * FROM jobs WHERE status='running' AND claimed<?", (now-900,)):
                error = {'ok':False,'error':'Zeitlimit: Ergebnis unbekannt. Agent und Server prüfen; Auftrag wird nicht automatisch wiederholt.'}
                store.execute("UPDATE jobs SET status='failed',result=? WHERE id=?", (json.dumps(error),job['id']))
                store.event('Auftrag ohne Rückmeldung', job['kind'], 'error')
            for routine in store.all('SELECT * FROM routines WHERE enabled=1 AND next_run<=?', (now,)):
                try:
                    server = get_server(routine['server_id'])
                    queue(server, routine['action'])
                    store.event('Routine ausgeführt', routine['name'])
                except HTTPException as exc:
                    store.event('Routine übersprungen', routine['name']+' · '+str(exc.detail), 'warning')
                store.execute('UPDATE routines SET next_run=? WHERE id=?', (now+routine['interval_minutes']*60,routine['id']))
            if not demo:
                agents = store.all('SELECT metrics FROM agents WHERE last_seen>?', (now-30,))
                metrics = [json.loads(a['metrics']) for a in agents]
                instances = store.all("SELECT s.* FROM servers s JOIN agents a ON a.id=s.agent_id WHERE s.status='running' AND a.last_seen>?", (now-30,))
                for s in instances:
                    s['metrics'] = json.loads(s['metrics'])
                players = count_players(instances)
                cpu = sum(m.get('cpu',0) for m in metrics)/max(len(metrics),1)
                ram = sum(m.get('ram_used',0) for m in metrics)
                last = store.one('SELECT MAX(time) AS time FROM samples')
                if not last['time'] or now-last['time'] >= 60:
                    store.execute('INSERT INTO samples VALUES (?,?,?,?)', (now,players,cpu,ram))
                    store.execute('DELETE FROM samples WHERE time<?', (now-86400*7,))
            store.execute('DELETE FROM sessions WHERE expires<?', (now,))
            store.execute('DELETE FROM pairing WHERE expires<?', (now,))
            store.execute("DELETE FROM jobs WHERE status IN ('done','failed') AND created<?", (now-86400*7,))

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(maintenance())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(title='NOVA', version='0.1.0', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.demo = demo
    attempts = defaultdict(deque)

    @app.middleware('http')
    async def protection(request, call_next):
        if request.method in {'POST','PUT','DELETE','PATCH'}:
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail':'Fremde Herkunft nicht erlaubt'}, status_code=403)
            try:
                if int(request.headers.get('content-length','0')) > 2_000_000:
                    return JSONResponse({'detail':'Anfrage zu groß (max. 2 MB)'}, status_code=413)
            except ValueError:
                return JSONResponse({'detail':'Ungültige Anfrage'}, status_code=400)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def admin(request: Request):
        if demo:
            return
        token = request.cookies.get('nova_session', '')
        if not token or not store.one('SELECT token FROM sessions WHERE token=? AND expires>?', (digest(token),time.time())):
            raise HTTPException(401, 'Bitte anmelden')

    def agent_auth(authorization: str = Header(default='')):
        if demo:
            raise HTTPException(403, 'Agenten können sich nicht mit dem Demomodus verbinden.')
        token = authorization.removeprefix('Bearer ')
        agent = store.one('SELECT * FROM agents WHERE token=?', (digest(token),))
        if not token or not agent:
            raise HTTPException(401, 'Ungültiger Agentenschlüssel')
        return agent

    @app.get('/api/session')
    def session(request: Request):
        try:
            admin(request)
            authenticated = True
        except HTTPException:
            authenticated = False
        return {'authenticated':authenticated,'demo':demo,'version':'0.1.0'}

    @app.post('/api/login')
    def login(body: Login, request: Request, response: Response):
        key = request.client.host if request.client else 'local'
        q = attempts[key]
        now = time.time()
        while q and q[0] < now-300:
            q.popleft()
        if len(q) >= 8:
            raise HTTPException(429, 'Zu viele Versuche. Bitte 5 Minuten warten.')
        if not demo and not hmac.compare_digest(password_hash(body.password,store.setting('salt')),store.setting('password')):
            q.append(now)
            raise HTTPException(401, 'Passwort stimmt nicht')
        q.clear()
        token = secrets.token_urlsafe(32)
        store.execute('INSERT INTO sessions VALUES (?,?)', (digest(token),now+86400))
        response.set_cookie('nova_session', token, httponly=True, samesite='strict', secure=request.url.scheme=='https', max_age=86400)
        return {'ok':True}

    @app.post('/api/logout', dependencies=[Depends(admin)])
    def logout(request: Request, response: Response):
        store.execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get('nova_session','')),))
        response.delete_cookie('nova_session')
        return {'ok':True}

    @app.get('/api/overview', dependencies=[Depends(admin)])
    def overview():
        now = time.time()
        agents = store.all('SELECT id,name,address,platform,last_seen,metrics FROM agents ORDER BY name')
        for a in agents:
            a['online'] = demo or now-a['last_seen'] < 30
            a['metrics'] = json.loads(a['metrics'])
        online = {a['id'] for a in agents if a['online']}
        servers = store.all('SELECT * FROM servers ORDER BY created,name')
        for s in servers:
            s['metrics'] = json.loads(s['metrics'])
            if s['agent_id'] not in online:
                s['status'] = 'unknown'
                s['metrics'] = {}
        players = count_players(servers)
        metrics = [a['metrics'] for a in agents if a['online']]
        return dict(demo=demo,agents=agents,servers=servers,players=players,
                    cpu=round(sum(m.get('cpu',0) for m in metrics)/max(len(metrics),1),1),
                    ram_used=round(sum(m.get('ram_used',0) for m in metrics),1),
                    ram_total=round(sum(m.get('ram_total',0) for m in metrics),1),
                    events=store.all('SELECT * FROM events ORDER BY id DESC LIMIT 30'),
                    errors=store.one("SELECT COUNT(*) AS count FROM events WHERE level='error' AND time>?", (now-86400,))['count'],
                    samples=store.all('SELECT * FROM samples WHERE time>? ORDER BY time', (now-86400,)),
                    routines=store.all('SELECT * FROM routines ORDER BY next_run'),
                    pending=store.one("SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued','running')")['count'])

    @app.get('/api/catalog/{software}', dependencies=[Depends(admin)])
    def get_catalog(software: str):
        if software not in {'paper','velocity'}:
            raise HTTPException(400, 'Unbekannte Software')
        if demo:
            return {'versions':['1.21.4','1.21.3','1.21.1','1.20.6'] if software=='paper' else ['3.4.0','3.3.0'], 'demo':True}
        try:
            return {'versions':catalog.versions(software), 'demo':False}
        except Exception:
            raise HTTPException(502, 'PaperMC-Katalog nicht erreichbar. Netzwerkzugriff auf fill.papermc.io prüfen.')

    @app.post('/api/pairing', dependencies=[Depends(admin)])
    def pairing():
        token = secrets.token_urlsafe(32)
        if not demo:
            store.execute('INSERT INTO pairing VALUES (?,?)', (digest(token),time.time()+600))
        return {'token':token,'expires_in':600,'demo':demo}

    @app.post('/api/agent/register')
    def register(body: Registration):
        if demo:
            raise HTTPException(403, 'Zum Koppeln NOVA ohne --demo starten.')
        id_, token = secrets.token_hex(8), secrets.token_urlsafe(40)
        with store.db() as db:
            deleted = db.execute('DELETE FROM pairing WHERE token=? AND expires>?', (digest(body.token),time.time())).rowcount
            if not deleted:
                raise HTTPException(401, 'Kopplungscode abgelaufen oder bereits verwendet')
            db.execute('INSERT INTO agents VALUES (?,?,?,?,?,?,?)', (id_,digest(token),body.name,body.address,body.platform,time.time(),'{}'))
        store.event('Agent verbunden', body.name, 'success')
        return {'id':id_,'token':token}

    @app.delete('/api/agents/{id_}', dependencies=[Depends(admin)])
    def revoke_agent(id_: str):
        if store.one('SELECT id FROM servers WHERE agent_id=?', (id_,)):
            raise HTTPException(409, 'Zuerst alle Instanzen dieses Agenten entfernen.')
        store.execute('DELETE FROM agents WHERE id=?', (id_,))
        store.event('Agent entfernt', 'Zugriffsschlüssel widerrufen')
        return {'ok':True}

    @app.post('/api/agent/heartbeat')
    def heartbeat(body: Heartbeat, agent=Depends(agent_auth)):
        store.execute('UPDATE agents SET last_seen=?,metrics=? WHERE id=?', (time.time(),body.metrics.model_dump_json(),agent['id']))
        for id_, state in body.servers.items():
            store.execute('UPDATE servers SET status=?,metrics=? WHERE id=? AND agent_id=?',
                          (state.status,json.dumps(state.metrics.model_dump(exclude_none=True)),id_,agent['id']))
        return {'ok':True}

    @app.post('/api/agent/claim')
    def claim(agent=Depends(agent_auth)):
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM jobs WHERE agent_id=? AND status='queued' ORDER BY created LIMIT 1", (agent['id'],)).fetchone()
            if not job:
                return {'job':None}
            db.execute("UPDATE jobs SET status='running',claimed=? WHERE id=?", (time.time(),job['id']))
            job = dict(job)
        job['payload'] = json.loads(job['payload'])
        return {'job':job}

    @app.post('/api/agent/jobs/{id_}')
    def complete(id_: str, body: JobResult, agent=Depends(agent_auth)):
        job = store.one("SELECT * FROM jobs WHERE id=? AND agent_id=? AND status='running'", (id_,agent['id']))
        if not job:
            raise HTTPException(404, 'Aktiver Auftrag nicht gefunden')
        store.execute('UPDATE jobs SET status=?,result=? WHERE id=?', ('done' if body.ok else 'failed',body.model_dump_json(),id_))
        if job['kind']=='install':
            store.execute('UPDATE servers SET status=? WHERE id=?', ('stopped' if body.ok else 'error',job['server_id']))
        if job['kind']=='forget' and body.ok:
            store.execute('DELETE FROM routines WHERE server_id=?', (job['server_id'],))
            store.execute('DELETE FROM servers WHERE id=?', (job['server_id'],))
        if not body.ok:
            store.event('Auftrag fehlgeschlagen', f"{job['kind']}: {body.error}", 'error')
        elif job['kind'] not in {'logs','files_list','files_read'}:
            store.event('Auftrag abgeschlossen', f"{job['kind']} · {job['server_id']}", 'success')
        return {'ok':True}

    @app.get('/api/jobs/{id_}', dependencies=[Depends(admin)])
    def job_status(id_: str):
        row = store.one('SELECT id,status,result FROM jobs WHERE id=?', (id_,))
        if not row:
            raise HTTPException(404, 'Auftrag nicht gefunden')
        row['result'] = json.loads(row['result']) if row['result'] else None
        return row

    @app.post('/api/servers', dependencies=[Depends(admin)])
    def create_server(body: ServerSpec):
        active_agent(body.agent_id)
        if body.software=='paper' and not body.accept_eula:
            raise HTTPException(400, 'Minecraft-EULA muss vor der Installation akzeptiert werden.')
        if store.one('SELECT id FROM servers WHERE agent_id=? AND port=?', (body.agent_id,body.port)):
            raise HTTPException(409, 'Dieser Port ist auf dem Agenten bereits vergeben.')
        if store.one('SELECT id FROM servers WHERE name=?', (body.name,)):
            raise HTTPException(409, 'Instanzname bereits vergeben.')
        id_ = secrets.token_hex(8)
        store.execute('INSERT INTO servers VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (id_,body.name,body.agent_id,body.software,body.version,body.port,body.memory,'installing','{}',time.time()))
        payload = body.model_dump()
        result = queue(get_server(id_), 'install', payload)
        store.event('Installation angefordert', f'{body.name} · {body.software} {body.version}')
        return {'server_id':id_,**result}

    @app.post('/api/servers/{id_}/actions/{action}', dependencies=[Depends(admin)])
    def action(id_: str, action: str):
        if action not in {'start','stop','restart','backup'}:
            raise HTTPException(400, 'Unbekannte Aktion')
        s = get_server(id_)
        if s['status']=='installing':
            raise HTTPException(409, 'Installation läuft noch.')
        return queue(s, action)

    @app.delete('/api/servers/{id_}', dependencies=[Depends(admin)])
    def delete_server(id_: str):
        s = get_server(id_)
        active_agent(s['agent_id'])
        if s['status'] not in {'stopped','error'}:
            raise HTTPException(409, 'Instanz muss vor dem Entfernen gestoppt sein.')
        if store.one("SELECT id FROM jobs WHERE server_id=? AND status IN ('queued','running')", (id_,)):
            raise HTTPException(409, 'Es gibt noch ausstehende Aufträge.')
        result = queue(s, 'forget')
        if demo:
            store.execute('DELETE FROM routines WHERE server_id=?', (id_,))
            store.execute('DELETE FROM servers WHERE id=?', (id_,))
        store.event('Entfernung angefordert', s['name']+' · Dateien bleiben auf dem Agenten erhalten')
        return result

    @app.post('/api/servers/{id_}/command', dependencies=[Depends(admin)])
    def command(id_: str, body: Command):
        if '\n' in body.command or '\r' in body.command:
            raise HTTPException(400, 'Nur ein Konsolenbefehl pro Anfrage erlaubt')
        return queue(get_server(id_), 'command', body.model_dump())

    @app.get('/api/servers/{id_}/logs', dependencies=[Depends(admin)])
    def logs(id_: str):
        return queue(get_server(id_), 'logs')

    @app.post('/api/servers/{id_}/files/{operation}', dependencies=[Depends(admin)])
    def files(id_: str, operation: str, body: FileOperation):
        if operation not in {'list','read','write','delete','mkdir'}:
            raise HTTPException(400, 'Unbekannte Dateiaktion')
        # Agent repeats the authoritative check against its actual filesystem.
        if '..' in body.path.replace('\\','/').split('/') or body.path.startswith(('/', '\\')) or ':' in body.path:
            raise HTTPException(400, 'Pfad muss innerhalb der Instanz liegen')
        result = queue(get_server(id_), 'files_'+operation, body.model_dump())
        if operation in {'write','delete','mkdir'}:
            store.event('Dateiaktion angefordert', f'{operation} · {body.path}')
        return result

    @app.post('/api/routines', dependencies=[Depends(admin)])
    def add_routine(body: RoutineSpec):
        get_server(body.server_id)
        id_ = secrets.token_hex(8)
        store.execute('INSERT INTO routines VALUES (?,?,?,?,?,?,?)', (id_,body.name,body.server_id,body.action,body.interval_minutes,time.time()+body.interval_minutes*60,1))
        store.event('Routine erstellt', body.name)
        return {'id':id_}

    @app.delete('/api/routines/{id_}', dependencies=[Depends(admin)])
    def remove_routine(id_: str):
        store.execute('DELETE FROM routines WHERE id=?', (id_,))
        return {'ok':True}

    @app.post('/api/network/sync', dependencies=[Depends(admin)])
    def sync_network():
        servers = store.all('SELECT * FROM servers')
        if not servers or not any(s['software']=='velocity' for s in servers):
            raise HTTPException(409, 'Mindestens ein Velocity-Proxy ist erforderlich.')
        if any(s['status']!='stopped' for s in servers):
            raise HTTPException(409, 'Vor dem Verknüpfen alle Instanzen stoppen.')
        for s in servers:
            active_agent(s['agent_id'])
        routes = {}
        for s in servers:
            if s['software']=='paper':
                address = active_agent(s['agent_id'])['address']
                routes[s['name']] = f"[{address}]:{s['port']}" if ':' in address else f"{address}:{s['port']}"
        if not routes:
            raise HTTPException(409, 'Mindestens ein Paper-Server ist erforderlich.')
        secret = store.setting('network_secret')
        ids = [queue(s,'configure_network',dict(routes=routes,secret=secret))['job_id'] for s in servers]
        store.event('Netzwerkverknüpfung angefordert', 'Modern Forwarding · bestehende Konfigurationen werden gesichert')
        return {'jobs':ids}

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    @app.get('/health')
    def health():
        return {'status':'ok','demo':demo}

    return app

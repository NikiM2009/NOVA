import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='NOVA · Minecraft Network Manager')
    sub = parser.add_subparsers(dest='mode')
    serve = sub.add_parser('serve',help='Zentrale und Weboberfläche starten')
    serve.add_argument('--host',default='127.0.0.1')
    serve.add_argument('--port',type=int,default=8000)
    serve.add_argument('--data',default='.nova')
    serve.add_argument('--demo',action='store_true',help='Isolierte Demonstration mit Beispieldaten')
    agent = sub.add_parser('agent',help='Agent koppeln und starten')
    agent.add_argument('--url',required=True)
    agent.add_argument('--name')
    agent.add_argument('--address',help='Vom Proxy erreichbare IP/Hostname dieses Agenten')
    agent.add_argument('--data',default='.nova/agent')
    agent.add_argument('--java',default='java')
    agent.add_argument('--allow-http',action='store_true')
    reset = sub.add_parser('password',help='Lokales Admin-Passwort ändern')
    reset.add_argument('--data',default='.nova')
    args = parser.parse_args()
    if args.mode=='agent':
        from .agent import run_agent
        from .locking import instance_lock
        with instance_lock(args.data,'agent'):
            run_agent(args.url,args.data,args.name,args.address,args.allow_http,args.java)
    elif args.mode=='password':
        import getpass
        import secrets
        from .store import Store,password_hash
        password = getpass.getpass('Neues Admin-Passwort (mindestens 12 Zeichen): ')
        if len(password)<12 or password!=getpass.getpass('Wiederholen: '):
            parser.error('Mindestens 12 Zeichen und übereinstimmende Eingaben erforderlich.')
        store = Store(Path(args.data)/'nova.sqlite')
        salt = secrets.token_hex(16)
        store.set_setting('salt',salt)
        store.set_setting('password',password_hash(password,salt))
        store.execute('DELETE FROM sessions')
        print('Passwort geändert; vorhandene Sitzungen abgemeldet.')
    else:
        import uvicorn
        from .app import create_app
        from .locking import instance_lock
        data = getattr(args,'data','.nova')
        demo = getattr(args,'demo',False)
        with instance_lock(data,'central-demo' if demo else 'central'):
            app = create_app(data,demo)
            uvicorn.run(app,host=getattr(args,'host','127.0.0.1'),port=getattr(args,'port',8000),log_level='info')


if __name__=='__main__':
    main()

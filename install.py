"""Cross-platform installer. Uses only Python's standard library before setup."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description='NOVA installieren – Python 3.11+ erforderlich')
    parser.add_argument('--agent',action='store_true')
    parser.add_argument('--url',help='Adresse der Zentrale für Agenten')
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--no-start',action='store_true')
    args = parser.parse_args()
    if sys.version_info < (3,11):
        parser.error('Python 3.11 oder neuer erforderlich')
    if args.agent and not args.url:
        parser.error('--agent erfordert --url https://deine-zentrale')
    root = Path(__file__).resolve().parent
    os.chdir(root)
    environment = root/'.venv'
    python = environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.exists():
        venv.create(environment,with_pip=True)
    pip_ready = subprocess.run([str(python),'-c','import pip'],capture_output=True).returncode == 0
    if not pip_ready:
        subprocess.run([str(python),'-m','ensurepip','--upgrade'],check=True)
    subprocess.run([str(python),'-m','pip','install','--require-hashes','-r',str(root/'requirements.lock')],check=True)
    subprocess.run([str(python),'-m','pip','install','--no-deps','-e',str(root)],check=True)
    command = [str(python),'-m','nova']
    if args.agent:
        command += ['agent','--url',args.url]
    else:
        command += ['serve']+(['--demo'] if args.demo else [])
    print('\nInstallation abgeschlossen. Künftiger Start:\n'+subprocess.list2cmdline(command),flush=True)
    if not args.no_start:
        subprocess.run(command,check=True)


if __name__=='__main__':
    main()

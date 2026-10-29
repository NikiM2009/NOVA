"""Prevent two control processes from sharing one local data directory."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def instance_lock(directory, name):
    root = Path(directory).resolve()
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    file = (root / ('.'+name+'.lock')).open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            file.seek(0,os.SEEK_END)
            if file.tell()==0:
                file.write(b'0')
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        file.close()
        raise RuntimeError('Dieses Datenverzeichnis wird bereits von einem anderen NOVA-Prozess verwendet.') from None
    try:
        yield
    finally:
        file.close()

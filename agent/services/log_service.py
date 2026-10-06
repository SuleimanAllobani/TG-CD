from pathlib import Path
from datetime import datetime


class LogWriter:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, msg):
        line = f'[{datetime.utcnow().isoformat()}Z] {msg}\n'
        with self.path.open('a', encoding='utf-8') as f:
            f.write(line)


class LogService:
    def __init__(self, storage_root):
        self.root = Path(storage_root) / 'logs'
        self.root.mkdir(parents=True, exist_ok=True)

    def open(self, site):
        folder = self.root / site
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")}.log'
        return LogWriter(path)

    def tail(self, site=None, limit=12000):
        root = self.root / site if site else self.root
        if not root.exists():
            return ''
        files = sorted(root.rglob('*.log'), key=lambda p: p.stat().st_mtime)
        if not files:
            return ''
        txt = '\n'.join([p.read_text(encoding='utf-8', errors='ignore') for p in files[-5:]])
        return txt[-limit:]

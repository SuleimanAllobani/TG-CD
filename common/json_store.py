import json, threading
from pathlib import Path

class JsonStore:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        if not self.path.exists(): self.path.write_text('{}', encoding='utf-8')
    def read(self):
        with self.lock:
            try: return json.loads(self.path.read_text(encoding='utf-8') or '{}')
            except json.JSONDecodeError: return {}
    def write(self, data):
        with self.lock:
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
            tmp.replace(self.path)

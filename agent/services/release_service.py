import json
import shutil
import uuid
from pathlib import Path
from datetime import datetime


class ReleaseService:
    def __init__(self, storage_root):
        self.root = Path(storage_root).resolve()
        self.releases = self.root / 'releases'
        self.current = self.root / 'current'
        self.failed = self.root / 'failed'
        for p in [self.releases, self.current, self.failed]:
            p.mkdir(parents=True, exist_ok=True)

    def release_id(self):
        return datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:6]

    def release_path(self, site, rid):
        return (self.releases / site / rid).resolve()

    def current_path(self, site):
        return (self.current / site).resolve()

    def failed_path(self, site, rid):
        return (self.failed / site / rid).resolve()

    def meta_path(self, site):
        return self.releases / site / 'releases.json'

    def current_meta_path(self, site):
        return self.current_path(site) / '.deployment_current.json'

    def _read_meta(self, site):
        p = self.meta_path(site)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return []

    def _write_meta(self, site, items):
        p = self.meta_path(site)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items, indent=2), encoding='utf-8')

    def prepare(self, site, rid):
        p = self.release_path(site, rid)
        p.mkdir(parents=True, exist_ok=False)
        return p

    def switch_current_to(self, site, release_path, release_id=None):
        release_path = Path(release_path)
        if not release_path.exists():
            raise FileNotFoundError(f'Release path not found: {release_path}')
        cur = self.current_path(site)
        tmp = cur.with_name(cur.name + '_tmp')
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(release_path, tmp)
        if release_id:
            (tmp / '.deployment_current.json').write_text(
                json.dumps({'release_id': release_id, 'path': str(release_path), 'activated_at': datetime.utcnow().isoformat() + 'Z'}, indent=2),
                encoding='utf-8'
            )
        if cur.exists():
            shutil.rmtree(cur)
        tmp.rename(cur)
        return cur

    def mark(self, site, rid, status, path, source=None):
        items = [x for x in self._read_meta(site) if x.get('release_id') != rid]
        entry = {
            'release_id': rid,
            'status': status,
            'path': str(path),
            'created_at': datetime.utcnow().isoformat() + 'Z'
        }
        if source:
            entry['source'] = source  # e.g. GitHub repo/ref/commit; absent for ZIP/folder releases
        items.append(entry)
        self._write_meta(site, items)

    def list(self, site):
        return self._read_meta(site)

    def current_release_id(self, site):
        p = self.current_meta_path(site)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding='utf-8')).get('release_id')
        except Exception:
            return None

    def last_success(self, site, exclude=None):
        ok = [x for x in self._read_meta(site) if x.get('status') == 'success' and x.get('release_id') != exclude]
        return ok[-1] if ok else None

    def previous_success_before_current(self, site):
        current_id = self.current_release_id(site)
        ok = [x for x in self._read_meta(site) if x.get('status') == 'success']
        if not ok:
            return None
        if current_id:
            filtered = [x for x in ok if x.get('release_id') != current_id]
            return filtered[-1] if filtered else None
        return ok[-2] if len(ok) >= 2 else None

    def preserve_failed(self, site, rid, release_path, source=None):
        dst = self.failed_path(site, rid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        if Path(release_path).exists():
            shutil.copytree(release_path, dst)
        self.mark(site, rid, 'failed_preserved', dst, source=source)
        return dst

    def rollback_to_release(self, site, release_item):
        if not release_item:
            raise RuntimeError('No release available for rollback')
        rid = release_item['release_id']
        path = Path(release_item['path'])
        self.switch_current_to(site, path, rid)
        return release_item

    def rollback_previous_success(self, site):
        prev = self.previous_success_before_current(site)
        return self.rollback_to_release(site, prev)

    def delete(self, site, rid):
        current_id = self.current_release_id(site)
        if current_id == rid:
            raise ValueError('Cannot delete the active release')
        items = self._read_meta(site)
        item = next((x for x in items if x.get('release_id') == rid), None)
        if not item:
            raise FileNotFoundError('Release not found')
        path = Path(item.get('path', ''))
        if path.exists():
            shutil.rmtree(path)
        self._write_meta(site, [x for x in items if x.get('release_id') != rid])

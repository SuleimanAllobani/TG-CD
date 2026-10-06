import shutil, uuid, zipfile
from pathlib import Path
from datetime import datetime

class PackageService:
    def __init__(self, storage_root):
        self.root = Path(storage_root).resolve() / 'uploads'
        self.root.mkdir(parents=True, exist_ok=True)

    def store_file(self, file_storage):
        name = Path(file_storage.filename or 'package.zip').name
        if not name.lower().endswith('.zip'):
            raise ValueError('Only .zip files are accepted')
        pid = 'pkg_' + datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:6]
        folder = self.root / pid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        file_storage.save(path)
        return {'package_id': pid, 'original_filename': name, 'stored_path': str(path.resolve())}

    def resolve(self, package_id):
        folder = self.root / package_id
        if not folder.exists():
            raise FileNotFoundError(f'Uploaded package not found: {package_id}')
        files = list(folder.glob('*.zip'))
        if not files:
            raise FileNotFoundError(f'No zip inside package: {package_id}')
        return files[0]

    def extract_zip(self, zip_path, dest):
        dest = Path(dest).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():
                target = (dest / member).resolve()
                if not str(target).lower().startswith(str(dest).lower()):
                    raise ValueError('Unsafe zip entry')
            z.extractall(dest)

        # If the ZIP contains exactly one top-level folder and no files at root,
        # flatten it so IIS can serve /index.html immediately.
        root_files = [x for x in dest.iterdir() if x.is_file()]
        root_dirs = [x for x in dest.iterdir() if x.is_dir()]
        if not root_files and len(root_dirs) == 1:
            inner = root_dirs[0]
            tmp = dest / '__flatten_tmp__'
            if tmp.exists():
                shutil.rmtree(tmp)
            inner.rename(tmp)
            for child in tmp.iterdir():
                shutil.move(str(child), str(dest / child.name))
            shutil.rmtree(tmp)

    def copy_folder(self, source, dest):
        source = Path(source)
        dest = Path(dest).resolve()
        if not source.exists() or not source.is_dir():
            raise FileNotFoundError(f'Folder not found: {source}')
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)

    def validate_contents(self, path, project_type):
        p = Path(path)
        if not p.exists():
            raise ValueError('Release folder missing after extraction')
        files = [x for x in p.rglob('*') if x.is_file()]
        if not files:
            raise ValueError('Deployment package is empty')
        if project_type == 'static' and not any(x.name.lower() in ['index.html','default.htm','default.html'] for x in files):
            return 'No index.html/default.html found; IIS may still serve configured content.'
        return None

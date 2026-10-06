from __future__ import annotations

import uuid
from pathlib import Path
from datetime import datetime, timezone
from werkzeug.datastructures import FileStorage


class PackageService:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, uploaded: FileStorage, filename: str, user_id: str) -> dict:
        package_id = "pkg_" + uuid.uuid4().hex[:12]
        safe_name = Path(filename).name or "package.zip"
        folder = self.root / package_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_name
        uploaded.save(path)
        return {
            "package_id": package_id,
            "original_filename": safe_name,
            "stored_path": str(path.resolve()),
            "uploaded_by_user_id": str(user_id),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

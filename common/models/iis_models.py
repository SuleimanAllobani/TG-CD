from dataclasses import dataclass, asdict, field
from typing import Optional
@dataclass
class IisSiteInfo:
    exists: bool
    site_name: str
    physical_path: Optional[str]=None
    app_pool: Optional[str]=None
    bindings: list[dict]=field(default_factory=list)
    state: Optional[str]=None
    def to_dict(self): return asdict(self)
@dataclass
class IisAppPoolInfo:
    exists: bool
    name: str
    runtime: Optional[str]=None
    pipeline_mode: Optional[str]=None
    state: Optional[str]=None
    def to_dict(self): return asdict(self)

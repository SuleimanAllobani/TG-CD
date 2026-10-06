from dataclasses import dataclass, asdict, field
from typing import Optional
@dataclass
class DeployResult:
    success: bool
    request_id: str
    site_name: str
    app_pool_name: str
    release_id: Optional[str]=None
    health_check_passed: bool=False
    rolled_back: bool=False
    first_deployment_failure: bool=False
    failed_release_preserved: bool=False
    message: str=''
    actions: list[str]=field(default_factory=list)
    warnings: list[str]=field(default_factory=list)
    log_path: Optional[str]=None
    source: Optional[dict]=None  # GitHub source metadata; omitted from output when not used
    def to_dict(self):
        d = asdict(self)
        if d.get('source') is None:
            d.pop('source', None)
        return d

from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class DeployRequest:
    request_id: str
    requested_by_user_id: int
    agent_id: str
    project_type: str
    source_type: str
    site_name: str
    app_pool_name: str
    health_check_url: str
    package_id: Optional[str]=None
    folder_path: Optional[str]=None
    site_exists_expected: bool=False
    create_site_if_missing: bool=True
    site_port: Optional[int]=80
    site_host: Optional[str]=None
    site_protocol: str='http'
    app_pool_exists_expected: bool=False
    create_app_pool_if_missing: bool=True
    app_pool_runtime: Optional[str]=''
    app_pool_pipeline_mode: Optional[str]='Integrated'
    notes: Optional[str]=None
    # GitHub source (only used when source_type == 'github'). All optional so ZIP/folder requests are unchanged.
    repo_url: Optional[str]=None
    git_ref: Optional[str]=None
    credential_ref: Optional[str]=None
    subdir: Optional[str]=None
    build_preset: Optional[str]=None   # None | 'npm_build' | 'dotnet_publish'
    build_target: Optional[str]=None   # .NET: project file; npm: output folder; None = auto-detect
    def to_dict(self): return asdict(self)
    @staticmethod
    def from_dict(d): return DeployRequest(**d)

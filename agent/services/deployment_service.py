from pathlib import Path
import traceback
from common.models.deploy_request import DeployRequest
from common.models.deploy_result import DeployResult
from .package_service import PackageService
from .release_service import ReleaseService
from .iis_service import IisService
from .health_service import HealthService
from .log_service import LogService
from .git_source_service import GitSourceService, GitSourceError, describe_source
from .build_service import BuildService, BuildError
from common.git_validation import validate_repo_url, redact

class DeploymentService:
    def __init__(self, storage_root, git_settings=None):
        self.storage_root=Path(storage_root).resolve()
        self.packages=PackageService(storage_root); self.releases=ReleaseService(storage_root); self.iis=IisService(storage_root); self.health=HealthService(); self.logs=LogService(storage_root)
        self.git=GitSourceService(storage_root, git_settings)  # GitHub source provider
        self.builder=BuildService(storage_root, git_settings)  # optional build presets for GitHub sources
    def deploy(self, data):
        req = data if isinstance(data, DeployRequest) else DeployRequest.from_dict(data)
        log=self.logs.open(req.site_name); actions=[]; warnings=[]; rid=None; release_path=None; source_info=None; staged=None
        try:
            log.write(f'Start deployment request {req.request_id} site={req.site_name}')
            if req.source_type not in ['uploaded_package','folder_path','github']: raise ValueError('Invalid source_type')
            if req.source_type=='github':
                # Fetch BEFORE creating a release so a bad URL/ref never touches releases, IIS or rollback.
                source_info=self._github_placeholder(req)
                preset=self._build_preset(req)
                if preset: self.builder.require_allowed(source_info.get('repo') or '')  # builds run repo code: allowlist first
                staged=self.git.stage(req.repo_url, req.git_ref, req.credential_ref, req.subdir, log)
                source_info=staged.info; actions.append('Fetched GitHub source '+describe_source(source_info))
                for w in source_info.get('warnings',[]): warnings.append(w)
                if preset:
                    # Build BEFORE the release exists: a failing build touches no release, IIS site or rollback state.
                    built=self.builder.build(preset, staged.root, staged.work, req.build_target, log)
                    staged.root=built.out_dir; source_info['build']=built.info
                    actions.append('Built '+preset)
                    for w in built.info.get('warnings',[]): warnings.append(w)
            rid=self.releases.release_id(); release_path=self.releases.prepare(req.site_name,rid); log.write(f'Prepared release {rid}')
            if req.source_type=='uploaded_package':
                if not req.package_id: raise ValueError('package_id is required')
                zp=self.packages.resolve(req.package_id); self.packages.extract_zip(zp,release_path); actions.append('Extracted uploaded ZIP')
            elif req.source_type=='github':
                self.packages.copy_folder(staged.root, release_path); actions.append('Copied GitHub source')
            else:
                if not req.folder_path: raise ValueError('folder_path is required')
                self.packages.copy_folder(req.folder_path, release_path); actions.append('Copied folder source')
            warn=self.packages.validate_contents(release_path, req.project_type)
            if warn: warnings.append(warn); log.write('Warning: '+warn)
            active=self.releases.current_path(req.site_name).resolve()
            self.iis.ensure(req, str(active), actions)
            self.releases.switch_current_to(req.site_name, release_path, rid); actions.append('Switched active release')
            self.iis.set_site_physical_path(req.site_name, str(active)); actions.append('Verified IIS physical path')
            self.iis.recycle_app_pool(req.app_pool_name); actions.append('Recycled app pool')
            ok,msg=self.health.check(req.health_check_url); log.write(f'Health check {ok}: {msg}')
            if not ok: raise RuntimeError('Health check failed: '+msg)
            self.releases.mark(req.site_name,rid,'success',release_path,source=source_info)
            log.write('Deployment success')
            return DeployResult(True,req.request_id,req.site_name,req.app_pool_name,rid,True,False,False,False,'Deployment completed successfully.',actions,warnings,str(log.path),source=source_info).to_dict()
        except Exception as e:
            log.write('Deployment failed: '+str(e)); log.write(traceback.format_exc())
            first=False; rolled=False; preserved=False
            if rid and release_path:
                prev=self.releases.last_success(req.site_name,exclude=rid)
                if prev:
                    try:
                        self.releases.switch_current_to(req.site_name, Path(prev['path']), prev['release_id']); self.iis.set_site_physical_path(req.site_name, str(self.releases.current_path(req.site_name).resolve())); self.iis.recycle_app_pool(req.app_pool_name)
                        rolled=True; actions.append('Rolled back to previous successful release'); log.write('Rollback success')
                    except Exception as re:
                        warnings.append('Rollback failed: '+str(re)); log.write('Rollback failed: '+str(re))
                else:
                    first=True; warnings.append('No previous successful release exists; destructive rollback skipped.'); log.write('First deployment failure: destructive rollback skipped')
                try:
                    self.releases.preserve_failed(req.site_name,rid,release_path,source=source_info); preserved=True; actions.append('Preserved failed release')
                except Exception as pe:
                    warnings.append('Failed release preservation failed: '+str(pe))
            return DeployResult(False,req.request_id,req.site_name,req.app_pool_name,rid,False,rolled,first,preserved,str(e),actions,warnings,str(log.path),source=source_info).to_dict()
        finally:
            if staged: staged.cleanup()
    @staticmethod
    def _build_preset(req):
        preset=req.build_preset or ('dotnet_publish' if req.project_type=='dotnet' else None)
        if req.project_type=='dotnet' and preset!='dotnet_publish': raise ValueError('A .NET project from GitHub must use the .NET publish build.')
        if req.project_type=='static' and preset not in (None,'npm_build'): raise ValueError('A static site can only use no build or the npm build.')
        if req.project_type not in ('static','dotnet'): raise ValueError("GitHub source supports project types 'static' and 'dotnet'.")
        return preset
    @staticmethod
    def _github_placeholder(req):
        # Metadata known before anything is fetched; never contains credentials.
        try: parts=validate_repo_url(req.repo_url)
        except Exception: parts={}
        return {'type':'github','repo_url':parts.get('url'),'repo':parts.get('full_name'),'requested_ref':redact(req.git_ref) if req.git_ref else 'default'}

import json
import platform
import subprocess
from pathlib import Path
from common.models.iis_models import IisSiteInfo, IisAppPoolInfo


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class IisService:
    def __init__(self, storage_root):
        self.storage_root = Path(storage_root).resolve()
        self.mock_path = self.storage_root / 'iis_mock.json'
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def is_windows(self):
        return platform.system().lower() == 'windows'

    def _ps(self, script):
        p = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
            capture_output=True, text=True, timeout=90
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or 'PowerShell failed').strip())
        return p.stdout.strip()

    def _mock(self):
        if not self.mock_path.exists():
            return {'sites': {}, 'pools': {}}
        return json.loads(self.mock_path.read_text(encoding='utf-8'))

    def _save_mock(self, d):
        self.mock_path.write_text(json.dumps(d, indent=2), encoding='utf-8')

    def get_site_info(self, site_name):
        if self.is_windows():
            n = ps_quote(site_name)
            ps = f"""
Import-Module WebAdministration
$s = Get-Website -Name {n} -ErrorAction SilentlyContinue
if ($null -eq $s) {{ @{{exists=$false; site_name={n}}} | ConvertTo-Json -Compress }}
else {{
  $b = @()
  foreach($x in $s.Bindings.Collection) {{ $b += @{{protocol=$x.protocol; bindingInformation=$x.bindingInformation}} }}
  @{{exists=$true; site_name=$s.Name; physical_path=$s.physicalPath; app_pool=$s.applicationPool; bindings=$b; state=[string]$s.State}} | ConvertTo-Json -Depth 5 -Compress
}}
"""
            d = json.loads(self._ps(ps))
            return IisSiteInfo(d.get('exists', False), d.get('site_name', site_name), d.get('physical_path'), d.get('app_pool'), d.get('bindings', []), d.get('state'))
        d = self._mock(); s = d['sites'].get(site_name)
        return IisSiteInfo(True, site_name, **s) if s else IisSiteInfo(False, site_name)

    def get_app_pool_info(self, name):
        if self.is_windows():
            n = str(name).replace("'", "''")
            ps = f"""
Import-Module WebAdministration
$p = Get-Item 'IIS:\\AppPools\\{n}' -ErrorAction SilentlyContinue
if ($null -eq $p) {{ @{{exists=$false; name='{n}'}} | ConvertTo-Json -Compress }}
else {{ @{{exists=$true; name=$p.Name; runtime=$p.managedRuntimeVersion; pipeline_mode=[string]$p.managedPipelineMode; state=[string]$p.state}} | ConvertTo-Json -Compress }}
"""
            d = json.loads(self._ps(ps))
            return IisAppPoolInfo(d.get('exists', False), d.get('name', name), d.get('runtime'), d.get('pipeline_mode'), d.get('state'))
        d = self._mock(); p = d['pools'].get(name)
        return IisAppPoolInfo(True, name, **p) if p else IisAppPoolInfo(False, name)

    def grant_iis_read(self, path):
        path = str(Path(path).resolve())
        Path(path).mkdir(parents=True, exist_ok=True)
        if self.is_windows():
            p = str(path).replace('"', '`"')
            self._ps(f'icacls "{p}" /grant "IIS_IUSRS:(OI)(CI)RX" /T | Out-Null')

    def create_app_pool(self, name, runtime='', pipeline_mode='Integrated'):
        if self.is_windows():
            n = str(name).replace("'", "''")
            runtime_q = ps_quote(runtime or '')
            pipeline_q = ps_quote(pipeline_mode or 'Integrated')
            ps = f"""
Import-Module WebAdministration
if (!(Test-Path 'IIS:\\AppPools\\{n}')) {{ New-WebAppPool -Name '{n}' | Out-Null }}
Set-ItemProperty 'IIS:\\AppPools\\{n}' managedRuntimeVersion {runtime_q}
Set-ItemProperty 'IIS:\\AppPools\\{n}' managedPipelineMode {pipeline_q}
"""
            self._ps(ps)
        else:
            d = self._mock(); d['pools'][name] = {'runtime': runtime, 'pipeline_mode': pipeline_mode, 'state': 'Started'}; self._save_mock(d)

    def create_site(self, site_name, physical_path, port, host, app_pool, protocol='http'):
        physical_path = str(Path(physical_path).resolve())
        Path(physical_path).mkdir(parents=True, exist_ok=True)
        if self.is_windows():
            n = ps_quote(site_name)
            p = ps_quote(physical_path)
            pool = ps_quote(app_pool)
            host_part = f" -HostHeader {ps_quote(host)}" if host else ''
            ps = f"""
Import-Module WebAdministration
if (!(Get-Website -Name {n} -ErrorAction SilentlyContinue)) {{ New-Website -Name {n} -Port {int(port)}{host_part} -PhysicalPath {p} -ApplicationPool {pool} | Out-Null }}
"""
            self._ps(ps)
        else:
            d = self._mock(); d['sites'][site_name] = {'physical_path': str(physical_path), 'app_pool': app_pool, 'bindings': [{'protocol': protocol, 'bindingInformation': f'*:{port}:{host or ""}'}], 'state': 'Started'}; self._save_mock(d)

    def set_site_physical_path(self, site_name, path):
        path = str(Path(path).resolve())
        Path(path).mkdir(parents=True, exist_ok=True)
        if self.is_windows():
            self._ps(f"Import-Module WebAdministration; Set-ItemProperty 'IIS:\\Sites\\{str(site_name).replace(chr(39), chr(39)+chr(39))}' -Name physicalPath -Value {ps_quote(path)}")
        else:
            d = self._mock(); d['sites'].setdefault(site_name, {}).update({'physical_path': str(path)}); self._save_mock(d)

    def set_site_app_pool(self, site_name, pool):
        if self.is_windows():
            self._ps(f"Import-Module WebAdministration; Set-ItemProperty 'IIS:\\Sites\\{str(site_name).replace(chr(39), chr(39)+chr(39))}' -Name applicationPool -Value {ps_quote(pool)}")
        else:
            d = self._mock(); d['sites'].setdefault(site_name, {}).update({'app_pool': pool}); self._save_mock(d)

    def recycle_app_pool(self, name):
        if self.is_windows():
            n = str(name).replace("'", "''")
            ps = f"Import-Module WebAdministration; if((Get-WebAppPoolState -Name '{n}').Value -eq 'Started'){{Restart-WebAppPool -Name '{n}'}} else {{Start-WebAppPool -Name '{n}'}}"
            self._ps(ps)

    def ensure(self, req, active_path, actions):
        self.grant_iis_read(active_path)
        pool = self.get_app_pool_info(req.app_pool_name)
        if not pool.exists:
            if not req.create_app_pool_if_missing:
                raise RuntimeError(f'Application pool does not exist: {req.app_pool_name}')
            self.create_app_pool(req.app_pool_name, req.app_pool_runtime or '', req.app_pool_pipeline_mode or 'Integrated')
            actions.append('Created application pool')
        site = self.get_site_info(req.site_name)
        if not site.exists:
            if not req.create_site_if_missing:
                raise RuntimeError(f'IIS site does not exist: {req.site_name}')
            self.create_site(req.site_name, active_path, req.site_port or 80, req.site_host, req.app_pool_name, req.site_protocol)
            actions.append('Created IIS site')
        else:
            self.set_site_physical_path(req.site_name, active_path)
            self.set_site_app_pool(req.site_name, req.app_pool_name)
            actions.append('Aligned IIS site path and app pool')
        self.grant_iis_read(active_path)
        actions.append('Granted IIS read access to active path')

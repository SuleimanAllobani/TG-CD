from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from common.config import load_settings
from agent.services.deployment_service import DeploymentService


SESSION = requests.Session()
SESSION.trust_env = False  # Permanent fix: never send Tailscale/private backend traffic through proxies.


def _endpoint(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return url


def _raise_clear_error(resp: requests.Response, url: str):
    if resp.status_code >= 400:
        body = (resp.text or "")[:1000]
        raise RuntimeError(f"HTTP {resp.status_code} calling {_endpoint(url)}: {body}")


def post_json(url: str, payload: dict, token: str, agent_id: str) -> dict:
    resp = SESSION.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    _raise_clear_error(resp, url)
    return resp.json() if resp.content else {}


def get_json(url: str, token: str) -> dict:
    resp = SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    _raise_clear_error(resp, url)
    return resp.json() if resp.content else {}


def download_file(url: str, dest: Path, token: str):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with SESSION.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=180, stream=True) as resp:
        _raise_clear_error(resp, url)
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


def _backend_health_check(backend: str):
    url = f"{backend}/api/health"
    resp = SESSION.get(url, timeout=20)
    _raise_clear_error(resp, url)
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Backend health returned non-JSON from {_endpoint(url)}: {(resp.text or '')[:500]}") from exc
    if not data.get("ok"):
        raise RuntimeError(f"Backend health returned not ok from {_endpoint(url)}: {data}")
    return data


def main():
    settings = load_settings()
    agent_cfg = settings["agent"]
    backend = agent_cfg.get("backend_url", settings.get("backend", {}).get("base_url", "http://127.0.0.1:9000")).rstrip("/")
    agent_id = agent_cfg.get("id", "agent_dev")
    token = agent_cfg.get("token", "dev-token")
    storage_root = Path(agent_cfg.get("storage_root", "storage")).resolve()
    interval = int(agent_cfg.get("poll_interval_seconds", 5))
    os.environ["AGENT_ID"] = agent_id

    deployer = DeploymentService(str(storage_root), git_settings=settings.get('git'))
    print(f"Phase 2.5 outbound agent started: {agent_id} -> {backend}", flush=True)
    print("Backend HTTP mode: proxy bypass enabled for agent requests.", flush=True)

    try:
        health = _backend_health_check(backend)
        print(f"Backend health OK: {health}", flush=True)
    except Exception as exc:
        print(f"Initial backend health check failed: {exc}", flush=True)

    while True:
        try:
            post_json(f"{backend}/api/agent/heartbeat", {"agent_id": agent_id, "name": agent_id, "status": "online"}, token, agent_id)
            job_response = get_json(f"{backend}/api/agent/jobs/next?agent_id={agent_id}", token)
            if not job_response or (job_response.get("job") is None and "job_id" not in job_response):
                time.sleep(interval)
                continue

            job = job_response.get("job") if isinstance(job_response.get("job"), dict) else job_response
            job_id = job["job_id"]
            print(f"Found job {job_id}", flush=True)
            post_json(f"{backend}/api/agent/jobs/{job_id}/claim", {"agent_id": agent_id}, token, agent_id)
            post_json(f"{backend}/api/agent/jobs/{job_id}/running", {"agent_id": agent_id}, token, agent_id)
            post_json(f"{backend}/api/agent/jobs/{job_id}/logs", {"agent_id": agent_id, "message": "Agent claimed job and started deployment."}, token, agent_id)

            payload = dict(job.get("payload") or {})
            if payload.get("job_type") == "rollback":
                site_name = payload.get("site_name")
                app_pool_name = payload.get("app_pool_name")
                item = deployer.releases.rollback_previous_success(site_name)
                deployer.iis.set_site_physical_path(site_name, str(deployer.releases.current_path(site_name).resolve()))
                if app_pool_name:
                    deployer.iis.recycle_app_pool(app_pool_name)
                result = {
                    "success": True,
                    "site_name": site_name,
                    "app_pool_name": app_pool_name,
                    "release_id": item.get("release_id"),
                    "message": "Rolled back to previous successful release.",
                }
            else:
                package_id = payload.get("package_id") or job.get("package_id")
                if payload.get("source_type", "uploaded_package") == "uploaded_package":
                    filename = Path(job.get("original_filename") or "package.zip").name
                    package_url = f"{backend}/api/agent/packages/{package_id}?agent_id={agent_id}"
                    folder = storage_root / "uploads" / package_id
                    folder.mkdir(parents=True, exist_ok=True)
                    zip_path = folder / filename
                    download_file(package_url, zip_path, token)
                    payload["package_id"] = package_id
                    payload["source_type"] = "uploaded_package"
                result = deployer.deploy(payload)

            post_json(f"{backend}/api/agent/jobs/{job_id}/result", {"agent_id": agent_id, **result}, token, agent_id)
            post_json(f"{backend}/api/agent/jobs/{job_id}/logs", {"agent_id": agent_id, "message": "Deployment result: " + json.dumps(result)[:7500]}, token, agent_id)
            print(f"Job {job_id} completed: {result.get('success')}", flush=True)
        except Exception as exc:
            print(f"Outbound loop error: {exc}", flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    main()

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from backend.services.database import Database
from common.git_validation import (
    GitInputError, looks_like_secret, validate_build_preset, validate_build_target, validate_credential_ref, validate_ref,
    validate_repo_url, validate_subdir,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobService:
    def __init__(self, db: Database):
        self.db = db

    def create(self, payload: dict) -> dict:
        required = ['agent_id', 'package_id', 'site_name', 'app_pool_name', 'project_type', 'health_check_url']
        is_github = payload.get('source_type') == 'github'
        if is_github:
            # GitHub jobs have no uploaded package; they need a repository instead.
            required = [k for k in required if k != 'package_id'] + ['repo_url']
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise ValueError('missing fields: ' + ', '.join(missing))
        deploy_payload = dict(payload)
        if is_github:
            deploy_payload = self._clean_github_payload(deploy_payload)
        deploy_payload.setdefault('request_id', 'req_' + uuid.uuid4().hex[:10])
        deploy_payload.setdefault('source_type', 'uploaded_package')
        deploy_payload.setdefault('site_exists_expected', False)
        deploy_payload.setdefault('create_site_if_missing', True)
        deploy_payload.setdefault('site_port', 80)
        deploy_payload.setdefault('site_host', None)
        deploy_payload.setdefault('site_protocol', 'http')
        deploy_payload.setdefault('app_pool_exists_expected', False)
        deploy_payload.setdefault('create_app_pool_if_missing', True)
        deploy_payload.setdefault('app_pool_runtime', '')
        deploy_payload.setdefault('app_pool_pipeline_mode', 'Integrated')
        deploy_payload.setdefault('notes', 'created by phase 2.5 backend')
        job = {
            'job_id': 'job_' + uuid.uuid4().hex[:12],
            'agent_id': payload['agent_id'],
            'package_id': payload.get('package_id') or '',
            'requested_by_user_id': str(payload.get('requested_by_user_id', 'unknown')),
            'site_name': payload['site_name'],
            'app_pool_name': payload['app_pool_name'],
            'project_type': payload['project_type'],
            'health_check_url': payload['health_check_url'],
            'payload': deploy_payload,
            'status': 'pending',
            'created_at': now(),
        }
        self.db.insert_job(job)
        self.db.audit(job['requested_by_user_id'], 'create_job', job['job_id'], {'agent_id': job['agent_id'], 'site_name': job['site_name']})
        return job


    @staticmethod
    def _clean_github_payload(payload: dict) -> dict:
        """Validate GitHub fields and refuse anything that looks like a credential.

        Job payloads are stored in SQLite, shown in History/Reports and sent to the
        agent, so secrets must never be accepted here. Tokens live on the agent.
        """
        if looks_like_secret(json.dumps(payload, default=str)):
            raise ValueError('Job payload appears to contain a credential; refusing to store it. '
                             'Configure the token on the agent machine and send only its name.')
        try:
            clean = dict(payload)
            clean['repo_url'] = validate_repo_url(payload.get('repo_url'))['url']
            clean['git_ref'] = validate_ref(payload.get('git_ref'))
            clean['credential_ref'] = validate_credential_ref(payload.get('credential_ref'))
            clean['subdir'] = validate_subdir(payload.get('subdir'))
            ptype = payload.get('project_type')
            preset = validate_build_preset(payload.get('build_preset'))
            if ptype == 'dotnet':
                preset = 'dotnet_publish'
            elif ptype == 'static' and preset == 'dotnet_publish':
                raise GitInputError('A static site cannot use the .NET publish build.')
            elif ptype not in ('static', 'dotnet'):
                raise GitInputError('GitHub source supports Static and .NET projects.')
            clean['build_preset'] = preset
            clean['build_target'] = validate_build_target(payload.get('build_target'), preset) if preset else None
        except GitInputError as exc:
            raise ValueError(str(exc))
        clean['package_id'] = ''
        return clean

    def create_rollback(self, payload: dict) -> dict:
        required = ['agent_id', 'site_name']
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise ValueError('missing fields: ' + ', '.join(missing))
        rollback_payload = {
            'job_type': 'rollback',
            'request_id': 'req_' + uuid.uuid4().hex[:10],
            'agent_id': payload['agent_id'],
            'requested_by_user_id': str(payload.get('requested_by_user_id', 'unknown')),
            'site_name': payload['site_name'],
            'app_pool_name': payload.get('app_pool_name') or '',
        }
        job = {
            'job_id': 'job_' + uuid.uuid4().hex[:12],
            'agent_id': payload['agent_id'],
            'package_id': '',
            'requested_by_user_id': str(payload.get('requested_by_user_id', 'unknown')),
            'site_name': payload['site_name'],
            'app_pool_name': payload.get('app_pool_name') or '',
            'project_type': 'rollback',
            'health_check_url': '',
            'payload': rollback_payload,
            'status': 'pending',
            'created_at': now(),
        }
        self.db.insert_job(job)
        self.db.audit(job['requested_by_user_id'], 'create_rollback_job', job['job_id'], {'agent_id': job['agent_id'], 'site_name': job['site_name']})
        return job

    def next_for_agent(self, agent_id: str):
        return self.db.next_job(agent_id)

    def claim(self, job_id: str, agent_id: str | None = None) -> dict:
        self.db.update_job_status(job_id, 'claimed', claimed_at=now())
        self.db.add_job_log(job_id, agent_id or '', 'Job claimed by agent')
        return {'ok': True, 'job_id': job_id, 'status': 'claimed'}

    def mark_running(self, job_id: str, agent_id: str | None = None) -> dict:
        self.db.update_job_status(job_id, 'running', started_at=now())
        self.db.add_job_log(job_id, agent_id or '', 'Job running')
        return {'ok': True, 'job_id': job_id, 'status': 'running'}

    def complete(self, job_id: str, payload: dict, agent_id: str | None = None) -> dict:
        status = 'success' if payload.get('success') else 'failed'
        summary = payload.get('message') or payload.get('summary') or payload.get('error_message') or ''
        self.db.update_job_status(
            job_id,
            status,
            completed_at=now(),
            result_summary=summary,
            error_message=payload.get('error_message') or ('' if payload.get('success') else summary),
            result_json=json.dumps(payload),
        )
        self.db.add_job_log(job_id, agent_id or '', f'Job completed: {status} - {summary}')
        return {'ok': True, 'job_id': job_id, 'status': status}

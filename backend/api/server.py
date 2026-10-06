from __future__ import annotations

import os
from functools import wraps
from flask import Flask, jsonify, request, send_file
from backend.services.database import Database
from backend.services.package_service import PackageService
from backend.services.job_service import JobService


def create_app() -> Flask:
    app = Flask(__name__)
    db = Database(os.environ.get('CICD_BACKEND_DB', 'backend/storage/db/phase25.sqlite3'))
    packages = PackageService(os.environ.get('CICD_BACKEND_PACKAGE_ROOT', 'backend/storage/packages'))
    jobs = JobService(db)
    bot_token = os.environ.get('CICD_BACKEND_BOT_TOKEN', 'phase25-bot-token')

    def bearer() -> str:
        h = request.headers.get('Authorization', '')
        return h.replace('Bearer ', '', 1).strip() if h.startswith('Bearer ') else ''

    def require_bot(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if bearer() != bot_token:
                return jsonify({'error': 'unauthorized bot'}), 401
            return fn(*args, **kwargs)
        return wrapper

    def require_agent(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = bearer()
            agent_id = request.args.get('agent_id')
            if request.is_json:
                data = request.get_json(silent=True) or {}
                agent_id = data.get('agent_id') or agent_id
            if not agent_id or not token or not db.verify_agent(agent_id, token):
                return jsonify({'error': 'unauthorized agent'}), 401
            request.agent_id = agent_id
            return fn(*args, **kwargs)
        return wrapper

    @app.get('/api/health')
    def health():
        return jsonify({'ok': True, 'service': 'phase2.5-backend'})

    # ---------- Bot-facing endpoints ----------
    @app.post('/api/bot/agents/register')
    @require_bot
    def bot_register_agent():
        payload = request.get_json(force=True)
        for k in ['agent_id', 'agent_token']:
            if not payload.get(k):
                return jsonify({'error': f'missing {k}'}), 400
        db.upsert_agent(
            payload['agent_id'],
            payload.get('name') or payload['agent_id'],
            payload['agent_token'],
            status=payload.get('status', 'pending'),
            owner_user_id=str(payload.get('owner_user_id', 'unknown')),
        )
        db.audit(str(payload.get('owner_user_id', 'unknown')), 'register_agent', payload['agent_id'], {'name': payload.get('name')})
        return jsonify({'ok': True, 'agent_id': payload['agent_id']})

    @app.get('/api/bot/agents')
    @require_bot
    def bot_agents():
        return jsonify({'agents': db.list_agents()})

    @app.get('/api/bot/agents/<agent_id>')
    @require_bot
    def bot_agent(agent_id: str):
        item = db.get_agent(agent_id)
        if not item:
            return jsonify({'error': 'agent not found'}), 404
        safe = dict(item)
        safe.pop('token', None)
        return jsonify(safe)


    @app.delete('/api/bot/agents/<agent_id>')
    @require_bot
    def bot_delete_agent(agent_id: str):
        item = db.get_agent(agent_id)
        if not item:
            return jsonify({'error': 'agent not found'}), 404
        db.delete_agent(agent_id)
        db.audit('bot', 'delete_agent', agent_id, {'name': item.get('name')})
        return jsonify({'ok': True, 'agent_id': agent_id})

    @app.get('/api/bot/agents/<agent_id>/history')
    @require_bot
    def bot_agent_history(agent_id: str):
        if not db.get_agent(agent_id):
            return jsonify({'error': 'agent not found'}), 404
        limit = int(request.args.get('limit', '10'))
        return jsonify({'agent_id': agent_id, 'jobs': db.list_jobs_for_agent(agent_id, limit)})

    @app.get('/api/bot/jobs')
    @require_bot
    def bot_recent_jobs():
        limit = int(request.args.get('limit', '10'))
        return jsonify({'jobs': db.list_recent_jobs(limit)})

    @app.post('/api/bot/packages')
    @require_bot
    def upload_package():
        if 'file' not in request.files:
            return jsonify({'error': 'missing file'}), 400
        uploaded = request.files['file']
        user_id = request.form.get('user_id', 'unknown')
        info = packages.save(uploaded, uploaded.filename or 'package.zip', user_id)
        db.insert_package(info)
        db.audit(str(user_id), 'upload_package', info['package_id'], {'filename': info['original_filename']})
        return jsonify(info)

    @app.post('/api/bot/jobs/deploy')
    @require_bot
    def create_deploy_job():
        try:
            payload = request.get_json(force=True)
            if not db.get_agent(payload.get('agent_id', '')):
                return jsonify({'error': 'unknown agent'}), 404
            job = jobs.create(payload)
            return jsonify(job)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400


    @app.post('/api/bot/jobs/rollback')
    @require_bot
    def create_rollback_job():
        try:
            payload = request.get_json(force=True)
            if not db.get_agent(payload.get('agent_id', '')):
                return jsonify({'error': 'unknown agent'}), 404
            job = jobs.create_rollback(payload)
            return jsonify(job)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 400

    @app.get('/api/bot/jobs/<job_id>')
    @require_bot
    def get_job(job_id: str):
        job = db.get_job(job_id)
        if not job:
            return jsonify({'error': 'job not found'}), 404
        return jsonify(job)

    @app.get('/api/bot/jobs/<job_id>/logs')
    @require_bot
    def get_job_logs(job_id: str):
        return jsonify({'logs': db.get_job_logs(job_id)})

    # ---------- Agent-facing endpoints ----------
    @app.post('/api/agent/heartbeat')
    @require_agent
    def heartbeat():
        payload = request.get_json(force=True)
        agent_id = request.agent_id
        existing = db.get_agent(agent_id)
        db.upsert_agent(agent_id, existing.get('name') if existing else payload.get('name', agent_id), existing.get('token') if existing else bearer(), status='online', owner_user_id=existing.get('owner_user_id') if existing else None)
        return jsonify({'ok': True})

    @app.get('/api/agent/jobs/next')
    @require_agent
    def next_job():
        agent_id = request.agent_id
        job = jobs.next_for_agent(agent_id)
        return jsonify(job or {'job': None})

    @app.post('/api/agent/jobs/<job_id>/claim')
    @require_agent
    def claim_job(job_id: str):
        return jsonify(jobs.claim(job_id, request.agent_id))

    @app.post('/api/agent/jobs/<job_id>/running')
    @require_agent
    def running_job(job_id: str):
        return jsonify(jobs.mark_running(job_id, request.agent_id))

    @app.get('/api/agent/packages/<package_id>')
    @require_agent
    def download_package(package_id: str):
        package = db.get_package(package_id)
        if not package:
            return jsonify({'error': 'package not found'}), 404
        return send_file(package['stored_path'], as_attachment=True, download_name=package['original_filename'])

    @app.post('/api/agent/jobs/<job_id>/result')
    @require_agent
    def job_result(job_id: str):
        payload = request.get_json(force=True)
        return jsonify(jobs.complete(job_id, payload, request.agent_id))

    @app.post('/api/agent/jobs/<job_id>/logs')
    @require_agent
    def job_logs(job_id: str):
        payload = request.get_json(force=True)
        msg = payload.get('message') or payload.get('logs') or ''
        db.add_job_log(job_id, request.agent_id, msg[:8000])
        return jsonify({'ok': True})

    return app


def run():
    from waitress import serve
    port = int(os.environ.get('CICD_BACKEND_PORT', '9000'))
    host = os.environ.get('CICD_BACKEND_HOST', '0.0.0.0')
    app = create_app()
    print(f'Phase 2.5 backend running on http://{host}:{port}', flush=True)
    serve(app, host=host, port=port)

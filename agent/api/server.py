from flask import Flask, request, jsonify
from common.config import load_settings
from common.auth import extract_bearer
from agent.services.deployment_service import DeploymentService

settings = load_settings()
agent_cfg = settings['agent']
TOKEN = agent_cfg.get('token', 'LOCAL_TEST_123')
service = DeploymentService(agent_cfg.get('storage_root', 'storage'), git_settings=settings.get('git'))
app = Flask(__name__)


def require_auth():
    got = extract_bearer(request.headers.get('Authorization'))
    if got != TOKEN:
        return jsonify({'error': 'unauthorized'}), 401
    return None


@app.before_request
def _auth():
    if request.path.startswith('/api/'):
        r = require_auth()
        if r:
            return r


@app.get('/api/health')
def health():
    return jsonify({'ok': True})


@app.get('/api/status')
def status():
    return jsonify({'status': 'ok', 'agent': 'running', 'storage_root': agent_cfg.get('storage_root', 'storage')})


@app.get('/api/iis/site-info')
def site_info():
    return jsonify(service.iis.get_site_info(request.args.get('site_name', '')).to_dict())


@app.get('/api/iis/app-pool-info')
def pool_info():
    return jsonify(service.iis.get_app_pool_info(request.args.get('app_pool_name', '')).to_dict())


@app.post('/api/upload-package')
def upload_package():
    if 'file' not in request.files:
        return jsonify({'error': 'file is required'}), 400
    return jsonify(service.packages.store_file(request.files['file']))


@app.post('/api/deploy')
def deploy():
    return jsonify(service.deploy(request.get_json(force=True)))


@app.post('/api/rollback')
def rollback():
    data = request.get_json(silent=True) or {}
    site_name = data.get('site_name') or request.args.get('site_name')
    app_pool_name = data.get('app_pool_name') or request.args.get('app_pool_name')
    if not site_name:
        return jsonify({'success': False, 'error': 'site_name is required'}), 400
    try:
        item = service.releases.rollback_previous_success(site_name)
        service.iis.set_site_physical_path(site_name, str(service.releases.current_path(site_name)))
        if app_pool_name:
            service.iis.recycle_app_pool(app_pool_name)
        return jsonify({'success': True, 'site_name': site_name, 'release_id': item['release_id'], 'message': 'Rolled back to previous successful release.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.get('/api/releases')
def releases():
    return jsonify({'releases': service.releases.list(request.args.get('site_name', ''))})


@app.get('/api/logs')
def logs():
    return jsonify({'logs': service.logs.tail(request.args.get('site_name'))})


@app.delete('/api/releases/<site_name>/<release_id>')
def delete_release(site_name, release_id):
    try:
        service.releases.delete(site_name, release_id)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


def run():
    from waitress import serve
    host = agent_cfg.get('host', '127.0.0.1')
    port = int(agent_cfg.get('port', 8765))
    print(f'Agent running on http://{host}:{port}')
    serve(app, host=host, port=port)


if __name__ == '__main__':
    run()

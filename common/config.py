import json, os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load_settings():
    path = ROOT / 'config' / 'settings.json'
    if not path.exists():
        agent_path = ROOT / 'config' / 'agent_settings.json'
        path = agent_path if agent_path.exists() else ROOT / 'config' / 'settings.example.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['telegram_bot_token'] = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token',''))
    data.setdefault('backend', {})
    data['backend']['enabled'] = str(os.getenv('CICD_BACKEND_ENABLED', data['backend'].get('enabled', False))).lower() in ('1','true','yes','on')
    data['backend']['base_url'] = os.getenv('CICD_BACKEND_URL', data['backend'].get('base_url', data.get('backend_url','http://127.0.0.1:9000'))).rstrip('/')
    data['backend']['public_url'] = os.getenv('CICD_BACKEND_PUBLIC_URL', data['backend'].get('public_url', data['backend'].get('base_url','http://127.0.0.1:9000'))).rstrip('/')
    data['backend']['bot_token'] = os.getenv('CICD_BACKEND_BOT_TOKEN', data['backend'].get('bot_token','phase25-bot-token'))
    data.setdefault('git', {})  # optional GitHub source settings (allowed_repos, credentials, ...)
    data.setdefault('agent', {})
    data['agent']['id'] = os.getenv('AGENT_ID', data['agent'].get('id','local_agent'))
    data['agent']['token'] = os.getenv('AGENT_TOKEN', data['agent'].get('token','LOCAL_TEST_123'))
    data['agent']['host'] = os.getenv('AGENT_HOST', data['agent'].get('host','127.0.0.1'))
    data['agent']['port'] = int(os.getenv('AGENT_PORT', data['agent'].get('port',8765)))
    data['agent']['mode'] = os.getenv('AGENT_MODE', data['agent'].get('mode', data.get('mode','direct')))
    data['agent']['backend_url'] = os.getenv('AGENT_BACKEND_URL', data['agent'].get('backend_url', data.get('backend_url', data['backend'].get('base_url','http://127.0.0.1:9000')))).rstrip('/')
    data['agent']['poll_interval_seconds'] = int(os.getenv('AGENT_POLL_INTERVAL', data['agent'].get('poll_interval_seconds', 5)))
    storage_root = os.getenv('AGENT_STORAGE_ROOT', data['agent'].get('storage_root','storage'))
    storage_path = Path(storage_root)
    if not storage_path.is_absolute():
        storage_path = ROOT / storage_path
    data['agent']['storage_root'] = str(storage_path.resolve())
    return data

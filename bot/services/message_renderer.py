def source_label(src):
    """'owner/repo@ref (abc1234)' for a GitHub source dict, '' for ZIP/folder."""
    if not src or src.get('type') != 'github':
        return ''
    label = f"{src.get('repo') or src.get('repo_url') or 'GitHub'}@{src.get('requested_ref') or 'default'}"
    if src.get('commit_short'):
        label += f" ({src['commit_short']})"
    return label


def _source_line(r):
    label = source_label(r.get('source'))
    if not label:
        return ''
    out = f"Source: {label}\n"
    b = (r.get('source') or {}).get('build')
    if b:
        out += f"Build: {b.get('preset')} ({b.get('seconds')}s)\n"
    return out


def deploy_result_text(r):
    if r.get('success'):
        return (
            "✅ Deployment completed successfully.\n"
            f"Site: {r.get('site_name')}\n"
            f"App pool: {r.get('app_pool_name')}\n"
            f"Release: {r.get('release_id')}\n"
            f"{_source_line(r)}"
            "Health check: passed."
        )
    if r.get('first_deployment_failure'):
        return (
            "❌ Deployment failed on first release.\n"
            "No previous release exists, so destructive rollback was skipped.\n"
            f"{_source_line(r)}"
            f"Failed release preserved: {r.get('failed_release_preserved')}\n"
            f"Reason: {r.get('message')}"
        )
    if r.get('rolled_back'):
        return (
            "❌ Deployment failed.\n"
            "✅ Rollback completed to previous successful release.\n"
            f"{_source_line(r)}"
            f"Failed release preserved: {r.get('failed_release_preserved')}\n"
            f"Reason: {r.get('message')}"
        )
    return (
        "❌ Deployment failed.\n"
        "Rollback: not completed.\n"
        f"{_source_line(r)}"
        f"Reason: {r.get('message')}"
    )

def _build_summary_line(s):
    preset = s.get('build_preset')
    if not preset:
        return "Build: none (files deployed as they are)\n"
    name = {'dotnet_publish': '.NET publish', 'npm_build': 'npm build'}.get(preset, preset)
    target = s.get('build_target') or 'auto-detect'
    return f"Build: {name} ({target})\n"


def _github_summary_lines(s):
    if s.get('source_type') != 'github':
        return ''
    ref = s.get('git_ref') or 'default branch'
    auth = f"private (token '{s['credential_ref']}' on the agent)" if s.get('credential_ref') else 'public'
    sub = s.get('subdir') or 'repository root'
    return (
        f"Repository: {s.get('repo_url')}\n"
        f"Branch/tag/commit: {ref}\n"
        f"Access: {auth}\n"
        f"Deploy folder: {sub}\n"
        f"{_build_summary_line(s)}"
    )


def summary(s):
    return (
        "Please confirm deployment:\n"
        f"Agent: {s.get('agent_id')}\n"
        f"Project: {s.get('project_type')}\n"
        f"Source: {s.get('source_type')}\n"
        f"{_github_summary_lines(s)}"
        f"Site: {s.get('site_name')}\n"
        f"Create site if missing: {s.get('create_site_if_missing')}\n"
        f"Port: {s.get('site_port')}\n"
        f"App pool: {s.get('app_pool_name')}\n"
        f"Create pool if missing: {s.get('create_app_pool_if_missing')}\n"
        f"Health URL: {s.get('health_check_url')}"
    )

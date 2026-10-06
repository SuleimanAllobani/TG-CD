from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any


def _lower_text(job: dict[str, Any], logs: list[dict[str, Any]] | str | None = None) -> str:
    parts: list[str] = []
    for key in ("status", "result_summary", "error_message", "health_check_url", "site_name", "app_pool_name", "project_type"):
        value = job.get(key)
        if value:
            parts.append(str(value))
    for key in ("payload", "result"):
        value = job.get(key)
        if value:
            try:
                parts.append(json.dumps(value, ensure_ascii=False))
            except Exception:
                parts.append(str(value))
    if isinstance(logs, list):
        for item in logs:
            if isinstance(item, dict):
                parts.append(str(item.get("message", "")))
            else:
                parts.append(str(item))
    elif logs:
        parts.append(str(logs))
    return "\n".join(parts).lower()


def _stage_from_text(text: str, job: dict[str, Any]) -> str:
    status = (job.get("status") or "unknown").lower()
    if status == "pending":
        return "Job queue / agent pickup"
    if status in {"claimed", "running"}:
        return "Agent execution"
    if "github source:" in text and ("build failed" in text or "builds run code" in text):
        return "Build (GitHub source)"
    if "github source:" in text:
        return "Source fetch (GitHub)"
    if "health" in text or "404" in text or "not found" in text or "timeout" in text:
        return "Health check"
    if "rollback" in text:
        return "Rollback / release restore"
    if "iis" in text or "webadministration" in text or "application pool" in text or "app pool" in text:
        return "IIS configuration"
    if "zip" in text or "package" in text or "extract" in text:
        return "Package preparation"
    if status == "success":
        return "Completed"
    return "Deployment execution"


def analyze_job(job: dict[str, Any], logs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Deterministic Phase 3 diagnostic analyzer.

    This is intentionally rule-based and offline. It classifies common IIS/CD
    failures and produces safe recommendations without executing any action.
    """
    logs = logs or []
    text = _lower_text(job, logs)
    status = (job.get("status") or "unknown").lower()
    stage = _stage_from_text(text, job)

    findings: list[str] = []
    likely_causes: list[str] = []
    recommendations: list[str] = []
    severity = "low"

    def add(cause: str, rec: str, finding: str | None = None, sev: str | None = None):
        if finding and finding not in findings:
            findings.append(finding)
        if cause not in likely_causes:
            likely_causes.append(cause)
        if rec not in recommendations:
            recommendations.append(rec)
        nonlocal severity
        if sev == "high" or (sev == "medium" and severity == "low"):
            severity = sev

    if status == "success":
        findings.append("Deployment job finished successfully.")
        recommendations.append("No corrective action is required. Keep this release as a rollback point.")

    if status == "pending":
        add(
            "Outbound agent has not picked up the job yet.",
            "Check that the agent service is running, online in /status, and can reach the backend URL from its config.",
            "Job is still pending.",
            "medium",
        )

    if "502" in text or "bad gateway" in text:
        add(
            "Backend request may be going through a proxy or a gateway that cannot reach the Tailscale/backend address.",
            "Verify the agent service uses the no-proxy build, then run scripts\\test_backend_connection.bat from the agent folder.",
            "502 Bad Gateway detected.",
            "medium",
        )

    if "10054" in text or "forcibly closed" in text:
        add(
            "The backend connection was reset during polling or reporting.",
            "Check backend service logs, Tailscale connectivity, and whether antivirus/proxy software is interrupting the connection.",
            "Connection reset detected.",
            "medium",
        )

    if "401" in text or "unauthorized" in text:
        add(
            "Agent or bot token does not match the backend registration.",
            "Regenerate the agent ZIP from Telegram, reinstall the agent service, and avoid editing agent_settings.json manually.",
            "Authorization failure detected.",
            "high",
        )

    if "403" in text or "forbidden" in text:
        add(
            "The request was rejected due to permission or access rules.",
            "Check backend authorization, agent ownership, and service access permissions.",
            "Forbidden response detected.",
            "medium",
        )

    if ("404" in text or "not found" in text) and "github source:" not in text:
        add(
            "Health URL path is wrong or the expected file is not at the IIS site root.",
            "Open the site root on the agent device and verify index.html exists in the deployed current release folder.",
            "HTTP 404 / Not Found detected.",
            "medium",
        )

    if "500" in text or "internal server error" in text:
        add(
            "The deployed application started but failed during request processing.",
            "Check application logs, IIS logs, runtime dependencies, and web.config/runtime configuration.",
            "HTTP 500 / application runtime failure detected.",
            "high",
        )

    if "timeout" in text or "timed out" in text:
        add(
            "The health check did not receive a response in time.",
            "Verify IIS binding/port, Windows Firewall, app pool state, and whether the application needs a longer startup time.",
            "Timeout detected.",
            "medium",
        )

    if "connection refused" in text or "actively refused" in text or "winerror 10061" in text:
        add(
            "Nothing is listening on the target host/port or the site is stopped.",
            "Check IIS site binding, port, app pool state, backend service status, and firewall rules.",
            "Connection refused detected.",
            "medium",
        )

    if "access is denied" in text or "access denied" in text or "elevated" in text or "webadministration" in text:
        add(
            "The agent does not have enough Windows/IIS permissions.",
            "Run the agent service as LocalSystem or an administrator-level service account and verify IIS Management Tools are installed.",
            "IIS/Windows permission issue detected.",
            "high",
        )

    if "github source:" in text:
        if "cannot access the repository" in text:
            add(
                "The repository is private, misspelled, or the token on the agent cannot read it.",
                "Check the repository URL. For a private repository, make sure a read-only token with access to it is configured on the agent machine (python -m agent.set_git_token) and choose that credential name.",
                "GitHub repository access failure detected.",
                "high",
            )
        if "was not found as a branch or tag" in text or "requested commit was not found" in text or "requested branch/tag/commit was not found" in text:
            add(
                "The branch, tag or commit does not exist in the repository.",
                "Check the spelling of the branch/tag/commit, or deploy the default branch.",
                "GitHub ref not found.",
                "medium",
            )
        if "git is not installed" in text:
            add(
                "Git is not installed (or not on PATH) on the agent machine.",
                "Install Git for Windows on the agent machine and restart the agent service.",
                "Git executable not found on the agent.",
                "high",
            )
        if "is not configured on the agent machine" in text or "is malformed" in text:
            add(
                "The credential name used for this deployment has no token configured on the agent.",
                "Run python -m agent.set_git_token --alias <name> on the agent machine, or choose a public repository.",
                "Credential missing on the agent.",
                "high",
            )
        if "build failed" in text or "builds run code" in text:
            if ".net sdk is not installed" in text or "could not start dotnet" in text:
                add(
                    "The .NET SDK was not found on the agent machine.",
                    "Install the .NET SDK (the version the project targets) and the ASP.NET Core Hosting Bundle on the agent machine, then restart the agent service.",
                    ".NET SDK missing on the agent.",
                    "high",
                )
            if "node.js/npm is not installed" in text or "could not start npm" in text:
                add(
                    "Node.js / npm was not found on the agent machine.",
                    "Install Node.js LTS on the agent machine and restart the agent service so it sees the new PATH.",
                    "npm missing on the agent.",
                    "high",
                )
            if "builds run code from the repository" in text:
                add(
                    "Builds are only allowed for repositories listed in git.allowed_repos on the agent.",
                    "If you trust this repository, add it (owner/repo or owner/*) to git.allowed_repos in config\\settings.json on the agent and restart the agent service. Otherwise deploy without a build.",
                    "Build blocked by the agent allowlist.",
                    "medium",
                )
            if "several .net projects" in text:
                add(
                    "The repository contains several web projects and the agent cannot choose one.",
                    "Redeploy and send the path of the .csproj to publish (for example src/Web/Web.csproj).",
                    "Ambiguous .NET project.",
                    "medium",
                )
            if "build failed" in text and "timed out" in text:
                add(
                    "The build ran longer than the agent's build timeout and was stopped.",
                    "Increase git.build_timeout_seconds in the agent settings, or build the project elsewhere and deploy the output.",
                    "Build timeout.",
                    "medium",
                )
            if "exit code" in text:
                add(
                    "The build command failed (compile error, missing package or failing script).",
                    "Open the deployment log (Logs) to see the last build output lines, fix the problem in the repository and deploy again. Nothing on IIS was changed.",
                    "Build command failed.",
                    "high",
                )
        if "allowed list" in text:
            add(
                "The repository is not in the agent's allowed_repos list.",
                "Add the repository (owner/repo or owner/*) to git.allowed_repos in the agent settings if it is trusted.",
                "Repository blocked by the agent allowlist.",
                "medium",
            )
        if "network error while contacting github" in text or ("timed out" in text and "build failed" not in text):
            add(
                "The agent could not reach GitHub in time.",
                "Check the agent machine's internet access, proxy settings and firewall for github.com on port 443.",
                "GitHub network problem.",
                "medium",
            )

    if "no previous release" in text or "first deployment" in text or "first release" in text:
        add(
            "This appears to be a first-deployment failure, so no safe previous release existed for rollback.",
            "Preserve the failed release for inspection, fix the deployment input, and deploy again; destructive rollback should remain skipped.",
            "First-deployment failure behavior detected.",
            "medium",
        )

    if "rollback completed" in text or "previous release restored" in text:
        findings.append("Rollback completed and the previous release was restored.")
        recommendations.append("Verify the restored site in a browser and keep the failed release for debugging evidence.")

    if "nssm" in text and ("can't open service" in text or "illegal characters" in text):
        add(
            "The Windows service installer hit an NSSM setup/path issue.",
            "Use the latest generated agent ZIP and reinstall; the installer should check service existence before stop/remove and avoid malformed NSSM paths.",
            "NSSM installer issue detected.",
            "medium",
        )

    if not findings:
        findings.append("No specific known failure pattern was detected.")
    if not likely_causes and status != "success":
        likely_causes.append("The failure is not recognized by the current rule set and needs manual log review.")
    if not recommendations:
        recommendations.append("Review the job logs, IIS site/app pool status, package structure, and backend/agent service logs.")

    title_status = status.upper() if status else "UNKNOWN"
    summary = f"Job {job.get('job_id', '-')}: {title_status}. Main stage: {stage}."

    return {
        "summary": summary,
        "status": status,
        "stage": stage,
        "severity": severity,
        "findings": findings[:8],
        "likely_causes": likely_causes[:8],
        "recommendations": recommendations[:8],
        "safety_note": "This analyzer only explains failures and recommendations. It does not execute IIS changes, deployments, rollbacks, or deletions.",
    }


def format_analysis(analysis: dict[str, Any], job: dict[str, Any]) -> str:
    def block(title: str, items: list[str]) -> list[str]:
        if not items:
            return []
        return [title] + [f"{i}. {item}" for i, item in enumerate(items, 1)]

    lines: list[str] = []
    lines.append("Deployment analysis")
    lines.append("")
    lines.append(f"Job: {job.get('job_id', '-')}")
    lines.append(f"Agent: {job.get('agent_id', '-')}")
    lines.append(f"Site: {job.get('site_name', '-')}")
    lines.append(f"Status: {analysis.get('status', '-')}")
    lines.append(f"Stage: {analysis.get('stage', '-')}")
    lines.append(f"Severity: {analysis.get('severity', '-')}")
    lines.append("")
    lines.append(str(analysis.get("summary", "")))
    for title, key in [
        ("\nFindings", "findings"),
        ("\nLikely causes", "likely_causes"),
        ("\nRecommended actions", "recommendations"),
    ]:
        lines.extend(block(title, analysis.get(key) or []))
    lines.append("")
    lines.append(analysis.get("safety_note", ""))
    return "\n".join(lines).strip()[:3900]


def format_report(job: dict[str, Any], logs: list[dict[str, Any]] | None = None) -> str:
    logs = logs or []
    result = job.get("result") or {}
    payload = job.get("payload") or {}
    lines = [
        "Deployment report",
        "",
        f"Job: {job.get('job_id', '-')}",
        f"Status: {job.get('status', '-')}",
        f"Agent: {job.get('agent_id', '-')}",
        f"Requested by: {job.get('requested_by_user_id', '-')}",
        f"Project type: {job.get('project_type', '-')}",
        f"Site: {job.get('site_name', '-')}",
        f"Application pool: {job.get('app_pool_name', '-')}",
        f"Health URL: {job.get('health_check_url', '-')}",
        f"Created: {job.get('created_at', '-')}",
        f"Started: {job.get('started_at') or '-'}",
        f"Completed: {job.get('completed_at') or '-'}",
        "",
        "Result summary:",
        str(job.get('result_summary') or result.get('message') or job.get('error_message') or '-'),
    ]
    if result:
        for key in ("release_id", "rollback_completed", "failed_release_preserved", "current_path", "previous_release_id"):
            if key in result:
                lines.append(f"{key}: {result.get(key)}")
        src = result.get("source")
        if isinstance(src, dict) and src.get("type") == "github":
            lines.append(f"Source: GitHub {src.get('repo') or src.get('repo_url')} @ {src.get('requested_ref')}")
            if src.get("commit"):
                lines.append(f"Commit: {src.get('commit')}")
    if logs:
        lines.append("")
        lines.append("Recent log lines:")
        for item in logs[-6:]:
            msg = item.get("message", "") if isinstance(item, dict) else str(item)
            msg = re.sub(r"\s+", " ", msg).strip()
            if len(msg) > 160:
                msg = msg[:157] + "..."
            lines.append(f"- {msg}")
    return "\n".join(lines).strip()[:3900]

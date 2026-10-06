"""Phase 3 placeholder: AI-assisted deployment analysis.

The current function is deterministic and safe for thesis testing. It can later
be replaced by a real LLM call while keeping the same interface.
"""

def analyze_failure(logs: str, deployment_result: dict) -> dict:
    text = (logs or "") + "\n" + str(deployment_result or {})
    recommendations = []
    if "404" in text or "Not Found" in text:
        recommendations.append("Check the health URL and verify index.html exists at the IIS site root.")
    if "Unauthorized" in text or "401" in text:
        recommendations.append("Verify the agent token and Authorization header.")
    if "WebAdministration" in text or "elevated" in text:
        recommendations.append("Run the agent service with sufficient privileges to manage IIS.")
    if not recommendations:
        recommendations.append("Review the deployment log, IIS binding, app pool status, and package structure.")
    return {
        "summary": "AI analysis placeholder generated a rule-based failure summary.",
        "recommendations": recommendations,
        "risk_level": "low",
    }

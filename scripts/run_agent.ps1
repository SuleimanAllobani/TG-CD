param([string]$AgentToken="LOCAL_TEST_123", [int]$Port=8765)
$env:AGENT_TOKEN=$AgentToken
$env:AGENT_PORT="$Port"
python -m agent.main

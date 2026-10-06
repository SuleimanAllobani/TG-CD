# Phase 2.5 Full Test Guide — Central Backend + Outbound Agent

## Goal

Phase 2.5 replaces the direct connection model:

```text
Bot laptop -> Agent laptop
```

with the backend/outbound model:

```text
Telegram Bot -> Central Backend <- Outbound Agent -> IIS
```

The bot laptop no longer needs to call the agent laptop directly. The agent connects outward to the backend, polls for jobs, downloads packages, deploys to IIS locally, and reports results back.

---

## Recommended test setup

For the first Phase 2.5 test, use two laptops:

```text
Old laptop: Bot service + Backend service
Main laptop: Outbound Agent service + IIS
```

Both laptops can be on the same Wi-Fi for the first test. Later, the backend can be moved to a VPS/cloud server.

---

## Part 1 — Old laptop: configure backend URL

Extract this ZIP on the old laptop:

```text
C:\CICD_PHASE25
```

Edit:

```text
C:\CICD_PHASE25\config\settings.json
```

Set your bot token and backend URL.

If backend runs on the old laptop, find the old laptop IP with `ipconfig`, for example `192.168.1.30`, then set:

```json
{
  "telegram_bot_token": "YOUR_BOT_TOKEN",
  "backend": {
    "enabled": true,
    "base_url": "http://192.168.1.30:9000",
    "bot_token": "phase25-bot-token"
  }
}
```

Important: do **not** use `127.0.0.1` as backend URL inside the generated agent package, because the agent laptop would interpret that as itself.

---

## Part 2 — Old laptop: start backend

Manual mode first:

```cmd
cd C:\CICD_PHASE25
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python -u -m backend.main
```

Test in another CMD:

```cmd
curl http://127.0.0.1:9000/api/health
```

Expected:

```json
{"ok":true,"service":"phase2.5-backend"}
```

Optional service mode:

```cmd
scripts\install_backend_service.bat
```

This requires NSSM in PATH.

---

## Part 3 — Old laptop: start bot

Manual mode:

```cmd
cd C:\CICD_PHASE25
.venv\Scripts\activate.bat
python -u -m bot.main
```

In Telegram:

```text
/start
/backend_status
```

Expected: backend online.

Optional service mode:

```cmd
scripts\install_bot_service.bat
```

If Telegram requires VPN, set the bot service Log On account to your Windows user.

---

## Part 4 — Generate outbound agent package

In Telegram:

```text
/setup_agent Main-Laptop-Outbound
```

The bot should send an agent setup ZIP. This ZIP contains:

- unique agent ID
- unique agent token
- backend URL
- outbound agent mode
- Windows service installer

Download this ZIP on the main/IIS laptop.

---

## Part 5 — Main laptop: install outbound agent

Extract generated package to a folder such as:

```text
C:\CICD_AGENT_OUTBOUND
```

Make sure NSSM is available in PATH on the main laptop.

Right-click:

```text
scripts\install_agent_service.bat
```

Choose:

```text
Run as Administrator
```

Check service:

```cmd
sc query type= service state= all | findstr /I CICD
```

The service name should look like:

```text
TelegramCICDAgent_agent_XXXXXX
```

In outbound mode, you do not need to test `http://agent-ip:8765`. The agent should poll the backend.

---

## Part 6 — Confirm agent heartbeat

In Telegram:

```text
/status
```

Expected:

```text
Backend agent status
Status: online
Last seen: ...
```

If it is offline:

- check backend is running
- check agent service is running
- check agent can reach backend URL
- open `logs\agent_stderr.log` in the agent folder

---

## Part 7 — Deploy through backend job queue

In Telegram:

```text
/deploy
```

Use:

```text
Agent: Main-Laptop-Outbound
Project type: static
Source: ZIP upload
Site name: DemoSite25
Site exists: No
Port: 8087
App pool: DemoPool25
App pool exists: No
Health URL: http://127.0.0.1:8087/index.html
```

Expected flow:

```text
Bot uploads ZIP to backend
Backend creates job
Outbound agent polls job
Agent downloads package
Agent deploys to IIS
Agent reports result
Bot shows success/failure
```

Verify on the main laptop:

```text
http://127.0.0.1:8087/index.html
```

---

## Part 8 — Rollback test

Deploy a second ZIP but use bad health URL:

```text
http://127.0.0.1:8087/notfound
```

Expected:

- job fails
- agent performs rollback if previous successful release exists
- failed release is preserved
- bot reports rollback result

---

## What proves Phase 2.5

Phase 2.5 is validated if:

- backend runs
- bot can connect to backend
- outbound agent heartbeats to backend
- bot creates deployment job
- agent pulls job without bot calling agent directly
- agent deploys to IIS
- agent reports result
- rollback works through backend job flow

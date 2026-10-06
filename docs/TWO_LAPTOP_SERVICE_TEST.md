# Phase 2: Two-Laptop Windows Service Test

## Goal
Run the Telegram bot as a Windows service on Laptop A and the IIS agent as a Windows service on Laptop B.

## Laptop A: Bot server

1. Extract this package to `C:\CICD_Bot`.
2. Edit `config\settings.json` and set your Telegram bot token.
3. Right-click `scripts\install_bot_service.bat` and choose **Run as Administrator**.
4. Open Telegram and send `/start`.

## Generate an agent installer

In Telegram:

```text
/setup_agent IIS-Laptop-B
```

The bot sends a unique agent setup ZIP. Download it on Laptop B.

## Laptop B: Agent + IIS server

1. Extract the agent setup ZIP to something like `C:\CICD_Agent`.
2. Right-click `scripts\install_agent_service.bat` and choose **Run as Administrator**.
3. The installer will:
   - create `.venv`
   - install requirements
   - install the agent as a Windows service
   - open firewall port 8765
   - grant IIS read access to storage
   - start the service
4. Find Laptop B's LAN IP address using `ipconfig`.

## Activate the agent in Telegram

Back in Telegram on Laptop A:

```text
/activate_agent <agent_id> http://<LAPTOP-B-IP>:8765
```

Example:

```text
/activate_agent agent_AB12CD http://192.168.1.55:8765
```

Then:

```text
/myagents
/use_agent agent_AB12CD
/status
```

## Deploy

Use:

```text
/deploy
```

Recommended first IIS site:

- Site name: `DemoSiteRemote`
- Port: `8085`
- App pool: `DemoPoolRemote`
- Health URL from Laptop A: `http://<LAPTOP-B-IP>:8085/index.html`

## Expected proof

- Bot runs as `TelegramCICDBot` service on Laptop A.
- Agent runs as `TelegramCICDAgent_<agent_id>` service on Laptop B.
- Telegram can activate and call the remote agent.
- ZIP uploaded to bot is sent to Laptop B agent.
- IIS on Laptop B serves the site.
- Rollback and first-deployment failure behave like Phase 1.

## Common issues

- Use Laptop B IP, not `127.0.0.1`, when registering from Laptop A.
- Agent installer must run as Administrator.
- If `/status` fails, check Windows Firewall on Laptop B and port 8765.
- If browser fails from Laptop A, check Windows Firewall for the IIS site port, e.g. 8085.

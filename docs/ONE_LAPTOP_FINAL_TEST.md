# One-Laptop Final Test Guide

Use this first before testing on 2 or 5 laptops.

## 0. Requirements

- Windows laptop
- Python 3.10+
- IIS enabled
- PowerShell as Administrator for preparation
- Telegram bot token from BotFather

## 1. Extract project

Example:

```text
C:\telegram_iis_phase1
```

Open PowerShell as Administrator in that folder.

## 2. Prepare

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\prepare_one_laptop.ps1
```

Then edit:

```text
config\settings.json
```

Set:

```json
"telegram_bot_token": "YOUR_BOT_TOKEN"
```

## 3. Start the agent

Terminal 1:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agent.ps1
```

Expected:

```text
Agent running on http://127.0.0.1:8765
```

## 4. Start the bot

Terminal 2:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_bot.ps1
```

## 5. Register local agent in Telegram

```text
/register_agent local1 MyLaptop http://127.0.0.1:8765 LOCAL_TEST_123
/status
```

Expected: bot says the agent is online.

## 6. Prepare a good ZIP

Create folder:

```text
C:\demo_app_good
```

Create `index.html`:

```html
<h1>Version 1 - Working</h1>
```

Zip the contents to:

```text
C:\demo_app_good.zip
```

Important: the ZIP should contain `index.html` at the root, not inside an extra nested folder.

## 7. Successful deployment test

In Telegram:

```text
/deploy
```

Wizard answers:

- agent: `MyLaptop`
- project type: `Static`
- source: `Upload ZIP here`
- send `C:\demo_app_good.zip`
- site name: `DemoSite`
- site exists: `No, create it`
- port: recommended `8085` if port 80 is already used
- app pool name: `DemoPool`
- app pool exists: `No, create it`
- health URL: `http://127.0.0.1:8085/` if you used port 8085
- confirm deployment

Open browser:

```text
http://127.0.0.1:8085/
```

Expected:

```text
Version 1 - Working
```

## 8. Rollback test

Create another ZIP with changed content, for example:

```html
<h1>Broken Version</h1>
```

Deploy it to the same site, but set health URL to:

```text
http://127.0.0.1:8085/notfound
```

Expected:

- deployment fails
- rollback runs
- browser still shows `Version 1 - Working`
- `/logs DemoSite` shows rollback information

## 9. First-deployment failure test

Deploy the same ZIP to a new site:

- site: `DemoSite2`
- app pool: `DemoPool2`
- port: `8086`
- health URL: `http://127.0.0.1:8086/notfound`

Expected:

- deployment fails
- no previous release exists
- destructive rollback is skipped
- failed release is preserved under `storage\failed\DemoSite2`

## 10. Useful commands

```text
/siteinfo DemoSite
/releases DemoSite
/logs DemoSite
/download_logs DemoSite
/rollback DemoSite DemoPool
/delete_release DemoSite <release_id>
```

## 11. Service mode after manual test works

Install NSSM and put `nssm.exe` in PATH.

Then run from project root as Administrator:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_agent_service.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\install_bot_service.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start_services.ps1
```

Manual run scripts are recommended for the first validation because you can see errors directly.

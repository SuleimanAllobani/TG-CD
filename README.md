# Telegram Release Manager for Windows IIS

*Telegram-controlled deployment and release automation for IIS, with automatic rollback.*

Deploy web applications to IIS from a Telegram chat. Send a ZIP, point to a folder, or give a GitHub repository; the system creates a versioned release, switches IIS to it, checks that the site is healthy, and **rolls back automatically** if it is not.

It is built for small teams that run their own Windows servers and want safe, repeatable releases without setting up a full CI/CD platform. It focuses on the delivery side (release, switch, verify, roll back); it does not run test suites or start deployments automatically on every commit.

**Contents**
[1. About the project](#1-about-the-project) ·
[2. Setup guide](#2-setup-guide) ·
[3. Using it](#3-using-it) ·
[4. Troubleshooting](#4-troubleshooting) ·
[5. Security](#5-security) ·
[6. Status and limitations](#6-status-and-limitations) ·
[7. Documentation and layout](#7-documentation-and-layout)

---

## 1. About the project

### The problem it solves

Releasing to IIS by hand means copying files, editing the site, recycling the pool, and hoping nothing breaks. When it does break, rolling back is manual and stressful. This tool turns a release into one chat conversation and makes failure safe.

### What it does

| | |
|---|---|
| **Three sources** | Upload a ZIP in Telegram, use a folder on the server, or deploy a **GitHub repository** (branch, tag or commit). |
| **Versioned releases** | Every deployment becomes a new release folder. Old releases are kept; the live site switches to the new one. |
| **IIS management** | Creates or updates the site and application pool, sets the physical path, recycles the pool. |
| **Health check and automatic rollback** | After switching, the site URL is checked. If it fails, the previous working release is restored and the failed one is kept for inspection. |
| **Build step** | For GitHub sources: `npm build` for static sites and `dotnet publish` for ASP.NET Core. Builds run in a temporary folder *before* a release exists, so a failed build never touches the live site. |
| **Which commit is live** | Each GitHub deployment records the repository, requested ref and exact commit with the release. |
| **Private repositories** | Supported with a read-only token stored only on the IIS server. Telegram and the backend carry only a credential *name*. |
| **Roles and teams** | Per server: viewer, deployer and owner roles, teams, and limits on which repositories and credentials a developer may use. |
| **History, logs, reports** | Browse releases and jobs, read logs, and get a plain-language explanation of why a deployment failed (rule-based, works offline). |
| **Easy server setup** | "Setup Agent" in the bot generates an installer package for each IIS server. |

### How it works

```
 Developer ──► Telegram bot ──► Backend (job queue) ◄── polls ── Agent on the IIS server
                                                                      │
        ZIP / folder / GitHub ──► fetch (+ optional build) ──► new release
                                                                      │
                                         IIS switch ──► health check ──► success
                                                                      └──► failure: automatic rollback
```

- **Bot** (`bot/`): the chat interface and access control.
- **Backend** (`backend/`): a small Flask + SQLite service that queues jobs and stores results.
- **Agent** (`agent/`): runs on each IIS server, connects *outbound* to the backend, and does the real work: fetching, building, releasing, IIS changes, health check, rollback.
- All three sources go through the same pipeline, so versioning, IIS handling, health checks and rollback behave identically.

### A typical deployment

1. In Telegram: **Deploy**, choose the server, then the project type (Static or .NET).
2. Choose **GitHub repository**, send the URL, then a branch, tag or commit (or the default branch).
3. Optionally choose a sub-folder and a build (npm build or .NET publish).
4. Enter the site name, port, application pool and health-check URL, and confirm.
5. The bot reports the result, for example `Source: owner/repo@v1.4.0 (abc1234)`, or explains what went wrong. If the health check fails, the previous release is live again.

---

## 2. Setup guide

### Requirements

| Where | What |
|---|---|
| Backend and bot machine | Windows (or any OS that runs Python), Python 3.12+, a Telegram bot token from @BotFather. It can be the IIS server itself. |
| Each IIS server | Windows with IIS and the IIS management tools; Administrator rights for the installer. |
| GitHub deployments | Git for Windows on the IIS server. |
| Builds (optional) | Node.js LTS for npm builds; the .NET SDK and the ASP.NET Core Hosting Bundle for .NET. |
| Network | A private path from each IIS server to the backend on port 9000 (Tailscale, LAN or VPN). |

### How the parts connect

```
 Telegram (cloud) ◄──────► Bot ───► Backend :9000 ◄─── Agent (on the IIS server)
                           └── same machine ──┘                   ▲
                                                 private network (Tailscale / LAN / VPN)
```

- The **bot** talks to Telegram over the internet (outbound only) and to the backend.
- The **backend** listens on port **9000** (all interfaces by default).
- The **agent** connects **outbound** to the backend address written into its installer package (`backend.public_url`), polls for jobs, downloads ZIP packages, deploys locally and reports back. Nothing connects *into* the IIS server.

**Where Tailscale fits.** Tailscale is not built into the code. It is the recommended way to give the agent a private, encrypted path to the backend without exposing port 9000 to the internet: every machine joins the same Tailscale network and gets a stable address (`100.x.y.z`), which you use as `backend.public_url`. A normal LAN address works the same way when the machines share a network. Alternatives are a VPN, or hosting the backend behind HTTPS.

### Step 1: backend and bot machine

1. Install Python 3.12+. In Telegram talk to @BotFather, create a bot and copy its token.
2. Copy `config/settings.example.json` to `config/settings.json` and fill it in:

   | Key | Meaning |
   |---|---|
   | `telegram_bot_token` | Token from @BotFather |
   | `backend.enabled` | `true` |
   | `backend.base_url` | How the bot reaches the backend, normally `http://127.0.0.1:9000` when both run on one machine |
   | `backend.bot_token` | Shared secret between bot and backend. **Change it** to a long random value |
   | `backend.public_url` | The address the *agent* will use: the backend machine's Tailscale or LAN address, for example `http://100.x.y.z:9000` |

3. Install the dependencies and start the services (Administrator rights are needed for the installers):
   ```
   py -3.12 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   scripts\install_backend_service.bat
   scripts\install_bot_service.bat
   ```
   To try things first without services, run `scripts\run_backend.bat` and `.venv\Scripts\python.exe -m bot.main` in two windows. The backend installer adds a Windows Firewall rule for port 9000; do not forward that port on your router.
4. Open Telegram, find your bot and send `/start`. The menu appears.

### Step 2: private network (Tailscale)

1. Install Tailscale on the backend machine and on every IIS server; sign in to the **same** Tailscale account on all of them.
2. On the backend machine run `tailscale ip -4` and put that address into `backend.public_url`. Restart the bot after editing the file.
3. From an IIS server check the path: `curl http://100.x.y.z:9000/api/health` should return `{"ok": true, ...}`.

### Step 3: install an agent on each IIS server

1. In Telegram choose **Setup Agent** and give the server a name. The bot sends an installer ZIP containing that agent's own token and the backend address.
2. On the IIS server extract the ZIP (for example to `C:\CICD_AGENT`) and run `install_agent_service.bat` **as Administrator**. It creates the Python environment, installs the agent as a Windows service and starts it.
3. In Telegram choose **Status**. When the agent shows as online, it is connected.
4. Install Git for Windows on that server for GitHub deployments, and Node.js and/or the .NET SDK for builds. Restart the agent service afterwards so it sees the new tools.

### Step 4: first deployment

Choose **Deploy**, select the agent, then the project type and source. A good first test is a small ZIP with an `index.html`: site name `DemoSite`, port `8085`, health URL `http://localhost:8085/index.html` (use the server's own address). Open the site in a browser afterwards.

---

## 3. Using it

### Main menu and commands

| Action | Button / command |
|---|---|
| Start a guided deployment | **Deploy** or `/deploy` |
| Agent online status | **Status** or `/status` |
| Your servers, select one | **Agents**, `/myagents`, `/use_agent <id>` |
| Releases and recent jobs | **Releases** or `/releases` |
| Roll back a site to its previous good release | `/rollback <site>` |
| Job logs | **Logs** or `/logs` |
| Job history | **History** or `/history` |
| Explain a failed deployment | **Analyze** or `/analyze_job` |
| Deployment report | **Report** or `/deployment_report` |
| Create an installer for a new IIS server | **Setup Agent** or `/setup_agent` |
| Your Telegram ID (needed to add people) | `/myid` |

### Deploying from GitHub

In **Deploy**, choose **GitHub repository** and answer the questions: repository URL, public or private, branch/tag/commit, optional sub-folder, and an optional build.

- **Public repositories** need nothing else.
- **Private repositories:** create a fine-grained GitHub token with *Contents: Read-only* for just the needed repositories, then on the IIS server run (as Administrator, in the agent folder):
  ```
  .venv\Scripts\python.exe -m agent.set_git_token --alias default --verify owner/private-repo
  ```
  In Telegram choose Private and send the credential name (`default`). The token itself is never sent through Telegram.
- **Builds** run only for repositories listed in `git.allowed_repos` in the agent's `config\settings.json`, for example `"git": {"allowed_repos": ["your-org/*"]}`. Restart the agent after changing it.

Details and all settings: [docs/GITHUB_SOURCE.md](docs/GITHUB_SOURCE.md).

### Teams and permissions

The person who creates an agent is its **owner**. Owners add people with `/add_user <telegram_id> [viewer|deployer|owner]`, group them with `/team_create` and `/team_add`, and limit GitHub access with `/allow_repos` and `/allow_creds`. Viewers can look; deployers can also deploy and roll back; owners manage everything. Full list: [docs/ACCESS_ROLES.md](docs/ACCESS_ROLES.md).

### What the results mean

| Outcome | Live site |
|---|---|
| Success | The new release is live. |
| Failed before a release was created (bad URL, branch, token, or build) | Untouched; the previous release keeps running. |
| Failed after switching (health check) | Previous release restored automatically; the failed one is kept for inspection. |
| Failed on the very first release | Nothing to go back to, so nothing is removed; the failed release is kept. |

---

## 4. Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Bot does not answer | Wrong `telegram_bot_token`, or the bot process or service is not running. Check the bot window or service. |
| `curl .../api/health` fails from the IIS server | Tailscale not signed in on one side, wrong `backend.public_url`, backend not running, or the firewall blocks port 9000. |
| Agent never shows online | The agent installer has an old backend address (regenerate it with **Setup Agent** after changing `public_url`), the service is stopped (service name `TelegramCICDAgent_<agent id>`), or the server cannot reach the backend. |
| "Invalid source_type" | The agent is older than the GitHub update; reinstall it or copy the new `agent` and `common` folders. |
| GitHub: "Git is not installed" | Install Git for Windows on the IIS server and restart the agent service. |
| GitHub: "cannot access the repository" | Wrong URL, or a private repository chosen as Public, or the token lacks access. |
| Build refused | The repository is not listed in `git.allowed_repos`; or Node.js / .NET SDK is missing on the server. |
| .NET site shows 500.19 or 502.5 | Install or repair the ASP.NET Core Hosting Bundle, then run `iisreset`. |
| A deployment failed and you do not know why | Use **Analyze** on that job. It explains the stage and suggests the next step. |

---

## 5. Security

- With the backend enabled (the recommended setup) the agent connects outward, so the IIS server needs no open inbound port.
- GitHub tokens live only on the IIS server (in a protected folder or an environment variable). They are passed to Git through the process environment, never on a command line, and are removed from every log and message. The bot deletes and ignores messages that look like tokens.
- GitHub input is strictly validated: only `https://github.com/owner/repo`, no credentials in URLs, no shell.
- Builds execute repository code, so they are **off unless the repository is listed** in `git.allowed_repos`; they use fixed presets, a minimal environment and a time limit.
- Access is controlled per server with roles, teams and repository limits, checked again at the final confirm.
- Never commit `config/settings.json`, `data/`, `logs/`, `dist/` or `storage/`; they contain tokens (the included `.gitignore` excludes them). Change all default tokens before real use.

## 6. Status and limitations

- 167 automated tests cover the deployment pipeline (with a simulated IIS), release records, rollback, GitHub fetching, real npm and .NET builds, the Telegram conversation and the roles:
  ```
  .venv\Scripts\python.exe -m unittest discover -s tests -t .
  ```
- Test on your own servers before relying on it for production: IIS behaviour, Windows service accounts and your network are specific to your environment.
- GitHub source: github.com over HTTPS only; submodules and Git LFS files are not downloaded; no webhooks (deployments start when someone asks in Telegram).
- Builds: npm projects (no yarn/pnpm-only projects) and .NET (Core) projects; not classic .NET Framework.
- Roles are per server, not per IIS site. Use separate agents for strictly separate environments.

## 7. Documentation and layout

| Document | Contents |
|---|---|
| [docs/GITHUB_SOURCE.md](docs/GITHUB_SOURCE.md) | GitHub deployments, private repositories, builds, settings |
| [docs/ACCESS_ROLES.md](docs/ACCESS_ROLES.md) | Roles, teams and limits, with all owner commands |
| [docs/](docs/) | Security notes and test guides from development |

```
bot/        Telegram bot, access control, message rendering
backend/    Flask + SQLite job queue and API
agent/      Deployment engine for the IIS server (releases, IIS, health check, rollback, GitHub, builds)
common/     Shared models, validation and configuration
ai/         Rule-based failure analyzer
scripts/    Windows install and start scripts
tests/      Automated tests
docs/       Documentation
```

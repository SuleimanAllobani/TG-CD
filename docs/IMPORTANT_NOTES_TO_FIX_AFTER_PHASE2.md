# Important Notes to Fix After Phase 2

## Service installation UX
The current test proved Windows services work, but manual NSSM setup was inconvenient. The next installer should:

- bundle or locate NSSM automatically;
- install bot service with the correct Python path and working directory;
- install agent service with the correct Python path and working directory;
- configure stdout/stderr logs;
- configure auto-restart;
- open required firewall ports;
- test the agent locally using the generated token;
- show a clean success/failure message.

## Telegram UI
Add a persistent bottom menu so users do not need to remember commands:

- 🚀 Deploy
- 🖥 My Agents
- 📊 Status
- 📦 Releases
- 🧾 Logs
- ➕ Setup Agent
- 👥 Agent Users
- ⚙️ Help

## Service reliability
Configure NSSM with:

```cmd
nssm set TelegramCICDBot AppExit Default Restart
nssm set TelegramCICDBot AppRestartDelay 5000
```

Do the same for each agent service.

## Phase 2 network limitation
Phase 2 requires the bot laptop to reach the agent laptop over the network. Phase 2.5 should remove this requirement by adding a backend and outbound agent polling.

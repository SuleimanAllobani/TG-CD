# Testing Roadmap

## Stage 1: One laptop
- Bot and agent run on same Windows machine.
- Agent URL: `http://127.0.0.1:8765`.
- Prove: guided bot flow, ZIP upload, IIS deploy, health check, rollback, first-failure preservation.

## Stage 2: Two laptops
- Laptop A runs bot.
- Laptop B runs agent and IIS.
- Register agent URL using Laptop B IP, e.g. `http://192.168.1.50:8765`.
- Open firewall port 8765 on Laptop B.
- Prove: bot can upload ZIP to remote agent and deploy on remote IIS.

## Stage 3: Five laptops
- One laptop/server runs bot.
- Five laptops run agents.
- Register each as `agent1`...`agent5`.
- Prove: one user can select different agents and deploy to different machines.
- Also test multi-user access by registering the same agent for another Telegram user if needed.

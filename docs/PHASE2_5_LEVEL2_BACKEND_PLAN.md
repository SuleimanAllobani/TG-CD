# Phase 2.5 — Level 2 Central Backend Prototype Plan

## Goal
Move from direct bot-to-agent communication to a product-style backend flow:

```text
Telegram Bot → Central Backend → Job Queue ← Agent polling outward → IIS
```

## Included in this prep package
- `backend/main.py`
- Flask backend with health endpoint
- package upload endpoint
- job creation endpoint
- agent heartbeat endpoint
- agent next-job endpoint
- package download endpoint
- job result endpoint
- SQLite database service
- package storage service
- job service
- preliminary `agent/outbound_main.py`
- placeholder `ai/ai_service.py`

## What is not complete yet
The outbound agent does not yet execute real deployment jobs. The next build step is to map backend job records into the existing `DeploymentService` and report the real deployment result back to the backend.

## Quick scaffold test
From project root:

```cmd
.venv\Scripts\activate.bat
python -u -m backend.main
```

Then open another terminal:

```cmd
curl http://127.0.0.1:9000/api/health
```

Expected:

```json
{"ok":true,"service":"phase2.5-backend"}
```

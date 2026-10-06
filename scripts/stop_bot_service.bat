@echo off
powershell -ExecutionPolicy Bypass -Command "Stop-Service TelegramCICDBot; Get-Service TelegramCICDBot"
pause

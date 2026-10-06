@echo off
powershell -ExecutionPolicy Bypass -Command "Start-Service TelegramCICDBot; Get-Service TelegramCICDBot"
pause

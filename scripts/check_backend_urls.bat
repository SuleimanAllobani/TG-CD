@echo off
cd /d "%~dp0.."
echo Local backend health on old laptop:
curl -i http://127.0.0.1:9000/api/health
echo.
echo Tailscale backend health. Replace OLD-LAPTOP-TAILSCALE-IP with the old laptop Tailscale IP, for example 100.x.y.z:
echo curl -i "http://OLD-LAPTOP-TAILSCALE-IP:9000/api/health"
echo.
echo To find this laptop's Tailscale IP, run:
echo tailscale ip -4
echo.
pause

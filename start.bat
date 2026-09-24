@echo off
cd /d D:\MassGen
start "" /min cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8765/"
"C:\Users\iiiis\.local\bin\uv.exe" run uvicorn api.main:app --host 0.0.0.0 --port 8765 --reload
pause

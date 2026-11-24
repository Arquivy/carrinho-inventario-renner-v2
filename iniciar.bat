@echo off
title SMARTX - Carrinho de Inventario Renner
cd /d "C:\Users\ivan_\Documents\carrinho inventario Renner"
echo ========================================================
echo   SMARTX - CARRINHO DE INVENTARIO RENNER
echo   Iniciando servidor FastAPI e abrindo navegador...
echo   Rotas disponiveis:
echo     - Home: http://localhost:8000/
echo     - Carrinho RFID: http://localhost:8000/carrinho
echo     - Cadastro de Leitores: http://localhost:8000/settings/devices
echo ========================================================
"C:\Users\ivan_\AppData\Local\pypoetry\Cache\virtualenvs\x-bridge-glq7imOo-py3.11\Scripts\python.exe" main.py
pause

@echo off
set "PYW=C:\Users\tmmag\AppData\Local\Programs\Python\Python314\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0opmize.pyw"

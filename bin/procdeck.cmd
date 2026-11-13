@echo off
rem PowerShell / cmd shim for the procdeck CLI. Uses %PROCDECK_PYTHON% if set, else the "procdeck" conda env in
rem the usual Miniconda / Anaconda install locations.
setlocal
set "PY=%PROCDECK_PYTHON%"
if defined PY goto run
for %%R in ("%LOCALAPPDATA%\miniconda3" "%USERPROFILE%\miniconda3" "%USERPROFILE%\anaconda3" "%LOCALAPPDATA%\anaconda3" "%PROGRAMDATA%\miniconda3" "%PROGRAMDATA%\anaconda3") do (
  if not defined PY if exist "%%~R\envs\procdeck\python.exe" set "PY=%%~R\envs\procdeck\python.exe"
)
if not defined PY (
  echo procdeck: conda env 'procdeck' not found; run "conda env create -f environment.yml" or set PROCDECK_PYTHON 1>&2
  exit /b 1
)
:run
"%PY%" -m procdeck %*

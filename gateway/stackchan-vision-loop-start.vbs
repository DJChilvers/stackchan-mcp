' Launch the local face-aware ambient vision loop hidden (no console window).
' pythonw.exe = no console; the loop self-throttles (busy/voice/pause markers)
' and no-ops when the device is offline. NOT auto-started at login — launch
' this deliberately, same opt-in convention as stackchan-idle-start.vbs.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\domin\tools\stackchan-mcp\gateway\.venv\Scripts\pythonw.exe"" ""C:\Users\domin\tools\stackchan-mcp\gateway\stackchan-vision-loop.py""", 0, False

' Launch the Wheatley ambient idle-fidget loop hidden (no console window).
' pythonw.exe = no console; the loop self-throttles and no-ops when the
' device is offline or Claude is active, so it is safe to run continuously.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\domin\tools\stackchan-mcp\gateway\.venv\Scripts\pythonw.exe"" ""C:\Users\domin\tools\stackchan-mcp\gateway\stackchan-idle.py""", 0, False

' Launch the LED status-chase loop hidden (no console window). Does nothing
' while idle — only animates while a busy/thinking marker file exists, and
' leaves stackchan-hook.py's static idle/urgent colors alone otherwise.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\domin\tools\stackchan-mcp\gateway\.venv\Scripts\pythonw.exe"" ""C:\Users\domin\tools\stackchan-mcp\gateway\stackchan-led-chase.py""", 0, False

' Launch all four StackChan background loops hidden, by delegating to each
' loop's own *-start.vbs (single source of truth for per-loop launch config).
' Run at login via the "StackChan Loops" scheduled task (90s delay so the
' gateway daemon task, 60s delay, comes up first). Each loop self-throttles
' and no-ops when the device/gateway is offline, so launch order is a
' nicety, not a requirement. Safe to re-run by hand to restore dead loops:
' idle/vision/led-chase hold single-instance file locks, and the voice
' bridge fails its port-8768 bind if already running, so duplicates exit.
Set WshShell = CreateObject("WScript.Shell")
Dim base
base = "C:\Users\domin\tools\stackchan-mcp\gateway\"
WshShell.Run "wscript.exe """ & base & "stackchan-idle-start.vbs""", 0, False
WshShell.Run "wscript.exe """ & base & "stackchan-voice-bridge-start.vbs""", 0, False
WshShell.Run "wscript.exe """ & base & "stackchan-vision-loop-start.vbs""", 0, False
WshShell.Run "wscript.exe """ & base & "stackchan-led-chase-start.vbs""", 0, False

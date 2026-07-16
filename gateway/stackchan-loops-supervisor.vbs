' StackChan loop supervisor — run every ~10 min by the "StackChan Loop Supervisor"
' scheduled task. Re-launches the loop set: each loop holds a single-instance file
' lock, so live loops no-op and only DEAD ones actually (re)start. Also nudges the
' gateway task (its daemon watchdog exits quietly if already listening on 8767).
'
' Honors a PAUSE marker so a deliberate stop during a crash hunt isn't fought:
'     Pause :  create  %TEMP%\stackchan-loops-supervisor-paused   (then stop a loop)
'     Resume:  delete that file
'
' Runs hidden (window style 0). For a status REPORT + heal on demand, use the
' PowerShell tool instead: stackchan-loops-check.ps1
Option Explicit
Dim fso, sh, base, pauseMarker
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

pauseMarker = sh.ExpandEnvironmentStrings("%TEMP%") & "\stackchan-loops-supervisor-paused"
If fso.FileExists(pauseMarker) Then WScript.Quit 0   ' paused — do nothing this cycle

base = "C:\Users\domin\tools\stackchan-mcp\gateway\"
' Revive any dead loops (idempotent via the per-loop file locks).
sh.Run "wscript.exe """ & base & "stackchan-loops-start.vbs""", 0, False
' Nudge the gateway daemon task — no-op if it's already listening on 8767.
sh.Run "schtasks /Run /TN ""StackChan Gateway""", 0, False

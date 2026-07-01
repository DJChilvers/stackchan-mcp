' Launch the touch-to-talk voice bridge hidden (no console window).
' Receives the device's tap-to-talk audio capture, transcribes it locally
' (faster-whisper), asks Claude for a reply, and speaks it back. Safe to
' run continuously — without STACKCHAN_VOICE_ANTHROPIC_API_KEY set it just
' speaks an apology instead of crashing. Logs to %TEMP%\stackchan-voice-bridge.log.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\domin\tools\stackchan-mcp\gateway\.venv\Scripts\pythonw.exe"" ""C:\Users\domin\tools\stackchan-mcp\gateway\stackchan-voice-bridge.py""", 0, False

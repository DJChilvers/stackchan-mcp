# StackChan loop supervisor — report status of the gateway + all background loops
# and (re)start any that aren't running.
#
# Safe to run anytime / repeatedly: every loop holds a single-instance msvcrt file
# lock, so launching one that's already up is a no-op (the 2nd instance exits). This
# only revives DEAD loops. Run by hand, or schedule it (see -Schedule note at bottom).
#
#   Report only (no changes):   powershell -File stackchan-loops-check.ps1 -CheckOnly
#   Report + heal (default):    powershell -File stackchan-loops-check.ps1
#   Pause auto-heal:            create  %TEMP%\stackchan-loops-supervisor-paused
#                               (supervisor then only reports; delete the file to resume)
#
param([switch]$CheckOnly)

$ErrorActionPreference = 'SilentlyContinue'
$base = 'C:\Users\domin\tools\stackchan-mcp\gateway\'

# Expected persistent loops: display name, the script that identifies it, its start-vbs.
$loops = @(
  @{ Name = 'idle';         Match = 'stackchan-idle.py';         Vbs = 'stackchan-idle-start.vbs' },
  @{ Name = 'voice-bridge'; Match = 'stackchan-voice-bridge.py'; Vbs = 'stackchan-voice-bridge-start.vbs' },
  @{ Name = 'vision-loop';  Match = 'stackchan-vision-loop.py';  Vbs = 'stackchan-vision-loop-start.vbs' },
  @{ Name = 'led-chase';    Match = 'stackchan-led-chase.py';    Vbs = 'stackchan-led-chase-start.vbs' }
)

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
$healed = @()

# Pause switch: if this marker exists, only REPORT — never heal. Lets you (or an agent)
# deliberately stop a loop during a crash hunt without the scheduled task reviving it.
$pauseMarker = Join-Path $env:TEMP 'stackchan-loops-supervisor-paused'
$paused = [bool](Test-Path $pauseMarker)
$heal = (-not $CheckOnly) -and (-not $paused)
if ($paused) { Write-Output '(PAUSED — report only; delete the pause marker to resume healing)' }

# --- Gateway: identified by the daemon listening on 8767, healed via its own task ---
$gwUp = [bool](Get-NetTCPConnection -LocalPort 8767 -State Listen)
if ($gwUp) {
  Write-Output ('{0,-14} UP' -f 'GATEWAY')
} else {
  Write-Output ('{0,-14} DOWN' -f 'GATEWAY')
  if ($heal) {
    schtasks /Run /TN "StackChan Gateway" | Out-Null
    $healed += 'GATEWAY'
  }
}

# --- Loops ---
foreach ($l in $loops) {
  $running = @($procs | Where-Object { $_.CommandLine -match [regex]::Escape($l.Match) })
  $n = $running.Count
  if ($n -eq 0) {
    Write-Output ('{0,-14} DOWN' -f $l.Name)
    if ($heal) {
      Start-Process wscript.exe -ArgumentList ('"' + $base + $l.Vbs + '"') -WindowStyle Hidden
      $healed += $l.Name
    }
  } elseif ($n -le 2) {
    Write-Output ('{0,-14} UP (procs={1})' -f $l.Name, $n)   # 2 = trampoline+child = normal
  } else {
    # >2 processes for one script = a genuine duplicate instance. Report, don't auto-kill
    # (killing a user process is a judgment call) — but make it loud.
    Write-Output ('{0,-14} DUPLICATE! ({1} procs, expected 2) PIDs={2}' -f $l.Name, $n, (($running.ProcessId) -join ','))
  }
}

if ($healed.Count -gt 0) {
  Write-Output ''
  Write-Output ('started: ' + ($healed -join ', ') + '  (re-run to confirm they came up)')
}

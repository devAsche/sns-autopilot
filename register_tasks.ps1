# 夜間運用のWindowsタスクを登録する(Museが SSH から1回だけ実行する)
#   powershell -ExecutionPolicy Bypass -File "C:\AI model\ops\register_tasks.ps1"
# 登録するもの:
#   Ops-Wake     毎日 02:55 にPCをスリープ/休止状態から起こすだけのタスク(復帰タイマー)
#   Ops-NightRun 時刻なし。Museが 03:00 に  schtasks /run /tn "Ops-NightRun"  で起動する(画面のあるセッションでChromeを動かすため)
$ErrorActionPreference = "Stop"
$ops = Split-Path -Parent $MyInvocation.MyCommand.Path
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /SETACTIVE SCHEME_CURRENT
# SSH経由だと USERDOMAIN が WORKGROUP になるため、実際のログオン名を使う
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

$wakeSettings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$wakeAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c exit 0"
Register-ScheduledTask -TaskName "Ops-Wake" -Action $wakeAction -Trigger (New-ScheduledTaskTrigger -Daily -At "02:55") `
  -Settings $wakeSettings -Principal $principal -Description "ops: 02:55 にPCを起こす" -Force | Out-Null

$runSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 7) -MultipleInstances IgnoreNew
$runAction = New-ScheduledTaskAction -Execute "$ops\run_night.bat" -WorkingDirectory $ops
Register-ScheduledTask -TaskName "Ops-NightRun" -Action $runAction -Settings $runSettings `
  -Principal $principal -Description "ops: 夜間収集。Museが schtasks /run で起動する" -Force | Out-Null

# Ops-LaunchClaude: 時刻なし。Museが 06:30 に ensure_claude_app.ps1 から必要なときだけ起動する(画面のあるセッションでアプリを開くため)
$launchAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ops\launch_claude.ps1`"" -WorkingDirectory $ops
Register-ScheduledTask -TaskName "Ops-LaunchClaude" -Action $launchAction -Settings $runSettings `
  -Principal $principal -Description "ops: Claudeデスクトップアプリを起動する" -Force | Out-Null
# Ops-EngageRun: 時刻なし。Museが mailbox 確認時刻(12:00/15:00/18:00/21:00)に schtasks /run で起動する。承認済みのいいね・フォロー・RTを実行
$engageSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
$engageAction = New-ScheduledTaskAction -Execute "$ops\run_engage.bat" -WorkingDirectory $ops
Register-ScheduledTask -TaskName "Ops-EngageRun" -Action $engageAction -Settings $engageSettings `
  -Principal $principal -Description "ops: 承認済みの反応(いいね・フォロー・RT)を実行する" -Force | Out-Null
Write-Output "OK: Ops-Wake (daily 02:55, WakeToRun) / Ops-NightRun (on demand) / Ops-LaunchClaude (on demand) / Ops-EngageRun (on demand)"

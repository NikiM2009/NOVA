param(
    [ValidateSet('central', 'agent')][string]$Mode = 'central',
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$CentralUrl = '',
    [string]$DataDirectory = ''
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path $Root).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (!(Test-Path $Python)) { throw 'Zuerst python install.py --no-start ausführen.' }
if ($Mode -eq 'agent' -and !$CentralUrl.StartsWith('https://')) { throw 'HTTPS-CentralUrl erforderlich.' }
if (!$DataDirectory) { $DataDirectory = Join-Path $Root ".nova\$Mode" }
# Task arguments must not contain embedded quotes.
foreach ($value in @($Root, $DataDirectory, $CentralUrl)) {
    if ($value.Contains('"') -or $value.Contains("`n")) { throw 'Ungültiges Argument.' }
}
$Command = if ($Mode -eq 'agent') { "-m nova agent --url `"$CentralUrl`" --data `"$DataDirectory`"" } else { "-m nova serve --data `"$DataDirectory`"" }
if ($Mode -eq 'agent' -and !(Test-Path (Join-Path $DataDirectory 'connection.json'))) {
    throw 'Agent zuerst manuell mit derselben URL und --data koppeln.'
}
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Command -WorkingDirectory $Root
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Name = "NOVA-$Mode"
# No -Force: do not silently overwrite an existing task.
Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings
Write-Host "Aufgabe $Name angelegt. Start bei nächster Anmeldung oder mit Start-ScheduledTask -TaskName $Name."

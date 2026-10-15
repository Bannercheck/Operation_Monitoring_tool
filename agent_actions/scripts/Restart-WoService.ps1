# Watchover action template win.service.restart: restart one Windows service by name and wait until it runs again.
# Installed under %ProgramData%\Watchover\actions\scripts (Administrators only); its sha256 is pinned in windows-svc.tmpl.json.
param([Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_.-]{1,80}$')][string]$Name)
$ErrorActionPreference = 'Stop'
$svc = Get-Service -Name $Name
Restart-Service -InputObject $svc -Force
$svc.WaitForStatus('Running', [TimeSpan]::FromSeconds(120))
"name=$Name state=$((Get-Service -Name $Name).Status)"

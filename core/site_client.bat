<# :
@echo off
rem Docket employee sync for __SITE__
rem Double-click to set up. Asks for the Mantra details, tests them, and schedules the push.
rem Needs nothing installed: uses only what is part of Windows (Server 2012 or later).
rem Contains this site's key: delete the downloaded copy once setup is done.
setlocal
set "DOCKET_BAT=%~f0"
set "DOCKET_MODE=%~1"
if "%DOCKET_MODE%"=="" set "DOCKET_MODE=setup"
if not "%DOCKET_MODE%"=="setup" goto start
fltmc >nul 2>&1
if not errorlevel 1 goto start
echo Asking for administrator rights...
powershell -NoProfile -Command "Start-Process -FilePath $env:DOCKET_BAT -Verb RunAs"
exit /b
:start
powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create([IO.File]::ReadAllText($env:DOCKET_BAT))) $env:DOCKET_MODE"
set "RC=%errorlevel%"
if "%DOCKET_MODE%"=="setup" pause
exit /b %RC%
#>
param([string]$Mode = 'setup')   # setup = ask and install, run = push now (the scheduled task), lib = functions only (tests)
$ErrorActionPreference = 'Stop'

# ---- filled in by Docket ----
$SiteName = '__SITE__'
$TaskName = '__TASK__'
$BaseUrl  = '__BASE_URL__'
$ApiPath  = '__API_PATH__'
$Key      = '__KEY__'
# Only these columns ever leave this server.
$Columns  = '__COLUMNS__' -split ','
$StaffView     = '__STAFF_VIEW__'
$AssociateView = '__ASSOCIATE_VIEW__'
# -----------------------------

$Dir     = Join-Path $env:ProgramFiles $TaskName   # not ProgramData: ordinary users can create folders there
$CfgFile = Join-Path $Dir 'config.json'
$LogFile = Join-Path $Dir 'sync.log'
Add-Type -AssemblyName System.Data, System.Security, System.Web.Extensions
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Log([string]$Text) {
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Text
    Write-Host $line
    if (Test-Path $Dir) {
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) { Move-Item $LogFile "$LogFile.old" -Force }
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    }
}

function Protect-Text([string]$Text) {   # readable only on this machine
    $bytes = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($Text), $null, 'LocalMachine')
    [Convert]::ToBase64String($bytes)
}

function Unprotect-Text([string]$Text) {
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($Text), $null, 'LocalMachine')
    [Text.Encoding]::UTF8.GetString($bytes)
}

function Get-ConnectionString($Cfg) {
    $b = New-Object Data.SqlClient.SqlConnectionStringBuilder   # quotes every value correctly
    $b['Data Source'] = if ($Cfg.Port) { "$($Cfg.Server),$($Cfg.Port)" } else { "$($Cfg.Server)" }
    $b['Initial Catalog'] = [string]$Cfg.Database
    $b['Connect Timeout'] = 15
    if ($Cfg.WindowsLogin) { $b['Integrated Security'] = $true }
    else { $b['User ID'] = [string]$Cfg.User; $b['Password'] = Unprotect-Text $Cfg.Password }
    $b.ConnectionString
}

function Open-Mantra($Cfg) {
    $conn = New-Object Data.SqlClient.SqlConnection (Get-ConnectionString $Cfg)
    $conn.Open()
    $conn
}

function Invoke-Query($Conn, [string]$Sql) {
    $cmd = $Conn.CreateCommand()
    $cmd.CommandText = $Sql
    $cmd.CommandTimeout = 300
    $table = New-Object Data.DataTable
    $table.Load($cmd.ExecuteReader())
    , $table
}

function ConvertTo-Text($Value) {
    if ($null -eq $Value -or $Value -is [DBNull]) { return '' }
    [Convert]::ToString($Value, [Globalization.CultureInfo]::InvariantCulture).Trim()
}

function Get-Payload($Cfg) {
    try { $conn = Open-Mantra $Cfg } catch { throw "Could not connect to Mantra: $($_.Exception.Message)" }
    try {
        # Plain .NET types only: the JSON serializer below cannot handle PowerShell-wrapped objects.
        $feeds = New-Object 'Collections.Generic.List[Collections.Generic.Dictionary[string,object]]'
        foreach ($view in $Cfg.Views) {
            $table = Invoke-Query $conn "SELECT * FROM dbo.[$view]"
            $keep = @($table.Columns | Where-Object { $Columns -contains ($_.ColumnName -replace '[\s_.]+', '').ToUpperInvariant() })
            $rows = New-Object 'Collections.Generic.List[string[]]'
            foreach ($r in $table.Rows) {
                $cells = New-Object string[] $keep.Count
                for ($i = 0; $i -lt $keep.Count; $i++) { $cells[$i] = ConvertTo-Text $r[$keep[$i]] }
                $rows.Add($cells)
            }
            $feed = New-Object 'Collections.Generic.Dictionary[string,object]'
            $feed['view'] = [string]$view
            $feed['header'] = [string[]]@($keep | ForEach-Object { $_.ColumnName.Trim() })
            $feed['rows'] = $rows.ToArray()
            $feeds.Add($feed)
            Write-Log "Read $($rows.Count) rows from $view"
        }
        $companies = New-Object 'Collections.Generic.Dictionary[string,string]'
        try {   # optional: not every Mantra has company names
            $table = Invoke-Query $conn 'SELECT DISTINCT [Employee ID], [Company Name] FROM dbo.view_Transaction_Report WHERE [Company Name] IS NOT NULL'
            foreach ($r in $table.Rows) {
                $id = ConvertTo-Text $r[0]; $name = ConvertTo-Text $r[1]
                if ($id -and $name) { $companies[$id] = $name }
            }
        } catch { Write-Log "Company names not available: $($_.Exception.Message)" }
        $payload = New-Object 'Collections.Generic.Dictionary[string,object]'
        $payload['feeds'] = $feeds.ToArray()
        $payload['companies'] = $companies.psobject.BaseObject
        $json = New-Object Web.Script.Serialization.JavaScriptSerializer
        $json.MaxJsonLength = [int]::MaxValue
        $json.Serialize($payload.psobject.BaseObject)
    } finally { if ($conn) { $conn.Dispose() } }
}

function Send-Payload($Cfg, [string]$Json) {
    $body = [Text.Encoding]::UTF8.GetBytes($Json)
    $resp = $null
    try {
        $req = [Net.WebRequest]::Create($Cfg.BaseUrl.TrimEnd('/') + $ApiPath)
        $req.Method = 'POST'
        $req.ContentType = 'application/json'
        $req.Headers['Authorization'] = 'Bearer ' + $Cfg.Key
        $req.AllowAutoRedirect = $false   # a redirect would drop the POST
        $req.Proxy = $null                # Docket is internal
        $req.Timeout = 300000
        $req.ReadWriteTimeout = 300000
        $stream = $req.GetRequestStream()
        $stream.Write($body, 0, $body.Length)
        $stream.Dispose()
        $resp = $req.GetResponse()
    } catch {
        $ex = $_.Exception
        while ($ex.InnerException -and $ex -isnot [Net.WebException]) { $ex = $ex.InnerException }
        if ($ex -isnot [Net.WebException] -or -not $ex.Response) { throw "Could not reach Docket: $($ex.Message)" }
        $resp = $ex.Response
    }
    try {
        $text = (New-Object IO.StreamReader $resp.GetResponseStream()).ReadToEnd()
        if ([int]$resp.StatusCode -ne 200) {
            throw "Docket answered $([int]$resp.StatusCode): $($text.Substring(0, [Math]::Min(300, $text.Length)))"
        }
    } finally { $resp.Dispose() }
    $text | ConvertFrom-Json
}

function Push-Employees($Cfg) {
    $r = Send-Payload $Cfg (Get-Payload $Cfg)
    Write-Log "Pushed $($r.received): $($r.inserted) added, $($r.updated) updated, $($r.kept_manual + $r.kept_staff) preserved, $($r.other_site) of other sites skipped"
    $r
}

function ConvertTo-Times([string]$Text) {
    $times = @($Text -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (-not $times) { throw 'Enter at least one time.' }
    foreach ($t in $times) {
        if ($t -notmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw "'$t' is not a time. Use HH:MM, e.g. 02:00 or 06:00,18:00" }
    }
    $times | Select-Object -Unique
}

function Read-Answer([string]$Question, $Default, [switch]$Optional) {
    while ($true) {
        $suffix = if ("$Default") { " [$Default]" } else { '' }
        $a = (Read-Host "$Question$suffix").Trim()
        if (-not $a) { $a = "$Default" }
        if ($a -or $Optional) { return $a }
    }
}

function Read-YesNo([string]$Question, [bool]$Default) {
    (Read-Answer "$Question (y/n)" $(if ($Default) { 'y' } else { 'n' })) -match '^[yY]'
}

function Read-Secret([string]$Question, [string]$OldProtected) {
    while ($true) {
        $s = Read-Host $(if ($OldProtected) { "$Question [Enter = keep current]" } else { $Question }) -AsSecureString
        if ($s.Length) { return Protect-Text (New-Object Net.NetworkCredential '', $s).Password }
        if ($OldProtected) { return $OldProtected }
    }
}

function Read-Config($Old) {
    $c = @{ Key = $Key }
    $c.BaseUrl = Read-Answer 'Docket address' $(if ($Old.BaseUrl) { $Old.BaseUrl } else { $BaseUrl })
    if ($c.BaseUrl -notmatch '^https?://') { $c.BaseUrl = 'http://' + $c.BaseUrl }
    $c.Server = Read-Answer 'Mantra SQL Server (IP, name or name\instance)' $Old.Server
    $isInstance = $c.Server.Contains('\')
    $port = if ($Old.Server -eq $c.Server) { $Old.Port } elseif ($isInstance) { '' } else { '1433' }
    $c.Port = Read-Answer 'Port (empty for a named instance)' $port -Optional
    $c.Database = Read-Answer 'Mantra database name' $Old.Database
    $c.WindowsLogin = Read-YesNo "Use Windows login? (the scheduled push runs as this computer's SYSTEM account)" ([bool]$Old.WindowsLogin)
    if (-not $c.WindowsLogin) {
        $c.User = Read-Answer 'SQL user' $Old.User
        $c.Password = Read-Secret 'SQL password' $Old.Password
    }
    $c.Views = @($StaffView)
    if (Read-YesNo 'Send associates (contractors) as well?' ($Old.Views -contains $AssociateView)) { $c.Views += $AssociateView }
    while ($true) {
        try { $c.Times = @(ConvertTo-Times (Read-Answer 'Push times, daily, 24h, comma separated' $(if ($Old.Times) { $Old.Times -join ',' } else { '02:00' }))); break }
        catch { Write-Host $_.Exception.Message -ForegroundColor Yellow }
    }
    $c
}

function Install-Sync($Cfg) {
    New-Item -ItemType Directory -Force $Dir | Out-Null
    # SYSTEM and Administrators only: the folder holds the key and the Mantra login.
    & icacls.exe $Dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE) { throw "Could not protect $Dir" }
    ConvertTo-Json $Cfg | Set-Content $CfgFile -Encoding UTF8
    $bat = Join-Path $Dir 'docket-sync.bat'
    $source = [IO.File]::ReadAllText($env:DOCKET_BAT)
    [IO.File]::WriteAllText($bat, $source, [Text.Encoding]::ASCII)

    $inv = [Globalization.CultureInfo]::InvariantCulture
    $triggers = @($Cfg.Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($_, 'HH:mm', $inv)) })
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument "/c `"`"$bat`" run`""
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
        -Description "Pushes the Mantra employee master of $SiteName to Docket" -Force -ErrorAction Stop | Out-Null
}

if ($Mode -eq 'lib') { return }

if ($Mode -eq 'run') {
    for ($try = 1; $try -le 3; $try++) {
        try {
            Push-Employees (Get-Content $CfgFile -Raw -Encoding UTF8 | ConvertFrom-Json) | Out-Null
            exit 0
        } catch {
            Write-Log "Push failed (attempt $try of 3): $($_.Exception.Message)"
            if ($try -lt 3) { Start-Sleep -Seconds 300 }
        }
    }
    exit 1
}

Write-Host "`nDocket employee sync - $SiteName`nPress Enter to accept a value shown in [brackets].`n" -ForegroundColor Cyan
$cfg = @{}
if (Test-Path $CfgFile) { try { $cfg = Get-Content $CfgFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $cfg = @{} } }
while ($true) {
    $cfg = Read-Config $cfg
    Write-Host "`nTesting: reading Mantra and pushing to Docket..."
    try { Push-Employees $cfg | Out-Null; break }
    catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
        if (-not (Read-YesNo "`nCheck the details and try again?" $true)) { Write-Host 'Nothing was installed or changed on this server.'; exit 1 }
    }
}
try { Install-Sync $cfg } catch { Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
Write-Host "`nDone. Pushes daily at $($cfg.Times -join ', ') (Task Scheduler: $TaskName)." -ForegroundColor Green
Write-Host "Log: $LogFile"
Write-Host "Delete this downloaded file now - it contains the key. To change settings later run $Dir\docket-sync.bat as Administrator."
if ($cfg.WindowsLogin) { Write-Host "Windows login: the test ran as you, the schedule runs as SYSTEM. Give this computer's account read access in Mantra, then check the log." -ForegroundColor Yellow }
exit 0

# Run by test_site_sync.py: loads the functions of a downloaded .bat and drives them with a fake Mantra.
param([string]$Bat)
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create([IO.File]::ReadAllText($Bat))) lib
$Dir = 'Z:\nowhere'   # no log file
function Assert($Ok, $What) { if (-not $Ok) { throw "FAILED: $What" } }
function Get-Failure($Block) { try { & $Block | Out-Null; '' } catch { $_.Exception.Message } }

$pw = "p;'`"}=$([char]0xE9) 1"
$cfg = @{ BaseUrl = $BaseUrl; Key = $Key; Server = 'db1'; Port = '1433'; Database = 'Mantra'; User = 'u'
          Password = (Protect-Text $pw); Views = @($StaffView); Times = @('02:00') }
$b = New-Object Data.SqlClient.SqlConnectionStringBuilder (Get-ConnectionString $cfg)
Assert ($b['Password'] -eq $pw -and $b['Data Source'] -eq 'db1,1433') 'connection string'
$w = New-Object Data.SqlClient.SqlConnectionStringBuilder (Get-ConnectionString @{ Server = 'h\i'; Database = 'M'; WindowsLogin = $true })
Assert ($w['Data Source'] -eq 'h\i' -and $w['Integrated Security']) 'windows login'
Assert ($cfg.Password -notmatch 'p;') 'password is encrypted'
$back = ConvertTo-Json $cfg | ConvertFrom-Json   # what Install-Sync stores must come back the same
Assert ((Unprotect-Text $back.Password) -eq $pw -and @($back.Views).Count -eq 1) 'config round trip'

Assert ((Get-Failure { Get-Payload @{ Server = '127.0.0.1'; Port = '1'; Database = 'M'; WindowsLogin = $true; Views = @('v') } }) `
    -match 'Could not connect to Mantra') 'mantra down'

function Open-Mantra($Cfg) { New-Object IO.MemoryStream }
function Invoke-Query($Conn, $Sql) {
    if ($Sql -match 'view_Transaction_Report') { throw 'no such view' }
    Assert ($Sql -eq "SELECT * FROM dbo.[$StaffView]") $Sql
    $t = New-Object Data.DataTable
    $t.Columns.Add('EMPLOYEE  ID', [decimal]) | Out-Null
    'EMPLOYEE NAME', 'Email ID', 'Employee Status', 'AADHAAR' | ForEach-Object { $t.Columns.Add($_) | Out-Null }
    $t.Rows.Add(4050163, "Asha `"O'Neil`" $([char]0x0906)", 'ASHA@x.in', 'Active', 'secret') | Out-Null
    $t.Rows.Add(4050164, 'Ravi', [DBNull]::Value, 'Active', 'secret') | Out-Null
    , $t
}
$json = Get-Payload $cfg
Assert ($json -notmatch 'AADHAAR|secret') 'unknown columns never leave the site'
$one = Push-Employees $back
$two = Push-Employees $back
Assert ($one.inserted -eq 2 -and $two.inserted -eq 0 -and $two.updated -eq 2) "upsert: $one $two"

Assert ((Get-Failure { Send-Payload @{ BaseUrl = $BaseUrl; Key = 'nope' } '{}' }) -match 'answered 401') 'bad key'
Assert ((Get-Failure { Send-Payload @{ BaseUrl = 'http://127.0.0.1:1'; Key = 'x' } '{}' }) -match 'Could not reach') 'docket down'
Assert (((ConvertTo-Times '02:00, 14:30,02:00') -join ',') -eq '02:00,14:30') 'times'
foreach ($bad in '', '25:00', '2') { Assert (Get-Failure { ConvertTo-Times $bad }) "bad time '$bad'" }
'ALL OK'

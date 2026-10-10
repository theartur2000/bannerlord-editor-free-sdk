# Player checks: the things a playtest finds that no unit test looks at, as one unattended run of named game checks with
# PASS / FAIL lines. Each check is one night_test.ps1 run whose module log is searched for a number.
#   powershell -ExecutionPolicy Bypass -File harness\player_checks.ps1 -Module MyMod -ChecksFile harness\checks.example.json [-Only a,b] [-LogFile <module log>]
# Takes and releases the game lock (game_lock.ps1), restores the launcher config after every run, and launches nothing while the
# lock is held by someone else.
# The checks file is a JSON list; every entry:
#   { "name": "melee", "words": "plain fight quit meleeprobe", "timeoutSec": 400, "campaign": false,
#     "pattern": "meleeprobe end: (\\S+) landed (\\d+), damage (\\d+)", "value": "{3}/{2}", "min": 10 }
# pattern is a regex over the run's log lines; value is an arithmetic expression over its capture groups ({1}, {2}, ...: numbers,
# + - * / and parentheses; each group it uses is read as a number with the invariant culture) evaluated for every matching line
# (the last match per first group wins; group 1 is the row's name); min / max are the thresholds. Entries without a pattern only
# need the run to pass (end marker seen, no crash). Results go to <out>\player_checks_<stamp>\summary.txt, where <out> is -OutDir,
# else <SDK home>\out (BANNERLORD_SDK_HOME, else %LOCALAPPDATA%\bannerlord-sdk, else ~/.bannerlord-sdk).
param([Parameter(Mandatory = $true)][string]$Module, [Parameter(Mandatory = $true)][string]$ChecksFile, [string]$Only = "",
      [string]$LogFile = "", [string]$OutDir = "")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sdkHome = if ($env:BANNERLORD_SDK_HOME) { $env:BANNERLORD_SDK_HOME } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "bannerlord-sdk" } else { Join-Path $HOME ".bannerlord-sdk" }   # same root as sdk/config.py
if (-not $OutDir) { $OutDir = Join-Path $sdkHome "out" }
$stamp = Get-Date -Format "MMdd_HHmm"; $out = Join-Path $OutDir "player_checks_$stamp"; New-Item -ItemType Directory -Force $out | Out-Null
$lines = New-Object System.Collections.Generic.List[string]
function Say([string]$s) { $s; $lines.Add($s) }

# Evaluates a check's "value" template over the capture groups of a log line: numbers, {n} (capture group n, parsed as a
# [double] with the invariant culture), + - * / and parentheses. A small recursive-descent parser; nothing is passed to
# Invoke-Expression. A group that is not a number, a missing group or a division by zero throws.
function Get-CheckValue([string]$Template, $Groups) {
    $script:cvTok = @([regex]::Matches($Template, '\{\d+\}|\d+(?:\.\d+)?|[-+*/()]|\S') | ForEach-Object { $_.Value })
    $script:cvPos = 0; $script:cvGroups = $Groups
    $v = Read-CvSum
    if ($script:cvPos -lt $script:cvTok.Count) { throw "unexpected '$($script:cvTok[$script:cvPos])'" }
    return $v
}
function Peek-CvToken { if ($script:cvPos -lt $script:cvTok.Count) { return $script:cvTok[$script:cvPos] } else { return $null } }
function Read-CvSum {
    $v = Read-CvProduct
    while ((Peek-CvToken) -eq "+" -or (Peek-CvToken) -eq "-") {
        $op = Peek-CvToken; $script:cvPos++
        $r = Read-CvProduct
        if ($op -eq "+") { $v = $v + $r } else { $v = $v - $r }
    }
    return $v
}
function Read-CvProduct {
    $v = Read-CvFactor
    while ((Peek-CvToken) -eq "*" -or (Peek-CvToken) -eq "/") {
        $op = Peek-CvToken; $script:cvPos++
        $r = Read-CvFactor
        if ($op -eq "*") { $v = $v * $r } else { if ($r -eq 0) { throw "division by zero" }; $v = $v / $r }
    }
    return $v
}
function Read-CvFactor {
    $t = Peek-CvToken; $script:cvPos++
    if ($null -eq $t) { throw "the value expression ends early" }
    if ($t -eq "-") { return - (Read-CvFactor) }
    if ($t -eq "(") {
        $v = Read-CvSum
        if ((Peek-CvToken) -ne ")") { throw "missing )" }
        $script:cvPos++
        return $v
    }
    $inv = [Globalization.CultureInfo]::InvariantCulture
    if ($t -match '^\{(\d+)\}$') {
        $g = [int]$Matches[1]
        if ($g -lt 1 -or $g -ge $script:cvGroups.Count) { throw "the pattern has no capture group $g" }
        $d = 0.0
        if (-not [double]::TryParse($script:cvGroups[$g].Value, [Globalization.NumberStyles]::Float, $inv, [ref]$d)) { throw "capture group $g ('$($script:cvGroups[$g].Value)') is not a number" }
        return $d
    }
    if ($t -match '^\d') { return [double]::Parse($t, [Globalization.NumberStyles]::Float, $inv) }
    throw "unexpected '$t'"
}
$checks = Get-Content $ChecksFile -Raw | ConvertFrom-Json
$want = if ($Only) { $Only.Split(",") } else { @() }
foreach ($c in $checks) {
    if ($want.Count -gt 0 -and $want -notcontains $c.name) { continue }
    $lock = (powershell -ExecutionPolicy Bypass -File "$here\game_lock.ps1" take player_checks | Out-String)
    if ($lock -notmatch "taken by player_checks") { Say "SKIP $($c.name) (game lock: $($lock.Trim()))"; continue }
    $tag = "pc_$($c.name)_$stamp"
    try {
        $args2 = @("-ExecutionPolicy", "Bypass", "-File", "$here\night_test.ps1", "-Module", $Module, "-Words", $c.words, "-Suite", $c.name, "-Tag", $tag,
                   "-TimeoutSec", [string]$(if ($c.timeoutSec) { $c.timeoutSec } else { 420 }), "-NoRetry", "-OutDir", $out)
        if ($c.campaign) { $args2 += "-Campaign" }
        if ($LogFile) { $args2 += @("-LogFile", $LogFile) }
        powershell @args2 | Select-Object -Last 1 | Out-Null
    } finally {
        powershell -ExecutionPolicy Bypass -File "$here\game_lock.ps1" release player_checks | Out-Null
        powershell -ExecutionPolicy Bypass -File "$here\bannerlord_test_config.ps1" -mode restore | Out-Null
    }
    $log = Join-Path $out "$tag\a1\log.txt"
    $att = Join-Path $out "$tag\a1\attempt.json"
    $ran = (Test-Path $att) -and ((Get-Content $att -Raw | ConvertFrom-Json).status -eq "pass")
    if (-not $c.pattern) { Say ("{0} {1}: run {2}" -f $(if ($ran) { "PASS" } else { "FAIL" }), $c.name, $(if ($ran) { "finished" } else { "crashed or never reached the end marker" })); continue }
    if (-not (Test-Path $log)) { Say "FAIL $($c.name): no log (pass -LogFile)"; continue }
    $rows = @{}; $evalFailed = $false
    foreach ($l in (Select-String -Path $log -Pattern $c.pattern)) {
        $m = $l.Matches[0]
        # the capture groups are read as numbers (invariant culture) and combined by Get-CheckValue; log text is never executed
        try { $rows[$m.Groups[1].Value] = Get-CheckValue ([string]$c.value) $m.Groups }
        catch { Say "FAIL $($c.name): value '$($c.value)' on log line '$($l.Line.Trim())': $($_.Exception.Message)"; $evalFailed = $true; break }
    }
    if ($evalFailed) { continue }
    if ($rows.Count -eq 0) { Say "FAIL $($c.name): the pattern matched no line"; continue }
    foreach ($k in ($rows.Keys | Sort-Object)) {
        $v = [Math]::Round($rows[$k], 2); $ok = $true
        if ($null -ne $c.min -and $v -lt $c.min) { $ok = $false }
        if ($null -ne $c.max -and $v -gt $c.max) { $ok = $false }
        Say ("{0} {1} {2}: {3}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $c.name, $k, $v)
    }
}
[IO.File]::WriteAllLines((Join-Path $out "summary.txt"), $lines)
"summary: $out\summary.txt (" + @($lines | Where-Object { $_ -like "FAIL*" }).Count + " FAIL)"

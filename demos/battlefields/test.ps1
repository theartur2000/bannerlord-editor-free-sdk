# One unattended run of the GenFields demo in the unmodified game plus only this module.
#   powershell -ExecutionPolicy Bypass -File demos\battlefields\test.ps1 -Scene gen_field_varagos [-Words "tod=12 fight secs=60 quit"]
#       [-Install "--scene a --scene b"] [-Dll <folder with GenFields.dll>] [-Campaign] [-Tag name] [-LockScript <game_lock.ps1>]
# Installs the module (demos\battlefields\install.py), runs harness\night_test.ps1, prints the runner's and the module's lines of
# this run, and turns the screenshots (BMP) of the run into PNG files under <SDK home>\out\<tag>\shots.
param([string]$Scene = "", [string]$Words = "tod=12 fight secs=60 quit", [string]$Install = "", [string]$Dll = "",
      [switch]$Campaign, [string]$Tag = "", [int]$TimeoutSec = 420, [string]$LockScript = "")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent (Split-Path -Parent $here)
$game = if ($env:BANNERLORD_DIR) { $env:BANNERLORD_DIR } else { "C:\Program Files (x86)\Steam\steamapps\common\Mount & Blade II Bannerlord" }
$sdkHome = if ($env:BANNERLORD_SDK_HOME) { $env:BANNERLORD_SDK_HOME } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "bannerlord-sdk" } else { Join-Path $HOME ".bannerlord-sdk" }
if (Get-Process Bannerlord*, TaleWorlds.MountAndBlade* -ErrorAction SilentlyContinue) { "the game is running: not starting"; exit 3 }
if (-not $Tag) { $Tag = "genfields_" + (Get-Date -Format "MMdd_HHmmss") }
if (-not $Install -and $Scene) { $Install = "--scene $Scene" }
$inst = @("$here\install.py") + ($Install -split " " | Where-Object { $_ })
if ($Dll) { $inst += @("--dll", $Dll) }
& python @inst | Select-Object -Last 2
if ($LASTEXITCODE -ne 0) { "install failed"; exit 1 }
if ($LockScript) { & powershell -ExecutionPolicy Bypass -File $LockScript take genfields | Out-Null; if ($LASTEXITCODE -ne 0) { "game lock is held: not starting"; exit 3 } }
$shots = Join-Path $sdkHome "out\genfields_shots"
if (Test-Path $shots) { Remove-Item "$shots\*.bmp" -Force -ErrorAction SilentlyContinue }
$log = Join-Path $game "Modules\GenFields\genfields_log.txt"
$w = if ($Scene) { "scene=$Scene $Words" } else { $Words }
$nt = @("-ExecutionPolicy", "Bypass", "-File", "$repo\harness\night_test.ps1", "-Module", "GenFields", "-Words", "`"$w`"", "-LogFile", "`"$log`"", "-Tag", $Tag, "-TimeoutSec", $TimeoutSec, "-NoRetry")
if ($Campaign) { $nt += "-Campaign" }
& powershell @nt 2>&1 | Select-Object -Last 2
if ($LockScript) { & powershell -ExecutionPolicy Bypass -File $LockScript release genfields | Out-Null }
$out = Join-Path $sdkHome "out\$Tag"
"--- runner"; Get-Content "$out\a1\run.txt" -ErrorAction SilentlyContinue | Where-Object { $_ -notmatch "not responding" } | Select-Object -First 8
"--- module"; Get-Content "$out\a1\log.txt" -ErrorAction SilentlyContinue | Where-Object { $_ -notmatch "screenshot|screen ->|video skipped" } | Select-Object -Last 22
New-Item -ItemType Directory -Force "$out\shots" | Out-Null
$py = "import glob,os,sys`nfrom PIL import Image`nfor f in sorted(glob.glob(os.path.join(sys.argv[1],'*.bmp'))):`n    im=Image.open(f).convert('RGB'); im.thumbnail((1280,1280)); im.save(os.path.join(sys.argv[2],os.path.splitext(os.path.basename(f))[0]+'.png'))`nprint(len(glob.glob(os.path.join(sys.argv[2],'*.png'))),'screenshots in',sys.argv[2])"
$py | python - $shots "$out\shots"

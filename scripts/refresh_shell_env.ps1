$ErrorActionPreference = "Stop"

# Reload PATH from registry for the current shell session.
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$machinePath;$userPath"

Write-Output "PATH reloaded for current PowerShell session."

function Test-Tool {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) {
        Write-Output "${Name}: OK -> $($cmd.Source)"
    }
    else {
        Write-Output "${Name}: MISSING"
    }
}

Write-Output "== Tool Check =="
Test-Tool git
Test-Tool node
Test-Tool npm
Test-Tool py

Write-Output "== Version Check =="
try { git --version } catch { Write-Output "git --version failed" }
try { node -v } catch { Write-Output "node -v failed" }
try { npm -v } catch { Write-Output "npm -v failed" }
try { py -V } catch { Write-Output "py -V failed" }

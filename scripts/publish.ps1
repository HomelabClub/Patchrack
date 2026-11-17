# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
<#
.SYNOPSIS
  Publishes a new Patchrack zip to GitHub in one go.

.DESCRIPTION
  Takes Patchrack_vX.Y.Z.zip (by default the newest one in Downloads), copies its files into
  your local repository, checks the result, and commits and pushes it. GitHub then tests it
  and, because the version is new, creates the tag, the release and the Docker image itself
  (see .github/workflows/release.yml). No tag commands needed.

  Run it from PowerShell:
    powershell -ExecutionPolicy Bypass -File scripts\publish.ps1

.PARAMETER Zip
  The zip to publish. Default: the newest Patchrack_v*.zip in your Downloads folder.

.PARAMETER Repo
  Your local repository folder. Default: the repository this script is in, or else
  Documents\patchrack-fresh.

.PARAMETER Yes
  Don't ask before committing and pushing.
#>
[CmdletBinding()]
param(
    [string]$Zip,
    [string]$Repo,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Fail($text) { Write-Host ""; Write-Host "Stopped: $text" -ForegroundColor Red; exit 1 }

$gitExe = (Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
function RunGit {
    # Runs git and returns its output lines; stops only when git itself fails (exit code).
    # Windows PowerShell 5.1 turns anything a program writes to stderr, such as git's
    # line-ending warnings, into an error, so stderr is collected as plain text here.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $gitExe.Source -c core.safecrlf=false @args 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { Fail ("git " + ($args -join " ") + " failed:`n" + ($out -join "`n")) }
    # git's warnings aren't results
    return @($out | Where-Object { $_ -notmatch '^(warning: |hint: )' })
}

# ---- what and where --------------------------------------------------------------------
if (-not $gitExe) { Fail "Git isn't installed, or not on PATH." }

$home_ = [Environment]::GetFolderPath("UserProfile")
if (-not $Zip) {
    $found = Get-ChildItem -Path (Join-Path $home_ "Downloads") -Filter "Patchrack_v*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $found) { Fail "No Patchrack_v*.zip found in your Downloads folder. Give one with -Zip." }
    $Zip = $found.FullName
}
if (-not (Test-Path -LiteralPath $Zip)) { Fail "Zip not found: $Zip" }
$Zip = (Resolve-Path -LiteralPath $Zip).Path

if (-not $Repo) {
    $here = Split-Path -Parent $PSScriptRoot
    if ($here -and (Test-Path (Join-Path $here ".git"))) { $Repo = $here }
    else { $Repo = Join-Path $home_ "Documents\patchrack-fresh" }
}
if (-not (Test-Path (Join-Path $Repo ".git"))) {
    Fail "$Repo isn't a Git repository. Give your repository folder with -Repo, or clone it first:`n  git clone https://github.com/HomelabClub/Patchrack.git `"$Repo`""
}
$Repo = (Resolve-Path -LiteralPath $Repo).Path

Say "Patchrack publish" "Cyan"
Say "  Zip:        $Zip"
Say "  Repository: $Repo"

# ---- unpack and read the version --------------------------------------------------------
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("patchrack-publish-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
try {
    Expand-Archive -LiteralPath $Zip -DestinationPath $tmp -Force
    $src = Join-Path $tmp "patchrack"
    $main = Join-Path $src "app\main.py"
    if (-not (Test-Path $main)) { $main = Join-Path $src "app/main.py" }
    if (-not (Test-Path $main)) { Fail "This zip doesn't contain patchrack/app/main.py. Is it a Patchrack zip?" }
    $m = Select-String -Path $main -Pattern '^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$' | Select-Object -First 1
    if (-not $m) { Fail "Couldn't read VERSION from app/main.py in the zip." }
    $version = $m.Matches[0].Groups[1].Value
    $zipName = [IO.Path]::GetFileName($Zip)
    if ($zipName -match 'v([0-9]+\.[0-9]+\.[0-9]+)' -and $Matches[1] -ne $version) {
        Fail "The zip is named $zipName but contains version $version."
    }
    Say "  Version:    $version"

    # ---- the repository must be clean and up to date ----------------------------------------
    Push-Location $Repo
    try {
        $branch = (RunGit rev-parse --abbrev-ref HEAD | Select-Object -First 1).ToString().Trim()
        if ($branch -ne "main") { Fail "The repository is on branch '$branch'. Switch to main first: git checkout main" }
        $dirty = RunGit status --porcelain
        if ($dirty) { Fail "The repository has changes that aren't committed:`n$($dirty -join "`n")`nCommit or undo them first (git stash), then run this again." }
        Say "Getting the latest state from GitHub..."
        RunGit pull --ff-only --quiet | Out-Null
        $remoteTag = RunGit ls-remote --tags origin "refs/tags/v$version"
        if ($remoteTag) { Fail "Version $version is already released on GitHub. A new zip needs a new version number." }

        # ---- copy: the repository becomes exactly the zip's content ----------------------------
        Say "Copying the new files..."
        $keep = @(".git", "secrets", "data", "backups")
        $srcFull = (Resolve-Path -LiteralPath $src).Path
        # remove files that are no longer part of Patchrack
        Get-ChildItem -LiteralPath $Repo -Recurse -Force -File | ForEach-Object {
            $rel = $_.FullName.Substring($Repo.Length).TrimStart('\', '/')
            $top = ($rel -split '[\\/]')[0]
            if ($keep -notcontains $top -and -not (Test-Path -LiteralPath (Join-Path $srcFull $rel))) {
                Remove-Item -LiteralPath $_.FullName -Force
            }
        }
        # copy everything from the zip
        Get-ChildItem -LiteralPath $srcFull -Recurse -Force -File | ForEach-Object {
            $rel = $_.FullName.Substring($srcFull.Length).TrimStart('\', '/')
            $top = ($rel -split '[\\/]')[0]
            if ($keep -contains $top) { return }
            $dest = Join-Path $Repo $rel
            $dir = Split-Path -Parent $dest
            if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
        }
        # remove folders that became empty
        Get-ChildItem -LiteralPath $Repo -Recurse -Force -Directory | Sort-Object { $_.FullName.Length } -Descending | ForEach-Object {
            $rel = $_.FullName.Substring($Repo.Length).TrimStart('\', '/')
            $top = ($rel -split '[\\/]')[0]
            if ($keep -notcontains $top -and -not (Get-ChildItem -LiteralPath $_.FullName -Force)) {
                Remove-Item -LiteralPath $_.FullName -Force
            }
        }

        # ---- check what will be committed ---------------------------------------------------
        # zips carry no "executable" flag; keep the flags the repository already has
        RunGit -c core.fileMode=false add -A | Out-Null
        $staged = RunGit diff --cached --name-status
        if (-not $staged) { Fail "The zip is the same as what's already on GitHub. Nothing to publish." }
        $secret = $staged | Where-Object { $_ -match '\ssecrets[\\/]' -or $_ -match '\spatchrack_secret' }
        if ($secret) {
            RunGit reset --quiet | Out-Null
            Fail "A secret file would be uploaded:`n$($secret -join "`n")`nNothing was committed. Check .gitignore."
        }
        Say ""
        Say "Changes for Patchrack ${version}:" "Cyan"
        $staged | ForEach-Object {
            $parts = $_ -split "`t"
            $what = switch -Regex ($parts[0]) { '^A' { "new     " } '^D' { "removed " } default { "changed " } }
            Say ("  " + $what + $parts[-1])
        }
        Say ""
        if (-not $Yes) {
            $answer = Read-Host "Publish Patchrack $version to GitHub? (y/N)"
            if ($answer -notmatch '^(y|yes|j|ja)$') {
                RunGit reset --quiet | Out-Null
                RunGit checkout -- . | Out-Null
                RunGit clean -fdq -e secrets -e data -e backups | Out-Null
                Say "Nothing was published. The repository is back as it was." "Yellow"
                exit 0
            }
        }
        RunGit commit --quiet -m "Patchrack $version" | Out-Null
        Say "Pushing to GitHub..."
        RunGit push --quiet origin main | Out-Null
        $url = (RunGit remote get-url origin | Select-Object -First 1).ToString().Trim() -replace '\.git$', ''
        if ($url -match '^git@github\.com:(.+)$') { $url = "https://github.com/" + $Matches[1] }
        Say ""
        Say "Done. Patchrack $version is on GitHub." "Green"
        Say "GitHub now tests it and publishes the release by itself, in about 5 to 10 minutes:"
        Say "  $url/actions"
        Say "When 'Release' is green, the release with Patchrack_v$version.zip is here:"
        Say "  $url/releases/latest"
    }
    finally { Pop-Location }
}
finally {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

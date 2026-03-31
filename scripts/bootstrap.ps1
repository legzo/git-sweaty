param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SetupArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (($null -eq $SetupArgs -or $SetupArgs.Count -eq 0) -and $null -ne $MyInvocation.UnboundArguments -and $MyInvocation.UnboundArguments.Count -gt 0) {
    $SetupArgs = @($MyInvocation.UnboundArguments | ForEach-Object { [string]$_ })
} elseif (($null -eq $SetupArgs -or $SetupArgs.Count -eq 0) -and $args.Count -gt 0) {
    $SetupArgs = @($args | ForEach-Object { [string]$_ })
}

$UpstreamRepo = if ([string]::IsNullOrWhiteSpace($env:GIT_SWEATY_UPSTREAM_REPO)) {
    "aspain/git-sweaty"
} else {
    $env:GIT_SWEATY_UPSTREAM_REPO
}

function Write-Info {
    param([string]$Message)
    Write-Host $Message
}

function Write-WarnLine {
    param([string]$Message)
    Write-Host "WARN: $Message"
}

function Fail {
    param([string]$Message)
    throw $Message
}

function Read-YesNo {
    param(
        [string]$Prompt,
        [string]$Default = "Y"
    )

    $assumeYes = $null
    if (-not [string]::IsNullOrWhiteSpace($env:GIT_SWEATY_BOOTSTRAP_ASSUME_YES)) {
        $assumeYes = $env:GIT_SWEATY_BOOTSTRAP_ASSUME_YES.Trim().ToLowerInvariant()
    }
    if ($assumeYes -in @("1", "true", "yes", "y")) {
        return $true
    }

    $suffix = if ($Default -eq "Y") { "[Y/n]" } else { "[y/N]" }
    while ($true) {
        $answer = Read-Host "$Prompt $suffix"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            return $Default -eq "Y"
        }

        switch ($answer.Trim().ToLowerInvariant()) {
            "y" { return $true }
            "yes" { return $true }
            "n" { return $false }
            "no" { return $false }
            default { Write-Host "Please enter y or n." }
        }
    }
}

function Refresh-Path {
    $paths = @()
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not [string]::IsNullOrWhiteSpace($machinePath)) {
        $paths += $machinePath
    }
    if (-not [string]::IsNullOrWhiteSpace($userPath)) {
        $paths += $userPath
    }
    if ($paths.Count -gt 0) {
        $env:Path = ($paths -join ";")
    }
}

function Join-PathSafe {
    param(
        [string]$Base,
        [string]$Child
    )

    if ([string]::IsNullOrWhiteSpace($Base)) {
        return $null
    }

    return Join-Path $Base $Child
}

function Resolve-CommandPath {
    param([string[]]$Names)

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command -and -not [string]::IsNullOrWhiteSpace($command.Path)) {
            return $command.Path
        }
    }

    return $null
}

function Invoke-SelfFromTempScriptIfNeeded {
    param([string[]]$SetupArgs)

    if (-not [string]::IsNullOrWhiteSpace($PSCommandPath)) {
        return
    }

    $scriptBlock = $MyInvocation.MyCommand.ScriptBlock
    if ($null -eq $scriptBlock) {
        return
    }

    $scriptText = $scriptBlock.ToString()
    if ([string]::IsNullOrWhiteSpace($scriptText)) {
        return
    }

    $tempScriptPath = Join-Path ([IO.Path]::GetTempPath()) ("git-sweaty-bootstrap-" + [Guid]::NewGuid().ToString("N") + ".ps1")
    $powershellPath = Resolve-CommandPath @("pwsh", "pwsh.exe", "powershell", "powershell.exe")
    if (-not $powershellPath) {
        Fail "PowerShell executable not found. Save this script to a .ps1 file and run it again."
    }

    try {
        Set-Content -Path $tempScriptPath -Value $scriptText -Encoding UTF8
        & $powershellPath -NoProfile -ExecutionPolicy Bypass -File $tempScriptPath @SetupArgs
        if ($null -ne $LASTEXITCODE) {
            return [int]$LASTEXITCODE
        }
        return 0
    } finally {
        Remove-Item -Path $tempScriptPath -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-WingetPath {
    Refresh-Path

    $commandPath = Resolve-CommandPath @("winget", "winget.exe")
    if ($commandPath) {
        return $commandPath
    }

    $windowsAppsWinget = Join-PathSafe $env:LOCALAPPDATA "Microsoft\WindowsApps\winget.exe"
    if (Test-Path $windowsAppsWinget) {
        return $windowsAppsWinget
    }

    return $null
}

function Invoke-WingetInstall {
    param(
        [string]$PackageId,
        [string]$DisplayName
    )

    $wingetPath = Resolve-WingetPath
    if (-not $wingetPath) {
        return $false
    }

    foreach ($scope in @("user", $null)) {
        $args = @(
            "install",
            "--id", $PackageId,
            "--exact",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
            "--silent"
        )
        if (-not [string]::IsNullOrWhiteSpace($scope)) {
            $args += @("--scope", $scope)
        }

        Write-Info "Installing $DisplayName with winget..."
        & $wingetPath @args
        if ($LASTEXITCODE -eq 0) {
            Refresh-Path
            return $true
        }
    }

    return $false
}

function Resolve-GhPath {
    Refresh-Path

    $explicitGhPath = $env:GIT_SWEATY_BOOTSTRAP_GH_PATH
    if (-not [string]::IsNullOrWhiteSpace($explicitGhPath) -and (Test-Path $explicitGhPath)) {
        return $explicitGhPath
    }

    $commandPath = Resolve-CommandPath @("gh", "gh.exe")
    if ($commandPath) {
        return $commandPath
    }

    foreach ($candidate in @(
        (Join-PathSafe $env:LOCALAPPDATA "Programs\GitHub CLI\gh.exe"),
        (Join-PathSafe $env:ProgramFiles "GitHub CLI\gh.exe"),
        (Join-PathSafe ${env:ProgramFiles(x86)} "GitHub CLI\gh.exe")
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    return $null
}

function Invoke-WebRequestWithRetry {
    param(
        [string]$Uri,
        [string]$OutFile,
        [int]$MaxAttempts = 5,
        [int]$DelaySeconds = 2
    )

    $lastError = $null
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $OutFile
            return
        } catch {
            $lastError = $_.Exception
            if ($attempt -eq $MaxAttempts) {
                throw $lastError
            }

            Write-WarnLine "Download attempt $attempt of $MaxAttempts failed. Retrying in $DelaySeconds seconds..."
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Ensure-GhPath {
    $ghPath = Resolve-GhPath
    if ($ghPath) {
        return $ghPath
    }

    Write-Info ""
    Write-Info "GitHub CLI ('gh') is required so setup can create or reuse your fork, store secrets, and configure GitHub Pages."
    if (-not (Read-YesNo "Try to install GitHub CLI automatically with winget now?" "Y")) {
        Fail "GitHub CLI ('gh') is required. Install it from https://cli.github.com/ and run this command again."
    }

    if (-not (Invoke-WingetInstall "GitHub.cli" "GitHub CLI")) {
        Fail "Unable to install GitHub CLI automatically with winget. Install it from https://cli.github.com/ and run this command again."
    }

    $ghPath = Resolve-GhPath
    if (-not $ghPath) {
        Fail "GitHub CLI appears to be installed, but PowerShell could not find 'gh'. Close and reopen PowerShell, then run this command again."
    }

    return $ghPath
}

function Test-PythonRuntime {
    param(
        [string]$CommandPath,
        [string[]]$BaseArgs
    )

    try {
        & $CommandPath @BaseArgs "--version" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-PythonRuntime {
    Refresh-Path

    $explicitPythonPath = $env:GIT_SWEATY_BOOTSTRAP_PYTHON_PATH
    if (-not [string]::IsNullOrWhiteSpace($explicitPythonPath) -and (Test-PythonRuntime -CommandPath $explicitPythonPath -BaseArgs @())) {
        return [pscustomobject]@{
            Command = $explicitPythonPath
            BaseArgs = @()
        }
    }

    $explicitPyLauncherPath = $env:GIT_SWEATY_BOOTSTRAP_PY_LAUNCHER_PATH
    if (-not [string]::IsNullOrWhiteSpace($explicitPyLauncherPath) -and (Test-PythonRuntime -CommandPath $explicitPyLauncherPath -BaseArgs @("-3"))) {
        return [pscustomobject]@{
            Command = $explicitPyLauncherPath
            BaseArgs = @("-3")
        }
    }

    foreach ($commandPath in @(
        (Resolve-CommandPath @("py", "py.exe")),
        (Resolve-CommandPath @("python", "python.exe"))
    )) {
        if (-not $commandPath) {
            continue
        }
        if ($commandPath -like "*WindowsApps*") {
            continue
        }

        $baseArgs = if ($commandPath.ToLowerInvariant().EndsWith("py.exe") -or $commandPath.ToLowerInvariant().EndsWith("\py")) {
            @("-3")
        } else {
            @()
        }

        if (Test-PythonRuntime -CommandPath $commandPath -BaseArgs $baseArgs) {
            return [pscustomobject]@{
                Command = $commandPath
                BaseArgs = $baseArgs
            }
        }
    }

    foreach ($root in @(
        (Join-PathSafe $env:LOCALAPPDATA "Programs\Python"),
        $env:ProgramFiles
    )) {
        if ([string]::IsNullOrWhiteSpace($root) -or -not (Test-Path $root)) {
            continue
        }

        $candidates = Get-ChildItem -Path $root -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
        foreach ($candidate in $candidates) {
            if (Test-PythonRuntime -CommandPath $candidate.FullName -BaseArgs @()) {
                return [pscustomobject]@{
                    Command = $candidate.FullName
                    BaseArgs = @()
                }
            }
        }
    }

    return $null
}

function Ensure-PythonRuntime {
    $runtime = Resolve-PythonRuntime
    if ($runtime) {
        return $runtime
    }

    Write-Info ""
    Write-Info "Python 3 is required so setup can run the guided GitHub and provider onboarding."
    if (-not (Read-YesNo "Try to install Python automatically with winget now?" "Y")) {
        Fail "Python 3 is required. Install it from https://www.python.org/downloads/windows/ and run this command again."
    }

    $installed = $false
    foreach ($packageId in @("Python.Python.3.13", "Python.Python.3.12")) {
        if (Invoke-WingetInstall $packageId "Python 3") {
            $installed = $true
            break
        }
    }
    if (-not $installed) {
        Fail "Unable to install Python automatically with winget. Install it from https://www.python.org/downloads/windows/ and run this command again."
    }

    $runtime = Resolve-PythonRuntime
    if (-not $runtime) {
        Fail "Python appears to be installed, but PowerShell could not find it. Close and reopen PowerShell, then run this command again."
    }

    return $runtime
}

function Test-GhAuthenticated {
    param([string]$GhPath)

    & $GhPath auth status *> $null
    return $LASTEXITCODE -eq 0
}

function Ensure-GhAuthenticated {
    param([string]$GhPath)

    if (Test-GhAuthenticated $GhPath) {
        return
    }

    Write-Info ""
    Write-Info "GitHub CLI is not authenticated."
    Write-Info "If you do not have a GitHub account yet, create one first: https://github.com/signup"
    if (-not (Read-YesNo "Run GitHub sign-in now? This will open your browser and request the repo and workflow permissions needed for setup." "Y")) {
        Fail "GitHub CLI auth is required. Run 'gh auth login' and then run this command again."
    }

    & $GhPath auth login --web --git-protocol https --scopes repo,workflow
    if ($LASTEXITCODE -ne 0 -or -not (Test-GhAuthenticated $GhPath)) {
        Fail "GitHub CLI auth did not complete successfully. Run 'gh auth login' and then run this command again."
    }
}

function Should-ForceInteractiveSetupAuth {
    $assumeYes = $null
    if (-not [string]::IsNullOrWhiteSpace($env:GIT_SWEATY_BOOTSTRAP_ASSUME_YES)) {
        $assumeYes = $env:GIT_SWEATY_BOOTSTRAP_ASSUME_YES.Trim().ToLowerInvariant()
    }

    return -not ($assumeYes -in @("1", "true", "yes", "y"))
}

function Get-SetupArgValue {
    param(
        [string[]]$SetupArgs,
        [string]$Name
    )

    if ($null -eq $SetupArgs -or $SetupArgs.Count -eq 0) {
        return $null
    }

    for ($i = 0; $i -lt $SetupArgs.Count; $i++) {
        $item = $SetupArgs[$i]
        if ($item -eq $Name) {
            if ($i + 1 -lt $SetupArgs.Count) {
                return $SetupArgs[$i + 1]
            }
            return $null
        }
        if ($item.StartsWith("$Name=")) {
            return $item.Substring($Name.Length + 1)
        }
    }

    return $null
}

function Invoke-GhJson {
    param(
        [string]$GhPath,
        [string[]]$Arguments
    )

    $jsonText = (& $GhPath @Arguments | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Fail "GitHub CLI command failed: gh $($Arguments -join ' ')"
    }
    if ([string]::IsNullOrWhiteSpace($jsonText)) {
        return $null
    }

    return $jsonText | ConvertFrom-Json
}

function Invoke-GhText {
    param(
        [string]$GhPath,
        [string[]]$Arguments
    )

    $text = (& $GhPath @Arguments | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Fail "GitHub CLI command failed: gh $($Arguments -join ' ')"
    }

    return $text
}

function Get-GhLogin {
    param([string]$GhPath)

    $login = Invoke-GhText $GhPath @("api", "user", "--jq", ".login")
    if ([string]::IsNullOrWhiteSpace($login)) {
        Fail "Unable to resolve your GitHub username from the current gh auth session."
    }

    return $login
}

function Get-ExistingForkRepo {
    param(
        [string]$GhPath,
        [string]$Login,
        [string]$UpstreamRepo
    )

    $defaultForkRepo = "$Login/$($UpstreamRepo.Split('/')[1])"
    & $GhPath repo view $defaultForkRepo *> $null
    if ($LASTEXITCODE -eq 0) {
        return $defaultForkRepo
    }

    $forkQuery = ".[] | select(.owner.login == `"$Login`") | .full_name"
    $forkMatches = Invoke-GhText $GhPath @("api", "repos/$UpstreamRepo/forks?per_page=100", "--paginate", "--jq", $forkQuery)
    foreach ($forkMatch in @($forkMatches -split "\r?\n")) {
        $trimmedForkMatch = $forkMatch.Trim()
        if (-not [string]::IsNullOrWhiteSpace($trimmedForkMatch)) {
            return $trimmedForkMatch
        }
    }

    $repos = Invoke-GhJson $GhPath @("repo", "list", $Login, "--fork", "--limit", "1000", "--json", "nameWithOwner,parent")
    foreach ($repo in @($repos)) {
        if ($null -ne $repo.parent -and $repo.parent.nameWithOwner -eq $UpstreamRepo) {
            return [string]$repo.nameWithOwner
        }
    }

    return $null
}

function Ensure-RepoAccess {
    param(
        [string]$GhPath,
        [string]$Repo
    )

    & $GhPath repo view $Repo *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail "Repository is not accessible with the current GitHub account: $Repo"
    }
}

function Prompt-RepositoryName {
    param([string]$DefaultName)

    while ($true) {
        $answer = Read-Host "Repository name (default: $DefaultName)"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            return $DefaultName
        }

        $trimmed = $answer.Trim()
        if ($trimmed -match "^[A-Za-z0-9._-]+$") {
            return $trimmed
        }

        Write-WarnLine "Invalid repository name. Use only letters, numbers, '.', '_' or '-'."
    }
}

function Ensure-RepoForCurrentLogin {
    param(
        [string]$GhPath,
        [string]$Login,
        [string]$DefaultRepoName
    )

    $repoName = Prompt-RepositoryName $DefaultRepoName
    $repoSlug = "$Login/$repoName"

    & $GhPath repo view $repoSlug *> $null
    if ($LASTEXITCODE -eq 0) {
        return $repoSlug
    }

    Write-Info "Creating public repository: $repoSlug"
    & $GhPath repo create $repoSlug --public
    if ($LASTEXITCODE -ne 0) {
        Fail "Unable to create repository: $repoSlug"
    }

    Ensure-RepoAccess $GhPath $repoSlug
    return $repoSlug
}

function Resolve-TargetRepository {
    param(
        [string]$GhPath,
        [string]$UpstreamRepo,
        [string[]]$SetupArgs
    )

    $explicitRepo = Get-SetupArgValue -SetupArgs $SetupArgs -Name "--repo"
    if (-not [string]::IsNullOrWhiteSpace($explicitRepo)) {
        Ensure-RepoAccess $GhPath $explicitRepo
        return $explicitRepo
    }

    $login = Get-GhLogin $GhPath
    $upstreamOwner = $UpstreamRepo.Split("/")[0]
    $defaultRepoName = "$($UpstreamRepo.Split('/')[1])-dashboard"

    if ($login -eq $upstreamOwner) {
        Write-Info "You are signed into the upstream owner account ($login)."
        return Ensure-RepoForCurrentLogin -GhPath $GhPath -Login $login -DefaultRepoName $defaultRepoName
    }

    $existingFork = Get-ExistingForkRepo -GhPath $GhPath -Login $login -UpstreamRepo $UpstreamRepo
    if (-not [string]::IsNullOrWhiteSpace($existingFork)) {
        Write-Info "Using existing fork: $existingFork"
        return $existingFork
    }

    Write-Info "Creating your fork of $UpstreamRepo..."
    # Omit explicit false boolean flags for broad gh CLI compatibility.
    & $GhPath repo fork $UpstreamRepo
    if ($LASTEXITCODE -ne 0) {
        $existingFork = Get-ExistingForkRepo -GhPath $GhPath -Login $login -UpstreamRepo $UpstreamRepo
        if (-not [string]::IsNullOrWhiteSpace($existingFork)) {
            Write-WarnLine "Fork creation did not exit cleanly, but an accessible fork already exists."
            return $existingFork
        }
        Fail "Unable to create or locate a fork for $UpstreamRepo under $login."
    }

    $forkRepo = Get-ExistingForkRepo -GhPath $GhPath -Login $login -UpstreamRepo $UpstreamRepo
    if ([string]::IsNullOrWhiteSpace($forkRepo)) {
        $forkRepo = "$login/$($UpstreamRepo.Split('/')[1])"
    }
    Ensure-RepoAccess $GhPath $forkRepo
    return $forkRepo
}

function Get-DefaultBranch {
    param(
        [string]$GhPath,
        [string]$Repo
    )

    $branch = Invoke-GhText $GhPath @("api", "repos/$Repo", "--jq", ".default_branch")
    if ([string]::IsNullOrWhiteSpace($branch)) {
        return "main"
    }

    return $branch
}

function New-TemporaryDirectory {
    $path = Join-Path ([IO.Path]::GetTempPath()) ("git-sweaty-bootstrap-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    return $path
}

function Invoke-OnlineSetup {
    param(
        [string]$GhPath,
        $PythonRuntime,
        [string]$UpstreamRepo,
        [string]$TargetRepo,
        [string[]]$SetupArgs
    )

    $archiveUrl = $env:GIT_SWEATY_BOOTSTRAP_ARCHIVE_URL
    if ([string]::IsNullOrWhiteSpace($archiveUrl)) {
        $defaultBranch = Get-DefaultBranch -GhPath $GhPath -Repo $UpstreamRepo
        $archiveUrl = "https://github.com/$UpstreamRepo/archive/refs/heads/$defaultBranch.zip"
    }
    $tempRoot = New-TemporaryDirectory
    $archivePath = Join-Path $tempRoot "source.zip"
    $extractDir = Join-Path $tempRoot "source"

    try {
        Write-Info "Downloading setup source bundle from $archiveUrl"
        Invoke-WebRequestWithRetry -Uri $archiveUrl -OutFile $archivePath

        Expand-Archive -Path $archivePath -DestinationPath $extractDir -Force
        $sourceRoot = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
        if ($null -eq $sourceRoot) {
            Fail "Unable to extract setup source bundle."
        }

        $setupScript = Join-Path $sourceRoot.FullName "scripts\setup_auth.py"
        if (-not (Test-Path $setupScript)) {
            Fail "Setup script not found in downloaded source bundle (scripts/setup_auth.py)."
        }

        Write-Info ""
        Write-Info "Launching online setup..."
        $env:GIT_SWEATY_BOOTSTRAP_GH_PATH = $GhPath
        if (Should-ForceInteractiveSetupAuth) {
            $env:GIT_SWEATY_BOOTSTRAP_FORCE_INTERACTIVE = "1"
        } else {
            Remove-Item Env:GIT_SWEATY_BOOTSTRAP_FORCE_INTERACTIVE -ErrorAction SilentlyContinue
        }
        $ghDir = Split-Path -Path $GhPath -Parent
        if (-not [string]::IsNullOrWhiteSpace($ghDir)) {
            $pathEntries = @($env:Path -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $ghDirNormalized = $ghDir.TrimEnd("\")
            $ghOnPath = $false
            foreach ($entry in $pathEntries) {
                if ($entry.TrimEnd("\") -ieq $ghDirNormalized) {
                    $ghOnPath = $true
                    break
                }
            }
            if (-not $ghOnPath) {
                $env:Path = "$ghDir;$env:Path"
            }
        }
        $pythonArgs = @() + $PythonRuntime.BaseArgs + @($setupScript)
        if ([string]::IsNullOrWhiteSpace((Get-SetupArgValue -SetupArgs $SetupArgs -Name "--repo"))) {
            $pythonArgs += @("--repo", $TargetRepo)
        }
        if ($null -ne $SetupArgs -and $SetupArgs.Count -gt 0) {
            $pythonArgs += $SetupArgs
        }

        Push-Location $sourceRoot.FullName
        try {
            & $PythonRuntime.Command @pythonArgs
            if ($null -ne $LASTEXITCODE) {
                return [int]$LASTEXITCODE
            }
            return 0
        } finally {
            Pop-Location
        }
    } finally {
        Remove-Item -Path $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

try {
    $relaunchExitCode = Invoke-SelfFromTempScriptIfNeeded -SetupArgs $SetupArgs
    if ($null -ne $relaunchExitCode) {
        if ([int]$relaunchExitCode -ne 0) {
            exit [int]$relaunchExitCode
        }
        return
    }

    Write-Info "Preparing native Windows setup (online-only, no WSL required)..."

    $pythonRuntime = Ensure-PythonRuntime
    $ghPath = Ensure-GhPath
    Ensure-GhAuthenticated $ghPath
    $targetRepo = Resolve-TargetRepository -GhPath $ghPath -UpstreamRepo $UpstreamRepo -SetupArgs $SetupArgs

    Write-Info ""
    Write-Info "Setup summary:"
    Write-Info "- Mode: Recommended (Online-only)"
    Write-Info "- Target repository: $targetRepo"
    if (-not (Read-YesNo "Proceed?" "Y")) {
        Write-Info "Skipped setup."
        exit 0
    }

    $status = Invoke-OnlineSetup -GhPath $ghPath -PythonRuntime $pythonRuntime -UpstreamRepo $UpstreamRepo -TargetRepo $targetRepo -SetupArgs $SetupArgs
    exit $status
} catch {
    $message = if ($null -ne $_ -and $null -ne $_.Exception -and -not [string]::IsNullOrWhiteSpace($_.Exception.Message)) {
        $_.Exception.Message
    } else {
        "Setup failed."
    }
    Write-Error $message
    exit 1
}

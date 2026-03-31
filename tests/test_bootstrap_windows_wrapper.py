import os
import unittest


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WRAPPER_PATH = os.path.join(ROOT_DIR, "scripts", "bootstrap.ps1")
README_PATH = os.path.join(ROOT_DIR, "README.md")


class BootstrapWindowsWrapperTests(unittest.TestCase):
    def _read_wrapper(self) -> str:
        with open(WRAPPER_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def test_windows_wrapper_runs_native_online_only_setup(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn("Preparing native Windows setup (online-only, no WSL required)...", wrapper)
        self.assertIn("winget", wrapper)
        self.assertIn("GitHub.cli", wrapper)
        self.assertIn("Python.Python.3.13", wrapper)
        self.assertIn("auth login --web --git-protocol https --scopes repo,workflow", wrapper)
        self.assertIn("repo fork", wrapper)
        self.assertIn("Invoke-WebRequestWithRetry", wrapper)
        self.assertIn("Expand-Archive", wrapper)
        self.assertIn("scripts\\setup_auth.py", wrapper)
        self.assertNotIn("wsl.exe", wrapper)

    def test_windows_wrapper_declares_param_block_before_executable_statements(self) -> None:
        with open(WRAPPER_PATH, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f]

        first_code_line = next(line for line in lines if line.strip())
        self.assertEqual(first_code_line, "param(")

    def test_windows_wrapper_recovers_unbound_setup_args_for_powershell_one_liners(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn("$MyInvocation.UnboundArguments", wrapper)
        self.assertIn("$args.Count -gt 0", wrapper)
        self.assertIn('$SetupArgs = @($MyInvocation.UnboundArguments | ForEach-Object { [string]$_ })', wrapper)

    def test_windows_wrapper_handles_missing_assume_yes_env_without_null_method_call(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn("if (-not [string]::IsNullOrWhiteSpace($env:GIT_SWEATY_BOOTSTRAP_ASSUME_YES)) {", wrapper)
        self.assertIn("$env:GIT_SWEATY_BOOTSTRAP_ASSUME_YES.Trim().ToLowerInvariant()", wrapper)
        self.assertNotIn('($env:GIT_SWEATY_BOOTSTRAP_ASSUME_YES | ForEach-Object { $_.Trim().ToLowerInvariant() })', wrapper)

    def test_windows_wrapper_falls_back_to_generic_top_level_error_message(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn('$message = if ($null -ne $_ -and $null -ne $_.Exception', wrapper)
        self.assertIn('Write-Error $message', wrapper)
        self.assertIn('"Setup failed."', wrapper)

    def test_windows_wrapper_uses_zip_download_and_not_unix_bootstrap(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn("GIT_SWEATY_BOOTSTRAP_ARCHIVE_URL", wrapper)
        self.assertIn("archive/refs/heads/$defaultBranch.zip", wrapper)
        self.assertIn("Invoke-WebRequestWithRetry", wrapper)
        self.assertIn("Download attempt $attempt of $MaxAttempts failed. Retrying in $DelaySeconds seconds...", wrapper)
        self.assertIn("Expand-Archive", wrapper)
        self.assertNotIn("bootstrap.sh", wrapper)
        self.assertNotIn("bash <(", wrapper)
        self.assertNotIn("tar -xzf", wrapper)

    def test_windows_wrapper_installs_python_and_gh_with_user_scope_first(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn("GIT_SWEATY_BOOTSTRAP_ASSUME_YES", wrapper)
        self.assertIn("GIT_SWEATY_BOOTSTRAP_GH_PATH", wrapper)
        self.assertIn("GIT_SWEATY_BOOTSTRAP_PYTHON_PATH", wrapper)
        self.assertIn("GIT_SWEATY_BOOTSTRAP_PY_LAUNCHER_PATH", wrapper)
        self.assertIn('Resolve-CommandPath @("winget", "winget.exe")', wrapper)
        self.assertIn('Resolve-CommandPath @("gh", "gh.exe")', wrapper)
        self.assertIn('Resolve-CommandPath @("py", "py.exe")', wrapper)
        self.assertIn('Resolve-CommandPath @("python", "python.exe")', wrapper)
        self.assertIn('foreach ($scope in @("user", $null))', wrapper)
        self.assertIn('Invoke-WingetInstall "GitHub.cli" "GitHub CLI"', wrapper)
        self.assertIn('foreach ($packageId in @("Python.Python.3.13", "Python.Python.3.12"))', wrapper)
        self.assertIn('--accept-package-agreements', wrapper)
        self.assertIn('--accept-source-agreements', wrapper)
        self.assertIn('--silent', wrapper)

    def test_windows_wrapper_prefers_existing_fork_then_creates_one(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn('$defaultForkRepo = "$Login/$($UpstreamRepo.Split(\'/\')[1])"', wrapper)
        self.assertIn('& $GhPath repo view $defaultForkRepo *> $null', wrapper)
        self.assertIn('repos/$UpstreamRepo/forks?per_page=100', wrapper)
        self.assertIn('Invoke-GhJson $GhPath @("repo", "list", $Login, "--fork", "--limit", "1000", "--json", "nameWithOwner,parent")', wrapper)
        self.assertIn('Write-Info "Using existing fork: $existingFork"', wrapper)
        self.assertIn('& $GhPath repo fork $UpstreamRepo', wrapper)
        self.assertNotIn('--remote=false', wrapper)
        self.assertNotIn('--clone=false', wrapper)
        self.assertIn('Fail "Unable to create or locate a fork for $UpstreamRepo under $login."', wrapper)

    def test_windows_wrapper_preserves_explicit_repo_and_other_setup_args(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn('[string[]]$SetupArgs', wrapper)
        self.assertIn('if ($null -eq $SetupArgs -or $SetupArgs.Count -eq 0)', wrapper)
        self.assertIn('Get-SetupArgValue -SetupArgs $SetupArgs -Name "--repo"', wrapper)
        self.assertIn('if ([string]::IsNullOrWhiteSpace((Get-SetupArgValue -SetupArgs $SetupArgs -Name "--repo")))', wrapper)
        self.assertIn('$pythonArgs += @("--repo", $TargetRepo)', wrapper)
        self.assertIn('if ($null -ne $SetupArgs -and $SetupArgs.Count -gt 0)', wrapper)
        self.assertIn('$pythonArgs += $SetupArgs', wrapper)

    def test_windows_wrapper_adds_resolved_gh_directory_to_path_before_python_handoff(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn('$env:GIT_SWEATY_BOOTSTRAP_GH_PATH = $GhPath', wrapper)
        self.assertIn('Should-ForceInteractiveSetupAuth', wrapper)
        self.assertIn('$env:GIT_SWEATY_BOOTSTRAP_FORCE_INTERACTIVE = "1"', wrapper)
        self.assertIn('Remove-Item Env:GIT_SWEATY_BOOTSTRAP_FORCE_INTERACTIVE -ErrorAction SilentlyContinue', wrapper)
        self.assertIn('Push-Location $sourceRoot.FullName', wrapper)
        self.assertIn('& $PythonRuntime.Command @pythonArgs', wrapper)
        self.assertIn('if ($null -ne $LASTEXITCODE)', wrapper)
        self.assertIn('return [int]$LASTEXITCODE', wrapper)
        self.assertIn('Pop-Location', wrapper)
        self.assertIn('Split-Path -Path $GhPath -Parent', wrapper)
        self.assertIn('$pathEntries = @($env:Path -split ";"', wrapper)
        self.assertIn('$env:Path = "$ghDir;$env:Path"', wrapper)

    def test_windows_wrapper_reexecs_from_temp_file_when_started_from_iex_pipeline(self) -> None:
        wrapper = self._read_wrapper()

        self.assertIn("function Invoke-SelfFromTempScriptIfNeeded", wrapper)
        self.assertIn('$PSCommandPath', wrapper)
        self.assertIn('$MyInvocation.MyCommand.ScriptBlock', wrapper)
        self.assertIn('$scriptBlock.ToString()', wrapper)
        self.assertIn('Set-Content -Path $tempScriptPath -Value $scriptText -Encoding UTF8', wrapper)
        self.assertIn('& $powershellPath -NoProfile -ExecutionPolicy Bypass -File $tempScriptPath @SetupArgs', wrapper)
        self.assertIn('return [int]$LASTEXITCODE', wrapper)
        self.assertIn('return 0', wrapper)
        self.assertIn('Invoke-SelfFromTempScriptIfNeeded -SetupArgs $SetupArgs', wrapper)
        self.assertIn('$relaunchExitCode = Invoke-SelfFromTempScriptIfNeeded -SetupArgs $SetupArgs', wrapper)
        self.assertIn('if ($null -ne $relaunchExitCode)', wrapper)
        self.assertIn('if ([int]$relaunchExitCode -ne 0)', wrapper)
        self.assertIn('Remove-Item -Path $tempScriptPath -Force -ErrorAction SilentlyContinue', wrapper)

    def test_windows_wrapper_executes_native_flow_in_expected_order(self) -> None:
        wrapper = self._read_wrapper()

        python_index = wrapper.index("$pythonRuntime = Ensure-PythonRuntime")
        gh_index = wrapper.index("$ghPath = Ensure-GhPath")
        auth_index = wrapper.index("Ensure-GhAuthenticated $ghPath")
        target_index = wrapper.index("$targetRepo = Resolve-TargetRepository")
        launch_index = wrapper.index("$status = Invoke-OnlineSetup")

        self.assertLess(python_index, gh_index)
        self.assertLess(gh_index, auth_index)
        self.assertLess(auth_index, target_index)
        self.assertLess(target_index, launch_index)

    def test_readme_points_windows_quick_start_to_powershell_wrapper(self) -> None:
        with open(README_PATH, "r", encoding="utf-8") as f:
            readme = f.read()

        self.assertIn("scripts/bootstrap.ps1", readme)
        self.assertIn("does not require WSL", readme)
        self.assertIn("install them automatically with `winget`", readme)
        self.assertIn("same terminal session", readme)


if __name__ == "__main__":
    unittest.main()

param(
    [Parameter(Mandatory = $true)]
    [string]$EventName
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
# Best-effort: decode the inherited hook payload (stdin) as UTF-8. Wrapped in
# try/catch because setting InputEncoding can throw when stdin is a redirected
# pipe with no real console attached.
try { [Console]::InputEncoding = $utf8NoBom } catch {}
$OutputEncoding = $utf8NoBom

# Fail soft. With $ErrorActionPreference = "Stop" any unexpected terminating
# error exits non-zero and the host reports a failed hook; capture is
# best-effort, so answer with an empty JSON response and exit 0 instead.
trap {
    [Console]::Error.WriteLine("[bloomfilter] hook error: $_")
    Write-Output "{}"
    exit 0
}

function Resolve-Python {
    # Name existence is not enough: an asdf/pyenv/mise shim, or the Microsoft
    # Store `python.exe` stub, resolves as a name but fails when actually run.
    # Probe every candidate source by executing it, requiring Python >= 3.10 (the
    # collector floor) AND that the collector's module-top imports resolve (json,
    # subprocess, urllib.request). Canonical names first; `py -3` last, since some environments
    # ship a `py` that does not behave like the real launcher. `-All` so a broken
    # first match on PATH (e.g. the Store stub) cannot hide a real interpreter.
    $candidates = @(
        @{ Command = "python3"; Args = @() },
        @{ Command = "python"; Args = @() },
        @{ Command = "py"; Args = @("-3") }
    )
    $probe = 'import sys, json, subprocess, urllib.request; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)'

    foreach ($candidate in $candidates) {
        $sources = @(Get-Command $candidate.Command -All -CommandType Application -ErrorAction SilentlyContinue)
        foreach ($command in $sources) {
            try {
                # Stdin from $null so the probe can never consume the hook payload
                # the caller reads later; *> $null discards probe output. A probe
                # that throws (Store stub, encoding error) is caught and skipped,
                # never propagated -- discovery always fails soft.
                $probeArgs = @($candidate.Args) + @('-c', $probe)
                $null | & $command.Source @probeArgs *> $null
                if ($LASTEXITCODE -eq 0) {
                    return @{
                        Executable = $command.Source
                        Arguments = $candidate.Args
                    }
                }
            } catch {
                continue
            }
        }
    }

    return $null
}

function Quote-ProcessArgument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return '"' + ($Value -replace '"', '\"') + '"'
}

$python = Resolve-Python
if (-not $python) {
    [Console]::Error.WriteLine("[bloomfilter] Python was not found on PATH; skipping hook collection.")
    Write-Output "{}"
    exit 0
}

$pluginRoot = $env:CURSOR_PLUGIN_ROOT
if (-not $pluginRoot) {
    # $PSScriptRoot is the directory holding this script — the plugin's
    # hooks/ directory — so one parent is the plugin root. Two was one too
    # many: it landed on the directory holding every plugin, and the
    # collector path built from it does not exist, so the hook answered {}
    # and captured nothing. The POSIX sibling takes one level.
    $pluginRoot = Split-Path -Parent $PSScriptRoot
}

$script = Join-Path $pluginRoot "scripts\collect_hook.py"
# Windows PowerShell 5.1 prepends a UTF-8 BOM when piping to a native process,
# and the InputEncoding set above stops the reader from stripping it. Drop it
# here so the payload handed to Python stays valid JSON.
$stdin = [Console]::In.ReadToEnd().TrimStart([char]0xFEFF)
$pythonExecutable = $python["Executable"]
$pythonArguments = $python["Arguments"]

$env:PYTHONIOENCODING = "utf-8"
$process = New-Object System.Diagnostics.Process
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $pythonExecutable
$startInfo.Arguments = (($pythonArguments + @($script, $EventName)) | ForEach-Object { Quote-ProcessArgument $_ }) -join " "
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
# Force UTF-8 (no BOM) on the child's redirected streams so non-ASCII payload
# and the JSON response round-trip correctly regardless of the Windows code page.
# StandardInputEncoding only exists on .NET Core 2.1+ (PowerShell 7+); Windows
# PowerShell 5.1 runs on .NET Framework, where assigning it throws. Set it when
# present and rely on the raw-byte stdin write below everywhere else.
if ($startInfo.PSObject.Properties.Name -contains "StandardInputEncoding") {
    $startInfo.StandardInputEncoding = $utf8NoBom
}
$startInfo.StandardOutputEncoding = $utf8NoBom
$startInfo.StandardErrorEncoding = $utf8NoBom
$process.StartInfo = $startInfo

$null = $process.Start()
# Write raw UTF-8 bytes rather than going through the StreamWriter, whose
# encoding is host-dependent on 5.1 and can prepend a BOM the child would
# choke on.
$stdinBytes = [System.Text.Encoding]::UTF8.GetBytes($stdin)
$process.StandardInput.BaseStream.Write($stdinBytes, 0, $stdinBytes.Length)
$process.StandardInput.BaseStream.Flush()
$process.StandardInput.Close()
$stdout = $process.StandardOutput.ReadToEnd()
$stderr = $process.StandardError.ReadToEnd()
$process.WaitForExit()

if ($stderr) {
    [Console]::Error.Write($stderr)
}

$response = $stdout.Trim()
if (-not $response) {
    Write-Output "{}"
    exit 0
}

try {
    $null = $response | ConvertFrom-Json -ErrorAction Stop
    Write-Output $response
} catch {
    [Console]::Error.WriteLine("[bloomfilter] Hook emitted non-JSON stdout; returning empty JSON response.")
    [Console]::Error.WriteLine($response)
    Write-Output "{}"
}

exit 0

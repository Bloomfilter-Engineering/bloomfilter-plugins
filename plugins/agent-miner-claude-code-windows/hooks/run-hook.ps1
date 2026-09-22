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

# Fail soft: any unexpected terminating error answers with an empty JSON response
# and exit 0 instead of surfacing a hook error to the user.
trap {
    [Console]::Error.WriteLine("[bloomfilter] hook error: $_")
    Write-Output "{}"
    exit 0
}

function Resolve-Python {
    # Name existence is not enough: an asdf/pyenv/mise shim, or the Microsoft
    # Store `python.exe` stub, resolves as a name but fails when actually run.
    # Probe every candidate source by executing it, requiring Python >= 3.11 (the
    # collector floor) AND that the collector's module-top imports resolve (json,
    # subprocess, urllib.request). Canonical names first; `py -3` last, since some environments
    # ship a `py` that does not behave like the real launcher. `-All` so a broken
    # first match on PATH (e.g. the Store stub) cannot hide a real interpreter.
    $candidates = @(
        @{ Command = "python3"; Args = @() },
        @{ Command = "python"; Args = @() },
        @{ Command = "py"; Args = @("-3") }
    )
    $probe = 'import sys, json, subprocess, urllib.request; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)'

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

function Write-NoPythonNote {
    # With no interpreter there is no collector, and the collector is what
    # writes debug.log -- so this failure is otherwise completely silent on
    # Windows: no data, and nothing anywhere to say why. The POSIX launcher
    # already leaves a line; leave the same one here.
    param([string]$Event)

    try {
        $base = $null
        foreach ($candidate in @($env:APPDATA, $env:XDG_CONFIG_HOME)) {
            # Fully qualified, not merely rooted. .NET documents the
            # difference as a frequent mistake: "C:temp" IS rooted yet is
            # drive-relative, so it resolves against the current directory --
            # which for a hook is the user's project, exactly the case this
            # guard exists to prevent. The Python resolver rejects a relative
            # value for the same reason.
            #
            # IsPathFullyQualified says this in one call but ships only on
            # .NET Core 2.1+, and Windows PowerShell 5.1 -- the in-box shell,
            # and the ONLY launcher the -windows builds ship -- runs on .NET
            # Framework, where it does not exist and the call throws into the
            # catch below, silently costing exactly those users their note. So
            # the same predicate is spelled with two primitives that exist on
            # every version: rooted, and not drive-relative -- "C:temp" is
            # rooted yet resolves against the current directory for C:.
            if ($candidate -and
                [System.IO.Path]::IsPathRooted($candidate) -and
                $candidate -notmatch '^[A-Za-z]:[^\\/]') {
                $base = $candidate
                break
            }
        }
        if (-not $base) { $base = Join-Path $HOME ".config" }
        $noteDir = Join-Path $base "bloomfilter"
        New-Item -ItemType Directory -Force -Path $noteDir -ErrorAction Stop | Out-Null
        # InvariantCulture, because ToString() formats through the CURRENT
        # culture's calendar: the same instant renders as 2569 under th-TH and
        # 1448 under ar-SA, which would sort and parse as a different century.
        # "Z" is appended rather than left in the format string, where it is
        # not a specifier.
        $stamp = (Get-Date).ToUniversalTime().ToString(
            "yyyy-MM-ddTHH:mm:ss",
            [System.Globalization.CultureInfo]::InvariantCulture) + "Z"
        # A fixed label, not a path-derived one. Every runtime installs this
        # plugin under a differently shaped directory -- a version number for
        # Claude Code and Codex, a commit sha for Cursor -- so a name taken from
        # the path reads as a bare version or sha rather than as a plugin, and
        # two builds sharing a version collide. The collector tags
        # its own lines with the plugin name; this says which layer wrote it.
        $line = "$stamp [bloomfilter-launcher] hook skipped: " +
                "reason=no-python-3.11-found event=$Event"
        Add-Content -Path (Join-Path $noteDir "debug.log") -Value $line -ErrorAction Stop
    } catch {
        # Never let diagnostics break the hook contract.
    }
}

$python = Resolve-Python
if (-not $python) {
    # "not found" and "found but too old" are different problems with different
    # fixes, and after the floor moved to 3.11 the second is the common one.
    [Console]::Error.WriteLine("[bloomfilter] No Python 3.11+ was found on PATH; skipping hook collection.")
    Write-NoPythonNote -Event $EventName
    Write-Output "{}"
    exit 0
}

$pluginRoot = $env:CLAUDE_PLUGIN_ROOT
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

param(
    [Parameter(Mandatory = $true)]
    [string]$EventName
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
# Best-effort: decode the inherited hook payload (stdin) as UTF-8, otherwise
# Console.In decodes it with the console code page and mangles non-ASCII into
# replacement chars. Wrapped in try/catch because setting InputEncoding can
# throw when stdin is a redirected pipe with no real console attached.
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
    $candidates = @(
        @{ Command = "python"; Args = @() },
        @{ Command = "python3"; Args = @() },
        @{ Command = "py"; Args = @("-3") }
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if ($command) {
            return @{
                Executable = $command.Source
                Arguments = $candidate.Args
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

$pluginRoot = $env:CLAUDE_PLUGIN_ROOT
if (-not $pluginRoot) {
    $pluginRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}

$script = Join-Path $pluginRoot "scripts\collect_hook.py"
# Windows PowerShell 5.1 can prepend a UTF-8 BOM when piping to a native
# process; drop it so the payload handed to Python stays valid JSON.
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

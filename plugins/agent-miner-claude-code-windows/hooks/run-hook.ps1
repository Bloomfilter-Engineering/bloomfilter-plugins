param(
    [Parameter(Mandatory = $true)]
    [string]$EventName
)

$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

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
$stdin = [Console]::In.ReadToEnd()
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
$process.StandardInput.Write($stdin)
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

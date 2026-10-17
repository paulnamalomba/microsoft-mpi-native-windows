# Ported from petsc-native-windows config/native_windows/install-toolchain.ps1 at
# b110b88419df3f98805b66533dfeeb18f868a315 (SHA-256 2bee7e68...016a). Changes: the
# fork's component set (no icx or mkl), native/toolchain-pins.json, and the fork's cache.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('visual_studio','visual_studio_installer','ifx','msmpi_runtime','msmpi_sdk')][string]$Component,
    [string]$Cache = (Join-Path $env:LOCALAPPDATA 'msmpi-native-windows/toolchain-cache'),
    [string]$VSPath = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools',
    [switch]$ListComponents,
    [ValidateNotNullOrEmpty()][ValidatePattern('^[A-Za-z0-9_.+-]+$')][string[]]$Components = @('default'),
    [ValidateSet('components','products','instances')][string]$Inventory,
    [string]$Instance,
    [string]$ProductId,
    [string]$ProductVersion
)
$ErrorActionPreference = 'Stop'
$pins = Get-Content -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) 'toolchain-pins.json') -Raw | ConvertFrom-Json
$pin = $pins.$Component
$installer = Join-Path $Cache $pin.filename
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw "Download missing: $installer" }
if ($pin.bytes -and (Get-Item -LiteralPath $installer).Length -ne $pin.bytes) { throw 'Installer size mismatch' }
if ($pin.sha384 -and (Get-FileHash -LiteralPath $installer -Algorithm SHA384).Hash -ne $pin.sha384) { throw 'Installer SHA384 mismatch' }
if ($pin.sha256 -and (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne $pin.sha256) { throw 'Installer SHA256 mismatch' }
$signature = Get-AuthenticodeSignature -LiteralPath $installer
if ($signature.Status -ne 'Valid') { throw "Installer signature invalid: $($signature.Status)" }
$publisher = if ($Component -eq 'ifx') { 'Intel Corporation' } else { 'Microsoft Corporation' }
if ($signature.SignerCertificate.Subject -notlike "*O=$publisher,*") { throw "Unexpected publisher: $($signature.SignerCertificate.Subject)" }
$logs = Join-Path $Cache ('logs/' + $Component + '-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$arguments = switch ($Component) {
    visual_studio {
        if (Test-Path -LiteralPath (Join-Path $VSPath 'VC/Auxiliary/Build/vcvars64.bat')) {
            @('update', '--installPath', $VSPath, '--quiet', '--wait', '--norestart')
        } else {
            # A hosted Enterprise instance is not this independently selected
            # Build Tools product. Exact installed-file checks still gate use.
            @('--installPath', $VSPath, '--add', 'Microsoft.VisualStudio.Workload.VCTools',
              '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.26100', '--includeRecommended',
              '--quiet', '--wait', '--norestart')
        }
    }
    visual_studio_installer { @('--installerOnly', '--quiet', '--wait', '--norestart') }
    msmpi_runtime { @('-force', '-unattend') }
    msmpi_sdk {
        $msi = $installer
        $installer = Join-Path $env:SystemRoot 'System32/msiexec.exe'
        # Repair reinstalls files even when the SDK is already present.
        $action = if (Test-Path -LiteralPath 'C:\Program Files (x86)\Microsoft SDKs\MPI\Include\mpi.h') { '/fvamus' } else { '/i' }
        @($action, $msi, '/qn', '/norestart', '/L*v', (Join-Path $logs 'sdk.log'))
    }
    default {
        if ($ListComponents -or $Inventory) {
            $query = if ($Inventory) { $Inventory } else { 'components' }
            $queryArguments = @('-s', '-a', ('--list-'+$query))
            if ($Instance) { $queryArguments += @('--instance', $Instance) }
            if ($ProductId) { $queryArguments += @('--product-id', $ProductId) }
            if ($ProductVersion) { $queryArguments += @('--product-ver', $ProductVersion) }
            $queryArguments
        }
        else { @('-s', '-a', '--silent', '--eula', 'accept', '--action', 'install', '--components', ($Components -join ':'), '-p=NEED_VS2022_INTEGRATION=0', '--log-dir', $logs) }
    }
}
$start = [Diagnostics.ProcessStartInfo]::new($installer)
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
foreach ($argument in $arguments) { $start.ArgumentList.Add($argument) }
$process = [Diagnostics.Process]::Start($start)
$stdout = $process.StandardOutput.ReadToEndAsync()
$stderr = $process.StandardError.ReadToEndAsync()
$process.WaitForExit()
[IO.File]::WriteAllText((Join-Path $logs 'stdout.log'), $stdout.GetAwaiter().GetResult())
[IO.File]::WriteAllText((Join-Path $logs 'stderr.log'), $stderr.GetAwaiter().GetResult())
$record = [ordered]@{
    component = $Component; requestedVersion = $pin.version; utc = [DateTime]::UtcNow.ToString('o')
    installer = $pin.filename; sha256 = (Get-FileHash -LiteralPath (Join-Path $Cache $pin.filename) -Algorithm SHA256).Hash
    signatureSubject = $signature.SignerCertificate.Subject; signatureStatus = [string]$signature.Status
    arguments = $arguments; exitCode = $process.ExitCode; logs = $logs
}
$record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $logs 'result.json')
$record | ConvertTo-Json -Depth 4
if ($process.ExitCode -notin @(0, 3010)) { throw "Installer failed with exit $($process.ExitCode); see $logs" }
if ($process.ExitCode -eq 3010) { Write-Warning 'Installer requests a reboot. No reboot performed; validate tools before building.' }

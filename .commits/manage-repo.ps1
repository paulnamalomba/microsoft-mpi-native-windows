# microsoft-mpi-native-windows repository operations. Build acceptance owns the version counter.
param(
    [ValidateSet('list','status','branch','add','commit','push','tag','release','help')]
    [string]$Action = 'list',
    [string]$Version = '',
    [string]$Branch = '',
    [string]$Message = '',
    [string]$Remote = 'origin',
    [switch]$Force,
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$script:RepoRoot = Split-Path $PSScriptRoot -Parent
$script:VersionFile = 'MSMPI_INTERNAL_VERSION'
$script:ExpectedRepository = 'github.com/paulnamalomba/microsoft-mpi-native-windows'
$script:TagPrefix = 'native/v'
$script:Product = 'msmpi-native-windows'
$script:GitExe = (Get-Command git -ErrorAction Stop).Source

function Invoke-Git {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $output = & $script:GitExe -C $script:RepoRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) { throw ($output | Out-String) }
    return $output
}

function Confirm-Operation {
    param([string]$Description)
    if ($Yes) { return }
    if ((Read-Host "$Description [y/N]") -notmatch '^[yY]$') { throw 'Operation cancelled' }
}

function Assert-Branch {
    $current = Invoke-Git -Arguments @('symbolic-ref','--quiet','--short','HEAD') -AllowFailure
    if ($LASTEXITCODE -ne 0) {
        throw 'Detached HEAD: create a branch with -Action branch -Branch <name> before changing or publishing the repository'
    }
    $current = ($current | Out-String).Trim()
    if ($Branch -and $Branch -ne $current) { throw "Current branch differs from -Branch $Branch" }
    return $current
}

function Assert-Remote {
    $urls = @(Invoke-Git -Arguments @('remote','get-url','--push','--all',$Remote))
    if ($urls.Count -ne 1) { throw 'Exactly one owned push URL is required' }
    $normalized = $urls[0].ToString().Trim() -replace '^https://','' -replace '^ssh://git@','' -replace '^git@github.com:','github.com/' -replace '^git@gitlab.com:','gitlab.com/' -replace '\.git/?$',''
    if ($normalized.TrimEnd('/') -ne $script:ExpectedRepository) { throw 'Remote is not the owned product repository' }
}

function Assert-Publishable {
    $recordFile = Join-Path $PSScriptRoot "builds/$script:AcceptedVersion.json"
    if (-not (Test-Path -LiteralPath $recordFile)) { throw 'No accepted build evidence for this version' }
    $record = Get-Content -LiteralPath $recordFile -Raw | ConvertFrom-Json
    if ($record.accepted -ne $script:AcceptedVersion -or $record.status -ne 'passed' -or -not $record.artifacts) {
        throw 'Build evidence does not identify a verified artifact'
    }
    if (-not (Test-Path -LiteralPath $script:FamilyNote)) { throw 'Missing family note' }
    if ((Get-Content -LiteralPath $script:FamilyNote -Raw) -notmatch [regex]::Escape($script:AcceptedVersion)) {
        throw 'Family note does not record this accepted version'
    }
    if ((Invoke-Git -Arguments @('status','--porcelain','--untracked-files=no') | Out-String).Trim()) {
        throw 'Commit tracked changes before tagging or releasing'
    }
}

function Push-Branch {
    Assert-Remote
    $current = Assert-Branch
    Confirm-Operation "Push $current to $script:ExpectedRepository"
    $arguments = @('push','--set-upstream',$Remote,$current)
    if ($Force) { $arguments += '--force-with-lease' }
    Invoke-Git -Arguments $arguments | Write-Output
}

function Create-Tag {
    Assert-Remote
    Assert-Publishable
    $tag = "$script:TagPrefix$script:AcceptedVersion"
    $local = Invoke-Git -Arguments @('tag','--list',$tag)
    $remoteTag = Invoke-Git -Arguments @('ls-remote','--tags',$Remote,"refs/tags/$tag","refs/tags/$tag^{}")
    if ($local -or $remoteTag) { throw "Tag $tag already exists; build identities and tag annotations are immutable" }
    Confirm-Operation "Create and publish $tag with its family note snapshot"
    Invoke-Git -Arguments @('tag','-a',$tag,'-F',$script:FamilyNote) | Write-Output
    Invoke-Git -Arguments @('push',$Remote,"refs/tags/$tag") | Write-Output
}

function Commit-Changes {
    $staged = Invoke-Git -Arguments @('diff','--cached','--name-only')
    if (-not $staged) { throw 'Nothing staged' }
    $summary = $Message
    if (-not $summary) { $summary = "update $(@($staged).Count) tracked paths" }
    if ($summary -match '[\r\n]') { throw 'Commit summary must be one line' }
    Invoke-Git -Arguments @('commit','-m',"$script:Product $script:AcceptedVersion`: $summary") | Write-Output
}

try {
    if ($Action -eq 'help') {
        Write-Output 'manage-repo.ps1 [-Action list|status|branch|add|commit|push|tag|release] [-Version accepted-version] [-Branch name] [-Message summary] [-Remote origin] [-Force] [-Yes]'
        exit 0
    }
    $null = Invoke-Git -Arguments @('rev-parse','--show-toplevel')
    $script:AcceptedVersion = (Get-Content -LiteralPath (Join-Path $script:RepoRoot $script:VersionFile) -Raw).Trim()
    if ($script:AcceptedVersion -notmatch '^(0|[1-9]\d*)\.([0-9])\.([0-9])\.([0-9])$') {
        throw 'Invalid canonical bounded four-part version'
    }
    if ($Version -and $Version -ne $script:AcceptedVersion) { throw "-Version must equal $script:VersionFile ($script:AcceptedVersion)" }
    $family = (($script:AcceptedVersion -split '\.')[0..2] -join '.')
    $script:FamilyNote = Join-Path $PSScriptRoot "$family.txt"
    if ($Action -in @('add','commit','push','tag','release')) { $null = Assert-Branch }
    switch ($Action) {
        'list' {
            Write-Output "$script:Product $script:AcceptedVersion"
            Write-Output "Version source: $script:VersionFile; family note: .commits/$family.txt"
            Invoke-Git -Arguments @('status','--short') | Write-Output
        }
        'status' { Invoke-Git -Arguments @('status','--short') | Write-Output }
        'branch' {
            if (-not $Branch) { throw '-Branch is required' }
            $null = Invoke-Git -Arguments @('check-ref-format','--branch',$Branch)
            Invoke-Git -Arguments @('switch','-c',$Branch) | Write-Output
        }
        'add' {
            Invoke-Git -Arguments @('status','--short') | Write-Output
            Confirm-Operation 'Stage the displayed changes with git add -A'
            Invoke-Git -Arguments @('add','-A') | Write-Output
        }
        'commit' { Commit-Changes }
        'push' { Push-Branch }
        'tag' { Create-Tag }
        'release' {
            Assert-Remote
            if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "builds/$script:AcceptedVersion.json"))) {
                throw 'Release requires an accepted build record'
            }
            Confirm-Operation "Stage, commit and publish $script:Product $script:AcceptedVersion to its owned repository"
            $Yes = $true
            Invoke-Git -Arguments @('add','-A') | Write-Output
            if (Invoke-Git -Arguments @('diff','--cached','--name-only')) { Commit-Changes }
            Push-Branch
            Create-Tag
            $tag = "$script:TagPrefix$script:AcceptedVersion"
            $repository = $script:ExpectedRepository.Substring('github.com/'.Length)
            & gh release create $tag --repo $repository --title "$script:Product $script:AcceptedVersion" --notes-file $script:FamilyNote
            if ($LASTEXITCODE -ne 0) { throw 'GitHub release creation failed' }
        }
    }
    exit 0
} catch {
    Write-Error -Message $_ -ErrorAction Continue
    exit 1
}

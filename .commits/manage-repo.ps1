# microsoft-mpi-native-windows repository operations. Build acceptance owns the version counter.
param(
    [ValidateSet('list','status','branch','add','commit','push','tag','release','check','help')]
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
# <breaking>.<major 0-9>.<minor 0-9>.<build 0-99>, ASCII digits, no zero padding (AGENTS.md). \z refuses a trailing newline.
$script:VersionPattern = '\A(0|[1-9][0-9]*)\.([0-9])\.([0-9])\.(0|[1-9][0-9]?)\z'
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

# True when the note text records exactly this version: 0.1.2.1 is not found inside 0.1.2.10 or 10.1.2.1.
function Test-NoteRecordsVersion {
    param([string]$NoteText, [string]$Text)
    return ($NoteText -cmatch ('(?<![0-9.])' + [regex]::Escape($Text) + '(?![0-9])'))
}

function Test-VersionText {
    param([string]$Text)
    return ($Text -cmatch $script:VersionPattern)
}

# Self-check of the version rule and the version file. It writes nothing and uses no network.
# Non-ASCII digits and line ends are built from code points so this file stays ASCII.
function Invoke-SelfCheck {
    $failed = 0
    $cases = @()
    foreach ($text in @('10.1.4.0', '10.1.4.2', '10.1.4.10', '10.1.4.99', '0.0.0.0', '11.9.9.99')) { $cases += ,@($text, $true) }
    foreach ($text in @('10.1.4.100', '10.10.4.0', '10.1.10.0', '10.1.4.010', '10.1.4.00', '10.1.4.01', '010.1.4.0', '10.1.4', '10.1.4.2.1', '10.1.4.x', ' 10.1.4.2', '',
                        "10.1.4.$([char]0x0660)", "10.1.4.1$([char]0x0662)", "10.1.$([char]0x0664).2", "10.$([char]0x0661).4.2", "1$([char]0x0660).1.4.2", "10.1.4.$([char]0xFF12)",
                        "10.1.4.2`n", "10.1.4.2`r`n", "`n10.1.4.2")) { $cases += ,@($text, $false) }
    foreach ($case in $cases) {
        $shown = -join ($case[0].ToCharArray() | ForEach-Object { if ([int]$_ -lt 32 -or [int]$_ -gt 126) { 'U+{0:X4}' -f [int]$_ } else { [string]$_ } })
        $verb = if ($case[1]) { 'accepts' } else { 'rejects' }
        if ((Test-VersionText $case[0]) -eq $case[1]) { Write-Output "ok    version $verb '$shown'" }
        else { Write-Output "FAIL  version $verb '$shown'"; $failed++ }
    }
    # Family-note lookup is bounded: 10.1.4.2 is not found inside 10.1.4.20 or 110.1.4.2.
    $notes = @(@("## 10.1.4.2`nbuilt", $true), @('tag native/v10.1.4.2.', $true), @('## 10.1.4.20', $false), @('## 110.1.4.2', $false), @('## 10.1.4.29 and 210.1.4.2', $false))
    foreach ($note in $notes) {
        $shown = $note[0] -replace "`n", ' '
        $verb = if ($note[1]) { 'records' } else { 'does not record' }
        if ((Test-NoteRecordsVersion $note[0] '10.1.4.2') -eq $note[1]) { Write-Output "ok    note '$shown' $verb 10.1.4.2" }
        else { Write-Output "FAIL  note '$shown' $verb 10.1.4.2"; $failed++ }
    }
    if (Test-VersionText $script:AcceptedVersion) { Write-Output "ok    $script:VersionFile holds a valid version" }
    else { Write-Output "FAIL  $script:VersionFile holds a valid version"; $failed++ }
    Write-Output "checks: $($cases.Count + $notes.Count + 1 - $failed) passed, $failed failed"
    if ($failed) { throw "$failed self-check(s) failed" }
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
    if (-not (Test-NoteRecordsVersion (Get-Content -LiteralPath $script:FamilyNote -Raw) $script:AcceptedVersion)) {
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
        Write-Output 'manage-repo.ps1 [-Action list|status|branch|add|commit|push|tag|release|check] [-Version accepted-version] [-Branch name] [-Message summary] [-Remote origin] [-Force] [-Yes]'
        exit 0
    }
    $null = Invoke-Git -Arguments @('rev-parse','--show-toplevel')
    $script:AcceptedVersion = (Get-Content -LiteralPath (Join-Path $script:RepoRoot $script:VersionFile) -Raw).Trim()
    if ($Action -eq 'check') { Invoke-SelfCheck; exit 0 }
    if (-not (Test-VersionText $script:AcceptedVersion)) {
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

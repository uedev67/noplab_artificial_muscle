#Requires -Version 5.1
<#
.SYNOPSIS
    noplab Conda 환경과 PowerShell 단축 명령을 자동으로 설치한다.

.DESCRIPTION
    저장소를 처음 받은 Windows PC에서 한 번 실행한다. Python 3.10 환경을 생성하거나
    업데이트하고, Windows PowerShell 5 및 PowerShell 7 프로필 모두에 noplab 함수를
    등록한다.
#>

$ErrorActionPreference = 'Stop'
$environmentName = 'noplab'
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$environmentFile = Join-Path $projectDirectory 'environment.yml'

function Find-CondaExecutable {
    $command = Get-Command conda.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE 'anaconda3\Scripts\conda.exe'),
        (Join-Path $env:USERPROFILE 'miniconda3\Scripts\conda.exe'),
        'C:\ProgramData\anaconda3\Scripts\conda.exe',
        'C:\ProgramData\miniconda3\Scripts\conda.exe'
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw 'Conda를 찾지 못했습니다. Anaconda 또는 Miniconda를 먼저 설치하세요.'
}

$condaExecutable = Find-CondaExecutable
Write-Host "Conda: $condaExecutable"

# 기존 환경이면 requirements까지 갱신하고, 없으면 새로 만든다.
$environmentList = (& $condaExecutable env list --json | ConvertFrom-Json).envs
$environmentExists = $environmentList | Where-Object {
    (Split-Path -Leaf $_) -eq $environmentName
}

if ($environmentExists) {
    Write-Host "'$environmentName' 환경을 업데이트합니다."
    & $condaExecutable env update --name $environmentName --file $environmentFile --prune
}
else {
    Write-Host "'$environmentName' 환경을 생성합니다."
    & $condaExecutable env create --file $environmentFile
}
if ($LASTEXITCODE -ne 0) {
    throw 'Conda 환경 생성 또는 업데이트에 실패했습니다.'
}

# 로컬 PowerShell 프로필이 실행될 수 있도록 사용자 범위만 설정한다.
try {
    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
}
catch {
    # 이 스크립트 자체를 -ExecutionPolicy Bypass로 실행하면 Process 범위가 더 높은
    # 우선순위를 가져 경고가 발생할 수 있다. 사용자 범위에 기록되었으면 정상이다.
    if ((Get-ExecutionPolicy -Scope CurrentUser) -ne 'RemoteSigned') {
        Write-Warning "실행 정책을 변경하지 못했습니다: $($_.Exception.Message)"
    }
}

$escapedCondaPath = $condaExecutable.Replace("'", "''")
$profileBlock = @"
# >>> noplab project >>>
# setup_windows.ps1이 관리하는 영역입니다.
function global:noplab {
    `$condaExecutable = '$escapedCondaPath'
    (& `$condaExecutable 'shell.powershell' 'hook') | Out-String | Invoke-Expression
    conda activate noplab
}
# <<< noplab project <<<
"@

$documentsDirectory = [Environment]::GetFolderPath('MyDocuments')
$profilePaths = @(
    (Join-Path $documentsDirectory 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1'),
    (Join-Path $documentsDirectory 'PowerShell\Microsoft.PowerShell_profile.ps1')
) | Select-Object -Unique

$startMarker = '# >>> noplab project >>>'
$endMarker = '# <<< noplab project <<<'
$managedPattern = '(?ms)^' + [regex]::Escape($startMarker) +
    '.*?^' + [regex]::Escape($endMarker) + '\s*'

foreach ($profilePath in $profilePaths) {
    $profileDirectory = Split-Path -Parent $profilePath
    New-Item -ItemType Directory -Path $profileDirectory -Force | Out-Null

    $existingContent = ''
    if (Test-Path -LiteralPath $profilePath) {
        $existingContent = Get-Content -LiteralPath $profilePath -Raw -ErrorAction SilentlyContinue
    }
    $existingContent = [regex]::Replace([string]$existingContent, $managedPattern, '').TrimEnd()
    if ($existingContent.Length -gt 0) {
        $newContent = $existingContent + [Environment]::NewLine + [Environment]::NewLine + $profileBlock
    }
    else {
        $newContent = $profileBlock
    }
    Set-Content -LiteralPath $profilePath -Value $newContent -Encoding UTF8
    Write-Host "PowerShell 프로필 설정: $profilePath"
}

Write-Host ''
Write-Host '설정이 완료되었습니다.' -ForegroundColor Green
Write-Host '현재 터미널을 닫고 새 PowerShell을 연 뒤 noplab을 입력하세요.'

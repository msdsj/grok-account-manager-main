<#
.SYNOPSIS
    AI Signuper — Windows 一键准备脚本

.DESCRIPTION
    幂等部署脚本：装依赖（git/uv/Docker Desktop/cloudflared）→ 拉仓库 →
    生成随机密钥 → 起 Sub2API → 准备注册机 .env 与依赖。
    跑完后还有 4 步手动操作（详见结尾打印的指引）。

.NOTES
    建议管理员身份运行：右键 PowerShell → "以管理员身份运行"，再执行：
        Set-ExecutionPolicy -Scope Process Bypass -Force
        .\bootstrap.ps1
    脚本可重复运行：已就绪的步骤会被跳过。

.PARAMETER RepoUrl
    项目 Git 仓库地址，默认 BeastOrange/ai_signuper。

.PARAMETER RepoPath
    本地存放路径，默认 C:\ai_signuper。
#>

[CmdletBinding()]
param(
    [string]$RepoUrl = 'https://github.com/BeastOrange/ai_signuper.git',
    [string]$RepoPath = 'C:\ai_signuper'
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    [!] $msg" -ForegroundColor Yellow }

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Update-EnvPath {
    # 装完工具后，让当前 shell 能立即看到新 PATH（不需要重开 PowerShell）
    $machine = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Install-Tool {
    param([string]$Id, [string]$Cmd)
    if (Test-Cmd $Cmd) {
        Write-OK "$Cmd 已存在"
        return $false
    }
    Write-Host "    安装 $Id ..."
    winget install --id=$Id -e --accept-source-agreements --accept-package-agreements | Out-Null
    return $true
}

function New-HexSecret {
    param([int]$Bytes = 32)
    $buf = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    return -join ($buf | ForEach-Object { '{0:x2}' -f $_ })
}

# ========== 阶段 1：工具链 ==========
Write-Step '阶段 1：安装基础工具（winget）'
$anyInstalled = $false
$anyInstalled = (Install-Tool 'Git.Git'                'git')         -or $anyInstalled
$anyInstalled = (Install-Tool 'astral-sh.uv'           'uv')          -or $anyInstalled
$anyInstalled = (Install-Tool 'Docker.DockerDesktop'   'docker')      -or $anyInstalled
$anyInstalled = (Install-Tool 'Cloudflare.cloudflared' 'cloudflared') -or $anyInstalled

if ($anyInstalled) {
    Update-EnvPath
    Write-Warn 'Docker Desktop 首次安装通常要重启电脑或注销后才能生效。'
    Write-Warn '如果下面阶段 4 报 docker 命令找不到 / 守护进程未启动，请重启后再次运行此脚本。'
}

# ========== 阶段 2：克隆仓库 ==========
Write-Step "阶段 2：克隆/更新仓库到 $RepoPath"
if (Test-Path (Join-Path $RepoPath '.git')) {
    Push-Location $RepoPath
    git pull --quiet
    git submodule update --init --recursive --quiet
    Pop-Location
    Write-OK '仓库已存在，已 pull + submodule update'
} else {
    git clone --recurse-submodules $RepoUrl $RepoPath
    Write-OK '仓库已克隆（含 submodule）'
}

# ========== 阶段 3：生成 sub2api .env 随机密钥 ==========
Write-Step '阶段 3：生成 sub2api/deploy/.env'
$subEnvDir  = Join-Path $RepoPath 'sub2api\deploy'
$subEnv     = Join-Path $subEnvDir '.env'
$subSample  = Join-Path $subEnvDir '.env.example'

if (Test-Path $subEnv) {
    Write-OK 'sub2api/.env 已存在，跳过（如需重置请手动删除）'
} else {
    if (-not (Test-Path $subSample)) {
        throw "找不到 $subSample —— submodule 可能没完整 clone"
    }
    $content = Get-Content $subSample
    $content = $content -replace '^POSTGRES_PASSWORD=.*',   "POSTGRES_PASSWORD=$(New-HexSecret)"
    $content = $content -replace '^JWT_SECRET=.*',          "JWT_SECRET=$(New-HexSecret)"
    $content = $content -replace '^TOTP_ENCRYPTION_KEY=.*', "TOTP_ENCRYPTION_KEY=$(New-HexSecret)"
    Set-Content -Path $subEnv -Value $content -Encoding ASCII
    Write-OK "已生成随机密钥到 $subEnv"
}

# ========== 阶段 4：启动 Sub2API ==========
Write-Step '阶段 4：启动 Sub2API（docker compose）'
$dockerReady = $false
try {
    docker info 2>$null | Out-Null
    $dockerReady = $LASTEXITCODE -eq 0
} catch { $dockerReady = $false }

if (-not $dockerReady) {
    Write-Warn 'Docker 守护进程未运行。请打开 Docker Desktop，等待状态变成 Running，再重新运行此脚本。'
    Write-Warn '本阶段已跳过，后续阶段继续。'
} else {
    Push-Location $subEnvDir
    docker compose -f docker-compose.local.yml up -d
    Pop-Location
    Write-OK 'Sub2API 容器已启动；http://localhost:8080 进入 Setup Wizard'
}

# ========== 阶段 5：注册机 .env + uv sync ==========
Write-Step '阶段 5：注册机环境'
$rootEnv    = Join-Path $RepoPath '.env'
$rootSample = Join-Path $RepoPath '.env.example'

if (-not (Test-Path $rootEnv)) {
    Copy-Item $rootSample $rootEnv
    Write-OK "已创建 $rootEnv（稍后填 SUB2API_ADMIN_API_KEY）"
} else {
    Write-OK '.env 已存在'
}

Push-Location $RepoPath
uv sync
Pop-Location
Write-OK 'uv 依赖就绪'

# ========== 完工指引 ==========
Write-Host @"

============================================================
 自动部分跑完。下面 4 步要手动：
============================================================

  1. 打开浏览器 http://localhost:8080
     完成 Setup Wizard，建管理员账号

  2. 登入 → /admin/settings → 生成 Admin API Key → 复制

  3. 用记事本编辑：
        $rootEnv
     填入：
        SUB2API_BASE_URL=http://localhost:8080
        SUB2API_ADMIN_API_KEY=<刚才复制的 key>

  4. 单轮端到端验证：
        cd $RepoPath
        uv run python -m ai_signuper grok --count 1 --sink sub2api
     验证后台账号列表多一条 platform=grok 的账号

============================================================
 可选：暴露到公网（Cloudflare Tunnel）
============================================================

  cloudflared tunnel login                                   # 浏览器登录 Cloudflare
  cloudflared tunnel create sub2api
  cloudflared tunnel route dns sub2api <你的域名>             # 域名要先托管在 Cloudflare
  cloudflared tunnel run --url http://localhost:8080 sub2api

  确认通了之后，注册成 Windows 服务开机自启：
      cloudflared service install

  没自己的域名？想先 demo 看看？跳过上面，直接：
      cloudflared tunnel --url http://localhost:8080
  会给你一个临时 *.trycloudflare.com 域名，进程关了就失效

============================================================
"@ -ForegroundColor Green

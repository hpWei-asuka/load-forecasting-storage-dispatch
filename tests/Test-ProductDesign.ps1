$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$documentPath = Join-Path $repoRoot '产品项目设计文档.md'
$content = Get-Content -LiteralPath $documentPath -Raw -Encoding UTF8

if ([string]::IsNullOrWhiteSpace($content) -or $content.Contains([char]0xFFFD)) {
    throw '设计文档为空或包含损坏的编码字符。'
}

$requiredSections = @(
    '产品目标', '使用流程', '功能与输出', '数据与模型边界',
    '当前验证结果', '交付与限制'
)
$sections = [regex]::Matches($content, '(?ms)^## \d+\. ([^\r\n]+)\r?\n(.*?)(?=^## |\z)')
foreach ($required in $requiredSections) {
    $section = @($sections | Where-Object { $_.Groups[1].Value -eq $required })
    if ($section.Count -ne 1 -or $section[0].Groups[2].Value.Trim().Length -lt 20) {
        throw "设计文档缺少有效章节：$required"
    }
}

if ($sections.Count -lt 5 -or $sections.Count -gt 7) { throw '产品说明应保持五至七个短章节。' }
for ($i = 0; $i -lt $sections.Count; $i++) {
    if (-not $content.Contains("## $($i + 1). $($sections[$i].Groups[1].Value)")) {
        throw '产品章节编号必须连续。'
    }
}

# 检查产品边界，避免省略失败处理或将运行费用写成投资收益。
$requiredConcepts = @{
    '命令行使用方式' = '命令行'
    '公开数据来源' = 'UCI.*Household'
    '功率与电量单位' = 'kW.*kWh'
    '因果填补上限' = '前向填补.*5 分钟'
    '原始观测完整性' = '60 个原始有效分钟'
    '电池终端条件' = '初末电量相等'
    '共同日期与覆盖率' = '共同有效日期.*覆盖率|共同有效日期[\s\S]*覆盖率'
    '失败费用留空' = '不可执行[\s\S]*费用留空'
    '费用适用范围' = '不代表真实账单或投资回报'
}
foreach ($name in $requiredConcepts.Keys) {
    if ($content -notmatch $requiredConcepts[$name]) {
        throw "产品说明缺少内容：$name"
    }
}
if ($content -match '规划稿|尚未实现业务程序') { throw '产品说明仍含过期的实现状态。' }

# 检查表格列数，防止 Markdown 功能表或阶段表被编辑破坏。
$tableWidth = $null
foreach ($line in ($content -split '\r?\n')) {
    if ($line.Trim().StartsWith('|')) {
        $width = ($line.Trim().Trim('|') -split '\|').Count
        if ($null -eq $tableWidth) { $tableWidth = $width }
        if ($width -ne $tableWidth) { throw "表格列数不一致：$line" }
    } else {
        $tableWidth = $null
    }
}

# 外部链接检查 URL 语法；相对链接检查仓库中的目标文件。
foreach ($link in [regex]::Matches($content, '\[[^\]]+\]\(([^)]+)\)')) {
    $target = $link.Groups[1].Value
    if ($target -match '^https?://') {
        $uri = $null
        if (-not [Uri]::TryCreate($target, [UriKind]::Absolute, [ref]$uri) -or -not $uri.Host) {
            throw "无效的外部链接：$target"
        }
    } elseif (-not $target.StartsWith('#')) {
        $relativePath = ($target -split '#', 2)[0]
        if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $relativePath))) {
            throw "文档链接目标不存在：$target"
        }
    }
}

Write-Output 'PASS: 产品说明的功能、数据与模型边界、失败处理及文档格式检查通过。'

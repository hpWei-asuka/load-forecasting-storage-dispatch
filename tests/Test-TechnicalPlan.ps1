$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$documentPath = Join-Path $repoRoot 'docs/技术方案.md'
$content = Get-Content -LiteralPath $documentPath -Raw -Encoding UTF8

if ([string]::IsNullOrWhiteSpace($content) -or $content.Contains([char]0xFFFD)) {
    throw '技术方案为空或包含损坏的编码字符。'
}

# 检查可阅读的文档结构，不依赖各段落的具体措辞。
$sections = [regex]::Matches($content, '(?ms)^## (\d+)\. ([^\r\n]+)\r?\n(.*?)(?=^## |\z)')
if ($sections.Count -eq 0) { throw '技术方案缺少二级章节。' }
for ($i = 0; $i -lt $sections.Count; $i++) {
    if ([int]$sections[$i].Groups[1].Value -ne ($i + 1)) {
        throw '技术方案章节编号必须连续。'
    }
    if ($sections[$i].Groups[3].Value.Trim().Length -lt 20) {
        throw "技术方案存在空章节：$($sections[$i].Groups[2].Value)"
    }
}

$fences = [regex]::Matches($content, '(?m)^```[^\r\n]*\r?$')
if ($fences.Count % 2 -ne 0) { throw '技术方案存在未闭合的代码块。' }

$tableWidth = $null
foreach ($line in ($content -split '\r?\n')) {
    if ($line.Trim().StartsWith('|')) {
        $width = ($line.Trim().Trim('|') -split '\|').Count
        if ($null -eq $tableWidth) { $tableWidth = $width }
        if ($width -ne $tableWidth) { throw "技术方案表格列数不一致：$line" }
    } else {
        $tableWidth = $null
    }
}

$documentDirectory = Split-Path -Parent $documentPath
foreach ($link in [regex]::Matches($content, '\[[^\]]+\]\(([^)]+)\)')) {
    $target = $link.Groups[1].Value
    if ($target -match '^https?://') {
        $uri = $null
        if (-not [Uri]::TryCreate($target, [UriKind]::Absolute, [ref]$uri) -or -not $uri.Host) {
            throw "技术方案包含无效 URL：$target"
        }
    } elseif (-not $target.StartsWith('#')) {
        $relativePath = ($target -split '#', 2)[0]
        if (-not (Test-Path -LiteralPath (Join-Path $documentDirectory $relativePath))) {
            throw "技术方案链接目标不存在：$target"
        }
    }
}

# 解析文档示例，核对默认配置及设备边界。
$tomlBlocks = [regex]::Matches($content, '(?ms)^```toml\r?\n(.*?)^```\s*$')
if ($tomlBlocks.Count -ne 1) { throw '技术方案应提供一份完整 TOML 示例。' }

$tomlParser = @'
import json
from pathlib import Path
import sys
import tomllib

config = tomllib.loads(sys.stdin.read())
default = tomllib.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
assert config == default, '技术方案示例与configs/example.toml不一致'
print(json.dumps(config))
'@
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$parsedOutput = $tomlBlocks[0].Groups[1].Value | & python -c $tomlParser (Join-Path $repoRoot 'configs/example.toml')
if ($LASTEXITCODE -ne 0) { throw '技术方案 TOML 示例无法使用 Python tomllib 解析。' }
$config = ($parsedOutput -join "`n") | ConvertFrom-Json

foreach ($section in @('experiment', 'data', 'tariff', 'battery', 'fixed_rule', 'solver', 'validation')) {
    if ($null -eq $config.PSObject.Properties[$section]) {
        throw "TOML 示例缺少配置组：$section"
    }
}

$testStart = [DateTime]::ParseExact($config.experiment.test_start, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
$testEnd = [DateTime]::ParseExact($config.experiment.test_end, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
if ($testStart -gt $testEnd) { throw '示例测试起始日期晚于终止日期。' }

$prices = @($config.tariff.hourly_prices)
if ($prices.Count -ne 24 -or @($prices | Where-Object { $_ -lt 0 }).Count -gt 0) {
    throw '示例必须提供 24 个非负小时电价。'
}

$battery = $config.battery
if ($battery.capacity_kwh -le 0 -or
    $battery.min_energy_kwh -lt 0 -or
    $battery.min_energy_kwh -gt $battery.initial_energy_kwh -or
    $battery.initial_energy_kwh -gt $battery.max_energy_kwh -or
    $battery.max_energy_kwh -gt $battery.capacity_kwh -or
    $battery.max_charge_kw -le 0 -or $battery.max_discharge_kw -le 0) {
    throw '示例电池容量、电量或功率边界不合法。'
}
foreach ($efficiency in @($battery.charge_efficiency, $battery.discharge_efficiency)) {
    if ($efficiency -le 0 -or $efficiency -gt 1) { throw '示例充放电效率必须处于 (0,1]。' }
}

$chargeHours = @($config.fixed_rule.charge_hours)
$dischargeHours = @($config.fixed_rule.discharge_hours)
if ($chargeHours.Count -eq 0 -or $dischargeHours.Count -eq 0) {
    throw '示例固定规则必须包含充电和放电窗口。'
}
$allHours = @($chargeHours) + @($dischargeHours)
if (@($allHours | Where-Object { $_ -lt 0 -or $_ -gt 23 -or $_ -ne [math]::Floor($_) }).Count -gt 0 -or
    @($allHours | Sort-Object -Unique).Count -ne $allHours.Count) {
    throw '示例窗口必须使用 0 到 23 的整数小时，且不能重复或重叠。'
}
$lastCharge = ($chargeHours | Measure-Object -Maximum).Maximum
$firstDischarge = ($dischargeHours | Measure-Object -Minimum).Minimum
if ($lastCharge -ge $firstDischarge) { throw '示例全部充电时段必须早于全部放电时段。' }
$highestChargePrice = ($chargeHours | ForEach-Object { $prices[$_] } | Measure-Object -Maximum).Maximum
$lowestDischargePrice = ($dischargeHours | ForEach-Object { $prices[$_] } | Measure-Object -Minimum).Minimum
if ($highestChargePrice -ge $lowestDischargePrice) {
    throw '示例低价充电与高价放电窗口不符合示例电价。'
}

if ($config.data.history_ffill_limit_minutes -lt 0 -or
    $config.data.history_ffill_limit_minutes -gt 5 -or
    $config.data.evaluation_require_observed -ne $true) {
    throw '示例必须限制历史填补，并仅使用原始观测进行正式评估。'
}
if ($config.solver.time_limit_seconds -le 0 -or $config.solver.threads -ne 1 -or
    $config.validation.absolute_tolerance -le 0 -or $config.validation.absolute_tolerance -ge 1) {
    throw '示例求解时限、线程数或检查容差不合法。'
}

if ($content -notmatch 'provenance\.py') { throw '技术方案遗漏来源追踪模块。' }
if ($content -match '尚未实现业务程序|后续实现目标') { throw '技术方案仍含过期的实现状态。' }

Write-Output 'PASS: 技术方案的编码、章节、表格、链接及 TOML 示例一致性检查通过。'

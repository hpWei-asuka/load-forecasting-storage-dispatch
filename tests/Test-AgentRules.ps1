$ErrorActionPreference = 'Stop'
$content = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../AGENT.md') -Raw -Encoding UTF8
$requiredRules = @(
    '①每次改动完成后，都必须创建一个对应的Git commit，以便后续追踪和回滚',
    '②每次改动后，都必须编写或更新相关测试，并在交付给用户前，确保所有测试也验证全部通过'
)
foreach ($rule in $requiredRules) {
    if (-not $content.Contains($rule)) {
        throw "AGENT.md 缺少规则：$rule"
    }
}
Write-Output 'PASS: AGENT.md 包含全部要求的规则。'

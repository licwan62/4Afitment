$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundledNode = "C:\Users\hzwlc\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
$BundledModules = "C:\Users\hzwlc\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
$BundledPnpmModules = Join-Path $BundledModules ".pnpm\node_modules"
$BundledPlaywright = Join-Path $BundledModules ".pnpm\playwright@1.60.0\node_modules\playwright"
$ProjectNodeModules = Join-Path $ProjectRoot "node_modules"

if (Test-Path $BundledNode) {
  if (Test-Path $BundledModules) {
    if ((Test-Path $ProjectNodeModules) -and ((Get-Item $ProjectNodeModules).LinkType -eq "Junction")) {
      [System.IO.Directory]::Delete($ProjectNodeModules)
    }

    New-Item -ItemType Directory -Force -Path $ProjectNodeModules | Out-Null

    $Packages = @(
      @{ Name = "playwright"; Source = $BundledPlaywright },
      @{ Name = "playwright-core"; Source = Join-Path $BundledPnpmModules "playwright-core" }
    )

    foreach ($Package in $Packages) {
      $Target = Join-Path $ProjectNodeModules $Package.Name
      if ((Test-Path $Package.Source) -and -not (Test-Path $Target)) {
        New-Item -ItemType Junction -Path $Target -Target $Package.Source | Out-Null
      }
    }
  }

  Push-Location $ProjectRoot
  try {
    & $BundledNode @args
  } finally {
    Pop-Location
  }
  exit $LASTEXITCODE
}

Push-Location $ProjectRoot
try {
  node @args
} finally {
  Pop-Location
}

# run_backtest_provisional.ps1 - 잠정 백테스트 원클릭 실행
# INDEX_ASOF=2026-07-23 (위원회 확정 전 잠정값 - 확정 시 아래 한 줄만 바꿔 재실행)
# 실행 위치: 레포 루트 (이 스크립트가 있는 폴더)
# 실행 방법: powershell -ExecutionPolicy Bypass -File .\run_backtest_provisional.ps1

Set-Location -Path $PSScriptRoot

# 0) 위치·입력 확인
if (-not (Test-Path ".\analysis\run_backtest.py")) {
    Write-Host "[FAIL] analysis\run_backtest.py 가 없습니다. 레포 루트가 아닙니다." ; exit 1 }
if (-not (Test-Path ".\data\verdict_ledger.csv")) {
    Write-Host "[FAIL] data\verdict_ledger.csv 가 없습니다. 이 폴더는 최신 레포 사본이 아닙니다." ; exit 1 }

# 1) 코드 상태 고정 (미커밋 변경이 있으면 커밋)
git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "chore(part3): pre-backtest auto-commit (state freeze)"
}
$commit = (git rev-parse HEAD)
Write-Host "[코드 커밋] $commit"

# 2) 환경 기록 + 재현 기준일(잠정)
New-Item -ItemType Directory -Force -Path out | Out-Null
New-Item -ItemType Directory -Force -Path out\backtest | Out-Null
pip freeze > out\env_freeze_20260729.txt
$env:INDEX_ASOF = "2026-07-23"
Write-Host "[INDEX_ASOF] $env:INDEX_ASOF (잠정 - 위원회 확정 전)"

# 3) 필요 시점 표 (네트워크 불필요)
python analysis\build_pit_snapshots.py --ledger data\verdict_ledger.csv --template-only | Tee-Object -FilePath out\01_template_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 템플릿 단계 실패 - out\01_template_log.txt 공유" ; exit 1 }

# 4) PIT 스냅샷 13회분 생성 (KRX 조회 - 수십 분 걸릴 수 있음)
python analysis\build_pit_snapshots.py --ledger data\verdict_ledger.csv --out data\snapshots --code-commit $commit | Tee-Object -FilePath out\02_snapshot_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 스냅샷 생성 실패 - out\02_snapshot_log.txt 공유" ; exit 1 }

# 5) 커버리지 게이트
python analysis\run_backtest.py --snapshots data\snapshots --prices-cache out\px.csv --coverage-only --out out\backtest | Tee-Object -FilePath out\03_coverage_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 커버리지 단계 실패 - out\03_coverage_log.txt 공유" ; exit 1 }

# 6) 본 실행 - PR 헤드라인 · 버퍼 4안 · 계보 강제 · 벤치마크는 잠정 제외
python analysis\run_backtest.py --snapshots data\snapshots --prices-cache out\px.csv --policy all --require-lineage --mode pr --no-benchmark --out out\backtest | Tee-Object -FilePath out\04_backtest_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 본 실행 실패 - out\03,04 로그와 콘솔 오류를 공유" ; exit 1 }

Write-Host ""
Write-Host "== 완료 =="
Write-Host "산출물: out\backtest\  (index_level.csv, change_history.csv, event_log.csv, theme_relevance.csv, policy_comparison.csv 등)"
Write-Host "로그  : out\01~04_*_log.txt"
Write-Host "다음  : 로그 4개와 out\backtest 폴더를 Claude 세션에 공유 - 검증·등록 단계 진행"

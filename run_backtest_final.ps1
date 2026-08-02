# run_backtest_final.ps1 - 확정(FINAL) 백테스트 원클릭 실행
# 선행: data\final_run_gates.json 에 게이트 5건(value/by/on) 전부 기입
#       (양식: data\final_run_gates_TEMPLATE.json)
#       게이트·코드 변경을 검토·커밋해 git status --short 가 비어 있어야 함
# 사용: powershell -ExecutionPolicy Bypass -File .\run_backtest_final.ps1 -IndexAsof 2026-07-23
# 옵션: -ReuseSnapshots (기존 data\snapshots 재사용 - ASOF 를 바꿨다면 쓰지 말 것)
#       -NoBenchmark    (벤치마크 제외 - 게이트 d2에 제외 결정이 기록된 경우만)

param(
    [Parameter(Mandatory=$true)][string]$IndexAsof,
    [switch]$ReuseSnapshots,
    [switch]$NoBenchmark
)
Set-Location -Path $PSScriptRoot

# 0) 게이트 사전점검 (fail-closed - 미기입이면 여기서 끝)
python analysis\make_backtest_manifest.py --final --index-asof $IndexAsof --gates data\final_run_gates.json --check-gates
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 게이트 미기입 - data\final_run_gates.json 을 채우세요" ; exit 1 }

# 1) 코드 상태 고정 - 사용자의 다른 작업을 자동 git add 하지 않는다
$dirty = @(git status --porcelain)
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] Git 상태 확인 실패" ; exit 1 }
if ($dirty.Count -gt 0) {
    Write-Host "[중단] 작업트리가 깨끗하지 않습니다. 게이트·코드 변경을 먼저 검토·커밋하세요."
    $dirty | ForEach-Object { Write-Host "  $_" }
    exit 1
}
$commit = (git rev-parse HEAD)
if ($LASTEXITCODE -ne 0 -or -not $commit) { Write-Host "[중단] Git HEAD 확인 실패" ; exit 1 }
Write-Host "[코드 커밋] $commit"
New-Item -ItemType Directory -Force -Path out\backtest | Out-Null
$freeze = @(python -m pip freeze)
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 환경 패키지 목록 생성 실패" ; exit 1 }
$freeze | Set-Content -Path out\env_freeze_final.txt -Encoding utf8
$env:INDEX_ASOF = $IndexAsof
Write-Host "[INDEX_ASOF] $IndexAsof (위원회 확정)"

# 2) PIT 스냅샷
if (-not $ReuseSnapshots) {
    if (-not (Test-Path -LiteralPath data\market_facts)) {
        Write-Host "[중단] data\market_facts 누락 - KRX 13회차 공식 팩트를 먼저 생성하세요"
        exit 1
    }
    python analysis\build_pit_snapshots.py --ledger data\verdict_ledger.csv --out data\snapshots --code-commit $commit --market-facts-dir data\market_facts | Tee-Object -FilePath out\f2_snapshot_log.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 스냅샷 생성 실패" ; exit 1 }
} else { Write-Host "[스냅샷] 기존 data\snapshots 재사용" }

# 3) 커버리지 게이트
python analysis\run_backtest.py --snapshots data\snapshots --prices-cache out\px.csv --coverage-only --out out\backtest | Tee-Object -FilePath out\f3_coverage_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 커버리지 실패" ; exit 1 }

# 4) 본 실행 (벤치마크: 기본 포함 - benchmark.yaml CONFIRMED 필요)
$benchmarkCache = "out\benchmark_5044_pr_20200615_20260723.csv"
$benchArgs = @()
if ($NoBenchmark) {
    $benchArgs += "--no-benchmark"
} else {
    $benchArgs += @("--benchmark-cache", $benchmarkCache)
}
python analysis\run_backtest.py --snapshots data\snapshots --prices-cache out\px.csv --policy all --require-lineage --mode pr @benchArgs --out out\backtest | Tee-Object -FilePath out\f4_backtest_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 본 실행 실패 - 로그 공유" ; exit 1 }
if (-not $NoBenchmark) {
    if (-not (Test-Path -LiteralPath $benchmarkCache)) {
        Write-Host "[중단] 벤치마크 캐시가 생성되지 않았습니다: $benchmarkCache"
        exit 1
    }
    # pandas가 Windows에서 쓴 CRLF/BOM을 Git 계약(LF, no BOM)으로 고정한다.
    $benchmarkText = [IO.File]::ReadAllText($benchmarkCache)
    $benchmarkText = $benchmarkText.Replace("`r`n", "`n").Replace("`r", "`n")
    [IO.File]::WriteAllText($benchmarkCache, $benchmarkText,
        [Text.UTF8Encoding]::new($false))
}

# 4b) 구성·버킷 진단 - 본 실행 산출물을 읽어 '규정 대비 실제'를 계량한다.
#     본 실행 뒤·매니페스트 앞에 두는 이유: 산출물(out\backtest)에 CSV를 더하고,
#     그 CSV까지 7단계 동결 커밋에 포함시키기 위해서다. 두 스크립트 모두 지수
#     계열을 바꾸지 않는다(읽기 전용 진단) - 헤드라인 수치는 4단계가 확정한다.
python analysis\composition_timeline.py --snapshots data\snapshots --index-level out\backtest\index_level.csv --out out\backtest | Tee-Object -FilePath out\f4b_composition_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 구성 이력 산출 실패" ; exit 1 }
python analysis\bucket_drift.py --snapshots data\snapshots --prices-cache out\px.csv --out out\backtest | Tee-Object -FilePath out\f4c_bucket_log.txt
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 버킷 드리프트 산출 실패 - 재현 경로가 엔진과 어긋났을 수 있음" ; exit 1 }

# 5) FINAL 매니페스트 (게이트 재검증 포함)
$manifestBenchArgs = @()
if (-not $NoBenchmark) { $manifestBenchArgs += @("--benchmark-cache", $benchmarkCache) }
python analysis\make_backtest_manifest.py --final --index-asof $IndexAsof --gates data\final_run_gates.json @manifestBenchArgs
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] FINAL 매니페스트 생성 실패" ; exit 1 }

# 6a) 클레임 검증 + FACTSHEET 재생성 - 테스트보다 **먼저** 돌린다.
#     순서가 중요하다. tests/test_claims.py 는 'FACTSHEET 저장본이 등록부보다
#     낡지 않았는가'를 검사하는데, 클레임을 새로 등록하면 재생성 전까지는 반드시
#     낡은 상태다. 테스트를 먼저 돌리면 그 검사에서 걸려 재생성 단계에 도달하지
#     못하고, 원인을 고쳐도 스스로 풀리지 않는다(2026-07-31 생존편향 클레임
#     추가 때 실제로 걸린 순서 함정).
#     팩트시트 재생성은 테스트 결과에 의존하지 않으므로 앞에 두는 것이 맞다.
#     --fast 는 '전 테스트 통과' 자기참조 클레임을 제외한다 - 그 클레임을 넣으면
#     테스트 실패 -> FACTSHEET [FAIL] -> test_claims 실패 -> 원점의 순환이 된다.
#     tests/test_claims.py 도 그 클레임을 기대 목록에서 빼고 있다(설계 일치).
python analysis\verify_claims.py --factsheet-out docs\FACTSHEET.md --fast
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 클레임 검증 실패 - 수치 공개 불가 상태" ; exit 1 }

# 6b) 전 테스트 스위트 - 갱신된 FACTSHEET 를 전제로 검사한다.
python tests\run_all.py
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 테스트 스위트 실패 - 위 출력의 FAIL 파일을 먼저 고칠 것" ; exit 1 }

# 6c) 테스트 통과 클레임까지 포함한 최종 FACTSHEET. c_test_suite 의 내부
#     run_all 은 HBM_VERIFY_CLAIMS_NESTED 로 표시되어 저장본 자기참조를 건너뛴다.
python analysis\verify_claims.py --factsheet-out docs\FACTSHEET.md
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 최종 클레임 검증 실패 - 수치 공개 불가 상태" ; exit 1 }

# 7) 생성 산출물만 동결 커밋
$releasePaths = @(
    "data\snapshots",
    "out\backtest",
    $benchmarkCache,
    "out\px.csv",
    "out\env_freeze_final.txt",
    "out\f2_snapshot_log.txt",
    "out\f3_coverage_log.txt",
    "out\f4_backtest_log.txt",
    "out\f4b_composition_log.txt",
    "out\f4c_bucket_log.txt",
    "docs\FACTSHEET.md"
)
foreach ($path in $releasePaths) {
    if (Test-Path -LiteralPath $path) {
        git add -- $path
        if ($LASTEXITCODE -ne 0) { Write-Host "[중단] 산출물 stage 실패: $path" ; exit 1 }
    }
}
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { Write-Host "[중단] 동결할 산출물 변경이 없습니다" ; exit 1 }
if ($LASTEXITCODE -ne 1) { Write-Host "[중단] staged diff 확인 실패" ; exit 1 }
git commit -m "release(part3): FINAL backtest $IndexAsof - gates approved, claims unlocked"
if ($LASTEXITCODE -ne 0) { Write-Host "[중단] FINAL 동결 커밋 실패" ; exit 1 }
Write-Host ""
Write-Host "== FINAL 완료 =="
Write-Host "매니페스트: out\backtest\backtest_run_manifest_FINAL.json"
Write-Host "팩트시트  : docs\FACTSHEET.md (성과 수치 인용 해제 상태 확인)"
Write-Host "대시보드  : python -m streamlit run app.py"

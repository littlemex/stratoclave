<!-- 最終更新: 2026-07-22。ここの数字は backend/tests/test_scenarios_coverage.py で
     run.py の出力に pin されている。手編集せず再生成すること。 -->

# 少数チームが1つの共有予算プールを使う

**対象読者:** user（チームリード）
**動作前提:** 出荷済み・全緑の成果物のみ（オフライン優先）。

## このシナリオで体験できること

3人チーム（`acme-team`）が1つの予算プールを共有する、6リクエストの1日を追う。チームリードが
気にする3軸 —— **コスト・性能・精度** —— を歩き、gateway が今日答えられるもの／答えられない
ものを正直に見る:

- **コスト** は今日フルに動く: 本物の Savings Certificate エンジンが、チームのルーティング助言が
  いくらの価値だったかを教える（escalation loss は隠さず減算）。
- **性能** は **gap** に突き当たる: TTFT/TPOT を読もうとすると、gateway は課金 write のレイテンシ
  しか出さないと分かる。その gap こそが学び。
- **精度** は小さな exact-match スコアラを走らせ、品質チェックの正直な *形* を見せる —— ただし
  実トラフィックから流し込むのは **gap**。

2つの gap はワークショップの失敗ではなく *成果* である。次に作るべき機能であり、
[`COVERAGE.md`](../../COVERAGE.md) に機械可読な形で可視化される。

## 前提

- Python、このリポジトリ、クラウド・ネットワーク不要（シナリオ全体が checked-in データへの純粋な
  fold）。
- **責務境界:** 測定・評価・可用性目標・backend 適合は利用者の責務。このシナリオが提供するのは
  *機構* —— 決定的なスクリプト、指標の定義、純粋な採点 fold —— であって、あなたのワークロードに
  対する監査済みの数字ではない。

## 手順

実行:

```bash
python scenarios/usage/small-team/run.py           # 人間可読
python scenarios/usage/small-team/run.py --json     # 生 JSON
```

### 1. コスト —— ルーティング助言の価値（今日動く）

チームの6リクエストが Savings Certificate に fold される。期待出力:

```
[COST]  (runs today — real Savings Certificate engine)
  rate version:            builtin
  priced requests (base):  3
  NET saving if followed:  $0.030000
    (+ cheaper-if-followed $0.086000 / - dearer $0.056000)
  potential (advice only): $0.064000 (never in headline)
  request classes:         {'counterfactual': 4, 'followed': 1, 'no_suggestion': 1}
  quality measured:        False
```

正直に読む: **net**（$0.030）は *実際に enact された* 助言が節約した額 —— cheaper-if-followed
合計（$0.086）から、ルーターが *より高い* モデルを勧めた実際の **escalation loss**（$0.056）を
引いたもの。shadow 助言のみの分（$0.064 `potential`）は headline に含めない。
`quality measured: False` —— コストは証明するが、品質は主張しない。

### 2. 性能 —— TTFT/TPOT を読もうとする（gap に突き当たる）

```
[PERF]  (GAP — token timing not emitted today)
  TTFT / TPOT:   None / None  <- not measured
  gap:           not-implemented (scenarios/GAPS.md#perf-token-timing)
```

gateway は `ledger_transact_latency`（課金 write）を出すが、first token や token 間ギャップの
タイムスタンプは取っていない。正直に出せる TTFT の数字が無いので、シナリオは何も出さず gap を
明示する。[`GAPS.md`](../../GAPS.md#perf-token-timing) を参照。

### 3. 精度 —— 小さな exact-match スコアラ（機構は動く、tap は gap）

```
[QUALITY]  (partial — exact-match scorer runs; the eval tap is a GAP)
  exact-match accuracy:  8/10 = 80%
  method:                exact-match, conservative (ambiguous=not-correct)
  tap gap:               not-implemented (scenarios/GAPS.md#quality-eval-tap)
```

10個の決定的タスク（算術・抽出・整形）を exact match で採点。スコアラは **conservative**:
`1024` に対する `"1,024"` も空欄も両方 *不正解* —— 部分点なし、類似度なし、判定モデルなし。
`N=10` を刻印し、これが採点 *機構* であってベンチマークでないことを明示する。チームの実トラフィック
から流し込むには、gateway がまだ出していない eval tap が要る ——
[`GAPS.md`](../../GAPS.md#quality-eval-tap)。

## 測定

- コスト: `mvp.learning.savings.summarize_savings` を built-in `rate_version` で、checked-in の
  `team_workload.jsonl` に対して実行（請求額は本物の pricer で recompute、手書きしない）。
- 性能: TTFT/TPOT として **定義** されるが **emit されない** —— gap。
- 精度: `mini_eval.jsonl` への exact-match、conservative、`N` 刻印。

## 期待結果

上記の数字は `backend/tests/test_scenarios_coverage.py` で `run.py` の出力に pin されており、
このドキュメントがコードの出力からサイレントにズレることはない。

## Coverage と gap

`coverage.yaml` は6手順を符号化する: cost-savings と quality-exact-match は `covered`、
per-user コスト分割と品質の acceptance bar は `user-responsibility`、TTFT/TPOT と eval tap は
issue リンク付きの `not-implemented`。全シナリオ横断の集計は
[`scenarios/COVERAGE.md`](../../COVERAGE.md)。

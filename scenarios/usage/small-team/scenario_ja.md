<!-- 最終更新: 2026-07-22。ここの数字は backend/tests/test_scenarios_coverage.py で
     run.py の出力に pin されている。手編集せず再生成すること。 -->

# 少数チームが1つの共有予算プールを使う

**対象読者:** user（チームリード）
**2つのモードで動く:** **offline** モード（決定的・CI 安全・ネットワーク不要）と、3軸すべてを
**実 Bedrock** で測る **`--live` モード**。

## このシナリオで体験できること

3人チーム（`acme-team`）が1つの予算プールを共有する。チームリードが気にする3軸 ——
**コスト・性能・精度** —— を、まず offline（機構・決定的）で、次に live（同じ機構・実トラフィック）で
歩く:

- **コスト** —— offline は checked-in ワークロードを Savings Certificate に fold する。**live** は
  *実* Bedrock のトークン使用量を出荷済み pricer で課金する。
- **性能** —— TTFT/TPOT は `--live` で実 Bedrock に対し **クライアント側で実測**（live baseline）。
  まだ無いのは、突合するための **gateway 側** の TTFT telemetry —— これが本当の gap。
- **精度** —— *同じ* conservative exact-match スコアラが、offline では canned 回答に、**live** では
  実モデル出力に対して走る。チームの実運用トラフィックから流し込むには eval tap が要る —— **gap**。

gap はワークショップの *成果* である。次に作るべき機能であり、live evidence 付きで
[`COVERAGE.md`](../../COVERAGE.md) に機械可読な形で可視化される。

## 前提

- **offline:** Python とこのリポジトリ —— クラウド・ネットワーク不要（checked-in データへの純粋な
  fold）。
- **`--live`:** Bedrock アクセス権を持つ AWS 認証情報（`AWS_PROFILE`、`AWS_REGION=us-east-1`）。
  実費が出るが `live.py` の `$0.10`/run ハード上限で bound（1回の実行は約 `$0.002`）。
- **責務境界:** 測定・評価・可用性目標・backend 適合は利用者の責務。このシナリオが提供するのは
  *機構* —— 決定的なスクリプト、指標の定義、共有の採点 fold、live baseline ハーネス —— であって、
  あなたのワークロードに対する監査済みの数字ではない。live の数字は **gateway を経由しない
  baseline** であり、「gateway 検証済み」の主張ではない。

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

### 2. 性能 —— offline では TTFT/TPOT は未測定、live baseline で実測する

offline ではモデル呼び出しが無いので `run.py` は TTFT を出さず、gateway telemetry の gap を指す。
**`--live`** ステップ（後述）は TTFT/TPOT を実 Bedrock に対しクライアント側で *実測* する。残る本物の
gap は、そのクライアント値と突合するための **gateway 側** の TTFT telemetry ——
[`GAPS.md`](../../GAPS.md#perf-token-timing) 参照。

### 3. 精度 —— 小さな exact-match スコアラ（同じスコアラが offline と live で走る）

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

### 4. gateway 経由 —— gateway を通ったデータで3軸（これが本命）

このワークショップが存在する理由の検証。タスクが **gateway 自身のリクエスト経路**
（`POST /v1/messages`: auth → reserve → 実 Bedrock → settle → ledger）を通るので、コストは
**gateway が settle した charge-of-record**、TTFT は **gateway 経路**のレイテンシ、精度は
**gateway の**応答を採点する。

```bash
SC_GW_LIVE=1 AWS_REGION=us-east-1 [AWS_LIVE_PROFILE=claude-code] \
    python -m pytest backend/tests/test_live_gateway.py -s -q
```

コミット済みのサンプル実行（[`results/live-gateway-gw1.json`](results/live-gateway-gw1.json)）:

```
=== small-team GATEWAY-PATH live (real Bedrock, gateway IN path) ===
    transport=in-process-asgi  ledger=moto (not real DynamoDB)
[COST]     charge-of-record (ledger settle) = $0.000492  (client-side estimate $0.000562)
[PERF]     gateway TTFT p50=2384.4ms  direct TTFT p50=2089.4ms
           paired overhead median=248.7ms (min=-320.2 max=778.6, N=10, point est.)
[QUALITY]  10/10 exact-match (100%), conservative, on the gateway response
```

正直に読む:

- **コスト** は **gateway が ledger に書いた charge-of-record**（$0.000492）を settle 後に読み戻した
  もの —— クライアントの推測ではない。client 側推定（$0.000562）のすぐ下にあり、gateway の課金が実
  使用量を追えている。
- **性能** は同一 run 内の **paired** 測定: タスクごとに gateway 経路 vs 直接経路（順序を交互に）。
  直叩きでは決して得られない **gateway overhead** が paired 中央値 **248.7ms**（auth + reserve +
  ledger + ASGI dispatch）。N=10 なので **点推定のみ**、分布の主張なし。min が負なのは実ジッタで、
  隠さず残す。
- **精度** は **gateway の**応答を同じ conservative スコアラで `10/10`。

結果に刻まれた honest ラベル: `gateway_in_path=true`、ただし `transport=in-process-asgi` と
`ledger=moto`、`excluded`（`network, ALB, TLS, process-boundary`）。これは **gateway のロジックと
課金の正確性を実モデルトラフィックで**検証するもので、デプロイ環境の SLO **ではない**（経路に
network/ALB は無い）。

### 5. 直接 baseline —— 物差し（単体では何も検証しない）

```bash
AWS_REGION=us-east-1 python scenarios/usage/small-team/baseline_direct.py --run-id demo1
```

同じ10タスク × 3反復を **直接** Bedrock に叩く（gateway は経路にいない）——
サンプル [`results/live-demo1.json`](results/live-demo1.json): コスト `$0.001686`、TTFT p50
`1074.5ms`（N=30、生値保存、min `873.9` / max `1431.7`）、精度 `10/10`。これは §4 の gateway 経路が
差分を取るための baseline としてのみ存在し、`path=direct-bedrock-no-gateway` ラベルと stderr 警告で
それを明示する。`10/10`（実モデル）vs offline `8/10` の差が学び: 実モデルは canned fixture が仮定した
comma 無しの `1024` を返した。

## 測定

- コスト: `mvp.learning.savings.summarize_savings` を built-in `rate_version` で —— offline は
  checked-in の `team_workload.jsonl`、live は Bedrock 自身のトークン使用量に対して。請求額は本物の
  pricer で recompute、手書きしない。
- 性能: TTFT/TPOT を `--live` で streaming 応答からクライアント側実測。突合用の **gateway 側**
  telemetry が gap。
- 精度: 共有の conservative exact-match スコアラを `mini_eval.jsonl` に適用、`N` 刻印 —— offline は
  canned 回答、live は実モデル出力。

## 期待結果

**offline** の数字は `backend/tests/test_scenarios_coverage.py` で `run.py` の出力に pin されており、
決定的な部分はサイレントにズレない。上記の **live** の数字は
[`results/live-demo1.json`](results/live-demo1.json) の凍結サンプル（CI が doc と当該コミット済み
ファイルの一致を検査）。新しい `--live` 実行は設計上、非決定的な新しい数字を出す —— CI はそれを
ゲートしない。

## Coverage と gap

`coverage.yaml` は8手順を符号化する: cost-savings, cost-live-billing, perf-ttft-client,
quality-exact-match は `covered`（うち3つは live evidence 付き）、per-user コスト分割と品質の
acceptance bar は `user-responsibility`、gateway TTFT telemetry と eval tap は
issue リンク付きの `not-implemented`。全シナリオ横断の集計は
[`scenarios/COVERAGE.md`](../../COVERAGE.md)。

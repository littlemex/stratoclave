# 配置B 実装仕様レビュー(money-path 着手直前版)

> **【重要・凍結】この文書は配置B(SRを実行バックエンドとして前段でpool-max予約)の実装仕様である。実機検証で `POST /api/v1/eval`(decision-only)の実在が判明したため、出荷経路は配置B'(A')= 「eval で判断だけ取得 → Strato が単一モデルを正確額で予約 → 自前トランスポートで実行 → 一次情報で settle」に切り替わった。正本は `CONTRACT.md`。**
>
> 本文書は**凍結された配置Bの参照仕様**として残す。B の money 機構(`reservation.py` の PoolReservation/ConsumedProof、`settle.py` の二段settle、`hardening.py` の HMAC、`serving/semantic_router.py` の forward_to_sr)は `sr_is_servable()==False` でダーク。凍結前に P1/P2/P3 のレビュー指摘は全て修正済み(将来解凍時に「検証済み」の看板ごと欠陥を復活させないため)。**解凍条件**: 「Strato の serving 層に無く SR プールにのみ存在するモデルへの実需要が計測された時」のみ(`CONTRACT.md` 参照)。それまで着手しない。
>
> A' で活きる資産: `port.py`(RouteDecision)/ `adapter.py`(sr_mode三態+kill-switch、`decide()`をeval clientに配線)/ `canary.py`(経路非依存)/ `observability.py`(join元をeval signalへ、divergence=推奨モデルvs実課金)/ pricing CIゲート(「eval decision空間↔registry写像」へ転用)。

---

## 1. served_by="semantic-router" の seam 設計

**結論**: SRプール全体を **1つの virtual ModelEntry**(`model_id="sr:pool/default"`, `served_by="semantic-router"`, `virtual=True`)で表現する。virtual entry は candidate chain と課金予約の入口にのみ使い、**charge-of-record のモデルには絶対にならない**(settle 時に replay の実モデルへ正規化)。

**根拠**: 既存 seam(`_servable` / infrarouter / serving/*)は「1エントリ=1トランスポート」の前提で組まれており、これを崩すと差分が全域に波及する。SRの背後の実モデル多重性は「実行時に決まる課金対象」の問題であり、これは settle 側(二段settle)で既に吸収する設計になっている。よって seam 側は virtual 1エントリで十分。逆に候補ごとに ModelEntry を並べると、Strato 側の routing が SR の decide を先取りする形になり「SRが executing gateway」という確定設計と矛盾する。

**具体**:

```python
# registry/types.py
served_by: Literal["bedrock", "vllm", "semantic-router"]
class ModelEntry:
    ...
    virtual: bool = False          # True → charge-of-record 禁止
    sr_pool_ref: str | None = None # "default" 等、SR pool config へのキー
```

- `_pipeline.py:1343 _validate_model_pin`: ユーザー pin が virtual entry を指す場合、`sr_mode_for(tenant) == "off"` なら reject。canary/active なら許可(pin = SR経路の明示opt-in)。
- `_pipeline.py:1440 _resolve_candidate_chain._servable`: 分岐追加 → `serving/semantic_router.py:sr_is_servable(entry, tenant, now)`。
- `infrarouter.py:48`: `case "semantic-router": return SemanticRouterTransport(...)`。

**sr_is_servable の判定(敵対検証の穴①「candidate_pool の servability 検証欠落」をここで塞ぐ)**:

```python
def sr_is_servable(entry: ModelEntry, tenant_id: str, now: float) -> bool:
    # (a) SR health: /healthz キャッシュ(TTL 5s)、stale>15s → False
    # (b) C = candidate_pool(tenant) が非空
    # (c) ∀c∈C: レジストリに enabled かつ priced(unit_price>0)かつ非tombstone
    # (d) SR /v1/models の直近 sync(TTL 60s)に C の全員が載っている。sync stale → False
    # (e) fail-open 先の default model が bedrock 側で servable(SRが死んだ瞬間の逃げ道保証)
```

(c)(d) が肝: 「tenant_allowlist にはあるが SR がもう提供していないモデル」「SR は提供するが Strato に単価がないモデル」の両方向のズレをリクエスト前に落とす。settle 側の snapshot 検証(§5)と二重防御になる。

`serving/semantic_router.py` は `serving/vllm.py:142` と同型のモジュール構造(`endpoint_is_servable` → `sr_is_servable`、`invoke` → `forward_to_sr`)で置く。トランスポート差分を serving/ 配下に閉じるのは既存2バックエンドと同じ規律。

---

## 2. 候補集合の確定と pool-max reserve

**結論**: `C = tenant_allowlist ∩ SR_backend_pool ∩ registry_priced` をリクエスト時に確定し、**snapshot(model_ids + 単価 + price_version + pool_hash)を Reservation 行に永続化**する。reserve は pool-max 単価。**C=∅ は sr_is_servable=False として candidate chain から SR を落とし、デフォルト経路へ**(拒否ではない)。

**根拠**: C=∅ は設定不整合であって money の問題ではない。fail-open 原則(SR死→直Bedrock)と同じ扱いが一貫する。拒否にするとSR設定ミス1つで tenant 全断になる。snapshot 永続化は敵対検証の穴③(reserve-forward 間 TOCTOU: 単価改定・pool改定・allowlist改定)への回答で、**charge は常に reserve 時点の snapshot 単価で計算し、snapshot 外のモデルが返ったら pool-max 確定**とする(§5)。

**具体**:

```python
# mvp/sr/port.py(段1既存に追記)
@dataclass(frozen=True)
class CandidatePool:
    tenant_id: str
    models: tuple[PricedCandidate, ...]   # (model_id, unit_price_microusd, price_version)
    pool_hash: str                         # sha256(sorted model_ids + price_versions)
    snapshot_at: float

# _pipeline.py — 既存 reserve_credit_for_model(:1025) の薄いラッパ
def reserve_credit_for_pool(
    tenant_id: str,
    pool: CandidatePool,
    est_input_tokens: int,
    max_tokens_cap: int,
) -> PoolReservation:
    if not pool.models:
        raise EmptyCandidatePool  # ここに来たら sr_is_servable のバグ。防御的に例外
    max_unit = max(c.unit_price_microusd for c in pool.models)
    inner = reserve_credit_for_model(
        tenant_id, model_id="sr:pool/default",
        amount_microusd=max_unit * (est_input_tokens + max_tokens_cap),
    )
    return PoolReservation._mint(inner, pool, max_tokens_cap)  # §3
```

**CIゲート**: `tests/ci/test_sr_pool_pricing_gate.py` — SR pool config(全pool)の全 model_id について `registry.lookup(m).unit_price > 0` を assert。加えて「default fail-open model ∈ 各 tenant の C」を config lint で強制。これが割れたら **マージ不可**(money-path の前提破壊)。

---

## 3. reserve token の型強制

**結論**: `PoolReservation` を **ledger モジュール外から構築不可能な単回消費トークン**にし、`forward_to_sr` の第1必須引数にする。三層(型・network・鍵)は以下。

**具体**:

```python
# ledger/reservation.py
_MINT_KEY = object()  # モジュールprivate sentinel

@final
class PoolReservation:
    __slots__ = ("_inner", "pool", "max_tokens_cap", "_consumed")
    def __init__(self, *args, **kwargs):
        raise TypeError("use reserve_credit_for_pool()")
    @classmethod
    def _mint(cls, inner, pool, cap):  # reserve_credit_for_pool からのみ呼ぶ
        self = object.__new__(cls); ...
    def consume(self) -> ConsumedProof:
        if self._consumed: raise ReservationAlreadyConsumed  # 単回消費
        self._consumed = True; return ConsumedProof(self)

# serving/semantic_router.py
async def forward_to_sr(
    reservation: PoolReservation,      # デフォルト値なし・Optional不可
    request: SRForwardRequest,
    transport: SRTransport,
) -> SRForwardResult: ...
```

- **型**: mypy strict + `tests/typing/test_forward_requires_reservation.py`(`forward_to_sr(request=..., transport=...)` が `mypy --strict` でエラーになることを CI で assert する negative typing test)。`__init__` の raise でランタイム迂回も封鎖。
- **network**: SR への egress は money-path service account の NetworkPolicy のみに許可。他 pod から SR へは L3 で到達不能。manifests は `deploy/netpol/sr-money-path.yaml` に置き、CI で「SR への egress を持つ SA が1つだけ」を検査。
- **鍵**: forward リクエストに `x-strato-reservation-sig = HMAC(k, reservation_id|tenant|max_tokens|pool_hash)` を付与。鍵 `k` は money-path pod のみに mount(`ledger/hmac.py` 以外から import 不可)。SR 側は sig 無し/不正を 401。→ 仮に型・networkを迂回しても reservation_id 無しでは署名が作れない。

---

## 4. forward 実装

**結論**: OpenAI 互換 pass-through、max_tokens 強制上書き、**タイムアウト4種を明示規定**(敵対検証の穴②)、idempotency key = reservation_id で SR 内部リトライの二重発火(穴④)を SR 側 dedupe + settle 側冪等の二重で吸収。

**具体** (`serving/semantic_router.py`):

```python
POST {SR_BASE}/v1/chat/completions
body:  messages, model="auto",
       max_tokens = min(client_requested or ∞, reservation.max_tokens_cap)  # 常に注入・上書き
       stream_options: {"include_usage": true}   # streaming時
headers:
  x-tenant-id, x-strato-span-id (= decision_log の span_id)
  x-strato-reservation-id, x-strato-reservation-sig
  x-strato-allowed-models: pool_hash            # SR側 best-effort 制約。強制は settle 側
  x-strato-idempotency-key: reservation_id      # SR内部リトライは同一keyでdedupe必須(SR側契約)
  Authorization: Bearer <service-token> + mTLS client cert
timeouts:
  connect=2s, first_byte=15s, inter_chunk_idle=30s,
  total_wall = min(120s, 8s + max_tokens_cap * 60ms)
```

- **x-vsr-replay-id 捕捉**: レスポンスヘッダはボディ前に届くので、streaming でも response start 時点で捕捉して decision item に即時記録(replay-id を落とすとreplay欠損=reserve額確定になるため、最優先で永続化)。
- **失敗時の分岐**:
  - first byte **前**の失敗/タイムアウト → **同一 reservation で** デフォルト経路へ1回だけフェイルオーバー(default model ∈ C なので pool-max reserve が被覆する。再reserve不要)。SR 側には idempotency key で「実行済みなら再実行しない」契約があるため二重実行リスクは replay evidence で検出可能。
  - first byte **後**の失敗 → クライアントにエラー伝播、リトライしない(非冪等)。暫定 settle は判明 usage、無ければ reserve 額で暫定→replay 待ち。
- **streaming settle タイミング**: SSE を素通しし、final usage chunk 受領後に暫定 settle。usage chunk 欠落 → 暫定 = reserve 額、replay で調整。

---

## 5. settle 二段

**結論**: `settle_reservation_and_log(:3113)` は改造せず **provisional 終端** として使い、確定は **非同期調整仕訳(adjustment journal)** を別関数で切る。ledger は `(reservation_id, phase)` unique 制約で冪等。

**根拠**: 既存 settle は「1 reservation → 1 確定行」の不変条件で多数のテスト・照合クエリが依存している。phase 引数で多態化するより、加算的スキーマ(phase列 + 調整行)の方が既存経路無傷でマージできる。二重発火(穴④)対策の冪等性も unique 制約1本で決まる。

**具体**:

```python
# ledger/settle_sr.py
def settle_provisional(
    proof: ConsumedProof,               # §3: forward済みの証明
    response_model_raw: str,            # SRレスポンスの model
    usage: MeasuredUsage | None,        # None → reserve額で暫定
    replay_id: str | None,
) -> LedgerEntry:
    model = registry.normalize(response_model_raw)   # 未知 → reserve額暫定 + alert
    unit  = proof.pool.price_of(model)               # snapshot単価。snapshot外 → reserve額暫定
    entry = settle_reservation_and_log(..., phase="provisional", ...)
    if replay_id: replay_queue.enqueue(entry.reservation_id, replay_id,
                                       key=(run_id, span_id))  # decision_log キーで join
    return entry

# workers/sr_replay_worker.py(非同期)
def finalize_from_replay(reservation_id, replay_id) -> AdjustmentEntry | None:
    replay = sr_client.fetch_replay(replay_id)       # retry: 30s×5 → 5min×3、締切15min
    # ケース分岐:
    # (a) 正常: charge = snapshot単価 × replay実測トークン。adj = final - provisional(±)
    # (b) replayモデル ∉ snapshot C:  final = reserve額 + sr_circuit.quarantine() + pager
    # (c) replayに複数実行(SR内部リトライ二重発火の物証): 返却responseに対応する1実行分のみ課金、
    #     残りはevidence保存 + alert。いずれも final ≤ reserve は不変条件として assert
    # (d) 締切超過/404: final = reserve額 + alert(既定設計どおり)
```

- **不変条件**: `final_charge ≤ reserve_amount` を settle 関数内 assert + ledger CHECK 制約の両方で。pool-max 定義上これは常に成立するはずで、破れたら計算バグ→fail-closed(reserve額)。
- decision_log 側は `build_decision_item(vsr=...)` に `replay_id`, `pool_hash`, `provisional_entry_id` を追記。(run_id, span_id) で三点照合(decision / provisional / final)可能にする。

---

## 6. canary サンプリング

**結論**: **決定的 session-sticky ハッシュ**。`sha256(tenant_id ‖ conversation_id) mod 10000 < canary_bps`(default 100 = 1%)。純ランダムは不採用。

**根拠**: 会話途中でモデルが揺れると UX 劣化と評価ノイズの両方を生む。決定的ハッシュなら再現可能で、インシデント時に「この会話はSR経路だったか」を後から確定できる(replay evidence との突合に必須)。

**具体**:
- `mvp/sr/adapter.py:sr_mode_for` の canary 分岐に `canary_bps`(routing config 相乗り、per-tenant)。
- **安全化**: canary 時は `max_tokens_cap = min(configured_cap, 1024)` に締める + per-tenant の SR 専用日次予算(microusd)。reserve はこのサブ予算から取り、枯渇 → その日は `sr_is_servable=False`(fail-open、拒否しない)。
- **自動遮断**: circuit breaker — 直近10分で {forward エラー率 >5% | replay欠損率 >2% | snapshot外モデル検出 ≥1} → `sr_circuit.open()`(全tenant即 off 相当)+ pager。config watch により sr_mode=off の手動 kill switch は数秒で伝播。

---

## 7. 段2 検証項目(配置B版)

**formal / property**:
- TLA+: 状態機械 `IDLE→RESERVED→FORWARDED→PROVISIONAL→FINAL` で
  (i) `□(FORWARDED ⇒ RESERVED済)`(money fail-closed)、
  (ii) `□(charge ≤ reserve)`、
  (iii) reservation 単回消費・final 単回、
  (iv) liveness: 全 reservation は replay欠損でも eventually FINAL(reserve額経由)。
- Hypothesis property test: 任意の (pool, usage, replay有無, replayモデル) 組合せで invariant (ii) と冪等性。

**実機(fake SR)**: `tests/fakes/sr_server.py`(FastAPI)— 注入可能な故障: 遅延、first-byte前/後の切断、x-vsr-replay-id 欠落、replay 404/500、**snapshot外モデル返却**、**同一 idempotency key での二重実行を replay に記録**。chaos: mid-stream kill → fail-open + reserve額暫定を確認。

**CLI/UI**:
- `strato ledger show --span <id>`: provisional / final / adjustment の三行と reserve 額を並列表示。
- `strato sr status`: circuit 状態、canary_bps、replay lag p95、replay欠損率、pool_hash 現行値。
- decision log viewer に「SR evidence vs charge-of-record 差分」列。

---

## 8. サブステップ分割(各々独立マージ・CI緑・fail-closed不変)

| # | 内容 | fail-closed 保証 | fake SR で検証可 |
|---|---|---|---|
| S1 | Literal拡張 + `virtual` flag + `sr_is_servable`(常にFalseのstub)+ seam 3箇所の分岐 | SR は決して選ばれない | — |
| S2 | `CandidatePool` / `reserve_credit_for_pool` / `PoolReservation` + pricing CIゲート + negative typing test | 未使用コード、reserve なし forward は型で不可を先に固定 | — |
| S3 | `serving/semantic_router.py`(forward + timeout 4種 + ヘッダ + replay-id捕捉) | sr_mode 全tenant off のまま | ✅ 全pass-through経路 |
| S4 | settle 二段(provisional + replay worker + 調整仕訳 + ledger unique制約) | 既存 settle 無改造 | ✅ 欠損/未知モデル/二重実行 |
| S5 | 三層ハードニング(HMAC鍵、NetworkPolicy、SA監査CI) | 迂回経路の物理封鎖 | 鍵検証は✅、netpolはstaging |
| S6 | canary sampler + circuit breaker + サブ予算 + kill switch | 遮断が既定動作 | ✅ 遮断発火まで |
| S7 | 段3観測性: metrics(reserve-settle差分ヒストグラム、replay lag、pool-max/実課金比)、alert、CLI | — | ✅ metric発火 |

**fake SR で実機前に検証できる範囲**: money-path の全状態遷移・全故障モード・冪等性・TOCTOU・二重発火——つまり§5の不変条件は**全て**実機前に閉じられる。実機でしか検証できない残余は (a) mTLS/netpol の実配線、(b) SRの実routing品質、(c) 実 replay latency 分布(締切15minの妥当性)、(d) 実トークンカウントと SR usage の乖離率——これらは S6 の canary(内部tenant、canary_bps=100、cap=1024)で吸収する。

**最終確認事項(マージ前に潰す)**: SR 側に (1) idempotency key dedupe、(2) reservation-sig 検証、(3) replay に attempt 履歴を含める——の3契約をコミットさせること。(3) が無いと二重発火が evidence 上不可視になり、穴④の検出が settle 冪等性だけの片肺になる。
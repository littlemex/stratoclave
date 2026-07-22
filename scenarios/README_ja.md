<!-- 最終更新: 2026-07-22 -->

# ワークショップ

Stratoclave が3軸 —— **コスト・性能・精度** —— で何をするか、そして同じくらい正直に、まだ
何を *しない* かを、手を動かして見るためのシナリオ集。各シナリオは決定的・オフライン優先の台本で、
出荷済みコードで動かせない手順はそれを明示し、数字を捏造せず gap を指し示す。

gap こそが狙い。ワークショップを走らせると「X を測定したい」が [`COVERAGE.md`](COVERAGE.md) の
機械可読エントリになる —— 次に作るべき機能の、需要ドリブンなリスト（[`GAPS.md`](GAPS.md) 参照）。

> これらは synthetic データ上の親切なサンプルであって、あなたのワークロードの **ベンチマークではなく**、
> **監査済みの数字でもない**。測定・評価・acceptance bar は利用者の責務。ワークショップは機構を提供する。

## `docs/` と `bench/` との関係

- **`bench/`** は数字を再現する装置（負荷・レイテンシ・節約）。
- **`docs/`** はシステムの説明。
- **`scenarios/`**（ここ）は人が辿る台本で、出荷済みコマンドや `bench/` スクリプトを1つのガイド体験に
  束ね、手順ごとに `covered` か gap かを記録する。シナリオは `bench/` を呼んでよいが、逆依存はしない。

## シナリオ一覧

| パス | 対象読者 | 軸 | 読む |
|---|---|---|---|
| [`usage/small-team`](usage/small-team/scenario_ja.md) ([English](usage/small-team/scenario.md)) | user | cost・perf・quality —— offline + 実 Bedrock を **gateway 経由**（charge-of-record・paired overhead）+ 直接 baseline | 3人チームが1つの共有予算プールを使う |

今後（同じ4ファイル構成で追加予定）: `admin/setup-tenant`（構築）、`usage/*` の追加、`perf/*`、`quality/*`。

## シナリオの構成

各シナリオディレクトリは4ファイルを持つ（[`_schema/`](_schema/) 参照）:

- `scenario.md` + `scenario_ja.md` —— 同じ手順の英語版と日本語版。
- `run.py` —— 実行可能で決定的な部分（オフライン優先。checked-in データへの純粋な fold なので数字を
  CI で pin できる）。
- `coverage.yaml` —— 手順ごとの機械可読 state: `covered`, `covered-elsewhere`,
  `not-implemented`（issue リンク必須）, `user-responsibility`。

`backend/tests/test_scenarios_coverage.py` が全 `coverage.yaml` を [`COVERAGE.md`](COVERAGE.md) に
集計し、2つのルールを強制する: gap は tracking issue を持つこと、そして何かを *測定する* と主張する
手順は実際に出荷済みコードで動くこと。

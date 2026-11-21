# sns-autopilot

X(@sudomix・@Ashley_motion0)と Instagram のキャラクターアカウントで、いいね・フォロー・RT を **本人の承認を挟んで** 実行する仕組み。
本番は PC の `C:\AI model\ops` に置いて動かしている(このリポジトリはその中の該当部分の記録)。

## 流れ
1. 03:00 夜間収集の最後に `collectors/engage.py` が、Muse のアカウント別 Chrome プロファイル(CDP)で通知・検索・指定アカウント周辺・タイムラインを読み、`data/<日付>/engage.json` に候補を書く(読むだけ)
2. 07:13 Claude の定期タスクが候補を選び、理由付きで「反応の承認」ページ(Artifact)に出す
3. 本人が承認・却下
4. 10:47 Claude の定期タスクが承認分を `mailbox/to_muse/engage-<日付>.json`(type: engage_enqueue)にする
5. Muse が `actions/engage_run.py --enqueue` で待ち行列へ入れ、12/15/18/21時に Windows タスク `Ops-EngageRun`(`run_engage.bat`)を起動。上限・間隔・ログイン中アカウントの確認を守って実行し、制限や確認画面を見つけたらそのアカウントを止める

## ファイル
- `lib/engage_common.py` 接続・アカウント確認・制限検知・回数と履歴の記録
- `collectors/engage.py` 候補集め
- `actions/engage_run.py` 実行(`--enqueue` / `--drain` / `--dry-run`)
- `tools/engage_probe.py` プロファイルの CDP ポートとログイン中アカウントの調査
- `config/engage.yaml` アカウント・上限・間隔・検索語
- `lib/common.py`, `run_night.py`, `register_tasks.ps1` は ops 本体のもの(依存と組み込み箇所の記録)

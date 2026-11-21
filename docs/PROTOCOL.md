# Claude ⇔ Muse 連絡の仕組み(mailbox)

本人が間に入らなくても、Claude と Muse が `C:\AI model\ops\mailbox` のファイルでやり取りする。
**指示を出すのは Claude、実行するのは Muse。** Muse から Claude への連絡は「結果」「質問」「相談」の3つだけ。

```
mailbox/
  to_muse/     Claude → Muse の作業指示(1作業1ファイル)
  to_claude/   Muse → Claude の結果報告と質問
  done/        Muse が終えた作業指示を移す場所
  consult/     Muse ⇔ Claude の相談スレッド(1相談1フォルダ。決まりは consult/README.md)
```

## 1. 作業指示(Claude が書く) `to_muse/<id>.json`

```json
{
  "id": "2026-11-04-post-tiktok-001",
  "created_by": "claude",
  "created_at": "2026-11-04T06:55:00+09:00",
  "type": "post_tiktok",
  "run_at": "2026-11-04T19:00:00+09:00",
  "params": {
    "video_path": "C:\\AI model\\ops\\outbox\\2026-11-04\\kindrest_a.mp4",
    "caption": "（このまま一字一句変えずに使う）",
    "account": "Kindrest"
  },
  "approved_by_user": true,
  "notes": "時刻ピッカーは使わず、run_at の時刻に『今すぐ投稿』する"
}
```

- `run_at` が `"asap"` のものは、次の確認時刻にすぐ実行する。
- `run_at` が時刻のものは、Muse がその時刻に自分の予定を入れて、ちょうどに実行する。
- 投稿系の作業は `approved_by_user: true` のものだけ実行する。false や欠けていれば実行せず `blocked` で返す。

### 作業の種類(type)
| type | 内容 | params |
|---|---|---|
| `post_tiktok` | `actions/post_tiktok.py --job <指示ファイル>` を実行して今すぐ投稿(結果はスクリプトが書く) | video_path, caption, account, visibility(everyone/only_me) |
| `post_instagram` | Instagramコネクタからリールを今すぐ投稿 | video_path, caption, account |
| `collect_post_stats` | 投稿URLの再生数・いいね・コメント・保存・シェアを読む | urls |
| `wake_pc` / `sleep_pc` | PCを起動/休眠させる | なし |
| `rerun_night` | `run_night.bat` をもう一度実行する | collectors(省略時は全部) |
| `run_command` | 指定コマンドをそのまま実行し、出力を返す | command, cwd |
| `copy_file` | ファイルを指定の場所へコピー | src, dst |
| `answer` | Muse の質問への回答(実行はしない、読んで以後の作業に従う) | question_id, answer |
| `collect_bgm` | BGMの在庫集め。Pixabay・Mixkit はブラウザで、Openverse は `actions/fetch_openverse.py` で取り、`library/bgm/<サイト>/` に保存する(手順は MUSE_RUNBOOK.md の「BGMの集め方」) | moods(検索語のリスト), per_site({pixabay, mixkit, openverse} の件数) |
| `engage_enqueue` | 本人が承認した反応(いいね・フォロー・RT)を `actions/engage_run.py --enqueue <指示ファイル>` で待ち行列に入れる。**ボタンは押さない**。実行は Windows タスク `Ops-EngageRun` が上限と間隔を守って行う(手順は MUSE_RUNBOOK.md の「反応の実行」) | actions(id, account, action, url のリスト) |
| `engage_probe` | `tools/engage_probe.py --launch --job <指示ファイル>` を実行し、アカウント別Chromeプロファイルのポートとログイン中のアカウントを調べる(読むだけ) | なし |
| `engage_profile_setup` | 各アカウントがどの Chrome プロファイルかを `state/engage_profile_map.json` に書き(この作業指示のときだけ書いてよい)、`tools/engage_setup_profiles.py --job <指示ファイル>` を実行する(手順は MUSE_RUNBOOK.md の「反応の実行」) | なし |

## 2. 結果報告(Muse が書く) `to_claude/<id>.result.json`

```json
{
  "id": "2026-11-04-post-tiktok-001",
  "status": "done",
  "finished_at": "2026-11-04T19:01:12+09:00",
  "what_i_did": ["Chrome-TikTokを開いた", "動画をアップロード", "キャプションを貼り付けて投稿"],
  "evidence": {"post_url": "https://www.tiktok.com/@.../video/...", "screenshot": "C:\\AI model\\ops\\logs\\muse\\....png"},
  "error_text": "",
  "not_done": [],
  "notes": ""
}
```

- `status` は `done`(全部できた)/ `failed`(やろうとして失敗)/ `blocked`(指示どおりにできないので手を付けなかった)のどれか。
- `done` には必ず証拠(投稿URLかスクリーンショットのパス)を付ける。証拠がなければ `done` にしない。
- エラーは要約せず、表示された文をそのまま `error_text` に貼る。
- 書き終えたら、元の作業指示ファイルを `done/` に移す。

## 3. 質問(Muse が書く) `to_claude/q-<YYYYMMDD-HHMM>.json`

```json
{"from": "muse", "question": "…", "context": "…", "blocking_job_id": "2026-11-04-post-tiktok-001"}
```

Claude は毎朝(6:41)の定期タスクで読み、`to_muse/a-<同じ番号>.json`(type: `answer`)で答える。
急ぎでなければ質問は翌朝に答えが届く。答えを待つ間、その作業は実行しない。

## 4. 確認の時刻

`config/schedule.yaml` の `muse_checks` の時刻に Muse が `to_muse/` を見る。
Claude は毎朝の定期タスクで `to_claude/` を読み、必要な指示を `to_muse/` に置く。

## 5. 相談(Muse ⇔ Claude のスレッド) `consult/<YYYYMMDD>-<題名>/`

結果の分析・原因の切り分け・テストの進め方・方針の選択のように、短い質問(3.)で済まないものは相談スレッドに書く。
書き方・読み方・返事の形は `mailbox/consult/README.md` に従う。
相談には定期タスクは答えない。本人が Claude に「Museの相談を見て」と声をかけたときに Claude が読んで返事を書く。

# agent-session-exporter

Codex / Claude の会話履歴を、特定ワークスペースや `.ai/` に依存せず
Obsidian Vaultへ集約する小さなアーカイバです。

ローカルCLI、デスクトップアプリ、Cloud環境では取得できる情報が異なるため、
入口を複数用意し、共通イベント形式を経由して同じMarkdownへ変換します。

```text
Codex / Claude Code hooks ───────────────┐
Codex Cloud CLI / data export ───────────┼─> local SQLite ─> Markdown ─> Vault
Claude Code on the web ─> Worker ─> D1 ─> ase pull ────────┘             │
                                                                         └─> Obsidian Sync
```

## 対応範囲

| 環境 | 取得方法 | 会話本文 |
|---|---|---|
| Codex CLI / Codex desktop host | ユーザーレベルのCodex hooks + transcript | 取得可能 |
| Claude Code CLI / 対応デスクトップhost | ユーザーレベルのcommand hooks + transcript | 取得可能 |
| Claude Code on the web | remote専用command hookからcurlで転送 | `MessageDisplay`が有効なら取得可能 |
| Codex Cloud | `codex cloud list/status/diff` | 公開CLIが返すメタデータ、初回prompt、diff。完全なtranscriptは対象外 |
| ChatGPT / Claudeの一般Web・desktop | アカウントのdata exportを一括import | exportに含まれる会話 |

これはバックアップ用途のツールです。Codex/Claude側の非公開データベースや
DOMを直接読む方式には依存しません。

## 必要環境とインストール

- Python 3.11以上
- 実行時にdetect-secrets（資格情報の検出）とcryptography（分割メッセージの暗号化）を使用
- Codex Cloud連携だけは、認証済みの`codex` CLIが必要
- Claude Cloud受信基盤を自分でdeployする場合だけ、Node.jsとCloudflare accountが必要

推奨:

```bash
cd /home/masa/work/agent-session-exporter
uv tool install .
```

開発中の変更を即時反映したい場合:

```bash
uv tool install --editable .
```

`pipx install .`でもインストールできます。必要なPython依存パッケージは、これらの
インストールコマンドで自動的に導入されます。以下では短いコマンド名`ase`を使います。

## 1. 初期設定

```bash
ase init --vault "$HOME/Documents/Obsidian/MyVault"
ase doctor
```

デフォルトの保存先:

- 設定: `~/.config/agent-session-exporter/config.toml`
- 状態DB: `~/.local/state/agent-session-exporter/events.sqlite3`
- Vault内: `ai-sessions/YYYY/MM/*.md`

XDG環境変数で場所を変更できます。また、全コマンドで
`--config /path/to/config.toml`をサブコマンドより前に指定できます。

`ase init`はOSからIANA timezone名を取得できれば、年・月・ファイル名に使う
`path_timezone`の初期値として保存します。取得できない環境では`UTC`を使います。
既存の設定ファイルにこの項目がない場合も、同じ検出結果を実行時の既定値として使い、
設定ファイル自体は書き換えません。動作を固定したい場合は明示的に設定してください。

```toml
path_timezone = "Asia/Tokyo"
```

設定したtimezoneはOSのIANA timezone databaseで検証します。render stateがある
既存セッションのパスは変更されません。

## 2. ローカルCLI / デスクトップhost

既存設定を残したまま、ユーザーレベルのhookへ追記します。

```bash
ase install-hooks --source codex
ase install-hooks --source claude
```

Codexでは起動後に`/hooks`を開き、新しいhookを確認してtrustしてください。
絶対パスが必要な環境では、次のように指定します。

```bash
ase install-hooks --source codex \
  --executable "$HOME/.local/bin/ase"
```

hookを自動編集せず内容だけ確認する場合:

```bash
ase hooks --source codex
ase hooks --source claude
```

デフォルトでは、hookでイベントを受けるたびに対象セッションだけをVaultへ
即時反映します。無効にする場合は`config.toml`へ
`sync_on_capture = false`を設定し、次のコマンドをtimer等から実行します。

```bash
ase sync
```

cron、systemd timer、launchdなどから定期実行できます。

`destination`を変更した場合、既存セッションの保存先はrender stateに残ります。
まずdry-runで移動・再紐付けの対象を確認し、問題がなければ適用します。

```bash
ase doctor
ase migrate-destination
ase migrate-destination --apply
```

移動元がrender stateの最新内容と一致し、移動先が同じセッションの古い生成ノート
だった場合は、生成frontmatterとタイトルを除く本文の差分も確認します。本文が同じか、
移動元側への末尾追記だけなら、そのまま最新内容へ置き換えます。本文途中にも差分が
ある場合は、古い移動先を同じフォルダのhash付き隠しバックアップへ退避してから
置き換えます。生成物ではないMarkdown、別セッション、最新内容を確認できない移動元は
引き続き変更せずエラーにします。

## 3. Claude Code on the web

Claude Cloudのcommand hookがイベントをcurlでCloudflare Workerへ送り、D1へ一時保管します。
Vaultのある端末が`ase pull`で取り込むため、Vaultや自宅ネットワークを公開する
必要はありません。Workerのdeploy手順は
[`deploy/cloudflare-worker`](deploy/cloudflare-worker/)にあります。

Claude Cloud向けhook設定を生成:

```bash
ase hooks --source claude-cloud \
  --inbox-url https://agent-session-exporter.example.workers.dev
```

出力をprojectの`.claude/settings.json`へマージします。以前の`type: "http"`設定や
検証用のcommand hookがある場合は、二重送信を避けるため、生成されたcommand hookへ
置き換えてください。Claude Code on the webの環境変数へ
`AGENT_SESSION_EXPORTER_INGEST_TOKEN`を設定し、Workerのhostnameをnetwork accessの
許可listへ追加します。生成したhookは`CLAUDE_CODE_REMOTE=true`のときだけcurlを
実行するため、同じproject設定をローカルで使っても通信しません。受信するイベントは
`UserPromptSubmit`、`MessageDisplay`、`Stop`、`StopFailure`、
`SessionEnd`です。

既知のhookイベント名は大文字・小文字の表記揺れを正規化します。`cwd`がなく
`workspace_roots`が1件だけ含まれるイベントでは、そのworkspaceをproject判定に
利用します。複数workspaceから代表を推測することはありません。

Vault端末の`config.toml`:

```toml
[claude_cloud]
url = "https://agent-session-exporter.example.workers.dev"
token_env = "AGENT_SESSION_EXPORTER_PULL_TOKEN"
timeout_seconds = 3.0
```

取得して同期:

```bash
export AGENT_SESSION_EXPORTER_PULL_TOKEN='pull専用token'
ase pull --sync
```

hookが使う`INGEST_TOKEN`と、ローカル取得に使う`PULL_TOKEN`は別の値にします。
Claude Cloud側のtokenが漏れても、保存済みイベントの読み取りには使えません。
D1はappend-onlyの受信箱として扱い、取得後も自動削除しません。

systemd user service例は
[`deploy/systemd-user`](deploy/systemd-user/)にあります。

## 4. Codex Cloud

既存タスクを取得:

```bash
ase codex-cloud-sync --limit 20 --details --sync
```

新しいタスクの初回promptも確実に残すには、このwrapperから開始します。

```bash
ase codex-cloud-exec --env ENVIRONMENT_ID \
  "このリポジトリのテスト失敗を調査してください"
```

Codex Cloud CLIが完全な会話transcriptを公開しない場合、ノートにはタスク情報、
取得できた状態、diff、wrapperへ渡した初回promptのみを保存します。

## 5. ChatGPT / Claude data export

サービスから取得したZIPまたはJSONを直接指定します。ZIPは展開不要です。

```bash
ase import-export ~/Downloads/chatgpt-export.zip --sync
ase import-export ~/Downloads/claude-export.zip --source claude --sync
```

自動判定はChatGPTの`mapping`形式と、Claudeのconversation/message形式に対応します。
export形式が変更された場合はadapterの更新が必要です。

## 運用

ローカルhookはSQLiteへ直接書きます。Claude Cloudのcommand hookだけはWorkerと
D1を経由し、`ase pull`で同じSQLiteへ取り込みます。重複イベントはfingerprintで
排除し、`ase sync`は変更されたセッションだけを原子的に書き換えます。

ノートのタイトルはhookが渡す最初の実ユーザープロンプトを優先します。
AGENTS.mdやenvironment contextなどの制御用テキストは本文へ残しますが、
タイトル候補には使いません。

生成するfrontmatterには、ingest判定向けの`content_kind`、`message_count`、
`event_count`、`revision`も含まれます。`content_kind = "metadata_only"`なら
会話本文を取得できなかったセッションです。`revision`はセッション内のイベントが
増えると変わるため、digestなど下流生成物の更新判定に利用できます。

frontmatterの時刻は、`updated_at`が最後のイベント、`rendered_at`がMarkdownを
実際に書いた時刻です。`archived_at`は完了または失敗したセッションだけに付き、
継続可能な`active`と`stopped`には付きません。

`redact = true`（既定）では、token、password、API key等のJSON fieldを値ごと
マスクします。会話中の資格情報はPython側で
[detect-secrets](https://github.com/Yelp/detect-secrets)、Worker側で
[Secretlint](https://github.com/secretlint/secretlint)の検出ルールを使います。
URLのユーザー情報と機密クエリ、Bearer/Basic認証の値も除去します。
検出はローカルで完結し、トークンの有効性を確かめる外部通信は行いません。
Python側は引用符のない会話中のトークンも、ライブラリの文字列のランダムさを
調べる機能で検出します。この判定には見逃しと過剰なマスクの両方があり得ます。
session・task・deviceのIDが検出対象になった場合は、同じIDから同じSHA-256由来の
仮名を生成し、異なる会話や端末を一つにまとめないようにします。

SQLite/D1への保存前に加え、transcriptの読込・分割メッセージの結合後にも
マスクします。Codex Cloudのタスク・diff・プロンプト、importした会話のタイトル、
Gitから補ったメタデータも対象です。Python側で秘密鍵のヘッダーを検出した場合は、
鍵の本体が残らないよう、その本文フィールド全体をマスクします。

`MessageDisplay`の断片はAES-256-GCMで暗号化して一時保管し、全文が揃ってから
マスク済みイベントとして保存します。ローカルの専用鍵は設定ディレクトリへ自動生成し、
Workerの専用鍵はWorkers Secretへ登録します。鍵の更新・復旧と導入手順は
[分割メッセージと暗号鍵の管理](docs/message-buffer.md)を参照してください。

検出できる形式は各ライブラリのルールに依存し、未知の形式や任意の秘密文を
すべて検出する保証はありません。WorkerとPythonで検出範囲が異なる場合もあります。
Vaultを同期・共有する前に内容を確認してください。toolの詳細はデフォルトでは
Markdownへ出力しません。

更新後の`ase sync`では既存イベントから作るノートにも新しいマスクを適用します。
旧IDと仮名IDが混在する場合も一つの会話として描画し、既存ノートのパスを引き継ぎます。
旧版で重複生成されたノートは、生成時から未編集と確認できたものだけ統合・整理します。
編集済みのノートがある場合は、内容を保護するため自動整理を止めてエラーを返します。
ただし、元のtranscript、既存のSQLite/D1イベント、バックアップは書き換えません。
過去に保存・共有した資格情報は別途点検してください。`redact = false`はPython側の
保存・出力のマスクを無効にしますが、Worker側のマスクは常に有効です。
CLIのエラー診断も、外部サービスやコマンドが資格情報を返す場合に備えて常にマスクします。

## 開発

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

設計上の判断は[`docs/architecture.md`](docs/architecture.md)にまとめています。

## 参考資料

- [Codex Hooks](https://learn.chatgpt.com/docs/hooks)
- [Codex Cloud CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-cloud)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web)

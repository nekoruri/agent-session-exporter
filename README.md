# agent-session-exporter

Codex / Claude の会話履歴を、特定ワークスペースや `.ai/` に依存せず
Obsidian Vaultへ集約する小さなアーカイバです。

ローカルCLI、デスクトップアプリ、Cloud環境では取得できる情報が異なるため、
入口を複数用意し、SQLiteの共通イベント形式を経由して同じMarkdownへ変換します。

```text
Codex / Claude Code hooks ─┐
Claude Cloud HTTP hooks ───┼─> SQLite event store ─> Markdown ─> Obsidian Vault
Codex Cloud CLI poller ─────┤
ChatGPT / Claude exports ───┘
```

## 対応範囲

| 環境 | 取得方法 | 会話本文 |
|---|---|---|
| Codex CLI / Codex desktop host | ユーザーレベルのCodex hooks + transcript | 取得可能 |
| Claude Code CLI / 対応デスクトップhost | ユーザーレベルのcommand hooks + transcript | 取得可能 |
| Claude Code on the web | 認証付きHTTP hooks | `MessageDisplay`が有効なら取得可能 |
| Codex Cloud | `codex cloud list/status/diff` | 公開CLIが返すメタデータ、初回prompt、diff。完全なtranscriptは対象外 |
| ChatGPT / Claudeの一般Web・desktop | アカウントのdata exportを一括import | exportに含まれる会話 |

これはバックアップ用途のツールです。Codex/Claude側の非公開データベースや
DOMを直接読む方式には依存しません。

## 必要環境とインストール

- Python 3.11以上
- 実行時の外部Python packageなし
- Codex Cloud連携だけは、認証済みの`codex` CLIが必要

推奨:

```bash
cd /home/masa/work/agent-session-exporter
uv tool install .
```

開発中の変更を即時反映したい場合:

```bash
uv tool install --editable .
```

`pipx install .`でもインストールできます。以下では短いコマンド名`ase`を使います。

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

年・月・ファイル名の日時は既定でUTCです。日次運用をローカル日付へ合わせる場合は、
IANA timezone名を設定します。既存セッションのパスは変更されません。

```toml
path_timezone = "Asia/Tokyo"
```

UTC以外ではOSのIANA timezone databaseを利用します。

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

移動対象と現在の内容が異なる場合や、生成物ではないMarkdownがある場合は
上書きせずエラーにします。

## 3. Claude Cloud / 別マシンから収集

collectorはSQLiteへイベントを受け取るだけです。Vaultは公開せず、
Vaultのある端末がcollectorからpullします。

collector側:

```bash
export AGENT_SESSION_EXPORTER_TOKEN='十分に長いランダム値'
ase serve
```

インターネットから受ける場合は、TLSを終端するreverse proxyやCloud Run等の
背後で動かしてください。loopback以外へlistenする設定ではtokenが必須です。
Obsidian Local REST APIをインターネットへ公開する必要はありません。

Claude Cloud向けhook設定を生成:

```bash
ase hooks --source claude-cloud \
  --collector-url https://sessions.example.com
```

出力をClaude Code環境の設定へマージし、環境変数
`AGENT_SESSION_EXPORTER_TOKEN`を設定します。受信するイベントは
`UserPromptSubmit`、`MessageDisplay`、`Stop`、`StopFailure`、
`SessionEnd`です。

既知のhookイベント名は大文字・小文字の表記揺れを正規化します。`cwd`がなく
`workspace_roots`が1件だけ含まれるイベントでは、そのworkspaceをproject判定に
利用します。複数workspaceから代表を推測することはありません。

Vault端末の`config.toml`:

```toml
[collector]
url = "https://sessions.example.com"
token_env = "AGENT_SESSION_EXPORTER_TOKEN"
timeout_seconds = 3.0
```

取得して同期:

```bash
export AGENT_SESSION_EXPORTER_TOKEN='collectorと同じ値'
ase pull --sync
```

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

hookはまずローカルDBへ書き、その後remote collectorへbest-effortで転送します。
ネットワーク障害でエージェント本体を止めません。重複イベントはfingerprintで
排除し、`ase sync`は変更されたセッションだけを原子的に書き換えます。

ノートのタイトルはhookが渡す最初の実ユーザープロンプトを優先します。
AGENTS.mdやenvironment contextなどの制御用テキストは本文へ残しますが、
タイトル候補には使いません。

生成するfrontmatterには、ingest判定向けの`content_kind`、`message_count`、
`event_count`、`revision`も含まれます。`content_kind = "metadata_only"`なら
会話本文を取得できなかったセッションです。`revision`はセッション内のイベントが
増えると変わるため、digestなど下流生成物の更新判定に利用できます。

機密情報対策として、token、password、API key等の名前を持つJSON fieldと、
代表的なcredential文字列を取り込み時にredactします。ただし万能ではありません。
Vaultを同期・共有する前に内容を確認してください。toolの詳細はデフォルトでは
Markdownへ出力しません。

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
- [Claude Code HTTP hooks](https://code.claude.com/docs/en/hooks-guide#http-hooks)
- [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web)

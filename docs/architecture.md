# Architecture

## 目的

エージェント固有の保存場所をObsidian Vaultへ直接同期するのではなく、
取得・正規化・描画を分離する。これにより、別ワークスペース、別マシン、
Cloud環境、サービスのdata exportを同じ運用へ載せる。

## データフロー

1. `capture`、HTTP collector、Cloud poller、export importerがイベントを受け取る。
2. `normalize_event`がsource、device、session、時刻、project情報を付与し、
   credentialらしい値をredactする。
3. SQLiteへappend-onlyで保存する。同じfingerprintのイベントは追加しない。
4. adapterがtranscriptまたはhook payloadからuser/assistant messageを復元する。
5. ローカルhookでは対象セッションだけを即時同期する。timerやpullでは全変更を
   探し、セッションごとの安定したパスへMarkdownを原子的に書く。

## 判断

### SQLiteを中間層にする

hookの実行時間を短くし、Obsidianが閉じている間もイベントを失わないため。
remote collectorとVault writerも分離できる。

### Vaultはfilesystemへ書く

Markdown Vaultの基本インターフェースであり、Obsidian pluginやLocal REST APIを
必須にしない。CloudからVaultを直接操作せず、Vault端末からpullする。

### transcript parserをbest-effortにする

CodexとClaude Codeのローカルtranscriptは便利だが、安定APIとは限らない。
読み取れなければhook payloadへfallbackし、セッションメタデータは残す。

### Cloudごとに能力差を残す

存在しない完全transcript APIを推測してscrapeしない。Claude CloudはHTTP hookで
表示messageを組み立てる。Codex Cloudは公開CLIのtask/status/diffと、
wrapper経由の初回promptを保存する。

### 自動commitしない

VaultがGit管理されていても、commit/pushは利用者固有の運用に委ねる。
このツールの責務はVault内のMarkdown生成までとする。

## 信頼境界

- collectorをloopback以外へbindする場合、Bearer tokenを必須にする。
- TLSはreverse proxyまたはmanaged platformで終端する。
- remote envelopeのfingerprintはcollector側で再計算する。
- `transcript_path`はremoteから受け入れず、collector filesystemを読ませない。
- Vault相対パスを検証し、Vault外へ書かない。
- state directoryと設定fileは可能な環境でowner-only permissionにする。

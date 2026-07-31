# Architecture

## 目的

Vaultへは直接同期しない。取得・正規化・描画を分離し、別ワークスペースや
別マシン、Cloud環境、サービスのdata exportを同じ運用へ載せる。

## データフロー

1. ローカルの`capture`、Cloud poller、export importerがイベントを受け取る。
2. Claude Code on the webのHTTP hookはCloudflare Workerが受け、D1へ保存する。
3. `normalize_event`がsource、device、session、時刻、project情報を付与し、
   credentialらしい値をredactする。Workerでも同等の処理を先に行う。
4. SQLiteへappend-onlyで保存する。同じfingerprintのイベントは追加しない。
5. adapterがtranscriptまたはhook payloadからuser/assistant messageを復元する。
6. ローカルhookでは対象セッションだけを即時同期する。timerやpullでは全変更を
   探し、セッションごとの安定したパスへMarkdownを原子的に書く。

## 判断

### SQLiteを中間層にする

狙いは耐障害性にある。hookの実行時間を短くし、Obsidianが閉じている間も
イベントを失わない。
Claude Cloudの受信箱とVault writerも分離できる。

### Vaultはfilesystemへ書く

Markdown Vaultの基本インターフェースであり、Obsidian pluginやLocal REST APIを
必須にしない。CloudからVaultを直接操作せず、Vault端末からpullする。

### transcript parserをbest-effortにする

CodexとClaude Codeのローカルtranscriptは便利だが、安定APIとは限らない。
読み取れなければhook payloadへfallbackし、セッションメタデータは残す。

### Cloudごとに能力差を残す

非公開APIは使わない。Claude CloudはHTTP hookで
表示messageを組み立てる。Codex Cloudは公開CLIのtask/status/diffと、
wrapper経由の初回promptを保存する。

### Claude Cloudの受信をWorkerとD1に限定する

Vaultは公開しない。個人端末でHTTP serverを保守する代わりに、Cloudflareの公開
HTTPS endpointだけをClaude Code on the webへ渡す。D1はappend-onlyの受信箱として
使い、Vault端末がcursor付きでpullする。汎用的なevent投稿APIや削除APIは持たせない。

### 自動commitしない

commitまでは行わない。VaultがGit管理されていても、commit/pushは利用者固有の
運用に委ねる。
このツールの責務はVault内のMarkdown生成までとする。

## 信頼境界

- Workerへの書き込みには`INGEST_TOKEN`、読み取りには別の`PULL_TOKEN`を使う。
- Workerは`CLAUDE_CODE_REMOTE=true`のhookだけを受け付ける。
- Workerはevent名を固定listで検証し、credentialをredactしてからD1へ保存する。
- Workerとローカルの双方でfingerprintを再計算する。
- `transcript_path`はWorkerで破棄し、remoteから指定されたpathをローカルで読まない。
- request bodyとpull件数に上限を設ける。D1は外部から直接公開しない。
- Vault相対パスを検証し、Vault外へ書かない。
- state directoryと設定fileは可能な環境でowner-only permissionにする。

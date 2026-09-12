# Architecture

## 目的

Vaultへは直接同期しない。取得・正規化・描画を分離し、別ワークスペースや
別マシン、Cloud環境、サービスのdata exportを同じ運用へ載せる。

## データフロー

1. ローカルの`capture`、Cloud poller、export importerがイベントを受け取る。
2. Claude Code on the webのcommand hookがcurlでWorkerへ転送し、D1へ保存する。
3. `normalize_event`がsource、device、session、時刻、project情報を付与する。
   `finalize_event`は全取込経路でメタデータ付与後に資格情報をマスクし、
   保存する値からfingerprintを計算する。検出ルールはPythonではdetect-secrets、
   WorkerではSecretlintを使用し、ベンダーごとのパターンは自前で保守しない。
   session IDがない入力では、マスク後のpayloadから仮IDを生成する。
   Codex Cloudの`exec`でtask IDが返らない場合は、実行ごとにUUIDを生成する。
   検出対象のIDはSHA-256由来の仮名に置き換え、保存・描画時の識別を保つ。
   マスク前のdevice IDとsession IDの組をハッシュ化した`identity_key`も保存し、
   仮名と同じ文字列が生のIDとして入力されても区別する。hook本文からは受け取らず、
   Workerの保存済みenvelopeからpullするときだけ引き継ぐ。
4. SQLiteへappend-onlyで保存する。同じfingerprintのイベントは追加しない。
   `MessageDisplay`は`finalize_event`から保存層へメモリ内で引き渡し、payloadを
   AES-256-GCMで暗号化して`message_chunks`へ保管する。全断片が揃ったら全文を検査し、
   マスク済みイベントの追加・受信済み記録・暗号文の削除を同じトランザクションで行う。
   Workerも同じ方針でD1のbatchを使う。詳細は[鍵の管理](message-buffer.md)を参照。
   セッションの列挙・検索はこの保存済み識別値を使い、現在の検出ルールには依存しない。
   旧イベントには空の`identity_key`列を追加し、既存の値とfingerprintは書き換えない。
   由来を記録していない旧IDは生のIDとして扱い、仮名らしい文字列から元のIDを推測しない。
   そのため、旧版ですでに仮名化された履歴は元のIDによる新規履歴と自動では結合しない。
5. adapterがtranscriptまたはhook payloadからuser/assistant messageを復元する。
   rendererは復元・結合後の本文、タイトル、diff、メタデータを再度マスクする。
   既存イベントにも適用するが、append-onlyのイベント自体は書き換えない。
6. ローカルhookでは対象セッションだけを即時同期する。timerやpullでは全変更を
   探し、セッションごとの安定したパスへMarkdownを原子的に書く。
   旧IDに紐づく描画先も引き継ぐ。重複した描画状態は統一し、不要なノートは
   生成時のハッシュ一致と他セッションからの参照がないことを確認してから削除する。

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

非公開APIは使わない。Claude Cloudはcommand hookからのHTTP転送で
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
- command hookは`CLAUDE_CODE_REMOTE=true`のときだけcurlを実行する。
- Workerも`X-Claude-Code-Remote: true`のrequestだけを受け付ける。
- Workerはevent名を固定listで検証し、credentialをredactしてからD1へ保存する。
- 未完成のMessageDisplayは暗号文としてのみ保管し、通常イベントの取得APIには出さない。
  鍵はDBから分離する。対象はDB単体の流出で、端末やWorkerの実行環境の侵害は対象外。
- 検出時の外部APIによる資格情報の検証は行わない。会話内のallowlistコメントを
  検出除外として解釈せず、検出処理が失敗した場合も平文の保存・出力へ戻さない。
- Workerとローカルの双方でfingerprintを再計算する。
- `transcript_path`はWorkerで破棄し、remoteから指定されたpathをローカルで読まない。
- request bodyとpull件数に上限を設ける。D1は外部から直接公開しない。
- Vault相対パスを検証し、Vault外へ書かない。
- state directoryと設定fileは可能な環境でowner-only permissionにする。

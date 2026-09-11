# Claude Cloud受信Worker

Claude Code on the webのcommand hookからイベントを受け、redact後にD1へ保存します。
Vaultやローカル端末をインターネットへ公開する必要はありません。

資格情報の検出にはSecretlintの推奨ルールを使います。認証フィールドとURL内の
ユーザー情報も除去し、会話中の`secretlint-disable`コメントは受け付けません。
資格情報を外部へ送って検証する処理はありません。ルール更新はnpm依存を更新して
テストし、`npm run check`でWorkerのビルドを確認します。`nodejs_compat`は
Secretlintが使うNode.js APIのために必要です。
検査はpayload内の文字列をまとめて1回実行し、検出位置から各フィールドへマスクを
戻します。CIでも`npm run check`を実行し、deployのdry-runまで確認します。

## Deploy

Node.js 22以上とCloudflare accountを用意します。初回の`deploy`でWorkerとD1が
作成され、`wrangler.jsonc`へD1のIDが追記されます。

```bash
cd deploy/cloudflare-worker
npm install
npx wrangler login
npm run deploy
npm run migrate
```

次に、用途の異なるtokenを2つ生成します。同じ値を使わないでください。

```bash
openssl rand -hex 32
openssl rand -hex 32
npx wrangler secret put INGEST_TOKEN
npx wrangler secret put PULL_TOKEN
```

各`secret put`のpromptへ、生成したtokenを1つずつ入力します。deploy結果に表示された
`https://...workers.dev`を控え、設定が揃ったことを確認します。

さらに[暗号鍵の管理手順](../../docs/message-buffer.md#workerの鍵)に従い、
`BUFFER_ENCRYPTION_KEYS`を登録してください。既存環境を更新するときも
`npm run migrate`で`0002_message_buffer.sql`を適用し、鍵を登録してから
新しいWorkerでのhook受信を有効にします。

```bash
curl https://agent-session-exporter.example.workers.dev/health
```

`{"status":"ok"}`なら準備完了です。個人利用の小規模なhookを想定しています。
利用量はCloudflare dashboardで確認してください。

## Claude Code on the web

hook設定を生成します。

```bash
ase hooks --source claude-cloud \
  --inbox-url https://agent-session-exporter.example.workers.dev
```

出力をprojectの`.claude/settings.json`へマージします。以前の`type: "http"`設定や
`.claude/settings.local.json`の検証用hookは削除し、同じイベントを二重送信しない
状態にしてください。Claude Code on the web側では、次の2点も設定します。

- 環境変数`AGENT_SESSION_EXPORTER_INGEST_TOKEN`へ`INGEST_TOKEN`を設定する
- network accessでWorkerのhostnameを許可する

生成されるcommand hookは`CLAUDE_CODE_REMOTE=true`のときだけcurlを実行します。
Cloud環境では`X-Claude-Code-Remote: true`を付け、Workerから202が返れば保存経路まで
到達しています。同じproject設定をローカルで使っても通信しません。

## Workerへの到達を確認する

別のterminalでLive Tailを開始します。

```bash
cd deploy/cloudflare-worker
npx wrangler tail --format pretty
```

アクセスログのstatusは次の意味です。

- `202`: 認証、event検証、D1への書き込みまで完了した
- `204`: `X-Claude-Code-Remote`が`true`でないため保存しなかった
- `401`: `INGEST_TOKEN`が一致しない
- `400`または`415`: eventまたはrequest bodyが不正
- `503`: D1 bindingや内部処理を確認する必要がある

## Vault端末

`~/.config/agent-session-exporter/config.toml`へ受信箱を設定します。

```toml
[claude_cloud]
url = "https://agent-session-exporter.example.workers.dev"
token_env = "AGENT_SESSION_EXPORTER_PULL_TOKEN"
timeout_seconds = 3.0
```

`PULL_TOKEN`を環境変数へ設定し、取得とVault同期を実行します。

```bash
export AGENT_SESSION_EXPORTER_PULL_TOKEN='pull専用token'
ase pull --sync
```

systemd user timerを使う場合は、次の内容をownerだけが読めるfileへ保存します。

```bash
mkdir -p ~/.config/agent-session-exporter
printf '%s\n' 'AGENT_SESSION_EXPORTER_PULL_TOKEN=pull専用token' \
  > ~/.config/agent-session-exporter/claude-cloud.env
chmod 600 ~/.config/agent-session-exporter/claude-cloud.env
```

serviceとtimerの雛形は[`../systemd-user`](../systemd-user/)にあります。

## Endpoint

- `POST /v1/hooks/claude-cloud`: 暗号化バッファまたは通常イベントへの保存時は202、remote対象外は204
- `GET /v1/events?after=0&limit=500`: Vault端末専用。`PULL_TOKEN`で認証する
- `GET /health`: bindingとsecretの設定状態だけを返す

通常の`events`はappend-onlyで、同じfingerprintは`UNIQUE`制約により重複保存しません。
分割メッセージの暗号化バッファは、マスク済みイベントの保存と同時に削除します。
未完成メッセージはpullに含めません。外部向けの汎用投稿・削除APIは用意していません。

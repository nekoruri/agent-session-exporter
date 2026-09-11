# 分割メッセージと暗号鍵の管理

`redact = true`のPython CLIとClaude Cloud Workerは、`MessageDisplay`の断片を
メッセージ単位で組み立ててから検査します。途中のpayloadはAES-256-GCMで暗号化して
`message_chunks`へ保管し、全indexと`final`が揃うまで通常の`events`には追加しません。
検査には既存のdetect-secrets／Secretlintを使います。

マスク済みイベントの追加、受信済み記録の作成、一時データの削除は一つの
トランザクションで行います。受信済み記録にはメッセージ本文を含めず、再送による
重複保存を防ぎます。nonceは暗号化のたびに生成し、メッセージ識別子・index・final・
鍵IDも認証対象にします。DBやSQL引数に平文の断片や暗号鍵を渡しません。

対象はDB単体の流出です。端末の同一ユーザー権限やWorkerの実行環境の侵害は対象外です。
実行中のメモリには復号した本文が存在します。元のtranscript、既存イベント、
既存バックアップの書き換えは行いません。公開前の既存データの確認は別途必要です。

## ローカルの鍵

最初の分割メッセージ受信時に、自動で専用鍵を生成します。既定の保存先は
設定ファイルと同じディレクトリの`config.buffer-keys.json`です。
設定ファイル名が異なる場合は、その拡張子を`.buffer-keys.json`に置き換えます。
`buffer_key_path`で保存先を指定することもできます。

鍵のディレクトリは`700`、ファイルは`600`で保護します。state directoryやVaultの
内側やGitの作業ツリーには配置できません。鍵ファイルはDBごとに専用にし、他のDBと共有しないでください。
現在の鍵ファイル管理はPOSIXのファイル権限と`flock`を使用します。

```bash
ase buffer-keys                  # 初期化／状態確認。鍵IDだけを表示する
ase buffer-keys --rotate         # 新鍵を追加して有効化。旧鍵は残す
ase buffer-keys --retire KEY_ID  # 使用中でも有効でもない旧鍵を削除する
```

途中の断片がある状態で鍵ファイルが消えた場合は、自動生成せずエラーにします。
鍵の読取り・復号・資格情報検出に失敗しても、平文で保存する処理へは切り替えません。
鍵と未完成メッセージを復旧したい場合は、DBとは別の保管先へ鍵をバックアップしてください。
古いDBバックアップ内の未完成メッセージを復旧するには、その時点の鍵も必要です。
完成済みのマスク済み履歴には暗号鍵は不要です。

## Workerの鍵

ローカル用とは別の鍵を生成し、`BUFFER_ENCRYPTION_KEYS`というWorkers Secretへ
登録します。`INGEST_TOKEN`／`PULL_TOKEN`とも別の値にします。
Workerからpullするのは完成・マスク済みイベントだけなので、Workerの鍵を
pullクライアントへ配布する必要はありません。

Secretの値は`{"active":"鍵ID","keys":{"鍵ID":"32バイトの鍵を小文字hexで表した値"}}`
というJSONです。鍵IDは鍵のバイト列のSHA-256の先頭16桁です。
初回の生成と登録は次のように行えます。更新時に旧鍵を引き継げるよう、
鍵JSONをリポジトリ外の専用ファイルにも保管します。値は端末へ表示しません。

```bash
cd deploy/cloudflare-worker
buffer_key_file="${XDG_CONFIG_HOME:-$HOME/.config}/agent-session-exporter/worker-buffer-keys.json"
node --input-type=module -e '
import { randomBytes, createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
const key = randomBytes(32);
const active = createHash("sha256").update(key).digest("hex").slice(0, 16);
const path = process.argv[1];
mkdirSync(dirname(path), {recursive: true, mode: 0o700});
writeFileSync(path, JSON.stringify({active, keys: {[active]: key.toString("hex")}}),
  {flag: "wx", mode: 0o600});
' "$buffer_key_file"
npx wrangler secret put BUFFER_ENCRYPTION_KEYS < "$buffer_key_file"
```

生成コマンドは既存の鍵ファイルを上書きしません。登録時にはWorkerが再デプロイされます。
この専用ファイルは暗号化したバックアップや秘密情報管理サービスにも保管してください。
Wrangler設定の`vars`、Git、D1には入れません。
詳細は[Workers Secretsの公式手順](https://developers.cloudflare.com/workers/configuration/secrets/)を参照してください。

鍵を更新する際は、現在の`keys`を保持したJSONに新しい鍵を追加して`active`を変更し、
Secretを再登録します。古いWorkerバージョンが旧鍵で新規書込みしなくなったことと、
次の集計で旧鍵の利用がなくなったことを確認してから、その鍵をJSONから除きます。
バックアップの復旧に必要な旧鍵は別途保管してください。

インストール済みCLIの鍵管理処理を使って、専用ファイルに新鍵を追加できます。
次のコマンドは旧鍵を保持してファイルを原子的に更新します。

```bash
python3 - "$buffer_key_file" <<'PY'
import sys
from pathlib import Path
from agent_session_exporter.stream_buffer import locked_keys, new_key, write_keys
path = Path(sys.argv[1])
with locked_keys(path) as keys:
    new_key(keys)
    write_keys(path, keys)
PY
npx wrangler secret put BUFFER_ENCRYPTION_KEYS < "$buffer_key_file"
```

```sql
SELECT key_id, COUNT(*) AS pending_chunks FROM message_chunks GROUP BY key_id;
```

新しいコードの利用前に`0002_message_buffer.sql`を適用してください。
`/health`は鍵の設定不備も`503`で返します。新規デプロイでは、DB作成後にmigrationと
Secret登録を完了してからhookの送信を有効にしてください。

## 未完成メッセージと上限

indexの順序が前後しても、0から最終indexまで揃うまでは暗号化したまま保持します。
空の最終deltaも受け付けます。同じindexで内容の異なる再送や矛盾したfinalは拒否します。
メッセージIDが秘密値として検出されても、固有の仮名に変換して別のメッセージと区別します。

1断片のpayloadは1 MiB、1メッセージは4096断片までです。
保存する暗号文の合計は1メッセージ8 MiB、DB全体64 MiBを上限とします。
WorkerではBase64化した暗号文のサイズを数えるため、同じ本文でもPythonより早く
上限に達する場合があります。
完成したWorkerイベントには、[D1の1行2,000,000バイトの上限](https://developers.cloudflare.com/d1/platform/limits/)も
適用されます。マスク後の本文とメタデータが収まらなければ保存をエラーにし、
暗号化バッファを保持します。

不足した断片や一時エラーは、同じmessage_id・index・payloadの再送で再試行します。
未完成データを自動削除する期限は設けていません。上限に達した場合は既存データを
維持してエラーにし、受信に成功した扱いにはしません。
送信元から不足分を再送するか、不要と確認したメッセージの暗号化バッファを管理者が
削除して容量を回復します。DBへの管理操作には事前にバックアップを取ってください。

未完成のhook本文は通常のイベント一覧・pull・hook由来のMarkdownには出しません。
ローカルにtranscriptがある場合は、従来どおりその内容を検査してMarkdownへ反映します。
`redact = false`のPython CLIは従来どおり平文保存を許可するため、この保護の対象外です。

# agent-session-exporter

Codex / Claude の会話イベントを収集し、SQLiteで正規化・重複排除したうえで、
Obsidian Vault向けのMarkdownへ変換するPython 3.11以上のCLIです。
資格情報の検出にはdetect-secretsを使用します。

## 構成

- `src/agent_session_exporter/core.py`: 設定、イベント正規化、SQLite保存
- `src/agent_session_exporter/redaction.py`: ライブラリによる資格情報検出とマスク
- `src/agent_session_exporter/adapters.py`: transcriptやhookイベントから会話を復元
- `src/agent_session_exporter/renderer.py`: Markdown生成とVault同期
- `src/agent_session_exporter/cli.py`: `ase` コマンドの入口
- `src/agent_session_exporter/hooks.py`: Codex / Claude hook設定
- `src/agent_session_exporter/importers.py`: ChatGPT / Claude export取込
- `src/agent_session_exporter/claude_cloud.py`: Worker受信箱からのpull
- `src/agent_session_exporter/codex_cloud.py`: Codex Cloud CLI連携
- `deploy/cloudflare-worker/`: Claude Cloud hookを受けるWorkerとD1 schema
- `tests/`: unittest、`docs/architecture.md`: 設計判断

## 開発

```bash
python3 -m pip install -e .
env PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

開発用の仮想環境に依存関係をインストールしてから検査してください。
Workerの資格情報検出にはSecretlintを使用し、`deploy/cloudflare-worker`で
`npm ci`、`npm test`を実行します。ベンダーごとの検出パターンは自前で追加せず、
依存ライブラリを更新します。検出時に資格情報を外部へ送信しないでください。

SQLiteのappend-only保存とfingerprintによる重複排除、credentialのredact、
Vault外への書き込み防止を維持してください。非公開DBやDOMのscrapeには依存しません。

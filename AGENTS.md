# agent-session-exporter

Codex / Claude の会話イベントを収集し、SQLiteで正規化・重複排除したうえで、
Obsidian Vault向けのMarkdownへ変換するPython 3.11以上のCLIです。
実行時の外部依存はありません。

## 構成

- `src/agent_session_exporter/core.py`: 設定、イベント正規化、redact、SQLite保存
- `src/agent_session_exporter/adapters.py`: transcriptやhookイベントから会話を復元
- `src/agent_session_exporter/renderer.py`: Markdown生成とVault同期
- `src/agent_session_exporter/cli.py`: `ase` コマンドの入口
- `src/agent_session_exporter/hooks.py`: Codex / Claude hook設定
- `src/agent_session_exporter/importers.py`: ChatGPT / Claude export取込
- `src/agent_session_exporter/remote.py`, `server.py`: remote転送、pull、HTTP collector
- `tests/`: unittest、`docs/architecture.md`: 設計判断

## 開発

```bash
env PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

SQLiteのappend-only保存とfingerprintによる重複排除、credentialのredact、
Vault外への書き込み防止を維持してください。非公開DBやDOMのscrapeには依存しません。

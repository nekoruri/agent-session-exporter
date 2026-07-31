# Obsidian ingestを安定させる6件の改善

## この文書で6件の実装範囲を固定する

実際のVaultでは、会話本文のないスタブ、実プロンプトを表さないタイトル、
継続セッションに追随していないdigestが確認された。また、イベント名の表記揺れ、
保存先変更後のrender state、UTC固定のパス、`archived_at`の意味にも課題がある。

この文書では、改善を独立した6件のPRに分ける。SQLiteのappend-only保存、
fingerprintによる重複排除、credentialのredact、Vault外への書き込み防止は
すべてのPRで維持する。会話本文を得られない環境から非公開データを取得する処理は
追加しない。

| 項目 | Pull request |
|---|---|
| ingest判定用メタデータ | [#5](https://github.com/nekoruri/agent-session-exporter/pull/5) |
| 実ユーザープロンプトのタイトル | [#6](https://github.com/nekoruri/agent-session-exporter/pull/6) |
| イベント名とworkspace情報の正規化 | [#7](https://github.com/nekoruri/agent-session-exporter/pull/7) |
| destination変更後のrender state修復 | [#8](https://github.com/nekoruri/agent-session-exporter/pull/8) |
| パス生成用timezone | [#9](https://github.com/nekoruri/agent-session-exporter/pull/9) |
| 更新・描画・アーカイブ時刻 | [#10](https://github.com/nekoruri/agent-session-exporter/pull/10) |

## PR 1: ingestが本文の有無と更新を判定できるようにする

Markdownのfrontmatterへ次の項目を追加する。

- `content_kind`: 会話があれば`transcript`、なければ`metadata_only`
- `message_count`: 描画対象のメッセージ数
- `event_count`: SQLiteに保存されたセッションイベント数
- `revision`: 順序付きevent fingerprint列から作るSHA-256

同じイベント集合からは同じ`revision`を生成し、イベントが増えれば値が変わることを
テストする。時刻プロパティの整理はPR 6で扱う。

## PR 2: 実ユーザープロンプトをタイトルにする

import済みタイトルがある場合は、これまでどおり最優先する。ローカルhookでは
`UserPromptSubmit`のpromptをtranscriptの先頭メッセージより優先する。
AGENTS.md、`<environment_context>`、`<command-...>`などの制御用テキストは
タイトル候補から除外する。候補がなければ現在のsource・project表記へ戻す。

## PR 3: イベント名とworkspace情報を正規化する

`sessionEnd`など既知イベント名の大文字・小文字の違いをcanonical名へそろえる。
`cwd`がなく、`workspace_roots`にルートが1件だけある場合は、その値をproject判定に
利用する。remote collectorに渡されたパスからcollector側のファイルは読まない。

## PR 4: destination変更後のrender stateを安全に修復する

`ase doctor`は、render stateが存在しないファイルまたは現在のdestination外を
指している場合に警告する。修復コマンドは既定でdry-runとし、明示指定された場合だけ
generated markerを持つノートの移動または既存ファイルへの再紐付けを行う。
対象はVault内に限定し、通常ノートや別セッションを上書きしない。最新の移動元と
同じセッションの古い生成ノートが移動先にある場合は、古い内容をhash付きの隠し
バックアップへ退避してから最新内容へ置き換える。

## PR 5: パス生成に使うタイムゾーンを設定可能にする

IANA timezone名を設定へ追加し、年・月・ファイル名の日時をそのtimezoneへ変換する。
`ase init`ではOSから取得したIANA timezone名を初期値とし、取得できない場合はUTCへ
戻す。既存設定に項目がない場合も同じ検出値を実行時に使うが、設定ファイルは
自動更新しない。不正なtimezone名は設定読込時にエラーとし、同じセッションの
既存パスはrender stateにより維持する。

## PR 6: セッションの更新・描画・アーカイブ時刻を分ける

最後のイベント時刻を`updated_at`、実際にMarkdownを書いた時刻を`rendered_at`として
出力する。`archived_at`は完了または失敗したセッションだけに付け、activeやstoppedの
セッションからは省く。時刻追加によって変更のないセッションが毎回再描画されないことを
テストする。

## 完了条件

各PRでunittestとcompileallを通し、READMEまたはこの文書へ利用者向けの変更を残す。
6件を統合した状態でも全テストが成功し、既存の保存・redact・Vault境界のテストが
後退していないことを最終確認する。

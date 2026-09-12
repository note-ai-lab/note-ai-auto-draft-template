# note AI自動下書き投稿テンプレート

Google Gemini(AI)・GitHub Actions・noteを連携させて、記事作成からnoteへの下書き保存までを
半自動化するためのテンプレートです。

## できること

- 記事の切り口(想定読者)を1つ指定するだけで、Gemini AIが導入文を自動生成
- 生成した記事をnoteの下書きに自動保存
- 価格設定・公開は仕様上手動(下書き確認後、ご自身で設定してください)

## 必要なもの

- GitHubアカウント(無料)
- note.comアカウント
- Google Gemini APIキー([aistudio.google.com](https://aistudio.google.com)で無料発行。支払い情報の登録も不要です)

## 使い方

詳しいセットアップ手順は、購入いただいた記事内の解説をご覧ください。

概要:
1. このテンプレートを「Use this template」から複製
2. GitHubのSecretsに `NOTE_EMAIL` / `NOTE_PASSWORD` / `NOTE_AUTH_STATE_B64` / `GEMINI_API_KEY` を登録
3. `cookie_converter.html` を使って認証情報を作成
4. `topic_example.json` の "angle"(切り口)を記入
5. GitHub Actionsの「Run workflow」で実行

## ファイル構成

| ファイル | 役割 |
|---|---|
| `note_auto_post.py` | メインスクリプト |
| `requirements.txt` | 必要なライブラリ |
| `topic_example.json` | 記事の切り口(想定読者)を指定するファイル |
| `.github/workflows/post.yml` | GitHub Actionsの実行設定 |
| `cookie_converter.html` | 認証情報変換ツール |

## 注意事項

- リポジトリは必ずPrivateで運用し、Secretsに登録した情報は第三者に共有しないでください
- 生成された記事は公開前に必ず内容を確認してください
- Gemini APIは無料枠に利用回数の上限があります。頻繁に実行する場合は制限に注意してください

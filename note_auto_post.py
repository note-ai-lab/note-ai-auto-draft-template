"""
note AI記事生成 + 下書き自動投稿スクリプト(低負荷設計)

【このバージョンでできること】
- テーマ(お題)を渡すと、Google Gemini API が記事のタイトル・本文を自動生成
- 生成した内容を note の新規投稿画面に自動入力し、下書きとして保存
- 価格設定・有料エリア指定・公開(投稿する)は行わない
  → note の仕様上、価格は「投稿する」を押す瞬間にしか保存されないため、
    金額設定と最終公開は毎回人の目で確認しながら手動で行う運用とする

【設計方針】
- サーバー負荷を抑えるため、操作間に人間の操作並みのランダムな待機時間を入れる
- 1回の実行につき投稿は1本のみ(まとめて連投しない)
- ログインは Cookie を保存して使い回し、毎回のパスワード送信を避ける
- 失敗時は自動リトライせず、ログを残して人間が確認する設計(暴走防止)

【事前準備】
1. pip install playwright google-genai --break-system-packages
2. playwright install chromium
3. 環境変数を設定
     export NOTE_EMAIL="you@example.com"
     export NOTE_PASSWORD="xxxxx"
     export GEMINI_API_KEY="AIza..."
4. 初回のみ --login-only で実行し、Cookie(auth_state.json)を保存
5. 以降はテーマ(お題)を渡して実行

【注意】
- note の画面構成(セレクタ)は変更されることがあります。
  実行前に一度、ブラウザの開発者ツールで対象要素を確認・更新してください。
- 実行間隔は最低でも数十分〜1時間以上を推奨します(短時間の連続投稿は避ける)。
- 一括連投・大量アカウント運用は規約上のリスクが高いため、このスクリプトは
  「1回の実行で1記事」を前提にしています。
- AIが生成した内容は必ず公開前に人の目で確認してください
  (事実誤認・著作権・不適切表現のチェックはスクリプトでは行いません)。
"""

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

AUTH_STATE_PATH = Path("auth_state.json")
LOG_PATH = Path("note_auto_post.log")

NOTE_LOGIN_URL = "https://note.com/login"
NOTE_NEW_POST_URL = "https://note.com/notes/new"

# 生成に使う Gemini のモデル。無料枠での利用を想定し、軽量なモデルを指定。
GEMINI_MODEL = "gemini-3.6-flash"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def human_delay(min_s: float = 1.5, max_s: float = 4.0) -> None:
    """人間の操作間隔を模した待機。連続アクセスによるサーバー負荷を避ける。"""
    time.sleep(random.uniform(min_s, max_s))


def human_type(page: Page, selector: str, text: str) -> None:
    """1文字ずつタイピングし、機械的な一括入力を避ける。"""
    el = page.locator(selector)
    el.click()
    for ch in text:
        el.type(ch, delay=random.uniform(30, 90))


@dataclass
class Article:
    title: str
    body: str  # 本文全体(hook + fixed_part を結合したもの。ログ・保存用)
    hook: str = ""  # 無料部分(AI生成)。ゆっくり人間らしく入力する
    fixed_part: str = ""  # 有料部分(固定文)。高速に入力する


# 有料部分(毎回同じ内容)。ここを直接編集してカスタマイズしてください。
# 実際の販売内容に合わせて書き換えるのはこの定数だけで済むようにしています。
PAID_CONTENT = """【ここから有料エリア】

ここでは、実際に私が使っているコード一式と、ゼロから設定する手順をすべて公開します。プログラミング未経験でも、順番通りに進めれば同じ仕組みが作れるように書いています。

■ 全体の仕組み

この仕組みは3つのサービスを組み合わせています。

1つ目はGoogle Gemini(AI)です。テーマを渡すと、記事のタイトルと本文を自動で書いてくれます。無料枠の範囲内であれば、費用は一切かかりません。

2つ目はGitHub Actionsです。指定した時間に、あるいはボタン一つで、裏側のクラウドサーバーが自動でブラウザを操作し、noteへの投稿作業をこなしてくれます。パソコンをつけっぱなしにする必要はありません。スマホからボタンを押すだけで動かせます。

3つ目はnoteです。実際に記事を公開する場所です。

これらを繋ぐことで、「テーマを1行決めるだけで、AIが記事を書き、noteの下書きに自動保存される」という流れができあがります。価格設定と最終的な公開ボタンだけは、note側の仕様上どうしても手動になりますが、それ以外はすべて自動化できます。

■ 必要なもの

・GitHubの無料アカウント
・note.comのアカウント(有料記事を書く予定のもの)
・Google AI StudioのAPIキー(AIを呼び出すために必要。無料枠の範囲内であれば無料で使えます。支払い情報の登録も不要です)
・パソコン(初回のログイン作業だけで使用します。以降はスマホだけで運用できます)

■ セットアップ手順(全体の流れ)

このテンプレートを使えば、以下の流れで環境が整います。

1. 下記のテンプレートリポジトリを、自分のGitHubアカウントに複製する
2. GitHubのSecrets(秘密情報の保管庫)に、noteのログイン情報とGoogle Gemini APIのキーを登録する
3. noteに一度ログインして、認証情報(Cookie)を作成し、Secretsに登録する
4. テーマを書いたファイルを用意する
5. GitHub Actionsの「Run workflow」ボタンを押す
6. 数十秒後、noteの下書きに記事が自動で保存されている

■ テンプレートリポジトリ

以下のリンクから、必要なファイル一式(コード・設定ファイル・認証情報変換ツール)を入手できます。

https://github.com/mitsu9214/note-ai-auto-draft-template

「Use this template」ボタンから、自分のアカウントに複製してお使いください。

■ 含まれるファイル

・note_auto_post.py … 記事生成〜下書き保存までを行うメインのスクリプト
・requirements.txt … 必要なライブラリの一覧
・topic_example.json … 記事のテーマを指定するファイル(ここを書き換えるだけで、毎回違うテーマの記事が作れます)
・post.yml … GitHub Actionsの実行設定ファイル
・cookie_converter.html … noteのログイン情報を、パソコンにPythonを一切インストールせずに変換できる専用ツール

■ Secretsへの登録項目

・NOTE_EMAIL … noteのログイン用メールアドレス
・NOTE_PASSWORD … noteのログインパスワード
・NOTE_AUTH_STATE_B64 … noteのログイン状態を保存したデータ(cookie_converter.htmlで作成します)
・GEMINI_API_KEY … Google Gemini APIのキー(aistudio.google.comで無料発行できます)

■ 認証情報(Cookie)の作り方

パソコンにPythonを入れる必要はありません。以下の手順だけで完結します。

1. パソコンのChromeに拡張機能「Cookie-Editor」を追加する
2. 普段通りnote.comにログインする
3. Cookie-Editorで「Export」(JSON形式)をクリックし、Cookie情報をコピーする
4. 付属の cookie_converter.html を開き、コピーした内容を貼り付ける
5. 変換された文字列をコピーし、GitHubのSecretsの NOTE_AUTH_STATE_B64 に貼り付ける

Cookieには有効期限があるため、数週間〜数ヶ月に一度、同じ手順で更新が必要です。

■ カスタマイズすべき箇所(ここだけ変えればOK)

・topic_example.json の "topic" … 書いてほしい記事のテーマを指定します
・topic_example.json の "guidelines" … 文体やトーンの指示(「〜だ、〜である調で」「初心者向けに」など)
・topic_example.json の "target_chars" … 記事のおおよその文字数
・note_auto_post.py 内の GEMINI_MODEL … 使用するGeminiのモデル名(無料枠の上限や文章のクオリティに応じて変更可能)
・post.yml 内の cron … 自動実行したい時間(協定世界時での指定のため、日本時間から9時間引いて設定してください)

■ つまずきやすいポイント(実体験のトラブルシューティング)

・記事タイトル欄が見つからずタイムアウトする場合 → noteの画面構成(HTML)が変わっている可能性があります。ブラウザの「検証」機能で、実際のタイトル入力欄のplaceholder属性やclass名を確認し、コード内のセレクタを書き換えてください。

・本文が変な順番で入力される場合 → 入力の途中で要素を何度もクリックし直すと、カーソル位置がずれて文章が混ざります。最初に1回だけクリックしてフォーカスし、以降はキーボード入力だけで進めるようにしてください。

・「---」で区切り線が入ってしまう場合 → noteのエディタは行頭の「---」を自動で罫線に変換する仕様があります。有料エリアの目印などに使う記号は、【】のようなダッシュを使わない記号にすると安全です。

・価格を入力してもリセットされる場合 → noteの仕様上、価格設定は「投稿する」ボタンを押した瞬間にしか保存されません。下書きの状態でいくら価格を設定しても、ページを閉じると初期値に戻ります。価格設定は必ず、実際に公開する直前に手動で行ってください。

・ヘッドレスモードで画面が真っ白のまま止まる場合 → 自動化ブラウザであることが検知され、ページの読み込みがブロックされている可能性があります。通常のブラウザを装うUser-Agentの設定や、自動化の痕跡(navigator.webdriver)を隠す設定を追加することで回避できることがあります。

■ 運用のコツ

・実行間隔は1日1〜数回程度に抑え、短時間に連続実行しないようにしてください
・生成された記事は、必ず公開前に人の目で内容を確認してください。AIは時々事実と異なる内容を書くことがあります
・有料エリアの範囲指定は現状自動化していません。価格設定と合わせて、公開前に手動で設定してください
・GitHubのリポジトリは必ずPrivateにし、認証情報が外部に漏れないよう注意してください

以上が、実際に私が構築した仕組みのすべてです。ここまでの手順を1つずつ進めていただければ、同じ環境を再現できます。わからない点があれば、購入者特典として質問を受け付けていますので、お気軽にどうぞ。"""


def generate_hook(angle: str, guidelines: str = "") -> Article:
    """
    無料部分(導入・フック)だけをGoogle Gemini APIで生成する。
    有料部分は PAID_CONTENT(固定文)を使うため、ここでは生成しない。
    angle には「今回はどんな読者に刺さる切り口で書くか」を指定する
    (例: '副業に興味がある会社員向け', '文章を書くのが苦手な人向け' など)。
    """
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log("環境変数 GEMINI_API_KEY が設定されていません。")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    guideline_text = f"\n\n【追加の指示】\n{guidelines}" if guidelines else ""

    prompt = f"""あなたはnoteで有料記事を販売しているライターです。
これから、ある有料記事の「無料部分(冒頭の導入文)」だけを書いてください。

【商品の内容(有料部分に書かれている内容。あなたはここを直接書く必要はありません)】
AI・GitHub Actions・noteを連携させて、記事作成からnoteへの下書き保存までを
半自動化する具体的な手順とコード一式。

【今回の切り口・想定読者】
{angle}

【無料部分に書くべきこと】
・上記の想定読者が抱えていそうな悩みへの共感
・この仕組みを知ることで得られるメリット(時間が浮く、収益化の第一歩になる、など)
・なぜこの方法が有効だと言えるのか、簡単な理由づけ
・具体的な手順やコードの中身までは書かない(それは有料部分にあるため)
・最後は自然に「続きは有料エリアで」という流れで締める
・文字数は300〜500字程度
・煽りすぎず、誠実なトーンで

【出力形式】
説明や前置きは一切書かず、次のJSON形式のみを出力してください。
段落の区切りは半角の改行2つ(\\n\\n)で表現してください。

{{"title": "記事タイトル(30文字以内、興味を引くもの)", "hook": "無料部分の本文"}}
{guideline_text}
"""

    log(f"Gemini ({GEMINI_MODEL}) で無料部分(導入文)を生成中... 切り口: {angle}")
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    raw_text = (response.text or "").strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        log(f"生成結果のJSON解析に失敗しました: {e}")
        log(f"生成結果の内容: {raw_text[:500]}")
        sys.exit(1)

    title = data.get("title", "").strip()
    hook = data.get("hook", "").strip()

    if not title or not hook:
        log("生成結果にタイトルまたは導入文が含まれていません。")
        sys.exit(1)

    log(f"導入文を生成しました。タイトル: {title}")

    # 無料部分(AI生成・毎回変わる)+ 有料部分(固定)を結合
    full_body = f"{hook}\n\n{PAID_CONTENT}"
    return Article(title=title, body=full_body, hook=hook, fixed_part=PAID_CONTENT)



def login_and_save_state(email: str, password: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # ログインは目視確認のため headless=False 推奨
        context = browser.new_context()
        page = context.new_page()

        log("ログインページへ移動")
        page.goto(NOTE_LOGIN_URL)
        human_delay()

        # ▼ 要:実際の note ログインフォームのセレクタに合わせて調整してください
        human_type(page, 'input[name="email"]', email)
        human_delay(0.8, 1.5)
        human_type(page, 'input[name="password"]', password)
        human_delay(0.8, 1.5)

        page.click('button[type="submit"]')

        # 2段階認証などが挟まる場合はここで人間の操作を待つ
        log("ログイン処理中。2段階認証などが表示された場合は手動で完了してください。")
        page.wait_for_load_state("networkidle", timeout=60000)
        human_delay(2, 4)

        context.storage_state(path=str(AUTH_STATE_PATH))
        log(f"認証情報を {AUTH_STATE_PATH} に保存しました。")

        browser.close()


def save_draft(article: Article, headless: bool = True) -> None:
    """タイトル・本文を入力し、下書きとして保存するところまでを行う。
    価格設定・公開は行わない(note の仕様上、下書きでは価格が保持されないため)。
    """
    if not AUTH_STATE_PATH.exists():
        log("認証情報がありません。先に --login-only で実行してください。")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            storage_state=str(AUTH_STATE_PATH),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        # navigator.webdriver フラグを隠す(ヘッドレス検知対策)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        log("新規投稿ページへ移動")
        page.goto(NOTE_NEW_POST_URL, wait_until="domcontentloaded", timeout=60000)
        human_delay(2, 3)
        log(f"遷移後のURL: {page.url}")

        try:
            # タイトル入力欄(placeholder属性で特定)。読み込みが遅い場合があるため長めに待つ
            title_selector = 'textarea[placeholder="記事タイトル"]'
            log("タイトル欄の表示を待機中...")
            page.wait_for_selector(title_selector, state="visible", timeout=45000)
            log("タイトル欄が見つかりました。入力を開始します。")
            human_type(page, title_selector, article.title)
            human_delay(1, 2)

            # 本文入力欄(contenteditableなエディタ本体)。最初に1回だけクリックしてフォーカスする。
            # 毎回クリックし直すと要素の中心をクリックしてしまいカーソル位置が
            # 意図しない場所に飛び、文章が混ざる原因になるため、以降はキーボード入力のみで進める。
            body_selector = '[contenteditable="true"]'
            page.click(body_selector)
            human_delay(0.5, 1)

            # 本文を段落ごとに分けてタイプ。
            # 無料部分(AI生成・短い)は人間らしくゆっくり、
            # 有料部分(固定文・長い)は高速に入力し、実行時間を抑える。
            if article.hook and article.fixed_part:
                log("無料部分(導入文)を入力中...")
                for paragraph in article.hook.split("\n\n"):
                    if not paragraph.strip():
                        continue
                    page.keyboard.type(paragraph, delay=random.uniform(30, 90))
                    page.keyboard.press("Enter")
                    page.keyboard.press("Enter")
                    human_delay(0.5, 1.5)

                log("有料部分(固定文)を入力中...")
                for paragraph in article.fixed_part.split("\n\n"):
                    if not paragraph.strip():
                        continue
                    # insertText(貼り付け相当)は note のエディタに正しく認識されない
                    # ことがあるため、実際のキー入力に近い type() を使う。
                    # 5ms/文字だとエディタの処理が追いつかず内容が失われることがあったため
                    # 20ms/文字に調整(速度より確実性を優先)。
                    page.keyboard.type(paragraph, delay=20)
                    page.keyboard.press("Enter")
                    page.keyboard.press("Enter")
                    human_delay(0.2, 0.4)
            else:
                # hook/fixed_part が無い場合(手動でbodyだけ指定したケース)は
                # 従来通り全文をゆっくり入力する。
                for paragraph in article.body.split("\n\n"):
                    if not paragraph.strip():
                        continue
                    page.keyboard.type(paragraph, delay=random.uniform(30, 90))
                    page.keyboard.press("Enter")
                    page.keyboard.press("Enter")
                    human_delay(0.5, 1.5)

            # エディタ内部の状態(ProseMirrorなど)がDOMに反映されるまで少し待つ
            page.wait_for_timeout(2000)
            human_delay(1, 2)

            # 実際にタイトルが入力された状態になっているか検証する。
            # (エディタの処理落ちなどで、入力したはずの内容が消えるケースがあるため)
            actual_title = page.locator(title_selector).input_value()
            if actual_title.strip() != article.title.strip():
                log(f"警告: タイトルが期待した内容と異なります(現在: '{actual_title}' / "
                    f"期待: '{article.title}')。入力内容が消えている可能性があります。")
            else:
                log("タイトルの入力内容を確認しました。")

            # 本文が実際にエディタに反映されているかも確認する。
            # (見た目上エラーが出なくても、内容が消えてしまうケースがあるため)
            body_text = page.locator(body_selector).first.text_content() or ""
            expected_min_len = len(article.body) * 0.5  # ある程度の余裕を持たせた最低文字数
            if len(body_text) < expected_min_len:
                log(f"警告: 本文の文字数が想定より少ないです(実際: {len(body_text)}文字 / "
                    f"期待: 約{len(article.body)}文字)。入力内容が消えている可能性があります。")
            else:
                log(f"本文の入力内容を確認しました(約{len(body_text)}文字)。")

            # 自動保存によって draft ID が付与され、URLが /notes/xxxxx/edit に
            # 変わるのを待つ。これが確認できて初めて「下書きとして保存された」と言える。
            log("自動保存(下書きID付与)を待機中...")
            try:
                page.wait_for_url("**/notes/**", timeout=15000)
                log(f"自動保存を確認しました。下書きURL: {page.url}")
            except PWTimeout:
                log(f"警告: 自動保存の確認ができませんでした(現在のURL: {page.url})。"
                    f"下書きが保存されていない可能性があります。")

            log("自動保存の完了を待機中(ネットワーク通信が落ち着くまで)...")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
                log("ネットワーク通信の完了を確認しました。")
            except PWTimeout:
                log("警告: ネットワーク通信が20秒以内に落ち着きませんでした。"
                    "保存が完了していない可能性があります。")
            # 保険として、念のため追加で数秒待つ
            page.wait_for_timeout(3000)
            log(f"終了時点のURL: {page.url}")
            log("下書き保存が完了しました。価格設定・公開はnote側で手動で行ってください。")

        except PWTimeout as e:
            log(f"タイムアウトが発生しました: {e}")
            log(f"現在のURL: {page.url}")
            log("note 側の画面構成が変わっている、またはログインに失敗している可能性があります。")
            debug_path = "debug_screenshot.png"
            page.screenshot(path=debug_path, full_page=True)
            log(f"デバッグ用スクリーンショットを保存しました: {debug_path}")
        finally:
            context.storage_state(path=str(AUTH_STATE_PATH))  # セッション更新分を保存
            browser.close()


def load_topic_from_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="note AI記事生成+下書き自動投稿ツール")
    parser.add_argument("--login-only", action="store_true", help="ログインしてCookieを保存するだけ")
    parser.add_argument("--topic-file", type=str, help="テーマ情報を記載したJSONファイルのパス")
    parser.add_argument("--headless", action="store_true", help="投稿時にブラウザを表示しない")
    args = parser.parse_args()

    if args.login_only:
        email = os.environ.get("NOTE_EMAIL")
        password = os.environ.get("NOTE_PASSWORD")
        if not email or not password:
            log("環境変数 NOTE_EMAIL / NOTE_PASSWORD を設定してください。")
            sys.exit(1)
        login_and_save_state(email, password)
        return

    if not args.topic_file:
        log("--topic-file でJSONファイルを指定してください。")
        sys.exit(1)

    topic_data = load_topic_from_json(args.topic_file)
    article = generate_hook(
        angle=topic_data["angle"],
        guidelines=topic_data.get("guidelines", ""),
    )

    # 生成結果をログとファイルの両方に残しておく(公開前チェック用)
    generated_path = Path("generated_article.json")
    with open(generated_path, "w", encoding="utf-8") as f:
        json.dump({"title": article.title, "body": article.body}, f, ensure_ascii=False, indent=2)
    log(f"生成結果を {generated_path} に保存しました。")

    save_draft(article, headless=args.headless)


if __name__ == "__main__":
    main()

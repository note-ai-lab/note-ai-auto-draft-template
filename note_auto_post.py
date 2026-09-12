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
- Gemini APIが一時的に混雑している場合(503エラー等)は自動で再試行するが、
  それ以外の失敗(セレクタ不一致など)は自動リトライせず、ログを残して
  人間が確認する設計(暴走防止)

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

■ 全体の仕組み

この仕組みは3つのサービスを組み合わせています。

1つ目はGoogle Gemini(Googleが提供するAI)です。「どんな読者に向けて書くか」を伝えるだけで、記事の導入文を自動で書いてくれます。決まった範囲内の利用であれば、費用は一切かかりません。

2つ目はGitHub Actions(GitHubが提供する自動実行の仕組み)です。指定した時間に、あるいはボタン一つで、インターネット上のコンピューターが自動でブラウザを操作し、noteへの投稿作業をこなしてくれます。自分のパソコンをつけっぱなしにする必要はなく、スマホからボタンを押すだけで動かせます。

3つ目はnoteです。実際に記事を公開する場所です。

これらを繋ぐことで、「どんな読者に向けて書くかを1つ決めるだけで、AIが導入文を書き、noteの下書きに自動保存される」という流れができあがります。価格の設定と、実際に「公開」するボタンを押す作業だけは、note側の仕組み上どうしても手作業になりますが、それ以外はすべて自動化できます。

■ 必要なもの

・GitHubの無料アカウント(お持ちでなければ、この後の手順内で作成方法も触れます)
・note.comのアカウント(有料記事を書く予定のもの)
・Google AI StudioのAPIキー(AIを呼び出すための「合言葉」のようなものです。決まった範囲内の利用であれば無料で、クレジットカードの登録も不要です)
・パソコン(最初のログイン作業の時だけ使います。それ以降は、スマホだけで運用できます)

■ セットアップ手順(全体の流れ)

まずは、この後の詳しい手順を読む前に、全体の流れをつかんでおいてください。細かいやり方は、それぞれこの後のセクションで説明します。

1. 下記の「テンプレートリポジトリ」を、自分のGitHubアカウントに複製する
2. GitHubの「Secrets」という保管庫に、noteのログイン情報とGoogle Gemini APIのキーを登録する
3. noteに一度ログインして、専用ツールで認証情報(ログイン状態を記録したデータ)を作成し、Secretsに登録する
4. 「どんな読者に向けて書くか」を1行だけ書いたファイルを用意する(やり方は後述の「カスタマイズすべき箇所」で説明します)
5. GitHub Actionsの「Run workflow」ボタンを押す
6. 2〜5分程度で、noteの下書きに記事が自動で保存されている

一度この流れを作ってしまえば、2回目以降は手順4と5(ファイルを書き換えてボタンを押すだけ)の繰り返しで記事が作れるようになります。

■ テンプレートリポジトリ

「リポジトリ」とは、GitHub上でファイル一式をまとめて保存しておく「フォルダ」のようなものです。以下のリンクから、今回必要なファイル一式(コード・設定ファイル・認証情報を作るための専用ツール)が入ったリポジトリにアクセスできます。

https://github.com/note-ai-lab/note-ai-auto-draft-template

このページを開いたら、「Use this template」というボタンをクリックしてください。これを押すと、このリポジトリの中身がまるごとコピーされ、あなた自身のGitHubアカウントの中に、あなた専用の新しいリポジトリが作られます。以降の作業は、すべてこの「自分のリポジトリ」の中で行います。

■ 含まれるファイル

・note_auto_post.py … 記事生成〜下書き保存までを行うメインのプログラム(基本的に中身を編集する必要はありません)
・requirements.txt … プログラムを動かすために必要な部品の一覧(編集不要)
・topic_example.json … 「どんな読者に向けて書くか」を指定するファイル(ここだけ、記事を作るたびに書き換えます)
・post.yml … GitHub Actionsに「いつ・何を実行するか」を伝える設定ファイル
・cookie_converter.html … noteのログイン情報を、パソコンにプログラミング用のソフトを一切インストールせずに変換できる専用ツール

■ Secretsへの登録項目

「Secrets」とは、GitHubが用意している「鍵のかかる保管庫」のようなものです。ここに登録した値は、GitHub Actionsが動く時にだけこっそり読み込まれ、リポジトリの画面をいくら見ても中身は表示されません(自分自身であっても、一度保存した値を後から見返すことはできない仕組みになっています)。パスワードやAPIキーなど、他人に見られたくない情報は、すべてこの場所に登録します。

【登録の場所】
複製したリポジトリのページを開き、上部に並んでいるタブの中から「Settings」を選びます。次に、左側のメニューから「Secrets and variables」→「Actions」と進んでください。「New repository secret」という緑色のボタンから、1つずつ登録していきます。

【登録する4つの項目】

・NOTE_EMAIL … noteのログイン用メールアドレス
　(Nameの欄に「NOTE_EMAIL」とそのまま入力し、Secretの欄に自分のnoteログイン用メールアドレスを入力します)

・NOTE_PASSWORD … noteのログインパスワード
　(Nameの欄に「NOTE_PASSWORD」、Secretの欄に自分のnoteのパスワードを入力します)

・NOTE_AUTH_STATE_B64 … noteのログイン状態を保存したデータ
　(これだけは単純な文字列ではなく、後述の「認証情報の作り方」の手順で作成する、少し長い文字列を貼り付けます。まだ作っていなくて大丈夫です、この後説明します)

・GEMINI_API_KEY … Google Gemini APIのキー
　(aistudio.google.com にアクセスし、Googleアカウントでログインした後、「Get API key」→「Create API key」という順にクリックすると発行できます。表示された「AIzaで始まる文字列」をそのままコピーして貼り付けます)

4つとも登録し終えると、Secretsの一覧に4つの名前が並んでいる状態になります。この4つが揃っていないと、この後の実行時にエラーになってしまうので、進める前に一度、名前のスペルミスがないかも含めて見直してみてください。

■ 認証情報の作り方(Cookie)

「Cookie」とは、noteに「ログイン済みです」ということを覚えておいてもらうための、小さなデータのことです。これをGitHub側に渡しておくことで、毎回パスワードを送信し直さなくても、ログイン済みの状態から作業を始められるようになります。

パソコンにプログラミング用のソフトを入れる必要は一切ありません。以下の手順だけで完結します。

1. パソコンのChromeブラウザに、拡張機能「Cookie-Editor」を追加する(Chromeウェブストアで無料で手に入ります)
2. 普段通りの方法で、Chromeでnote.comにログインする
3. 追加した「Cookie-Editor」のアイコンをクリックし、表示された画面の中にある「Export」というボタン(JSON形式)をクリックする。これでログイン情報がコピーされます
4. 複製したリポジトリの中にある cookie_converter.html というファイルを開き(パソコンにダウンロードしてダブルクリックすれば、ブラウザで開けます)、先ほどコピーした内容を貼り付ける
5. 変換された文字列が表示されるので、それをコピーし、GitHubのSecretsの NOTE_AUTH_STATE_B64 の欄に貼り付ける

このCookieには有効期限があります。数週間〜数ヶ月に一度、ログインができなくなるタイミングが来るので、その際は同じ手順で作り直してください。

■ 自動実行のスケジュールについて

このテンプレートには、あらかじめ「毎日決まった時刻に自動実行する」という設定が入っています。これは post.yml というファイルの中の、schedule という部分に書かれています。特に何もしなければ、複製した直後からこの設定通りに動き始めるので、意図せず動いてしまわないよう、内容を知っておいてください。

【時刻の変更・停止方法】

post.yml を開くと、以下のような行があります。

    schedule:
      - cron: '17 3 * * *'

この時刻は「協定世界時(UTC)」という、日本より9時間遅れた時刻の基準で書かれています。日本時間に直すには9時間を足すので、上の例(3時17分)は日本時間の12時17分になります。

時刻を変更したい場合は、「実行してほしい日本時間」から9時間を引いた時刻を、この場所に書き込んでください。例えば日本時間の朝9時に実行したいなら、9時から9時間を引いた「0時0分」になるので、以下のように書き換えます。

    schedule:
      - cron: '0 0 * * *'

自動実行そのものを完全に止めたい場合は、schedule から始まる上記の2行を削除するか、行の最初に # を付けてください(#を付けると、その行は「無視する」という意味になります)。

あるいは、GitHubの「Actions」タブを開き、対象のワークフロー(実行の設定)を選んで、右上の「•••」というメニューから「Disable workflow」を選ぶ方法もあります。こちらは、自動実行だけでなく手動でのボタン実行も含めて、完全に止める方法です。再開したくなったら、同じ場所から「Enable workflow」を選べば元に戻ります。

■ カスタマイズすべき箇所(記事を作るたびに触るのはここだけ)

2回目以降、記事を作りたい時に編集する必要があるのは、基本的に次の1つのファイルだけです。

【topic_example.json というファイル】

複製したリポジトリを開き、一覧の中から topic_example.json というファイルをクリックします。右上の鉛筆マーク(編集ボタン)を押すと、中身を書き換えられます。中身は以下のような形になっています。

    {
      "angle": "副業に興味があるが何から始めればいいか分からない会社員向け",
      "guidelines": "誠実で落ち着いたトーン。煽り文句は避け、具体的なメリットで惹きつける。"
    }

・"angle" の部分 … 「今回はどんな読者に向けて書くか」を、日本語でそのまま書きます。例えば「文章を書くのが苦手な人向け」「定年後の収入源を探しているシニア層向け」のように、書き換えるたびに違う内容の導入文が生成されます。

・"guidelines" の部分 … 文章のトーンや雰囲気の指示です。「初心者にも分かりやすく」「熱意を感じる文体で」のように、自由に指定できます。

書き換えたら、画面下の方にある「Commit changes...」というボタンを押して保存すれば完了です。

【その他、必要であれば触ってもよい箇所(通常は触らなくて大丈夫です)】

・note_auto_post.py の中にある GEMINI_MODEL という部分 … 使用するAIの種類を指定しています。Google側の都合でモデルが使えなくなることがまれにあり、その場合はここを新しいモデル名に書き換える必要があります(トラブルシューティングの章で詳しく説明します)。

・post.yml の中の cron の部分 … 自動実行の時刻です(上の「自動実行のスケジュールについて」を参照してください)。

■ うまくいかない時に(困った時の対処法)

自動化の仕組みは、note側やGoogle側の細かい仕様変更の影響を受けることがあります。「あれ、動かない」と思っても、慌てず以下を順番に確認してみてください。

〈まず最初に見る場所〉

GitHubの「Actions」タブ→実行した記録(一番上のもの)→「post」という項目、の中に、実行中の詳しい記録(ログ)が残っています。エラーが起きている場合、ここに赤い印と、エラーの内容が表示されます。何が起きているか分からなくても、まずはこの画面をスクリーンショットで保存しておくと、後で調べたり人に相談したりする際に役立ちます。

〈よくある症状と原因〉

・記事のタイトルを入力する場所が見つからない、というエラーが出る
　→ note側のページのデザインが変更された可能性があります。これは購入者側で直すのが難しい種類の問題なので、その場合はテンプレートの更新をお待ちいただくか、ご連絡ください。

・使っているAIの種類(モデル)が「もう使えません」というエラーが出る
　→ Google側がAIの種類を新しくした際に起こります。エラーメッセージの中に「代わりにこちらを使ってください」という新しい名前が書かれていることが多いので、note_auto_post.py の中の GEMINI_MODEL という部分を、その新しい名前に書き換えてみてください。

・「今、利用が集中しています」といった内容のエラーが出る
　→ Google側のサーバーが一時的に混雑しているだけです。これはプログラム側で自動的に少し待って再挑戦する仕組みをすでに入れてあるので、多くの場合は何もしなくても解決します。何度も続くようなら、少し時間を置いてから「Run workflow」をもう一度押してみてください。

・記事を公開する際に設定した金額が、なぜか元の金額(300円など)に戻ってしまう
　→ これは不具合ではなく、noteの仕組み上そうなるようにできています。金額の設定は、実際に「投稿する」ボタンを押す、その瞬間にしか保存されません。下書きの状態でいくら金額を設定しても、一旦ページを閉じると元に戻ってしまうので、金額の設定は必ず、本当に公開する直前に手動で行うようにしてください。

〈それでも分からない場合〉

エラーの画面をスクリーンショットで保存し、内容を保存しておいてください。テンプレート側の更新や修正で対応できることも多いので、慌てて色々な場所を書き換えようとせず、まずは記録を残しておくことをおすすめします。

■ 運用のコツ

・実行する回数は1日1〜数回程度に抑え、短い時間の間に何度も連続して実行しないようにしてください
・AIが生成した文章は、必ず公開前に人の目で読んで確認してください。AIは時々、事実と異なる内容を書いてしまうことがあります
・「本文のどこからを有料エリアにするか」という細かい範囲の指定は、現状は自動化されていません。金額の設定とあわせて、公開前に手動で設定してください
・複製したGitHubのリポジトリは、必ず「Private(非公開)」の設定のまま使ってください。ログイン情報などが第三者に見られてしまう事故を防ぐためです

以上が、実際に私が構築した仕組みのすべてです。ここまでの手順を1つずつ、慌てず進めていただければ、同じ環境を再現できます。"""


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

    # Gemini側が混雑している場合(503 UNAVAILABLEなど)、少し待って再試行する。
    max_retries = 4
    response = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            break
        except Exception as e:
            error_text = str(e)
            is_transient = "503" in error_text or "UNAVAILABLE" in error_text or "overloaded" in error_text.lower()
            if is_transient and attempt < max_retries:
                wait_seconds = 15 * attempt  # 15秒, 30秒, 45秒... と待ち時間を伸ばす
                log(f"Geminiが混雑しているようです(試行{attempt}/{max_retries})。"
                    f"{wait_seconds}秒待って再試行します。")
                time.sleep(wait_seconds)
            else:
                log(f"Gemini APIの呼び出しに失敗しました: {error_text}")
                sys.exit(1)

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

    # 無料部分(AI生成)の末尾に、固定の説明文を追加する。
    # この記事が note・GitHub・Gemini を組み合わせた半自動化の解説であることを
    # 読者に明示するための一文(毎回同じ内容)。
    hook_note = (
        "この記事の有料部分では、note・GitHub・Google Gemini(AI)の3つを連携させ、"
        "記事作成からnoteへの下書き保存までを半自動化する仕組みの作り方を、"
        "実際のコード付きで解説しています。\n\n"
        "有料エリアでは、実際に私が使っているコード一式と、ゼロから設定する手順をすべて公開します。"
        "パソコンの操作に多少慣れていれば、プログラミングの経験がなくても、"
        "順番通りに進めるだけで同じ仕組みが作れるように書いています。"
    )
    hook_with_note = f"{hook}\n\n{hook_note}"

    # 無料部分(AI生成・毎回変わる)+ 有料部分(固定)を結合
    full_body = f"{hook_with_note}\n\n{PAID_CONTENT}"
    return Article(title=title, body=full_body, hook=hook_with_note, fixed_part=PAID_CONTENT)


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

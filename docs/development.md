# 開発・検証の記録（旧 README）

これは公開用 README から切り出した**開発者向けの記録**です（実装の内訳、テストの意味、
実機で測った値、QA の手順）。利用者向けの説明は [README.md](../README.md) を見てください。

> このリポジトリは **Hermes Agent のカンバン機能を試す目的**で作られました。
> 作者はソースを一切確認していません（詳細は README の「このリポジトリについて」）。
> 記録中のユーザー名・ホスト名・ディストリ名・絶対パスは `<user>` / `<distro>` / `<repo>` /
> `~` に置換してあります（測定値は当時のまま）。

---

Windows 11 常駐ツール（お試し・低コスト版）。キーボードの Enter にポリシーを適用し、
意図した連打／ホットキーだけを検出してテキストを送り先（クリップボード／ブラウザ AI
チャット／Hermes エージェント）へルーティングします。**通常権限のみで動作**します
（管理者権限は要求しません。elevated ウィンドウへのキー・注入は OS 仕様で届かない
＝受容済み。research.md §2-a-2）。

- 言語/構成: **Python 3.14 + pywin32（追加パッケージなし）**。ビルド工程はありません
  （「ビルド」＝構文チェック + import チェック）。
- 実行環境: Windows 11（`python.exe` = Python 3.14.0、pywin32 導入済み）。
- 開発/検証環境: WSL2 <distro>（python3 3.14.9, git, uv。**pytest は未導入**のため正式な
  テスト手順は `unittest discover`）。
- 設計と調査の一次資料: [`docs/design.md`](docs/design.md) / [`docs/research.md`](docs/research.md)。

## リポジトリ構成

```
src/enter_router/
  core/       純ロジック層（設定・Enterポリシー状態機械・ルーティング・ISink 契約）
              — 標準ライブラリのみ。Linux (WSL) の pytest でテストできる
  platform/   Windows アダプタ（LL フック / IME / SendInput / クリップボード / 通知 / hermes CLI）
  app/        合成ルート（main エントリ・AppController・監視ドッグ・設定ホットリロード）
  ui/         トレイ + 設定画面（tkinter、フロントエンドカードの担当。platform/ は import しない）
  main.py     エントリポイント（python / pythonw 両対応）
tests/        ユニットテスト（core + UI モデル/トレイ/IME。Linux 上で実行）
config/sample.config.json   サンプル設定（そのまま動く既定値）
config/notepad-qa.config.json  メモ帳だけで試すための QA 設定（1 プロファイル・README「メモ帳で試す」）
scripts/win_check.ps1       Windows 側チェック（WSL から interop で実行）
scripts/win_ui_check.ps1    Windows 側 UI チェック（実 Tk + 実トレイ + 実フック、23 項目）
docs/           design.md（契約） / research.md（調査）
```

## WSL から「ビルド」＝検証する

```bash
cd ~/enter-router

# (1) 純ロジック層のユニットテスト（Linux 実行がこの設計の検証手段）
#     ※ この WSL には pytest が未導入（追加インストールは方針上やらない）ため、
#        正式手順は unittest。テストは pytest でも同様に走る（両対応）:
python3 -m unittest discover -s tests -v
#     pytest を導入済みの環境ではこちらでも可:
python3 -m pytest tests/ -q

# (2) Windows 側の「ビルド」＝構文/import/API レイアウト検証（interop 経由）
/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass \
  '<repo>\scripts\win_check.ps1'
#   確認される内容: 全 .py の py_compile / pywin32 到達性 / core の import /
#   **全アダプタの import と SendInput INPUT が実機で 40 バイトであること** /
#   `--once` が Windows 上で完走すること / **実機セルフテスト（下記）**。
#   末尾に WIN_CHECK_OK が出れば OK。

# (3) 実機セルフテスト単体（フック・クリップボード・スレッド間注入の実測）
python.exe <repo>\scripts\win_selftest.py
#   キーを一切飲み込まない安全なテスト。クリップボード往復（put/get）/ 64bit
#   lparam での CallNextHookEx / 実フックを 0.x 秒張って F24 を通過させる /
#   「ロック保持のまま SendInput（旧実装＝617ms ストール）」対
#   「ロック解放後」「フックスレッドへ委譲」の所要時間比較。末尾 WIN_SELFTEST OK。

# (4) 常駐プロセスの実 E2E プローブ（本物のフック・タイマー・SendInput を駆動）
python.exe <repo>\scripts\app_probe.py
#   保留 Enter が「タイマースレッド → 受け渡しキュー → hook.wake() → フックスレッド
#   → SendInput」で 1 回だけ届くこと、パニックキーで保留分を先に送出してから
#   アンフックすること（I7）をログで確認。末尾 APP_PROBE OK。
```

テスト構成（Linux で実行できる範囲）:

| ファイル | 何を保証するか |
|---|---|
| `tests/test_smoke.py` | 設定スキーマ・検証・2 層解決（design §4.2〜4.7、51 件） |
| `tests/test_policy.py` | 状態機械の遷移と **I1〜I8**（保留/連打/修飾キー/IME/injected/順序/パニック） |
| `tests/test_sinks_fallback.py` | 3 シンクの挙動、失敗分類、**クリップボード退避フォールバック**、capture 失敗時に送らないこと |
| `tests/test_platform_pure.py` | SendInput の INPUT 40 バイト・コンボ→イベント列・**物理修飾キーの中和**・Hermes の argv/env・`--dry-run` ログ書式 |
| `tests/test_app_safety.py` | 合成ルート: パニック→順序付きフラッシュ→アンフック、コールバック例外の封じ込め、**注入がロック外/フックスレッド経由であること**、キュー FIFO、BUSY 復帰、watchdog の有界ロック |
| `tests/test_ui_model.py` | 設定画面のモデル: 全 18 トリガの一覧と未設定表示、保存 dict がスキーマ通り（UI 専用フィールド無し）、不正項目の日本語ラベル、プレビュー（2 層解決・未定義アプリ=素通し）、テスト送信の状態機械（二重実行ガード / 不明は成功にしない） |
| `tests/test_ui_tray.py` | トレイの状態表示（色・文言・ツールチップ）、メニュー順序（パニックが先頭・パニック中は淡色だが消えない）、Linux では Win32 に触れず soft-fail すること |
| `tests/test_ui_ime.py` | **IME Enter 安全**: UI ソースに `<Return>` 束縛が 1 つも無いこと、`ime_guard` が実ウィンドウ/偽ツリーで Enter 束縛を検出すること |

## Windows で起動する

リポジトリは WSL 側にあるので、Windows からは UNC パスで直接実行できます。

```bat
:: 1) まず設定と解決表を確認（フックを張らない・何も注入しない）
python.exe <repo>\src\enter_router\main.py --config \\wsl.localhost\...\config\sample.config.json --once

:: 2) 判定だけをリプレイして観測（フックを張らない。QA の安全確認はこれ）
python.exe <repo>\src\enter_router\main.py --config \\wsl.localhost\...\config\sample.config.json --dry-run

:: 3) 常駐起動（コンソールなし。ここで初めてキーフックが入る）
pythonw.exe <repo>\src\enter_router\main.py
```

- `--dry-run` は **フックを張らず**、設定から生成した現実的なキー列（Enter×1/2/3・
  Ctrl/Shift/Alt+Enter・F5・`あ`→Enter→`い`・injected のエコー・保留中パニック）を
  状態機械に通し、1 判定 1 行で出します。**SendInput・シンク・capture は一切実行しません。**
  出力書式は design §3.9 に固定（`DRYRUN t=… op=… meaning=… profile=… resolved=… decision=… swallow=… reason=…`
  と末尾の `DRYRUN-SUMMARY handled=… swallowed=… injected_ignored=… unresolved=…`）。
- `--once` は設定の検証結果・警告・第1層マップ・**プロファイル別の
  `trigger → meaning → resolved`（送出キー列）** を表で出して終了します（exit 0 / 設定不正は 2）。
- 設定ファイル: `%APPDATA%\enter-router\config.json`（初回起動時に既定値から生成。
  手元で試すなら `--config \\wsl.localhost\...\config\sample.config.json` でも可）。
- ログ: `%APPDATA%\enter-router\logs\enter-router.log`（1MB x 3 でローテーション。
  **打鍵文字列・取得テキストはログに書かない**方針）。
- 万が一キーが止められない場合の保険: **F1 を 3 回連打（パニック）で即アンフック**（`policy.panic_key` / `panic_repeat` で変更可）。
  コードを覚える必要がなく、片手で押せます（F12 は機種によって無い/位置が違うため既定から外しました）。
  保留中の Enter は**順序を保って 1 回だけ送出してから**アンフックし、以後は二度と再送しません
  （design §6-4 / I7）。
- CLI フラグ（固定）: `--config PATH` / `--dry-run` / `--once` / `--settings` / `--version`。
  `--settings` は**常駐インスタンスの設定画面を開く**コマンドです（別プロセスで 2 つ目の UI を作らない）。
  常駐が無い場合は案内を表示して終了します。

## メモ帳で試す（最短の実機確認）

`sample.config.json` には**メモ帳のプロファイルがありません**。プロファイルの無いアプリは仕様どおり
全操作 `pass`（素通し）なので、サンプルのままメモ帳で試すと **「Enter がそのまま改行になるだけで何も起きない」**
ように見えます（不具合ではなく I8 の安全側の動作）。メモ帳だけで確認するための最小構成を同梱しています:
**`config/notepad-qa.config.json`**（**`Notepad.exe`** を対象にした 1 プロファイルのみ。
プロセス名の照合は**大文字小文字を区別しません** — 実装は両辺を小文字化して glob 比較するため、
`Notepad.exe` / `notepad.exe` / `NOTEPAD.EXE` のどれでも一致します）。

```bat
:: 1) 先に解決表を確認する（フックを張らない・何も注入しない。warnings: 0 と exit 0 が正常）
python.exe <repo>\src\enter_router\main.py --config <repo>\config\notepad-qa.config.json --once

:: 2) メモ帳を開いた状態で常駐起動（コンソールなし。ここで初めてキーフックが入る）
pythonw.exe <repo>\src\enter_router\main.py --config <repo>\config\notepad-qa.config.json
```

手順 1 の出力に次の 1 行が出ていることをまず確認してください（メモ帳が「定義済みアプリ」になった証拠）:

```
PREVIEW profile=notepad      trigger=enter2       meaning=send      resolved=key:ctrl+enter
```

メモ帳にフォーカスした状態での期待挙動（この設定は `send`→`ctrl+enter` / `newline`→`enter`、
保留窓は **250ms** — 一般論ではなく**この環境の実測値**です。`docs/measurements.md`）:

> ✅ **この環境（Windows 11 + MS-IME）で、他プロセスの未確定（変換中）を実際に判別できるようになりました**
>
> IMM（`ImmGetContext` + `GCS_COMPSTR`）は他プロセスでは原理的に読めません（実測: `AttachThreadInput`
> は成功するが `ImmGetContext` が 3 段すべて 0）。そこで **TSF の窓**を第 4 段として追加しました:
> 変換中だけアプリのプロセスに `MSCTFIME Composition` 窓が現れるので、**あれば `composing`**、
> **無くて `MSCTFIME UI`（アンカー）があれば `not_composing`**、どちらも無ければ **`unknown`** です
> （＝アンカーが無いアプリでは「不在」を「確定」と読み替えません。IME の確定 Enter を壊さないため）。
>
> 実測（`scripts/win_ime_check.py`）: 変換中 `composing` / 確定後 `not_composing` の**両方**を別プロセス
> から観測、Enter で確定して改行は入らず、次の Enter で改行（プローブ 1 回 0.10ms）。
> 詳細は `docs/design.md` §4.3 / §10-1。
>
> `unknown`（MSCTFIME をホストしないアプリ＝TSF ネイティブ実装・他社 IME など）のときは
> `ime_state=unknown action=<pass|process> source=… detail=<API と Win32 エラー>` がログに出て、
> **初回だけ通知**（トレイ）が出ます（連続時はログを 1 分に 1 回へ間引き）。
> ※ Windows 11 のメモ帳（WinUI3）が MSCTFIME をホストするかは**未検証**です（このカードの実測は
> Win32 EDIT の別プロセスで実施）。ログの `ime_state=` を見ればそのアプリで判別できているか分かります。
> 既定は `"pass"`（変換の邪魔をしない）。`"global": { "ime_unknown_policy": "process" }` にすると
> 不明時も通常の Enter 判定を走らせます。

| 操作（250ms 以内の連打） | 意味（第1層） | メモ帳で起きること |
|---|---|---|
| Enter ×1 | `newline` | **250ms 待ってから**改行が 1 つ入る（素の Enter はアプリに届かない） |
| Enter ×2 | `send` | 改行は入らず、**Ctrl+Enter を 1 回**注入（メモ帳は Ctrl+Enter に既定動作が無いので**画面は変化しません**。成否はログで確認） |
| Enter ×3 | `hermes` | もう 250ms 待って（4 連打の取り消しができるように）から Hermes シンクが走る（応答は通知に出る。要 `wsl.exe` + Hermes CLI） |
| Enter ×4 以上 | **キャンセル** | **何も出しません**（改行・送信・hermes のどれも）。入力欄のテキストは**そのまま残ります**（読まない・消さない）。**通知も出しません**（ログに `decision=CANCEL`） |
| Shift+Enter | `newline` | 改行が 1 つ入る（抑止してから注入） |
| Ctrl+Enter | `send` | Ctrl+Enter を 1 回注入 |
| メモ帳以外のウィンドウ | — | プロファイル未定義なので**従来どおり素通し**（誤爆しません） |

**確認するログ**: `%APPDATA%\enter-router\logs\enter-router.log`（1MB×3 でローテーション）。
決定 1 件につき 1 行で、本文は書きません。Enter×2 が送信として解決されたことは次の行で分かります:

```
decision=DELIVER op=enter2 meaning=send state=IDLE swallow=0 resolved=key:ctrl+enter count=1 reason=deadline
```

Enter ×1 だけを押したときは `op=enter meaning=newline … resolved=key:enter`、Enter×3 は
`decision=HOLD op=enter3 … reason=hold_extend_third` → 250ms 後に
`decision=DELIVER op=enter3 meaning=hermes … resolved=sink:hermes`、Enter×4 以上は
`decision=CANCEL … reason=burst_cancel_3` になります。
止めたいときは **F1 を 3 回連打**（パニック）か、トレイメニューの「一時停止」/「終了」。

## 緊急停止（F1 連打）と、アプリの登録キー（v2-g）

**止める**: `F1` を **3 回連打**（250ms 以内）。保留中の Enter は順序どおり 1 回だけ送ってから
アンフックします。トレイの「パニック停止」1 クリックでも同じです。
単発の F1 は普通の F1 として通ります（`f1` の意味は `none`）。回数・キーは設定で変更できます:

```json
"policy":    { "panic_key": "f1", "panic_repeat": 3 },
```

**登録**（設定を手で書かなくてもアプリを足せる）: 対象のウィンドウを前面にして

| キー（既定） | 登録される内容 | `match` |
|---|---|---|
| `Alt+F5` | **プロセスのみ**（その exe のどのウィンドウでも有効） | `{"process": "notepad.exe"}` |
| `Alt+F6` | **プロセス＋ウィンドウタイトル**（そのウィンドウだけ） | `{"process": "notepad.exe", "title": "<タイトル（リテラル）>"}` |

- 押した瞬間の設定ファイル（`--config` のファイル）に**プロファイルが 1 件追記**され、その場で有効になります
  （検証 → 原子的書き込み → 即時反映。保存に失敗したらファイルは変わりません）。
- keymap は `send=enter` / `newline=shift+enter`（チャットの慣習）。あとから設定画面タブ B で編集できます。
- タイトルは**パターンではなく文字列**として保存します（`[` `*` `?` はエスケープされ、そのウィンドウだけに一致）。
- 同じ条件を二度登録しても増えません（「既に登録済み」と通知）。
- 使用するキーは設定で変更できます（**`alt` + `F1`〜`F24` のみ**。無修飾の F キーは Enter ポリシーの持ち物）:

```json
"registration": { "process_key": "alt+f5", "process_title_key": "alt+f6" },
```

設定画面タブ A「緊急停止と登録キー」からも同じ 3 項目（パニックキー・連打回数・登録キー 2 つ）を編集できます。

## UI（トレイ常駐 + 設定画面・design §7）

実装は `src/enter_router/ui/`（トレイ = **ctypes の `Shell_NotifyIconW`**、設定画面 = **標準 tkinter**。
追加パッケージなし）。UI は `app/controller.py` の `AppController` だけを叩き、`platform/` を import しません。

| 画面 | 内容 |
|---|---|
| **トレイ（S0）** | 状態色アイコン（緑=有効 / 黄=一時停止 / 赤=パニック停止中 / 青=シンク実行中）、ツールチップに状態・設定パス・警告件数・直近エラー。メニュー = **パニック停止（先頭）** / 設定を開く / 有効⇔一時停止 / ログフォルダ / 終了。左クリックでも設定画面 |
| **設定・タブA「操作マップ」** | **全 18 トリガを隠さず一覧表示**（折りたたみなし）+ 意味プルダウン、`hold_mode` / `hold_window_ms` / `triple_enter_window_ms` / `trigger_cooldown_ms`、コアの警告をそのまま表示、動作プレビュー（例 `Enter（1 回） → 改行（このアプリでは shift+enter を送る）`） |
| **設定・タブB「プロファイル」** | 一覧 + 追加 / 複製（複製は無効で作成）/ 削除、対象（process / title / url）、`keymap` の send・newline コンボ、有効 / 無効、3 シンクのパラメータ。**未定義アプリは「素通し」**と明記 |
| **設定・タブC「テスト送信」** | シンク選択 + サンプル文字列 + **明示クリック**で実行。成否と理由を画面に残す（送信中はボタン無効＝二重送信防止、結果が取れない場合は「結果不明」と表示し成功扱いにしない） |

- **保存**は `AppController.save_config_dict()` 経由で **core が検証してから JSON を書く**。失敗時はファイルを
  変更せず、**どの項目が不正か**を日本語ラベル + core の理由で表示（例:
  `保留窓（hold_window_ms）: policy.hold_window_ms must be 120..600`）。成功時は**再起動不要**
  （`_apply_config` で即時反映。mtime 監視のホットリロードも併用）。
- **キャンセル**で保存済みの状態に戻る。未保存のまま閉じる場合は確認ダイアログ（既定は「破棄しない」）。
- **多言語入力（IME）配慮**: 設定画面のどこにも `<Return>` 束縛を作りません
  （tkinter に「既定ボタン」の概念は無く、Enter は何も起動しない）。保存・送信は明示クリックのみ。
  `ui/ime_guard.py` が**構築時に実ウィンドウを走査して検査**し（違反があれば例外＝起動しない）、
  `tests/test_ui_ime.py` がソース走査 + 偽ウィジェットで機械的に検査します。キー割当ては
  「キーを押して取得」方式ではなくプリセット付きコンボ（入力可）なので、Enter の入力欄との衝突もありません。
- **Windows 11 の通知領域**: 新しいアプリのアイコンは既定で**オーバーフロー（隠れたアイコン）**に入ります
  （実測: 画面に自アイコンの画素が出ない）。常時表示にするには
  `設定 > 個人用設定 > タスクバー > その他のシステム トレイ アイコン` で enter-router をオンに。
  隠れていても到達できるよう、常駐中に
  `python.exe \\wsl.localhost\...\src\enter_router\main.py --settings` を実行すると設定画面が開きます
  （`FindWindowW` + `WM_APP` メッセージ）。**パニック停止**はトレイメニュー先頭と設定画面ヘッダの
  ボタンの 2 か所にあり、どちらも 1 クリックです。

### 設定ファイルの書き込み規則（削除したプロファイルを復活させない）

- **初回生成**（ファイルが無いとき）は `first_run_config_dict()` を使う。`profiles` は**空**。
  サンプル（site-a / site-b / excel）は `config/sample.config.json` と design §4.2 にだけ置く。
- **書き込みは常に「ディスクの現在の内容」を起点にする**。登録キー（Alt+F2/F3）は
  `_config_dict_from_disk()` で読み直してから追記し、読めないときだけメモリの写しに落ちる。
  メモリの古い写しを丸ごと書き戻すと、設定画面で消したプロファイルが次の書き込みで復活する
  （実機で起きた。`tests/test_registration.py::test_registration_does_not_resurrect_a_deleted_profile`）。
- `profiles` が無い／空の設定は**合法**（全アプリ素通し）。欠落時にサンプルへフォールバックしない。

### 設定画面のレイアウト（クリップされない要件）

設定画面は**高さを固定しない**。内容の要求高さ（タブ A だけで 759px）が現在 951px あり、
`geometry("960x760")` のような固定値だと Tk の pack が**最後に pack したウィジェットから
潰す**ため、footer の **保存／キャンセル（変更を破棄）／設定を読み直す** が 1px になって
消えます（実測: `save button mapped=0`, footer=1px）。この現象は 2026-10-28 に実際に起きた。

守るべき 2 点:

- **下段（footer と警告欄）を `side="bottom"` で先に pack し、notebook を最後に pack**する。
  潰れてよいのは伸縮できる notebook だけで、ボタンは絶対に潰さない。
- 高さは `_fit_to_content()` が要求高さから算出し、画面の作業領域を上限にする
  （`min(req, screen_h - 140)`）。`minsize` もその値から決める。

確認コマンド（実 Tk で要求高さ・footer・保存ボタンの mapped を測る）:

```powershell
C:\Python314\python.exe -X utf8 <repo>\scripts\win_settings_layout.py
#   正常: window geometry now : 960x951 / footer actual : 960x37 / save button mapped : 1
#   異常: save button mapped : 0 / footer actual : 1x1
```

### UI の実機チェック（Windows）

```powershell
# WSL から: 実 Tk + 実トレイ + 実 LL フックで UI を駆動し、23 項目を検査（スクショ 3 枚 + レポート JSON）
/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -ExecutionPolicy Bypass \
  '<repo>\scripts\win_ui_check.ps1'
#   末尾に UI_CHECK_OK 23/23 が出れば OK。
```

このチェックは**全トリガを `none` にした一時設定**で常駐を起動するため、実行中も実機の打鍵は
保留・抑止・再送されません（フック自体は本物を張ります）。検査内容は、トレイアイコンの登録
（`NIM_ADD` / `HICON` / `Shell_NotifyIconGetRect`）、設定画面の起動（トレイコマンド経由と
`--settings` メッセージ経由）、3 タブ・18 トリガ行・IME ガード・レイアウトのクリップ検査、
プロファイル追加/削除、保存（**JSON 差分 + 再起動なしの反映**）、不正値（**ファイル未変更 + 項目名表示**）、
キャンセル、テスト送信（**二重クリック無視**）、パニック 1 クリック、終了です。

## 設定の要点（詳細は docs/design.md §4）

設定は **2 層マッピング**です（v2 確定・design.md §3.1）。

- **第1層 `global.operations`（全アプリ共通 = あなたの癖）**: 操作 → 意味。
  既定は「**Enter=改行 / 2 連続=送信 / 3 連続=Hermes**」。`shift+enter`→`newline`、
  `ctrl+enter`→`send`、`alt+enter`→`pass`、`f1`〜`f12`→`none`（未使用）。
  意味の値: `send` / `newline` / `hermes` / `aichat` / `clipboard` / `none` / `pass`。
  サンプルは**全トリガを明示列挙**しています（暗黙既定に頼らない）。必須トリガ
  （`enter`/`enter2`/`enter3`/`shift+enter`/`ctrl+enter`）が欠けると起動時に警告が出ます。
- **第2層 `profiles[].keymap`（アプリ/サイトごとの流儀）**: 意味 → 実キー列。
  例: サイトA `send`→`ctrl+enter` / サイトB `send`→`enter` / Excel `newline`→`alt+enter`。
  **プロファイルが無いアプリは全て素通し**（誤爆防止）。プロファイルを作ることが明示的なオプトインで、
  そのアプリではシンク（Hermes 等）も有効になります。
- **保留（hold）と遅延**: 既定 `hold_mode: "deterministic"` では **単発 Enter が最大 250ms
  保留**されます（2 連続・3 連続に意味を持つため判定必須。design.md §3.8）。
  **この 250ms は一般論ではなく実測値**です: `scripts/measure_enter.py` で連続打鍵の間隔
  （15 サンプル 112〜180ms）と、意図的に離した間隔（5 サンプル 495〜585ms）を採り、
  アプリの実ログに残っていた 2 連打（9 サンプル 96〜201ms）と合わせて、最も遅い連続（201ms）
  の 1.25 倍 = 251ms → **250ms** に丸めました（最も速い「非連続」495ms の 0.8 倍 = 396ms を
  十分下回ります）。手順と生データは **`docs/measurements.md`**、計算は `core/calibrate.py`。
  - 遅延ゼロにしたい: `policy.hold_mode = "fast"`（その場で即送出。ただし 3 連続で
    send と hermes が両方発火しうる）、または `global.operations.enter2/enter3` を `pass` に。
  - 保留中の Enter は**必ず順序を保って配信**されます（他のキーが来たら先にフラッシュ。
    パニック（F1 連打）・停止時も無言破棄はしません。ただし**二重送信はしません**）。
- **`LLKHF_INJECTED` のイベントは処理しません**（自分が注入したキーの再帰・無限ループ防止）。
- `f1`〜`f12` の既定 `none` は「トリガ未使用＝ツール無反応」です。**F キーは抑止されず
  通常どおりアプリに届きます**（`none` はキーを飲みません。design.md §4.3）。
- サイト側の逐次絞り込み（入力すると候補が勝手に動く挙動）は**抑止対象外**（サイト JS の領分。
  キーを飲むと日本語入力自体が成立しないため）。緩和は「シンクへは一括ペースト」のみ
  （docs/design.md §4.5）。
- Hermes 送り先は `wsl.exe -d <distro> --exec /bin/bash -lc ...` 経由
  （**`--exec` と `-lc` は必須**。`--` 区切りだと interop 側が末尾をシェル評価して `$1` が消え、
  プロンプトをコマンドラインに載せると `$`／バッククォートが実行される。`-c` は PATH に
  `~/.local/bin` が無く `exec: hermes: not found`＝exit 127）。
  **子プロセス環境から `_HERMES_GATEWAY` 系変数を必ず除去**します（除去しないと
  `invalid_params` で応答が来ない。research R14 実測）。

## 実装アーキテクチャ（実装済み・design §1/§2）

```
core/      純ロジック（stdlib のみ）
           events.py  KeyEvent / ForegroundInfo / ImeState / TriggerInfo
           keys.py    VK 表・コンボ正規化（config.py の正規化と等価であることをテストで固定）
           config.py  スキーマ・既定値・検証（warnings は黙って埋めない）
           routing.py 2 層解決（lookup_meaning / resolve_meaning）＋プロファイル照合＋シンク選択
           policy.py  Enter ポリシー状態機械（状態 IDLE/HOLDING/COOLDOWN/BUSY/PANIC）
           capture.py 取得チェーンの順序ポリシー
           sinks.py   ISink 契約・SinkResult・FailureKind
platform/  Windows アダプタ（win32 呼び出しは関数内 import ＝ Linux でも import 可能）
           hook.py LL フック＋メッセージループ＋マウス割り込み＋前景情報
           ime.py / inject.py / textcap_win.py / notify_win.py / hermes_cli.py
           sinks_win.py ClipboardSink / AiChatSink / HermesSink（OS 依存は注入可能）
app/       合成ルート: main.py（配線・AppController 実装）/ sink_runner.py /
           fgcache.py / watchdog.py / config_watcher.py / logsetup.py / dryrun.py
```

スレッド（1 プロセス・管理者権限なし）: main / hook（LL フック）/ worker（capture＋シンク、
最大 30s）/ timer（保留デッドライン）/ fgcache / watchdog。**フックコールバックが行うのは
「IME 問い合わせ → 状態機械 → 同期 SendInput（`key:`/確定配信）またはシンクのキュー投入 → return」だけ**
で、シンクと capture はワーカーで動きます。状態機械へのアクセスは 1 本のロックで直列化し、
タイマーがキーイベントに割り込まないようにしています。

安全要件の実装位置:

| 要件 | 実装 |
|---|---|
| フック内例外の完全捕捉 | `platform/hook.py` のコールバック最外 + `app/main.py::_decide` の二重防御（失敗時は必ず素通し） |
| コールバック所要時間の上限超過で自動アンフック | `app/watchdog.py::SafetyMonitor.observe` → 5 連続 50ms 超 / 単発 250ms 超 → `panic_stop` |
| 無応答時の自動復旧 | watchdog が heartbeat 3s 途絶 → 保留フラッシュ（50ms 予算）→ 通知 → `os._exit(70)` |
| パニックキーで即停止 | `policy.on_panic`（F1 連打 = `panic_repeat` 回で成立。保留を順序付きフラッシュ）→ `hook.request_stop` → 通知＋ログ |
| 停止時にキーを飲み込んだままにしない | 保留は必ず 1 回配信（I1/I7）。PANIC 後のイベントは全て素通し |
| ログ | `app/logsetup.py`（1MB×3）。決定ログは op/meaning/resolved/decision のみ、**本文は書かない** |

## ブラウザ照合の落とし穴（2026-10-28 実測）

### 1. タイトル完全一致の登録は Web アプリで壊れる

`Alt+F3`（プロセス＋タイトル登録）は**その瞬間のタイトルをリテラル化**して保存します。
Claude のような SPA は会話ごとにタイトルを変えるため（登録時
`新規チャット - Claude — Mozilla Firefox` → 実測時 `Greeting - Claude — Mozilla Firefox`）、
以後どのプロファイルにも一致せず `resolved=pass`（＝素通し＝アプリ既定の Enter 動作）に
なります。ログでは `decision=DELIVER op=enter meaning=newline … resolved=pass` が証拠です。
ブラウザでは `Alt+F2`（プロセスのみ）か `*Claude — Mozilla Firefox` のような
**ワイルドカード**、または下記の URL 照合を使ってください。

### 2. Firefox では URL が読めなかった（修正済み）

旧 `platform/textcap_win.get_document_url` は
`AccessibleObjectFromWindow(hwnd, OBJID_CLIENT)` の**ルート**の `accValue` しか読んでおらず、
Firefox では常に `None` でした（＝URL 照合プロファイルは絶対に一致しない）。実測した構造:

- ルート = APPLICATION ノード、`accValue` は空
- アドレスバー = 子ノード。role `PROGRESSBAR`(0x2E)、value `claude.ai/chat/<id>`
  （https は表示上カットされている）
- 各タブの DOCUMENT ノードの value は完全な `https://…`。ただし**非アクティブタブの分も
  同じ木に並ぶ**ので「木の中のどれか」を使ってはいけない

そこで `core/browser_url.py`（純粋・ユニットテスト付き）に選択規則を分離し、
`select_document_url_node` が (1) アクティブなページタイトルと一致する DOCUMENT →
(2) フォーカスされた DOCUMENT → (3) アドレスバー（`https://` を補う）の順で選びます。
**他タブの URL は使いません**（誤爆防止）。

コスト: クロスプロセス MSAA で実測 **約 2.5ms/ノード**、全探索は 141 ノード・523ms。
そのため探索はウィンドウごとに**バックグラウンドで 1 回だけ**行い、見つけたノードの
子 ID 経路（例 `(27, 6, 2)`）をキャッシュ → 以降は 3〜4 回の COM 呼び出し・**約 10ms**。
経路が解決できなくなったら（レイアウト変更・ブラウザ更新）キャッシュを捨てて再探索します。
初回だけ URL は `None` を返します（URL 照合プロファイルは一致しない＝安全側）。

## このカードで検証したこと / 未検証のこと

**検証済み（実行ログは handoff 参照）**
- Linux（WSL）: `python3 -m unittest discover -s tests -v` → **394 件 OK**
  （＋ v2-f/v2-g で `test_calibrate.py`（実測値の決定規則）と `test_registration.py`（登録キー）を追加）
- **実機 E2E（人手の打鍵、2026-10-28）**: メモ帳を前面にして実施し、ログで確認:
  - Enter×2 → `decision=DELIVER op=enter2 … resolved=key:tab` → `inject combo=tab`（2 連打が成立）
  - Enter×3 → `hold_extend_third` → 250ms 後に `resolved=sink:clipboard` → `capture step=… ok`
  - Enter×4 → `decision=CANCEL … reason=burst_cancel_3`（**何も出ず、通知もなし**。テキストは残存）
  - `Alt+F5` → `registered profile=notepad match={'process': 'notepad.exe'}`（設定ファイルへ追記）
  - `Alt+F6` → `registered profile=notepad-2 match={… 'title': '[*]ddd - メモ帳'}`（タイトルはリテラル化）
  - `F1`×3 → `panic state entered reason=panic_key_x3` → `hook removed`
- 以前の記録（300 件時点の内訳）: 238 件 + UI 55 件 + メモ帳 QA 設定 7 件
  （シンク選択 / `CanAccept` / 失敗分類 / フォールバック順序 / 一括挿入 / クリップボード復元の
  32 件は `tests/test_sinks_selection.py`、通知の要約は `test_sinks_fallback.py`、
  UI は `test_ui_model.py` / `test_ui_tray.py` / `test_ui_ime.py` の 55 件）
- `--dry-run` の `DRYRUN-SINK` 行（選択シンク・payload・method・送信キー・timeout）が
  実挙動（同じ `resolve_operation` / `select_sink` / `ISink` インスタンス）と一致すること
- Windows 側ビルド: `scripts/win_check.ps1` → `ADAPTER_IMPORT_OK` / `INPUT_SIZE 40` / `--once` 完走 / `WIN_CHECK_OK`
- Windows 実機セルフテスト: `scripts/win_selftest.py` → **21 チェック PASS**
  （クリップボード put/get 往復 ×3、64bit lparam の `CallNextHookEx`、実フック通過、
  スレッド間注入の所要時間。旧実装の形＝621ms ストール／現行＝1.1ms を実測）
- Windows 実機 UI チェック: `scripts/win_ui_check.py` → **23/23 PASS**
  （実 Tk・実トレイ・実 LL フック。トレイ登録 (`NIM_ADD`/`HICON`/`Shell_NotifyIconGetRect`)、
  トレイコマンドと `--settings` メッセージの両経路で設定画面が開くこと、3 タブ/18 行/IME ガード/
  レイアウトのクリップ無し、**保存の JSON 差分と再起動なし反映**、不正値で**ファイル未変更 + 項目名表示**、
  キャンセル復元、テスト送信の**二重クリック無視**と**失敗理由の表示**（Hermes を存在しない WSL
  ディストリに設定して意図的に失敗させ、画面に「失敗」と理由が出ることを確認）、パニック 1 クリック、終了。
  スクショ 3 枚（3 タブ）とレポート JSON を取得）
- 常駐 E2E プローブ: `scripts/app_probe.py` → **15 チェック PASS**
  （保留 Enter の 350ms 配信がキュー経由でフックスレッドから 1 回だけ送出されること、
  自己アンフックが起きないこと、**queue 済みの配信を素のキーが追い越さないこと（I6）**、
  パニックで保留を先に送出してからアンフックすること）
- `--dry-run` の実出力が design §3.9 の書式・§4.7 の 4 例・SUMMARY 4 値と一致すること（テストでも固定）

**未検証（QA カード t_62f3be62 の範囲）**
- ~~実機でのキー入力 E2E~~ → **2026-10-28 に人手の打鍵で実施済み**（上記「実機 E2E」）。
- クロスプロセス IME 判定（`ImmGetContext` + `GCS_COMPSTR` が MS-IME で True を返すか。
  この環境の前面ウィンドウは IMM コンテキストを持たず `composing=None`＝fail-open になる）
- `SendInput` の実送出（40 バイトは実機で確認済みだが、注入先アプリでの受理は未確認）
- ブラウザの URL 取得（MSAA `accValue` の best effort。失敗時は `None`＝URL 指定プロファイルは
  マッチしない安全側）と、textarea / contenteditable への実ペースト
- Hermes 非対話 CLI の実往復は **t_92c498ed で実測済み**: WSL 内から `hermes -z "<text>"` を直接実行し、
  親 env に `_HERMES_GATEWAY` を残したまま **13.9s / exit 0 / stdout 先頭行 `ENTER_ROUTER_SINK_OK`**。
  Windows 側 `wsl.exe` 経由（既定設定 `query_file_stdin=false`）も **QA-B の後続修正カードで実測済み**:
  `--exec` + 絶対パス `~/.local/bin/hermes`（`-c`）で **status=ok / 4.0s / 応答 `ENTER_ROUTER_SINK_OK`**（`_HERMES_GATEWAY` を
  親 env に残したまま）。旧 `--` + `-c` は `$1` が消えて `hermes -z ""` になり到達不能だった
- S3 前景奪取（`AttachThreadInput` 手順）と通知バルーン（トレイアイコン登録後）

## 開発ルール（実装カード必読）

1. `core/` は標準ライブラリのみ。OS 依存は必ず `platform/` に隔離する。
2. フックコールバックは「キュー投入 + 所要時間計測 + return」のみ（目標 1ms、
   OS の解除上限 1000ms）。例外は最外で全捕捉し素通し（fail-open）。
3. シンク（`HermesSink` 等）の実行はフックスレッドではなくワーカースレッドで行う。
4. `.ps1` は **ASCII のみ**で書く（PS 5.1 が BOM 無し UTF-8 を CP932 読みする実測バグ回避）。
5. ログに打鍵・取得テキスト・クリップボード内容を書かない（privacy, research R9）。

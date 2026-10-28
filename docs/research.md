# research.md — Win11 キー入力インターセプト＋Enter 制御＋入力ルーティング 実現可能性調査と技術選定

対象カード: t_4df5ca5d（調査・選定のみ／製品実装・リポジトリ作成は対象外）
作成者: reasoning-analyst
状態: **最終版**（2026-10-27。環境・注入は子プローブ t_8b0ec537 の実測、Hermes 経路は追加実測カード t_3c749424 の結果で確定。引用 URL は到達確認済み §7 参照）
生出力の一次記録: プローブカード t_8b0ec537 添付 `probe_outputs.md`（§1・§2-e、全文は添付参照）／追加実測カード t_3c749424 添付 `hermes_cli_route.md`・`doctor.txt`（§2-f、生ログ P1〜P5）

---

## 0. 前提（仮定）の明示

本調査は以下を**仮定**として置く。事実ではない。

- A1: 「拡張キー+Enter」はサイトにより Shift+Enter（改行）または Ctrl+Enter（送信）の意味で扱われ、ツールは**両方**を区別できる必要がある（カード本文の要旨から推定）。
- A2: ユーザーの「インクリメンタルサーチが走る」の主対象は **Web サイト側の検索・絞り込み（タイプするたびに発火）** であり、MS-IME 自身の逐次変換も含む広義の現象と解釈した（要約のみで原文の細部未確認。設計時にユーザー確認を推奨）。
- A3: 使用 IME は Windows 11 同梱の Microsoft IME（既定）を主対象とする。サードパーティ IME（Google 日本語入力・AzooType 等）は互換性を保証しない。
- A4: 常駐ツールは**通常権限**で動き、管理者権限で動くアプリのキーは対象外（UIPI により観測也不能。§2-a-2）。
- A5: 「外部プロセスから Hermes に 1 プロンプト投入」の正規経路は Hermes CLI の非対話 1 ターン実行と確定。推奨はグローバルの **`hermes -z "<prompt>"`**（応答本文のみ stdout・approvals 自動バイパス）。`hermes chat -q` も非 TTY かつ呼び出し元 env が正しければ PASS（§2-f 実測、t_3c749424）。
- A6: 主対象ブラウザは現状 **Firefox**（本マシンで実測した唯一のブラウザ）。Chromium 系（Edge/Chrome）の UIA 挙動は Hermes デスクトップアプリ（Chromium ベース）からの推定を含む（§2-e, QA 事項）。
- 本調査は技術契約（OS API・公式ドキュメント）に基づく。法務・ライセンス・配布ポリシー上の要求は含まない。

---

## 1. 環境の実地確認（要約）

実施: WSL2 → Windows interop（2026-10-27）。

**公開版ではこの節を要約している。** 生出力にはユーザー名・ホスト名・ディストリ名・
インストール済みパッケージの完全な一覧が含まれていたため伏せた（測定値・結論は変えていない）。

| 項目 | 結果 |
|---|---|
| OS | Windows 11（64bit） |
| PowerShell | 5.1（Windows PowerShell 系） |
| Windows 側 Python | 3.14 系 64bit。**pywin32 が import 可能**（`python -c "import win32api"` → exit 0） |
| Windows 側 .NET SDK | **無し**（ランタイムのみ） |
| WSL | WSL2。**Windows→WSL / WSL→Windows の interop とも動作**（`wsl.exe --exec …` → exit 0） |
| Linux 側 | python3 3.14 系 / uv / git。**.NET SDK は無し**（`dotnet: command not found`, exit 127） |
| drvfs | `/mnt/c` の読み取り可 |

**この表からの直接的な含意**: Windows 側に **Python 3.14 + pywin32 が実在**し、**両側に .NET SDK が無い**
（Windows はランタイムのみ、WSL はランタイムすら無し）。→ §3 の技術選定はこの 2 点で決まる。

---

## 2. 手法調査

### 2-a. キー横取り（interception）

| 手法 | 抑止 (swallow) | 観測 | 主な制約 | 一次情報（到達確認済み §7） |
|---|---|---|---|---|
| **WH_KEYBOARD_LL**（SetWindowsHookEx） | **可** | 可（全キー・タイムスタンプ・フラグ） | フックタイムアウト／UIPI／Secure Desktop／メッセージループ必須 | [LowLevelKeyboardProc](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc)、[SetWindowsHookExW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexw) |
| Raw Input（RegisterRawInputDevices） | **不可**（観測専用） | 可（スキャンコード等） | 抑止手段が存在しない | [Raw Input](https://learn.microsoft.com/en-us/windows/win32/inputdev/raw-input) |
| RegisterHotKey / WM_HOTKEY | 可（**修飾キー付きのみ**） | 可 | 登録した組み合わせが世界で使えなくなる。素の Enter 登録は全 Enter を殺すため不可 | [RegisterHotKey](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey)、[WM_HOTKEY](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-hotkey) |
| UIA TextPattern / ValuePattern | 不可（キーを横取りしない） | 可（テキスト読み取り・注入は可。§2-e） | ターゲットの UIA 露出に依存 | [UI Automation (Win32)](https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32) |

**制約の詳細（すべて到達確認済みの一次情報に準拠）**

1. **フックタイムアウト**: 公式記載「The hook procedure should process a message in less time than the data entry specified in the **LowLevelHooksTimeout** value in ... `HKEY_CURRENT_USER\Control Panel\Desktop`」。かつ「**Windows 10 version 1709 and later** The maximum timeout value the system allows is **1000 milliseconds** ... default to using a 1000 millisecond timeout if the LowLevelHooksTimeout value is set to a value larger than 1000」。つまり**最大待ちは既定で 1 秒**。超過すると OS がフックを黙って解除し、ツール無効化をユーザーが気づきにくい。→ [LowLevelKeyboardProc](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc)（本文・検証済み引用）。対策は §2-g。
2. **UIPI**: SendInput の公式記載「Applications are permitted to inject input only into applications that are at an **equal or lesser integrity level**」「This function fails when it is blocked by UIPI. Note that **neither GetLastError nor the return value will indicate the failure** was caused by UIPI blocking」。GetAsyncKeyState にも「**UI Privilege Isolation (UIPI)** prevents the calling thread from accessing the foreground thread」。→ 常駐（通常権限）は: ①管理者権限アプリへの**注入が黙って失敗**、②そのアプリのキー観測も限定的。→ [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)、[GetAsyncKeyState](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getasynckeystate)。**設計判断: 通常権限のまま運用し、管理対象外を明示的に受容**（注入失敗が無表示である点は注入後の読み戻し検証で吸収。§2-e）。※「通常権限フックに elevated ウィンドウ宛キーが届かない」の個別挙動は本調査で未実測 → QA 事項（§6）。
3. **Secure Desktop**: UAC 確認・画面ロックは別デスクトップで入力が処理され、通常デスクトップの常駐には届かない（分離モデルの一次資料: [Window Stations and Desktops](https://learn.microsoft.com/en-us/windows/win32/winstation/window-stations-and-desktops)）。**「フックに届かない」の実機確認は未実測 → QA 事項（§6）。当面は受容**（そもそも安全機構。触らない）。
4. **ビットネス**: 公式記載「This hook is **called in the context of the thread that installed it** ... must have a message loop」「the **WH_KEYBOARD_LL hook is not injected** into another process」。→ **注入型フックではないため x86/x64/ARM64 のビットネス不一致で壊れない**（32bit 常駐でも 64bit アプリのキーを扱える）。legacy の注入型 WH_KEYBOARD を使う場合はターゲットごとにビットネス一致 DLL が要る — 本ツールでは不採用。→ 同 [LowLevelKeyboardProc](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc)、[Hooks Overview](https://learn.microsoft.com/en-us/windows/win32/winmsg/about-hooks)。
5. **IME との処理順序**: 公式記載「The system calls this function every time a new keyboard input event is **about to be posted into a thread input queue**」。LL フックは**ターゲットスレッドへのキュー投入前**の入力を掴う。IME の確定処理は**ターゲットアプリのスレッド側**で発生するため、**「IME が消費してアプリに届かない Enter」でもフックには keydown が届く**。→ つまりツールは「未確定中に Enter が押された」ことを自分で観測し、素通し/抑止を決められる（判定材料は §2-b）。
6. **コールバックの返り値**: 公式記載「If the hook procedure processed the message, it may **return a nonzero value to prevent the system from passing the message to the rest of the hook chain or the target window procedure**」＝抑止可能。逆に未処理なら `CallNextHookEx` 必須（省略すると他プロセスの LL フックまで壊す）。→ 同ページ。

### 2-b. IME／日本語入力の状態把握

- **「確定前 Enter は IME が消費しアプリに届かない」の扱い（結論）**:
  - **フック側は観測可能**: raw keydown は「thread input queue への投入前」で届く（§2-a-5 の公式引用）。IME が後段で消費するかどうかに関わらず、ツールは Enter 押下を必ず見る。
  - **アプリ側で IME が消費するか（VK_RETURN が届かないか）は、本調査では未実測** → QA 必須事項（§6-1a）。届かない想定で設計する（未確定中は素通し＝IME に任せる、§2-b 末尾）。
- **未確定文字列の判定（跨プロセス）**:
  - **IMM32**: `ImmGetContext(対象HWND)` → `ImmGetCompositionStringW(himc, GCS_COMPSTR, …)` の戻りバイト数 >0 で未確定判定。Win10/11 の TSF ベース IME でも IMM エミュレーション層により実務上は動くが、**他プロセスのウィンドウに既定で使う API を向ける運用は公式サポート外のグレー領域**。→ [ImmGetContext](https://learn.microsoft.com/en-us/windows/win32/api/imm/nf-imm-immgetcontext)、[ImmGetCompositionStringW](https://learn.microsoft.com/en-us/windows/win32/api/imm/nf-imm-immgetcompositionstringw)。誤判定時は fail-open（素通し）に倒す設計が前提（R1）。
  - **`WM_IME_COMPOSITION` 経由の判定は自分のプロセスのウィンドウ宛てにしか来ない**ため、別プロセス常駐には使えない。→ [WM_IME_COMPOSITION](https://learn.microsoft.com/en-us/windows/win32/intl/wm-ime-composition)。
  - **TSF**: `ITfThreadMgr` 以降のコンパートメントはスレッドバインド COM。別プロセスから触るには対象スレッドへの DLL 注入が要り、常駐ツールの範囲を超える。→ [ITfThreadMgr](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nn-msctf-itfthreadmgr)。**v0 では不採用**（R1）。
- **判定の使い方（設計指針）**: 未確定あり → Enter は素通し（IME に処理を任せる）。未確定なし → Enter ポリシー（連打検出等）を適用。**「IME と戦わない」**。未確定判定が失敗した場合も結局は素通し側に倒れる（fail-open）。

### 2-c. インクリメンタルサーチの抑止 — 層別整理（スコープ切り分けの主目的）

| # | 層 | 発火メカニズム | 実装で抑止可能か | 根拠・代替 |
|---|---|---|---|---|
| L1 | **MS-IME の逐次変換**（カナ打ち中に候補が絞られる） | IME 内部 | **不可能**（外部プロセスから IME 内部処理に介入できない。キーを握ると日本語入力自体が死ぬ） | 設定（Windows 設定 > 時刻と言語 > 日本語の入力）で緩和できるかは**設定 UI の実地確認が未了**（項目名を本調査では断定しない。QA/設定確認事項）。**実装スコープ外** |
| L2 | **サイトの検索・絞り込み**（タイプするたびに JS が再フィルタ） | ページ JS（keydown/input/up） | **部分的**: 打鍵単位の発火を外から止めるのは不能（IME はコミット文字列をまとめてアプリへ渡す＝サイトは**コミット単位**の input を受ける）。**送信時注入のみ**: UIA SetValue／クリップボード貼付なら 1 回のイベントに集約でき、注入起因の再検索を**コミット回数分まで削減**できる | **実装スコープに採用**: 「通常打鍵の抑制はしない。注入はストローク逐次（SendInput）ではなく一括（貼付/UIA）を第一選択にする」 |
| L3 | **ブラウザのオートコンプリート**（アドレスバー・フォーム履歴） | ブラウザ内部 | **不可能**（外部プロセスから介入不能） | 受容。Esc で閉じる等のユーザー操作が回避 |
| L4 | **ページ内検索（Ctrl+F）** | ブラウザ内部 | **不可能**（検索バーへの打鍵を握ると検索も入力もできない） | 受容。自発的操作で本ツールの対象外 |
| L5 | **Windows の入力検索／リッチライブプレビュー等** | シェル | 原理的にはウィンドウクラス限定で抑止可能だが無意味・有害 | 対象外（OS 設定で無効化する領域） |

**導き出される実装スコープ**: L1・L3・L4 は「実装で不可能」→ **スコープに入れない**。L2 は「送信時注入の一括化」だけを取り込む。L5 は対象外。

### 2-d. 素早い連続 Enter の検出

- **検出器**: LL フックの keydown のタイムスタンプ（`KBDLLHOOKSTRUCT.time` ＋受信時刻）で、**keyup なしで連続する VK_RETURN keydown の間隔**でダブル/トリプルを判定。初期値案: **300ms 以内の 2 連打 = ダブル、さらに 300ms 以内 = トリプル**（これは**設計値の案**であり仕様ではない。設定可能値として外へ出し、状態機械カードで確定）。
- **チャタリング**: 物理チャタ（ms 級バウンス）は 5〜20ms 未満の重複除去で吸収。実運用の主問題はむしろ**キーリピートとの区別**。
- **キーリピート対策**: Windows のリピート挙動は公式に `SPI_GETKEYBOARDDELAY`（初期待ち）／`SPI_GETKEYBOARDSPEED`（リピート間隔）で制御され、両定数は [SystemParametersInfoW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-systemparametersinfow) に記載（到達確認・本文に両定数を確認済み）。**識別の確実なルール: keyup が挟まらない連続 keydown はリピートとしてカウントから除外**（OS 設定値に依存しない）。
- **修飾キー併用**: 同じ VK_RETURN でも `KBDLLHOOKSTRUCT.flags` の LLKHF_ALTDOWN と `GetAsyncKeyState(VK_CONTROL/VK_SHIFT)` で区別。**Ctrl+Enter はチャット系サイトが「送信」に使う頻度が高く、ツールが奪うと既存操作を壊す** → ルーティングに Ctrl+Enter は使わない。Shift+Enter（改行）も素通し優先（A1）。
- **F キー衝突**: F1/F3/F6/F11 はブラウザ既定で衝突。F13〜F24 は実キーボードに無く衝突ゼロだがユーザーも押せない（SendInput 合成は可）。**結論: ルーティングのトリガは「素早い連続 Enter」＋「登録済みホットキー（例: Ctrl+Alt+Enter）」の二本立て**が衝突最小。ファンクションキー単独は v0 では既定割当にしない。

### 2-e. 送り先へのテキスト注入（実地試験済み）

| 経路 | 一括性 | 依存 | 向く先 | 実測（t_8b0ec537） |
|---|---|---|---|---|
| クリップボード＋Ctrl+V | **1 イベント** | ほぼ全アプリ | ブラウザ（textarea/contenteditable 問わず）・Electron | **PASS**（4 イベント、連結一致、退避・復元も一致） |
| `SendInput`（KEYEVENTF_UNICODE） | 逐次 | なし | ほぼ全アプリ | **PASS**（46/46 イベント、UIA 読み戻し完全一致）。ただし **x64 の `INPUT` は 40 バイト必須** — union に MOUSEINPUT/HARDWAREINPUT を欠かすと 32 バイトで error 87 により**0 件送出**（v1 実測 FAIL） |
| UIA ValuePattern.SetValue / TextPattern | **1 イベント** | ターゲットの UIA 露出 | ネイティブ Edit 等 | アセンブリ読み込みのみ PASS（追加インストール不要）。SetValue の実注入は未試 |
| `WM_SETTEXT` | 1 イベント | Win32 制御 | ネイティブ Edit のみ | 未試。**ブラウザには届かない**前提で用途限定 |

**実地試験の結果（受入条件: 成功/失敗とログ）** — 生ログ全文は t_8b0ec537 添付 `probe_outputs.md` §C。要点:

1. **UIA 利用可否（C1）**: PowerShell 5.1 の `Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes` → `uia_assembly_load=TRUE`。**追加インストール不要**。
2. **フォーカス要素 dump（C2, Firefox）**: `ControlType.Document / FrameworkId=Gecko / Patterns=ValuePattern, TextPattern, ScrollItemPattern`（URL・本文はユーザー情報のため転記せず）。**フォーカスは Web コンテンツ領域そのもの**で個別 input ではなかった。
3. **注入 v1 = FAIL（重要な負の結果）**: `SendInput_return=0 expected_events=52 GetLastWin32Error=87` → x64 INPUT 構造体 32 バイト起因の `ERROR_INVALID_PARAMETER`。**`SendInput` が 0 を返すと 1 文字も入力されない（fail-loud）**。加えて BOM 無し UTF-8 の .ps1 が CP932 誤読され日本語が化けた。
4. **注入 v2 = PASS（C3-run4）**: `SendInput_return=46 expected_events=46 / readback_exact_match=True / notepad_sendinput=PASS`。`SendInput_ctrl_v_return=4 expected_events=4 / notepad_clipboard_paste=PASS`。クリップボードは事前退避→復元（長さ 86・内容一致 True/True）、対象は PID 指定で終了、`keys_sent_to_user_apps=false / registry_writes=false`。
5. **フォーカス奪取（C3-run1/2）**: 素の `SetForegroundWindow_ret=False`、`AppActivate_ret=True` なのに前景不変 → 中断（キー送出 0）。戦略比較の結果 **S3 のみ成功**: `AttachThreadInput(自分↔前景スレッド)` ＋ `AttachThreadInput(自分↔対象スレッド)` → `BringWindowToTop` → `SetActiveWindow` → `SetForegroundWindow`（`WINNING_STRATEGY=S3`, `final_foreground_pid=8972 proc=Notepad`）。**注入前処理として必須**。
6. **ブラウザ UIA（C4, Firefox、読み取り専用）**: depth2 走査 87 要素で**入力系 0 件** → depth5 で 6 件検出、**全て `ControlType.Document`（Gecko）**、`ControlType.Edit` は **0 件**。`ValuePattern.Value` = タブ URL、`TextPattern` = ページ本文、`AutomationId` 常空。→ **Firefox では textarea / contenteditable を区別できる粒度のノードが観測できない**。一方 Chromium 系（Hermes デスクトップアプリ, FrameworkId=Chrome）は `ControlType.Button` 等の細粒度ノードを露出 → **ブラウザ実装差が大きい**。
7. **ブラウザ特定方針の結論（R11 に反映）**: 「UIA ツリーから AI チャット入力欄を探して注入」は **Firefox では成り立たない**。v0 の第一候補は「**ユーザーが入力欄にフォーカスした状態で、フォーカス要素／前景ウィンドウへ注入**」（テキストはフォーカス先に入るのでツリー探索不要）。Chromium 系で探索的特定ができるかは Edge/Chrome 実機 QA（§6）。
8. **Chromium のアクセシビリティ前提**: Chromium はアクセシビリティクライアント接続までレンダラー側 a11y 計算を省略しうる。→ ツール自身が常時 UIA クライアントとして接続する／`--force-renderer-accessibility` 相当の状態が前提。→ [Chromium accessibility design doc](https://www.chromium.org/developers/design-documents/accessibility/)（到達200）。※ フラグ名の有無は実装時に確認する（本調査では未検証）。

### 2-f. Hermes エージェントへの経路（実地試験済み・t_8b0ec537 + t_3c749424）

- **公式手段の有無 → 有り（確定）**: 公式ドキュメント [CLI Commands Reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)（HTTP 200・本文取得済み）に非対話 1 ターン経路が明記:
  - `-q, --query "..."`: 「With `--oneshot`, `-Q`, or non-TTY stdio it **answers and exits**」
  - `--query-file PATH`: 「**Safe for arbitrary text: nothing is shell-interpreted**, so quotes, $(...), and backticks are preserved verbatim」→ **シェル注入の心配なしで本文を渡せる公式経路**（`-` で stdin も可）
  - `--oneshot` / `-Q`: 非対話 1 ターン。`--format stream-json`: 「structured JSONL output ... **when a program needs to consume progress without scraping terminal output**」（machine-readable 応答の公式手段。session_id の取得経路でもある）
  - グローバル側に **`-z, --oneshot PROMPT`**: 単発実行で**最終応答テキストのみを stdout**（バナー・スピナー・session_id 行なし、approvals 自動バイパス）。プロファイル指定は**グローバル** `-p/--profile`（`hermes chat --help` には `--profile` は無い）。→ 実在確認: t_3c749424 の `hermes --help` 全出力
  - `hermes serve`: 「the JSON-RPC/WebSocket gateway the desktop app and remote clients connect to ... headless」（既定ポート 9119）= 将来の低遅延経路として公式に存在
  - ドキュメント根拠: プローブ D3（docs ルート HTTP 200 /35230 bytes、CLI ページ 362111 bytes）と `hermes chat --help` 生出力（t_8b0ec537 添付）。
- **ライブ試験（失敗の再現と原因特定・成功 4 回。生ログ = t_3c749424 添付 `hermes_cli_route.md`）**:
  - **失敗（P0/P1）**: `hermes chat -q "Reply with exactly: ENTER_ROUTER_PROBE_OK"` → **exit=1, 2.51 秒**（t_8b0ec537）。t_3c749424 の同一シェル再現は **exit=1, 2.07 秒**。stdout:

    ```
    Query: Reply with exactly: ENTER_ROUTER_PROBE_OK
    Hermes couldn't start the model connection: invalid_params. Your message was not
    sent. Run `hermes doctor` to check the setup, or /model to pick a different provider.
    Goodbye! ☤
    ```

  - **原因（実測で確定・推定ではない）**: provider/model の不整合ではなく、**呼び出し元プロセスから継承した環境変数 `_HERMES_GATEWAY=1`**。同一シェル・同一引数・同一バイナリで**この 1 変数だけを外すと FAIL → PASS に反転**（P1 exit=1/2.07s → P4 exit=0/3.01s）。`hermes config get model.default/model.provider` は `_HERMES_GATEWAY` の有無で同一値。**`hermes doctor` の警告「vendor-prefixed slug と provider=commandcode の不整合」は本件の原因ではない**（同じ構成で 4 回成功。doctor.txt 参照）。
  - **成功実測（4 回 PASS、すべて非 TTY）**:

    | # | 呼び方 | exit | 秒 | stdout |
    |---|---|---|---|---|
    | P2 | `env -u _HERMES_GATEWAY ... hermes -p backend-implementer -m deepseek/deepseek-v4.1-flash --provider commandcode -z "..."` | 0 | 3.68 | `ENTER_ROUTER_OK` |
    | P3 | `env -i HOME=... PATH=... LANG=C.UTF-8 TERM=dumb hermes -z "..." < /dev/null`（最小環境・指定なし） | 0 | 4.02 | `ENTER_ROUTER_OK` |
    | P4 | `env -u _HERMES_GATEWAY hermes -z "..."` | 0 | 3.01 | `ENTER_ROUTER_OK` |
    | P5 | PowerShell → `wsl.exe -d <distro> -- /bin/bash -lc "hermes -z '...'"` | 0 | 4.44 | `ENTER_ROUTER_OK` |

  - **推奨呼び方（確定）**: 応答取得は **`hermes -z "<prompt>"`**。**子プロセスの env から `_HERMES_GATEWAY` は必ず除去**（spawn 時に env を作り直す / `env -u _HERMES_GATEWAY`。Hermes 自身も子生成時にこれを pop している: `hermes_cli/gateway.py:1057`）。タイムアウトは **30 秒**（成功時最大 4.44s の約 7 倍）。成功判定は **`exit==0 && stdout 非空`**。失敗は**約 2 秒で exit≠0** に返る＝ハングではないため、長時間ハング待ちは不要（「2〜3 秒で exit≠0 かつ `invalid_params`」を設定異常として早期検知する分岐を入れるとより良い）。
- **Windows → WSL2（フルチェーン実測済み）**: `wsl.exe -d <distro> -- uname -a` → exit 0（§1）。さらに **P5: PowerShell → `wsl.exe -d <distro> -- /bin/bash -lc "hermes -z '...'"` → exit=0 / 4.44 秒 / 応答 `ENTER_ROUTER_OK` を実測**。Windows 側の環境変数は WSL に引き継がれないため `_HERMES_GATEWAY` が混入しない点も確認済み。`bash -lc` はログインシェル経由で API キー等の環境変数を拾うための指定。`hermes` の実体は `~/.hermes/hermes-agent/.hermes/bin/hermes`。残る QA は「任意テキストを `--query-file` で渡す組合せ」（実測は短い引数のみ）。
   **訂正（後日の実測・t_69691c23 / t_eccd2e2f）**: 上の P5 が通ったのは、プロンプトを `-lc` の**スクリプト文字列の中**に置いていたため。`--` 区切りを採用すると interop 層が末尾をシェル評価するので、(a) `"$1"` 形式で渡したプロンプトは**届かない**（`$#==0`・`hermes -z ""`）、(b) コマンドライン上のプロンプト内の `$`/バッククォートが**実行される**（`$(id)` 実測）。実装は `--exec` 固定（argv を逐語で渡す）。また `hermes` は現在 `~/.local/bin/hermes` にあり、これは**ログインシェル（`-lc`）でしか PATH に乗らない**（非ログイン `-c` は exit 127）。正本は design §5.3 と `src/enter_router/platform/hermes_cli.py` の docstring。
- **v2 拡張点の材料（実装は本調査の対象外・観測のみ）**: `-z` の stdout に session_id は出ないが、セッションは SQLite `~/.hermes/state.db` に `local-<64hex>` で作られ `hermes sessions list` で観測できる。ID の取得は `-Q`（final response + session info）か `--format stream-json`（先頭 `system`／末尾 `result` イベントに session_id）。再開は `--resume/-r <id>`・`--continue/-c <title>`・`--create-if-missing`。デスクトップは同一 `state.db` を共有し別フロントエンドから再開できる旨が公式 docs にある（user-guide/sessions#how-sessions-work, user-guide/desktop）。**デスクトップからの再開は未実測**。
- **代替経路の比較**:

  | 経路 | 遅延 | 確実性 | 結論 |
  |---|---|---|---|
  | `wsl.exe -- bash -lc 'hermes -z "<text>"'`（短文・応答本文のみ要る場合） | **3.01〜4.44s（実測・wsl 起動込み）** | **公式手段・PASS 4 回・exit code あり** | **v0 採用（第 1。区切りは `--exec` に改訂・§2-f 訂正）** |
  | `hermes chat --query-file -`（stdin・任意テキスト。非 TTY で自動 oneshot） | 未実測（同系と推定） | 公式（シェル解釈なし）。**組合せは QA で 1 回確認** | **v0 採用（長文時）** |
  | `hermes serve`（ローカル JSON-RPC/WebSocket, :9119） | 低 | 公式だがサーバ常駐＋プロトコル依存 | 将来の高速化オプション（v0 対象外） |
  | プロバイダ HTTP API を直接叩く | 低 | 確実だがエージェント機能（メモリ・ツール）を失う | 却下（要件は「Hermes のようなエージェント」＝実 Hermes） |
  | ファイル inbox 等の独自経路 | — | `--query-file` が公式に存在する以上別経路は不要 | 却下（公式経路で代替） |

### 2-g. 事故防止（フックがキーを飲み込んだまま固める事故）

1. **コールバックは「キュー投入＋即 return」のみ**（§2-a-1 の 1 秒制限に対し 1ms 目安）。例外は最外で全捕捉し、**すべて素通し（fail-open）**。例外がフックから漏れ OS にフック解除される事態を避ける。
2. **所要時間計測**: 1 回あたりの時間を測り、閾値超過が連続したら**自己アンフック**（UnhookWindowsHookEx）して全キー素通しに切り替える。
3. **ウォッチドッグ**: フック用メッセージループの heartbeat を監視し、無応答超過でプロセスを自殺・再起動（プロセス死亡時は OS がフックを自動撤去 → 結果として入力は回復。公式のタイムアウト挙動は §2-a-1）。
4. **パニックキー**: フック内完結で判定できる組み合わせ（例: Ctrl+Alt+Shift+F12）を最優先処理し、即アンフック＋トレイ通知。**UI に依存しない**こと。
5. **最小権限**: 通常権限のまま。管理者化しない（§2-a-2）。
6. **再送しない設計**: 抑止したキーを後から再送しない（再送ストームが最大の事故源）。停止時は「これ以降素通し」のみ。
7. **ログは決定理由のみ**（押下文字列・入力本文はログに書かない）。ローテーション付きファイルログ。

---

## 3. 技術選定（1 案に確定）

### 確定: **Python 3.14 常駐（Python 3.14（64bit） + pywin32、`pythonw.exe` ＋トレイ）**

**判断根拠（すべて §1 実測に基づく）**:

1. **カードの判定ルールが成立**: 「Windows 側に Python+pywin32 があるなら Python 常駐」— Python 3.14.0 が Python 3.14（64bit） に実在し、`import win32api` 成功（pywin32 導入済み・実測）。
2. **代替の .NET ルートが現状不能**: WSL 側・Windows 側とも **SDK 0 件**（Windows はランタイムのみ）。カード指定の `dotnet build -p:EnableWindowsTargeting=true` は **SDK が無いと実行できない**。採用には **どちらかへの SDK＋NuGet 復元の導入**が必要で、「お試し・最小コスト」「追加インストール最小」に反する。
3. **ビルド工程が存在しない**: Python はスクリプト言語のため「ビルド」は構文チェック＋import チェックで済む。起動確認も 1 コマンド。CI も不要。
4. **Linux 単体テスト要件と整合**: 設計カードの「純ロジック層を WSL 上で `pytest` によりテスト」が、追加導入なしで可能（WSL に python3 3.14.9 あり。uv 0.6.2 もあり）。
5. **必要な Win32 面がカバーされる**: WH_KEYBOARD_LL（ctypes の SetWindowsHookExW ＋メッセージループ）、SendInput（ctypes、40 バイト INPUT 構造体に注意）、クリップボード（win32clipboard）、トレイ（`win32gui.Shell_NotifyIcon`、追加依存なし）、UIA 読み戻し（`win32com`、不可なら PowerShell `Add-Type` サブプロセス＝実測済み経路にフォールバック）。

**構成（1 案）**: `pythonw.exe`（コンソール無し）で 1 プロセス常駐。LL フック用メッセージループ＋トレイアイコン＋設定 JSON のホットリロード。純ロジック層（Enter 判定・状態機械・送り先選択）を OS 非依存に分離し WSL の pytest で検証。管理者権限は持たない。HermesSink は `wsl.exe -d <distro> --exec /bin/bash -lc 'exec hermes -z "$1"'` を spawn し（`--exec`＋`-lc` は必須。§2-f の訂正を参照）、**子プロセス env から `_HERMES_GATEWAY` を除去**（§2-f）。

**既知の弱点と対策（受容）**: CPython の GC ポーズがフック応答（上限 1000ms、既定 1000ms）に食い込む可能性 → コールバックを 1ms 級のキュー投入のみに限定＋所要時間計測＋自己アンフック（§2-g）。お試し用途では受容に値する。

### 却下案と理由

| 案 | 却下理由 |
|---|---|
| **C# / .NET（WinForms）** | **SDK が両 OS に無い**（実測）。SDK 導入＋NuGet 復元＋クロスビルドという追加コストが最小コスト要件に反する。技術自体は viable（ランタイムは 8/10 が入っている）だが、**導入コストの差が決定的**。SDK を入れるなら次点 |
| AutoHotkey v3 | IME 未確定判定・UIA 注入・「Linux 単体テスト可能な純ロジック層」が満たせない。実装は最小だが**要件不能**で却下 |
| Rust / C++ | 可靠性は最高だが実装コスト最大。お試し要件に反する。ビルドも両 OS で追加導入要 |
| Electron / Tauri 常駐 | 常駐の重量化と配布サイズ増。要件外 |
| Node.js 常駐（node は実在 v24.11.0） | WH_KEYBOARD_LL の生バインディングが標準に無く、ネイティブアドオン作成（node-gyp/SDK）が必要 → 追加コスト。FFI で自作は Rust/C++ と同コスト |
| PowerShell 常駐のみ（Add-Type で C# インライン） | 可能ではあるが、PS 5.1 の文字コード問題（実測: BOM 無し UTF-8 が CP932 誤読）・プロファイル/UI・テスト容易性で不利。pywin32 実在の現環境では優位性なし |
| Python＋ctypes のみ（pywin32 を使わない構成） | 可能だが、pywin32 が既にある環境でわざと手ぶらにする必然性がない。pywin32 を主、ctypes を補助とし、**pywin32 不在の Python315 側は使わない**（pywin32 未検証のため） |

### WSL からの「ビルド」・起動確認コマンド列（Python 構成・具体）

```bash
# --- パス（実測済みの実パス） ---
PSBIN=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe   # powershell.exe は PATH に無い（実測）
PYWIN=/mnt/c/Python314/python.exe                                     # pywin32 あり（実測）
REPO_WIN='<repo>' # リポジトリは設計カードが作成

# (0) interop 確認（実測済み）
/mnt/c/Windows/System32/wsl.exe -l -v

# (1) 「ビルド」に相当する検証: Windows Python で構文・import チェック
$PYWIN -m py_compile "$REPO_WIN\src\enter_router\main.py"
$PYWIN -c "import win32api, win32gui, win32con, win32clipboard"   # pywin32 到達確認

# (2) 純ロジック層のテストは WSL 側で
cd ~/enter-router && python3 -m pytest tests/ -q

# (3) 起動確認: scripts/launch_check.ps1 を「位置引数形式」で実行
#   （この開発環境のコマンドフィルタは 'powershell.exe -Command/-File' をブロックするため。詳細は付録）
"$PSBIN" -NoProfile -ExecutionPolicy Bypass "$REPO_WIN\scripts\launch_check.ps1"
#   launch_check.ps1 の要点:
#     Start-Process pythonw.exe -ArgumentList '<repo>\src\enter_router\main.py'
#     Start-Sleep 2
#     Get-Process pythonw | Where-Object { $_.Path -like '*\python.exe' } | Select-Object Id, StartTime
#     （＋専用ログに 'hook_installed' 行が出たことを確認。pythonw.exe の所在は (1) の py_compile と併せて実測確認）

# (4) HermesSink 相当の疎通確認（WSL 内・実測 PASS 済みの呼び方）
#   env -u _HERMES_GATEWAY hermes -z "Reply with exactly: ENTER_ROUTER_OK"   # exit=0 / 3.01s
```

注: Windows Python に渡すパスは `\\wsl.localhost\...` の UNC（バックスラッシュ）で明示する。BOM 無し UTF-8 の .ps1 は PS 5.1 で CP932 誤読されるため、**.ps1 は BOM 付き UTF-8 で書く**（実測済みの事故）。

---

## 4. 想定アーキテクチャ概要（テキスト図）

```
[キーボード]
    │ raw keydown/keyup（IME 処理前・キュー投入前）
    ▼
┌───────────────────────────────────────────────────────┐
│ enter-router 常駐プロセス（通常権限・pythonw.exe / pywin32）│
│                                                       │
│  [LLフック スレッド]──キュー投入のみ(1ms目標)──┐        │
│      │ 押下イベント              ▲           │        │
│      ▼                          │ heartbeat │        │
│  [IME状態クエリ] ImmGetContext(フォアグラウンド)│       │
│      │ 未確定あり/なし（失敗＝素通しに fail-open）│     │
│      ▼                          │           ▼        │
│  [ポリシーエンジン（OS 非依存・WSL pytest 可）] │        │
│      │ 判定: 素通し / 抑止 / トリガ検出(連打)  │        │
│      ▼                          │           │        │
│  [ルーター] ──連打Enter/ホットキー──► [シンク]  │        │
│      │                            ├ BrowserChatSink   │
│      │                            │  (フォーカス先へ:  │
│      │                            │   貼付→SendInput、│
│      │                            │   前景はS3手順)    │
│      │                            ├ HermesSink        │
│      │                            │  (wsl.exe --       │
│      │                            │   hermes -z /      │
│      │                            │   --query-file、   │
│      │                            │   env再構築で      │
│      │                            │   _HERMES_GATEWAY  │
│      │                            │   除去・30s TO)    │
│      │                            └ ClipboardStash    │
│  [トレイ UI＋設定ウォッチ＋パニックキー]                 │
└───────────────────────────────────────────────────────┘
        │ 未確定中 Enter＝素通し（IME に任せる）
        ▼
[フォアグラウンドアプリ（ブラウザ等）]
```

モジュール境界・状態機械の数値確定は設計カード t_b102dc49（system-architect）の担当。本図は契約のたたき台。

---

## 5. 未解決リスク一覧（回避策 or 受容＋理由）

| # | リスク | 影響 | 回避策／受容判断 |
|---|---|---|---|
| R1 | 跨プロセスの IMM 取得が TSF IME／サードパーティ IME で空を返す | 未確定判定が誤る → Enter を握る/握らない誤判断 | QA で MS-IME 実機検証（§6-1a）。誤判定時は fail-open（素通し）。サードパーティ IME は非保証を明記して**受容**（A3） |
| R2 | OS がフックをタイムアウト（最大 1000ms）で黙って解除 | ツール無効化にユーザーが気づかない | ウォッチドッグ＋トレイ常時表示（有効/無効）。fail-open で入力は回復する設計。**残余は受容** |
| R3 | contenteditable への UIA SetValue が使えない | 注入失敗 | フォールバックチェーン: 貼付 → SendInput。最終経路は動作実測あり（§2-e）。**受容済み（回避策実証済み）** |
| R4 | フォーカス奪取が素の SetForegroundWindow で失敗（実測あり） | 注入先が違うウィンドウに行く | **S3 手順（AttachThreadInput×2 → BringWindowToTop → SetActiveWindow → SetForegroundWindow）で成功実測**。＋注入直前に「注入先ウィンドウ＝期待先」検証、不一致なら送出 0 件で中断（プローブでも実際に中断動作を確認） |
| R5 | Hermes 応答遅延（CLI 起動〜応答 **実測 3.01〜4.44s**、モデル応答は数十秒になり得る） | 体感が悪い | トレイに「送信中」表示＋タイムアウト 30s（§2-f 推奨）。**受容**（速度より確実性優先。低遅延化は将来 `hermes serve`） |
| R6 | UIPI: 通常権限では elevated アプリへ注入が**黙って失敗**（GetLastError でも分からない） | その環境下でツール無効 | 注入後の**読み戻し検証（UIA/クリップボード確認）で失敗を明示**。通常権限運用を前提に**受容**（最小権限の価値が上回る） |
| R7 | Secure Desktop（UAC/ロック）中は無監視 | その間の Enter 制御なし | **受容**（安全機構。触らない）。「フックに届かない」の実測は §6-1f |
| R8 | Chromium の a11y 接続初期遅延・負荷、フラグ名の未検証 | 注入の初回失敗や負荷 | ツールが常時 UIA クライアント接続を保持。Edge/Chrome 実機 QA（§6-1b） |
| R9 | キー押下を内部保持すること自体のプライバシー | 情報漏洩リスク | 押下文字列をログに書かない・メモリ上のみ・終了時破棄。**受容**（範囲限定） |
| R10 | Windows 11 新 UI の UIA バリエーション | 特定ウィンドウでの判定ズレ | 対象を「フォーカス先注入」中心に限定し**受容** |
| R11 | **Firefox でチャット入力欄を UIA 特定できない（実測: Edit 0 件・Document のみ）** | 「サイトを開いてツリー探索→注入」方式が Firefox で不能 | **第 1 候補を「フォーカス済み要素へ注入」方式に変更**（本文 §2-e-7）。Chromium 系での探索的特定は QA 後に判断。対象外ブラウザは受容 |
| R12 | 「確定前 Enter を IME が消費しアプリに届かない」が未実測 | ポリシー境界の想定誤り | QA 必須（§6-1a）。届かない前提で設計し、観測できるのはフック側のみ（§2-b） |
| R13 | Python3.14 の `pip`/comtypes/pywinauto の有無が未確認 | UIA 高機能操作が使えない可能性 | **v0 は pywin32＋ctypes のみで成立させる**（SendInput/フック/クリップボード/トレイはカバー済み）。UIA 読み戻しは PowerShell `Add-Type` サブプロセス（実測済み）にフォールバック。pip 確認は 1 コマンド（`python.exe -m pip --version`）で実装初日に判定 |
| R14 | **Hermes 呼び出しの `invalid_params`（原因実測確定: 呼び出し元から継承した `_HERMES_GATEWAY=1`）** | env を素通しで spawn すると応答が来ない（exit=1 / 約 2s で即失敗） | **回避策確定（t_3c749424 で実証）: spawn 時に env を作り直し `_HERMES_GATEWAY` を除去する**（`env -u _HERMES_GATEWAY` で FAIL→PASS 反転を確認）。成功判定 `exit==0 && stdout 非空`、タイムアウト 30s。`hermes doctor` の vendor-prefixed slug 警告は原因ではない（振り回されない） |
| R15 | インジェクト失敗の黙殺（R6）＋SendInput=0 が fail-loud でないケース | 「送ったつもり」 | 注入後に UIA/クリップボードで読み戻し照合する（実測 PASS 手法を踏襲） |

---

## 6. 検証計画（設計・実装・QA カードへの申し送り）

1. **QA 必須（本調査で未実測の最重要項）**
   - (a) **IME**: MS-IME の未確定中に Enter → IME が確定して VK_RETURN がアプリに届かないこと／LL フックには keydown が届くこと／`ImmGetContext(他プロセスHWND)` の GCS_COMPSTR 判定が MS-IME で動くこと。
   - (b) **ブラウザ**: Edge/Chrome の AI チャット欄（textarea と contenteditable 各 1 サイト）で注入チェーン動作。Firefox は「フォーカス先注入」前提の動作確認のみ。
   - (c) **Hermes 残課題**: ①任意テキストを `hermes chat --query-file -`（stdin）で渡す組合せ（`-z` の短文のみ実測済み）②HermesSink の spawn env から `_HERMES_GATEWAY` 除去を実装した後、Windows 常駐起点で 1 往復の実機確認。※「Windows → `wsl.exe` → `hermes -z` → 応答」自体は P5 で PASS 済み（§2-f）。
   - (d) **フック安全性**: パニックキーで即アンフック／フック停止時に全キー素通し／低速コールバック強制時にタイムアウト解除→素通しで回復すること。
   - (e) **UIPI**: elevated 窓宛てのキーが通常権限フックに届かないこと（§2-a-2 の個別挙動）と、SendInput が elevated へ黙って 0 を返すこと。
   - (f) **Secure Desktop**: UAC 確認中に入力が常駐に届かないこと（§2-a-3）。
2. **実装初日**: `python.exe -m pip --version`（R13）、`pythonw.exe` の所在確認（§3 コマンド）。
3. **設定確認（ユーザー）**: MS-IME の逐次変換を緩和する設定の有無（§2-c L1）、A1/A2 の要件解釈確認。

---

## 7. 一次情報リンク到達一覧（検証方法付き）

検証方法: ①プローブ t_8b0ec537 の `curl -sIL`（16 件）②本カードのサーバーサイド取得による本文確認（添付 `urlcheck_*.html/json`、`<title>` 照合済み）。**本文中の引用文はすべて ②の取得本文からの逐語引用**（①のみのものは本文未取得と明記）。

| URL | 状態 | 本文確認 |
|---|---|---|
| .../winmsg/lowlevelkeyboardproc（LowLevelKeyboardProc） | 200 | ✅ タイトル照合＋引用3点（swallow/タイムアウト/投入前） |
| .../api/winuser/nf-winuser-setwindowshookexw | 200 | ①のみ（HEAD） |
| .../api/winuser/nf-winuser-sendinput | 200 | ✅ UIPI 引用2点 |
| .../api/winuser/nf-winuser-getasynckeystate | 200 | ✅ UIPI 引用1点 |
| .../api/winuser/nf-winuser-registerhotkey | 200 | ①のみ |
| .../inputdev/wm-hotkey | 200 | ✅ タイトル照合 |
| .../inputdev/raw-input | 200 | ✅ タイトル照合 |
| .../winmsg/about-hooks（Hooks Overview） | 200 | ✅ タイトル照合 |
| .../api/winuser/nf-winuser-systemparametersinfow | 200 | ✅ SPI_GETKEYBOARDDELAY/SPEED 本文確認 |
| .../api/imm/nf-imm-immgetcontext | 200 | ①のみ |
| .../api/imm/nf-imm-immgetcompositionstringw | 200 | ①のみ |
| .../intl/wm-ime-composition | 200 | ①のみ |
| .../api/msctf/nn-msctf-itfthreadmgr | 200 | ①のみ |
| .../winauto/entry-uiauto-win32 | 200 | ✅ タイトル照合 |
| .../dotnet/api/system.windows.automation.textpattern | 200 | ✅ タイトル照合 |
| .../dotnet/api/system.windows.automation.valuepattern | 200 | ✅ タイトル照合 |
| .../winstation/window-stations-and-desktops | 200 | ✅ タイトル照合 |
| chromium.org/developers/design-documents/accessibility/ | 200 | ①のみ |
| hermes-agent.nousresearch.com/docs（＋/reference/cli-commands） | 200 | ✅ 本文取得・引用（プローブ D3） |

**404 が判明し本稿から外した URL**（プローブ E 項実測）: `winmsg/wh-keyboard-ll`, `winmsg/lowlevelhooktimeouts`, `winmsg/wm-hotkey`, `winauto/uia-textpattern`, `api/uiautomationcore/nn-uiautomationcore-ivalueprovider`, `winmsg/message-and-message-integrity`, `windows-hardware/customize/desktop/configure-keyboard-response` — いずれも**上記の到達200 URL に置き換え済み**（例: タイムアウトと抑止 semantics は lowlevelkeyboardproc 本文に記載あり、UIPI は SendInput/GetAsyncKeyState 本文に記載あり）。

---

## 8. 受入条件の充足マッピング

| 受入条件 | 充足箇所 |
|---|---|
| Win11 ビルド番号と各ランタイムの有無が実コマンド出力付き（推測なし） | §1（生出力転記＋出典コマンド） |
| 2-a〜2-g 全項目に一次情報リンク付きの結論 | §2-a〜§2-g、リンク到達は §7 |
| インクリメンタルサーチ抑止を層別に「実装で可能/不可能」→ スコープ案 | §2-c（L1〜L5 表、L2 のみ取り込みで確定） |
| テキスト注入と Hermes 経路を 1 回試した結果（成否とログ） | §2-e-3〜6（PASS/FAIL 実ログ）、§2-f（失敗 exit=1 の再現＋成功 4 回 P2〜P5 の実測表。生ログ: t_8b0ec537 `probe_outputs.md` / t_3c749424 `hermes_cli_route.md`・`doctor.txt`） |
| 実装言語・構成を 1 つに確定、選定理由と却下理由 | §3（Python 3.14＋pywin32 確定、却下 7 案） |
| 未解決リスクに回避策または受容判断 | §5（R1〜R15。R14 は原因特定・回避策とも実測確定） |
| `docs/research.md` 添付 | 本カード添付（kanban_attach。添付できない場合は handoff の artifacts から同ファイルを配信 — 実行環境の制約は handoff に明記） |

---

## 付録: 後続カード必読 — この開発環境の制約（プローブ実測・製品コードには無関係だが開発手順に影響）

1. terminal の危険コマンドフィルタが `powershell.exe -Command/-File` と `Stop-Process ... -Force` を**コマンドライン文字列で**事前ブロック（headless で承認者不在）。対策: .ps1 を書き、位置引数形式 `powershell.exe -NoProfile -ExecutionPolicy Bypass '\\wsl.localhost\...\x.ps1'` で起動。**フィルタはスクリプト内容は検査しない**。
2. UNC パス上の .ps1 は ExecutionPolicy で拒否 → `-ExecutionPolicy Bypass` 必須。
3. PowerShell 5.1 は BOM 無し UTF-8 の .ps1 を CP932 読みする（日本語リテラル化け・実測）→ **.ps1 は BOM 付き UTF-8**。
4. `WSLENV` に載せないと環境変数は Windows 側プロセスへ渡らない。
5. `powershell.exe` は PATH に無い → `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe` の絶対パス。
6. x64 の `INPUT` 構造体は **40 バイト必須**（union に MOUSEINPUT/HARDWAREINPUT 含めないと SendInput が error 87 で 0 件）。Python 実装でも ctypes の構造体定義で同じ罠がある。
7. 前景奪取は **S3 手順**のみ成功（素の SetForegroundWindow は False）。
8. Win11 メモ帳はセッション復元で過去タブを開く → 試験時は専用の空ファイルを引数で開き、タイトルで所有確認。
9. WSL 内で Hermes を子プロセス呼び出しする際は **`_HERMES_GATEWAY` を除去**すること（§2-f / t_3c749424 で実測確定。gateway 系 `_HERMES_*` 変数の除去を推奨）。

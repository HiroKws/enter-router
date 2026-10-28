# enter-router 設計（確定版 v2）

対象カード: t_b102dc49（設計＋リポジトリ初期化）／実装: t_57577e39（中核）, t_e0676b15（UI）
前提資料: `docs/research.md`（親カード t_4df5ca5d の実測調査。技術選定はこれに従う）
状態: **確定**（2026-10-27。v2 でオペレータ指示 4 件を反映）。本ファイルが実装カード間の契約である。
矛盾を見つけた実装カードは本ファイルを変更せず、コメントで設計者に差し戻すこと。

## 0.0 改訂履歴（レビュー用・削除禁止）

| 版 | 指示 | 変更点 |
|---|---|---|
| v1 | 設計カード初版 | 単一テーブル（`enter_action` + `hotkeys`）、既定 Ctrl+Enter→Hermes、PANIC 時保留 Enter 破棄 |
| v2-a | オペレータ注記「Enter を変換器に」 | トリガ/アクション概念、保留+フラッシュ、順序保証を導入 |
| v2-b | オペレータ注記「単一表は不正確・2 層マッピング」 | **`global.operations`（第1層・全アプリ共通の操作→意味）と `profiles[].keymap`（第2層・意味→実キー列）に分離**。単一表設計と v1 の既定値の案は**取り下げ** |
| v2-c | オペレータ注記「操作レイヤ既定の確定」 | 既定マップ確定（Enter=改行/2連=送信/3連=Hermes）、保留窓 **250ms**・`hold_mode`、順序保証、パンニック時も順序付きフラッシュ |
| v2-d | オペレータ注記「Shift/Ctrl+Enter を pass にしない」「必須トリガ警告」「injected 再処理禁止」 | shift/ctrl+enter 既定=意味割当、`pass` は明示選択のみ、全トリガ明示サンプル、欠落トリガは警告列挙、**`LLKHF_INJECTED` は処理対象外（削除禁止）** |
| v2-g | オペレータ注記「パニックはコンビネーションでは緊急時に思い出せない／売っている余裕がない。F1 連打にして。あと alt+ファンクションで前面プロセスを登録したい」 | **パニック停止をコードから連打へ**: `panic_key`(既定 `f1`) を `panic_repeat`(既定 3, 2〜6) 回、`triple_enter_window_ms`(250) 以内に叩くと停止（`rule1`。押した F1 自体は素通しなので単発 F1 は従来どおり）。F12 は機種によって無い/位置が違うため既定から外した。**登録キー**を新設（`registration.process_key`=既定 `alt+f5` / `registration.process_title_key`=既定 `alt+f6`、**alt+F1〜F24 のみ**許可）: 押した瞬間の前面アプリを **プロファイルとして設定ファイルへ追記**し、その場で適用（プロセスのみ / プロセス＋ウィンドウタイトル。タイトルは**リテラル**として glob エスケープ）。使用する F キーは設定（JSON / 設定画面タブ A）で変更可 |
| v2-f | オペレータ注記「2 連打が動かない」「窓は実測で決めろ」「4 連打以上はキャンセル」 | **窓を実測で確定: `hold_window_ms` = `triple_enter_window_ms` = 250ms**（連続 24 サンプル 96〜201ms / 非連続 5 サンプル 495〜585ms。`docs/measurements.md`、計算は `core/calibrate.py`、計測器は `scripts/measure_enter.py`）。v2-e の「一般的な 2 連打は 200〜350ms」という一般論の根拠を実測に置換。**3 発目は 250ms 保留してから発火**し、**4 発目が来たら連続打鍵ごとキャンセル**（改行・送信・hermes を出さず、入力欄のテキストは保持。§3.4 の CANCELLED）。あわせて**どのアプリでも動く既定設定 `config/chat-default.config.json`**（catch-all プロファイル）を追加。実機 E2E で判明した 2 件も同時に修正: **(a) キャンセルとクリップボード退避の成功は通知しない**（軽い操作にトーストは合わない。ログのみ）、**(b) `clipboard_select_copy` の後に `Right` を注入して選択を解除**（Ctrl+A の選択が残ると次の入力が上書きされる）、**(c) `GUITHREADINFO` を `win32lib` に一本化**（2 モジュールが別々に `GetGUIThreadInfo` の prototype を登録し、後勝ちで片方が `ArgumentError` になっていた＝実測で uia_focused 取得が毎回失敗していた） |
| v2-e | オペレータ注記「保留窓 250ms は人間の 2 連打に厳しい」「メモ帳で試せない」 | **既定 `hold_window_ms` を 250 → 350**（`triple_enter_window_ms` と範囲 120〜600 は不変）。根拠: 一般的な 2 連打間隔は 200〜350ms で、250ms は意識しないと成立しない。あわせて**メモ帳用の実機テスト設定 `config/notepad-qa.config.json`** を追加（未定義アプリは `pass`＝素通しのため、サンプルだけではメモ帳で何も起きない） |

---

## 0. 決定サマリー（1 画面で）

| # | 決定 | 結論 |
|---|---|---|
| 1 | 言語/ビルド | Python 3.14 + pywin32（Python 3.14 64bit）。ビルド工程なし（構文+import チェック）。追加パッケージ導入 **禁止**（トレイ=Shell_NotifyIcon、設定画面=tkinter） |
| 2 | プロセス構成 | **1 プロセス**（pythonw.exe）＋スレッド分離。通常権限のまま。管理者化しない |
| 3 | 純ロジック層 | `src/enter_router/core/`（標準ライブラリのみ）。WSL の `python3 -m unittest discover -s tests -v` で検証（pytest は未導入） |
| 4 | アーキテクチャ核 | **2 層マッピング**: 第1層 `global.operations`（操作→意味・全アプリ共通・ユーザーの癖）／第2層 `profiles[].keymap`（意味→実キー列・アプリ/サイトごと）。未定義アプリは全て `pass`（誤爆防止） |
| 5 | 既定マップ（第1層・確定） | `enter`→`newline`／`enter2`→`send`／`enter3`→`hermes`／`shift+enter`→`newline`／`ctrl+enter`→`send`／`alt+enter`→`pass`／`f1`〜`f12`→`none`。**全トリガをサンプルに明示記載** |
| 6 | 保留（hold） | 既定 `hold_mode: "deterministic"`＝**単発 Enter は必ず 250ms 保留**（`hold_window_ms` 既定 250・範囲 120〜600。**250ms はこの環境の実測値**＝v2-f / `docs/measurements.md`）。3 発目も 250ms 保留し、**4 発目が来たらキャンセル**（何も出さない）。`"fast"`＝即時送出・遅延ゼロ（代償は §3.8）。カウント対象の Enter は**1 個もアプリに届かない**（解決後のアクションのみ送出） |
| 7 | 順序保証 | 保留中に他のキー/マウス/フォーカス変更が来たら → **保留分を先にフラッシュ**してから当該イベントを通す（`あ`→Enter→`い` は `あ改行い` になる）。パニック・停止・例外時も**順序を保ってフラッシュ（無言破棄しない）** |
| 8 | injected | **`LLKHF_INJECTED` が立ったイベントは一切処理しない**（常に素通し。カウントもトリガもしない）。自己参照設定（`ctrl+enter→send→ctrl+enter`）の無限ループ防止。**削除禁止** |
| 9 | 設定 | JSON（`%APPDATA%\enter-router\config.json`、`schema_version: 1`、mtime ポーリングでホットリロード）。サンプル: `config/sample.config.json` |
| 10 | 送り先 | `ISink` 契約（§5）。clipboard / ai_chat / hermes の 3 実装が同一インターフェース |
| 11 | 安全 | パニック＝`panic_key`（既定 **F1**）の **`panic_repeat`(3) 連打**で即アンフック＋**保留分の順序付きフラッシュ**（v2-g。コードは緊急時に思い出せず片手で押せないため連打に変更）。前面アプリの登録キー `alt+f5`/`alt+f6`（設定可）＝**プロファイル追記**。例外全捕捉・fail-open・監視ドッグ（§6）。抑制キーの**二重送信（再送）は決してしない** |
| 12 | UI | トレイ（Shell_NotifyIcon）＋設定 1 ウィンドウ 3 タブ（§7）。トライアルは `--dry-run` |
| 13 | Hermes | `wsl.exe -d <distro> --exec /bin/bash -lc ...` 経由・`_HERMES_GATEWAY` 除去（削除禁止）・タイムアウト 30s（§5.3） |
| 14 | インクリメンタルサーチ | サイト側の逐次絞り込みは**抑止対象外**（§4.5）。v1 の緩和は L2 一括ペーストのみ。v1.5 候補「間接入力オーバーレイ」は設計評価のみ（実装しない） |

---

## 1. プロセス構成

**結論: 1 プロセス（`pythonw.exe`）＋ 4 スレッド。管理者権限は持たない。**

```
pythonw.exe enter-router（通常権限・単一プロセス・単一インスタンス Mutex: Local\enter-router）
 ├─ main スレッド      : Win32 メッセージループ + トレイ(Shell_NotifyIcon) + tkinter 設定画面
 ├─ hook スレッド      : SetWindowsHookExW(WH_KEYBOARD_LL) + 専用メッセージループ
 │                       コールバックは「キュー投入 / 同期フラッシュ(SendInput) / 計測 / return」のみ
 ├─ worker スレッド    : 前景キャッシュ更新(URL 取得)・解決実行・シンク実行(Hermes 最大30s)。
 │                       フックを絶対にブロックしない。キューは FIFO（順序保証の根幹）
 └─ watchdog スレッド : hook スレッドの heartbeat 監視 + 遅延コールバック連続計測
```

- **同期フラッシュの例外規定**: §3.5 の順序保証のため、保留中 Enter の確定配信（`key:`/`pass`/`none` 解決）だけは
  フックコールバック内で `SendInput` してから戻り値を返す。これは §6-2 の所要時間予算に**計上**する
  （SendInput の組は 1〜2 イベント・サブミリ秒目安。予算超過は同じ自己アンフック経路に流す）。
  シンク（capture 要）の場合はキュー投入のみで戻る（順序の例外は §3.5 に明記）。
- **前景情報はキャッシュ**: worker が EVENT_SYSTEM_FOREGROUND で process/title/URL を更新し続ける。
  フックコールバックはこのキャッシュを読む（µs）。URL は UIA で遅延しうる → 未更新なら
  `url=None` として扱う（URL 指定プロファイルはマッチしない＝安全側に倒す。§4.3）。

**なぜ 2 プロセスにしないのか**: UIPI は「通常権限プロセスが elevated へ注入する」ことを止めるが、
**elevated で動かす設計自体が最小コスト要件と管理者権限禁止に反する**。フックが OS に黙って解除される
事故（research §2-a-1）は、プロセス分離ではなく「コールバック時間の自己管理 + watchdog + fail-open」
（§6）で吸収する。**再考トリガー（観測可能）**: ログの `hook_slow_callback` が 1 週間で 2 回以上
「自動アンフック」に到達したら、フック部分だけを別バイナリ（Rust/C ヘルパー）に切り出す設計変更カードを作る。

**UIPI の扱い（受容を明記）**: 通常権限の常駐から elevated ウィンドウへのキー観測・注入は届かない／
黙って失敗する（research §2-a-2, R6）。ツールは elevated ウィンドウを**対象外**と宣言し、注入後は
読み戻し検証（§5.4）で失敗をユーザーに可視化する。

---

## 2. モジュール境界と依存図

```
                 ┌─────────────────────────────────────────────┐
                 │ core/  純ロジック層（標準ライブラリのみ）        │
                 │  events.py   KeyEvent / ForegroundInfo / ImeState
                 │  config.py   スキーマ・既定値・パース/検証        │
                 │  policy.py   Enter ポリシー状態機械（契約=§3）    │
                 │  routing.py  2層解決 resolve() + プロファイル照合  │
                 │              + シンク選択                       │
                 │  capture.py  取得チェーンの順序ポリシー           │
                 │  sinks.py    ISink 契約・SinkResult・失敗分類     │
                 └───────▲────────────────────────▲────────────┘
                         │ (型と純関数のみ import)    │
   ┌─────────────────────┴───────┐    ┌─────────────┴─────────────────┐
   │ platform/  Windows アダプタ   │    │ ui/  トレイ + 設定画面          │
   │  hook.py       LL フック      │    │  tkinter のみ / Shell_NotifyIcon│
   │  ime.py        ImmGetContext │    │  app.controller.AppController  │
   │  inject.py     SendInput 40B │    │ だけを叩く（platform を触らない）│
   │                S3 前景奪取    │    └─────────────▲─────────────────┘
   │  textcap_win.py UIA/選択コピー │                  │ AppController Protocol
   │  notify_win.py  バルーン通知   │    ┌─────────────┴─────────────────┐
   │  hermes_cli.py  wsl.exe spawn │◄───┤ app/  合成ルート                │
   └───────────────▲───────────────┘    │  main.py        エントリ/CLI    │
                   │                    │  controller.py  共有契約        │
                   └────────────────────┤  watchdog.py / config_watcher  │
                                        │  logsetup.py / fgcache(前景更新)│
                                        └────────────────────────────────┘
依存のルール:
 - core は platform / app / ui を一切 import しない（これが Linux テスト成立の根拠）。
 - ui は platform を import しない（トレイの win32 呼び出しは platform/notify_win 経由）。
 - tests/ は core のみを対象とする（platform は Windows 実機で QA カードが検証）。
 - policy.py は「解決済みアクション」しか見ない。第1層/第2層の解決は routing.py の純関数。
```

**解決レイヤの契約（routing.py・pure）**

```python
# 第1層: 操作キー → 意味（global.operations から。キーは正規化済み）
lookup_meaning(config, op_key: str) -> str | None      # None = 未定義トリガ(警告済み欠落)
# 第2層: 意味 → 解決済みアクション
resolve_meaning(config, profile: Profile | None, meaning: str) -> str
#   戻り値: "pass" | "none" | "key:<combo>" | "sink:<name>"
#   profile が None（未定義アプリ）→ 常に "pass"（誤爆防止・明示的でない pass はここにだけ存在）
#   meaning ∈ {send, newline} → profile.keymap で解決（未登録なら警告 + "pass"）
#   meaning ∈ {hermes, aichat, clipboard} → "sink:<name>"（マッチ済みプロファイルが存在する場合のみ）
#   meaning ∈ {pass, none} → 同名のまま
```

---

## 3. 操作解決と Enter ポリシー状態機械

### 3.1 用語（2 層マッピング・削除禁止）

| 層 | 何を決めるか | 単位 | 例 |
|---|---|---|---|
| 第1層 `global.operations` | ユーザーの**操作**が何を**意味**するか | **全アプリ共通**（ユーザーの癖） | `ctrl+enter` → `send`、`enter2` → `send`、`enter3` → `hermes` |
| 第2層 `profiles[].keymap` | その**意味**を、このアプリでは何を送れば実現できるか | **プロファイル（アプリ/サイト）ごと** | サイトA: `send`→`ctrl+enter`／サイトB: `send`→`enter`／Excel: `newline`→`alt+enter` |

- **操作キー（第1層のキー・JSON 表記）**: `"enter"`（Enter×1）, `"enter2"`（Enter×2 ※散文の `enter×2`）,
  `"enter3"`（Enter×3）, `"shift+enter"`, `"ctrl+enter"`, `"alt+enter"`, `"f1"`〜`"f12"`。
  修飾付き combo は `core/config.py::_norm_combo` の正規化（小文字・修飾子 ctrl→alt→shift→win 順）を使う。
  任意 combo 追加可（`ctrl+shift+enter` 等）。`win+…` は第2層の値としてのみ禁止（OS 予約）。
- **意味の語彙（第1層の値）**: `send` / `newline` / `hermes` / `aichat` / `clipboard` / `none` / `pass`。
  - `send`, `newline`: キー変換意味 → **必ず第2層で実キー列に解決**する（未定義アプリは pass）。
  - `hermes`, `aichat`, `clipboard`: シンク意味 → `sinks.hermes` / `sinks.ai_chat` / `sinks.clipboard` へ。
  - `pass`: 「アプリに素通し」— **明示的に選んだ場合のみ**有効。解決は「元の操作キー列をそのまま届ける」。
  - `none`: 「このトリガは未使用」— ツールは**無反応**（抑止しない・変換しない・元キーが素通し）。
    ※v1 で「抑止して何も送出しない」と定義していた `none` は、オペレータ指示の
    「`f1`〜`f12` → 未割当（`none`）」に整合させるためこの意味に**確定改訂**した。
    （`none` でキーを飲むことはしない。飲む必要が出た場合は将来値を追加する）
- **実キー列の語彙（第2層の値）**: `enter` / `shift+enter` / `ctrl+enter` / `alt+enter` / 任意 combo
  （`win+…` を除く。正規化は第1層と同じ）。

### 3.2 処理パイプライン（1 本道。Enter 連打・修飾付き Enter・F キーは同じ管を通す）

```
hook (platform)
  └─ KeyEvent(vk, kind, t_ms, mods, injected, os_repeat) + ImeState + 前景キャッシュ
       │
   ① rule0: injected == True  →  PASS（処理対象外。カウントしない・トリガしない・panic 判定もしない）
   ② rule1: panic key 成立    →  PANIC（§6-4）
   ③ rule2: IME 未確定中(composing=True) の Enter → PASS（IME と戦わない。カウントもリセット）
       │
   ④ 操作検出（policy.py 内・時刻は全部引数）
       ・Enter 連打: keyup を挟んだ down をカウント（窓: hold_window_ms / triple_enter_window_ms）
       ・修飾付き Enter / F キー等: down 時に combo 一致（第1層キーへ正規化）
       │
   ⑤ 第1層: meaning = global.operations[op]      （欠落 → 警告 + pass 相当。§4.3）
   ⑥ 第2層: resolved = resolve_meaning(cfg, profile, meaning)
            （profile = match_profile(fg)。None なら常に "pass"）
       │
   ⑦ 状態機械（§3.4）: HOLD/FLUSH/TRIGGER/DELIVER/PASS/IGNORE を決定
       │
   ⑧ 呼び出し側の実行:
       "key:<combo>" → 同期またはキュー経由で SendInput（組は 1 回だけ）
       "sink:<name>" → worker で capture → ISink.send（§5）
       "pass"/"none" → 確定している元 Enter をキュー投入分だけ SendInput、または素通し
```

- **解決は保留開始時に前倒し計算する**: `HOLDING` 入り（`enter` down）の時点で前景キャッシュから
  profile を引き、count 1/2/3 の `resolved` を worker で確定して保持する。フラッシュ時は
  **保留開始時点のプロファイルで解決した結果だけ**を使う（フォーカスが変わっても新しいウィンドウの
  キー列へ振り替えない＝送り先の取り違え防止）。
- **第2層の結果が自分自身の注入になる自己参照設定**（例 `ctrl+enter`→`send`、
  サイトの keymap `send`→`ctrl+enter`）は、注入イベントに `LLKHF_INJECTED` が立つため ①で素通しになり、
  無限ループしない（§0-8・削除禁止）。

### 3.3 意味の解決と保留（hold）の要否

- **`hold_mode: "deterministic"`（既定）**: `enter2`/`enter3` のうち **どちらかが実意味
  （`send`/`newline`/`hermes`/`aichat`/`clipboard`）である限り**、Enter は判定まで必ず保留される
  （既定マップは両方実意味 → **常時保留**。オペレータ受容済み・§3.8）。
  `pass`/`none` だけの count は保留しない（未割当と同じ扱い）。
  - 1 発目 down → HOLD（デッドライン = t + `hold_window_ms`）。
  - 2 発目 down（keyup 経由・窓内）→ 「より高い count(3) に実意味があるか」で判定:
    あり → **保留継続**（デッドライン = t + `triple_enter_window_ms`）／なし → その場で count=2 を解決＝TRIGGER。
  - 3 発目 down → count=3 を即 TRIGGER（これ以上拡張不能のため即時）。
  - デッドライン到達 → 保留中の Enter を **count の意味で解決してフラッシュ**（§3.5）。
- **`hold_mode: "fast"`**: 保留しない。各 down を**その場で** count の意味へ解決して即時送出。
  - ゼロ遅延。ただし 1 発目は既に届くため「3 連続」を押すと `send`（2 発目）と `hermes`（3 発目）が
    **両方発火しうる**。既定は `deterministic`。
  - **アイデンティティ短絡**: 解決結果が押下した combo と同一（例: サイトB の `newline`→`enter` ×
    素の Enter）なら、抑止も注入もせず **PASS のまま**（往復コスト 0）。
- **カウント中の Enter は 1 個もアプリに届かない（削除禁止）**: deterministic/fast とも、
  カウント対象の down は必ず抑止し、**解決後のアクションだけ**を送出する。
  素通しすると 2 発目の Enter がアプリ側の送信/生成ストップを誘発し、二重送信・意図しない停止になる
  （オペレータ指示 v2-c §3）。→ 不変条件 I3 のテストで証明する。

### 3.4 状態機械（遷移表。`core/policy.py` の docstring と同一）

状態: `IDLE / HOLDING / COOLDOWN / BUSY / CANCELLED / PANIC`。P = `policy.*`（全て設定可能）。

| 状態 | イベント | 次状態 | 出力 |
|---|---|---|---|
| IDLE | Enter down・deterministic で count2/3 に実意味なし（または fast） | IDLE | **保留せず即時解決**（count1 の意味で TRIGGER/DELIVER。fast ならアイデンティティ短絡で PASS 可） |
| IDLE | Enter down・deterministic で count2/3 に実意味あり | HOLDING | **HOLD**（抑止、デッドライン = t+`hold_window_ms`(250)） |
| HOLDING | Enter down（keyup 経由・窓内）・higher count に実意味なし | IDLE/COOLDOWN* | **TRIGGER**（count 解決・抑止のまま） |
| HOLDING | Enter down（2 発目）・higher count に実意味あり | HOLDING | **HOLD 継続**（デッドライン = t+`triple_enter_window_ms`(250)） |
| HOLDING | Enter down（3 発目） | HOLDING | **HOLD 継続**（同じ窓 250ms。ここで発火せず、**4 発目が来ないことを確認してから**デッドラインで DELIVER） |
| HOLDING | Enter down（**4 発目**・窓内） | **CANCELLED** | **CANCEL**: 連続打鍵ごと破棄。**改行も送信も hermes も出さない**（テキストは触らない）。**通知はしない**（v2-f: 軽い操作に毎回トーストは合わない）。ログの `decision=CANCEL` 行だけが記録 |
| CANCELLED | Enter down（窓内・さらに連打） | CANCELLED | IGNORE（飲み続ける。窓はスライド） |
| CANCELLED | デッドライン / 他のキー / 割り込み | IDLE | 何も出さない（他のキーはそのまま通る） |
| HOLDING | Enter repeat down（keyup なし連続） | HOLDING | IGNORE（抑止のまま） |
| HOLDING | **他のキー down / マウス押下 / フォーカス変更** | IDLE | **FLUSH（先に保留分を確定配信）→ その後このイベントを通す**（§3.5） |
| HOLDING | デッドライン到達 | IDLE | **DELIVER**（保留 count の解決で SendInput。回数は解決結果に従う） |
| HOLDING | IME 未確定開始を検知 | IDLE | DELIVER（ユーザーが先へ進んだ＝待たない） |
| HOLDING | 設定更新で無効化 | IDLE | DELIVER |
| 任意 | Enter down だが IME 未確定中（composing=True） | 変化なし | **PASS** + カウントリセット（IME と戦わない） |
| 任意 | IME 状態が不明（unknown = 照会失敗） | 変化なし | `global.ime_unknown_policy`: `"pass"`（既定）→ **PASS** / `"process"` → 通常の Enter 判定。**どちらでもログ `ime_state=unknown action=…` ＋ 初回のみ通知**（黙って何もしない状態は禁止, t_dd246e5a） |
| 任意 | **injected イベント（rule0）** | 変化なし | **PASS**（処理対象外・カウントしない・ループ防止） |
| 任意 | チャタ（同 vk down が `chatter_debounce_ms`(15) 未満・keyup なし） | 変化なし | IGNORE |
| 任意 | 修飾付き Enter / F キー等の第1層 combo 成立 | IDLE | **TRIGGER**（抑止 + 解決実行。`key:` は同期 SendInput 可） |
| 任意 | **パニックキー**の 1〜`panic_repeat-1` 発目（rule0 の後・他の判定より先） | 変化なし | PASS（キー自体は素通し。単発 F1 は通常の F1） |
| 任意 | **パニックキー**が `triple_enter_window_ms` 以内に `panic_repeat` 回 | PANIC | **PANIC**: 保留分を順序付きフラッシュ → 即アンフック → 通知+ログ（§6-4） |
| 任意 | **登録キー**（`registration.*`、rule0 の後） | 変化なし | 保留分を順序付きフラッシュ → **前面アプリのプロファイルを設定へ追記**（保存は検証付き・原子的）→ 通知 1 回。キーは抑止 |
| 任意 | ウォッチドッグ trip | PANIC | PANIC + 保留分フラッシュ（best effort, 50ms 予算）+ `os._exit(70)` |
| BUSY | Enter down / 第1層 combo | BUSY | count は即 count1 意味で解決（保留しない）。シンク系 combo は**ドロップ+「処理中」通知+ログ**、`key:` 系はそのまま解決 |
| BUSY | シンク完了 (on_sink_done) | COOLDOWN | — |
| COOLDOWN | Enter down（`trigger_cooldown_ms`(500) まで） | COOLDOWN | count は即 count1 意味で解決（保留・連打判定しない） |
| COOLDOWN | デッドライン | IDLE | — |

\* TRIGGER が `sink:` でシンクが実際に走った場合のみ BUSY に入る。`key:`/`pass`/`none` は BUSY に入らない。

**コールバック API（frozen・実装は t_57577e39）**

```python
EnterPolicyMachine(policy: PolicyConfig, resolver: Callable[[str], str])
  on_key(ev, ime=None)        -> Outcome   # 全キーイベント（rule0 は最前）
  on_deadline(t_ms)           -> Outcome   # hold/triple/cooldown デッドライン発火
  on_interrupt(kind, t_ms)     -> Outcome  # kind ∈ {"key","mouse","foreground"} → FLUSH
  on_sink_done(t_ms)          -> Outcome
  on_panic(t_ms)              -> Outcome
  reconfigure(policy, resolver) -> Outcome
  state / deadline_ms / pending_count / pending_resolved
```

- `resolver(op_key) -> resolved` は routing.resolve の束縛。テストは偽 resolver を差し込む。
- `Outcome(decision, state, swallow, action, resolved, count, reason)`。
  `decision ∈ {PASS, HOLD, IGNORE, TRIGGER, CANCEL, FLUSH, DELIVER, PANIC}`。
  - `CANCEL`: `swallow=True` で**何も配信しない**（4 連打以上の取り消し）。**通知しない**（ログのみ）。
  - `FLUSH`: `swallow=False`（フックは**同期フラッシュ後に**このイベントを素通し）。
  - `DELIVER`/`PANIC`: `count` = 送出する解決済み結果の本数・`resolved` が実体。

### 3.5 保留・フラッシュ・順序保証（削除禁止）

1. **保留した Enter は必ず確定配信する。** 消える経路はない:
   - 窓の経過（デッドライン）→ FLUSH/DELIVER。
   - 他のキー入力・マウス押下 → **保留分を先に注入してから**そのイベントを通す。
     `あ`→Enter→`い` は必ず `あ改行い` になる（`あい改行` になってはならない）。
     実装: フックコールバック内で解決済み `key:`/`pass`/`none` を **同期 SendInput** してから戻り値を返す。
   - フォーカス変更（他プロセス起因・イベントのみ）→ 保留開始時点のプロファイルで解決した結果を、
     **元ウィンドウを AttachThreadInput 手順で一度前面化して注入 → 元の前景へ戻す**（best effort 1 回）。
     これが失敗した場合は**新しいウィンドウへは絶対に注入せず**、通知+ログ（`pending_flush_missed`）で可視化。
   - IME 未確定開始・設定無効化・パニック・停止・例外 → 順序付きフラッシュしてから遷移（無言破棄しない）。
2. **フラッシュは「再送」ではない**: 抑止したキーの**二重送信（過去分の再生・リトライ）は決してしない**。
   1 個の保留は (a) ちょうど 1 回配信、(b) TRIGGER で消費、のどちらか。I1/I2 を参照。
3. **シンク解決のフラッシュ例外**: 保留 count の解決が `sink:` の場合、capture+send は worker で
   行うため順序保証はキー経路より弱い（capture 前に後続キーが到着しうる）。既定マップでは
   count1=`newline` / count2=`send` のため**保留中の解決は常に `key:`（順序保証は厳密）**。
   `enter`/`enter2` をシンクに割り当てる設定を行ったユーザーにはこの例外を README に明記する。
4. **解決の前倒し**: §3.2⑧ の通り、保留開始時点で profile と count 1/2/3 の解決を確定済みにする。
   フラッシュ時にフォーカスが変わっても、**新しいウィンドウの keymap で解決し直さない**。

### 3.6 数値パラメータ（全て `policy.*` として設定可能・確定値）

| 項目 | キー | 既定 | 範囲 | 根拠 |
|---|---|---|---|---|
| 保留モード | `hold_mode` | `"deterministic"` | `deterministic` / `fast` | 常時保留（deterministic）を既定。fast は遅延ゼロの明示オプトイン |
| 保留窓（1→2 判定＝単発 Enter の最大遅延） | `hold_window_ms` | **250** | 120〜600 | **実測**（v2-f）: 連続 24 サンプル 96〜201ms の最遅 201 × 1.25 = 251 → 250。非連続 5 サンプル 495〜585ms の最速 495 × 0.8 = 396 を十分下回る（`docs/measurements.md`） |
| トリプル窓（2→3 判定・**3→4 のキャンセル判定**も兼ねる） | `triple_enter_window_ms` | **250** | 120〜600 | 実測の 2→3 は 112〜180ms。3 発目をこの窓だけ保留すると 4 連打を確実に取り消せる（コストは count3 の発火が 250ms 遅れることだけ。§3.4/§3.8） |
| チャタ除去 | `chatter_debounce_ms` | 15 | 0〜200 | 同 vk down の連続ノイズ |
| トリガ後クールダウン | `trigger_cooldown_ms` | 500 | 0〜5000 | 二重発火の抑止 |
| パニックキー | `panic_key` | **`f1`** | 任意のコンボ | 判定は rule0 の次に最優先。**連打で停止**（v2-g） |
| パニック連打の回数 | `panic_repeat` | **3** | 2〜6 | 連打キー `panic_key` を窓内に何回で停止するか。窓は `triple_enter_window_ms` |
| 登録キー（プロセスのみ） | `registration.process_key` | `alt+f5` | `alt+F1`〜`alt+F24` | 前面プロセスのプロファイルを追記（v2-g） |
| 登録キー（プロセス＋タイトル） | `registration.process_title_key` | `alt+f6` | `alt+F1`〜`alt+F24` | 前面ウィンドウのタイトルも条件にしたプロファイルを追記 |
| （安全）各閾値 | `safety.*` | §4.2 の通り | — | OS の LL フック解除上限 1000ms に収める |

キーリピート: keyup を挟まない連続 Enter down は 1 扱い（OS リピート設定に非依存。research §2-d）。

### 3.7 不変条件（実装カードのテストで必ず assert すること）

- I1 抑止した Enter は **ちょうど 1 回**（配信 or 消費）。**二重配信・再送は決してしない。**
- I2 同時に保持できる保留 Enter は **最大 3 個**（3 連打の 1・2・3 発目）。**4 発目は保持せずキャンセル**（CANCELLED: 飲むだけで何も出さない）。保持数が 4 に増える経路は無い。
- I3 **カウント対象になった Enter は 1 個もアプリに届かない**（届くのは解決後のアクションだけ。
  v2-c §3・このテストが無いと設計崩壊）。
- I4 `injected` イベントは**一切処理しない**（rule0。カウント外・トリガ外・panic 外）。
- I5 IME 未確定中の Enter は必ず PASS（例外はここだけ。§3.4 参照）。
- I6 FLUSH 後のイベント順序: `FLUSH(inject) → 次イベント PASS` の順が保たれる（あ→Enter→い = あ改行い）。
- I7 PANIC/停止/例外時は**順序付きフラッシュを先に行い**、その後にアンフック/退出する（無言破棄しない）。
- I8 未定義アプリ（match_profile が None）では、どの操作でも解決は `pass`（誤爆しない）。

### 3.8 レイテンシのトレードオフ（明記・必読）

- 既定（deterministic + 既定マップ）では **どの単発 Enter も最大 `hold_window_ms`(250) 遅れて届く**。
  3 連打の解決（送信/hermes）はさらに `triple_enter_window_ms`(250) 待つ（4 連打の取り消しのため）。
  これは v2-c でユーザーが受容した帰結（既定が ×2/×3 に意味を持つため判定必須）。
  **逃げ道（設定 1 行）**: ① `policy.hold_mode = "fast"`（遅延ゼロ・3連の二重発火リスクあり）、
  ② `global.operations.enter2/enter3` を `pass` に（保留そのものが消える。明示的な選択なので許容）。
- プロファイル未定義のアプリでも保留は発生する（解決は pass なので**機能的な変化は無く遅延だけ**）。
  README の起動手順にこの注意を書く。
- 保留中に他キーが来たら同期フラッシュするため、**打鍵の体感順序は保たれる**（I6）。

### 3.9 `--dry-run` の観測ログ書式（QA 契約・実装カード必須）

`--dry-run` は**判定だけを観測するモード**（SendInput・シンク・capture を一切実行しない。フックを張らず、
または張っても抑止＝ログのみ）。判定 1 件ごとに 1 行、**同一フォーマット**で出す:

```
DRYRUN t=<t_ms> op=<操作キー|-> meaning=<意味|-> profile=<profile_id|-> resolved=<pass|none|key:<combo>|sink:<name>> decision=<PASS|HOLD|IGNORE|TRIGGER|FLUSH|DELIVER|PANIC> swallow=<0|1> reason=<code>
```

- ログ末尾に 30 分間などのサマリ 1 行を必ず出す:
  `DRYRUN-SUMMARY handled=<n> swallowed=<抑止したキー数> injected_ignored=<rule0 で無視した注入イベント数> unresolved=<意味欠落数>`
- QA はこの 3 点で照合する: ①`resolved` の実キー列が §4.7 の例と一致、
  ②`swallowed` が「想定外抑止 0 件」（§9 S1 の観測トリガー）、
  ③`injected_ignored > 0`（自己参照設定で注入が再トリガされず 1 回のみ＝§6-6 / I4）。
- 通常モードでも `DRYRUN` 以外の決定ログは同じキー名（op/meaning/resolved/decision）を使い、
  **打鍵文字列・取得テキストは書かない**（§6-7）。

---

## 4. 設定スキーマ

### 4.1 形式と所在

- **JSON**（`schema_version: 1`）。標準ライブラリ `json`・追加依存ゼロ（YAML/TOML 不採用）。
- 所在（優先順）: `--config PATH` → `%APPDATA%\enter-router\config.json`（初回に既定値生成）→ 組み込み既定値。
- **ホットリロード**: mtime/サイズを 1s ポーリング（`app/config_watcher.py`）。成功時のみ Atomic 差し替え +
  `policy.reconfigure()`。**検証失敗時は旧設定のまま**、エラーをログ+トレイ通知。
- バージョニング: `schema_version` 不一致は拒否（旧設定維持）。
- v1 の `enter_action` / `hotkeys` は **`global.operations` + `profiles[].keymap` に置換済み**（v2-b）。

### 4.2 スキーマ（実体 = `config/sample.config.json`。**このブロックと byte 単位で一致させる**。
全トリガを明示列挙する（暗黙既定に頼らない＝オペレータ指示 v2-d §3）

```json
{
  "schema_version": 1,
  "global": {
    "operations": {
      "enter": "newline",
      "enter2": "send",
      "enter3": "hermes",
      "shift+enter": "newline",
      "ctrl+enter": "send",
      "alt+enter": "pass",
      "f1": "none",
      "f2": "none",
      "f3": "none",
      "f4": "none",
      "f5": "none",
      "f6": "none",
      "f7": "none",
      "f8": "none",
      "f9": "none",
      "f10": "none",
      "f11": "none",
      "f12": "none"
    },
    "ime_unknown_policy": "pass"
  },
  "policy": {
    "enabled": true,
    "hold_mode": "deterministic",
    "hold_window_ms": 250,
    "triple_enter_window_ms": 250,
    "chatter_debounce_ms": 15,
    "trigger_cooldown_ms": 500,
    "panic_key": "f1",
    "panic_repeat": 3
  },
  "registration": {
    "process_key": "alt+f5",
    "process_title_key": "alt+f6"
  },
  "safety": {
    "callback_budget_warn_ms": 5,
    "callback_overbudget_ms": 50,
    "callback_overbudget_streak": 5,
    "callback_hard_limit_ms": 250,
    "watchdog_interval_ms": 1000,
    "watchdog_stale_ms": 3000
  },
  "capture": {
    "chain": [
      "uia_focused",
      "clipboard_select_copy"
    ],
    "clipboard_restore": true,
    "preview_chars": 60
  },
  "sinks": {
    "clipboard": {
      "type": "clipboard",
      "enabled": true
    },
    "ai_chat": {
      "type": "ai_chat",
      "enabled": true,
      "method": "paste",
      "send_after_insert": false
    },
    "hermes": {
      "type": "hermes",
      "enabled": true,
      "wsl_distro": "",
      "shell": "/bin/bash",
      "timeout_sec": 30,
      "profile": null,
      "query_file_stdin": false
    }
  },
  "profiles": [
    {
      "id": "site-a",
      "enabled": true,
      "match": {
        "process": "firefox.exe",
        "url": "*site-a.example.com*"
      },
      "keymap": {
        "send": "ctrl+enter",
        "newline": "shift+enter"
      }
    },
    {
      "id": "site-b",
      "enabled": true,
      "match": {
        "process": "chrome.exe",
        "url": "*site-b.example.com*"
      },
      "keymap": {
        "send": "enter",
        "newline": "shift+enter"
      }
    },
    {
      "id": "excel",
      "enabled": false,
      "match": {
        "process": "excel.exe"
      },
      "keymap": {
        "send": "enter",
        "newline": "alt+enter"
      }
    }
  ],
  "logging": {
    "level": "INFO",
    "dir": null,
    "max_bytes": 1048576,
    "backup_count": 3
  }
}
```

### 4.3 意味論（検証済み・core/config.py + core/routing.py が正）

- **第1層の必須トリガ**: `enter`, `enter2`, `enter3`, `shift+enter`, `ctrl+enter`。
  欠けている場合、**起動時に 1 つの警告へ不足分を列挙**して続行する（黙って既定で埋めない）。
  欠落したトリガの動作は `pass`（安全側。ユーザーが必ず見えるように警告に出す）。
  `alt+enter` と `f1`〜`f12` は任意（サンプルは全件明示）。
- **第1層の値検証**: 語彙外（例 `transform:…`, `sink:…`, 空文字）は **ConfigError**（読み込み単位で拒否）。
- **第2層（keymap）検証**:
  - キーは `send` / `newline` のみ。それ以外の意味を keymap に書いたら ConfigError。
  - 値は `_norm_combo` 正規化。**`win+…` は ConfigError**（OS 予約）。
  - `send`/`newline` のどちらか欠落する keymap → **警告にプロファイル名と不足意味を列挙** +
    当該意味は `pass` にフォールバック（黙って埋めない）。
- **登録キー（v2-g, `registration` セクション）**:
  - `process_key` / `process_title_key` は `alt+<F1..F24>` のみ（`_norm_combo` 正規化後に検証）。
    それ以外は **ConfigError**（無修飾 F キーは Enter ポリシーの持ち物、`ctrl+…` は他アプリと衝突）。
    2 つは同一値を許さない。
  - 押すと**その瞬間の前面アプリ**を `profiles` へ**追記**し、`save_config_dict`（検証 → 原子的書き込み →
    即時適用）で反映する。`id` は exe 名から作り、衝突時は `-2`, `-3`。keymap は `send=enter` /
    `newline=shift+enter`（チャットの慣習。後から設定画面で編集可）。
  - `process_key` は `match.process` のみ。`process_title_key` は `match.title` も入れる。
    タイトルは**データであってパターンではない**ので `routing.literal_glob()` でエスケープする
    （`Report [draft]` が `Report d` に一致してしまう事故を防ぐ）。
  - 重複判定は「同じ exe・同じ title 条件・url 条件なし」の 3 点一致。
    （`site-a`（firefox + URL）は「firefox 全体」の登録を妨げない。）
  - 保留中の Enter は**登録より先に**フラッシュする（I6）。登録キー自体は抑止する。
  - 失敗（前面プロセス/タイトルが読めない・保存が検証で落ちた）は**通知 + ログ**で可視化する。
- **照合（既存の 2 段走査を維持）**: ①specific（process≠`*` または title/url 指定）を書いた順、
  ②catch-all（`process:"*"` かつ title/url なし）を最後の受け皿。無効プロファイルは除外。
  **match_profile が None（どのプロファイルにも当たらない）＝未定義アプリ → 全操作 `pass`**（I8・誤爆防止）。
  catch-all プロファイルは「全アプリが定義済みになる」ため**推奨しない**（サンプルには含めない）。
- **`pass` と `none` の確定意味**（v2-d で改訂）:
  - `pass` = 明示的な「アプリに素通し」。元の操作キー列（Enter n 個 / 元の combo）をそのまま届ける。
  - `none` = トリガ未使用。ツールは無反応（抑止しない・変換しない・元キーが届く）。
  - どちらも count 判定に使う場合は §3.3 の通り（`pass`/`none` だけの count は保留しない）。
- **シンク意味の発火条件**: `hermes`/`aichat`/`clipboard` は **match_profile がプロファイルを
  返したアプリでのみ**発火する（未定義アプリでは pass）。＝「プロファイルを作ったアプリに入み込む」ことが明示的なオプトイン。
- **URL の扱い**: `match.url` 指定プロファイルは URL が読み取れないときに**マッチしない**（推測しない）。
  前景キャッシュの URL が古い場合は安全側（不マッチ）に倒る。
- **`global.ime_unknown_policy`**（t_dd246e5a）: IME 未確定状態を **3 値**（composing / not_composing /
  unknown）で返し、`unknown`（照会失敗）のときの挙動を設定で選ぶ。`"pass"`（既定）は素通し、
  `"process"` は「未確定でない」とみなして通常の Enter 判定を走らせる。**どちらでもログ
  `ime_state=unknown action=<pass|process> source=… occurrences=N detail=<API と Win32 エラー>` を出し、
  初回のみ通知する**（＝「黙って何もしない」を禁止）。連続時はログを 1 分に 1 回へ間引く。
  `policy` セクション側に書いた場合は無視して既定 `pass` を使い、**警告を出す**（黙って無視しない）。
  プローブは「読めなかった」と「未確定でない」を必ず区別する（前者だけが unknown）。
  照会は **4 段**で、IMM が他プロセスでは原理的に読めないことを実測で確認したうえで 4 段目に
  TSF の窓走査を置く（t_dd246e5a 実測）:
  ① `AttachThreadInput` + `ImmGetContext(hwnd)`（前面窓） ② 同 `ImmGetDefaultIMEWnd`（IME 窓）
  ③ `GetGUIThreadInfo` の `hwndFocus`/`hwndCaret`（各 1 回の attach 内で読む）
  ④ **`EnumThreadWindows` で対象スレッド（前面窓 + `hwndFocus`/`hwndCaret` のスレッド）の窓クラスを走査**し、
  `MSCTFIME Composition` が 1 個でもあれば **composing**（非表示でも数える＝陽性のみを許す）、
  無くて同スレッドに `MSCTFIME UI`（アンカー）があれば **not_composing**、どちらも無ければ **unknown**。
  アンカーが無いアプリ（TSF ネイティブ実装・他社 IME）は**不在を not_composing に読み替えない**
  ＝IME の確定 Enter を壊さない。実測（Win11 + MS-IME、別プロセスの EDIT）: 初期 = `MSCTFIME UI`
  のみで合成窓 0、変換中 = 合成窓 4（うち 1 個 visible）、確定後 = 合成窓 0。プローブ 1 回 0.10ms。
  前面ウィンドウが変わった時だけ経路（`GetGUIThreadInfo`/`ImmGetDefaultIMEWnd`/当たった候補/
  走査対象 tid）を再探索し、**合成状態そのものは毎回読み直す**（打鍵中に変化するため。答えを
  キャッシュすると誤判定）。IMM 3 段が全滅した窓は `_imm_dead` として以後 IMM を試さない
  （窓が変われば解除、unknown の再試行窓 2 秒経過で全経路再試行）。全段失敗したウィンドウは
  2 秒だけ unknown を記憶し、その間は Win32 呼び出しをしない。
- 既定値: 全フィールドに既定値があり、`global` 欠落時は組み込み既定マップを適用し**警告を 1 件出す**。
  初回生成ファイル（DEFAULT_CONFIG）は §4.2 と同内容＝全トリガ明示・警告ゼロ。
- **`hotkeys[]` は存在しない**: F キーや任意 combo は第1層 `global.operations` に直接書く
  （第1層は全アプリ共通。アプリ別の差は第2層 keymap で吸収する）。

### 4.4 既定値の設計方針

- **既定マップ（v2-c/v2-d 確定）**: 常に「Enter=改行、2 連続=送信、3 連続=Hermes」と覚えられる。
  - `enter`→`newline` / `enter2`→`send` / `enter3`→`hermes`
  - `shift+enter`→`newline` / `ctrl+enter`→`send`（**pass にしない**＝アプリ任せの非一貫性を排除）
  - `alt+enter`→`pass`（明示的に「アプリ素通し」を選べる値として pass を残す）
  - `f1`〜`f12`→`none`（未使用＝ツール無反応。**F キーは抑止されず通常どおりアプリに届く**）
- **プロファイル既定**: サンプルは site-a / site-b（有効・ドメインはサンプルなので実運用で書き換える）と
  excel（**既定無効**。Excel の既定挙動を変えうるため明示的にオフ）。**catch-all は同梱しない**。
- **遅延について**: §3.8 の通り既定では単発 Enter が 250ms 保留される。逃げ道 2 種を README に明記。
- ユーザーが後から変えるのは第1層の値（癖）と各プロファイルの keymap（サイトの流儀）だけでよい。
  **設定ファイルだけで全トリガ・全意味を任意に変更可能**（受入条件）。

### 4.5 インクリメンタルサーチ要件の扱い（明記必須）

要件の実体は「サイトの入力欄に日本語を打つと候補/絞り込みが勝手に動く」＝サイト側 JS が `input` を
購読するケース。**v1 では抑止できない**（キーを飲むと日本語入力自体が成立しない。
research.md §2-c の L1/L3/L4 と同根）。**「v1 で抑止した」と書かない。**
v1 の緩和は **L2 注入の一括化のみ**（`AiChatSink` の `method:"paste"` 既定）。
**v1.5 候補（設計評価のみ・実装しない）: 間接入力オーバーレイ** — tkinter の小さな入力ウィンドウに
打たせ、確定時に 1 回だけペースト。サイト側に逐次絞り込みが原理的に起きない唯一の本命緩和。
tkinter のみで実装可。コスト: ①入力のたびにフォーカス移動で体験が重い、②IME 確定 Enter の扱いを自前定義、
③工数概算 2〜3 日。採否は v1 実機 QA 後。

### 4.6 骨組みとの既知差分（**適用は実装カード t_57577e39 の最初のコミット**・検証必須）

初期コミット（14de979）のコードは v1 スキーマのまま。v2 の契約に合わせて以下を適用し、
**`python3 -m unittest discover -s tests -v` を緑にしてからコミットする**こと。

| ファイル | 変更（契約） |
|---|---|
| `core/config.py` | ①トップレベル `global`（`operations` dict）を既知キーに追加・必須トリガ欠落は warnings に列挙（拒否しない）②値検証: 意味語彙 {send,newline,hermes,aichat,clipboard,none,pass} 以外は ConfigError ③`Profile`: `enter_action`/`hotkeys` を廃止し `keymap: dict[str,str]`（キーは send/newline のみ・値は `_norm_combo`・`win+` 拒否・欠落は warnings+pass フォールバック）④`PolicyConfig`: `double_enter_window_ms` を **`hold_window_ms`(250, 120..600)** に置換、**`hold_mode: str = "deterministic"`** 追加、`triple_enter_window_ms` を (250, 120..600) に（※既定値は v2-f で実測 250。許容範囲 120..600 は不変）⑤`DEFAULT_OPERATIONS` = §4.2 と同一、`DEFAULT_CONFIG` = §4.2 と同一、catch-all 既定プロファイル廃止 |
| `core/routing.py` | `lookup_meaning` / `resolve_meaning`（§2 の契約）を追加。`match_profile`・`select_sink` は維持 |
| `config/sample.config.json` | §4.2 の JSON と **byte 単位で一致**させる |
| `tests/test_smoke.py` | 下記の必須テストへ更新（既存の enter_action 系アサーションは keymap 系へ置換） |
| `README.md`, `docs/design.md` | 設計カードが更新済み（触らない） |

**必須テスト（この構成で緑であること＝受入条件）**
1. サンプルが parse + validate 通る。`warnings == ()`。第1層 18 トリガすべて §4.2 の値であること。
2. サンプルから `shift+enter` を消す → 起動は成功し **warnings が `shift+enter` を列挙**する。
3. 意味語彙外（`transform:ctrl+enter` 等）→ ConfigError。keymap に `win+enter` → ConfigError。
   keymap に `send` 以外の意味 → ConfigError。combo 正規化（`Shift + Ctrl + F10` → `ctrl+shift+f10`）。
4. 2 層解決のウォークスルー（§4.7 の 4 例と **byte 一致の期待値**）:
   site-a / site-b / excel（excel は enabled を True に差し替えて照合）/ 未定義アプリ。
5. 既存テストのうち capture / select_sink / schema 拒否系は維持（profile 照合系は keymap 前提へ更新）。
6. （v2 以前から維持）`_HERMES_GATEWAY` の env 除去・`env_remove` 既定のテスト。

> 状態機械（`policy.py` の遷移ロジック本体と I1〜I8 のテスト）は **t_57577e39 の担当**。
> 本カードの骨組みは `policy.py` を契約（docstring + frozen API）止める。

### 4.7 具体例 3 つ（設定サンプルと実動作が一致することの証明）

共通の第1層（§4.2）: `enter`→`newline`, `enter2`→`send`, `enter3`→`hermes`,
`shift+enter`→`newline`, `ctrl+enter`→`send`, `alt+enter`→`pass`, `f*`→`none`。
保留 = deterministic / 250ms（実測・v2-f）。

**例 1: サイトA（`send`→`ctrl+enter`, `newline`→`shift+enter`。Enter=送信系のサイト）**
| 操作 | 意味(第1層) | 解決(第2層) | アプリに届くもの |
|---|---|---|---|
| Enter×1 | newline | `key:shift+enter` | 250ms 後に Shift+Enter（改行）1 回。素の Enter は届かない |
| Enter×2 | send | `key:ctrl+enter` | 保留を消費して Ctrl+Enter 1 回（送信）。**2 発の Enter は届かない** |
| Enter×3 | hermes | `sink:hermes` | Hermes へ（テキスト取得→送信）。Enter は届かない |
| Ctrl+Enter | send | `key:ctrl+enter` | 押下元を抑止し Ctrl+Enter を注入（同一だが経路は一貫） |
| Shift+Enter | newline | `key:shift+enter` | 抑止して Shift+Enter を注入 |

**例 2: サイトB（`send`→`enter`, `newline`→`shift+enter`。Enter=送信・Shift+Enter=改行のチャット）**
| 操作 | 意味 | 解決 | 届くもの |
|---|---|---|---|
| Enter×1 | newline | `key:shift+enter` | 250ms 後に Shift+Enter（このサイトの改行） |
| Enter×2 | send | `key:enter` | Enter 1 回（このサイトの送信）。カウント分の Enter は届かない |
| Ctrl+Enter | send | `key:enter` | **ユーザーの癖（Ctrl+Enter=送信）がこのサイトの送信キーに変換される** |
| Shift+Enter | newline | `key:shift+enter` | 同一キーへ変換（経路はツール管理） |

**例 3: Excel（`send`→`enter`, `newline`→`alt+enter`。既定無効・有効化した場合）**
| 操作 | 意味 | 解決 | 届くもの |
|---|---|---|---|
| Enter×1 | newline | `key:alt+enter` | 250ms 後に Alt+Enter（セル内改行） |
| Enter×2 | send | `key:enter` | Enter 1 回（セル確定/一括入力） |
| Ctrl+Enter | send | `key:enter` | ユーザーの「送信」癖 → Excel の Enter へ |

**例 4: 未定義アプリ（match_profile が None。例: メモ帳・ターミナル）**
| 操作 | 解決 | 届くもの |
|---|---|---|
| 任意（Enter×1, Ctrl+Enter, F5…） | `pass` | 元のキーがそのまま。**誤爆しない**（I8） |
| Enter×1（deterministic のため保留のみ） | `pass` | 250ms 後に素の Enter（遅延のみ・機能変化なし） |

---

## 5. 送り先アダプタ境界（ISink）

### 5.1 契約（`core/sinks.py` が正。3 シンクは同一インターフェースで差し替え可能）

```python
class ISink(Protocol):
    name: str
    def can_accept(self, ctx: SinkContext) -> bool: ...   # 事前確認。例外を出さない。迷ったら False
    def send(self, text: str, ctx: SinkContext) -> SinkResult: ...  # ワーカースレッドのみ。例外を出さない
    def verify(self) -> VerifyResult: ...                 # 設定画面「テスト送信」用の到達性確認
```

- `SinkContext(trigger, foreground, captured_text)`（`trigger` には `op`/`meaning`/`profile_id` を載せる）。
- `SinkResult(sink, status, failure, message, data)`。失敗分類 `FailureKind`
  （`not_acceptable / capture_failed / injected_blocked / focus_failed / clipboard_busy /
  cli_spawn_failed / model_connection_failed / timeout / empty_response / internal_error`）。
- **`key:` 解決にはシンクも capture も使わない**（注入のみ）。capture は `sink:` 解決のときだけ §5.4 を通る。

### 5.2 3 実装の挙動

| シンク | send の実体 | 主な失敗 |
|---|---|---|
| `ClipboardSink` | クリップボードへ退避（**成功時は無通知**。v2-f: 軽い操作なのでトーストしない。失敗はルータ側が通知） | clipboard_busy |
| `AiChatSink` | 前景維持(S3) → method=paste: 退避+Ctrl+V+復元（**1 イベント**・L2 一括化）。復元は**貼付先が貼付を処理した後**（`PASTE_SETTLE_S`=350ms 待ってから戻す。即時復元だと SendInput が積んだだけの段階で旧クリップボードに戻り、Chrome は旧値を貼る＝QA-B 項目5 の実測）／method=sendinput。`send_after_insert=false` 既定 | focus_failed / injected_blocked(UIPI) |
| `HermesSink` | §5.3 | 3 分類 |

### 5.3 HermesSink 固定仕様（research §2-f / R14 の実測に一致・逸脱禁止）

- 主系: `~/.local/bin/hermes -z "<text>"`（**WSL 内は wsl.exe を介さず直接**、Windows からは
  `wsl.exe -d <distro> --exec /bin/bash -c 'exec ~/.local/bin/hermes -z "$1"' erouter <prompt>` に `$1` で渡す）。
  **`--exec` 固定**（旧 `--` 区切りだと interop 側が末尾をシェル評価するため、`$1` が空に展開され
  プロンプトが渡らない。さらにコマンドライン上のプロンプト内の `$`/バッククォートが実行される＝
  実測: `$(id)` が実行された。`--exec` は argv をそのまま渡すので「シェルに解釈させない」が成立）。
  **実行ファイルは絶対パス固定**（t_4674d88c。`hermes` のコマンド名呼びは PATH 依存で、
  `exec: hermes: not found` = exit 127 で既定設定が必ず失敗した。`command -v` 解決も設定項目も作らない）。
  絶対パスなのでシェルは非ログイン `-c` でよい（`-lc` は PATH 用だった）。
  起動不能（exit 126/127・`not found`）は `model_connection_failed` ではなく `cli_spawn_failed` に分類する。
- 長文（>2000 字）: stdin 形式 `wsl.exe -d <distro> --exec /bin/bash -c 'exec ~/.local/bin/hermes chat --query-file -'`。
  ARGV 形式が `empty_response` を返した場合もこの形式で 1 回だけ再試行する。
  （t_92c498ed でカード本文 §3 の呼び出し仕様に合わせ、主系を `-z` に入れ替え。
  `query_file_stdin` は「常に stdin 形式を強制する」フラグに降格し既定 false。）
- `-p <profile>` は**グローバル**オプション（`chat` に `--profile` は存在しない）。
- **子プロセス env は作り直し `_HERMES_` 前頭一致をすべて除去**（必須・削除禁止）。
- 成功判定 `exit==0 && stdout 非空`、タイムアウト既定 **30s**（実測 3.01〜4.44s）。
- 失敗 3 分類 (a) `cli_spawn_failed` (b) `model_connection_failed` (c) `timeout`。いずれも
  **プロンプト本文をクリップボード退避 + 通知 + ログ**（送ったつもりにしない）。
- v1 は結果をクリップボード+通知で返す。**v2 拡張点**: stdout/stderr から `local-[0-9a-f]{64}` を
  観測できたら `SinkResult.data["session_id"]` に格納（取れなければ「取れなかった」旨をログ）。
  **ISink は v2 でも壊さない。**

### 5.4 テキスト取得（capture chain）

トリガ成立時のみ、そのアプリに入っているテキストを取得する:
1. `uia_focused`: フォーカス要素の UIA 読み取り（ネイティブ Edit 等）。
2. `clipboard_select_copy`: Ctrl+A → Ctrl+C をスタッシュに取り、**必ずクリップボードを復元**し、
   最後に **`Right` を 1 回注入して選択を解除**する（Ctrl+A の選択が残ると次の入力が上書きされる。v2-f の実機指摘）。
   Firefox は UIA で入力欄を特定できない（research R11）ため、この経路が現実解。
3. どちらも失敗 → **トリガした Enter は破棄（再送なし）** + 通知 + ログ（黙って失敗しない）。

**フォールバック（全シンク共通）**: `send()` 失敗時は `ClipboardSink`（元テキストの退避）へ自動切替 +
通知 + ログ。Hermes の場合はプロンプト本文を退避し、ユーザーの入力を絶対に失わない。

---

## 6. 安全要件（削除禁止・実装カード必須）

1. **フック内例外の完全捕捉**: コールバック最外で `except BaseException` → **保留分の順序付きフラッシュ
   （可能なら）→ 素通し** + エラーログ。例外が漏れると OS がフックを解除する。
2. **コールバック所要時間**: 本体は「キュー投入（上限 256 件、超過は drop-oldest + カウンタ）／
   同期フラッシュ（§1 の例外規定）／計測 / return」のみ。閾値は設定値:
   `callback_budget_warn_ms=5`（WARN）、`callback_overbudget_ms=50` が **5 連続** or 単発
   `callback_hard_limit_ms=250` → **自己アンフック**（OS 上限は `LowLevelHooksTimeout` 最大 1000ms）。
   同期フラッシュの所要時間はこの予算に**計上**する。
3. **無応答時の自動復旧**: watchdog が heartbeat を `watchdog_stale_ms=3000` 未受信なら、
   **保留分のフラッシュ（50ms 予算・best effort）→ ログ + 通知 → `os._exit(70)`**。
   プロセス死亡で OS がフックを撤去 → 入力は必ず回復する。
4. **パニックキー**: `panic_key`（既定 **F1**）の **`panic_repeat`(3) 連打**（設定可・窓は 250ms）。v2-g でコードから連打へ変更（緊急時にコードは思い出せず、片手で押せない。F12 は機種欠落や位置差がある）。rule0（injected 除外）の後、**他のすべての判定より先**に最優先判定 →
   **保留分を順序付きフラッシュ → `UnhookWindowsHookEx` → 通知 → ログ**（UI に依存しない・フック内完結）。
5. **再送しない設計（意味を確定）**: 「飲み込んだキーの**再送**＝二重送信・過去分の再生」は
   **決してしない**。一方「**保留中の Enter の確定配信**（ちょうど 1 回）」は停止・パニック・例外の
   **すべてで行う**（無言破棄しない＝オペレータ指示 v2-c §6）。両者は I1 で区別される。
6. **injected の再処理禁止（重大・削除禁止）**: `LLKHF_INJECTED` は rule0 で処理対象外。
   自己参照設定（`ctrl+enter`→`send`→`ctrl+enter` 等）で無限ループ・二重送出が起きないことを
   テストで示すこと（I4）。
7. **ログ**: `%APPDATA%\enter-router\logs\enter-router.log`、`RotatingFileHandler`（1MB x 3）。
   **打鍵文字列・取得テキスト・クリップボード内容は書かない**（決定理由・タイミング・シンク結果のみ。
   research R9）。通知プレビューは `preview_chars=60` 上限。
8. **削除義務の扱い**: 永続化するユーザー由来データは**設定ファイルとログのみ**。打鍵・取得テキストは
   メモリ上のみで、処理完了（または破棄）で消える。全削除 = `%APPDATA%\enter-router` を削除するだけ。

---

## 7. UI の範囲（フロントエンドカードの実装対象・画面数を増やさない）

**S0 トレイアイコン（Shell_NotifyIcon）**
- 状態色: 緑=有効 / 黄=一時停止 / 赤=パニック停止中 / 回転風=シンク実行中。
- メニュー: 有効⇔一時停止 / 設定を開く / パニック停止（アンフック）/ ログを開く / 終了。

**S1 設定ウィンドウ（1 ウィンドウ・tkinter・3 タブ）**
- **Tab A 操作マップ（第1層）**: 全 18 トリガ（enter / enter2 / enter3 / shift・ctrl・alt+enter /
  f1〜f12）を行表示し、各行を意味プルダウン（send/newline/hermes/aichat/clipboard/none/pass）で編集。
  保存時に必須トリガ欠落を警告表示（core の warnings をそのまま表示）。併せて `hold_mode`
  （deterministic/fast）と `hold_window_ms`・`triple_enter_window_ms`・`trigger_cooldown_ms`・
  `panic_repeat` を編集。**v2-g: パニックキーと登録キー 2 つ（`registration.*`）はテキストで編集**できる。
- **Tab B プロファイル（第2層）**: 一覧 + 追加/削除/複製。編集項目: 対象（process/title/url）、
  **keymap（send/newline → 実キー列のコンボエディタ）**、有効/無効。シンク設定（3 種の有効化と
  パラメータ）もこのタブの下段に集約。**保存は JSON へ**（UI 都合でスキーマを変えない）。
- **Tab C テスト送信**: シンク選択 + サンプル文字列 + 送信 → 成否と理由を画面表示（`AppController.test_send`）。
- ステータスバー: フック状態 / 設定パス / 設定警告 / last_error。
- UX: パニック停止はメニューから 1 クリック。キャンセルで変更前へ。日本語 UI。
- **IME 配慮**: 設定画面の Entry で **Enter を既定ボタン決定に絶対割り当てない**（IME 確定 Enter の
  誤発火防止）。送信系ボタンは明示クリックのみ。

---

## 8. 実装分割（2 枚の実装カード）

**並走想定なし（frontend の parents に backend 完了を含む）。同じ main ブランチに sequential に積む。**

### t_57577e39 backend-implementer（中核）
担当:
- `core/`: **§4.6 の差分適用（config.py / routing.py / sample / test_smoke.py 更新 → テスト緑）**、
  `policy.py` の遷移ロジック実装（契約=§3.4 docstring）と **I1〜I8 の境界値テスト**、
  特に「カウント対象の Enter が 1 個もアプリに届かない」(I3) と「injected は処理対象外」(I4)、
  「あ→Enter→い の順序」(I6)。
- `platform/` 全部（LL フック・IME・SendInput 同期フラッシュ・S3・前景キャッシュ更新・通知・hermes CLI）、
  `app/`（main, watchdog, config_watcher, logsetup, AppController 実装, 同期フラッシュの配線）。
- `--dry-run` / `--once` 実装、README の実行手順の実測反映。

### t_e0676b15 frontend-implementer（UI）
担当: `ui/` 全部（§7 の 3 タブ + トレイ）。ロジックは**書かない**（AppController 経由のみ）。
必要最小の main.py 変更は「トレイ起動の wiring」のみ、理由を handoff に明記。

### 共有ファイル（変更は相手カードの handoff に必ず言及）

| ファイル | 契約 | 変更ルール |
|---|---|---|
| `core/config.py` | 設定スキーマと既定値（§4.2/§4.6） | **両カードとも勝手にフィールドを足さない**。必要なら設計者へ差し戻し |
| `core/routing.py` | 2 層解決 + プロファイル照合 + シンク選択 | 意味語彙・解決結果の文字列形式は変更禁止 |
| `core/sinks.py` | ISink / SinkResult / FailureKind | 追加は可（列挙値）、既存の意味変更は禁止 |
| `app/controller.py` | AppController Protocol | **メソッド追加（`get_config_dict()` / `save_config_dict(d)` を含む）は可**、意味変更は禁止 |
| `app/main.py` | エントリ + wiring | backend が実装、frontend はトレイ起動 wiring のみ |
| `config/sample.config.json` | サンプル（§4.2 と byte 一致） | スキーマ変更は設計者経由 |
| `tests/test_smoke.py` | 契約テスト（§4.6 必須 6 件） | backend のみ更新可 |
| `docs/design.md` | 本ファイル | 実装カードは変更しない（コメントで差し戻し） |

---

## 9. 実装順序と段階的ロールアウト（移行計画）

**リポジトリは新規（移行なし）だが、ユーザーのキー環境に対する「移行」は段階的に行う。**

| 段階 | 内容 | 進めるための観測トリガー |
|---|---|---|
| S0 | コア+アダプタ実装。WSL unittest 緑 + win_check 緑 | テスト緑 |
| S0.5 | **既定を `fast` にして 30 分打鍵**し、遅延ゼロの挙動を確認（`deterministic` の 350ms 保留はこの後） | 打鍵で違和感・誤送信 0 件 |
| S1 | **`--dry-run`**: 判定だけ観測 | ログ 30 分で「想定外抑止」0 件 |
| S2 | site-a/site-b 相当の 1 サイトをプロファイル化して限定有効 | Enter×1=改行・×2=送信 が意図通り |
| S3 | 実ブラウザの AI チャット 1 サイト + `AiChatSink` | 一括ペーストが成功、素通しに戻る |
| S4 | Hermes シンク有効（`--query-file -` の 1 回確認後） | 1 往復成功 + フォールバック動作確認 |

**ロールバック点**: どの段階でも ①パニックキー（1 秒）、②トレイ「一時停止」、③`policy.enabled=false`
（ホットリロード即反映）、④最悪 `pythonw` プロセス終了（OS がフック自動撤去）。
**常に「キーが素通しに戻る」ことを最優先の復旧経路とする。**

---

## 10. 危険な仮定（この 3 つが崩れると設計の根幹に効く）

1. **跨プロセスの IME 未確定判定**（`ImmGetContext` + `GCS_COMPSTR` は他プロセスでは不可能）。
   **2026-10-27 実測（t_dd246e5a）: IMM 経路は崩れており、TSF 経路で解決した。**
   - `AttachThreadInput(自スレッド, 前面スレッド, TRUE)` は成功する（tid は `GetWindowThreadProcessId` の
     **戻り値**。pid と取り違えると err=87 で必ず失敗する）。しかし `ImmGetContext(hwnd)` は前面ウィンドウ・
     `ImmGetDefaultIMEWnd`・`GetGUIThreadInfo` の `hwndFocus` の**3 段すべて 0（コンテキスト無し）**。
     IMM の入力コンテキストはプロセス内に閉じており、他プロセスからは取得できない（IMM の仕様）。
     さらに**同一プロセス内**でも Win11 の MS-IME(TSF) は合成文字列を IMM に公開しない
     （対象プロセスの `ImmGetCompositionStringW(GCS_COMPSTR)` が打鍵中も 0）。
   - 代わりに **TSF の窓は他プロセスから見える**（`EnumThreadWindows` + クラス名、実測）:
     未確定中だけ `MSCTFIME Composition`（4 個、うち 1 個 visible）と `CiceroUIWndFrame` が現れ、
     確定すると合成窓は消える。`MSCTFIME UI`（アンカー）はスレッド存続中ずっと居る。
   - **採用（§4.3 の第 4 段）**: 合成窓あり → `composing`（陽性のみ）／無し かつ 同スレッドにアンカー
     あり → `not_composing`／どちらも無し → `unknown`。実機で **composing と not_composing の両方**を
     観測（`scripts/win_ime_check.py`: `tsf_ime_ui` → `tsf_composition` → `tsf_ime_ui`、
     Enter で確定し改行なし、次の Enter で改行。チェック 14 件すべて PASS）。
   - **残る制約**: MSCTFIME をホストしないアプリ（TSF ネイティブ実装・他社 IME）は `unknown` のまま。
     その場合は `global.ime_unknown_policy`（既定 `pass`）に従い、**ログ `ime_state=unknown` ＋
     初回通知**で可視化する（黙って何も起きない状態にはしない）。
2. **既定の「常時 350ms 保留（deterministic）」が実用に耐える**。単発 Enter の遅延は全アプリに乗る
   （§3.8）。**崩れた場合**（体感が悪すぎる/対象アプリが注入 Enter を受け付けない）:
   `hold_mode=fast`（遅延ゼロ・3連の二重発火リスク明記）または `enter2/enter3=pass` の 1 行で即座に
   素通し体質へ戻れる。S0.5 段階で必ず確認。
3. **テキスト取得チェーンが実ターゲットで成立する**（特に Firefox の `clipboard_select_copy`）。
   UIA では Firefox の入力欄を特定できない（research R11 実測）。**崩れた場合**: 取得失敗 →
   「破棄 + 通知」で可視化（黙って失敗しない）。ホットキー + 手動コピーに切替可能だが体験は毀損する
   ため S3 段階で確認。

（受容済みリスクの一覧は research.md §5 を正とする。UIPI=対象外宣言、前景奪取は S3 手順のみ成功、
 SendInput の INPUT は x64 で 40 バイト等。）

---

## 11. 実装カードへの受け渡しメモ（この設計から読むべき場所）

- backend: §3（2 層パイプライン・遷移表・I1〜I8・順序保証）, §4.6（適用差分と必須テスト）,
  §5（ISink+Hermes 固定仕様）, §6（安全）, §9 S0-S0.5
- frontend: §7（画面定義・操作マップタブ）, §4（保存先スキーマ）, §8（共有ファイルの変更ルール）,
  app/controller.py（`get_config_dict`/`save_config_dict` 追加予定）
- QA: research.md §6（必須検証一覧）+ §9 の各段階トリガー + §10 の仮定 1/2/3
  + §3.9（`--dry-run` ログ書式の照合）
  + 「カウント中の Enter が届かない」「あ→Enter→い の順序」「injected が再トリガしない」の実機確認

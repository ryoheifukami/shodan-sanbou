# 商談準備アシスタント（B2B営業向け）

会社名と営業資料（PDF / Word / テキスト）を入れて、商談スタイルをボタンで選ぶだけで、
商談で使える材料を自動生成するツールです。

## 作れるもの

- 商談スクリプト（会話の流れ・セリフ調）
- 想定QA（相手から出そうな質問と回答）
- 切り返しトーク（「高い」「検討中」などへの対応）
- お礼メール（商談後すぐ送れる）
- 事前チェックリスト
- ヒアリング項目

## 使い方（ローカルで動かす）

このパソコンには Python が入っていて、APIキー（`ANTHROPIC_API_KEY`）も環境変数に設定済みなので、
追加の設定なしで動きます。

### 1. 必要なライブラリを入れる（初回だけ）

```powershell
cd C:\Users\aiacq\Projects\X_Buffer\sales-prep-tool
python -m pip install -r requirements.txt
```

### 2. アプリを起動する

```powershell
cd C:\Users\aiacq\Projects\X_Buffer\sales-prep-tool
python -m streamlit run app.py
```

ブラウザが自動で開きます（開かない場合は表示された `http://localhost:8501` を開く）。
止めるときは、起動したウィンドウで `Ctrl + C`。

## 設定（任意）

- **合言葉でロックしたい**場合：`.streamlit/secrets.toml`（見本は `secrets.toml.example`）に
  `APP_PASSWORD = "好きな合言葉"` を書く。空のままなら誰でも入れます。
- **生成の品質**：画面左の「設定」で「標準／最高品質／最速」を切り替えられます。

## このあとの拡張（後フェーズ）

- ログイン認証（営業個人ごとのID・パスワード）
- Stripe によるサブスク課金
- 有料会員を管理する CRM（データベース）

※ 心臓部（資料の読み込み〜Claudeでの生成ロジック）は `app.py` にまとまっているので、
将来 Web サービス化するときもそのまま引き継げます。

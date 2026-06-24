# -*- coding: utf-8 -*-
"""
ショウダン参謀（B2B営業向け / Streamlit）

3つの機能をタブで提供する。
  ① 準備資料を作る … 会社名・資料・スタイルから、商談スクリプト/想定QA/お礼メール等を生成
  ② その場で質問     … 想定外の質問を打ち込むと、資料をもとにそのまま言える回答を作る
  ③ 議事録まとめ     … 文字起こし/メモを貼ると、要点・決定事項・ネクストアクションに整理

会社名・サービス資料は画面上部で入力し、①②で共有して使う。
営業担当者自身の情報（氏名・会社・連絡先・署名）はサイドの「プロフィール」に保存される。

APIキー: Streamlit Secrets → 環境変数 ANTHROPIC_API_KEY の順で読む（ローカルは環境変数だけで動く）。
生成モデルは標準品質に固定（DEFAULT_MODEL）。
※ ログイン認証・決済（Stripe）・CRM は後フェーズ。今は check_password() の合言葉ゲートのみ。
"""

import os
import io
import re
import json
import base64

import streamlit as st
from anthropic import Anthropic

import membership

# ===== 基本設定 =====
LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "logo_trimmed.png")
PROFILE_PATH = os.path.join(os.path.dirname(__file__), "profile.json")
# モデルの使い分け（品質が要る成果物はSonnet、短い作業はHaikuでコスト最適化）
PREP_MODEL = "claude-sonnet-4-6"      # ① 準備資料（成果物・品質優先）
MINUTES_MODEL = "claude-sonnet-4-6"   # ③ 議事録（成果物・品質優先）
KARTE_MODEL = "claude-sonnet-4-6"     # サービスカルテの要約（下流の品質を左右するのでSonnet）
QA_MODEL = "claude-haiku-4-5"         # ② その場で質問（短い対話。Haikuで十分）
OCR_MODEL = "claude-haiku-4-5"        # 画像OCR（文字の書き起こし作業）
DEFAULT_MODEL = PREP_MODEL            # 後方互換

DOC_TEXT_LIMIT = 16000
MINUTES_LIMIT = 30000
KARTE_THRESHOLD = 2000   # これ未満の資料は要約せず原文のまま使う（要約の手間・コストに見合わないため）

PROFILE_FIELDS = ["name", "company", "title", "email", "phone", "signature"]

# ===== ボタン選択肢 =====
PHASES = [
    "初回アポ（顔合わせ・関係づくり）",
    "ヒアリング（課題の深掘り）",
    "提案・クロージング（受注を狙う）",
    "再訪・フォロー（検討中の後押し）",
]
STYLES = [
    "ヒアリング重視（聞く8割）",
    "提案型（こちらから設計を提示）",
    "価値・ROI訴求（費用対効果で説得）",
    "関係構築重視（信頼を積む）",
    "スピード重視（短時間で要点）",
]
PRODUCT_TYPES = [
    "無形サービス（代行・支援など）",
    "SaaS・ITツール",
    "有形商品・製品",
    "コンサルティング",
]
COUNTERPART_ROLES = [
    "経営者・決裁者",
    "現場の担当者",
    "情シス・技術担当",
    "役割が分からない／複数同席",
]
DURATIONS = ["15分", "30分", "60分"]
INDUSTRIES = [
    "指定なし",
    "製造業", "建設業", "IT・ソフトウェア", "卸売・小売", "飲食・サービス",
    "医療・介護", "士業（税理士・弁護士など）", "不動産", "教育", "金融・保険",
    "運輸・物流", "美容・健康", "広告・マーケティング", "農林水産",
    "その他（自由入力）",
]

MATERIALS = {
    "商談スクリプト": "アイスブレイク→課題確認→提案→次アクションまでの会話の流れ（実際に話せるセリフ調で。途中で省略せず最後まで書く）",
    "想定QA": "相手から出そうな質問と、それに対する模範回答（5〜8個）",
    "切り返しトーク": "「高い」「他社と比較中」「今は不要」など断り文句への切り返し（3〜5個）",
    "お礼メール": "商談後すぐ送れるお礼メール（件名＋本文）。次アクションへ自然につなげる",
    "事前チェックリスト": "商談前に準備・確認しておくべき項目のチェックリスト",
    "ヒアリング項目": "商談中に必ず聞き出したい質問リスト（優先順位つき）",
}

SECTION_MARK = "###SECTION:{key}###"
SECTION_RE = re.compile(r"###SECTION:(.+?)###")

COMMON_RULES = (
    "次のルールを必ず守ってください。\n"
    "- 資料に書かれていない事実・数値・実績を勝手に作らない（誇張・捏造の禁止）\n"
    "- 情報が足りない箇所は［要確認：◯◯］のように明示する\n"
    "- 抽象論ではなく、その場で話せる・送れる具体的な言葉で書く\n"
    "- 日本語。読みやすく、営業担当者がすぐ使える体裁にする\n"
    "- 同じ内容の繰り返しや前置きの水増しはせず、密度の高い実用的な内容にする\n"
)


# ===== プロフィール（営業担当者自身の固定情報）の保存・読み込み =====
def load_profile() -> dict:
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_profile(p: dict):
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def profile_block(p: dict) -> str:
    """プロンプトに差し込む担当者情報。お礼メール等の署名に使う。"""
    lines = []
    if p.get("company"):
        lines.append(f"- 会社名：{p['company']}")
    if p.get("name"):
        title = f"（{p['title']}）" if p.get("title") else ""
        lines.append(f"- 担当者：{p['name']}{title}")
    if p.get("email"):
        lines.append(f"- メール：{p['email']}")
    if p.get("phone"):
        lines.append(f"- 電話：{p['phone']}")
    if p.get("signature"):
        lines.append(f"- 署名テンプレート：\n{p['signature']}")
    return "\n".join(lines) if lines else "（担当者情報の登録なし）"


# ===== ロゴ表示（画像埋め込み。全画面ボタンは出さない）=====
@st.cache_data(show_spinner=False)
def _logo_data_uri() -> str:
    with open(LOGO_PATH, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def show_logo(width: int = 330):
    if os.path.exists(LOGO_PATH):
        st.markdown(
            f'<img src="{_logo_data_uri()}" width="{width}" style="margin:6px 0 10px;">',
            unsafe_allow_html=True,
        )
    else:
        st.title("ショウダン参謀")


# ===== ファイル・画像からテキストを抽出 =====
@st.cache_data(show_spinner=False)
def _extract_cached(name: str, data: bytes) -> str:
    name = (name or "").lower()
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        if name.endswith(".docx"):
            from docx import Document
            doc = Document(io.BytesIO(data))
            return "\n".join(par.text for par in doc.paragraphs)
        if name.endswith((".txt", ".md")):
            for enc in ("utf-8", "cp932", "shift_jis"):
                try:
                    return data.decode(enc)
                except UnicodeDecodeError:
                    continue
            return data.decode("utf-8", errors="ignore")
    except Exception as e:
        return f"（{name} の読み取りに失敗しました: {e}）"
    return f"（{name} は対応していない形式です）"


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}


def _shrink_image(data: bytes, media: str, max_edge: int = 1568):
    """大きすぎる画像を長辺max_edgeまで縮小（可読性は保ちつつOCRのトークンを削減）。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) <= max_edge:
            return data, media
        ratio = max_edge / max(im.size)
        im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="PNG")  # 可逆PNGで文字の劣化を避ける
        return buf.getvalue(), "image/png"
    except Exception:
        return data, media


@st.cache_data(show_spinner=False)
def _ocr_cached(api_key: str, name: str, data: bytes) -> str:
    """画像内のテキストをClaudeの画像認識で書き起こす（OCR）。同じ画像はキャッシュ。"""
    ext = os.path.splitext(name.lower())[1]
    media = IMAGE_MEDIA.get(ext, "image/png")
    data, media = _shrink_image(data, media)
    b64 = base64.standard_b64encode(data).decode()
    client = Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model=OCR_MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": "この画像に書かれている文字をすべて書き出してください。"
                 "表は分かる範囲で整え、前置きや説明は不要、本文テキストのみを返してください。"
                 "文字が見当たらなければ『（文字なし）』とだけ返してください。"},
            ]}],
        )
        return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    except Exception as e:
        return f"（{name} の画像読み取りに失敗しました: {e}）"


def gather_material_text(uploaded_files, pasted_text: str, api_key: str = "",
                         limit: int = DOC_TEXT_LIMIT) -> str:
    parts = []
    for f in uploaded_files or []:
        ext = os.path.splitext(f.name.lower())[1]
        if ext in IMAGE_EXTS and api_key:
            text = _ocr_cached(api_key, f.name, f.getvalue()).strip()
        else:
            text = _extract_cached(f.name, f.getvalue()).strip()
        if text:
            parts.append(f"【資料: {f.name}】\n{text}")
    if pasted_text.strip():
        parts.append(f"【貼り付けメモ】\n{pasted_text.strip()}")
    combined = "\n\n".join(parts)
    if len(combined) > limit:
        combined = combined[:limit] + "\n\n（※ 文字数が多いため一部省略しました）"
    return combined


def claude_text(client, model, system_text, user_text, max_tokens=8000) -> str:
    msg = client.messages.create(
        model=model, max_tokens=max_tokens,
        system=system_text, messages=[{"role": "user", "content": user_text}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")


# ===== サービスカルテ（資料を一度だけ忠実に要約。①②の入力を軽くする）=====
@st.cache_data(show_spinner=False)
def _karte_cached(api_key: str, material_text: str) -> str:
    system_text = (
        "あなたは営業資料を整理する専門家です。提供された資料を、営業担当が商談準備に使うための"
        "『サービスカルテ』に再構成します。次を厳守してください。\n"
        "- 料金・数値・実績・固有名詞・導入事例・差別化ポイントは省略せず正確に残す（情報の欠落は厳禁）\n"
        "- 資料に書かれていない情報は足さない（推測・創作の禁止）\n"
        "- 次の見出しで整理：サービス概要／提供価値・強み／料金・プラン／実績・事例／"
        "差別化（競合との違い）／想定される懸念と回答材料\n"
        "- 箇条書き中心。冗長な説明や前置きは省く"
    )
    user_text = f"# 元資料\n{material_text}\n\n上記を、情報を落とさずサービスカルテに整理してください。"
    client = Anthropic(api_key=api_key)
    return claude_text(client, KARTE_MODEL, system_text, user_text, max_tokens=2500)


def context_for_generation(api_key: str, material_text: str) -> str:
    """資料が大きいときだけカルテ化（要約）して返す。小さければ原文のまま。"""
    if material_text and len(material_text) > KARTE_THRESHOLD and api_key:
        return _karte_cached(api_key, material_text)
    return material_text


# ===== ① 準備資料の生成（ストリーミング＋進捗）=====
def build_prep_prompt(cfg: dict) -> tuple[str, str]:
    system_text = (
        "あなたは日本のB2B営業を支援する、経験豊富な営業コーチです。"
        "提供された会社情報・サービス資料をよく読み、営業担当者がそのまま商談で使える"
        "実践的な材料を作ります。\n" + COMMON_RULES
    )
    spec_lines = [f"{SECTION_MARK.format(key=k)}\n{k}：{MATERIALS[k]}" for k in cfg["materials"]]
    relation = cfg["counterpart_company"].strip() or "（指定なし）"
    industry = cfg["counterpart_industry"].strip() or "（指定なし）"

    user_text = f"""# 商談の前提
- サービス名・商材：{cfg['service']}
- 商談相手の会社名：{relation}
- 相手の業種：{industry}
- 商談の相手（役職）：{cfg['role']}
- 商談フェーズ：{cfg['phase']}
- 営業スタイル：{cfg['style']}
- 商材タイプ：{cfg['product_type']}
- 商談時間：{cfg['duration']}

# 自社・担当者の情報（お礼メールの署名・自己紹介に使う）
{profile_block(cfg['profile'])}

# 自社サービスの資料・情報
{cfg['material_text'] or '（資料なし。上記の前提から一般的な範囲で作成）'}

# 出力してほしい材料
各セクションを、指定の見出しマーカー（###SECTION:◯◯###）をそのまま付けて順番に出力してください。
お礼メールには、上の担当者情報を使った署名を入れてください。

{chr(10).join(spec_lines)}
"""
    return system_text, user_text


def to_plain(md: str) -> str:
    """Markdownの記号（* # ` - など）を取り除き、そのまま貼れるプレーン文章にする。"""
    out = []
    for raw in (md or "").split("\n"):
        s = raw.rstrip()
        if re.fullmatch(r"\s*[-=*_]{3,}\s*", s):          # 区切り線を削除
            out.append("")
            continue
        s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s)            # 見出しの # を削除
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1・", s)          # 箇条書き → ・
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", s)  # リンク[文字](URL)→文字（URL）
        s = re.sub(r"^\s*>\s?", "", s)                     # 引用符 > を削除
        s = s.replace("`", "").replace("**", "").replace("__", "")
        s = s.replace("*", "").replace("~~", "")           # 残った強調記号を削除
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def to_display(md: str) -> str:
    """画面表示用。装飾の罫線(=== --- など)で文字が巨大化する誤変換（Setext見出し）を防ぐ。太字などは残す。"""
    out = []
    for line in (md or "").split("\n"):
        if re.fullmatch(r"\s*[=\-_*]{3,}\s*", line):
            out.append("")                              # 罫線だけの行は空行に
            continue
        line = re.sub(r"^\s*[=\-_*]{3,}\s*", "", line)  # 行頭の装飾記号の連続を除去
        out.append(line)
    return "\n".join(out)


def parse_sections(text: str, requested: list) -> dict:
    result = {}
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        result[requested[0]] = text.strip()
        return result
    for i, m in enumerate(matches):
        key = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[key] = text[start:end].strip()
    return result


def generate_materials(client, cfg, on_progress=None) -> dict:
    system_text, user_text = build_prep_prompt(cfg)
    total = len(cfg["materials"]) or 1
    buf, seen = [], 0
    with client.messages.stream(model=PREP_MODEL, max_tokens=8000, system=system_text,
                                messages=[{"role": "user", "content": user_text}]) as stream:
        for delta in stream.text_stream:
            buf.append(delta)
            markers = SECTION_RE.findall("".join(buf))
            if on_progress and len(markers) != seen:
                seen = len(markers)
                on_progress(min(seen / total, 0.95), markers[-1] if markers else "作成中")
    if on_progress:
        on_progress(1.0, "完了")
    return parse_sections("".join(buf), cfg["materials"])


# ===== ② その場で質問 =====
def answer_question(client, ctx: dict, question: str) -> str:
    system_text = (
        "あなたは日本のB2B営業に同席するベテランの先輩営業です。"
        "商談中に相手から出た想定外の質問に対し、担当者がそのまま声に出して言える回答を、"
        "提供されたサービス資料に基づいて作ります。\n"
        "- 回答は2〜4文程度で簡潔に。話し言葉で。\n"
        "- 見出し記号（#）や強調記号（*）、箇条書き記号は使わず、自然な文章で書く。\n"
        "- 資料から答えられないことは無理に作らず、『一度持ち帰って確認する』形の自然な返し方を提案する。\n"
        "- 必要なら、相手に投げ返す一言（質問返し）も添える。\n"
        + COMMON_RULES
    )
    user_text = f"""# 自社サービスの情報
{ctx.get('material_text') or '（資料なし）'}

# サービス名・商材
{ctx.get('service') or '（未入力）'}

# 商談相手から受けた質問
{question}

この質問に対する、その場で言える回答を作ってください。"""
    return claude_text(client, QA_MODEL, system_text, user_text, max_tokens=1200)


# ===== ③ 議事録まとめ =====
def summarize_minutes(client, ctx: dict, transcript: str) -> str:
    system_text = (
        "あなたは商談に同席した優秀なアシスタントです。"
        "商談の文字起こし・メモを読み、営業担当者と上司が一目で把握できる議事録にまとめます。"
        "話し言葉の繰り返しや言い淀みは整理し、要点だけを残します。\n"
        "次の見出しで、Markdownの箇条書き中心にまとめてください。\n"
        "## 商談サマリー（3〜5行）\n"
        "## 相手の課題・関心・懸念\n"
        "## 決定事項・合意したこと\n"
        "## ネクストアクション（自社がやること／相手がやること／期限）\n"
        "## フォローメール案（件名＋本文。すぐ送れる形で）\n"
        "- ネクストアクションは『誰が・何を・いつまでに』を明確に。期限が不明なものは［期限：要確認］と書く。\n"
        "- フォローメールには下の担当者情報を使った署名を入れる。\n"
        "- 文字起こしに無い事実は足さない。\n"
    )
    user_text = f"""# サービス名・商材
{ctx.get('service') or '（未入力）'}

# 自社・担当者の情報（署名に使う）
{profile_block(ctx.get('profile', {}))}

# 商談の文字起こし・メモ
{transcript}
"""
    return claude_text(client, MINUTES_MODEL, system_text, user_text, max_tokens=4000)


# ===== APIキー・合言葉 =====
def read_api_key() -> str:
    candidates = []
    try:
        for k in ("ANTHROPIC_API_KEY", "anthropic_api_key", "API_KEY"):
            v = st.secrets.get(k, "")
            if v:
                candidates.append(str(v))
    except Exception:
        pass
    env_v = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_v:
        candidates.append(env_v)
    for s in candidates:
        s = re.sub(r"\s+", "", s.strip().strip("\"'“”’‘").strip())
        if s:
            return s
    return ""


def check_password() -> bool:
    try:
        expected = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        expected = ""
    if not expected or st.session_state.get("auth_ok"):
        return True
    show_logo()
    pw = st.text_input("合言葉を入力してください", type="password")
    if pw:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("合言葉が違います。")
    return False


# ===== サイドバー：プロフィール =====
def render_profile_sidebar(email) -> dict:
    # ログイン中の会員のプロフィールをDBから読み込む（会員が変わったら読み直す）
    if st.session_state.get("pf_for") != email:
        prof = membership.get_profile(email)
        for k in PROFILE_FIELDS:
            st.session_state[f"pf_{k}"] = prof.get(k, "")
        st.session_state["pf_for"] = email

    with st.sidebar:
        membership.render_account_controls(email)
        st.divider()
        st.subheader("あなたのプロフィール")
        st.caption("お礼メールの署名などに使われます。")
        st.text_input("氏名", key="pf_name")
        st.text_input("会社名", key="pf_company")
        st.text_input("部署・役職 :gray[任意]", key="pf_title")
        st.text_input("連絡先メール :gray[任意]", key="pf_email")
        st.text_input("電話番号 :gray[任意]", key="pf_phone")
        st.text_area("メール署名 :gray[任意]", key="pf_signature", height=90)
        if st.button("プロフィールを保存", use_container_width=True):
            membership.save_profile(email, {k: st.session_state[f"pf_{k}"] for k in PROFILE_FIELDS})
            st.success("保存しました")

    return {k: st.session_state[f"pf_{k}"] for k in PROFILE_FIELDS}


# ===== 各タブ =====
def render_prep_tab(client, ctx):
    st.subheader("商談のスタイル")
    s1, s2 = st.columns(2)
    with s1:
        phase = st.radio("商談フェーズ", PHASES, index=0)
        style = st.radio("営業スタイル", STYLES, index=0)
        product_type = st.radio("商材タイプ", PRODUCT_TYPES, index=0)
    with s2:
        role = st.selectbox("商談の相手（役職）", COUNTERPART_ROLES, index=0)
        duration = st.radio("商談時間", DURATIONS, index=1)

    st.subheader("用意する材料")
    default_on = {"商談スクリプト", "想定QA", "切り返しトーク", "お礼メール"}
    cols = st.columns(3)
    materials = []
    for i, key in enumerate(MATERIALS.keys()):
        with cols[i % 3]:
            if st.checkbox(key, value=(key in default_on), help=MATERIALS[key]):
                materials.append(key)

    if st.button("🚀 商談材料を作成する", type="primary", use_container_width=True):
        if not ctx["service"]:
            st.warning("上の「サービス名・商材名」を入力してください。")
            return
        if not materials:
            st.warning("材料を1つ以上選んでください。")
            return
        prog = st.progress(0.0, text="資料を整理しています…")
        # 大きい資料はカルテ化（忠実な要約）してから生成。品質は保ちつつ入力を軽量化する
        material = context_for_generation(ctx["api_key"], ctx["material_text"])
        cfg = dict(ctx, material_text=material, phase=phase, style=style,
                   product_type=product_type, role=role, duration=duration, materials=materials)

        def _cb(frac, label):
            prog.progress(frac, text=f"{label} を作成中…（{int(frac * 100)}%）")

        try:
            st.session_state["prep_sections"] = generate_materials(client, cfg, on_progress=_cb)
        except Exception as e:
            prog.empty()
            st.error(f"作成中にエラーが発生しました：{e}")
            return
        prog.empty()

    sections = st.session_state.get("prep_sections")
    if sections:
        st.success("できあがりました。タブを切り替えて確認・コピーできます。")
        full = "\n\n".join(f"■ {k}\n{to_plain(v)}" for k, v in sections.items())
        st.download_button("⬇ まとめてテキスト保存", full,
                           file_name=f"商談準備_{ctx['service']}.txt", mime="text/plain")
        tabs = st.tabs(list(sections.keys()))
        for tab, (key, content) in zip(tabs, sections.items()):
            with tab:
                st.markdown(to_display(content))
                with st.expander("📋 コピー用（そのまま貼れる文章）"):
                    st.code(to_plain(content), language=None)


def render_qa_tab(client, ctx):
    st.caption("想定外の質問に、その場で言える回答を作ります。")
    question = st.text_input("商談相手から受けた質問",
                             placeholder="例：他社さんと何が違うんですか？／導入にどれくらいかかりますか？")
    if st.button("💬 回答を作る", type="primary"):
        if not question.strip():
            st.warning("質問を入力してください。")
        else:
            with st.spinner("回答を考えています…"):
                try:
                    qctx = dict(ctx, material_text=context_for_generation(ctx["api_key"], ctx["material_text"]))
                    ans = answer_question(client, qctx, question.strip())
                    st.session_state.setdefault("qa_history", []).insert(0, (question.strip(), ans))
                except Exception as e:
                    st.error(f"エラーが発生しました：{e}")

    history = st.session_state.get("qa_history", [])
    if history:
        st.divider()
        for q, a in history:
            st.markdown(f"**Q. {q}**")
            st.markdown(to_display(a))
            st.markdown("---")
        if st.button("🗑 履歴をクリア"):
            st.session_state["qa_history"] = []
            st.rerun()


def render_minutes_tab(client, ctx):
    st.caption("文字起こしやメモを貼ると、要点とネクストアクションに整理します。")
    transcript = st.text_area("文字起こし・メモを貼り付け", height=240,
                              placeholder="ZoomやTeamsのトランスクリプト、手書きメモの書き起こしなど")
    if st.button("📝 議事録にまとめる", type="primary"):
        if not transcript.strip():
            st.warning("文字起こし・メモを貼り付けてください。")
        else:
            text = transcript.strip()[:MINUTES_LIMIT]
            with st.spinner("議事録にまとめています…"):
                try:
                    st.session_state["minutes_result"] = summarize_minutes(client, ctx, text)
                except Exception as e:
                    st.error(f"エラーが発生しました：{e}")

    result = st.session_state.get("minutes_result")
    if result:
        st.success("まとめました。")
        st.download_button("⬇ 議事録をテキスト保存", to_plain(result),
                           file_name=f"議事録_{ctx['service']}.txt", mime="text/plain")
        st.markdown(to_display(result))
        with st.expander("📋 コピー用（そのまま貼れる文章）"):
            st.code(to_plain(result), language=None)


def main():
    st.set_page_config(page_title="ショウダン参謀",
                       page_icon=LOGO_PATH if os.path.exists(LOGO_PATH) else "🤝", layout="wide")

    # ログイン＋サブスク確認。未ログイン/未加入ならここで画面を出して停止する
    member_email = membership.authenticate(show_logo)

    api_key = read_api_key()
    show_logo()

    if not api_key:
        st.error("ただいまご利用いただけません（APIキー未設定）。")
        st.stop()
    client = Anthropic(api_key=api_key)

    profile = render_profile_sidebar(member_email)

    # --- 基本情報（①②で共有）---
    st.subheader("基本情報")
    c1, c2 = st.columns(2)
    with c1:
        service = st.text_input("サービス名・商材名 :red[＊]", placeholder="例：△△というSaaS / ○○サービス")
        counterpart_company = st.text_input("商談相手の会社名 :gray[任意]", placeholder="例：株式会社サンプル")
    with c2:
        industry_choice = st.selectbox("相手の業種 :gray[任意]", INDUSTRIES, index=0)
        if industry_choice == "その他（自由入力）":
            counterpart_industry = st.text_input("業種を入力", placeholder="例：印刷業")
        elif industry_choice == "指定なし":
            counterpart_industry = ""
        else:
            counterpart_industry = industry_choice

    st.markdown("**サービス資料** :gray[任意]")
    uploaded_files = st.file_uploader(
        "ファイルをアップロード（PDF / Word / テキスト / 画像）",
        type=["pdf", "docx", "txt", "md", "png", "jpg", "jpeg", "webp", "gif"],
        accept_multiple_files=True)
    st.caption("画像・スクリーンショットは文字を自動で読み取ります。")
    pasted_text = st.text_area("資料を貼り付け", height=160,
                               placeholder="サービスの特徴・強み・料金・実績など。ファイルが無くても、ここに貼るだけで使えます。")

    has_image = any(os.path.splitext(f.name.lower())[1] in IMAGE_EXTS for f in (uploaded_files or []))
    if has_image:
        with st.spinner("画像から文字を読み取っています…"):
            material_text = gather_material_text(uploaded_files, pasted_text, api_key)
    else:
        material_text = gather_material_text(uploaded_files, pasted_text, api_key)

    ctx = {
        "service": service.strip(),
        "counterpart_company": counterpart_company,
        "counterpart_industry": counterpart_industry,
        "material_text": material_text,
        "profile": profile,
        "api_key": api_key,
    }

    st.divider()
    tab1, tab2, tab3 = st.tabs(["① 準備資料を作る", "② その場で質問", "③ 議事録まとめ"])
    with tab1:
        render_prep_tab(client, ctx)
    with tab2:
        render_qa_tab(client, ctx)
    with tab3:
        render_minutes_tab(client, ctx)


if __name__ == "__main__":
    main()

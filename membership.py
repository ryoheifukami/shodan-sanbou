# -*- coding: utf-8 -*-
"""
会員制・決済の土台（ショウダン参謀）

- 会員DB：DATABASE_URL（Neon等のPostgres）があればそれを使い、無ければローカルSQLiteにフォールバック。
  → ローカルは設定ゼロで動き、公開時は DATABASE_URL を入れるだけでPostgresに切替（無料運用・データ永続化）。
- 認証：メール＋パスワード（pbkdf2でハッシュ化保存／標準ライブラリのみ・追加依存なし）。
- 決済：Stripe Checkout（申込）＋ Customer Portal（解約・カード変更）。支払い状態はStripe APIで確認。
- これらの会員データがそのままCRMの基盤（メール・状態・登録日・最終ログイン）になる。

設定（環境変数 or .streamlit/secrets.toml）:
    DATABASE_URL       = "postgresql://..."   # 無ければローカルSQLite
    STRIPE_SECRET_KEY  = "sk_test_..."        # 無ければ「テスト用 手動有効化」で動作確認できる
    STRIPE_PRICE_ID    = "price_..."          # 月額990円プランのPrice ID
    APP_BASE_URL       = "http://localhost:8501"  # 決済後の戻り先（公開時は本番URL）
"""

import os
import hashlib
import secrets
import datetime

import streamlit as st
from sqlalchemy import create_engine, text

PROFILE_KEYS = ["name", "company", "title", "phone", "signature"]


def _cfg(key, default=""):
    v = os.environ.get(key)
    if v:
        return v
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ===== データベース =====
def _db_url():
    url = _cfg("DATABASE_URL")
    if not url:
        return "sqlite:///" + os.path.join(os.path.dirname(__file__), "members.db")
    # ドライバ名を明示（psycopg2）
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


@st.cache_resource(show_spinner=False)
def get_engine():
    eng = create_engine(_db_url(), pool_pre_ping=True)
    with eng.begin() as cx:
        cx.execute(text(
            "CREATE TABLE IF NOT EXISTS members ("
            "email TEXT PRIMARY KEY,"
            "password_hash TEXT NOT NULL,"
            "created_at TEXT,"
            "last_login_at TEXT,"
            "stripe_customer_id TEXT,"
            "subscription_status TEXT DEFAULT 'none',"
            "name TEXT, company TEXT, title TEXT, phone TEXT, signature TEXT,"
            "session_token TEXT"
            ")"
        ))
    # 既存DB向けマイグレーション（列が無ければ足す。あればエラーを無視）
    for ddl in ("ALTER TABLE members ADD COLUMN session_token TEXT",):
        try:
            with eng.begin() as cx:
                cx.execute(text(ddl))
        except Exception:
            pass
    return eng


def get_member(email):
    with get_engine().begin() as cx:
        row = cx.execute(text("SELECT * FROM members WHERE email=:e"), {"e": email}).mappings().first()
    return dict(row) if row else None


def get_member_by_token(token):
    if not token:
        return None
    with get_engine().begin() as cx:
        row = cx.execute(text("SELECT * FROM members WHERE session_token=:t"),
                         {"t": token}).mappings().first()
    return dict(row) if row else None


def create_member(email, password):
    with get_engine().begin() as cx:
        cx.execute(text(
            "INSERT INTO members (email, password_hash, created_at, subscription_status) "
            "VALUES (:e, :p, :c, 'none')"
        ), {"e": email, "p": hash_password(password), "c": _now()})


def update_member(email, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    params = dict(fields, e=email)
    with get_engine().begin() as cx:
        cx.execute(text(f"UPDATE members SET {sets} WHERE email=:e"), params)


# ===== パスワード（標準ライブラリのpbkdf2でハッシュ化）=====
def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2$200000${salt}${h}"


def verify_password(pw, stored):
    try:
        _, iters, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters)).hex()
        return secrets.compare_digest(calc, h)
    except Exception:
        return False


# ===== プロフィール（会員ごと）=====
def get_profile(email):
    m = get_member(email) or {}
    p = {k: (m.get(k) or "") for k in PROFILE_KEYS}
    p["email"] = email   # 署名用の連絡先メールはログインメールを既定に
    return p


def save_profile(email, prof):
    update_member(email, **{k: prof.get(k, "") for k in PROFILE_KEYS})


# ===== Stripe（決済）=====
def stripe_enabled():
    return bool(_cfg("STRIPE_SECRET_KEY") and _cfg("STRIPE_PRICE_ID"))


def _stripe():
    import stripe
    stripe.api_key = _cfg("STRIPE_SECRET_KEY")
    return stripe


def _base_url():
    return _cfg("APP_BASE_URL", "http://localhost:8501").rstrip("/")


def ensure_customer(email):
    m = get_member(email)
    if m and m.get("stripe_customer_id"):
        return m["stripe_customer_id"]
    cust = _stripe().Customer.create(email=email)
    update_member(email, stripe_customer_id=cust.id)
    return cust.id


def subscription_active(email):
    """Stripeに問い合わせて有効なサブスクがあるか確認（Webサーバー不要）。"""
    m = get_member(email)
    cid = (m or {}).get("stripe_customer_id")
    if not cid:
        return False
    try:
        subs = _stripe().Subscription.list(customer=cid, status="all", limit=10)
        for s in subs.auto_paging_iter():
            if s.status in ("active", "trialing", "past_due"):
                update_member(email, subscription_status=s.status)
                return True
        update_member(email, subscription_status="none")
        return False
    except Exception:
        return False


def _comp_emails():
    """支払い不要で使える招待（コンプ）アカウントのメール一覧。テスト・デモ・関係者向け。"""
    raw = _cfg("COMP_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_active(email):
    if email.lower() in _comp_emails():   # 招待アカウントは支払い不要で常に有効
        return True
    if stripe_enabled():
        return subscription_active(email)
    # Stripe未設定時はDBのフラグ（テスト用 手動有効化）で判定
    return (get_member(email) or {}).get("subscription_status") == "active"


def checkout_url(email):
    cid = ensure_customer(email)
    base = _base_url()
    sess = _stripe().checkout.Session.create(
        mode="subscription",
        customer=cid,
        line_items=[{"price": _cfg("STRIPE_PRICE_ID"), "quantity": 1}],
        success_url=base + "/?checkout=success",
        cancel_url=base + "/?checkout=cancel",
        locale="ja",
        allow_promotion_codes=True,
    )
    return sess.url


def portal_url(email):
    cid = ensure_customer(email)
    ps = _stripe().billing_portal.Session.create(customer=cid, return_url=_base_url())
    return ps.url


# ===== ログイン保持（Cookie）=====
COOKIE_NAME = "sst"
COOKIE_DAYS = 30


def cookie_manager():
    """1リクエストにつき1回だけ生成して使い回す（main()で生成）。"""
    import extra_streamlit_components as stx
    return stx.CookieManager(key="sst_mgr")


def _start_session(cm, email):
    """ログイン成立時：トークンを発行してDBとCookieに保存（次回以降ログイン不要に）。"""
    token = secrets.token_urlsafe(32)
    update_member(email, session_token=token)
    st.session_state["member_email"] = email
    st.session_state.pop("sub_active", None)
    try:
        cm.set(COOKIE_NAME, token,
               expires_at=datetime.datetime.now() + datetime.timedelta(days=COOKIE_DAYS),
               key="sst_set")
    except Exception:
        pass


# ===== 画面 =====
def _render_auth(cm):
    st.subheader("ログイン / 新規登録")
    tab_login, tab_reg = st.tabs(["ログイン", "新規登録"])
    with tab_login:
        with st.form("login_form"):
            email = st.text_input("メールアドレス")
            pw = st.text_input("パスワード", type="password")
            if st.form_submit_button("ログイン", type="primary", use_container_width=True):
                m = get_member(email.strip().lower())
                if m and verify_password(pw, m["password_hash"]):
                    update_member(m["email"], last_login_at=_now())
                    _start_session(cm, m["email"])
                    st.rerun()
                else:
                    st.error("メールアドレスまたはパスワードが違います。")
    with tab_reg:
        with st.form("register_form"):
            email = st.text_input("メールアドレス", key="reg_email")
            pw = st.text_input("パスワード（8文字以上）", type="password", key="reg_pw")
            pw2 = st.text_input("パスワード（確認）", type="password", key="reg_pw2")
            if st.form_submit_button("登録する", type="primary", use_container_width=True):
                email = email.strip().lower()
                if "@" not in email or "." not in email:
                    st.error("メールアドレスの形式が正しくありません。")
                elif len(pw) < 8:
                    st.error("パスワードは8文字以上にしてください。")
                elif pw != pw2:
                    st.error("確認用パスワードが一致しません。")
                elif get_member(email):
                    st.error("このメールアドレスは既に登録されています。")
                else:
                    create_member(email, pw)
                    _start_session(cm, email)
                    st.rerun()


def _render_paywall(email, cm):
    st.info("ご利用には月額プラン（990円）へのお申し込みが必要です。")
    if stripe_enabled():
        try:
            st.link_button("💳 月額990円で申し込む", checkout_url(email),
                           type="primary", use_container_width=True)
        except Exception as e:
            st.error(f"決済ページを開けませんでした：{e}")
        if st.button("支払い済みの場合：最新の状態に更新", use_container_width=True):
            st.session_state.pop("sub_active", None)
            st.rerun()
    else:
        st.warning("（管理者向け）Stripe未設定のため、テスト用に手動で有効化できます。")
        if st.button("（テスト用）この会員を有効化する", use_container_width=True):
            update_member(email, subscription_status="active")
            st.session_state["sub_active"] = True
            st.rerun()
    st.divider()
    if st.button("ログアウト"):
        logout(cm)


def logout(cm):
    email = st.session_state.get("member_email")
    if email:
        try:
            update_member(email, session_token=None)
        except Exception:
            pass
    try:
        cm.delete(COOKIE_NAME, key="sst_del")
    except Exception:
        pass
    for k in list(st.session_state.keys()):
        if k.startswith("pf_") or k in ("member_email", "sub_active", "pf_for", "_ck_tried"):
            st.session_state.pop(k, None)
    st.rerun()


def authenticate(cm, header_fn=None):
    """ログイン＋サブスク有効を確認し、OKなら会員メールを返す。未達なら画面を出してst.stop()。"""
    get_engine()  # DB初期化

    # 決済完了で戻ってきたとき、状態を再確認させる
    try:
        if st.query_params.get("checkout") == "success":
            st.session_state.pop("sub_active", None)
            st.query_params.clear()
    except Exception:
        pass

    email = st.session_state.get("member_email")

    # Cookieからログイン復元（再ログイン不要にする）
    if not email:
        token = None
        try:
            token = (cm.get_all() or {}).get(COOKIE_NAME)
        except Exception:
            token = None
        if token:
            m = get_member_by_token(token)
            if m:
                email = m["email"]
                st.session_state["member_email"] = email
        elif not st.session_state.get("_ck_tried"):
            # Cookieの読み込みは1テンポ遅れるので、初回だけ待つ（ログイン画面のチラつき防止）
            st.session_state["_ck_tried"] = True
            if header_fn:
                header_fn()
            st.caption("読み込み中…")
            st.stop()

    if not email:
        if header_fn:
            header_fn()
        _render_auth(cm)
        st.stop()

    if not st.session_state.get("sub_active"):
        active = _is_active(email)
        st.session_state["sub_active"] = active
        if not active:
            if header_fn:
                header_fn()
            _render_paywall(email, cm)
            st.stop()
    return email


def render_account_controls(email, cm):
    """サイドバー用：契約管理・ログアウト。"""
    st.caption(f"ログイン中：{email}")
    if stripe_enabled():
        try:
            st.link_button("契約・カードの管理", portal_url(email), use_container_width=True)
        except Exception:
            pass
    if st.button("ログアウト", use_container_width=True):
        logout(cm)

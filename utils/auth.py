# utils/auth.py
import os
import streamlit as st

# ON/OFF global
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "1") == "1"

# --- Modo legacy (1 usuario) ---
LOGIN_USER = os.getenv("APP_LOGIN_USER", "admin")
LOGIN_PASS = os.getenv("APP_LOGIN_PASS", "1234")

# --- Modo multi-usuario con password compartida ---
# APP_LOGIN_USERS="admin,miguel,joan"
# APP_LOGIN_PASSWORD="tu_password_unica"
LOGIN_USERS = {u.strip() for u in os.getenv("APP_LOGIN_USERS", "").split(",") if u.strip()}
LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "")

# --- Modo multi-usuario con credenciales dedicadas (RECOMENDADO) ---
# APP_LOGIN_CREDENTIALS="admin:1234,miguel:s3cr3t,joan:abcd"
LOGIN_CREDENTIALS = os.getenv("APP_LOGIN_CREDENTIALS", "")

def _parse_credentials_map(raw: str) -> dict:
    """Convierte 'u1:p1,u2:p2' -> {'u1':'p1','u2':'p2'}"""
    creds = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        u, p = pair.split(":", 1)
        u, p = u.strip(), p.strip()
        if u and p:
            creds[u] = p
    return creds

def _check(user: str, pwd: str) -> bool:
    """Prioridad: mapa credenciales > lista + pass compartida > legacy 1 usuario."""
    # 1) Mapa usuario:contraseña
    if LOGIN_CREDENTIALS.strip():
        creds = _parse_credentials_map(LOGIN_CREDENTIALS)
        return user in creds and pwd == creds[user]
    # 2) Lista de usuarios + password compartida
    if LOGIN_USERS:
        return (user in LOGIN_USERS) and (LOGIN_PASSWORD and pwd == LOGIN_PASSWORD)
    # 3) Legacy single user
    if LOGIN_USER and LOGIN_PASS:
        return (user == LOGIN_USER) and (pwd == LOGIN_PASS)
    # 4) Nada configurado
    return False

def gate() -> bool:
    if not AUTH_ENABLED:
        return True

    if st.session_state.get("auth_ok"):
        with st.sidebar:
            st.success(f"Sesión: {st.session_state.get('auth_user','')}")
            if st.button("Cerrar sesión", use_container_width=True, key="logout_btn"):
                for k in ("auth_ok", "auth_user", "login_user", "login_pass"):
                    st.session_state.pop(k, None)
                st.rerun()
        return True

    st.title("🔐 Iniciar sesión")
    u = st.text_input("Usuario", key="login_user")
    p = st.text_input("Contraseña", type="password", key="login_pass")
    if st.button("Entrar", type="primary"):
        if _check(u, p):
            st.session_state["auth_ok"] = True
            st.session_state["auth_user"] = u
            st.success("Autenticación correcta.")
            st.rerun()
        else:
            st.error("Credenciales inválidas o no configuradas.")
    return False

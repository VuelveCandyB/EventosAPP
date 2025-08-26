# utils/auth.py
import os, streamlit as st

AUTH_ENABLED = os.getenv("AUTH_ENABLED", "1") == "1"
LOGIN_USER   = os.getenv("APP_LOGIN_USER", "admin")
LOGIN_PASS   = os.getenv("APP_LOGIN_PASS", "1234")

def gate() -> bool:
    if not AUTH_ENABLED:
        return True
    if st.session_state.get("auth_ok"):
        with st.sidebar:
            st.success(f"Sesión: {st.session_state.get('auth_user','')}")
            if st.button("Cerrar sesión", use_container_width=True, key="logout_btn"):
                for k in ["auth_ok","auth_user","login_user","login_pass"]:
                    st.session_state.pop(k, None)
                st.rerun()
        return True
    st.title("🔐 Iniciar sesión")
    u = st.text_input("Usuario", key="login_user")
    p = st.text_input("Contraseña", type="password", key="login_pass")
    if st.button("Entrar", type="primary"):
        if LOGIN_USER and LOGIN_PASS and u == LOGIN_USER and p == LOGIN_PASS:
            st.session_state["auth_ok"] = True
            st.session_state["auth_user"] = u
            st.success("Autenticación correcta.")
            st.rerun()
        else:
            st.error("Credenciales inválidas o no configuradas.")
    return False

# Home.py
import streamlit as st
from utils.auth import gate


st.set_page_config(page_title="Inicio", page_icon="🏠", layout="wide")

if not gate():
    st.stop()

st.title("🏠 Inicio")
st.write("Bienvenido al sistema de reservas.")
st.write("Usa el menú de la izquierda para ir a **Reservas**.")

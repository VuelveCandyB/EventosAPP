# Home.py
import streamlit as st
from utils.auth import gate




if not gate():
    st.stop()

st.title("🏇 Inicio")
st.write("Bienvenido al sistema para reservar salones de actividades.")
st.write("Usa el menú de la izquierda para ir a **Reservas**.")

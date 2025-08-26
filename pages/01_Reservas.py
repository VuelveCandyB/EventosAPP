# app.py
# ------------------------------------------------------------
# Reservas con calendario + confirmación/cancelación por link
# WhatsApp Cloud API (flujo "gratis" con enlace wa.me)
# - SQLite local
# - Prevención de choques por sala
# - Cantidad de personas, organizador y teléfono (E.164)
# - Capacidad por sala (tabla rooms) con UI de edición
# - Recordatorios 24h (Cloud API o enlaces wa.me)
# - Campos extra: notas, sillas (tipo/cant), mesas (tipo/cant)
# - Migración automática de columnas si tu DB es antigua
# - Links de Confirmar/Cancelar por query param
# - Editor de reservas + reset seguro del formulario
# Requisitos: pip install streamlit streamlit-calendar pandas requests
# ------------------------------------------------------------

import os
import re
import sqlite3
import requests
from uuid import uuid4
from datetime import datetime, time, timedelta
from urllib.parse import urlencode, quote_plus
from pathlib import Path

import pandas as pd
import streamlit as st



# ======================= PAGE CONFIG (temprano) =======================
APP_DIR = Path(__file__).resolve().parent
ENV_LOGO = os.getenv("APP_LOGO_PATH", "").strip()
DEFAULT_LOGO = APP_DIR / "img" / "logo.png"


def resolve_logo_path():
    if ENV_LOGO:
        p = Path(ENV_LOGO)
        if not p.is_absolute():
            p = (APP_DIR / ENV_LOGO).resolve()
        return p
    return DEFAULT_LOGO

LOGO_PATH = resolve_logo_path()
PAGE_ICON = str(LOGO_PATH) if LOGO_PATH.exists() else "📅"

st.set_page_config(page_title="Calendario de Eventos", layout="wide", page_icon=PAGE_ICON)

# ======================= AUTH (fallback si falta utils.auth) =======================
try:
    from utils.auth import gate  # type: ignore
except Exception:
    # Fallback ultra-simple: si defines APP_AUTH_PASSWORD, pide password en sidebar.
    def gate():
        pwd = os.getenv("APP_AUTH_PASSWORD", "").strip()
        if not pwd:
            st.info("🔓 Autenticación desactivada (no se encontró utils.auth ni APP_AUTH_PASSWORD).")
            return True
        st.sidebar.markdown("### 🔐 Acceso")
        typed = st.sidebar.text_input("Password", type="password")
        if not typed:
            st.stop()
        if typed != pwd:
            st.sidebar.error("Contraseña incorrecta.")
            st.stop()
        return True

# Ejecuta auth
if not gate():
    st.stop()

# ======================= CALENDARIO (componente) =======================
CAL_AVAILABLE = True
try:
    from streamlit_calendar import calendar
except ModuleNotFoundError:
    CAL_AVAILABLE = False

# ======================= CONFIG BASE =======================
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://192.168.17.103:8501")
ROOMS = [
    "Glass Room 1",
    "Glass Room 2",
    "Glass Room 3",
    "Glass Room 4",
    "Winners",
    "Ballito Area"
]
ROOM_CAPACITY_DEFAULTS = {
    "Glass Room 1": 30,
    "Glass Room 2": 30,
    "Glass Room 3": 30,
    "Glass Room 4": 60,
    "Winners": 500,
    "Ballito Area": 1000,
}

# Tipos sugeridos (puedes editar)
CHAIR_TYPES = ["(Ninguna)", "Tiffany", "Plegable", "Banquetera", "Auditorio", "Otro"]
TABLE_TYPES = ["(Ninguna)", 'Redonda 60"', 'Redonda 72"', "Rectangular 6ft", "Rectangular 8ft", "Cocktail", "Otro"]

# ======================= INIT STATE MÍNIMO =======================
# Evita KeyError si algún bloque lee flags de estado muy temprano
st.session_state.setdefault("_reset_form", False)
st.session_state.setdefault("new_initialized", False)

# ======================= UTILIDADES =======================

def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %I:%M %p")  # 12h legible


def fmt_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def is_valid_e164(phone: str) -> bool:
    return bool(re.fullmatch(r"\+\d{8,15}", (phone or "").strip()))


def to_wa_me_number(phone_e164: str) -> str:
    return re.sub(r"\D", "", phone_e164 or "")


def get_params() -> dict:
    try:
        return dict(st.query_params)
    except Exception:
        return dict(st.experimental_get_query_params())


def clear_params():
    try:
        st.query_params.clear()
    except Exception:
        try:
            st.experimental_set_query_params(**{})
        except Exception:
            pass


def build_confirm_cancel_urls(token: str) -> tuple[str, str]:
    confirm_url = f"{APP_BASE_URL}/?{urlencode({'confirm': token})}"
    cancel_url = f"{APP_BASE_URL}/?{urlencode({'cancel': token})}"
    return confirm_url, cancel_url


def build_whatsapp_cta(phone_e164, room, title, start_dt, end_dt, attendees, token):
    confirm_url, cancel_url = build_confirm_cancel_urls(token)
    msg = (
        "🏇Hola, quiero confirmar la reserva:\n"
        f"• Sala: {room}\n"
        f"• Evento: {title}\n"
        f"• Inicio: {fmt_dt(start_dt)}\n"
        f"• Fin: {fmt_dt(end_dt)}\n"
        f"• Personas: {attendees}\n"
        f"• Confirmar: {confirm_url}\n"
        f"• Cancelar: {cancel_url}\n"
        f"• Código: {token}"
    )
    num_for_wa = to_wa_me_number(phone_e164)
    return f"https://wa.me/{num_for_wa}?{urlencode({'text': msg}, quote_via=quote_plus)}"


def send_whatsapp_cloud_reply(to_phone_e164, text_message):
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    token = os.environ.get("WHATSAPP_TOKEN")
    if not phone_id or not token:
        raise RuntimeError("Faltan variables WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_TOKEN.")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": to_phone_e164.replace(" ", ""),
        "type": "text",
        "text": {"body": text_message[:4096]},
    }
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    r = requests.post(url, headers=headers, json=data, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Cloud API error {r.status_code}: {r.text}")


# ======= Helpers para reset seguro del formulario =======

def _new_defaults():
    return {
        "new_room": ROOMS[0],
        "new_title": "",
        "new_org": "",
        "new_start_date": datetime.now().date(),
        "new_start_time": time(9, 0),
        "new_end_date": datetime.now().date(),
        "new_end_time": time(17, 0),
        "new_attendees": 0,
        "new_color": "#3b82f6",
        "new_phone": "",
        "new_notes": "",
        "new_chair_type": CHAIR_TYPES[0],
        "new_chair_qty": 0,
        "new_table_type": TABLE_TYPES[0],
        "new_table_qty": 0,
    }


def bootstrap_new_form_state():
    if "new_initialized" not in st.session_state or not st.session_state["new_initialized"]:
        st.session_state.update(_new_defaults())
        st.session_state["new_initialized"] = True
    if st.session_state.get("_reset_form", False):
        st.session_state.update(_new_defaults())
        st.session_state["_reset_form"] = False
        # Inicialización


def request_new_form_reset_and_rerun():
    st.session_state["_reset_form"] = True
    st.rerun()


# ======================= DB + MIGRACIÓN =======================
@st.cache_resource
def get_conn():
    
    APP_DIR = Path(__file__).resolve().parent   # carpeta donde está app.py
    DATA_DIR = APP_DIR / "data"
    DATA_DIR.mkdir(exist_ok=True)

    # Ahora sí guarda la DB dentro de la carpeta /data
    db_path = DATA_DIR / "bookings.db"

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # Tabla principal (mínimo)
    conn.execute(
        """
    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT NOT NULL,
        title TEXT NOT NULL,
        organizador TEXT NOT NULL,
        start_dt TEXT NOT NULL,
        end_dt TEXT NOT NULL,
        notes TEXT,
        chair_type TEXT,
        chair_qty INTEGER,
        table_type TEXT,
        table_qty INTEGER
    )
    """
    )
    # Tabla rooms (capacidad por sala)
    conn.execute(
        """
    CREATE TABLE IF NOT EXISTS rooms (
        room TEXT PRIMARY KEY,
        type TEXT,
        capacity INTEGER
    )
    """
    )
    migrate_schema(conn)
    ensure_rooms_seed(conn)
    return conn


def migrate_schema(conn: sqlite3.Connection):
    cur = conn.execute("PRAGMA table_info(bookings)")
    cols = {row[1] for row in cur.fetchall()}

    # base + extras
    if "notes" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN notes TEXT")
    if "color" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN color TEXT")
    if "organizador" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN organizador TEXT")
    if "attendees" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN attendees INTEGER DEFAULT 0")
        conn.execute("UPDATE bookings SET attendees = 0 WHERE attendees IS NULL")
    if "phone" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN phone TEXT")
    if "status" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN status TEXT DEFAULT 'Pendiente'")
        conn.execute("UPDATE bookings SET status = 'Pendiente' WHERE status IS NULL OR status = ''")
    if "confirm_token" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN confirm_token TEXT")
    # Recordatorios 24h
    if "reminder_24h_sent" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN reminder_24h_sent INTEGER DEFAULT 0")
    if "reminder_24h_sent_at" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN reminder_24h_sent_at TEXT")
    # Sillas/Mesas
    if "chair_type" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN chair_type TEXT")
    if "chair_qty" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN chair_qty INTEGER DEFAULT 0")
    if "table_type" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN table_type TEXT")
    if "table_qty" not in cols:
        conn.execute("ALTER TABLE bookings ADD COLUMN table_qty INTEGER DEFAULT 0")

    conn.commit()


def ensure_rooms_seed(conn: sqlite3.Connection):
    # Defaults por prefijo si no hay override exacto
    default_by_prefix = {
        "Glass Room": ("Sala Acristalada", 12),
        "Winners": ("Salón Conferencias", 60),
        "Ballito": ("Área Abierta", 100),
    }

    for r in ROOMS:
        cur = conn.execute("SELECT room, type, capacity FROM rooms WHERE room = ?", (r,))
        row = cur.fetchone()

        if not row:
            # 1) Override exacto por nombre
            if r in ROOM_CAPACITY_DEFAULTS:
                tipo = None
                cap = ROOM_CAPACITY_DEFAULTS[r]
            else:
                # 2) Fallback por prefijo
                tipo, cap = None, 30
                for pref, (t, c) in default_by_prefix.items():
                    if r.startswith(pref):
                        tipo, cap = t, c
                        break
            conn.execute("INSERT INTO rooms(room, type, capacity) VALUES(?,?,?)", (r, tipo, cap))
        else:
            # Si ya existe y capacity es NULL, completa
            _, tipo, cap_actual = row
            if cap_actual is None:
                if r in ROOM_CAPACITY_DEFAULTS:
                    cap = ROOM_CAPACITY_DEFAULTS[r]
                else:
                    cap = 30
                    for pref, (_t, _c) in default_by_prefix.items():
                        if r.startswith(pref):
                            cap = _c
                            break
                conn.execute("UPDATE rooms SET capacity=? WHERE room=?", (cap, r))
    conn.commit()


def get_room_capacity(conn, room: str) -> int | None:
    cur = conn.execute("SELECT capacity FROM rooms WHERE room = ?", (room,))
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def read_rooms(conn) -> pd.DataFrame:
    return pd.read_sql_query("SELECT room, type, capacity FROM rooms ORDER BY room", conn)


def save_rooms(conn, df_rooms: pd.DataFrame):
    for _, row in df_rooms.iterrows():
        conn.execute(
            "UPDATE rooms SET type=?, capacity=? WHERE room=?",
            (row["type"], int(row["capacity"]) if pd.notna(row["capacity"]) else None, row["room"]),
        )
    conn.commit()


def read_bookings(conn, room_filter=None, date_from=None, date_to=None):
    q = (
        "SELECT id, room, title, organizador, start_dt, end_dt, color, "
        "attendees, phone, status, confirm_token, reminder_24h_sent, reminder_24h_sent_at, "
        "notes, chair_type, chair_qty, table_type, table_qty "
        "FROM bookings"
    )
    conds, params = [], []
    if room_filter:
        conds.append("room = ?"); params.append(room_filter)
    if date_from:
        conds.append("datetime(end_dt) >= datetime(?)"); params.append(fmt_iso(datetime.combine(date_from, time())))
    if date_to:
        conds.append("datetime(start_dt) <= datetime(?)"); params.append(fmt_iso(datetime.combine(date_to, time(23, 59, 59))))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY start_dt"
    df = pd.read_sql_query(q, conn, params=params)
    return df


def has_overlap(conn, room, start_dt_iso, end_dt_iso, ignore_id=None):
    q = (
        """
    SELECT COUNT(*) FROM bookings
    WHERE room = ?
      AND datetime(?) < datetime(end_dt)
      AND datetime(?) > datetime(start_dt)
    """
    )
    params = [room, start_dt_iso, end_dt_iso]
    if ignore_id is not None:
        q += " AND id != ?"
        params.append(ignore_id)
    cur = conn.execute(q, params)
    return cur.fetchone()[0] > 0


def insert_booking(
    conn,
    room,
    title,
    organizador,
    start_dt_iso,
    end_dt_iso,
    color,
    attendees,
    phone,
    token,
    notes,
    chair_type,
    chair_qty,
    table_type,
    table_qty,
):
    conn.execute(
        "INSERT INTO bookings("
        " room, title, organizador, start_dt, end_dt, color, attendees, phone, status, confirm_token,"
        " notes, chair_type, chair_qty, table_type, table_qty"
        ") VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?)",
        (
            room,
            title,
            organizador,
            start_dt_iso,
            end_dt_iso,
            color,
            attendees,
            phone,
            "Pendiente",
            token,
            notes,
            chair_type,
            chair_qty,
            table_type,
            table_qty,
        ),
    )
    conn.commit()


def update_booking(
    conn,
    booking_id,
    room,
    title,
    organizador,
    start_dt_iso,
    end_dt_iso,
    color,
    attendees,
    phone,
    status,
    notes,
    chair_type,
    chair_qty,
    table_type,
    table_qty,
):
    conn.execute(
        """
        UPDATE bookings
           SET room=?, title=?, organizador=?, start_dt=?, end_dt=?,
               color=?, attendees=?, phone=?, status=?,
               notes=?, chair_type=?, chair_qty=?, table_type=?, table_qty=?
         WHERE id=?
    """,
        (
            room,
            title,
            organizador,
            start_dt_iso,
            end_dt_iso,
            color,
            attendees,
            phone,
            status,
            notes,
            chair_type,
            chair_qty,
            table_type,
            table_qty,
            booking_id,
        ),
    )
    conn.commit()


def delete_booking(conn, booking_id):
    conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    conn.commit()


def update_status_by_token(conn, token, to_status):
    cur = conn.execute("SELECT id, status FROM bookings WHERE confirm_token = ?", (token,))
    row = cur.fetchone()
    if not row:
        return False, None
    current = row[1]
    if current == to_status:
        return True, "already"
    conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (to_status, row[0]))
    conn.commit()
    return True, "updated"


def status_color(row):
    stt = (row.get("status") or "").strip()
    if stt == "Confirmado":
        return "#16a34a"
    if stt == "Cancelado":
        return "#6b7280"
    return row.get("color") or "#f59e0b"


# ======================= PROCESAR QUERY PARAMS =======================
conn = get_conn()
params = get_params()
changed = False
if "confirm" in params:
    token = params.get("confirm")
    if isinstance(token, list):
        token = token[0]
    ok, state = update_status_by_token(conn, token, "Confirmado")
    if ok and state == "updated":
        st.success("✅ Reserva confirmada.")
        changed = True
    elif ok and state == "already":
        st.info("Esta reserva ya estaba confirmada.")
    else:
        st.warning("Token de confirmación inválido.")
elif "cancel" in params:
    token = params.get("cancel")
    if isinstance(token, list):
        token = token[0]
    ok, state = update_status_by_token(conn, token, "Cancelado")
    if ok and state == "updated":
        st.warning("❌ Reserva cancelada.")
        changed = True
    elif ok and state == "already":
        st.info("Esta reserva ya estaba cancelada.")
    else:
        st.warning("Token de cancelación inválido.")
if changed:
    clear_params()

# ======================= HEADER CON LOGO =======================
hc1, hc2 = st.columns([1, 6])
with hc1:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), width=180)
with hc2:
    st.title("📅 Reservas de espacios")
    st.caption("Calendario con prevención de choques, capacidad por sala y recordatorios por WhatsApp.")

# ======================= SIDEBAR =======================
if LOGO_PATH.exists():
    st.sidebar.image(str(LOGO_PATH), use_container_width=True)

st.sidebar.header("Filtros")
room_filter = st.sidebar.selectbox("Salones", ["(Todas)"] + ROOMS)
room_filter = None if room_filter == "(Todas)" else room_filter

# ======================= ADMIN: SALAS Y CAPACIDADES =======================
with st.expander("🛠️ Salas y capacidades (editar)"):
    df_rooms = read_rooms(conn)
    edited = st.data_editor(
        df_rooms,
        num_rows="fixed",
        use_container_width=True,
        column_config={
            "room": st.column_config.TextColumn("Sala", disabled=True),
            "type": st.column_config.TextColumn("Tipo de sala"),
            "capacity": st.column_config.NumberColumn("Capacidad", min_value=0, step=1),
        },
    )
    if st.button("Guardar cambios de salas", use_container_width=True):
        save_rooms(conn, edited)
        st.success("Salas actualizadas.")

# ======================= FORM NUEVA RESERVA =======================
bootstrap_new_form_state()
with st.expander("➕ Crear nueva reservación", expanded=True):
    col1, col2 = st.columns(2)
    with col1:
        
        if "new_room" not in st.session_state:
            st.session_state["new_room"] = ROOMS[0]  # inicializa con el primero

            room = st.selectbox(
                "Salón",
                ROOMS,
                index=ROOMS.index(st.session_state["new_room"]),
                key="new_room"
            )
        cap_vis = get_room_capacity(conn, st.session_state["new_room"])
        if cap_vis is not None:
            st.caption(f"Capacidad máxima de {st.session_state['new_room']}: **{cap_vis}** personas")

        if "new_title" not in st.session_state:
            st.session_state["new_title"] = ""   # o valor por defecto
        
        title = st.text_input(
            "Título del evento",
            value=st.session_state["new_title"],
            placeholder="Boda / Reunión / Cumpleaños / Graduación",
            key="new_title",
        )
        if "new_org" not in st.session_state:
            st.session_state["new_org"] = ""   # o valor por defecto
        organizador = st.text_input("Organizador", value=st.session_state["new_org"], key="new_org")
       
        if "new_start_date" not in st.session_state:
            st.session_state["new_start_date"] = datetime.today()   # o valor por defecto       
        
        start_date = st.date_input("Fecha comienzo", value=st.session_state["new_start_date"], key="new_start_date")
        
        if "new_start_time" not in st.session_state:
            st.session_state["new_start_time"] = datetime.now().time()     
        
        start_time = st.time_input("Hora comienzo", value=st.session_state["new_start_time"], key="new_start_time")
       
        if "new_attendees" not in st.session_state:
            st.session_state["new_attendees"] = 0   # o valor por defecto       
        
        attendees = st.number_input(
            "Cantidad de personas", min_value=0, step=1, value=st.session_state["new_attendees"], key="new_attendees"
        )

        st.markdown("**Sillas**")
        st.selectbox("Tipo de silla", CHAIR_TYPES, key="new_chair_type")
        st.number_input("Cantidad de sillas", min_value=0, step=1, key="new_chair_qty")

    with col2:
        if "new_end_date" not in st.session_state:
            st.session_state["new_end_date"] = datetime.today()   # o valor por defecto       
                
        end_date = st.date_input("Fecha cierre", value=st.session_state["new_end_date"], key="new_end_date")

        if "new_end_time" not in st.session_state:
            st.session_state["new_end_time"] = datetime.now().time()             
        end_time = st.time_input("Hora cierre", value=st.session_state["new_end_time"], key="new_end_time")
        
        if "new_color" not in st.session_state:
            st.session_state["new_color"] = "#16a34a"        
        color = st.color_picker("Color del calendario", value=st.session_state["new_color"], key="new_color")
        
        if "new_phone" not in st.session_state:
            st.session_state["new_phone"] = ""   # o valor por defecto
        phone = st.text_input(
            "WhatsApp de Contacto (E.164)",
            value=st.session_state["new_phone"],
            placeholder="+1787XXXXXXX",
            key="new_phone",
        )
        
        if "new_notes" not in st.session_state:
            st.session_state["new_notes"] = ""   # o valor por defecto        
        st.text_area("Notas (opcional)", value=st.session_state.get("new_notes", ""), key="new_notes")

        st.markdown("**Mesas**")

        if "new_table_type" not in st.session_state:
            st.session_state["new_table_type"] = TABLE_TYPES[0]  # o valor por defecto          
        st.selectbox("Tipo de mesa", TABLE_TYPES, key="new_table_type")
        
        
        st.number_input("Cantidad de mesas", min_value=0, step=1, key="new_table_qty")

    btn = st.button(
        "Guardar (generar enlace de WhatsApp)", type="primary", use_container_width=True, disabled=not title
    )

    if btn:
        start_dt = datetime.combine(st.session_state["new_start_date"], st.session_state["new_start_time"])
        end_dt = datetime.combine(st.session_state["new_end_date"], st.session_state["new_end_time"])
        start_iso, end_iso = start_dt.isoformat(), end_dt.isoformat()

        # Validaciones
        cap = get_room_capacity(conn, st.session_state["new_room"])
        if cap is not None and st.session_state["new_attendees"] > cap:
            st.error(
                f"Capacidad excedida: {st.session_state['new_attendees']} > {cap} para {st.session_state['new_room']}."
            )
        elif end_dt <= start_dt:
            st.error("La hora/fecha de cierre debe ser posterior al inicio.")
        elif has_overlap(conn, st.session_state["new_room"], start_iso, end_iso):
            st.error(
                f"⚠️ Conflicto: ya existe una reservación en **{st.session_state['new_room']}** dentro de ese rango."
            )
        elif st.session_state["new_phone"] and not is_valid_e164(st.session_state["new_phone"]):
            st.error("Teléfono inválido. Usa formato E.164 (ej. +1787XXXXXXX).")
        else:
            if st.session_state["new_chair_qty"] and st.session_state["new_attendees"] > st.session_state["new_chair_qty"]:
                st.warning("Hay más personas que sillas asignadas. Revisa cantidades.")

            token = str(uuid4())
            insert_booking(
                conn,
                st.session_state["new_room"],
                st.session_state["new_title"],
                st.session_state["new_org"],
                start_iso,
                end_iso,
                st.session_state["new_color"],
                st.session_state["new_attendees"],
                st.session_state["new_phone"],
                token,
                st.session_state["new_notes"],
                st.session_state["new_chair_type"],
                int(st.session_state["new_chair_qty"]),
                st.session_state["new_table_type"],
                int(st.session_state["new_table_qty"]),
            )
            st.success("¡Reservación creada correctamente!")
            if st.session_state["new_phone"]:
                try:
                    cta = build_whatsapp_cta(
                        st.session_state["new_phone"],
                        st.session_state["new_room"],
                        st.session_state["new_title"],
                        start_dt,
                        end_dt,
                        st.session_state["new_attendees"],
                        token,
                    )
                    st.info("Comparte este enlace con el cliente para que INICIE el chat en WhatsApp y confirme/cancele:")
                    st.markdown(f"[📲 Abrir WhatsApp con mensaje pre-escrito]({cta})")
                except Exception as e:
                    st.warning(f"No se pudo generar el enlace de WhatsApp: {e}")
            else:
                st.warning("No se ingresó teléfono. No se generó enlace de WhatsApp.")
            # Reset seguro del formulario
            request_new_form_reset_and_rerun()

# ======================= DATOS & CALENDARIO =======================
today = datetime.now().date()
df = read_bookings(
    conn,
    room_filter=room_filter,
    date_from=today - timedelta(days=60),
    date_to=today + timedelta(days=120),
)

st.subheader("Vista Calendario")
if CAL_AVAILABLE:
    events = []
    for _, r in df.iterrows():
        title_txt = f'{r["room"]}: {r["title"]}'
        if pd.notna(r.get("organizador", None)) and str(r["organizador"]).strip():
            title_txt += f' ({r["organizador"]})'
        events.append(
            {
                "id": str(r["id"]),
                "title": title_txt,
                "start": r["start_dt"],
                "end": r["end_dt"],
                "color": status_color(r),
            }
        )

    cal_options = {
        "initialView": "dayGridMonth",
        "headerToolbar": {
            "left": "prev,next today",
            "center": "title",
            "right": "dayGridMonth,timeGridWeek,timeGridDay,listWeek",
        },
        "slotMinTime": "07:00:00",
        "slotMaxTime": "23:00:00",
        "locale": "es",
        "weekNumbers": True,
        "nowIndicator": True,
        "selectable": False,
        "expandRows": True,
        "height": "auto",
    }
    calendar(
        events={"events": events},
        options=cal_options,
        key="calendar",
        custom_css="""
            .fc-event-title { font-weight:600; }
            .fc .fc-col-header-cell-cushion { padding: 6px 4px; }
        """,
    )
else:
    st.error("No se encontró 'streamlit_calendar'. Instala: `pip install streamlit-calendar`")

# ======================= LISTA, ENLACES, ESTADO, BORRAR =======================
with st.expander("📋 Lista de reservaciones (enlaces/estado/borrar)", expanded=False):
    if df.empty:
        st.info("No hay reservaciones registradas.")
    else:
        df_view = df.assign(
            start=lambda x: pd.to_datetime(x["start_dt"]).dt.strftime("%Y-%m-%d %I:%M %p"),
            end=lambda x: pd.to_datetime(x["end_dt"]).dt.strftime("%Y-%m-%d %I:%M %p"),
        )[[
            "id",
            "room",
            "title",
            "organizador",
            "start",
            "end",
            "attendees",
            "phone",
            "chair_type",
            "chair_qty",
            "table_type",
            "table_qty",
            "status",
            "color",
            "notes",
            "confirm_token",
            "reminder_24h_sent",
            "reminder_24h_sent_at",
        ]]
        st.dataframe(df_view.drop(columns=["confirm_token"]), use_container_width=True, hide_index=True)

        c1, c2, c3 = st.columns(3)

        with c1:
            st.markdown("**Generar enlace wa.me**")
            link_id = st.number_input("ID", min_value=0, step=1, value=0, key="link_id")
            if st.button("Crear enlace", use_container_width=True):
                if link_id and (df["id"] == link_id).any():
                    row = df[df["id"] == link_id].iloc[0]
                    if not row["phone"] or not is_valid_e164(row["phone"]):
                        st.warning("La reserva no tiene teléfono válido en E.164.")
                    else:
                        cta = build_whatsapp_cta(
                            row["phone"],
                            row["room"],
                            row["title"],
                            pd.to_datetime(row["start_dt"]).to_pydatetime(),
                            pd.to_datetime(row["end_dt"]).to_pydatetime(),
                            int(row["attendees"] or 0),
                            row["confirm_token"],
                        )
                        st.markdown(f"[📲 Abrir WhatsApp con mensaje pre-escrito]({cta})")
                else:
                    st.warning("ID no encontrado.")

        with c2:
            st.markdown("**Actualizar estado**")
            up_id = st.number_input("ID", min_value=0, step=1, value=0, key="update_id")
            new_status = st.selectbox("Nuevo estado", ["Pendiente", "Confirmado", "Cancelado"], key="new_status")
            if st.button("Aplicar", use_container_width=True):
                if up_id and (df["id"] == up_id).any():
                    conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (new_status, int(up_id)))
                    conn.commit()
                    st.success("Estado actualizado.")
                    st.rerun()
                else:
                    st.warning("ID no encontrado.")

        with c3:
            st.markdown("**Borrar reservación**")
            del_id = st.number_input("ID", min_value=0, step=1, value=0, key="delete_id")
            if st.button("Eliminar", type="secondary", use_container_width=True):
                if del_id and (df["id"] == del_id).any():
                    delete_booking(conn, int(del_id))
                    st.success("Reservación eliminada.")
                    st.rerun()
                else:
                    st.warning("ID no encontrado.")

        # ============== EDITAR RESERVA ==============
        st.markdown("---")
        st.markdown("### ✏️ Editar reservación")
        edit_id = st.number_input("ID a editar", min_value=0, step=1, value=0, key="edit_id")

        if st.button("Cargar reservación", key="btn_load_edit", use_container_width=True):
            if edit_id and (df["id"] == edit_id).any():
                row = df[df["id"] == edit_id].iloc[0]
                st.session_state["e_room"] = row["room"]
                st.session_state["e_title"] = row["title"]
                st.session_state["e_org"] = row.get("organizador", "")
                st.session_state["e_start_date"] = pd.to_datetime(row["start_dt"]).date()
                st.session_state["e_start_time"] = pd.to_datetime(row["start_dt"]).time()
                st.session_state["e_end_date"] = pd.to_datetime(row["end_dt"]).date()
                st.session_state["e_end_time"] = pd.to_datetime(row["end_dt"]).time()
                st.session_state["e_att"] = int(row["attendees"] or 0)
                st.session_state["e_color"] = row["color"] or "#3b82f6"
                st.session_state["e_phone"] = row["phone"] or ""
                st.session_state["e_status"] = row["status"] or "Pendiente"
                st.session_state["e_notes"] = row.get("notes", "") if "notes" in df.columns else ""
                st.session_state["e_chair_type"] = row.get("chair_type") or CHAIR_TYPES[0]
                st.session_state["e_chair_qty"] = int(row.get("chair_qty") or 0)
                st.session_state["e_table_type"] = row.get("table_type") or TABLE_TYPES[0]
                st.session_state["e_table_qty"] = int(row.get("table_qty") or 0)
            else:
                st.warning("ID no encontrado.")

        if "e_room" in st.session_state:
            ec1, ec2 = st.columns(2)
            with ec1:
                e_room = st.selectbox("Sala", ROOMS, index=ROOMS.index(st.session_state["e_room"]), key="e_room")
                e_title = st.text_input("Título", value=st.session_state["e_title"], key="e_title")
                e_org = st.text_input("Organizador", value=st.session_state["e_org"], key="e_org")
                e_start_date = st.date_input("Fecha inicio", value=st.session_state["e_start_date"], key="e_start_date")
                e_start_time = st.time_input("Hora inicio", value=st.session_state["e_start_time"], key="e_start_time")
                st.text_area("Notas (opcional)", value=st.session_state["e_notes"], key="e_notes")
            with ec2:
                e_end_date = st.date_input("Fecha fin", value=st.session_state["e_end_date"], key="e_end_date")
                e_end_time = st.time_input("Hora fin", value=st.session_state["e_end_time"], key="e_end_time")
                e_att = st.number_input("Personas", min_value=0, step=1, value=st.session_state["e_att"], key="e_att")
                e_color = st.color_picker("Color", value=st.session_state["e_color"], key="e_color")
                e_phone = st.text_input("WhatsApp (E.164)", value=st.session_state["e_phone"], key="e_phone")
                e_status = st.selectbox(
                    "Estado",
                    ["Pendiente", "Confirmado", "Cancelado"],
                    index=["Pendiente", "Confirmado", "Cancelado"].index(st.session_state["e_status"]),
                    key="e_status",
                )
                st.markdown("**Sillas / Mesas**")
                st.selectbox("Tipo de silla", CHAIR_TYPES, key="e_chair_type")
                st.number_input("Cantidad de sillas", min_value=0, step=1, key="e_chair_qty")
                st.selectbox("Tipo de mesa", TABLE_TYPES, key="e_table_type")
                st.number_input("Cantidad de mesas", min_value=0, step=1, key="e_table_qty")

            if st.button("Guardar cambios", type="primary", use_container_width=True):
                sdt = datetime.combine(st.session_state["e_start_date"], st.session_state["e_start_time"])
                edt = datetime.combine(st.session_state["e_end_date"], st.session_state["e_end_time"])
                cap2 = get_room_capacity(conn, st.session_state["e_room"])
                if cap2 is not None and int(st.session_state["e_att"]) > cap2:
                    st.error(
                        f"Capacidad excedida: {int(st.session_state['e_att'])} > {cap2} para {st.session_state['e_room']}."
                    )
                elif edt <= sdt:
                    st.error("La hora/fecha de fin debe ser posterior al inicio.")
                elif not is_valid_e164(st.session_state["e_phone"]) and st.session_state["e_phone"]:
                    st.error("Teléfono inválido. Usa formato E.164 (ej. +1787XXXXXXX).")
                elif has_overlap(conn, st.session_state["e_room"], sdt.isoformat(), edt.isoformat(), ignore_id=int(edit_id)):
                    st.error("⚠️ Conflicto: ya existe una reserva en esa sala dentro de ese rango.")
                else:
                    if st.session_state["e_chair_qty"] and int(st.session_state["e_att"]) > int(st.session_state["e_chair_qty"]):
                        st.warning("Hay más personas que sillas asignadas. Revisa cantidades.")
                    update_booking(
                        conn,
                        int(edit_id),
                        st.session_state["e_room"],
                        st.session_state["e_title"],
                        st.session_state["e_org"],
                        sdt.isoformat(),
                        edt.isoformat(),
                        st.session_state["e_color"],
                        int(st.session_state["e_att"]),
                        st.session_state["e_phone"],
                        st.session_state["e_status"],
                        st.session_state["e_notes"],
                        st.session_state["e_chair_type"],
                        int(st.session_state["e_chair_qty"]),
                        st.session_state["e_table_type"],
                        int(st.session_state["e_table_qty"]),
                    )
                    st.success("Reservación actualizada.")
                    for k in list(st.session_state.keys()):
                        if k.startswith("e_"):
                            del st.session_state[k]
                    st.rerun()

            if st.button("Cancelar edición", use_container_width=True):
                for k in list(st.session_state.keys()):
                    if k.startswith("e_"):
                        del st.session_state[k]
                st.rerun()

# ======================= RECORDATORIOS 24H =======================
with st.expander("🔔 Recordatorios 24 h"):
    st.caption("Envía recordatorios para eventos que empiezan en ~24 horas. Se enviará 1 vez por reserva.")
    lookahead_h = st.number_input("Horas hacia adelante", min_value=1, max_value=72, value=24, step=1)
    now = datetime.now()
    start_win = now + timedelta(hours=lookahead_h - 1)  # ventana tolerante 23-26h
    end_win = now + timedelta(hours=lookahead_h + 2)
    st.write(f"Ventana objetivo: **{fmt_dt(start_win)}** → **{fmt_dt(end_win)}**")

    df_up = pd.read_sql_query(
        """
        SELECT id, room, title, organizador, start_dt, end_dt, attendees, phone, status, reminder_24h_sent
          FROM bookings
         WHERE phone IS NOT NULL AND phone != ''
           AND datetime(start_dt) BETWEEN datetime(?) AND datetime(?)
           AND (status = 'Confirmado' OR status = 'Pendiente')
        """,
        conn,
        params=(fmt_iso(start_win), fmt_iso(end_win)),
    )

    if df_up.empty:
        st.info("No hay reservas en la ventana de recordatorio.")
    else:
        st.dataframe(
            df_up.assign(
                inicio=lambda x: pd.to_datetime(x["start_dt"]).dt.strftime("%Y-%m-%d %I:%M %p"),
                fin=lambda x: pd.to_datetime(x["end_dt"]).dt.strftime("%Y-%m-%d %I:%M %p"),
            )[
                [
                    "id",
                    "room",
                    "title",
                    "organizador",
                    "inicio",
                    "fin",
                    "attendees",
                    "phone",
                    "status",
                    "reminder_24h_sent",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

        colA, colB = st.columns(2)
        with colA:
            if st.button("Enviar por Cloud API (si hay token)", type="primary", use_container_width=True):
                ok_count, fail = 0, []
                for _, r in df_up.iterrows():
                    if int(r["reminder_24h_sent"] or 0) == 1:
                        continue
                    body = (
                        f"🔔 Recordatorio: {r['title']} ({r['organizador']}) en {r['room']}\n"
                        f"Inicio: {fmt_dt(pd.to_datetime(r['start_dt']).to_pydatetime())}\n"
                        f"Personas: {int(r['attendees'] or 0)}\n"
                        f"¡Te esperamos!"
                    )
                    try:
                        send_whatsapp_cloud_reply(r["phone"], body)
                        conn.execute(
                            "UPDATE bookings SET reminder_24h_sent=1, reminder_24h_sent_at=? WHERE id=?",
                            (fmt_iso(datetime.now()), int(r["id"])),
                        )
                        conn.commit()
                        ok_count += 1
                    except Exception as e:
                        fail.append((r["id"], str(e)))
                st.success(f"Recordatorios enviados: {ok_count}")
                if fail:
                    st.warning("Fallidos: " + ", ".join([f"#{i}:{err[:40]}" for i, err in fail]))
                st.rerun()
        with colB:
            st.markdown("**Enlaces manuales (wa.me) si no tienes token:**")
            for _, r in df_up.iterrows():
                if int(r["reminder_24h_sent"] or 0) == 1:
                    continue
                msg = (
                    f"🔔 Recordatorio de tu evento:\n"
                    f"• {r['title']} ({r['organizador']}) en {r['room']}\n"
                    f"• Inicio: {fmt_dt(pd.to_datetime(r['start_dt']).to_pydatetime())}\n"
                    f"• Personas: {int(r['attendees'] or 0)}"
                )
                wa = f"https://wa.me/{to_wa_me_number(r['phone'])}?{urlencode({'text': msg}, quote_via=quote_plus)}"
                st.markdown(f"- #{r['id']} [{r['title']}]({wa})")

# ======================= AYUDA =======================
with st.expander("ℹ️ Ayuda"):
    st.markdown(
        f"""
- **Capacidad por sala:** edita en “🛠️ Salas y capacidades”. La creación/edición valida que `personas` ≤ `capacidad`.
- **Sillas/Mesas:** puedes registrar tipo y cantidad; la app avisa si hay más personas que sillas.
- **Recordatorios 24 h:** si tienes `WHATSAPP_TOKEN` y `WHATSAPP_PHONE_NUMBER_ID`, usa “Enviar por Cloud API”.  
  Sin token, usa los enlaces `wa.me` generados para cada reserva.
- **Flujo gratis:** el cliente inicia chat con `wa.me`. Luego puedes responder **gratis durante 24 h**.
- **APP_BASE_URL:** actualmente `{APP_BASE_URL}`. Ajusta al desplegar.
""")



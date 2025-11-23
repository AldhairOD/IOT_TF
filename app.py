import os
import json
import threading
import queue
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import streamlit as st
import pandas as pd
import plotly.express as px
import paho.mqtt.client as mqtt
from supabase import create_client, Client

# ================== CARGA DE SECRETOS ==================
def get_secret(name: str, default=None):
    # 1) intenta en st.secrets (nube/local)
    if name in st.secrets:
        return st.secrets[name]
    # 2) fallback a variable de entorno (.env o export)
    return os.environ.get(name, default)

SUPABASE_URL      = get_secret("SUPABASE_URL")
SUPABASE_ANON_KEY = get_secret("SUPABASE_ANON_KEY")

MQTT_HOST         = get_secret("MQTT_HOST", "localhost")
MQTT_PORT         = int(get_secret("MQTT_PORT", "1883"))
MQTT_USER         = get_secret("MQTT_USER", "")
MQTT_PASS         = get_secret("MQTT_PASS", "")

TOPIC_TELEMETRY   = get_secret("MQTT_TOPIC_TELEMETRY", "sjl/aire/telemetry")
TOPIC_CMD_ALARM   = get_secret("MQTT_TOPIC_CMD_ALARM", "sjl/aire/cmd/alarm")
TOPIC_CMD_FAN     = get_secret("MQTT_TOPIC_CMD_FAN", "sjl/aire/cmd/fan")
TOPIC_CMD_PWM     = get_secret("MQTT_TOPIC_CMD_FAN_PWM", "sjl/aire/cmd/fan_pwm")
TOPIC_CMD_RECAL   = get_secret("MQTT_TOPIC_CMD_RECAL", "sjl/aire/cmd/recal")
TOPIC_ACK         = get_secret("MQTT_TOPIC_ACK", "sjl/aire/ack")

# ================== SUPABASE CLIENT (con manejo de error) ==================
sb_client: Optional[Client] = None
try:
    if SUPABASE_URL and SUPABASE_ANON_KEY:
        sb_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    else:
        # se llenará en session_state más abajo
        pass
except Exception as e:
    # el mensaje se guardará en session_state luego
    sb_client = None
    init_supabase_error = f"Error creando cliente Supabase: {e}"
else:
    init_supabase_error = None

# ================== MQTT CLIENT ==================
mqtt_client = mqtt.Client()

# ================== CONFIG STREAMLIT ==================
st.set_page_config(
    page_title="SJL Aire - IoT Dashboard",
    page_icon="🌫️",
    layout="wide"
)

# ================== ESTADO GLOBAL ==================
if "rt_queue" not in st.session_state:
    st.session_state.rt_queue = queue.Queue(maxsize=5000)

if "rt_points" not in st.session_state:
    # lista de dicts: {"ts_utc", "device_id", "metric", "value", "alarm_on", "fan_pct", "base_ready"}
    st.session_state.rt_points = []

if "mqtt_started" not in st.session_state:
    st.session_state.mqtt_started = False

if "mqtt_error" not in st.session_state:
    st.session_state.mqtt_error = None

if "supabase_error" not in st.session_state:
    # si hubo error al crear el cliente, lo guardamos aquí
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        st.session_state.supabase_error = "Faltan SUPABASE_URL o SUPABASE_ANON_KEY."
    else:
        st.session_state.supabase_error = init_supabase_error

# ================== MQTT CALLBACKS ==================
def on_mqtt_connect(client, userdata, flags, rc):
    print(f"[MQTT] Conectado ({rc})")
    client.subscribe(TOPIC_TELEMETRY)
    client.subscribe(TOPIC_ACK)
    print(f"[MQTT] Suscrito a {TOPIC_TELEMETRY} y {TOPIC_ACK}")

def on_mqtt_message(client, userdata, msg):
    topic = msg.topic
    raw = msg.payload.decode(errors="ignore")
    # print(f"[MQTT RX] {topic} <- {raw}")

    if topic == TOPIC_ACK:
        # Podrías parsear ACK y mostrar en UI si quieres
        return

    if topic != TOPIC_TELEMETRY:
        return

    ts_utc = datetime.now(timezone.utc)

    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"[MQTT] Error JSON: {e}")
        return

    device_id = data.get("device_id", "esp32-01")

    base_extra = {
        "alarm_on": data.get("alarm_on"),
        "fan_pct": data.get("fan_pct"),
        "base_ready": data.get("base_ready")
    }

    metrics = {
        "co_raw": data.get("co_raw"),
        "voc_raw": data.get("voc_raw"),
        "temp_c": data.get("temp_c"),
        "hum_pct": data.get("hum_pct")
    }

    for metric, value in metrics.items():
        if value is None:
            continue
        try:
            item = {
                "ts_utc": ts_utc.isoformat(),
                "device_id": device_id,
                "metric": metric,
                "value": float(value),
                **base_extra
            }
            st.session_state.rt_queue.put_nowait(item)
        except Exception:
            # Si la cola está llena o hay algún problema, lo ignoramos
            pass

# ================== CONEXIÓN MQTT (con try/except y sin intentos infinitos) ==================
def start_mqtt_if_needed():
    # Si ya está conectado o ya falló antes, no intentamos de nuevo
    if st.session_state.mqtt_started or st.session_state.mqtt_error:
        return

    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        err = f"No se pudo conectar a MQTT ({MQTT_HOST}:{MQTT_PORT}): {e}"
        st.session_state.mqtt_error = err
        # mostramos también aquí por si estamos en este rerun
        st.sidebar.error(err)
        return

    # Hilo de loop
    thread = threading.Thread(target=mqtt_client.loop_forever, daemon=True)
    thread.start()
    st.session_state.mqtt_started = True

# ================== FUNCIONES MQTT COMANDOS ==================
def send_alarm(on: bool):
    msg = "ON" if on else "OFF"
    mqtt_client.publish(TOPIC_CMD_ALARM, msg, qos=0, retain=False)

def send_fan(on: bool):
    msg = "ON" if on else "OFF"
    mqtt_client.publish(TOPIC_CMD_FAN, msg, qos=0, retain=False)

def send_fan_pwm(pct: int):
    pct = max(0, min(100, pct))
    mqtt_client.publish(TOPIC_CMD_PWM, str(pct), qos=0, retain=False)

def send_recal():
    mqtt_client.publish(TOPIC_CMD_RECAL, "NOW", qos=0, retain=False)

# ================== DRAIN COLA RT ==================
def drain_realtime_queue(max_pull: int = 500):
    pulled = 0
    while not st.session_state.rt_queue.empty() and pulled < max_pull:
        item = st.session_state.rt_queue.get()
        st.session_state.rt_points.append(item)
        pulled += 1

    # limitar memoria (mantener ~5000 puntos)
    if len(st.session_state.rt_points) > 5000:
        st.session_state.rt_points = st.session_state.rt_points[-5000:]

# ================== SIDEBAR ==================
with st.sidebar:
    st.markdown("## ⚙️ Configuración")

    start_mqtt_if_needed()

    if st.session_state.mqtt_error:
        st.error(st.session_state.mqtt_error)

    auto_refresh = st.checkbox(
        "Auto-refrescar tiempo real",
        value=True,
        help="Si está activo, la vista se actualiza cada ~2 segundos"
    )
    refresh_secs = st.slider("Intervalo de refresco (s)", 1, 10, 2)

    st.markdown("---")
    st.markdown("### Filtros generales")

    # En este TP probablemente solo uses un device, pero dejo campo
    filter_device = st.text_input("Device ID (vacío = todos)", value="")

    metric_options = ["co_raw", "voc_raw", "temp_c", "hum_pct"]
    filter_metric_rt = st.multiselect(
        "Métricas en tiempo real",
        metric_options,
        default=["temp_c", "hum_pct"]
    )

    st.markdown("---")
    st.markdown("### Histórico")
    hist_hours = st.slider("Ventana histórica (horas)", 1, 72, 24)

    st.markdown("---")
    st.markdown("### Comandos MQTT")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("🔔 Alarm ON"):
            send_alarm(True)
    with c2:
        if st.button("🔕 Alarm OFF"):
            send_alarm(False)

    c3, c4 = st.columns(2)
    with c3:
        if st.button("🌀 Fan ON (100%)"):
            send_fan(True)
    with c4:
        if st.button("🧊 Fan OFF"):
            send_fan(False)

    pwm_val = st.slider("Fan PWM (%)", 0, 100, 40)
    if st.button("Enviar PWM"):
        send_fan_pwm(pwm_val)

    if st.button("♻️ Recalibrar sensores (RECAL)"):
        send_recal()

    st.markdown("---")
    st.caption(
        "MQTT conectado a: "
        f"`{MQTT_HOST}:{MQTT_PORT}`\n\n"
        "Supabase URL: "
        f"`{SUPABASE_URL or 'no configurado'}`"
    )

# ================== HEADER ==================
st.markdown(
    """
    <style>
    .big-title {
        font-size: 2.0rem;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .subtitle {
        font-size: 0.9rem;
        color: #94a3b8;
        margin-bottom: 0.2rem;
    }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown('<div class="big-title">🌫️ SJL Aire – Dashboard IoT</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">Monitoreo de calidad de aire, temperatura, humedad y control de actuadores en tiempo casi real.</div>',
    unsafe_allow_html=True
)
st.markdown("---")

# ================== REALTIME ==================
drain_realtime_queue()

col_rt, col_cards = st.columns([2.5, 1.5])

with col_rt:
    st.subheader("📡 Tiempo real (últimos ~3 minutos)")

    if st.session_state.rt_points:
        df_rt = pd.DataFrame(st.session_state.rt_points)
        df_rt["ts_dt"] = pd.to_datetime(df_rt["ts_utc"], utc=True)
        # filtrar ventana de 3 min
        now_utc = datetime.now(timezone.utc)
        df_rt = df_rt[df_rt["ts_dt"] >= (now_utc - timedelta(minutes=3))]

        if filter_device:
            df_rt = df_rt[df_rt["device_id"] == filter_device]

        if filter_metric_rt:
            df_rt = df_rt[df_rt["metric"].isin(filter_metric_rt)]

        if df_rt.empty:
            st.info("No hay datos en la ventana de tiempo real con estos filtros.")
        else:
            df_rt = df_rt.sort_values("ts_dt")
            df_rt["ts_local"] = df_rt["ts_dt"].dt.tz_convert("America/Lima")

            fig_rt = px.line(
                df_rt,
                x="ts_local",
                y="value",
                color="metric",
                hover_data=["device_id", "alarm_on", "fan_pct"],
                labels={"value": "Valor", "ts_local": "Hora local"}
            )
            fig_rt.update_layout(
                height=380,
                margin=dict(l=10, r=10, t=10, b=10),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="right",
                    x=1
                )
            )
            st.plotly_chart(fig_rt, use_container_width=True, theme="streamlit")
    else:
        st.info("Esperando datos MQTT desde el ESP32...")

with col_cards:
    st.subheader("🔎 Estado actual")

    df_tail = pd.DataFrame(st.session_state.rt_points)
    if not df_tail.empty:
        df_tail["ts_dt"] = pd.to_datetime(df_tail["ts_utc"], utc=True)
        df_tail = df_tail.sort_values("ts_dt", ascending=False)
        last = df_tail.iloc[0]

        ts_local = last["ts_dt"].tz_convert("America/Lima")
        st.markdown(f"**Última actualización:** {ts_local.strftime('%Y-%m-%d %H:%M:%S')}")

        c1, c2 = st.columns(2)
        with c1:
            st.metric(
                "🌡️ Temp (°C)",
                f"{last.get('value'):.1f}" if last["metric"] == "temp_c" else "-"
            )
        with c2:
            # buscar última humedad
            hum_row = df_tail[df_tail["metric"] == "hum_pct"].head(1)
            hum_val = hum_row["value"].iloc[0] if not hum_row.empty else None
            st.metric(
                "💧 Humedad (%)",
                f"{hum_val:.1f}" if hum_val is not None else "-"
            )

        c3, c4 = st.columns(2)
        with c3:
            st.metric("🛑 Alarma", "ON" if bool(last.get("alarm_on")) else "OFF")
        with c4:
            st.metric("🌀 Ventilador (%)", int(last.get("fan_pct") or 0))
    else:
        st.info("Aún no hay lecturas para mostrar resumen.")

st.markdown("---")

# ================== HISTÓRICO DESDE SUPABASE ==================
st.subheader(f"📈 Histórico (últimas {hist_hours} h desde BD)")

if not sb_client:
    msg = st.session_state.supabase_error or "Supabase no está configurado (revise URL y ANON KEY)."
    st.warning(msg)
else:
    try:
        t_from = datetime.now(timezone.utc) - timedelta(hours=hist_hours)
        t_from_iso = t_from.isoformat()

        q = (
            sb_client.table("telemetry")
            .select("ts, device_id, metric, value")
            .gte("ts", t_from_iso)
            .order("ts", desc=False)
        )

        if filter_device:
            q = q.eq("device_id", filter_device)

        data = q.execute().data
        df_hist = pd.DataFrame(data)

        if df_hist.empty:
            st.info("No hay datos históricos en Supabase para este rango/filtros.")
        else:
            # selector de métrica para histórico
            metric_hist = st.multiselect(
                "Métricas a mostrar en el histórico",
                metric_options,
                default=["temp_c", "hum_pct"]
            )
            if metric_hist:
                df_hist = df_hist[df_hist["metric"].isin(metric_hist)]

            df_hist["ts_dt"] = pd.to_datetime(df_hist["ts"], utc=True)
            df_hist = df_hist.sort_values("ts_dt")
            df_hist["ts_local"] = df_hist["ts_dt"].dt.tz_convert("America/Lima")

            fig_hist = px.line(
                df_hist,
                x="ts_local",
                y="value",
                color="metric",
                hover_data=["device_id"],
                labels={"value": "Valor", "ts_local": "Fecha/hora local"}
            )
            fig_hist.update_layout(
                height=380,
                margin=dict(l=10, r=10, t=10, b=10),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="right",
                    x=1
                )
            )
            st.plotly_chart(fig_hist, use_container_width=True, theme="streamlit")

            with st.expander("Ver datos en tabla"):
                st.dataframe(
                    df_hist[["ts_local", "device_id", "metric", "value"]],
                    use_container_width=True,
                    height=260
                )

    except Exception as e:
        st.session_state.supabase_error = str(e)
        st.error(f"Error consultando Supabase: {e}")

# ================== AUTO-REFRESH ==================
#if auto_refresh:
    # pequeño delay para no saturar
    #time.sleep(refresh_secs)
    #st.experimental_rerun()

import os
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

TOPIC_CMD_ALARM   = get_secret("MQTT_TOPIC_CMD_ALARM", "sjl/aire/cmd/alarm")
TOPIC_CMD_FAN     = get_secret("MQTT_TOPIC_CMD_FAN", "sjl/aire/cmd/fan")
TOPIC_CMD_PWM     = get_secret("MQTT_TOPIC_CMD_FAN_PWM", "sjl/aire/cmd/fan_pwm")
TOPIC_CMD_RECAL   = get_secret("MQTT_TOPIC_CMD_RECAL", "sjl/aire/cmd/recal")

# ================== NOMBRES AMIGABLES DE MÉTRICAS ==================
METRIC_LABELS = {
    "temp_c":  "Temperatura (°C)",
    "hum_pct": "Humedad relativa (%)",
    "co_raw":  "CO₂ (ppm)",
    "voc_raw": "Comp. volátiles (VOC)",
}

def metric_label(key: str) -> str:
    return METRIC_LABELS.get(key, key)

BASE_METRICS = list(METRIC_LABELS.keys())

# ================== SUPABASE CLIENT (con manejo de error) ==================
sb_client: Optional[Client] = None
init_supabase_error: Optional[str] = None

try:
    if SUPABASE_URL and SUPABASE_ANON_KEY:
        sb_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    else:
        init_supabase_error = "Faltan SUPABASE_URL o SUPABASE_ANON_KEY."
except Exception as e:
    sb_client = None
    init_supabase_error = f"Error creando cliente Supabase: {e}"

# ================== MQTT CLIENT (SOLO PARA ENVIAR COMANDOS) ==================
mqtt_client = mqtt.Client()

# ================== CONFIG STREAMLIT ==================
st.set_page_config(
    page_title="SJL Aire - IoT Dashboard",
    page_icon="🌫️",
    layout="wide"
)

# ================== ESTADO GLOBAL ==================
if "mqtt_started" not in st.session_state:
    st.session_state.mqtt_started = False

if "mqtt_error" not in st.session_state:
    st.session_state.mqtt_error = None

if "supabase_error" not in st.session_state:
    st.session_state.supabase_error = init_supabase_error

# ================== CONEXIÓN MQTT (comandos) ==================
def start_mqtt_if_needed():
    # Si ya está conectado o falló antes, no intentamos de nuevo
    if st.session_state.mqtt_started or st.session_state.mqtt_error:
        return

    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        err = f"No se pudo conectar a MQTT ({MQTT_HOST}:{MQTT_PORT}): {e}"
        st.session_state.mqtt_error = err
        st.sidebar.error(err)
        return

    mqtt_client.loop_start()
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

# ================== QUERIES A SUPABASE ==================
@st.cache_data(ttl=5)
def load_realtime(from_iso: str, device_id: Optional[str]):
    """
    Datos últimos ~3 minutos.
    """
    q = (
        sb_client.table("telemetry")
        .select("ts, device_id, metric, value")
        .gte("ts", from_iso)
        .order("ts", desc=False)
    )
    if device_id:
        q = q.eq("device_id", device_id)
    return q.execute().data

@st.cache_data(ttl=30)
def load_history(from_iso: str, device_id: Optional[str]):
    """
    Datos históricos.
    """
    q = (
        sb_client.table("telemetry")
        .select("ts, device_id, metric, value")
        .gte("ts", from_iso)
        .order("ts", desc=False)
    )
    if device_id:
        q = q.eq("device_id", device_id)
    return q.execute().data

@st.cache_data(ttl=300)  # 5 minutos
def load_status(from_iso: str, device_id: Optional[str]):
    """
    Datos para 'Estado actual' (se refresca cada 5 min como máximo).
    """
    q = (
        sb_client.table("telemetry")
        .select("ts, device_id, metric, value")
        .gte("ts", from_iso)
        .order("ts", desc=False)
    )
    if device_id:
        q = q.eq("device_id", device_id)
    return q.execute().data

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

    filter_device = st.text_input("Device ID (vacío = todos)", value="")

    filter_metric_rt = st.multiselect(
        "Métricas en tiempo real",
        options=BASE_METRICS,
        default=BASE_METRICS,
        format_func=metric_label
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
    '<div class="subtitle">Monitoreo de CO₂, compuestos volátiles, temperatura y humedad, con control de actuadores en tiempo casi real (lectura desde base de datos).</div>',
    unsafe_allow_html=True
)
st.markdown("---")

# ================== REALTIME DESDE BD ==================
col_rt, col_cards = st.columns([2.5, 1.5])

with col_rt:
    st.subheader("📡 Tiempo real (últimos ~3 minutos) desde BD")

    if not sb_client:
        msg = st.session_state.supabase_error or "Supabase no está configurado (revise URL y ANON KEY)."
        st.warning(msg)
    else:
        now_utc = datetime.now(timezone.utc)
        t_from = now_utc - timedelta(minutes=3)
        t_from_iso = t_from.isoformat()

        try:
            data_rt = load_realtime(t_from_iso, filter_device)
            df_rt = pd.DataFrame(data_rt)

            if df_rt.empty:
                st.info("No hay datos en la ventana de tiempo real con estos filtros.")
            else:
                # filtrar métricas seleccionadas
                if filter_metric_rt:
                    df_rt = df_rt[df_rt["metric"].isin(filter_metric_rt)]

                if df_rt.empty:
                    st.info("No hay datos para las métricas seleccionadas.")
                else:
                    df_rt["ts_dt"] = pd.to_datetime(df_rt["ts"], utc=True)
                    df_rt = df_rt.sort_values("ts_dt")
                    df_rt["ts_local"] = df_rt["ts_dt"].dt.tz_convert("America/Lima")

                    df_rt["metric_name"] = df_rt["metric"].map(metric_label)

                    fig_rt = px.line(
                        df_rt,
                        x="ts_local",
                        y="value",
                        color="metric_name",
                        hover_data=["device_id", "metric"],
                        labels={"value": "Valor", "ts_local": "Hora local", "metric_name": "Métrica"}
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
        except Exception as e:
            st.error(f"Error cargando datos en tiempo real desde Supabase: {e}")
            st.session_state.supabase_error = str(e)

# ================== ESTADO ACTUAL (snapshot de una sola lectura) ==================
with col_cards:
    st.subheader("🔎 Estado actual (última lectura en BD, refresco cada 5 min)")

    if not sb_client:
        msg = st.session_state.supabase_error or "Supabase no está configurado."
        st.warning(msg)
    else:
        try:
            now_utc = datetime.now(timezone.utc)
            t_from = now_utc - timedelta(hours=1)  # buscamos en la última hora
            t_from_iso = t_from.isoformat()

            data_last = load_status(t_from_iso, filter_device)
            df_last = pd.DataFrame(data_last)

            if df_last.empty:
                st.info("Aún no hay lecturas recientes para mostrar resumen.")
            else:
                df_last["ts_dt"] = pd.to_datetime(df_last["ts"], utc=True)

                # Timestamp de la ÚLTIMA lectura
                last_ts = df_last["ts_dt"].max()
                df_snapshot = df_last[df_last["ts_dt"] == last_ts]

                # Diccionario metric -> value con TODAS las métricas de ESA lectura
                last_values = {
                    row["metric"]: row["value"]
                    for _, row in df_snapshot.iterrows()
                }

                last_ts_local = last_ts.tz_convert("America/Lima")
                st.markdown(f"**Última actualización:** {last_ts_local.strftime('%Y-%m-%d %H:%M:%S')}")

                # Extraer cada métrica del diccionario
                temp_val = last_values.get("temp_c")
                hum_val  = last_values.get("hum_pct")
                co_val   = last_values.get("co_raw")
                voc_val  = last_values.get("voc_raw")

                c1, c2 = st.columns(2)
                with c1:
                    st.metric(
                        "🌡️ Temperatura (°C)",
                        f"{temp_val:.1f}" if temp_val is not None else "-"
                    )
                with c2:
                    st.metric(
                        "💧 Humedad (%)",
                        f"{hum_val:.1f}" if hum_val is not None else "-"
                    )

                c3, c4 = st.columns(2)
                with c3:
                    st.metric(
                        "🧪 CO₂ (ppm)",
                        f"{co_val:.0f}" if co_val is not None else "-"
                    )
                with c4:
                    st.metric(
                        "☁️ VOC",
                        f"{voc_val:.0f}" if voc_val is not None else "-"
                    )

        except Exception as e:
            st.error(f"Error cargando estado actual desde Supabase: {e}")
            st.session_state.supabase_error = str(e)

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

        data_hist = load_history(t_from_iso, filter_device)
        df_hist = pd.DataFrame(data_hist)

        if df_hist.empty:
            st.info("No hay datos históricos en Supabase para este rango/filtros.")
        else:
            metric_hist = st.multiselect(
                "Métricas a mostrar en el histórico",
                options=BASE_METRICS,
                default=["temp_c", "hum_pct"],
                format_func=metric_label
            )
            if metric_hist:
                df_hist = df_hist[df_hist["metric"].isin(metric_hist)]

            if df_hist.empty:
                st.info("No hay datos para las métricas seleccionadas en este rango.")
            else:
                df_hist["ts_dt"] = pd.to_datetime(df_hist["ts"], utc=True)
                df_hist = df_hist.sort_values("ts_dt")
                df_hist["ts_local"] = df_hist["ts_dt"].dt.tz_convert("America/Lima")
                df_hist["metric_name"] = df_hist["metric"].map(metric_label)

                fig_hist = px.line(
                    df_hist,
                    x="ts_local",
                    y="value",
                    color="metric_name",
                    hover_data=["device_id", "metric"],
                    labels={"value": "Valor", "ts_local": "Fecha/hora local", "metric_name": "Métrica"}
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
                        df_hist[["ts_local", "device_id", "metric_name", "value"]],
                        use_container_width=True,
                        height=260
                    )

    except Exception as e:
        st.session_state.supabase_error = str(e)
        st.error(f"Error consultando Supabase: {e}")

# ================== AUTO-REFRESH ==================
if auto_refresh and hasattr(st, "autorefresh"):
    st.autorefresh(interval=refresh_secs * 1000, key="rt_autorefresh")

import os
from datetime import datetime, timedelta, timezone
from typing import Optional
import time   
import streamlit as st
import pandas as pd
import plotly.express as px
import requests
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

# URL de tu backend bridge (FastAPI/Flask) que realmente envía a MQTT
BRIDGE_URL        = get_secret("BRIDGE_URL", "http://127.0.0.1:8000/cmd")

# ================== NOMBRES AMIGABLES DE MÉTRICAS ==================
METRIC_LABELS = {
    "temp_c":  "Temperatura (°C)",
    "hum_pct": "Humedad relativa (%)",
    "co_raw":  "CO₂ (ppm)",
    "voc_raw": "Compuestos volátiles (VOC)",
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
        init_supabase_error = "Faltan credenciales para el almacenamiento de datos."
except Exception as e:
    sb_client = None
    init_supabase_error = f"Ocurrió un problema al conectarse al almacenamiento de datos: {e}"

# ================== CONFIG STREAMLIT ==================
st.set_page_config(
    page_title="SJL Aire - Panel de calidad de aire",
    page_icon="🌫️",
    layout="wide"
)

# ================== ESTADO GLOBAL ==================
if "supabase_error" not in st.session_state:
    st.session_state.supabase_error = init_supabase_error

# ================== FUNCIONES HTTP → BRIDGE MQTT (ENVÍO, SIN VENTANA DEBUG) ==================
def call_bridge(cmd_type: str, value: Optional[str] = None):
    payload = {"type": cmd_type}
    if value is not None:
        payload["value"] = value

    # Solo log a consola (para debug técnico)
    print("===== HTTP → BRIDGE MQTT =====")
    print(f"URL: {BRIDGE_URL}")
    print(f"JSON: {payload}")
    print("================================")

    try:
        resp = requests.post(BRIDGE_URL, json=payload, timeout=5)
        if resp.status_code != 200:
            st.sidebar.error(f"Bridge respondió {resp.status_code}")
        else:
            # Si quieres menos ruido, comenta esta línea
            st.sidebar.success("Comando enviado correctamente.")
    except Exception as e:
        st.sidebar.error(f"Error llamando al bridge: {e}")

def send_alarm(on: bool):
    value = "ON" if on else "OFF"
    call_bridge("alarm", value)

def send_fan(on: bool):
    value = "ON" if on else "OFF"
    call_bridge("fan", value)

def send_fan_pwm(pct: int):
    pct = max(0, min(100, pct))
    call_bridge("fan_pwm", str(pct))

def send_recal():
    call_bridge("recal", None)

# ================== QUERIES A SUPABASE (POR MÉTRICA, SIN CACHE EN RT) ==================

def load_metric_window(metric: str, minutes: int, device_id: Optional[str]):
    """
    Datos últimos N minutos para UNA métrica.
    Sin cache para que siempre lea lo más reciente.
    """
    if not sb_client:
        return []
    now_utc = datetime.now(timezone.utc)
    t_from = now_utc - timedelta(minutes=minutes)
    t_from_iso = t_from.isoformat()

    q = (
        sb_client.table("telemetry")
        .select("ts, device_id, metric, value")
        .eq("metric", metric)
        .gte("ts", t_from_iso)
        .order("ts", desc=False)
    )
    if device_id:
        q = q.eq("device_id", device_id)
    return q.execute().data

def load_last_metric(metric: str, device_id: Optional[str]):
    """
    Último valor para UNA métrica.
    Sin cache para que se actualice bien.
    """
    if not sb_client:
        return None
    q = (
        sb_client.table("telemetry")
        .select("ts, device_id, metric, value")
        .eq("metric", metric)
        .order("ts", desc=True)
        .limit(1)
    )
    if device_id:
        q = q.eq("device_id", device_id)
    data = q.execute().data
    if not data:
        return None
    return data[0]

# Histórico puede quedar cacheado porque no es tan crítico que cambie al segundo
@st.cache_data(ttl=30)
def load_history(from_iso: str, device_id: Optional[str]):
    if not sb_client:
        return []
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

    auto_refresh = st.checkbox(
        "Actualizar automáticamente",
        value=True,
        help="Si está activo, la vista se actualiza sola cada cierto tiempo."
    )
    refresh_secs = st.slider("Intervalo de actualización (segundos)", 5, 30, 10)

    st.markdown("---")
    st.markdown("### Filtros")

    # Permite filtrar por dispositivo, pero NO se muestra en el gráfico
    filter_device = st.text_input("Identificador de dispositivo (opcional)", value="")

    filter_metric_rt = st.multiselect(
        "Variables a mostrar en tiempo real",
        options=BASE_METRICS,
        default=BASE_METRICS,
        format_func=metric_label
    )

    st.markdown("---")
    st.markdown("### Rango histórico")
    hist_hours = st.slider("Ventana histórica (horas)", 1, 72, 24)

    st.markdown("---")
    st.markdown("### Control del sistema")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Activar alarma"):
            send_alarm(True)
    with c2:
        if st.button("Desactivar alarma"):
            send_alarm(False)

    c3, c4 = st.columns(2)
    with c3:
        if st.button("Encender ventilación (100%)"):
            send_fan(True)
    with c4:
        if st.button("Apagar ventilación"):
            send_fan(False)

    pwm_val = st.slider("Velocidad de ventilación (%)", 0, 70, 40)
    if st.button("Aplicar velocidad"):
        send_fan_pwm(pwm_val)

    if st.button("Recalibrar sensores"):
        send_recal()

    st.markdown("---")
    st.caption(
        "Almacenamiento de datos: "
        f"`{'configurado' if SUPABASE_URL else 'no configurado'}`"
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

st.markdown('<div class="big-title">🌫️ SJL Aire – Panel de calidad del aire interior</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">Monitoreo de CO₂, compuestos volátiles, temperatura y humedad, con control de ventilación y alarma.</div>',
    unsafe_allow_html=True
)
st.markdown("---")

# ================== REALTIME DESDE BD (4 QUERIES) ==================
col_rt, col_cards = st.columns([2.5, 1.5])

with col_rt:
    st.subheader("📡 Tiempo real")

    if not sb_client:
        msg = st.session_state.supabase_error or "El almacenamiento de datos no está configurado."
        st.warning(msg)
    else:
        dfs = []
        for metric in filter_metric_rt:
            data_m = load_metric_window(metric, minutes=3, device_id=filter_device or None)
            df_m = pd.DataFrame(data_m)
            if not df_m.empty:
                df_m["metric"] = metric
                dfs.append(df_m)

        if not dfs:
            st.info("No hay datos recientes para las variables seleccionadas.")
        else:
            df_rt = pd.concat(dfs, ignore_index=True)

            df_rt["ts_dt"] = pd.to_datetime(df_rt["ts"], utc=True)
            df_rt = df_rt.sort_values("ts_dt")
            df_rt["ts_local"] = df_rt["ts_dt"].dt.tz_convert("America/Lima")
            df_rt["metric_name"] = df_rt["metric"].map(metric_label)

            fig_rt = px.line(
                df_rt,
                x="ts_local",
                y="value",
                color="metric_name",
                hover_data={"ts_local": True, "value": True, "metric_name": True},
                labels={
                    "value": "Valor medido",
                    "ts_local": "Hora local",
                    "metric_name": "Variable"
                }
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

# ================== ESTADO ACTUAL (4 SELECTS) ==================
with col_cards:
    st.subheader("🔎 Estado actual")

    if not sb_client:
        msg = st.session_state.supabase_error or "El almacenamiento de datos no está configurado."
        st.warning(msg)
    else:
        last_temp = load_last_metric("temp_c",  filter_device or None)
        last_hum  = load_last_metric("hum_pct", filter_device or None)
        last_co   = load_last_metric("co_raw",  filter_device or None)
        last_voc  = load_last_metric("voc_raw", filter_device or None)

        ts_candidates = [
            pd.to_datetime(x["ts"], utc=True) for x in
            [last_temp, last_hum, last_co, last_voc] if x is not None
        ]

        if not ts_candidates:
            st.info("Aún no hay lecturas recientes para mostrar el estado actual.")
        else:
            last_ts = max(ts_candidates)
            last_ts_local = last_ts.tz_convert("America/Lima")
            st.markdown(f"**Última actualización:** {last_ts_local.strftime('%Y-%m-%d %H:%M:%S')}")

            temp_val = last_temp["value"] if last_temp else None
            hum_val  = last_hum["value"]  if last_hum  else None
            co_val   = last_co["value"]   if last_co   else None
            voc_val  = last_voc["value"]  if last_voc  else None

            c1, c2 = st.columns(2)
            with c1:
                st.metric(
                    "🌡️ Temperatura (°C)",
                    f"{temp_val:.1f}" if temp_val is not None else "–"
                )
            with c2:
                st.metric(
                    "💧 Humedad relativa (%)",
                    f"{hum_val:.1f}" if hum_val is not None else "–"
                )

            c3, c4 = st.columns(2)
            with c3:
                st.metric(
                    "🧪 CO₂ (ppm)",
                    f"{co_val:.0f}" if co_val is not None else "–"
                )
            with c4:
                st.metric(
                    "☁️ Compuestos volátiles (VOC)",
                    f"{voc_val:.0f}" if voc_val is not None else "–"
                )

st.markdown("---")

# ================== HISTÓRICO DESDE SUPABASE ==================
st.subheader(f"📈 Histórico (últimas {hist_hours} horas)")

if not sb_client:
    msg = st.session_state.supabase_error or "El almacenamiento de datos no está configurado."
    st.warning(msg)
else:
    try:
        t_from = datetime.now(timezone.utc) - timedelta(hours=hist_hours)
        t_from_iso = t_from.isoformat()

        data_hist = load_history(t_from_iso, filter_device or None)
        df_hist = pd.DataFrame(data_hist)

        if df_hist.empty:
            st.info("No hay datos históricos para este rango.")
        else:
            metric_hist = st.multiselect(
                "Variables a mostrar en el histórico",
                options=BASE_METRICS,
                default=["temp_c", "hum_pct"],
                format_func=metric_label
            )
            if metric_hist:
                df_hist = df_hist[df_hist["metric"].isin(metric_hist)]

            if df_hist.empty:
                st.info("No hay datos para las variables seleccionadas en este rango.")
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
                    hover_data={"ts_local": True, "value": True, "metric_name": True},
                    labels={
                        "value": "Valor medido",
                        "ts_local": "Fecha y hora",
                        "metric_name": "Variable"
                    }
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
                        df_hist[["ts_local", "metric_name", "value"]],
                        use_container_width=True,
                        height=260
                    )

    except Exception as e:
        st.session_state.supabase_error = str(e)
        st.error(f"Ocurrió un problema al consultar los datos históricos: {e}")

# ================== AUTO-REFRESH ==================
if auto_refresh:
    # Espera la cantidad de segundos elegida
    time.sleep(refresh_secs)
    # Vuelve a ejecutar TODO el script (vuelve a consultar Supabase)
    st.rerun()

import streamlit as st
import duckdb
import plotly.express as px
import pandas as pd

st.set_page_config(layout="wide", page_title="AIS Data Quality & Coverage")

st.title("🌐 AIS Data Lake - Explorateur de Qualité & Couverture")

# 1. Connexion DuckDB et Configuration S3
@st.cache_resource
def get_duckdb_connection():
    con = duckdb.connect(database=':memory:')
    # Activation du protocole HTTPFS pour lire le S3 d'OVH
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("""
        ATTACH 'https://ais-public-prod.s3.gra.io.cloud.ovh.net/ais.ducklake' AS ais 
        (TYPE ducklake, AUTOMATIC_MIGRATION true);
    """)
    return con

con = get_duckdb_connection()

# Barre latérale pour le choix de la date
st.sidebar.header("📅 Paramètres du Data Lake")
date_input = st.sidebar.date_input("Choisir une date", pd.to_datetime("2026-05-27"))
year = date_input.strftime('%Y')
month = date_input.strftime('%m')
day = date_input.strftime('%d')

# 2. Chargement des métriques globales de qualité
@st.cache_data(show_spinner="Calcul des indicateurs de qualité...")
def load_quality_metrics(y, m, d):
    query = f"""
        SELECT 
            COUNT(*) as total_rows,
            COUNT(DISTINCT mmsi) as unique_mmsi,
            SUM(CASE WHEN lat IS NULL OR lon IS NULL THEN 1 ELSE 0 END) as missing_coords,
            SUM(CASE WHEN message_type = 'ShipStaticData' AND name IS NULL THEN 1 ELSE 0 END) as static_without_name,
            SUM(CASE WHEN sog > 102 THEN 1 ELSE 0 END) as aberrant_speeds -- 102.3 nœuds est souvent la valeur "N/A" en AIS
        FROM ais.messages
        WHERE year = '{y}' AND month = '{m}' AND day = '{d}'
    """
    return con.execute(query).df().iloc[0]

# 3. Chargement de la Heatmap mondiale (agrégée au degré pour aller vite)
@st.cache_data(show_spinner="Génération de la carte de couverture mondiale...")
def load_world_coverage(y, m, d):
    query = f"""
        SELECT 
            ROUND(lat, 0) AS latitude, 
            ROUND(lon, 0) AS longitude, 
            COUNT(*) AS density,
            COUNT(DISTINCT message_type) as types_diversite
        FROM ais.messages
        WHERE year = '{y}' AND month = '{m}' AND day = '{d}'
          AND lat IS NOT NULL AND lon IS NOT NULL
        GROUP BY latitude, longitude
    """
    return con.execute(query).df()

# 4. Distribution des types de messages
@st.cache_data(show_spinner="Analyse des types de messages...")
def load_message_types(y, m, d):
    query = f"""
        SELECT message_type, COUNT(*) as count
        FROM ais.messages
        WHERE year = '{y}' AND month = '{m}' AND day = '{d}'
        GROUP BY message_type
        ORDER BY count DESC
    """
    return con.execute(query).df()


# --- EXECUTION ET AFFICHAGE ---
try:
    metrics = load_quality_metrics(year, month, day)
    
    # KPIs de surface
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Messages Totaux", f"{metrics['total_rows']:,}")
    col2.metric("Navires Uniques (MMSI)", f"{metrics['unique_mmsi']:,}")
    
    # Calcul des taux d'erreur
    pct_missing_coords = (metrics['missing_coords'] / metrics['total_rows']) * 100
    col3.metric("Positions Manquantes (Null)", f"{pct_missing_coords:.3f} %", delta="- Bon" if pct_missing_coords < 1 else "- À vérifier", delta_color="inverse")
    
    col4.metric("Vitesses Aberrantes (>102 kn)", f"{metrics['aberrant_speeds']:,}")

    st.markdown("---")

    # SECTION 1 : LA CARTE DE COUVERTURE VERITABLE
    st.subheader("🗺️ Couverture géographique réelle (Heatmap)")
    df_map = load_world_coverage(year, month, day)
    
    if not df_map.empty:
        fig_map = px.density_mapbox(
            df_map, 
            lat='latitude', 
            lon='longitude', 
            z='density', 
            radius=10,
            center=dict(lat=20, lon=0), 
            zoom=1,
            mapbox_style="carto-positron",
            title="Densité des messages reçus au degré carré",
            color_continuous_scale=px.colors.sequential.Jet,
            height=600
        )
        st.plotly_chart(fig_map, use_container_width=True)
        st.info("💡 Si la carte ne montre que l'Europe, c'est que ton pipeline de collecte filtre géographiquement les données en amont, ou que le flux de ce jour-là était partiel.")
    else:
        st.warning("Aucune donnée géographique pour cette journée.")

    st.markdown("---")

    # SECTION 2 : REPARTITION ET INTEGRITE DES MESSAGES
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("📊 Volumétrie par Type de Message")
        df_types = load_message_types(year, month, day)
        fig_pie = px.pie(df_types, values='count', names='message_type', title="Répartition des types AIS", hole=0.4)
        st.plotly_chart(fig_pie, use_container_width=True)

    with col_right:
        st.subheader("🔍 Audit de la qualité des données statiques")
        st.write("Idéalement, chaque émetteur envoie son nom. Regardons la proportion de messages statiques corrompus (sans nom) :")
        
        static_corrupt = metrics['static_without_name']
        st.bar_chart(pd.DataFrame({
            "Statut du message ShipStaticData": ["Valides (Avec Nom)", "Corrompus (Nom NULL)"],
            "Nombre": [df_types[df_types['message_type'] == 'ShipStaticData']['count'].sum() - static_corrupt, static_corrupt]
        }).set_index("Statut du message ShipStaticData"))

except Exception as e:
    st.error(f"Erreur lors de l'accès au Data Lake pour cette date : {e}")
    st.info("Vérifie que la date existe dans ton bucket S3 (dossiers year/month/day).")

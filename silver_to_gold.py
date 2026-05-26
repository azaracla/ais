#!/usr/bin/env python3
"""
Silver → Gold: Chargement en append-only dans DuckLake
- Lit TOUS les Parquet depuis ./silver/
- Charge dans DuckLake (append-only)
"""

import duckdb
import glob
import os


def run_consolidation():
    con = duckdb.connect()
    
    # Créer le schema si inexistant
    con.execute("CREATE SCHEMA IF NOT EXISTS public_lake")
    
    # Nettoyer les tables existantes
    con.execute("DROP TABLE IF EXISTS public_lake.positions")
    con.execute("DROP TABLE IF EXISTS public_lake.ship_static")
    con.execute("DROP TABLE IF EXISTS public_lake.base_stations")
    con.execute("DROP TABLE IF EXISTS public_lake.aids_to_navigation")
    con.execute("DROP TABLE IF EXISTS public_lake.unstructured_messages")
    
    # Trouver tous les fichiers Parquet
    parquet_files = glob.glob("./silver/**/*.parquet", recursive=True)
    
    if not parquet_files:
        print("❌ Aucun fichier Parquet trouvé dans ./silver/")
        return
    
    print(f"📥 {len(parquet_files)} fichiers Parquet trouvés")
    
    # 1. Positions
    print("⏳ Chargement des positions...")
    con.execute("""
        CREATE TABLE public_lake.positions (
            message_type VARCHAR,
            metadata VARCHAR,
            message VARCHAR,
            received_at TIMESTAMP,
            listener_id VARCHAR
        )
    """)
    
    for f in parquet_files:
        con.execute(f"""
            INSERT INTO public_lake.positions
            SELECT message_type, metadata, message, received_at, listener_id 
            FROM read_parquet('{f}')
            WHERE message_type IN ('PositionReport', 'ExtendedClassBPositionReport', 
                                   'StandardClassBPositionReport', 'LongRangeAisBroadcast')
        """)
    
    # 2. Ship static
    print("⏳ Chargement des données statiques...")
    con.execute("""
        CREATE TABLE public_lake.ship_static (
            message_type VARCHAR,
            metadata VARCHAR,
            message VARCHAR,
            received_at TIMESTAMP,
            listener_id VARCHAR
        )
    """)
    
    for f in parquet_files:
        con.execute(f"""
            INSERT INTO public_lake.ship_static
            SELECT message_type, metadata, message, received_at, listener_id 
            FROM read_parquet('{f}')
            WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
        """)
    
    # 3. Base stations
    print("⏳ Chargement des stations de base...")
    con.execute("""
        CREATE TABLE public_lake.base_stations (
            message_type VARCHAR,
            metadata VARCHAR,
            message VARCHAR,
            received_at TIMESTAMP,
            listener_id VARCHAR
        )
    """)
    
    for f in parquet_files:
        con.execute(f"""
            INSERT INTO public_lake.base_stations
            SELECT message_type, metadata, message, received_at, listener_id 
            FROM read_parquet('{f}')
            WHERE message_type = 'BaseStationReport'
        """)
    
    # 4. Aids to navigation
    print("⏳ Chargement des ATON...")
    con.execute("""
        CREATE TABLE public_lake.aids_to_navigation (
            message_type VARCHAR,
            metadata VARCHAR,
            message VARCHAR,
            received_at TIMESTAMP,
            listener_id VARCHAR
        )
    """)
    
    for f in parquet_files:
        con.execute(f"""
            INSERT INTO public_lake.aids_to_navigation
            SELECT message_type, metadata, message, received_at, listener_id 
            FROM read_parquet('{f}')
            WHERE message_type = 'AidsToNavigationReport'
        """)
    
    # 5. Unstructured messages
    print("⏳ Chargement des messages restants...")
    con.execute("""
        CREATE TABLE public_lake.unstructured_messages (
            message_type VARCHAR,
            metadata VARCHAR,
            message VARCHAR,
            received_at TIMESTAMP,
            listener_id VARCHAR
        )
    """)
    
    for f in parquet_files:
        con.execute(f"""
            INSERT INTO public_lake.unstructured_messages
            SELECT message_type, metadata, message, received_at, listener_id 
            FROM read_parquet('{f}')
            WHERE message_type NOT IN ('PositionReport', 'ExtendedClassBPositionReport', 
                                       'StandardClassBPositionReport', 'LongRangeAisBroadcast',
                                       'ShipStaticData', 'StaticDataReport', 'BaseStationReport',
                                       'AidsToNavigationReport')
        """)
    
    print(f"✅ Consolidation terminée")
    
    # Stats
    for table in ['positions', 'ship_static', 'base_stations', 'aids_to_navigation', 'unstructured_messages']:
        result = con.execute(f"SELECT COUNT(*) FROM public_lake.{table}").fetchone()
        print(f"  - {table}: {result[0]} records")
    
    con.close()


if __name__ == "__main__":
    run_consolidation()

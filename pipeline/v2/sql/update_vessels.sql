-- Update Vessels Dimension Table for DuckLake v2
-- Upsert static vessel information from the latest static data messages

MERGE INTO ais_lake.vessels AS target
USING (
    SELECT 
        mmsi,
        name,
        call_sign,
        imo_number,
        ship_type,
        length,
        width,
        destination,
        ts AS last_seen_static
    FROM ais_lake.messages
    WHERE message_type IN ('ShipStaticData', 'StaticDataReport')
      AND name IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY mmsi ORDER BY ts DESC) = 1
) AS source
ON target.mmsi = source.mmsi
WHEN MATCHED THEN
    UPDATE SET
        name = source.name,
        call_sign = source.call_sign,
        imo_number = source.imo_number,
        ship_type = source.ship_type,
        length = source.length,
        width = source.width,
        destination = source.destination,
        last_seen_static = source.last_seen_static
WHEN NOT MATCHED THEN
    INSERT (mmsi, name, call_sign, imo_number, ship_type, length, width, destination, last_seen_static)
    VALUES (
        source.mmsi,
        source.name,
        source.call_sign,
        source.imo_number,
        source.ship_type,
        source.length,
        source.width,
        source.destination,
        source.last_seen_static
    );

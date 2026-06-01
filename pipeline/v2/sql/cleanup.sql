-- DuckLake v2 Cleanup SQL
-- Optional cleanup operations

-- Vacuum to optimize storage (DuckLake specific)
VACUUM ais_lake;

-- Analyze to update statistics
ANALYZE ais_lake;

-- Optional: Remove old data (use with caution!)
-- DELETE FROM ais_lake.messages WHERE year < 2024;

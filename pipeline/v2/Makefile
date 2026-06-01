.PHONY: help init consolidate derive vessels full-pipeline full-day save load test query

# Colors
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m

# Include env
include ../../.env

export OVH_REGION
export OVH_ENDPOINT
export OVH_ACCESS_KEY
export OVH_SECRET_KEY
export BUCKET_RAW
export BUCKET_PUBLIC

LOCAL_CATALOG := /tmp/ais.ducklake
DUCKLAKE_URL := ducklake:https://${BUCKET_PUBLIC}.s3.gra.io.cloud.ovh.net/v2/ais.ducklake

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

init: ## Initialize DuckLake v2 catalog
	@echo "$(YELLOW)🔧 Initializing DuckLake v2...$(NC)"
	./run_init.sh

consolidate: ## Consolidate raw NDJSON.zst files (DATE and HOUR required)
	@echo "$(YELLOW)📦 Consolidating data...$(NC)"
	./run_consolidate.sh ${DATE} ${HOUR}

derive: ## Derive gold tables (DATE and HOUR required)
	@echo "$(YELLOW)🏗️ Deriving gold tables...$(NC)"
	./run_derive.sh ${DATE} ${HOUR}

vessels: ## Update vessels dimension table
	@echo "$(YELLOW)🚢 Updating vessels table...$(NC)"
	./run_vessels.sh

full-pipeline: ## Run full pipeline for DATE and HOUR (default: yesterday 00)
	@echo "$(YELLOW)🚀 Running full pipeline...$(NC)"
	./run_full_pipeline.sh ${DATE:-$(shell date -d "yesterday" +%Y-%m-%d)} ${HOUR:-00}

full-day: ## Run full pipeline for all 24 hours of DATE
	@echo "$(YELLOW)📅 Running full day pipeline...$(NC)"
	./run_full_day.sh ${DATE:-$(shell date -d "yesterday" +%Y-%m-%d)}

save: ## Save local catalog to S3 with public-read ACL
	@echo "$(YELLOW)💾 Saving catalog to S3...$(NC)"
	./save_catalog.sh

load: ## Load catalog from S3 to local
	@echo "$(YELLOW)📥 Loading catalog from S3...$(NC)"
	./load_catalog.sh

test: ## Test DuckLake direct access
	@echo "$(YELLOW)🧪 Testing DuckLake access...$(NC)"
	duckdb ${DUCKLAKE_URL} -c "SELECT COUNT(*) FROM messages;"
	duckdb ${DUCKLAKE_URL} -c "SELECT COUNT(*) FROM vessels_positions;"
	duckdb ${DUCKLAKE_URL} -c "SELECT COUNT(*) FROM vessels;"

query: ## Run a SQL query against public DuckLake (QUERY required)
	@echo "$(YELLOW)📊 Running query:$(NC) ${QUERY}"
	duckdb ${DUCKLAKE_URL} -c "${QUERY}"

# Shortcuts
all: init consolidate derive vessels save ## Run init + consolidate + derive + vessels + save
	
clean: ## Remove local catalog
	@echo "$(YELLOW)🧹 Cleaning local catalog...$(NC)"
	rm -f ${LOCAL_CATALOG}

# Example: make DATE=2026-05-29 HOUR=00 full-pipeline
# Example: make DATE=2026-05-29 full-day
# Example: make QUERY="SELECT * FROM messages LIMIT 10" query

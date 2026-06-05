# Variables
SYNAPSE_IMAGE = matrixdotorg/synapse:v1.141.0
SERVER_NAME = localhost
DATA_VOLUME = matrix_synapse_data

.PHONY: all synapse-gen config-yaml compose-up register-admin

# Default target: runs everything in sequential order
all: synapse-gen config-yaml compose-up register-admin

# 1. Generate required cryptographic signing keys
synapse-gen:
	docker run --rm \
		-v $(DATA_VOLUME):/data \
		-e SYNAPSE_SERVER_NAME=$(SERVER_NAME) \
		-e SYNAPSE_REPORT_STATS=no \
		$(SYNAPSE_IMAGE) generate

# 2. Overwrite homeserver.yaml using the template file (WSL Safe)
config-yaml:
	@echo "Writing custom homeserver.yaml to volume..."
	@docker run --rm -i -v $(DATA_VOLUME):/data alpine sh -c "cat > /data/homeserver.yaml" < homeserver.tmpl

# 3. Boot up the services via docker compose
compose-up:
	docker compose up -d

# Register your user
register-admin:
	@echo "Registering admin user..."
	docker exec -it synapse register_new_matrix_user \
		-c /data/homeserver.yaml \
		http://localhost:8008
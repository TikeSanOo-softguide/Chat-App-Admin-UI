# Variables
SYNAPSE_IMAGE = matrixdotorg/synapse:v1.141.0
SERVER_NAME = localhost
DATA_VOLUME = matrix_synapse_data

.PHONY: all synapse-gen up register-admin

# Generate required cryptographic signing keys
synapse-gen:
	docker run --rm \
		-v $(DATA_VOLUME):/data \
		-e SYNAPSE_SERVER_NAME=$(SERVER_NAME) \
		-e SYNAPSE_REPORT_STATS=no \
		$(SYNAPSE_IMAGE) generate

# Register admin
register-admin:
	@echo "Registering admin user..."
	docker exec -it synapse register_new_matrix_user \
		-c /data/homeserver.yaml \
		http://localhost:8008

all: synapse-gen up register-admin

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

clear:
	docker compose down -v

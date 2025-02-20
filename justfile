set dotenv-load := false

# Show all available recipes
default:
  @just --list --unsorted


##########
# Docker #
##########

DOCKER_FILE := "-f docker-compose.yml"

# Bring all Docker services up
up:
    docker-compose {{ DOCKER_FILE }} up -d

# Take all Docker services down
down args="":
    docker-compose {{ DOCKER_FILE }} down {{ args }}

# Show logs of all, or named, Docker services
logs services="":
    docker-compose {{ DOCKER_FILE }} logs -f {{ services }}


########
# Init #
########

# Create .env files from templates
env:
    cp openverse_api/env.template openverse_api/.env
    cp ingestion_server/env.template ingestion_server/.env

# Load sample data into the Docker Compose services
init: up
    ./load_sample_data.sh

# Make a test cURL request to the API
healthcheck:
    curl "http://localhost:8000/v1/images/stats/"


#######
# Dev #
#######

# Install Python dependencies in Pipenv environments
install:
    cd openverse_api && pipenv install --dev
    cd ingestion_server && pipenv install --dev

# Setup pre-commit as a Git hook
precommit:
    cd openverse_api && pipenv run pre-commit install

# Run pre-commit to lint and reformat all files
lint:
    cd openverse_api && pipenv run pre-commit run --all-files


#######
# API #
#######

# Run API tests inside Docker
test: up
    docker-compose exec web ./test/run_test.sh

# Run API tests locally
testlocal:
    cd openverse_api && pipenv run ./test/run_test.sh

# Run Django administrative commands
dj args="":
    cd openverse_api && pipenv run python manage.py {{ args }}

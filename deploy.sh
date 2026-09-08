#!/usr/bin/env bash
# Deploy CandyTest from a published image. This script deliberately never builds images.
set -Eeuo pipefail

PROJECT_NAME="candytest"
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$ROOT_DIR/.env"
DEPLOY_DIR="$ROOT_DIR/.deploy"
BACKUP_DIR="$DEPLOY_DIR/backups"
LOCK_DIR="$DEPLOY_DIR/deploy.lock"
DATA_VOLUME="${PROJECT_NAME}_candytest-data"
DEFAULT_IMAGE="ghcr.io/mashouo/candytest:latest"
cd -- "$ROOT_DIR"

LOCK_HELD=0
ENV_TRANSACTION_ACTIVE=0
ENV_SNAPSHOT_EXISTS=0
ENV_SNAPSHOT_FILE=""

COMMAND="deploy"
REQUESTED_MODE="auto"
MODE_WAS_SET=0
TAG=""
IMAGE=""
IMAGE_WAS_SET=0
NPM_NETWORK_OPTION=""
DOMAIN_OPTION=""
BACKUP_ARGUMENT=""
FOLLOW_LOGS=0
COMPOSE=()
BACKUP_PATH=""
RESOLVED_MODE=""
DETECTED_NPM_NETWORKS=()
NPM_CANDIDATE_IDS=()

PREVIOUS_CONTAINER=""
PREVIOUS_IMAGE_ID=""
PREVIOUS_IMAGE_REF=""
PREVIOUS_MODE=""
PREVIOUS_NETWORK=""
PREVIOUS_DOMAIN=""
PREVIOUS_WAS_RUNNING=0

info() {
    printf '==> %s\n' "$*"
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

error() {
    printf 'ERROR: %s\n' "$*" >&2
}

fatal() {
    error "$*"
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  ./deploy.sh [deploy] [--mode auto|npm|host|caddy] [--tag TAG] [--image IMAGE]
              [--npm-network NAME] [--domain DOMAIN]
  ./deploy.sh status
  ./deploy.sh logs [-f]
  ./deploy.sh backup [OUTPUT.tar.gz]
  ./deploy.sh restore BACKUP.tar.gz
  ./deploy.sh --help

On a first deployment, the default auto mode uses the one usable network of a
running Nginx Proxy Manager container, or falls back to loopback-only host mode
for an existing host reverse proxy. On updates, the last successful
host/npm/caddy mode is retained unless --mode is explicit. CandyTest never
publishes its application port on a public host address. Use --mode caddy
--domain example.com to let the bundled Caddy layer obtain and renew HTTPS
certificates. --image is a complete image reference; --tag expands to
ghcr.io/mashouo/candytest:TAG.
EOF
}

cleanup() {
    local status=$?
    set +e
    if [[ "$ENV_TRANSACTION_ACTIVE" -eq 1 ]]; then
        if ! restore_env_snapshot; then
            error "Could not restore the original .env during cleanup. Manual repair is required."
            status=1
        fi
    fi
    [[ -z "$ENV_SNAPSHOT_FILE" ]] || rm -f -- "$ENV_SNAPSHOT_FILE"
    if [[ "$LOCK_HELD" -eq 1 ]]; then
        rm -rf -- "$LOCK_DIR"
    fi
    trap - EXIT
    exit "$status"
}
trap cleanup EXIT

require_argument() {
    local option=$1
    [[ $# -ge 2 && -n ${2:-} ]] || fatal "$option requires a value."
}

acquire_lock() {
    mkdir -p -- "$DEPLOY_DIR"
    if mkdir -- "$LOCK_DIR" 2>/dev/null; then
        printf '%s\n' "$$" > "$LOCK_DIR/pid"
        LOCK_HELD=1
        return
    fi

    local owner=""
    if [[ -f "$LOCK_DIR/pid" ]]; then
        owner=$(<"$LOCK_DIR/pid")
    fi
    if [[ "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
        warn "Removing stale deployment lock left by process $owner."
        rm -rf -- "$LOCK_DIR"
        mkdir -- "$LOCK_DIR" || fatal "Unable to acquire deployment lock."
        printf '%s\n' "$$" > "$LOCK_DIR/pid"
        LOCK_HELD=1
        return
    fi
    fatal "Another CandyTest deployment operation is running${owner:+ (PID $owner)}."
}

require_docker() {
    command -v docker >/dev/null 2>&1 || fatal "Docker is required but was not found in PATH."
    docker info >/dev/null 2>&1 || fatal "Cannot connect to the Docker daemon."
    docker compose version >/dev/null 2>&1 || fatal "Docker Compose v2 (docker compose) is required."
}

begin_env_transaction() {
    umask 077
    mkdir -p -- "$DEPLOY_DIR"
    ENV_SNAPSHOT_FILE=$(mktemp "$DEPLOY_DIR/.env.before.XXXXXX")
    if [[ -e "$ENV_FILE" ]]; then
        [[ -f "$ENV_FILE" ]] || fatal ".env exists but is not a regular file."
        cp -- "$ENV_FILE" "$ENV_SNAPSHOT_FILE" \
            || fatal "Could not save the existing .env before deployment."
        chmod 600 "$ENV_SNAPSHOT_FILE" \
            || fatal "Could not secure the saved .env before deployment."
        ENV_SNAPSHOT_EXISTS=1
    fi
    ENV_TRANSACTION_ACTIVE=1
}

restore_env_snapshot() {
    local temporary
    [[ "$ENV_TRANSACTION_ACTIVE" -eq 1 ]] || return 0
    if [[ "$ENV_SNAPSHOT_EXISTS" -eq 1 ]]; then
        temporary=$(mktemp "$DEPLOY_DIR/.env.restore.XXXXXX") || return 1
        if ! cp -- "$ENV_SNAPSHOT_FILE" "$temporary"; then
            rm -f -- "$temporary"
            return 1
        fi
        if ! chmod 600 "$temporary"; then
            rm -f -- "$temporary"
            return 1
        fi
        if ! mv -- "$temporary" "$ENV_FILE"; then
            rm -f -- "$temporary"
            return 1
        fi
    else
        rm -f -- "$ENV_FILE" || return 1
    fi
    return 0
}

commit_env_transaction() {
    ENV_TRANSACTION_ACTIVE=0
    [[ -z "$ENV_SNAPSHOT_FILE" ]] || rm -f -- "$ENV_SNAPSHOT_FILE"
    ENV_SNAPSHOT_FILE=""
}

ensure_env_file() {
    umask 077
    mkdir -p -- "$DEPLOY_DIR"
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ -f "$ROOT_DIR/.env.example" ]]; then
            cp -- "$ROOT_DIR/.env.example" "$ENV_FILE"
        else
            : > "$ENV_FILE"
        fi
    fi
    chmod 600 "$ENV_FILE" 2>/dev/null || true
}

read_env_value_from_file() {
    local file=$1 key=$2
    [[ -f "$file" ]] || return 1
    awk -v prefix="${key}=" '
        index($0, prefix) == 1 {
            value = substr($0, length(prefix) + 1)
            sub(/\r$/, "", value)
            found = 1
        }
        END {
            if (found) print value
            else exit 1
        }
    ' "$file"
}

read_env_value() {
    read_env_value_from_file "$ENV_FILE" "$1"
}

read_snapshot_value() {
    [[ "$ENV_SNAPSHOT_EXISTS" -eq 1 ]] || return 1
    read_env_value_from_file "$ENV_SNAPSHOT_FILE" "$1"
}

validate_env_value() {
    local key=$1 value=$2
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *"#"* ]] \
        || fatal "$key contains characters that cannot be safely written to .env."
}

set_env_value_in_file() {
    local file=$1 key=$2 value=$3 temporary
    validate_env_value "$key" "$value"
    [[ -f "$file" ]] || fatal "Cannot update missing environment file: $file"
    temporary=$(mktemp "$DEPLOY_DIR/.env.write.XXXXXX")
    awk -v prefix="${key}=" -v replacement="${key}=${value}" '
        index($0, prefix) == 1 {
            if (!written) {
                print replacement
                written = 1
            }
            next
        }
        { print }
        END {
            if (!written) print replacement
        }
    ' "$file" > "$temporary"
    chmod 600 "$temporary" 2>/dev/null || true
    mv -- "$temporary" "$file"
}

set_env_value() {
    ensure_env_file
    set_env_value_in_file "$ENV_FILE" "$1" "$2"
}

value_or_default() {
    local key=$1 default_value=$2 value
    value=$(read_env_value "$key" 2>/dev/null || true)
    printf '%s' "${value:-$default_value}"
}

set_compose_files() {
    local mode=$1 env_file=${2:-$ENV_FILE}
    COMPOSE=(docker compose --project-name "$PROJECT_NAME" --env-file "$env_file" -f "$ROOT_DIR/compose.yaml")
    case "$mode" in
        host)
            COMPOSE+=(-f "$ROOT_DIR/compose.host.yaml")
            ;;
        npm)
            COMPOSE+=(-f "$ROOT_DIR/compose.npm.yaml")
            ;;
        caddy)
            COMPOSE+=(-f "$ROOT_DIR/compose.caddy.yaml")
            ;;
        *)
            fatal "Unsupported deployment mode: $mode"
            ;;
    esac
}

compose() {
    "${COMPOSE[@]}" "$@"
}

project_container_ids() {
    docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_NAME"
}

find_candytest_container_any() {
    local -a containers=()
    mapfile -t containers < <(docker ps -aq \
        --filter "label=com.docker.compose.project=$PROJECT_NAME" \
        --filter "label=com.docker.compose.service=candytest")
    if [[ ${#containers[@]} -gt 1 ]]; then
        fatal "More than one CandyTest container belongs to project $PROJECT_NAME. Resolve this conflict before deploying."
    fi
    [[ ${#containers[@]} -eq 1 ]] || return 1
    printf '%s\n' "${containers[0]}"
}

show_candytest_logs() {
    if ! compose logs --tail=150 candytest; then
        warn "Could not read CandyTest logs."
    fi
}

health_timeout() {
    local timeout
    timeout=$(value_or_default CANDYTEST_HEALTH_TIMEOUT 120)
    [[ "$timeout" =~ ^[1-9][0-9]*$ ]] || fatal "CANDYTEST_HEALTH_TIMEOUT must be a positive number of seconds."
    printf '%s\n' "$timeout"
}

wait_for_container_healthy() {
    local container_id=$1 timeout start now state health
    timeout=$(health_timeout)
    start=$(date +%s)
    while true; do
        state=$(docker inspect -f '{{.State.Status}}' "$container_id")
        health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")
        case "$health" in
            healthy)
                return 0
                ;;
            unhealthy)
                error "CandyTest reported unhealthy."
                return 1
                ;;
        esac
        case "$state" in
            exited|dead|removing)
                error "CandyTest container entered state: $state"
                return 1
                ;;
        esac
        now=$(date +%s)
        if (( now - start >= timeout )); then
            error "Timed out waiting for CandyTest health."
            return 1
        fi
        sleep 2
    done
}

wait_for_healthy() {
    local timeout start now container_id
    timeout=$(health_timeout)
    start=$(date +%s)
    info "Waiting up to ${timeout}s for CandyTest health."
    while true; do
        container_id=$(find_candytest_container_any || true)
        if [[ -n "$container_id" ]] && wait_for_container_healthy "$container_id"; then
            info "CandyTest is healthy."
            return 0
        fi
        now=$(date +%s)
        if (( now - start >= timeout )); then
            error "Timed out waiting for CandyTest health."
            return 1
        fi
        sleep 2
    done
}

backup_data_from_container() {
    local container_id=$1 destination=$2 destination_dir temporary image_id was_running=0 helper_ok=0
    destination_dir=$(dirname -- "$destination")
    mkdir -p -- "$destination_dir"
    [[ ! -e "$destination" ]] || fatal "Backup destination already exists: $destination"
    image_id=$(docker inspect -f '{{.Image}}' "$container_id")
    [[ -n "$image_id" ]] || fatal "Could not determine the image for CandyTest container $container_id."

    if [[ "$(docker inspect -f '{{.State.Running}}' "$container_id")" == "true" ]]; then
        was_running=1
        info "Stopping CandyTest briefly for a consistent SQLite backup."
        if ! docker stop "$container_id" >/dev/null; then
            error "Could not stop CandyTest for its backup."
            return 1
        fi
    fi

    temporary=$(mktemp "$DEPLOY_DIR/backup.XXXXXX")
    info "Creating a consistent /data backup with the existing image."
    if docker run --rm --network none --volumes-from "${container_id}:ro" \
        --entrypoint tar "$image_id" -C /data -czf - . > "$temporary"; then
        helper_ok=1
    else
        error "The backup helper failed."
    fi

    if [[ "$was_running" -eq 1 ]]; then
        info "Restarting CandyTest after the consistent backup."
        if ! docker start "$container_id" >/dev/null; then
            rm -f -- "$temporary"
            error "CandyTest could not be restarted after backup; refusing to continue."
            return 1
        fi
        if ! wait_for_container_healthy "$container_id"; then
            rm -f -- "$temporary"
            error "CandyTest did not become healthy after backup; refusing to continue."
            return 1
        fi
    fi

    if [[ "$helper_ok" -ne 1 ]]; then
        rm -f -- "$temporary"
        return 1
    fi
    mv -- "$temporary" "$destination"
    chmod 600 "$destination" 2>/dev/null || true
    BACKUP_PATH="$destination"
    info "Data backup created: $destination"
}

backup_data_from_named_volume() {
    local destination=$1 destination_dir temporary image_ref
    destination_dir=$(dirname -- "$destination")
    mkdir -p -- "$destination_dir"
    [[ ! -e "$destination" ]] || fatal "Backup destination already exists: $destination"
    docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1 \
        || { error "CandyTest data volume does not exist: $DATA_VOLUME"; return 1; }
    image_ref=$(value_or_default CANDYTEST_IMAGE "")
    [[ -n "$image_ref" ]] || { error "CANDYTEST_IMAGE is required to read the stopped data volume."; return 1; }
    docker image inspect "$image_ref" >/dev/null 2>&1 \
        || { error "The deployed image is not available locally: $image_ref"; return 1; }

    temporary=$(mktemp "$DEPLOY_DIR/backup.XXXXXX")
    info "Creating a consistent backup from stopped data volume $DATA_VOLUME."
    if ! docker run --rm --network none -v "${DATA_VOLUME}:/data:ro" \
        --entrypoint tar "$image_ref" -C /data -czf - . > "$temporary"; then
        rm -f -- "$temporary"
        error "The backup helper failed."
        return 1
    fi
    mv -- "$temporary" "$destination"
    chmod 600 "$destination" 2>/dev/null || true
    BACKUP_PATH="$destination"
    info "Data backup created: $destination"
}

data_volume_exists() {
    docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1
}

default_backup_path() {
    local base candidate index=0
    mkdir -p -- "$BACKUP_DIR"
    base="$BACKUP_DIR/candytest-$(date -u +%Y%m%dT%H%M%SZ)"
    candidate="${base}.tar.gz"
    while [[ -e "$candidate" ]]; do
        index=$((index + 1))
        candidate="${base}-${index}.tar.gz"
    done
    printf '%s' "$candidate"
}

is_npm_container() {
    local description=${1,,}
    [[ "$description" == *"nginx-proxy-manager"* ||
       "$description" == *"nginx_proxy_manager"* ||
       "$description" == *"nginxproxymanager"* ]]
}

append_detected_network() {
    local candidate=$1 network
    for network in "${DETECTED_NPM_NETWORKS[@]}"; do
        [[ "$network" == "$candidate" ]] && return
    done
    DETECTED_NPM_NETWORKS+=("$candidate")
}

detect_npm_networks() {
    DETECTED_NPM_NETWORKS=()
    NPM_CANDIDATE_IDS=()
    local id image name network
    while read -r id image name; do
        [[ -n "$id" ]] || continue
        if ! is_npm_container "$image $name"; then
            continue
        fi
        NPM_CANDIDATE_IDS+=("$id")
        info "Detected Nginx Proxy Manager: $name ($image)" >&2
        while IFS= read -r network; do
            case "$network" in
                ""|bridge|host|none) continue ;;
            esac
            append_detected_network "$network"
        done < <(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' "$id")
    done < <(docker ps --format '{{.ID}} {{.Image}} {{.Names}}')
}

npm_is_attached_to_network() {
    local requested_network=$1 id network
    for id in "${NPM_CANDIDATE_IDS[@]}"; do
        while IFS= read -r network; do
            [[ "$network" == "$requested_network" ]] && return 0
        done < <(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' "$id")
    done
    return 1
}

validate_npm_network() {
    local network=$1
    [[ -n "$network" ]] || fatal "NPM network is empty. Pass --npm-network NAME."
    case "$network" in
        bridge|host|none) fatal "NPM network must be a user-defined Docker network, not: $network" ;;
    esac
    docker network inspect "$network" >/dev/null 2>&1 \
        || fatal "NPM network does not exist: $network (the script will not create it)."
    detect_npm_networks
    [[ ${#NPM_CANDIDATE_IDS[@]} -gt 0 ]] \
        || fatal "NPM mode requires a running Nginx Proxy Manager container; refusing a configuration that would return 502."
    npm_is_attached_to_network "$network" \
        || fatal "NPM network $network is not attached to a detected Nginx Proxy Manager container; refusing a configuration that would return 502."
}

validate_domain() {
    local domain=$1
    [[ -n "$domain" && "$domain" != *[[:space:]]* ]] \
        || fatal "Caddy mode requires --domain example.com (or CANDYTEST_DOMAIN in .env)."
}

capture_existing_deployment() {
    PREVIOUS_CONTAINER=$(find_candytest_container_any || true)
    [[ -n "$PREVIOUS_CONTAINER" ]] || return 0

    PREVIOUS_IMAGE_ID=$(docker inspect -f '{{.Image}}' "$PREVIOUS_CONTAINER")
    PREVIOUS_IMAGE_REF=$(docker inspect -f '{{.Config.Image}}' "$PREVIOUS_CONTAINER")
    PREVIOUS_MODE=$(read_snapshot_value CANDYTEST_MODE 2>/dev/null || true)
    PREVIOUS_NETWORK=$(read_snapshot_value NPM_NETWORK 2>/dev/null || true)
    PREVIOUS_DOMAIN=$(read_snapshot_value CANDYTEST_DOMAIN 2>/dev/null || true)
    PREVIOUS_WAS_RUNNING=$(docker inspect -f '{{.State.Running}}' "$PREVIOUS_CONTAINER")
    case "$PREVIOUS_MODE" in
        host|npm|caddy) ;;
        *)
            fatal "An existing CandyTest container was found, but .env has no concrete CANDYTEST_MODE. Re-run with an explicit --mode after checking the old deployment."
            ;;
    esac
    [[ -n "$PREVIOUS_IMAGE_ID" && -n "$PREVIOUS_IMAGE_REF" ]] \
        || fatal "Could not capture the prior CandyTest image for a safe rollback."
}

print_connection_details() {
    local mode=$1 network domain port
    case "$mode" in
        npm)
            network=$(value_or_default NPM_NETWORK "")
            cat <<EOF

Nginx Proxy Manager settings
  Detected network: $network
  Scheme: http
  Forward Hostname: candytest
  Forward Port: 8765
EOF
            ;;
        host)
            port=$(value_or_default CANDYTEST_PORT 8765)
            cat <<EOF

Host mode is active (loopback only).
  Local address / reverse-proxy upstream: http://127.0.0.1:$port
External access requires an HTTPS reverse proxy running on the host.
EOF
            ;;
        caddy)
            domain=$(value_or_default CANDYTEST_DOMAIN "")
            cat <<EOF

Caddy mode is active.
  HTTPS address: https://$domain
EOF
            ;;
    esac
}

resolve_deploy_settings() {
    local existing_image existing_network existing_domain selected_mode
    ensure_env_file

    if [[ -n "$TAG" && "$IMAGE_WAS_SET" -eq 1 ]]; then
        fatal "Use either --tag or --image, not both."
    fi
    if [[ -n "$TAG" ]]; then
        IMAGE="ghcr.io/mashouo/candytest:$TAG"
    elif [[ "$IMAGE_WAS_SET" -eq 0 ]]; then
        existing_image=$(read_env_value CANDYTEST_IMAGE 2>/dev/null || true)
        IMAGE=${existing_image:-$DEFAULT_IMAGE}
    fi
    [[ -n "$IMAGE" && "$IMAGE" != *[[:space:]]* ]] || fatal "Image reference must not be empty or contain whitespace."

    existing_network=$(read_env_value NPM_NETWORK 2>/dev/null || true)
    existing_domain=$(read_env_value CANDYTEST_DOMAIN 2>/dev/null || true)

    if [[ -n "$PREVIOUS_CONTAINER" && "$MODE_WAS_SET" -eq 0 ]]; then
        selected_mode=$PREVIOUS_MODE
        info "Existing CandyTest deployment found; retaining its $selected_mode mode." >&2
        case "$selected_mode" in
            npm)
                [[ -z "$NPM_NETWORK_OPTION" || "$NPM_NETWORK_OPTION" == "$PREVIOUS_NETWORK" ]] \
                    || fatal "The prior npm deployment uses $PREVIOUS_NETWORK. Use --mode npm --npm-network NAME to change it deliberately."
                NPM_NETWORK_OPTION=$PREVIOUS_NETWORK
                validate_npm_network "$NPM_NETWORK_OPTION"
                ;;
            caddy)
                [[ -z "$DOMAIN_OPTION" || "$DOMAIN_OPTION" == "$PREVIOUS_DOMAIN" ]] \
                    || fatal "The prior caddy deployment uses $PREVIOUS_DOMAIN. Use --mode caddy --domain DOMAIN to change it deliberately."
                [[ -z "$NPM_NETWORK_OPTION" ]] \
                    || fatal "--npm-network cannot change an existing caddy deployment without --mode npm."
                DOMAIN_OPTION=$PREVIOUS_DOMAIN
                validate_domain "$DOMAIN_OPTION"
                ;;
            host)
                [[ -z "$NPM_NETWORK_OPTION" && -z "$DOMAIN_OPTION" ]] \
                    || fatal "Use an explicit --mode to change an existing host deployment."
                ;;
        esac
    else
        case "$REQUESTED_MODE" in
            auto)
                if [[ -n "$NPM_NETWORK_OPTION" ]]; then
                    selected_mode=npm
                    validate_npm_network "$NPM_NETWORK_OPTION"
                else
                    detect_npm_networks
                    if [[ ${#NPM_CANDIDATE_IDS[@]} -eq 0 ]]; then
                        selected_mode=host
                        info "No running Nginx Proxy Manager container was detected; using loopback-only host mode for a host reverse proxy." >&2
                    elif [[ ${#DETECTED_NPM_NETWORKS[@]} -eq 1 ]]; then
                        selected_mode=npm
                        NPM_NETWORK_OPTION=${DETECTED_NPM_NETWORKS[0]}
                        validate_npm_network "$NPM_NETWORK_OPTION"
                        info "Using detected NPM network: $NPM_NETWORK_OPTION" >&2
                    elif [[ ${#DETECTED_NPM_NETWORKS[@]} -gt 1 ]]; then
                        fatal "Nginx Proxy Manager has multiple usable networks (${DETECTED_NPM_NETWORKS[*]}). Re-run with --npm-network NAME."
                    else
                        fatal "Nginx Proxy Manager was detected but has no usable non-default Docker network. Re-run with --npm-network NAME."
                    fi
                fi
                ;;
            npm)
                selected_mode=npm
                NPM_NETWORK_OPTION=${NPM_NETWORK_OPTION:-$existing_network}
                validate_npm_network "$NPM_NETWORK_OPTION"
                ;;
            host)
                selected_mode=host
                [[ -z "$NPM_NETWORK_OPTION" ]] || fatal "--npm-network is only valid with --mode npm or --mode auto."
                ;;
            caddy)
                selected_mode=caddy
                [[ -z "$NPM_NETWORK_OPTION" ]] || fatal "--npm-network is only valid with --mode npm or --mode auto."
                ;;
            *)
                fatal "--mode must be auto, npm, host, or caddy."
                ;;
        esac

        if [[ "$selected_mode" == caddy ]]; then
            DOMAIN_OPTION=${DOMAIN_OPTION:-$existing_domain}
            validate_domain "$DOMAIN_OPTION"
        elif [[ -n "$DOMAIN_OPTION" ]]; then
            fatal "--domain is only valid with --mode caddy."
        fi
    fi

    set_env_value CANDYTEST_MODE "$selected_mode"
    set_env_value CANDYTEST_IMAGE "$IMAGE"
    if [[ "$selected_mode" == npm ]]; then
        set_env_value NPM_NETWORK "$NPM_NETWORK_OPTION"
    fi
    if [[ "$selected_mode" == caddy ]]; then
        set_env_value CANDYTEST_DOMAIN "$DOMAIN_OPTION"
    fi
    RESOLVED_MODE=$selected_mode
}

stop_project_containers() {
    local -a containers=()
    local container_id failed=0
    mapfile -t containers < <(project_container_ids)
    for container_id in "${containers[@]}"; do
        [[ -n "$container_id" ]] || continue
        if ! docker stop "$container_id" >/dev/null; then
            error "Could not stop failed project container $container_id."
            failed=1
        fi
    done
    [[ "$failed" -eq 0 ]]
}

create_rescue_env() {
    local rescue_tag=$1 rescue_env
    [[ "$ENV_SNAPSHOT_EXISTS" -eq 1 ]] \
        || fatal "Cannot construct a rollback environment because the original .env did not exist."
    rescue_env=$(mktemp "$DEPLOY_DIR/rescue-env.XXXXXX")
    cp -- "$ENV_SNAPSHOT_FILE" "$rescue_env" || { rm -f -- "$rescue_env"; return 1; }
    chmod 600 "$rescue_env" || { rm -f -- "$rescue_env"; return 1; }
    set_env_value_in_file "$rescue_env" CANDYTEST_IMAGE "$rescue_tag"
    printf '%s' "$rescue_env"
}

rollback_to_image() {
    local previous_image_id=$1 previous_image_ref=$2 previous_mode=$3 backup_path=$4 rescue_tag rescue_env
    warn "Deployment failed; restoring the previous $previous_mode configuration before rollback."
    restore_env_snapshot \
        || { error "Could not restore the previous .env before rollback."; return 1; }

    rescue_tag="candytest-rescue:$(date -u +%Y%m%dT%H%M%SZ)-$$"
    if ! docker image tag "$previous_image_id" "$rescue_tag"; then
        error "Could not create a local rescue tag for prior image $previous_image_id ($previous_image_ref)."
        return 1
    fi
    if ! rescue_env=$(create_rescue_env "$rescue_tag"); then
        error "Could not create a temporary rescue environment."
        return 1
    fi
    set_compose_files "$previous_mode" "$rescue_env"
    if ! compose config --quiet; then
        rm -f -- "$rescue_env"
        error "The previous Compose configuration is no longer valid."
        return 1
    fi
    if ! compose up -d --no-build --remove-orphans; then
        rm -f -- "$rescue_env"
        error "The image rollback could not be started. Data backup remains at: $backup_path"
        return 1
    fi
    if ! wait_for_healthy; then
        show_candytest_logs
        rm -f -- "$rescue_env"
        error "The image rollback did not become healthy. Data backup remains at: $backup_path"
        return 1
    fi
    rm -f -- "$rescue_env"
    set_compose_files "$previous_mode"
    warn "Rollback completed with local rescue tag $rescue_tag. Data was not restored automatically; backup remains at: $backup_path"
    return 0
}

handle_deployment_failure() {
    local failed_mode=$1 backup_path=$2
    if [[ -n "$PREVIOUS_CONTAINER" ]]; then
        if rollback_to_image "$PREVIOUS_IMAGE_ID" "$PREVIOUS_IMAGE_REF" "$PREVIOUS_MODE" "$backup_path"; then
            return 1
        fi
        error "Rollback failed. Stopping failed CandyTest services; the data volume is preserved."
    else
        restore_env_snapshot || error "Could not restore the original .env after first-deployment failure."
        error "First deployment failed and there is no previous local image to roll back to. Stopping failed services; the data volume is preserved."
    fi
    if ! stop_project_containers; then
        error "One or more failed containers could not be stopped. Inspect docker ps immediately."
    fi
    [[ -z "$backup_path" ]] || error "Pre-update backup remains at: $backup_path"
    return 1
}

run_deploy() {
    local mode backup_path=""
    acquire_lock
    require_docker
    begin_env_transaction
    capture_existing_deployment
    resolve_deploy_settings
    mode=$RESOLVED_MODE
    set_compose_files "$mode"

    info "Validating Compose configuration for $mode mode."
    if ! compose config --quiet; then
        error "Compose configuration is invalid; the original .env will be restored."
        return 1
    fi

    info "Pulling required images."
    if ! compose pull; then
        if [[ "$IMAGE" == ghcr.io/* ]]; then
            error "Pulling GHCR failed. If this is a private package, authenticate first with: docker login ghcr.io"
        fi
        error "No deployment changes were started; the original .env will be restored."
        return 1
    fi

    if [[ -n "$PREVIOUS_CONTAINER" ]]; then
        backup_path=$(default_backup_path)
        info "Existing CandyTest container found; backing up /data before update."
        if ! backup_data_from_container "$PREVIOUS_CONTAINER" "$backup_path"; then
            error "Automatic data backup failed; deployment was not started."
            return 1
        fi
    fi

    info "Starting CandyTest in $mode mode."
    if ! compose up -d --no-build --remove-orphans; then
        show_candytest_logs
        handle_deployment_failure "$mode" "$backup_path"
        return 1
    fi
    if ! wait_for_healthy; then
        show_candytest_logs
        handle_deployment_failure "$mode" "$backup_path"
        return 1
    fi

    commit_env_transaction
    info "Deployment completed with image $IMAGE."
    [[ -z "$backup_path" ]] || info "Pre-update backup retained at: $backup_path"
    print_connection_details "$mode"
}

run_status() {
    require_docker
    docker ps -a \
        --filter "label=com.docker.compose.project=$PROJECT_NAME" \
        --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
}

run_logs() {
    local container_id
    require_docker
    container_id=$(find_candytest_container_any || true)
    [[ -n "$container_id" ]] || fatal "No CandyTest container was found for project $PROJECT_NAME."
    if [[ "$FOLLOW_LOGS" -eq 1 ]]; then
        docker logs --tail=150 -f "$container_id"
    else
        docker logs --tail=150 "$container_id"
    fi
}

run_backup() {
    local container_id destination
    acquire_lock
    require_docker
    container_id=$(find_candytest_container_any || true)
    if [[ -n "$BACKUP_ARGUMENT" ]]; then
        destination=$BACKUP_ARGUMENT
        [[ "$destination" = /* ]] || destination="$ROOT_DIR/$destination"
    else
        destination=$(default_backup_path)
    fi
    if [[ -n "$container_id" ]]; then
        backup_data_from_container "$container_id" "$destination" \
            || fatal "Backup failed; no deployment changes were made."
    elif data_volume_exists; then
        backup_data_from_named_volume "$destination" \
            || fatal "Backup failed; no deployment changes were made."
    else
        fatal "No CandyTest container or data volume was found to back up."
    fi
}

validate_backup_archive() {
    local archive=$1 entry listing entry_type
    [[ -f "$archive" ]] || fatal "Backup archive was not found: $archive"
    tar -tzf "$archive" >/dev/null || fatal "Backup archive is not a readable tar.gz file: $archive"
    while IFS= read -r entry; do
        case "$entry" in
            /*|..|../*|*/..|*/../*) fatal "Backup archive contains an unsafe path: $entry" ;;
        esac
    done < <(tar -tzf "$archive")
    while IFS= read -r listing; do
        entry_type=${listing:0:1}
        case "$entry_type" in
            -|d) ;;
            *) fatal "Backup archive contains a non-file/non-directory entry and was rejected: $listing" ;;
        esac
    done < <(tar -tzvf "$archive")
}

restore_archive_into_volume() {
    local archive=$1 token stage previous
    token="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    stage=".candytest-restore-staging-$token"
    previous=".candytest-restore-previous-$token"
    info "Extracting archive into hidden staging directory /data/$stage."
    compose run --rm --no-deps -T candytest sh -c '
set -eu
stage="/data/$1"
previous="/data/$2"
cleanup() {
    rm -rf -- "$stage" "$previous"
}
move_contents() {
    source=$1
    target=$2
    find "$source" -mindepth 1 -maxdepth 1 -exec sh -c '\''target=$1; shift; for item do mv -- "$item" "$target"/ || exit 1; done'\'' sh "$target" {} +
}
move_live_data() {
    find /data -mindepth 1 -maxdepth 1 ! -path "$stage" ! -path "$previous" -exec sh -c '\''target=$1; shift; for item do mv -- "$item" "$target"/ || exit 1; done'\'' sh "$previous" {} +
}
remove_partial_live_data() {
    find /data -mindepth 1 -maxdepth 1 ! -path "$stage" ! -path "$previous" -exec rm -rf -- {} +
}
rollback_after_live_data_move_failure() {
    move_contents "$previous" /data
}
rollback_after_stage_move_failure() {
    remove_partial_live_data
    move_contents "$previous" /data
}
trap cleanup EXIT
mkdir -- "$stage" "$previous"
if tar --help 2>&1 | grep -q -- "--no-same-owner" && tar --help 2>&1 | grep -q -- "--no-same-permissions"; then
    tar --no-same-owner --no-same-permissions -xzf - -C "$stage"
else
    tar -xzf - -C "$stage"
fi
if ! move_live_data; then
    rollback_after_live_data_move_failure || exit 1
    exit 1
fi
if ! move_contents "$stage" /data; then
    rollback_after_stage_move_failure || exit 1
    exit 1
fi
rm -rf -- "$stage" "$previous"
trap - EXIT
' sh "$stage" "$previous" < "$archive"
}

restart_original_service_after_restore_failure() {
    local mode=$1 safety_backup=$2
    if [[ "$PREVIOUS_WAS_RUNNING" != "true" ]]; then
        info "Original CandyTest container was stopped; leaving it stopped after restore failure."
        return 0
    fi
    info "Restore failed before switching data; restarting the original service."
    set_compose_files "$mode"
    if ! compose up -d --no-build --remove-orphans; then
        error "Could not restart the original service. Safety backup remains at: $safety_backup"
        return 1
    fi
    if ! wait_for_healthy; then
        error "Original service did not become healthy. Safety backup remains at: $safety_backup"
        return 1
    fi
    return 0
}

recover_from_restore_failure() {
    local mode=$1 safety_backup=$2
    [[ -n "$safety_backup" ]] || return 1
    error "Restore did not become healthy. Restoring the safety backup: $safety_backup"
    if ! compose stop; then
        error "Could not stop the failed restored service."
        return 1
    fi
    if ! restore_archive_into_volume "$safety_backup"; then
        error "Could not restore the safety backup."
        return 1
    fi
    if ! compose up -d --no-build --remove-orphans; then
        error "Could not start the service after restoring the safety backup."
        return 1
    fi
    if ! wait_for_healthy; then
        error "Service did not become healthy after restoring the safety backup."
        return 1
    fi
    warn "Original data and service were restored from the safety backup."
    return 0
}

run_restore() {
    local mode container_id archive safety_backup=""
    acquire_lock
    require_docker
    [[ -n "$BACKUP_ARGUMENT" ]] || fatal "restore requires BACKUP.tar.gz"
    archive=$BACKUP_ARGUMENT
    [[ "$archive" = /* ]] || archive="$ROOT_DIR/$archive"
    [[ -f "$ENV_FILE" ]] || fatal "No .env exists. Deploy CandyTest before restoring a backup."
    validate_backup_archive "$archive"
    mode=$(value_or_default CANDYTEST_MODE "")
    case "$mode" in
        host|npm|caddy) ;;
        *) fatal "CANDYTEST_MODE must be host, npm, or caddy before restore." ;;
    esac
    set_compose_files "$mode"
    info "Validating Compose configuration for $mode mode."
    compose config --quiet

    container_id=$(find_candytest_container_any || true)
    PREVIOUS_WAS_RUNNING=false
    if [[ -n "$container_id" ]]; then
        PREVIOUS_WAS_RUNNING=$(docker inspect -f '{{.State.Running}}' "$container_id")
        safety_backup=$(default_backup_path)
        info "Creating safety backup before restore."
        if ! backup_data_from_container "$container_id" "$safety_backup"; then
            fatal "Safety backup failed; restore was not started."
        fi
    elif data_volume_exists; then
        safety_backup=$(default_backup_path)
        info "Creating safety backup from the stopped data volume before restore."
        if ! backup_data_from_named_volume "$safety_backup"; then
            fatal "Safety backup failed; restore was not started."
        fi
    fi

    info "Stopping the current service before switching restored data."
    if ! compose stop; then
        fatal "Could not stop the current service; restore was not started. Safety backup remains at: $safety_backup"
    fi
    if ! restore_archive_into_volume "$archive"; then
        error "Archive extraction or data switch failed; original data was kept."
        if ! restart_original_service_after_restore_failure "$mode" "$safety_backup"; then
            fatal "Restore failed and the original service could not be recovered. Safety backup remains at: $safety_backup"
        fi
        fatal "Restore failed. Safety backup remains at: $safety_backup"
    fi

    info "Starting restored service."
    if ! compose up -d --no-build --remove-orphans || ! wait_for_healthy; then
        show_candytest_logs
        if recover_from_restore_failure "$mode" "$safety_backup"; then
            fatal "Restore failed health checks; the prior data and service were restored. Safety backup remains at: $safety_backup"
        fi
        if ! stop_project_containers; then
            error "One or more failed containers could not be stopped. Inspect docker ps immediately."
        fi
        fatal "Restore failed health checks and automatic recovery failed. Safety backup remains at: $safety_backup"
    fi
    info "Restore completed. The backup archive was not deleted: $archive"
    [[ -z "$safety_backup" ]] || info "Safety backup retained at: $safety_backup"
    print_connection_details "$mode"
}

if [[ $# -gt 0 ]]; then
    case "$1" in
        deploy|status|logs|backup|restore)
            COMMAND=$1
            shift
            ;;
        -h|--help|help)
            usage
            exit 0
            ;;
    esac
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            require_argument "$1" "${2:-}"
            REQUESTED_MODE=$2
            MODE_WAS_SET=1
            shift 2
            ;;
        --tag)
            require_argument "$1" "${2:-}"
            TAG=$2
            shift 2
            ;;
        --image)
            require_argument "$1" "${2:-}"
            IMAGE=$2
            IMAGE_WAS_SET=1
            shift 2
            ;;
        --npm-network)
            require_argument "$1" "${2:-}"
            NPM_NETWORK_OPTION=$2
            shift 2
            ;;
        --domain)
            require_argument "$1" "${2:-}"
            DOMAIN_OPTION=$2
            shift 2
            ;;
        -f)
            [[ "$COMMAND" == logs ]] || fatal "-f is only valid with logs."
            FOLLOW_LOGS=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            fatal "Unknown option: $1"
            ;;
        *)
            case "$COMMAND" in
                backup)
                    [[ -z "$BACKUP_ARGUMENT" ]] || fatal "backup accepts at most one output path."
                    BACKUP_ARGUMENT=$1
                    ;;
                restore)
                    [[ -z "$BACKUP_ARGUMENT" ]] || fatal "restore accepts exactly one backup archive."
                    BACKUP_ARGUMENT=$1
                    ;;
                *)
                    fatal "Unexpected argument: $1"
                    ;;
            esac
            shift
            ;;
    esac
done

case "$COMMAND" in
    deploy) run_deploy ;;
    status) run_status ;;
    logs) run_logs ;;
    backup) run_backup ;;
    restore) run_restore ;;
esac

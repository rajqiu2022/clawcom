#!/usr/bin/env bash
set -u

LOG_FILE=${LOG_FILE:-/var/log/openclaw_dependencies_start.log}
TS() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(TS)] $*" | tee -a "$LOG_FILE"; }

run_step() {
  local name="$1"
  shift
  log "==> $name"
  if "$@" >>"$LOG_FILE" 2>&1; then
    log "OK: $name"
    return 0
  fi
  local code=$?
  log "FAIL($code): $name"
  return $code
}

is_listening() {
  local port="$1"
  ss -lnt 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)${port}$"
}

start_lampp() {
  if [ ! -x /opt/lampp/lampp ]; then
    log "MISS: /opt/lampp/lampp"
    return 1
  fi
  run_step "start LAMPP" /opt/lampp/lampp start
  /opt/lampp/lampp status 2>&1 | tee -a "$LOG_FILE"
}

start_mongodb() {
  local dir=/opt/db/mongodb/mongodbbin/bin
  if pgrep -fa 'mongod .*mongodb.conf|mongod.*--config.*mongodb.conf' >/dev/null 2>&1 || is_listening 27017; then
    log "SKIP: mongodb already appears running"
    return 0
  fi
  if [ ! -x "$dir/mongod" ] || [ ! -f "$dir/mongodb.conf" ]; then
    log "MISS: mongodb binary or config under $dir"
    return 1
  fi
  log "==> start mongodb"
  (cd "$dir" && nohup ./mongod -f ./mongodb.conf >>"$LOG_FILE" 2>&1 &)
  sleep 2
  if pgrep -fa 'mongod .*mongodb.conf|mongod.*--config.*mongodb.conf' >/dev/null 2>&1 || is_listening 27017; then
    log "OK: mongodb started"
  else
    log "WARN: mongodb start command issued, but process/27017 not detected yet"
  fi
}

start_django_uwsgi() {
  local dir=/data/home/user00/data_platform_release/env3/spdqa_platform_relase
  if pgrep -fa 'uwsgi .*script/uwsgi.ini|uwsgi .*uwsgi.ini' >/dev/null 2>&1; then
    log "SKIP: uwsgi already appears running"
    return 0
  fi
  if [ ! -d "$dir" ] || [ ! -f "$dir/script/uwsgi.ini" ]; then
    log "MISS: django release dir or uwsgi.ini"
    return 1
  fi
  log "==> start django uwsgi"
  (cd "$dir" && nohup uwsgi --ini ./script/uwsgi.ini >>"$LOG_FILE" 2>&1 &)
  sleep 2
  if pgrep -fa 'uwsgi .*script/uwsgi.ini|uwsgi .*uwsgi.ini' >/dev/null 2>&1; then
    log "OK: uwsgi started"
  else
    log "WARN: uwsgi command issued, but process not detected yet"
  fi
}

start_handledata_nginx() {
  local nginx_bin=/usr/local/nginx/sbin/nginx
  local nginx_conf=/usr/local/nginx/conf/handledata_nginx.conf
  if pgrep -fa 'nginx.*handledata_nginx.conf' >/dev/null 2>&1; then
    log "SKIP: handledata nginx already appears running"
    return 0
  fi
  if [ ! -x "$nginx_bin" ] || [ ! -f "$nginx_conf" ]; then
    log "MISS: nginx binary or handledata config"
    return 1
  fi
  run_step "test handledata nginx config" "$nginx_bin" -t -c "$nginx_conf"
  run_step "start handledata nginx" "$nginx_bin" -c "$nginx_conf"
}

start_golang_backend() {
  local dir=/opt/go_path/src/micro-cloud
  if [ ! -d "$dir" ] || [ ! -x "$dir/rebuild.sh" ]; then
    log "MISS: golang backend dir or rebuild.sh"
    return 1
  fi
  log "==> start/rebuild golang backend"
  (cd "$dir" && ./rebuild.sh) >>"$LOG_FILE" 2>&1
  local code=$?
  if [ $code -eq 0 ]; then
    log "OK: golang backend rebuild.sh finished"
  else
    log "FAIL($code): golang backend rebuild.sh"
  fi
  return $code
}

main() {
  mkdir -p "$(dirname "$LOG_FILE")"
  touch "$LOG_FILE"
  log "===== start openclaw dependencies ====="
  start_lampp || true
  start_mongodb || true
  start_django_uwsgi || true
  start_handledata_nginx || true
  start_golang_backend || true
  log "===== final listeners ====="
  ss -lntp 2>&1 | awk 'NR==1 || /:80|:443|:3306|:27017|:8001|:8002|:8080|:18800|:5232|:1933/' | tee -a "$LOG_FILE"
  log "===== done ====="
}

main "$@"

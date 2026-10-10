#!/usr/bin/env bash
# Развёртывание бота одной командой (Ubuntu/Debian).
#   ./deploy.sh            — установить/обновить и запустить
#   ./deploy.sh logs       — логи бота
#   ./deploy.sh restart    — перезапуск
#   ./deploy.sh stop       — остановить
#   ./deploy.sh backup     — бэкап БД сейчас
#   ./deploy.sh restore <файл.sql.gz> — восстановить БД из бэкапа
set -euo pipefail
cd "$(dirname "$0")"

say() { echo -e "\033[1;32m==>\033[0m $*"; }
die() { echo -e "\033[1;31mОшибка:\033[0m $*" >&2; exit 1; }

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

dc() {
  if docker info >/dev/null 2>&1; then docker compose "$@"; else $SUDO docker compose "$@"; fi
}

load_env() {
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return
  fi
  say "Устанавливаю Docker и compose plugin..."
  command -v curl >/dev/null 2>&1 || { $SUDO apt-get update -y && $SUDO apt-get install -y curl; }
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker
  if [ -n "$SUDO" ]; then
    $SUDO usermod -aG docker "$USER" || true
  fi
  docker compose version >/dev/null 2>&1 || $SUDO docker compose version >/dev/null 2>&1 \
    || die "compose plugin не установился"
  say "Docker установлен"
}

check_env() {
  if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    say "Создан файл .env. Заполни в нём:"
    echo "   TELEGRAM_BOT_TOKEN — токен от @BotFather"
    echo "   OPENAI_API_KEY     — ключ с platform.openai.com"
    echo "   ALLOWED_USER_ID    — свой ID от @userinfobot"
    echo "   POSTGRES_PASSWORD  — придумай пароль (и тот же в DATABASE_URL)"
    echo "Потом снова запусти ./deploy.sh"
    exit 0
  fi
  load_env
  local missing=()
  for v in TELEGRAM_BOT_TOKEN OPENAI_API_KEY OPENAI_MODEL ALLOWED_USER_ID \
           POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB DATABASE_URL; do
    [ -n "${!v:-}" ] || missing+=("$v")
  done
  [ ${#missing[@]} -eq 0 ] || die "в .env не заполнено: ${missing[*]}"
  [[ "$ALLOWED_USER_ID" =~ ^[0-9]+$ ]] || die "ALLOWED_USER_ID должен быть числом"
  for f in seed/profile.md seed/targets.json seed/plan.json; do
    [ -f "$f" ] || die "нет файла $f"
  done
}

wait_healthy() {
  say "Жду, пока бот поднимется..."
  local id status
  for _ in $(seq 1 60); do
    id=$(dc ps -q bot)
    status=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null \
      || $SUDO docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo "?")
    if [ "$status" = "healthy" ]; then say "Бот работает"; return 0; fi
    sleep 5
  done
  echo "Бот не стал healthy за 5 минут — смотри логи ниже."
  return 1
}

cmd_up() {
  install_docker
  check_env
  if [ -d .git ]; then
    say "Обновляю код (git pull)..."
    git pull --ff-only || echo "git pull не удался — продолжаю с текущим кодом"
  fi
  mkdir -p backups
  say "Собираю и запускаю контейнеры..."
  dc up -d --build
  local ok=0
  wait_healthy || ok=1
  dc ps
  dc logs --tail 30 bot
  return $ok
}

cmd_backup() {
  load_env
  mkdir -p backups
  local f="backups/db_$(date +%Y-%m-%d_%H%M)_manual.sql.gz"
  dc exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists | gzip > "$f"
  say "Бэкап сохранён: $f"
}

cmd_restore() {
  local f="${1:-}"
  [ -n "$f" ] && [ -f "$f" ] || die "укажи файл бэкапа: ./deploy.sh restore backups/....sql.gz"
  load_env
  read -r -p "Текущая база будет перезаписана данными из $f. Продолжить? [y/N] " a
  [[ "$a" =~ ^[yYдД]$ ]] || { echo "Отменено"; exit 0; }
  dc stop bot
  gunzip -c "$f" | dc exec -T db psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
  dc start bot
  say "Восстановлено из $f"
}

case "${1:-up}" in
  up)      cmd_up ;;
  logs)    dc logs -f --tail 100 bot ;;
  restart) dc restart bot && say "Бот перезапущен" ;;
  stop)    dc down && say "Остановлено (данные сохранены)" ;;
  backup)  cmd_backup ;;
  restore) cmd_restore "${2:-}" ;;
  *)       die "неизвестная команда: $1 (есть: logs, restart, stop, backup, restore <файл>)" ;;
esac

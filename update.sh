#!/usr/bin/env bash
#
# Обновление дашборда одной командой с ноутбука.
#
#   ./update.sh              полный цикл: сбор → извлечение → приёмка → сборка → публикация
#   ./update.sh --rebuild    только пересобрать HTML из data/banks.json (без сети и ключа)
#   ./update.sh --local      всё собрать, но не пушить (посмотреть перед публикацией)
#
# Ключ Gemini берётся из файла .env рядом со скриптом (см. .env.example).
# Файл в .gitignore и на GitHub не уезжает.

set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
REBUILD=0
PUSH=1

for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    --local)   PUSH=0 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный аргумент: $arg (см. --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ "$REBUILD" -eq 0 ]; then
  [ -n "${GEMINI_API_KEY:-}" ] || fail "Не задан GEMINI_API_KEY. Скопируйте .env.example в .env и впишите ключ."

  step "Сбор страниц банков"
  # Частичный сбой не фатален: банк без свежего снимка получит статус stale,
  # решение принимает validate.py.
  $PY src/fetch.py || echo "  (часть банков не собралась — продолжаем)"

  step "Извлечение условий через Gemini"
  $PY src/extract.py || echo "  (часть банков не извлеклась — продолжаем)"

  step "Проверка и приёмка данных"
  # Единственный шаг, который может остановить публикацию: если развалилось
  # больше двух банков, данные не записываются и дальше мы не идём.
  $PY src/validate.py || fail "Данные не прошли приёмку — публикация отменена. Разбор: data/snapshots/"
fi

step "Сборка страницы"
$PY src/build.py

if [ "$PUSH" -eq 0 ]; then
  printf '\n\033[1m✓ Готово (--local): docs/index.html собран, публикация пропущена\033[0m\n'
  printf 'Открыть: open docs/index.html\n'
  exit 0
fi

step "Публикация на GitHub"
git remote get-url origin >/dev/null 2>&1 || fail "Не настроен remote origin — см. README, шаг «Репозиторий на GitHub»."

git add data/banks.json docs/index.html
if git diff --staged --quiet; then
  echo "  Изменений нет — коммит и пуш пропущены"
else
  git commit -q -m "Данные банков на $(date +%d.%m.%Y)"
  git push -q
  echo "  Запушено. Страница обновится через 1–2 минуты."
  url=$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
  user=${url%%/*}; repo=${url##*/}
  echo "  https://${user}.github.io/${repo}/"
fi

printf '\n\033[1m✓ Готово\033[0m\n'

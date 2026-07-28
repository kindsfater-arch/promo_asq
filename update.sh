#!/usr/bin/env bash
#
# Обновление дашборда одной командой с ноутбука.
#
#   ./update.sh              полный цикл: сбор → извлечение → приёмка → сборка → публикация
#   ./update.sh --repair     точечно: только банки, которые отстали от остальных
#   ./update.sh --bank vtb   точечно: названные банки (через запятую)
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
SCOPE=""        # пусто — все банки; иначе список id через запятую

while [ $# -gt 0 ]; do
  case "$1" in
    --rebuild) REBUILD=1 ;;
    --local)   PUSH=0 ;;
    --repair)  SCOPE="stale" ;;
    --bank|--banks)
      [ $# -ge 2 ] || { echo "После $1 нужен id банка" >&2; exit 2; }
      SCOPE="$2"; shift ;;
    --bank=*|--banks=*) SCOPE="${1#*=}" ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный аргумент: $1 (см. --help)" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ "$REBUILD" -eq 0 ]; then
  [ -n "${GEMINI_API_KEY:-}" ] || fail "Не задан GEMINI_API_KEY. Скопируйте .env.example в .env и впишите ключ."

  # Один список id на все три шага: считаем его здесь, чтобы сбор,
  # извлечение и приёмка не разъехались, если банк по дороге сменит статус.
  if [ "$SCOPE" = "stale" ]; then
    SCOPE=$($PY src/selection.py)
    [ -n "$SCOPE" ] || { echo "Дообновлять нечего: у всех банков свежие данные"; exit 0; }
    echo "Дообновляем: $SCOPE"
  elif [ -n "$SCOPE" ]; then
    SCOPE=$($PY src/selection.py --ids "$SCOPE") || exit 2
  else
    SCOPE=$($PY src/selection.py --all)
  fi
  SEL="--banks=$SCOPE"

  step "Сбор страниц банков"
  # Частичный сбой не фатален: банк без свежего снимка получит статус stale,
  # решение принимает validate.py.
  $PY src/fetch.py "$SEL" || echo "  (часть банков не собралась — продолжаем)"

  step "Извлечение условий через Gemini"
  $PY src/extract.py "$SEL" || echo "  (часть банков не извлеклась — продолжаем)"

  step "Проверка и приёмка данных"
  # Единственный шаг, который может остановить публикацию: если в полном
  # прогоне развалилось больше двух банков, данные не записываются и дальше
  # мы не идём. Точечный прогон таким порогом не связан.
  $PY src/validate.py "$SEL" || fail "Данные не прошли приёмку — публикация отменена. Разбор: data/snapshots/"
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
# Смотрим только на данные: в пересобранном HTML всегда меняется время
# сборки, и по нему «изменений нет» не наступило бы никогда.
if git diff --staged --quiet -- data/banks.json; then
  echo "  Данные не изменились — коммит и пуш пропущены"
  git restore --staged data/banks.json docs/index.html
else
  git commit -q -m "Данные банков на $(date +%d.%m.%Y)"
  git push -q
  echo "  Запушено. Страница обновится через 1–2 минуты."
  url=$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
  user=${url%%/*}; repo=${url##*/}
  echo "  https://${user}.github.io/${repo}/"
fi

printf '\n\033[1m✓ Готово\033[0m\n'

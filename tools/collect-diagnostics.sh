#!/usr/bin/env bash
# ============================================
# Btrfs Backup Manager — сбор диагностического отчета
# ============================================
# Только ЧТЕНИЕ: ничего не монтирует, не форматирует, не удаляет, не
# изменяет конфигурацию, не запускает резервирование/восстановление.
# Результат печатается в stdout; сохраните его целиком:
#   sudo bash tools/collect-diagnostics.sh > bbm-report.txt 2>&1
# Секреты не собираются: пароли в журналы не пишутся (ТЗ 1.4.8 §6.2),
# содержимое архивов и снапшотов не выводится (только имена и размеры).
# ТЗ 1.4.8.1 §8: каждая команда сбора сопровождается статусом
# [OK]/[НЕ УДАЛОСЬ]; в конце — сводка, какие сведения не получены.

set -u

section() { echo; echo "===== $* ====="; }

OK_COUNT=0
FAIL_COUNT=0
FAIL_LIST=""

have() { command -v "$1" > /dev/null 2>&1; }

note_fail() {
  # note_fail "<описание>" "<причина>"
  FAIL_COUNT=$((FAIL_COUNT + 1))
  FAIL_LIST="$FAIL_LIST
  $1 ($2)"
  echo "[НЕ УДАЛОСЬ] $1 ($2)"
}

run_cmd() {
  # run_cmd "<описание>" <команда с аргументами...>
  local desc="$1"; shift
  echo "--- $desc"
  if "$@"; then
    OK_COUNT=$((OK_COUNT + 1))
  else
    note_fail "$desc" "код возврата $?"
  fi
}

cmd() {
  # cmd <утилита> <описание> <аргументы...> — с проверкой наличия утилиты
  local tool="$1" desc="$2"; shift 2
  echo "--- $desc"
  if ! have "$tool"; then
    note_fail "$desc" "утилита $tool не найдена"
    return 0
  fi
  if "$tool" "$@"; then
    OK_COUNT=$((OK_COUNT + 1))
  else
    note_fail "$desc" "код возврата $?"
  fi
}

section "0. Системная информация"
run_cmd "ядро и архитектура (uname -a)" uname -a
run_cmd "текущий пользователь (id)" id
run_cmd "дата отчета" date

section "1. Версии инструментов (ТЗ 1.4.8 §2.1)"
cmd python3 "версия python3" --version
cmd btrfs "версия btrfs" --version
cmd gpg "версия gpg" --version
cmd gzip "версия gzip" --version
if have zstd; then
  cmd zstd "версия zstd" --version
else
  echo "zstd: НЕ УСТАНОВЛЕН — проверки zstd в приемке будут ПРОПУЩЕНО"
  note_fail "версия zstd" "утилита zstd не установлена"
fi
cmd lsblk "версия lsblk" --version

section "2. Тестовая среда (ТЗ 1.4.8 §10: loop0, /mnt/btrfs-test)"
cmd lsblk "loop0: тип и размер" -no NAME,TYPE,FSTYPE,SIZE /dev/loop0
cmd findmnt "точка монтирования /mnt/btrfs-test" \
  -no SOURCE,FSTYPE,TARGET,OPTIONS /mnt/btrfs-test

section "3. Все монтирования Btrfs (mountpoints)"
if have findmnt; then
  run_cmd "список монтирований btrfs (findmnt -t btrfs)" \
    findmnt -t btrfs -o TARGET,SOURCE,FSTYPE,OPTIONS
else
  run_cmd "список монтирований btrfs (из /proc/self/mounts)" \
    grep btrfs /proc/self/mounts
fi

section "4. Реальный вывод btrfs filesystem show (UUID)"
cmd btrfs "filesystem show /mnt/btrfs-test" filesystem show /mnt/btrfs-test

section "5. Реальный вывод btrfs subvolume list (проверка формата)"
cmd btrfs "subvolume list /mnt/btrfs-test" subvolume list /mnt/btrfs-test

section "6. Свободное место"
cmd df "свободное место на /mnt/btrfs-test" -h /mnt/btrfs-test

section "7. Снапшоты и архивы тестовой среды (только имена и размеры)"
run_cmd "список каталога снапшотов" ls -la /mnt/btrfs-test/.snapshots
run_cmd "список каталога архивов" ls -la /mnt/btrfs-test/.snapshots/backup

section "8. Журнал программы (последние строки; пароли не логируются)"
if [ -f /tmp/bbm-acceptance-test.log ]; then
  run_cmd "журнал приемки (tail -40)" tail -40 /tmp/bbm-acceptance-test.log
elif [ -f /var/log/btrfs-backup.log ]; then
  run_cmd "основной журнал (tail -40)" tail -40 /var/log/btrfs-backup.log
else
  note_fail "журнал приложения" "не найден ни /tmp/bbm-acceptance-test.log, ни /var/log/btrfs-backup.log"
fi

section "9. Версия программы"
PROG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
run_cmd "APP_VERSION приложения" \
  python3 -c "import sys; sys.path.insert(0, '$PROG_DIR'); from core import APP_VERSION; print('Btrfs Backup Manager', APP_VERSION)"

section "Сводка (ТЗ 1.4.8.1 §8)"
echo "Команд выполнено успешно: $OK_COUNT"
echo "Сведений не получено: $FAIL_COUNT"
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "Не удалось получить:$FAIL_LIST"
fi
section "Конец отчета"

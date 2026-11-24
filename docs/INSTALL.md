# Установка и запуск (переносной режим)

Программа не устанавливается в систему: не требует установщика,
демона, systemd-службы или серверной части. Каталог проекта
копируется в удобное место и запускается оттуда. Формулировки с
`/opt` ниже — пример, а не обязательный путь.

## 1. Копирование

```bash
# Распакуйте архив релиза в удобный каталог либо клонируйте Git-репозиторий.
cd /путь/к/btrfs-backup-manager          # каталог, в котором находится main.py
ls main.py
```

## 2. Проверка Python

```bash
python3 --version    # требуется 3.12 или новее
```

Программа использует только стандартную библиотеку; сторонние
Python-пакеты не нужны (`requirements.txt` — только для справки).

## 3. Проверка системных утилит

```bash
btrfs --version      # btrfs-progs, обязательно
gpg --version        # обязательно
gzip --version       # обязательно
zstd --version       # необязательно (fallback на gzip)
```

Сводная проверка окружения из самой программы:

```bash
python3 main.py diagnostics
```

Для операций с subvolume и `btrfs send/receive` нужны права root:
используйте `doas`, `sudo` или учетную запись root (в зависимости от
дистрибутива).

## 4. Рабочий backup_config.py

```bash
cp backup_config.py.example backup_config.py
$EDITOR backup_config.py
```

Минимум для неинтерактивного запуска: `SUBVOLUMES`, `SNAPSHOT_DIR`,
`GPG_PASSWORD` (и `NETWORK_MOUNT`, если используется сеть). Проверить
настройки: `python3 main.py diagnostics`.

Файл содержит пароль GPG — сразу ограничьте доступ:

```bash
chown root:root backup_config.py
chmod 600 backup_config.py
```

`config.py` редактировать не нужно: он загружает и проверяет значения
из `backup_config.py`.

## 5. Запуск TUI

```bash
doas python3 main.py tui
```

Полноэкранный интерфейс (curses). Если curses недоступен или терминал
неинтерактивный, программа сама запускает простой CLI-меню.

## 6. Запуск CLI

```bash
doas python3 main.py                    # простой CLI-меню
doas python3 main.py backup             # резервное копирование
doas python3 main.py backup -n          # строго по конфигурации (cron)
doas python3 main.py restore --archive A --target D
doas python3 main.py verify --archive A
doas python3 main.py snapshots           # список снапшотов
doas python3 main.py list-archives       # список локальных и сетевых архивов
doas python3 main.py upload-pending      # отправка ожидающих архивов
```

Список всех команд: `python3 main.py --help`.

## 7. Подготовка cron

Неинтерактивные команды работают строго по `backup_config.py` — без
вопросов и запросов пароля, поэтому пароль должен быть задан в
конфигурации (см. [CRON.md](CRON.md)):

```cron
30 2 * * *  root  cd /opt/btrfs-backup-manager && /usr/bin/python3 main.py backup -n
```

## Альтернативный файл настроек

Для отдельного задания cron или приемочных испытаний можно указать
другой файл настроек (порядок аргументов любой):

```bash
doas python3 main.py backup --config /etc/btrfs/home.py
doas python3 main.py --config /etc/btrfs/home.py backup
```

Отдельного управления профилями в программе нет: основной файл —
`backup_config.py`, альтернативные файлы передаются явно.

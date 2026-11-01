# Диагностика PR #16 на существующем Ubuntu VPS

Команды предназначены для последовательного выполнения владельцем через
Termius. В этой работе они **не выполнялись на VPS**. Диагностика не перезапускает
бота, не меняет его конфигурацию или файлы `/opt/funding-arb-test`, не обращается
к биржам и не отправляет ордера. Она создаёт только временные файлы с фиктивными
данными в `/var/tmp` и отдельную краткоживущую systemd-задачу. После переподключения
к SSH переменные shell необходимо задать заново. Не вводите реальные ключи.

## 1. Прочитать сведения о существующей установке

```sh
cd /opt/funding-arb-test
pwd
git status --short
git rev-parse --short HEAD
command -v python3
command -v age
command -v age-keygen
command -v gh
systemctl list-units --type=service --all --no-pager | grep -i funding
```

Если `age`, `age-keygen` или `gh` отсутствует, остановитесь: установка пакетов,
настройка GitHub-доступа и изменение VPS требуют отдельного решения владельца.
`gh auth status` покажет, доступен ли уже настроенный GitHub CLI; токен в команду
вставлять не нужно. Для приватного репозитория требуется существующий read-доступ.
Найдите имя **системного** сервиса в последней команде и подставьте его здесь:

```sh
SERVICE_UNIT='ИМЯ-СЕРВИСА.service'
systemctl show "$SERVICE_UNIT" -p User -p Group -p DynamicUser -p WorkingDirectory -p FragmentPath
```

Если сервис не найден или `DynamicUser=yes`, остановитесь: автоматическое
назначение пользователя требует отдельной проверки. Если поле `User` пустое для
системного сервиса, systemd запускает его от root. Не выводите `Environment`,
`EnvironmentFile`, аргументы процесса или содержимое файлов credentials: они
могут содержать секреты. Не включайте shell trace (`set -x`).

## 2. Подготовить только временный диагностический код

Эти команды считывают текущий коммит PR с GitHub и извлекают **четыре отдельных
файла** в `/var/tmp`; это не checkout и не вторая установка бота. Они не
изменяют `/opt/funding-arb-test`. Если любая команда завершается ошибкой,
остановитесь; не продолжайте с неполными файлами.

```sh
SERVICE_USER="$(systemctl show "$SERVICE_UNIT" --property=User --value)"
[ -n "$SERVICE_USER" ] || SERVICE_USER=root
id "$SERVICE_USER"
PR_HEAD="$(gh api repos/sany6259/funding-arb-test/pulls/16 --jq .head.sha)"
printf 'PR head: %s\n' "$PR_HEAD"
printf '%s' "$PR_HEAD" | grep -Eq '^[0-9a-f]{40}$'
PROBE_CODE="$(mktemp -d /var/tmp/funding-arb-pr16-code-XXXXXXXX)"
mkdir -p "$PROBE_CODE/scripts/core" "$PROBE_CODE/scripts/cli"
FETCH_OK=1
for f in scripts/core/file_lock.py scripts/core/execution_mode.py scripts/core/demo_credentials.py scripts/cli/demo_age_diagnostic.py; do
  gh api -H 'Accept: application/vnd.github.raw+json' \
    "repos/sany6259/funding-arb-test/contents/$f?ref=$PR_HEAD" > "$PROBE_CODE/$f" || { FETCH_OK=0; break; }
done
for f in scripts/core/file_lock.py scripts/core/execution_mode.py scripts/core/demo_credentials.py scripts/cli/demo_age_diagnostic.py; do
  test -s "$PROBE_CODE/$f" || FETCH_OK=0
done
if [ "$FETCH_OK" -eq 1 ]; then
  sudo chown -R -- "$SERVICE_USER" "$PROBE_CODE"
  PROBE_DIR="$(sudo -u "$SERVICE_USER" mktemp -d /var/tmp/funding-arb-pr16-probe-XXXXXXXX)"
  printf 'PROBE_CODE=%s\nPROBE_DIR=%s\n' "$PROBE_CODE" "$PROBE_DIR"
else
  echo 'Ошибка загрузки, остановитесь'
fi
```

Запишите два последних пути для повторной проверки после переподключения. Если
нет `sudo` или файлы не загружаются, остановитесь. Дополнительный аккаунт или
вторая установка не создаются. Перед запуском в следующих шагах сверьте
выведенный `PR head` с проверенным коммитом PR #16 и просмотрите эти четыре
файла в PR.

## 3. Проверить сохранение, обновление и чтение в SSH

```sh
AGE_DIR="$(dirname "$(command -v age)")"
sudo -u "$SERVICE_USER" env "PATH=$AGE_DIR:/usr/bin:/bin" \
  "PYTHONPATH=$PROBE_CODE/scripts" /usr/bin/python3 \
  "$PROBE_CODE/scripts/cli/demo_age_diagnostic.py" create "$PROBE_DIR"
sudo -u "$SERVICE_USER" env "PATH=$AGE_DIR:/usr/bin:/bin" \
  "PYTHONPATH=$PROBE_CODE/scripts" /usr/bin/python3 \
  "$PROBE_CODE/scripts/cli/demo_age_diagnostic.py" verify "$PROBE_DIR"
```

Ожидается `PASS` в обоих случаях. Первая команда создаёт отдельный тестовый
ключ age, сохраняет и обновляет фиктивную запись и проверяет, что на диске
шифротекст. Вторая читает её новым процессом. Существующие файлы ключей и HOME
бота не используются; `HOME` для этих процессов задаётся внутри диагностики.

## 4. Проверить чтение отдельным systemd-процессом

Команда запускает одноразовый процесс с тем же пользователем; она **не** меняет
и не перезапускает существующий сервис. Возможно потребуется sudo:

```sh
sudo systemd-run --wait --pipe --collect \
  --property="User=$SERVICE_USER" \
  --setenv="PATH=$AGE_DIR:/usr/bin:/bin" \
  --setenv="PYTHONPATH=$PROBE_CODE/scripts" \
  /usr/bin/python3 "$PROBE_CODE/scripts/cli/demo_age_diagnostic.py" verify "$PROBE_DIR"
```

Ожидается `PASS`. Это проверяет доступ выбранного пользователя из временной
systemd-задачи, но не доказывает совместимость с ограничениями действующего
unit-файла (`ProtectHome`, `ReadWritePaths`, дополнительные группы и т. п.).
После **отдельно согласованного** перезапуска машины можно повторить команду,
заново задав сохранённые выше переменные; очистка `/var/tmp` системой может
удалить диагностические данные. Не перезапускайте работающий сервер или VPS
только ради этой проверки без отдельного разрешения.

## 5. Проверить центральный запрет ордеров без сети

```sh
sudo -u "$SERVICE_USER" env "PYTHONPATH=$PROBE_CODE/scripts" \
  "HOME=$PROBE_DIR/home" /usr/bin/python3 - <<'PY'
from core.execution_mode import set_mode, forbid_orders, simulation_request
for mode in ('simulation', 'demo'):
    set_mode(mode)
    try:
        forbid_orders()
    except RuntimeError:
        pass
    else:
        raise SystemExit('FAIL: order guard')
try:
    simulation_request(lambda: (_ for _ in ()).throw(AssertionError('network callback executed')))
except RuntimeError:
    print('PASS: order guard and demo legacy-request guard')
else:
    raise SystemExit('FAIL: demo legacy-request guard')
PY
```

Проверяются только функции извлечённого коммита и временное состояние. Это не
подтверждает поведение уже запущенной старой версии, все её адаптеры или внешние
процессы. Реальных сетевых вызовов не происходит.

## 6. Удалить фиктивную запись

```sh
sudo -u "$SERVICE_USER" env "PATH=$AGE_DIR:/usr/bin:/bin" \
  "PYTHONPATH=$PROBE_CODE/scripts" /usr/bin/python3 \
  "$PROBE_CODE/scripts/cli/demo_age_diagnostic.py" delete "$PROBE_DIR"
```

Ожидается `PASS`. Временные каталоги содержат только диагностический код и
фиктивную идентичность; их можно удалить после проверки путей вручную. Не
удаляйте ничего внутри `/opt/funding-arb-test` или настоящего HOME сервиса.

## Только после отдельного разрешения на изменение VPS

Потребуется согласовать установку `age`, создать отдельную identity с правами
`0600` от имени пользователя сервиса, настроить для его unit
`FARB_DEMO_CREDENTIAL_STORE=age` и при необходимости `FARB_DEMO_AGE_IDENTITY`,
проверить его HOME и ограничения доступа к файлам. Не перезаписывайте
существующую identity. Это потребует изменения конфигурации и последующего
перезапуска; команды изменения намеренно не являются частью предварительной
диагностики. Затем, после отдельного разрешения на реальные read-only API,
можно подключить демосчета и сравнить балансы. Проверка баланса не разрешает и
не доказывает готовность ордеров.

Если обнаружена открытая реальная позиция, остановите автоматизацию и проверьте
обе ноги и заявки непосредственно на биржах. Ручное закрытие выполняйте через
предусмотренные интерфейсы бирж по своему регламенту. Эта версия бота блокирует
и открытие, и автоматическое аварийное закрытие; отдельного подтверждённого
механизма аварийных ордеров внутри бота пока нет.

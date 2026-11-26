# Optimize Dockerfile — Result

Исходный `common/lesson_5/docker-lab/Dockerfile` не изменялся. Оптимизация выполнена отдельными
файлами: `Dockerfile.step1` … `Dockerfile.step4`; итоговый образ собирается из `Dockerfile.step4`.
`app/`, `tests/`, `pyproject.toml`, `uv.lock` не тронуты.

## 1. Original State

Замер «до оптимизации»: файлы из HEAD, без `.dockerignore` и без локального `.venv`
(чистое состояние репозитория).

### `./scripts/bench.sh baseline`

```
=== bench: launch-control:baseline (Dockerfile) ===

[1/3] холодная сборка (--no-cache) — это долго, так и задумано
  18s
[2/3] размер 1562 MB, слоёв в истории: 24
[3/3] тёплая пересборка после правки одной строки в app/main.py
  6s

--------------------------------------------------
  Размер образа        1562 MB
  Холодная сборка      18 s
  Тёплая пересборка    6 s   <- вот эта
  Слоёв в истории      24
--------------------------------------------------
```

### `./scripts/score.sh baseline`

```
=== score: launch-control:baseline ===

  ✗ Размер <= 400 MB                     1562 MB
  ✗ Работает не от root                  root
  ✗ Есть HEALTHCHECK
  ✗ Нет компилятора в runtime            найдено: gcc cc g++ make
  ✗ Нет dev-инструментов                 найдено: git vim wget curl pytest uv
  ✗ Тесты и мусор не едут в образ        внутри: /app/tests /app/pyproject.toml
  ✗ Нет секретов в ENV и в истории       ADMIN_TOKEN в Config.Env
  ✗ .dockerignore есть                   файла нет
  ✗ hadolint чист (>= warning)           0 error, 7 warning

  Готово: 0/9
```

### `hadolint Dockerfile`

```
Dockerfile:7 DL3064 warning: Potentially sensitive data should not be used in the `ARG` or `ENV` commands
Dockerfile:10 DL3009 info: Delete the apt lists (/var/lib/apt/lists) after installing something
Dockerfile:10 DL3008 warning: Pin versions in apt get install. Instead of `apt-get install <package>` use `apt-get install <package>=<version>`
Dockerfile:10 DL3015 info: Avoid additional packages by specifying `--no-install-recommends`
Dockerfile:18 DL3013 warning: Pin versions in pip. Instead of `pip install <package>` use `pip install <package>==<version>` or `pip install --requirement <requirements file>`
Dockerfile:18 DL3042 warning: Avoid use of cache directory with pip. Use `pip install --no-cache-dir <package>`
Dockerfile:19 DL3042 warning: Avoid use of cache directory with pip. Use `pip install --no-cache-dir <package>`
Dockerfile:19 DL3013 warning: Pin versions in pip. Instead of `pip install <package>` use `pip install <package>==<version>` or `pip install --requirement <requirements file>`
Dockerfile:19 DL3059 info: Multiple consecutive `RUN` instructions. Consider consolidation.
Dockerfile:27 DL3059 info: Multiple consecutive `RUN` instructions. Consider consolidation.
Dockerfile:31 DL3025 warning: Use arguments JSON notation for CMD and ENTRYPOINT arguments
```

Итого **7 warning / 4 info**.

### `trivy image launch-control:baseline`

| Target | Type | Vulnerabilities |
| --- | --- | ---: |
| launch-control:baseline (debian 13.8) | debian | 2901 |
| Python | python-pkg | 3 |
| usr/local/bin/uvx | rustbinary | 0 |
| usr/local/bin/uv | rustbinary | 0 |

| Severity | Найдено |
| --- | ---: |
| CRITICAL | 3 |
| HIGH | 195 |
| MEDIUM | 727 |
| LOW | 1458 |
| UNKNOWN | 521 |
| **Всего** | **2904** |

Примеры: `libxml2` / `libxml2-dev` — CVE-2026-6653 (CRITICAL), `openssh-client` — CVE-2026-60002 (CRITICAL),
`setuptools` 70.3.0 — CVE-2025-47273 (HIGH). В отчёте есть цели вида `root/.cache/uv/archive-v0/...` —
uv-кеш едет внутри образа, то есть в нём есть мусор.

### `dive launch-control:baseline --ci`

```
efficiency: 98.9509 %
wastedBytes: 23175266 bytes (23 MB)
userWastedPercent: 1.6319 %
Inefficient Files:
Count  Wasted Space  File Path
    5        4.2 MB  /var/cache/debconf/templates.dat
    4        3.3 MB  /var/cache/debconf/templates.dat-old
    6        1.7 MB  /var/lib/dpkg/status
    6        1.7 MB  /var/lib/dpkg/status-old
    5        648 kB  /var/log/dpkg.log
...
Result:PASS [Total:3] [Passed:2] [Failed:0] [Warn:0] [Skipped:1]
```

## 2. Choosing the base image

| Вариант | Плюсы | Минусы | Выбор |
| --- | --- | --- | --- |
| `python:3.12` (baseline) | всё уже внутри | ~1.5 ГБ, компилятор и dev-тулзы в проде | нет |
| `python:3.12-slim-trixie` | glibc → все зависимости ставятся готовыми manylinux-колёсами (uvloop, httptools, watchfiles, pyyaml, websockets, numpy, pandas); компилятор не нужен; ~130 МБ; есть `sh` для диагностики | больше distroless на ~60 МБ | **да** |
| `alpine` | маленькая база | musl: для перечисленных пакетов нет колёс → сборка из исходников, нужны компилятор/rust; дольше и в итоге больше | нет |
| `distroless` | минимальный, нет шелла (проверки «нет компилятора/dev-tools/тестов» проходят автоматически) | нет шелла → сложнее `docker exec` и диагностика; в `gcr.io/distroless/python3` старее Python и сложнее с venv/`PYTHONPATH`; HEALTHCHECK только бинарём | нет |

Выбран **slim**: он единственный даёт готовые wheels для всех runtime-зависимостей, поэтому
компилятор не нужен ни в builder, ни в runtime, и при этом остаётся шелл для проверок и отладки.
Сборка идёт в `ghcr.io/astral-sh/uv:0.13.7-python3.12-trixie-slim` — тот же Python 3.12/trixie,
что и runtime, поэтому ABI виртуального окружения совпадает.

## 3. Compare by tools

### hadolint

| Файл | error | warning | info |
| --- | ---: | ---: | ---: |
| `Dockerfile` (baseline) | 0 | 7 | 4 |
| `Dockerfile.step1` | 0 | 7 | 4 |
| `Dockerfile.step2` | 0 | 7 | 4 |
| `Dockerfile.step3` | 0 | **0** | **0** |
| `Dockerfile.step4` | 0 | **0** | **0** |

Что исправлено: `DL3064` (секрет `ADMIN_TOKEN` в `ENV`), `DL3008`/`DL3013`/`DL3042` (незапиненные
apt/pip, pip-кеш), `DL3025` (shell-форма `CMD`), `DL3009`/`DL3015` (apt-мусор и `--no-install-recommends`),
`DL3059` (лишние последовательные `RUN`). В шагах 3–4 apt/pip отсутствуют вовсе — нарушать нечего.

### trivy

| Severity | baseline | step4 | Δ |
| --- | ---: | ---: | ---: |
| CRITICAL | 3 | **0** | −3 |
| HIGH | 195 | 54 | −141 |
| MEDIUM | 727 | 85 | −642 |
| LOW | 1458 | 78 | −1380 |
| UNKNOWN | 521 | 15 | −506 |
| **Всего** | **2904** | **232** | **−2672** |

Сокращение идёт за счёт пакетов, которые не нужны runtime: `build-essential`, `git`, `vim`, `curl`,
`net-tools`, `procps`, pip/uv вместе с их кешами и старым `setuptools`. Остаток — уязвимости базового
Debian (их лечит обновление базового образа, а не Dockerfile).

### dive

| Метрика | baseline | step4 |
| --- | ---: | ---: |
| efficiency | 98.95 % | 96.83 % |
| wastedBytes | 23 MB | **6.0 MB** |
| userWastedPercent | 1.63 % | 7.58 % |
| CI result | PASS | PASS |

`efficiency` у step4 ниже не потому, что стало хуже: метрика относительная и считается по слоям,
а сам образ меньше в 8 раз — те же ~6 МБ «мусора» дают больший процент. Абсолютный объём упал почти
в 4 раза (23 МБ → 6.0 МБ). Важно: dive считает байты в слоях, поэтому удалённые из базового образа
логи/кеши (шаг 4) в его метрике остаются — они лежат в нижних слоях и перекрыты whiteout-ами.


## 4. Steps (measurements)

| Step | File | Size | delta | Холодная сборка | Тёплая пересборка | Слои |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| baseline | `Dockerfile` | 1562 MB | na | 18 s | 6 s | 24 |
| + .dockerignore | `Dockerfile.step1` | 1562 MB | 0 MB | 20 s | 6 s | 24 |
| + slim base image | `Dockerfile.step2` | 960 MB | −602 MB | 56 s | 14 s | 21 |
| + multi-stage without build-deps | `Dockerfile.step3` | 337 MB | −623 MB | 7 s | 1 s | 19 |
| + without dev and unnecessary dependencies | `Dockerfile.step4` | **179 MB** | **−158 MB** | 3 s | 2 s | 19 |

Замеры сняты скриптом `scripts/bench.sh stepN -f Dockerfile.stepN` (флаг `-f` добавлен в скрипт,
он описан в задании). Полные отчёты — в `bench-results.md`.

Про шаг «+ .dockerignore»: на чистом дереве он почти не меняет размер (0 МБ), потому что в контексте
и так нет мусора. Его ценность видна на рабочей машине: при локальном `.venv` на 109 МБ baseline
весит 1666 МБ, а тот же шаг даёт 1561 МБ (−105 МБ) — `.venv` и кеши больше не попадают в `COPY . .`.
Кроме того, `.dockerignore` защищает от случайной утечки локальных файлов в образ.

## 5. Readiness

| Критерий | Результат |
| --- | --- |
| размер ≤ 400 MB | ✓ 179 MB |
| процесс не от root | ✓ `USER 10001` |
| HEALTHCHECK | ✓ `healthy` |
| нет компилятора в runtime | ✓ нет `gcc/cc/g++/make` |
| нет dev-инструментов | ✓ нет `git/vim/wget/curl/pytest/uv` |
| нет тестов и мусора | ✓ в `/app` только `.venv` и `app` |
| нет логов, кешей, отладочных файлов и дампов | ✓ 0 `*.log`, 0 `__pycache__`, 0 `*.pyc`, `/var/log` пуст, в `/var/cache` нет файлов |
| нет секретов в ENV и истории | ✓ `ADMIN_TOKEN` — 0 совпадений |
| `.dockerignore` есть | ✓ 14 правил |
| hadolint без warning | ✓ `hadolint Dockerfile.step4` → 0/0 |
| образ обслуживает эндпоинты | ✓ `scripts/smoke.sh` → 5/5, exit 0 |

`./scripts/score.sh step4` даёт **8/9**: девятая проверка (`hadolint`) в скрипте жёстко линтует файл
`Dockerfile`, а он по условию задания не изменялся (7 warning). Сам оптимизированный файл
`Dockerfile.step4` проходит hadolint чисто — это видно в таблице выше.

### Чистота образа

```
$ docker run --rm --entrypoint sh launch-control:step4 -c '...'
pycache: 0
pyc:     0
logs:    0
/var/log:    []
/var/cache:  [adduser apt debconf ldconfig]   # только пустые каталоги, файлов: 0
dpkg old:    []
venv:        [bin lib lib64 pyvenv.cfg]
odd stdlib:  []
app:         [.venv app]
```

Что вычищено в `Dockerfile.step4`:

* в builder — байткод (`__pycache__`, `*.pyc`) и служебные файлы venv (`.lock`, `.gitignore`, `CACHEDIR.TAG`):
  это дало −7.8 МБ (186 → 179 МБ);
* в runtime — `/var/log/*`, `/var/cache/apt`, `/var/cache/debconf`, `/var/lib/apt/lists`,
  dpkg-бэкапы (`status-old`, `diversions-old`), ненужные модули stdlib (`ensurepip`, `idlelib`,
  `lib2to3`, `turtledemo`, `tkinter`, `pydoc_data`) и пустые `__pycache__` базового образа.

### Smoke

Прогнан для **каждого шага** — поведение сервиса не меняется по мере оптимизации:

| Образ | Результат | Лог |
| --- | --- | --- |
| `launch-control:baseline` | ✓ 5/5, exit 0 | `bench-artifacts/smoke-baseline.txt` |
| `launch-control:step1` | ✓ 5/5, exit 0 | `bench-artifacts/smoke-step1.txt` |
| `launch-control:step2` | ✓ 5/5, exit 0 | `bench-artifacts/smoke-step2.txt` |
| `launch-control:step3` | ✓ 5/5, exit 0 | `bench-artifacts/smoke-step3.txt` |
| `launch-control:step4` | ✓ 5/5, exit 0 | `bench-artifacts/smoke-step4.txt` |

Пример вывода (step4):

```
=== smoke: launch-control:step4 ===

  ✓ сервис поднялся и отвечает на /health
  ✓ GET /health -> 200 (получено: 200)
  ✓ GET /api/v1/launches -> 200, 5 объектов (код: 200, объектов: 5)
  ✓ POST /api/v1/launches/LC-101/countdown -> 200, t_minus_seconds=600 (код: 200, t_minus: 600)
  ✓ POST /api/v1/launches/LC-102/countdown -> 409 (получено: 409)

  Готово: 5/5
```

Скрипт возвращает `0` только при полном успехе. Проверено: успешный образ → exit 0,
образ без сервиса (`python:3.12-slim-trixie`) → exit 1.

## 6. Artifacts

| Файл | Назначение |
| --- | --- |
| `common/lesson_5/docker-lab/Dockerfile` | исходный Dockerfile (не изменялся) |
| `common/lesson_5/docker-lab/Dockerfile.step1` | baseline + `.dockerignore` |
| `common/lesson_5/docker-lab/Dockerfile.step2` | + slim base image |
| `common/lesson_5/docker-lab/Dockerfile.step3` | + multi-stage без build-deps |
| `common/lesson_5/docker-lab/Dockerfile.step4` | + без dev и неиспользуемых зависимостей (итоговый) |
| `common/lesson_5/docker-lab/.dockerignore` | контекст сборки |
| `common/lesson_5/docker-lab/scripts/smoke.sh` | smoke-проверка эндпоинтов |
| `common/lesson_5/docker-lab/scripts/bench.sh` | замеры; добавлен флаг `-f <dockerfile>` |
| `common/lesson_5/docker-lab/bench-artifacts/` | сырые логи: bench, score, hadolint, trivy, dive (baseline и step4) + smoke для baseline и всех шагов |
| `common/lesson_5/docker-lab/bench-results.md` | накопленные замеры каждого шага |



## Личные результаты.
Перед выполнением задания в opencode был добавлен skill docker-build-strategies из официальной документации Docker
Все отчеты выше были сгенериорованы агентом.
Я не смог найти 2 случая того что он предложил неверно. Насколько я понял все зависит от задания ему. Если все явно определить, то результат приемлемый.
Единственный случай, который пришлось "допилить" в запросе - это то что агент не почистил мусор, а просто констатировал что он есть и обосновал что метрика dive стала хуже потому что процентное 
  соотношение полезных данных и мусора изменилось. Был уточнен промпт с явным указанием об уделнии мусора. 

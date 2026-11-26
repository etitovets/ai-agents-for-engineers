
## baseline

| Метрика | Значение |
| --- | --- |
| Размер образа | 1562 MB |
| Холодная сборка | 18 s |
| **Тёплая пересборка** | **6 s** |
| Слоёв в истории | 24 |

Самые тяжёлые шаги:

```
7.71MB     RUN /bin/sh -c pytest -q # buildkit
291MB      RUN /bin/sh -c uv pip install --system -r pyproject.toml --extra dev #
520kB      COPY . . # buildkit
51.7MB     RUN /bin/sh -c pip install uv # buildkit
17.8MB     RUN /bin/sh -c pip install --upgrade pip # buildkit
```

## step1

| Метрика | Значение |
| --- | --- |
| Размер образа | 1562 MB |
| Холодная сборка | 20 s |
| **Тёплая пересборка** | **6 s** |
| Слоёв в истории | 24 |

Самые тяжёлые шаги:

```
7.71MB     RUN /bin/sh -c pytest -q # buildkit
291MB      RUN /bin/sh -c uv pip install --system -r pyproject.toml --extra dev #
488kB      COPY . . # buildkit
51.7MB     RUN /bin/sh -c pip install uv # buildkit
17.8MB     RUN /bin/sh -c pip install --upgrade pip # buildkit
```

## step2

| Метрика | Значение |
| --- | --- |
| Размер образа | 960 MB |
| Холодная сборка | 56 s |
| **Тёплая пересборка** | **14 s** |
| Слоёв в истории | 21 |

Самые тяжёлые шаги:

```
7.71MB     RUN /bin/sh -c pytest -q # buildkit
291MB      RUN /bin/sh -c uv pip install --system -r pyproject.toml --extra dev #
488kB      COPY . . # buildkit
51.7MB     RUN /bin/sh -c pip install uv # buildkit
17.8MB     RUN /bin/sh -c pip install --upgrade pip # buildkit
```

## step3

| Метрика | Значение |
| --- | --- |
| Размер образа | 337 MB |
| Холодная сборка | 7 s |
| **Тёплая пересборка** | **1 s** |
| Слоёв в истории | 19 |

Самые тяжёлые шаги:

```
2.69kB     COPY --chown=10001:10001 app ./app # buildkit
193MB      COPY --chown=10001:10001 /app/.venv /app/.venv # buildkit
4.35kB     RUN /bin/sh -c groupadd --gid 10001 app     && useradd --uid 10001 --g
36B        RUN /bin/sh -c set -eux;  for src in idle3 pip3 pydoc3 python3 python3
40.1MB     RUN /bin/sh -c set -eux;   savedAptMark="$(apt-mark showmanual)";  apt
```

## step4

| Метрика | Значение |
| --- | --- |
| Размер образа | 179 MB |
| Холодная сборка | 3 s |
| **Тёплая пересборка** | **2 s** |
| Слоёв в истории | 19 |

Самые тяжёлые шаги:

```
2.69kB     COPY --chown=10001:10001 app ./app # buildkit
34.7MB     COPY --chown=10001:10001 /app/.venv /app/.venv # buildkit
4.35kB     RUN /bin/sh -c groupadd --gid 10001 app     && useradd --uid 10001 --g
36B        RUN /bin/sh -c set -eux;  for src in idle3 pip3 pydoc3 python3 python3
40.1MB     RUN /bin/sh -c set -eux;   savedAptMark="$(apt-mark showmanual)";  apt
```

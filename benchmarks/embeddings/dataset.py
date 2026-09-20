"""Multilingual contextual search microbenchmark: backlog-like documents and labeled queries.

Documents are grouped in clusters of three related items. Document IDs are `<cluster><letter>`.
A query names its target documents; other documents of the same cluster count as partially
relevant (grade 1) so that nDCG rewards a model that ranks the neighbors above unrelated items.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GRADE_TARGET = 3
GRADE_SIBLING = 1


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    lang: str
    title: str
    body: str
    tags: tuple[str, ...] = ()

    @property
    def cluster(self) -> str:
        return self.id[:-1]

    @property
    def text(self) -> str:
        return "\n".join((self.title, self.body, " ".join(self.tags)))


@dataclass(frozen=True, slots=True)
class Query:
    id: str
    lang: str
    text: str
    targets: tuple[str, ...]
    kind: str
    grades: dict[str, int] = field(default_factory=dict)


D = Document
# fmt: off
DOCUMENTS: list[Document] = [
    # 01 sessions
    D("01a", "en", "Answer 404 on an unknown Mcp-Session-Id",
      "A POST or GET that names a session the store does not hold must fail with 404 and a JSON-RPC error body, so the client runs initialize again. Today the request is served as if no session existed and capabilities are lost silently.",
      ("sessions", "spec", "endpoint")),
    D("01b", "en", "Renew the session TTL on every request",
      "MemorySessionStore.get does not extend the lifetime. A session dies one hour after initialize no matter how active the client is. Add touch() to the store protocol and call it from the endpoint.",
      ("sessions", "ttl")),
    D("01c", "ru", "Хранилище сессий в Redis для нескольких нод",
      "Реализовать RedisSessionStore поверх redis-py: SET с EX для TTL, версия записи для оптимистичной блокировки, ключи с префиксом namespace. Нужно для кластера из нескольких воркеров.",
      ("sessions", "redis", "cluster")),
    # 02 search
    D("02a", "ru", "Отсекать низкоскоринговый семантический шум в поиске",
      "Когда FTS уже нашёл точные совпадения, семантические результаты с косинусом ниже 0.25 только засоряют выдачу. Поднять порог min_score при наличии FTS-совпадений и вернуть источник совпадения в каждом хите.",
      ("search", "embeddings", "ranking")),
    D("02b", "en", "Prefix matching in full-text search",
      "Queries in FTS5 should match word prefixes: 'sess' finds 'session' and 'sessions'. Append * to every token that is not quoted and escape the FTS5 operators in user input.",
      ("search", "fts5", "sqlite")),
    D("02c", "en", "Invalidate the vector index cache after external writes",
      "The in-process embedding index is rebuilt only when this process writes. A second process that edits the database leaves the first with stale vectors. Compare PRAGMA data_version and total_changes before reusing the snapshot.",
      ("search", "cache", "embeddings")),
    # 03 cli
    D("03a", "en", "Keep --project on the root parser only",
      "Subcommands must not define their own --project option. They read the value from the root parser so that `backlog --project X list` is the single spelling and the option name stays unique.",
      ("cli", "argparse", "projects")),
    D("03b", "ru", "Отключать цветовые коды Rich при выводе не в терминал",
      "Когда stdout не TTY (пайп в файл или grep), вывод содержит ANSI escape-последовательности. Создавать Console с force_terminal=False и уважать переменную NO_COLOR.",
      ("cli", "rich", "tty")),
    D("03c", "en", "Generate a TOML config file with every option documented",
      "`backlog --generate-config FILE` should write a config with one section per subcommand, the default of each key, and the allowed choices as a comment above the key. Loading the generated file back must reproduce the same values.",
      ("cli", "config", "toml")),
    # 04 auth and origins
    D("04a", "en", "Validate Bearer tokens and derive the namespace from the principal",
      "Add an Authorization middleware for Bearer tokens following RFC 9728 protected resource metadata. The identity of the principal becomes the namespace that prefixes session keys and hub topics.",
      ("auth", "oauth", "security")),
    D("04b", "en", "Wildcard patterns in allowed origins",
      "allowed_origins requires exact strings. Support `https://*.example.com` where * matches one host label and ** matches several. Escape the literal parts so that `example.com.evil.com` does not pass.",
      ("cors", "origins", "security")),
    D("04c", "de", "Origin-Prüfung an den Reverse-Proxy delegieren",
      "Hinter nginx oder Caddy mit TLS-Terminierung kommt die Anfrage als http:// an, während der Origin-Header https:// enthält. Ein Flag trust_proxy_origin_validation schaltet die eigene strenge Prüfung ab.",
      ("proxy", "origins", "deployment")),
    # 05 events
    D("05a", "en", "Resume the SSE notification stream with Last-Event-ID",
      "Clients lose every event published during a reconnect. Give each hub event a monotonically increasing id, send it in the SSE frame, and start the subscription after the id named in the Last-Event-ID header.",
      ("sse", "events", "resumption")),
    D("05b", "ru", "Pull-модель Hub с курсором вместо push-очередей",
      "Сообщения, опубликованные до подключения слушателя, терялись. Перевести Hub на опрос по курсору: position() фиксирует позицию до действия, poll(cursor, timeout) ждёт новые сообщения. Это устраняет гонку потерянного пробуждения.",
      ("hub", "events", "pubsub")),
    D("05c", "en", "PostgresHub on LISTEN/NOTIFY with a messages table",
      "Implement the hub protocol for PostgreSQL: append to a table, NOTIFY the topic channel, and wake waiting pollers. Rows older than the retention window are deleted by a periodic job.",
      ("hub", "postgres", "events")),
    # 06 release
    D("06a", "en", "Publish wheels and sdist to PyPI when a tag is pushed",
      "Add a GitHub Actions workflow that builds with uv, checks the version against the tag, and uploads with trusted publishing. Classifiers, license and project URLs must be present in pyproject.toml.",
      ("ci", "release", "pypi")),
    D("06b", "en", "Combine coverage across the Python matrix and upload to Coveralls",
      "Each matrix job produces its own coverage file. Upload them as artifacts, merge in a final job, and send the combined report. Enable FORCE_COLOR for readable pytest output in the logs.",
      ("ci", "coverage")),
    D("06c", "es", "Migrar el backend de compilación a uv_build",
      "Sustituir setuptools por uv_build 0.12 en pyproject.toml, comprobar que el paquete incluye el modelo y las licencias, y que `uv build` produce la misma rueda que antes.",
      ("build", "packaging")),
    # 07 data model
    D("07a", "en", "Soft-delete tasks instead of removing rows",
      "task_delete sets status=deleted and keeps the row. Listings and search hide deleted tasks unless the status filter names them explicitly. Notes are still deleted for good after a confirmation.",
      ("tasks", "schema", "deletion")),
    D("07b", "en", "One project on several computers with per-computer roots",
      "Move (computer, root) out of projects into project_roots. A project is a logical board; each computer binds it to a directory. An empty root denotes a contextual binding without a filesystem path.",
      ("projects", "computers", "schema")),
    D("07c", "ru", "Заметки, общие для всех проектов",
      "Флаг shared у заметки делает её видимой из любого проекта: поиск, note_list и note_get возвращают общие заметки вместе с заметками текущего проекта. Удаление общей заметки из другого проекта требует подтверждения.",
      ("notes", "sharing")),
    # 08 embeddings
    D("08a", "en", "Bundle the embedding model inside the package",
      "Ship model.safetensors, tokenizer.json and config.json under src/backlog_mcp/model so that the server never downloads anything at runtime. Record the upstream revision and file checksums in provenance.json.",
      ("embeddings", "packaging", "offline")),
    D("08b", "en", "Quantize the static embedding matrix to int8",
      "The float32 matrix is 105 MB. Symmetric int8 quantization with one scale cuts it to a quarter with a negligible change in cosine ranking. Keep normalize=True so that dot products stay cosine similarities.",
      ("embeddings", "quantization", "memory")),
    D("08c", "ru", "Бенчмарк моделей эмбеддингов для мультиязычного поиска",
      "Сравнить статические модели Model2Vec на запросах на русском и английском к одному корпусу задач: MRR, recall@5, скорость кодирования и размер на диске. Проверить варианты квантования и усечения размерности.",
      ("embeddings", "benchmark", "research")),
    # 09 documentation
    D("09a", "en", "Test the README code examples with markdown-pytest",
      "Every python block in README.md runs as a test. Mark blocks that need a database with a fixture name in the fence info string and skip blocks that are only illustrative.",
      ("docs", "tests", "readme")),
    D("09b", "en", "Rewrite code comments in Simplified Technical English",
      "Comments and docstrings follow ASD-STE100: short active sentences, one claim per sentence, one term per concept. Keep identifiers, paths and API names unchanged.",
      ("docs", "style", "comments")),
    D("09c", "ru", "Перевести руководство пользователя на русский язык",
      "Подготовить русскую версию docs/guide: установка, регистрация в Claude Code и Codex, команды CLI. Термины протокола MCP оставить на английском, примеры команд не переводить.",
      ("docs", "i18n", "russian")),
    # 10 performance
    D("10a", "en", "Fix the N+1 query pattern in task listing",
      "The list command fetches tags with one SELECT per task. Load tags for the whole page with one query using IN (...) and join them in Python. Measured 40 ms to 3 ms for 200 tasks.",
      ("performance", "sqlite", "cli")),
    D("10b", "en", "Memory grows without bound in the vector cache",
      "IndexState objects are kept per Store in a plain dict and never released when a store is closed. Use a WeakKeyDictionary so that a garbage-collected store drops its index.",
      ("performance", "memory", "cache")),
    D("10c", "fr", "Démarrage lent à cause des imports du modèle",
      "L'import de model2vec et de numpy au chargement du module ajoute 400 ms au démarrage du CLI. Importer paresseusement dans Encoder.encode et ne charger le modèle qu'à la première recherche sémantique.",
      ("performance", "startup", "imports")),
    # 11 web console
    D("11a", "en", "Editable endpoint address in the web console",
      "The endpoint path is a static <code> element. Replace it with an input, remember the value in localStorage per pathname, connect on Enter, and refuse URLs on a different origin.",
      ("console", "ui")),
    D("11b", "ru", "Рендерить Markdown в результатах инструментов в консоли",
      "Описания инструментов и текстовые ответы приходят в Markdown, а консоль показывает сырой текст. Добавить безопасный рендер без выполнения HTML и подсветку блоков кода.",
      ("console", "markdown", "ui")),
    D("11c", "en", "Dark theme for the built-in console",
      "Follow prefers-color-scheme and add a manual toggle stored in localStorage. Define the palette as CSS variables so that both themes share one stylesheet.",
      ("console", "css", "theme")),
    # 12 pagination
    D("12a", "en", "Cursor pagination for tools/list, resources/list and prompts/list",
      "Registry gets an optional page_size. The cursor is the key of the last returned item in the sorted dictionary; nextCursor is absent on the last page.",
      ("pagination", "protocol", "registry")),
    D("12b", "en", "Return Invalid params for an unknown pagination cursor",
      "A cursor that names a key no longer present must produce JSON-RPC error -32602 rather than an empty page, so that a client detects a stale cursor.",
      ("pagination", "errors")),
    D("12c", "de", "Client folgt nextCursor automatisch",
      "list_tools, list_resources und list_prompts im Client sollen alle Seiten einsammeln und eine einzige Liste zurückgeben. Eine Obergrenze für die Seitenzahl schützt vor Endlosschleifen.",
      ("pagination", "client")),
    # 13 flaky tests
    D("13a", "en", "Raise the pytest timeout to stop false failures on slow CI runners",
      "A one second timeout fails on loaded GitHub Actions runners. Set 30 seconds in pyproject.toml with a comment that explains the value.",
      ("tests", "ci", "timeout")),
    D("13b", "ru", "Гонка в тесте уведомлений: подписка открывается без начального ID",
      "Тест открывает поток уведомлений после публикации события и иногда его пропускает. Фиксировать ID события до действия и открывать подписку с after=before_id.",
      ("tests", "race", "notifications")),
    D("13c", "en", "Replace sleep in the fake Docker daemon with a live event feed",
      "The fake daemon used in the docker-mcp example waits with asyncio.sleep, which slows the suite and flakes. Push events through an asyncio.Queue that the test awaits.",
      ("tests", "docker", "examples")),
    # 14 conventions (notes)
    D("14a", "en", "Commit convention: one focused commit per task, hash appended to the task",
      "Each task is implemented in one or more small commits. Every commit hash that implements a task is appended to the task history so that the task links to its changes.",
      ("convention", "git", "workflow")),
    D("14b", "ru", "Язык общения: чат по-русски, код и комментарии по-английски",
      "В переписке пользователь общается на русском. Идентификаторы, комментарии в коде, сообщения коммитов и документация пишутся на английском.",
      ("convention", "language")),
    D("14c", "en", "No backward compatibility for the MCP tool surface",
      "Tool names, parameters and result shapes may change between releases. Clients pick up the new surface through tools/list_changed. Do not keep deprecated aliases or shims.",
      ("convention", "mcp", "compatibility")),
    # 15 deployment
    D("15a", "en", "Multi-arch Docker image for amd64 and arm64",
      "Build with buildx from a python:3.14-slim base, install with uv, run as a non-root user, and publish to GHCR on every release tag.",
      ("deployment", "docker")),
    D("15b", "ru", "systemd unit для HTTP-сервера",
      "Описать сервис с Restart=on-failure, переменными окружения из EnvironmentFile, ограничением памяти и запуском от отдельного пользователя. Логи в journald.",
      ("deployment", "systemd", "linux")),
    D("15c", "en", "Health check endpoint for load balancers",
      "GET /healthz returns 200 when the database opens and the model loads, 503 otherwise. Keep it unauthenticated and cheap; do not touch the hub.",
      ("deployment", "monitoring", "http")),
    # 16 errors and logging
    D("16a", "en", "Map failure kinds to JSON-RPC error codes per protocol revision",
      "FailureKind values map to -32600 to -32603 and to server-defined codes such as -32001 for a missing session. Each adapter may override the table for its revision.",
      ("errors", "jsonrpc", "protocol")),
    D("16b", "en", "Report validation errors with the path of the offending field",
      "Pydantic errors are flattened into 'arguments.limit: must be >= 1'. Include every error, not only the first, in the data field of the JSON-RPC error.",
      ("errors", "validation", "pydantic")),
    D("16c", "es", "Nivel de registro por sesión",
      "El cliente envía logging/setLevel y el servidor guarda el nivel en la sesión. Las notificaciones de log por debajo de ese nivel no se emiten a esa sesión.",
      ("logging", "sessions", "protocol")),
    # 17 security hardening
    D("17a", "en", "Protect the skills loader from symlink attacks",
      "Skills.from_directory follows symbolic links outside the skills directory. Resolve every path, refuse links that escape the root, and cap the walk at 512 files and 16 MB.",
      ("security", "skills", "filesystem")),
    D("17b", "ru", "Запретить выход за корень при чтении файловых ресурсов",
      "URI вида file:///../../etc/passwd после нормализации указывает вне разрешённого каталога. Проверять, что resolved-путь начинается с корня, и отвечать ошибкой доступа.",
      ("security", "path-traversal", "resources")),
    D("17c", "en", "Rate limit tool calls per principal",
      "A token bucket per authenticated principal, configurable rate and burst, answers 429 with Retry-After when exhausted. Unauthenticated requests share one bucket keyed by client IP.",
      ("security", "rate-limit", "http")),
    # 18 migrations
    D("18a", "en", "Schema migration that moves computer and root into project_roots",
      "Create project_roots(project_id, computer, root), copy every existing pair, then drop the two columns from projects. Run inside one transaction and bump user_version.",
      ("migration", "schema", "sqlite")),
    D("18b", "ru", "Импорт задач из старого JSON-экспорта",
      "Команда import читает файл со списком задач, сохраняет исходные даты создания и закрытия, сопоставляет проекты по slug и пропускает дубликаты по заголовку.",
      ("migration", "import", "cli")),
    D("18c", "en", "Back up the database before running migrations",
      "Copy backlog.sqlite3 to backlog.sqlite3.bak-<user_version> with the SQLite backup API before any schema change. Keep the last three backups.",
      ("migration", "backup", "sqlite")),
    # 19 non-engineering
    D("19a", "en", "Design a logo for the project",
      "A simple mark that works at 16 px as a favicon and in the README header. Monochrome variant required. Provide SVG and PNG.",
      ("design", "branding")),
    D("19b", "ru", "Написать пост в блог о релизе 0.3",
      "Рассказать о пагинации, возобновлении SSE и семантическом поиске. Целевая аудитория: разработчики MCP-серверов. Объём около 800 слов, с примерами кода.",
      ("writing", "blog", "release")),
    D("19c", "en", "Submit a conference talk proposal on protocol version negotiation",
      "Abstract of 300 words: how one server speaks five revisions of the MCP protocol through adapters, and what broke along the way. Deadline is the end of the month.",
      ("writing", "conference", "talk")),
    # 20 operations
    D("20a", "en", "Rotate the API keys used by the deployment",
      "Generate new keys, deploy them through the secret store, keep the old keys valid for 24 hours, then revoke them. Document the procedure in the runbook.",
      ("operations", "secrets", "runbook")),
    D("20b", "ru", "Алерт при заполнении диска на сервере",
      "Мониторинг должен предупреждать при 80% заполнения раздела с базой и логами и поднимать критический алерт при 95%. Проверить ротацию логов journald.",
      ("operations", "monitoring", "disk")),
    D("20c", "en", "Timestamps drift by one hour after daylight saving time change",
      "created_at is stored as local time without a zone. Store UTC epoch seconds and convert on output; migrate existing rows using the computer's zone at write time.",
      ("bug", "timezone", "timestamps")),
]

Q = lambda targets, lang, text, kind: (targets, lang, text, kind)  # noqa: E731
QUERIES_RAW: list[tuple[str, str, str, str]] = [
    # 01a
    Q("01a", "ru", "неизвестный идентификатор сессии должен возвращать 404", "cross"),
    Q("01a", "en", "what happens when the client sends a session id the server has forgotten", "problem"),
    Q("01a", "de", "Antwort 404 bei unbekannter Session-ID", "cross"),
    # 01b
    Q("01b", "ru", "сессия истекает через час даже при активном использовании", "cross"),
    Q("01b", "en", "extend session lifetime when the client keeps making requests", "paraphrase"),
    # 01c
    Q("01c", "en", "store sessions in Redis so several workers share them", "cross"),
    Q("01c", "ru", "сессии в редисе для кластера", "paraphrase"),
    Q("01c", "fr", "stockage des sessions dans Redis pour plusieurs nœuds", "cross"),
    # 02a
    Q("02a", "en", "semantic results with low cosine scores pollute the search output when keyword matches exist", "cross"),
    Q("02a", "ru", "поднять порог похожести, если полнотекстовый поиск уже что-то нашёл", "paraphrase"),
    # 02b
    Q("02b", "ru", "поиск по началу слова в полнотекстовом индексе", "cross"),
    Q("02b", "en", "typing part of a word should still find the task", "problem"),
    # 02c
    Q("02c", "ru", "устаревшие векторы после записи из другого процесса", "cross"),
    Q("02c", "en", "embedding cache not refreshed when another process changes the database", "paraphrase"),
    Q("02c", "es", "el índice de vectores no se actualiza tras cambios externos", "cross"),
    # 03a
    Q("03a", "ru", "опция --project только у корневого парсера", "cross"),
    Q("03a", "en", "where should the project option live in the command line parser", "problem"),
    # 03b
    Q("03b", "en", "strip ANSI color codes when output is piped to a file", "cross"),
    Q("03b", "ru", "мусорные escape-последовательности в выводе при перенаправлении в файл", "problem"),
    Q("03b", "de", "Farbcodes abschalten, wenn die Ausgabe kein Terminal ist", "cross"),
    # 03c
    Q("03c", "ru", "сгенерировать файл конфигурации TOML со всеми опциями", "cross"),
    Q("03c", "en", "write out a config template with defaults and allowed values", "paraphrase"),
    # 04a
    Q("04a", "ru", "проверка Bearer токена и namespace из идентичности пользователя", "cross"),
    Q("04a", "en", "authenticate requests with OAuth tokens", "paraphrase"),
    # 04b
    Q("04b", "ru", "маски с звёздочкой для разрешённых origin", "cross"),
    Q("04b", "en", "allow every subdomain in CORS origins without listing each one", "problem"),
    # 04c
    Q("04c", "en", "origin check fails behind a TLS terminating reverse proxy", "cross"),
    Q("04c", "ru", "за nginx запросы приходят по http и проверка Origin отклоняет их", "cross"),
    # 05a
    Q("05a", "ru", "возобновление потока событий после переподключения по Last-Event-ID", "cross"),
    Q("05a", "en", "client misses notifications while it reconnects", "problem"),
    # 05b
    Q("05b", "en", "poll the hub by cursor instead of pushing to per-stream queues", "cross"),
    Q("05b", "ru", "сообщения теряются, если опубликованы до подключения слушателя", "problem"),
    Q("05b", "de", "Hub-Nachrichten per Cursor abfragen statt Push-Warteschlangen", "cross"),
    # 05c
    Q("05c", "ru", "хаб событий на PostgreSQL через LISTEN и NOTIFY", "cross"),
    Q("05c", "en", "event bus backed by a Postgres table", "paraphrase"),
    # 06a
    Q("06a", "ru", "публикация пакета в PyPI по тегу через GitHub Actions", "cross"),
    Q("06a", "en", "automate the release upload when a version tag is pushed", "paraphrase"),
    # 06b
    Q("06b", "ru", "объединить покрытие тестов со всех версий Python и отправить в Coveralls", "cross"),
    Q("06b", "en", "merge coverage reports from matrix jobs", "paraphrase"),
    # 06c
    Q("06c", "en", "switch the build backend to uv_build", "cross"),
    Q("06c", "ru", "заменить setuptools на uv_build", "cross"),
    # 07a
    Q("07a", "ru", "мягкое удаление задач со статусом deleted", "cross"),
    Q("07a", "en", "deleted tasks should be hidden but recoverable", "problem"),
    # 07b
    Q("07b", "ru", "один проект на нескольких компьютерах с разными корневыми директориями", "cross"),
    Q("07b", "en", "bind the same board to different directories on each machine", "paraphrase"),
    # 07c
    Q("07c", "en", "notes visible from every project", "cross"),
    Q("07c", "ru", "общие заметки", "keyword"),
    Q("07c", "fr", "notes partagées entre tous les projets", "cross"),
    # 08a
    Q("08a", "ru", "положить файлы модели внутрь пакета, чтобы ничего не скачивать при запуске", "cross"),
    Q("08a", "en", "the server must work offline without downloading the model", "problem"),
    # 08b
    Q("08b", "ru", "квантование матрицы эмбеддингов в int8 для экономии памяти", "cross"),
    Q("08b", "en", "shrink the embedding weights four times", "problem"),
    # 08c
    Q("08c", "en", "compare embedding models on Russian and English queries", "cross"),
    Q("08c", "ru", "какая модель эмбеддингов лучше для мультиязычного поиска", "problem"),
    Q("08c", "de", "Vergleich von Embedding-Modellen für mehrsprachige Suche", "cross"),
    # 09a
    Q("09a", "ru", "запускать примеры кода из README как тесты", "cross"),
    Q("09a", "en", "make sure the README snippets still run", "problem"),
    # 09b
    Q("09b", "ru", "переписать комментарии в коде по стандарту упрощённого технического английского", "cross"),
    Q("09b", "en", "comment style guide ASD-STE100", "keyword"),
    # 09c
    Q("09c", "en", "translate the user guide into Russian", "cross"),
    Q("09c", "ru", "русская документация", "keyword"),
    # 10a
    Q("10a", "ru", "один запрос к базе на каждую задачу при выводе списка", "cross"),
    Q("10a", "en", "listing is slow because tags are loaded one task at a time", "problem"),
    # 10b
    Q("10b", "ru", "утечка памяти в кэше векторного индекса", "cross"),
    Q("10b", "en", "index state is never freed when the store closes", "paraphrase"),
    # 10c
    Q("10c", "en", "CLI startup is slow because heavy modules are imported eagerly", "cross"),
    Q("10c", "ru", "ленивый импорт numpy и model2vec для ускорения запуска", "cross"),
    # 11a
    Q("11a", "ru", "редактируемое поле адреса эндпоинта в веб-консоли", "cross"),
    Q("11a", "en", "let the user change the endpoint URL in the console", "paraphrase"),
    # 11b
    Q("11b", "en", "render markdown in tool results in the web console", "cross"),
    Q("11b", "ru", "консоль показывает сырой markdown вместо форматированного текста", "problem"),
    # 11c
    Q("11c", "ru", "тёмная тема для консоли", "cross"),
    Q("11c", "en", "dark mode toggle following the system color scheme", "paraphrase"),
    # 12a
    Q("12a", "ru", "постраничная выдача списка инструментов с курсором", "cross"),
    Q("12a", "en", "large tool lists overwhelm the client context; split them into pages", "problem"),
    # 12b
    Q("12b", "ru", "ошибка Invalid params на несуществующий курсор", "cross"),
    Q("12b", "en", "stale cursor should raise an error rather than return nothing", "paraphrase"),
    # 12c
    Q("12c", "en", "client walks through all pages automatically", "cross"),
    Q("12c", "ru", "клиент сам обходит все страницы по nextCursor", "cross"),
    # 13a
    Q("13a", "ru", "таймаут тестов слишком маленький для медленных раннеров CI", "cross"),
    Q("13a", "en", "tests fail randomly on GitHub Actions because of a short timeout", "problem"),
    # 13b
    Q("13b", "en", "race condition in the notification test: subscription opened without a start id", "cross"),
    Q("13b", "ru", "тест уведомлений иногда пропускает событие", "problem"),
    # 13c
    Q("13c", "ru", "убрать sleep из фейкового демона Docker в тестах", "cross"),
    Q("13c", "en", "fake docker daemon slows down the test suite", "problem"),
    # 14a
    Q("14a", "ru", "правило про коммиты: один коммит на задачу и хэш в истории задачи", "cross"),
    Q("14a", "en", "how should commits be linked to tasks", "problem"),
    # 14b
    Q("14b", "en", "which language to use in chat versus code", "cross"),
    Q("14b", "ru", "на каком языке писать комментарии в коде", "problem"),
    # 14c
    Q("14c", "ru", "обратная совместимость инструментов MCP не требуется", "cross"),
    Q("14c", "en", "can tool signatures change between releases", "problem"),
    # 15a
    Q("15a", "ru", "докер-образ для amd64 и arm64", "cross"),
    Q("15a", "en", "container image published to GHCR on release", "paraphrase"),
    # 15b
    Q("15b", "en", "run the HTTP server as a systemd service", "cross"),
    Q("15b", "ru", "юнит systemd с перезапуском при падении", "paraphrase"),
    Q("15b", "de", "systemd-Dienst für den HTTP-Server", "cross"),
    # 15c
    Q("15c", "ru", "эндпоинт проверки здоровья для балансировщика", "cross"),
    Q("15c", "en", "liveness probe endpoint", "paraphrase"),
    # 16a
    Q("16a", "ru", "соответствие видов ошибок кодам JSON-RPC", "cross"),
    Q("16a", "en", "which error code does a missing session map to", "problem"),
    # 16b
    Q("16b", "ru", "ошибки валидации с путём до поля", "cross"),
    Q("16b", "en", "show all pydantic validation errors with field names", "paraphrase"),
    # 16c
    Q("16c", "en", "per-session logging level set by the client", "cross"),
    Q("16c", "ru", "уровень логирования на сессию", "cross"),
    # 17a
    Q("17a", "ru", "защита загрузчика навыков от символических ссылок наружу", "cross"),
    Q("17a", "en", "skills directory walk follows symlinks outside the root", "paraphrase"),
    # 17b
    Q("17b", "en", "path traversal in file resources", "cross"),
    Q("17b", "ru", "чтение файлов за пределами разрешённого каталога через ../", "paraphrase"),
    # 17c
    Q("17c", "ru", "ограничение частоты вызовов инструментов на пользователя", "cross"),
    Q("17c", "en", "throttle clients with a token bucket and answer 429", "paraphrase"),
    # 18a
    Q("18a", "ru", "миграция схемы: перенести computer и root в отдельную таблицу", "cross"),
    Q("18a", "en", "move project roots into their own table", "paraphrase"),
    # 18b
    Q("18b", "en", "import tasks from an old JSON export keeping original dates", "cross"),
    Q("18b", "ru", "импорт задач из json", "keyword"),
    # 18c
    Q("18c", "ru", "резервная копия базы перед миграцией", "cross"),
    Q("18c", "en", "snapshot the sqlite file before changing the schema", "paraphrase"),
    # 19a
    Q("19a", "ru", "нарисовать логотип проекта и favicon", "cross"),
    Q("19a", "en", "project branding and icon", "paraphrase"),
    # 19b
    Q("19b", "en", "write a blog post announcing the release", "cross"),
    Q("19b", "ru", "статья о новых возможностях релиза", "paraphrase"),
    # 19c
    Q("19c", "ru", "подать заявку на доклад на конференцию", "cross"),
    Q("19c", "en", "conference abstract about supporting several protocol revisions", "paraphrase"),
    # 20a
    Q("20a", "ru", "ротация API-ключей", "cross"),
    Q("20a", "en", "replace the deployment secrets without downtime", "problem"),
    # 20b
    Q("20b", "en", "alert when the disk is almost full", "cross"),
    Q("20b", "ru", "мониторинг свободного места на диске", "paraphrase"),
    # 20c
    Q("20c", "ru", "время сдвигается на час после перехода на летнее время", "cross"),
    Q("20c", "en", "store timestamps in UTC instead of local time", "paraphrase"),
    Q("20c", "es", "las marcas de tiempo cambian una hora tras el horario de verano", "cross"),
    # multi-target queries
    Q("01a,01b", "en", "session handling deviates from the Streamable HTTP spec", "multi"),
    Q("02a,02c", "ru", "проблемы семантического поиска по эмбеддингам", "multi"),
    Q("04b,04c", "ru", "проверка заголовка Origin", "multi"),
    Q("05a,05b", "en", "events lost on reconnect or before subscribing", "multi"),
    Q("08a,08b", "en", "model2vec model shipping and size", "multi"),
    Q("17a,17b", "en", "filesystem escapes outside an allowed directory", "multi"),
    Q("18a,18c", "ru", "безопасное изменение схемы базы данных", "multi"),
    Q("13a,13b,13c", "en", "flaky tests", "multi"),
]
# fmt: on


def build_queries() -> list[Query]:
    by_id = {doc.id: doc for doc in DOCUMENTS}
    queries: list[Query] = []
    for number, (targets, lang, text, kind) in enumerate(QUERIES_RAW, start=1):
        ids = tuple(targets.split(","))
        grades: dict[str, int] = {}
        for target in ids:
            cluster = by_id[target].cluster
            for doc in DOCUMENTS:
                if doc.cluster == cluster and doc.id not in ids:
                    grades[doc.id] = GRADE_SIBLING
        for target in ids:
            grades[target] = GRADE_TARGET
        queries.append(Query(f"q{number:03d}", lang, text, ids, kind, grades))
    return queries


QUERIES: list[Query] = build_queries()

__all__ = ["DOCUMENTS", "GRADE_SIBLING", "GRADE_TARGET", "QUERIES", "Document", "Query"]
